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
from collections.abc import Callable, Sequence
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
    "DEFAULT_SSH",
    "EXTENSIONS_DIR",
    "SSH_CONNECT_TIMEOUT",
    "EditorError",
    "PROVISION_FLAG",
    "check_reachable",
    "open_seat",
    "remote_authority",
    "resolve_editor",
    "unpacked_extensions",
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
    "--provision: the seat will make the target debuggable - installing debugpy "
    "into it if it cannot import one, then starting the server so the emitted "
    "configuration has something to connect to. Starting it ptraces the app, "
    "which stops answering probes until the attach returns (a few seconds; the "
    "deadlines printed with the report above are the budget). " + "; ".join(CAVEATS)
)
"""Said before the exec, not after: the install is a uv resolve and download.

The costs are :data:`podbench.provision.CAVEATS` itself rather than a retelling,
so the sentence the laptop prints cannot drift from the one the seat prints. The
probe deadlines are *pointed at* rather than repeated, for the reason the
vscode-in-a-seat skill gives: :mod:`podbench.budget` computes them from the pod
spec and ``attach`` has already printed them, and a second hand-written copy is
a second thing to keep true.
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

_RELOAD_NOTE = (
    "if a VS Code window was already connected to this alias, reload it now "
    "(Command Palette -> Developer: Reload Window). --install-extension only "
    "unpacks into the seat's ~/.vscode-server; the extension host that window "
    "started does not pick that up, so the debug adapter stays unregistered "
    "and its launch.json entry cannot run. A window this command opens for the "
    "first time is fine - the line above checked the seat, so the unpacking has "
    "provably already happened by then."
)
"""The step a *second* ``--open`` needs and a first one does not.

Measured in the seat on 2026-08-16: the extension host started at 16:53 and
never restarted, and `ms-python.debugpy` was unpacked into
``~/.vscode-server/extensions`` at 17:33 — on disk, installed successfully, and
not running. Nothing in VS Code says so from the remote side, and the symptom is
the debugger simply not being there.

Said whenever anything was installed rather than only when a window is known to
be open, because this side cannot tell: ``code --install-extension`` exits 0 for
"already installed", and the desktop ``code`` returns as soon as a window has
the argv.
"""

_STORAGE_NOTE = (
    "these unpack into the seat's ~/.vscode-server, which in Observe mode is on "
    "the workload's ephemeral-storage budget - an ephemeral container may not "
    "declare resources of its own (report 3.9), and a server plus one extension "
    "measured 1215 MiB live. VS Code resolves each one's dependencies too, so "
    "ms-python.python brings Pylance and vscode-python-envs with it, measured "
    "in spike s2"
)


DEFAULT_SSH = "ssh"
"""The client ``--open`` proves the seat with, and the one VS Code will use."""

SSH_CONNECT_TIMEOUT = 15
"""Seconds ssh may spend establishing the connection, matching what Remote-SSH
gives its own probe. The ProxyCommand is a ``kubectl exec``, so this covers an
apiserver that is slow as well as one that is unreachable."""

_PROBE_COMMAND = "true"
"""What the probe runs in the seat. It has to run *something*: authentication,
the seat's login name and sshd's config file are all resolved before a command
is dispatched, so anything that exits 0 proves the whole path."""

UNREACHABLE_CAUSES = """\
  - `Could not resolve hostname`: ssh never read podbench's stanza. The
    Include line is missing from ~/.ssh/config, or sits below a `Host *`
    block. `podbench doctor --fix`
  - `agent refused operation`: your ssh agent holds that key and will not
    sign for it - nothing in the pod is involved. `IdentityAgent none` in a
    `Host podbench-*` block, below the Include line
  - `sshd_config: No such file or directory`: the seat has no ssh transport,
    because its agent never wrote one. `podbench attach --new` lands a fresh
    seat; the exec helpers work on this one meanwhile
  - `Permission denied (publickey)`: this seat's authorized_keys was written
    when it started and does not carry the key in the stanza. `podbench
    attach --new` is the only way to change it"""


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


def check_reachable(
    alias: str,
    *,
    ssh: str = DEFAULT_SSH,
    runner: Runner | None = None,
) -> None:
    """Prove the seat answers on ``alias``, or refuse to start VS Code.

    Remote-SSH cannot be asked whether it will work. ``code --remote`` returns
    as soon as a window has the argv, so the connection — and every way it can
    fail — happens in the GUI afterwards, where the only trace is the Remote-SSH
    log the user has to know to open. In between, ``--open`` bootstraps
    vscode-server in the seat, so the cost of finding out the hard way is
    several minutes and a 214 MiB download that also fails.

    One ``ssh <alias> true`` settles it, and settles all of it: ssh resolves the
    alias through the config it will use, runs the ProxyCommand, authenticates
    with the key in the stanza and asks sshd for a command. Everything
    Remote-SSH needs is exercised, and it is exercised *by* the ssh binary
    Remote-SSH itself will spawn.

    Deliberately not ``BatchMode=yes``. A passphrase-protected key with no agent
    prompts here and succeeds, which is the truth — VS Code would have prompted
    too — whereas BatchMode would turn it into a refusal, and a preflight whose
    false negatives block a working setup is worse than none. The connection is
    left multiplexed for the window that follows, so it also costs the editor's
    first connect nothing.
    """
    run = runner if runner is not None else run_subprocess
    result = run(
        [
            ssh,
            "-T",
            "-o",
            f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
            alias,
            _PROBE_COMMAND,
        ]
    )
    if result.returncode == 0:
        return
    raise EditorError(
        f"--open: `ssh {alias}` does not reach the seat, so VS Code was not "
        "started - a Remote-SSH window would have failed the same way, minutes "
        "and one vscode-server download later. ssh said:\n"
        f"{_quoted(result.stderr or result.stdout)}\n"
        f"{UNREACHABLE_CAUSES}\n"
        "The seat itself is landed and the kubectl exec helpers above work "
        "regardless; this is the ssh half of it."
    )


def _quoted(output: str) -> str:
    """Another program's stderr, indented so it cannot be read as ours."""
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    return "\n".join(f"    {line}" for line in lines) or "    (nothing)"


