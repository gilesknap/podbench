"""Author the VS Code configuration for a seat — the debug config, and the
machine-level settings that keep opening a folder from killing the seat.

``attach`` already does this job for ssh: it writes a ready-made stanza and
prints the alias, so nobody hand-writes a ProxyCommand. Debugging had no
equivalent — the user copied a template out of a how-to and filled in the pid,
the sysroot-prefixed program path and the source mapping themselves. Every one
of those is something podbench already knows, and every one of them fails
*silently* when wrong: the wrong ``program`` reads the debug image's own binary
and produces a plausible backtrace off the wrong symbols, and a ``sourceFileMap``
rooted at ``/`` makes gdb re-apply the substitution on display and emit
``/proc/1/root/proc/1/root/…`` (report 3.3, and the anti-patterns in
``docs/how-to/debug-with-gdb.md``).

Two fields here exist only because of measured failures rather than taste:

* ``miDebuggerPath`` names :data:`GDB_WRAPPER`, not ``/usr/bin/gdb``. cpptools
  launches gdb inheriting cpptools' own cwd — its extension directory — which
  VS Code deletes on extension update; gdb's libpython then fails ``getcwd()``
  during ``-enable-pretty-printing`` and dies before it can name the signal.
  See ``image/bin/gdb-podbench``.
* ``cwd`` is set explicitly. On a developer's machine ``${workspaceFolder}``
  always exists so nobody sets it; in a seat it can resolve to nothing, and the
  result is that same unformattable crash.

The ordering inside ``setupCommands`` is not this module's invention: it is
:func:`podbench.gdbcmd.attach_commands` with the two lines cpptools issues
itself removed, so the sequence report 3.3 made load-bearing cannot drift
between the CLI path and the DAP path.

:data:`SEAT_MACHINE_SETTINGS` is the other half, and the one that has to be in
place *before* the user does anything: File -> Open Folder -> ``/`` is the
obvious first move in a seat and it can OOM the container unrecoverably. See
that constant for why each entry is there. :func:`podbench.agent.ensure_vscode_settings`
is what installs it, because the file lives in a directory the client creates.
"""

from __future__ import annotations

import copy
import json
import platform
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from .cli import new_app, run
from .gdbcmd import (
    EXIT_USAGE,
    attach_commands,
    read_exe,
    resolve_target_pid,
    sysroot_path,
)
from .model import as_dict
from .proc import DEFAULT_PROC

__all__ = [
    "ADAPTER_CPPDBG",
    "ADAPTER_LLDB",
    "GDB_WRAPPER",
    "MACHINE_SETTINGS_PATH",
    "SEAT_CWD",
    "SEAT_MACHINE_SETTINGS",
    "cppdbg_configuration",
    "launch_json_text",
    "lldb_configuration",
    "machine_settings_text",
    "main",
    "merge_launch_json",
    "merge_machine_settings",
    "setup_commands",
    "target_architecture",
]

GDB_WRAPPER = "/usr/local/bin/gdb-podbench"
"""The image's cwd-safe gdb. Never ``/usr/bin/gdb`` — see the module docstring."""

SEAT_CWD = "/root"
"""Where the debug adapter should start gdb.

Any directory that exists would do; the seat's home is the one the image
guarantees. The value matters far less than the field being present at all.
"""

ADAPTER_CPPDBG = "cppdbg"
ADAPTER_LLDB = "lldb"

_VERSION = "0.2.0"
"""The ``launch.json`` schema version, which VS Code has never bumped."""

#: ``platform.machine()`` to cpptools' ``targetArchitecture`` spelling. Wrong or
#: absent, cpptools logs "Debuggee TargetArchitecture not detected, assuming
#: x86_64" and decodes registers for the wrong machine.
_ARCHITECTURES = {
    "x86_64": "x64",
    "amd64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
    "armv6l": "arm",
    "i386": "x86",
    "i686": "x86",
}

_DESCRIPTION = (
    "Write the VS Code debug configuration for this seat, with the pid, the "
    "sysroot-prefixed program path and the gdb setup order already filled in."
)

