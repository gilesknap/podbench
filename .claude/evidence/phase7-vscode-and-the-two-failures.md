# Phase 7 — VS Code, and the two named field failures

2026-08-24, `p47-beamline` on pollux, through
`k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig`, against the real
`bl47p-ea-fastcs-01-0`. Launcher under test:
`/home/giles/code/podbench/.venv/bin/podbench`, version
`0.7.3.dev41+g25fe18bd8`, branch `hotfix/easy-to-drive`. Seat image pinned
throughout the *landing* commands: `ghcr.io/gilesknap/podbench:0.7.3-beta.1-hotfix-easy-to-drive`.

This picks up where `.claude/evidence/phase7-the-live-walk.md` left off: the
target pod entering this session fresh (created ~06:20Z, 0 restarts, no
podbench wiring) and the values→check→init→…→retire mutating walk already
done and evidenced separately. This file covers only the VS Code half:
driving a real window through the bridge, and chasing the two named field
failures from 2026-08-23.

Environment checked before starting: `DISPLAY=:0`, `WAYLAND_DISPLAY=wayland-0`,
`XDG_RUNTIME_DIR=/run/user/1000` all present in the Bash tool's own
environment (`echo $DISPLAY` etc. all answered). A real GUI is reachable —
this is not a headless run.

---

## 0. Setup

Shim symlinked and put first on `PATH`, per the bridge README:

```
mkdir -p /tmp/bridge-bin
ln -sf /home/giles/code/podbench/tools/vscode-bridge/code-with-bridge /tmp/bridge-bin/code
export PATH="/tmp/bridge-bin:$PATH"
which code   # -> /tmp/bridge-bin/code
```

`/tmp/bridge-bin` contains neither `/remote-cli/` nor `/.vscode-server/`, so
`resolve_editor` accepts it. Before landing anything, checked for a stray
already-running `code` holding the IPC socket (the trap the task named): one
personal VS Code window was running (`pid=4187293`,
`--user-data-dir=/home/giles/.config/Code`), a *different* user-data-dir from
the bridge's own (`~/.local/state/podbench-vscode-bridge/user-data`), so it
could not intercept the shim's launches — confirmed by reading
`code-with-bridge` itself, which always passes an explicit
`--user-data-dir` (never omits it), which is exactly what defeats the IPC
hand-off this trap depends on. `vsc.py ls` found nothing before the first
landing, as expected.

---

## 1. First landing

```
podbench vscode bl47p-ea-fastcs-01 -n p47-beamline \
  --image ghcr.io/gilesknap/podbench:0.7.3-beta.1-hotfix-easy-to-drive --pull always
```

First attempt hit a transient API conflict, unrelated to either named
failure — a stale `resourceVersion` on the ephemeral-containers PATCH:

```
podbench: kubectl ... replace --raw .../ephemeralcontainers ... exited 1:
Error from server (Conflict): Operation cannot be fulfilled on pods
"bl47p-ea-fastcs-01-0": the object has been modified; please apply your
changes to the latest version and try again
```

Immediate retry, same command: `exit=0`, `podbench-1` landed, `degraded`
rung, `verdict live attach available`. Confirmed with the bridge:

```
$ vsc.py ls
pid=499227  remote=ssh-remote  vscode-remote://ssh-remote%2Bpodbench-p47-beamline-bl47p-ea-fastcs-01-0-1/tmp/podbench-home
$ vsc.py info
{ "pid": 499227, "remoteName": "ssh-remote", ...,
  "folders": [{"fsPath": "/tmp/podbench-home"}], "terminals": [{"name": "bash"}] }
```

The bridge is genuinely in the window (`remoteName: ssh-remote`, the seat's
own folder) — the IPC hand-off trap did not recur.

The run's own report already carried a hint toward Failure 2, unprompted:
after installing `ms-python.python`/`ms-python.debugpy` via the seat's own
vscode-server, it printed

```
[warn] the window has noticed these but not registered them: Command
       Palette -> Developer: Reload Window, or F5 says
       `could not find a debug adapter descriptor`
```

— exactly the out-of-process-install symptom the `vscode-in-a-seat` skill
documents. That is a *known, named* caveat and turned out not to be the
operative cause here (§4 below); it is recorded because it is a real, distinct
gotcha this run also produced and a future reader of this file should not
attribute Failure 2 to it without checking.

