# Phase 8 — `podbench vscode` actually debugs, on the live p47 target

2026-08-24, `p47-beamline` on pollux, through
`k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig`, against the real
`bl47p-ea-fastcs-01-0` on node `bl47p-ea-serv-01.diamond.ac.uk`. Launcher under
test: `/home/giles/code/podbench/.venv/bin/podbench`, an editable install
resolving to `/home/giles/code/podbench/src` on branch `vscode/actually-debugs`,
version `0.7.3.dev50+g75e5ef79a.d20260824`. Seat image pinned:
`ghcr.io/gilesknap/podbench:0.7.3-beta.1-vscode-actually-debugs` with
`--pull always` (the seat reports `0.7.3.dev56+g1cf50b7e0`). VS Code driven
through `tools/vscode-bridge`, human present at the keyboard.

This is slice 6 of `.claude/plans/vscode-actually-debugs.md` — the only slice
whose result is evidence rather than code. No file under `src/` was touched and
no `git` command was run.

**Headline, stated before the evidence.** A breakpoint in the beamline
application's own `controllers.py` was set from VS Code, bound, and hit, twice,
with the workload's real locals visible in the frame — measured, quoted in §5.
Five of the plan's six checkboxes pass on the first attempt with no hand-fix.
The sixth passes on the run that followed it, because **the first invocation of
`podbench vscode` failed outright** on a 409 race between its own resize and its
own ephemeral-container write (§2). That is a defect this run found, it landed
no seat, and the run that is reported below is the one after it. Two further
defects, both newly *visible* rather than newly created, are in §8.

The phase-8 open thread from
`.claude/evidence/phase8-why-the-adapter-never-answers.md` §8.2 is **closed**:
VS Code was the client, the adapter answered it, and the session worked. §6.

---

## 0. The state the run started in

```
$ kubectl get pod bl47p-ea-fastcs-01-0 -n p47-beamline -o json | ...
creationTimestamp 2026-08-24T09:44:25Z
phase Running
hostNetwork True
node bl47p-ea-serv-01.diamond.ac.uk
container bl47p-ea-fastcs-01 restartCount 0 ready True
container temp-controller-simulator restartCount 0 ready True
ephemeralContainers []
ephStatuses []
spec bl47p-ea-fastcs-01 {'limits': {'cpu': '500m', 'ephemeral-storage': '2Gi', 'memory': '256Mi'},
                         'requests': {'cpu': '100m', 'ephemeral-storage': '100Mi', 'memory': '64Mi'}}
spec temp-controller-simulator {'limits': {..., 'memory': '1Gi'}, ...}
```

**Zero seats, `restartCount 0/0`, the target container at its template 256Mi
limit.** Hotfix-wired, with the redo's `.podbench-debugpy` still on the claim
from the previous slice, and the application's `.vscode` committed and
unmodified:

```
$ kubectl exec ... -c bl47p-ea-fastcs-01 -- md5sum /podbench/app/.vscode/*.json
e154d42245efc585ee4425e0b1d83102  /podbench/app/.vscode/extensions.json
bd93b131588050fa3abc2355bd436a5a  /podbench/app/.vscode/launch.json
d936fe403bded5dd53d68756c7cc8f8e  /podbench/app/.vscode/settings.json
bdd8e5bd443d86cb2723a01e0d2ca92d  /podbench/app/.vscode/tasks.json
```

`launch.json` carries its four `//` comments, its two committed configurations
and the trailing comma before the second one's closing brace — slice 3's
fixture, intact.

No bridge window existed before the run:

```
$ python3 tools/vscode-bridge/vsc.py ls
(no bridge windows)
```

The shim was already in place, and every `podbench vscode` below was prefixed
with `PATH="/tmp/bridge-bin:$PATH"`.

---

## 1. The command, run three times

```
KUBECONFIG=k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig \
PATH="/tmp/bridge-bin:$PATH" ./.venv/bin/podbench vscode bl47p-ea-fastcs-01-0 \
  -n p47-beamline \
  --image ghcr.io/gilesknap/podbench:0.7.3-beta.1-vscode-actually-debugs \
  --pull always
```

