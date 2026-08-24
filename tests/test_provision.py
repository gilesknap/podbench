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
import os
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from podbench.dap import HANDSHAKE_TIMEOUT_SECONDS, Answer, Handshake
from podbench.kubectl import CommandResult
from podbench.proc import Capabilities, Credentials
from podbench.provision import (
    CAVEATS,
    INJECTION_TIMEOUT_SECONDS,
    blocker_sentence,
    inject_debugpy,
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
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
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
    assert uv.argv[:6] == [
        "uv",
        "pip",
        "install",
        "--no-cache",
        "--python-version",
        "3.12",
    ]
    assert uv.argv[6:] == [
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


def test_a_denied_write_on_a_seat_that_is_not_root_names_the_ownership() -> None:
    """The `degraded` rung, where the sentence above excludes the true cause.

    Measured on a beamline pod (2026-08-24): the seat is uid 37887 with `CapEff
    0000000000000000`, `/opt` in the target is `drwxr-xr-x root root`, and `ls
    /proc/13/root` *succeeded* - so the traversal and every LSM are ruled out
    and file modes are the whole of it. A message that sends that reader to
    SELinux has spent their afternoon.
    """
    seat = Credentials(37887, 37887, Capabilities(0, 0, 0))
    sentence = blocker_sentence(
        OSError(errno.EACCES, "denied"),
        Path("/proc/13/root/opt/podbench-debugpy"),
        credentials=seat,
    )

    assert "uid 37887" in sentence
    assert "ownership and modes" in sentence
    # None of the three the root-seat sentence offers, because none of them is
    # available to a capability-less uid: they would each be a mechanism to
    # check instead of the one that refused.
    assert "CAP_DAC_OVERRIDE" not in sentence
    assert "SELinux" not in sentence and "AppArmor" not in sentence


def test_a_root_seat_is_still_told_its_modes_are_not_the_explanation() -> None:
    """The other rung, unchanged: at uid 0 `CAP_DAC_OVERRIDE` really does make
    the target's modes irrelevant, so naming them would be the same mistake in
    the other direction."""
    root = Credentials(0, 0, Capabilities(0, 0, 0))
    sentence = blocker_sentence(
        OSError(errno.EACCES, "denied"), Path("/proc/1/root"), credentials=root
    )

    assert "CAP_DAC_OVERRIDE" in sentence
    assert "PTRACE_MODE_READ" in sentence


@pytest.mark.skipif(
    os.geteuid() == 0, reason="uid 0 ignores the modes this test refuses with"
)
def test_the_refusal_reads_the_seats_own_credentials_from_the_same_proc(
    tmp_path: Path,
) -> None:
    """End to end, because the plumbing is the defect: the sentence was composed
    without ever asking what this container is running as, and the answer is one
    `/proc/self/status` away in the seat that is composing it."""
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "status").write_text(
        "Name:\tpodbench\nUid:\t37887\t37887\t37887\t37887\n"
        "Gid:\t37887\t37887\t37887\t37887\nCapEff:\t0000000000000000\n"
    )
    unwritable = tmp_path / str(PID) / "root" / "opt"
    unwritable.mkdir(parents=True)
    unwritable.chmod(0o555)
    uv = FakeUv()
    try:
        result = provision_debugpy(PID, python_version="3.12", proc=tmp_path, runner=uv)
    finally:
        unwritable.chmod(0o755)

    assert not result.ok
    assert uv.argv == [], "the probe refuses before uv is asked to resolve"
    assert "uid 37887" in " ".join(result.messages)


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


# -- the injection's own pause ----------------------------------------------

INJECTION = "PYTHONPATH=/proc/597/root/dbg \\\n  /app/.venv/bin/python -m debugpy"


def answering(outcome: Handshake, detail: str = "detail") -> Callable[[int], Answer]:
    """A handshake seam that says what the test needs it to say.

    Injected rather than served: what these assert on is the *sentence* each
    outcome produces, and a real socket per case would make the failure of one
    of them a networking question.
    """
    return lambda _port: Answer(outcome, detail, 0.2)


class FakeShell:
    """The shell the injection runs in, with a returncode chosen by the test."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.argv: list[str] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        self.argv = list(argv)
        return CommandResult(tuple(argv), self.returncode, "", self.stderr)


def test_the_injection_is_bounded_and_the_command_is_still_verbatim() -> None:
    """Issue #76: the attach stops the app and ran to whatever end gdb reached.

    The bound is ``timeout``'s rather than ``subprocess``'s because the driver
    forks gdb: killing our own ``sh`` would leave that gdb holding the workload.
    What the bound may not do is edit the command, which is the one string that
    has to stay character for character what the seat printed.
    """
    shell = FakeShell()
    injected = inject_debugpy(
        INJECTION,
        runner=shell,
        port=5678,
        clock=lambda: 0.0,
        prove=answering(Handshake.ANSWERED),
    )

    assert injected.ok
    assert shell.argv[0] == "timeout"
    assert str(INJECTION_TIMEOUT_SECONDS) in shell.argv
    assert shell.argv[-3:] == ["sh", "-c", INJECTION]


def test_a_timed_out_injection_names_the_bound_that_stopped_it() -> None:
    """The defect was a duration podbench measured and did not control.

    124 is ``timeout``'s own code and cannot come from the driver, so the
    message may say what happened rather than relaying the last line of a gdb
    that was killed mid-sentence.
    """
    shell = FakeShell(returncode=124)
    injected = inject_debugpy(INJECTION, runner=shell, port=5678, clock=lambda: 0.0)

    assert not injected.ok
    assert f"within {INJECTION_TIMEOUT_SECONDS}s" in injected.messages[0]
    assert "resumes" in injected.messages[1]


def test_a_missing_timeout_binary_is_reported_rather_than_raised() -> None:
    """``debug-config`` authors a file and may not traceback at somebody. The
    workload was never touched here, and the message has to say so."""

    def missing(
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    injected = inject_debugpy(INJECTION, runner=missing, port=5678, clock=lambda: 0.0)

    assert not injected.ok
    assert "not stopped" in injected.messages[0]


def test_an_answered_initialize_is_what_the_success_line_asserts() -> None:
    """The success line used to be returned on the injector's exit code, and on
    the live target that code was 0 while no session could start. What it now
    says is the thing the redo could not falsify: a DAP `initialize` that got an
    answer."""
    injected = inject_debugpy(
        INJECTION,
        runner=FakeShell(),
        port=37189,
        clock=lambda: 0.0,
        prove=answering(Handshake.ANSWERED),
    )

    assert injected.ok
    assert injected.proved
    said = injected.messages[0]
    assert "injected in" in said
    assert "initialize" in said
    assert "debuggable" in said


def test_an_open_port_that_never_answers_is_not_reported_as_success() -> None:
    """**The live target's own state**, and the test that matters most here.

    The injector exited 0 and the port was genuinely open and in LISTEN, so
    everything podbench used to measure was satisfied. The line has to name the
    half that is established - the injection ran, the port is open - and the half
    that is not, which is a session of any kind.
    """
    injected = inject_debugpy(
        INJECTION,
        runner=FakeShell(),
        port=37189,
        clock=lambda: 0.0,
        prove=answering(Handshake.SILENT, "nothing arrived in 5s"),
    )

    assert not injected.proved
    said = injected.messages[0]
    assert "injected in" in said
    assert "accepts a connection" in said
    assert "no debug session could be started" in said
    assert f"within {HANDSHAKE_TIMEOUT_SECONDS:.0f}s" in said
    # And it must not read as the success it replaced.
    assert "debuggable" not in said


def test_a_refused_connection_is_told_apart_from_a_silent_one() -> None:
    """Different halves to chase, so different sentences: here the injector
    returned 0 and left nothing listening at all, which is a closed port under
    the configuration rather than a wedged adapter behind an open one."""
    injected = inject_debugpy(
        INJECTION,
        runner=FakeShell(),
        port=37189,
        clock=lambda: 0.0,
        prove=answering(Handshake.REFUSED, "Connection refused"),
    )

    assert not injected.proved
    said = injected.messages[0]
    assert "nothing is listening" in said
    assert "Connection refused" in said
    assert "accepts a connection" not in said


def test_a_peer_that_is_not_an_adapter_names_the_port_rather_than_the_session() -> None:
    """Under `hostNetwork` the port is the node's, so something else answering
    is a real case - and the remedy is a different port, not a debugpy log."""
    injected = inject_debugpy(
        INJECTION,
        runner=FakeShell(),
        port=37189,
        clock=lambda: 0.0,
        prove=answering(Handshake.REJECTED, "not DAP framing"),
    )

    assert not injected.proved
    assert "`--port`" in injected.messages[0]


def test_a_failed_injection_asks_the_adapter_nothing() -> None:
    """There is no server to ask, and a refusal invented here would read as a
    second failure rather than as the absence of a question. The injector's own
    failure path is untouched by any of this."""

    def unreachable(_port: int) -> Answer:
        raise AssertionError("the handshake must not run when the injector failed")

    injected = inject_debugpy(
        INJECTION,
        runner=FakeShell(returncode=1),
        port=37189,
        clock=lambda: 0.0,
        prove=unreachable,
    )

    assert not injected.ok
    assert injected.session is None
    assert not injected.proved
    assert "injection exited 1" in injected.messages[0]
