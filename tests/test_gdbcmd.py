"""Tests for the in-pod ``pids`` and ``dbg`` helpers.

Everything runs against a synthetic ``/proc`` tree, a fake ptrace backend and a
recording gdb runner. Nothing here may exec a real gdb or consult the host's
real capabilities: the answers depend entirely on the kernel underneath, and the
spike found two nodes in one cluster that disagree about Yama.

The assertions that matter are about *ordering*. ``set sysroot`` after
``attach`` produces a backtrace that looks plausible and is wrong (report 3.3),
so the sequence is pinned exactly rather than checked for membership.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from functools import partial
from pathlib import Path

import pytest

from podbench import execfile, gdbcmd
from podbench.gdbcmd import (
    EXIT_USAGE,
    MAX_OFFERED_PIDS,
    RUST_PRETTY_PRINTERS,
    attach_commands,
    command_file_text,
    format_process_table,
    gdb_argv,
    launch_commands,
    listing_heading,
    main,
    read_exe,
    resolve_target_pid,
    resolve_target_pids,
    startup_commands,
    strip_deleted,
)
from podbench.model import (
    POD_CONTAINERS_ENV,
    TARGET_NAME_ENV,
    Blocker,
    Verdict,
)
from podbench.probe import AttachOutcome, SkippedAttacher
from proc_samples import CID, sample, write_tree

TARGET_CID = "87d20e2380a1c0ffee0b1e5deadbeef00d15ea5e0000111122223333444455556"
OTHER_CID = "7206c89b11111111222222223333333344444444555555556666666677777777"

# CapEff with bit 19 (CAP_SYS_PTRACE) set, as measured in the spike's dbg-a
# container, and the same container without it.
CAP_WITH_PTRACE = "00000000a80c25fb"
CAP_WITHOUT_PTRACE = "00000000a80425fb"

TARGET_PID = 597
SELF_PID = 609


class RecordingRunner:
    """Stands in for ``execvp``: records the argv it was handed."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, argv: Sequence[str]) -> int:
        self.calls.append(list(argv))
        return self.returncode


class OkAttacher:
    """A ptrace backend where everything is permitted."""

    def attach_child(self) -> AttachOutcome:
        return AttachOutcome(ok=True)

    def attach(self, pid: int) -> AttachOutcome:
        return AttachOutcome(ok=True)


_EPERM = AttachOutcome(ok=False, errno=1, message="Operation not permitted")


class DeniedTargetAttacher:
    """The Diamond shape: our own child traces, the workload does not."""

    def attach_child(self) -> AttachOutcome:
        return AttachOutcome(ok=True)

    def attach(self, pid: int) -> AttachOutcome:
        return _EPERM


class NoPtraceAttacher:
    """ptrace(2) refused even on our own fork, so nothing traces here."""

    def attach_child(self) -> AttachOutcome:
        return _EPERM

    def attach(self, pid: int) -> AttachOutcome:
        return _EPERM


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _status(uid: int, *, capeff: str) -> str:
    return (
        "Name:\tvictim\n"
        f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        "TracerPid:\t0\n"
        f"CapEff:\t{capeff}\n"
        f"CapBnd:\t{capeff}\n"
        "CapAmb:\t0000000000000000\n"
        "Seccomp:\t0\n"
        "NoNewPrivs:\t0\n"
    )


def _add_process(
    proc: Path,
    pid: int,
    *,
    comm: str,
    cmdline: str,
    cgroup: str,
    uid: int = 1000,
    exe: str | None = None,
) -> None:
    base = proc / str(pid)
    _write(base / "comm", f"{comm}\n")
    _write(base / "cmdline", "\x00".join(cmdline.split()) + "\x00")
    _write(base / "cgroup", f"{cgroup}\n")
    _write(base / "status", _status(uid, capeff="0000000000000000"))
    _write(base / "maps", "00400000-00401000 r-xp /app/victim\n")
    _write(base / "environ", "PATH=/usr/bin\x00")
    (base / "root").mkdir(parents=True, exist_ok=True)
    (base / "fd").mkdir(parents=True, exist_ok=True)
    if exe is not None:
        (base / "exe").symlink_to(exe)


def make_proc(tmp_path: Path, *, capeff: str = CAP_WITHOUT_PTRACE) -> Path:
    """A shared PID namespace with a pause process, the target, another
    podbench session and us — the layout observed in report 3.15."""
    proc = tmp_path / "proc"
    _write(proc / "sys" / "kernel" / "yama" / "ptrace_scope", "1\n")
    _write(proc / "self" / "status", _status(1000, capeff=capeff))
    _write(proc / "self" / "cgroup", "0::/\n")

    _add_process(
        proc,
        1,
        comm="pause",
        cmdline="/pause",
        cgroup="0::/../cri-containerd-aaa.scope",
    )
    _add_process(
        proc,
        TARGET_PID,
        comm="victim",
        cmdline="/app/victim --loop",
        cgroup=f"0::/../cri-containerd-{TARGET_CID}.scope",
        exe="/app/victim (deleted)",
    )
    _add_process(
        proc,
        603,
        comm="sleep",
        cmdline="/bin/sleep 100000",
        cgroup=f"0::/../cri-containerd-{OTHER_CID}.scope",
    )
    _add_process(
        proc, SELF_PID, comm="python3", cmdline="podbench agent", cgroup="0::/"
    )
    return proc


