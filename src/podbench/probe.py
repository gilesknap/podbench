"""``capreport`` — name the mechanism that denied ptrace, before it costs an
afternoon.

Four unrelated subsystems (the credential check, Yama, seccomp and whichever
LSM the node runs) refuse ``PTRACE_ATTACH`` with the same bare ``EPERM``, and
gdb makes it worse by reporting a stale ``ENOTTY`` as
``ptrace: Inappropriate ioctl for device``
(spike S5, finding 4). So the probe never infers the answer from a failure: it
reads the kernel's own accounting, then measures ptrace twice.

Neither measurement stops the workload. The live one is a ``PTRACE_SEIZE``,
which takes the same permission check as gdb's attach and leaves the tracee
running (finding 14); ``PTRACE_ATTACH``, which stops it, is kept for the scratch
attach on our own child and as the fallback on a kernel older than 3.4.

The two measurements are the whole trick. The first attaches to a child the
probe **forked itself** — the credential check always passes against your own
child and Yama exempts descendants below ``ptrace_scope=2``, so a failure there
cannot be a policy decision about the target and must be structural: a seccomp
filter rejecting the syscall, ``ptrace_scope`` 2 or 3, or the node's LSM. Only
if that succeeds is a failure on the target informative about the target.

Exit codes are :class:`~podbench.model.Verdict`'s values — 0 live attach,
10 read-only, 15 launch-only, 20 nothing — so a shell can branch without
parsing anything.
"""

from __future__ import annotations

import ctypes
import errno
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
    ATTACH_PROBE,
    SEIZE_PROBE,
    TARGET_CID_ENV,
    Blocker,
    CapabilityReport,
    Lsm,
    Verdict,
    describe_gated_fallback,
    describe_lsm_remedy,
    describe_pause,
    describe_reads,
    ptrace_reads_ok,
)
from .proc import (
    DEFAULT_PROC,
    DEFAULT_SYS,
    candidate_note,
    debug_candidates,
    env_target_container_id,
    lsm_label,
    no_new_privs,
    proc_read_matrix,
    read_gid,
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
    "discover_target",
    "derive_verdict",
    "format_report",
    "main",
    "probe",
]

PTRACE_ATTACH = 16
PTRACE_DETACH = 17
PTRACE_SEIZE = 0x4206

SEIZE_OPTIONS = 0
"""``PTRACE_SEIZE``'s ``data`` word, which is an options mask and not padding.

The old helper hard-wired ``(request, pid, 0, 0)`` and so would have passed this
by accident. It is spelled out because of what else lives in this word:
``PTRACE_O_EXITKILL`` (0x00100000) asks the kernel to **SIGKILL the tracee when
the tracer exits**, and the tracer exiting is exactly how
:meth:`CtypesAttacher.attach` detaches. A stray options word would turn a probe
that stops nothing into one that kills the workload.
"""

_SEIZE_UNREPORTED = 255
"""Exit code for a seizing child that could not name an errno.

Well clear of every errno ``ptrace(2)`` returns (EPERM, ESRCH, EIO, EFAULT,
EBUSY, EINVAL), which is what the rest of the exit status carries.
"""

