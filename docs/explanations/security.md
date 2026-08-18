# Security model

The honest version: what podbench needs, what it deliberately does not need, and
what to say to the person who has to approve it.

The short answer is that podbench's default posture is *less* privileged than
most debugging workflows it replaces. It opens no inbound network path, needs no
pod IP reachability and no port-forward, and asks for one capability —
`CAP_SYS_PTRACE` — only for the single feature that genuinely cannot work
without it. Everything else runs with capabilities dropped, non-root, under the
**restricted** Pod Security Standard.

## What it needs from the cluster

RBAC, in the namespace being debugged. That is the whole list:

| Resource | Verbs | Why | When |
|---|---|---|---|
| `pods`, `pods/log` | `get`, `list`, `watch` | read the target's spec — its container UID and `securityContext` — before choosing a capability rung. Without this podbench would have to guess, and the guess it would make is root | always |
| `pods/ephemeralcontainers` | `get`, `patch`, `update` | attach the seat. podbench posts here directly rather than via `kubectl debug`, which merges a profile over the spec afterwards and can produce an invalid rung | always |
| `pods/exec` | `create` | the transport. This is the entire network story | always |
| `pods` | `create`, `delete` | mint and remove a dev pod | Iterate mode |
| `services` | `get`, `list`, `patch` | repoint a Service at the dev pod | `--take-traffic` / `--cutover` only |
| `persistentvolumeclaims` | `get`, `list` | granted by the chart for the optional scratch workspace claim. Nothing in the launcher reads it yet — the dev pod's workspace is always an `emptyDir` | Iterate mode |
| `pods/resize` | `patch` | raise a running workload's memory limit before attaching | `attach --resize` only |
| `apps`: `deployments`, `statefulsets`, `replicasets` | `get` | walk pod → ReplicaSet → Deployment to find the pod template the provenance belongs on, and to refuse a multi-replica target before two writers race one ReadWriteOnce checkout. The ReplicaSet is only ever read — annotating it would be discarded by the next rollout | Hotfix mode |
| `apps`: `deployments`, `statefulsets` | `patch` | write the provenance annotations onto the pod template. Pod annotations do not survive the reschedule Hotfix mode relies on, so they go on the template — and that same edit is what rolls the workload | Hotfix mode |
| `pods` | `patch`, `delete` | annotate a pod that has no pod template, and delete one whose controller podbench does not template so that the patch is picked up. An unowned pod is never deleted: nothing would bring it back | Hotfix mode |

`podbench doctor` asks the cluster for these one `kubectl auth can-i` at a time,
as your own kubeconfig, and reports them per feature — `attach OK`,
`iterate missing`. It is the same list: `podbench.doctor.FEATURES` names the
chart flag that grants each feature, and `tests/test_chart_contract.py` renders
the chart to assert the two cannot drift. Without it an RBAC denial arrives
mid-attach, after a container name has been burnt for the life of the pod.

The chart splits these into `rbac.observe` (on by default), `rbac.iterate`,
`rbac.resize` and `rbac.hotfix`, because they are genuinely different levels of
trust: reading and attaching to a pod you own is not the same as creating and
deleting pods in a namespace, and neither is the same as changing a running
workload's limits.

`rbac.hotfix` is the one to hand out most sparingly, and the table row that says
`patch` on `deployments` is the reason. It is nominally an annotation write, but
the annotation is on the pod template, so the same call rolls the workload — the
mechanism `kubectl rollout restart` uses. It therefore *deploys code*, which is
the most privileged thing podbench does anywhere. It also grants nothing on its
own: Hotfix mode still reads pods and execs into the seat, so it is `rbac.observe`
plus the three rules above.

Note what is **not** there. No cluster-scoped anything. No nodes, no secrets, no
CRDs, no admission webhook to install, no controller to run, no agent DaemonSet.
podbench has no cluster-side component at all: it is a CLI and an image.

## What it deliberately does not need

* **No inbound network path.** ssh is carried by `kubectl exec`. There is no
  listening socket in the pod, so nothing to reach, and no NetworkPolicy hole to
  open.
* **No pod IP reachability.** Your machine never routes to the pod network.
* **No port-forward.** Nothing to babysit, nothing left listening on your
  laptop, no local port that another process on your machine can reach.
* **No second credential.** Authentication is the kubeconfig you already have —
  including exec credential plugins, SSO and short-lived tokens — plus an ssh
  key for the container itself. podbench issues no tokens and stores no secrets.
