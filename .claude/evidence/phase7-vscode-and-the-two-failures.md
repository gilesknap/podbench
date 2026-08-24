# Phase 7 — VS Code, and the two named field failures

2026-08-24, `p47-beamline` on pollux, through
`k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig`, against the real
`bl47p-ea-fastcs-01-0`. Launcher under test:
`/home/giles/code/podbench/.venv/bin/podbench`, version
`0.7.3.dev41+g25fe18bd8`, branch `hotfix/easy-to-drive`. Seat image pinned:
`ghcr.io/gilesknap/podbench:0.7.3-beta.1-hotfix-easy-to-drive` (seat reports
`0.7.3.dev44+g649cf9188`).

**This file replaces an earlier one of the same name, committed as `649cf91`
and withdrawn.** That run drove VS Code against a pod the mutating walk had
correctly and completely *retired* — a bare pod with no claim, no supervisor
loop, and the application running the image's own `/app/.venv`. The two named
2026-08-23 field failures were seen against a **hotfixed** pod, which is a
different mount-namespace shape, so its findings could not be trusted. §6 below
records what that run got wrong and what it got right; the rest of this file is
a fresh run against a target put back into a genuinely hotfixed state.

Environment: `DISPLAY=:0`, `WAYLAND_DISPLAY=wayland-0`,
`XDG_RUNTIME_DIR=/run/user/1000`. A real GUI, a real window, human present —
the standing exception recorded in the `vscode-driving-stays-unsandboxed`
memory.

---

## 0. Putting the target back into a hotfixed state

The pod was deleted first, at Giles' instruction, so no seat from the withdrawn
run could survive into this one: ephemeral containers cannot be removed, only
outlived. `podbench-1` and `podbench-2` from that run died with it, and their
laptop-side ssh configs were removed from `~/.podbench/config.d/`.

The wiring was authored by podbench itself, not by hand:

```
podbench hotfix values --app bl47p-ea-fastcs-01 \
  --from-pod bl47p-ea-fastcs-01-0 -n p47-beamline \
  --values services/bl47p-ea-fastcs-01/values.yaml \
  --parent-values services/values.yaml
```

**This is the first live exercise of the from-scratch wiring path.** The
previous run's `values` diff was one line (`enabled: false → true`) only because
the wiring was already in the file; here the file had none, and `values`
emitted the whole shape — `podbench-app`/`podbench-home` volumes, the
`/podbench/app` mount, the supervisor loop, `podSecurityContext.fsGroup: 37887`
— plus two correct stderr notes that `volumes` and `volumeMounts` had to be
copied down from `services/values.yaml` because a helm list replaces rather
than merges across the parent/child merge. Committed as `c0fdd90` on
`podbench-hotfix-claim`, pushed **07:11:57Z**.

`--values` writes nothing in place: it emits the merged file on stdout and the
caller redirects. Worth knowing — the withdrawn run's phrasing ("merged into
the deployed values file with `--values`") reads as an in-place edit.

Sync: StatefulSet generation 19 → 20 at **07:14:30Z**, **2m33s** after the push
— inside the plan's "up to 2 minutes" only just, and faster than the 3m13s the
mutating walk measured. New pod `creationTimestamp 2026-08-24T07:14:26Z`, both
containers ready 07:14:55Z, `restartCount 0/0`, **no `ephemeralContainers`**.

### The shape this run exists to test

```
$ kubectl exec ... -c bl47p-ea-fastcs-01 -- ls -l /proc/13/exe
/proc/13/exe -> /podbench/app/.python/cpython-3.11.13-linux-x86_64-gnu/bin/python3.11
```

The application now runs the **claim's** uv-managed interpreter, not the
image's. Its first log line is `podbench: running the hotfixed project`, the
supervisor loop's own echo. Process tree: pid 1 `bash` (supervisor), 7
`stdio-socket`, 10 `sh`, 12 `pptty`, **13 `fastcs-example`** — the debuggable
target, 34 threads. This is exactly the `/python/cpython-<version>-<triple>/`
collision shape the repo's own hard rule and the `gdb-across-namespaces` skill
are about, and it is not present on a bare pod.

One consequence matters immediately, and is measured, not assumed:

```
/podbench/app/.venv/.../site-packages/debugpy   -> No such file or directory
/app/.venv/.../site-packages/debugpy            -> exists
```

