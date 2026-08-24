# Attach to a pod

Observe mode: put a debug seat into a **live** pod without disturbing it, and
find out what that seat can actually do. For the guided version, see
[Your first session](../tutorials/first-session.md); this page is the recipes.

:::{note}
Commands here are written `podbench <verb>` — the only spelling there is. If you
have not installed the launcher, run each as `uvx podbench <verb>`. See
[Setup](../tutorials/setup.md).
:::

:::{warning}
On a live pod podbench shares the workload's memory and ephemeral-storage limits
and **cannot reserve its own** — an ephemeral container may not declare
`resources` at all. What that costs is measured, and the seat is not the
expensive half: ten live seats on the Diamond p47 beamline (2026-08-19) were
**13–23 MiB** each. A **vscode-server** is, at **1215 MiB** live with one
extension, and that is what gets a workload OOM-killed or a pod evicted — an OOM
inside an ephemeral container being unrecoverable. Anything heavier than looking
belongs in a dev pod ([Iterate on Python](iterate-on-python.md)).
:::

:::{warning}
**A breakpoint on a probed pod is on a timer.** A process stopped in a debugger
does not answer its probes, and the kubelet cannot tell that from a hang. The
budget is
`(failureThreshold - 1) x max(periodSeconds, timeoutSeconds) + timeoutSeconds`
after the pause begins (a `timeoutSeconds` longer than the period paces the
attempts itself), plus up to one more period depending on where in the probe
cycle it began — and there are two of them:

* **readiness** — the pod goes not-ready and stops taking Service traffic. This
  is the quiet one: nothing restarts, no state survives it, and it recovers a
  probe period after you continue, so the only symptom is traffic that stopped
  arriving and it will not look as though the debugger did it.
* **liveness** — the container is killed and restarted, and the seat, which
  shares its namespaces, is killed with it. An ephemeral container cannot be
  restarted, so that name is burnt and coming back needs `--new`.

Probes cannot be changed on a running pod — they are not in the short list of
fields a pod update may touch, and unlike `resources` they have no resize-style
subresource, so there is no `--resize` equivalent to reach for. `podbench
attach` computes both deadlines from the target's own spec and prints them,
so the numbers below are for the pod you name rather than for pods in general:

```
supports
  [x] live attach (gdb -p <pid>)
      TIME-LIMITED: 'app' answers probes, so a pause has a deadline -
      readiness at 11-16s (drops out of the Service, and leaves no trace
      afterwards), liveness at 21-31s (restarts the container, killing the
      seat with it). Probes cannot be changed on a running pod; `podbench
      dev` strips all three
```

It is one line under the tick rather than a `WARNING` block of its own,
because the only part of the block that was about *your* pod is those two
numbers — the mechanism behind them is this page.

A target with no probes gets the opposite statement — `no deadline: 'victim'
declares no readiness, liveness or startup probe` — because "explore freely"
and "you have twenty seconds" are different facts and you need to know which
one you are in. For an unlimited pause on a probed workload use
[`podbench dev`](iterate-on-python.md), which strips all three probes by
construction; [Debug with gdb](debug-with-gdb.md) has the measurements.

A pod whose liveness probe is podbench's **hold-aware wrapper** gets a third
answer, which is neither of those two:

```
supports
  [x] live attach (gdb -p <pid>)
      no deadline while the hold is in place: 'app' answers its liveness
      probe through podbench's hold-aware wrapper, which returns 0 whenever
      /tmp/podbench-hold exists - so nothing restarts it while it is held.
      Once the hold is gone the target's own check applies again, at 61-91s
```

