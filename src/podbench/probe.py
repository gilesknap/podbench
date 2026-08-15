"""``capreport`` — name the mechanism that denied ptrace, before it costs an
afternoon.

Four unrelated subsystems (the credential check, Yama, seccomp and AppArmor)
refuse ``PTRACE_ATTACH`` with the same bare ``EPERM``, and gdb makes it worse
by reporting a stale ``ENOTTY`` as ``ptrace: Inappropriate ioctl for device``
(spike S5, finding 4). So the probe never infers the answer from a failure: it
reads the kernel's own accounting, then measures ptrace twice.

The two measurements are the whole trick. The first attaches to a child the
probe **forked itself** — Yama always permits descendants and the credential
check always passes against your own child, so a failure there cannot be a
policy decision about the target and must be structural: a seccomp filter
rejecting the syscall, ``ptrace_scope=3``, or AppArmor. Only if that succeeds
is a failure on the target informative about the target.

Exit codes are :class:`~podbench.model.Verdict`'s values — 0 live attach,
10 read-only, 20 nothing — so a shell can branch without parsing anything.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .model import TARGET_CID_ENV, Blocker, CapabilityReport, Verdict
from .proc import (
    DEFAULT_PROC,
    apparmor_profile,
    env_target_container_id,
    no_new_privs,
    proc_read_matrix,
    read_tracer_pid,
    read_uid,
    scan_processes,
    seccomp_mode,
    self_capabilities,
    yama_scope,
)

__all__ = [
    "AttachOutcome",
    "Attacher",
    "CtypesAttacher",
    "SkippedAttacher",
    "default_attacher",
    "derive_verdict",
    "format_report",
    "main",
    "probe",
]

PTRACE_ATTACH = 16
PTRACE_DETACH = 17

YAMA_MEANINGS = {
    0: "classic ptrace permissions - any same-UID attach allowed",
    1: (
        "restricted - attach only to DESCENDANTS of the tracer, or targets "
        "that called prctl(PR_SET_PTRACER)"
    ),
    2: "admin-only - attach requires CAP_SYS_PTRACE",
    3: "no attach - PTRACE_ATTACH disabled entirely, unchangeable until reboot",
}

SECCOMP_MEANINGS = {
    0: "disabled",
    1: "SECCOMP_MODE_STRICT (ptrace WILL be killed)",
    2: "SECCOMP_MODE_FILTER",
}


@dataclass(frozen=True)
class AttachOutcome:
    """What one ``PTRACE_ATTACH`` attempt did."""

    ok: bool
    errno: int = 0
    message: str = "OK"
    skipped: bool = False
    """No way to issue ptrace at all, so this is not evidence either way."""

    notes: tuple[str, ...] = ()
    """Anything the attempt did to the target that the user must be told about.

    An attach that could not be undone belongs in the report even though the
    attach itself succeeded, so it travels with the outcome rather than being
    dropped at the boundary.
    """

    @classmethod
    def skip(cls, reason: str) -> AttachOutcome:
        """An attempt that never happened."""
        return cls(ok=False, message=reason, skipped=True)

    @property
    def measured_ok(self) -> bool | None:
        """``None`` when the probe was skipped, so callers cannot mistake an
        unmeasured attach for a failed one."""
        return None if self.skipped else self.ok


class Attacher(Protocol):
    """Something that can issue ``PTRACE_ATTACH``."""

    def attach_child(self) -> AttachOutcome:
        """Attach to a freshly forked child of this process."""
        ...

    def attach(self, pid: int) -> AttachOutcome:
        """Attach to an existing process."""
        ...


class SkippedAttacher:
    """Stands in when ptrace cannot be issued at all — an exotic platform
    degrades to "probe skipped" rather than crashing the diagnostic."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def attach_child(self) -> AttachOutcome:
        return AttachOutcome.skip(self.reason)

    def attach(self, pid: int) -> AttachOutcome:
        return AttachOutcome.skip(self.reason)


