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
  --install-extension`` looks like that button as a flag and is not: it answers
  from the *laptop's* install list and short-circuits on anything held there,
  so it is attempted, verified, and then made good by the seat's own
  ``code-server`` — which is that button, being the code path it runs.

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

It answers **two** blockers, and reading it as one is what left this verb unable
to finish its own job. A Python target that cannot import debugpy gets no
configuration; a Python target that can — which is most of them, since an app
with debugpy in its lockfile is the common shape — gets one that connects to a
port nothing is listening on. Only the first is a non-zero exit from the seat,
so keying on the exit code meant the second silently skipped provisioning and
handed over a launch.json whose F5 met ``ECONNREFUSED``. The trigger is now the
seat naming the flag, which it does for either.

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

import enum
import json
import shutil
import time
from collections.abc import Callable, Sequence
from shlex import quote
from typing import Any, cast

from .kubectl import (
    DEFAULT_CALL_TIMEOUT,
    UNBOUNDED,
    Kubectl,
    Runner,
    run_subprocess,
)
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
    "SERVER_CLI_ATTEMPTS",
    "SERVER_CLI_INTERVAL",
    "Provision",
    "check_reachable",
    "open_seat",
    "remote_authority",
    "resolve_editor",
    "unpacked_extensions",
]

DEFAULT_EDITOR = "code"
"""The only editor ``podbench vscode`` drives.

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
"""What a provisioning run appends to :data:`DEBUG_CONFIG_ARGV`.

Also the string an *unprovisioned* run is recognised by. ``debug-config`` names
this flag in its own narration when debugpy is the blocker and does not name it
for any other flavour — there is no ``--provision`` for a missing delve — so
matching on it offers the pass-through exactly where it is the answer, without
this side of the wire guessing the target's language a second time.

"Blocker" covers both of debugpy's, and deliberately: the flag is named by the
refusal a target that cannot import debugpy gets, *and* by the hint a target
that can import it gets when nothing is listening on the port its configuration
connects to. One string, because to this side of the wire they are one request.
"""


class Provision(enum.Enum):
    """Whether this run may make the target debuggable, and on whose say-so.

    Three states rather than a boolean because the *decision* and the *consent*
    are separate facts, and only the seat holds the first one. ``debug-config``
    is the one thing that can see the target, and it names
    :data:`PROVISION_FLAG` in its own narration when — and only when — debugpy
    is what stands between this target and a bound breakpoint. So a run that
    already has consent can answer the need itself instead of printing it.

    :attr:`NEVER` and :attr:`ALWAYS` are what ``attach --open`` had.
    :attr:`IF_NEEDED`
    is what a verb named for the editor can offer: choosing ``podbench vscode``
    *is* asking for a debuggable target, whereas ``attach``'s contract is that
    it touches the workload not at all — which is why this is a mode and not a
    new default.
    """

    NEVER = "never"
    """Author whatever fits the target as it stands, and offer the flag."""

    IF_NEEDED = "if-needed"
    """Provision only where the seat itself named the flag.

    Not "only where no configuration came back": a target shipping its own
    debugpy gets one, and it connects to a port nothing is listening on until
    the injection runs. That is the same request, made by a run that exited 0.
    """

    ALWAYS = "always"
    """Provision first, whether or not the target turns out to need it."""


_REMOTE_CLI_MARKERS = ("/remote-cli/", "/.vscode-server/", "/.vscode-server-insiders/")
"""What a ``code`` that is not the desktop one looks like on disk.

VS Code puts ``<server>/bin/remote-cli`` on the PATH of a remote window's
integrated terminal, so ``shutil.which`` finds *that* whenever podbench is run
from inside a Remote-SSH window, a devcontainer or a Codespace — this repo's own
workflow being a devcontainer. It forwards to the window it belongs to over
``VSCODE_IPC_HOOK_CLI``, which is the one thing this verb must not do: the
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

