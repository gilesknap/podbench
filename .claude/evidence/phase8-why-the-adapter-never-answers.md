# Phase 8 — why does the adapter never answer `initialize`?

2026-08-24, `p47-beamline` on pollux, through
`k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig`, against the real
`bl47p-ea-fastcs-01-0` on node `bl47p-ea-serv-01.diamond.ac.uk`. Launcher under
test: `/home/giles/code/podbench/.venv/bin/podbench`, version
`0.7.3.dev50+g75e5ef79a.d20260824`, branch `vscode/actually-debugs`. Seat image
pinned: `ghcr.io/gilesknap/podbench:0.7.3-beta.1-vscode-actually-debugs`
(`--pull always`; the seat reports `0.7.3.dev54+g67dcd1f76`).

This is slice 5 of `.claude/plans/vscode-actually-debugs.md` — the one open
question the plan carries, left by §4 of
`.claude/evidence/phase7-vscode-and-the-two-failures.md`. It is a diagnosis, not
a fix: no code under `src/` was touched and no git command was run.

**Headline, stated before the evidence so nothing here reads as a hedge:** with
debugpy's own logging turned on in the target and in the adapter, the complete
DAP chain was measured working on this exact target shape — `initialize`
answered in 0.00 s, `attach` in 4.00 s, `threads` returning the workload's four
real threads — both from a hand-driven injection and from podbench's own
`--provision`. The redo's §4 symptom **did not reproduce**, and four deliberate
attempts to wedge the adapter all failed. One thing §4 asserted is measurably
wrong and is named in §3 below. **Why the redo's client got 0 bytes is still not
measured**, and this file does not pretend otherwise.

---

## 0. The state the run started in

The pod had been recreated clean immediately before this run:

```
$ kubectl get pod bl47p-ea-fastcs-01-0 -n p47-beamline -o json | ...
creationTimestamp 2026-08-24T09:27:35Z
phase Running
hostNetwork True
node bl47p-ea-serv-01.diamond.ac.uk
container bl47p-ea-fastcs-01 restartCount 0 ready True
container temp-controller-simulator restartCount 0 ready True
ephemeralContainers []
ephStatuses []
spec bl47p-ea-fastcs-01 {'limits': {'cpu': '500m', 'ephemeral-storage': '2Gi', 'memory': '256Mi'}, ...}
spec temp-controller-simulator {'limits': {'cpu': '1', 'ephemeral-storage': '2Gi', 'memory': '1Gi'}, ...}
```

**Zero seats. Zero ephemeral containers. `restartCount 0/0`. The target
container at its template 256Mi limit** — not the 3Gi the redo left behind, so
none of this run's measurements inherit that resize.

Hotfix-wired, and the claim survives the pod because it is an NFS PVC — so the
redo's deliberate artefact was still there before a seat existed:

```
$ kubectl exec ... -c bl47p-ea-fastcs-01 -- ls -a /podbench/app
.podbench-debugpy      .podbench-hotfix.json      .python  .venv  .vscode  src  ...
```

and the process tree is the hotfixed shape, pid 13 the workload:

```
37887  1  0  bash -c while :; do ... exec bash -c 'stdio-socket --ptty "fastcs-example run ..."'
37887  7  1  /podbench/app/.venv/bin/python /podbench/app/.venv/bin/stdio-socket --ptty fastcs-example run ...
37887 10  7  /bin/sh -c pptty "stdbuf -oL -eL fastcs-example run ..."
37887 12 10  /podbench/app/.venv/bin/python /podbench/app/.venv/bin/pptty stdbuf -oL -eL fastcs-example run ...
37887 13 12  /podbench/app/.venv/bin/python3 /podbench/app/.venv/bin/fastcs-example run /epics/ioc/config/controller.yaml
```

---

## 1. One seat, landed once

```
KUBECONFIG=... ./.venv/bin/podbench attach bl47p-ea-fastcs-01-0 -n p47-beamline \
  --image ghcr.io/gilesknap/podbench:0.7.3-beta.1-vscode-actually-debugs --pull always
```

`exit=0`. **`podbench-1`, and nothing else, for the whole run.** `attach` and
not `vscode`, deliberately: this slice needs a debug adapter, not an editor, and
`vscode` is the verb that would have resized the pod. The limit stayed 256Mi
throughout.

