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
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from podbench.flavour import Mode
from podbench.gdbcmd import attach_commands
from podbench.kubectl import CommandResult
from podbench.vscode import (
    GDB_WRAPPER,
    MACHINE_SETTINGS_PATH,
    SEAT_CWD,
    SEAT_FOLDER_SETTINGS,
    SEAT_MACHINE_SETTINGS,
    cppdbg_configuration,
    cppdbg_launch_configuration,
    debugpy_attach_configuration,
    debugpy_launch_configuration,
    delve_configuration,
    extensions_for,
    launch_json_text,
    launch_setup_commands,
    lldb_configuration,
    main,
    merge_extensions_json,
    merge_folder_settings,
    merge_launch_json,
    merge_machine_settings,
    python_path_mappings,
    setup_commands,
    target_architecture,
)
from test_elf import EM_AARCH64
from test_flavour import (
    SITE_PACKAGES,
    python_proc,
    seat_debugpy,
    which_of,
    write_debugpy,
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
    cpptools alone is 330 MiB (issue #42)."""
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
    entries called "podbench: attach to victim" are a coin toss.
    """
    assert cli([str(PID), "--print-config"], proc_tree) == 0
    document = json.loads(capsys.readouterr().out)
    names = [entry["name"] for entry in document["configurations"]]
    assert names == [
        "podbench: attach to victim (gdb)",
        "podbench: attach to victim (lldb)",
    ]


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


def test_observe_mode_maps_the_source_through_the_sysroot() -> None:
    """The editor sees the source through ``/proc/<pid>/root``; the app does not."""
    assert python_path_mappings(1, "/src", Mode.OBSERVE) == [
        {"localRoot": "/proc/1/root/src", "remoteRoot": "/src"}
    ]


def test_dev_mode_has_no_mappings_at_all() -> None:
    """Editor and interpreter are the same inodes, and a spurious mapping is
    the same silent wrong answer as a missing one: breakpoints never bind."""
    assert python_path_mappings(1, "/src", Mode.DEV) == []


def test_debugpy_connects_rather_than_listens() -> None:
    """The server is in the app process; the editor is the client.

    ``127.0.0.1`` is right even in Observe mode — separate containers, one
    network namespace — so no port-forward is involved.
    """
    config = debugpy_attach_configuration(1, name="x", port=5678, source_root="/src")
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


# -- the Python path, end to end ---------------------------------------------


def no_listeners(
    argv: Sequence[str], *, stdin: str | None = None, capture: bool = True
) -> CommandResult:
    """An ``ss`` that reports an empty pod. Injected: the unit suite may not
    shell out, and whether something is listening decides which debugpy shape
    is emitted."""
    return CommandResult(tuple(argv), 0, "State Recv-Q Send-Q Local Peer\n", "")


def test_a_python_target_emits_debugpy_with_the_observe_mapping(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole language x mode x architecture path, through the CLI.

    An amd64 Python service with debugpy on both sides: the configuration is a
    ``connect``, and its ``pathMappings`` carries the sysroot on the left and
    the target's own path on the right.
    """
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES)
    code = main(
        [str(PID), "--print-config"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench"),
        runner=no_listeners,
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )
    assert code == 0
    captured = capsys.readouterr()
    configurations = json.loads(captured.out)["configurations"]
    assert [entry["type"] for entry in configurations] == ["debugpy"]
    assert configurations[0]["pathMappings"] == [
        {"localRoot": f"/proc/{PID}/root/src", "remoteRoot": "/src"}
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
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )
    assert code != 0
    assert "attach_linux_arm64.so" in capsys.readouterr().err


# -- provisioning debugpy into the target ------------------------------------


class InstallingUv:
    """A uv that really unpacks a debugpy-shaped tree where it is told to.

    It has to, because the point of the flag is what happens *after*: the
    debugpy configuration may only be emitted once the **target** can import
    debugpy, and nothing but the filesystem answers that. Anything that is not
    uv is the ``ss`` this verb also runs.
    """

    def __init__(self) -> None:
        self.argv: list[str] = []

    def __call__(
        self, argv: Sequence[str], *, stdin: str | None = None, capture: bool = True
    ) -> CommandResult:
        if argv[0] != "uv":
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
    # The install is only half of it: the injection is still printed rather than
    # run, and its PYTHONPATH is the copy that was just made.
    assert f"PYTHONPATH=/proc/{PID}/root/opt/podbench-debugpy" in captured.err
    for caveat in ("egress", "restart", "ephemeral storage"):
        assert caveat in captured.err


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
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )
    assert uv.argv == []
    assert "--provision-python X.Y" in capsys.readouterr().err


def test_provision_without_ptrace_says_so_and_still_installs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not a refusal, unlike arm64: the copy outlives the seat.

    It goes into the *target's* rootfs, so a relaunch on the ``full`` rung picks
    it up rather than repeating the install — but the run ends in
    "CAP_SYS_PTRACE is not in this seat's effective set", and the two have to be
    joined up or the install reads as having been spent for nothing.
    """
    proc = python_proc(tmp_path, cap_sys_ptrace=False)
    uv = InstallingUv()
    code = main(
        [str(PID), "--print-config", "--provision"],
        proc=proc,
        which=which_of("gdb", "gdb-podbench", "uv"),
        runner=uv,
        debugpy_root=seat_debugpy(tmp_path, helpers=["attach_linux_amd64.so"]),
    )
    assert code != 0
    captured = capsys.readouterr()
    assert "outlives this seat" in captured.err
    assert "CAP_SYS_PTRACE is not in this seat's effective set" in captured.err
    assert uv.argv[:3] == ["uv", "pip", "install"]