MACHINE_SETTINGS_PATH = ".vscode-server/data/Machine/settings.json"
"""VS Code's machine-scope settings file, relative to the ssh session's ``$HOME``.

Machine scope, not user or workspace scope, because it is the only one that
applies to *every* folder without the user having configured anything — and the
folder that kills the seat is the first one they open.

The path is relative because the home it hangs off is the one the **passwd
record** names, which is not always the container's ``$HOME``; see
:func:`podbench.agent.session_home`.
"""

_SEAT_EXCLUDES = {
    # /proc/<pid>/root is a symlink into another container's root, so a
    # recursive walk from / has no bottom. An ephemeral seat shares the pod's
    # memory limit and cannot reserve its own (report 3.9), and an OOM-killed
    # ephemeral container cannot be restarted — the seat is gone and its name is
    # burnt for the pod's lifetime.
    "**/proc/**": True,
    # sysfs is a symlink graph with cycles (/sys/class/* back into
    # /sys/devices/*) and nothing under it is source.
    "**/sys/**": True,
    # /dev/fd is a symlink to /proc/self/fd, so a walker that skipped /proc
    # re-enters it here.
    "**/dev/**": True,
    # ~/.vscode-server is 700 MiB before a single extension and 2.2 GB with a
    # few (s2 §"Aggregate"), and the seat's own home is a folder we tell people
    # to open.
    "**/.vscode-server/**": True,
}

SEAT_MACHINE_SETTINGS: dict[str, Any] = {
    "files.watcherExclude": dict(_SEAT_EXCLUDES),
    "search.exclude": dict(_SEAT_EXCLUDES),
    # ripgrep is given --follow by default, and the one thing a seat must never
    # follow is /proc/<pid>/root: it is the doorway into every other container
    # in the pod, and /proc/self/root makes the walk re-enter itself.
    "search.followSymlinks": False,
    # Pylance's spelling: a list of globs, and absolute ones, rather than the
    # workspace-relative object the two above take.
    "python.analysis.exclude": [
        "/proc/**",
        "/sys/**",
        "/dev/**",
        "**/.vscode-server/**",
    ],
    # cpptools' tag parser walks the workspace on its own account, so excluding
    # it from search and the watcher does not stop it. cpptools is the extension
    # this image's debug configuration is written for.
    "C_Cpp.files.exclude": dict(_SEAT_EXCLUDES),
}
"""The machine settings a seat needs before its first folder is opened.

Deliberately *not* ``files.exclude``: that hides the paths from the explorer,
and browsing the workload's filesystem through ``/proc/<pid>/root`` is the whole
point of Observe mode. Opening a *file* under ``/proc`` is safe; it is the
recursive walk a folder starts that is not.
"""


def machine_settings_text(settings: Mapping[str, Any]) -> str:
    """A whole machine ``settings.json`` document, newline-terminated."""
    return json.dumps(dict(settings), indent=2) + "\n"


def _add_missing_keys(current: dict[str, Any], defaults: Mapping[str, Any]) -> bool:
    """Add the keys ``current`` lacks. Returns whether anything was added."""
    added = False
    for key, value in defaults.items():
        if key not in current:
            current[key] = value
            added = True
    return added


def _append_missing(current: list[Any], defaults: Sequence[Any]) -> bool:
    """Append the entries ``current`` lacks. Returns whether anything was."""
    added = False
    for value in defaults:
        if value not in current:
            current.append(value)
            added = True
    return added