def open_seat(
    kubectl: Kubectl,
    seat: ContainerRef,
    *,
    alias: str,
    folder: str,
    report: Callable[[str], None],
    editor: str = DEFAULT_EDITOR,
    provision: bool = False,
    ssh: str = DEFAULT_SSH,
    runner: Runner | None = None,
) -> None:
    """Configure ``folder`` in the seat, install what it needs, and open it.

    Nothing is written, downloaded or launched until :func:`check_reachable`
    has proven the alias: the two steps that follow it both report success on a
    seat ssh cannot reach.

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
    # Before anything is written, installed or downloaded: everything below
    # this line travels over the alias, and the two steps that do - the
    # extension install and the window itself - both report success without it
    # (`code --install-extension` exits 0 having only queued the work for a
    # window that has not connected yet). --provision is here too: it writes
    # ~15 MB into the workload and ptraces it, which is not worth spending on a
    # seat no editor can reach.
    check_reachable(alias, ssh=ssh, runner=run)
    report(f"`ssh {alias}` reaches the seat, so Remote-SSH will too")
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
    # Whether anything actually landed, not whether anything was asked for: the
    # reload note asserts that `--install-extension` unpacked something into the
    # seat, and a run whose every install failed unpacked nothing. Telling
    # somebody to reload a window over that sends them to look for an extension
    # the lines above have just said is not there.
    attempted = False
    for extension in extensions:
        result = run([editor, "--remote", authority, "--install-extension", extension])
        if result.returncode != 0:
            report(f"could not install {extension}: {_detail(result.stderr)}")
            continue
        attempted = True
    if attempted:
        # The exit code above proves nothing, so the seat is asked. See
        # `unpacked_extensions` — this is the check whose absence let a DLS run
        # print "is installed" for three extensions that were not.
        installed, missing = _verify_installed(
            alias, extensions, ssh=ssh, runner=run, report=report
        )
        if installed:
            report(_RELOAD_NOTE)
        if missing:
            report(_MISSING_REMEDY.format(missing=", ".join(missing), alias=alias))

    result = run([editor, "--remote", authority, folder])
    if result.returncode != 0:
        raise EditorError(
            f"`{editor} --remote {authority} {folder}` failed: "
            f"{_detail(result.stderr)}. --remote needs the Remote - SSH "
            "extension (ms-vscode-remote.remote-ssh) in the local VS Code; the "
            "alias itself was proven above."
        )
    report(f"asked VS Code to open {folder} over Remote-SSH ({alias})")
    # The exit code still is not evidence - `code` hands the argv to a window
    # and returns, and the connection is made in that window afterwards - but
    # the preflight has already removed every cause that lives outside VS Code,
    # so what is left to say is small and specific.
    report(
        "if that window says 'could not establish connection', the local VS "
        "Code has no Remote - SSH extension (ms-vscode-remote.remote-ssh): ssh "
        f"itself reached {alias} a moment ago, from this terminal, with the "
        "same config the window reads."
    )


EXTENSIONS_DIR = "~/.vscode-server/extensions"
"""Where vscode-server unpacks an extension, under the *ssh login's* home.

