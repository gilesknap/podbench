"""Point the local VS Code at a seat that has just landed.

Four steps stood between ``podbench attach`` and a bound breakpoint, and the two
a user is most likely to improvise are the two that fail quietly:

* **which folder.** ``/proc/<pid>/root`` is a symlink into another container's
  root, so File -> Open Folder -> ``/`` is a recursive walk with no bottom. A
  seat cannot reserve memory of its own (report 3.9) and an OOM-killed ephemeral
  container cannot be restarted, so that walk ends the seat and burns its name
  for the pod's lifetime. A verb that picks the folder is a verb that cannot
  pick ``/``.
* **where the extension installs.** The button has to read "Install in SSH:
  ``<alias>``". A locally-installed extension runs the debug adapter on the
  laptop, where none of the ``/proc/<pid>/root`` paths mean anything, and the
  failure presents as a bad ``launch.json``. ``code --remote ssh-remote+<alias>
  --install-extension`` is that button as a flag.

Everything either step needs is already known here: the alias comes from the
stanza this attach just wrote, the folder from the seat's own home, and the
extensions from the configurations ``debug-config`` emits in the seat — so they
are read from the *emitted* configurations rather than assessed a second time
from the laptop, which could not see the target's ``/proc`` anyway.

The order below is load-bearing. ``.vscode/settings.json`` is written **before**
the window opens, because the watcher and the indexer start walking the moment
it does; opening first and configuring afterwards is the race whose loser is an
unrecoverable seat.

Files reach the seat over ``kubectl exec`` rather than over the ssh transport:
this runs while the launcher still has the :class:`podbench.kubectl.Kubectl` it
landed the seat with, and the ssh path additionally requires the user to have
made the one manual edit (``Include``) podbench has always asked for.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from shlex import quote
from typing import Any, cast

from .kubectl import Kubectl, Runner, run_subprocess
from .model import ContainerRef, as_dict
from .vscode import (
    extensions_for,
    merge_extensions_json,
    merge_folder_settings,
    merge_launch_configs,
)

__all__ = [
    "DEFAULT_EDITOR",
    "EditorError",
    "open_seat",
    "remote_authority",
    "resolve_editor",
]

DEFAULT_EDITOR = "code"
"""The only editor ``--open`` drives.

``cursor``, ``codium`` and ``windsurf`` take the same flags and would cost a
mapping and a flag, but none of the four has been driven against a podbench seat
from a real GUI client yet. One that is unverified is a smaller claim than four.
"""

VSCODE_DIR = ".vscode"

DEBUG_CONFIG_ARGV = ("podbench", "debug-config", "--print-config")
"""How the seat is asked which debuggers apply.

``--print-config`` rather than ``--output``: the assessment then happens exactly
once, so the extensions installed and the configurations written cannot come
from two different measurements of the same target.

Spelled as the two-token verb for the reason ``CAPREPORT_ARGV`` is: since #47
the image ships ``podbench`` and ``gdb-podbench`` and no per-subcommand alias,
so a bare ``debug-config`` over ``kubectl exec`` is ``executable file not
found`` — which arrives here as "printed no JSON", blaming the assessment for a
command that never ran.
"""

_STORAGE_NOTE = (
    "these unpack into the seat's ~/.vscode-server, which in Observe mode is on "
    "the workload's ephemeral-storage budget - an ephemeral container may not "
    "declare resources of its own (report 3.9), and a server plus one extension "
    "measured 1215 MiB live"
)


class EditorError(RuntimeError):
    """``--open`` cannot be honoured, and the sentence says which mechanism.

    A named error rather than a traceback because every cause here is something
    the user can fix in one step — install the CLI, add the ``Include`` line,
    land a seat that can log in — and a traceback names none of them.
    """


def resolve_editor(which: Callable[[str], str | None] = shutil.which) -> str:
    """Where the VS Code CLI is, or a refusal naming what to install.

    Resolved *before* the seat is landed. An ephemeral container's name is
    permanent, so a run that was always going to end at "no ``code``" must not
    spend one first.
    """
    found = which(DEFAULT_EDITOR)
    if found is None:
        raise EditorError(
            f"--open needs the VS Code CLI (`{DEFAULT_EDITOR}`) on PATH and "
            "there is none. Install it from VS Code with Command Palette -> "
            "'Shell Command: Install 'code' command in PATH', or drop --open "
            "and connect with Remote-SSH: Connect to Host."
        )
    return found


def remote_authority(alias: str) -> str:
    """VS Code's spelling of "the Remote-SSH host named ``alias``".

    >>> remote_authority("podbench-demo-api")
    'ssh-remote+podbench-demo-api'
    """
    return f"ssh-remote+{alias}"


def open_seat(
    kubectl: Kubectl,
    seat: ContainerRef,
    *,
    alias: str,
    folder: str,
    editor: str = DEFAULT_EDITOR,
    runner: Runner | None = None,
) -> list[str]:
    """Configure ``folder`` in the seat, install what it needs, and open it.

    ``folder`` is the caller's choice and is never ``/``: ``attach`` passes the
    seat's own home, which is where the workload is read from through
    ``/proc/<pid>/root``.

    Returns the lines to print. Anything that went wrong but left the seat
    usable is a line rather than an exception — a missing ``launch.json`` costs
    an F5, whereas the excludes and the folder are what keep the seat alive.
    """
    run = runner if runner is not None else run_subprocess
    notes: list[str] = []
    configurations = _configurations(kubectl, seat, notes)
    extensions = extensions_for(configurations)

    base = f"{folder}/{VSCODE_DIR}"
    # First, and not merely early: the excludes have to be on disk before the
    # window that starts the walk.
    _merge_into(kubectl, seat, f"{base}/settings.json", merge_folder_settings, notes)
    if configurations:
        _merge_into(
            kubectl,
            seat,
            f"{base}/launch.json",
            lambda existing: merge_launch_configs(existing, configurations),
            notes,
        )
    if extensions:
        _merge_into(
            kubectl,
            seat,
            f"{base}/extensions.json",
            lambda existing: merge_extensions_json(existing, extensions),
            notes,
        )

    authority = remote_authority(alias)
    for extension in extensions:
        result = run([editor, "--remote", authority, "--install-extension", extension])
        if result.returncode != 0:
            notes.append(f"could not install {extension}: {_detail(result.stderr)}")
            continue
        notes.append(f"installed {extension} in SSH: {alias}")
    if extensions:
        notes.append(f"({_STORAGE_NOTE})")

    result = run([editor, "--remote", authority, folder])
    if result.returncode != 0:
        raise EditorError(
            f"`{editor} --remote {authority} {folder}` failed: "
            f"{_detail(result.stderr)}. --remote needs the Remote - SSH "
            "extension (ms-vscode-remote.remote-ssh) in the local VS Code, and "
            "an alias ssh itself resolves - `podbench doctor --fix` adds the "
            "Include line that makes podbench's stanzas visible."
        )
    notes.append(f"opened {folder} in VS Code (Remote-SSH: {alias})")
    return notes


def _detail(stderr: str) -> str:
    """The last thing a failed command said, on one line."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else "it said nothing"