def merge_machine_settings(existing: str | None) -> str | None:
    """Add :data:`SEAT_MACHINE_SETTINGS` to a settings document, clobbering none
    of it. ``None`` means the document already says everything we would say.

    Merged per *key* rather than written only when the file is absent, and the
    difference is not cosmetic: the file-level rule would mean a user who set
    one unrelated setting — a font size, a theme — silently loses every exclude,
    and the failure that costs is an unrecoverable seat. Merged per key, an
    existing value always wins, including a deliberate ``"**/proc/**": false``,
    and a pattern we never heard of is left where it is.

    Raises ``ValueError`` when the file cannot be parsed, for the same reason
    :func:`merge_launch_json` does: VS Code permits comments in ``settings.json``
    and :mod:`json` does not, so rewriting it would discard whatever this parser
    could not see. The caller reports the refusal rather than swallowing it —
    unapplied excludes are exactly the silence this file exists to end.

    >>> shipped = merge_machine_settings(None)
    >>> json.loads(shipped)["search.followSymlinks"]
    False
    >>> merge_machine_settings(shipped) is None
    True
    >>> mine = merge_machine_settings('{"search.followSymlinks": true}')
    >>> json.loads(mine)["search.followSymlinks"]
    True
    >>> json.loads(mine)["search.exclude"]["**/proc/**"]
    True
    """
    if existing is None or not existing.strip():
        return machine_settings_text(copy.deepcopy(SEAT_MACHINE_SETTINGS))
    document: Any
    try:
        document = json.loads(existing)
    except ValueError as error:
        raise ValueError(f"cannot parse the existing settings.json: {error}") from error
    if not isinstance(document, dict):
        raise ValueError("the existing settings.json is not a JSON object")
    settings = cast("dict[str, Any]", document)

    changed = False
    for key, default in SEAT_MACHINE_SETTINGS.items():
        current: Any = settings.get(key)
        if isinstance(default, dict) and isinstance(current, dict):
            changed |= _add_missing_keys(
                cast("dict[str, Any]", current), cast("dict[str, Any]", default)
            )
        elif isinstance(default, list) and isinstance(current, list):
            changed |= _append_missing(
                cast("list[Any]", current), cast("list[Any]", default)
            )
        elif key not in settings:
            settings[key] = copy.deepcopy(cast("Any", default))
            changed = True
        # Anything else is a value the user set to a shape we did not expect,
        # and theirs beats ours.
    return machine_settings_text(settings) if changed else None


def target_architecture(machine: str | None = None) -> str | None:
    """cpptools' spelling of this machine, or ``None`` if it has no opinion.

    >>> target_architecture("aarch64")
    'arm64'
    >>> target_architecture("x86_64")
    'x64'
    >>> target_architecture("riscv64") is None
    True
    """
    return _ARCHITECTURES.get(machine or platform.machine())


def setup_commands(
    pid: int,
    *,
    source_dirs: Sequence[str] = (),
    debuginfod: bool = True,
) -> list[str]:
    """The gdb settings cpptools must apply *before* it attaches.

    Derived from :func:`podbench.gdbcmd.attach_commands` rather than written out
    again, so the one ordering that produces a correct backtrace has a single
    definition. ``file`` and ``attach`` are dropped because the adapter issues
    both itself, from ``program`` and ``processId``.

    >>> for command in setup_commands(597):
    ...     print(command)
    set pagination off
    set sysroot /proc/597/root
    directory /proc/597/root
    add-auto-load-safe-path /proc/597/root
    set debuginfod enabled on
    """
    return [
        command
        for command in attach_commands(
            pid, exe=None, source_dirs=source_dirs, debuginfod=debuginfod
        )
        if not command.startswith(("file ", "attach "))
    ]


def cppdbg_configuration(
    pid: int,
    program: str,
    *,
    name: str | None = None,
    source_dirs: Sequence[str] = (),
    source_map: Mapping[str, str] | None = None,
    debuginfod: bool = True,
    machine: str | None = None,
) -> dict[str, Any]:
    """One ``cppdbg`` attach configuration for a process in this pod.

    ``program`` is the path *inside the target's rootfs*; it is prefixed with
    the sysroot here, because the unprefixed form reads this container's idea of
    the binary and the resulting backtrace looks entirely believable (3.3).

    >>> config = cppdbg_configuration(597, "/app/victim")
    >>> config["program"]
    '/proc/597/root/app/victim'
    >>> config["miDebuggerPath"]
    '/usr/local/bin/gdb-podbench'
    >>> config["processId"]
    '597'
    """
    configuration: dict[str, Any] = {
        "name": name or f"podbench: attach to {Path(program).name}",
        "type": ADAPTER_CPPDBG,
        "request": "attach",
        # A string, which is what cpptools' own templates use; an int works
        # today but is not what the schema documents.
        "processId": str(pid),
        "program": f"{sysroot_path(pid)}{program}",
        "cwd": SEAT_CWD,
        "MIMode": "gdb",
        "miDebuggerPath": GDB_WRAPPER,
        "setupCommands": [
            {"text": command}
            for command in setup_commands(
                pid, source_dirs=source_dirs, debuginfod=debuginfod
            )
        ],
    }
    architecture = target_architecture(machine)
    if architecture is not None:
        configuration["targetArchitecture"] = architecture
    if source_map:
        configuration["sourceFileMap"] = dict(source_map)
    return configuration


