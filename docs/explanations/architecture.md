# Architecture

podbench is a packaging exercise. Every mechanism it stands on is an ordinary,
individually-proven Kubernetes or Linux feature; what did not exist before was
the combination, and the diagnostics that make the combination survivable.

This page explains how the pieces fit and why each has the shape it does. The
empirical basis for almost all of it is the
[Phase 0 gate report](spikes/phase0-report.md) — five spikes against a real
6-node k3s cluster, five falsified assumptions.

## The picture

```
              laptop
     VS Code · Remote-SSH · kubectl
                 |
                 |  ssh, carried by `kubectl exec` (API server only)
                 |
  +--------------+--------------------------------------+
  |  pod                                                 |
  |                                                      |
  |  +----------------------+  +----------------------+  |
  |  | app container        |  | podbench             |  |
  |  |                      |  |                      |  |
  |  | workload process     |  | sshd -i -e (per conn)|  |
  |  |   (maybe distroless) |  | vscode-server        |  |
  |  |                      |  | gdb · git · uv       |  |
  |  | rootfs readable at   |  |                      |  |
  |  |   /proc/<pid>/root  <---- sysroot, sources      |  |
  |  +----------------------+  +----------------------+  |
  |                                                      |
  |  shared: PID namespace (Observe: via the target;     |
  |                         Iterate: shareProcessNamespace)|
  |          network namespace (always)                  |
  +------------------------------------------------------+
```

Two containers, three shared things — a PID namespace, a network namespace and a
`/proc` view — and one transport that is not a network path at all.

## The ephemeral container

`kubectl debug --target` has been able to inject a container into a running pod,
in another container's PID namespace, since Kubernetes 1.25. podbench uses the
same primitive but **posts to the `ephemeralcontainers` subresource itself**
rather than shelling out to `kubectl debug`, for a measured reason: `--custom`
takes a file path rather than inline JSON, and — worse — it *merges* a profile
applied **after** your JSON. Asking for `runAsUser: 1000` with the default
profile yields `{"capabilities":{"add":["SYS_PTRACE"]},"runAsUser":1000}`, which
is the one combination that is invalid by construction (see
[Security model](security.md)). Building a capability ladder on top of that is
building on sand.

Three properties of ephemeral containers shape everything downstream:

* **They are permanent.** They cannot be removed, restarted or edited. Every
  attach appends to the pod spec for the rest of the pod's life, and a name once
  used is burnt. So `attach` reconnects by default, a failed rung takes a *fresh*
  name rather than retrying its own, and readiness is gated on the container
  genuinely `running` rather than merely accepted.
* **They cannot declare `resources`.** The field is rejected outright. That is
  the origin of Observe mode's entire risk profile, and of Iterate mode's
  existence.
* **They start from the image every time.** A restart — or an OOM, which is
  unrecoverable since there is no restart — yields a completely fresh rootfs.
  Nothing may live *only* in the writable layer, so the agent rebuilds the host
  key, the authorized keys and the sshd config on every start. Every step in the
  startup path is "ensure", never "create".

## The shared PID namespace, and `/proc/<pid>/root`

Joining the target's PID namespace is what makes the workload's processes
visible. `/proc/<pid>/root` then gives a full view of the target container's
filesystem — rootfs plus its volumes — with no shared mount and no cooperation
from the target. This is the mechanism that makes distroless targets debuggable
at all: there is no shell in there to exec into, but there does not need to be.

Two consequences that are easy to get wrong:

**Finding the target's processes is not obvious.** "The target is PID 1" is
wrong under `shareProcessNamespace: true`, where PID 1 is `/pause`. Matching
mount namespaces fails there too. Excluding `0::/` cgroups includes every *other*
podbench session's processes. The rule that holds in all cases is a **substring
match of the target's container runtime ID against `/proc/<pid>/cgroup`** — the
in-container cgroup path is relative (`0::/../cri-containerd-<id>.scope`) because
the debug container gets its own cgroup namespace, so substring, never equality.
The launcher injects that ID as `PODBENCH_TARGET_CID` at attach time.

**The bridge is one-directional.** The debug container can read the app's rootfs;
the app cannot see the debug container's. That difference is exactly
`CAP_SYS_PTRACE`. It is a good security property — a compromised app container
cannot reach the debug toolchain — but it also removes the tempting workaround of
symlinking from the target into the workspace, which is why the mount-namespace
rule below is absolute rather than a default.

## The ssh-over-exec transport

The headline mechanism: ssh's `ProxyCommand` is a `kubectl exec` that runs
`sshd -i -e` inside the debug container.

```
ProxyCommand kubectl -n <ns> exec -i <pod> -c podbench -- \
  /usr/sbin/sshd -i -e -f /etc/podbench/sshd_config -o LogLevel=ERROR
```