_CHILD_LIFETIME_SECONDS = 30
"""How long the scratch child may live if nothing kills it.

Belt to :func:`_kill`'s braces, and the other half of #92: the child holds the
inherited stdout, and ``kubectl exec`` returns only once every holder of that
pipe has gone. A ``signal.pause()`` with no alarm meant one refused ``kill``
hung the launcher for ever; SIGALRM's default action is to terminate, so the
worst case is now bounded by this constant instead. Orders of magnitude longer
than the microseconds the probe needs, so it cannot fire mid-measurement.
"""

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
    """What one ptrace attempt did, and what it cost the tracee."""

    ok: bool
    errno: int = 0
    message: str = "OK"
    skipped: bool = False
    """No way to issue ptrace at all, so this is not evidence either way."""

    method: str = ""
    """Which primitive produced this - :data:`~podbench.model.SEIZE_PROBE` or
    :data:`~podbench.model.ATTACH_PROBE` - empty when nothing was issued.

    Carried rather than assumed by the reader, because the two cost the tracee
    different things and the report has to be able to say which was paid."""

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
    """Something that can issue a ptrace attach."""

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

    def _ptrace(
        self, request: int, pid: int, method: str, *, data: int = 0
    ) -> AttachOutcome:
        # `data` is passed rather than left to the old hard-wired zero: it is
        # PTRACE_SEIZE's options word, and see SEIZE_OPTIONS for what one of
        # those bits would do to the workload.
        ctypes.set_errno(0)
        rc = int(self._libc.ptrace(request, pid, 0, data))
        err = ctypes.get_errno()
        if rc == 0:
            return AttachOutcome(ok=True, method=method)
        return AttachOutcome(
            ok=False, errno=err, message=os.strerror(err), method=method
        )

    def attach(self, pid: int) -> AttachOutcome:
        """Probe ptrace against a process this container did not fork.

        The question is only whether the kernel would let gdb attach, and
        ``PTRACE_SEIZE`` answers it through the same
        ``PTRACE_MODE_ATTACH_REALCREDS`` check without stopping the workload -
        so the answer arrives with no pause to reap, no detach to race, and no
        window in which a failed detach leaves the pod frozen. ``PTRACE_ATTACH``
        remains, as the fallback for a kernel older than 3.4.
        """
        seize = self._seize_from_a_child(pid)
        if seize is not None and (seize.ok or seize.errno != errno.EIO):
            # EIO is the only "ask the other way" answer: a denial from SEIZE is
            # a denial of the identical credential check ATTACH would take, and
            # retrying it would stop a workload to be told the same thing.
            return seize
        return self._attach_and_detach(pid)

    def _seize_from_a_child(self, pid: int) -> AttachOutcome | None:
        """Seize ``pid`` from a process that immediately exits. ``None`` if the
        fork failed, so the caller can fall back rather than invent a verdict.

        The fork is what makes the *undo* free, and it is not decoration.
        ``PTRACE_DETACH`` needs its tracee in a ptrace-stop and answers ESRCH to
        a seized-but-running one (measured), so the only detach that costs no
        stop is the kernel's own: a tracer that exits detaches every tracee it
        holds and leaves it running. That also bounds the one hazard a seize
        carries - while a process is traced, any signal delivered to it traps
        and waits for its tracer, and a tracer off formatting a report is a
        tracee frozen until the report is printed (measured: a seized process
        sending ticks stopped dead on SIGUSR1 and resumed the moment its tracer
        exited). Here the tracer's whole life is one syscall.

        The seize is *not* done this way for :meth:`attach_child`, and must not
        be: Yama ``ptrace_scope=1`` exempts descendants of the tracer and
        nothing else, so a grandchild seizing its sibling is refused EPERM
        where this process seizing its own child is allowed (report 3.12's
        second row, re-measured in the devcontainer, which runs at scope 1).
        A structural probe that answered EPERM on every Yama node would report
        the seat as broken and withdraw the one rung that still works.
        """
        try:
            child = os.fork()
        except OSError:
            return None
        if child == 0:  # pragma: no cover - the child never returns
            code = _SEIZE_UNREPORTED
            try:
                outcome = self._ptrace(
                    PTRACE_SEIZE, pid, SEIZE_PROBE, data=SEIZE_OPTIONS
                )
                if outcome.ok:
                    code = 0
                elif 0 < outcome.errno < _SEIZE_UNREPORTED:
                    code = outcome.errno
            finally:
                # The seize is undone by this exit, so nothing here may run an
                # exit handler, flush an inherited buffer, or raise on the way.
                os._exit(code)
        status = _wait_status(child)
        if status is None:
            return AttachOutcome.skip(
                "could not reap the child that probes ptrace; nothing was measured"
            )
        if os.WIFSIGNALED(status):
            # A SECCOMP_MODE_STRICT filter kills the caller of ptrace(2) outright.
            # In a child that is a measurement; in this process it would have been
            # capreport dying instead of reporting.
            return AttachOutcome(
                ok=False,
                method=SEIZE_PROBE,
                message=(
                    f"the probe's own child was killed by signal "
                    f"{os.WTERMSIG(status)} issuing {SEIZE_PROBE}"
                ),
            )
        code = os.WEXITSTATUS(status)
        if code == 0:
            return AttachOutcome(ok=True, method=SEIZE_PROBE)
        if code == _SEIZE_UNREPORTED:
            return AttachOutcome(
                ok=False,
                method=SEIZE_PROBE,
                message=f"{SEIZE_PROBE} failed and the probe's child named no errno",
            )
        return AttachOutcome(
            ok=False, errno=code, message=os.strerror(code), method=SEIZE_PROBE
        )

    def _attach_and_detach(self, pid: int) -> AttachOutcome:
        """The stopping primitive, and everything undoing it takes.

        Kept whole for the two callers that still need it - a pre-3.4 kernel,
        and the scratch attach on our own child - because the goal was that the
        stop need not happen, not that the safety net could go. A bare
        ``PTRACE_ATTACH`` with no ``waitpid``, hand-rolled against live blueapi
        on 2026-08-19, left it ``State: T (stopped)`` for ninety seconds.
        """
        outcome = self._ptrace(PTRACE_ATTACH, pid, ATTACH_PROBE)
        if not outcome.ok:
            return outcome
        # PTRACE_ATTACH leaves the tracee SIGSTOPped until it is detached, so
        # reaping the stop and detaching is not optional politeness.
        _reap(pid)
        detach = self._ptrace(PTRACE_DETACH, pid, ATTACH_PROBE)
        if detach.ok:
            return outcome
        # A detach can genuinely fail — an ESRCH swallowed by waitpid, a tracee
        # that changed state underneath us — and an unnoticed failure leaves the
        # workload stopped. capreport freezing the process it was asked to
        # describe is worse than any verdict it could print, so the stop is
        # lifted by hand and said out loud.
        return AttachOutcome(
            ok=True, method=ATTACH_PROBE, notes=(_resume(pid, detach),)
        )

    def attach_child(self) -> AttachOutcome:
        """Attach to a child forked for the purpose.

        ``PTRACE_ATTACH`` and not the seize: this tracee exists to be stopped,
        it is killed a line later, and the question here is the structural one -
        whether ptrace(2) can be issued at all - which is best asked with the
        call gdb itself makes.
        """
        pid = os.fork()
        if pid == 0:  # pragma: no cover - the child never returns
            try:
                # Bounded rather than endless: see _CHILD_LIFETIME_SECONDS for
                # what an unkillable child does to the launcher (#92).
                signal.alarm(_CHILD_LIFETIME_SECONDS)
                while True:
                    signal.pause()
            finally:
                os._exit(0)
        try:
            return self._attach_and_detach(pid)
        finally:
            _kill(pid)
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