```
rung        degraded - uid 37887, gid 37887, CapEff 0000000000000000
ladder
  degraded  landed   running since 2026-08-24T09:32:15Z
  [x] live attach (gdb -p <pid>)
      no deadline: 'bl47p-ea-fastcs-01' declares no readiness, liveness
      or startup probe, so nothing removes it from a Service or restarts
      it while it is stopped
measured
  ids         seat 37887:37887, target 37887:37887 (pid 13)
  memory      1082Mi free of 1280Mi (198Mi in use)
WARNING  this pod carries the hotfix layout, so the claim 'podbench-app'
         was mounted into the seat at /podbench/app without being asked
         for: hotfix mode needs it at the same path in both
```

The "no deadline" row is why every ptrace pause below was free: this container
declares no probes, so the `vscode-in-a-seat` breakpoint timer does not apply to
it.

---

## 2. Read the mechanism before reading a socket table

`podbench.flavour.injection_command` emits, and `provision.inject_debugpy` runs
under `timeout`:

```
PYTHONPATH=/proc/13/root/podbench/app/.podbench-debugpy \
  /app/.venv/bin/python -m debugpy --listen 127.0.0.1:<port> --pid 13
```

debugpy 1.8.21's attach-by-pid architecture, read out of the tree that is
actually installed in the claim rather than recalled:

- `debugpy/server/cli.py:attach_to_pid` computes
  `script_dir = os.path.dirname(debugpy.server.__file__)` — **the path as the
  driver sees it** — and drives gdb to `DoAttach` a one-line bootstrap into the
  target. The driver's job ends there.
- `debugpy/server/attach_pid_injected.py` then runs **inside the target** and
  calls `debugpy.listen(address)`.
- `debugpy/server/api.py:listen` (lines 139-280) does four things in order:
  1. binds an **ephemeral endpoints listener in the debuggee**, port E;
  2. spawns the adapter as a child of the *target*, argv
     `[sys.executable, .../debugpy/adapter, "--for-server", str(E), "--host", H,
     "--port", P, "--server-access-token", ...]`;
  3. **`endpoints_listener.accept()`** — the adapter connects *back* to E and
     posts `{"client": {...}, "server": {...}}`, after which E is closed in the
     `finally`;
  4. `_settrace(host=server_host, port=server_port, block_until_connected=True)`
     — pydevd connects out to the adapter's *server* port.

**So `--for-server E` is the debuggee's transient endpoints port, not the
adapter's server-facing port.** It lives for about one second and is then
closed. Its absence from the socket table is the normal, healthy state.

The adapter's real server port is a *different*, kernel-chosen number that
appears in exactly two places: the adapter's own log
(`Listening for incoming Server connections on 127.0.0.1:<n>`) and the
`debugpySockets` DAP event, where it is flagged `"internal": true`.

**This matters because §4 of the redo read `--for-server 33215` as the adapter's
server-facing port and concluded from its absence that "the debuggee half of the
session was never established".** That inference does not follow. The redo's own
quoted table is consistent with a completely healthy adapter.

---

## 3. Instrumentation

Two instruments, both of which §4 explicitly lacked.

**(a) debugpy's own logs, in all three processes.** `DEBUGPY_LOG_DIR` reaches
only the driver: the injected code takes `setup["log_to"]`, which `cli.py:427`
fills from the **`--log-to`** flag alone. Passing `--log-to` therefore turns on
all three at once — driver, target, and (via `api.py:193`,
`adapter_args += ["--log-dir", log.log_dir]`) the adapter. The path is used as a
string in both mount namespaces, so it has to be one that means the same file on
both sides; the claim is exactly that, and the redo already proved it:

```
$ stat -c '%d:%i %n' /podbench/app /proc/13/root/podbench/app
1048592:64 /podbench/app
1048592:64 /proc/13/root/podbench/app
```

**(b) A hand-rolled DAP client**, `Content-Length: N\r\n\r\n{json}` framed, that
prints every message it receives with the round-trip time, and can drive
`initialize` → `attach` → `configurationDone` → `threads` → `disconnect`.