byte-identical each time.

| run | exit | seat | what it did |
|---|---|---|---|
| 1 | 2 | **none landed** | resized 256Mi → 6Gi, then lost a 409 on its own ephemeral-container write (§2) |
| 2 | 0 | `podbench-1` **(new)** | the run everything in §3–§6 is measured from |
| 3 | 0 | `podbench-1` **(reconnected)** | the "second, identical run" of the checklist; no resize, and §8.2's defect |

**Exactly one seat existed at every moment of this run, `podbench-1`, and it is
the only one that ever existed on this pod.** Run 1 landed nothing; runs 2 and 3
are the same container. Verified after each:

```
ephemeralContainers ['podbench-1']
eph podbench-1 state ['running']
```

An intermediate fourth invocation was botched by the harness, not by podbench:
it was piped through `head -60`, which closed the pipe and gave podbench a
`BrokenPipeError` and exit 1. It is discarded and replaced by run 3 above,
which was captured to a file with no pipe. Saying so rather than quietly
dropping it, because "exit 1" in a log is exactly the kind of thing that
becomes a phantom defect later.

---

## 2. Run 1 failed, and left a silently resized pod

Complete output of run 1 — one line, and it is stderr:

```
podbench: kubectl -n p47-beamline --request-timeout=25s replace --raw
/api/v1/namespaces/p47-beamline/pods/bl47p-ea-fastcs-01-0/ephemeralcontainers -f -
exited 1: Error from server (Conflict): Operation cannot be fulfilled on pods
"bl47p-ea-fastcs-01-0": the object has been modified; please apply your changes
to the latest version and try again
```

The pod immediately afterwards:

```
ephemeralContainers []
spec bl47p-ea-fastcs-01 {'limits': {'cpu': '500m', 'ephemeral-storage': '2Gi', 'memory': '6Gi'},
                         'requests': {'cpu': '100m', 'ephemeral-storage': '100Mi', 'memory': '615Mi'}}
```

**Measured**: the resize succeeded (256Mi → 6Gi, request 64Mi → 615Mi), no seat
landed, and **nothing was printed about the resize**. `restartCount` stayed 0/0.

**Inferred** (mechanism, not measured): `Kubectl.add_ephemeral_container`
(`src/podbench/kubectl.py:1031`) does a fresh `get_pod_subresource` and then a
`raw_put` of the whole pod. The 409 says the object changed between that GET and
that PUT. The resize had completed moments earlier and the kubelet writes the
resize back asynchronously (allocated resources, resize status), so there is a
window of a second or so after a resize in which podbench's own PUT loses. There
is no retry on 409 anywhere on this path.

Two consequences worth separating:

- **The failure is transient and self-healing on retry** — run 2, identical,
  succeeded — but the retry is a retry, and a user hitting this sees a crash
  with a Kubernetes conflict message and no seat.
- **The silent resize is the worse half.** The report prints at the end, so a
  run that aborts here says nothing about the mutation it just made to a live
  production-shaped pod. `try_resize`'s success note, the `terminal-reports`
  rule that "a caveat about a *mutation* belongs on the path that made it", is
  buffered into a report that never prints.

This is only reachable on a pod that needs resizing, which after slice 1 is
every pod under 6Gi — i.e. the first `podbench vscode` any real pod ever sees.
It is a **new** defect found by this run, not one the plan predicted, and it is
the one thing here that is a failure of the plan rather than of the redo.

---

## 3. Run 2, the run under test

Exit 0. The seat, verbatim from the report's head:

```
seat        p47-beamline/bl47p-ea-fastcs-01-0[podbench-1]  (new)
target      bl47p-ea-fastcs-01; this pod also has
            temp-controller-simulator.
version     0.7.3.dev56+g1cf50b7e0 in the seat,
            0.7.3.dev50+g75e5ef79a.d20260824 in this launcher
rung        degraded - uid 37887, gid 37887, CapEff 0000000000000000
ladder
  degraded  landed   running since 2026-08-24T09:52:53Z
supports
  [x] live attach (gdb -p <pid>)
      no deadline: 'bl47p-ea-fastcs-01' declares no readiness, liveness
      or startup probe, so nothing removes it from a Service or restarts
      it while it is stopped
measured
  ids         seat 37887:37887, target 37887:37887 (pid 13)
  memory      6964Mi free of 7Gi (204Mi in use)
```

The "no deadline" row is why the breakpoint in §5 could be held for minutes for
free: this container declares no probes, so the `vscode-in-a-seat` breakpoint
timer does not apply to it.

Slice 2's choice, printed as the plan asked — chosen up front from the layout,
and named:

```
  [ok] any debugpy this run installs goes to
       /podbench/app/.podbench-debugpy on the claim, not
       /opt/podbench-debugpy: on a hotfixed pod that is the tree the
       seat shares with the target and can write without being root
```

### 3.1 The provision sentence: `ANSWERED`

The whole point of slice 5b, verbatim:

```
debug-config: --provision: installed debugpy for Python 3.11 into /proc/13/root/podbench/app/.podbench-debugpy
debug-config: --provision: injected in 3.9s and the adapter answered a DAP `initialize` on 127.0.0.1:40448 in 0.01s, so the app is debuggable rather than merely listening - F5 on the configuration this run emitted reaches it
```

**Of `_proof`'s four sentences (`provision.py:580`), the one printed was
`Handshake.ANSWERED`.** This is the first live confirmation of the criterion.
The `0.01s` matches the phase-8 measurement that `initialize` is answered from a
constant capability table with no debuggee round trip.

The emit stage then found that server and reused its port rather than choosing
another:

```
debug-config: emitting debugpy: a debugpy server is already listening on 127.0.0.1:40448,
held by pid 181 (/podbench/app/.venv/bin/python3
/proc/13/root/podbench/app/.podbench-debugpy/debugpy/adapter --for-server 58508
--host 127.0.0.1 --port 40448 --server-access-token 7e525a6d...) in the target container
```

---

## 4. The two files the checklist is about

### 4.1 `launch.json` — slice 3, additive and comment-preserving

`diff -u` of the committed file against the file after run 2, complete:

```diff
@@ -30,6 +30,54 @@
                  "debug-test"
              ],
              "console": "integratedTerminal",
+        },
+        {
+          "name": "podbench: attach to fastcs-example [pid 13 fastcs-example] (debugpy)",
+          "type": "debugpy",
+          "request": "attach",
+          "connect": {
+            "host": "127.0.0.1",
+            "port": 40448
+          },
+          "justMyCode": false,
+          "pathMappings": [
+            {
+              "localRoot": "/proc/13/root",
+              "remoteRoot": "/"
+            }
+          ]
+        },
+        {   ... the same shape for pid 7 (port 55672) ...
+        {   ... and pid 12 (port 43145) ...
         }
     ]
 }
```

**Every removed line is zero.** The four `//` comments survive, both committed
configurations survive byte for byte including the trailing comma inside `Debug
Unit Test`, and the diff is exactly the podbench block. That is slice 3's
falsification condition — "comments stripped, its two existing configurations
disturbed, or its diff larger than the podbench block" — not met in any of its
three parts.

The report's own line for it:

```
  [ok] wrote settings.json, launch.json, extensions.json in
       /podbench/app/.vscode
```

### 4.2 `ms-python.debugpy` in the seat

```
$ ssh podbench-p47-beamline-bl47p-ea-fastcs-01-0-1 'ls -1 ~/.vscode-server/extensions'
extensions.json
ms-python.debugpy-2026.6.0-linux-x64
ms-python.python-2026.4.0-linux-x64
ms-python.vscode-pylance-2026.3.1
ms-python.vscode-python-envs-1.36.0-linux-x64
```

Asked of the seat over ssh, not inferred from `code --install-extension`'s exit
code, for the reason `vscode-in-a-seat` gives.

---

## 5. The window, the session, and the breakpoint