class CtypesAttacher:
    """``ptrace(2)`` through libc, so no compiled helper has to be shipped.

    S5 exercised exactly this sequence in a ``python:3.12-slim`` debug
    container and it produced the same verdicts as the C helper.
    """

    def __init__(self, libc: ctypes.CDLL) -> None:
        self._libc = libc

    def _ptrace(self, request: int, pid: int) -> AttachOutcome:
        ctypes.set_errno(0)
        rc = int(self._libc.ptrace(request, pid, 0, 0))
        err = ctypes.get_errno()
        if rc == 0:
            return AttachOutcome(ok=True)
        return AttachOutcome(ok=False, errno=err, message=os.strerror(err))

    def attach(self, pid: int) -> AttachOutcome:
        outcome = self._ptrace(PTRACE_ATTACH, pid)
        if not outcome.ok:
            return outcome
        # PTRACE_ATTACH leaves the tracee SIGSTOPped until it is detached, so
        # reaping the stop and detaching is not optional politeness.
        _reap(pid)
        detach = self._ptrace(PTRACE_DETACH, pid)
        if detach.ok:
            return outcome
        # A detach can genuinely fail — an ESRCH swallowed by waitpid, a tracee
        # that changed state underneath us — and an unnoticed failure leaves the
        # workload stopped. capreport freezing the process it was asked to
        # describe is worse than any verdict it could print, so the stop is
        # lifted by hand and said out loud.
        return AttachOutcome(ok=True, notes=(_resume(pid, detach),))

    def attach_child(self) -> AttachOutcome:
        pid = os.fork()
        if pid == 0:  # pragma: no cover - the child never returns
            try:
                while True:
                    signal.pause()
            finally:
                os._exit(0)
        try:
            return self.attach(pid)
        finally:
            os.kill(pid, signal.SIGKILL)
            _reap(pid)


def _resume(pid: int, detach: AttachOutcome) -> str:
    """Undo the attach's SIGSTOP after PTRACE_DETACH refused to."""
    try:
        os.kill(pid, signal.SIGCONT)
    except OSError as exc:
        return (
            f"PTRACE_DETACH from pid {pid} failed ({detach.message}) and SIGCONT "
            f"failed too ({exc}): if that process is still alive it may be left "
            "stopped - check with `ps -o pid,stat` and send SIGCONT by hand"
        )
    return (
        f"PTRACE_DETACH from pid {pid} failed ({detach.message}); sent SIGCONT to "
        "resume it, since the attach had left it stopped"
    )


def _reap(pid: int) -> None:
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass


def _load_libc() -> ctypes.CDLL | None:
    if not hasattr(os, "fork"):
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.ptrace
    except (OSError, AttributeError, TypeError):
        return None
    fn.restype = ctypes.c_long
    fn.argtypes = [ctypes.c_long] * 4
    return libc


def default_attacher() -> Attacher:
    """The real ptrace backend, or a skipping stand-in on platforms without one."""
    libc = _load_libc()
    if libc is None:
        return SkippedAttacher("no usable ptrace(2) via libc on this platform")
    return CtypesAttacher(libc)