The injection, run from the seat exactly as podbench would but with `--log-to`
added and the port pinned so the socket table is greppable:

```
$ timeout --kill-after=5 120 sh -c "PYTHONPATH=/proc/13/root/podbench/app/.podbench-debugpy \
    /app/.venv/bin/python -m debugpy --log-to /proc/13/root/podbench/app/.podbench-logs \
    --listen 127.0.0.1:45678 --pid 13"
...
$3 = 0
[Inferior 1 (process 13) detached]
RC=0 elapsed=4s
```

---

## 4. Raw evidence: every link of the chain, measured

### 4.1 The target: `pydevd is connected to adapter`

`/podbench/app/.podbench-logs/debugpy.server-13.log`, verbatim:

```
I+00000.016: Linux-3.10.0-1160.119.1.el7.x86_64-x86_64-with-glibc2.39 x86_64
             CPython 3.11.13 (64-bit)
             debugpy 1.8.21

I+00000.863: Initial environment:
             System paths:
                 sys.executable: /podbench/app/.venv/bin/python3(/podbench/app/.python/cpython-3.11.13-linux-x86_64-gnu/bin/python3.11)
                 debugpy.__file__: /proc/13/root/podbench/app/.podbench-debugpy/debugpy/__init__.py(/podbench/app/.podbench-debugpy/debugpy/__init__.py)

D+00000.863: listen(['127.0.0.1', 45678], **{})

I+00000.867: Waiting for adapter endpoints on 127.0.0.1:56253...

I+00000.867: debugpy.listen() spawning adapter: [
                 "/podbench/app/.venv/bin/python3",
                 "/proc/13/root/podbench/app/.podbench-debugpy/debugpy/adapter",
                 "--for-server", "56253",
                 "--host", "127.0.0.1",
                 "--port", "45678",
                 "--server-access-token", "2af49ab2...",
                 "--log-dir", "/proc/13/root/podbench/app/.podbench-logs"
             ]

I+00001.749: Endpoints received from adapter: {
                 "client": { "host": "127.0.0.1", "port": 45678 },
                 "server": { "host": "127.0.0.1", "port": 41095 }
             }

I+00001.749: Adapter is accepting incoming client connections on 127.0.0.1:45678

D+00001.749: pydevd.settrace(*(), **{'host': '127.0.0.1', 'port': 41095, ...
             'block_until_connected': True, 'access_token': '2af49ab2...'})

I+00001.893: pydevd is connected to adapter at 127.0.0.1:41095

I+00001.893: debugpy injected successfully
```

**`pydevd is connected to adapter at 127.0.0.1:41095` is precisely the link §4
declared absent.** It is present, it took 144 ms, and the target logged no error
anywhere.

### 4.2 The adapter: both listeners up, server session authorised

`debugpy.adapter-218.log`, verbatim:

```
I+00000.052: CPython 3.11.13 (64-bit) / debugpy 1.8.21
I+00000.679: debugpy.adapter startup environment:
                 sys.executable: /podbench/app/.venv/bin/python3(/podbench/app/.python/cpython-3.11.13-linux-x86_64-gnu/bin/python3.11)
                 debugpy.__file__: /proc/13/root/podbench/app/.podbench-debugpy/debugpy/adapter/../../debugpy/__init__.py(/podbench/app/.podbench-debugpy/debugpy/__init__.py)

I+00000.679: Listening for incoming Client connections on 127.0.0.1:45678...
I+00000.679: Listening for incoming Server connections on 127.0.0.1:41095...
I+00000.679: Sending endpoints info to debug server at localhost:56253:
             { "client": {"host": "127.0.0.1", "port": 45678},
               "server": {"host": "127.0.0.1", "port": 41095} }
I+00000.683: Accepted incoming Server connection from 127.0.0.1:46440.
D+00000.683: Starting message loop for channel Server[?]
D+00000.683: Server[?] <-- { "command": "pydevdAuthorize", "arguments": {"debugServerAccessToken": "2af49ab2..."} }
D+00000.684: Server[?] --> { "success": true, "command": "pydevdAuthorize", "body": {"clientAccessToken": null} }
D+00000.684: Server[?] <-- { "command": "pydevdSystemInfo" }
D+00000.725: Server[?] --> { "success": true, ... "python": {"version": "3.11.13final0", ...
I+00000.725: No active debug session for parent process of Server[pid=13].
```