The window came up carrying the bridge with no interaction:

```
$ python3 tools/vscode-bridge/vsc.py ls
pid=685643  remote=ssh-remote  vscode-remote://ssh-remote%2Bpodbench-p47-beamline-bl47p-ea-fastcs-01-0-1/podbench/app
```

### 5.1 `started: true`, and the session stayed up

```
$ python3 tools/vscode-bridge/vsc.py debug "podbench: attach to fastcs-example [pid 13 fastcs-example] (debugpy)"
{
  "started": true,
  "since": 2,
  "session": {
    "id": "d0d6b3ef-4c05-44dc-a72a-dbf328116d60",
    "name": "podbench: attach to fastcs-example [pid 13 fastcs-example] (debugpy)",
    "type": "debugpy"
  }
}
```

`started: true` is not success, as the bridge README insists. The events:

```
$ python3 tools/vscode-bridge/vsc.py events 0
  seq 2  debug.active  { id d0d6b3ef-..., name "podbench: attach to fastcs-example ..." }
  seq 3  debug.start   { id d0d6b3ef-..., type "debugpy" }
```

and eight seconds later, `events 3` returned that one record and nothing more —
**no `dap.terminated`, no `dap.adapterError`, no `dap.adapterExit`, no
`dap.output` on stderr.** The doomed-session signature the README documents did
not appear, and `vsc.py info` continued to report a live `debugSession`.

**`dap.*` events did not follow, and that is a bridge limitation rather than a
failure of the session.** `extension.js:368` registers
`registerDebugAdapterTrackerFactory('*')` in the **laptop's** UI extension host;
this session's adapter descriptor is created in the **seat's** workspace
extension host (§6 quotes its log). No DAP traffic reached the laptop-side
tracker, so no `dap.stopped`, `dap.thread` or `dap.process` was recorded even
though §5.2 proves all three happened. The plan's third checkbox asks for
`dap.*` events; they are **not** what proved the session, and the file says so
rather than ticking it on the strength of `started: true`. What proved it is
below.

### 5.2 The breakpoint bound and was hit

`src/fastcs_example/controllers.py:99` is inside `update_voltages`, decorated
`@scan(0.1)`, so it runs ten times a second.

```
$ python3 tools/vscode-bridge/vsc.py bp src/fastcs_example/controllers.py 99
[
  {
    "id": "fd2cab6d-bfa5-4c99-bbd8-929cc83228a5",
    "enabled": true,
    "uri": "vscode-remote://ssh-remote%2Bpodbench-.../podbench/app/src/fastcs_example/controllers.py",
    "fsPath": "/podbench/app/src/fastcs_example/controllers.py",
    "line": 99
  }
]
```

Six seconds later, unprompted, the window's **active editor had changed by
itself** — to the *mapped* spelling of the same file, at that line:

```
  "activeEditor": {
    "uri": "vscode-remote://ssh-remote%2Bpodbench-.../proc/13/root/podbench/app/src/fastcs_example/controllers.py",
    "fsPath": "/proc/13/root/podbench/app/src/fastcs_example/controllers.py",
    "languageId": "python",
    "lineCount": 114,
    "selection": { "start": { "line": 98, "character": 0 }, ... }
  }
```

That is VS Code revealing a stopped frame. `line: 98` is zero-based, i.e. line
99. And the stack:

```
$ python3 tools/vscode-bridge/vsc.py stack
{
  "threadId": 1,
  "frames": [
    { "id": 5,  "name": "update_voltages",   "line": 99,   "source": ".../proc/13/root/podbench/app/src/fastcs_example/controllers.py" },
    { "id": 8,  "name": "_run",              "line": 84,   "source": ".../proc/13/root/podbench/app/.python/cpython-3.11.13-linux-x86_64-gnu/lib/python3.11/asyncio/events.py" },
    { "id": 9,  "name": "_run_once",         "line": 1936, "source": ".../asyncio/base_events.py" },
    { "id": 10, "name": "run_forever",       "line": 608,  "source": ".../asyncio/base_events.py" },
    { "id": 11, "name": "run_until_complete","line": 641,  "source": ".../asyncio/base_events.py" },
    { "id": 12, "name": "run",               "line": 103,  "source": ".../.venv/lib/python3.11/site-packages/fastcs/launch.py" },
    { "id": 13, "name": "run",               "line": 265,  "source": ".../fastcs/launch.py" },
    { "id": 14, "name": "wrapper",           "line": 685,  "source": ".../typer/main.py" },
    { "id": 15, "name": "invoke",            "line": 814,  "source": ".../click/core.py" },
    { "id": 16, "name": "invoke",            "line": 1246, "source": ".../click/core.py" }
  ],
  "variables": {
    "Locals": [
      { "name": "controller", "value": "<fastcs_example.controllers.TemperatureRampController object at 0x7f28599f83d0>", "type": "TemperatureRampController" },
      { "name": "index",      "value": "0", "type": "int" },
      { "name": "self",       "value": "<fastcs_example.controllers.TemperatureController object at 0x7f2859a001d0>", "type": "TemperatureController" },
      ...
```

Those are the beamline application's own objects, at the beamline's own frame,
through the whole of its real call stack down into click. **The
`pathMappings` the plan's §4 could not prove — `localRoot /proc/13/root`,
`remoteRoot /` — resolve a real source file**, which was on
`phase8-why-the-adapter-never-answers.md`'s "not measured" list and is now
measured.

Continued once, and it stopped again with fresh frame ids (`5` → `38`), so it
genuinely resumed and re-hit rather than never having moved:

```
$ python3 tools/vscode-bridge/vsc.py cmd workbench.action.debug.continue
null
$ python3 tools/vscode-bridge/vsc.py stack        # 4s later
  { "id": 38, "name": "update_voltages", "line": 99, ... }
```

Breakpoints were then cleared and the process continued, and it is running:
`/proc/13/stat` reports state `S`.

### 5.3 No reload was needed, and podbench had warned one might be

Run 2's last editor step:

```
  [warn] the window has noticed these but not registered them: Command
         Palette -> Developer: Reload Window, or F5 says
         `could not find a debug adapter descriptor`
```

**Measured: no reload was performed, and the session started anyway.** The
`vsc.py debug` above was the first thing done to the window and it worked. The
seat's own logs show three server log directories and two extension hosts
(`exthost1`, `exthost2`), so VS Code restarted its extension host on its own
during bootstrap; whether that is *why* the extension was registered is
**inferred, not measured**. The warning is not wrong in general — the skill
documents the out-of-process install as structural — but on this run it named a
step that was not needed. Recording it as an observation, not a defect: a
warning that is sometimes unnecessary is the right side of that trade.

**No step in §3–§5 required a hand-fix.** Nothing was edited, no file was
repaired, no port was substituted, no extension was installed by hand.

---

## 6. The phase-8 open thread is closed

`phase8-why-the-adapter-never-answers.md` §8.2: *"The next instrument is
therefore VS Code itself... press F5, and read the adapter's log for a `Starting
message loop for channel Client` line."*

The adapter's own log does not exist on this run — podbench does not pass
`--log-to`, and turning it on would have been an instrumented run rather than
the product path this slice exists to test. What does exist is the **client's**
log, in the seat, and it is the direct discriminator:

```
$ ssh podbench-... 'cat "~/.vscode-server/data/logs/20260824T095342/exthost1/ms-python.debugpy/Python Debugger.log"'
2026-08-24 09:54:49.030 [info] Resolving attach configuration with substituted variables
2026-08-24 09:54:49.055 [info] createDebugAdapterDescriptor: request='attach' name='podbench: attach to fastcs-example [pid 13 fastcs-example] (debugpy)'
2026-08-24 09:54:49.055 [info] Connecting to DAP Server at:  127.0.0.1:40448
2026-08-24 09:54:49.088 [info] Received 'debugpySockets' event from debugpy.
```

**The redo's log stopped at `Connecting to DAP Server at:`. This one goes one
line further, 33 ms later.** `debugpySockets` is the first thing the adapter
emits in reply to `initialize` (it is the event the hand-rolled client saw in
§4.4 of the previous file). So the adapter served the window, the client message
loop did start, and everything after it — attach, breakpoints, stack, locals —
is in §5.

The socket table while the session was live, read against the right port
(40448 = 0x9E00):

```
$ kubectl exec ... -- grep -i ":9E00 " /proc/net/tcp
  14: 0100007F:9E00 00000000:0000 0A ... 37887   # LISTEN, adapter <- client
  55: 0100007F:A98C 0100007F:9E00 01 ... 37887   # ESTABLISHED, VS Code -> adapter
  80: 0100007F:9E00 0100007F:A98C 01 ... 37887   # the other half