Read it as the conditional it is. The wrapper short-circuits only while the hold
file exists — the window `podbench hotfix apply` opens around a relaunch — and
outside that window the target's own check applies and `61-91s` is your budget
again. The arithmetic is reported rather than discarded because it was never
wrong: it is what a pause costs once the hold is gone, and what podbench stopped
doing is calling it a deadline on a pause that nothing can interrupt. On
`bl47p-mo-ioc-01` on 2026-08-22 it said `liveness at 61-91s` flatly, about a
probe that was already returning 0 (issue #179). Whether the line appears is read
from the probe's own `exec` command and not from the pod carrying the hotfix
layout, because a pod can be given one without the other and it is the handler
that decides whether the kubelet restarts a held container. A **readiness** probe
beside a wrapped liveness one keeps its own deadline, with `; its liveness probe
is podbench's hold-aware wrapper and imposes none while the hold exists` appended
to it: only liveness is wrapped, and readiness still drops the pod out of the
Service.

**Symbol fetches are spent out of that same budget.** gdb fetches the
executable's debuginfo when it opens the file, but a shared library's only
*after* the attach — which is with the workload stopped — and waits
`DEBUGINFOD_TIMEOUT` for each one. gdb's own default for that is 90 seconds,
which is longer than most of the deadlines above, so the seat does three
things: the image sets `DEBUGINFOD_TIMEOUT=2`; the agent opens a connection to
the symbol server once at start-up and drops `DEBUGINFOD_URLS` from ssh
sessions when nothing answers, saying so in the container's start-up log; and
`podbench dbg --no-debuginfod` (or `podbench debug-config --no-debuginfod`)
turns it off for one run, which is the flag to reach for when the server is
reachable but slow. Symbols are worth having — see
[Debug with gdb](debug-with-gdb.md) — so it stays on by default, bounded.
:::

## Attach, and re-attach

```
$ podbench attach web -n demo
'web' matched pod web-6c9d7f4b8b-hq2vn
```

**You do not have to type the whole name.** `POD` is matched as a substring
against the pods in the namespace: one hit is used and echoed, as above. An
exact name — `pod/NAME` or a bare `NAME` — is always taken as typed, even when
it is also a substring of another pod's name. Namespace defaults to your current
context's.

When more than one pod matches — or you name none at all and the namespace holds
more than one — podbench lists what it found and asks:

```
$ podbench attach -n demo
3 pods in namespace demo
      NAME                   READY  STATUS   AGE  PODBENCH
  1.  web-6c9d7f4b8b-hq2vn   1/1    Running  3h   podbench-1
  2.  web-6c9d7f4b8b-t4xz9   1/1    Running  3h   -
  3.  postgres-0             1/1    Running  6d   -
which one? [number or name, empty to cancel] 2
```

The `PODBENCH` column is the seat that is already in the pod, so you can tell
"reconnect to mine" from "land a new one" before choosing. Answer with the
number, the name, or a longer substring.

A namespace holding a single pod is not a choice, so it is not a question:
`podbench attach -n demo` resolves to that pod and says which, the same echo any
other single match gets.

In a script, a CI job or over `ssh host podbench ...` there is nobody to answer,
and a prompt would be a hang. podbench detects that stdin is not a tty, prints
the same listing, and exits `2` instead of waiting. `--no-prompt` asks for the
same refusal on a terminal.

Running it again **reconnects** to the running podbench container rather than
adding a second one. That is not an optimisation: ephemeral containers cannot be
removed or restarted, every attach appends to the pod spec permanently, and a
container name once used is burnt for the pod's lifetime. `--new` forces a fresh
container with the next free `podbench-<n>` name — use it when the previous one
died, not out of habit.

### Two seats can appear on one attach

`__ptrace_may_access()` compares the group ids as well as the user ids, so a seat
that landed at the target's uid in the image's group reads nothing the rung
exists for. podbench measures the target's real gid from `/proc` and, where it
disagrees with the one the seat was authored at, lands a **corrected** seat
beside it and says so on one `WARNING` line. That costs a second container name
for the pod's lifetime, because an ephemeral container's `securityContext`
cannot be changed in place. It happens once. `--target-gid GID` spends one name
instead of two; `--no-correct-ids` keeps the first seat with the `gid-mismatch`
blocker. See [`--target-gid`](../reference/cli.md).

### Reconnecting only reaches *your* seat

A pod is a shared thing, and a seat is not: an ephemeral container's
`authorized_keys` is written from the environment it started with and cannot be
added to afterwards, so reconnecting into a colleague's seat would produce a
stanza whose only outcome is `Permission denied (publickey)`.

So each seat records the cluster identity that landed it — whatever `kubectl
auth whoami` answers for your kubeconfig — in its container spec, and `attach`,
`vscode` and `ssh-config` reconnect only to a seat that records yours. Somebody
else's is named in one line and a fresh seat is landed beside it:

```
WARNING  podbench-1 is running but was not reused because
         system:serviceaccount:beamline:ci landed it: ...
```

The `owner` row says whose each seat is. On the attach report, two answers are
not names:

* **`unknown - this container was landed before seats recorded one`** — an older
  podbench landed it. It is still reconnected to, because refusing it would
  spend a permanent container name, but podbench will not tell you it is yours.
* **`unknown - kubectl auth whoami did not name this kubeconfig's user`** — the
  cluster has no `SelfSubjectReview` resource (it is a 1.28 API) or your role
  cannot create one. Seats landed from here stay anonymous, and podbench invents
  no local substitute: `$USER` is a fact about a workstation, not about a
  cluster.

`status` and `list` compress both of those to
`unknown - this seat records none`.

## Choosing the target container

podbench needs to know *which* container's PID namespace to join and whose UID
to match:

```
$ podbench attach web --target api
```

Without `--target` it picks the pod's first container, which is what `kubectl
exec` does. It does not do so silently — the report's `target` row names the
container it entered, and every other container the pod has, with the
invocation that reaches each:

```
target      p47-epics-gateways-ca-gateway; this pod also has
            p47-epics-gateways-pva-gateway.
            reach it with `--target p47-epics-gateways-pva-gateway`
```

`podbench pids` heads its listing the same way, so a three-container pod does
not read as a one-container pod from inside the seat either. The target choice
determines the sysroot, the UID of the degraded rung, and what `podbench pids`
calls a target process.

If the pod spec does not state a `runAsUser` for the target (so the UID comes
from the image), tell podbench with `--target-uid 1000`. The degraded rung must
match the target's UID exactly; it never defaults to root, because root without
`CAP_SYS_PTRACE` is strictly *worse* than the target's own UID — it cannot even
read `/proc/<pid>/root`.

## When the cluster refuses `SYS_PTRACE`

Nothing to do — that is the normal path. podbench catches the refusal and falls
to the next rung automatically, and still exits `0`:

```
rung        degraded - uid 1000, gid 1000, CapEff 0000000000000000
ladder
  full      refused  Pod Security Admission: must not include "SYS_PTRACE" in
                     securityContext.capabilities.add
  degraded  landed   running since 2026-08-18T09:01:33Z
supports
  [ ] live attach (gdb -p <pid>)
      CAP_SYS_PTRACE is not in this container's effective set...
  [x] read-only inspect (/proc/<pid>/root, maps, environ)
      root, maps and environ readable
  [x] debug launched processes (podbench dbg --launch ./prog)
  [ ] iterate (edit, relaunch, verify through the Service)
  [x] ssh seat (Remote-SSH: editor, shell, git, sftp)
  [x] exec seat (kubectl exec -- podbench capreport, pids, dbg)
```

The degraded rung is genuinely useful. It reads the target's rootfs, `maps`,
`environ`, `exe` and `cwd`, and it gives you **full source-level debugging of
programs gdb starts itself** — breakpoints, `run`, `continue`, backtraces,
locals — with `CapEff: 0000000000000000`. What you lose is attach to an
already-running process. See [Debug with gdb](debug-with-gdb.md).

Two things it cannot do, so do not plan on them: `/proc/<pid>/mem` and
`/proc/<pid>/syscall` use `PTRACE_MODE_ATTACH` and are denied.

(stripped-sys-ptrace)=
## When the cluster *strips* `SYS_PTRACE`

The quieter case, and the one that needs a flag. A policy engine can enforce
"no `SYS_PTRACE` here" two ways: by **refusing** the request, which is the
section above and needs nothing from you, or by **mutating** it — admitting the
container and rewriting its `capabilities` on the way through. Mutating
admission runs *before* validating admission, so by the time anything could
refuse the request there is no capability left in it to object to. The API
server returns success.

podbench drops a rung when something refuses it, and nothing refuses this. Left
alone the walk would stop on the full rung and hand you a **root seat with no
capability** — strictly worse than the degraded rung, because root that cannot
ptrace cannot read `/proc/<pid>/root`, `maps` or `environ` either.

So every rung is rehearsed first. Before a container name is committed to it,
podbench submits the rung with `?dryRun=All`: the API server runs the whole
admission chain, returns the container as it *would* have stored it, and stores
nothing. A stripped capability is visible there, and the rung is withdrawn
instead of spent:

```
rung        degraded - uid 1000, gid 1000, CapEff 0000000000000000
ladder
  full      refused  admission would take it and remove SYS_PTRACE from it,
                     landing a root seat with no capability: that reads three of
                     the six probe paths where the rung below, at uid 1000,
                     reads all six (report 3.11). A dry run read that
                     back before a name was spent; `--max-rung degraded` says it
                     up front
  degraded  landed   running since 2026-08-18T09:01:33Z
```

A rewrite that costs the rung nothing is reported rather than acted on, as one
`WARNING` line naming what admission changed — a DLS policy adds thirteen
capabilities to a container that asked for none, which is the cluster's house
default and harms nothing. The line cannot tell you *which* controller did it,
and neither can anything else you can run as a namespaced user: the API server
attributes a mutation to the field manager of the request that triggered it —
podbench's own — rather than to the webhook or policy that made it, and
`mutatingwebhookconfigurations` is cluster-scoped. Ask whoever administers the
cluster. The rung line is unaffected either way: it is read
from the seat's own `/proc/self/status` after the seat is up, so it says what
the container *is* rather than what was asked for or what was stored. Those
thirteen capabilities do not appear in it, because capabilities beside a
non-zero `runAsUser` land in `CapBnd` and never reach `CapEff`.

You can still state the cap up front, which spends no dry run either:

```
$ podbench attach bl47p-mo-ioc-01-0 --max-rung degraded
```

The full rung is then never submitted at all, the seat lands at the target's own
UID, and the ptrace credentials match. Two things worth knowing:

* It is a **starting rung**, not a choice. The rungs below it are still tried, so a
  target podbench cannot author a degraded rung for — one running as root, or
  one whose UID neither the pod spec nor the node's container status reports —
  still falls through to the seat rung. Where the UID is genuinely missing, pass
  `--target-uid` as well; the ladder line says which of the two it was, and does
  not offer the flag against a target the node already reports as root.
* A running seat the ceiling would not have landed is **not** reconnected to.
  An ephemeral container's `securityContext` is fixed for the pod's lifetime, so
  there is no reconnecting into a different one — podbench lands a new container
  and says which one it declined and why. That name is spent either way, which
  is the whole reason this is a flag rather than an automatic retry.

## When the reads are denied too

The line under each tick is the measurement it was taken from, so the case above
is distinguishable from this one at a glance:

```
supports
  [ ] live attach (gdb -p <pid>)
      denied by Yama: /proc/sys/kernel/yama/ptrace_scope forbids attaching...
  [ ] read-only inspect (/proc/<pid>/root, maps, environ)
      cmdline, status and fd only; root, maps and environ denied
      the three paths this line names take PTRACE_MODE_READ, which the
      mechanism that refused attach gates too - see the blocker below
  [x] debug launched processes (podbench dbg --launch ./prog)
measured    --no-probe skips this block
  verdict     launch-only: `podbench dbg --launch` works; no read-only inspection
```

This is the **launch-only** rung, and it is a real one — a Diamond production pod
lands on it. None of the three opens: no sysroot, no `environ`, no `maps`.
What still works is a program the seat starts *itself*, because tracing your own
descendant needs no capability and no Yama exemption. So go straight to
`podbench dbg --launch ./prog` and do not spend the afternoon on a sysroot.

`cmdline`, `status` and `fd` staying readable is not a partial win: they need no
permission at all, and are readable on any pod whatsoever. That is why the tick
is decided by the three paths it names and nothing else.

## The `iterate` row, and the pod it ticks on

`[ ] iterate (edit, relaunch, verify through the Service)` is the ordinary
answer, and the line under it says why: `attach` shares a live pod, where killing
PID 1 restarts the container and a liveness probe would kill a stopped one, so
the relaunch loop needs a sacrificial dev pod ([Iterate on
Python](iterate-on-python.md)) and never the live workload.

On a pod carrying the [hotfix layout](../explanations/hotfix-flow.md) both halves
of that sentence are false, and the row is ticked instead:

```
  [x] iterate (edit, relaunch, verify through the Service)
      `podbench hotfix apply` relaunches the application's own child in
      place: this pod carries the supervisor, so the loop runs on the live
      workload without a second pod and without restarting the container.
```

There the supervisor is PID 1 and the application is its child, so killing the
child relaunches it rather than restarting the container, and the liveness probe
is podbench's hold-aware wrapper rather than the target's own. Both p47 pods
reported the unticked row on 2026-08-22 while carrying the layout (issue #179) —
a true sentence about a pod other than the one in front of you. The row is
decided by the same spec-derived predicate that decides whether the seat mounts
the claim, so the tick and what is in the seat cannot disagree.

## What the probe itself does to the workload

Nothing, and the report says so on the line it is measured on:

```
measured    --no-probe skips this block
  ...
  pause       none - PTRACE_SEIZE does not stop the tracee
```

The question `capreport` has to answer is whether the kernel would let gdb
attach, and `PTRACE_SEIZE` answers it through the same
`PTRACE_MODE_ATTACH_REALCREDS` check that `PTRACE_ATTACH` takes — but without
stopping the tracee. So there is no stop to reap, no detach to race, and no
window in which a failed detach leaves the workload frozen. Measured against a
live Diamond `blueapi` PID 1 with 195 threads, from a seat holding no
capabilities: `State: S (sleeping)` before the seize, during it, and after.

`PTRACE_ATTACH` is still there, as the fallback on a kernel older than 3.4 —
that one *does* stop the workload while the probe reaps the stop and detaches,
and the same line then reads `brief - PTRACE_ATTACH stopped it until the probe
detached`. It is also what the *scratch* attach on the probe's own forked child
uses, where the tracee exists to be stopped and is killed a line later.

`--no-probe` skips the exec entirely, on `attach` and on `status` alike. Reach
for it when the pod must not be touched at all rather than when a pause would
be expensive: since the seize there is no pause to avoid.

## How much room this pod actually has

Every attach reads it, and prints it as a row of the `measured` block:

```
measured    --no-probe skips this block
  ...
  memory      170Mi free of 256Mi (86Mi in use)
```

The ceiling is the sum of the pod's container memory limits — a seat is charged
against it and contributes nothing to it — and what the pod is using comes from
`kubectl top pod`, so it needs a metrics-server and `get` on
`pods.metrics.k8s.io`. Neither is required: without them the row reads

```
  memory      limit 256Mi; in use not measured (no metrics API here)
```

which says **unmeasured** and not *fine*. A pod where some container declares no
memory limit gets `no pod memory limit, so no ceiling for the seat to share`,
because the kubelet leaves that pod's cgroup unbounded.

**podbench warns about this only when the margin is genuinely thin** — under
64 MiB free, which is three of the largest seat measured. The number that
decides is the *headroom*, not the limit: p47's three smallest limits are 100Mi
socat containers, and they sit in the pod with the most room per byte used
(300 MiB limit, 15 MiB in use). Across fifteen pods there, headroom ran from
170 MiB to 3858 MiB, with up to three seats in one pod at once and no OOM in any
of them. That is one beamline at one moment, so the threshold stays — a 100Mi
pod really using 80 is a real case — but it does not fire on a pod that is fine.

`podbench vscode` is checked against the other number. vscode-server measured
1215 MiB live with a single extension, which does not fit in most of those pods
— so that verb raises the target's limit to 6Gi before the seat lands, and warns
where the raise did not take. Connecting VS Code by hand after
a plain `attach` gets neither, because there is no moment at which podbench
learns you did — see [VS Code over Remote-SSH](vscode-remote-ssh.md).

## Making memory and CPU headroom first

```
$ podbench attach web --resize 6Gi --resize-cpu 4
```

This raises the **target container's** limits in place
(`kubectl patch pod --subresource resize`) before the seat lands, because the
headroom has to exist before vscode-server starts allocating into a limit
podbench cannot reserve. Naming the target container is not a detail: an
ephemeral container may not declare `resources` at all, so the seat lives inside
the pod's cgroup and the pod's ceiling is the sum of its containers' limits.

### Requests move with limits

A namespace whose `LimitRange` sets `maxLimitRequestRatio` bounds
limit ÷ request, so raising a limit on its own only ever widens that ratio:

```
pods "web-0" is forbidden: memory max limit to request ratio per Container
is 10, but provided ratio is 96.000000
```

That is a 6Gi limit over a 64Mi request. podbench reads the namespace's
`LimitRange` and raises the request to the smallest value that satisfies it —
`615Mi` here — so `--resize 6Gi` works rather than being refused with
arithmetic. Two things it will not do, and says so instead of finding out from
the API server: it will not raise a request to *equal* its limit on a pod that
is not already Guaranteed, because a resize may not change a pod's QoS class;
and it will not ask for a limit above the `LimitRange`'s own `max`.

Write `REQUEST:LIMIT` — `--resize 1Gi:6Gi` — to choose the request yourself. A
request already large enough is left alone: it is a scheduling promise the
workload was placed on.

The `--resize` flag is opt-in on `attach` — `podbench vscode` raises the limit
itself unless `--no-resize` — and podbench prints a warning either way, for two
reasons.

### A Guaranteed pod has to be asked for both halves

A Guaranteed pod is one whose every request already equals its limit, and the
API server refuses any resize that would move a pod between QoS classes:

```
Invalid value: "Guaranteed": Pod QOS Class may not change as a result of
resizing
```

Raising the limit on its own must change the class, at every number, so there
is nothing to retry. podbench does not send that patch: it says the pod is
Guaranteed and names the spelling that works — `--resize 2Gi:2Gi`, both halves,
which resizes and keeps the class (measured on k3s v1.36.3, 2026-08-21). This
is also the one case where `podbench vscode`'s automatic raise stops and hands
you a command instead of choosing the number for you. It does not pin the
request on your behalf, because a request is a *reservation* on the node and
not a cap: moving it takes that memory from everything else scheduled there,
and can leave the pod unresizable for want of allocatable memory.

### A container with a resource claim cannot be resized at all

A container that declares `resources.claims` — a DRA claim, which is how a
device such as a usbip-attached instrument reaches a pod — refuses every
resize, whatever the patch says:

```
The Pod "bl01c-ea-flip-02-0" is invalid: spec: Forbidden: only cpu and
memory resources are mutable
```

The message is about neither cpu nor memory. Validating a resize rebuilds the
incoming container's resources from limits and requests alone —
`core.ResourceRequirements{Limits: lim, Requests: req}` — so the claim is
dropped from the value compared against the stored container, which still has
one. The two can never compare equal, and that sentence is the only error the
comparison knows how to raise.

Measured at Diamond on 2026-08-18 against an EPICS IOC holding a claim for its
usbip device. A strategic-merge patch, a JSON patch of the single memory limit,
and a JSON patch rewriting `256Mi` as `256Mi` were refused identically, while a
claim-free pod in the same namespace — same `LimitRange`, same admission
policies — accepted a no-op resize. Nothing about the patch is at fault, so
nothing podbench can send will get through.

It is an upstream defect rather than a rule about claims: `release-1.32`
through `release-1.36` all drop `Claims` in that comparison. Only `master`
preserves it: the fix missed the 1.36 release, so no released Kubernetes
resizes such a container today. podbench
submits the patch rather than refusing first, for that reason, and names the
claim when the refusal comes back.

Until then the only lever is the workload's own template — raise the limits
there and let it roll — because `podbench dev` cannot help either: a claim is
allocated to one pod, so a copy of the workload would either be refused the
device or take it away from the pod being debugged.

It is only **partly proven**: three pods, two of them managed by a Deployment —
a ReplicaSet reconciles pod *existence*, not pod *spec*, so it does not fight
the resize — but all on one Kubernetes version, and never against a
`ResourceQuota` (report R13).

And the raised limits **live on the pod, not on its controller**. The Deployment
template still asks for the original ones, nothing reconciles the difference,
and so any rollout, scale, image bump or eviction regenerates the pod from that
template and silently reverts the resize. Argo CD does not itself revert it —
the pod is not one of its manifests, so there is nothing to compare against git
— but the sync that rolls the workload takes it away like any other rollout. If
you resize to make a seat viable, raise the template too, or expect the next
unrelated rollout to take it away.

Failure is reported, not fatal — a seat that lands with a loud warning beats one
that does not land.

It also needs `get` and `patch` on `pods/resize`, which the chart grants
separately from the rest. Both verbs: kubectl reads the subresource back before
it sends the write, so `patch` alone fails on the GET.

## Opening VS Code on the seat

That is a different verb:

```
$ podbench vscode web -n demo
```

`podbench vscode` is this whole page plus the things an editor needs and a bare
seat does not — it sizes the pod's memory for vscode-server, writes the
folder-walk excludes into the seat, installs the extensions **in the remote
window** and opens the seat's home: `/root`, or `/home/podbench` on a
`podbench-home` volume. Never `/`, which is the one folder that can end the seat.
On a hotfixed pod it opens the claim instead.

It writes nothing into that folder and installs nothing into your application.
Debugging is a second command, run in the seat, and the report offers it:
`podbench debug-config --provision`.

The resize is the one step that changes the workload, which is why it is not on
`attach`: adding a container to the pod is the whole of what that verb does, and
that is the promise the [mode table](../explanations/ways-in.md) makes for it.

[VS Code Remote-SSH](vscode-remote-ssh.md) has what it writes and why, and the
warning that no GUI client has driven this yet.

## Getting the ssh stanza again

```
$ podbench ssh-config web -n demo
$ podbench ssh-config web -n demo --print-config
```

`ssh-config` regenerates the stanza for a seat that is already running, without
touching the pod. `--print-config` writes it to stdout instead of to
`~/.podbench/config.d/`, for piping somewhere else.

Useful flags:

* `--host-alias myseat` — the ssh `Host` name. Defaults to
  `podbench-<namespace>-<pod>-<n>`, where `<n>` is the seat's number. The seat is
  in the name because a pod can carry several at once — an ephemeral container is
  never removed, so every `attach --new` adds one and the earlier ones keep
  running. One name over all of them meant the newest seat's stanza overwrote the
  previous one's, while `ControlMaster` kept every `ssh` — and every VS Code
  window — on the connection already open to the *older* seat.
* `--pull always` — re-check the registry for the seat's image. The default is
  `IfNotPresent`, which is what lets a side-loaded image work at all (`kind
  load`, `ctr import`, an air-gapped mirror) — `Always` is the one policy that
  *requires* a reachable registry. Use it when you are iterating on a tag that
  moves, such as `main` or a branch image: a node that already has a copy will
  otherwise serve it, and a seat older than the launcher that started it has no
  symptom at all. `attach` measures it rather than guessing — the `version` row
  of the report is what the seat itself answered — but a running seat is
  reconnected to rather than replaced, so re-checking the registry takes
  `--pull always --new`.
* `--ssh-user` — the login name. `root` on the full rung; `podbench` on a
  degraded one, which is the name in the record the seat registered for its own
  uid — unless that uid already has an account in the image, where it is whatever
  the image calls it (`nobody` at 65534). sshd resolves the name through NSS
  before it looks at a key, so a wrong value here fails as
  `Permission denied (publickey)` — the same message a missing key or an agent
  that will not sign gives. `podbench doctor` separates them.
* `--identity ~/.ssh/id_work` — the key to offer, and the one whose public half
  is injected into the container.

## Host keys and `known_hosts`

podbench mints a host key per attach and manages its own `known_hosts` at
`~/.podbench/known_hosts`, keyed on an alias derived from the **pod UID and the
seat** — a pod can carry several seats and each mints its own host key. It
deliberately does not ship `StrictHostKeyChecking no`: a debugging tool that
teaches you to skip host verification has taught you something you will apply
elsewhere.

A consequence: a pod that restarts is a new pod UID and therefore a new host,
not a man-in-the-middle warning. A container that restarts *within* the same pod
gets a fresh rootfs and a fresh host key, and podbench replaces the entry on
re-attach.

To make host keys survive, deliver one from a Secret via
`PODBENCH_SSH_HOST_KEY_FILE` (default mount
`/etc/podbench/ssh/ssh_host_ed25519_key`).

## Seeing what is out there

```
$ podbench status web -n demo  # every seat in one pod
$ podbench list -n demo        # every pod in the namespace carrying one
```

`KIND` names which of the three modes a seat is serving, and it is derived from
the pod rather than stamped on the container: `dev` is the `podbench` sidecar of
a pod podbench cloned, `hotfix` is a seat that mounts one of the workload's own
volumes in a pod carrying the hotfix layout — the `podbench-app` claim, and a
container running podbench's supervisor loop — and `attach` is everything
else. Two consequences are worth knowing.

A `dev` seat is an **ordinary** container, not an ephemeral one. `podbench
attach` on a dev pod reconnects to it rather than landing an ephemeral seat
beside it: in a dev pod the workload container is idled and the application runs
as a child of the sidecar, so a seat in that container would see nothing — and
would spend a permanent name to see it. The reconnect says which mode the seat
is, since that decides what a debugger attaches to. `--new` lands an
Observe-mode seat anyway, which is worth the name where the sidecar is non-root
and the cluster admits `SYS_PTRACE`.

A seat in a pod carrying the hotfix layout that mounts none of the workload's
volumes carries a `note` saying so. Nothing is broken — the seat works — but the
application is running the code on the claim while an editor or debugger in that
seat resolves the image's, so the code you read is not the code running and
breakpoints set on it never bind. It is a reconnect's note in practice: a seat
landed fresh mounts the claim by convention, so the way to be here is to have
reconnected into one that predates the layout. An ephemeral container's
`volumeMounts` are fixed when it is created, so that seat cannot be repaired —
`podbench attach --new` lands one that mounts the claim itself.

Each seat is listed under `RUNG (measured)` — the four numbers the agent writes
into the container log at start-up, recovered with `kubectl logs` and no exec, so
the cost does not scale with the namespace. A seat whose log could not be read
reads `not measured`, with a `request:` row naming what admission *stored*, which
is not what the kernel gave the container. A seat the gid correction replaced
carries a `superseded by podbench-N` row. The mechanism is in the
{term}`rung` entry of the [Glossary](../reference/glossary.md).

Under each seat are a `target` and a `verdict`. The `target` is the container that seat's namespaces are those
of: two seats on one pod may have entered different containers, and an
ephemeral container's `targetContainerName` is fixed for its lifetime, so this
is read back from the spec rather than assumed.
The verdict is measured: `status` runs `capreport` in every *running* seat, on
the node, exactly as `attach` did. `--no-probe` skips the exec, and every verdict then reads `not probed`, which is
also what `list` says: it lists a whole namespace and execs into nothing.

`status` shows dead containers too, because their names remain burnt. Both
print the ssh alias for each pod, read from the stanza on disk — so a seat
someone else landed, or one you landed from another machine, is reported as
having no config here rather than under an alias that would not resolve. Run
`podbench ssh-config` to mint the missing one.

## Removing a seat

You cannot. An ephemeral container lives until its pod dies. Delete the pod (a
controller will replace it) or leave it — an idle podbench container is
`sleep`-cheap, but it still counts against the pod's ephemeral-storage budget
for whatever it has written.

## When it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `ssh_dispatch_run_fatal: ... Broken pipe`, `command terminated with exit code 255` | something closed or redirected sshd's stderr; closing fd 2 in a `kubectl exec`'d process tears down the whole CRI exec stream | do not hand-edit the generated `ProxyCommand`. `-i` and `-e` are both mandatory and `2>&1` breaks it |
| `sign_and_send_pubkey: signing failed ... agent refused operation`, then `Permission denied (publickey,keyboard-interactive)` | not the seat: your agent holds that key, so ssh asked *it* to sign and it refused. The trailing message names the key, which is not what is wrong | `SSH_AUTH_SOCK= ssh <alias>` proves it — if that logs in, the agent was the only thing refusing. Then put `IdentityAgent none` in a `Host podbench-*` block in your own `~/.ssh/config`, below the `Include` line so it cannot shadow the generated stanza, and never for a FIDO/`sk-*` key or a smartcard. `podbench doctor` reports this before the first attach |
| ssh hangs forever with no output | a *stalled* transport (apiserver or konnectivity hiccup) | the generated config sets `ServerAliveInterval 15`/`CountMax 3`, which fails in ~19 s instead. Do not remove them |
| `ControlPath too long ('...' >= 108 bytes)` | the control socket is not under `/tmp/podbench-cm` | keep the generated `ControlPath`; `sun_path` is 108 bytes |
| container status `CreateContainerConfigError`, `container's runAsUser breaks non-root policy` | the kubelet refused a root container *after* the API server accepted it | podbench pre-empts this by reading `runAsNonRoot` and skips the full rung; if you forced it, do not |
| traffic stopped reaching the pod while you sat at a breakpoint, and came back on its own | the readiness budget expired: the pod went not-ready, so its EndpointSlice kept the address but flipped `conditions.ready` to false and kube-proxy stopped routing to it. Quiet, not silent — `Unhealthy` events are emitted while it lasts, but no restart survives it | nothing to fix — it self-heals. Stay inside the budget `attach` printed, or use a dev pod |
| the workload restarted mid-session and the seat went with it | the liveness budget expired; the seat shares the target's namespaces | `attach --new` for a fresh seat (the old name is burnt), and debug in a dev pod if you need to stop for longer |
| every rung refused with `The fields spec.securityContext.runAsUser is set to an invalid value. Allowed runAsUser values are: "36096\|37887"` | the cluster allow-lists the uid a pod may run as — standard where pods do host mounts — and no rung of the ladder may invent one | re-run with `--target-uid 36096`, one of the uids the refusal names. The ladder line names them and the flag |
| attach lands but `blocker: yama-scope` | Yama's `ptrace_scope >= 1` on **that node** forbids attaching to non-descendants | `podbench dbg --launch`, or have the target call `prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)` |
| every library reports `missing debugging information` | `ca-certificates` absent, so `libdebuginfod` fails the TLS handshake silently | use the published image; it is mandatory there for exactly this reason |
| attach works on one pod, is denied on the next | Yama differs **per node**, by kernel flavour, not by architecture | nothing to fix. The report prints the node name and Yama state for this reason |
| `pods "web-..." is forbidden: User cannot update resource "pods/ephemeralcontainers"` | your kubeconfig lacks a verb podbench needs, discovered mid-attach | `podbench doctor -n demo` asks for every verb up front and names the chart flag that grants it |

`podbench attach` returns `2` only for a real error. A degraded seat is
a success: returning non-zero for "the cluster would not grant `SYS_PTRACE`"
would make an honest report look like a failure.