**The hotfixed target runs an interpreter that has no debugpy**, while the
image's venv — the one it is no longer using — has one. `kubectl logs` carries
zero debugpy lines since pod start: a genuinely fresh process with an unspent
`debugpy.listen()` latch. That is the precondition the withdrawn run could not
offer.

---

## 1. One seat, landed once

```
podbench vscode bl47p-ea-fastcs-01 -n p47-beamline \
  --image ghcr.io/gilesknap/podbench:0.7.3-beta.1-hotfix-easy-to-drive --pull always
```

`exit=0`, 07:17:51Z → 07:18:07Z. `podbench-1` landed 07:18:01Z, rung
`degraded` — **uid 37887, gid 37887, `CapEff 0000000000000000`**. Exactly one
seat was landed in this entire run, so no second injection into the same pid
can confound anything below.

The report got the hotfix-specific decisions right:

```
  [ok] opening /podbench/app, not the seat's home /home/podbench: on a
       hotfixed pod the claim is the only tree here where an edit
       reaches the running process
  [ok] ssh reaches the seat, so Remote-SSH will too
```

and the window came up carrying the bridge, on the claim:

```
$ vsc.py ls
pid=529684  remote=ssh-remote
  vscode-remote://ssh-remote%2Bpodbench-...-0-1/podbench/app
```

The IPC hand-off trap did not recur (the shim always passes an explicit
`--user-data-dir`). `vsc.py text .vscode/launch.json` — a bare relative path —
returned **the seat's** file, re-confirming the bridge's 2026-08-23 assumption
on a hotfixed pod as well.

But the same report also carried this, twice:

```
  [warn] no launch.json: nothing above could be turned into one
```

**Failure 2, reproduced — and on a hotfixed pod it is not "debugging starts and
dies", it is "there is nothing to press F5 on".**

---

## 2. Failure 2, first cause: `--provision` writes to a directory the seat cannot write

The report's own narration names the step that failed:

```
debug-config: --provision: running `uv pip install --no-cache --python-version 3.11
  --target /proc/13/root/opt/podbench-debugpy debugpy`
debug-config: --provision: permission denied at /proc/13/root/opt/podbench-debugpy: ...
debug-config: --provision: not starting the server - debugpy is not importable by the target
debug-config: no debugger flavour could be emitted for this target
```

Measured from inside the seat, rather than reasoned:

| probe | result |
|---|---|
| `ls /proc/13/root/` | **succeeds** — so no `PTRACE_MODE_READ` refusal, no LSM denial |
| `ls -ld /proc/13/root/opt` | `drwxr-xr-x. root root` |
| seat `id` | `uid=37887(podbench) gid=37887` — **not root, no capabilities** |
| `mkdir /proc/13/root/opt/podbench-debugpy` | `Permission denied` |
| target `/` mount flags | `rw,relatime … overlay` — **not** read-only |

The cause is the plainest one available: **`/opt` is a root-owned 0755
directory and the seat is uid 37887.** Ordinary Unix file permissions.

### The explanation podbench prints rules that cause out

`provision.py:211-225`, the `EACCES`/`EPERM` branch of `blocker_sentence`:

```python
# Three distinct causes, and CAP_DAC_OVERRIDE covers only the first.
return (
    f"permission denied at {destination}: uid 0 in this seat carries "
    "CAP_DAC_OVERRIDE, so the target's own file modes are not it. What "
    "is left is the /proc/<pid>/root traversal, which takes "
    "PTRACE_MODE_READ ... or an LSM denying the cross-container write ..."
)
```