$ ls -l /proc/181/fd | grep -c socket
4
```

Four socket fds, not the healthy-but-idle three the previous file measured: the
fourth is the accepted client connection. Worth carrying forward as the shape of
a *served* adapter.

**Verdict: the window's session works. The thread §8.2 left open is closed by a
working session rather than by a log line.** The redo's 0-byte symptom did not
reproduce here either, and this run still cannot say why it happened — that
question is retired rather than answered, because the thing it was blocking is
now demonstrably fine.

### 6.1 The 5b probe does not burn the adapter

The plan's one load-bearing unmeasured assumption in slice 5b — that closing the
probe socket without a DAP `disconnect` leaves the adapter serving. This run
tests it directly and for free:

1. podbench's own probe connected to `127.0.0.1:40448`, sent `initialize`, got a
   successful response in **0.01 s**, and closed the socket (`dap.py`, no
   `disconnect` sent).
2. Roughly two minutes later, **VS Code connected to that same port 40448** and
   was served — `Received 'debugpySockets' event from debugpy` at 09:54:49.088 —
   and went on to bind and hit a breakpoint.

**Measured: the assumption holds.** A `disconnect` would have called
`servers.stop_serving()` and closed the client listener for good; the listener
was still in `LISTEN` when VS Code arrived, and F5 worked. `dap.py`'s docstring
is confirmed on a live target.

---

## 7. The checklist

| # | check | verdict | evidence |
|---|---|---|---|
| 1 | a `launch.json` exists, and the app's own two configurations survive it with their comments | **pass** | §4.1 — diff is purely additive, zero removed lines, four `//` comments and the trailing comma intact |
| 2 | `ms-python.debugpy` is installed in the seat | **pass** | §4.2 — `ms-python.debugpy-2026.6.0-linux-x64` listed over ssh from the seat's own extensions dir |
| 3 | `vsc.py debug …` returns `started: true` and `dap.*` events follow | **half** | §5.1 — `started: true` measured, and the session did not terminate; **no `dap.*` events**, because the bridge's tracker runs in the laptop host and this adapter runs in the seat. Not a session failure; the session is proved by #4 instead |
| 4 | a breakpoint in `src/fastcs_example/controllers.py` binds and is hit | **pass** | §5.2 — stopped at `update_voltages:99` with `controller`, `index`, `self` from the live workload; continued and re-hit |
| 5 | the memory limit is unchanged by a second, identical run | **pass** | §8.1 — 6Gi after run 2, 6Gi after run 3, no resize note and no resize |
| 6 | `restartCount` is still 0 on both application containers | **pass** | §9 — 0/0 at every check, start to finish |
| + | the new provision success line | **`ANSWERED`** | §3.1 — *"injected in 3.9s and the adapter answered a DAP `initialize` on 127.0.0.1:40448 in 0.01s"*. First live confirmation of the criterion |
| + | the 5b probe does not burn the adapter | **pass** | §6.1 — VS Code attached to the same port the probe had used and closed |
| + | the phase-8 open thread | **closed** | §6 — the seat's client log goes one line past where the redo's stopped |
| — | run 1 | **fail** | §2 — 409 on its own resize, no seat, silent 256Mi → 6Gi |