``~`` rather than a path, and asked over ssh rather than over ``kubectl exec``,
because the home that matters is the one NSS gives the seat's login user — which
is not always the container's ``$HOME`` (see :func:`podbench.agent.session_home`).
Asking through the same client, as the same user, is the only spelling that
cannot be right here and wrong for VS Code.
"""

_MISSING_REMEDY = (
    "{missing} did not land in the seat, whatever `code` said: nothing matching "
    "is unpacked under {dir}. Install it from the Extensions view of the window "
    "below - the button must read 'Install in SSH: {alias}', because a local "
    "install runs the debug adapter on this machine, where the /proc/<pid>/root "
    "paths in launch.json do not exist and the failure reads as a bad "
    "configuration ('program path is missing or invalid')."
).replace("{dir}", EXTENSIONS_DIR)
"""Said when the seat disagrees with ``code``'s exit code. The local-install
trap is spelled out because it is where somebody sent to the Extensions view by
this very line then lands: both buttons are there, and only one of them works."""


def unpacked_extensions(
    alias: str,
    *,
    ssh: str = DEFAULT_SSH,
    runner: Runner | None = None,
) -> set[str] | None:
    """Extension ids unpacked in the seat, lowercased. ``None`` if unknowable.

    ``code --install-extension --remote`` exits 0 whether it installed anything,
    found it already installed, or never reached the remote at all — the third
    of which is what a DLS run hit on 2026-08-16, printing success for
    extensions that were not there. The seat's own directory listing is the only
    statement about the seat, so it is the one worth making.

    ``None`` and ``set()`` are different answers: an ssh that failed knows
    nothing, while an empty directory knows that nothing landed.

    The directory name carries the version — ``ms-vscode.cpptools-1.19.9`` — so
    the ids are the part before the version, which is what
    :func:`_verify_installed` matches on rather than comparing whole names.
    """
    run = runner if runner is not None else run_subprocess
    result = run(
        [
            ssh,
            "-T",
            "-o",
            f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
            alias,
            # `|| true` so an absent directory - nothing was ever installed - is
            # an empty answer rather than an unknown one. `ls` alone exits 2 for
            # that, which is indistinguishable here from ssh's own failures.
            f"ls -1 {EXTENSIONS_DIR} 2>/dev/null || true",
        ]
    )
    if result.returncode != 0:
        return None
    return {line.strip().lower() for line in result.stdout.splitlines() if line.strip()}


def _verify_installed(
    alias: str,
    extensions: Sequence[str],
    *,
    ssh: str,
    runner: Runner,
    report: Callable[[str], None],
) -> tuple[list[str], list[str]]:
    """Split ``extensions`` into those the seat has and those it does not."""
    unpacked = unpacked_extensions(alias, ssh=ssh, runner=runner)
    if unpacked is None:
        # The alias worked minutes ago, so this is a transient rather than a
        # verdict: say what is unproven and claim nothing either way.
        report(
            f"could not list {EXTENSIONS_DIR} in the seat, so whether "
            f"{', '.join(extensions)} landed is unverified. If the debugger is "
            "not in the Run and Debug list, check the Extensions view"
        )
        return [], []
    installed: list[str] = []
    missing: list[str] = []
    for extension in extensions:
        # Prefix match: the directory carries a version and a platform triple,
        # and an exact comparison would report every installed extension missing.
        if any(name.startswith(f"{extension.lower()}-") for name in unpacked):
            report(f"{extension} is unpacked in SSH: {alias}")
            installed.append(extension)
        else:
            missing.append(extension)
    return installed, missing


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