# --- the command sequence ---------------------------------------------------


def test_attach_sequence_is_pinned_in_order() -> None:
    assert attach_commands(TARGET_PID, exe="/app/victim") == [
        "set pagination off",
        "handle SIGURG nostop noprint pass",
        f"set sysroot /proc/{TARGET_PID}/root",
        f"directory /proc/{TARGET_PID}/root",
        f"add-auto-load-safe-path /proc/{TARGET_PID}/root",
        "set debuginfod enabled on",
        f"file /proc/{TARGET_PID}/root/app/victim",
        f"attach {TARGET_PID}",
    ]


def test_sigurg_is_handled_before_the_process_resumes() -> None:
    """The ordering, which is what this line's value depends on.

    `nostop noprint pass` is already gdb's default - a stock gdb 17.1 reports
    `SIGURG No No Yes` with no startup commands at all - so this pins it rather
    than repairing a flood. Pinning it is only worth anything before the attach:
    on a gdb configured otherwise the flood starts the moment the inferior
    resumes, and Go preempts goroutines hundreds of times a second.
    """
    commands = attach_commands(TARGET_PID, exe="/app/victim")
    assert commands.index("handle SIGURG nostop noprint pass") < commands.index(
        f"attach {TARGET_PID}"
    )
    # `pass`, not `ignore`: the signal is still delivered, because swallowing it
    # changes the Go runtime's scheduling.
    assert "handle SIGURG nostop noprint pass" in launch_commands("./victim")


def test_the_rust_printers_are_sourced_only_for_a_rust_target() -> None:
    """No rustup toolchain exists in the pod, so nothing else loads them.

    Conditional rather than always: `source` of a path that is not there is an
    error in the debug console on every unrelated attach.
    """
    plain = attach_commands(TARGET_PID, exe="/app/victim")
    rust = attach_commands(TARGET_PID, exe="/app/victim", rust=True)
    assert not any(command.startswith("source ") for command in plain)
    assert f"source {RUST_PRETTY_PRINTERS}" in rust
    # Before `file`, so the printers are registered by the time gdb renders
    # anything out of the inferior.
    assert rust.index(f"source {RUST_PRETTY_PRINTERS}") < rust.index(
        f"attach {TARGET_PID}"
    )


def test_sysroot_and_file_precede_attach() -> None:
    # Report 3.3: `attach` first leaves the main executable unresolved and the
    # user frames come back as `?? ()` while libc's look entirely believable.
    commands = attach_commands(TARGET_PID, exe="/app/victim")
    assert commands.index(f"set sysroot /proc/{TARGET_PID}/root") < commands.index(
        f"attach {TARGET_PID}"
    )
    assert commands.index(f"file /proc/{TARGET_PID}/root/app/victim") < commands.index(
        f"attach {TARGET_PID}"
    )


def test_deleted_suffix_is_stripped_from_the_file_command() -> None:
    commands = attach_commands(TARGET_PID, exe="/app/victim (deleted)")
    assert f"file /proc/{TARGET_PID}/root/app/victim" in commands
    assert "(deleted)" not in "\n".join(commands)


def test_strip_deleted_leaves_other_paths_alone() -> None:
    assert strip_deleted("/app/my (deleted) thing") == "/app/my (deleted) thing"


def test_substitute_path_is_never_generated() -> None:
    # It works, but gdb re-applies it on display and corrupts `info source`'s
    # fullname — the one field the VS Code C++ extension consumes (3.3, R5).
    text = command_file_text(
        attach_commands(TARGET_PID, exe="/app/victim", source_dirs=["/src"])
    )
    text += command_file_text(launch_commands("./victim", source_dirs=["/src"]))
    assert "substitute-path" not in text


def test_source_dirs_are_searched_before_the_target_rootfs() -> None:
    commands = attach_commands(TARGET_PID, exe="/app/victim", source_dirs=["/src"])
    # gdb searches the most recently added directory first.
    assert commands.index("directory /src") > commands.index(
        f"directory /proc/{TARGET_PID}/root"
    )


def test_missing_exe_omits_the_file_command() -> None:
    commands = attach_commands(TARGET_PID, exe=None)
    assert not any(command.startswith("file ") for command in commands)
    assert commands[-1] == f"attach {TARGET_PID}"