There is **no listening socket in the pod**, no port-forward to babysit, no pod
IP to route to, and no inbound network path of any kind. The outer
authentication is the kubeconfig — including exec credential plugins, which is
one reason podbench shells out to `kubectl` rather than embedding a client
library — and the inner authentication is an ssh key. The RBAC it needs is
`create pods/exec`, nothing more.

Everything else is a real ssh connection, so port forwarding, sftp, agent
forwarding, `scp` and connection multiplexing all work. Measured: 0.345 s cold
connect, 0.058 s over the `ControlMaster`, 26 MB/s pod→client, ~10 MB RSS per
session, 0 failures in 30 churn cycles.

The one thing that is genuinely surprising is **`-e`**. It reads like a logging
preference; it is not. Closing or replacing fd 2 in a `kubectl exec`'d process
tears down the entire CRI exec stream, silently truncating stdin and stdout with
`rc=0`. Isolated without sshd at all:

```
( echo one; sleep 4; echo two ) | kubectl exec -i pod -c c -- sh -c 'exec cat'
one
two                     # both arrive

( echo one; sleep 4; echo two ) | kubectl exec -i pod -c c -- sh -c 'exec 2>/dev/null; exec cat'
one                     # "two" SILENTLY LOST, rc=0
```

When that hits sshd the symptom is a network-looking error at key exchange
(`ssh_dispatch_run_fatal: … Broken pipe`), and a wrapper shell that does *not*
`exec` masks it — so it passes a casual test and fails in the field. Both ends
of the transport are therefore generated from one place and never hand-written,
and `-o LogLevel=ERROR` satisfies the competing requirement (zero stderr bytes)
without closing the fd.

`-t` is refused outright. From a script kubectl silently degrades to non-tty and
appears to work; with a real TTY forced onto the ProxyCommand the ssh client
hangs indefinitely.

## Why the launcher authors pod specs itself

For Iterate mode the obvious tool is `kubectl debug --copy-to`. It cannot do the
job, and the way it fails is silent. Measured on the clone:

| field | after `--copy-to` |
|---|---|
| `metadata.labels` | **removed** |
| `metadata.annotations` | **removed** |
| `metadata.ownerReferences` | removed |
| all probes, every container | removed |
| ports, resources, volumeMounts, volumes | preserved |

With the labels gone the clone is invisible to the Service, the endpointslice
keeps only the original pod, and the user sees the *old* response forever with
no diagnostic — which is to say the headline Iterate demo cannot work at all.
`--copy-to` also prints nothing on success and has no `--dry-run`, so the output
cannot even be previewed. Even in `--image` mode the added container comes out
with `resources: {}` and no workspace volume, with no flag to set either.

So the launcher builds the spec. Nothing is then off-limits, and four things it
must get right are all things `--copy-to` cannot express:

1. **Label policy.** Copy the origin's labels, delete every controller label
   (`pod-template-hash`, `controller-revision-hash`, job and StatefulSet
   labels). Keeping the Service-selector labels while dropping the hash puts the
   dev pod in the endpointslice without making it a ReplicaSet member; keeping
   the hash would give a `replicas: 1` ReplicaSet two matching pods and one
   would be reaped. And the whole thing is behind `--take-traffic`, default off.
2. **A readiness probe on the podbench container**, `tcpSocket` on the app's
   port. A probe-less clone is `Ready` the instant it starts and joins the
   Service while nothing is listening — measured as roughly half errors. With
   the probe, Service membership tracks the inner loop: the pod joins when your
   process binds and drops out ~6 s after it dies.
3. **Real resources and a workspace volume** on the sidecar. This is what makes
   Iterate mode immune to the OOM and eviction footguns.
4. **An inert PID 1 by construction** — the app container's command becomes
   `sleep infinity` and its probes are stripped. Never by pausing a live pod: if
   PID 1 exits the kubelet restarts the container with pristine image code, and
   a SIGSTOPped process still holds its listening socket while liveness probes
   kill it anyway. Do not fight the kubelet.

The same "author it yourself" instinct applies to a Service cutover, which uses
a JSON **replace** patch on the selector. A merge patch unions the maps, which
adds the dev pod *without* removing the original — the opposite of a cutover,
and invisible until half the responses are stale.

## One binary, two halves

The launcher and the in-pod helpers are the same Python package. That is not
tidiness: the logic that decides what a session can do — which capability rung
is valid, what blocks ptrace, which processes belong to the target — has to give
the same answer on both sides. Two implementations would be two answers.

