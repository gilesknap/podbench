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

**Which** debugger is not this module's decision. :mod:`podbench.flavour`
measures the target and returns a verdict per flavour; this module turns each
available one into a configuration and each unavailable one into a sentence
naming the mechanism. ``launch.json`` holds a *list*, so every applicable
configuration is emitted at once and VS Code's own dropdown becomes the choice —
no exclusive guess has to be made, and a wrong guess cannot be made silently.

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

And ``pathMappings`` is **mode-dependent**, which is the one field here that
fails without any error at all — breakpoints simply never bind. In Observe mode
the target is another container, so the editor sees the source through
``/proc/<pid>/root`` and the debuggee reports its own path: a mapping is
required. In a ``dev`` pod editor and interpreter are the same container and
the same inodes, so the mapping must be **empty**. Both spellings are emitted
from the detected mode rather than from a flag, because a user cannot be
expected to know which side of that line they are on.

:data:`SEAT_MACHINE_SETTINGS` is the other half, and the one that has to be in
place *before* the user does anything: File -> Open Folder -> ``/`` is the
obvious first move in a seat and it can OOM the container unrecoverably. See
that constant for why each entry is there. :func:`podbench.agent.ensure_vscode_settings`
is what installs it, because the file lives in a directory the client creates.
:data:`SEAT_FOLDER_SETTINGS` is the same guard in the folder's own file, which
is the one copy that survives *Kill/Uninstall VS Code Server on Host* — and the
only one podbench can put in place from the laptop, which is what
``attach --open`` does.
"""

from __future__ import annotations

import copy
import json
import platform
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from .cli import new_app, run
from .elf import debugpy_helper_name, debugpy_helper_published
from .execfile import gdb_exec_file
from .flavour import (
    DEBUGPY_PORT,
    Assessment,
    Flavour,
    Language,
    Mode,
    Seat,
    Target,
    Which,
    assess,
    can_ptrace_target,
    detect_mode,
    injection_command,
    inspect_target,
    survey_seat,
)
from .gdbcmd import (
    EXIT_USAGE,
    attach_commands,
    launch_commands,
    resolve_target_pids,
    sysroot_path,
)
from .kubectl import Runner, run_subprocess
from .model import as_dict, describe_pause
from .probe import Attacher, default_attacher
from .proc import DEFAULT_PROC
from .provision import (
    PROVISION_DEST,
    inject_debugpy,
    provision_debugpy,
    provision_paste,
    target_destination,
)

__all__ = [
    "ADAPTER_CPPDBG",
    "ADAPTER_DEBUGPY",
    "ADAPTER_DELVE",
    "ADAPTER_LLDB",
    "EXTENSIONS",
    "GDB_WRAPPER",
    "MACHINE_SETTINGS_PATH",
    "SEAT_CWD",
    "SEAT_FOLDER_SETTINGS",
    "SEAT_MACHINE_SETTINGS",
    "configurations_for",
    "cppdbg_configuration",
    "cppdbg_launch_configuration",
    "debugpy_attach_configuration",
    "debugpy_launch_configuration",
    "delve_configuration",
    "extensions_for",
    "extensions_json_text",
    "launch_json_text",
    "launch_setup_commands",
    "lldb_configuration",
    "machine_settings_text",
    "main",
    "measured_attach",
    "merge_extensions_json",
    "merge_folder_settings",
    "merge_launch_configs",
    "merge_launch_json",
    "merge_machine_settings",
    "program_load_error",
    "python_path_mappings",
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
ADAPTER_DEBUGPY = "debugpy"
ADAPTER_DELVE = "go"
"""The Go extension's adapter type, which is ``go`` and not ``delve``: the
flavour is named after the debugger, the ``type`` after the extension."""

DEBUGPY_HOST = "127.0.0.1"
"""Right in both modes, and worth stating because it looks wrong in Observe
mode: the seat and the app are separate containers but share the pod's network
namespace, so no port-forward and no tunnel is involved."""

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
    "arm": "arm",
    "armv7l": "arm",
    "armv6l": "arm",
    "i386": "x86",
    "i686": "x86",
}

_DESCRIPTION = (
    "Write the VS Code debug configuration for this seat: one entry per "
    "debugger flavour that applies, with the pid, the sysroot-prefixed program "
    "path and the mode's path mappings already filled in."
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


SEAT_FOLDER_SETTINGS: dict[str, Any] = dict(SEAT_MACHINE_SETTINGS)
"""The same guard, in a file podbench controls — derived, never restated.

Machine scope reaches every folder and is the right home for all of these, but
the machine file lives under ``~/.vscode-server``, a directory the client owns
and *Kill/Uninstall VS Code Server on Host* deletes wholesale. A folder's own
``.vscode/settings.json`` is the copy that survives that, and the only one
``attach --open`` can write before a client exists.

Every key, not a resource-scoped subset. ``--open`` opens a *single* folder, so
that file is VS Code's **workspace** settings, which honour window- and
resource-scoped settings alike; only machine and application scope are dropped
there, and none of these is either. The asymmetry settles what is left over:
a key VS Code ignores costs nothing, while an omitted one costs the seat — and
``C_Cpp.files.exclude`` is the omission that would bite, since cpptools' tag
parser walks on its own account (so the search and watcher excludes do not stop
it) and cpptools is exactly what ``--open`` installs for a C/C++ target.