def _kill(pid: int) -> None:
    """SIGKILL a child, without letting the kill become the failure.

    Unguarded, this was #92: an ``os.kill`` that raises skips the ``_reap``
    below it, and the forked child goes on calling ``signal.pause()`` holding
    the ``kubectl exec`` pipes open - so the launcher waits for an EOF that
    never comes, however the probe itself turned out. There is nothing to do
    about a refused kill except carry on and let the child's own alarm end it.
    """
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _reap(pid: int) -> None:
    # WUNTRACED, because the stop this is here to collect is not the only stop
    # that can happen to the tracee. A ptrace-stop is reported to the tracer
    # with or without the flag, but a child stopped for any other reason is not,
    # and a wait that returns only for the stop we expected is a wait that hangs
    # when the expectation is wrong.
    try:
        os.waitpid(pid, os.WUNTRACED)
    except OSError:
        pass


def _wait_status(pid: int) -> int | None:
    """The raw wait status of one of our own children, or ``None``."""
    try:
        return os.waitpid(pid, 0)[1]
    except OSError:
        return None


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
    sysfs: Path = DEFAULT_SYS,
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
    # Asked for beside the uid, never derived from it. The seat's gid is a
    # separate pin (`runAsGroup`) that a manifest silent about it leaves at the
    # image's - gid 0 on podbench's - so the two ids disagree far more often
    # than they look like they would (#103).
    self_gid = read_gid("self", proc=proc)
    if self_gid is None:
        self_gid = os.getgid()
        notes.append("could not read our own gid from /proc; used getgid()")

    seccomp = seccomp_mode(proc=proc)
    if seccomp is None:
        seccomp = 0
        notes.append("no Seccomp field in /proc/self/status; assumed disabled")
    nnp = no_new_privs(proc=proc)
    if nnp is None:
        nnp = False
        notes.append("no NoNewPrivs field in /proc/self/status; assumed unset")

    yama = yama_scope(proc=proc)
    lsm, label_self = lsm_label("self", proc=proc, sysfs=sysfs)
    label_target: str | None = None
    target_uid: int | None = None
    target_gid: int | None = None
    tracer_pid: int | None = None
    reads: dict[str, bool] = {}
    if target_pid is not None:
        _, label_target = lsm_label(target_pid, proc=proc, sysfs=sysfs)
        target_uid = read_uid(target_pid, proc=proc)
        # `status` needs no ptrace permission, so this is readable from a seat
        # that can do nothing else to the target - which is the whole point:
        # it is how a seat landed at the wrong gid reports the right one.
        target_gid = read_gid(target_pid, proc=proc)
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

    node = node_name if node_name is not None else _node_name_from_env()

    verdict, blocker, derived = derive_verdict(
        cap_sys_ptrace=caps.sys_ptrace_effective,
        yama=yama,
        seccomp=seccomp,
        lsm=lsm,
        lsm_label=label_self,
        lsm_label_target=label_target,
        node_name=node,
        self_uid=self_uid,
        target_uid=target_uid,
        self_gid=self_gid,
        target_gid=target_gid,
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
        lsm_label=label_self,
        lsm_label_target=label_target,
        self_uid=self_uid,
        target_uid=target_uid,
        self_gid=self_gid,
        target_gid=target_gid,
        target_pid=target_pid,
        node_name=node,
        child_attach_ok=child.measured_ok,
        target_attach_ok=(
            target_attach.measured_ok if target_attach is not None else None
        ),
        # What the live attach cost the workload is derived from this and
        # nothing else, so an unprobed target must leave it None rather than
        # inherit the primitive some other attempt happened to use.
        attach_method=(
            target_attach.method or None if target_attach is not None else None
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
    if seccomp == 2 and child.ok:
        # Conditioned on the child attach, not on the mode. This note used to be
        # printed for any filter and asserted that RuntimeDefault permits
        # ptrace; at DLS on 2026-08-18 it appeared in a report that named
        # seccomp as the blocker, telling the reader the filter allows the
        # syscall it had just measured being refused. Where the child attach
        # worked the filter demonstrably permits ptrace and the ASLR half is
        # the useful part; where it did not, `_classify_structural` says so.
        notes.append(
            "this seccomp filter permits ptrace but blocks "
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
    lsm: Lsm,
    lsm_label: str | None,
    lsm_label_target: str | None,
    node_name: str | None,
    self_uid: int,
    target_uid: int | None,
    self_gid: int,
    target_gid: int | None,
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
            node_name=node_name,
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
            self_gid=self_gid,
            target_gid=target_gid,
            tracer_pid=tracer_pid,
            yama=yama,
            lsm=lsm,
            lsm_label=lsm_label,
            lsm_label_target=lsm_label_target,
            node_name=node_name,
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
        return Verdict.LAUNCH_ONLY, blocker, notes
    return Verdict.NONE, blocker, notes


def _classify_structural(
    *,
    seccomp: int,
    yama: int | None,
    lsm: Lsm,
    node_name: str | None,
    cap_sys_ptrace: bool,
    child: AttachOutcome,
) -> tuple[Blocker, list[str]]:
    notes = [
        f"PTRACE_ATTACH failed on our own forked child ({child.message}), so "
        "ptrace(2) itself is unusable here - this is not about the target"
    ]
    if seccomp in (1, 2):
        # No longer an untested branch. S5 could not install a profile that
        # denied ptrace and report R7 concluded RuntimeDefault allows it, which
        # is true only of the runtimes R7 ran on: at DLS on 2026-08-18 a seat
        # at Seccomp 2 was refused PTRACE_ATTACH on its own forked child, on a
        # node where the same pod's unfiltered root seat had managed it. That
        # profile was podbench's own — see `spec.target_seccomp_profile` for
        # why the rungs now mirror the target instead of imposing one.
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
    # No LSM arm here, and deliberately: a forked child inherits our own label,
    # so there is no pair to compare and naming the LSM would be the verdict
    # off one label that #104 removed. What the reader gets instead is where to
    # find the denial if there was one.
    notes.append(describe_lsm_remedy(lsm, node_name))
    return Blocker.UNKNOWN, notes


def _classify_denial(
    *,
    self_uid: int,
    target_uid: int | None,
    self_gid: int,
    target_gid: int | None,
    tracer_pid: int | None,
    yama: int | None,
    lsm: Lsm,
    lsm_label: str | None,
    lsm_label_target: str | None,
    node_name: str | None,
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
            "capabilities, Yama or its security label is implicated"
        ]
    if cap_sys_ptrace:
        if _labels_differ(lsm_label, lsm_label_target):
            return Blocker.LSM_MISMATCH, [
                f"{lsm.title} labels differ: ours is {lsm_label!r} and the "
                f"target's is {lsm_label_target!r}; ptrace is permitted within "
                "one label, not across two",
                describe_lsm_remedy(lsm, node_name),
            ]
        return Blocker.UNKNOWN, [
            "CAP_SYS_PTRACE is effective and our own child attached fine, so "
            "this is not a capability problem",
            describe_lsm_remedy(lsm, node_name),
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
    if target_gid is not None and self_gid != target_gid:
        # Between the uid arm and Yama because that is where the kernel puts it.
        # `__ptrace_may_access()` requires six equalities - uid/euid/suid and
        # gid/egid/sgid - and returns before Yama is consulted if any of them
        # fails, so a seat that matched the uid alone was being sent off to
        # investigate AppArmor and user namespaces by the UNKNOWN arm below
        # (#103, measured on p47-blueapi-0: seat 1000:0, target 1000:1000).
        return Blocker.GID_MISMATCH, [
            f"tracer gid={self_gid}, target gid={target_gid}: the uids match "
            "but __ptrace_may_access() compares the group ids as peers of the "
            "user ids, so the credential check still fails - and with it "
            "/proc/<pid>/root, maps, environ and exe, which need "
            "PTRACE_MODE_READ and are gated by the same check"
        ]
    if yama not in (None, 0):
        return Blocker.YAMA_SCOPE, [
            "both processes have the same uid and gid so the credential check "
            "passes; "
            "ptrace_scope is a node sysctl and /proc/sys is read-only in the "
            "container, so no securityContext change fixes it. The target can "
            "opt in with prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)"
        ]
    # The tail that used to send every reader to "check AppArmor and user
    # namespaces" - on nodes that run SELinux, and on a bed that runs neither.
    # It now names whatever is actually there, and where its log is (#104).
    return Blocker.UNKNOWN, [
        "same uid and gid, no existing tracer, no Yama restriction and no "
        "capability in play, yet attach was refused"
        + (
            f"; the {lsm.title} labels match ({lsm_label!r}), so the label is "
            "not the reason"
            if lsm is not Lsm.NONE and not _labels_differ(lsm_label, lsm_label_target)
            else ""
        ),
        describe_lsm_remedy(lsm, node_name),
    ]


def _labels_differ(label: str | None, target_label: str | None) -> bool:
    """Whether two LSM labels are a *pair* that disagrees.

    Both sides must be known. A missing label is "we could not read it", which
    is the ordinary answer for a target the seat has no PTRACE_MODE_READ on -
    exactly the seat that is already being denied - so treating absence as
    difference would blame the LSM for every credential failure.
    """
    return label is not None and target_label is not None and label != target_label


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
        # `apparmor_profile` was the published name for this string back when
        # podbench believed it was always an AppArmor profile. It is kept, and
        # kept meaning "the seat's label", because the versioning rule cuts
        # both ways: an older launcher reads it and a newer one still parses an
        # older seat's report. The LSM it belongs to, and the target's label to
        # compare it with, are new keys - so an older launcher ignores them and
        # a newer one reads their absence as "this seat is older than the
        # question", which `Lsm.NONE` and `None` say honestly (#104).
        "apparmor_profile": report.lsm_label,
        "lsm": report.lsm.value,
        "lsm_label": report.lsm_label,
        "lsm_label_target": report.lsm_label_target,
        "self_uid": report.self_uid,
        "target_uid": report.target_uid,
        # Added after the shape was published, so every consumer must treat a
        # missing key as "this seat is older than the question" and not as a
        # gid of 0 - `capability_report_from_json` does, and the correction the
        # launcher makes off the back of it declines to act on `None`.
        "self_gid": report.self_gid,
        "target_gid": report.target_gid,
        "target_pid": report.target_pid,
        "node_name": report.node_name,
        "child_attach_ok": report.child_attach_ok,
        "target_attach_ok": report.target_attach_ok,
        # Added after the shape was published, so an older launcher ignores it
        # and a newer one reads a missing key as "this seat is older than the
        # question" - which `describe_pause` renders as "not measured", never
        # as an assurance that nothing was stopped.
        "attach_method": report.attach_method,
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
    kv("gid", report.self_gid if report.self_gid is not None else "?")
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
    # Under the LSM's own name, because the generic attribute this comes from
    # is shared and podbench used to attribute it to AppArmor wherever it read
    # (#104). The target's label gets its own row below, next to the uid and
    # gid it is compared exactly like.
    kv(report.lsm.title, _label_text(report.lsm, report.lsm_label))
    kv("Yama ptrace_scope", _yama_text(report.yama_scope))
    kv("node", report.node_name or "unknown")

    if report.target_pid is not None:
        lines.append(f"TARGET (pid {report.target_pid})")
        kv("uid", report.target_uid if report.target_uid is not None else "?")
        kv("gid", report.target_gid if report.target_gid is not None else "?")
        if report.lsm is not Lsm.NONE:
            kv(report.lsm.title, _target_label_text(report))
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
            f"{_attach_text(report.target_attach_ok)}"
            + (f" via {report.attach_method}" if report.attach_method else ""),
        )
        # Stated on every report, not only when a pause happened: what the probe
        # does to the workload is exactly the thing a reader of this report is
        # entitled to check before running it, and "none" is only reassuring if
        # it is the same line that would have said otherwise.
        kv(
            "workload pause",
            describe_pause(report.attach_method, report.target_attach_ok),
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


def _label_text(lsm: Lsm, label: str | None) -> str:
    if lsm is Lsm.NONE:
        # Said out loud rather than left blank: "no LSM" is the answer that
        # stops a reader hunting for an AppArmor profile that cannot exist.
        return "none - neither SELinux nor AppArmor on this node"
    return label or "no label"


def _target_label_text(report: CapabilityReport) -> str:
    label = report.lsm_label_target
    if label is None:
        return "unreadable"
    if report.lsm_label is None:
        return label
    return f"{label} ({'same as tracer' if label == report.lsm_label else 'MISMATCH'})"


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
    sysfs: Path = DEFAULT_SYS,
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
        pid, discovery_notes = discover_target(container_id, proc=proc)
        notes.extend(discovery_notes)

    report = probe(pid, proc=proc, sysfs=sysfs, attacher=attacher, extra_notes=notes)
    print(format_report(report, json_output, inventory()))
    return report.verdict.value


def discover_target(
    container_id: str | None = None, *, proc: Path = DEFAULT_PROC
) -> tuple[int | None, list[str]]:
    """Find the target pid from the container id, saying how sure we are.

    With no container id at all we deliberately refuse to guess: "the target is
    PID 1" is wrong under ``shareProcessNamespace: true``, where PID 1 is
    ``/pause`` (report §3.15).

    **The pid is always named.** Every number in the report below - the read
    matrix, the live attach, the verdict - is a measurement of one process, and
    a report that does not say which one cannot be checked or disagreed with.

    **A dead process is not scored.** A zombie has no ``mm_struct``, so
    ``maps`` opens with no ptrace check and the read matrix comes back "3/6 ok"
    while the attach comes back ``EPERM`` - which reads exactly like a partial
    LSM denial and was diagnosed as one for months (issue #52). Reporting on
    this seat alone is the honest answer: the denial is not about the seat.
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
    candidates = debug_candidates(targets)
    chosen = candidates[0]
    if not chosen.alive:
        # The ranking sorts the living first, so a dead head means they are all
        # dead. There is no pid here worth a verdict.
        return None, [
            *notes,
            f"the best of {len(candidates)} processes in the target container is "
            f"pid {chosen.pid} ({chosen.comm}), and it is dead (state "
            f"{chosen.state}): no seat can attach to a zombie, so this report "
            "covers this container only. Run `podbench pids` for the container's "
            "state",
        ]
    note = candidate_note(candidates, "probing")
    # candidate_note answers "why this one of several" and returns None when
    # there was only one. The identity is owed either way, so the one-process
    # case says it in the short form rather than saying nothing.
    notes.append(
        note
        if note is not None
        else f"probing pid {chosen.pid} ({chosen.comm}), the only process here"
    )
    if chosen.ptrace_readable is False:
        # Said before the reads are taken, not derived from them afterwards:
        # `status`, `cmdline` and `fd` answer on any pod whatsoever, so a
        # matrix taken at the wrong credentials still scores 3/6 and the number
        # flatters the seat unless something names the reason next to it.
        notes.append(
            f"pid {chosen.pid} is not ptrace-readable from this seat, and it is "
            "the most readable process in the container: the /proc reads below "
            "are the ones that need no permission"
        )
    return chosen.pid, notes
