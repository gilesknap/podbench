"""S3 as a regression: gdb against a target that cannot help itself.

The spike's finding worth protecting is not "gdb works". It is that four
things have to be true *in one order* before a backtrace is trustworthy, and
that getting the order wrong produces a plausible-looking wrong answer rather
than an error: ``set sysroot`` after ``attach`` leaves libc frames correct and
everything above them as ``?? ()``, and a matched-distro debug image hides the
whole class of bug (report §3.3, §3.4).

So this file asserts on the ordering of the generated command file as well as
on the result, and it runs against ``gcr.io/distroless/cc-debian12`` — proven
here to have no shell — so that nothing in the target can be quietly doing the
work.

Yama, seccomp and AppArmor differ per node and per kernel flavour (report
§3.13: two arm64 nodes on this cluster disagree with each other), so "the
cluster will not grant SYS_PTRACE" is a skip with the probe's own diagnosis
attached, never a failure.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from podbench.kubectl import Kubectl
from podbench.launcher import Session, attach
from podbench.model import Rung, Verdict

from .conftest import KubectlCli

VICTIM_POD = "victim"
VICTIM_CONTAINER = "victim"

READY_TIMEOUT = 420.0
"""The initContainer runs ``apt-get install gcc`` before the target starts, on
top of two image pulls."""


@pytest.fixture(scope="module")
def victim(kubectl: KubectlCli, apps_dir: Path) -> str:
    """The distroless C target from ``apps/distroless-c.yaml``, running."""
    kubectl.apply(apps_dir / "distroless-c.yaml")
    kubectl.wait(f"pod/{VICTIM_POD}", "condition=Ready", timeout=READY_TIMEOUT)
    return VICTIM_POD


@pytest.fixture(scope="module")
def seat(kube: Kubectl, victim: str, podbench_image: str) -> Session:
    """A podbench container attached to the victim, with its probe run.

    The library entry point rather than the CLI: this test asserts on the
    capability report's *structure* — which rung landed, which mechanism
    refused — and parsing that back out of the formatted banner would be
    testing the formatter.
    """
    return attach(kube, victim, target=VICTIM_CONTAINER, image=podbench_image)


def test_the_target_really_is_distroless(kubectl: KubectlCli, victim: str) -> None:
    """No shell in the target container.

    Cheap, and it guards the premise of everything below: if someone swaps the
    runtime image for a debian one to make the manifest simpler, every other
    test in this file keeps passing while testing the easy case.
    """
    result = kubectl.exec(
        victim, ["/bin/sh", "-c", "true"], container=VICTIM_CONTAINER, check=False
    )
    assert result.returncode != 0, (
        "the target container has a working /bin/sh, so it is not distroless "
        "and this file is no longer testing what it claims to"
    )


def test_generated_commands_keep_the_load_bearing_order(
    kubectl: KubectlCli, victim: str, seat: Session
) -> None:
    """``dbg --dry-run`` emits report §4.3's sequence, in its one working order.

    Run *in the pod*, so this also covers the pid discovery it depends on:
    ``dbg`` finds the target by substring-matching the injected container id
    against ``/proc/<pid>/cgroup``, which is the only attribution that survives
    ``shareProcessNamespace`` and a second attached session (report §3.15).
    """
    commands = _dry_run_commands(kubectl, victim, seat)
    text = "\n".join(commands)

    sysroot = _index_of(commands, "set sysroot /proc/")
    directory = _index_of(commands, "directory /proc/")
    safe_path = _index_of(commands, "add-auto-load-safe-path /proc/")
    file_command = _index_of(commands, "file /proc/")
    attach_command = _index_of(commands, "attach ")

    assert sysroot < attach_command, (
        "`set sysroot` must precede `attach`. The other way round, libraries "
        f"are fixed up but user frames come back as ?? () (report §3.3):\n{text}"
    )
    assert file_command < attach_command, (
        "`file /proc/<pid>/root<exe>` before `attach` is what recovers the "
        f"user frames (report §3.3):\n{text}"
    )
    assert directory > sysroot, (
        f"sysroot does not cover source lookup; `directory` does (§3.3):\n{text}"
    )
    assert safe_path > sysroot, (
        "a sysroot makes gdb decline the target's libthread_db unless "
        f"add-auto-load-safe-path is set (§3.3):\n{text}"
    )
    assert "substitute-path" not in text, (
        "`set substitute-path / /proc/<pid>/root/` functions but gdb "
        "re-applies it on display, so `info source` fullname comes back as "
        "/proc/1/root/proc/1/root/... — which is the string a DAP client hands "
        f"to the editor (§3.3):\n{text}"
    )


def test_backtrace_names_the_functions(
    kubectl: KubectlCli, victim: str, seat: Session
) -> None:
    """A real attach yields a backtrace with user function names in it.

    ``outer_loop`` and ``main`` are the frames the target is guaranteed to be
    in: it spends its life in ``sleep(2)`` inside ``outer_loop``. Their
    presence is what distinguishes a correct sysroot from the §3.4 failure,
    where libc frames look fine and everything above them is ``?? ()``.
    """
    _require_live_attach(seat)
    commands = _dry_run_commands(kubectl, victim, seat)
    argv: list[str] = ["gdb", "-q", "-batch"]
    for command in commands:
        argv += ["-ex", command]
    argv += ["-ex", "bt", "-ex", "detach"]

    result = kubectl.exec(
        victim, argv, container=seat.seat.container, check=False, timeout=420
    )
    output = result.stdout + result.stderr
    assert "outer_loop" in output, (
        f"no user frames in the backtrace — the §3.4 wrong-sysroot shape:\n{output}"
    )
    assert "main" in output, output
    assert "?? ()" not in output.split("outer_loop")[0], (
        "frames below the user code came back unresolved, which is what a "
        f"sysroot applied after attach looks like:\n{output}"
    )


def test_read_only_inspection_survives_without_attach(
    kubectl: KubectlCli, victim: str, seat: Session
) -> None:
    """The target's rootfs is readable through the sysroot, cap or no cap.

    This is the property that makes the degraded rung worth having: an
    Ubuntu-based debug container reading a Debian target's filesystem (report
    §3.11 measured 6/6 ``/proc`` reads at the target's uid). It is asserted
    here, on the full rung, because it must hold on both.
    """
    report = seat.report
    assert report is not None, "attach was asked to probe and returned no report"
    assert report.target_pid is not None, (
        "the probe could not find the target process, so PODBENCH_TARGET_CID "
        "did not reach the container or did not match any cgroup (§3.15)"
    )
    listing = kubectl.exec(
        victim,
        ["ls", f"/proc/{report.target_pid}/root/app"],
        container=seat.seat.container,
    )
    assert "victim" in listing.stdout, listing.stdout


def _require_live_attach(seat: Session) -> None:
    """Skip, with the probe's own diagnosis, when attach is not on offer."""
    if seat.rung is not Rung.FULL:
        refused = "; ".join(
            f"{step.rung.value}: {step.detail}"
            for step in seat.steps
            if not step.admitted
        )
        pytest.skip(
            "the cluster would not grant a root + SYS_PTRACE container, so the "
            f"seat landed on the {seat.rung.value} rung. "
            f"{refused or 'no reason recorded'}"
        )
    report = seat.report
    assert report is not None
    if report.verdict is not Verdict.LIVE_ATTACH:
        pytest.skip(
            f"the container has the capability but attach is denied on node "
            f"{report.node_name}: {report.blocker.value} — "
            f"{report.blocker.explanation}"
        )


def _dry_run_commands(kubectl: KubectlCli, victim: str, seat: Session) -> list[str]:
    result = kubectl.exec(
        victim,
        # --no-debuginfod: symbols from the network are not what this asserts,
        # and a CI runner without egress would fail for the wrong reason.
        ["podbench", "dbg", "--dry-run", "--no-debuginfod"],
        container=seat.seat.container,
    )
    commands = [line for line in result.stdout.splitlines() if line.strip()]
    assert commands, f"dbg --dry-run printed nothing:\n{result.stderr}"
    return commands


def _index_of(commands: list[str], prefix: str) -> int:
    for index, command in enumerate(commands):
        if command.startswith(prefix):
            return index
    raise AssertionError(f"no command starting {prefix!r} in:\n" + shlex.join(commands))
