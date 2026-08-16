---
name: ephemeral-containers
description: What the Kubernetes API will and will not let an ephemeral container do — the constraints that shaped podbench's ladder, dev pods and seat identity. Read before changing spec.py, launcher.py, or anything that authors a container spec.
---

# Ephemeral containers

`podbench attach` adds an **ephemeral** container; `podbench dev` authors an
**ordinary** pod. Most of podbench's odder shapes exist because those two are not
interchangeable, and the API server enforces the difference. Every rule below was hit
for real against a cluster, not read in a doc.

## They are permanent

An ephemeral container cannot be removed, stopped or restarted. Consequences that drive
real code:

- **A name is burnt once used.** A container that exits reaches `Completed` and its name
  is unusable for the pod's lifetime. Hence `kubectl.next_container_name` and the
  `podbench-1`, `-2`, `-3` suffixes — and hence a failing rung must take a *fresh* name
  rather than retry its own.
- **Never give one a short-lived command.** `spec.AGENT_COMMAND` is the default for this
  reason. A container handed `true` — or handed a verb the CLI does not dispatch — burns
  its name immediately.
- **The agent must not die.** An unhandled exception in start-up costs a name and, if the
  ladder then walks every rung, three of them. `agent.ensure_all` records failures and
  keeps idling instead of raising. Keep it that way.
- **An OOM is unrecoverable.** A replacement comes up with a fresh rootfs, losing the
  vscode-server, extensions and host keys. Nothing may live *only* in the writable layer
  that cannot be rebuilt.

## They may not use `subPath`

```
spec.ephemeralContainers[0].volumeMounts[0].subPath:
  Forbidden: cannot be set for an Ephemeral Container
```

The API server refuses the whole request. There is no workaround: mounting the volume
somewhere else does not help, because NSS reads `/etc/passwd` and nothing else.

This is why the seat's identity has two mechanisms, split by container kind:

| | mechanism |
|---|---|
| `attach` (ephemeral) | the agent registers its own passwd record, against the image's group-writable `/etc/passwd`, under `--seat-gid-root` |
| `podbench dev` (ordinary pod) | the projected identity from the chart's `seatIdentity`, mounted with `subPath` — better, since nothing is written and no group 0 is needed |

`spec.validate_ephemeral_volume_mounts` enforces it at the authoring layer so it cannot
recur.

## They may only mount volumes the pod already declares

Pod volumes are immutable after creation, and an ephemeral container may not introduce
one. So anything needing a volume — Patch mode's venv claim, the seat's home directory —
only works where the deployment cooperated at creation time. That is the whole reason
Patch mode asks for a helm change, and `patch --print-values` emits the ask.

`launcher.resolve_mounts` refuses an undeclared volume with an explanation rather than
letting the API server produce a confusing one.

## They may not carry `resources`

The field is rejected outright, and the container is confined by the pod's cgroup
regardless. So on a live pod podbench **shares the workload's memory and
ephemeral-storage limits and cannot reserve its own** — the biggest footgun in the
product, and the reason Iterate mode authors a real pod (where the sidecar *can* have
resources) instead of attaching.

The mitigation is `kubectl patch pod --subresource resize`, offered behind `--resize`
and only partly proven (three pods, one k3s version, never against a `LimitRange` or a
`ResourceQuota`). A Deployment does **not** fight it — a ReplicaSet reconciles pod
existence, not pod spec — but the raised limit lives on the pod alone, so anything that
regenerates the pod from the unchanged template silently reverts it. Both halves belong
in the warning text (report R13).

## `capabilities.add` on a non-root uid is a silent no-op

The kernel grants capabilities to a non-zero uid only through the ambient set, which no
CRI populates. `capabilities.add: [SYS_PTRACE]` beside `runAsUser: 1000` is **admitted**
and produces `CapEff: 0000000000000000`: a container that looks privileged and behaves
unprivileged.

The ladder therefore has exactly two capability rungs and no middle ground, and
`spec.validate_security_context` raises rather than emitting the combination. Do not
"fix" a failing degraded rung by adding the capability back.

## Refusal arrives through two unrelated channels

- **Synchronous** — Pod Security Admission, in `kubectl`'s stderr, matched on the stable
  substring `must not include "SYS_PTRACE" in securityContext.capabilities.add`.
- **Asynchronous** — the kubelet, *seconds later*, in the container's status:
  `CreateContainerConfigError` / `container's runAsUser breaks non-root policy`. The API
  call already exited 0.

Only the first can be caught by wrapping the call, and only the second burns a name — so
each needs its own arm of the walk. Pre-empt the second by reading the target pod's
`securityContext.runAsNonRoot` before trying a root container.

## Attribution: which processes belong to the target

Under a shared PID namespace the debug container sees everything, including its own
processes and any other podbench session's. The only attribution that stays correct is a
substring match of the target's container id against `/proc/<pid>/cgroup`, injected as
`PODBENCH_TARGET_CID` (`model.TARGET_CID_ENV`).

The cgroup-difference fallback is a guess. `proc.scan_processes` returns
`Attribution.CGROUP_FALLBACK` and a warning so callers can say so rather than present it
as fact. PID 1 is *not* reliably the target — under `--target` it is often `/pause`.

## Dev pods are clones, and clones carry junk

`spec.dev_pod_spec` strips what the API server owns or what would make a controller adopt
the clone. Two that were learned the hard way:

- **`spec.ephemeralContainers`** — `Forbidden: cannot be set on create`. Cloning any pod
  that had ever been attached to failed until this was popped.
- **Controller labels** (`pod-template-hash` and friends) — keeping them gives a
  `replicas: 1` ReplicaSet two matching pods and one gets reaped.

`kubectl debug --copy-to` is not an alternative: it strips **all** labels, so the clone
never joins the Service, which is the demo. That is why the launcher authors the spec
itself — and why it can also add resources, volumes and a real sidecar.
