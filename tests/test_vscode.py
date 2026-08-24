"""Tests for the generated VS Code debug configuration.

The generated document is asserted field by field rather than compared to a
golden file, because each field is here for its own measured reason and a golden
file would let one of them change without anyone noticing which.

Two of them are the whole point of the module. ``miDebuggerPath`` must name the
image's wrapper: cpptools inherits a cwd that VS Code deletes on extension
update, and ``/usr/bin/gdb`` then segfaults during startup with no signal name.
And ``program`` must be sysroot-prefixed, because the unprefixed form reads the
*debug image's* binary and produces a plausible backtrace off the wrong symbols.

Nothing here touches a cluster: pids come from a synthetic ``/proc`` tree.
"""

from __future__ import annotations

import json
import re
import socket
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any, cast

import pytest

from podbench import vscode
from podbench.execfile import gdb_exec_file
from podbench.flavour import DEBUGPY_PORT, Language, Mode, Seat, Target
from podbench.gdbcmd import EXIT_USAGE, RUST_PRETTY_PRINTERS, attach_commands
from podbench.kubectl import CommandResult
from podbench.model import SEIZE_PROBE
from podbench.probe import AttachOutcome
from podbench.proc import DEFAULT_PROC, seat_cwd
from podbench.provision import INJECTION_TIMEOUT_SECONDS
from podbench.vscode import (
    GDB_WRAPPER,
    MACHINE_SETTINGS_PATH,
    SEAT_FOLDER_SETTINGS,
    SEAT_MACHINE_SETTINGS,
    cppdbg_configuration,
    cppdbg_launch_configuration,
    debugpy_attach_configuration,
    debugpy_launch_configuration,
    delve_configuration,
    ephemeral_port,
    extensions_for,
    launch_json_text,
    launch_setup_commands,
    lldb_configuration,
    main,
    measured_attach,
    merge_extensions_json,
    merge_folder_settings,
    merge_launch_json,
    merge_machine_settings,
    program_load_error,
    python_path_mappings,
    setup_commands,
    target_architecture,
)
from test_elf import EM_AARCH64
from test_flavour import (
    BEAM_CMDLINE,
    JVM_CMDLINE,
    SITE_PACKAGES,
    TARGET_CID,
    make_proc,
    proc_stat,
    python_proc,
    seat_debugpy,
    which_of,
    write_debugpy,
)

PID = 597
EXE = "/app/victim"
CID = "cafe1234cafe1234cafe1234cafe1234"


@pytest.fixture
def proc_tree(tmp_path: Path) -> Path:
    """A ``/proc`` with one process whose ``exe`` link resolves."""
    entry = tmp_path / str(PID)
    entry.mkdir()
    (entry / "exe").symlink_to(EXE)
    return tmp_path


def cli(
    args: list[str], proc: Path, *, present: tuple[str, ...] = ("gdb", "gdb-podbench")
) -> int:
    """``main`` with the image's debugger inventory injected.

    Whether a gdb configuration can be emitted depends on whether the *image*
    ships gdb, and a unit test must not answer that from whatever happens to be
    installed on the machine running the suite.
    """
    return main(args, proc=proc, which=which_of(*present))


# -- the fields that fail silently when wrong -------------------------------


def test_program_is_sysroot_prefixed() -> None:
    """The unprefixed form reads this container's binary and looks fine."""
    assert cppdbg_configuration(PID, EXE)["program"] == f"/proc/{PID}/root/app/victim"


def test_program_names_the_staged_copy_where_this_seat_shadows_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #90 through the editor, which is the half no gdb command can fix.

    gdb canonicalises the exec file's name and ``/proc/<pid>/root``
    canonicalises to ``/``, so a sysroot-prefixed ``program`` still reads this
    seat's binary. cpptools sends ``program`` as ``-file-exec-and-symbols``
    before any ``setupCommands``, so the configuration itself has to name a
    path nothing of ours occupies. Asserted on the bytes behind the emitted
    path: the right-looking path with the seat's binary behind it is the
    defect, not the fix.
    """
    proc = tmp_path / "proc"
    (proc / str(PID)).mkdir(parents=True)
    (proc / str(PID) / "exe").symlink_to(EXE)
    for view, text in (
        (proc / "self" / "root", "ours\n"),
        (proc / str(PID) / "root", "theirs\n"),
    ):
        (view / "app").mkdir(parents=True)
        (view / "app" / "victim").write_text(text)
    monkeypatch.setattr(
        vscode,
        "gdb_exec_file",
        partial(gdb_exec_file, staging=tmp_path / "staged"),
    )

    assert cli([str(PID), "--print-config"], proc) == 0
    document = json.loads(capsys.readouterr().out)
    emitted = [
        entry for entry in document["configurations"] if entry["type"] == "cppdbg"
    ]

    assert len(emitted) == 1, document
    program = emitted[0]["program"]
    assert Path(program).read_text() == "theirs\n", (
        f"cpptools would load this seat's own /app/victim: {program}"
    )


def test_no_lldb_entry_is_written_where_this_seat_shadows_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same collision, and the other debugger, which cannot be staged out of
    it.

    cppdbg keeps its entry because ``program`` can name a copy. CodeLLDB was
    measured to override ``program`` after attaching, re-resolving the
    executable from the process in *this* seat's namespace, so the entry would
    debug the file above with a warning in the debug console as its only
    notice. Nothing is emitted instead, and the refusal names the file.
    """
    proc = tmp_path / "proc"
    (proc / str(PID)).mkdir(parents=True)
    (proc / str(PID) / "exe").symlink_to(EXE)
    for view, text in (
        (proc / "self" / "root", "ours\n"),
        (proc / str(PID) / "root", "theirs\n"),
    ):
        (view / "app").mkdir(parents=True)
        (view / "app" / "victim").write_text(text)
    monkeypatch.setattr(
        vscode,
        "gdb_exec_file",
        partial(gdb_exec_file, staging=tmp_path / "staged"),
    )

    assert cli([str(PID), "--print-config"], proc) == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)

    assert [entry["type"] for entry in document["configurations"]] == ["cppdbg"]
    assert "lldb unavailable" in captured.err
    assert EXE in captured.err


def test_mi_debugger_is_the_wrapper_not_usr_bin_gdb() -> None:
    """`/usr/bin/gdb` segfaults under cpptools' inherited deleted cwd.

    The failure is a fatal signal during startup with no signal name and no
    backtrace, surfaced by VS Code as "GDB exited unexpectedly" — which points
    at the attach rather than at gdb's own initialisation. Anyone tidying this
    back to the obvious path reintroduces a bug that costs a day to find.
    """
    assert cppdbg_configuration(PID, EXE)["miDebuggerPath"] == GDB_WRAPPER
    assert GDB_WRAPPER != "/usr/bin/gdb"


def test_cwd_is_always_set() -> None:
    """``${workspaceFolder}`` can resolve to nothing in a seat, and gdb dies."""
    for configuration in (
        cppdbg_configuration(PID, EXE),
        cppdbg_launch_configuration(EXE),
    ):
        cwd = configuration["cwd"]
        assert cwd and cwd.startswith("/"), configuration


def test_cwd_is_the_seats_own_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Not a constant: ``/root`` is the home of a *root* seat only.

    On every rung below full the seat runs at the target's uid, for which this
    image has no account, so the launcher pins ``HOME`` under ``/tmp`` and
    ``/root`` is a directory that seat cannot enter. cpptools starting gdb
    there is finding 06.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    assert cppdbg_configuration(PID, EXE)["cwd"] == str(tmp_path)
    assert cppdbg_launch_configuration(EXE)["cwd"] == str(tmp_path)


def test_an_explicit_cwd_still_wins_in_launch_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``dev`` mode has a real answer — the target's own cwd — and it is not
    the seat's home."""
    monkeypatch.setenv("HOME", str(tmp_path))

    assert cppdbg_launch_configuration(EXE, cwd="/workspace")["cwd"] == "/workspace"


