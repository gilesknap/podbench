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
from pathlib import Path

import pytest

from podbench.gdbcmd import (
    attach_commands,
    command_file_text,
    format_process_table,
    gdb_argv,
    launch_commands,
    main,
    read_exe,
    resolve_target_pid,
    strip_deleted,
)
from podbench.model import Blocker, Verdict
from podbench.probe import AttachOutcome, SkippedAttacher

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
        f"set sysroot /proc/{TARGET_PID}/root",
        f"directory /proc/{TARGET_PID}/root",
        f"add-auto-load-safe-path /proc/{TARGET_PID}/root",
        "set debuginfod enabled on",
        f"file /proc/{TARGET_PID}/root/app/victim",
        f"attach {TARGET_PID}",
    ]


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


def test_resolve_target_pid_reports_an_unmatched_container(tmp_path: Path) -> None:
    proc = make_proc(tmp_path)
    pid, notes = resolve_target_pid(None, "containerd://" + "f" * 64, proc=proc)
    assert pid is None
    assert any("no process found" in note for note in notes)


# --- pids -------------------------------------------------------------------


def test_pids_table_marks_only_the_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proc = make_proc(tmp_path)
    assert main(["pids", "--container-id", TARGET_CID], proc=proc) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["PID", "UID", "TARGET", "CONTAINER", "COMM", "CMDLINE"]
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
    assert (
        capsys.readouterr().out.strip() == "PID  UID  TARGET  CONTAINER  COMM  CMDLINE"
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