def _configurations(
    kubectl: Kubectl, seat: ContainerRef, notes: list[str]
) -> list[dict[str, Any]]:
    """What ``debug-config`` would write, asked for rather than recomputed.

    A target no debugger fits is not a failure of ``--open``: the folder, the
    excludes and the terminals are the rest of the seat, and every mechanism
    that said no was already named on ``debug-config``'s stderr.
    """
    result = kubectl.exec_(
        seat.pod.name, list(DEBUG_CONFIG_ARGV), container=seat.container, check=False
    )
    if result.returncode != 0:
        notes.append(f"no launch.json: {_detail(result.stderr)}")
        return []
    document: Any
    try:
        document = json.loads(result.stdout)
    except ValueError as error:
        notes.append(f"no launch.json: debug-config printed no JSON ({error})")
        return []
    if not isinstance(document, dict):
        notes.append("no launch.json: debug-config printed no JSON object")
        return []
    raw: Any = as_dict(document).get("configurations")
    entries = cast("list[Any]", raw) if isinstance(raw, list) else []
    return [as_dict(entry) for entry in entries if isinstance(entry, dict)]


def _merge_into(
    kubectl: Kubectl,
    seat: ContainerRef,
    path: str,
    merge: Callable[[str | None], str | None],
    notes: list[str],
) -> None:
    """Apply ``merge`` to the seat's copy of ``path``, adding never replacing.

    A refusal to parse is reported and the file left alone, which is
    :func:`podbench.vscode.merge_machine_settings`'s rule and for its reason:
    VS Code permits comments in these files and :mod:`json` does not, so
    rewriting one would discard whatever this parser could not see.
    """
    try:
        text = merge(_read(kubectl, seat, path))
    except ValueError as error:
        notes.append(f"{path} left exactly as it is: {error}")
        return
    if text is None:
        notes.append(f"{path} already says everything podbench would")
        return
    _write(kubectl, seat, path, text)
    notes.append(f"wrote {path}")


def _read(kubectl: Kubectl, seat: ContainerRef, path: str) -> str | None:
    """The seat's copy of ``path``, or ``None`` if it has none."""
    result = kubectl.exec_(
        seat.pod.name, ["cat", path], container=seat.container, check=False
    )
    return result.stdout if result.returncode == 0 else None


def _write(kubectl: Kubectl, seat: ContainerRef, path: str, text: str) -> None:
    """Put ``text`` at ``path`` in the seat, creating its directory.

    The content goes over stdin rather than into the command line: these are
    JSON documents, and an argv that has to survive both this shell and
    ``kubectl``'s own handling is a quoting bug waiting for the first path with
    a quote in it. Note what the shell does *not* do — no redirection of stderr,
    which would tear down the CRI exec stream and truncate the write with a zero
    exit (report 3.1).
    """
    directory = path.rsplit("/", 1)[0]
    kubectl.exec_(
        seat.pod.name,
        ["sh", "-c", f"mkdir -p {quote(directory)} && cat > {quote(path)}"],
        container=seat.container,
        stdin=text,
    )
