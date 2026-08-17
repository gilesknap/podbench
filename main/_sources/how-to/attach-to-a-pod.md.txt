# Attach to a pod

Observe mode: put a debug seat into a **live** pod without disturbing it, and
find out what that seat can actually do. For the guided version, see
[Your first session](../tutorials/first-session.md); this page is the recipes.

:::{note}
Commands here are written `podbench <verb>` — the only spelling there is. If you
have not installed the launcher, run each as `uvx podbench <verb>`, or, before
the first PyPI release, as
`uvx --from git+https://github.com/gilesknap/podbench podbench <verb>`. See
[Setup](../tutorials/setup.md).
:::

:::{warning}
On a live pod podbench shares the workload's memory and ephemeral-storage limits
and **cannot reserve its own** — an ephemeral container may not declare
`resources` at all. A VS Code session is a 1.1–1.3 GB working set, so attaching
to a tightly limited pod can get the workload OOM-killed or the whole pod
evicted, and an OOM inside an ephemeral container is unrecoverable. Anything
heavier than looking belongs in a dev pod
([Iterate on Python](iterate-on-python.md)).
:::

:::{warning}
**A breakpoint on a probed pod is on a timer.** A process stopped in a debugger
does not answer its probes, and the kubelet cannot tell that from a hang. The
budget is `(failureThreshold - 1) x periodSeconds + timeoutSeconds` after the
pause begins, plus up to one more period depending on where in the probe cycle
it began — and there are two of them:

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

## Choosing the target container

podbench needs to know *which* container's PID namespace to join and whose UID
to match:

```
$ podbench attach web --target api
```

Without `--target` it picks the pod's first container. On a multi-container pod,
name it — the target choice determines the sysroot, the UID of the degraded
rung, and what `podbench pids` calls a target process.

If the pod spec does not state a `runAsUser` for the target (so the UID comes
from the image), tell podbench with `--target-uid 1000`. The degraded rung must
match the target's UID exactly; it never defaults to root, because root without
`CAP_SYS_PTRACE` is strictly *worse* than the target's own UID — it cannot even
read `/proc/<pid>/root`.

## When the cluster refuses `SYS_PTRACE`

Nothing to do — that is the normal path. podbench catches the refusal and falls
to the next rung automatically, and still exits `0`:

```
rung        degraded - target UID, no capabilities (read-only inspection)
ladder
  full      refused  Pod Security Admission: must not include "SYS_PTRACE" in
                     securityContext.capabilities.add
  degraded  landed   admitted by the API server and the kubelet
supports
  [ ] live attach (gdb -p <pid>)
      CAP_SYS_PTRACE is not in this container's effective set...
  [x] read-only inspect (/proc/<pid>/root, maps, environ)
      root, maps and environ readable
  [x] debug launched processes (podbench dbg --launch ./prog)
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
measured
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

It is opt-in and it prints a warning either way, for two reasons.

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

It also needs `pods/resize` `patch`, which the chart grants separately from the
rest.

## Opening VS Code on the seat

```
$ podbench attach web -n demo --open
```

`--open` finishes the job the stanza starts: it configures the folder, installs
the extension this target's debugger needs **in the remote window**, and opens
the seat's home — `/root`, or `/home/podbench` on a `podbench-home` volume.
Never `/`, which is the one folder that can end the seat.

It needs `code` on your PATH and the **Remote - SSH** extension in the local VS
Code. `code` is looked for before the pod is touched, so a laptop without it
costs you a message and not a burnt container name. `--open` and
`--print-config` are mutually exclusive: the second writes no stanza, and
`code --remote` resolves the alias through ssh.

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
  `podbench-<namespace>-<pod>`.
* `--ssh-user` — the login name. `root` on the full rung; on a degraded rung
  sshd resolves the name through NSS, so the image needs an account for that
  UID and you may need to say which.
* `--identity ~/.ssh/id_work` — the key to offer, and the one whose public half
  is injected into the container.

## Host keys and `known_hosts`

podbench mints a host key per attach and manages its own `known_hosts` at
`~/.podbench/known_hosts`, keyed on an alias derived from the **pod UID**. It
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
| attach lands but `blocker: yama-scope` | Yama's `ptrace_scope >= 1` on **that node** forbids attaching to non-descendants | `podbench dbg --launch`, or have the target call `prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)` |
| every library reports `missing debugging information` | `ca-certificates` absent, so `libdebuginfod` fails the TLS handshake silently | use the published image; it is mandatory there for exactly this reason |
| attach works on one pod, is denied on the next | Yama differs **per node**, by kernel flavour, not by architecture | nothing to fix. The report prints the node name and Yama state for this reason |
| `pods "web-..." is forbidden: User cannot update resource "pods/ephemeralcontainers"` | your kubeconfig lacks a verb podbench needs, discovered mid-attach | `podbench doctor -n demo` asks for every verb up front and names the chart flag that grants it |

`podbench attach` returns `2` only for a real error. A degraded seat is
a success: returning non-zero for "the cluster would not grant `SYS_PTRACE`"
would make an honest report look like a failure.