PROVISION_TIMEOUT = 600.0
"""How long a provisioning ``debug-config`` may take before it is killed.

Twenty times :data:`podbench.kubectl.DEFAULT_CALL_TIMEOUT`, because this one
``exec`` contains a uv resolve, a download and a gdb injection — work whose
honest duration is minutes on a cold index, and which
:data:`_PROVISION_NOTICE` announces for exactly that reason. The bound is here
so that a resolve against an index with *no route* ends in a sentence instead of
in the hang the notice was written to make bearable.
"""

_PROVISION_NOTICE = (
    "--provision: installing debugpy into the target and starting its server, "
    "which ptraces the app for a few seconds (deadline under `supports` above). "
    + "; ".join(CAVEATS)
)
"""Said before the exec, not after: the install is a uv resolve and download.

The costs are :data:`podbench.provision.CAVEATS` itself rather than a retelling,
so the sentence the laptop prints cannot drift from the one the seat prints. The
probe deadline is *pointed at* rather than repeated, for the reason the
vscode-in-a-seat skill gives: :mod:`podbench.budget` computes it from the pod
spec and ``attach`` has already printed it, and a second hand-written copy is
a second thing to keep true.
"""

_PROVISION_REMEDY = (
    "re-run without `--no-provision` to install it from the seat first - it is "
    "what this verb does by default, and an opt-out rather than an opt-in "
    "because it mutates the workload with the costs named above."
)
"""The offer, reached now only by somebody who declined the default.

Issue #45 settled that provisioning is not implicit, and it still is not: what
changed is where the consent is given. It used to be a flag typed beside
``--open`` on a verb whose contract is that it touches the workload not at all;
it is now the name of the verb, which is why the only reader left here is one
who said ``--no-provision`` and then met the refusal it leads to.
"""

_PROVISION_NEEDED = (
    "debugpy is what stands between this target and a bound breakpoint, which "
    "is what the seat named `--provision` for above - either it cannot import "
    "debugpy at all, or it can and nothing is listening on the port the "
    "configuration connects to; provisioning it now, because that is what this "
    "verb is for. `--no-provision` authors whatever fits the target as it "
    "stands instead."
)
"""Said instead of :data:`_PROVISION_REMEDY` when the answer is already known.

The narration it follows is relayed rather than swallowed, for the reason the
module docstring gives: ``debug-config`` is the only thing that can see the
target, so its account of *why* debugpy is the blocker is the diagnosis, and a
run that hid it would be asserting the need rather than showing it. What changes
is only the last line — "re-run with the flag" becomes "doing it now", because
:attr:`Provision.IF_NEEDED` means the flag was already typed, as the verb's own
name.

Both blockers are named because only the seat knows which one it met, and this
sentence is composed on the laptop. Naming the wrong one would be worse than
naming both: the reader has the seat's own account two lines above.
"""

_RELOAD_NOTE = (
    "a window already connected to this alias needs Command Palette -> "
    "Developer: Reload Window, or the debug adapter stays unregistered; one "
    "this command opens for the first time does not, because the line above "
    "asked the seat and it is already unpacked there."
)
"""The step a *second* run needs and a first one does not.

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
    "1215 MiB measured, on the workload's ephemeral-storage budget in Observe mode"
)
"""The size of what the install line is about to do, as a parenthesis.

