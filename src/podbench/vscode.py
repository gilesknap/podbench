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
  result is that same unformattable crash. It is
  :func:`podbench.proc.seat_cwd` — the seat's own ``$HOME``, measured here
  because this verb runs in the seat — and not a constant: ``/root`` is the
  home of a *root* seat only, and naming it on a uid-pinned rung emitted a
  directory that seat cannot enter.

The ordering inside ``setupCommands`` is not this module's invention: it is
:func:`podbench.gdbcmd.attach_commands` with the two lines cpptools issues
itself removed, so the sequence report 3.3 made load-bearing cannot drift
between the CLI path and the DAP path.

And ``pathMappings`` is **mode-dependent**, which is the one field here that
fails without any error at all. In Observe mode the target is another
container, so the editor sees the source through ``/proc/<pid>/root`` while the
debuggee reports its own path: a mapping is required, and it maps the *mount
namespace* — ``/proc/<pid>/root`` to ``/`` — rather than a root guessed from
``argv``. In a ``dev`` pod editor and interpreter are the same container and
the same inodes, so the mapping must be **empty**. Both spellings are emitted
from the detected mode rather than from a flag, because a user cannot be
expected to know which side of that line they are on.

:data:`SEAT_MACHINE_SETTINGS` is the other half, and the one that has to be in
place *before* the user does anything: File -> Open Folder -> ``/`` is the
obvious first move in a seat and it can OOM the container unrecoverably. See
that constant for why each entry is there. :func:`podbench.agent.ensure_vscode_settings`
installs it at seat start-up, because the file lives in a directory the client
creates; :func:`podbench.editor.open_seat` merges into the same file on every
``podbench vscode`` run, which is where :data:`PYTHON_INTERPRETER_KEY` joins it
— only a run that has measured the *target* knows which interpreter is being
debugged, and the seat's start-up cannot.

