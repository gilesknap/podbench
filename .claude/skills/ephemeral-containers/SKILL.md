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

The API server refuses the whole request. Before #102 there was no way round it either:
the only paths NSS consulted were `/etc/passwd` and `/etc/nsswitch.conf`, and a
whole-volume mount destroys whichever it lands on — over `/etc/passwd` it replaces the
file with a directory, and over `/etc` it takes `nsswitch.conf` with it, which is the
lookup the identity existed to satisfy.

**That is now a statement about `attach`'s mount contract and not about what is
possible.** `nsswitch.conf` reads `files extrausers`, so `/var/lib/extrausers` is a
second directory NSS consults, it holds nothing else, and a whole-volume mount there
destroys nothing — no `subPath` needed. `attach` still does not mount the pod's
identity volume, by convention and not because the API forbids it
(`launcher.seat_identity_mounts`), so a live-pod seat registers its own record instead.
Worth knowing because it is the missing half of the follow-up on #102: with a writable
NSS source *and* a mountable one, the two identity mechanisms below stop needing to be
two.

This is why the seat's identity has two mechanisms, split by container kind:

| | mechanism |
|---|---|
| `attach` (ephemeral) | the agent registers a passwd record for its own uid at start-up, in `/var/lib/extrausers/passwd` — a second NSS source the image installs `libnss-extrausers` for and ships **mode 0666**, which is what permits the append: the seat's credentials are discovered at attach time, so no owner or group baked into the image could be the writable one |
| `podbench dev` (ordinary pod) | the projected identity from the chart's `seatIdentity`, mounted with `subPath` — better, since the identity is declared rather than written and nothing in the seat has to be writable |

The image's group-writable `/etc/passwd` is the *fallback*, reached by `--seat-gid-root`
and by any seat `extrausers` will not serve, and it is not free: `__ptrace_may_access`
compares the gid as well as the uid, so pinning `runAsGroup: 0` against a target whose
gid is not 0 buys ssh and takes the debugger (#102, measured — it is how one seat was
sent round the loop twice). Do not "fix" a seat with no login by reaching for that flag.

**0666 means every process in the seat, not "the seat's uid".** Matching the target's
credentials is not what grants the write — the mode is, and that is the point, since the
credentials are not known until the attach. It is safe on the rungs that append there
because sshd runs as the seat's own uid (`SshdLayout.for_uid(n)`, `run_as_root=False`):
it never `setuid`s out of a passwd record, so a forged one buys its author the uid it
already had. It is the image *default*, and the `full` rung — whose sshd is root — has
it taken off at start-up by `agent.restrict_seat_nss_database`. The argument in full,
for both rungs, is in `docs/explanations/security.md`; #98's shape (a root sshd that
`setuid`s into a non-root session) is the one that must not join it.

**`extrausers` has floors and they are compiled in.** `MINUID 500`, `MINGID 500`, with
gid 100 exempted (`s_config.h`, Debian 0.6-4.1), and a record below them is ignored by
`getpwnam` as well as `getpwuid` — the append succeeds, the line is in the file, and
nothing resolves. So the append target is chosen on the seat's uid and gid
(`agent.extrausers_serves`) and never on the file's mode. A prototype of #102 skipped
that check, and would have taken ssh away from the commonest shape there is: a target
that sets `runAsUser` and no `runAsGroup` leaves `target_uid_gid` returning a gid of
`None`, so the seat pins no group and runs with the image's **gid 0**. Also from every
`--seat-gid-root` seat, and from every target on a low-numbered system uid (grafana
472, nginx-unprivileged 101).

The package is installed unversioned, so those two numbers are an assumption about a
build, and they are checked rather than pinned: a pinned apt version rots as soon as the
archive supersedes it, and a version string only implies the behaviour the code depends
on. `tests/e2e/test_nonroot_gid_identity.py` writes a record below both floors into a
landed seat's database and asserts the real library ignores it for `getpwnam` as well as
`getpwuid`, against an image built from the same commit. If it goes red,
`agent.extrausers_serves` is what needs updating, not the test.

It fires *after* `container` has published, though, because that is the job order. A
copy of the same assertion as a smoke step in `_container.yml` would fail before an
image with moved floors is ever tagged; it is worth adding and is not in this branch,
because pushing a workflow change needs a token scope the agent that wrote this did not
have.

`spec.validate_ephemeral_volume_mounts` enforces it at the authoring layer so it cannot
recur.

## They may only mount volumes the pod already declares

Pod volumes are immutable after creation, and an ephemeral container may not introduce
one. So anything needing a volume — Hotfix mode's venv claim, the seat's home directory —
only works where the deployment cooperated at creation time. That is the whole reason
Hotfix mode asks for a helm change, and `patch --print-values` emits the ask.

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

### Every policy engine words a denial differently, and one word decides

`ADMISSION_DENIAL_MARKERS` (`kubectl.py`) decides whether a refusal is a *verdict* — drop a
rung and retry — or an *error*, which ends the walk. Get it wrong and the ladder stops on a
rung the cluster would have admitted, reporting "no rung of the capability ladder was
admitted".

A native `ValidatingAdmissionPolicy` names itself and its binding and says **`denied
request`** — not `denied the request`, and with no webhook name — so it misses the webhook
group by one word (issue #93). It is the engine most likely to be in play, because it needs
nothing installed.

Keep each group narrow. `denied the request` is required beside a webhook's *name* so that
a webhook which failed to *answer* — unreachable, timed out, `failed calling webhook` —
stays an error: retrying lower rungs against a broken webhook replaces one honest failure
with three.

### A mutating policy does not refuse — it rewrites, and nothing tells you

The quieter half. A `MutatingAdmissionPolicy` that strips `capabilities.add` leaves the API
call succeeding and the seat landing, so the walk never drops a rung. What lands is a root
container with no capability, which is **indistinguishable from the degraded rung** by
reading the spec: `runAsUser: 0`, nothing added.

Two consequences already bitten:

- the ladder remembers the rung it *asked for*, so `attach` names a capability the seat does
  not have while `status` reads the spec back and says `degraded` (issue #94)
- `seat_layout` cannot use the rung to decide where the agent put `sshd_config`, because a
  stripped full rung looks degraded and the reconnect names the wrong path — the symptom is
  `No such file or directory` in an editor's ssh log (DLS, 2026-08-16)

The honest answer to "what can this seat do" is never the rung. It is what the probe
measured; see issue #89 and `flavour.can_ptrace_target`.

`--max-rung` (`launcher.plan_ladder`) is the way past it, and it is a **ceiling**: the
rungs above are skipped, the ones below still tried. It exists because there is no
signal to react to — the walk drops a rung only when something refuses one — so on such
a cluster the user states the cap up front instead of spending a permanent container
name discovering the strip. A running seat above the ceiling is deliberately not reused:
`above_ceiling` decides that on the seat's **uid**, never on `rung_of_spec`, because the
stripped full rung reads back as `degraded` and reusing it would silently ignore the
flag. Measured at DLS, 2026-08-18.

### A root target may have no admissible rung at all

Worth checking before assuming the walk lands anywhere. With `target_uid == 0`,
`spec._rung_security_context` raises `InvalidSpecError` (runAsNonRoot at uid 0), so a
policy that refuses `full` leaves `degraded` **skipped** rather than tried, and `seat`
fails asynchronously with `CreateContainerConfigError`. A target whose uid is absent from
the pod spec skips `degraded` even earlier, rather than guessing.

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