def test_debuginfod_can_be_turned_off_explicitly() -> None:
    commands = attach_commands(TARGET_PID, exe="/app/v", debuginfod=False)
    assert "set debuginfod enabled off" in commands
    assert "set debuginfod enabled off" in launch_commands("./v", debuginfod=False)


def test_launch_needs_no_sysroot_and_never_attaches() -> None:
    commands = launch_commands("./victim", ["--fast"], run=True)
    assert not any(
        command.startswith(("set sysroot", "attach")) for command in commands
    )
    assert commands[-1] == "run"
    assert "set args --fast" in commands


def test_gdb_argv_preserves_command_order() -> None:
    argv = gdb_argv(["one", "two"])
    assert argv == ["gdb", "-q", "-ex", "one", "-ex", "two"]


def test_command_file_text_is_newline_terminated() -> None:
    assert command_file_text(["a", "b"]) == "a\nb\n"


# --- reading /proc ----------------------------------------------------------


def test_read_exe_strips_the_deleted_marker(tmp_path: Path) -> None:
    proc = make_proc(tmp_path)
    assert read_exe(TARGET_PID, proc=proc) == "/app/victim"


def test_read_exe_returns_none_when_the_link_is_unreadable(tmp_path: Path) -> None:
    proc = make_proc(tmp_path)
    assert read_exe(603, proc=proc) is None


def test_resolve_target_pid_uses_the_container_id(tmp_path: Path) -> None:
    proc = make_proc(tmp_path)
    pid, notes = resolve_target_pid(None, TARGET_CID, proc=proc)
    assert pid == TARGET_PID
    assert notes == []


def test_resolve_target_pid_refuses_to_guess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PID 1 is /pause under shareProcessNamespace, so "assume pid 1" is wrong.
    monkeypatch.delenv("PODBENCH_TARGET_CID", raising=False)
    proc = make_proc(tmp_path)
    pid, notes = resolve_target_pid(None, None, proc=proc)
    assert pid is None
    assert any("PODBENCH_TARGET_CID" in note for note in notes)
    assert any("pause process, not the target" in note for note in notes)


def test_the_refusal_describes_the_namespace_this_seat_is_really_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 17.2: the old sentence was false on 15 of 15 pods surveyed.

    A pod that does not set ``shareProcessNamespace`` puts the seat in the
    *target container's* namespace through ``targetContainerName``, and PID 1
    there is the workload — so "PID 1 is the pod's pause process" sent every
    reader on that beamline away from the right answer. ``/proc/1/comm`` is
    what settles it, because a seat has no pod spec to read.
    """
    monkeypatch.delenv("PODBENCH_TARGET_CID", raising=False)
    proc = make_proc(tmp_path)
    # The same tree with the pause process replaced by the target's own
    # entrypoint, which is what PID 1 is without the flag.
    (proc / "1" / "comm").write_text("victim\n")

    _, notes = resolve_target_pid(None, None, proc=proc)

    assert any("does not share its process namespace" in note for note in notes)
    assert not any("pause" in note for note in notes)


def test_an_unreadable_pid_one_is_hedged_rather_than_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither sentence may be spent on a namespace nothing measured."""
    monkeypatch.delenv("PODBENCH_TARGET_CID", raising=False)
    proc = make_proc(tmp_path)
    (proc / "1" / "comm").unlink()

    _, notes = resolve_target_pid(None, None, proc=proc)

    assert any("not reliably the target" in note for note in notes)


def test_resolve_target_pid_reports_an_unmatched_container(tmp_path: Path) -> None:
    proc = make_proc(tmp_path)
    pid, notes = resolve_target_pid(None, "containerd://" + "f" * 64, proc=proc)
    assert pid is None
    assert any("no process found" in note for note in notes)


# --- pids -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_inherited_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub the two variables the listing's heading is built from.

    They are set in a real seat, and this suite is sometimes run in one. Every
    test that wants a heading sets them itself; the rest see what a seat landed
    by a launcher older than them prints, which is no heading at all.
    """
    monkeypatch.delenv(TARGET_NAME_ENV, raising=False)
    monkeypatch.delenv(POD_CONTAINERS_ENV, raising=False)


def test_the_listing_says_whose_processes_it_is_showing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "The pod's processes" over one container's namespace is finding 15.

    The sibling is named with the whole invocation that reaches it, and neither
    line claims anything about what is *not* below: under
    ``shareProcessNamespace: true`` the other container's processes are here too,
    and this seat cannot tell from in here.
    """
    monkeypatch.setenv(TARGET_NAME_ENV, "ca-gateway")
    monkeypatch.setenv(POD_CONTAINERS_ENV, "ca-gateway,pva-gateway")
    proc = make_proc(tmp_path)

    assert main(["pids", "--container-id", TARGET_CID], proc=proc) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "container ca-gateway: the processes in its PID namespace"
    assert lines[1] == (
        "this pod also has pva-gateway - re-attach with `--target pva-gateway` "
        "for a seat in that one"
    )
    assert lines[2].split()[0] == "PID"