---

## 2. Reproducing Failure 1 — the `--new` refusal

Two plain reconnects first (default identity, i.e. the same ssh key both
times): `podbench vscode bl47p-ea-fastcs-01 -n p47-beamline` (no `--new`)
reconnected cleanly, `exit=0`, no key warning. `podbench vscode ... --new`
also landed cleanly (a second seat, `podbench-2`) — **neither reproduced a
refusal**, because both offered the same ssh key the seat's `authorized_keys`
already carries. This matches #204's own diagnosis: the failure is not "a
seat cannot be reconnected to", it is "a client offering a *different* key
than the one baked into this seat's `authorized_keys` cannot use it, and
`authorized_keys` is immutable once the ephemeral container exists."

So the real repro needs a genuinely different identity. Generated a
throwaway keypair (`ssh-keygen -t ed25519`, scratchpad-local) and reconnected
with `--identity <throwaway>`, no `--new`:

```
podbench vscode bl47p-ea-fastcs-01 -n p47-beamline --identity <throwaway-key>
```

First attempt with the throwaway key still reported `[ok] ssh reaches the
seat` and completed successfully — a measurement artefact, not the real
answer: `ssh`'s `ControlMaster`/`ControlPersist 10m` from the *earlier*,
real-key reconnect was still alive at the same `ControlPath` (the path hash
is derived from host/port/user, **not** from the identity file), so
`check_reachable`'s `ssh <alias> true` silently rode the old, already-
authenticated master instead of authenticating fresh. Confirmed directly:

```
$ ssh -O check -F <conf> podbench-p47-beamline-bl47p-ea-fastcs-01-0-1
Master running (pid=498828)
$ ssh -O exit  -F <conf> podbench-p47-beamline-bl47p-ea-fastcs-01-0-1
Exit request sent.
$ ssh -F <conf> -o IdentitiesOnly=yes -i <throwaway-key> podbench-p47-beamline-bl47p-ea-fastcs-01-0-1 true
podbench@bl47p-ea-fastcs-01-0: Permission denied (publickey,keyboard-interactive).
```

This is a genuine, independently-worth-flagging gap in `check_reachable`'s
proof (a live master from a prior identity can mask a real key mismatch),
noted here because it nearly hid the real refusal, but it is **not** either
of the two named failures and is not chased further — the task named exactly
two, and this is adjacent, not one of them.

With every `ControlMaster` cleared (`for sock in /tmp/podbench-cm/*; do ssh -O
exit -o ControlPath=$sock x; done`), the clean repro:

```
$ podbench vscode bl47p-ea-fastcs-01 -n p47-beamline --identity <throwaway-key>
exit=2
```

stdout (the report) carried, correctly, the **measured** warning — not a
guess:

```
WARNING  this seat does not authorise the key being offered, and its
         authorized_keys cannot be added to from here, so ssh will be
         refused. `--new` lands a seat that takes it.
```

and stderr carried the actual refusal:

```
podbench: `ssh podbench-p47-beamline-bl47p-ea-fastcs-01-0-1` does not reach
the seat, so VS Code was not started - a Remote-SSH window would have failed
the same way, minutes and one vscode-server download later. ssh said:
    podbench@bl47p-ea-fastcs-01-0: Permission denied (publickey,keyboard-interactive).
  - `Could not resolve hostname`: ...
  - `agent refused operation`: ...
  - `sshd_config: No such file or directory`: ... `podbench attach --new` lands a fresh seat...
  - `Permission denied (publickey)`: this seat's authorized_keys was written
    when it started and does not carry the key in the stanza. `podbench
    attach --new` is the only way to change it
The seat itself is landed and the kubectl exec helpers above work
regardless; this is the ssh half of it.
```

### Verdict: correct refusal, message names the wrong verb

Measured, not guessed: `seat_authorises()` (`launcher.py`) does exactly what
#204 asked — `cat`s the seat's `authorized_keys` and compares the key blob —
and it correctly found the throwaway key **absent**. A direct `ssh` with
`IdentitiesOnly=yes` against a cleared `ControlMaster` independently confirms
the seat truly refuses that key: `Permission denied (publickey,keyboard-
interactive)`. So this is not "a seat that should have been reusable" — an
ephemeral container's `authorized_keys` is genuinely immutable
(`ephemeral-containers` skill), so no seat carrying the wrong key can ever be
made to accept a new one without landing a new container. **The refusal is
correct.**