That last line is not an error — it is the adapter noting there is no *parent*
session to graft a subprocess onto. It appears in every healthy run here.

### 4.3 The socket table, read against the right port

```
$ grep -iE ":(B26E|A087) " /proc/net/tcp      # 45678 = 0xB26E, 41095 = 0xA087
  16: 0100007F:A087 00000000:0000 0A ...   # 41095 LISTEN  (adapter <- server)
  25: 0100007F:B26E 00000000:0000 0A ...   # 45678 LISTEN  (adapter <- client)
  44: 0100007F:B568 0100007F:A087 01 ...   # 46440 -> 41095 ESTABLISHED (pydevd -> adapter)
 197: 0100007F:A087 0100007F:B568 01 ...   # the other half of the same pair
```

**The ESTABLISHED pair is the check §4 should have made and did not.** It is
invisible if you grep for the `--for-server` value, because that is a different
port that has already been closed.

### 4.4 `initialize` is answered, and answered locally

```
$ /app/.venv/bin/python .../dap.py 45678 15
connect 127.0.0.1:45678 -> OK
send    initialize (seq=1)
recv    event output {"category": "telemetry", "output": "ptvsd", "data": {"packageVersion": "1.8.21"}}
recv    event output {"category": "telemetry", "output": "debugpy", "data": {"packageVersion": "1.8.21"}}
recv    event debugpySockets {"sockets": [{"host": "127.0.0.1", "port": 45678, "internal": false}, {"host": "127.0.0.1", "port": 41095, "internal": true}]}
recv    response initialize success=True
```

`adapter/clients.py:148 initialize_request` returns the capability dictionary
inline. **It needs no round trip to the debuggee**, so an adapter that accepts
TCP and never answers `initialize` is an adapter whose *client message loop
never started*, not one whose debuggee is missing. That distinction is what
makes §4's inference unsafe even before the measurements above.

### 4.5 A whole session, end to end

```
connect 127.0.0.1:45678 -> OK
send    initialize (seq=1)
recv    response initialize success=True in 0.00s
send    attach (seq=2)
recv    event debugpyWaitingForServer {"host": "127.0.0.1", "port": 41095}
recv    event initialized null
send    configurationDone (seq=3)
recv    response configurationDone success=True in 0.00s
recv    response attach success=True in 4.00s
recv    event process {"name": "/podbench/app/.venv/bin/fastcs-example", "systemProcessId": 13, "isLocalProcess": true, "startMethod": "attach"}
recv    event thread {"reason": "started", "threadId": 1}
... threadId 2, 3, 4
send    threads (seq=4)
recv    response threads success=True in 0.00s
        threads={"threads": [{"id": 1, "name": "MainThread"}, {"id": 2, "name": "asyncio_0"},
                             {"id": 3, "name": "IPythonHistorySavingThread"}, {"id": 4, "name": "patch-stdout-flush-thread"}]}
send    disconnect (seq=5)
recv    event terminated null
recv    response disconnect success=True in 0.00s
recv    EOF
```

Those are the workload's real threads. A second, identical session on the same
adapter immediately afterwards produced byte-for-byte the same transcript.

`attach` costing a flat **4.00 s** in every run is worth recording as an
observation, not a defect: the adapter emits `debugpyWaitingForServer` and then
completes on a fixed poll. A client that gives up in under ~5 s will see the
session fail with everything working.

---

## 5. The two candidates the plan named, both ruled out — measured

### 5.1 `hostNetwork: true` and `127.0.0.1`

**Ruled out.** The seat, the target and the adapter share the node's network
namespace, so the `127.0.0.1:41095` pydevd dialled is the same socket the
adapter bound. §4.1 records the connect, §4.3 records the ESTABLISHED pair from
the shared table. This was the plan's "first thing to check" and it is clean.

The real consequence of `hostNetwork` here is the one podbench already prints —
the server authenticates no client, so any hostNetwork pod or node daemon on
`bl47p-ea-serv-01` can reach it — not a loopback mismatch.

### 5.2 The `gdb-across-namespaces` interpreter split

**Ruled out for this shape, and the reason is worth keeping.** The split is real:

```
driver  /app/.venv/bin/python        Python 3.11.16   (the seat image's)
target  /proc/13/exe -> /podbench/app/.python/cpython-3.11.13-linux-x86_64-gnu/bin/python3.11
```

but it does not bite, for three measured reasons:

1. **The debugpy tree is the claim's, at the same inode in both namespaces**
   (§3). `script_dir` is injected as `/proc/13/root/podbench/app/.podbench-debugpy/...`,
   which resolves *inside* the target to that same inode — `/proc/<pid>/root`
   for the target's own pid is its own root. The driver and the debuggee load
   the same files. The redo's slice-2 remedy is what buys this.
2. **The adapter is spawned by the target's interpreter, not the seat's** —
   `api.py:181` uses `_config["python"] = sys.executable`, evaluated in the
   debuggee. The adapter log's `sys.executable` line above is the claim's
   3.11.13, not `/app/.venv`.
3. **Both interpreters are 3.11**, so the single `pydevd_cython.cpython-311-*.so`
   in the tree matches on both sides.

This is a *candidate ruled out by measurement on this pod*, not a general
acquittal. On a target whose Python minor differs from the seat's, or on a pod
with no claim (where `PROVISION_DEST` is `/opt/podbench-debugpy` and the seat
image has its own `/python/cpython-*`), the collision the skill describes is
still live.

---

## 6. Four attempts to reproduce the wedge, all negative

Each ran against a live, healthy adapter. None produced §4's symptom.

| # | what was done | result |
|---|---|---|
| W3 | a bare TCP connection opened and held, sending nothing, while a real client connects — **the shape of §2's own "a TCP connect to 37189 from the seat succeeded" probe** | second client served, `initialize success=True` |
| W1 | a client sends `initialize` + `attach`, then is destroyed with `SO_LINGER 0` (RST, no `disconnect`); a fresh client follows | fresh client served, `initialize success=True` |
| W2 | client A holds a *live attached* session; client B connects alongside it | B served, `initialize success=True` |
| — | a clean `disconnect`, then a second full session on the same adapter | identical full transcript |

The adapter also survived having its own installation tree reinstalled under it
(§7): after `uv pip install --target` overwrote `.podbench-debugpy`, pid 13's
adapter still answered `initialize`.

---

## 7. The product path, on a virgin process, with no pod churn

The runs above drove the injection by hand. To test podbench's own code path —
its own port choice, its own success line, no `--log-to` — without a second seat
and without recreating the pod, it was pointed at **pid 7 (`stdio-socket`)**, a
Python process into which nothing had ever been injected:

```
$ podbench debug-config 7 --provision --provision-dest /podbench/app/.podbench-debugpy --print-config
debug-config: --provision: running `uv pip install --no-cache --python-version 3.11
  --target /proc/7/root/podbench/app/.podbench-debugpy debugpy`
debug-config: --provision: installed debugpy for Python 3.11 into /proc/7/root/podbench/app/.podbench-debugpy
debug-config: --provision: this pod runs with hostNetwork: true, so 127.0.0.1:52152 is the *node's* loopback ...
debug-config: --provision: injected in 3.3s; the app now serves debugpy on 127.0.0.1:52152
debug-config: emitting debugpy: a debugpy server is already listening on 127.0.0.1:52152, held by pid 461
  (/podbench/app/.venv/bin/python /proc/7/root/podbench/app/.podbench-debugpy/debugpy/adapter
   --for-server 51291 --host 127.0.0.1 --port 52152 --server-access-token 9fd8ee43...)
```

and then, against podbench's own port:

```
$ grep -i "CBB8" /proc/net/tcp                       # 52152
   1: 0100007F:CBB8 00000000:0000 0A ...             # one LISTEN row, nothing else
$ ls -l /proc/461/fd | grep -c socket
3

connect 127.0.0.1:52152 -> OK
send    initialize (seq=1)
recv    event debugpySockets {"sockets": [{"host": "127.0.0.1", "port": 52152, "internal": false},
                                          {"host": "127.0.0.1", "port": 33065, "internal": true}]}
recv    response initialize success=True in 0.00s
send    attach (seq=2)
recv    response attach success=True in 4.00s
recv    event process {"name": "/podbench/app/.venv/bin/stdio-socket", "systemProcessId": 7, ...}
send    threads (seq=4)
recv    response threads success=True in 0.00s
        threads={"threads": [{"id": 1, "name": "MainThread"}, {"id": 2, "name": "asyncio-waitpid-0"}]}
```

