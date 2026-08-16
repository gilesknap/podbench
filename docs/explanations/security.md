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
| `apps`: `deployments`, `statefulsets`, `replicasets` | `get` | walk pod → ReplicaSet → Deployment to find the pod template the provenance belongs on, and to refuse a multi-replica target before two writers race one ReadWriteOnce checkout. The ReplicaSet is only ever read — annotating it would be discarded by the next rollout | Patch mode |
| `apps`: `deployments`, `statefulsets` | `patch` | write the provenance annotations onto the pod template. Pod annotations do not survive the reschedule Patch mode relies on, so they go on the template — and that same edit is what rolls the workload | Patch mode |
| `pods` | `patch`, `delete` | annotate a pod that has no pod template, and delete one whose controller podbench does not template so that the patch is picked up. An unowned pod is never deleted: nothing would bring it back | Patch mode |

`podbench doctor` asks the cluster for these one `kubectl auth can-i` at a time,
as your own kubeconfig, and reports them per feature — `attach OK`,
`iterate missing`. It is the same list: `podbench.doctor.FEATURES` names the
chart flag that grants each feature, and `tests/test_chart_contract.py` renders
the chart to assert the two cannot drift. Without it an RBAC denial arrives
mid-attach, after a container name has been burnt for the life of the pod.

The chart splits these into `rbac.observe` (on by default), `rbac.iterate`,
`rbac.resize` and `rbac.patch`, because they are genuinely different levels of
trust: reading and attaching to a pod you own is not the same as creating and
deleting pods in a namespace, and neither is the same as changing a running
workload's limits.

