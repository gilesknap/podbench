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
from pathlib import Path
from typing import cast

import pytest

from podbench.gdbcmd import attach_commands
from podbench.vscode import (
    GDB_WRAPPER,
    MACHINE_SETTINGS_PATH,
    SEAT_CWD,
    SEAT_MACHINE_SETTINGS,
    cppdbg_configuration,
    launch_json_text,
    lldb_configuration,
    main,
    merge_launch_json,
    merge_machine_settings,
    setup_commands,
    target_architecture,
)

PID = 597
EXE = "/app/victim"


@pytest.fixture
def proc_tree(tmp_path: Path) -> Path:
    """A ``/proc`` with one process whose ``exe`` link resolves."""
    entry = tmp_path / str(PID)
    entry.mkdir()
    (entry / "exe").symlink_to(EXE)
    return tmp_path


# -- the fields that fail silently when wrong -------------------------------


def test_program_is_sysroot_prefixed() -> None:
    """The unprefixed form reads this container's binary and looks fine."""
    assert cppdbg_configuration(PID, EXE)["program"] == f"/proc/{PID}/root/app/victim"


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
    assert cppdbg_configuration(PID, EXE)["cwd"] == SEAT_CWD


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
    code = main(["--source-map", "/=/proc/1/root", "--print-config"], proc=proc_tree)
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
    assert main([str(PID), "--print-config"], proc=proc_tree) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["configurations"][0]["processId"] == str(PID)


def test_writes_to_the_named_path(proc_tree: Path, tmp_path: Path) -> None:
    output = tmp_path / "out" / "launch.json"
    assert main([str(PID), "--output", str(output)], proc=proc_tree) == 0
    assert json.loads(output.read_text())["configurations"][0]["type"] == "cppdbg"


def test_lldb_flag_switches_adapter(
    proc_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(PID), "--lldb", "--print-config"], proc=proc_tree) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["configurations"][0]["type"] == "lldb"


def test_unreadable_exe_is_refused_rather_than_guessed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A guessed ``program`` is the silent failure this module exists to stop."""
    (tmp_path / str(PID)).mkdir()
    code = main([str(PID), "--print-config"], proc=tmp_path)
    assert code != 0
    assert "--program" in capsys.readouterr().err


def test_explicit_program_overrides_an_unreadable_exe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / str(PID)).mkdir()
    code = main([str(PID), "--program", "/app/other", "--print-config"], proc=tmp_path)
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["configurations"][0]["program"] == f"/proc/{PID}/root/app/other"


def test_no_pid_and_no_container_id_refuses_to_guess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "The target is PID 1" is wrong under ``shareProcessNamespace``."""
    monkeypatch.delenv("PODBENCH_TARGET_CID", raising=False)
    assert main(["--print-config"], proc=tmp_path) != 0
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