It does, however, fail to explain itself precisely, exactly as the task
framed the two possibilities: `UNREACHABLE_CAUSES`
(`editor.py:389-401`) is a module-level constant with four generic bullet
causes, reached only from `check_reachable`, which is reached only from
`open_seat`, which is called only from the `vscode` verb's own
`_open_editor` (`grep` confirms — the string has exactly one call site, and
`attach` never calls it). Two of its four bullets nonetheless hard-code
`podbench attach --new` as the remedy — including the exact one that fired
here. A reader running `podbench vscode` and hitting this refusal is told to
run a *different* verb. This is precisely "a correct refusal that failed to
explain itself" — a message-quality defect, not a behaviour defect — and per
the task's rule it is reported, not changed: no fix was applied to
`UNREACHABLE_CAUSES` or `check_reachable`.

By contrast, the WARNING line earlier in the same report
(`_KEY_REMEDY_SEAT = "\`--new\` lands a seat that takes it."`) is
verb-agnostic and correctly worded regardless of which command produced it —
only the hard failure's cause list is wrong.

Restored to a working state afterward: `podbench vscode bl47p-ea-fastcs-01
-n p47-beamline` with the default (correct) identity, `exit=0`, `[ok] ssh
reaches the seat`, new window opened and bridge confirmed present
(`vsc.py info`, `pid=506510`, `remoteName: ssh-remote`,
`fsPath: /tmp/podbench-home`).

---

## 3. Failure 2 — debugging does not start

### 3.1 What podbench wrote and what the seat has

`vsc.py text .vscode/launch.json` — the bare relative path resolved into
**the seat's** file (the bridge's proven-2026-08-23 assumption, used as
given, not re-proven): four `debugpy` "attach" configs, one per debuggable
pid (`fastcs-example` pid 12, `stdio-socket` pid 1, `pptty` pid 11, and an
`adapter` pid 133 left from an earlier injection), each
`"connect": {"host": "127.0.0.1", "port": <ephemeral>}`,
`pathMappings: [{"localRoot": "/proc/<pid>/root", "remoteRoot": "/"}]` — the
mount-namespace mapping the skill requires for observe mode, correctly
present.

The adapter the config names is genuinely in the seat:

```
$ kubectl exec ... -- ls ~/.vscode-server/extensions/
ms-python.debugpy-2026.6.0-linux-x64
ms-python.python-2026.4.0-linux-x64
...
```

So `type: "debugpy"` matches an adapter the seat actually has. **The
launch.json/adapter pairing is not the defect.**

### 3.2 `vsc.py debug` and the events

```
$ vsc.py debug "podbench: attach to fastcs-example [pid 12 fastcs-example] (debugpy)"
{ "started": false, "since": 1, "session": null }
$ vsc.py events 1
[ {"kind":"terminal.open",...}, {"kind":"editor.active",...} ]   # no dap.* at all
```

`started: false` immediately, and **zero** `dap.*` events — unlike the
README's doomed-file example, which got `started: true` and then a
`dap.terminated`/`dap.adapterError` a moment later. Here the failure is
earlier: no DAP session was ever created to emit events from.

The seat's own extension log corroborates precisely where it stops:

```
$ kubectl exec ... -- cat .../exthost5/ms-python.debugpy/'Python Debugger.log'
2026-08-24 06:46:48.885 [info] Resolving attach configuration with substituted variables
2026-08-24 06:46:48.926 [info] createDebugAdapterDescriptor: request='attach' name='podbench: attach to fastcs-example ...'
2026-08-24 06:46:48.926 [info] Connecting to DAP Server at:  127.0.0.1:46531
```

— then nothing. It tries to connect and the log simply stops, consistent
with the TCP connect being refused with no protocol exchange to log.

### 3.3 The named cause: `debugpy.listen()` can only be called once, podbench does not know that, and its success message is not a measurement

Directly connecting to every port this session ever offered, from inside the
seat (same host network as the target — `hostNetwork: true`):

```
$ for port in 45683 37687 59339 43391 59533 46531 47593 47001; do ...; done
port 45683: ConnectionRefusedError: [Errno 111] Connection refused
port 37687: ConnectionRefusedError: [Errno 111] Connection refused
... (all eight: refused)
```