def lldb_configuration(
    pid: int,
    program: str,
    *,
    name: str | None = None,
    source_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The CodeLLDB equivalent, for targets whose toolchain is not gdb's.

    Three deliberate differences from :func:`cppdbg_configuration`, all of them
    lldb's rather than ours: the key is ``sourceMap`` and not ``sourceFileMap``,
    there is no analogue of ``set sysroot`` so the library search paths are set
    by hand, and the pid field is ``pid``.

    >>> config = lldb_configuration(597, "/app/victim")
    >>> config["type"], config["pid"]
    ('lldb', 597)
    """
    root = sysroot_path(pid)
    configuration: dict[str, Any] = {
        "name": name or f"podbench: attach to {Path(program).name} (lldb)",
        "type": ADAPTER_LLDB,
        "request": "attach",
        "pid": pid,
        "program": f"{root}{program}",
        "initCommands": [
            f"settings set target.exec-search-paths {root}/usr/lib {root}/lib",
        ],
    }
    if source_map:
        configuration["sourceMap"] = dict(source_map)
    return configuration


def launch_json_text(configurations: Sequence[Mapping[str, Any]]) -> str:
    """A whole ``launch.json`` document, newline-terminated."""
    document = {"version": _VERSION, "configurations": list(configurations)}
    return json.dumps(document, indent=2) + "\n"


def merge_launch_json(existing: str | None, configuration: Mapping[str, Any]) -> str:
    """Add or replace one configuration, keeping every other one intact.

    Matched on ``name``, so re-running this verb updates its own entry rather
    than appending a second copy, and a hand-written configuration beside it
    survives untouched.

    Raises ``ValueError`` when the existing file cannot be parsed. VS Code
    permits comments in ``launch.json`` and :mod:`json` does not, so refusing is
    the only safe answer — silently rewriting the file would discard the
    comments and any configuration this parser could not see.

    >>> print(merge_launch_json(None, {"name": "a"}), end="")
    {
      "version": "0.2.0",
      "configurations": [
        {
          "name": "a"
        }
      ]
    }
    """
    if existing is None or not existing.strip():
        return launch_json_text([configuration])
    document: Any
    try:
        document = json.loads(existing)
    except ValueError as error:
        raise ValueError(f"cannot parse the existing launch.json: {error}") from error
    if not isinstance(document, dict):
        raise ValueError("the existing launch.json is not a JSON object")
    raw: Any = as_dict(document).get("configurations")
    entries = cast("list[Any]", raw) if isinstance(raw, list) else []
    name = configuration.get("name")
    merged = [
        as_dict(entry)
        for entry in entries
        if isinstance(entry, dict) and as_dict(entry).get("name") != name
    ]
    merged.append(dict(configuration))
    return launch_json_text(merged)


def _warn(message: str) -> None:
    """Warnings go to stderr, so ``--print-config`` stays pasteable."""
    print(f"debug-config: {message}", file=sys.stderr)


def _parse_source_map(entries: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """``FROM=TO`` pairs, and a complaint for anything that is not one."""
    mapping: dict[str, str] = {}
    problems: list[str] = []
    for entry in entries:
        source, separator, destination = entry.partition("=")
        if not separator or not source or not destination:
            problems.append(f"--source-map needs FROM=TO, got {entry!r}")
            continue
        if source == "/":
            # gdb re-applies a root substitution on display, so the fullname the
            # adapter hands the editor grows a /proc/<pid>/root prefix per stop.
            problems.append(
                "--source-map / is the doubling anti-pattern: gdb re-applies it "
                "on display and the editor gets /proc/<pid>/root/proc/<pid>/root/… "
                "— map the compilation directory instead (`info source` prints it)"
            )
            continue
        mapping[source] = destination
    return mapping, problems


def _run(
    pid: int | None,
    container_id: str | None,
    *,
    program: str | None,
    source_dirs: Sequence[str],
    source_map_entries: Sequence[str],
    debuginfod: bool,
    lldb: bool,
    print_config: bool,
    output: str | None,
    proc: Path,
) -> int:
    source_map, problems = _parse_source_map(source_map_entries)
    for problem in problems:
        _warn(problem)
    if problems:
        return EXIT_USAGE

    pid, notes = resolve_target_pid(pid, container_id, proc=proc)
    for note in notes:
        _warn(note)
    if pid is None:
        return EXIT_USAGE

    target_program = program or read_exe(pid, proc=proc)
    if target_program is None:
        # Refused rather than guessed: cpptools requires `program`, and a wrong
        # one is the silent failure this whole module exists to prevent.
        _warn(
            f"could not read /proc/{pid}/exe (it needs PTRACE_MODE_READ), so "
            "the target's binary is unknown. Pass --program with the path "
            "inside the target's own rootfs."
        )
        return EXIT_USAGE

    configuration = (
        lldb_configuration(pid, target_program, source_map=source_map)
        if lldb
        else cppdbg_configuration(
            pid,
            target_program,
            source_dirs=source_dirs,
            source_map=source_map,
            debuginfod=debuginfod,
        )
    )

    if print_config:
        print(launch_json_text([configuration]), end="")
        return 0

    path = Path(output) if output else Path.cwd() / ".vscode" / "launch.json"
    existing = path.read_text() if path.exists() else None
    try:
        text = merge_launch_json(existing, configuration)
    except ValueError as error:
        _warn(
            f"{error}. Re-run with --print-config and paste the configuration "
            "in by hand, or --output a different path."
        )
        return EXIT_USAGE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"debug-config written to {path}")
    print(f'then: Run and Debug -> "{configuration["name"]}"')
    if path.parent.parent != Path.cwd():
        # VS Code reads .vscode/launch.json from the folder that is *open*, not
        # from $HOME, which is the commonest reason a written config never shows
        # up in the Run and Debug list.
        print(
            f"note: VS Code only sees this if {path.parent.parent} is the open "
            "folder; use --output to write it beside the folder you opened"
        )
    return 0


def main(args: Sequence[str] | None = None, *, proc: Path = DEFAULT_PROC) -> int:
    """``podbench debug-config`` — author ``launch.json`` for this seat.

    ``proc`` is a test seam; the CLI passes none.
    """
    app = new_app()

    @app.command(help=_DESCRIPTION)
    def debug_config(
        pid: Annotated[
            int | None,
            typer.Argument(
                metavar="[PID]",
                help="pid to attach to; discovered from the container id if omitted",
            ),
        ] = None,
        container_id: Annotated[
            str | None,
            typer.Option(
                "--container-id",
                metavar="ID",
                help="target container id used to discover the pid "
                "(default: $PODBENCH_TARGET_CID)",
            ),
        ] = None,
        program: Annotated[
            str | None,
            typer.Option(
                "--program",
                metavar="PATH",
                help="the target's binary as its own rootfs spells it, when "
                "/proc/<pid>/exe cannot be read. It is prefixed with the sysroot "
                "here, so do not prefix it yourself",
            ),
        ] = None,
        source_dir: Annotated[
            list[str] | None,
            typer.Option(
                "--source-dir",
                metavar="DIR",
                help="extra source directory in *this* container, wired with "
                "gdb's `directory`. Repeatable",
            ),
        ] = None,
        source_map: Annotated[
            list[str] | None,
            typer.Option(
                "--source-map",
                metavar="FROM=TO",
                help="map a DWARF compilation directory (`info source` prints "
                "it) onto a readable path. Repeatable",
            ),
        ] = None,
        no_debuginfod: Annotated[
            bool,
            typer.Option(
                "--no-debuginfod",
                help="do not enable debuginfod (it needs ca-certificates and network)",
            ),
        ] = False,
        lldb: Annotated[
            bool,
            typer.Option(
                "--lldb",
                help="emit a CodeLLDB configuration instead of cpptools' cppdbg",
            ),
        ] = False,
        print_config: Annotated[
            bool,
            typer.Option(
                "--print-config",
                help="print the configuration instead of writing it",
            ),
        ] = False,
        output: Annotated[
            str | None,
            typer.Option(
                "--output",
                metavar="PATH",
                help="where to write it (default: ./.vscode/launch.json)",
            ),
        ] = None,
    ) -> None:
        raise typer.Exit(
            _run(
                pid,
                container_id,
                program=program,
                source_dirs=source_dir or (),
                source_map_entries=source_map or (),
                debuginfod=not no_debuginfod,
                lldb=lldb,
                print_config=print_config,
                output=output,
                proc=proc,
            )
        )

    return run(app, args, prog="debug-config")