Two things in that block deserve to be carried forward:

- **`3` socket fds is the healthy count** — client listener, server listener,
  accepted server connection. §4 offered "the adapter is alive and holds only
  three socket fds" as evidence of a wedge. It is evidence of nothing.
- **The socket table shows one row for the client port and none for
  `--for-server 51291`, in a session that works perfectly.** That is §4's
  socket-table observation reproduced *on a healthy adapter*.

---

## 8. The cause

Two answers, and they must not be run together.

### 8.1 Named, and measured: §4's reasoning does not hold

`--for-server <n>` is the **debuggee's transient endpoints listener**, closed
about a second after the adapter reports its ports (`api.py:139-255`,
`finally: endpoints_listener.close()`). Finding no rows for it, and finding an
adapter with three socket fds, are both the *normal* state of a working session
— reproduced above on two working sessions. §4's conclusion, "the debuggee half
of the session was never established", was inferred from evidence that cannot
support it, and the correct check — an ESTABLISHED pair to the port the adapter
logs as `internal: true` — was not made.

`initialize` is also answered by the adapter *locally*, with no debuggee round
trip, so "no answer to `initialize`" could never have been diagnostic of a
missing debuggee in the first place.

### 8.2 Still not measured: why the redo's client got 0 bytes

**The redo's symptom did not reproduce, and this run cannot say why it happened.
Saying so is the finding.** The plan's own falsification condition —
"falsified if a session is reported as working after an unexplained retry" —
applies to this file, so it is stated flatly rather than dressed up:

- Everything measured here says the chain works: the target connects back, the
  adapter serves clients, sessions attach, threads enumerate, and a second
  session on the same adapter works too.
- Nothing measured here explains a client connecting to a live adapter and
  receiving zero bytes. Four attempts to induce it failed.
- The evidence that would have settled it is **gone**: `podbench-home` is an
  `emptyDir` (`/proc/self/mountinfo` shows
  `kubernetes.io~empty-dir/podbench-home`), so the redo's seat, its
  vscode-server and its extension-host log died with the pod at 09:27.

Two *unmeasured* candidates remain, and they are recorded as candidates:

1. **The redo's hand-rolled client.** Its source is not in the evidence file. A
   framing error — `\n\n` instead of `\r\n\r\n`, or a missing `Content-Length` —
   produces exactly "connected, sent, 0 bytes, forever", because
   `messaging.JsonIOStream` blocks reading headers. If that is what happened,
   §4's *isolation* was invalid and the only real failure was VS Code's.
2. **The VS Code side alone.** The extension log stopping at
   `Connecting to DAP Server at: 127.0.0.1:37189` is consistent with the window
   never getting as far as sending `initialize`, which is a VS Code/Remote-SSH
   question and not an adapter one. **This run could not test it**: driving VS
   Code needs the GUI and the human-present exception, which a subagent over
   `kubectl exec` does not have.

**The next instrument is therefore VS Code itself, not more socket archaeology.**
Drive one `podbench vscode` run with `--log-to` wired into the provision so all
three debugpy logs exist, press F5, and read the adapter's log for a
`Starting message loop for channel Client` line. If it is there, the adapter
served the window and the fault is downstream; if it is absent while the TCP
connection exists, the client message loop genuinely never started and *that* is
the bug to chase. Slice 6 is the run that can do this.

### 8.3 What this does to slice 5b

Slice 5b stands, and is now better supported. `--provision`'s success line is
returned on the injector's exit code; the redo added that an open port is not
proof either; this run adds a third: **an adapter with a listening port, three
socket fds and no rows for `--for-server` is indistinguishable, by every signal
podbench currently reads, from the wedged one §4 reported.** The assertion worth
making is a real `initialize` that gets an answer — which §4.4 shows costs
0.00 s and about thirty lines of client — and, if a cheaper check is wanted
first, an ESTABLISHED connection from the target's pid to the port the adapter
reports as `internal: true`.

