"""Tests for the /proc reader and the capability probe.

Everything runs against a synthetic ``/proc`` tree and a fake ptrace backend:
the answers this code produces depend entirely on the kernel it runs on, and CI
runners, the devcontainer and the cluster nodes all differ (the spike found two
arm64 nodes in one cluster that disagree about Yama). A test that consulted the
host's real capabilities would assert whatever the host happened to say.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from podbench.model import Blocker, CapabilityReport, Lsm, LsmStatus, Verdict
from podbench.probe import (
    PTRACE_ATTACH,
    PTRACE_DETACH,
    Attacher,
    AttachOutcome,
    CtypesAttacher,
    SkippedAttacher,
    derive_verdict,
    format_report,
    main,
)
from podbench.probe import probe as _probe
from podbench.proc import (
    Attribution,
    detect_lsm,
    list_processes,
    lsm_context,
    no_new_privs,
    proc_read_matrix,
    read_status_field,
    read_uid,
    scan_processes,
    seccomp_mode,
    self_capabilities,
    strip_container_scheme,
    yama_scope,
)

TARGET_CID = "87d20e2380a1c0ffee0b1e5deadbeef00d15ea5e0000111122223333444455556"
OTHER_CID = "7206c89b11111111222222223333333344444444555555556666666677777777"
OWN_CGROUP = "0::/"

# CapEff with bit 19 set, as measured in the spike's dbg-a container.
CAP_WITH_PTRACE = "00000000a80c25fb"
# The same container minus SYS_PTRACE (dbg-c).
CAP_WITHOUT_PTRACE = "00000000a80425fb"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def make_status(
    uid: int,
    *,
    capeff: str,
    capbnd: str,
    seccomp: int,
    nnp: int,
    tracer_pid: int = 0,
) -> str:
    return (
        "Name:\tpython3\n"
        f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        f"Gid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        f"TracerPid:\t{tracer_pid}\n"
        "CapPrm:\t0000000000000000\n"
        f"CapEff:\t{capeff}\n"
        f"CapBnd:\t{capbnd}\n"
        "CapAmb:\t0000000000000000\n"
        f"Seccomp:\t{seccomp}\n"
        "Seccomp_filters:\t1\n"
        f"NoNewPrivs:\t{nnp}\n"
    )


def make_proc(
    tmp_path: Path,
    *,
    self_uid: int = 1000,
    capeff: str = "0000000000000000",
    capbnd: str = "0000000000000000",
    seccomp: int = 0,
    nnp: int = 0,
    yama: int | None = 1,
    context: str | None = "cri-containerd.apparmor.d (enforce)",
    lsm: Lsm = Lsm.APPARMOR,
    selinux_enforce: int = 1,
    target_uid: int = 1000,
    target_reads: bool = True,
    target_tracer_pid: int = 0,
) -> Path:
    """A synthetic /proc with our own container (pid 42) and a target (pid 1).

    It builds a matching ``/sys`` beside it — see :func:`make_sysfs` — because
    which LSM wrote ``attr/current`` is now read from there rather than guessed
    from the string, and a test that let the real ``/sys`` through would be
    asserting whatever module the runner happens to load (issue #52).
    """
    make_sysfs(tmp_path, lsm=lsm, selinux_enforce=selinux_enforce)
    proc = tmp_path / "proc"

    write(
        proc / "self" / "status",
        make_status(self_uid, capeff=capeff, capbnd=capbnd, seccomp=seccomp, nnp=nnp),
    )
    write(proc / "self" / "comm", "capreport\n")
    write(proc / "self" / "cgroup", OWN_CGROUP + "\n")
    if context is not None:
        write(proc / "self" / "attr" / "current", context + "\n")

    if yama is not None:
        write(proc / "sys" / "kernel" / "yama" / "ptrace_scope", f"{yama}\n")

    # pid 1: the workload, in the target container's cgroup.
    write(
        proc / "1" / "status",
        make_status(
            target_uid,
            capeff="0" * 16,
            capbnd="0" * 16,
            seccomp=0,
            nnp=0,
            tracer_pid=target_tracer_pid,
        ),
    )
    write(proc / "1" / "comm", "python3\n")
    write(proc / "1" / "cmdline", "python3\x00-u\x00-c\x00app\x00")
    write(proc / "1" / "cgroup", f"0::/../cri-containerd-{TARGET_CID}.scope\n")
    if context is not None:
        write(proc / "1" / "attr" / "current", context + "\n")
    if target_reads:
        write(proc / "1" / "maps", "6549010cd000-6549010ce000 r--p /usr/bin/python3\n")
        write(proc / "1" / "environ", "PODBENCH_SECRET_MARKER=s5-environ-canary\x00")
        (proc / "1" / "fd").mkdir(parents=True, exist_ok=True)
        (proc / "1" / "root" / "etc").mkdir(parents=True, exist_ok=True)

    # pid 42: our own debug container.
    write(
        proc / "42" / "status",
        make_status(self_uid, capeff=capeff, capbnd=capbnd, seccomp=seccomp, nnp=nnp),
    )
    write(proc / "42" / "comm", "sleep\n")
    write(proc / "42" / "cmdline", "sleep\x00infinity\x00")
    write(proc / "42" / "cgroup", OWN_CGROUP + "\n")

    return proc


def make_sysfs(
    tmp_path: Path,
    *,
    lsm: Lsm = Lsm.APPARMOR,
    selinux_enforce: int = 1,
) -> Path:
    """A synthetic /sys carrying what the LSMs publish about themselves.

    ``Lsm.NONE`` is a directory with neither file in it, which is a different
    answer from the directory being absent: absent means nothing was ruled out.
    """
    sysfs = tmp_path / "sys"
    sysfs.mkdir(parents=True, exist_ok=True)
    if lsm is Lsm.SELINUX:
        write(sysfs / "fs" / "selinux" / "enforce", f"{selinux_enforce}\n")
    elif lsm is Lsm.APPARMOR:
        write(sysfs / "module" / "apparmor" / "parameters" / "enabled", "Y\n")
    return sysfs


def sysfs_for(proc: Path) -> Path:
    """The /sys tree :func:`make_proc` built beside this /proc."""
    return proc.parent / "sys"


def probe(
    target_pid: int | None,
    *,
    proc: Path,
    sysfs: Path | None = None,
    attacher: Attacher | None = None,
    node_name: str | None = None,
    extra_notes: Sequence[str] = (),
) -> CapabilityReport:
    """The real probe, pointed at the synthetic /sys as well as the /proc.

    Every call in this module goes through here so that none of them can ask
    the *runner's* kernel which LSM is loaded: CI is AppArmor, a RHEL node is
    SELinux, and the answer decides which blocker gets named.
    """
    return _probe(
        target_pid,
        proc=proc,
        sysfs=sysfs if sysfs is not None else sysfs_for(proc),
        attacher=attacher,
        node_name=node_name,
        extra_notes=extra_notes,
    )


class FakeAttacher:
    """A scripted ptrace backend, so a test can pose any of the five denials."""

    def __init__(self, *, child: AttachOutcome, target: AttachOutcome) -> None:
        self._child = child
        self._target = target
        self.attached: list[int] = []

    def attach_child(self) -> AttachOutcome:
        return self._child

    def attach(self, pid: int) -> AttachOutcome:
        self.attached.append(pid)
        return self._target


OK = AttachOutcome(ok=True)
EPERM = AttachOutcome(ok=False, errno=1, message="Operation not permitted")


def attacher(
    *, child: AttachOutcome = OK, target: AttachOutcome = EPERM
) -> FakeAttacher:
    return FakeAttacher(child=child, target=target)


# --------------------------------------------------------------------- proc.py


def test_read_status_field_and_uid(tmp_path: Path) -> None:
    proc = make_proc(tmp_path, self_uid=1000)
    assert read_uid("self", proc=proc) == 1000
    assert read_status_field("self", "CapAmb", proc=proc) == "0000000000000000"
    assert read_status_field("self", "Nonesuch", proc=proc) is None


def test_readers_tolerate_missing_paths(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert read_uid(1, proc=empty) is None
    assert yama_scope(proc=empty) is None
    assert lsm_context("self", proc=empty) is None
    assert seccomp_mode(proc=empty) is None
    assert no_new_privs(proc=empty) is None
    assert self_capabilities(proc=empty).readable is False
    assert list_processes(proc=empty) == []


def test_yama_absent_is_not_zero(tmp_path: Path) -> None:
    assert yama_scope(proc=make_proc(tmp_path, yama=None)) is None
    assert yama_scope(proc=make_proc(tmp_path, yama=0)) == 0


def test_self_capabilities_bit_19(tmp_path: Path) -> None:
    caps = self_capabilities(proc=make_proc(tmp_path, capeff=CAP_WITH_PTRACE))
    assert caps.sys_ptrace_effective is True
    caps = self_capabilities(
        proc=make_proc(tmp_path, capeff=CAP_WITHOUT_PTRACE, capbnd=CAP_WITH_PTRACE)
    )
    assert caps.sys_ptrace_effective is False
    assert caps.sys_ptrace_bounding is True
    assert caps.effective_hex == CAP_WITHOUT_PTRACE


def test_an_empty_attribute_means_unconfined(tmp_path: Path) -> None:
    proc = make_proc(tmp_path, context="")
    assert lsm_context("self", proc=proc) == "unconfined"


def test_the_lsm_is_detected_from_sysfs_not_from_the_context(
    tmp_path: Path,
) -> None:
    """The four-field context is SELinux's, but the shape is not the evidence.

    ``/proc/self/attr/current`` is written by whichever module is loaded, so a
    string that looks like one module's is asserted here against a ``/sys`` that
    says the other. Detection has to follow ``/sys``, or the answer is a guess
    dressed as a measurement (issue #52).
    """
    selinux = make_proc(tmp_path / "a", lsm=Lsm.SELINUX, context=DIAMOND_CONTEXT)
    assert detect_lsm(proc=selinux, sysfs=sysfs_for(selinux)) == LsmStatus(
        Lsm.SELINUX, DIAMOND_CONTEXT, True
    )

    # The same string on a node where AppArmor is the module that wrote it.
    misleading = make_proc(tmp_path / "b", lsm=Lsm.APPARMOR, context=DIAMOND_CONTEXT)
    assert detect_lsm(proc=misleading, sysfs=sysfs_for(misleading)).kind is Lsm.APPARMOR


def test_lsm_detection_distinguishes_absent_from_unreadable(tmp_path: Path) -> None:
    none = make_proc(tmp_path / "a", lsm=Lsm.NONE, context="")
    assert detect_lsm(proc=none, sysfs=sysfs_for(none)) == LsmStatus(
        Lsm.NONE, "unconfined"
    )
    assert detect_lsm(proc=none, sysfs=tmp_path / "nothing").kind is Lsm.UNKNOWN


def test_a_disabled_apparmor_module_is_not_an_active_one(tmp_path: Path) -> None:
    proc = make_proc(tmp_path, lsm=Lsm.NONE, context="unconfined")
    write(sysfs_for(proc) / "module" / "apparmor" / "parameters" / "enabled", "N\n")
    assert detect_lsm(proc=proc, sysfs=sysfs_for(proc)).kind is Lsm.NONE


def test_a_context_nobody_claimed_is_unknown_rather_than_no_lsm(
    tmp_path: Path,
) -> None:
    """The realistic /sys failure, and the one direction that must not lie.

    ``_read_text`` folds ENOENT and EACCES onto the same ``None``, so a seat at
    uid 1000 that may not read ``/sys/module/apparmor/parameters/enabled``, or
    one on an SELinux node with no selinuxfs bind-mounted, looks exactly like a
    node carrying no LSM at all — while ``attr/current`` still holds a context
    somebody wrote. ``Lsm.NONE`` is read as an exoneration downstream, so it is
    not an answer available here. A Smack label lands in the same place, and
    Smack can deny ptrace.
    """
    for context in (DIAMOND_CONTEXT, "_"):
        proc = make_proc(tmp_path / context, lsm=Lsm.NONE, context=context)
        status = detect_lsm(proc=proc, sysfs=sysfs_for(proc))
        assert status == LsmStatus(Lsm.UNKNOWN, context)
        assert status.confines is False


def test_selinux_wins_when_both_modules_publish_state(tmp_path: Path) -> None:
    """Only one exclusive LSM initialises, and a mounted selinuxfs says which.

    AppArmor compiled in and answering ``Y`` does not make it the module that
    wrote the context: ``/sys/fs/selinux/enforce`` exists only once SELinux has
    initialised, so first-match-wins is the right order rather than an accident
    of it.
    """
    proc = make_proc(tmp_path, lsm=Lsm.SELINUX, context=DIAMOND_CONTEXT)
    write(sysfs_for(proc) / "module" / "apparmor" / "parameters" / "enabled", "Y\n")
    assert detect_lsm(proc=proc, sysfs=sysfs_for(proc)).kind is Lsm.SELINUX


def test_an_unparseable_enforce_value_is_not_reported_as_permissive(
    tmp_path: Path,
) -> None:
    """``enforcing`` is three-valued, and the note it drives quotes the file.

    "SELinux is permissive on this node (/sys/fs/selinux/enforce is 0)" must
    not be printed about a string that was never read as 0.
    """
    proc = make_proc(tmp_path, lsm=Lsm.SELINUX, context=DIAMOND_CONTEXT)
    write(sysfs_for(proc) / "fs" / "selinux" / "enforce", "\n")

    status = detect_lsm(proc=proc, sysfs=sysfs_for(proc))
    assert status.enforcing is None
    assert status.confines is False
    report = probe(1, proc=proc, attacher=attacher())
    assert not any("permissive" in note for note in report.notes)


def test_a_complain_mode_profile_does_not_confine(tmp_path: Path) -> None:
    """AppArmor states its mode in the profile string; complain permits.

    Read only once ``/sys`` has said the module is AppArmor — the mode suffix
    is not what decides which module wrote the line.
    """
    proc = make_proc(tmp_path, lsm=Lsm.APPARMOR, context="podbench-custom (complain)")
    status = detect_lsm(proc=proc, sysfs=sysfs_for(proc))
    assert status.enforcing is False
    assert status.confines is False
    assert status.blocker is None


def test_proc_read_matrix(tmp_path: Path) -> None:
    proc = make_proc(tmp_path)
    assert proc_read_matrix(1, proc=proc) == {
        "root": True,
        "maps": True,
        "environ": True,
        "fd": True,
        "cmdline": True,
        "status": True,
    }
    denied = proc_read_matrix(1, proc=make_proc(tmp_path / "b", target_reads=False))
    assert denied == {
        "root": False,
        "maps": False,
        "environ": False,
        "fd": False,
        "cmdline": True,
        "status": True,
    }


def test_attribution_by_container_id(tmp_path: Path) -> None:
    listing = scan_processes(f"containerd://{TARGET_CID}", proc=make_proc(tmp_path))
    assert listing.attribution is Attribution.CONTAINER_ID
    assert listing.warning is None
    assert [p.pid for p in listing.targets] == [1]
    target = listing.targets[0]
    assert target.container_id == TARGET_CID
    assert target.cmdline == "python3 -u -c app"
    assert target.uid == 1000


def test_attribution_by_container_id_ignores_other_sessions(tmp_path: Path) -> None:
    proc = make_proc(tmp_path)
    # A second podbench session attached to the same pod.
    write(proc / "77" / "comm", "sshd\n")
    write(proc / "77" / "cgroup", f"0::/../cri-containerd-{OTHER_CID}.scope\n")
    write(
        proc / "77" / "status",
        make_status(0, capeff="0" * 16, capbnd="0" * 16, seccomp=0, nnp=0),
    )

    assert [p.pid for p in scan_processes(TARGET_CID, proc=proc).targets] == [1]
    # ... whereas the fallback cannot tell them apart, and says so.
    fallback = scan_processes(proc=proc)
    assert [p.pid for p in fallback.targets] == [1, 77]
    assert fallback.attribution is Attribution.CGROUP_FALLBACK
    assert fallback.warning is not None


def test_fallback_skips_pause_and_our_own_processes(tmp_path: Path) -> None:
    proc = make_proc(tmp_path)
    write(proc / "2" / "comm", "pause\n")
    write(proc / "2" / "cgroup", f"0::/../cri-containerd-{OTHER_CID}.scope\n")
    write(
        proc / "2" / "status",
        make_status(0, capeff="0" * 16, capbnd="0" * 16, seccomp=0, nnp=0),
    )

    listing = scan_processes(proc=proc)
    assert [p.pid for p in listing.targets] == [1]
    assert {p.pid for p in listing.processes} == {1, 2, 42}


def test_an_unreadable_uid_is_not_reported_as_root(tmp_path: Path) -> None:
    """`or 0` here would hand the degraded rung a runAsUser of 0.

    That is the one value the report forbids: root without the capability reads
    3/6 of the probe paths where the target's own uid reads 6/6 (report 3.11).
    """
    proc = make_proc(tmp_path)
    (proc / "1" / "status").unlink()

    processes = {p.pid: p for p in list_processes(TARGET_CID, proc=proc)}
    assert processes[1].uid is None
    assert processes[42].uid == 1000


def test_strip_container_scheme() -> None:
    assert strip_container_scheme(f"containerd://{TARGET_CID}") == TARGET_CID
    assert strip_container_scheme(TARGET_CID) == TARGET_CID


# -------------------------------------------------------------------- probe.py


def test_live_attach(tmp_path: Path) -> None:
    report = probe(
        1,
        proc=make_proc(tmp_path, self_uid=0, capeff=CAP_WITH_PTRACE),
        attacher=attacher(target=OK),
        node_name="node02",
    )
    assert report.verdict is Verdict.LIVE_ATTACH
    assert report.blocker is Blocker.NONE
    assert report.can_attach is True
    assert report.verdict.value == 0
    assert report.node_name == "node02"


def test_yama_denies_same_uid(tmp_path: Path) -> None:
    """The spike's config (b): same uid, zero caps, ptrace_scope=1."""
    report = probe(
        1,
        proc=make_proc(tmp_path, self_uid=1000, target_uid=1000, yama=1),
        attacher=attacher(),
    )
    assert report.verdict is Verdict.READ_ONLY
    assert report.blocker is Blocker.YAMA_SCOPE
    assert report.verdict.value == 10
    assert all(report.proc_reads.values())


def test_uid_mismatch(tmp_path: Path) -> None:
    """Config (c): root without the capability — 3/6 reads, credential check.

    Those three reads are `cmdline`, `status` and `fd`, which need no
    permission at all, so this is not read-only inspection: it is launch-only.
    """
    report = probe(
        1,
        proc=make_proc(
            tmp_path,
            self_uid=0,
            capeff=CAP_WITHOUT_PTRACE,
            target_uid=1000,
            target_reads=False,
        ),
        attacher=attacher(),
    )
    assert report.verdict is Verdict.LAUNCH_ONLY
    assert report.blocker is Blocker.UID_MISMATCH
    assert report.proc_reads["maps"] is False
    assert report.proc_reads["status"] is True
    assert report.reads_ok is False


DIAMOND_READS = {
    "root": False,
    "maps": False,
    "environ": False,
    "cmdline": True,
    "status": True,
    "fd": True,
}
"""The read matrix measured on b01-1-beamline/b01-1-blueapi-0, 2026-08-16.

Pinned verbatim because it is the shape that falsifies the old rule: `any` over
these six is True, and every read that makes it True needs no permission. The
seat that produced it was told it had "read-only inspect (/proc/<pid>/root,
maps, environ)" (issue #51).
"""


DIAMOND_CONTEXT = "system_u:system_r:spc_t:s0"
"""The context both the seat and the target carried on that pod.

Four fields, so an SELinux context — and it arrived in the report under the key
`apparmor_profile`, which is how the denial went unrecognised (issue #52). Seat
and target share it, which is what makes the policy question a real one rather
than an obvious cross-domain refusal.
"""


def diamond_proc(tmp_path: Path) -> Path:
    """A /proc matching :data:`DIAMOND_READS` — ptrace-gated reads gone, the
    world-readable ones intact.

    The rest of the accounting is that pod's too: ``ptrace_scope`` 0, seccomp
    filter mode, uid 1000 on both sides and no capability anywhere. Every
    mechanism podbench knew therefore says "not me", which is the state the
    report has to survive without saying ``unknown``.
    """
    proc = make_proc(
        tmp_path,
        self_uid=1000,
        target_uid=1000,
        yama=0,
        seccomp=2,
        lsm=Lsm.SELINUX,
        context=DIAMOND_CONTEXT,
    )
    for name in ("maps", "environ"):
        (proc / "1" / name).unlink()
    (proc / "1" / "root" / "etc").rmdir()
    (proc / "1" / "root").rmdir()
    return proc


def test_the_diamond_shape_is_launch_only_not_read_only(tmp_path: Path) -> None:
    """The regression: three denied reads must outrank three free ones."""
    report = probe(1, proc=diamond_proc(tmp_path), attacher=attacher())

    assert report.proc_reads == DIAMOND_READS
    assert report.child_attach_ok is True
    assert report.target_attach_ok is False
    assert report.reads_ok is False
    assert report.verdict is Verdict.LAUNCH_ONLY
    assert report.verdict.value == 15
    # The useful half of the answer, which a boolean cannot carry.
    assert report.reads_summary == (
        "cmdline, status and fd only; root, maps and environ denied"
    )


def test_the_diamond_shape_says_what_still_works(tmp_path: Path) -> None:
    report = probe(1, proc=diamond_proc(tmp_path), attacher=attacher())
    text = format_report(report, False)
    # "3/6 ok" on its own reads as half a loaf; the line beside it says which
    # half, and that the half that landed is the free one.
    assert "3/6 ok" in text
    assert "read-only inspect" in text
    assert "cmdline, status and fd only" in text
    assert "podbench dbg --launch" in text


def test_the_diamond_shape_names_selinux_rather_than_giving_up(
    tmp_path: Path,
) -> None:
    """The regression for issue #52: everything else says "not me".

    uid 1000 on both sides, no capability in either set, ptrace_scope 0, a
    seccomp mode that permits ptrace, and the seat's own child attaches. The
    only mechanism left is the one whose context was being filed as an AppArmor
    profile, and the report used to answer ``unknown``.
    """
    report = probe(1, proc=diamond_proc(tmp_path), attacher=attacher())

    assert report.blocker is Blocker.SELINUX
    assert report.lsm == LsmStatus(Lsm.SELINUX, DIAMOND_CONTEXT, True)
    assert report.verdict is Verdict.LAUNCH_ONLY
    notes = " ".join(report.notes)
    # The specific rule is not readable from here, so the report has to say
    # where it is instead of inventing one.
    assert "ausearch -m avc -ts recent" in notes
    assert "audit log" in notes
    assert DIAMOND_CONTEXT in notes
    assert "the same context on both sides" in notes
    # And that this rung is not a verdict on the other two modes.
    assert "podbench hotfix" in notes


def test_a_permissive_policy_is_not_blamed_for_a_denial(tmp_path: Path) -> None:
    """Permissive SELinux logs the AVC and allows the call.

    Naming it here would send someone to write a policy module for a policy
    that permitted the syscall, so the honest answer is still ``unknown``.
    """
    proc = make_proc(
        tmp_path,
        self_uid=1000,
        target_uid=1000,
        yama=0,
        lsm=Lsm.SELINUX,
        selinux_enforce=0,
        context=DIAMOND_CONTEXT,
    )
    report = probe(1, proc=proc, attacher=attacher())

    assert report.lsm.enforcing is False
    assert report.blocker is Blocker.UNKNOWN
    assert any("permissive" in note for note in report.notes)


def test_the_unknown_note_no_longer_sends_the_reader_to_apparmor(
    tmp_path: Path,
) -> None:
    """ "check AppArmor and user namespaces" named two things it was not.

    On the pod that produced issue #52 the LSM *was* the answer, and on a pod
    where it is not, saying so is what stops the next reader repeating the
    search.
    """
    proc = make_proc(
        tmp_path, self_uid=1000, target_uid=1000, yama=0, lsm=Lsm.NONE, context=""
    )
    report = probe(1, proc=proc, attacher=attacher())

    assert report.blocker is Blocker.UNKNOWN
    notes = " ".join(report.notes)
    assert "check AppArmor and user namespaces" not in notes
    assert "no LSM confining this seat" in notes


def test_an_unreadable_sys_leaves_the_module_unknown(tmp_path: Path) -> None:
    """Absent /sys is not "no LSM": nothing was ruled out.

    An SELinux context reported as an AppArmor profile is the failure; a
    context reported as nobody's is merely honest.
    """
    proc = make_proc(tmp_path, lsm=Lsm.NONE, context=DIAMOND_CONTEXT)
    report = probe(1, proc=proc, sysfs=tmp_path / "absent", attacher=attacher())

    assert report.lsm == LsmStatus(Lsm.UNKNOWN, DIAMOND_CONTEXT)
    assert report.lsm.confines is False
    assert any("which LSM wrote it is unknown" in note for note in report.notes)


def test_bounding_only_capability_is_flagged(tmp_path: Path) -> None:
    """The silent no-op: SYS_PTRACE added to a non-root container."""
    report = probe(
        1,
        proc=make_proc(
            tmp_path, self_uid=1000, capeff="0" * 16, capbnd=CAP_WITH_PTRACE
        ),
        attacher=attacher(),
    )
    assert report.cap_sys_ptrace is False
    assert report.cap_bounding_sys_ptrace is True
    assert any("bounding set" in note for note in report.notes)


def test_seccomp_blocks_own_child(tmp_path: Path) -> None:
    report = probe(
        1,
        proc=make_proc(tmp_path, seccomp=2),
        attacher=attacher(child=EPERM),
    )
    assert report.blocker is Blocker.SECCOMP
    assert report.child_attach_ok is False
    assert report.verdict is Verdict.READ_ONLY


def test_yama_scope_three_blocks_own_child(tmp_path: Path) -> None:
    proc = make_proc(tmp_path, yama=3, target_reads=False)
    # Nothing about the target is readable either, so there is no fallback left.
    (proc / "1" / "cmdline").unlink()
    (proc / "1" / "status").unlink()
    report = probe(1, proc=proc, attacher=attacher(child=EPERM))
    assert report.blocker is Blocker.YAMA_SCOPE
    assert report.verdict is Verdict.NONE
    assert report.verdict.value == 20


def test_apparmor_blocks_own_child(tmp_path: Path) -> None:
    report = probe(
        1,
        proc=make_proc(tmp_path, seccomp=0, yama=1, context="podbench-custom"),
        attacher=attacher(child=EPERM),
    )
    assert report.blocker is Blocker.APPARMOR


def test_unclassified_structural_failure(tmp_path: Path) -> None:
    report = probe(
        1,
        proc=make_proc(tmp_path, seccomp=0, yama=1, context=""),
        attacher=attacher(child=EPERM),
    )
    assert report.blocker is Blocker.UNKNOWN


def test_denied_despite_capability_is_apparmor(tmp_path: Path) -> None:
    report = probe(
        1,
        proc=make_proc(tmp_path, self_uid=0, capeff=CAP_WITH_PTRACE, target_uid=0),
        attacher=attacher(),
    )
    assert report.blocker is Blocker.APPARMOR


def test_denied_despite_capability_unconfined_is_unknown(tmp_path: Path) -> None:
    report = probe(
        1,
        proc=make_proc(
            tmp_path, self_uid=0, capeff=CAP_WITH_PTRACE, target_uid=0, context=""
        ),
        attacher=attacher(),
    )
    assert report.blocker is Blocker.UNKNOWN


def test_an_existing_tracer_is_named_rather_than_blamed_on_policy(
    tmp_path: Path,
) -> None:
    """A target another debugger holds refuses PTRACE_ATTACH with EPERM too.

    Same uid, Yama at 1: the old classification blamed Yama, sending the user
    to a node sysctl they cannot change and that is not the cause.
    """
    report = probe(
        1,
        proc=make_proc(
            tmp_path, self_uid=1000, target_uid=1000, yama=1, target_tracer_pid=8123
        ),
        attacher=attacher(),
    )
    assert report.blocker is Blocker.ALREADY_TRACED
    assert report.verdict is Verdict.READ_ONLY
    assert any("8123" in note for note in report.notes)


def test_an_existing_tracer_outranks_a_uid_mismatch(tmp_path: Path) -> None:
    # Even with the capability, a second tracer is refused: this is not about
    # the credential check, and saying so would be wrong.
    _, blocker, notes = derive_verdict(
        cap_sys_ptrace=True,
        yama=1,
        seccomp=0,
        lsm=LsmStatus(Lsm.APPARMOR, "cri-containerd.apparmor.d (enforce)", True),
        target_context="custom",
        self_uid=0,
        target_uid=1000,
        target_pid=1,
        tracer_pid=8123,
        child=OK,
        target_attach=EPERM,
        proc_reads={"maps": True},
    )
    assert blocker is Blocker.ALREADY_TRACED
    assert any("8123" in note for note in notes)


def test_no_tracer_still_classifies_the_policy_mechanisms(tmp_path: Path) -> None:
    # TracerPid: 0 must not shadow the four real denials.
    report = probe(
        1,
        proc=make_proc(
            tmp_path, self_uid=1000, target_uid=1000, yama=1, target_tracer_pid=0
        ),
        attacher=attacher(),
    )
    assert report.blocker is Blocker.YAMA_SCOPE


def test_unknown_target_uid_names_the_capability(tmp_path: Path) -> None:
    proc = make_proc(tmp_path)
    (proc / "1" / "status").unlink()
    report = probe(1, proc=proc, attacher=attacher())
    assert report.target_uid is None
    assert report.blocker is Blocker.NO_CAP_SYS_PTRACE


def test_no_target_reports_on_launch_only(tmp_path: Path) -> None:
    report = probe(None, proc=make_proc(tmp_path), attacher=attacher())
    assert report.verdict is Verdict.LIVE_ATTACH
    assert report.blocker is Blocker.NONE
    assert report.target_attach_ok is None
    assert report.proc_reads == {}
    assert any("gdb-launch" in note for note in report.notes)


def test_skipped_probe_does_not_claim_a_measurement(tmp_path: Path) -> None:
    report = probe(
        1,
        proc=make_proc(tmp_path, self_uid=1000, target_uid=1000, yama=1),
        attacher=SkippedAttacher("no libc"),
    )
    assert report.child_attach_ok is None
    assert report.target_attach_ok is None
    assert report.blocker is Blocker.YAMA_SCOPE
    assert report.verdict is Verdict.READ_ONLY
    assert any("skipped" in note for note in report.notes)


def test_skipped_probe_with_capability_names_no_blocker(tmp_path: Path) -> None:
    report = probe(
        1,
        proc=make_proc(tmp_path, self_uid=0, capeff=CAP_WITH_PTRACE),
        attacher=SkippedAttacher("no libc"),
    )
    assert report.blocker is Blocker.NONE
    assert report.verdict is Verdict.READ_ONLY


@pytest.mark.parametrize(
    ("seccomp", "yama", "lsm", "expected"),
    [
        (2, 1, LsmStatus(Lsm.APPARMOR, "unconfined"), Blocker.SECCOMP),
        (0, 3, LsmStatus(Lsm.APPARMOR, "unconfined"), Blocker.YAMA_SCOPE),
        # Scope 2 is the one Yama setting with no descendant exemption, so a
        # scratch attach can fail on it — and it used to fall past this table
        # to "none of the known mechanisms accounts for it", with the scope
        # printed six lines above in the same report.
        (0, 2, LsmStatus(Lsm.APPARMOR, "unconfined"), Blocker.YAMA_SCOPE),
        (0, 1, LsmStatus(Lsm.APPARMOR, "custom"), Blocker.APPARMOR),
        # The structural half of issue #52: ptrace(2) unusable, nothing else to
        # blame, and an enforcing SELinux policy that used to be invisible here.
        (
            0,
            1,
            LsmStatus(Lsm.SELINUX, "system_u:system_r:spc_t:s0", True),
            Blocker.SELINUX,
        ),
        (
            0,
            1,
            LsmStatus(Lsm.SELINUX, "system_u:system_r:spc_t:s0", False),
            Blocker.UNKNOWN,
        ),
        (0, None, LsmStatus(Lsm.NONE), Blocker.UNKNOWN),
    ],
)
def test_structural_classification(
    seccomp: int, yama: int | None, lsm: LsmStatus, expected: Blocker
) -> None:
    _, blocker, _ = derive_verdict(
        cap_sys_ptrace=False,
        yama=yama,
        seccomp=seccomp,
        lsm=lsm,
        target_context=lsm.context,
        self_uid=1000,
        target_uid=1000,
        target_pid=1,
        child=EPERM,
        target_attach=EPERM,
        proc_reads={"maps": True},
    )
    assert blocker is expected


def test_scope_two_with_the_capability_is_not_blamed_on_yama() -> None:
    """The scope-2 branch is conditioned on the capability being absent.

    With CAP_SYS_PTRACE the scratch attach should have succeeded, so a failure
    here is something else and naming Yama would send the user to a node
    sysctl that is not what refused them.
    """
    _, blocker, _ = derive_verdict(
        cap_sys_ptrace=True,
        yama=2,
        seccomp=0,
        lsm=LsmStatus(Lsm.APPARMOR, "unconfined"),
        target_context="unconfined",
        self_uid=1000,
        target_uid=1000,
        target_pid=1,
        child=EPERM,
        target_attach=EPERM,
        proc_reads={"maps": True},
    )
    assert blocker is Blocker.UNKNOWN


def test_no_reads_but_our_own_child_attaches_is_launch_only() -> None:
    verdict, blocker, notes = derive_verdict(
        cap_sys_ptrace=False,
        yama=1,
        seccomp=0,
        lsm=LsmStatus(Lsm.APPARMOR, "unconfined"),
        target_context="unconfined",
        self_uid=0,
        target_uid=1000,
        target_pid=1,
        child=OK,
        target_attach=EPERM,
        proc_reads=dict.fromkeys(("root", "maps", "environ"), False),
    )
    assert verdict is Verdict.LAUNCH_ONLY
    assert blocker is Blocker.UID_MISMATCH
    assert any("podbench dbg --launch" in note for note in notes)


def test_no_reads_and_no_ptrace_at_all_is_verdict_none() -> None:
    """Launch-only needs the scratch attach: gdb traces an inferior it forked,
    which is precisely the attach that failed here."""
    verdict, blocker, _ = derive_verdict(
        cap_sys_ptrace=False,
        yama=3,
        seccomp=0,
        lsm=LsmStatus(Lsm.APPARMOR, "unconfined"),
        target_context="unconfined",
        self_uid=0,
        target_uid=1000,
        target_pid=1,
        child=EPERM,
        target_attach=EPERM,
        proc_reads=dict.fromkeys(("root", "maps", "environ"), False),
    )
    assert verdict is Verdict.NONE
    assert blocker is Blocker.YAMA_SCOPE


def test_a_partly_denied_matrix_still_points_at_the_sysroot() -> None:
    """All-or-nothing is right for the tick and wrong for the prose.

    A matrix that kept `root` fails :func:`ptrace_reads_ok` and lands on
    launch-only, but `set sysroot /proc/<pid>/root` is exactly the mandatory
    fix from report 3.4 — so the note must not steer the reader off it.
    """
    verdict, _, notes = derive_verdict(
        cap_sys_ptrace=False,
        yama=1,
        seccomp=0,
        lsm=LsmStatus(Lsm.APPARMOR, "unconfined"),
        target_context="unconfined",
        self_uid=0,
        target_uid=1000,
        target_pid=1,
        child=OK,
        target_attach=EPERM,
        proc_reads={"root": True, "maps": False, "environ": False},
    )
    assert verdict is Verdict.LAUNCH_ONLY
    prose = " ".join(notes)
    assert "a sysroot on root still will" in prose
    assert "a sysroot, `environ` or `maps` is not the fallback" not in prose


def test_read_only_does_not_claim_gdb_launch_it_never_measured() -> None:
    """`gdb-launch needs no capability` is true and was not the claim being
    made: on a skipped scratch attach nothing about ptrace(2) was measured."""
    verdict, _, notes = derive_verdict(
        cap_sys_ptrace=False,
        yama=1,
        seccomp=0,
        lsm=LsmStatus(Lsm.APPARMOR, "unconfined"),
        target_context="unconfined",
        self_uid=1000,
        target_uid=1000,
        target_pid=1,
        child=AttachOutcome.skip("no libc"),
        target_attach=AttachOutcome.skip("no libc"),
        proc_reads=dict.fromkeys(("root", "maps", "environ"), True),
    )
    assert verdict is Verdict.READ_ONLY
    prose = " ".join(notes)
    assert "gdb-launch is unmeasured" in prose
    assert "gdb-launch needs no capability" not in prose


def test_an_unmeasured_scratch_attach_does_not_claim_launch_only() -> None:
    verdict, _, _ = derive_verdict(
        cap_sys_ptrace=False,
        yama=1,
        seccomp=0,
        lsm=LsmStatus(Lsm.APPARMOR, "unconfined"),
        target_context="unconfined",
        self_uid=0,
        target_uid=1000,
        target_pid=1,
        child=AttachOutcome.skip("no libc"),
        target_attach=AttachOutcome.skip("no libc"),
        proc_reads=dict.fromkeys(("root", "maps", "environ"), False),
    )
    assert verdict is Verdict.NONE


# ------------------------------------------------------- the ptrace backend


class FakeLibc:
    """A libc whose ``ptrace`` can be made to fail on one request.

    Real ptrace cannot be exercised in CI — the runners, the devcontainer and
    the cluster nodes all answer differently — so the backend is tested against
    its own syscall layer instead.
    """

    def __init__(self, *, attach_errno: int = 0, detach_errno: int = 0) -> None:
        self.requests: list[int] = []
        self._failures = {
            PTRACE_ATTACH: attach_errno,
            PTRACE_DETACH: detach_errno,
        }

    def ptrace(self, request: int, pid: int, addr: int, data: int) -> int:
        self.requests.append(request)
        failure = self._failures.get(request, 0)
        ctypes.set_errno(failure)
        return -1 if failure else 0


def ctypes_attacher(libc: FakeLibc) -> CtypesAttacher:
    return CtypesAttacher(cast(ctypes.CDLL, libc))


def test_a_successful_attach_detaches_and_leaves_no_note() -> None:
    libc = FakeLibc()
    outcome = ctypes_attacher(libc).attach(os.getpid())
    assert outcome.ok
    assert libc.requests == [PTRACE_ATTACH, PTRACE_DETACH]
    assert outcome.notes == ()


def test_a_failed_detach_resumes_the_target_rather_than_freezing_it() -> None:
    """PTRACE_ATTACH SIGSTOPs the tracee; an unnoticed ESRCH leaves it stopped.

    capreport freezing the workload it was asked to describe is worse than any
    verdict it could print, so the detach result is checked and the stop lifted.
    """
    libc = FakeLibc(detach_errno=errno.ESRCH)
    # SIGCONT to our own already-running process is a no-op, so this is safe.
    outcome = ctypes_attacher(libc).attach(os.getpid())
    assert outcome.ok
    assert libc.requests == [PTRACE_ATTACH, PTRACE_DETACH]
    assert len(outcome.notes) == 1
    assert "SIGCONT" in outcome.notes[0]
    assert "No such process" in outcome.notes[0]


def test_a_failed_detach_that_cannot_be_resumed_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(pid: int, sig: int) -> None:
        raise ProcessLookupError("No such process")

    monkeypatch.setattr(os, "kill", refuse)
    outcome = ctypes_attacher(FakeLibc(detach_errno=errno.ESRCH)).attach(os.getpid())
    assert outcome.ok
    assert "SIGCONT failed too" in outcome.notes[0]


def test_a_failed_attach_never_detaches() -> None:
    # Nothing was attached, so there is no stop to lift and nothing to say.
    libc = FakeLibc(attach_errno=errno.EPERM)
    outcome = ctypes_attacher(libc).attach(4242)
    assert not outcome.ok
    assert outcome.errno == errno.EPERM
    assert libc.requests == [PTRACE_ATTACH]
    assert outcome.notes == ()


def test_an_unresumable_target_is_reported_in_the_notes(tmp_path: Path) -> None:
    # The note has to survive the trip from the backend into the report the
    # user actually reads.
    left_stopped = AttachOutcome(ok=True, notes=("pid 1 may be left stopped",))
    report = probe(
        1,
        proc=make_proc(tmp_path, self_uid=0, capeff=CAP_WITH_PTRACE),
        attacher=attacher(target=left_stopped),
    )
    assert "pid 1 may be left stopped" in report.notes


# ------------------------------------------------------------- report rendering


JSON_KEYS = {
    "verdict",
    "exit_code",
    "summary",
    "blocker",
    "explanation",
    "cap_sys_ptrace",
    "cap_bounding_sys_ptrace",
    "yama_scope",
    "seccomp_mode",
    "no_new_privs",
    # Three fields where `apparmor_profile` was one, and named the wrong module
    # on every SELinux node (issue #52).
    "lsm",
    "lsm_context",
    "lsm_enforcing",
    "self_uid",
    "target_uid",
    "target_pid",
    "node_name",
    "child_attach_ok",
    "target_attach_ok",
    "proc_reads",
    # The corrected boolean, so a shell branching on --json need not know which
    # three of the six reads ptrace gates (issue #51).
    "reads_ok",
    "notes",
    # What the image ships, beside what the kernel allows: a seat that may
    # attach but has no adapter for the target's language fails at F5 with an
    # error naming neither (issue #20).
    "debuggers",
}


def test_json_shape_is_stable(tmp_path: Path) -> None:
    report = probe(
        1,
        proc=make_proc(tmp_path, self_uid=1000, target_uid=1000, yama=1),
        attacher=attacher(),
        node_name="nuc2",
    )
    payload = json.loads(format_report(report, True))
    assert set(payload) == JSON_KEYS
    assert payload["verdict"] == "read_only"
    assert payload["exit_code"] == 10
    assert payload["blocker"] == "yama-scope"
    assert payload["yama_scope"] == 1
    assert payload["node_name"] == "nuc2"
    assert payload["child_attach_ok"] is True
    assert payload["target_attach_ok"] is False
    assert payload["proc_reads"]["root"] is True


def test_json_carries_the_module_beside_the_context(tmp_path: Path) -> None:
    """One field held both answers and got one of them wrong.

    A consumer that reads ``lsm_context`` alone can no longer conclude AppArmor
    from it, which is the whole of the rename (issue #52).
    """
    report = probe(1, proc=diamond_proc(tmp_path), attacher=attacher())
    payload = json.loads(format_report(report, True))

    assert payload["lsm"] == "selinux"
    assert payload["lsm_context"] == DIAMOND_CONTEXT
    assert payload["lsm_enforcing"] is True
    assert payload["blocker"] == "selinux"
    assert "ausearch -m avc" in payload["explanation"]


def test_the_human_report_names_the_module_it_read(tmp_path: Path) -> None:
    report = probe(1, proc=diamond_proc(tmp_path), attacher=attacher())
    text = format_report(report, False)

    assert "AppArmor" not in text
    assert f"LSM selinux (enforcing) - {DIAMOND_CONTEXT}" in " ".join(text.split())


def test_json_verdict_names_round_trip(tmp_path: Path) -> None:
    report = probe(
        1,
        proc=make_proc(tmp_path, self_uid=0, capeff=CAP_WITH_PTRACE),
        attacher=attacher(target=OK),
    )
    payload = json.loads(format_report(report, True))
    assert payload["verdict"] == "live_attach"
    assert payload["exit_code"] == 0
    assert payload["blocker"] == "none"


def test_human_report_names_the_mechanism(tmp_path: Path) -> None:
    report = probe(
        1,
        proc=make_proc(tmp_path, self_uid=1000, target_uid=1000, yama=1),
        attacher=attacher(),
    )
    text = format_report(report, False)
    assert "capreport" in text
    assert "yama-scope" in text
    assert Blocker.YAMA_SCOPE.explanation.split(":")[0] in text
    assert "scratch attach (own child) OK" in text
    assert "live attach (pid 1)" in text


def test_human_report_without_target(tmp_path: Path) -> None:
    report = probe(None, proc=make_proc(tmp_path), attacher=attacher())
    text = format_report(report, False)
    assert "TARGET" not in text
    assert "live attach (pid" not in text


# --------------------------------------------------------------------- the CLI


def test_main_returns_the_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proc = make_proc(tmp_path, self_uid=1000, target_uid=1000)
    code = main(["1", "--json"], proc=proc, sysfs=sysfs_for(proc), attacher=attacher())
    assert code == Verdict.READ_ONLY.value
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == code


def test_main_discovers_the_target_from_the_container_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proc = make_proc(tmp_path, self_uid=1000, target_uid=1000)
    code = main(
        ["--json", "--container-id", f"containerd://{TARGET_CID}"],
        proc=proc,
        sysfs=sysfs_for(proc),
        attacher=attacher(),
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["target_pid"] == 1
    assert code == Verdict.READ_ONLY.value


def test_main_refuses_to_guess_the_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """PID 1 is /pause under shareProcessNamespace, so no id means no target."""
    monkeypatch.delenv("PODBENCH_TARGET_CID", raising=False)
    proc = make_proc(tmp_path)
    code = main(["--json"], proc=proc, sysfs=sysfs_for(proc), attacher=attacher())
    payload = json.loads(capsys.readouterr().out)
    assert payload["target_pid"] is None
    assert code == Verdict.LIVE_ATTACH.value
    assert any("PODBENCH_TARGET_CID" in note for note in payload["notes"])