def test_a_one_container_pod_is_headed_but_offered_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(TARGET_NAME_ENV, "app")
    monkeypatch.setenv(POD_CONTAINERS_ENV, "app")
    proc = make_proc(tmp_path)

    main(["pids", "--container-id", TARGET_CID], proc=proc)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "container app: the processes in its PID namespace"
    assert lines[1].split()[0] == "PID"


def test_a_seat_that_was_never_told_heads_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Absence is "an older launcher landed me", never "one container".

    Guessing the pod's shape from a missing variable is the claim this heading
    exists to stop, so the listing goes back to what it printed before.
    """
    assert listing_heading(None, ["ca-gateway", "pva-gateway"]) == []
    proc = make_proc(tmp_path)

    main(["pids", "--container-id", TARGET_CID], proc=proc)
    assert capsys.readouterr().out.splitlines()[0].split()[0] == "PID"


def test_the_json_form_is_not_headed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A heading is prose for a reader, and `--json` has neither."""
    monkeypatch.setenv(TARGET_NAME_ENV, "ca-gateway")
    monkeypatch.setenv(POD_CONTAINERS_ENV, "ca-gateway,pva-gateway")
    proc = make_proc(tmp_path)

    main(["pids", "--container-id", TARGET_CID, "--json"], proc=proc)
    json.loads(capsys.readouterr().out)


def test_three_containers_offer_the_flag_for_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``p47-proxy``, where the seat holds a third of the pod's processes."""
    monkeypatch.setenv(TARGET_NAME_ENV, "panda80")
    monkeypatch.setenv(POD_CONTAINERS_ENV, "panda80,panda8080,pmac1025")
    proc = make_proc(tmp_path)

    main(["pids", "--container-id", TARGET_CID], proc=proc)
    assert capsys.readouterr().out.splitlines()[1] == (
        "this pod also has panda8080 and pmac1025 - re-attach with "
        "`--target panda8080` or `--target pmac1025` for a seat in one of those"
    )


def test_pids_table_marks_only_the_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proc = make_proc(tmp_path)
    assert main(["pids", "--container-id", TARGET_CID], proc=proc) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == [
        "PID",
        "UID",
        "TARGET",
        "ST",
        "THR",
        "PTRACE",
        "CONTAINER",
        "COMM",
        "CMDLINE",
    ]
    marked = [line for line in lines[1:] if line.split()[2] == "yes"]
    assert [line.split()[0] for line in marked] == [str(TARGET_PID)]
    assert "/app/victim --loop" in "\n".join(lines)


def test_pids_targets_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    proc = make_proc(tmp_path)
    main(["pids", "--container-id", TARGET_CID, "--targets"], proc=proc)
    body = capsys.readouterr().out.splitlines()[1:]
    assert len(body) == 1


def test_pids_warns_rather_than_presenting_the_fallback_as_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Without a container id every *other* podbench session's processes are
    # marked as target too (report 4.3), so the guess must be labelled.
    monkeypatch.delenv("PODBENCH_TARGET_CID", raising=False)
    proc = make_proc(tmp_path)
    assert main(["pids"], proc=proc) == 0
    captured = capsys.readouterr()
    assert "PODBENCH_TARGET_CID" in captured.err
    assert captured.err.startswith("warning:")
    # the sibling debug container is a false positive, which is the point
    assert [line.split()[2] for line in captured.out.splitlines()[1:]].count("yes") == 2


def test_pids_json_carries_the_attribution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proc = make_proc(tmp_path)
    main(["pids", "--container-id", TARGET_CID, "--json"], proc=proc)
    payload = json.loads(capsys.readouterr().out)
    assert payload["attribution"] == "container-id"
    assert payload["warning"] is None
    targets = [p for p in payload["processes"] if p["is_target"]]
    assert [p["pid"] for p in targets] == [TARGET_PID]


def test_pids_json_records_the_fallback_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PODBENCH_TARGET_CID", raising=False)
    proc = make_proc(tmp_path)
    main(["pids", "--json"], proc=proc)
    payload = json.loads(capsys.readouterr().out)
    assert payload["attribution"] == "cgroup-fallback"
    assert payload["warning"] is not None


def test_pids_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    # CI smoke-tests `podbench pids --help` against the built image, so a broken
    # parser fails there rather than in a pod.
    assert main(["pids", "--help"]) == 0
    assert "pids" in capsys.readouterr().out


