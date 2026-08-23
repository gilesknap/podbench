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