def probe(
    target_pid: int | None,
    *,
    proc: Path = DEFAULT_PROC,
    attacher: Attacher | None = None,
    node_name: str | None = None,
    extra_notes: Sequence[str] = (),
) -> CapabilityReport:
    """Measure what this container may do to ``target_pid``.

    Passing ``None`` still reports the kernel accounting and the scratch
    attach, which is enough to say whether gdb-*launch* will work — the inner
    loop the report tells us to design for (§3.12).
    """
    attacher = default_attacher() if attacher is None else attacher
    notes: list[str] = list(extra_notes)

    caps = self_capabilities(proc=proc)
    if not caps.readable:
        notes.append("could not read /proc/self/status: capability masks unknown")

    self_uid = read_uid("self", proc=proc)
    if self_uid is None:
        self_uid = os.getuid()
        notes.append("could not read our own uid from /proc; used getuid()")

    seccomp = seccomp_mode(proc=proc)
    if seccomp is None:
        seccomp = 0
        notes.append("no Seccomp field in /proc/self/status; assumed disabled")
    nnp = no_new_privs(proc=proc)
    if nnp is None:
        nnp = False
        notes.append("no NoNewPrivs field in /proc/self/status; assumed unset")

    yama = yama_scope(proc=proc)
    aa_self = apparmor_profile("self", proc=proc)
    aa_target: str | None = None
    target_uid: int | None = None
    tracer_pid: int | None = None
    reads: dict[str, bool] = {}
    if target_pid is not None:
        aa_target = apparmor_profile(target_pid, proc=proc)
        target_uid = read_uid(target_pid, proc=proc)
        tracer_pid = read_tracer_pid(target_pid, proc=proc)
        reads = proc_read_matrix(target_pid, proc=proc)

    child = attacher.attach_child()
    target_attach = attacher.attach(target_pid) if target_pid is not None else None
    notes.extend(child.notes)
    if target_attach is not None:
        notes.extend(target_attach.notes)

    notes.extend(
        _accounting_notes(
            caps_bounding=caps.sys_ptrace_bounding,
            caps_effective=caps.sys_ptrace_effective,
            self_uid=self_uid,
            yama=yama,
            seccomp=seccomp,
            target_pid=target_pid,
            tracer_pid=tracer_pid,
            child=child,
        )
    )

    verdict, blocker, derived = derive_verdict(
        cap_sys_ptrace=caps.sys_ptrace_effective,
        yama=yama,
        seccomp=seccomp,
        apparmor_self=aa_self,
        apparmor_target=aa_target,
        self_uid=self_uid,
        target_uid=target_uid,
        target_pid=target_pid,
        tracer_pid=tracer_pid,
        child=child,
        target_attach=target_attach,
        proc_reads=reads,
    )
    notes.extend(derived)

    return CapabilityReport(
        verdict=verdict,
        blocker=blocker,
        cap_sys_ptrace=caps.sys_ptrace_effective,
        cap_bounding_sys_ptrace=caps.sys_ptrace_bounding,
        yama_scope=yama,
        seccomp_mode=seccomp,
        no_new_privs=nnp,
        apparmor_profile=aa_self,
        self_uid=self_uid,
        target_uid=target_uid,
        target_pid=target_pid,
        node_name=node_name if node_name is not None else _node_name_from_env(),
        child_attach_ok=child.measured_ok,
        target_attach_ok=(
            target_attach.measured_ok if target_attach is not None else None
        ),
        proc_reads=reads,
        notes=notes,
    )