* On your machine: `podbench <verb>`, normally reached as `uvx podbench <verb>`
  — uv fetches the launcher for that one run and leaves nothing installed. There
  is no kubectl plugin: a plugin has to be an executable on `PATH`, which is
  exactly the thing that outlives the command.
* In the pod: `podbench agent` is PID 1, and the same spelling reaches every
  other in-pod verb — `podbench pids`, `podbench dbg`, `podbench capreport`,
  `podbench debug-config`, `podbench dev-bootstrap`, `podbench run`,
  `podbench stop`. One file,
  `/usr/local/bin/podbench`, is what puts them within reach of an ssh session
  that sources no profile — sshd leaks none of the image's environment, so the
  agent's generated config carries `PATH` (and `PODBENCH_TARGET_CID`, and the
  debuginfod settings) into a session with `SetEnv`, and that one file is what
  resolves the verb when it cannot.

There is **one runtime dependency, and it is the CLI** — typer, which brings
click and rich. The help a developer reads at 3 a.m. is part of the product, and
four small pure-Python wheels is what that costs on a cold `uvx` start.
Everything else is the stdlib. In particular there is no Kubernetes client: the
launcher shells out to `kubectl` on purpose, so authentication, contexts and
credential plugins are inherited rather than reimplemented.

Running the launcher from the index rather than from an install has one
consequence worth stating: its version can change between two attaches with no
visible event. So the image tag is **derived from the launcher's own version**
rather than fixed, and a launcher asks for the image built from its own source.
The failure that prevents is a launcher authoring a container spec its image
does not understand — which fails inside the pod, where an ephemeral container
cannot be restarted. A dev build off a checkout matches no published image and
falls back to `main`, the branch-tip image CI pushes on every default-branch
commit — not to `latest`, which moves only on a final release and so may be far
older than the launcher. `--image` and `PODBENCH_IMAGE` still win over both.

## The mount-namespace rule

Interpreter, venv and checkout must all live on the same side of the container
boundary, and podbench standardises on: **everything in the debug container**.

The failure it prevents is the quietest in the whole system. A `.pth` written
into the target's site-packages that names a checkout in the debug container's
filesystem does not exist in the namespace that resolves it, and `site.py` only
appends directories that exist — so a path-style `.pth` is **silently ignored**
and surfaces much later as an unrelated-looking `ModuleNotFoundError`. The
exec-style `.pth` that PEP 660 editable installs emit prints a traceback and
then carries on with exit 0. And because the `/proc/<pid>/root` bridge is
one-directional, there is no symlink workaround.

`dev-bootstrap` and `run` therefore validate the layout and refuse rather than
letting it be discovered.

## What the probe is for

Four unrelated subsystems refuse `PTRACE_ATTACH` with the same `EPERM`: a
missing capability, Yama's `ptrace_scope`, a seccomp filter, and the node's
LSM. A
previous hand-rolled attempt at this tool reached same-UID and still could not
tell which one had said no. Naming the blocker is the point of the whole probe —
"denied by Yama (ptrace_scope=1)" is actionable; "ptrace: Operation not
permitted" is a wasted afternoon.

So `capreport` runs **inside the container the launcher just landed, on that
node**, and the attach output reports what was *measured*, never what was
requested. Yama differs per node by kernel flavour — two arm64 nodes in the same
cluster disagreed — so the answer can never be cached cluster-wide, and the node
name and Yama state appear in the session banner precisely so that "attach
worked yesterday" is explicable.

## Where the modes diverge

| | Observe | Iterate |
|---|---|---|
| Container kind | ephemeral, in the live pod | a real sidecar, in an authored clone |
| PID namespace | the target's, via `--target` semantics | `shareProcessNamespace: true` |
| Resources | none possible — shares the workload's limits | its own requests and limits |
| Storage | the container's writable layer, against the pod's ephemeral-storage budget | an `emptyDir` workspace (4 Gi); the chart's scratch PVC exists but the launcher cannot mount it yet |
| Risk to the workload | real: OOM, eviction | none; the origin pod is untouched |
| Debugging | attach to the live process, read-only inspection, or debugpy where `podbench vscode` provisions it into the target | gdb-launch, debugpy, the relaunch loop |

Hotfix mode — a PVC mounted over the app's venv so a fix survives restarts and
reschedules — is the one mode that requires deploy-time cooperation, because
durable-across-restart code must sit on a volume that was present in the pod
spec at creation: pod volumes are immutable and a container's rootfs is reset on
every restart. The workflow ships as `podbench hotfix`
(`init`/`apply`/`status`/`consolidate`, plus `--print-values` for the chart
snippet), but it has only ever been exercised against unit tests: no cluster has
run it, and `attach` cannot yet mount the claim into the seat, so the seat must
be authored by hand or `hotfix` run inside it with `--local`.