The module is written on the premise that the seat is uid 0 — the docstring at
`provision.py:9` says so outright ("which uid 0 writes through
`CAP_DAC_OVERRIDE` whatever the …"). On the **`full`** rung that premise holds
and the sentence is correct. On the **`degraded`** rung — a non-root uid with
an empty effective set, which is what this beamline pod admits and what the
report printed four lines earlier — the premise is false, file modes are the
entire cause, and the message **explicitly excludes the true explanation** and
sends the reader to SELinux, AppArmor and `CAP_SYS_PTRACE`.

`writable_blocker`'s docstring carries the same assumption ("`os.access`
reports on uid and modes, which `CAP_DAC_OVERRIDE` already makes irrelevant").

`PROVISION_DEST = "/opt/podbench-debugpy"` (`provision.py:79`) is a fixed
constant; grepping `src/` shows it is never varied for a hotfixed pod.

### The fix is available and works — proven, not proposed

On a hotfixed pod there is a location that is writable *and* mounted at the
**identical path in both mount namespaces**, which is exactly what the
`gdb-across-namespaces` problem demands:

```
$ stat -c '%d:%i %n' /podbench/app /proc/13/root/podbench/app
1048592:64 /podbench/app
1048592:64 /proc/13/root/podbench/app        # same device, same inode
$ ls -ld /proc/13/root/podbench/app
drwxrwxrwx. 13 podbench-99 99
```

Pointed there, every step podbench had given up on succeeded:

```
$ podbench debug-config 13 --provision --provision-dest /podbench/app/.podbench-debugpy
debug-config: --provision: installed debugpy for Python 3.11 into /proc/13/root/podbench/app/.podbench-debugpy
debug-config: --provision: injected in 8.7s; the app now serves debugpy on 127.0.0.1:37189
```

Corroborated three ways: the target's thread count went **34 → 38**; its log
printed pydevd's frozen-modules notice at 07:20:16Z with **no `RuntimeError`**
(a clean first `listen()` on a fresh process); and a TCP connect to 37189 from
the seat succeeded.

**So the whole cascade turns on one unwritable path**, and podbench already has
the flag to fix it — it simply does not choose it on a pod it has just
announced is hotfixed (`WARNING this pod carries the hotfix layout, so the
claim 'podbench-app' was mounted into the seat at /podbench/app`).

---

## 3. Failure 2, second cause: podbench cannot write into a real project's `.vscode`

Independent of the first, and fatal on its own. With configurations available,
the merge still refused:

```
debug-config: cannot parse the existing launch.json: Expecting property name
  enclosed in double quotes: line 2 column 5 (char 6). Re-run with --print-config
  and paste the configuration in by hand, or --output a different path.
```

and, in run 1's report, the same for settings:

```
  [warn] /podbench/app/.vscode/settings.json left exactly as it is:
         cannot parse the existing settings.json: Expecting property
         name enclosed in double quotes: line 11 column 5 (char 325)
```

Both files are **committed in the application's own repository** and unmodified:

```
$ git -C /podbench/app ls-files .vscode/
.vscode/extensions.json
.vscode/launch.json
.vscode/settings.json
.vscode/tasks.json
$ git -C /podbench/app status --short .vscode/     # empty
```

They fail strict JSON for two different ordinary reasons — `launch.json` on
`//` comments (VS Code's own scaffold writes them), `settings.json` on a
trailing comma before `}`. Both are valid JSONC, which is what VS Code
documents and writes.

**On a hotfixed pod this is the common case, not an edge case.** `podbench
vscode` deliberately opens the claim — the application's checkout — rather than
the seat's empty `/tmp/podbench-home`, and a normal Python project ships a
`.vscode/`. The project already knows about this asymmetry: the bridge's own
shim handles VS Code settings as "JSON with comments" and inserts after the
opening brace precisely so it cannot reorder or drop anything. podbench's
merge does not.

---

## 4. Failure 2, third cause: with both fixed, the adapter still never speaks DAP

This is the one the withdrawn run could not have reached, and it survives every
correction above.

With debugpy provisioned into the claim, a valid `launch.json` in place naming
the live port, and `ms-python.debugpy` v2026.6.0 installed into the seat's
vscode-server, a debug session was started through the bridge:

```
$ vsc.py debug "podbench: attach to fastcs-example [pid 13 fastcs-example] (debugpy)"
TimeoutError: timed out          # vsc.py's own recv, ~30s
$ vsc.py events
[ bridge.ready, terminal.open ]  # no dap.* at all
$ vsc.py info  -> debugSession: None
```

The seat's own extension log stops at the same place the withdrawn run's did:

```
07:23:39.952 [info] Resolving attach configuration with substituted variables
07:23:39.993 [info] createDebugAdapterDescriptor: request='attach' name='podbench: attach to ...'
07:23:39.993 [info] Connecting to DAP Server at:  127.0.0.1:37189
```

— and then nothing. **But this time the port is genuinely open**, so the
withdrawn run's explanation (a closed port behind a spent latch) cannot apply.
A hand-rolled DAP client from inside the seat isolates it:

```
connect 127.0.0.1:37189  -> OK
send    initialize
recv    -> TIMEOUT, 0 bytes in 15s
```

**The adapter accepts the TCP connection and never answers `initialize`.** The
reason is visible in the node's socket table — the adapter's own server-facing
port, `--for-server 33215` (`0x81BF`), has **no listener and no connection at
all**, while the client port 37189 (`0x9145`) is in `LISTEN`:

```
local=9145 rem=0000 st=0A   # 37189 LISTEN  (the IDE side)
                            # 33215: no rows whatsoever
```

The adapter process is alive and holds only three socket fds. So the debuggee
half of the session was never established: the adapter has nothing to attach an
IDE to, and blocks. The target's own log shows pydevd starting and records **no
error** at all.

**Named cause, stated at the precision it was measured:** `--provision`
reports success on the injector's exit code, and a listening socket does not
raise that bar — here both were satisfied and the session still cannot start,
because the adapter's server-side connection is absent. The withdrawn run
reached the right general criticism ("the exit code of the injector, nothing
about the socket") from an example that was self-inflicted; this run reaches it
from a fresh process, and tightens it: *an open port is not proof either.*

**Not measured:** *why* the target never completed its connection back to the
adapter. Its log carries no error, and this run did not instrument debugpy's
own logging inside the target. This is the one open thread and it should not be
reported as understood.

---

## 5. Failure 1 — the `--new` refusal: a correct refusal that names the wrong verb

Reproduced on the hotfixed pod. A throwaway `ed25519` key, and — importantly —
an ssh config in which the throwaway key is the *only* identity:

> A first attempt used `-o IdentitiesOnly=yes -i <throwaway>` against podbench's
> own generated config and **reached the seat**, which looks like the refusal
> not reproducing. It is a measurement artefact: `IdentitiesOnly` restricts
> ssh to the identities in the config file *plus* those given with `-i`, so the
> config's own `IdentityFile /home/giles/.ssh/id_ed25519` was still offered and
> still accepted. Rewriting the config's `IdentityFile` line is what isolates
> it. (The withdrawn run hit a different masking artefact in the same place — a
> live `ControlMaster` from a prior identity. Both were cleared here.)

Isolated properly, the seat genuinely refuses the key:

```
$ ssh -F <throwaway.conf> -o ControlMaster=no -o ControlPath=none ... true
exit=255
podbench@bl47p-ea-fastcs-01-0: Permission denied (publickey,keyboard-interactive).
```

and through the verb:

```
$ podbench vscode bl47p-ea-fastcs-01 -n p47-beamline --identity <throwaway>
exit=2
```

The **WARNING** in the report is measured, correct and verb-agnostic — #204's
rule working:

```
WARNING  this seat does not authorise the key being offered, and its
         authorized_keys cannot be added to from here, so ssh will be
         refused. `--new` lands a seat that takes it.
```

The **hard failure's cause list on stderr is not**. Two of its four bullets
hard-code a different verb, including the one that actually fired:

```
  - `Permission denied (publickey)`: this seat's authorized_keys was written
    when it started and does not carry the key in the stanza. `podbench
    attach --new` is the only way to change it
```

The user ran `podbench vscode`. `UNREACHABLE_CAUSES` (`editor.py:389-401`) is a
module-level constant reached only from `check_reachable` → `open_seat` → the
`vscode` verb's `_open_editor`; `attach` never reaches it. So the one code path
that can print this sentence is the one path on which its advice names the
wrong command.

**Verdict: a correct refusal that fails to explain itself.** An ephemeral
container's `authorized_keys` is genuinely immutable, so no seat carrying the
wrong key can be made to accept another — the refusal is right, and
auto-landing a replacement is what `attach-endgame` deliberately refused. This
is a message defect, not a behaviour defect, and per the task's rule it is
reported, not fixed. This verdict is unchanged from the withdrawn run — as
expected, since it turns on ssh keys and `authorized_keys` immutability, which
a hotfixed pod does not alter — but it is now established against the right
pod state rather than assumed to carry over.

---

## 6. What the withdrawn run got wrong

Its central claim about Failure 2 was that pid 12's `debugpy.listen()` latch was
already spent before its own first attempt, with *which* call consumed it
recorded as "not measured". **That is measurable from the pod's own log, and it
was that run's own first injection.** Read from the still-live pod before this
run deleted it (`--since-time` = its creation, 06:20:00Z):

| time (`+01:00`) | event |
|---|---|
| 07:20:28 | pod starts; **no debugpy for 16 minutes** |
| 07:36:47.875 | pydevd frozen-modules notice — **no error: this listen() succeeded** |
| 07:37:59 | first `RuntimeError: debugpy.listen() has already been called` |
| 07:40:12, 07:42:01, 07:44:40 | three more, all the same |

The first injection took the one-shot latch and every later one failed —
self-inflicted, exactly as suspected when the run was withdrawn. The file's
suggestion that the latch might predate its first `podbench vscode` landing
(06:36:41Z) is contradicted by its own timeline: 07:36:47 **+01:00** is
06:36:47Z, *after* that landing, not before.

What it got right, and this run confirms independently: Failure 1's verdict
(§5), that the bridge genuinely drives a real window, and the general criticism
that `--provision`'s success line measures the injector's exit code rather than
a working debugger (§4, now tightened).

---

## 7. Two lesser observations

**A compounding resize on every reconnect.** Run 1 raised the container's
memory limit to 1Gi (`resized … to memory 103Mi/1Gi`); the run in §5,
a plain reconnect to the same seat, raised it again to 2Gi (`205Mi/2Gi`). The
headroom row moved `1855Mi free of 2Gi` → `1637Mi free of 3Gi`. This is
consistent with sizing for *current* usage plus the 1215Mi vscode-server
measurement — usage was higher the second time because vscode-server was by
then running — so it may be intended. Flagged as observed, **not judged**: it
means a reconnect ratchets a workload's limit upward, which on a pod nobody
asked to resize is at least worth a deliberate decision.

**An unguarded traceback.** `podbench debug-config 13 --provision` run by hand
inside the seat with no `--output` crashed with a raw Python traceback —
`PermissionError: [Errno 13] Permission denied: '/.vscode'` from
`vscode.py:2077`, `path.parent.mkdir(parents=True, exist_ok=True)`, because the
default output path resolved against the seat's cwd of `/`. **Self-provoked**:
this is a seat-side verb that `podbench vscode` always calls with an explicit
output path, so no user following the documented path reaches it. Reported
because a raw traceback is never the intended failure mode, ranked last because
the invocation was mine.

---

## 8. State left behind

* **`bl47p-ea-fastcs-01-0`** — created 07:14:26Z, `Running`, `restartCount 0/0`
  on both application containers throughout (the falsification condition holds).
  Hotfix-wired. **One** ephemeral container, `podbench-1`, still running; its
  name is burnt for the pod's lifetime either way. Memory limit left raised at
  3Gi/205Mi by the two runs above — it reverts on the next rollout, per the
  warning podbench itself printed.
* **The claim** carries `.podbench-debugpy/` (debugpy 1.8.21, ~15 MB), left in
  place deliberately as the artefact proving §2's remedy. The application's own
  `.vscode/launch.json` was temporarily replaced to drive §4 and has been
  **restored from backup**; `git status` on the claim shows `.vscode/` clean.
* **A live debugpy adapter**, pid 907 in the target container, listening on
  127.0.0.1:37189 and wedged as described in §4. It does not survive a restart
  and nothing off the node can reach it, but on a `hostNetwork: true` pod it is
  reachable from every other hostNetwork pod on `bl47p-ea-serv-01` — podbench
  said so at the time, and it is repeated here because this run left one
  running.
* **Laptop side** — the ssh config for `podbench-1` was rewritten to the
  throwaway key by §5 and has been **restored** to `/home/giles/.ssh/id_ed25519`;
  reachability re-verified (`SEAT_REACHED`). The throwaway keypair is
  scratchpad-only and uncommitted. One VS Code window is open on
  `/podbench/app` with the bridge alive.
* **`p47-services`, branch `podbench-hotfix-claim`** — `HEAD c0fdd90`, pushed
  direct, not a PR. **The target is left hotfix-wired**, deliberately: retiring
  it is a separate act and the mutating walk has already evidenced `retire`
  end to end.

---

## Not measured

* **Why the target never connected back to the adapter's `--for-server` port**
  (§4) — the single open thread, and the one that decides whether Failure 2 has
  a *fourth* cause behind the three named here.
* **Whether fixing the provision destination alone would produce a working F5**
  — it cannot be known while §4 stands; §2's remedy is proven only as far as
  "debugpy installs, injects, and listens".
* **`--new` landing a seat that takes the new key** (§5) — not exercised, to
  hold this run to exactly one seat. The refusal path is what the failure is
  about; the acceptance path is unit-tested.
* **The compounding resize's intent** (§7) — observed across two runs, not
  traced to the code that computes it.
* **GUI-only surfaces** — no VS Code modal or toast can be read by the bridge
  (`showErrorMessage` is write-only). Every finding above rests on DAP events,
  extension logs, `kubectl logs`, socket state or file contents instead.