* **No changes to the workloads being debugged.** Observe and Iterate mode work
  against any pod, from any chart, completely unmodified. Nothing is installed
  into the application image and no chart has to be edited. (Hotfix mode is the
  one licensed exception — it needs a claim mounted over the app's venv path,
  which only the application's own chart can do — and it is deliberately the
  last thing in the design for that reason.)
* **Never `CAP_SYS_ADMIN`.** It would make gdb's default sysroot work with zero
  configuration, which is why it gets rediscovered as a shortcut. It is
  container-escape-adjacent, rejected by any restricted policy, and it *also*
  breaks thread debugging. podbench documents it as an anti-pattern rather than
  using it.
* **No privileged mode, ever.**

## The two-rung ladder

`CAP_SYS_PTRACE` is outside both the **baseline** and the **restricted** Pod
Security Standards' allowed capability lists. Being refused is therefore a
mainstream scenario, not an edge case — which is why the degraded path is a
first-class mode rather than an error state.

| Rung | securityContext | Admitted under | Buys |
|---|---|---|---|
| **full** | `runAsUser: 0`, `capabilities.add: [SYS_PTRACE]` | privileged / exempted namespaces, or a targeted policy | attach to the workload's live processes |
| **degraded** | `runAsUser: <target's uid>`, `runAsGroup: <target's gid>`, `capabilities.drop: [ALL]`, `allowPrivilegeEscalation: false`, `runAsNonRoot: true`, and the target's own `seccompProfile` where it has one | **restricted**, verified | `/proc/<pid>/root`, `maps`, `environ`, `exe`, `cwd`; full source-level debugging of processes gdb starts itself |
| *(seat)* | whatever the cluster will admit | anything | editor, shell, git, uv |

The launcher tries them in order and falls down on refusal, then reports the
rung it landed on. A degraded seat exits `0`: returning non-zero for "the
cluster would not grant `SYS_PTRACE`" would make an honest report look like a
failure.

### The seat mirrors the target's seccomp profile, and imposes none

The non-root rungs used to state `seccompProfile: RuntimeDefault` outright, to
be restricted-PSS-shaped by construction. That is only *needed* where the
workload complies with the same standard — a namespace enforcing `restricted`
would have refused the workload without a profile of its own — and it is not
free.

Measured at DLS on 2026-08-18, on a pod declaring no `seccompProfile`: the seat
landed at `Seccomp 2 (SECCOMP_MODE_FILTER)` and `PTRACE_ATTACH` was refused on a
child the seat had **forked itself**. That call needs no capability, is exempt
from Yama below `ptrace_scope=2`, and passes the credential check by
construction — and the same pod's earlier root seat, authored by the full rung
and so carrying no profile at all, had made the same call successfully on the
same node. The node's `RuntimeDefault` denies `ptrace`.

So podbench was putting the seat under a filter the container it was there to
debug did not have, and paying for it twice: live attach, and `dbg --launch` —
the fallback recommended everywhere else precisely because it needs no
privilege. What survived was read-only inspection, because `/proc` reads are
gated on credentials rather than on the syscall.

The rule now is to mirror: copy the target container's profile if it names one,
say nothing where the *pod* names one (an ephemeral container and a sidecar both
inherit it), and impose nothing where neither does. The seat is then never more
confined than the workload beside it, and stays admissible wherever compliance
is genuinely enforced.

### There is no middle rung, and this matters

`capabilities.add: [SYS_PTRACE]` on a container with a non-zero `runAsUser` is a
**silent no-op**:

```
Uid:	1000	1000	1000	1000
CapPrm:	0000000000000000
CapEff:	0000000000000000
CapBnd:	00000000a80c25fb      <-- SYS_PTRACE (bit 19) in BOUNDING only
CapAmb:	0000000000000000
```

The kernel grants capabilities to non-root UIDs only through the ambient set,
which the CRI does not populate. The pod is admitted, the container runs,
everything looks right, and ptrace fails with a bare `EPERM`. This is the
mystery-`EPERM` that cost a previous attempt at this tool an afternoon — and it
is **self-inflicted by the launcher's own manifest**, not caused by the cluster.

podbench refuses to author that combination. Shipping a container that silently
has `CapEff: 0` would tell a user they have live attach when they do not, which
is worse than refusing.

### Root without the capability is *worse* than non-root

Counter-intuitive, and measured:

| path | uid 1000, `CapEff 0` | uid 0, `CapEff 0` | uid 0 + `SYS_PTRACE` |
|---|---|---|---|
| `readlink /proc/T/root` | **OK** | FAIL | OK |
| `ls /proc/T/root/etc` (sysroot) | **OK** | Permission denied | OK |
| `/proc/T/maps`, `/smaps` | **OK** | Permission denied | OK |
| `/proc/T/environ` | **OK** | Permission denied | OK |
| `open /proc/T/mem` | denied | denied | SUCCESS |

Reads pass the kernel's credential check when the UIDs match and are exempt from
Yama. Root with no capability matches nothing and gets 3 of 6 probe paths;
the target's own UID with zero capabilities gets 6 of 6. So the degraded rung
matches the **target's UID** and never defaults to root.

`/proc/<pid>/mem` and `/proc/<pid>/syscall` are the exceptions — they use
`PTRACE_MODE_ATTACH`, so any "read-only memory inspection" feature planned on
them does not work in the degraded rung.

## Four ways to be denied, one errno

Even with `CAP_SYS_PTRACE` granted, attach can be refused by:

1. **the capability itself** being absent or ineffective;
2. **Yama** — `/proc/sys/kernel/yama/ptrace_scope`, a *node-level*, read-only
   knob. At `1` (Ubuntu's default) attach to a non-descendant is denied;
3. **seccomp** — a filter rejecting `ptrace(2)`. Whether `RuntimeDefault` does
   this is the *runtime's* business, not the name's: the spike nodes permitted
   it, a DLS node denied it even on a self-forked child (2026-08-18), so only
   `capreport` can say which node you are on. It blocks
   `personality(ADDR_NO_RANDOMIZE)` either way, so gdb cannot disable ASLR;
4. **AppArmor** — a profile denying ptrace between the two domains. Everything
   observed ran under `cri-containerd.apparmor.d (enforce)`, which permits
   ptrace between peers *in the same profile*; a target with a custom profile
   breaks that.

All four return `EPERM`. `capreport` reads the capability sets, `Seccomp`,
`NoNewPrivs`, both AppArmor profiles and the Yama scope, then runs a scratch
`PTRACE_ATTACH` on its own forked child — always permitted by Yama, so a failure
*there* is structural — and a live attach on the target. It names the mechanism.

Yama is per node and differs by **kernel flavour, not architecture**: two arm64
nodes in the same cluster disagreed, one denying and one allowing the
byte-identical container. podbench probes per pod and never caches a
cluster-wide answer.

## Living without the capability

Losing `SYS_PTRACE` costs exactly one feature — attach to an *already running*
process — and even that has workarounds:

* **The seat is untouched.** ssh, VS Code, git and uv need no capability.
* **Iterate mode is untouched.** Relaunched processes are the debug container's
  own children, and debugging your own descendants is always permitted; Yama and
  the capability check both exempt them.
* **gdb-launch survives.** `podbench dbg --launch ./prog` gives breakpoints,
  `run`, `continue`, backtraces, arguments and locals at uid 1000 with
  `CapEff: 0000000000000000`, under `restricted` with `RuntimeDefault` seccomp.
  Document the inner loop as gdb-**launch**; attach is the privileged special
  case.
* **In-process debug servers are the ptrace-free live attach.** debugpy, Node's
  inspector, JDWP: the app listens on loopback, the editor attaches through the
  shared network namespace and the ssh tunnel. For Python this means the live
  attach story never touches ptrace at all. Bind such a listener to `127.0.0.1`,
  never `0.0.0.0` — on the pod IP it is an unauthenticated code-execution
  endpoint.
* **`prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)`** in the target program is a
  one-line, capability-free, node-change-free opt-in that makes a sibling
  attachable under Yama. Note that "start it yourself and attach" does *not*
  satisfy Yama on its own: `myprog & ; gdb -p $!` makes gdb a sibling, and
  siblings are denied.

## The organisational escape hatch

This is the argument to take to a security team, and it is the reason a
published, pinned image is part of the product rather than a convenience.

Policy engines — Kyverno, OPA/Gatekeeper, Validating Admission Policy — and PSS
exemptions can express a rule far narrower than any blanket exception:

> allow `CAP_SYS_PTRACE` **only** when the container is
> `ghcr.io/gilesknap/podbench@sha256:…`, **only** as an *ephemeral* container,
> **only** with that one capability added and nothing else, **only** in these
> namespaces, and **only** when requested by these users or groups.

Every clause of that is checkable at admission time, and it is a far easier ask
than "privileged". It is also only *writable* against a pinned, published,
minimal image — which is exactly why podbench ships one image with a digest
rather than telling people to build their own.

Sketch, deliberately abbreviated:

```yaml
# Kyverno, illustrative — adapt to your policy library and test it
match:
  any:
    - resources:
        kinds: ["Pod/ephemeralcontainers"]
validate:
  message: "SYS_PTRACE is allowed only for the pinned podbench image"
  foreach:
    - list: "request.object.spec.ephemeralContainers[]"
      deny:
        conditions:
          any:
            - key: "{{ element.securityContext.capabilities.add[] || `[]` }}"
              operator: AnyIn
              value: ["SYS_PTRACE"]
            - key: "{{ element.image }}"
              operator: NotEquals
              value: "ghcr.io/gilesknap/podbench@sha256:<digest>"
```

Pair it with a RoleBinding that grants `pods/ephemeralcontainers` only to the
people who should have it. The RBAC decides *who*; the policy decides *what*.

Refusal is fine too. If the answer is no, podbench lands the degraded rung
automatically and prints why.

## Things worth knowing before you approve it

* **The blast radius of the seat is the pod.** A podbench container sees that
  pod's processes, that pod's network namespace and the target container's
  filesystem — nothing outside. There is no node access, no host mount, no
  hostNetwork, no hostPID.
* **The `/proc/<pid>/root` bridge is one-directional.** The debug container can
  read the app's rootfs; the app cannot see the debug container's. A compromised
  application container cannot reach the debug toolchain.
* **The target's filesystem is never written.** `readOnlyRootFilesystem` is
  common in production and podbench does not depend on writing into the target.
  `/proc/<pid>/root` is a read path in every standard workflow.
* **An ssh-able seat on a live pod runs as the target's own uid and gid**, and
  changes no identity to get there. sshd will not authenticate a user NSS cannot
  resolve, and no account for a uid discovered at attach time can be pre-baked
  into an image, so the seat writes its own record — normally into an NSS
  database of its own rather than into `/etc/passwd`, which it leaves as the image
  built it. That is the subject of the next section, because the
  file it writes to is world-writable and a reviewer should see the argument
  rather than the mode. `podbench dev`'s seat is an ordinary container and takes
  a *projected* identity instead, writing nothing at all.
* **Ephemeral containers are an audit trail.** They cannot be removed, so an
  attach is permanently visible in the pod spec, with the image, the
  securityContext and the container name. `podbench list` reads the same
  data.
* **Host keys are minted per attach**, so `known_hosts` identity is per pod.
  podbench manages its own `known_hosts`, keyed on the pod UID, rather than
  shipping `StrictHostKeyChecking no` — a debugging tool that teaches people to
  skip host verification has taught them something they will apply elsewhere.
  Delivering a stable host key from a Secret is supported and is the better
  posture where it is available.
* **The image needs egress on first connect** for the VS Code server download —
  four host groups, listed in
  [VS Code Remote-SSH](../how-to/vscode-remote-ssh.md). Air-gapped operation is
  unspiked.
* **`--resize` changes a running workload's memory limit.** It is a separate
  RBAC grant for that reason, and it is opt-in.
* **Availability, not confidentiality, is the real risk in Observe mode.**
  podbench cannot reserve resources on a live pod, so the plausible incident is
  an OOM-killed or evicted workload, not a data breach. See the footgun section
  on the front page.

### The seat's login, and the world-writable file that provides it

This is the one mode bit in podbench that looks wrong at a glance, so here is the
whole of it.

The degraded rung runs the seat as the target's uid **and** gid — 36070, say,
discovered from the target's `securityContext` at attach time. sshd resolves the
login name a client offers through NSS *before* it looks at any key, and
`ssh-keygen` calls `getpwuid()` whatever it is asked to do, so a seat with no NSS
record for its own uid has no ssh at all. Nothing can be pre-baked: the uid is
not known until the attach.

So the seat registers a record for itself, and where it registers it is the
question:

* `/etc/passwd` **is not modified**. It stays as the image built it: root-owned,
  group `root`, and mode 664 — `chmod g=u` makes it group-writable deliberately,
  in the OpenShift convention. Only a seat in group 0 can use that, and a seat in
  group 0 is no longer the target's group, which is a credential `ptrace`
  compares: pinning
  `runAsGroup: 0` buys the transport and loses the debugger (measured, issue
  #98). That route survives as `attach --seat-gid-root` for images that need it
  and is not the default.
* Instead the image installs `libnss-extrausers`, points the `passwd` line of
  `/etc/nsswitch.conf` at it, and ships `/var/lib/extrausers/passwd` **empty and
  mode 0666**. The agent appends one line — `podbench:x:<uid>:<gid>:…` — and NSS
  resolves the uid with no capability, no gid and no change to the workload's
  manifest. Not for every seat: this NSS source has floors compiled in (uid and
  gid 500, gid 100 exempted) and ignores a record below them, for `getpwnam` as
  well as `getpwuid`. A seat under a floor takes `/etc/passwd` instead, and the
  commonest one can write it — a target that sets `runAsUser` and no
  `runAsGroup` leaves the seat pinning no group, so it runs with the image's gid
  0. `agent.extrausers_serves` decides which file, and the mode never enters
  into it.

Why a world-writable file is not a privilege boundary here:

* **The only writer is the seat's own uid, which is already the seat.** The file
  is in the seat container's own read-write layer, in the debug image, not in the
  target's filesystem and not on any volume. Anyone who can write it can already
  write the seat's `$HOME`, its `authorized_keys` and its sshd config — it is the
  same identity, reached through the same ssh session or the same
  `kubectl exec`, and both are gated by RBAC on `pods/exec` before any of this.
  A process in the *target* container cannot reach it at all: the seat has its own
  mount namespace, and traversing `/proc/<seat-pid>/root` is gated by the same
  `ptrace_may_access` check the debugger is subject to.
* **The sshd that reads it cannot act on a privileged record.** On every rung
  whose seat is not root — `degraded` and `seat` — sshd runs *as the seat's own uid*
  (`SshdLayout.for_uid(n)` with `run_as_root=False`): no privilege separation, no
  `setuid`. A record claiming uid 0 does not produce a root session, because the
  process serving it holds no privilege to hand over — it can only serve the uid
  it already is.
* **Setuid binaries are inert.** Every rung sets
  `allowPrivilegeEscalation: false`, so `NoNewPrivs` is on for the whole
  container: `su` and friends cannot change uid from any passwd record, however
  written.
* **Nothing else consults the file.** It exists for this and is otherwise empty,
  so a forged record is a forged answer to a question only the seat's own NSS
  asks. `/etc/passwd`, which the rest of the image does read, is left byte-for-byte
  as the image built it.

The **full** rung is the exception to the second bullet and has to be argued
separately, because it ships today: it is `runAsUser: 0`, so
`SshdLayout.for_uid(0)` gives it `run_as_root=True` — privilege separation on,
and sshd `setuid`-ing into the session from whatever NSS answers with. The reason
0666 is still not an escalation there is that a root seat has no unprivileged
principal to forge with: every process in it, the `kubectl exec` that carries the
ssh transport included, is already uid 0, and writing a passwd record buys
nothing over writing `/root/.ssh/authorized_keys`. Nor does a root seat ever
append: `getpwuid(0)` resolves to `root` from the image's own `/etc/passwd` and
the registration step returns early.

That argument holds by a property of the rung rather than by construction, so the
agent closes the gap instead of resting on it: on a root seat the start-up path
takes group and other write off the database (`agent.restrict_seat_nss_database`,
the `nss-db-mode` step). An ephemeral container has its own copy of the image's
layers, so narrowing a root seat's database leaves a degraded seat in the same pod
its 0666.

The combination that *would* be an escalation, stated so it is not discovered
later: **a root sshd that `setuid`s into a non-root session** — one container
holding both an unprivileged writer of this file and a privileged reader of it.
That is the shape issue #98 proposes, and it is the one thing neither argument
above covers. #98 and this mode must not both ship as they stand; whichever lands
second has to close the other off, either by giving the database an owner and
losing group/other write on that rung too, or by keeping the root sshd from
resolving out of it. The image says so beside the `chmod`.

## Unproven areas

Stated so nobody relies on them:

* The **`SECCOMP_MODE_STRICT` branch** of the capability probe has never
  executed — a `localhost/` profile could not be installed on a test node. The
  filter branch itself is no longer unproven: it fired at DLS on 2026-08-18,
  under a `RuntimeDefault` profile that denies ptrace, and named the right
  mechanism.
* **AppArmor uniformity is an assumption.** Every container observed shared one
  profile, and ptrace worked because that profile permits ptrace between peers
  within it. A custom profile on the target breaks that, and the diagnostic text
  for that case has never been seen in the field.
* **Targets in user namespaces**, and targets with unusual UID mappings, were
  never tested.
* **Behaviour through konnectivity or an API gateway** is unknown; every
  transport measurement comes from a flat exec path.