Two copies of an exclude list would be two things to keep true, and the one that
drifted would go on looking correct right up to the walk that ends the seat.
"""

#: Adapter ``type`` to the extension that contributes it. Keyed on the type
#: rather than on :class:`podbench.flavour.Flavour` because the type in an
#: emitted configuration *is* the extension's own identifier for it: install
#: what the types name and a configuration VS Code cannot start is impossible by
#: construction. ``debugpy`` takes two because ``ms-python.debugpy`` is the
#: adapter and ``ms-python.python`` is what registers the interpreter it debugs
#: — and that second one is an extension *pack*, so VS Code resolves
#: ``ms-python.vscode-pylance`` alongside it whatever is asked for here. That is
#: why :data:`SEAT_MACHINE_SETTINGS` carries ``python.analysis.exclude``: Pylance
#: is going to be in the seat, and it walks on its own account.
EXTENSIONS: dict[str, tuple[str, ...]] = {
    ADAPTER_CPPDBG: ("ms-vscode.cpptools",),
    ADAPTER_LLDB: ("vadimcn.vscode-lldb",),
    ADAPTER_DEBUGPY: ("ms-python.python", "ms-python.debugpy"),
    ADAPTER_DELVE: ("golang.go",),
}


def extensions_for(configurations: Sequence[Mapping[str, Any]]) -> list[str]:
    """The extensions ``configurations`` cannot run without, in emission order.

    The debuggers the target actually has, and no others: in Observe mode an
    extension is unpacked into the seat's ``~/.vscode-server``, which sits on the
    *workload's* ephemeral-storage budget — an ephemeral container may not
    declare ``resources`` (report 3.9) — and ``ms-vscode.cpptools`` alone is
    330 MiB against a server that already measured 1215 MiB live. Installing a
    language the target does not use spends the workload's disk on nothing
    (issue #42).

    What this cannot promise is "and nothing else". ``ms-python.python`` is an
    extension *pack*: s2 §7 ran the install and got ``vscode-python-envs``,
    ``debugpy`` and ``vscode-pylance`` with it, and Pylance alone is a 117 MiB
    install whose RSS is still unmeasured (report R2). It stays on the list
    anyway — without it the interpreter the ``debugpy`` configuration names is
    not registered, and the CLI has no "without its pack" — which is why
    :data:`SEAT_MACHINE_SETTINGS` carries ``python.analysis.exclude``: the guard
    is written for the extensions that actually arrive, not for the two named
    here.

    >>> extensions_for([{"type": "debugpy"}, {"type": "debugpy"}])
    ['ms-python.python', 'ms-python.debugpy']
    >>> extensions_for([{"type": "coreclr"}])
    []
    """
    found: list[str] = []
    for configuration in configurations:
        adapter = configuration.get("type")
        for extension in (
            EXTENSIONS.get(adapter, ()) if isinstance(adapter, str) else ()
        ):
            if extension not in found:
                found.append(extension)
    return found


def extensions_json_text(recommendations: Sequence[str]) -> str:
    """A whole ``extensions.json`` document, newline-terminated."""
    return json.dumps({"recommendations": list(recommendations)}, indent=2) + "\n"


def merge_extensions_json(
    existing: str | None, recommendations: Sequence[str]
) -> str | None:
    """Add ``recommendations`` to a folder's ``extensions.json``.

    ``None`` means every one of them is already recommended. Recommendations are
    a *fallback* for the install podbench performs itself: the prompt VS Code
    raises from this file offers "Install in SSH: ``<alias>``", which is the one
    place the choice can still be got wrong by hand.

    Raises ``ValueError`` on a file that will not parse, for the reason
    :func:`merge_launch_configs` does.

    >>> print(merge_extensions_json(None, ["golang.go"]), end="")
    {
      "recommendations": [
        "golang.go"
      ]
    }
    >>> merge_extensions_json('{"recommendations": ["golang.go"]}', ["golang.go"])
    """
    if existing is None or not existing.strip():
        return extensions_json_text(recommendations)
    document: Any
    try:
        document = json.loads(existing)
    except ValueError as error:
        raise ValueError(
            f"cannot parse the existing extensions.json: {error}"
        ) from error
    if not isinstance(document, dict):
        raise ValueError("the existing extensions.json is not a JSON object")
    settings = cast("dict[str, Any]", document)
    raw: Any = settings.get("recommendations")
    current = cast("list[Any]", raw) if isinstance(raw, list) else []
    if not _append_missing(current, recommendations):
        return None
    settings["recommendations"] = current
    return json.dumps(settings, indent=2) + "\n"


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
    return _merge_settings(existing, SEAT_MACHINE_SETTINGS)


def merge_folder_settings(existing: str | None) -> str | None:
    """The same, for the ``.vscode/settings.json`` of the folder about to open.

    The same set — see :data:`SEAT_FOLDER_SETTINGS` — and the same key-by-key
    merge, so a folder that already carries a user's excludes keeps every one of
    them.

    >>> json.loads(merge_folder_settings(None))["files.watcherExclude"]["**/proc/**"]
    True
    """
    return _merge_settings(existing, SEAT_FOLDER_SETTINGS)


def _merge_settings(existing: str | None, defaults: Mapping[str, Any]) -> str | None:
    if existing is None or not existing.strip():
        return machine_settings_text(copy.deepcopy(dict(defaults)))
    document: Any
    try:
        document = json.loads(existing)
    except ValueError as error:
        raise ValueError(f"cannot parse the existing settings.json: {error}") from error
    if not isinstance(document, dict):
        raise ValueError("the existing settings.json is not a JSON object")
    settings = cast("dict[str, Any]", document)

    changed = False
    for key, default in defaults.items():
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


def launch_setup_commands(
    *, source_dirs: Sequence[str] = (), debuginfod: bool = True
) -> list[str]:
    """The same, for a program gdb starts itself.

    Notably **no sysroot**: in a ``dev`` pod the program runs in this
    container's own mount namespace, so its libraries are already the ones gdb
    would find, and a sysroot pointing anywhere else would be actively wrong.
    Derived from :func:`podbench.gdbcmd.launch_commands` for the same
    single-definition reason as :func:`setup_commands`.

    >>> for command in launch_setup_commands():
    ...     print(command)
    set pagination off
    set debuginfod enabled on
    """
    return [
        command
        for command in launch_commands(
            "unused", source_dirs=source_dirs, debuginfod=debuginfod
        )
        if not command.startswith(("file ", "set args ", "run"))
    ]


def _name(action: str, target: str, flavour: Flavour) -> str:
    """Every configuration is named for its flavour.

    ``launch.json`` holds a list and VS Code's dropdown shows the names, so the
    flavour has to be *in* the name — two configurations called "podbench:
    attach to app" are a coin toss, and picking the wrong one produces a
    debugger that attaches and then shows nothing useful.

    >>> _name("attach to", "demo_service.py", Flavour.DEBUGPY)
    'podbench: attach to demo_service.py (debugpy)'
    """
    return f"podbench: {action} {target} ({flavour.value})"


def cppdbg_configuration(
    pid: int,
    program: str,
    *,
    exec_file: str | None = None,
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
    >>> config["name"]
    'podbench: attach to victim (gdb)'

    ``exec_file`` replaces that prefixed path where the seat has a binary of its
    own at ``program`` — gdb canonicalises the sysroot away and reads ours
    (issue #90), and cpptools sends ``program`` as ``-file-exec-and-symbols``
    before any ``setupCommands`` run, so no gdb command can undo it. The name
    still comes from ``program``, because "attach to victim" is what the reader
    is choosing between and the staged copy is an implementation detail.

    >>> cppdbg_configuration(597, "/app/victim", exec_file="/tmp/x/victim")["program"]
    '/tmp/x/victim'
    """
    configuration: dict[str, Any] = {
        "name": name or _name("attach to", Path(program).name, Flavour.GDB),
        "type": ADAPTER_CPPDBG,
        "request": "attach",
        # A string, which is what cpptools' own templates use; an int works
        # today but is not what the schema documents.
        "processId": str(pid),
        "program": exec_file or f"{sysroot_path(pid)}{program}",
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


def cppdbg_launch_configuration(
    program: str,
    *,
    name: str | None = None,
    cwd: str | None = None,
    source_dirs: Sequence[str] = (),
    debuginfod: bool = True,
    machine: str | None = None,
) -> dict[str, Any]:
    """The ``dev``-mode shape: gdb starts the program rather than attaching.

    A different configuration, not a flag on the previous one. ``PTRACE_TRACEME``
    from a child gdb forked itself needs no capability and satisfies Yama
    unconditionally, so this works where attach does not — report §3.12 is
    explicit that this is the inner loop to design for. Consequently there is no
    ``processId``, no sysroot and no mapping: the program is *here*.

    >>> config = cppdbg_launch_configuration("/workspace/victim")
    >>> config["request"], config["name"]
    ('launch', 'podbench: launch victim (gdb)')
    >>> "processId" in config or "sourceFileMap" in config
    False
    """
    configuration: dict[str, Any] = {
        "name": name or _name("launch", Path(program).name, Flavour.GDB),
        "type": ADAPTER_CPPDBG,
        "request": "launch",
        "program": program,
        "args": [],
        "cwd": cwd or SEAT_CWD,
        "MIMode": "gdb",
        "miDebuggerPath": GDB_WRAPPER,
        "setupCommands": [
            {"text": command}
            for command in launch_setup_commands(
                source_dirs=source_dirs, debuginfod=debuginfod
            )
        ],
    }
    architecture = target_architecture(machine)
    if architecture is not None:
        configuration["targetArchitecture"] = architecture
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
        "name": name or _name("attach to", Path(program).name, Flavour.LLDB),
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


def delve_configuration(
    pid: int,
    program: str,
    *,
    name: str | None = None,
    source_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The Go extension's local attach, which runs ``dlv`` here in the seat.

    ``substitutePath`` is delve's spelling of the same mode-dependent mapping
    every other adapter here needs, and its direction is the opposite of
    cpptools': ``from`` is the path the *editor* has.

    >>> config = delve_configuration(597, "/app/server")
    >>> config["type"], config["mode"], config["processId"]
    ('go', 'local', 597)
    """
    configuration: dict[str, Any] = {
        "name": name or _name("attach to", Path(program).name, Flavour.DELVE),
        "type": ADAPTER_DELVE,
        "request": "attach",
        "mode": "local",
        "processId": pid,
    }
    if source_map:
        configuration["substitutePath"] = [
            {"from": source, "to": destination}
            for source, destination in source_map.items()
        ]
    return configuration


def python_path_mappings(
    pid: int, source_root: str | None, mode: Mode
) -> list[dict[str, str]]:
    """The mapping debugpy needs, which is decided by the mode and nothing else.

    Getting this wrong does not error — breakpoints simply never bind — so it
    is derived from the detected mode rather than left to a flag.

    >>> python_path_mappings(1, "/src", Mode.OBSERVE)
    [{'localRoot': '/proc/1/root/src', 'remoteRoot': '/src'}]
    >>> python_path_mappings(1, "/src", Mode.DEV)
    []
    """
    if mode is Mode.DEV or not source_root:
        # Dev mode: editor and interpreter are the same container and the same
        # inodes, so any mapping at all is a spurious one.
        return []
    return [
        {"localRoot": f"{sysroot_path(pid)}{source_root}", "remoteRoot": source_root}
    ]


def debugpy_attach_configuration(
    pid: int,
    *,
    name: str,
    port: int = DEBUGPY_PORT,
    source_root: str | None = None,
    mode: Mode = Mode.OBSERVE,
) -> dict[str, Any]:
    """Connect to a debugpy server running inside the target's interpreter.

    ``connect``, never ``listen``: the server is in the app process (whether the
    app called ``debugpy.listen()`` itself or the injection put it there) and
    the editor is the client.

    ``justMyCode`` is false because the interesting frames in a pod are usually
    somebody else's — a framework's request handler, an ORM's session teardown.

    >>> config = debugpy_attach_configuration(1, name="x", source_root="/src")
    >>> config["connect"]
    {'host': '127.0.0.1', 'port': 5678}
    >>> config["pathMappings"]
    [{'localRoot': '/proc/1/root/src', 'remoteRoot': '/src'}]
    """
    return {
        "name": name,
        "type": ADAPTER_DEBUGPY,
        "request": "attach",
        "connect": {"host": DEBUGPY_HOST, "port": port},
        "justMyCode": False,
        "pathMappings": python_path_mappings(pid, source_root, mode),
    }


def debugpy_launch_configuration(
    program: str, *, name: str | None = None, cwd: str | None = None
) -> dict[str, Any]:
    """The ``dev``-mode shape for Python: the editor starts the interpreter.

    No ``pathMappings`` at all, and none is omitted by accident — see
    :func:`python_path_mappings`.

    >>> config = debugpy_launch_configuration("/workspace/src/app.py")
    >>> config["request"], config["name"]
    ('launch', 'podbench: launch app.py (debugpy)')
    """
    return {
        "name": name or _name("launch", Path(program).name, Flavour.DEBUGPY),
        "type": ADAPTER_DEBUGPY,
        "request": "launch",
        "program": program,
        "cwd": cwd or str(Path(program).parent),
        # The app's own stdout belongs somewhere the developer can see it; the
        # default `internalConsole` swallows anything not written by the adapter.
        "console": "integratedTerminal",
        "justMyCode": False,
    }


def configurations_for(
    flavour: Flavour,
    target: Target,
    mode: Mode,
    seat: Seat,
    *,
    port: int = DEBUGPY_PORT,
    source_dirs: Sequence[str] = (),
    source_map: Mapping[str, str] | None = None,
    debuginfod: bool = True,
    exec_file: str | None = None,
) -> list[dict[str, Any]]:
    """Every configuration one available flavour contributes, in emission order.

    A list rather than a single configuration because ``dev`` mode for Python
    genuinely has two answers — launch the app under the debugger, or connect to
    the one ``podbench run`` already started — and forcing a choice between them
    here would be the same exclusive guess this module exists to avoid.
    """
    program = target.program or ""
    if flavour is Flavour.GDB:
        if mode is Mode.DEV:
            return [
                cppdbg_launch_configuration(
                    program,
                    cwd=target.cwd,
                    source_dirs=source_dirs,
                    debuginfod=debuginfod,
                    machine=target.machine,
                )
            ]
        return [
            cppdbg_configuration(
                target.pid,
                program,
                exec_file=exec_file,
                source_dirs=source_dirs,
                source_map=source_map,
                debuginfod=debuginfod,
                machine=target.machine,
            )
        ]
    if flavour is Flavour.LLDB:
        return [lldb_configuration(target.pid, program, source_map=source_map)]
    if flavour is Flavour.DELVE:
        return [delve_configuration(target.pid, program, source_map=source_map)]
    return _debugpy_configurations(target, mode, seat, port=port)


def _debugpy_configurations(
    target: Target, mode: Mode, seat: Seat, *, port: int
) -> list[dict[str, Any]]:
    source_root = target.source_root
    connect = debugpy_attach_configuration(
        target.pid,
        name=_name("connect to", target.name, Flavour.DEBUGPY),
        port=port,
        source_root=source_root,
        mode=mode,
    )
    if mode is not Mode.DEV:
        return [
            debugpy_attach_configuration(
                target.pid,
                name=_name("attach to", target.name, Flavour.DEBUGPY),
                port=port,
                source_root=source_root,
                mode=mode,
            )
        ]
    if target.script:
        return [
            debugpy_launch_configuration(target.script, cwd=target.cwd),
            connect,
        ]
    # `python -m pkg` names a module rather than a file, so there is nothing to
    # put in `program`; connecting to the process `podbench run` started is the
    # only shape left.
    return [connect]


def launch_json_text(configurations: Sequence[Mapping[str, Any]]) -> str:
    """A whole ``launch.json`` document, newline-terminated."""
    document = {"version": _VERSION, "configurations": list(configurations)}
    return json.dumps(document, indent=2) + "\n"


def merge_launch_configs(
    existing: str | None, configurations: Sequence[Mapping[str, Any]]
) -> str:
    """Add or replace configurations by name, keeping every other one intact.

    Matched on ``name``, so re-running this verb updates its own entries rather
    than appending a second copy of each, and a hand-written configuration
    beside them survives untouched. That is also why every generated name
    carries its flavour: the match is the name, so two flavours must not share
    one.

    Raises ``ValueError`` when the existing file cannot be parsed. VS Code
    permits comments in ``launch.json`` and :mod:`json` does not, so refusing is
    the only safe answer — silently rewriting the file would discard the
    comments and any configuration this parser could not see.

    >>> print(merge_launch_configs(None, [{"name": "a"}]), end="")
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
        return launch_json_text(configurations)
    document: Any
    try:
        document = json.loads(existing)
    except ValueError as error:
        raise ValueError(f"cannot parse the existing launch.json: {error}") from error
    if not isinstance(document, dict):
        raise ValueError("the existing launch.json is not a JSON object")
    raw: Any = as_dict(document).get("configurations")
    entries = cast("list[Any]", raw) if isinstance(raw, list) else []
    names = {configuration.get("name") for configuration in configurations}
    merged = [
        as_dict(entry)
        for entry in entries
        if isinstance(entry, dict) and as_dict(entry).get("name") not in names
    ]
    merged.extend(dict(configuration) for configuration in configurations)
    return launch_json_text(merged)


def merge_launch_json(existing: str | None, configuration: Mapping[str, Any]) -> str:
    """:func:`merge_launch_configs` for a single configuration."""
    return merge_launch_configs(existing, [configuration])


def _warn(message: str) -> None:
    """Warnings go to stderr, so ``--print-config`` stays pasteable."""
    for line in message.splitlines():
        print(f"debug-config: {line}", file=sys.stderr)


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


_LOAD_FAILURES = (
    "Can't read symbols",
    "No such file or directory",
    "not in executable",
)
"""gdb's phrasings for "I could not load that program".

Matched on the text because ``gdb -batch`` **exits 0 whether the ``file``
command worked or not** — an exit code here would silently answer "readable" for
every target. On success gdb prints nothing at all, including for a
:term:`stripped` binary, so anything else is worth showing the user verbatim.
"""


def program_load_error(
    pid: int, program: str, *, runner: Runner | None = None
) -> str | None:
    """What gdb says when asked to load ``program``, or ``None`` if it is happy.

    Asked rather than inferred, because the two failures look identical from the
    editor and only one of them is fatal. A binary with no :term:`DWARF` loads
    silently and debugs fine; a binary whose :term:`symbol versioning` this
    seat's :term:`BFD` rejects cannot be opened, and cpptools turns that into
    ``Program path '<x>' is missing or invalid`` — pointing at the one thing that
    is not wrong.

    The sysroot goes in with ``-iex`` for report §3.3's reason, so that the
    separate debug file is looked for in the *target's* rootfs and not this
    seat's, and debuginfod is off: this is a question about the file in front of
    us, and a network round trip would make a local answer depend on egress.
    """
    run = runner if runner is not None else run_subprocess
    try:
        result = run(
            [
                GDB_WRAPPER,
                "-batch",
                "-iex",
                f"set sysroot {sysroot_path(pid)}",
                "-iex",
                "set debuginfod enabled off",
                "-ex",
                f"file {program}",
            ]
        )
    except OSError:
        # No gdb to ask. `assess` has its own, better-worded refusal for that,
        # so this must not become a second one saying the same thing.
        return None
    output = f"{result.stderr}\n{result.stdout}".strip()
    if not any(marker in output for marker in _LOAD_FAILURES):
        return None
    # Whole and verbatim: BFD's own line names the section it choked on, and a
    # paraphrase would cost exactly the detail that identifies the mechanism.
    return " / ".join(line.strip() for line in output.splitlines() if line.strip())


def _listening_debugpy(port: int, *, runner: Runner | None) -> int | None:
    """Whether something is already serving ``port`` in this pod.

    Reuses ``dev``'s ``ss`` parsing rather than growing a second one: the pod's
    network namespace is shared, so a listener anywhere in it is reachable from
    the seat at ``127.0.0.1`` — which is exactly what makes the "the app called
    ``debugpy.listen()`` itself" path work with no capability and on any
    architecture. An ``ss`` that cannot run answers ``None``, since a guess here
    would emit a configuration that connects to nothing.

    Imported here rather than at module scope because the three modules that
    hold VS Code, dev-pod and seat-preparation knowledge genuinely each need a
    little of the next: ``agent`` wants this module's machine settings, ``dev``
    wants ``agent``'s pubkey env var, and this function wants ``dev``'s ``ss``
    parsing — a cycle that only appears once all three are present. This is the
    narrowest of the three edges, a single call in a single function, so it is
    the one that gives way.
    """
    from .dev import LISTENERS_COMMAND, listeners_on, parse_ss

    run = runner if runner is not None else run_subprocess
    try:
        result = run(list(LISTENERS_COMMAND))
    except OSError:
        # "cannot run" includes "is not installed", which is not a non-zero exit
        # but an exception — and the caller is mid-way through authoring a
        # configuration, so it must not be one here.
        return None
    if result.returncode != 0:
        return None
    return port if listeners_on(parse_ss(result.stdout), port) else None


def measured_attach(
    target: Target,
    mode: Mode,
    seat: Seat,
    *,
    proc: Path,
    attacher: Attacher | None,
    probe: bool = True,
) -> bool | None:
    """Whether this seat can really ptrace ``target``, or ``None`` for unasked.

    The pid injection is ``gdb --pid``, so what the debugpy flavour needs to
    know is whether ptrace is permitted — and CAP_SYS_PTRACE is one of four ways
    to be permitted it. A same-uid attach under ``ptrace_scope=0`` needs no
    capability at all, and reading the bit instead of asking is what refused a
    Diamond seat the injection that ``capreport`` had measured working, from the
    same seat, against the same pid, in the same minute (issue #89).

    So it is asked, with the probe ``capreport`` uses — a ``PTRACE_SEIZE``,
    which answers the same permission question without stopping the workload.
    It stays narrowed to the case that would otherwise be refused wrongly (a
    Python target, in attach mode, with nothing already listening, in a seat
    whose capability bit is clear) because a measurement that costs nothing is
    still an answer nobody asked for anywhere else.

    ``probe=False`` is ``--print-config``: printing what *would* be written must
    not touch the workload, however cheap touching it has become.

    A synthetic ``/proc`` is never probed. Its pids name unrelated processes on
    whatever machine is running, so an attach there would measure something
    else entirely — and the unit tests that inject one must keep answering from
    the capability mask they wrote into it.
    """
    if proc != DEFAULT_PROC:
        return None
    if mode is Mode.DEV or target.language is not Language.PYTHON:
        # Neither consults the answer: dev mode debugs a child of this
        # container, and a non-Python target has no injection to drive.
        return None
    if seat.listening_port is not None or seat.cap_sys_ptrace:
        # Nothing would be refused, so there is nothing worth measuring.
        return None
    if not probe:
        # Said only here, where the answer would otherwise have differed: every
        # return above is `None` whether or not a probe was allowed, so warning
        # earlier would be a paragraph about nothing on most runs.
        _warn(
            f"--print-config touches nothing, so ptrace to pid {target.pid} was "
            "not measured and the debugpy injection is judged on CapEff alone. "
            "Re-run `podbench debug-config` without --print-config, or "
            "`podbench capreport`, to measure it"
        )
        return None
    outcome = (default_attacher() if attacher is None else attacher).attach(target.pid)
    # Notes travel with the outcome because they are about what the attach *did*
    # — a detach that failed leaves the workload stopped, and that has to be
    # said whatever the verdict was.
    for note in outcome.notes:
        _warn(note)
    if outcome.ok:
        _warn(
            f"measured ptrace to pid {target.pid} rather than reading CapEff: "
            f"{outcome.method or 'the ptrace probe'} succeeded, so the injection "
            "is offered on the measurement and not refused on the capability "
            f"bit. Pause to the workload: {describe_pause(outcome.method, True)}"
        )
    return outcome.measured_ok


def _inject(
    target: Target,
    mode: Mode,
    seat: Seat,
    *,
    port: int,
    runner: Runner | None,
) -> bool:
    """Start the debugpy server inside the target, under ``--provision``.

    :func:`podbench.flavour.injection_command`'s docstring says this is printed
    rather than run because ptracing the workload "is not something authoring a
    launch.json may do on its own" — and that is still true of a bare
    ``debug-config``. ``--provision`` is the flag that revokes it: issue #45
    ordered the two mutations and put *installing* above *injecting*, so a run
    that has already been told it may write 15 MB into the workload has been
    told it may do the smaller thing too. Asking twice for the lesser of them
    left the configuration emitted and the port closed, which is a launch.json
    that connects to nothing.

    Gated on the verdict rather than on the install's return value, so the
    reason a refusal gives is the same sentence :func:`assess` would have given.
    """
    if mode is Mode.DEV or target.language is not Language.PYTHON:
        # Both already named by `_provision`, which refuses first for the same
        # two reasons; saying it twice would read as two separate problems.
        return False
    if seat.listening_port is not None:
        # Not a no-op worth reporting: `_provision` has just said the same
        # thing, and the emitted configuration connects to it either way.
        return False
    verdict = next(
        (
            item
            for item in assess(target, mode, seat)
            if item.flavour is Flavour.DEBUGPY
        ),
        None,
    )
    if verdict is None or not verdict.available:
        _warn(
            "--provision: not starting the server - "
            + (verdict.reason if verdict else "debugpy was not assessed")
        )
        return False
    command = injection_command(target, seat, port)
    _warn(
        f"--provision: starting the server inside the app. This ptraces the "
        f"workload, so it stops answering probes until the attach returns; on "
        f"a probed pod the deadlines `attach` prints are the budget it spends. "
        f"Running `{command}`"
    )
    injected = inject_debugpy(command, runner=runner, port=port)
    for message in injected.messages:
        _warn(f"--provision: {message}")
    return injected.ok


def _provision(
    target: Target,
    mode: Mode,
    seat: Seat,
    *,
    dest: str,
    python_version: str | None,
    proc: Path,
    runner: Runner | None,
    which: Which,
) -> bool:
    """Install debugpy into the target, or say which mechanism stopped it.

    Opt-in, and for a stronger reason than :func:`injection_command` is only
    printed: this writes ~15 MB into the *workload's* writable layer, against an
    ephemeral-storage budget an ephemeral container may not reserve (report
    3.9), and a verb that authors a configuration file must stay safe to re-run.

    Every refusal below names its mechanism, in the same house style as the
    verdicts: a config author that quietly does nothing when it was asked to do
    something is the silent wrong answer this module exists to prevent, one
    layer up from where it usually appears.
    """
    if mode is Mode.DEV:
        _warn(
            "--provision: dev mode debugs a process in this container, where "
            "debugpy is an ordinary workspace dependency - there is no other "
            "container's rootfs to install into"
        )
        return False
    if target.language is not Language.PYTHON:
        _warn(
            f"--provision: the target is {target.language.value} and debugpy "
            "debugs CPython, so there is nothing to install"
        )
        return False
    if seat.listening_port is not None:
        _warn(
            "--provision: a debugpy server is already listening on "
            f"{DEBUGPY_HOST}:{seat.listening_port}, and the emitted "
            "configuration connects to it without any of this"
        )
        return False
    machine = target.machine or seat.machine
    if not debugpy_helper_published(machine):
        # 15 MB into the workload that could never help: debugpy publishes no
        # aarch64 Linux wheel, so there is no helper to dlopen on any path.
        _warn(
            f"--provision: debugpy publishes no {machine} attach helper, so "
            "installing one cannot make pid-injection work here - bake "
            "`debugpy.listen()` into the app image, or use `podbench dev`"
        )
        return False
    if seat.debugpy_there == target_destination(target.pid, dest):
        # This flag's own destination, so it is installed over rather than kept.
        # Nothing in an installed tree records the X.Y uv resolved it for, and
        # `_target_debugpy` checks this path first - so a copy made for the wrong
        # version imports fine, shadows the target's real one, and drops pydevd
        # to pure Python silently, which is the failure the whole module is
        # shaped around. Refusing here would leave no way to correct it.
        _warn(
            f"--provision: {seat.debugpy_there} is this flag's own destination, "
            "so it is installed over rather than kept - re-running is the only "
            "way to correct a copy resolved for the wrong X.Y, and the tree "
            "records no version to check it against"
        )
    elif seat.debugpy_there is not None and seat.debugpy_helper:
        _warn(
            f"--provision: the target can already import debugpy from "
            f"{seat.debugpy_there}, so nothing is installed"
        )
        return False
    elif seat.debugpy_there is not None:
        # Importable but incomplete, which is the one case worth writing over:
        # the injection would get as far as the dlopen and then fail on the
        # helper the target's own copy does not have.
        _warn(
            f"--provision: the target's debugpy at {seat.debugpy_there} has no "
            f"{debugpy_helper_name(machine)}, so a complete copy goes in beside it"
        )
    if which("uv") is None:
        _warn(
            "--provision: no uv on PATH in this seat, and it is uv's "
            "--python-version that resolves a wheel for the *target's* "
            "interpreter rather than this one"
        )
        return False
    version = python_version or target.python_version
    if version is None:
        _warn(
            "--provision: could not read the target's X.Y from its exe, its "
            "command line or its rootfs, and installing for the wrong one "
            "leaves pydevd on its pure-Python fallback with nothing said - "
            "pass --provision-python X.Y (`python -V` in the target names it)"
        )
        return False
    # Asked of the same predicate `assess` uses, not of the bit: this sentence
    # and the refusal it prepares the reader for are two spellings of one fact,
    # and a seat that measured an attach reads "the injection cannot be driven
    # from here" immediately before the injection is driven from here (#89).
    if not can_ptrace_target(seat):
        # Said, not refused - unlike the arm64 gate above. The tree lands in the
        # *target's* rootfs, which outlives this seat, so provisioning now and
        # relaunching on the `full` rung still works; what would be wrong is
        # letting the install land and then reading "CAP_SYS_PTRACE is not in
        # this seat's effective set" two lines later with nothing joining them.
        _warn(
            "--provision: this seat cannot ptrace the target, so the injection "
            "cannot be driven from here whatever gets installed - the copy goes "
            "into the target's own rootfs and outlives this seat, so a relaunch "
            "on the `full` rung picks it up rather than repeating the install"
        )
    # Announced before it runs: uv's output is captured for the failure message,
    # so a resolve against an index with no route is several silent seconds
    # (bounded by uv's own HTTP timeout, not by anything here) that would
    # otherwise be indistinguishable from a hang.
    _warn(
        "--provision: running `"
        f"{provision_paste(target.pid, dest=dest, python_version=version)}`"
    )
    result = provision_debugpy(
        target.pid,
        python_version=version,
        dest=dest,
        proc=proc,
        runner=runner,
    )
    for message in result.messages:
        _warn(f"--provision: {message}")
    return result.ok


def _emit(
    assessments: Sequence[Assessment],
    wanted: set[Flavour],
    explicit: bool,
    target: Target,
    mode: Mode,
    seat: Seat,
    *,
    port: int,
    source_dirs: Sequence[str],
    source_map: Mapping[str, str],
    debuginfod: bool,
    exec_file: str | None = None,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """Turn the verdicts into configurations, and the refusals into sentences.

    ``verbose`` is false for a candidate the user did not ask about — the second
    and third process in the container. Their refusals are the *same* refusals as
    the target's nine times in ten (one seat, one set of capabilities), and
    repeating three paragraphs of them buries the configurations that did come
    out.
    """
    configurations: list[dict[str, Any]] = []
    for assessment in assessments:
        if assessment.flavour not in wanted:
            continue
        if assessment.available:
            # The reason is printed for an emitted flavour too, not only for a
            # refused one: "why is there a debugpy entry here" and "why is there
            # not" are the same question, and answering only half of it leaves
            # the reader to guess which of several paths produced the file.
            _warn(f"emitting {assessment.flavour.value}: {assessment.reason}")
            configurations.extend(
                configurations_for(
                    assessment.flavour,
                    target,
                    mode,
                    seat,
                    port=port,
                    source_dirs=source_dirs,
                    source_map=source_map,
                    debuginfod=debuginfod,
                    exec_file=exec_file,
                )
            )
            continue
        # A flavour ruled out purely by language is noise unless it was asked
        # for by name — nobody debugging a C binary needs to be told that delve
        # is for Go. Everything else is a mechanism the user could act on.
        if (explicit or not assessment.language_mismatch) and verbose:
            _warn(assessment.message())
    return configurations


def _run(
    pid: int | None,
    container_id: str | None,
    *,
    program: str | None,
    source_dirs: Sequence[str],
    source_map_entries: Sequence[str],
    debuginfod: bool,
    flavours: Sequence[Flavour],
    lldb: bool,
    mode: Mode | None,
    port: int,
    print_config: bool,
    output: str | None,
    provision: bool,
    provision_dest: str,
    provision_python: str | None,
    proc: Path,
    runner: Runner | None,
    attacher: Attacher | None,
    which: Which,
    debugpy_root: str | None,
) -> int:
    source_map, problems = _parse_source_map(source_map_entries)
    for problem in problems:
        _warn(problem)
    if problems:
        return EXIT_USAGE

    pids, notes = resolve_target_pids(pid, container_id, proc=proc)
    for note in notes:
        _warn(note)
    if not pids:
        return EXIT_USAGE
    explicit_pid = pid is not None

    requested = list(flavours) + ([Flavour.LLDB] if lldb else [])
    explicit = bool(requested)
    wanted = set(requested) if explicit else set(Flavour)

    configurations: list[dict[str, Any]] = []
    # Every candidate gets a configuration, not just the best one. An entrypoint
    # script's children are usually two *different* languages — an IOC binary
    # under gdb beside the Python that supervises it — so the flavours do not
    # compete for one slot, and launch.json holds a list precisely so the choice
    # can be made at F5 rather than guessed here (issue #92). The best candidate
    # goes first, and is the only one --provision may touch.
    for index, candidate in enumerate(pids):
        primary = index == 0
        if not primary:
            _warn(f"also emitting for pid {candidate}")
        configurations.extend(
            _for_target(
                candidate,
                program=program if explicit_pid else None,
                mode=mode,
                wanted=wanted,
                explicit=explicit,
                # A refusal is worth a paragraph for the target the user is
                # about to debug and worth a line for the alternatives, which
                # they did not ask about and may not want.
                verbose=primary or explicit_pid,
                port=port,
                source_dirs=source_dirs,
                source_map=source_map,
                debuginfod=debuginfod,
                provision=provision and primary,
                provision_dest=provision_dest,
                provision_python=provision_python,
                proc=proc,
                runner=runner,
                attacher=attacher,
                which=which,
                debugpy_root=debugpy_root,
                hint=primary,
                # A verb that prints rather than writes touches nothing, and the
                # probe is per candidate: N candidates were N real attaches on
                # the workload for a run that changes no file.
                probe=not print_config,
            )
        )

    if not configurations:
        _warn(
            "no debugger flavour could be emitted for this target — every "
            "mechanism that said no is named above"
        )
        return EXIT_USAGE

    if print_config:
        print(launch_json_text(configurations), end="")
        return 0
    return _write(configurations, output)


def _for_target(
    pid: int,
    *,
    program: str | None,
    mode: Mode | None,
    wanted: set[Flavour],
    explicit: bool,
    verbose: bool,
    port: int,
    source_dirs: Sequence[str],
    source_map: Mapping[str, str],
    debuginfod: bool,
    provision: bool,
    provision_dest: str,
    provision_python: str | None,
    proc: Path,
    runner: Runner | None,
    attacher: Attacher | None,
    which: Which,
    debugpy_root: str | None,
    hint: bool,
    probe: bool = True,
) -> list[dict[str, Any]]:
    """Everything one candidate pid contributes to ``launch.json``."""
    target = inspect_target(pid, proc=proc, program=program)
    for note in target.notes:
        _warn(note)
    mode = mode or detect_mode(pid, proc=proc)
    # Bound to its own name for the closure below: a captured variable keeps its
    # declared type, so `mode` reads as `Mode | None` inside `measure()` however
    # it was narrowed out here.
    target_mode = mode
    # The path cpptools will put in `program`, and so the one to ask gdb about.
    # Not `sysroot_program`, though the sysroot prefix is where it starts:
    # unprefixed it names *this* image's binary of the same name, which is the
    # substitution report §3.3 measured. In dev mode the program is here, so
    # there is no prefix to add.
    #
    # The sysroot prefix is not enough on its own. gdb canonicalises the exec
    # file's name and `/proc/<pid>/root` canonicalises to `/`, so where this
    # seat keeps a file at the target's path it reads ours anyway (issue #90) —
    # and cpptools sends `program` as `-file-exec-and-symbols` before any
    # setupCommands run, so nothing in the configuration can undo it. A copy is
    # staged instead, and the same path is used for both the question below and
    # the configuration: asking gdb about one file and handing cpptools another
    # is how a seat ends up refusing a target it can debug.
    #
    # Staged for the languages that reach gdb only. A Python target's cppdbg
    # entry is withdrawn on language (`flavour._assess_gdb`), so a copy per
    # candidate pid would spend the pod's ephemeral storage (report 3.9) on a
    # configuration that is never written.
    gdb_program: str | None = None
    if target.program:
        if mode is Mode.DEV:
            gdb_program = target.program
        elif target.language in (Language.NATIVE, Language.UNKNOWN):
            gdb_program, exec_file_notes = gdb_exec_file(pid, target.program, proc=proc)
            for note in exec_file_notes:
                _warn(note)
        else:
            gdb_program = f"{sysroot_path(pid)}{target.program}"

    # Asked at most once, and reused when the seat is re-measured after an
    # install: whether this seat may ptrace the target is not a question
    # installing a wheel can change. The latch is per candidate pid, so this is
    # also the only thing between a five-candidate pod and five probes.
    attach_ok: bool | None = None
    attach_asked = False

    def measure() -> Seat:
        nonlocal attach_ok, attach_asked
        # The listener is re-probed on every measurement rather than read once:
        # --provision can *start* one part way through this function, and a
        # port sampled before that would emit a configuration whose "already
        # listening" answer is stale in the one run that changed it.
        listening = (
            _listening_debugpy(port, runner=runner)
            if target.language is Language.PYTHON
            else None
        )
        # Native and unknown targets only, and still, though the debugpy pid
        # injection now withdraws on the same answer — it drives gdb to `call
        # (void*)dlopen(...)`, which needs the symbols this asks about. What gdb
        # says about a python-build-standalone interpreter is issue #90's open
        # question, and measuring it here would settle that issue by accident,
        # in a verb that otherwise only authors a file, at the price of a gdb
        # start-up per candidate. #90 is where the Python case joins.
        load_error = (
            program_load_error(target.pid, gdb_program, runner=runner)
            if gdb_program and target.language in (Language.NATIVE, Language.UNKNOWN)
            else None
        )
        surveyed = survey_seat(
            target,
            proc=proc,
            which=which,
            debugpy_root=debugpy_root,
            listening_port=listening,
            provision_dest=provision_dest,
            program_load_error=load_error,
        )
        if not attach_asked:
            attach_asked = True
            attach_ok = measured_attach(
                target,
                target_mode,
                surveyed,
                proc=proc,
                attacher=attacher,
                probe=probe,
            )
        return replace(surveyed, target_attach_ok=attach_ok)

    seat = measure()
    if provision:
        if _provision(
            target,
            mode,
            seat,
            dest=provision_dest,
            python_version=provision_python,
            proc=proc,
            runner=runner,
            which=which,
        ):
            # Measured again rather than assumed: whether the target can import
            # what was just written is the prerequisite itself, and
            # `_target_debugpy` is the only thing that answers it.
            seat = measure()
        # Not chained onto the install: a target that could already import
        # debugpy takes the "nothing is installed" path above and still has
        # nothing listening, which is the case this whole flag exists to end.
        if _inject(target, mode, seat, port=port, runner=runner):
            seat = measure()
    assessments = assess(target, mode, seat)

    _warn(
        f"pid {pid} ({target.name}): {target.language.value} target, "
        f"{mode.value} mode" + (f", {target.machine}" if target.machine else "")
    )
    configurations = _emit(
        assessments,
        wanted,
        explicit,
        target,
        mode,
        seat,
        port=port,
        source_dirs=source_dirs,
        source_map=source_map,
        debuginfod=debuginfod,
        exec_file=gdb_program if mode is not Mode.DEV else None,
        verbose=verbose,
    )
    if not configurations and not verbose:
        # The quiet path still has to account for itself: a candidate that was
        # announced and then contributed nothing reads as a bug in the emitter.
        refused = next(
            (a for a in assessments if not a.available and not a.language_mismatch),
            None,
        )
        _warn(
            f"pid {pid} ({target.name}): nothing emitted"
            + (f" — {refused.reason}" if refused else "")
        )
    if hint and configurations:
        _hint(target, mode, seat, assessments, wanted, port=port)
    return configurations


def _write(configurations: Sequence[Mapping[str, Any]], output: str | None) -> int:
    path = Path(output) if output else Path.cwd() / ".vscode" / "launch.json"
    existing = path.read_text() if path.exists() else None
    try:
        text = merge_launch_configs(existing, configurations)
    except ValueError as error:
        _warn(
            f"{error}. Re-run with --print-config and paste the configuration "
            "in by hand, or --output a different path."
        )
        return EXIT_USAGE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"debug-config written to {path}")
    for configuration in configurations:
        print(f'  Run and Debug -> "{configuration["name"]}"')
    if path.parent.parent != Path.cwd():
        # VS Code reads .vscode/launch.json from the folder that is *open*, not
        # from $HOME, which is the commonest reason a written config never shows
        # up in the Run and Debug list.
        print(
            f"note: VS Code only sees this if {path.parent.parent} is the open "
            "folder; use --output to write it beside the folder you opened"
        )
    return 0


def _hint(
    target: Target,
    mode: Mode,
    seat: Seat,
    assessments: Sequence[Assessment],
    wanted: set[Flavour],
    *,
    port: int,
) -> None:
    """Say what still has to happen before the debugpy entry can connect.

    The configuration is emitted as soon as the *prerequisites* are met, but
    nothing is listening until the injection is run — and running it is not
    something authoring a launch.json may do on its own, since it ptraces the
    workload and leaves a server inside it.
    """
    if Flavour.DEBUGPY not in wanted or mode is Mode.DEV:
        return
    debugpy = next(
        (item for item in assessments if item.flavour is Flavour.DEBUGPY), None
    )
    if debugpy is None or not debugpy.available or seat.listening_port is not None:
        return
    print(
        f"debug-config: nothing is listening on {DEBUGPY_HOST}:{port} yet. "
        "Start the server inside the app with:\n"
        f"{injection_command(target, seat, port)}",
        file=sys.stderr,
    )


def main(
    args: Sequence[str] | None = None,
    *,
    proc: Path = DEFAULT_PROC,
    runner: Runner | None = None,
    attacher: Attacher | None = None,
    which: Which = shutil.which,
    debugpy_root: str | None = None,
) -> int:
    """``podbench debug-config`` — author ``launch.json`` for this seat.

    ``proc``, ``runner``, ``attacher``, ``which`` and ``debugpy_root`` are test
    seams; the CLI passes none of them. ``which`` in particular: whether this
    image ships gdb decides whether a gdb configuration is emitted at all, and a
    unit test must not answer that question from the machine it happens to run
    on. ``attacher`` is the ptrace backend :func:`measured_attach` uses, and a
    test that wants an answer out of it has to inject one *and* a real ``proc``:
    against a synthetic tree nothing is attached at all.
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
        flavour: Annotated[
            list[Flavour] | None,
            typer.Option(
                "--flavour",
                help="emit only this debugger flavour, and say why if it cannot "
                "be emitted. Repeatable; the default is every flavour that "
                "applies",
            ),
        ] = None,
        mode: Annotated[
            Mode | None,
            typer.Option(
                "--mode",
                help="override the detected mode. Observe attaches to another "
                "container and needs path mappings; dev launches in this one "
                "and must not have any",
            ),
        ] = None,
        port: Annotated[
            int,
            typer.Option(
                "--port",
                metavar="PORT",
                help="the debugpy port to connect to (shared network namespace, "
                "so always 127.0.0.1)",
            ),
        ] = DEBUGPY_PORT,
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
                help="do not enable debuginfod (it needs ca-certificates and "
                "network). Library symbols are fetched after the attach, with "
                "the target stopped, so this is the flag to reach for when the "
                "pause is what costs",
            ),
        ] = False,
        lldb: Annotated[
            bool,
            typer.Option(
                "--lldb",
                help="shorthand for --flavour lldb",
            ),
        ] = False,
        provision: Annotated[
            bool,
            typer.Option(
                "--provision",
                help="make the target debuggable: install debugpy with uv when "
                "it cannot import one, then start the server inside it so the "
                "emitted configuration has something to connect to. Mutates "
                "the workload: ~15 MB of shared ephemeral storage, needs egress "
                "from the pod, ptraces the app for a few seconds, and no "
                "restart survives it",
            ),
        ] = False,
        provision_dest: Annotated[
            str,
            typer.Option(
                "--provision-dest",
                metavar="PATH",
                help="where --provision installs it, as the *target* spells it, "
                "and the one extra path searched for the target's copy. Point "
                "it at a writable mount when the target's rootfs is read-only",
            ),
        ] = PROVISION_DEST,
        provision_python: Annotated[
            str | None,
            typer.Option(
                "--provision-python",
                metavar="X.Y",
                help="the target's Python version for uv to resolve against, "
                "when it cannot be read from the target itself",
            ),
        ] = None,
        print_config: Annotated[
            bool,
            typer.Option(
                "--print-config",
                help="print the configuration instead of writing it, and "
                "measure nothing: this run touches no workload",
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
                flavours=flavour or (),
                lldb=lldb,
                mode=mode,
                port=port,
                print_config=print_config,
                output=output,
                provision=provision,
                provision_dest=provision_dest,
                provision_python=provision_python,
                proc=proc,
                runner=runner,
                attacher=attacher,
                which=which,
                debugpy_root=debugpy_root,
            )
        )

    # See the note on ``prog`` in probe.py: the usage line names the only
    # spelling there is.
    return run(app, args, prog="podbench debug-config")