def test_empty_table_still_has_a_header(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    main(["pids", "--container-id", TARGET_CID], proc=proc)
    assert capsys.readouterr().out.strip() == (
        "PID  UID  TARGET  ST  THR  PTRACE  CONTAINER  COMM  CMDLINE"
    )


def test_format_process_table_is_a_pure_function(tmp_path: Path) -> None:
    from podbench.proc import scan_processes

    listing = scan_processes(TARGET_CID, proc=make_proc(tmp_path))
    assert format_process_table(listing, targets_only=True).count("\n") == 1


# --- dbg --------------------------------------------------------------------


def test_dbg_dry_run_prints_the_command_file_without_gdb(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proc = make_proc(tmp_path)
    runner = RecordingRunner()
    assert main(["dbg", str(TARGET_PID), "--dry-run"], proc=proc, runner=runner) == 0
    assert runner.calls == []
    assert capsys.readouterr().out == command_file_text(
        attach_commands(TARGET_PID, exe="/app/victim")
    )


def test_dbg_print_commands_is_the_same_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proc = make_proc(tmp_path)
    main(
        ["dbg", str(TARGET_PID), "--print-commands"],
        proc=proc,
        runner=RecordingRunner(),
    )
    assert capsys.readouterr().out.startswith("set pagination off\n")


def test_dbg_execs_gdb_when_attach_is_permitted(tmp_path: Path) -> None:
    proc = make_proc(tmp_path, capeff=CAP_WITH_PTRACE)
    runner = RecordingRunner(returncode=0)
    code = main(
        ["dbg", str(TARGET_PID)], proc=proc, attacher=OkAttacher(), runner=runner
    )
    assert code == 0
    assert runner.calls == [gdb_argv(attach_commands(TARGET_PID, exe="/app/victim"))]


def _shadow_the_targets_exe(proc: Path, tmp_path: Path) -> Path:
    """Put a *different* ``/app/victim`` in both rootfs views, as #90 needs.

    The synthetic tree has no ``/proc/self/root`` at all, which is why every
    other test in this file goes on emitting the plain sysroot path: nothing of
    ours can shadow a path we do not have. Here both sides exist and differ,
    which is the seat-image-versus-target-image collision.

    Returns the staging directory to redirect ``gdb_exec_file`` at, so the copy
    lands under ``tmp_path`` instead of the seat's real ``/tmp``.
    """
    ours = proc / "self" / "root" / "app" / "victim"
    ours.parent.mkdir(parents=True, exist_ok=True)
    ours.write_text("this seat's own /app/victim\n")
    theirs = proc / str(TARGET_PID) / "root" / "app" / "victim"
    theirs.parent.mkdir(parents=True, exist_ok=True)
    theirs.write_text("the target's /app/victim\n")
    return tmp_path / "staged"


def test_dbg_gives_gdb_a_file_command_no_binary_of_ours_can_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #90's wiring: the ``file`` path must hold the *target's* bytes.

    gdb canonicalises the exec file's name, ``/proc/<pid>/root`` canonicalises
    to ``/``, and BFD then reads whatever this container keeps at that path —
    so the sequence has to name a path nothing of ours occupies. Asserted on
    the bytes at the emitted path rather than on its spelling: a `file` command
    pointing at the right-looking path with the seat's binary behind it is the
    defect, not the fix.
    """
    proc = make_proc(tmp_path)
    staging = _shadow_the_targets_exe(proc, tmp_path)
    monkeypatch.setattr(
        gdbcmd, "gdb_exec_file", partial(execfile.gdb_exec_file, staging=staging)
    )

    assert main(["dbg", str(TARGET_PID), "--dry-run"], proc=proc) == 0
    captured = capsys.readouterr()
    emitted = [
        line.removeprefix("file ")
        for line in captured.out.splitlines()
        if line.startswith("file ")
    ]

    assert len(emitted) == 1, captured.out
    assert Path(emitted[0]).read_text() == "the target's /app/victim\n"
    assert "#90" in captured.err, (
        "substituting the path silently leaves the reader unable to explain why "
        f"gdb is reading /tmp: {captured.err}"
    )


def test_dbg_prints_one_path_for_the_gdb_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--print-exec-file`` is what ``image/bin/gdb-podbench`` shells out to.

    One path on stdout and nothing else, because the wrapper interpolates it
    straight into a ``file`` command for a third-party ``gdb --pid``.
    """
    proc = make_proc(tmp_path)
    staging = _shadow_the_targets_exe(proc, tmp_path)
    monkeypatch.setattr(
        gdbcmd, "gdb_exec_file", partial(execfile.gdb_exec_file, staging=staging)
    )
    runner = RecordingRunner()

    code = main(["dbg", str(TARGET_PID), "--print-exec-file"], proc=proc, runner=runner)
    out = capsys.readouterr().out

    assert code == 0
    assert runner.calls == [], "printing a path must not start a debugger"
    assert out.splitlines() == [str(staging / str(TARGET_PID) / "victim")]


def test_print_exec_file_says_nothing_when_the_exe_cannot_be_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A degraded rung cannot read ``/proc/<pid>/exe`` at all (report 3.11).

    The wrapper then has nothing to give gdb and must leave it exactly as it
    was, so this has to be an empty stdout and a non-zero exit rather than a
    guess at the path.
    """
    proc = make_proc(tmp_path)
    (proc / str(TARGET_PID) / "exe").unlink()

    code = main(["dbg", str(TARGET_PID), "--print-exec-file"], proc=proc)

    assert code == EXIT_USAGE
    assert capsys.readouterr().out == ""


def test_print_exec_file_never_starts_a_debugger_instead(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--launch`` has no pid, but it must not turn a query into an exec.

    The program is here, in this container, so there is no sysroot to
    canonicalise away and the answer is the program itself.
    """
    runner = RecordingRunner()

    code = main(
        ["dbg", "--print-exec-file", "--launch", "./prog"],
        proc=make_proc(tmp_path),
        runner=runner,
    )

    assert code == 0
    assert runner.calls == []
    assert capsys.readouterr().out == "./prog\n"


def test_an_unshadowed_target_still_gets_the_sysroot_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The common case is untouched, and says nothing about #90.

    Every target that does not share a base-image convention with the seat
    keeps report 4.3's sequence verbatim; a copy would only spend the pod's
    ephemeral storage.
    """
    proc = make_proc(tmp_path)

    assert main(["dbg", str(TARGET_PID), "--print-exec-file"], proc=proc) == 0
    captured = capsys.readouterr()

    assert captured.out == f"/proc/{TARGET_PID}/root/app/victim\n"
    assert captured.err == ""


def test_startup_commands_are_the_attach_sequence_without_the_attach() -> None:
    """One author for both, so the wrapper cannot fall behind (finding 17.6).

    ``gdb-podbench`` supplies its own ``--pid``, and used to carry two of these
    lines hand-copied into ``sh`` — missing ``add-auto-load-safe-path``, which
    costs every thread-aware command, and later ``handle SIGURG``, which pins
    the default a Go session's readability rests on. Neither absence reports
    anything.
    """
    attach = attach_commands(TARGET_PID, exe="/app/victim")

    assert startup_commands(TARGET_PID, exe="/app/victim") == attach[:-1]
    assert attach[-1] == f"attach {TARGET_PID}"


def test_startup_commands_can_leave_debuginfod_alone() -> None:
    """``None`` is not ``False``: it emits no ``set debuginfod`` line at all.

    Symbols are fetched with the workload *stopped*, so enabling it is a pause
    ``podbench dbg`` chooses to spend on its own behalf. The wrapper stands
    behind cpptools and debugpy, which have agreed to no such thing.
    """
    left_alone = startup_commands(TARGET_PID, exe="/app/victim", debuginfod=None)

    assert not any(command.startswith("set debuginfod") for command in left_alone)
    assert "set debuginfod enabled off" in startup_commands(
        TARGET_PID, exe="/app/victim", debuginfod=False
    )


def test_dbg_prints_the_startup_sequence_for_the_gdb_wrapper(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--print-startup-commands`` is the wrapper's whole answer, one per line.

    The wrapper turns each into an ``-iex``; ``attach`` is absent because the
    caller passed ``--pid`` and gdb attaches during startup.
    """
    proc = make_proc(tmp_path)
    runner = RecordingRunner()

    code = main(
        ["dbg", str(TARGET_PID), "--print-startup-commands"], proc=proc, runner=runner
    )
    out = capsys.readouterr().out

    assert code == 0
    assert runner.calls == [], "printing commands must not start a debugger"
    assert out.splitlines() == [
        "set pagination off",
        "handle SIGURG nostop noprint pass",
        f"set sysroot /proc/{TARGET_PID}/root",
        f"directory /proc/{TARGET_PID}/root",
        f"add-auto-load-safe-path /proc/{TARGET_PID}/root",
        f"file /proc/{TARGET_PID}/root/app/victim",
    ]


def test_the_startup_sequence_survives_an_unreadable_exe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A degraded rung cannot read ``/proc/<pid>/exe``, and still needs the rest.

    ``--print-exec-file`` has nothing to say here and exits non-zero. This flag
    does: the sysroot alone is still the difference between this container's
    libraries and the target's, and dropping the whole sequence over the one
    line that cannot be built would be the worse trade (report 3.3).
    """
    proc = make_proc(tmp_path)
    (proc / str(TARGET_PID) / "exe").unlink()

    code = main(["dbg", str(TARGET_PID), "--print-startup-commands"], proc=proc)
    captured = capsys.readouterr()

    assert code == 0
    assert f"set sysroot /proc/{TARGET_PID}/root" in captured.out.splitlines()
    assert not any(line.startswith("file ") for line in captured.out.splitlines())


def test_dbg_names_the_blocker_and_offers_launch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No CAP_SYS_PTRACE and no measurement: the user must be told *which*
    # mechanism said no, not merely that gdb failed (report 4.3, model.Blocker).
    proc = make_proc(tmp_path, capeff=CAP_WITHOUT_PTRACE)
    runner = RecordingRunner()
    code = main(
        ["dbg", str(TARGET_PID)],
        proc=proc,
        attacher=SkippedAttacher("no ptrace here"),
        runner=runner,
    )
    assert code == Verdict.READ_ONLY.value
    assert runner.calls == []
    err = capsys.readouterr().err
    assert Blocker.YAMA_SCOPE.value in err
    assert "--launch" in err
    assert "PR_SET_PTRACER" in err


def _deny_gated_reads(proc: Path, *, keep_root: bool = False) -> None:
    """Take away the ptrace-gated reads, leaving the world-readable ones."""
    for name in ("maps", "environ"):
        (proc / str(TARGET_PID) / name).unlink()
    if not keep_root:
        (proc / str(TARGET_PID) / "root").rmdir()


def test_dbg_does_not_offer_a_sysroot_it_cannot_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no measurement at all the read-only consolation is still wrong.

    Printing "the rootfs, maps and environ are still readable" here sends the
    reader to a second denial, which is the failure issue #51 describes. The
    verdict is `none` rather than launch-only because a skipped scratch attach
    measured nothing; the launch-only rung proper is the next test.
    """
    proc = make_proc(tmp_path)
    _deny_gated_reads(proc)

    code = main(
        ["dbg", str(TARGET_PID)],
        proc=proc,
        attacher=SkippedAttacher("no ptrace here"),
        runner=RecordingRunner(),
    )
    assert code == Verdict.NONE.value, (
        "nothing was measured, so launch-only cannot be claimed either"
    )
    err = capsys.readouterr().err
    assert "still readable" not in err
    assert "cmdline, status and fd only" in err
    # Unmeasured is offered, and said to be unmeasured.
    assert "--launch" in err
    assert "the scratch attach was skipped here" in err


def test_dbg_on_the_launch_only_rung_offers_the_rung_it_measured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Diamond shape as `dbg` meets it: scratch attach OK, target denied,
    gated reads gone. This is the report the launch-only message was written
    for, and nothing exercised it before."""
    proc = make_proc(tmp_path)
    _deny_gated_reads(proc)

    code = main(
        ["dbg", str(TARGET_PID)],
        proc=proc,
        attacher=DeniedTargetAttacher(),
        runner=RecordingRunner(),
    )
    assert code == Verdict.LAUNCH_ONLY.value
    err = capsys.readouterr().err
    assert "launch-only" in err
    assert "ptrace-free alternative" in err
    assert "not the fallback here" in err


def test_dbg_keeps_the_sysroot_when_only_some_gated_reads_went(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`root` alone still fails the all-or-nothing tick, and `set sysroot
    /proc/<pid>/root` is the mandatory fix from report 3.4 — so the message
    must not tell the reader a sysroot is not the fallback."""
    proc = make_proc(tmp_path)
    _deny_gated_reads(proc, keep_root=True)

    code = main(
        ["dbg", str(TARGET_PID)],
        proc=proc,
        attacher=DeniedTargetAttacher(),
        runner=RecordingRunner(),
    )
    assert code == Verdict.LAUNCH_ONLY.value
    err = capsys.readouterr().err
    assert "a sysroot on root still will" in err
    assert "a sysroot, `environ` or `maps` is not the fallback" not in err


def test_dbg_does_not_offer_launch_when_the_scratch_attach_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """gdb traces an inferior it forked, which is the attach that just failed.

    The offer used to be unconditional, and it is the last line a denied user
    reads — so it sent them straight to a second identical denial.
    """
    proc = make_proc(tmp_path)
    _deny_gated_reads(proc)

    code = main(
        ["dbg", str(TARGET_PID)],
        proc=proc,
        attacher=NoPtraceAttacher(),
        runner=RecordingRunner(),
    )
    assert code == Verdict.NONE.value
    err = capsys.readouterr().err
    assert "ptrace-free alternative" not in err
    assert "is not the way out either" in err


def test_dbg_launch_never_probes_or_attaches(tmp_path: Path) -> None:
    proc = make_proc(tmp_path)
    runner = RecordingRunner()

    class ExplodingAttacher:
        def attach_child(self) -> AttachOutcome:
            raise AssertionError("launch mode must not need ptrace permission")

        def attach(self, pid: int) -> AttachOutcome:
            raise AssertionError("launch mode must not attach")

    code = main(
        ["dbg", "--launch", "./victim", "--fast"],
        proc=proc,
        attacher=ExplodingAttacher(),
        runner=runner,
    )
    assert code == 0
    assert runner.calls == [gdb_argv(launch_commands("./victim", ["--fast"]))]


def test_dbg_launch_without_a_program_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The parser's own refusal, not a hand-written one: --launch is declared as
    # taking a PROGRAM, and only the program's *arguments* are lifted out of
    # argv before parsing.
    assert main(["dbg", "--launch"], runner=RecordingRunner()) == 2
    assert "--launch" in capsys.readouterr().err


def test_dbg_warns_when_the_exe_link_is_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proc = make_proc(tmp_path)
    main(["dbg", "603", "--dry-run"], proc=proc, runner=RecordingRunner())
    captured = capsys.readouterr()
    assert "PTRACE_MODE_READ" in captured.err
    assert not any(line.startswith("file ") for line in captured.out.splitlines())


def test_dbg_without_a_target_exits_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PODBENCH_TARGET_CID", raising=False)
    runner = RecordingRunner()
    assert main(["dbg"], proc=make_proc(tmp_path), runner=runner) == 2
    assert runner.calls == []
    assert "PODBENCH_TARGET_CID" in capsys.readouterr().err


def test_dbg_discovers_the_pid_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PODBENCH_TARGET_CID", f"containerd://{TARGET_CID}")
    proc = make_proc(tmp_path)
    main(["dbg", "--dry-run"], proc=proc, runner=RecordingRunner())
    assert f"attach {TARGET_PID}\n" in capsys.readouterr().out


# --- the shared entry point -------------------------------------------------


def test_main_dispatches_both_subcommands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proc = make_proc(tmp_path)
    assert main(["pids", "--container-id", TARGET_CID, "--json"], proc=proc) == 0
    assert json.loads(capsys.readouterr().out)["attribution"] == "container-id"
    assert main(["dbg", str(TARGET_PID), "--dry-run"], proc=proc) == 0
    assert "set sysroot" in capsys.readouterr().out


def test_main_requires_a_subcommand() -> None:
    # A usage error, not a success: `podbench` alone must not look like a
    # command that ran.
    assert main([]) == 2


# -- what is offered, and what is only ranked --------------------------------


def test_the_offering_is_capped_and_says_what_it_left_out(tmp_path: Path) -> None:
    """opis: 98 offerable processes, one dropdown.

    Every offered pid becomes a full set of launch.json entries, so an
    unbounded offering is a dropdown that is scrolled rather than read. The cap
    is not silent: what it left out is named, counted, and pointed at the pid
    argument that reaches it.
    """
    proc = write_tree(tmp_path / "proc", sample("opis"))
    pids, notes = resolve_target_pids(None, CID, proc=proc)
    assert len(pids) == MAX_OFFERED_PIDS
    assert pids[0] == 1
    dropped = next(note for note in notes if "not offered" in note)
    assert "of 98 candidates" in dropped
    assert "33 (nginx)" in dropped
    assert "and 89 more" in dropped
    assert "podbench pids" in dropped


def test_a_zombie_is_never_offered_though_a_shell_still_is(tmp_path: Path) -> None:
    """The asymmetry between the two fallbacks, in one pod.

    blueapi's zombie is ranked last and then dropped: an entry for it is an
    entry that can only fail, whatever seat selects it. A shell is dropped only
    because something better ran — on a container that is nothing but a shell it
    is still offered.
    """
    proc = write_tree(tmp_path / "proc", sample("blueapi"))
    pids, _ = resolve_target_pids(None, CID, proc=proc)
    assert pids == [1, 233, 10, 232]


def test_the_pids_table_marks_the_disqualified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "Run `podbench pids` to choose another" has to show what to choose on.

    The listing is in raw pid order, so the columns are all the reader has:
    a `Z` under ST is a process no seat can attach to, and a DENIED under
    PTRACE is one this seat in particular cannot.
    """
    proc = write_tree(tmp_path / "proc", sample("blueapi"))
    assert main(["pids", "--container-id", CID, "--targets"], proc=proc) == 0
    rows = {
        line.split()[0]: line.split() for line in capsys.readouterr().out.splitlines()
    }
    assert rows["1"][3:6] == ["S", "195", "ok"]
    assert rows["518"][3:6] == ["Z", "1", "DENIED"]


def test_pids_json_carries_the_new_measurements(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Additive keys: an older seat omits them, and a consumer must not read a
    missing key as a measured `false`."""
    proc = write_tree(tmp_path / "proc", sample("blueapi"))
    main(["pids", "--container-id", CID, "--targets", "--json"], proc=proc)
    payload = json.loads(capsys.readouterr().out)
    zombie = next(p for p in payload["processes"] if p["pid"] == 518)
    assert zombie["state"] == "Z"
    assert zombie["threads"] == 1
    assert zombie["ptrace_readable"] is False
    assert zombie["ppid"] == 233
