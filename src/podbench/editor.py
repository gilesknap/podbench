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

That last point is also why the seat's ``stderr`` is relayed line by line rather
than summarised. ``debug-config`` is the only thing here that can see the
target, so its narration *is* the diagnosis — it ends "every mechanism that said
no is named above", and a caller that keeps the last line alone points at output
it has just thrown away. It carries the injection command too, which a written
``launch.json`` needs and cannot state: the configuration is emitted once the
prerequisites are met, and nothing is listening until that command is run.

``--provision`` is a pass-through to the same verb and not a default, which is
issue #45's decision: writing ~15 MB into the workload's writable layer, on a
budget the seat shares and cannot reserve, is the larger of the two mutations a
config author must be asked for — the other being the ptrace
``flavour.injection_command`` prints rather than runs. What the flag fixes is
that the remedy was previously unreachable from the laptop at all.

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
from .model import SEAT_HOME_VOLUME, ContainerRef, as_dict
from .provision import CAVEATS
from .vscode import (
    extensions_for,
    merge_extensions_json,
    merge_folder_settings,
    merge_launch_configs,
)

__all__ = [
    "DEFAULT_EDITOR",
    "EditorError",
    "PROVISION_FLAG",
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

PROVISION_FLAG = "--provision"
"""What ``--open --provision`` appends to :data:`DEBUG_CONFIG_ARGV`.

Also the string the *unprovisioned* refusal is recognised by. ``debug-config``
names this flag in its own remedy when debugpy is the blocker and does not name
it for any other flavour — there is no ``--provision`` for a missing delve — so
matching on it offers the pass-through exactly where it is the answer, without
this side of the wire guessing the target's language a second time.
"""

_REMOTE_CLI_MARKERS = ("/remote-cli/", "/.vscode-server/", "/.vscode-server-insiders/")
"""What a ``code`` that is not the desktop one looks like on disk.

VS Code puts ``<server>/bin/remote-cli`` on the PATH of a remote window's
integrated terminal, so ``shutil.which`` finds *that* whenever podbench is run
from inside a Remote-SSH window, a devcontainer or a Codespace — this repo's own
workflow being a devcontainer. It forwards to the window it belongs to over
``VSCODE_IPC_HOOK_CLI``, which is the one thing ``--open`` must not do: the
extensions would land on the machine the user is already on, and a seat with the
adapter installed anywhere but in it is the silent "looks like a bad
launch.json" failure this whole module exists to prevent.

Matched on the resolved path rather than on ``VSCODE_IPC_HOOK_CLI``, which is
also set in a *local* window's terminal, where ``code`` is the desktop CLI and
everything here works.
"""

_ABSENT = 3
"""Exit code :func:`_read`'s script uses for "there is no such file".

Not 1: that is what ``cat`` itself exits with when it *found* the file and could
not read it, which is the case this whole arrangement exists to tell apart. 2
and 127 belong to ``sh``.
"""

_PROVISION_NOTICE = (
    "--provision: the seat will install debugpy into the target if the target "
    "cannot import one, since the injection bootstrap runs in the target's own "
    "interpreter. " + "; ".join(CAVEATS)
)
"""Said before the exec, not after: the install is a uv resolve and download.

The costs are :data:`podbench.provision.CAVEATS` itself rather than a retelling,
so the sentence the laptop prints cannot drift from the one the seat prints.
"""

_PROVISION_REMEDY = (
    "re-run with `--open --provision` to install it from the seat first. That "
    "is opt-in because it mutates the workload - see the costs the seat named "
    "above - and it is undone by any restart of the target container."
)
"""The pass-through, offered only where the seat itself named the flag.

Issue #45 settled that provisioning is not implicit; what was missing is that
the flag lived on the in-pod verb alone, so from a laptop the remedy could be
read and not reached.
"""

_STORAGE_NOTE = (
    "these unpack into the seat's ~/.vscode-server, which in Observe mode is on "
    "the workload's ephemeral-storage budget - an ephemeral container may not "
    "declare resources of its own (report 3.9), and a server plus one extension "
    "measured 1215 MiB live. VS Code resolves each one's dependencies too, so "
    "ms-python.python brings Pylance and vscode-python-envs with it, measured "
    "in spike s2"
)


class EditorError(RuntimeError):
    """``--open`` cannot be honoured, and the sentence says which mechanism.

    A named error rather than a traceback because every cause here is something
    the user can fix in one step — install the CLI, add the ``Include`` line,
    land a seat that can log in — and a traceback names none of them.
    """


def resolve_editor(which: Callable[[str], str | None] = shutil.which) -> str:
    """Where the *desktop* VS Code CLI is, or a refusal naming the mechanism.

    Resolved *before* the seat is landed. An ephemeral container's name is
    permanent, so a run that was always going to end at "no ``code``" must not
    spend one first.
    """
    found = which(DEFAULT_EDITOR)
    if found is None:
        raise EditorError(
            f"--open needs the VS Code CLI (`{DEFAULT_EDITOR}`) on PATH and "
            "there is none. From VS Code: Command Palette -> 'Shell Command: "
            "Install 'code' command in PATH'. That command has nothing to offer "
            "a flatpak install, which cannot reach the host PATH from its "
            "sandbox, and the forks (`cursor`, `codium`, `windsurf`) take the "
            "same flags but have no flag of their own here yet. From any of "
            "them, drop --open and use Remote-SSH: Connect to Host on the alias "
            "podbench prints."
        )
    if any(marker in found for marker in _REMOTE_CLI_MARKERS):
        raise EditorError(
            f"--open found `{found}`, which is VS Code's *remote* CLI rather "
            "than the desktop one: it is what a Remote-SSH window, a "
            "devcontainer or a Codespace puts on the PATH of its integrated "
            "terminal, and it talks to the window this terminal is already in. "
            "--install-extension there installs into *this* machine and not "
            "into the seat, so the seat would get its .vscode files, no "
            "extensions, and breakpoints that never bind - the failure this "
            "flag exists to prevent. Run podbench from a terminal on the "
            "machine your VS Code itself runs on, or drop --open and use "
            "Remote-SSH: Connect to Host on the alias podbench prints."
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
    report: Callable[[str], None],
    editor: str = DEFAULT_EDITOR,
    provision: bool = False,
    runner: Runner | None = None,
) -> None:
    """Configure ``folder`` in the seat, install what it needs, and open it.

    ``folder`` is the caller's choice — ``attach`` passes the seat's own home,
    which is where the workload is read from through ``/proc/<pid>/root`` — and
    it is checked here rather than assumed, because it is the one argument whose
    wrong value is unrecoverable and it is not a constant: the home follows a
    ``podbench-home`` mount, and ``--mount podbench-home:/`` is a spelling of
    that a user can reach.

    ``report`` is called with each line **as it becomes true**, not with a list
    at the end. The install is the reason: it bootstraps vscode-server in the
    seat — a 214 MiB download and a 5.62 s extract measured with egress (report
    3.8) — with its output captured for the failure message, so a namespace with
    no route to ``update.code.visualstudio.com`` is otherwise minutes of nothing
    at all, indistinguishable from a hang.

    ``provision`` is handed straight to ``debug-config`` and is the only argument
    here that changes the *target*: it installs debugpy into the workload when
    the workload cannot import one. Off by default for issue #45's reason, given
    in the module docstring.

    Anything that went wrong but left the seat usable is a line rather than an
    exception — a missing ``launch.json`` costs an F5, whereas the excludes and
    the folder are what keep the seat alive.
    """
    if not folder.startswith("/") or folder.strip("/") == "":
        raise EditorError(
            f"--open will not open `{folder}` as a folder. It has to be an "
            "absolute path and it cannot be `/`: a folder at the root walks "
            "/proc/<pid>/root, which is a symlink into another container's "
            "rootfs, so the walk has no bottom and OOMs a seat that cannot be "
            "restarted. This is the seat's $HOME - `--mount "
            f"{SEAT_HOME_VOLUME}:<path>` is what moves it."
        )
    run = runner if runner is not None else run_subprocess
    configurations = _configurations(kubectl, seat, report, provision=provision)
    extensions = extensions_for(configurations)

    base = f"{folder}/{VSCODE_DIR}"
    # First, and not merely early: the excludes have to be on disk before the
    # window that starts the walk.
    _merge_into(kubectl, seat, f"{base}/settings.json", merge_folder_settings, report)
    if configurations:
        _merge_into(
            kubectl,
            seat,
            f"{base}/launch.json",
            lambda existing: merge_launch_configs(existing, configurations),
            report,
        )
    if extensions:
        _merge_into(
            kubectl,
            seat,
            f"{base}/extensions.json",
            lambda existing: merge_extensions_json(existing, extensions),
            report,
        )

    authority = remote_authority(alias)
    if extensions:
        report(
            f"installing {', '.join(extensions)} in SSH: {alias} - the first "
            f"one bootstraps vscode-server in the seat, so this is a download "
            f"and not just a copy ({_STORAGE_NOTE})"
        )
    for extension in extensions:
        result = run([editor, "--remote", authority, "--install-extension", extension])
        if result.returncode != 0:
            report(f"could not install {extension}: {_detail(result.stderr)}")
            continue
        # "is installed", not "installed": `code` exits 0 for "already
        # installed" too, and this run cannot tell the two apart.
        report(f"{extension} is installed in SSH: {alias}")

    result = run([editor, "--remote", authority, folder])
    if result.returncode != 0:
        raise EditorError(
            f"`{editor} --remote {authority} {folder}` failed: "
            f"{_detail(result.stderr)}. --remote needs the Remote - SSH "
            "extension (ms-vscode-remote.remote-ssh) in the local VS Code, and "
            "an alias ssh itself resolves - `podbench doctor --fix` adds the "
            "Include line that makes podbench's stanzas visible."
        )
    report(f"asked VS Code to open {folder} over Remote-SSH ({alias})")
    # Said rather than implied by an exit code, because the exit code is not
    # evidence: the desktop `code` hands the argv to a window and returns, and
    # the authority is resolved in that window afterwards. So the two failures
    # a first run actually meets - no Remote - SSH extension locally, and an
    # alias ssh cannot resolve because ~/.ssh/config never got the Include line
    # - both arrive as a dialog there and as a zero here.
    report(
        "that exit code is not evidence the seat was reached: `code` returns "
        "as soon as the window has the argv, and the connection is made in the "
        "window. 'could not establish connection' there means the local VS Code "
        "has no Remote - SSH extension (ms-vscode-remote.remote-ssh), or ssh "
        "cannot resolve the alias - the Include line above, or `podbench "
        "doctor --fix`."
    )


def _detail(stderr: str) -> str:
    """The last thing a failed command said, on one line."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else "it said nothing"


def _relay(stderr: str, report: Callable[[str], None]) -> bool:
    """Pass the seat's own narration through, one line per call.

    Per line rather than as one block because ``report`` is a paragraph
    formatter on the launcher side: it re-wraps on whitespace, so a multi-line
    note handed over whole comes back with its newlines gone — and one of these
    notes is the two-line injection command, whose first line ends in a
    continuation ``\\`` that only means anything at the end of a line.

    Returns whether anything was relayed, which is what tells "the seat
    explained itself and the answer was no" from "the seat never ran".
    """
    lines = [line.rstrip() for line in stderr.splitlines() if line.strip()]
    for line in lines:
        report(line)
    return bool(lines)


def _configurations(
    kubectl: Kubectl,
    seat: ContainerRef,
    report: Callable[[str], None],
    *,
    provision: bool = False,
) -> list[dict[str, Any]]:
    """What ``debug-config`` would write, asked for rather than recomputed.

    A target no debugger fits is not a failure of ``--open``: the folder, the
    excludes and the terminals are the rest of the seat, and every mechanism
    that said no was named on ``debug-config``'s stderr — which is relayed here
    whether or not a configuration came back with it, since on success it also
    carries the injection command an emitted debugpy entry needs before anything
    is listening for it.
    """
    argv = [*DEBUG_CONFIG_ARGV, *([PROVISION_FLAG] if provision else [])]
    if provision:
        report(_PROVISION_NOTICE)
    result = kubectl.exec_(seat.pod.name, argv, container=seat.container, check=False)
    relayed = _relay(result.stderr, report)
    if result.returncode != 0:
        # The last line only when there was nothing to relay - a `podbench` the
        # image does not resolve exits 127 with sh's message and no narration,
        # and that message is the whole diagnosis.
        report(
            "no launch.json: nothing above could be turned into one"
            if relayed
            else f"no launch.json: {_detail(result.stderr)}"
        )
        if not provision and PROVISION_FLAG in result.stderr:
            report(_PROVISION_REMEDY)
        return []
    document: Any
    try:
        document = json.loads(result.stdout)
    except ValueError as error:
        report(f"no launch.json: debug-config printed no JSON ({error})")
        return []
    if not isinstance(document, dict):
        report("no launch.json: debug-config printed no JSON object")
        return []
    raw: Any = as_dict(document).get("configurations")
    entries = cast("list[Any]", raw) if isinstance(raw, list) else []
    return [as_dict(entry) for entry in entries if isinstance(entry, dict)]


def _merge_into(
    kubectl: Kubectl,
    seat: ContainerRef,
    path: str,
    merge: Callable[[str | None], str | None],
    report: Callable[[str], None],
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
        report(f"{path} left exactly as it is: {error}")
        return
    if text is None:
        report(f"{path} already says everything podbench would")
        return
    _write(kubectl, seat, path, text)
    report(f"wrote {path}")


def _read(kubectl: Kubectl, seat: ContainerRef, path: str) -> str | None:
    """The seat's copy of ``path``, or ``None`` if it has none.

    The ``test`` is what separates the two, and a bare ``cat`` cannot: it exits
    non-zero both for a file that is not there and for one it could not read,
    and reading the second as the first turns :func:`_merge_into`'s merge into a
    replacement of whatever the seat was already carrying. Anything else is
    raised rather than guessed at.
    """
    result = kubectl.exec_(
        seat.pod.name,
        ["sh", "-c", f"test -e {quote(path)} || exit {_ABSENT}; cat {quote(path)}"],
        container=seat.container,
        check=False,
    )
    if result.returncode == _ABSENT:
        return None
    if result.returncode != 0:
        raise EditorError(
            f"cannot read {path} in the seat: {_detail(result.stderr)}. --open "
            "adds to that file rather than replacing it, so one it cannot read "
            "stops the run instead of being overwritten with podbench's own "
            "copy."
        )
    return result.stdout


def _write(kubectl: Kubectl, seat: ContainerRef, path: str, text: str) -> None:
    """Put ``text`` at ``path`` in the seat, creating its directory.

    The content goes over stdin rather than into the command line: these are
    JSON documents, and an argv that has to survive both this shell and
    ``kubectl``'s own handling is a quoting bug waiting for the first path with
    a quote in it. Note what the shell does *not* do — no redirection of stderr,
    which would tear down the CRI exec stream and truncate the write with a zero
    exit (report 3.1).

    A refusal ends ``--open`` with a sentence rather than with ``kubectl``'s
    argv, and ends it *before* the folder opens: the first of these files is the
    exclude list, and a window opened without it is the walk that OOMs a seat
    which cannot be restarted.
    """
    directory = path.rsplit("/", 1)[0]
    result = kubectl.exec_(
        seat.pod.name,
        ["sh", "-c", f"mkdir -p {quote(directory)} && cat > {quote(path)}"],
        container=seat.container,
        stdin=text,
        check=False,
    )
    if result.returncode != 0:
        raise EditorError(
            f"cannot write {path} in the seat: {_detail(result.stderr)}. A "
            "read-only root filesystem, or a home owned by a uid this seat does "
            "not run as, is the usual cause - `--mount` a writable volume, or "
            "land a seat whose home it can write. Nothing is opened: without "
            f"{VSCODE_DIR}/settings.json the window walks /proc/<pid>/root and "
            "ends the seat."
        )