`rbac.patch` is the one to hand out most sparingly, and the table row that says
`patch` on `deployments` is the reason. It is nominally an annotation write, but
the annotation is on the pod template, so the same call rolls the workload — the
mechanism `kubectl rollout restart` uses. It therefore *deploys code*, which is
the most privileged thing podbench does anywhere. It also grants nothing on its
own: Patch mode still reads pods and execs into the seat, so it is `rbac.observe`
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
  into the application image and no chart has to be edited. (Patch mode is the
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
| **degraded** | `runAsUser: <target's uid>`, `runAsGroup: <target's gid>`, `capabilities.drop: [ALL]`, `allowPrivilegeEscalation: false`, `runAsNonRoot: true`, `seccompProfile: RuntimeDefault` | **restricted**, verified | `/proc/<pid>/root`, `maps`, `environ`, `exe`, `cwd`; full source-level debugging of processes gdb starts itself |
| *(seat)* | whatever the cluster will admit | anything | editor, shell, git, uv |

The launcher tries them in order and falls down on refusal, then reports the
rung it landed on. A degraded seat exits `0`: returning non-zero for "the
cluster would not grant `SYS_PTRACE`" would make an honest report look like a
failure.

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

## Five ways to be denied, one errno

Even with `CAP_SYS_PTRACE` granted, attach can be refused by:

1. **the capability itself** being absent or ineffective;
2. **Yama** — `/proc/sys/kernel/yama/ptrace_scope`, a *node-level*, read-only
   knob. At `1` (Ubuntu's default) attach to a non-descendant is denied;
3. **seccomp** — a filter rejecting `ptrace(2)`. `RuntimeDefault` does **not**
   do this, though it does block `personality(ADDR_NO_RANDOMIZE)`, so gdb cannot
   disable ASLR;
4. **AppArmor** — a profile denying ptrace between the two domains. Everything
   observed ran under `cri-containerd.apparmor.d (enforce)`, which permits
   ptrace between peers *in the same profile*; a target with a custom profile
   breaks that;
5. **SELinux** — policy denying ptrace between the two contexts, on any
   RHEL-family node. A Diamond production pod ruled out all four of the above —
   same uid, no capability anywhere, `ptrace_scope=0`, seccomp permitting
   `ptrace` — and attach still failed, with both sides carrying
   `system_u:system_r:spc_t:s0`.

All five return `EPERM`. `capreport` reads the capability sets, `Seccomp`,
`NoNewPrivs`, the security context of both itself and the target and the Yama
scope, then runs a scratch `PTRACE_ATTACH` on its own forked child — always
permitted by Yama, so a failure *there* is structural — and a live attach on the
target. It names the mechanism.

**Which LSM wrote that context is detected, not assumed.** `/proc/PID/attr/current`
belongs to whichever module is loaded, and podbench read it as an AppArmor profile
unconditionally — which is how the Diamond denial came back as `blocker: unknown`
with `"apparmor_profile": "system_u:system_r:spc_t:s0"`, an SELinux context, printed
beside it (issue #52). The module now comes from `/sys/fs/selinux/enforce` and
`/sys/module/apparmor/parameters/enabled`, and the report carries `lsm`,
`lsm_context` and `lsm_enforcing` rather than one field naming the wrong module.

**SELinux is the one blocker whose evidence is off the pod.** Enforcing tells you
the policy is live, but the AVC record naming the source type, target type, class
and permission is in the *node's* audit log, which no seat can read. `capreport`
says so and points at `ausearch -m avc -ts recent` on the node named in the report,
which needs someone with access to it. A permissive policy
(`/sys/fs/selinux/enforce` is `0`) logs the denial and allows the call, so podbench
never names it as the blocker.

**Neither module is named for a pair the probe measured it permitting.** Both
decide ptrace on the *pair* of labels — SELinux `allow <source_type>
<target_type>:process ptrace`, AppArmor a `ptrace` rule between two profiles — and
a forked child inherits its parent's label. So the scratch `PTRACE_ATTACH` on the
seat's own child *is* that check, run with this seat's context on both sides; where
the target carries the same context, a scratch attach that succeeded has already
measured the policy allowing the pair. That is the Diamond pod's own shape, and it
is why the report there says `unknown` and names what it cannot see — a target that
is not dumpable (`prctl(PR_SET_DUMPABLE, 0)`, or one that dropped privileges, which
loses exactly `root`, `maps`, `environ` and `exe` to a tracer without
`CAP_SYS_PTRACE`) and a user-namespace boundary — rather than sending a reader to a
node sysadmin for an AVC that cannot exist.

**A context whose module could not be confirmed is `unknown`, never `none`.**
`/sys/module/apparmor/parameters/enabled` may be unreadable at the seat's uid, and
selinuxfs may not be mounted into the container; both look exactly like a node with
no LSM at all, while `attr/current` still holds a context somebody wrote. `none` is
read downstream as "no LSM denied anything", so it is reserved for the case where
nothing wrote a context either. A Smack or TOMOYO label lands in `unknown` for the
same reason: podbench asks two modules and there are more than two.

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
* **An ssh-able seat on a live pod runs with `runAsGroup: 0`**, and that is a
  deliberate opt-in (`attach --seat-gid-root`). The degraded rung runs as the
  *target's* uid, discovered at attach time, and sshd will not authenticate a
  user NSS cannot resolve; the debug image makes its own `/etc/passwd`
  group-writable so the agent can append a record for that uid, which needs GID
  0. Projecting the file instead — a ConfigMap over `/etc/passwd` — is not
  available here: a `volumeMount` on an *ephemeral* container may not carry a
  `subPath`, and the API server refuses the whole request if one does. So the
  choice on a live pod is a seat in group 0 or a seat reachable only by
  `kubectl exec`. Group 0 is a group, not a capability: it grants nothing in the
  target container, whose files it does not own, and `restricted` PSA admits it
  (measured: uid 1000 / gid 0). `podbench dev`'s seat is an ordinary container
  and takes the projected identity instead, needing no group change.
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

## Unproven areas

Stated so nobody relies on them:

* The **seccomp branch** of the capability probe has never executed — a
  `localhost/` profile could not be installed on a test node. `RuntimeDefault`
  was tested and permits ptrace.
* **AppArmor uniformity is an assumption.** Every container observed shared one
  profile, and ptrace worked because that profile permits ptrace between peers
  within it. A custom profile on the target breaks that, and the diagnostic text
  for that case has never been seen in the field.
* **The SELinux blocker is named by elimination**, and on the Diamond pod itself
  the elimination now goes the other way. Every other mechanism measurably said
  "not me" and the policy is enforcing — but both sides carried the *same*
  context, and the scratch attach on the seat's own child, which is that identical
  pair, succeeded. So SELinux is ruled out there too, and **what denied that attach
  is unknown**: the two candidates the report names — a non-dumpable target and a
  user-namespace boundary — are both invisible from inside a seat, and neither has
  been checked on that node. No AVC record has ever been read back from it. Where
  the two contexts *differ*, `selinux` is still an inference from eliminating the
  other four and not a confirmed rule.
* **`/sys/fs/selinux` has never been read from inside an ephemeral container.**
  The phase-0 report measured AppArmor on k3s only, and kind runs AppArmor too, so
  there is no e2e for the SELinux path. If selinuxfs turns out not to be visible
  in a seat, `detect_lsm` answers `unknown` with the context still printed, which
  is the honest failure — but it is untested against the one node class this
  exists for.
* **`enforce` is the global mode, not the seat's effective one.** `semanage
  permissive -a <type>` makes a single domain permissive while
  `/sys/fs/selinux/enforce` still reads `1`, and the per-domain list lives in
  `/sys/fs/selinux/policy`, which a seat cannot read and could not parse without a
  second runtime dependency. So a globally-enforcing node can still be permissive
  for exactly the pair podbench names. It shows up in the `ausearch` the report
  already sends the reader to: that AVC carries `permissive=1`.
* **Targets in user namespaces**, and targets with unusual UID mappings, were
  never tested.
* **Behaviour through konnectivity or an API gateway** is unknown; every
  transport measurement comes from a flat exec path.