**Hand-fixes performed: none.** Nothing in checks 1–6 needed a manual repair of
the kind the redo had to perform. Run 1's failure was answered by re-running the
identical command, which is a retry and is reported as one — it is *explained*
(the race closes once the pod is already at 6Gi and no resize happens), but an
explained retry is still not a first-attempt pass, which is why §2 exists and
why the run-1 row above says fail.

---

## 8. Two more defects, both newly visible rather than newly created

### 8.1 The idempotence half is clean

Run 3, reconnecting to the same seat:

```
seat        p47-beamline/bl47p-ea-fastcs-01-0[podbench-1]  (reconnected)
  memory      5734Mi free of 7Gi (1434Mi in use)
```

`grep -ciE "resiz|6Gi"` over the whole of run 2's and run 3's output returns
**0** for both: neither run resized, and neither mentioned the limit run 1 had
already set. The limit read from the API before and after:

```
limit bl47p-ea-fastcs-01 6Gi req 615Mi      # after run 2
limit bl47p-ea-fastcs-01 6Gi req 615Mi      # after run 3
```

Slice 1's property holds on a live pod: the first run sets 6Gi, the second is a
no-op, and there is no ratchet.

### 8.2 A reconnect re-provisions a pid that already has a server, and writes a dead port

Run 3's provision, verbatim:

```
debug-config: --provision: injected in 1.8s, but nothing is listening on 127.0.0.1:37516 (Connection refused): the injector returned 0 and left no server behind, so no debug session can be started and a configuration pointing here connects to a closed port. The injection command printed below runs the same thing by hand
```

That is `_proof`'s **`Handshake.REFUSED`** sentence, and it is correct: the
injector exited 0 and nothing was listening. The launch.json diff between run 2
and run 3 is three port numbers and nothing else —

```diff
-            "port": 40448        +            "port": 37516     # pid 13, live -> dead
-            "port": 55672        +            "port": 58126     # pid 7
-            "port": 43145        +            "port": 41715     # pid 12
```

— so run 3 **replaced the working configuration with one pointing at a closed
port**, while the file stayed structurally idempotent (no duplicated entries,
comments still intact).

**Measured**: injection into pid 13 a second time exits 0 and starts no server;
podbench then emits a configuration for that dead port. **Inferred**: debugpy's
`api.listen()` refuses a second call in a process that already has one, so the
bootstrap does nothing and returns cleanly.

This is a real defect and it is on the reconnect path, which is the common case.
But note what changed: **before slice 5b this run would have printed "injected
in 1.8s; the app now serves debugpy on 127.0.0.1:37516" and the user would have
pressed F5 into silence.** The failure is now stated on the line that made it.
Two candidate fixes suggest themselves and neither was tried here: reuse the
port an existing server is already on (the emit stage already knows how to find
one — §3.1 quotes it doing so), or refuse to re-provision a pid that has debugpy
mapped.

### 8.3 A small one: the adapter becomes a debug candidate

Run 3's `notes` row listed `181 (python3)` — the debug adapter podbench itself
started — among the debuggable pids, and emitted for it:

```
debug-config: could not read /proc/181/exe (it needs PTRACE_MODE_READ), so the language was decided from the command line alone
debug-config: pid 181 (pid 181): unknown target, observe mode
debug-config: pid 181 (pid 181): nothing emitted — could not read /proc/181/exe ...
```

It emitted nothing, so it costs the file nothing; it costs the report three
lines and the `notes` row one entry naming a process podbench created. Cosmetic,
recorded, not chased.

---

## 9. State left behind

* **`bl47p-ea-fastcs-01-0` still exists and was never recreated.**

  ```
  container bl47p-ea-fastcs-01 restartCount 0 ready True
  container temp-controller-simulator restartCount 0 ready True
  ephemeralContainers ['podbench-1']
  eph podbench-1 state ['running']
  limit bl47p-ea-fastcs-01 6Gi req 615Mi
  limit temp-controller-simulator 1Gi req 256Mi
  ```

  **`restartCount` is 0 on both application containers, and was 0 at every check
  through the run.** The falsification condition holds.
