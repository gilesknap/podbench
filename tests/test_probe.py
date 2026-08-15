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
from pathlib import Path
from typing import cast

import pytest

from podbench.model import Blocker, Verdict
from podbench.probe import (
    PTRACE_ATTACH,
    PTRACE_DETACH,
    AttachOutcome,
    CtypesAttacher,
    SkippedAttacher,
    derive_verdict,
    format_report,
    main,
    probe,
)
from podbench.proc import (
    Attribution,
    apparmor_profile,
    list_processes,
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
    apparmor: str | None = "cri-containerd.apparmor.d (enforce)",
    target_uid: int = 1000,
    target_reads: bool = True,
    target_tracer_pid: int = 0,
) -> Path:
    """A synthetic /proc with our own container (pid 42) and a target (pid 1)."""
    proc = tmp_path / "proc"

    write(
        proc / "self" / "status",
        make_status(self_uid, capeff=capeff, capbnd=capbnd, seccomp=seccomp, nnp=nnp),
    )
    write(proc / "self" / "comm", "capreport\n")
    write(proc / "self" / "cgroup", OWN_CGROUP + "\n")
    if apparmor is not None:
        write(proc / "self" / "attr" / "current", apparmor + "\n")

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
    if apparmor is not None:
        write(proc / "1" / "attr" / "current", apparmor + "\n")
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


class FakeAttacher:
    """A scripted ptrace backend, so a test can pose any of the four denials."""

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
    assert apparmor_profile("self", proc=empty) is None
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


def test_apparmor_empty_attribute_means_unconfined(tmp_path: Path) -> None:
    proc = make_proc(tmp_path, apparmor="")
    assert apparmor_profile("self", proc=proc) == "unconfined"


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
    """Config (c): root without the capability — 3/6 reads, credential check."""
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
    assert report.verdict is Verdict.READ_ONLY
    assert report.blocker is Blocker.UID_MISMATCH
    assert report.proc_reads["maps"] is False
    assert report.proc_reads["status"] is True


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
        proc=make_proc(tmp_path, seccomp=0, yama=1, apparmor="podbench-custom"),
        attacher=attacher(child=EPERM),
    )
    assert report.blocker is Blocker.APPARMOR


def test_unclassified_structural_failure(tmp_path: Path) -> None:
    report = probe(
        1,
        proc=make_proc(tmp_path, seccomp=0, yama=1, apparmor=""),
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
            tmp_path, self_uid=0, capeff=CAP_WITH_PTRACE, target_uid=0, apparmor=""
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
        apparmor_self="cri-containerd.apparmor.d (enforce)",
        apparmor_target="custom",
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
    ("seccomp", "yama", "apparmor", "expected"),
    [
        (2, 1, "unconfined", Blocker.SECCOMP),
        (0, 3, "unconfined", Blocker.YAMA_SCOPE),
        (0, 1, "custom", Blocker.APPARMOR),
        (0, None, "unconfined", Blocker.UNKNOWN),
    ],
)
def test_structural_classification(
    seccomp: int, yama: int | None, apparmor: str, expected: Blocker
) -> None:
    _, blocker, _ = derive_verdict(
        cap_sys_ptrace=False,
        yama=yama,
        seccomp=seccomp,
        apparmor_self=apparmor,
        apparmor_target=apparmor,
        self_uid=1000,
        target_uid=1000,
        target_pid=1,
        child=EPERM,
        target_attach=EPERM,
        proc_reads={"maps": True},
    )
    assert blocker is expected


def test_no_reads_at_all_is_verdict_none() -> None:
    verdict, blocker, _ = derive_verdict(
        cap_sys_ptrace=False,
        yama=1,
        seccomp=0,
        apparmor_self="unconfined",
        apparmor_target="unconfined",
        self_uid=0,
        target_uid=1000,
        target_pid=1,
        child=OK,
        target_attach=EPERM,
        proc_reads=dict.fromkeys(("root", "maps", "environ"), False),
    )
    assert verdict is Verdict.NONE
    assert blocker is Blocker.UID_MISMATCH


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
    "apparmor_profile",
    "self_uid",
    "target_uid",
    "target_pid",
    "node_name",
    "child_attach_ok",
    "target_attach_ok",
    "proc_reads",
    "notes",
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
    code = main(
        ["1", "--json"],
        proc=make_proc(tmp_path, self_uid=1000, target_uid=1000),
        attacher=attacher(),
    )
    assert code == Verdict.READ_ONLY.value
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == code


def test_main_discovers_the_target_from_the_container_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["--json", "--container-id", f"containerd://{TARGET_CID}"],
        proc=make_proc(tmp_path, self_uid=1000, target_uid=1000),
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
    code = main(["--json"], proc=make_proc(tmp_path), attacher=attacher())
    payload = json.loads(capsys.readouterr().out)
    assert payload["target_pid"] is None
    assert code == Verdict.LIVE_ATTACH.value
    assert any("PODBENCH_TARGET_CID" in note for note in payload["notes"])