Machine scope and **no folder copy**, which is D1b (2026-08-24). The folder
``podbench vscode`` opens on a hotfixed pod is the user's committed checkout on
an NFS PVC, so every file podbench authors there is a permanent line in
somebody's git diff; ``launch.json`` earns that and a duplicate exclude list
does not. The cost is stated here rather than left to be discovered:
*Kill/Uninstall VS Code Server on Host* deletes ``~/.vscode-server`` wholesale,
so that action now takes podbench's excludes with it — including
``**/proc/**``, which is the one that stops the recursive walk that OOMs an
unrestartable seat. Two things cover that: the agent writes them at start-up,
and a re-run of ``podbench vscode`` restores them on a reconnect.
What is left uncovered is real — a window kept open through a Kill Server and
reconnected by VS Code itself, rather than by podbench, opens a folder with no
excludes.
"""

from __future__ import annotations

import copy
import json
import platform
import shutil
import socket
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

import typer

from .cli import new_app, run
from .dap import initialize as dap_initialize
from .elf import debugpy_helper_name, debugpy_helper_published
from .execfile import gdb_exec_file
from .flavour import (
    DEBUGPY_PORT,
    NATIVE_LANGUAGES,
    Assessment,
    Flavour,
    Language,
    Mode,
    PtraceEvidence,
    Seat,
    Target,
    Which,
    assess,
    can_ptrace_target,
    detect_mode,
    injection_command,
    inspect_target,
    ptrace_evidence,
    survey_seat,
)
from .gdbcmd import (
    EXIT_USAGE,
    attach_commands,
    launch_commands,
    resolve_target_pids,
    sysroot_path,
)
from .jsonc import (
    Edit,
    Node,
    append_items,
    apply_edits,
    insert_members,
    parse,
    replace_value,
)
from .kubectl import Runner, run_subprocess
from .model import describe_pause
from .probe import Attacher, default_attacher
from .proc import (
    DEFAULT_PROC,
    DebugpyAdapter,
    debugpy_adapters,
    env_host_network,
    env_target_container_id,
    list_processes,
    seat_cwd,
    strip_container_scheme,
)
from .provision import (
    PROVISION_DEST,
    Injected,
    Prover,
    inject_debugpy,
    provision_debugpy,
    provision_paste,
    target_destination,
)

if TYPE_CHECKING:
    # Type-only: the runtime import lives inside `_listening_debugpy`, where its
    # docstring explains which of the three module cycles here gives way.
    from .dev import PortOwner

__all__ = [
    "ADAPTER_CPPDBG",
    "ADAPTER_DEBUGPY",
    "ADAPTER_DELVE",
    "ADAPTER_LLDB",
    "EXTENSIONS",
    "GDB_WRAPPER",
    "INTERPRETER_NOTE",
    "MACHINE_SETTINGS_PATH",
    "PYTHON_INTERPRETER_KEY",
    "SEAT_MACHINE_SETTINGS",
    "WITHHELD_NOTE",
    "ListeningServer",
    "configurations_for",
    "cppdbg_configuration",
    "cppdbg_launch_configuration",
    "debugpy_attach_configuration",
    "debugpy_launch_configuration",
    "delve_configuration",
    "ephemeral_port",
    "extensions_for",
    "launch_json_text",
    "launch_setup_commands",
    "lldb_configuration",
    "machine_settings_text",
    "main",
    "measured_attach",
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

ADAPTER_CPPDBG = "cppdbg"
ADAPTER_LLDB = "lldb"
ADAPTER_DEBUGPY = "debugpy"
ADAPTER_DELVE = "go"
"""The Go extension's adapter type, which is ``go`` and not ``delve``: the
flavour is named after the debugger, the ``type`` after the extension."""

DEBUGPY_HOST = "127.0.0.1"
"""Right in both modes, and worth stating because it looks wrong in Observe
mode: the seat and the app are separate containers but share the pod's network
namespace, so no port-forward and no tunnel is involved.

Under ``hostNetwork: true`` that namespace is the **node's**, and this address
stops meaning "inside this pod". Loopback is still the right bind — nothing off
the node can reach it — but every other hostNetwork pod and every node daemon
shares it, in both directions: a port found here may be a stranger's, and a
server started here is reachable by all of them. See
:data:`~podbench.model.HOST_NETWORK_ENV` and issue #87.
"""

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


PYTHON_INTERPRETER_KEY = "python.defaultInterpreterPath"
"""The setting that answers the Python extension's "no interpreter found" popup.

Issue #219: the window raises it on a pod where debug attach then works
perfectly, because nothing had ever told the extension which interpreter the
seat is looking at. podbench has measured the answer one line earlier — it is
narrated beside the debugpy configuration — and simply dropped it.

In the **machine** file and never in the folder's, because ms-python declares
this key ``machine-overridable``: a machine value answers the popup for every
folder the seat opens, and a value the user sets in their own workspace still
wins over it. That is exactly what #219 asks for, and it is the reason the key
can be written at all without overriding somebody's choice.

Never the *seat's* own interpreter. What the extension is being told is which
interpreter is being **debugged**, and on a pod without the hotfix layout that
file is in another mount namespace: the seat's copy at the same path is a
different file — the collision :func:`python_path_mappings` and issue #90 are
both shaped around — so naming it is a confident wrong answer where the popup
was an annoying right one. :data:`INTERPRETER_NOTE` carries the measurement and
:func:`podbench.editor.open_seat` decides, because only the laptop side knows
whether the seat and the target share the tree the path is in.
"""

INTERPRETER_NOTE = "the target's interpreter is "
"""How the measured interpreter reaches the laptop, which is over **stderr**.

``--print-config`` prints a launch document on stdout and it has to stay
pasteable byte for byte (the ``terminal-reports`` skill), so a second top-level
key cannot travel there: pasted into a ``launch.json`` it draws a schema warning
on the one file this verb exists to hand over. The precedent is
:data:`podbench.editor.PROVISION_FLAG`, which the launcher already reads out of
this same narration — this is that wire carrying a value instead of a request.

Said for every Python target rather than only for a hotfixed one, because it is
a true and useful sentence either way and the *decision* it feeds is not the
seat's to take: whether the path means anything on the other side depends on
whether the tree holding it is shared with the seat, which is a fact about the
pod's mounts (:func:`podbench.editor.interpreter_for_folder`).
"""

WITHHELD_NOTE = "so no debugpy configuration is written"
"""How "the handshake refused, keep the file you have" reaches the laptop.

The third thing this stderr carries, after
:data:`podbench.editor.PROVISION_FLAG` and :data:`INTERPRETER_NOTE`, and it is
read for the same reason the other two are:
an empty ``configurations`` list means "nothing fits this target" and "the
configuration that fits it could not be proved" alike, and only the second of
those must stop :func:`podbench.editor._configurations` falling back to an
earlier run's answer. That fallback is right for an injection that failed on
egress and wrong here, because the answer it would fall back to is a port from
a run that never started a server either - #218 arriving one layer up.
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
    language the target does not use spends the workload's disk on nothing.

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


def machine_settings_text(settings: Mapping[str, Any]) -> str:
    """A whole machine ``settings.json`` document, newline-terminated."""
    return json.dumps(dict(settings), indent=2) + "\n"


def _document(existing: str, name: str) -> Node:
    """The JSONC document in ``existing``, which has to be an object.

    Both refusals keep the wording they had when these files were read with
    :func:`json.loads`, because the one that survives — text that is not JSONC
    either — is still reported to the user verbatim, ``line … column …`` and all.
    """
    try:
        document = parse(existing)
    except ValueError as error:
        raise ValueError(f"cannot parse the existing {name}: {error}") from error
    if document.members is None:
        raise ValueError(f"the existing {name} is not a JSON object")
    return document


def merge_machine_settings(
    existing: str | None, *, interpreter: str | None = None
) -> str | None:
    """Add :data:`SEAT_MACHINE_SETTINGS` to a settings document, clobbering none
    of it. ``None`` means the document already says everything we would say.

    Merged per *key* rather than written only when the file is absent, and the
    difference is not cosmetic: the file-level rule would mean a user who set
    one unrelated setting — a font size, a theme — silently loses every exclude,
    and the failure that costs is an unrecoverable seat. Merged per key, an
    existing value always wins, including a deliberate ``"**/proc/**": false``,
    and a pattern we never heard of is left where it is.

    The document is read as JSONC and edited *textually* — see :mod:`podbench.jsonc`
    — because a settings.json with comments and a trailing comma is what a real
    project ships, and a merge that reformatted it would be a worse outcome than
    the refusal it replaces. Text that is not JSONC either still raises
    ``ValueError``, and the caller reports the refusal rather than swallowing it:
    unapplied excludes are exactly the silence this file exists to end.

    ``interpreter`` adds :data:`PYTHON_INTERPRETER_KEY`, and is keyword-only and
    optional because the two callers know different things:
    :func:`podbench.agent.ensure_vscode_settings` runs at seat start-up, before
    any target has been measured, and only :func:`podbench.editor.open_seat` has
    an answer. It goes through the same merge as everything else, so a value the
    user set at machine scope wins — see the "clobbering none" rule above.

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
    >>> pinned = merge_machine_settings(None, interpreter="/podbench/app/.venv/bin/py")
    >>> json.loads(pinned)["python.defaultInterpreterPath"]
    '/podbench/app/.venv/bin/py'
    >>> "python.defaultInterpreterPath" in json.loads(shipped)
    False
    """
    if interpreter is None:
        return _merge_settings(existing, SEAT_MACHINE_SETTINGS)
    return _merge_settings(
        existing, {**SEAT_MACHINE_SETTINGS, PYTHON_INTERPRETER_KEY: interpreter}
    )


def _merge_settings(existing: str | None, defaults: Mapping[str, Any]) -> str | None:
    if existing is None or not existing.strip():
        return machine_settings_text(copy.deepcopy(dict(defaults)))
    document = _document(existing, "settings.json")
    edits: list[Edit] = []
    absent: dict[str, Any] = {}
    for key, default in defaults.items():
        node = document.member(key)
        if node is None:
            absent[key] = copy.deepcopy(default)
        elif isinstance(default, dict) and node.members is not None:
            theirs = cast("dict[str, Any]", node.value)
            ours = cast("dict[str, Any]", default)
            missing = {k: v for k, v in ours.items() if k not in theirs}
            if missing:
                edits.append(insert_members(existing, node, missing))
        elif isinstance(default, list) and node.items is not None:
            patterns = cast("list[Any]", node.value)
            extra = [
                value for value in cast("list[Any]", default) if value not in patterns
            ]
            if extra:
                edits.append(append_items(existing, node, extra))
        # Anything else is a value the user set to a shape we did not expect,
        # and theirs beats ours.
    if absent:
        edits.append(insert_members(existing, document, absent))
    return apply_edits(existing, edits) if edits else None


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
    rust: bool = False,
) -> list[str]:
    """The gdb settings cpptools must apply *before* it attaches.

    Derived from :func:`podbench.gdbcmd.attach_commands` rather than written out
    again, so the one ordering that produces a correct backtrace has a single
    definition. ``file`` and ``attach`` are dropped because the adapter issues
    both itself, from ``program`` and ``processId``.

    >>> for command in setup_commands(597):
    ...     print(command)
    set pagination off
    handle SIGURG nostop noprint pass
    set sysroot /proc/597/root
    directory /proc/597/root
    add-auto-load-safe-path /proc/597/root
    set debuginfod enabled on
    """
    return [
        command
        for command in attach_commands(
            pid,
            exe=None,
            source_dirs=source_dirs,
            debuginfod=debuginfod,
            rust=rust,
        )
        if not command.startswith(("file ", "attach "))
    ]


def launch_setup_commands(
    *,
    source_dirs: Sequence[str] = (),
    debuginfod: bool = True,
    rust: bool = False,
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
    handle SIGURG nostop noprint pass
    set debuginfod enabled on
    """
    return [
        command
        for command in launch_commands(
            "unused", source_dirs=source_dirs, debuginfod=debuginfod, rust=rust
        )
        if not command.startswith(("file ", "set args ", "run"))
    ]


def _name(action: str, target: str, flavour: Flavour) -> str:
    """Every configuration is named for its flavour, and for its process.

    ``launch.json`` holds a list and VS Code's dropdown shows the names, so the
    flavour has to be *in* the name — two configurations called "podbench:
    attach to app" are a coin toss, and picking the wrong one produces a
    debugger that attaches and then shows nothing useful.

    ``target`` is :attr:`podbench.flavour.Target.label` at every call site here
    for the same reason one step further out: the ranking now offers up to five
    candidates, and ordering them is worthless if the dropdown reads "attach to
    python" three times.

    >>> _name("attach to", "demo_service.py [pid 12 python]", Flavour.DEBUGPY)
    'podbench: attach to demo_service.py [pid 12 python] (debugpy)'
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
    rust: bool = False,
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
        "cwd": seat_cwd(),
        "MIMode": "gdb",
        "miDebuggerPath": GDB_WRAPPER,
        "setupCommands": [
            {"text": command}
            for command in setup_commands(
                pid, source_dirs=source_dirs, debuginfod=debuginfod, rust=rust
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
    rust: bool = False,
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
        "cwd": cwd or seat_cwd(),
        "MIMode": "gdb",
        "miDebuggerPath": GDB_WRAPPER,
        "setupCommands": [
            {"text": command}
            for command in launch_setup_commands(
                source_dirs=source_dirs, debuginfod=debuginfod, rust=rust
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

    A fourth difference is the missing ``exec_file`` parameter, and it is not an
    oversight. cppdbg takes one because a staged copy is how gdb is kept off the
    seat's own binary at the target's path (issue #90); lldb was measured to
    *override* whatever ``program`` says once it has attached, re-resolving the
    executable from the process in this seat's mount namespace, so there is no
    path this function could be given that would survive. The collision is
    refused instead of worked around — ``flavour._assess_lldb`` withdraws the
    flavour where :func:`podbench.execfile.shadowing_file` finds one, and this
    function is reached only for the targets nothing here shadows, where the
    sysroot-prefixed ``program`` is right and lldb keeps it.

    Measured 2026-08-19 with standalone lldb 21.1.8 on the k3s bed, against a
    mount namespace built with podman - not in a podbench seat, and not with
    CodeLLDB's own bundled lldb in a remote extension host.
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


def python_path_mappings(pid: int, mode: Mode) -> list[dict[str, str]]:
    """The mapping debugpy needs, which is decided by the mode and nothing else.

    Getting this wrong does not error, and there are two ways to be wrong: a
    mapping that binds nothing means breakpoints simply never bind, and a
    mapping that binds to the *wrong real file* means the editor shows
    confident, plausible, wrong source. So it is derived from the detected mode
    rather than left to a flag.

    In Observe mode the answer is the mount namespace itself, which is true for
    a script, a console script and an editable install alike — no root is
    guessed, so none can be guessed wrongly.

    >>> python_path_mappings(1, Mode.OBSERVE)
    [{'localRoot': '/proc/1/root', 'remoteRoot': '/'}]
    >>> python_path_mappings(1, Mode.DEV)
    []

    ``/`` on the right is not the anti-pattern :func:`_parse_source_map`
    refuses: that one is gdb re-applying its own ``substitute-path`` on every
    display, which is a gdb behaviour and not a property of a root mapping.
    """
    if mode is Mode.DEV:
        # Dev mode: editor and interpreter are the same container and the same
        # inodes, so any mapping at all is a spurious one.
        return []
    # The namespace, never a narrower root — the same discipline gdbcmd states
    # for the exec file ("String concatenation, never a path join: the exe link
    # is absolute, and joining would discard the sysroot and silently read our
    # own binary"), and the same failure. A root taken from `argv` is
    # `/app/.venv/bin` for a console script, and podbench's own image installs
    # under `/app/.venv` too, so that path exists on both sides with different
    # contents: the wrong mapping *resolves* instead of failing, and the editor
    # opens the seat's copy of the workload's frame (issue #112).
    #
    # One entry, and deliberately no narrower ones beside it: a second, more
    # specific pair reintroduces exactly that collision for the paths it covers,
    # and covers nothing this one does not.
    return [{"localRoot": sysroot_path(pid), "remoteRoot": "/"}]


def debugpy_attach_configuration(
    pid: int,
    *,
    name: str,
    port: int = DEBUGPY_PORT,
    mode: Mode = Mode.OBSERVE,
) -> dict[str, Any]:
    """Connect to a debugpy server running inside the target's interpreter.

    ``connect``, never ``listen``: the server is in the app process (whether the
    app called ``debugpy.listen()`` itself or the injection put it there) and
    the editor is the client.

    ``justMyCode`` is false because the interesting frames in a pod are usually
    somebody else's — a framework's request handler, an ORM's session teardown.

    >>> config = debugpy_attach_configuration(1, name="x")
    >>> config["connect"]
    {'host': '127.0.0.1', 'port': 5678}
    >>> config["pathMappings"]
    [{'localRoot': '/proc/1/root', 'remoteRoot': '/'}]
    """
    return {
        "name": name,
        "type": ADAPTER_DEBUGPY,
        "request": "attach",
        "connect": {"host": DEBUGPY_HOST, "port": port},
        "justMyCode": False,
        "pathMappings": python_path_mappings(pid, mode),
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
    # Every name is built from `Target.label`, never from the program alone: N
    # candidates each get a full set of entries, and `merge_launch_configs`
    # matches by name, so two candidates sharing a basename do not merely read
    # alike in the dropdown - the second silently replaces the first.
    # Sourcing the Rust printers is the one thing here that turns on the
    # target's language rather than on the mode, and it is asked once so both
    # shapes agree.
    rust = target.language is Language.RUST
    if flavour is Flavour.GDB:
        if mode is Mode.DEV:
            return [
                cppdbg_launch_configuration(
                    program,
                    name=_name("launch", target.label, Flavour.GDB),
                    cwd=target.cwd,
                    source_dirs=source_dirs,
                    debuginfod=debuginfod,
                    machine=target.machine,
                    rust=rust,
                )
            ]
        return [
            cppdbg_configuration(
                target.pid,
                program,
                name=_name("attach to", target.label, Flavour.GDB),
                exec_file=exec_file,
                source_dirs=source_dirs,
                source_map=source_map,
                debuginfod=debuginfod,
                machine=target.machine,
                rust=rust,
            )
        ]
    if flavour is Flavour.LLDB:
        return [
            lldb_configuration(
                target.pid,
                program,
                name=_name("attach to", target.label, Flavour.LLDB),
                source_map=source_map,
            )
        ]
    if flavour is Flavour.DELVE:
        return [
            delve_configuration(
                target.pid,
                program,
                name=_name("attach to", target.label, Flavour.DELVE),
                source_map=source_map,
            )
        ]
    return _debugpy_configurations(target, mode, seat, port=port)


def _debugpy_configurations(
    target: Target, mode: Mode, seat: Seat, *, port: int
) -> list[dict[str, Any]]:
    connect = debugpy_attach_configuration(
        target.pid,
        name=_name("connect to", target.label, Flavour.DEBUGPY),
        port=port,
        mode=mode,
    )
    if mode is not Mode.DEV:
        return [
            debugpy_attach_configuration(
                target.pid,
                name=_name("attach to", target.label, Flavour.DEBUGPY),
                port=port,
                mode=mode,
            )
        ]
    if target.script:
        return [
            debugpy_launch_configuration(
                target.script,
                name=_name("launch", target.label, Flavour.DEBUGPY),
                cwd=target.cwd,
            ),
            connect,
        ]
    # `python -m pkg` names a module rather than a file, so there is nothing to
    # put in `program`; connecting to the process `podbench run` started is the
    # only shape left.
    return [connect]


def launch_json_text(configurations: Sequence[Mapping[str, Any]]) -> str:
    """A whole ``launch.json`` document, newline-terminated.

    This is what ``--print-config`` puts on stdout, and it is pasted into a
    ``launch.json`` by hand often enough that its **top-level keys are part of
    the contract**: exactly the two a launch document has, and nothing else. A
    third — the measured interpreter was the candidate, #219 — would make every
    pasted copy carry a key VS Code's own schema does not know, and draw a
    squiggle on the file this verb exists to hand over. Anything else podbench
    has to tell the laptop travels on stderr instead; see
    :data:`INTERPRETER_NOTE`.

    >>> document = json.loads(launch_json_text([{"name": "podbench: x"}]))
    >>> sorted(document)
    ['configurations', 'version']
    >>> document["version"], [entry["name"] for entry in document["configurations"]]
    ('0.2.0', ['podbench: x'])
    """
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

    The file this merges into is usually the *application's*, committed and
    unmodified: on a hotfixed pod ``podbench vscode`` opens the claim, and a
    normal project ships a ``.vscode/launch.json`` that VS Code's own scaffold
    wrote with ``//`` comments in it. So it is read as JSONC and edited
    textually — podbench's own entries are replaced where they stand and new
    ones appended, and every other byte in the file, comments included, is left
    exactly where the user put it. See :mod:`podbench.jsonc`.

    Raises ``ValueError`` on text that is not JSONC either, with :mod:`json`'s
    own wording: a file podbench cannot read is one it must not rewrite.

    >>> written = merge_launch_configs(None, [{"name": "a"}])
    >>> print(written, end="")
    {
      "version": "0.2.0",
      "configurations": [
        {
          "name": "a"
        }
      ]
    }
    >>> print(merge_launch_configs(written, [{"name": "a", "port": 1}]), end="")
    {
      "version": "0.2.0",
      "configurations": [
        {
          "name": "a",
          "port": 1
        }
      ]
    }
    """
    if existing is None or not existing.strip():
        return launch_json_text(configurations)
    document = _document(existing, "launch.json")
    node = document.member("configurations")
    if node is None or node.items is None:
        # No list to merge into — either the key is absent or it holds something
        # that is not a list, which is the one case where podbench's entries can
        # only arrive by putting a list there.
        entries = [dict(configuration) for configuration in configurations]
        edit = (
            insert_members(existing, document, {"configurations": entries})
            if node is None
            else replace_value(existing, node, entries)
        )
        return apply_edits(existing, [edit])
    edits: list[Edit] = []
    appended: list[dict[str, Any]] = []
    claimed: set[int] = set()
    for configuration in configurations:
        name = configuration.get("name")
        # `claimed` guards the one way two of these edits could overlap: two
        # configurations sharing a name would otherwise both replace the same
        # entry. Names are unique by construction (see `_name`), and an
        # invariant worth relying on is worth not corrupting a file over.
        at = next(
            (
                index
                for index, item in enumerate(node.items)
                if index not in claimed
                and item.members is not None
                and cast("dict[str, Any]", item.value).get("name") == name
            ),
            None,
        )
        if at is None:
            appended.append(dict(configuration))
            continue
        claimed.add(at)
        edits.append(replace_value(existing, node.items[at], dict(configuration)))
    if appended:
        edits.append(append_items(existing, node, appended))
    return apply_edits(existing, edits)


def merge_launch_json(existing: str | None, configuration: Mapping[str, Any]) -> str:
    """:func:`merge_launch_configs` for a single configuration."""
    return merge_launch_configs(existing, [configuration])


def _warn(message: str) -> None:
    """Warnings go to stderr, so ``--print-config`` stays pasteable."""
    for line in message.splitlines():
        print(f"debug-config: {line}", file=sys.stderr)


def _parse_source_map(entries: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """``FROM=TO`` pairs, and a complaint for anything that is not one.

    Refusing ``/`` here is not in tension with
    :func:`python_path_mappings` emitting ``remoteRoot: "/"``, however alike the
    two read. They are different debuggers doing different things.
    ``remoteRoot`` is a DAP path translation the adapter applies *once*, to turn
    a path the debuggee reported into one the editor can open. This becomes
    gdb's ``substitute-path``, which gdb re-applies every time it computes
    ``fullname`` — and the exec file is already loaded through the sysroot, so
    substituting ``/`` again prefixes a path that carries the prefix already.

    Left unremarked, the apparent contradiction invites reconciling one to the
    other, and either direction reintroduces a defect: dropping this refusal
    doubles the prefix, and narrowing ``remoteRoot`` puts back the guessed root
    that made a wrong mapping *resolve* instead of fail (issue #112).
    """
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


PortChooser = Callable[[], int]
"""How a free port is obtained. Injected so the unit suite never binds one."""


def ephemeral_port(host: str = DEBUGPY_HOST) -> int:
    """Ask the kernel for a port nothing on this network namespace holds.

    Bind zero and read back what was assigned: it is the only way to choose a
    port without racing whoever else is choosing one. The fixed 5678 raced
    twice over. Within a pod two seats on two pids collide on it silently — the
    second ``debugpy --listen`` dies and the emitted configuration connects to
    the first. Under ``hostNetwork: true`` the namespace is the *node's*, so the
    collision is with **another pod** on the same node, which is issue #87 seen
    from the writing end rather than the reading end.

    The socket is closed before the port is handed on, so this is a hint and not
    a reservation — but the window is microseconds against 5678's permanence,
    and :func:`_listening_debugpy` re-reads the port afterwards and attributes
    whatever it finds. Nothing is left behind to trip the next bind either: a
    listener that never accepted a connection has no ``TIME_WAIT`` to leave
    (report 3.16).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return cast("tuple[str, int]", sock.getsockname())[1]


@dataclass(frozen=True)
class ListeningServer:
    """A listener found on the debugpy port, and who was proved to hold it."""

    port: int
    owner: PortOwner | None = None
    """``None`` when nothing could be attributed: either ``ss`` reported no
    owning pid, or the pid it reported is gone or a zombie."""

    @property
    def attributed(self) -> bool:
        """Whether the socket was traced to a container in this pod.

        The two facts that establish it are the pid resolving in *our* pid
        namespace at all — a process in another pod is invisible there, so ``ss``
        cannot name it and this is already ``False`` — and
        ``/proc/<pid>/root`` answering, which is what tells one container from
        another. Anything short of both is ``unknown``, and unknown must never
        be reported as "in this pod" (issue #87).
        """
        owner = self.owner
        return owner is not None and (
            owner.is_target or owner.same_container is not None
        )

    def describe(self) -> str:
        """Who holds the port, for the sentence that offers it."""
        return (
            self.owner.describe()
            if self.owner is not None
            else "a process this seat cannot see"
        )


def _listening_debugpy(
    port: int,
    *,
    runner: Runner | None,
    proc: Path,
    target_cid: str | None,
) -> ListeningServer | None:
    """What is serving ``port``, and which container it belongs to.

    Reuses ``dev``'s ``ss`` parsing rather than growing a second one — and now
    its *attribution* as well, which is the half that was missing. The premise
    this function used to rest on ("the pod's network namespace is shared, so a
    listener anywhere in it is reachable from the seat at 127.0.0.1") is true of
    an ordinary pod and false under ``hostNetwork: true``, where that namespace
    is the node's: the sweep found 5678 held by a podbench seat in a **different
    pod on the same node**, announced it as this pod's own server and emitted a
    configuration pointing at it (issue #87).

    So a port is not evidence. ``ss -lntpe`` already reports the owning pid and
    the socket inode; the fix is to stop discarding them. The pid is resolved in
    this seat's own ``/proc``, which is the pod's pid namespace, so a process in
    another pod cannot be named there at all — an unattributable listener is
    exactly the shape #87 takes, and it is returned as ``unknown`` rather than
    as a server.

    ``read_process`` first, because a pid can also be a zombie holding nothing:
    a dead server that still answers ``kill -0`` is how a bootstrap once handed
    a client a dead port (report 3.19).

    An ``ss`` that cannot run answers ``None``, since a guess here would emit a
    configuration that connects to nothing.

    Imported here rather than at module scope because the three modules that
    hold VS Code, dev-pod and seat-preparation knowledge genuinely each need a
    little of the next: ``agent`` wants this module's machine settings, ``dev``
    wants ``agent``'s pubkey env var, and this function wants ``dev``'s ``ss``
    parsing — a cycle that only appears once all three are present. This is the
    narrowest of the three edges, a single call in a single function, so it is
    the one that gives way.
    """
    from .dev import (
        LISTENERS_COMMAND,
        identify_owner,
        listeners_on,
        parse_ss,
        read_process,
    )

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
    entries = listeners_on(parse_ss(result.stdout), port)
    if not entries:
        return None
    for entry in entries:
        for pid in entry.pids:
            snapshot = read_process(pid, proc=proc)
            if snapshot is None or not snapshot.alive:
                continue
            owner = identify_owner(pid, proc=proc, target_cid=target_cid)
            if owner.is_target or owner.same_container is not None:
                return ListeningServer(port, owner=owner)
    return ListeningServer(port)


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
    It stays narrowed to the case that would otherwise be decided without one (a
    Python target, in attach mode, with nothing already listening, in a seat
    whose capability bit is clear) because a measurement that costs nothing is
    still an answer nobody asked for anywhere else. Narrow, not redundant: the
    free credential check `flavour.ptrace_evidence` falls back to is a reason
    not to *refuse* that seat, and only an attach is a reason to say it works.

    ``probe=False`` is ``--print-config``: printing what *would* be written must
    not touch the workload, however cheap touching it has become.

    A synthetic ``/proc`` is never probed. Its pids name unrelated processes on
    whatever machine is running, so an attach there would measure something
    else entirely — and the unit tests that inject one must keep answering from
    what they wrote into the tree: the capability mask, and whether they gave
    the target a ``root`` that resolves.
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
        #
        # It names the credential check rather than CapEff because that is what
        # decides the injection on this path (`flavour.ptrace_evidence`), and a
        # warning that named the bit would send the reader after the mechanism
        # that is not deciding - which is #89 in miniature.
        _warn(
            f"--print-config touches nothing, so ptrace to pid {target.pid} was "
            "not measured; the debugpy injection is judged on whether this seat "
            f"may read /proc/{target.pid}/root, which the kernel gates on the "
            "same credentials an attach takes. Re-run `podbench debug-config` "
            f"without --print-config, or `podbench capreport {target.pid}`, to "
            "measure the attach itself"
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


def _exposure_warning(port: int, host_network: bool | None) -> str | None:
    """Who else can reach a debugpy server started on ``port``, or ``None``.

    A debugpy server authenticates nobody: anything that can open the socket can
    load a module into the target and run it. Inside a pod's own network
    namespace that is the same blast radius the seat already has, so it is not
    worth a line. Under ``hostNetwork: true`` the same loopback bind is on the
    **node's** loopback, shared with every other hostNetwork pod and every node
    daemon — a different blast radius entirely, and the reader has to be told
    before the server exists rather than after (issue #87).

    Allowed, not refused: the sweep's own targets are hostNetwork IOCs, and a
    verb that refused them would refuse the pods podbench exists for.
    """
    if host_network is True:
        return (
            f"this pod runs with hostNetwork: true, so {DEBUGPY_HOST}:{port} is "
            "the *node's* loopback and not this pod's: the debugpy server "
            "authenticates nobody, so every other hostNetwork pod and every "
            "node daemon on this node can connect to it and run code inside "
            "the target. Nothing off the node can reach it. Stop the server "
            "when you are done - killing the seat does not - and pass `--port` "
            "if you need to pin which port it is on"
        )
    if host_network is None:
        return (
            f"this seat cannot tell whether {DEBUGPY_HOST}:{port} is this pod's "
            "loopback or the node's - it was landed before podbench recorded "
            "hostNetwork - so treat the debugpy server as unauthenticated and "
            "node-visible until you have checked `kubectl get pod -o "
            "jsonpath='{.spec.hostNetwork}'`. Re-run `podbench attach` for a "
            "seat that knows, and pass `--port` to pin which port it is on"
        )
    return None


def _inject(
    target: Target,
    mode: Mode,
    seat: Seat,
    *,
    port: int,
    host_network: bool | None,
    runner: Runner | None,
    prove: Prover,
) -> Injected | None:
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

    ``None`` is "nothing was injected", and it is a third answer rather than a
    failure: the caller withdraws a configuration whose port could not be
    proved, and a run that never touched the workload has nothing to withdraw.
    The whole :class:`~podbench.provision.Injected` record comes back for the
    same reason - ``ok`` is the injector's exit code and ``proved`` is whether a
    DAP session could be started, and the two disagreeing is exactly the
    2026-08-24 measurement that #218 is (§8.2).
    """
    if mode is Mode.DEV or target.language is not Language.PYTHON:
        # Both already named by `_provision`, which refuses first for the same
        # two reasons; saying it twice would read as two separate problems.
        return None
    if seat.listening_port is not None:
        # Not a no-op worth reporting: `_provision` has just said the same
        # thing, and the emitted configuration connects to it either way.
        return None
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
        return None
    exposure = _exposure_warning(port, host_network)
    if exposure is not None:
        _warn(f"--provision: {exposure}")
    command = injection_command(target, seat, port)
    _warn(
        f"--provision: starting the server inside the app. This ptraces the "
        f"workload, so it stops answering probes until the attach returns; on "
        f"a probed pod the deadlines `attach` prints are the budget it spends. "
        f"Running `{command}`"
    )
    injected = inject_debugpy(command, runner=runner, port=port, prove=prove)
    for message in injected.messages:
        _warn(f"--provision: {message}")
    # Both halves travel, and the caller reads a different one for each
    # decision: `ok` decides where to re-probe for a listener - an injection
    # whose adapter never answered has still moved the server off the
    # conventional port - and `proved` decides whether anything may be written
    # naming this port at all.
    return injected


def _withhold(pid: int, port: int, *, detail: str | None, remedy: str) -> None:
    """Say that nothing was written naming ``port``, and why.

    One sentence for both gates - the adapter that was found and the injection
    that was made - because it is one decision. **The DAP handshake is the only
    evidence anywhere in a run that a session can be started**, and a
    configuration naming a port that failed ``initialize`` is one whose F5 hangs
    or is refused (#218). Written once so the two cannot drift into saying
    different things about the same verdict, and so
    :data:`WITHHELD_NOTE` - which the laptop reads out of this line - has a
    single author.

    ``remedy`` is the caller's, because that is the half that genuinely differs:
    an adapter that stopped answering wants the app restarted, while an
    injection that started nothing has a command to run by hand.
    """
    _warn(
        f"no debug session could be started on {DEBUGPY_HOST}:{port}"
        + (f" ({detail})" if detail else "")
        + f", {WITHHELD_NOTE} for pid {pid} and whatever launch.json already "
        "holds is kept - replacing a configuration that worked with one naming "
        f"a port nothing answers is worse than changing nothing. {remedy}"
    )


def _no_ptrace_clause(target: Target, evidence: PtraceEvidence) -> str:
    """Why the injection cannot be driven from here, in the reader's terms.

    One clause per mechanism, because the reader's next move differs: a measured
    refusal has four candidate mechanisms and `capreport` names which, while a
    credential denial is the seat's own uid against the target's and no
    capability will move it.

    >>> target = Target(pid=7, language=Language.PYTHON, program="/usr/bin/python3")
    >>> _no_ptrace_clause(target, PtraceEvidence.DENIED)
    'this seat may not read /proc/7/root, which takes the credentials an attach takes'
    """
    if evidence is PtraceEvidence.REFUSED:
        return "ptrace to the target was refused when this seat measured it"
    if evidence is PtraceEvidence.DENIED:
        return (
            f"this seat may not read /proc/{target.pid}/root, which takes the "
            "credentials an attach takes"
        )
    return "this seat cannot ptrace the target"


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
            f"{DEBUGPY_HOST}:{seat.listening_port}, held by "
            f"{seat.listening_owner or 'a process this seat could not name'}, "
            "and the emitted configuration connects to it without any of this"
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
    # Asked of the same evidence `assess` uses, not of the bit: this sentence
    # and the refusal it prepares the reader for are two spellings of one fact,
    # and a seat that measured an attach reads "the injection cannot be driven
    # from here" immediately before the injection is driven from here (#89).
    # Said at all only where the injection is really withdrawn: an unmeasured
    # seat that passes the credential check is offered it, and the caveat it is
    # owed - that nothing attached - is `assess`'s to print beside the emission.
    if not can_ptrace_target(seat):
        # Said, not refused - unlike the arm64 gate above. The tree lands in the
        # *target's* rootfs, which outlives this seat, so provisioning now and
        # relaunching on the `full` rung still works; what would be wrong is
        # letting the install land and then reading "CAP_SYS_PTRACE is not in
        # this seat's effective set" two lines later with nothing joining them.
        # The clause names the mechanism that said no, because the remedy
        # differs: a measured refusal is one of four mechanisms and a credential
        # denial is the seat's uid.
        _warn(
            f"--provision: {_no_ptrace_clause(target, ptrace_evidence(seat))}, so the "
            "injection cannot be driven from here whatever gets installed - the "
            "copy goes into the target's own rootfs and outlives this seat, so "
            "a relaunch on the `full` rung with `podbench attach --max-rung "
            "full` picks it up rather than repeating the install"
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
    port: int | None,
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
    choose_port: PortChooser,
    prove: Prover,
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

    # Resolved once, here, for the same reason `resolve_target_pids` reads it:
    # the id is what turns "not this container" into "the app", and without it
    # a listener in the target reads as an anonymous neighbour (report 3.15).
    target_cid = (
        strip_container_scheme(container_id) or None
        if container_id
        else env_target_container_id()
    )
    host_network = env_host_network()
    # Where a debugpy server already is, read out of the PID namespace rather
    # than guessed from a port (#218). `debugpy.listen()` spawns its adapter
    # from the debuggee, so the adapter is a *child* of the target and carries
    # the client port in its own argv - and `ppid` is the discriminator because
    # under hostNetwork a port names nothing about which pod holds it (#87).
    #
    # Enumerated here rather than per candidate: `_for_target` runs once per
    # offered pid, and this is one walk of a namespace that had 112 processes on
    # the largest pod surveyed.
    adapters = debugpy_adapters(list_processes(target_cid, proc=proc))

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
                choose_port=choose_port,
                prove=prove,
                target_cid=target_cid,
                host_network=host_network,
                adapter=adapters.get(candidate),
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
    port: int | None,
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
    choose_port: PortChooser,
    prove: Prover,
    target_cid: str | None,
    host_network: bool | None,
    adapter: DebugpyAdapter | None = None,
    hint: bool,
    probe: bool = True,
) -> list[dict[str, Any]]:
    """Everything one candidate pid contributes to ``launch.json``.

    ``port`` is ``None`` unless the user named one, and the two questions it
    used to answer are separated here because their answers differ. *Where to
    look* for a server that is already running is ``adapter``'s port where this
    pid has one and :data:`DEBUGPY_PORT` otherwise, the conventional port an app
    that ran ``-m debugpy`` at start-up will be on.
    *Where a server started from this run should listen* is a port the kernel
    picks (:func:`ephemeral_port`), so that two seats on one node - which under
    ``hostNetwork`` share a network namespace - cannot land on the same one.

    ``adapter`` is the third question, and it is the one #218 was: *where the
    server this target already has actually is*. A reconnect used to probe 5678,
    find nothing there, choose a fresh port, inject into a pid debugpy declines
    to serve twice and emit the new number - replacing a working ``launch.json``
    with one naming a closed port (2026-08-24, §8.2).
    """
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
        elif target.language in NATIVE_LANGUAGES:
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

    # **Parentage proves the adapter is this target's; it does not prove it
    # still answers.** An adapter mid-teardown, or one whose pydevd threads have
    # exited while the process lingers, is live by `ppid` and dead by
    # `initialize` - and §9's ten debugpy/pydevd mappings that outlived the
    # adapter's death are why "still there" is not taken on trust here. So the
    # port a configuration would name is asked the same question the injection
    # is judged by, through the same `prove` seam, rather than a second
    # implementation whose verdict could differ from the one #218 is about.
    #
    # It costs one `initialize` and nothing else: §6.1 measured that this probe
    # does not burn a live adapter's session, which is what makes asking safe on
    # the reconnect path - now the common one - rather than only on the
    # injection's.
    #
    # Never in dev mode: the dev-mode debugpy entry is a `launch` and names no
    # port at all, so there is nothing here for a handshake to be evidence of.
    adapter_answers = True
    if adapter is not None and mode is not Mode.DEV:
        answer = prove(adapter.port)
        adapter_answers = answer.ok
        if not adapter_answers:
            wanted = wanted - {Flavour.DEBUGPY}
            _withhold(
                target.pid,
                adapter.port,
                detail=answer.detail,
                remedy=(
                    f"{adapter.describe()} is still running, so this is an "
                    "adapter that has stopped answering rather than a missing "
                    "one - and debugpy serves a process once, so nothing is "
                    "injected over it either. Restart the app to get a process "
                    "debugpy has not already served; `debugpy.listen()` in the "
                    "app image needs no injection at all"
                ),
            )

    if (
        adapter is not None
        and adapter_answers
        and port is not None
        and adapter.port != port
    ):
        # The adapter wins over `--port`, and is said rather than done quietly:
        # debugpy refuses a second `listen()` in a process that already has one,
        # so injecting on the requested port would return 0, start nothing, and
        # leave the working server unnamed (§8.2). `--port` still decides where
        # a *new* server goes, which is every target without one.
        _warn(
            f"pid {target.pid} is already served by {adapter.describe()} on "
            f"{DEBUGPY_HOST}:{adapter.port}, so the configuration names that "
            f"port rather than the {port} you asked for - debugpy serves one "
            "process once, and a second listen in it starts nothing"
        )

    # Where to look for a server that is already running. It moves exactly once,
    # to the port an injection was actually made on: an ephemeral port is not
    # 5678, so re-probing 5678 after --provision would find the run's own new
    # server missing and report "nothing is listening" about a port it had just
    # opened.
    probe_at = DEBUGPY_PORT if port is None else port
    chosen: int | None = None
    said_unattributed = False

    def serving_port() -> int:
        """The port a server started by *this* run would listen on."""
        nonlocal chosen
        if port is not None:
            # Named by the user, so it is theirs: `--port` is the way to pin one,
            # and a verb that overrode it would leave no way to.
            return port
        if mode is Mode.DEV or target.language is not Language.PYTHON:
            # Dev mode connects to whatever `podbench run` started, which is on
            # the conventional port; a non-Python target has no server at all.
            return DEBUGPY_PORT
        if chosen is None:
            try:
                chosen = choose_port()
            except OSError as error:
                # Not fatal: a seat that cannot bind loopback still authors a
                # correct configuration, it just cannot promise the port is free.
                _warn(
                    f"could not ask the kernel for a free port ({error}), so "
                    f"the conventional {DEBUGPY_PORT} is used - pass `--port` "
                    "to pin one this seat is known to be able to bind"
                )
                chosen = DEBUGPY_PORT
        return chosen

    def survey(listening: int | None, listening_owner: str | None) -> Seat:
        """The seat as it stands, given who was found serving this target."""
        nonlocal attach_ok, attach_asked
        # The languages that reach gdb only (NATIVE_LANGUAGES, which is where
        # Rust joins), and still, though the debugpy pid
        # injection now withdraws on the same answer — it drives gdb to `call
        # (void*)dlopen(...)`, which needs the symbols this asks about. What gdb
        # says about a python-build-standalone interpreter is issue #90's open
        # question, and measuring it here would settle that issue by accident,
        # in a verb that otherwise only authors a file, at the price of a gdb
        # start-up per candidate. #90 is where the Python case joins.
        load_error = (
            program_load_error(target.pid, gdb_program, runner=runner)
            if gdb_program and target.language in NATIVE_LANGUAGES
            else None
        )
        surveyed = survey_seat(
            target,
            proc=proc,
            which=which,
            debugpy_root=debugpy_root,
            listening_port=listening,
            listening_owner=listening_owner,
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

    def measure() -> Seat:
        nonlocal attach_ok, attach_asked, said_unattributed
        if adapter is not None:
            # Attributed by parentage, so no `ss` and no attribution question:
            # this pid's own child is serving this pid, which is the one fact a
            # port cannot establish under hostNetwork (#87). A seat with no `ss`
            # - or one whose sweep names an owner it cannot resolve - still gets
            # the right answer here, because the evidence is the process tree.
            return survey(adapter.port, adapter.describe())
        # The listener is re-probed on every measurement rather than read once:
        # --provision can *start* one part way through this function, and a
        # port sampled before that would emit a configuration whose "already
        # listening" answer is stale in the one run that changed it.
        found = (
            _listening_debugpy(
                probe_at, runner=runner, proc=proc, target_cid=target_cid
            )
            if target.language is Language.PYTHON
            else None
        )
        if found is not None and not found.attributed and not said_unattributed:
            said_unattributed = True
            # The whole of issue #87 in one line. `ss` names an owner for every
            # process in this pod's pid namespace, so a listener it cannot name
            # is one this seat has no business claiming - under hostNetwork it
            # belongs to another pod or a node daemon, and the old code emitted
            # a configuration pointing straight at it.
            _warn(
                f"port {found.port} is held by {found.describe()}: this seat "
                "cannot attribute the socket to any container in this pod"
                + (
                    ", and hostNetwork: true means the listener may be in "
                    "another pod or a node daemon sharing the node's network "
                    "namespace"
                    if host_network is True
                    else ", and whether this pod uses hostNetwork is unknown "
                    "to this seat"
                    if host_network is None
                    else ""
                )
                + ". Treating it as unknown rather than as this pod's server, "
                "so nothing here connects to it - pass `--port` to look at a "
                "different one"
            )
        listening = found.port if found is not None and found.attributed else None
        listening_owner = (
            found.describe() if found is not None and found.attributed else None
        )
        return survey(listening, listening_owner)

    seat = measure()
    # `adapter_answers` and not merely `provision`: a live adapter that has
    # stopped answering is not licence to start a second one. debugpy serves a
    # process once, so the injection would return 0 and leave nothing behind
    # (§8.2) - and the workload would have been ptraced for it. Both mutations
    # are skipped rather than refused one layer down, because `_provision`'s
    # "already listening ... and the emitted configuration connects to it"
    # sentence is untrue of a run that has just withdrawn that configuration.
    if provision and adapter_answers:
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
        injected_on = serving_port()
        injected = _inject(
            target,
            mode,
            seat,
            port=injected_on,
            host_network=host_network,
            runner=runner,
            prove=prove,
        )
        if injected is not None and injected.ok:
            # The re-measure has to look where the server was actually put, not
            # where a server conventionally is.
            probe_at = injected_on
            seat = measure()
        if injected is not None and not injected.proved:
            # **The handshake decides what may be written**, and this is the
            # whole of #218. The DAP `initialize` above is the only evidence
            # anywhere in this run that a session can be started, and a run that
            # computed it and then emitted the port anyway replaced a working
            # launch.json with one naming a closed port (2026-08-24, §8.2).
            #
            # Withdrawing the flavour keeps whatever the file already holds:
            # `merge_launch_configs` matches on name, so an entry podbench does
            # not emit is an entry it does not touch. That is deliberately the
            # answer to a question nobody has measured - whether a third
            # injection into a pid whose pydevd threads have exited would work -
            # because it does not need answering.
            wanted = wanted - {Flavour.DEBUGPY}
            _withhold(
                target.pid,
                injected_on,
                # The injector's own outcome, where there was one: a run that
                # never reached a handshake has `session is None`, and inventing
                # a refusal for it would read as a second failure rather than as
                # the absence of a question.
                detail=injected.session.detail if injected.session else None,
                remedy=(
                    "debugpy serves a process once, so a target that has "
                    "already been debugged needs a restart before it can be "
                    "again; `debugpy.listen()` in the app image needs none"
                ),
            )
            # The paste survives the withdrawal, because now it is the reader's
            # only way through: `_hint` is suppressed with the flavour, and its
            # own first sentence ("the configuration above connects to a closed
            # port") would name a configuration this run did not write.
            print(
                "debug-config: --provision: the injection this run attempted, "
                "to run by hand - it runs in *this seat*, whose interpreter and "
                "PYTHONPATH are not the target's however alike they are "
                f"spelled:\n{injection_command(target, seat, injected_on)}",
                file=sys.stderr,
            )
    assessments = assess(target, mode, seat, wanted=wanted)

    _warn(
        f"pid {pid} ({target.name}): {target.language.value} target, "
        f"{mode.value} mode" + (f", {target.machine}" if target.machine else "")
    )
    # The best candidate only. `hint` is what marks it, and N candidates each
    # naming an interpreter would leave the laptop to pick one - which is a
    # choice made twice, in the half of the pair that cannot see /proc.
    if hint and target.language is Language.PYTHON and target.program:
        # `program` is the exe as the *target's* rootfs spells it, which for a
        # Python process is its interpreter. Whether that spelling means the
        # same file in the seat is `interpreter_for_folder`'s question, not this
        # one: see INTERPRETER_NOTE.
        _warn(f"{INTERPRETER_NOTE}{target.program}")
    # An attributed server's own port beats any port this run would have chosen:
    # the configuration has to connect to the process that exists, and where the
    # app started its own `debugpy --listen` that is neither 5678-by-default nor
    # the ephemeral one nothing was ever bound to.
    emit_port = (
        seat.listening_port if seat.listening_port is not None else serving_port()
    )
    configurations = _emit(
        assessments,
        wanted,
        explicit,
        target,
        mode,
        seat,
        port=emit_port,
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
    # `hint` alone, never `and configurations`. `hint` means "this is the best
    # candidate"; whether the hint is *owed* is a question about the seat, and
    # _hint's own guards - Python, not dev mode, nothing already listening - are
    # the ones that answer it. Coupling it to a non-empty emission withheld the
    # actionable half of a failure on exactly the run that failed, and it only
    # happens not to fire today because _emit and _hint gate on the same
    # `wanted`/`available` pair. The next flavour that assesses available and
    # contributes nothing makes it fire.
    if hint:
        _hint(
            target,
            mode,
            seat,
            assessments,
            wanted,
            port=emit_port,
            host_network=host_network,
        )
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
    host_network: bool | None,
) -> None:
    """Say what still has to happen before the debugpy entry can connect.

    The configuration is emitted as soon as the *prerequisites* are met, but
    nothing is listening until the injection is run — and running it is not
    something authoring a launch.json may do on its own, since it ptraces the
    workload and leaves a server inside it.

    ``--provision`` is named here for a reader who has just run this verb
    without it, and it is the *second* of debugpy's two blockers: a target that
    can already import debugpy meets every prerequisite, so the configuration is
    emitted and nothing is listening on the port it connects to. Both blockers
    have one answer and it is this flag. Since #230 nothing on the laptop reads
    this line to decide anything - ``podbench vscode`` offers the step and the
    reader runs it - so what it owes is a person, not a parser.

    The seat is named too, because on this pod's shape it is ambiguous: the
    workload's venv and the seat's are both ``/app/.venv``, so the interpreter
    in the pasted command is one file over ``kubectl exec -c <seat>`` and a
    different one over ``-c <target>``. Same collision as the sysroot ones, in
    the instruction text rather than in BFD.
    """
    if Flavour.DEBUGPY not in wanted or mode is Mode.DEV:
        return
    debugpy = next(
        (item for item in assessments if item.flavour is Flavour.DEBUGPY), None
    )
    if debugpy is None or not debugpy.available or seat.listening_port is not None:
        return
    # Before the command, not after it: this is the paste that creates the
    # exposure, and a caveat printed under a command has already been skipped.
    exposure = _exposure_warning(port, host_network)
    if exposure is not None:
        _warn(exposure)
    print(
        f"debug-config: nothing is listening on {DEBUGPY_HOST}:{port} yet, so "
        "the configuration above connects to a closed port. `podbench "
        "debug-config --provision` starts the server itself; by hand it is the "
        "command below, which runs in *this seat* - the interpreter is the "
        "seat's own and PYTHONPATH reaches the target's debugpy through /proc, "
        "so the same spelling pasted into the target container is a different "
        "file:\n"
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
    port_chooser: PortChooser = ephemeral_port,
    prover: Prover = dap_initialize,
) -> int:
    """``podbench debug-config`` — author ``launch.json`` for this seat.

    ``proc``, ``runner``, ``attacher``, ``which``, ``debugpy_root`` and
    ``port_chooser`` are test seams; the CLI passes none of them. ``which`` in
    particular: whether this image ships gdb decides whether a gdb
    configuration is emitted at all, and a unit test must not answer that
    question from the machine it happens to run on. ``attacher`` is the ptrace
    backend :func:`measured_attach` uses, and a test that wants an answer out of
    it has to inject one *and* a real ``proc``: against a synthetic tree nothing
    is attached at all. ``port_chooser`` and ``prover`` are last because their
    defaults really open sockets: a test asserting on the emitted port has to
    know which number to expect, and one exercising ``--provision`` has to say
    what the adapter answers rather than letting a handshake reach whatever holds
    that port on the machine the suite happens to run on.
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
            int | None,
            typer.Option(
                "--port",
                metavar="PORT",
                help="pin the debugpy port. The default looks for an existing "
                f"server on {DEBUGPY_PORT} and lets the kernel choose a free "
                "port for one --provision starts, so two seats on a node "
                "cannot collide. Always on 127.0.0.1: the seat shares the "
                "target's network namespace",
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
                choose_port=port_chooser,
                prove=prover,
            )
        )

    # See the note on ``prog`` in probe.py: the usage line names the only
    # spelling there is.
    return run(app, args, prog="podbench debug-config")