---

## 9. What would falsify §8.1

- An adapter log from a failing run showing `Listening for incoming Client
  connections` but **no** `Starting message loop for channel Client` after a TCP
  connection is made. That would mean the client loop really can fail to start
  and §8.1's correction, while still true about `--for-server`, would not be the
  whole story.
- A run in which pydevd's connect-back genuinely fails: the target's
  `debugpy.server-<pid>.log` would show `listen()` raising, or hang at
  `pydevd.settrace(... block_until_connected=True)` with no
  `pydevd is connected to adapter` line.
- Any reproduction at all of "connect OK, `initialize` sent, 0 bytes" against an
  adapter whose log shows a healthy Server channel.

---

## 10. State left behind

* **`bl47p-ea-fastcs-01-0`** was **deleted at the end of the run and recreated by
  the StatefulSet**, deliberately, so slice 6 starts from a clean pod rather than
  from two injected processes and a burnt seat name:

  ```
  creationTimestamp 2026-08-24T09:44:25Z Running
  container bl47p-ea-fastcs-01 restartCount 0 ready True
  container temp-controller-simulator restartCount 0 ready True
  ephemeralContainers []
  limits {'bl47p-ea-fastcs-01': '256Mi', 'temp-controller-simulator': '1Gi'}
  ```

  **`restartCount 0/0` on both application containers**, and it was 0/0
  throughout the run as well — the falsification condition holds. The memory
  limit is the template's 256Mi: nothing in this run resized the pod.
* **Seats: exactly one, `podbench-1`, for the whole run**, landed 09:32:15Z and
  outlived by the pod deletion. The pod that carries it no longer exists, so the
  live count is now zero and the `podbench-1` name is free again on the new pod.
  Its laptop-side stanza (`~/.podbench/config.d/p47-beamline-bl47p-ea-fastcs-01-0-1.conf`)
  was removed.
* **The claim** still carries `.podbench-debugpy` — the redo's deliberate
  artefact, reinstalled in place by §7's `--provision` (podbench said so:
  "is this flag's own destination, so it is installed over rather than kept").
  The scratch `.podbench-logs/` this run created, holding the four debugpy logs
  and the probe scripts, has been **removed**; the logs are copied out and the
  load-bearing lines are quoted above.
* **The application's `.vscode/` is untouched**, and slice 3's fixture is intact
  — `launch.json` still carries its four `//` comments, its two committed
  configurations and the trailing comma in the second, all of which is what makes
  it fail strict JSON.
* **The two debugpy servers this run injected** — pid 13 on 127.0.0.1:45678 and
  pid 7 on 127.0.0.1:52152, both reachable from every hostNetwork pod on
  `bl47p-ea-serv-01` while they lived — **died with the pod.** Nothing is
  listening now.
* **Nothing else was mutated.** No `git` command was run, no file under `src/`
  was touched, and `just lint` / `just test` were not run (another agent is
  mid-edit in `provision.py`).

---

## Not measured

* **Why the redo's client received 0 bytes** — the question this slice exists
  for. Not reproduced in six attempts, and not explained. §8.2.
* **VS Code as the client.** Every DAP measurement here is from a hand-rolled
  client over `kubectl exec`. The redo's primary symptom was a VS Code window,
  and that half is untested; it needs the GUI and the human-present exception.
* **Whether a breakpoint binds and is hit.** The session attaches and enumerates
  threads; `setBreakpoints` was not exercised, so slice 6's fourth checkbox is
  still open. The `pathMappings` in the emitted configuration
  (`localRoot /proc/13/root`, `remoteRoot /`) were accepted by `attach` but not
  proven to resolve a real source file.
* **Whether the collision is live on a pod with no claim.** §5.2's acquittal
  rests on the debugpy tree being the claim's, at one inode in both namespaces.
  A bare pod, where `PROVISION_DEST` is `/opt/podbench-debugpy` and the seat's
  own `/python/cpython-*` is in play, was not tested.
* **The 4.00 s `attach`.** Reproducible to the centisecond across four sessions
  and two pids, so it is a fixed poll somewhere in `servers.wait_for_connection`
  rather than network time — but it was not traced to the code, and a client with
  a shorter deadline than that would see a working session fail.
