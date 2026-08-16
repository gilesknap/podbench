"""Tests for installing debugpy into the target from the seat.

Two things are pinned. *The command*, because it is the whole finding: uv is
asked to resolve for the **target's** version and not for the interpreter it is
running on, and a copy of the seat's own tree — the obvious alternative — ships
the seat's accelerators and degrades pydevd to pure Python without a word.

*The refusals*, because the one genuinely new precondition is a writable rootfs
and `readOnlyRootFilesystem: true` cannot be read from here at all: it lives in
the target's mount namespace and arrives as an errno on the write.

Nothing here touches a cluster or a network: the destination is a synthetic
rootfs under ``tmp_path`` and uv is an injected runner that is never really run.
"""

from __future__ import annotations

import errno
from collections.abc import Sequence
from pathlib import Path

from podbench.kubectl import CommandResult
from podbench.provision import (
    CAVEATS,
    blocker_sentence,
    provision_debugpy,
    writable_blocker,
)

PID = 597


class FakeUv:
    """A uv that records its argv and reports what it was told to report."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.argv: list[str] = []

    def __call__(
        self, argv: Sequence[str], *, stdin: str | None = None, capture: bool = True
    ) -> CommandResult:
        self.argv = list(argv)
        return CommandResult(tuple(argv), self.returncode, "", self.stderr)


def test_uv_resolves_for_the_targets_version_not_the_seats(tmp_path: Path) -> None:
    """The bench ran a 3.11 seat against a 3.12 target and the wheel matched.

    Without ``--python-version`` uv resolves for the interpreter it is running
    on, the target loads a wheel built for another one, and pydevd falls back to
    pure Python silently — the failure this whole module is shaped around.
    """
    uv = FakeUv()
    result = provision_debugpy(PID, python_version="3.12", proc=tmp_path, runner=uv)
    assert result.ok
    assert uv.argv[:5] == ["uv", "pip", "install", "--python-version", "3.12"]
    assert uv.argv[5:] == [
        "--target",
        str(tmp_path / str(PID) / "root" / "opt" / "podbench-debugpy"),
        "debugpy",
    ]
    assert result.path == f"/proc/{PID}/root/opt/podbench-debugpy"


def test_the_three_costs_are_printed_with_the_install(tmp_path: Path) -> None:
    """None of them announces itself later: no egress looks like a resolver
    error, a restart looks like the debugger stopping, and running the pod out
    of ephemeral storage evicts the *workload*."""
    result = provision_debugpy(
        PID, python_version="3.12", proc=tmp_path, runner=FakeUv()
    )
    assert all(caveat in result.messages for caveat in CAVEATS)


def test_a_chosen_destination_is_where_it_goes(tmp_path: Path) -> None:
    """Read-only rootfs is common, and a writable emptyDir is the usual escape."""
    uv = FakeUv()
    result = provision_debugpy(
        PID, python_version="3.12", dest="/tmp/dbg", proc=tmp_path, runner=uv
    )
    assert result.path == f"/proc/{PID}/root/tmp/dbg"
    assert str(tmp_path / str(PID) / "root" / "tmp" / "dbg") in uv.argv


def test_a_failed_install_names_the_no_egress_case_and_the_fallback(
    tmp_path: Path,
) -> None:
    """uv's own error is quoted, and the commonest cause of it is named.

    A locked-down namespace is the expected failure here, and the fallback that
    works without an index is worth stating in the same breath as its cost.
    """
    uv = FakeUv(returncode=2, stderr="error: failed to fetch")
    result = provision_debugpy(PID, python_version="3.12", proc=tmp_path, runner=uv)
    assert not result.ok
    joined = " ".join(result.messages)
    assert "failed to fetch" in joined
    assert "no-egress" in joined
    assert "copy this seat's own debugpy tree" in joined


def test_a_read_only_rootfs_is_named_rather_than_guessed_at() -> None:
    """The mount flag is in the *target's* namespace, so EROFS is all there is.

    Naming it is the difference between a one-line fix and an afternoon: uid 0
    plus CAP_DAC_OVERRIDE means the target's own uid and modes cannot be the
    explanation, so nothing else in the pod is worth checking.
    """
    sentence = blocker_sentence(OSError(errno.EROFS, "ro"), Path("/proc/1/root/opt"))
    assert "readOnlyRootFilesystem: true" in sentence
    assert "--provision-dest" in sentence


def test_a_full_filesystem_is_not_blamed_on_the_pods_storage_limit() -> None:
    """Two disjoint mechanisms, and only one of them produces an errno.

    ``ENOSPC`` is the filesystem under the destination. The pod's
    ephemeral-storage limit is polled by the kubelet and arrives as an eviction
    with no errno at all, so naming it here would send the reader to the wrong
    `describe` for a full node disk.
    """
    sentence = blocker_sentence(OSError(errno.ENOSPC, "full"), Path("/proc/1/root"))
    assert "the node's disk" in sentence
    assert "eviction rather than as an errno" in sentence


def test_a_denied_write_names_the_causes_dac_override_does_not_cover() -> None:
    """Uid 0 rules out the target's modes, and nothing else.

    Report 3.11 measured a root seat with no CAP_SYS_PTRACE being refused even
    `ls /proc/<pid>/root`, and R8 leaves a custom AppArmor profile as the
    unvalidated case - both survive CAP_DAC_OVERRIDE, and the old sentence
    steered the reader away from them.
    """
    sentence = blocker_sentence(OSError(errno.EACCES, "denied"), Path("/proc/1/root"))
    assert "PTRACE_MODE_READ" in sentence
    assert "AppArmor" in sentence


def test_the_probe_writes_rather_than_asking_the_kernel_about_modes(
    tmp_path: Path,
) -> None:
    """``os.access`` reports on uid and modes, which CAP_DAC_OVERRIDE overrules.

    So writability is measured by writing — and the probe is cleaned up, since a
    file left in the workload's rootfs is a mutation nobody asked for.
    """
    destination = tmp_path / "opt" / "podbench-debugpy"
    assert writable_blocker(destination) is None
    assert list(destination.iterdir()) == []


def test_an_unwritable_destination_is_reported_not_raised(tmp_path: Path) -> None:
    """Any errno at all has to become a sentence: this verb authors a file and
    must not traceback over the workload's filesystem layout."""
    collision = tmp_path / "opt"
    collision.write_text("not a directory")
    blocker = writable_blocker(collision / "podbench-debugpy")
    assert blocker is not None
    assert str(collision / "podbench-debugpy") in blocker