@pytest.mark.parametrize(
    "home",
    ["", "/nonexistent-podbench-home", "unenterable"],
    ids=["unset", "missing", "unenterable"],
)
def test_a_home_the_seat_cannot_enter_loses_to_tmp(
    home: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cwd that cannot be entered is worse than no cwd.

    ``/tmp`` is 1777 in every image, so it is enterable at whatever uid the
    seat turned out to run as — which a ``podbench-home`` volume mounted with
    no ``fsGroup`` need not be.
    """
    if home == "unenterable":
        (tmp_path / "home").mkdir(mode=0o600)
        home = str(tmp_path / "home")
    monkeypatch.setenv("HOME", home)

    assert seat_cwd() == "/tmp"
    assert cppdbg_configuration(PID, EXE)["cwd"] == "/tmp"


def test_setup_commands_keep_the_load_bearing_order() -> None:
    """The DAP path must not drift from the CLI path.

    ``set sysroot`` after ``attach`` fixes libraries up on the fly but not the
    main executable, and the frames above libc come back as ``?? ()`` while
    reading as a real backtrace (report 3.3). Pinned exactly, not by membership.
    """
    commands = [
        entry["text"] for entry in cppdbg_configuration(PID, EXE)["setupCommands"]
    ]
    assert commands == [
        "set pagination off",
        "handle SIGURG nostop noprint pass",
        f"set sysroot /proc/{PID}/root",
        f"directory /proc/{PID}/root",
        f"add-auto-load-safe-path /proc/{PID}/root",
        "set debuginfod enabled on",
    ]


def test_setup_commands_are_derived_from_the_cli_sequence() -> None:
    """One definition of the ordering, not two that can diverge."""
    cli = attach_commands(PID, exe=None)
    assert setup_commands(PID) == [
        command for command in cli if not command.startswith("attach ")
    ]


def test_adapter_issues_file_and_attach_itself() -> None:
    """Both are implied by ``program``/``processId``; repeating them breaks."""
    commands = setup_commands(PID)
    assert not any(command.startswith(("file ", "attach ")) for command in commands)


def test_source_dirs_follow_the_target_rootfs() -> None:
    """gdb searches the most recently added directory first, so ours wins."""
    commands = setup_commands(PID, source_dirs=["/workspace/src"])
    assert commands.index("directory /workspace/src") > commands.index(
        f"directory /proc/{PID}/root"
    )


def test_debuginfod_can_be_turned_off() -> None:
    assert "set debuginfod enabled off" in setup_commands(PID, debuginfod=False)


# -- architecture -----------------------------------------------------------


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("aarch64", "arm64"),
        ("x86_64", "x64"),
        ("armv7l", "arm"),
        ("i686", "x86"),
    ],
)
def test_target_architecture_spellings(machine: str, expected: str) -> None:
    assert target_architecture(machine) == expected


def test_unknown_architecture_is_omitted_rather_than_guessed() -> None:
    """A wrong ``targetArchitecture`` decodes registers for the wrong machine."""
    assert "targetArchitecture" not in cppdbg_configuration(PID, EXE, machine="riscv64")


# -- source maps ------------------------------------------------------------


def test_source_map_is_emitted_under_the_cpptools_key() -> None:
    config = cppdbg_configuration(PID, EXE, source_map={"/app/src": "/w/src"})
    assert config["sourceFileMap"] == {"/app/src": "/w/src"}


def test_lldb_spells_it_sourcemap_and_uses_an_int_pid() -> None:
    """CodeLLDB's schema differs from cpptools' in exactly these ways."""
    config = lldb_configuration(PID, EXE, source_map={"/app/src": "/w/src"})
    assert config["sourceMap"] == {"/app/src": "/w/src"}
    assert "sourceFileMap" not in config
    assert config["pid"] == PID


def test_lldb_sets_library_search_paths_by_hand() -> None:
    """lldb has no analogue of gdb's ``set sysroot`` for ``/proc/<pid>/root``."""
    commands = lldb_configuration(PID, EXE)["initCommands"]
    assert any(f"/proc/{PID}/root/lib" in command for command in commands)


def test_root_source_map_is_refused(
    proc_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mapping ``/`` is the doubling anti-pattern, and it looks like a shortcut.

    gdb re-applies the substitution on display, so the ``fullname`` the adapter
    hands the editor grows another ``/proc/<pid>/root`` on every stop.
    """
    code = cli(["--source-map", "/=/proc/1/root", "--print-config"], proc_tree)
    assert code != 0
    assert "doubling anti-pattern" in capsys.readouterr().err


# -- merging ----------------------------------------------------------------


def test_merge_into_an_empty_file_creates_the_document() -> None:
    document = json.loads(merge_launch_json(None, {"name": "a"}))
    assert document["version"] == "0.2.0"
    assert document["configurations"] == [{"name": "a"}]


def test_merge_replaces_our_own_entry_rather_than_appending() -> None:
    """Re-running the verb must not leave two copies behind."""
    first = merge_launch_json(None, {"name": "podbench", "v": 1})
    second = merge_launch_json(first, {"name": "podbench", "v": 2})
    configurations = json.loads(second)["configurations"]
    assert configurations == [{"name": "podbench", "v": 2}]


def test_merge_keeps_a_hand_written_configuration() -> None:
    existing = launch_json_text([{"name": "mine"}])
    configurations = json.loads(merge_launch_json(existing, {"name": "ours"}))[
        "configurations"
    ]
    assert [item["name"] for item in configurations] == ["mine", "ours"]


def test_merge_refuses_a_file_it_cannot_parse() -> None:
    """VS Code allows comments in launch.json and :mod:`json` does not.

    Rewriting it anyway would silently drop the comments and any configuration
    this parser could not see, so refusing is the only safe answer.
    """
    with pytest.raises(ValueError, match="cannot parse"):
        merge_launch_json("{ // a comment\n}", {"name": "ours"})


# -- the CLI ----------------------------------------------------------------


def test_print_config_emits_valid_json(
    proc_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli([str(PID), "--print-config"], proc_tree) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["configurations"][0]["processId"] == str(PID)


def test_writes_to_the_named_path(proc_tree: Path, tmp_path: Path) -> None:
    output = tmp_path / "out" / "launch.json"
    assert cli([str(PID), "--output", str(output)], proc_tree) == 0
    assert json.loads(output.read_text())["configurations"][0]["type"] == "cppdbg"


def test_lldb_flag_switches_adapter(
    proc_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli([str(PID), "--lldb", "--print-config"], proc_tree) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["configurations"][0]["type"] == "lldb"


def test_unreadable_exe_is_refused_rather_than_guessed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A guessed ``program`` is the silent failure this module exists to stop."""
    (tmp_path / str(PID)).mkdir()
    code = cli([str(PID), "--print-config"], tmp_path)
    assert code != 0
    assert "--program" in capsys.readouterr().err


def test_explicit_program_overrides_an_unreadable_exe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / str(PID)).mkdir()
    code = cli([str(PID), "--program", "/app/other", "--print-config"], tmp_path)
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["configurations"][0]["program"] == f"/proc/{PID}/root/app/other"


def test_no_pid_and_no_container_id_refuses_to_guess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "The target is PID 1" is wrong under ``shareProcessNamespace``."""
    monkeypatch.delenv("PODBENCH_TARGET_CID", raising=False)
    assert cli(["--print-config"], tmp_path) != 0
    assert "PID 1" in capsys.readouterr().err


# --- machine settings -------------------------------------------------------
#
# The failure these guard is unrecoverable rather than annoying: a folder opened
# at / walks /proc/<pid>/root into every other container in the pod, the seat
# cannot reserve memory of its own, and an OOM-killed ephemeral container cannot
# be restarted.

_WALKERS = ("files.watcherExclude", "search.exclude", "C_Cpp.files.exclude")
"""Every shipped setting that decides where a recursive walk may go."""


@pytest.mark.parametrize("setting", _WALKERS)
@pytest.mark.parametrize("pattern", ["**/proc/**", "**/sys/**", "**/dev/**"])
def test_every_walker_is_kept_out_of_the_pseudo_filesystems(
    setting: str, pattern: str
) -> None:
    assert SEAT_MACHINE_SETTINGS[setting][pattern] is True


def test_pylance_gets_the_same_paths_in_its_own_spelling() -> None:
    """``python.analysis.exclude`` is a list of absolute globs, not an object."""
    excludes = cast("list[str]", SEAT_MACHINE_SETTINGS["python.analysis.exclude"])
    assert {"/proc/**", "/sys/**", "/dev/**"} <= set(excludes)


def test_search_does_not_follow_the_way_into_another_container() -> None:
    """ripgrep is handed ``--follow`` by default, and ``/proc/<pid>/root`` is a
    symlink into another container's rootfs — ``/proc/self/root`` back into this
    one. Excludes alone do not cover a symlink that leaves the excluded tree."""
    assert SEAT_MACHINE_SETTINGS["search.followSymlinks"] is False


def test_the_explorer_is_left_alone() -> None:
    """``files.exclude`` would *hide* /proc, and reading the workload's files
    through ``/proc/<pid>/root`` is what Observe mode is for. Opening a file
    there is safe; only the recursive walk a folder starts is not."""
    assert "files.exclude" not in SEAT_MACHINE_SETTINGS


def test_the_settings_file_is_machine_scope() -> None:
    """User or workspace scope would apply to one folder, and the folder that
    kills the seat is the first one opened."""
    assert MACHINE_SETTINGS_PATH == ".vscode-server/data/Machine/settings.json"


def test_an_absent_file_gets_the_whole_document() -> None:
    document = json.loads(merge_machine_settings(None) or "")
    assert document == SEAT_MACHINE_SETTINGS


def test_merging_what_we_wrote_changes_nothing() -> None:
    """``None`` rather than an identical rewrite: a second attach reports what it
    did, and "changed nothing" is what tells the user the first session is
    untouched."""
    assert merge_machine_settings(merge_machine_settings(None)) is None


def test_a_users_own_value_wins_over_ours() -> None:
    """Including a deliberate ``false``: somebody who turned an exclude off
    means it, and podbench turning it back on every reconnect would be a fight
    they cannot win."""
    merged = merge_machine_settings(
        json.dumps(
            {
                "editor.fontSize": 15,
                "search.followSymlinks": True,
                "search.exclude": {"**/proc/**": False, "**/vendor/**": True},
            }
        )
    )
    document = json.loads(merged or "")
    assert document["editor.fontSize"] == 15
    assert document["search.followSymlinks"] is True
    assert document["search.exclude"]["**/proc/**"] is False
    # …and the patterns they had no opinion about are still added.
    assert document["search.exclude"]["**/vendor/**"] is True
    assert document["search.exclude"]["**/sys/**"] is True
    assert document["files.watcherExclude"]["**/proc/**"] is True


def test_a_setting_of_an_unexpected_shape_is_not_rewritten() -> None:
    """A string where we ship an object is the user's, whatever they meant by
    it. Replacing it would be the clobber this whole merge exists to avoid."""
    merged = merge_machine_settings(json.dumps({"search.exclude": "everything"}))
    assert json.loads(merged or "")["search.exclude"] == "everything"


def test_pylance_excludes_are_appended_rather_than_replaced() -> None:
    merged = merge_machine_settings(
        json.dumps({"python.analysis.exclude": ["/opt/vendor/**"]})
    )
    excludes = json.loads(merged or "")["python.analysis.exclude"]
    assert excludes[0] == "/opt/vendor/**"
    assert "/proc/**" in excludes


def test_merge_refuses_a_settings_file_it_cannot_parse() -> None:
    """VS Code allows comments in settings.json and :mod:`json` does not, so a
    rewrite would drop whatever this parser could not see."""
    with pytest.raises(ValueError, match="cannot parse"):
        merge_machine_settings("{ // mine\n}")


def test_merge_refuses_a_document_that_is_not_an_object() -> None:
    with pytest.raises(ValueError, match="not a JSON object"):
        merge_machine_settings("[]")


# -- the same guard, in a folder ---------------------------------------------


def test_folder_settings_carry_the_whole_guard_including_cpptools() -> None:
    """``--open`` opens a single folder, so that file is the *workspace*
    settings, where window- and resource-scoped keys are both honoured.

    ``C_Cpp.files.exclude`` is the one that would be missed: cpptools' tag
    parser walks on its own account, so the search and watcher excludes do not
    stop it, and cpptools is exactly what ``--open`` installs for a C/C++
    target. A key VS Code ignores costs nothing; an omitted one costs the seat.
    """
    document = json.loads(merge_folder_settings(None) or "")
    assert document["files.watcherExclude"]["**/proc/**"] is True
    assert document["search.exclude"]["**/sys/**"] is True
    assert "/proc/**" in document["python.analysis.exclude"]
    assert document["C_Cpp.files.exclude"]["**/proc/**"] is True
    assert document["search.followSymlinks"] is False


def test_folder_settings_are_the_machine_ones_and_not_a_second_copy() -> None:
    """Two exclude lists would be two things to keep true, and the one that
    drifted would look correct until the walk that ends the seat."""
    assert SEAT_FOLDER_SETTINGS == SEAT_MACHINE_SETTINGS


def test_a_folders_own_settings_survive_the_merge() -> None:
    merged = merge_folder_settings(
        json.dumps({"editor.tabSize": 2, "search.exclude": {"**/proc/**": False}})
    )
    document = json.loads(merged or "")
    assert document["editor.tabSize"] == 2
    assert document["search.exclude"]["**/proc/**"] is False
    assert document["files.watcherExclude"]["**/proc/**"] is True


def test_merging_folder_settings_twice_changes_nothing() -> None:
    assert merge_folder_settings(merge_folder_settings(None)) is None


# -- extensions --------------------------------------------------------------


def test_each_flavour_names_only_its_own_extensions() -> None:
    """Never a bundle: in Observe mode an extension is unpacked into the seat's
    ~/.vscode-server, which is on the *workload's* ephemeral-storage budget, and
    cpptools alone is 330 MiB."""
    assert extensions_for([{"type": "cppdbg"}]) == ["ms-vscode.cpptools"]
    assert extensions_for([{"type": "lldb"}]) == ["vadimcn.vscode-lldb"]
    assert extensions_for([{"type": "go"}]) == ["golang.go"]
    assert extensions_for([{"type": "debugpy"}]) == [
        "ms-python.python",
        "ms-python.debugpy",
    ]


def test_two_configurations_of_one_flavour_ask_for_one_install() -> None:
    """dev mode for Python emits both a launch and a connect entry."""
    assert extensions_for(
        [{"type": "debugpy", "request": "launch"}, {"type": "debugpy"}]
    ) == ["ms-python.python", "ms-python.debugpy"]


def test_an_adapter_podbench_never_emits_asks_for_nothing() -> None:
    """A hand-written configuration in the same file is not a licence to spend
    the workload's disk on an extension podbench did not choose."""
    assert extensions_for([{"type": "coreclr"}, {}]) == []


def test_recommendations_are_added_to_a_folders_own() -> None:
    merged = merge_extensions_json(
        json.dumps({"recommendations": ["esbenp.prettier-vscode"]}),
        ["ms-vscode.cpptools"],
    )
    assert json.loads(merged or "")["recommendations"] == [
        "esbenp.prettier-vscode",
        "ms-vscode.cpptools",
    ]


def test_recommending_what_is_already_recommended_writes_nothing() -> None:
    text = merge_extensions_json(None, ["golang.go"]) or ""
    assert merge_extensions_json(text, ["golang.go"]) is None


def test_extensions_json_that_will_not_parse_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot parse"):
        merge_extensions_json("{ // mine\n}", ["golang.go"])


# -- one entry per flavour that applies --------------------------------------


def test_every_applicable_flavour_is_emitted_and_named(
    proc_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``launch.json`` holds a list, so no exclusive guess has to be made.

    The flavour has to be *in* the name: VS Code's dropdown shows names, and two
    entries called "podbench: attach to victim" are a coin toss. So does the
    pid, for the same reason one level up — the ranking offers several
    candidates and three of them are routinely called ``python``. This tree has
    no ``comm`` file, which is the graceful case: the pid alone still separates
    them.
    """
    assert cli([str(PID), "--print-config"], proc_tree) == 0
    document = json.loads(capsys.readouterr().out)
    names = [entry["name"] for entry in document["configurations"]]
    assert names == [
        f"podbench: attach to victim [pid {PID}] (gdb)",
        f"podbench: attach to victim [pid {PID}] (lldb)",
    ]


def container_tree(
    tmp_path: Path, processes: Sequence[tuple[int, str, str, int]]
) -> Path:
    """A ``/proc`` holding one container's process tree.

    ``processes`` is ``(pid, comm, exe, ppid)``. Every entry lands in the same
    cgroup, so all of them are the target container's — which is the case the
    ranking exists for.
    """
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "cgroup").write_text("0::/\n")
    for pid, comm, exe, ppid in processes:
        entry = tmp_path / str(pid)
        entry.mkdir()
        (entry / "comm").write_text(f"{comm}\n")
        (entry / "cmdline").write_text(f"{exe}\x00")
        (entry / "cgroup").write_text(f"0::/../cri-containerd-{CID}.scope\n")
        (entry / "status").write_text(
            f"Name:\t{comm}\nPPid:\t{ppid}\nUid:\t0\t0\t0\t0\n"
        )
        (entry / "exe").symlink_to(exe)
    return tmp_path


#: The tree from the live IOC pod that produced the bug: a bash entrypoint, the
#: Python that supervises it, the ``sh -c`` it re-execs through, and the binary
#: the pod exists to run — which is the *deepest* of the four, not the lowest.
IOC_TREE = (
    (1, "bash", "/usr/bin/bash", 0),
    (8, "python3", "/usr/bin/python3", 1),
    (11, "sh", "/usr/bin/sh", 8),
    (13, "ioc", "/epics/ioc/bin/ioc", 11),
)


def test_an_entrypoint_script_does_not_become_the_debug_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bug from a live IOC pod: pid 1 is bash, and nobody wants to debug it.

    A cppdbg entry for ``/proc/1/root/usr/bin/bash`` is not a broken
    configuration — it is a correct configuration for the wrong process, which
    is exactly why it costs an afternoon. Shells are dropped rather than listed
    after the real target: an entry in the dropdown gets picked.
    """
    proc = container_tree(tmp_path, IOC_TREE)
    assert cli(["--container-id", CID, "--print-config"], proc) == 0
    document = json.loads(capsys.readouterr().out)
    programs = [
        entry["program"] for entry in document["configurations"] if "program" in entry
    ]
    assert programs
    assert not any("bash" in program or "/sh" in program for program in programs)
    assert all("/proc/13/root" in program for program in programs)


def test_every_candidate_gets_an_entry_rather_than_one_winning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing measurable separates two live children of an entrypoint script.

    So they do not compete for one slot: launch.json holds a list and the choice
    is made at F5, which is the same reason two flavours of one process are both
    emitted. The best candidate is first, because VS Code's dropdown defaults to
    whatever is at the top.
    """
    proc = container_tree(
        tmp_path,
        (
            (1, "bash", "/usr/bin/bash", 0),
            (8, "supervisor", "/app/supervisor", 1),
            (13, "worker", "/app/worker", 8),
        ),
    )
    assert cli(["--container-id", CID, "--print-config"], proc) == 0
    document = json.loads(capsys.readouterr().out)
    pids = [
        str(entry["processId"])
        for entry in document["configurations"]
        if "processId" in entry
    ]
    assert pids[0] == "13"
    assert set(pids) == {"13", "8"}


def test_an_explicit_pid_is_not_second_guessed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Naming a pid is an answer, so offering three others is not helpfulness.

    This is also the escape hatch when the ranking is wrong — including the way
    back to a shell — so it has to be exactly one entry per flavour and not one
    per process.
    """
    proc = container_tree(tmp_path, IOC_TREE)
    assert cli(["1", "--print-config"], proc) == 0
    document = json.loads(capsys.readouterr().out)
    pids = {
        str(entry.get("processId") or entry.get("pid"))
        for entry in document["configurations"]
    }
    assert pids == {"1"}


def test_a_refused_flavour_names_its_mechanism(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point of #20: not "cannot emit", but *what* says no."""
    entry = tmp_path / str(PID)
    entry.mkdir()
    (entry / "exe").symlink_to("/app/victim")
    assert cli([str(PID), "--flavour", "delve", "--print-config"], tmp_path) != 0
    assert "no .gopclntab" in capsys.readouterr().err


def test_an_irrelevant_flavour_is_silent_unless_asked_for(
    proc_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nobody debugging a C binary needs to be told that delve is for Go."""
    assert cli([str(PID), "--print-config"], proc_tree) == 0
    assert "delve" not in capsys.readouterr().err


def test_flavour_restricts_the_emitted_set(
    proc_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli([str(PID), "--flavour", "gdb", "--print-config"], proc_tree) == 0
    document = json.loads(capsys.readouterr().out)
    assert [entry["type"] for entry in document["configurations"]] == ["cppdbg"]


def test_a_seat_without_gdb_says_so_rather_than_emitting_cppdbg(
    proc_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Emitting a configuration the image cannot run is #18's failure again."""
    code = main(
        [str(PID), "--flavour", "gdb", "--print-config"],
        proc=proc_tree,
        which=which_of(),
    )
    assert code != 0
    assert "no gdb on PATH" in capsys.readouterr().err


# -- the mode-dependent shapes ----------------------------------------------


def test_observe_mode_maps_the_mount_namespace_and_nothing_narrower() -> None:
    """The editor sees the target's filesystem through ``/proc/<pid>/root``.

    One entry and no narrower one beside it: a root guessed from ``argv`` is
    ``/app/.venv/bin`` for a console script, and podbench's own image installs
    under ``/app/.venv``, so such a mapping resolves to *this* container's copy
    of the file rather than failing (issue #112).
    """
    assert python_path_mappings(1, Mode.OBSERVE) == [
        {"localRoot": "/proc/1/root", "remoteRoot": "/"}
    ]


def test_dev_mode_has_no_mappings_at_all() -> None:
    """Editor and interpreter are the same inodes, and a spurious mapping is
    the same silent wrong answer as a missing one: breakpoints never bind."""
    assert python_path_mappings(1, Mode.DEV) == []


def test_debugpy_connects_rather_than_listens() -> None:
    """The server is in the app process; the editor is the client.

    ``127.0.0.1`` is right even in Observe mode — separate containers, one
    network namespace — so no port-forward is involved.
    """
    config = debugpy_attach_configuration(1, name="x", port=5678)
    assert config["connect"] == {"host": "127.0.0.1", "port": 5678}
    assert "listen" not in config


def test_dev_mode_native_is_a_launch_with_no_sysroot() -> None:
    """gdb forks the inferior itself, which needs no capability (report §3.12)."""
    config = cppdbg_launch_configuration("/workspace/victim")
    assert config["request"] == "launch"
    assert "processId" not in config
    commands = [entry["text"] for entry in config["setupCommands"]]
    assert not any(command.startswith("set sysroot") for command in commands)


def test_dev_launch_setup_is_derived_from_the_cli_sequence() -> None:
    """One definition of the launch ordering, not two that can diverge."""
    assert launch_setup_commands() == [
        "set pagination off",
        "handle SIGURG nostop noprint pass",
        "set debuginfod enabled on",
    ]


def test_dev_mode_python_offers_both_launch_and_connect(tmp_path: Path) -> None:
    """Two real answers in a dev pod: start it under the debugger, or attach to
    the one ``podbench run`` already started."""
    config = debugpy_launch_configuration("/workspace/src/app.py")
    assert config["request"] == "launch"
    assert "pathMappings" not in config


def test_delve_uses_substitute_path_and_an_int_pid() -> None:
    config = delve_configuration(PID, "/app/server", source_map={"/w": "/app"})
    assert config["processId"] == PID
    assert config["substitutePath"] == [{"from": "/w", "to": "/app"}]


# -- can gdb read the program at all? ----------------------------------------

BFD_REFUSAL = (
    "BFD: /usr/bin/bash: .gnu.version_r invalid entry\n"
    "Can't read symbols from /usr/bin/bash: bad value\n"
)
"""Measured against a RHEL-family target from a Debian bookworm seat.

The seat's binutils is older than the toolchain that linked the target, so BFD
rejects its symbol-versioning section and the file cannot be opened at all -
which is a different thing from the file having no debug information, and the
only one of the two that is fatal.
"""


def gdb_saying(stderr: str, returncode: int = 0) -> Callable[..., CommandResult]:
    """A gdb whose ``file`` command produced ``stderr``.

    ``returncode`` defaults to 0 on purpose: ``gdb -batch`` exits 0 whether the
    command worked or not, so a check that trusted it would answer "readable"
    for every target ever.
    """

    def runner(
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        return CommandResult(tuple(argv), returncode, "", stderr)

    return runner


def test_a_program_gdb_cannot_read_withdraws_cppdbg_and_keeps_lldb(
    proc_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cpptools aborts when the program will not load, and blames the path.

    ``Program path '<x>' is missing or invalid`` while the path is present and
    readable sends the reader after the one thing that is not wrong. So the
    entry is not emitted at all - and the lldb entry stays, because CodeLLDB
    brings its own reader rather than going through binutils.
    """
    code = main(
        [str(PID), "--print-config"],
        proc=proc_tree,
        which=which_of("gdb", "gdb-podbench", "lldb"),
        runner=gdb_saying(BFD_REFUSAL),
    )
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert [entry["type"] for entry in document["configurations"]] == ["lldb"]


def test_the_refusal_quotes_bfd_rather_than_paraphrasing_it(
    proc_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """BFD's own line names the section, which is what identifies the cause."""
    main(
        [str(PID), "--print-config"],
        proc=proc_tree,
        which=which_of("gdb", "gdb-podbench", "lldb"),
        runner=gdb_saying(BFD_REFUSAL),
    )
    err = capsys.readouterr().err
    assert ".gnu.version_r invalid entry" in err
    assert "cpptools would abort" in err


def test_a_silent_gdb_means_the_program_loads(
    proc_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """gdb prints nothing on success - including for a stripped binary.

    So "no output" must not be read as "no answer": a target with no debug info
    is perfectly debuggable and its entry has to survive this check.
    """
    code = main(
        [str(PID), "--print-config"],
        proc=proc_tree,
        which=which_of("gdb", "gdb-podbench"),
        runner=gdb_saying(""),
    )
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert "cppdbg" in [entry["type"] for entry in document["configurations"]]


def test_the_load_check_ignores_the_exit_code() -> None:
    """``gdb -batch`` exits 0 for a `file` that failed, so the text is the only
    evidence there is."""
    assert (
        program_load_error(1, "/proc/1/root/bin/x", runner=gdb_saying(BFD_REFUSAL, 0))
        is not None
    )


def test_the_load_check_asks_about_the_sysroot_prefixed_path() -> None:
    """The unprefixed path names *this* image's binary of the same name, so the
    check would pass on a file the debugger will never open (report §3.3)."""
    seen: list[tuple[str, ...]] = []

    def runner(
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        seen.append(tuple(argv))
        return CommandResult(tuple(argv), 0, "", "")

    program_load_error(597, "/proc/597/root/app/victim", runner=runner)
    assert "file /proc/597/root/app/victim" in seen[0]
    assert "set sysroot /proc/597/root" in seen[0]


def test_no_gdb_to_ask_is_not_a_second_refusal() -> None:
    """`assess` already has a better-worded refusal for an image without gdb."""

    def missing(
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    assert program_load_error(1, "/proc/1/root/bin/x", runner=missing) is None


# -- languages with no real debugger, end to end ------------------------------


@pytest.mark.parametrize(
    ("exe", "cmdline", "named"),
    [
        ("/opt/java/bin/java", JVM_CMDLINE, "jdwp"),
        ("/erts-15.2/bin/beam.smp", BEAM_CMDLINE, "remsh"),
    ],
)
def test_a_runtime_with_no_debugger_gets_a_refusal_and_no_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    exe: str,
    cmdline: str,
    named: str,
) -> None:
    """Nothing is emitted, and the sentence names the tool that would work.

    gdb attaches to both of these perfectly well and shows named C++ frames
    inside somebody else's interpreter, which reads as progress. An empty
    launch.json with a reason beats a full one that debugs the runtime.
    """
    code = main(
        [str(PID), "--print-config"],
        proc=make_proc(tmp_path, exe=exe, cmdline=cmdline),
        which=which_of("gdb", "gdb-podbench"),
        runner=gdb_saying(""),
    )
    captured = capsys.readouterr()
    # Nothing on stdout: --print-config prints a file or it prints nothing, and
    # an empty `configurations` list would be a file the reader could paste.
    assert captured.out == ""
    assert code == EXIT_USAGE
    assert named in captured.err
    # The language is named, so the reader knows which of the pod's processes
    # this was said about.
    assert "jvm target" in captured.err or "erlang target" in captured.err


def test_a_rust_target_gets_cppdbg_with_the_printers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rust is served rather than refused, and needs one thing C does not.

    Verified by fixture only: there is no Rust workload in p47-beamline, so
    nothing here has been through a cluster.
    """
    code = main(
        [str(PID), "--print-config"],
        proc=make_proc(tmp_path, sections=[".rustc", ".text"]),
        which=which_of("gdb", "gdb-podbench"),
        runner=gdb_saying(""),
    )
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    cppdbg = next(
        entry for entry in document["configurations"] if entry["type"] == "cppdbg"
    )
    commands = [entry["text"] for entry in cppdbg["setupCommands"]]
    assert f"source {RUST_PRETTY_PRINTERS}" in commands


# -- the Python path, end to end ---------------------------------------------


SS_HEADER = "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"

INJECT_PORT = 41597
"""What the injected ``port_chooser`` returns. Deliberately not 5678: the whole
point of asking the kernel is that the port is not the conventional one, and a
fixture that answered 5678 would let a run that never asked pass."""


def fixed_port() -> int:
    """A ``port_chooser`` with no socket behind it. The real one binds."""
    return INJECT_PORT


def ss_line(port: int, *, pid: int | None = PID, inode: int = 90210) -> str:
    """One ``ss -lntpe`` row, with the two fields the port filter used to drop.

    ``pid=None`` is the shape issue #87 arrives in and the reason this fixture
    was rebuilt: ``ss`` attributes a socket by scanning ``/proc``, so a listener
    held by a process in *another pod* — which is every process on the node once
    ``hostNetwork: true`` puts the seat in the node's network namespace and
    leaves it in its own pid namespace — is reported with no ``users:`` field at
    all. A fixture without one cannot tell that case from a healthy pod.
    """
    users = f' users:(("python",pid={pid},fd=13))' if pid is not None else ""
    return (
        f"LISTEN 0      4096   127.0.0.1:{port}      0.0.0.0:*"
        f"{users} uid:1000 ino:{inode} sk:2 cgroup:/kubepods v6only:0\n"
    )


def ss_saying(*rows: str) -> Callable[..., CommandResult]:
    """An ``ss`` that reports exactly ``rows``. Injected: the unit suite may not
    shell out, and whether something is listening decides which debugpy shape
    is emitted."""

    def runner(
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        return CommandResult(tuple(argv), 0, SS_HEADER + "".join(rows), "")

    return runner


def no_listeners(
    argv: Sequence[str],
    *,
    stdin: str | None = None,
    capture: bool = True,
    timeout: float | None = None,
) -> CommandResult:
    """An ``ss`` that reports an empty pod."""
    return ss_saying()(argv, stdin=stdin, capture=capture)


def listening_on(port: int) -> Callable[..., CommandResult]:
    """An ``ss`` reporting a debugpy server the *target* holds, as one appears
    the moment ``--provision`` finishes its injection."""
    return ss_saying(ss_line(port))


def stranger_on(port: int) -> Callable[..., CommandResult]:
    """An ``ss`` reporting a listener nothing in this pid namespace owns.

    Issue #87 exactly: on ``bl47p-ea-serv-01`` six hostNetwork pods share one
    network namespace, and the 5678 a seat found there was another pod's.
    """
    return ss_saying(ss_line(port, pid=None))


def test_a_python_target_emits_debugpy_with_the_observe_mapping(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole language x mode x architecture path, through the CLI.

    An amd64 Python service with debugpy on both sides: the configuration is a
    ``connect``, and its ``pathMappings`` carries the sysroot on the left and
    the target's own root on the right.
    """
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES)
    code = main(
        [str(PID), "--print-config"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench"),
        runner=no_listeners,
        port_chooser=fixed_port,
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )
    assert code == 0
    captured = capsys.readouterr()
    configurations = json.loads(captured.out)["configurations"]
    assert [entry["type"] for entry in configurations] == ["debugpy"]
    assert configurations[0]["pathMappings"] == [
        {"localRoot": f"/proc/{PID}/root", "remoteRoot": "/"}
    ]
    # Nothing is listening yet, so the command that starts the server is printed
    # rather than run: it ptraces the workload and leaves a server inside it.
    assert "python -m debugpy --listen" in captured.err
    assert f"PYTHONPATH=/proc/{PID}/root/{SITE_PACKAGES}" in captured.err


def test_an_arm64_python_target_names_the_missing_helper(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The message issue #20 asks for, fired from the CLI.

    ``podbench-demo``'s Python service on an arm64 node: everything else could
    be fixed in the pod, and this cannot be fixed anywhere.
    """
    proc = python_proc(tmp_path, machine=EM_AARCH64, site_packages=SITE_PACKAGES)
    code = main(
        [str(PID), "--print-config"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench"),
        runner=no_listeners,
        port_chooser=fixed_port,
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )
    assert code != 0
    assert "attach_linux_arm64.so" in capsys.readouterr().err


# -- may this seat ptrace the target? ----------------------------------------
#
# Issue #89: the pid injection is `gdb --pid`, so the question is ptrace, and
# CAP_SYS_PTRACE is one of four ways to have it. What is pinned here is *when*
# the seat is allowed to answer that question by really attaching. The probe is
# a PTRACE_SEIZE now and stops nothing, so what these guards protect is no
# longer the workload's uptime but the caller's consent: a verb that only
# authors a file does not touch the workload behind their back.


class RecordingAttacher:
    """An ``Attacher`` that records the pid instead of ptracing it."""

    def __init__(self, outcome: AttachOutcome) -> None:
        self.outcome = outcome
        self.attached: list[int] = []

    def attach_child(self) -> AttachOutcome:
        raise AssertionError("debug-config has no reason to fork a probe child")

    def attach(self, pid: int) -> AttachOutcome:
        self.attached.append(pid)
        return self.outcome


PYTHON_TARGET = Target(
    pid=PID, language=Language.PYTHON, program="/usr/local/bin/python3.11"
)


def test_a_synthetic_proc_is_never_ptraced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The seam is safe by default, which is why the guard is on ``proc``.

    A unit test injects a ``/proc`` whose pids name unrelated processes on the
    machine running the suite, so an attach there would measure something else
    entirely and SIGSTOP somebody's editor. What the tree says about the seat's
    capabilities and about the target's readability stays the answer, and the
    run says which of the two it answered from.
    """
    attacher = RecordingAttacher(AttachOutcome(ok=True))
    main(
        [str(PID), "--print-config"],
        proc=python_proc(tmp_path, site_packages=SITE_PACKAGES, cap_sys_ptrace=False),
        which=which_of("gdb", "gdb-podbench"),
        runner=no_listeners,
        port_chooser=fixed_port,
        attacher=attacher,
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )
    assert attacher.attached == []
    printed = capsys.readouterr().err
    assert f"ptrace to pid {PID} was not measured" in printed
    # The sentence the *probed* path prints. Nothing that skipped the probe may
    # borrow it, whichever way the run then went.
    assert "measured ptrace to pid" not in printed


def test_a_capless_seat_asks_the_kernel_rather_than_the_bit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The #89 case: the answer is measured, not read off CapEff."""
    attacher = RecordingAttacher(AttachOutcome(ok=True, method=SEIZE_PROBE))
    measured = measured_attach(
        PYTHON_TARGET,
        Mode.OBSERVE,
        Seat(machine="x86_64", cap_sys_ptrace=False),
        proc=DEFAULT_PROC,
        attacher=attacher,
    )
    assert measured is True
    assert attacher.attached == [PID]
    # Said out loud, and with the cost named: the answer changed because
    # something was done to the workload, so the reader is told which primitive
    # did it and what it cost — which here is nothing.
    warned = capsys.readouterr().err
    assert SEIZE_PROBE in warned
    assert "Pause to the workload: none" in warned


def test_printing_a_configuration_measures_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--print-config`` writes no file, so it touches no workload either.

    The probe fires per candidate pid, so this was N real attaches on a pod
    with N candidates, for a run that changes nothing. Free is not the same as
    invited (finding 17.4), and the reader is told what was not measured.
    """
    attacher = RecordingAttacher(AttachOutcome(ok=True, method=SEIZE_PROBE))
    measured = measured_attach(
        PYTHON_TARGET,
        Mode.OBSERVE,
        Seat(machine="x86_64", cap_sys_ptrace=False),
        proc=DEFAULT_PROC,
        attacher=attacher,
        probe=False,
    )
    assert measured is None
    assert attacher.attached == []
    warned = capsys.readouterr().err
    assert "--print-config" in warned
    # It names the mechanism that decides in the probe's absence. Saying
    # "CapEff alone" there sent the reader after the bit that decides nothing
    # on this path, which is #89's error in a warning rather than a refusal.
    assert f"ptrace to pid {PID} was not measured" in warned
    assert f"/proc/{PID}/root" in warned
    assert "CapEff" not in warned


def test_a_refused_attach_is_measured_as_a_refusal_not_as_silence() -> None:
    """``None`` means unasked, so a real EPERM must not come back as one."""
    attacher = RecordingAttacher(AttachOutcome(ok=False, errno=1, message="EPERM"))
    assert (
        measured_attach(
            PYTHON_TARGET,
            Mode.OBSERVE,
            Seat(machine="x86_64", cap_sys_ptrace=False),
            proc=DEFAULT_PROC,
            attacher=attacher,
        )
        is False
    )


@pytest.mark.parametrize(
    ("mode", "target", "seat"),
    [
        (
            Mode.DEV,
            PYTHON_TARGET,
            Seat(machine="x86_64", cap_sys_ptrace=False),
        ),
        (
            Mode.OBSERVE,
            Target(pid=PID, language=Language.NATIVE, program="/app/victim"),
            Seat(machine="x86_64", cap_sys_ptrace=False),
        ),
        (
            Mode.OBSERVE,
            PYTHON_TARGET,
            Seat(machine="x86_64", cap_sys_ptrace=True),
        ),
        (
            Mode.OBSERVE,
            PYTHON_TARGET,
            Seat(machine="x86_64", cap_sys_ptrace=False, listening_port=DEBUGPY_PORT),
        ),
    ],
    ids=["dev-mode", "not-python", "already-capable", "already-listening"],
)
def test_the_workload_is_left_alone_where_the_answer_changes_nothing(
    mode: Mode, target: Target, seat: Seat
) -> None:
    """Four cases where no refusal hangs on the answer, so no attach is made:
    dev mode debugs a child of this container, a native target has no injection
    to drive, a capable seat is not going to be refused, and a listening server
    needs no ptrace at all."""
    attacher = RecordingAttacher(AttachOutcome(ok=True))
    assert (
        measured_attach(target, mode, seat, proc=DEFAULT_PROC, attacher=attacher)
        is None
    )
    assert attacher.attached == []


# -- provisioning debugpy into the target ------------------------------------


class InstallingUv:
    """A uv that really unpacks a debugpy-shaped tree where it is told to.

    It has to, because the point of the flag is what happens *after*: the
    debugpy configuration may only be emitted once the **target** can import
    debugpy, and nothing but the filesystem answers that.

    It is also the pod's ``ss`` and the shell the injection runs in, and those
    two are coupled: ``--provision`` starts a server, so an ``ss`` that reports
    the same empty pod afterwards would let a run that injected nothing pass.
    ``inject_rc`` is the failing half.
    """

    def __init__(self, *, inject_rc: int = 0) -> None:
        self.argv: list[str] = []
        self.injected: str | None = None
        self.bound: list[str] = []
        self.inject_rc = inject_rc

    @property
    def injected_port(self) -> int:
        """The port the injection command was told to listen on."""
        assert self.injected is not None
        match = re.search(r"--listen 127\.0\.0\.1:(\d+)", self.injected)
        assert match, self.injected
        return int(match.group(1))

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        if argv[0] == "timeout":
            # The injection reaches the shell only through a bound: #76 was a
            # pause that lasted as long as gdb felt like waiting, and a fake
            # that answered a bare `sh -c` would let that come back unnoticed.
            assert list(argv[-3:-1]) == ["sh", "-c"]
            self.bound = list(argv[1:-3])
            self.injected = argv[-1]
            if self.inject_rc != 0:
                return CommandResult(tuple(argv), self.inject_rc, "", "ptrace: denied")
            return CommandResult(tuple(argv), 0, "", "")
        if argv[0] != "uv":
            if self.injected is not None and self.inject_rc == 0:
                # On the port the injection was *given*, not on 5678. The run
                # asks the kernel for a free one, so a fixture that answered
                # 5678 would report the run's own new server as missing and
                # print the "nothing is listening" hint over the top of it.
                return listening_on(self.injected_port)(
                    argv, stdin=stdin, capture=capture
                )
            return no_listeners(argv, stdin=stdin, capture=capture)
        self.argv = list(argv)
        write_debugpy(
            Path(argv[list(argv).index("--target") + 1]),
            helpers=["attach_linux_amd64.so"],
        )
        return CommandResult(tuple(argv), 0, "", "")


def provision_seat(tmp_path: Path) -> tuple[Path, str]:
    """A full-rung seat on a stock Python workload with no debugpy in it."""
    return (
        python_proc(tmp_path),
        seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )


def test_provision_installs_into_the_target_and_then_emits_debugpy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bench's own run, end to end: refusal, install, injection command.

    The seat is 3.11 and the target 3.12, so the version uv is given is the
    target's — and what it is given decides whether the accelerators the target
    loads exist at all.
    """
    proc, root = provision_seat(tmp_path)
    uv = InstallingUv()
    code = main(
        [str(PID), "--print-config", "--provision"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=root,
    )
    assert code == 0
    assert uv.argv[:6] == [
        "uv",
        "pip",
        "install",
        # The cache lands in the *seat's* layer and the target is another
        # filesystem, so without this the wheel is paid for twice out of one
        # pod-level ephemeral-storage budget.
        "--no-cache",
        "--python-version",
        "3.12",
    ]
    captured = capsys.readouterr()
    assert json.loads(captured.out)["configurations"][0]["type"] == "debugpy"
    # The install is only half of it. The injection runs, and its PYTHONPATH is
    # the copy that was just made - the whole point of resolving for the
    # target's version rather than this seat's.
    assert uv.injected is not None
    assert f"PYTHONPATH=/proc/{PID}/root/opt/podbench-debugpy" in uv.injected
    # And it names the seat's interpreter in full: the seat has two, and the
    # one this recipe means is the venv the image resolved debugpy against.
    assert " /app/.venv/bin/python -m debugpy" in uv.injected
    # The ptrace pause is bounded by podbench and not by gdb's patience (#76).
    assert str(INJECTION_TIMEOUT_SECONDS) in uv.bound
    for caveat in ("egress", "restart", "ephemeral storage"):
        assert caveat in captured.err


def test_a_named_destination_beats_the_seats_default_all_the_way_through(
    tmp_path: Path,
) -> None:
    """`--provision-dest` is what the laptop spells on a hotfixed pod, where the
    seat's own default is a root-owned `/opt` it cannot write. So it has to win
    over that default for both things the destination is: where uv installs, and
    the extra path the injection's PYTHONPATH then names."""
    proc, root = provision_seat(tmp_path)
    uv = InstallingUv()
    dest = "/podbench/app/.podbench-debugpy"
    code = main(
        [str(PID), "--print-config", "--provision", "--provision-dest", dest],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=root,
    )

    assert code == 0
    assert str(proc / str(PID) / "root" / dest.lstrip("/")) in uv.argv
    assert not any("podbench-debugpy" in arg and "/opt/" in arg for arg in uv.argv)
    assert uv.injected is not None
    assert f"PYTHONPATH=/proc/{PID}/root{dest}" in uv.injected


def test_provision_leaves_a_server_listening_rather_than_a_command_to_paste(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #45 ordered the two mutations and put *installing* above
    *injecting*, so a run allowed the larger one has been allowed the smaller.
    Asking twice left the configuration emitted and the port closed, which is a
    launch.json that connects to nothing."""
    proc, root = provision_seat(tmp_path)
    uv = InstallingUv()
    code = main(
        [str(PID), "--print-config", "--provision"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=root,
    )

    assert code == 0
    assert uv.injected is not None
    assert f"--pid {PID}" in uv.injected
    captured = capsys.readouterr()
    # The hint that exists for the un-injected case must not survive the run
    # that injected: it would send the reader to do what was just done.
    assert "nothing is listening" not in captured.err
    assert "injected in" in captured.err


def test_a_target_that_already_has_debugpy_still_gets_a_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The install is skipped and the injection is not. This is the case the
    flag exists for at its sharpest: debugpy importable, nothing listening, and
    every prerequisite already met - so the old --provision said "nothing is
    installed" and left the port closed."""
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES)
    write_debugpy(
        proc / str(PID) / "root" / SITE_PACKAGES.lstrip("/"),
        helpers=["attach_linux_amd64.so"],
    )
    uv = InstallingUv()
    code = main(
        [str(PID), "--print-config", "--provision"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )

    assert code == 0
    assert uv.argv == []
    assert uv.injected is not None
    assert "nothing is listening" not in capsys.readouterr().err


def test_a_refused_injection_is_reported_and_not_claimed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ptrace an LSM denies is the DLS case. The install still happened, so
    the run is not a failure - but it must not read as one that connected."""
    proc, root = provision_seat(tmp_path)
    uv = InstallingUv(inject_rc=1)
    main(
        [str(PID), "--print-config", "--provision"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=root,
    )

    captured = capsys.readouterr()
    assert "injection exited 1" in captured.err
    assert "injected in" not in captured.err
    # And the paste is back, because now it is the reader's only way through.
    assert "nothing is listening" in captured.err


def test_without_the_flag_nothing_is_injected_either(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`injection_command` is printed rather than run for a bare debug-config,
    and that has not changed: authoring a launch.json may not ptrace the
    workload on its own. --provision is what revokes it."""
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES)
    write_debugpy(
        proc / str(PID) / "root" / SITE_PACKAGES.lstrip("/"),
        helpers=["attach_linux_amd64.so"],
    )
    uv = InstallingUv()
    main(
        [str(PID), "--print-config"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )

    assert uv.injected is None
    assert "nothing is listening" in capsys.readouterr().err


def test_without_the_flag_nothing_is_written_into_the_workload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Opt-in, and the reason is the mutation's size.

    ~15 MB into the workload's writable layer, against a budget the seat shares
    and cannot reserve, is larger than the injection that is already judged too
    big to run implicitly. So the same target gets the command to run and no
    install.
    """
    proc, root = provision_seat(tmp_path)
    uv = InstallingUv()
    code = main(
        [str(PID), "--print-config"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=root,
    )
    assert code != 0
    assert uv.argv == []
    assert (
        "uv pip install --no-cache --python-version 3.12 --target "
        f"/proc/{PID}/root/opt/podbench-debugpy debugpy" in capsys.readouterr().err
    )


def test_provision_probes_writability_before_it_runs_uv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A read-only rootfs is the one genuinely new precondition, and it is
    unreadable from here: the mount flag lives in the target's namespace, so it
    arrives as an errno on the write. Probing first means the blocker is named
    rather than arriving as whatever uv says about a directory it could not
    create."""
    proc, root = provision_seat(tmp_path)
    (proc / str(PID) / "root" / "readonly").write_text("not a directory")
    uv = InstallingUv()
    main(
        [str(PID), "--print-config", "--provision", "--provision-dest", "/readonly/x"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=root,
    )
    assert uv.argv == []
    assert f"cannot write {proc}/{PID}/root/readonly/x" in capsys.readouterr().err


def test_provision_refuses_where_no_wheel_could_help(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """On arm64 the install would spend the workload's storage for nothing.

    debugpy publishes no aarch64 Linux wheel and ships `attach_linux_amd64.so`
    alone, so there is no helper for the injection to dlopen on any path — the
    one prerequisite with no remedy inside the pod.
    """
    proc = python_proc(tmp_path, machine=EM_AARCH64)
    uv = InstallingUv()
    main(
        [str(PID), "--print-config", "--provision"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )
    assert uv.argv == []
    assert "publishes no aarch64 attach helper" in capsys.readouterr().err


def test_provision_says_no_in_dev_mode_rather_than_installing_anyway(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only Observe mode needs this.

    A dev pod relaunches the app as the seat's own child in this container,
    where debugpy is an ordinary workspace-venv dependency — there is no other
    rootfs to write into, and Iterate's non-root sidecar is a restricted-PSS
    feature rather than an oversight.
    """
    proc, root = provision_seat(tmp_path)
    uv = InstallingUv()
    main(
        [str(PID), "--print-config", "--provision", "--mode", "dev"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=root,
    )
    assert uv.argv == []
    assert "dev mode debugs a process in this container" in capsys.readouterr().err


def test_provision_installs_over_its_own_previous_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An existing tree at this flag's own destination cannot be a refusal.

    Nothing in an installed tree records the X.Y uv resolved it for, and
    ``_target_debugpy`` checks this path first — so a copy made for the wrong
    version imports fine, shadows the target's real one, and drops pydevd to
    pure Python silently. Re-running has to be able to correct that.
    """
    proc, root = provision_seat(tmp_path)
    write_debugpy(
        proc / str(PID) / "root" / "opt" / "podbench-debugpy",
        helpers=["attach_linux_amd64.so"],
    )
    uv = InstallingUv()
    code = main(
        [str(PID), "--print-config", "--provision"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=root,
    )
    assert code == 0
    assert uv.argv[:3] == ["uv", "pip", "install"]
    assert "is this flag's own destination" in capsys.readouterr().err


def test_the_targets_own_complete_copy_is_never_written_over(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The app image's site-packages is not podbench's to install into.

    It is complete, the injection would load it as it stands, and 15 MB beside
    it buys nothing — so this one really is a refusal.
    """
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES)
    uv = InstallingUv()
    code = main(
        [str(PID), "--print-config", "--provision"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )
    assert code == 0
    assert uv.argv == []
    assert "can already import debugpy" in capsys.readouterr().err


def test_an_incomplete_target_copy_gets_a_complete_one_beside_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Importable but missing the helper is the case worth writing over.

    The injection would get as far as the dlopen and then fail on a helper the
    target's own copy does not have, so a complete tree goes in at the
    provisioned path and takes over ``PYTHONPATH`` from there.
    """
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES, target_helpers=[])
    uv = InstallingUv()
    code = main(
        [str(PID), "--print-config", "--provision"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )
    assert code == 0
    captured = capsys.readouterr()
    assert "a complete copy goes in beside it" in captured.err
    assert f"PYTHONPATH=/proc/{PID}/root/opt/podbench-debugpy" in captured.err


def test_provision_without_uv_says_what_uv_was_for(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A seat with no uv cannot resolve for an interpreter it is not running,
    which is the whole reason this is an install and not a copy."""
    proc, root = provision_seat(tmp_path)
    main(
        [str(PID), "--print-config", "--provision"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench"),
        runner=no_listeners,
        port_chooser=fixed_port,
        debugpy_root=root,
    )
    assert "no uv on PATH in this seat" in capsys.readouterr().err


def test_provision_refuses_to_guess_the_targets_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Installing for the wrong X.Y leaves pydevd on its pure-Python fallback
    with nothing said, so an unreadable version is a refusal with a flag to pass
    rather than the seat's own version quietly substituted."""
    proc = python_proc(tmp_path, exe="/usr/local/bin/python")
    uv = InstallingUv()
    main(
        [str(PID), "--print-config", "--provision"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )
    assert uv.argv == []
    assert "--provision-python X.Y" in capsys.readouterr().err


def test_provision_on_a_capless_but_readable_seat_installs_and_emits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The DLS run of #89, as a unit test: the command, the seat and the exit.

    ``debug-config --provision --print-config`` from a seat whose policy engine
    strips SYS_PTRACE, against a target it may read. Nothing is measured —
    ``--print-config`` touches nothing — so the run must neither refuse the
    injection for want of a capability nor claim an attach it never made.
    """
    proc = python_proc(tmp_path, cap_sys_ptrace=False)
    uv = InstallingUv()
    code = main(
        [str(PID), "--print-config", "--provision"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )
    captured = capsys.readouterr()
    assert uv.argv[:3] == ["uv", "pip", "install"]
    assert code == 0, captured.err
    assert captured.out.strip()
    # The two sentences tests/e2e/test_dls_ioc.py matches on, in the same run
    # that installs. Both are about a capability that decides nothing here.
    assert "CAP_SYS_PTRACE is not in this seat's effective set" not in captured.err
    assert "cannot be driven from here" not in captured.err
    # And no attach was claimed either: the run probed nothing, so the sentence
    # the probed path prints must be nowhere in it.
    assert "measured ptrace to pid" not in captured.err


def test_provision_into_an_unreadable_target_names_the_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not a refusal, unlike arm64: the copy outlives the seat.

    A seat the kernel refuses ``PTRACE_MODE_READ`` on cannot drive the
    injection — the attach takes strictly more than the read — and the note has
    to name that rather than the capability, which is not what said no. The
    install is still attempted and still spoken for: the tree lands in the
    target's own rootfs, so a relaunch picks it up rather than repeating it.
    """
    proc = python_proc(tmp_path, cap_sys_ptrace=False, ptrace_readable=False)
    uv = InstallingUv()
    code = main(
        [str(PID), "--print-config", "--provision"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )
    assert code != 0
    captured = capsys.readouterr()
    assert "outlives this seat" in captured.err
    assert f"may not read /proc/{PID}/root" in captured.err
    assert "CAP_SYS_PTRACE is not in this seat's effective set" not in captured.err


def test_a_denied_candidate_does_not_cost_the_others_their_configurations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 01: one refused rootfs used to take the whole file with it.

    ``pathlib`` raises rather than answering on a denied read — ``EACCES`` is
    not among the errnos it ignores — and this verb writes ``launch.json``
    last, so a raise anywhere in the walk left the run with a traceback and no
    file at all. The Python candidate here is the one the seat may not read;
    the IOC beside it is unaffected, and its entry has to survive.
    """
    proc = container_tree(tmp_path, IOC_TREE)
    assert cli(["--container-id", CID, "--print-config"], proc) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["configurations"]
    # Declined on the credentials, and not on an install that could not be run:
    # `--provision` writes through the same `/proc/<pid>/root` the kernel just
    # refused, so offering it here would be the denial told as a remedy.
    assert "may not read /proc/8/root" in captured.err
    assert "not importable by the target" not in captured.err


def test_two_candidates_with_one_basename_get_two_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ordering the candidates is worthless if the dropdown cannot tell them apart.

    `merge_launch_configs` matches by name, so identical names are worse than
    confusing: re-running with a different winner silently replaces an entry the
    user already had rather than adding one.
    """
    proc = container_tree(
        tmp_path,
        (
            (1, "bash", "/usr/bin/bash", 0),
            (8, "worker", "/app/bin/worker", 1),
            (13, "worker", "/app/bin/worker", 1),
        ),
    )
    assert cli(["--container-id", CID, "--print-config"], proc) == 0
    document = json.loads(capsys.readouterr().out)
    names = [entry["name"] for entry in document["configurations"]]
    assert len(names) == len(set(names))
    assert all("worker [pid " in name for name in names)
    assert any("[pid 13 worker]" in name for name in names)


# -- attributing a listening port, and choosing one to serve on --------------
#
# Issue #87. The old check filtered `ss` output on the port and nothing else,
# on the premise that "the pod's network namespace is shared, so a listener
# anywhere in it is reachable from the seat at 127.0.0.1". That is true of an
# ordinary pod and false under `hostNetwork: true`, where the namespace is the
# node's: the sweep found 5678 held by a podbench seat in a *different pod on
# the same node*, called it "already listening in this pod" and emitted a
# configuration pointing at the stranger.


def debugpy_seat(tmp_path: Path) -> dict[str, Any]:
    """A full-rung seat on a Python target that can already import debugpy."""
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES)
    return {
        "proc": proc,
        "which": which_of("gdb", "gdb-podbench"),
        "debugpy_root": seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
        "port_chooser": fixed_port,
    }


def test_a_listener_in_the_target_container_is_offered_as_the_targets_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The good case, and the one the beamline supplies: `p47-blueapi-0` is
    deployed with `-m debugpy --listen 5678` in PID 1's own cmdline. That is a
    real server worth connecting to, and it is distinguished from a stranger by
    the same evidence — who holds the socket, not which port it is."""
    code = main(
        [str(PID), "--print-config", "--container-id", TARGET_CID],
        runner=listening_on(DEBUGPY_PORT),
        **debugpy_seat(tmp_path),
    )

    assert code == 0
    captured = capsys.readouterr()
    connect = json.loads(captured.out)["configurations"][0]["connect"]
    assert connect == {"host": "127.0.0.1", "port": DEBUGPY_PORT}
    assert f"already listening on 127.0.0.1:{DEBUGPY_PORT}" in captured.err
    assert f"held by pid {PID}" in captured.err
    assert "in the target container" in captured.err
    # No injection is offered over the top of a server that is already there.
    assert "nothing is listening" not in captured.err


def test_a_listener_this_seat_cannot_attribute_is_not_this_pods_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #87 itself. `ss` attributes a socket by scanning `/proc`, which is
    this pod's pid namespace, so a listener held by a process in another pod is
    reported with no owning pid at all. Unknown is the answer, and unknown must
    not be spelled "in this pod"."""
    code = main(
        [str(PID), "--print-config"],
        runner=stranger_on(DEBUGPY_PORT),
        **debugpy_seat(tmp_path),
    )

    assert code == 0
    captured = capsys.readouterr()
    # The whole of the defect: the emitted configuration used to point at the
    # stranger's port. It points at the port this run would serve on instead.
    assert json.loads(captured.out)["configurations"][0]["connect"]["port"] == (
        INJECT_PORT
    )
    assert "already listening" not in captured.err
    assert "cannot attribute the socket to any container in this pod" in captured.err
    assert "--port" in captured.err


def test_a_zombie_holding_the_port_is_not_a_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`kill -0` is not this check. Under `shareProcessNamespace` pid 1 reaps
    nothing, so an orphaned child stays a zombie for the pod's lifetime — and
    that is how a bootstrap once handed a client a dead port (report 3.19)."""
    seat = debugpy_seat(tmp_path)
    proc = cast(Path, seat["proc"])
    (proc / str(PID) / "stat").write_text(proc_stat(PID, "python", state="Z"))

    code = main([str(PID), "--print-config"], runner=listening_on(DEBUGPY_PORT), **seat)

    assert code == 0
    captured = capsys.readouterr()
    assert "already listening" not in captured.err
    assert "cannot attribute the socket" in captured.err


def test_a_listener_held_by_a_pid_outside_this_namespace_is_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pid `ss` reports can be stale, or in a namespace this seat cannot
    read. Either way there is no `/proc` entry to attribute it with, and a port
    number on its own establishes nothing."""
    code = main(
        [str(PID), "--print-config"],
        runner=ss_saying(ss_line(DEBUGPY_PORT, pid=4242)),
        **debugpy_seat(tmp_path),
    )

    assert code == 0
    assert "cannot attribute the socket" in capsys.readouterr().err


def test_the_server_this_run_starts_gets_a_port_the_kernel_chose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The acceptance property, at unit scale: two seats on one node must not
    both inject on 5678. The injection, the emitted configuration and the
    re-probe that confirms the server all have to agree on the one port."""
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES)
    write_debugpy(
        proc / str(PID) / "root" / SITE_PACKAGES.lstrip("/"),
        helpers=["attach_linux_amd64.so"],
    )
    uv = InstallingUv()
    code = main(
        [str(PID), "--print-config", "--provision"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        port_chooser=fixed_port,
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )

    assert code == 0
    assert uv.injected_port == INJECT_PORT
    captured = capsys.readouterr()
    assert json.loads(captured.out)["configurations"][0]["connect"]["port"] == (
        INJECT_PORT
    )
    # The re-probe has to follow the injection to its port. Looking at 5678
    # afterwards would report the run's own new server missing and print the
    # "start one" hint over the top of the one just started.
    assert "nothing is listening" not in captured.err
    assert f"already listening on 127.0.0.1:{INJECT_PORT}" in captured.err


def test_a_named_port_is_the_users_and_is_not_overridden(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--port` is the way to pin one, so a verb that chose anyway would leave
    no way to. It pins both halves: where a running server is looked for, and
    where a new one is started."""
    code = main(
        [str(PID), "--print-config", "--port", "9999"],
        runner=no_listeners,
        **debugpy_seat(tmp_path),
    )

    assert code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["configurations"][0]["connect"]["port"] == 9999
    assert "--listen 127.0.0.1:9999" in captured.err


def test_ephemeral_port_is_read_back_from_the_kernel(tmp_path: Path) -> None:
    """Bind zero and read back what was assigned. The only unit-level proof
    that this is a *choice* rather than a constant is that the number it hands
    back is one nothing holds — so it is bound here, once, and released."""
    port = ephemeral_port()
    assert 1024 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_a_seat_that_cannot_bind_falls_back_rather_than_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A seat with no loopback still authors a correct configuration; it just
    cannot promise the port is free, and says which flag pins one."""

    def refuses() -> int:
        raise OSError(1, "Operation not permitted")

    seat = debugpy_seat(tmp_path)
    seat["port_chooser"] = refuses
    code = main([str(PID), "--print-config"], runner=no_listeners, **seat)

    assert code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["configurations"][0]["connect"]["port"] == (
        DEBUGPY_PORT
    )
    assert "could not ask the kernel for a free port" in captured.err
    assert "`--port`" in captured.err


# -- what a hostNetwork pod is told ------------------------------------------


def test_a_host_network_pod_is_warned_the_port_is_node_visible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allowed, with the warning, and not refused: the pods podbench exists for
    at Diamond are hostNetwork IOCs. A debugpy server authenticates nobody, so
    on the node's loopback it is an arbitrary-code-execution endpoint for every
    other hostNetwork pod and every node daemon on the box."""
    monkeypatch.setenv("PODBENCH_HOST_NETWORK", "true")
    code = main(
        [str(PID), "--print-config"], runner=no_listeners, **debugpy_seat(tmp_path)
    )

    assert code == 0
    warned = capsys.readouterr().err
    assert "hostNetwork: true" in warned
    assert "authenticates nobody" in warned
    assert "node daemon" in warned
    # Every prose remedy names its flag, and the way out of a shared port is to
    # pin one.
    assert "`--port`" in warned


def test_a_pod_with_its_own_namespace_is_not_warned_about_the_node(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary case, where loopback is the pod's and the blast radius is
    the one the seat already has. A warning here would be noise on every run."""
    monkeypatch.setenv("PODBENCH_HOST_NETWORK", "false")
    main([str(PID), "--print-config"], runner=no_listeners, **debugpy_seat(tmp_path))

    warned = capsys.readouterr().err
    assert "hostNetwork" not in warned
    assert "node daemon" not in warned


def test_a_seat_without_the_variable_says_unknown_and_not_no(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seat landed by an older launcher carries no such variable. Reading its
    absence as "no hostNetwork" would put back exactly the claim the whole
    change exists to stop, so absence is a third answer with its own sentence
    and its own way out."""
    monkeypatch.delenv("PODBENCH_HOST_NETWORK", raising=False)
    main([str(PID), "--print-config"], runner=no_listeners, **debugpy_seat(tmp_path))

    warned = capsys.readouterr().err
    assert "cannot tell whether" in warned
    assert "podbench attach" in warned
