"""``capreport`` — name the mechanism that denied ptrace, before it costs an
afternoon.

Five unrelated subsystems (the credential check, Yama, seccomp, AppArmor and
SELinux) refuse ``PTRACE_ATTACH`` with the same bare ``EPERM``, and gdb makes it
worse by reporting a stale ``ENOTTY`` as ``ptrace: Inappropriate ioctl for
device`` (spike S5, finding 4). So the probe never infers the answer from a
failure: it reads the kernel's own accounting, then measures ptrace twice.

The two measurements are the whole trick. The first attaches to a child the
probe **forked itself** — the credential check always passes against your own
child and Yama exempts descendants below ``ptrace_scope=2``, so a failure there
cannot be a policy decision about the target and must be structural: a seccomp
filter rejecting the syscall, ``ptrace_scope`` 2 or 3, or the node's LSM. Only
if that succeeds is a failure on the target informative about the target.

Which LSM that is comes from ``/sys``, never from the shape of the context in
``/proc/self/attr/current``: the file belongs to whichever module is loaded, and
reading it as AppArmor is how an SELinux denial on a Diamond node was reported
as ``blocker: unknown`` (issue #52).

Exit codes are :class:`~podbench.model.Verdict`'s values — 0 live attach,
10 read-only, 15 launch-only, 20 nothing — so a shell can branch without
parsing anything.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol

import typer

from .cli import new_app, run
from .flavour import Debugger, format_inventory, inventory
from .model import (
    TARGET_CID_ENV,
    Blocker,
    CapabilityReport,
    Lsm,
    LsmStatus,
    Verdict,
    describe_gated_fallback,
    describe_reads,
    ptrace_reads_ok,
)
from .proc import (
    DEFAULT_PROC,
    DEFAULT_SYSFS,
    detect_lsm,
    env_target_container_id,
    lsm_context,
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
    sysfs: Path = DEFAULT_SYSFS,
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
    lsm = detect_lsm(proc=proc, sysfs=sysfs)
    target_context: str | None = None
    target_uid: int | None = None
    tracer_pid: int | None = None
    reads: dict[str, bool] = {}
    if target_pid is not None:
        target_context = lsm_context(target_pid, proc=proc)
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
            lsm=lsm,
            target_pid=target_pid,
            tracer_pid=tracer_pid,
            child=child,
        )
    )

    verdict, blocker, derived = derive_verdict(
        cap_sys_ptrace=caps.sys_ptrace_effective,
        yama=yama,
        seccomp=seccomp,
        lsm=lsm,
        target_context=target_context,
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
        lsm=lsm,
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
    lsm: LsmStatus,
    target_pid: int | None,
    tracer_pid: int | None,
    child: AttachOutcome,
) -> list[str]:
    notes: list[str] = []
    if lsm.kind is Lsm.SELINUX and lsm.enforcing is False:
        # Worth a line whatever the verdict: a reader who sees an SELinux
        # context and a denial will reach for the policy first, and permissive
        # mode means the policy allowed the call and merely logged it.
        notes.append(
            "SELinux is permissive on this node (/sys/fs/selinux/enforce is "
            "0): it logs AVC denials and allows the call, so it can never be "
            "what refuses ptrace here"
        )
    if lsm.kind is Lsm.UNKNOWN and lsm.context is not None:
        notes.append(
            f"a security context is set ({lsm.context}) but /sys could not be "
            "read, so which LSM wrote it is unknown - and that string alone "
            "does not say, which is how an SELinux denial was once reported as "
            "an AppArmor profile"
        )
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
    lsm: LsmStatus,
    target_context: str | None,
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
    # Only the ptrace-gated reads may carry this. `any` over the whole matrix
    # made the verdict very nearly unfalsifiable — `cmdline` and `status` need
    # no permission at all, so they are true on a pod where nothing works, and
    # a Diamond seat with root, maps and environ all denied was reported as
    # read-only (issue #51).
    reads_ok = ptrace_reads_ok(proc_reads)

    if not child.ok and not child.skipped:
        # Yama and the credential check both always permit our own child, so
        # this cannot be a policy decision about the target. There is no
        # launch-only fallback from here either: gdb traces an inferior it
        # forked, which is the very attach that just failed.
        blocker, structural_notes = _classify_structural(
            seccomp=seccomp,
            yama=yama,
            lsm=lsm,
            cap_sys_ptrace=cap_sys_ptrace,
            child=child,
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
            lsm=lsm,
            target_context=target_context,
            cap_sys_ptrace=cap_sys_ptrace,
        )
        notes.extend(denial_notes)

    if reads_ok:
        # gdb-launch is named only when the scratch attach measured it. It
        # needs no *capability*, which is not the same as working: a skipped
        # probe measured nothing, and this branch is reachable with one.
        launch = (
            ", and gdb-launch needs no capability"
            if child.measured_ok
            else "; the scratch attach was skipped, so gdb-launch is unmeasured"
        )
        notes.append(
            f"read-only debugging is still available: {describe_reads(proc_reads)}"
            f"{launch}"
        )
        return Verdict.READ_ONLY, blocker, notes
    if child.measured_ok:
        # The rung the brief never named. Attach and the target's own /proc are
        # both gone, but tracing a descendant is always permitted (report
        # 3.12), so the inner loop the report tells us to design for is intact
        # — and saying "nothing works" here would hide it. What is *not* said
        # is that a sysroot is out: the rule is all-or-nothing, so a matrix
        # that kept `root` lands here too, and report 3.4 makes that sysroot
        # the mandatory fix rather than a consolation.
        notes.append(
            "the reads that take PTRACE_MODE_READ went with it "
            f"({describe_reads(proc_reads)}), "
            f"{describe_gated_fallback(proc_reads)}; what still works is "
            "debugging a process the seat starts itself - `podbench dbg "
            "--launch ./prog` - which needs no capability"
        )
        # The rung says nothing about the other two modes, and a reader who has
        # just lost live attach assumes it does. In Iterate and Hotfix the
        # debuggee is the sidecar's own child, which is exactly the attach this
        # seat has already proved it may make (issue #52).
        notes.append(
            "this is a limit on attaching to someone else's process, not on "
            "debugging: `podbench dev` and `podbench hotfix` run the code as "
            "the seat's own child, the case the scratch attach above just "
            "measured as permitted, so neither mode is affected by whatever "
            "denied attach here"
        )
        return Verdict.LAUNCH_ONLY, blocker, notes
    return Verdict.NONE, blocker, notes


def _classify_structural(
    *,
    seccomp: int,
    yama: int | None,
    lsm: LsmStatus,
    cap_sys_ptrace: bool,
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
    if yama == 2 and not cap_sys_ptrace:
        # `ptrace_scope=2` is the one Yama setting with no descendant
        # exemption: yama_ptrace_access_check takes the CAP_SYS_PTRACE branch
        # without ever asking whether the tracee is our child, and
        # yama_ptrace_traceme demands the same of the parent, so gdb-launch
        # goes too. Naming it matters more here than anywhere: without this
        # the scratch failure falls through to "none of the known mechanisms
        # accounts for it", with `Yama ptrace_scope 2` printed six lines above.
        notes.append(
            "ptrace_scope=2 permits attach only to a tracer holding "
            "CAP_SYS_PTRACE, and unlike scope 1 it makes no exception for a "
            "descendant, so `gdb ./prog` is refused as well"
        )
        return Blocker.YAMA_SCOPE, notes
    lsm_blocker = lsm.blocker
    if lsm_blocker is not None:
        return lsm_blocker, [*notes, f"the LSM here is {lsm.summary}"]
    return Blocker.UNKNOWN, notes


def _classify_denial(
    *,
    self_uid: int,
    target_uid: int | None,
    tracer_pid: int | None,
    yama: int | None,
    lsm: LsmStatus,
    target_context: str | None,
    cap_sys_ptrace: bool,
) -> tuple[Blocker, list[str]]:
    if tracer_pid:
        # First, and ahead of the capability check: a tracee has exactly one
        # tracer, so an already-traced target refuses even CAP_SYS_PTRACE with
        # the same EPERM. Reading TracerPid is the only way to tell that apart
        # from the five policy mechanisms, and blaming Yama or the uid here
        # would send the user to fix something that is not broken.
        return Blocker.ALREADY_TRACED, [
            f"pid {tracer_pid} is already tracing this process, so the kernel "
            "refuses a second tracer. Nothing about this container's uid, "
            "capabilities, Yama or the LSM is implicated"
        ]
    if cap_sys_ptrace:
        denial = _lsm_denial(lsm, target_context)
        if denial is not None:
            return denial
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
    # The rung a Diamond production pod lands on, and the one that produced
    # "blocker: unknown" (issue #52): every mechanism above says "not me", and
    # the LSM was never asked because its context had been filed as an AppArmor
    # profile. It is asked here, ahead of giving up.
    lsm_denial = _lsm_denial(lsm, target_context)
    if lsm_denial is not None:
        return lsm_denial
    return Blocker.UNKNOWN, [
        "same uid, no existing tracer, no Yama restriction, no capability in "
        f"play and no LSM confining this seat ({lsm.summary}), yet attach was "
        "refused. Everything visible from inside the container has been ruled "
        "out, so the next evidence is on the node: `ausearch -m avc -ts recent` "
        "if it runs SELinux, `dmesg | grep -i apparmor` if AppArmor. Please "
        "report this with the full capreport output"
    ]


def _lsm_denial(
    lsm: LsmStatus, target_context: str | None
) -> tuple[Blocker, list[str]] | None:
    """Name the LSM as the blocker, or ``None`` if it cannot be one.

    Two modules, two different pieces of advice, and only one of them can be
    acted on from inside the pod - which is precisely why the module has to be
    detected rather than assumed (issue #52).
    """
    if lsm.kind is Lsm.SELINUX:
        if not lsm.confines:
            return None
        return Blocker.SELINUX, [_selinux_note(lsm, target_context)]
    if lsm.kind is Lsm.APPARMOR and (lsm.confines or _confined(target_context)):
        return Blocker.APPARMOR, [
            f"our profile is {lsm.context!r} and the target's is "
            f"{target_context!r}; the containerd default profile permits "
            "ptrace only between peers in the SAME profile"
        ]
    return None


def _selinux_note(lsm: LsmStatus, target_context: str | None) -> str:
    """Say what the contexts are, and that identical ones settle nothing.

    Diamond's seat and target both ran as ``spc_t:s0``, so the tempting reading
    - "different domains, obvious denial" - is not available, and a reader who
    assumes it will look for a cross-domain rule that does not exist.
    """
    same = lsm.context is not None and lsm.context == target_context
    shared = (
        " - the same context on both sides, so this is not a cross-domain "
        "denial and the policy question is a real one"
        if same
        else ""
    )
    return (
        f"SELinux is enforcing; this seat's context is {lsm.context!r} and the "
        f"target's is {target_context!r}{shared}. The AVC record naming the "
        "exact source type, target type, class and permission is in the node's "
        "audit log, which no seat can read: it takes `ausearch -m avc -ts "
        "recent` on the node, run by someone with access to it"
    )


def _confined(profile: str | None) -> bool:
    """Whether an AppArmor profile string names a confining profile."""
    return profile is not None and profile != "unconfined"


def format_report(
    report: CapabilityReport,
    json_output: bool,
    debuggers: Sequence[Debugger] = (),
) -> str:
    """Render the report, either for a human or as the stable JSON form.

    ``debuggers`` rides along with the capability verdict rather than living in
    a verb of its own, because the two answers are only useful together: a seat
    that may attach but ships no adapter for the target's language fails at F5
    with an error naming neither. It is a parameter rather than a field of
    :class:`~podbench.model.CapabilityReport` because it describes the *image*,
    not the probe.
    """
    if json_output:
        return json.dumps(_json_payload(report, debuggers), indent=2, sort_keys=True)
    return _human_report(report, debuggers)


def _json_payload(
    report: CapabilityReport, debuggers: Sequence[Debugger] = ()
) -> dict[str, object]:
    """The JSON shape is a public interface — CI runs ``podbench capreport
    --json`` against the built image, and the launcher parses what it prints."""
    return {
        "debuggers": {
            entry.name: {"present": entry.present, "detail": entry.detail}
            for entry in debuggers
        },
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
        # Three fields where there was one `apparmor_profile`, which held an
        # SELinux context on every RHEL-family node and named it wrongly. The
        # module and the string it wrote are separate answers, and `enforcing`
        # is what makes "SELinux is here" into "SELinux said no" (issue #52).
        "lsm": report.lsm.kind.value,
        "lsm_context": report.lsm.context,
        "lsm_enforcing": report.lsm.enforcing,
        "self_uid": report.self_uid,
        "target_uid": report.target_uid,
        "target_pid": report.target_pid,
        "node_name": report.node_name,
        "child_attach_ok": report.child_attach_ok,
        "target_attach_ok": report.target_attach_ok,
        "proc_reads": report.proc_reads,
        # Derived, and emitted anyway, so a shell branching on `--json` gets the
        # same answer as the verdict without re-deriving which of the six reads
        # ptrace actually gates. The launcher deliberately does *not* read it:
        # it recomputes from `proc_reads`, which is what lets a new launcher
        # correct an older image's verdict.
        "reads_ok": report.reads_ok,
        "notes": report.notes,
    }


def _human_report(report: CapabilityReport, debuggers: Sequence[Debugger] = ()) -> str:
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
    # "LSM", not "AppArmor": the label was the bug. A reader who is shown
    # `system_u:system_r:spc_t:s0` under a heading that says AppArmor goes
    # looking for a profile that does not exist (issue #52).
    kv("LSM", report.lsm.summary)
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
        # The count alone reads as a score, and "3/6 ok" looks like half a loaf
        # when it is the whole loaf missing: the three that survive a denial
        # need no permission in the first place (issue #51).
        kv(
            "read-only inspect",
            f"{_yn(report.reads_ok)} - {report.reads_summary}",
        )

    lines.append("PROBES")
    kv("scratch attach (own child)", _attach_text(report.child_attach_ok))
    if report.target_pid is not None:
        kv(
            f"live attach (pid {report.target_pid})",
            _attach_text(report.target_attach_ok),
        )

    if debuggers:
        lines.append("DEBUGGERS (what this image ships)")
        lines.extend(f"  {line}" for line in format_inventory(debuggers))

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
    sysfs: Path = DEFAULT_SYSFS,
    attacher: Attacher | None = None,
) -> int:
    """Run the probe and print the report. Returns the verdict's exit code.

    ``proc``, ``sysfs`` and ``attacher`` are seams for testing against a
    synthetic tree; the CLI passes none of them, which is why the app is built
    here rather than at import time: the command closes over them.
    """
    app = new_app()

    @app.command()
    def capreport(
        pid: Annotated[
            int | None,
            typer.Argument(
                metavar="[PID]",
                help="target pid; discovered from the target container id if omitted",
            ),
        ] = None,
        container_id: Annotated[
            str | None,
            typer.Option(
                "--container-id",
                metavar="ID",
                help=f"target container id (default: ${TARGET_CID_ENV})",
            ),
        ] = None,
        json_output: Annotated[
            bool,
            typer.Option(
                "--json", help="emit the stable JSON form instead of the human report"
            ),
        ] = False,
    ) -> None:
        """Name the mechanism that denies ptrace in this container."""
        raise typer.Exit(
            _run(
                pid,
                container_id,
                json_output=json_output,
                proc=proc,
                sysfs=sysfs,
                attacher=attacher,
            )
        )

    # ``podbench capreport``, matching ``podbench doctor`` and ``podbench
    # agent``: the usage line has to name something a reader can type, and since
    # #47 the image ships no bare ``capreport``.
    return run(app, args, prog="podbench capreport")


def _run(
    pid: int | None,
    container_id: str | None,
    *,
    json_output: bool,
    proc: Path,
    sysfs: Path,
    attacher: Attacher | None,
) -> int:
    notes: list[str] = []
    if pid is None:
        pid, discovery_notes = _discover_target(container_id, proc=proc)
        notes.extend(discovery_notes)

    report = probe(pid, proc=proc, sysfs=sysfs, attacher=attacher, extra_notes=notes)
    print(format_report(report, json_output, inventory()))
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