Every port from every provisioning attempt across this whole session —
run 1's, run 6's, and a hand-run repro — is dead. Enumerating pid 12's own
held listening sockets precisely (its `/proc/12/fd` socket inodes
cross-referenced against `/proc/12/net/tcp`, rather than the whole-node
listing `hostNetwork` otherwise returns) shows **exactly one**, port 44951,
and it is not a debugpy port — thread names on pid 12 are all EPICS support
threads (`CAS-TCP`, `PVXTCP`, `dbCaLink`, …), no `pydevd`/debugpy thread
present at all.

`kubectl logs` on the *target* container (not the seat — the process's own
stdout/stderr, invisible to podbench's injector) is the direct proof of
*why*. Every debugpy-related traceback in the whole session's log, back to
the earliest timestamp checked, is the same:

```
RuntimeError: debugpy.listen() has already been called on this process
  File ".../debugpy/server/api.py", line 145, in listen
    raise RuntimeError("debugpy.listen() has already been called on this process")
```

caught and merely *logged* by debugpy's own `attach_pid_injected.attach()`
(`log.reraise_exception()` — a log line, not a crash), so it never reaches
podbench at all. Confirmed by hand-reproducing the whole injection from
scratch against a **fresh, never-before-tried port** (47001) and polling it
for 15s straight — `gdb` ran cleanly (`dlopen` returned a non-null pointer,
`DoAttach` returned via `call`, gdb detached, injector exited **0**) and the
port never opened for the entire 15s:

```
$ PYTHONPATH=... /app/.venv/bin/python -m debugpy --listen 127.0.0.1:47001 --pid 12
[gdb output, clean detach, exit 0]
t=1s..15s: port CLOSED (every second)
```

And `provision.py`'s own success path confirms exactly what gets checked:

```python
return Injected(
    True, seconds,
    (f"injected in {seconds:.1f}s; the app now serves debugpy on 127.0.0.1:{port}",),
)
```

reached whenever `result.returncode == 0` for the `gdb`/`timeout` subprocess
— **the exit code of the injector, nothing about the socket**. This is the
same class of gap the `vscode-in-a-seat` skill already documents elsewhere
("`gdb -batch` exits 0 either way") applied to a different call: a clean
gdb detach proves the *injection call ran*, never that the Python code it
ran did what it was asked. `debug-config`'s own text even contradicts itself
within a single run's output over four lines — "injected in 1.4s; the app
now serves debugpy on 127.0.0.1:46531" immediately followed by a fresh
measurement pass reporting "nothing is listening on 127.0.0.1:46531 yet" —
which is `_author()`'s post-provision remeasurement catching, honestly, what
the provision step's own success line could not: the target's log makes
clear that remeasurement is not a race, it is the same
`RuntimeError`-guarded `listen()` call failing every time.

**Named cause:** `debugpy.listen()` sets a permanent, process-lifetime latch
(`listen.called`) the first time it succeeds on a given target process. This
target (pid 12) already had that latch set before this session's own first
provisioning attempt could run — the earliest occurrence found in
`kubectl logs` (`--since-time=2026-08-24T06:20:00Z`, i.e. from pod creation)
already shows the "already been called" error, before this run's first
`podbench vscode` landing at 06:36:41Z. *Which* call originally consumed the
one-shot latch is **not measured** — plausibly this run's own first
`_author(provision=True)` pass (`editor.py`), which the code shows performs
the real injection and then immediately remeasures within the same call; if
that first real `listen()` briefly succeeded before the remeasurement's own
socket probe (or something about it) tore it down, every attempt after is
structurally doomed regardless of `--new`, a fresh identity, or a fresh
window, because the latch lives in the *target's* process, not the seat's.
`--new` cannot fix this (a new seat still injects into the same pid 12);
only a new target process (an app restart) could hand back one more
single-use `listen()` call.

This is not the OOM trap (folder opened was `/tmp/podbench-home`, never `/`;
pod never OOMed — restartCount 0/0 throughout, checked at the end), not the
breakpoint-vs-probe timer (this target "declares no readiness, liveness or
startup probe" per its own report — no deadline applies), and — checked and
ruled out rather than assumed — not a `/python/cpython-*` interpreter
collision (#160's shape): the seat runs `cpython-3.11.16`, the target
(read via `/proc/12/root`, single-hop, no further symlink chase) runs
`cpython-3.11.13` — a real, distinct build mismatch, present and worth
knowing, but the failure observed is a plain Python-level `RuntimeError`
inside the target's *own* already-correct debugpy copy, nothing an ABI or
namespace collision would produce.

### 3.4 A second, secondary data point: a different target pid hangs rather than fails fast

Attaching to a *different* process in the same target container (`pid 1`,
`stdio-socket`) did not fail immediately: the bridge call itself timed out
client-side (`vsc.py`'s own socket recv, ~30s+) rather than resolving
`started: false` promptly. By the time it was checked, `debugSession: null`
— no session left running. The target container's own log had **no new
output** for this attempt (unlike pid 12's clean `RuntimeError`), and its
launch.json port (53540) is, like every other port this session, refused on
direct connect. So the *end state* is the same (nothing listens, debugging
does not start), but the *path* there was slower — most likely debugpy's
adapter performing several connect retries with backoff before giving up,
rather than one immediate refusal. **Not fully diagnosed** — reported as an
observed variation, not folded into the pid-12 finding as the same
mechanism, since the target's own log gives no direct confirmation for this
pid the way it did for pid 12.

---

## 4. Final state — what is left behind

Pod: `bl47p-ea-fastcs-01-0`, `Running`, `restartCount 0/0` on both real
containers (`bl47p-ea-fastcs-01`, `temp-controller-simulator`) and both
ephemeral seats, checked at the end of this session — the falsification
condition (`restartCount` unchanged) holds.

Two permanent ephemeral containers exist on this pod (names are burnt for
its lifetime regardless of what this file does):

* **`podbench-1`** — degraded rung, pinned image
  (`0.7.3-beta.1-hotfix-easy-to-drive`), still running, ssh alias
  `podbench-p47-beamline-bl47p-ea-fastcs-01-0-1`. A VS Code window is open
  against it right now (`vsc.py ls` → `pid=506510`,
  `remoteName: ssh-remote`, folder `/tmp/podbench-home`), with the bridge
  confirmed alive. **Left running and left open** — this is the seat every
  measurement above was taken against, and closing the window does not
  retire the seat regardless.
* **`podbench-2`** — landed only to reproduce `--new` behaviour on a
  non-key-mismatched run (§2's first, inconclusive attempt); landed
  *without* `--image`/`--pull always`, so it carries the **default**,
  unpinned image (`0.2.0b3.dev27+gaf61404ae.d20260818` — visibly older than
  this launcher and than the pinned tag), not the one this task specified
  for landed seats. It is otherwise idle and was not used for the debugpy
  investigation. Left running because ephemeral containers cannot be
  removed; flagged here so its odd version does not read as a mystery.

Target process `pid 12` (`fastcs-example`) has its `debugpy.listen()` latch
permanently spent for the remainder of this pod's life — no further
injection into that specific pid can ever succeed, by design of debugpy
itself, until the app process restarts.

Scratchpad artefacts (not committed): the throwaway ssh keypair used for §2,
and the raw command outputs referenced above, under
`/tmp/claude-1000/-home-giles-code-podbench/c5f3383d-fe44-4000-ba56-4949b34e829c/scratchpad/`.

---

## Not measured

* **Which specific call first consumed pid 12's `debugpy.listen()` latch**
  (§3.3) — the target's own log does not carry enough context before
  06:20:00Z to say for certain it was this session's own first attempt
  rather than something in the pod's first sixteen minutes before this
  session touched it.
* **The pid-1 hang's exact mechanism** (§3.4) — end state (nothing listens)
  matches pid 12's finding; the slower failure path is observed, not traced
  to a specific line.
* **Whether `ControlMaster` reuse across a changed `--identity`** (the
  near-miss in §2) is itself considered a bug worth filing — it is reported
  as a measured mechanism, not judged, since it is adjacent to but not one
  of the two named failures this task specified.
* **GUI-only surfaces** — the workspace-trust modal (never seen: the shim's
  `security.workspace.trust.enabled: false` write suppressed it on every
  run, consistent with the README's 2026-08-23 finding), any VS Code error
  dialog (the bridge cannot read `showErrorMessage` — none was needed here,
  since every failure surfaced through DAP events, the extension's own log,
  or the target's `kubectl logs`, all of which the bridge/cluster access
  *can* read), and anything reachable only by mouse or living inside a
  webview — none of this run's findings depended on any of those.
