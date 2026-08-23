# The VS Code bridge

A control socket for one VS Code window, so that a terminal agent — or a human
with a shell — can drive the editor podbench just opened: run commands, open
files, set breakpoints, start a debug session and read where it stopped.

It exists because `podbench vscode` ends at `code --remote ssh-remote+<alias>
<folder>` (`editor.py:671`) and nothing after that is observable. The Phase 5
live run recorded in `.claude/evidence/phase5-the-workflow-on-p47.md` could not
open a window at all — `code` was an argv-recording stub — so the half of the
workflow that a user actually looks at went untested.

**This is a development tool. It is not part of podbench, not shipped in the
wheel, not imported by anything under `src/`, and not run by CI.** Wiring a real
cluster into CI would mean storing credentials that are not going in a repo, so
the tests it supports are ones a developer runs by hand against a cluster they
already have.

## Using it

Nothing is installed into `~/.vscode/extensions`. The extension is loaded from
this directory with `--extensionDevelopmentPath`, so there is nothing to
uninstall and no effect on a normal VS Code session.

```sh
# Point a `code` on PATH at the shim, which adds the extension.
mkdir -p /tmp/bridge-bin
ln -sf "$PWD/tools/vscode-bridge/code-with-bridge" /tmp/bridge-bin/code
export PATH="/tmp/bridge-bin:$PATH"

podbench vscode <pod> -n <namespace>     # opens a window carrying the bridge

tools/vscode-bridge/vsc.py ls            # find it
tools/vscode-bridge/vsc.py info          # remote name, folders, open editors
tools/vscode-bridge/vsc.py open src/app.py 42
tools/vscode-bridge/vsc.py bp src/app.py 42
tools/vscode-bridge/vsc.py debug "podbench: launch app.py (debugpy)"
tools/vscode-bridge/vsc.py events        # dap.stopped, debug.start, ...
tools/vscode-bridge/vsc.py stack         # frames and locals where it stopped
```

The shim's directory must contain neither `/remote-cli/` nor `/.vscode-server/`,
or `resolve_editor` refuses it as VS Code's *remote* CLI (`editor.py:437`).

**The shim starts its own VS Code instance, and has to.** `code` with no
`--user-data-dir` hands the request to an already-running VS Code over its IPC
socket in `$XDG_RUNTIME_DIR`, and *that* process was started without
`--extensionDevelopmentPath` — so the window opens, Remote-SSH connects, and the
bridge is simply not in it. Nothing reports this: `podbench vscode` prints
`[ok] asked VS Code to open ... over Remote-SSH` because the CLI exited 0, and
`vsc.py ls` finds no window. Measured 2026-08-23, against a VS Code that had been
open since earlier in the day. `--extensions-dir` is deliberately left alone, so
the new instance keeps the real `~/.vscode/extensions` — Remote-SSH lives there
and `podbench vscode` cannot work without it.

A separate `--user-data-dir` is also a separate *profile*, which arrives with no
theme, no keybindings and a "Welcome to VS Code" tab over the window you asked
for. That is not cosmetic: the point of driving this window is to see what a
user sees, and nobody's VS Code looks like a default profile. So the shim seeds
the profile once from `~/.config/Code/User` — `settings.json`, `keybindings.json`
and `snippets` — and turns the welcome tab off, then leaves it alone, because the
profile accumulates state that must persist between runs. `settings.json` is JSON
*with comments*, so the setting is inserted after the opening brace when the file
will not parse strictly, which cannot reorder or drop anything already in it.

**Workspace trust is the trap that stops an unattended run dead.** A fresh
profile trusts no folder, so opening the seat's home raises a modal, and until
somebody clicks it **extensions run restricted — the bridge does not start**.
The window is plainly on screen while `vsc.py ls` reports nothing, which reads
exactly like the extension being broken. VS Code 1.124 has no
`--disable-workspace-trust` flag, so the shim writes
`security.workspace.trust.enabled: false` into the profile, along with the
startup-editor setting, on *every* run rather than once — they are the harness's
requirements rather than the user's preference, and a hand-edit turning either
back on breaks the next unattended run silently.

Measured 2026-08-23: with trust disabled, `podbench vscode`'s window reached
`vsc.py ls` **6 seconds after launch with no interaction**, against a run that
had previously hung until a human clicked *Trust*.

Closing a driven window is `vsc.py cmd workbench.action.closeWindow`. That
disconnects the editor and nothing else — **the seat is an ephemeral container
and outlives it**, until the pod is replaced.

## Proven against a real seat

2026-08-23, `podbench vscode bl47p-ea-simdet-01-0` on the p47 test beamline,
which is what the three design bets above were resting on:

* the window came up as `remote=ssh-remote` on
  `vscode-remote://ssh-remote%2B<alias>/tmp/podbench-home`, driven from the
  laptop-side extension host;
* `vsc.py text .vscode/launch.json` — a bare relative path — returned **the
  seat's** file, podbench's generated `cppdbg` config with its `/proc/31/root`
  paths, rather than a same-named file on the laptop;
* `vsc.py debug "podbench: attach to ioc ..."` started a `cppdbg` session that
  stayed alive, so `startDebugging` from the UI extension host does resolve an
  adapter living in the **seat**.

One caveat found doing it: `vsc.py commands` lists the **local** extension host's
registry, so a remote extension's own contributed commands do not appear there
(`--filter cpptools` came back empty while cpptools was demonstrably running in
the seat). Workbench commands are unaffected.

`vsc.py eval '<js>'` runs arbitrary JavaScript with `vscode`, `ctx` and
`require` in scope and awaits it, which is the escape hatch for anything the
named verbs do not cover.

## Detecting a debug failure

**VS Code's error dialogs cannot be read.** `showErrorMessage` is write-only and
there is no `onDidShowNotification`, so a modal or a toast is invisible to this
tool — and to any other extension.

What *is* visible is the traffic the dialog is rendering. The adapter tracker
records failed DAP responses (`dap.error`, carrying `message` and the adapter's
own `body.error.format`), the debug console (`dap.output`, including `stderr`),
the adapter failing to start or dying (`dap.adapterError`, `dap.adapterExit`),
and `dap.terminated`. In practice this is *more* than the dialog shows. Measured
against a launch config naming a file that does not exist, the events carried the
whole traceback down to
`FileNotFoundError: [Errno 2] No such file or directory: '.../does-not-exist.py'`.

**`started: true` is not success.** `vscode.debug.startDebugging` resolved `true`
for that same doomed session; the failure arrived over the next two seconds as
`dap.terminated`, `dap.adapterError` and stderr output. Anything asserting that
debugging works must read the events, never the boolean. `debug` returns a
`since` watermark for exactly this — pass it to `events` to get that session's
traffic and nothing earlier:

```sh
tools/vscode-bridge/vsc.py debug "podbench: launch app.py (debugpy)"   # -> {"started": true, "since": 7, ...}
sleep 3
tools/vscode-bridge/vsc.py events 7
```

The one case this misses is an extension that calls `showErrorMessage` without
any DAP exchange at all. For that, VS Code's own logs are on disk under the
bridge profile — `~/.local/state/podbench-vscode-bridge/user-data/logs/` — and
the extension-host log there is readable directly.

## Why the extension is `extensionKind: ["ui"]`

In a Remote-SSH window the *workspace* extension host runs inside the seat,
which is ephemeral and whose filesystem the laptop cannot reach — a socket
opened there would be unreachable and would die with the seat. A **UI**
extension runs in the laptop's extension host, where the driving agent already
is, and still sees the remote workspace because the workbench proxies the API
across the two hosts.

That is also why `resolve()` in `extension.js` builds a URI against the
workspace folder rather than calling `Uri.file()`. In a remote window the folder
is a `vscode-remote://` URI, and resolving a bare path with `Uri.file()` would
silently open a same-named file **on the laptop** — the failure mode the
`gdb-across-namespaces` skill is about, wearing different clothes.

## What it cannot do

Two blind spots, and a test that depends on either must say so rather than
claim a pass:

- **It cannot see the screen.** GNOME refuses `org.gnome.Shell.Screenshot` to
  unsandboxed callers (`AccessDenied`), and the portal route prompts
  interactively every time.
- **It cannot synthesise input.** No keyboard or mouse events, and under Wayland
  the X11-era tools do not work regardless.

So anything reachable only by a mouse gesture, or living inside a webview, is
out of reach. Everything exposed through the extension API — commands, editor
and document state, breakpoints, debug sessions, diagnostics, terminals — is not.

## Protocol

One JSON object per line, in and out. The extension writes `<pid>.json` and
`<pid>.sock` into `~/.local/state/podbench-vscode-bridge/` per window; the
descriptor carries `remoteName` and `folders` so a caller with several windows
open can tell them apart *before* connecting. `vsc.py` prunes descriptors whose
socket refuses a connection, which is what a killed VS Code leaves behind.

Replies are `{"ok": true, "result": ...}` or `{"ok": false, "error": ...}`.

A `DebugSession` is never serialised whole: its `configuration` carries the
*resolved* launch config, which for debugpy includes the entire inherited
environment — tokens, kubeconfig paths and all.