* **The target container's memory limit is 6Gi, up from its template 256Mi**,
  and its request 615Mi up from 64Mi. That is slice 1's flat `EDITOR_LIMIT`,
  applied by run 1. It is not reverted. Nothing in the chart knows about it, so
  the next ArgoCD sync or pod recreation returns it to 256Mi.
* **Exactly one seat, `podbench-1`, still running**, landed 09:52:53Z. Its
  laptop-side stanza is at
  `~/.podbench/config.d/p47-beamline-bl47p-ea-fastcs-01-0-1.conf` and was left in
  place. `ssh podbench-p47-beamline-bl47p-ea-fastcs-01-0-1` reaches it.
* **The workload is running and not paused**: `/proc/13/stat` state `S`, all
  breakpoints cleared, `Threads: 34` — back to its pre-injection count.
* **No debugpy server is listening any more.** The adapter (pid 181) is gone and
  port 40448 was in `TIME_WAIT` at the last check. Ten `debugpy`/`pydevd`
  mappings remain in pid 13's address space. **Measured**: the adapter died some
  time during run 3. **Inferred**: run 3's window reload replaced the extension
  host (`exthost1` → `exthost2`, the second's debugpy log empty), the DAP client
  went with it, and the adapter exited when its only client disconnected with the
  debuggee detached.
* **`/podbench/app/.vscode/launch.json` is modified on the claim** and carries
  three `podbench: attach to …` configurations whose ports are all dead (§8.2).
  Its committed content is untouched — comments, both original configurations,
  the trailing comma. `settings.json`, `extensions.json` and `tasks.json` were
  reported by run 3 as already saying everything podbench would. **The claim is
  an NFS PVC, so this survives pod recreation**; a `git checkout .vscode` in the
  claim restores it.
* **`/podbench/app/.podbench-debugpy` was reinstalled twice** (runs 2 and 3),
  both times over itself, as podbench said it would.
* **A VS Code window is open on Giles' desktop** — the bridge profile's own
  instance, `vsc.py ls` reporting `pid=690290 remote=ssh-remote
  vscode-remote://ssh-remote%2B…/podbench/app`, with no debug session. Closing it
  is `vsc.py cmd workbench.action.closeWindow`; the seat outlives it.
* **Nothing else was mutated.** No `git` command was run, nothing under `src/`
  was touched, and `just lint` / `just test` were not run.

---

## Not measured

* **Whether run 1's 409 reproduces.** It was seen once, on the one run where a
  resize preceded the ephemeral-container write. The mechanism in §2 is read out
  of `kubectl.py` and from the timing, not from a second occurrence — the second
  run could not reproduce it because the pod no longer needed resizing. A
  deliberate reproduction needs a pod put back to 256Mi.
* **The adapter's own log.** `--log-to` was deliberately not passed, so the
  literal `Starting message loop for channel Client` line §8.2 named was never
  written. The client's log (§6) and the working session are the substitutes,
  and they are stronger, but the exact line is not in this file.
* **Why the bridge sees no `dap.*` traffic for a seat-side adapter.** §5.1 gives
  the extension-host split as the reason and it is consistent with the README's
  own `extensionKind: ["ui"]` note, but no experiment isolated it — a local
  debugpy session in the same window was not run as a control.
* **Whether a third injection into pid 13 would now succeed.** Its pydevd threads
  have exited (34 again) while the debugpy modules stay mapped, so §8.2's
  inference about `listen()` refusing a second call is untested in that state.
* **Whether the reload warning is ever load-bearing on this path.** §5.3 measured
  that it was not needed here. A cold seat on a pod that has never had one — the
  case `vscode-in-a-seat` says is the only real test of the install path — was
  not run, because this pod had had a seat earlier in the day.
* **`podbench vscode` against a pod with no claim.** Everything here is the
  hotfixed shape, where slice 2 sends debugpy to the claim. `/opt/podbench-debugpy`
  and the `gdb-across-namespaces` interpreter collision it can create were not
  exercised.