def _node_name_from_env() -> str | None:
    """The node podbench landed on, if the launcher passed it down.

    Worth surfacing: two arm64 nodes in the same cluster disagreed about Yama,
    so "attach worked yesterday" has a node-shaped explanation (§3.13).
    """
    for var in ("PODBENCH_NODE_NAME", "NODE_NAME"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return None


def _accounting_notes(
    *,
    caps_bounding: bool,
    caps_effective: bool,
    self_uid: int,
    yama: int | None,
    seccomp: int,
    target_pid: int | None,
    tracer_pid: int | None,
    child: AttachOutcome,
) -> list[str]:
    notes: list[str] = []
    if caps_bounding and not caps_effective and self_uid != 0:
        notes.append(
            "SYS_PTRACE is in the bounding set but not the effective set: a "
            "capability added to a container with a non-zero runAsUser lands "
            "only in bounding and grants nothing. CAP_SYS_PTRACE requires "
            "runAsUser: 0."
        )
    if yama is None:
        notes.append(
            "no Yama LSM on this node (the file is absent, which is not the "
            "same as ptrace_scope=0); other nodes in the same cluster may differ"
        )
    if seccomp == 2:
        notes.append(
            "RuntimeDefault seccomp permits ptrace but blocks "
            "personality(ADDR_NO_RANDOMIZE): gdb cannot disable ASLR, so "
            "addresses vary run to run"
        )
    if tracer_pid:
        notes.append(f"target pid {target_pid} is already traced by pid {tracer_pid}")
    if child.skipped:
        notes.append(
            f"ptrace probe skipped ({child.message}): the verdict below comes "
            "from the kernel accounting alone and was not measured"
        )
    return notes


def derive_verdict(
    *,
    cap_sys_ptrace: bool,
    yama: int | None,
    seccomp: int,
    apparmor_self: str | None,
    apparmor_target: str | None,
    self_uid: int,
    target_uid: int | None,
    target_pid: int | None,
    child: AttachOutcome,
    target_attach: AttachOutcome | None,
    proc_reads: dict[str, bool],
    tracer_pid: int | None = None,
) -> tuple[Verdict, Blocker, list[str]]:
    """Turn the measurements into a verdict and a named blocker."""
    notes: list[str] = []
    reads_ok = any(proc_reads.values())

    if not child.ok and not child.skipped:
        # Yama and the credential check both always permit our own child, so
        # this cannot be a policy decision about the target.
        blocker, structural_notes = _classify_structural(
            seccomp=seccomp, yama=yama, apparmor_self=apparmor_self, child=child
        )
        notes.extend(structural_notes)
        verdict = Verdict.READ_ONLY if reads_ok else Verdict.NONE
        return verdict, blocker, notes

    if target_pid is None:
        notes.append(
            "no target pid given, so live attach was not tested; gdb-launch "
            "(gdb ./prog) and attaching to processes gdb itself started work "
            "with no capability at all"
        )
        return Verdict.LIVE_ATTACH, Blocker.NONE, notes

    if target_attach is not None and target_attach.ok:
        return Verdict.LIVE_ATTACH, Blocker.NONE, notes

    unmeasured = target_attach is None or target_attach.skipped
    if unmeasured and cap_sys_ptrace:
        # Nothing in the accounting stands in the way, and we never asked the
        # kernel, so naming a blocker would be an invention.
        notes.append(
            "CAP_SYS_PTRACE is effective and nothing in the accounting blocks "
            "attach, but no attach was actually attempted"
        )
        blocker = Blocker.NONE
    else:
        blocker, denial_notes = _classify_denial(
            self_uid=self_uid,
            target_uid=target_uid,
            tracer_pid=tracer_pid,
            yama=yama,
            apparmor_self=apparmor_self,
            apparmor_target=apparmor_target,
            cap_sys_ptrace=cap_sys_ptrace,
        )
        notes.extend(denial_notes)

    if reads_ok:
        notes.append(
            "read-only debugging is still available: sysroot, maps, environ "
            "and fd are readable, and gdb-launch needs no capability"
        )
        return Verdict.READ_ONLY, blocker, notes
    return Verdict.NONE, blocker, notes


def _classify_structural(
    *,
    seccomp: int,
    yama: int | None,
    apparmor_self: str | None,
    child: AttachOutcome,
) -> tuple[Blocker, list[str]]:
    notes = [
        f"PTRACE_ATTACH failed on our own forked child ({child.message}), so "
        "ptrace(2) itself is unusable here - this is not about the target"
    ]
    if seccomp in (1, 2):
        # Untested branch: S5 could not install a profile that denies ptrace,
        # and RuntimeDefault demonstrably allows it (report R7).
        return Blocker.SECCOMP, notes
    if yama == 3:
        return Blocker.YAMA_SCOPE, notes
    if _confined(apparmor_self):
        return Blocker.APPARMOR, notes
    return Blocker.UNKNOWN, notes


def _classify_denial(
    *,
    self_uid: int,
    target_uid: int | None,
    tracer_pid: int | None,
    yama: int | None,
    apparmor_self: str | None,
    apparmor_target: str | None,
    cap_sys_ptrace: bool,
) -> tuple[Blocker, list[str]]:
    if tracer_pid:
        # First, and ahead of the capability check: a tracee has exactly one
        # tracer, so an already-traced target refuses even CAP_SYS_PTRACE with
        # the same EPERM. Reading TracerPid is the only way to tell that apart
        # from the four policy mechanisms, and blaming Yama or the uid here
        # would send the user to fix something that is not broken.
        return Blocker.ALREADY_TRACED, [
            f"pid {tracer_pid} is already tracing this process, so the kernel "
            "refuses a second tracer. Nothing about this container's uid, "
            "capabilities, Yama or AppArmor is implicated"
        ]
    if cap_sys_ptrace:
        if _confined(apparmor_self) or _confined(apparmor_target):
            return Blocker.APPARMOR, [
                f"our profile is {apparmor_self!r} and the target's is "
                f"{apparmor_target!r}; the containerd default profile permits "
                "ptrace only between peers in the SAME profile"
            ]
        return Blocker.UNKNOWN, [
            "CAP_SYS_PTRACE is effective and our own child attached fine, so "
            "this is not a capability problem"
        ]
    if target_uid is None:
        return Blocker.NO_CAP_SYS_PTRACE, [
            "the target's uid could not be read, so the credential check "
            "cannot be ruled in or out"
        ]
    if self_uid != target_uid:
        # The credential check in __ptrace_may_access() fails before Yama is
        # consulted, and it also gates PTRACE_MODE_READ - hence the lost reads.
        return Blocker.UID_MISMATCH, [
            f"tracer uid={self_uid}, target uid={target_uid}: at this uid you "
            "also lose /proc/<pid>/root, maps, environ and exe, which need "
            "PTRACE_MODE_READ and are gated by the same credential check"
        ]
    if yama not in (None, 0):
        return Blocker.YAMA_SCOPE, [
            "both processes have the same uid so the credential check passes; "
            "ptrace_scope is a node sysctl and /proc/sys is read-only in the "
            "container, so no securityContext change fixes it. The target can "
            "opt in with prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)"
        ]
    return Blocker.UNKNOWN, [
        "same uid, no existing tracer, no Yama restriction and no capability in "
        "play, yet attach was refused; check AppArmor and user namespaces"
    ]


def _confined(profile: str | None) -> bool:
    return profile is not None and profile != "unconfined"


def format_report(report: CapabilityReport, json_output: bool) -> str:
    """Render the report, either for a human or as the stable JSON form."""
    if json_output:
        return json.dumps(_json_payload(report), indent=2, sort_keys=True)
    return _human_report(report)


def _json_payload(report: CapabilityReport) -> dict[str, object]:
    """The JSON shape is a public interface — CI runs ``capreport --json``."""
    return {
        "verdict": report.verdict.name.lower(),
        "exit_code": report.verdict.value,
        "summary": report.verdict.summary,
        "blocker": report.blocker.value,
        "explanation": report.blocker.explanation,
        "cap_sys_ptrace": report.cap_sys_ptrace,
        "cap_bounding_sys_ptrace": report.cap_bounding_sys_ptrace,
        "yama_scope": report.yama_scope,
        "seccomp_mode": report.seccomp_mode,
        "no_new_privs": report.no_new_privs,
        "apparmor_profile": report.apparmor_profile,
        "self_uid": report.self_uid,
        "target_uid": report.target_uid,
        "target_pid": report.target_pid,
        "node_name": report.node_name,
        "child_attach_ok": report.child_attach_ok,
        "target_attach_ok": report.target_attach_ok,
        "proc_reads": report.proc_reads,
        "notes": report.notes,
    }


def _human_report(report: CapabilityReport) -> str:
    width = 72
    lines = [" capreport ".center(width, "=")]

    def kv(key: str, value: object) -> None:
        lines.append(f"  {key:<26} {value}")

    lines.append("TRACER")
    kv("uid", report.self_uid)
    kv(
        "CAP_SYS_PTRACE (eff)",
        f"{_yn(report.cap_sys_ptrace)}   "
        f"[bounding: {_yn(report.cap_bounding_sys_ptrace)}]",
    )
    kv(
        "Seccomp",
        f"{report.seccomp_mode} "
        f"({SECCOMP_MEANINGS.get(report.seccomp_mode, 'unknown')})",
    )
    kv("NoNewPrivs", int(report.no_new_privs))
    kv("AppArmor", report.apparmor_profile or "unavailable")
    kv("Yama ptrace_scope", _yama_text(report.yama_scope))
    kv("node", report.node_name or "unknown")

    if report.target_pid is not None:
        lines.append(f"TARGET (pid {report.target_pid})")
        kv("uid", report.target_uid if report.target_uid is not None else "?")
        ok = sum(1 for value in report.proc_reads.values() if value)
        detail = " ".join(
            f"{name}={'ok' if value else 'DENIED'}"
            for name, value in report.proc_reads.items()
        )
        kv("/proc reads", f"{ok}/{len(report.proc_reads)} ok - {detail}")

    lines.append("PROBES")
    kv("scratch attach (own child)", _attach_text(report.child_attach_ok))
    if report.target_pid is not None:
        kv(
            f"live attach (pid {report.target_pid})",
            _attach_text(report.target_attach_ok),
        )

    lines.append("-" * width)
    lines.append(f"VERDICT: {report.verdict.summary} (exit {report.verdict.value})")
    lines.append(f"BLOCKER: {report.blocker.value}")
    lines.append(f"         {report.blocker.explanation}")
    if report.notes:
        lines.append("NOTES:")
        lines.extend(f"  - {note}" for note in report.notes)
    lines.append("=" * width)
    return "\n".join(lines)


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _attach_text(ok: bool | None) -> str:
    if ok is None:
        return "skipped"
    return "OK" if ok else "denied"


def _yama_text(scope: int | None) -> str:
    if scope is None:
        return "absent - Yama LSM not present on this node"
    return f"{scope} - {YAMA_MEANINGS.get(scope, 'unknown value')}"


def main(
    args: Sequence[str] | None = None,
    *,
    proc: Path = DEFAULT_PROC,
    attacher: Attacher | None = None,
) -> int:
    """Run the probe and print the report. Returns the verdict's exit code.

    ``proc`` and ``attacher`` are seams for testing against a synthetic tree;
    the CLI passes neither.
    """
    parser = argparse.ArgumentParser(
        prog="capreport",
        description="Name the mechanism that denies ptrace in this container.",
    )
    parser.add_argument(
        "pid",
        nargs="?",
        type=int,
        default=None,
        help="target pid; discovered from the target container id if omitted",
    )
    parser.add_argument(
        "--container-id",
        default=None,
        help=f"target container id (default: ${TARGET_CID_ENV})",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="emit the stable JSON form instead of the human report",
    )
    parsed = parser.parse_args(args)

    pid: int | None = parsed.pid
    notes: list[str] = []
    if pid is None:
        pid, discovery_notes = _discover_target(parsed.container_id, proc=proc)
        notes.extend(discovery_notes)

    report = probe(pid, proc=proc, attacher=attacher, extra_notes=notes)
    print(format_report(report, parsed.json_output))
    return report.verdict.value


def _discover_target(
    container_id: str | None, *, proc: Path
) -> tuple[int | None, list[str]]:
    """Find the target pid from the container id, saying how sure we are.

    With no container id at all we deliberately refuse to guess: "the target is
    PID 1" is wrong under ``shareProcessNamespace: true``, where PID 1 is
    ``/pause`` (report §3.15).
    """
    cid = container_id or env_target_container_id()
    if cid is None:
        return None, [
            f"no target pid and no {TARGET_CID_ENV}: reporting on this "
            "container only, since PID 1 is not reliably the target"
        ]
    listing = scan_processes(cid, proc=proc)
    targets = listing.targets
    if not targets:
        return None, [f"no process found in a cgroup matching container id {cid}"]
    notes = [] if listing.warning is None else [listing.warning]
    if len(targets) > 1:
        notes.append(
            "target container has "
            f"{len(targets)} processes; probing the lowest pid "
            f"({targets[0].pid}, {targets[0].comm})"
        )
    return targets[0].pid, notes