The number is the whole point of saying it — it is the one that decides whether
this pod can afford a seat — and the mechanism behind it is already the report's
OOM warning a few blocks up, so repeating it here would be the third telling.
"""


DEFAULT_SSH = "ssh"
"""The client podbench proves the seat with, and the one VS Code will use."""

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
    """The editor cannot be opened, and the sentence says which mechanism.

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
            f"`podbench vscode` needs the VS Code CLI (`{DEFAULT_EDITOR}`) on PATH "
            "and "
            "there is none. From VS Code: Command Palette -> 'Shell Command: "
            "Install 'code' command in PATH'. That command has nothing to offer "
            "a flatpak install, which cannot reach the host PATH from its "
            "sandbox, and the forks (`cursor`, `codium`, `windsurf`) take the "
            "same flags but have no flag of their own here yet. From any of "
            "them, run `podbench attach` and use Remote-SSH: Connect to Host on "
            "the alias it prints."
        )
    if any(marker in found for marker in _REMOTE_CLI_MARKERS):
        raise EditorError(
            f"found `{found}`, which is VS Code's *remote* CLI rather "
            "than the desktop one: it is what a Remote-SSH window, a "
            "devcontainer or a Codespace puts on the PATH of its integrated "
            "terminal, and it talks to the window this terminal is already in. "
            "--install-extension there installs into *this* machine and not "
            "into the seat, so the seat would get its .vscode files, no "
            "extensions, and breakpoints that never bind - the failure this "
            "verb exists to prevent. Run podbench from a terminal on the "
            "machine your VS Code itself runs on, or run `podbench attach` "
            "and use Remote-SSH: Connect to Host on the alias it prints."
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
    log the user has to know to open. In between, the install bootstraps
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
        ],
        # Unbounded on purpose, and for the same reason BatchMode is off above:
        # a passphrase-protected key with no agent prompts *here*, and a bound
        # would kill the prompt while the user was typing into it. ssh's own
        # ConnectTimeout covers the failure a number can cover.
        timeout=UNBOUNDED,
    )
    if result.returncode == 0:
        return
    raise EditorError(
        f"`ssh {alias}` does not reach the seat, so VS Code was not "
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
    provision: Provision = Provision.NEVER,
    ssh: str = DEFAULT_SSH,
    runner: Runner | None = None,
    sleep: Callable[[float], None] = time.sleep,
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

    ``provision`` is the only argument here that changes the *target*: it
    installs debugpy into the workload when the workload cannot import one.
    :attr:`Provision.NEVER` by default for issue #45's reason, given in the
    module docstring — a caller that has been given consent passes
    :attr:`Provision.IF_NEEDED` and lets the seat decide whether to spend it.

    Anything that went wrong but left the seat usable is a line rather than an
    exception — a missing ``launch.json`` costs an F5, whereas the excludes and
    the folder are what keep the seat alive.
    """
    if not folder.startswith("/") or folder.strip("/") == "":
        raise EditorError(
            f"podbench will not open `{folder}` as a folder. It has to be an "
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
    # window that has not connected yet). Provisioning is behind it too: it
    # writes ~15 MB into the workload and ptraces it, which is not worth
    # spending on a seat no editor can reach.
    check_reachable(alias, ssh=ssh, runner=run)
    report(f"`ssh {alias}` reaches the seat, so Remote-SSH will too")
    # Everything from here is one line each: this is a sequence of steps, and
    # a step that explains itself in a paragraph buries the one that failed.
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
            f"installing {', '.join(extensions)} in SSH: {alias}; the first "
            f"bootstraps vscode-server, so this is a download ({_STORAGE_NOTE})"
        )
    # Whether anything actually landed, not whether anything was asked for: the
    # reload note asserts that `--install-extension` unpacked something into the
    # seat, and a run whose every install failed unpacked nothing. Telling
    # somebody to reload a window over that sends them to look for an extension
    # the lines above have just said is not there.
    attempted = False
    for extension in extensions:
        # Unbounded: the first of these bootstraps vscode-server, a 214 MiB
        # download into the seat over whatever link the cluster has, and a bound
        # that fired would leave the seat with a half-unpacked server.
        result = run(
            [editor, "--remote", authority, "--install-extension", extension],
            timeout=UNBOUNDED,
        )
        if result.returncode != 0:
            report(f"could not install {extension}: {_detail(result.stderr)}")
            continue
        attempted = True
    missing: list[str] = []
    if attempted:
        # The exit code above proves nothing, so the seat is asked. See
        # `unpacked_extensions` — this is the check whose absence let a DLS run
        # print "is installed" for three extensions that were not.
        installed, missing = _verify_installed(
            alias, extensions, ssh=ssh, runner=run, report=report
        )
        if installed:
            report(_RELOAD_NOTE)

    # Unbounded: this hands the argv to a window and returns, and on a cold
    # start it is waiting for the user's editor to exist.
    result = run([editor, "--remote", authority, folder], timeout=UNBOUNDED)
    if result.returncode != 0:
        raise EditorError(
            f"`{editor} --remote {authority} {folder}` failed: "
            f"{_detail(result.stderr)}. --remote needs the Remote - SSH "
            "extension (ms-vscode-remote.remote-ssh) in the local VS Code; the "
            "alias itself was proven above."
        )
    report(f"asked VS Code to open {folder} over Remote-SSH ({alias})")
    # Only now, and this ordering is the fix: the seat's own vscode-server does
    # not exist until a window has connected and bootstrapped it, so the
    # fallback cannot run before the line above. What it must never move ahead
    # of is `settings.json` — the watcher starts walking the moment the window
    # opens, and that race is the one whose loser is an unrecoverable seat.
    if missing:
        missing = _install_through_the_seat(
            alias, missing, ssh=ssh, runner=run, report=report, sleep=sleep
        )
    if missing:
        report(_MISSING_REMEDY.format(missing=", ".join(missing), alias=alias))
    # The exit code still is not evidence - `code` hands the argv to a window
    # and returns, and the connection is made in that window afterwards - but
    # the preflight has already removed every cause that lives outside VS Code,
    # so what is left to say is small and specific.
    report(
        "if it says 'could not establish connection', the local VS Code has no "
        "Remote-SSH extension (ms-vscode-remote.remote-ssh); ssh itself reached "
        f"{alias} a moment ago with the same config."
    )


EXTENSIONS_DIR = "~/.vscode-server/extensions"
"""Where vscode-server unpacks an extension, under the *ssh login's* home.

``~`` rather than a path, and asked over ssh rather than over ``kubectl exec``,
because the home that matters is the one NSS gives the seat's login user — which
is not always the container's ``$HOME`` (see :func:`podbench.agent.session_home`).
Asking through the same client, as the same user, is the only spelling that
cannot be right here and wrong for VS Code.
"""

SERVERS_DIR = "~/.vscode-server/cli/servers"
"""Where the seat keeps the vscode-server builds it has bootstrapped.

More than one lives here — this seat held ``Stable-6928394…`` beside
``Stable-110a328…`` — and *which* one an extension is installed through decides
whether the open window ever notices it. See :func:`_server_cli`.
"""

_SERVER_CLI_SCRIPT = r"""
servers=$HOME/.vscode-server/cli/servers
running=$(ps -eo args= 2>/dev/null |
  grep -o '/[^ ]*/cli/servers/[^/]*/server' | head -1)
lru=$(sed 's/[][",]/ /g' "$servers/lru.json" 2>/dev/null)
for candidate in "$running" $lru; do
  case "$candidate" in
    /*) bin="$candidate/bin/code-server" ;;
    ?*) bin="$servers/$candidate/server/bin/code-server" ;;
    *) continue ;;
  esac
  [ -x "$bin" ] && { echo "$bin"; exit 0; }
done
exit 1
"""
"""Find the ``code-server`` belonging to the server the window is *using*.

The running process first, because that is not a guess: the server the window is
attached to is a process in this seat, and its argv carries its own path.
``lru.json`` — a plain most-recent-first array of ids — is the fallback for the
window that has not started one yet, and it agreed with the running process when
both were measured (2026-08-21).

Picking the wrong one is not a harmless miss. An install through a server the
window is *not* attached to lands the extension on disk where that window never
sees it, which is precisely the 2026-08-16 observation :data:`_RELOAD_NOTE`
records — so a glob over :data:`SERVERS_DIR` would reintroduce the reload as an
intermittent, unexplainable one.
"""

SERVER_CLI_ATTEMPTS = 60
SERVER_CLI_INTERVAL = 5.0
"""How long to wait for the window to bootstrap a server, as attempts x seconds.

Five minutes. The bootstrap is the 1215 MiB download :data:`_STORAGE_NOTE`
names, over whatever link the cluster has, and this runs immediately after the
window was asked to open — so on a cold seat the server genuinely does not exist
yet and the wait is the normal case rather than the failure.
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
        ],
        # Same exemption as the preflight: this is a second ssh to the same
        # alias, so it can prompt for the same passphrase.
        timeout=UNBOUNDED,
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


def _server_cli(
    alias: str,
    *,
    ssh: str,
    runner: Runner,
    sleep: Callable[[float], None],
    attempts: int = SERVER_CLI_ATTEMPTS,
) -> str | None:
    """The ``code-server`` of the server this seat's window is using.

    ``None`` once :data:`SERVER_CLI_ATTEMPTS` have passed without one — which
    is a window that never connected, not a slow one, since the caller has
    already proved the alias and asked VS Code to open it.
    """
    for attempt in range(attempts):
        result = runner(
            [ssh, "-T", "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}", alias],
            stdin=_SERVER_CLI_SCRIPT,
            timeout=UNBOUNDED,
        )
        found = result.stdout.strip().splitlines() if result.returncode == 0 else []
        if found:
            return found[0].strip()
        if attempt + 1 < attempts:
            sleep(SERVER_CLI_INTERVAL)
    return None


def _install_through_the_seat(
    alias: str,
    missing: Sequence[str],
    *,
    ssh: str,
    runner: Runner,
    report: Callable[[str], None],
    sleep: Callable[[float], None],
) -> list[str]:
    """Install what the local CLI would not, and return what is still absent.

    ``code --remote <authority> --install-extension`` answers from the
    *developer's own* install list and short-circuits: an extension held
    locally is reported "already installed" and the seat is never contacted,
    with or without ``--force`` (measured at DLS, 2026-08-21 — the seat held no
    matching path anywhere on its filesystem). That fails worst for the people
    most likely to be here, since anyone who debugs Python already has the
    Python extension on their laptop.

    So the install is made where it has to land, by the server's own CLI over
    ssh. That is the same code path the "Install in SSH: <alias>" button takes,
    which is why the extension is **live in the open window with no reload** —
    also measured, and the reason no reload note is printed here.
    """
    binary = _server_cli(alias, ssh=ssh, runner=runner, sleep=sleep)
    if binary is None:
        report(
            f"{', '.join(missing)}: no vscode-server has started in the seat, "
            "so there is nothing to install through. The window above has not "
            "connected - its own error is the diagnosis"
        )
        return list(missing)
    report(
        f"installing {', '.join(missing)} through the seat's own vscode-server, "
        "because `code --install-extension` answers from this machine's "
        "install list and never reaches the seat"
    )
    for extension in missing:
        result = runner(
            [
                ssh,
                "-T",
                "-o",
                f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
                alias,
                f"{quote(binary)} --install-extension {quote(extension)}",
            ],
            timeout=UNBOUNDED,
        )
        if result.returncode != 0:
            report(
                f"could not install {extension} in the seat: {_detail(result.stderr)}"
            )
    # Asked again rather than believed: this CLI reports "already installed"
    # too, and the whole defect above was an exit code that meant nothing.
    return _verify_installed(alias, missing, ssh=ssh, runner=runner, report=report)[1]


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
    provision: Provision = Provision.NEVER,
) -> list[dict[str, Any]]:
    """What ``debug-config`` would write, asked for rather than recomputed.

    A target no debugger fits is not a failure of the editor: the folder, the
    excludes and the terminals are the rest of the seat, and every mechanism
    that said no was named on ``debug-config``'s stderr — which is relayed by
    :func:`_author` whether or not a configuration came back with it.

    :attr:`Provision.IF_NEEDED` is the whole of the retry. The seat has just
    said, in its own words, that debugpy in the target is the blocker; consent
    was given by the verb; so the second ask is the first one answered rather
    than a second measurement of anything. It cannot loop — the retry runs with
    :attr:`Provision.ALWAYS`, and only a run that did *not* provision reports
    itself unprovisioned.

    The retry falls back to what the first run authored, which it did not have
    to do while the trigger was a refusal: back then the first run had no
    configurations by definition, so there was nothing to lose. Now it often
    has good ones whose only fault is a closed port — and the command that
    opens that port by hand was relayed along with them. A provisioning run
    that then fails on egress or on the injection must not take them with it,
    or widening the trigger would be a regression for the very case it was
    widened to serve.
    """
    entries, unprovisioned = _author(
        kubectl, seat, report, provision=provision is Provision.ALWAYS
    )
    if not unprovisioned:
        return entries
    if provision is not Provision.IF_NEEDED:
        report(_PROVISION_REMEDY)
        return entries
    report(_PROVISION_NEEDED)
    return _author(kubectl, seat, report, provision=True)[0] or entries


def _author(
    kubectl: Kubectl,
    seat: ContainerRef,
    report: Callable[[str], None],
    *,
    provision: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """One ``debug-config`` run, and whether the seat asked to be provisioned.

    The second half of the pair is the only thing :func:`_configurations` needs
    that a list of configurations cannot carry: "empty" is the same answer for a
    target with no debugger, a seat that could not run the verb at all, and a
    Python workload one ``uv pip install`` away from working. Reading
    :data:`PROVISION_FLAG` out of the seat's own stderr keeps the three apart
    without this side of the wire guessing the target's language.

    Which is why it is read on a **successful** run too, and not only on a
    refusal. Provisioning answers two blockers, not one: a target that cannot
    import debugpy gets no configuration at all, and a target that *can* gets
    one whose port has nothing behind it until the injection is run. Only the
    first is a non-zero exit. Keying the retry on the exit code therefore left
    the second — every Python workload shipping its own debugpy, which is most
    of them — with a launch.json whose F5 met a closed port, from the one verb
    that exists to hand over a debuggable target (measured against a Diamond
    IOC, 2026-08-21). The flag in the narration is the seat's own request, and
    it is the same request either way.

    ``not provision`` is what keeps it from looping, and it is the whole of
    that guarantee: a provisioning run's stderr names the flag on every line it
    narrates, so the guard has to be here rather than in the caller.
    """
    argv = [*DEBUG_CONFIG_ARGV, *([PROVISION_FLAG] if provision else [])]
    if provision:
        report(_PROVISION_NOTICE)
    result = kubectl.exec_(
        seat.pod.name,
        argv,
        container=seat.container,
        check=False,
        # An assessment answers in a moment; a provisioning run installs.
        timeout=PROVISION_TIMEOUT if provision else DEFAULT_CALL_TIMEOUT,
    )
    relayed = _relay(result.stderr, report)
    wants_provisioning = not provision and PROVISION_FLAG in result.stderr
    if result.returncode != 0:
        # The last line only when there was nothing to relay - a `podbench` the
        # image does not resolve exits 127 with sh's message and no narration,
        # and that message is the whole diagnosis.
        report(
            "no launch.json: nothing above could be turned into one"
            if relayed
            else f"no launch.json: {_detail(result.stderr)}"
        )
        return [], wants_provisioning
    document: Any
    try:
        document = json.loads(result.stdout)
    except ValueError as error:
        report(f"no launch.json: debug-config printed no JSON ({error})")
        return [], False
    if not isinstance(document, dict):
        report("no launch.json: debug-config printed no JSON object")
        return [], False
    raw: Any = as_dict(document).get("configurations")
    entries = cast("list[Any]", raw) if isinstance(raw, list) else []
    return [
        as_dict(entry) for entry in entries if isinstance(entry, dict)
    ], wants_provisioning


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
            f"cannot read {path} in the seat: {_detail(result.stderr)}. podbench "
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

    A refusal ends the run with a sentence rather than with ``kubectl``'s
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
