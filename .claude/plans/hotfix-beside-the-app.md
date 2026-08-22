# Hotfix mode, rebuilt beside the app

A sequenced plan for replacing Hotfix mode's venv-on-a-claim design with the
project-beside-the-app layout, written for an agent with pollux (`p47-beamline`)
through `k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig`.

- **Base:** `main`.
- **Decided:** 2026-08-22, six decisions, recorded below and in the issues they
  produced.
- **Evidence discipline:** the same rule as `attach-endgame.md` — *measure the
  seat, do not restate the request.* Every phase below states what would falsify
  it. Where a measurement could not be taken, say so; never "fine", and never a
  warning invented to fill the gap.

Hotfix mode has never met a cluster. This plan is the first time it will, and the
design it will meet is not the one in `hotfix.py`'s docstring.

---

## What changed, and why it collapsed

The old design mounts the claim **over** the application's venv. Everything
awkward about the mode follows from that one choice:

* the image's own venv is behind the mount in every container, so the claim can
  only be seeded by an initContainer at a staging path — which `ioc-instance`
  cannot express, because every initContainer there inherits `volumeMounts:
  *volumeMounts` from the main container;
* the seat, which is a python-copier-template image with its own `/app/.venv`,
  loses that venv when the target's project is mounted over `/app`;
* provenance goes on the workload's pod template, which Argo self-heal strips;
* the relaunch is a container restart, which — measured — SIGKILLs the seat.

Mounting **beside** the project instead dissolves all four. The image's project
is never occluded, so nothing needs staging; `/app` is never covered, so the seat
keeps its venv; the relaunch happens inside the container, so nothing restarts
and nothing is stripped.

### The layout

Five values per hotfixable service, all emitted by `--print-values`:

```yaml
volumes:            # the claim, plus podbench-home
volumeMounts:       # claim at /podbench/app — beside the project, never over it
command / args:     # runtime switch + hold loop + recorded child pid
livenessProbe:      # complete object, wrapped to honour the hold
podSecurityContext: # fsGroup
```

`ioc-instance 5.0.1-beta.2` — the version p47 pins — has all five. So does
`dev-c7`. **The only upstream chart change the design needs is blueapi's `args`
passthrough.**

### The six decisions

| # | Decision |
|---|---|
| 1 | The hold file replaces the bounce outright. `init` refuses a target with no loop; no delete-pod fallback; `rbac.hotfix` collapses toward `observe`. |
| 2 | The claim mounts *beside* the project at `/podbench/app`, and the seed rebuilds (`cp -a` then `uv sync`) rather than copying a venv whose absolute paths would lie. |
| 3 | The interpreter lives on the claim at `/podbench/app/.python`, seeded by copy. |
| 4 | The hold file carries an absolute deadline, checked at child exit. |
| 5 | The wrapped `livenessProbe` is emitted by `--print-values`, not owned by `ibek`. |
| 6 | `held` is its own column in `status`, orthogonal to `HotfixHealth`, and moves the exit code. |

---

## The cluster this is tested against

`p47-beamline` on pollux, through the tunnel kubeconfig. **Read
`k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig`'s prerequisites first: the
tunnel does not survive a devcontainer rebuild and only Giles can raise it.**

### Three permissions that shape everything below

`kubectl auth can-i --list` on that service account:

| | |
|---|---|
| `pods` | get list watch **create delete** patch |
| `pods/exec` | create |
| `statefulsets` / `deployments` | get list watch patch — **no create** |
| `persistentvolumeclaims` | get list watch — **no create** |

So test targets are **bare pods**, never workloads, and the claim cannot be
created with this account. Two consequences:

* A bare pod has no controller. Under the new design that is fine — the relaunch
  is hold-plus-kill and needs no owner — but it means the *old* design's
  refusal paths cannot be exercised here. Say so rather than reporting them
  passed.
* For storage, use a **generic ephemeral volume**
  (`spec.volumes[].ephemeral.volumeClaimTemplate`). The ephemeral-volume
  controller creates the PVC with the pod as owner, so it needs only `pods:
  create`. It survives a container restart, which is what almost every phase
  needs. It does **not** survive pod replacement — Phase 6 is the one that does,
  and it needs a real PVC from Giles.

### Preflight, run 2026-08-22 — measured, not assumed

Done against `p47-beamline` with the tunnel kubeconfig, pod created and deleted.
Re-run it if anything below stops holding; do not re-derive it otherwise.

| check | result |
|---|---|
| generic ephemeral volume from `pods: create` alone | **works** — the controller created `podbench-test-preflight-podbench-hotfix`, Trident bound 2 Gi RWO on the `netapp` default class in ~24 s, and it was reclaimed on pod delete |
| `fsGroup: 37887` + `fsGroupChangePolicy: OnRootMismatch` on that volume, container as uid/gid 37887 | **writable** — `id` reported 37887 and the write probe returned `WRITE-OK`. This is the fsGroup half of the layout, proved on real netapp storage rather than reasoned about |
| `pods/ephemeralcontainers` | `patch` **yes**, `update` **no**. `kubectl debug --target` returned no authorization error, so `attach` has the verb it needs |
| tolerations needed to land on `bl47p-ea-serv-01` | `beamline=bl47p`, `location=bl47p`, `nodetype=training-rig`, all `NoSchedule` |
| push to `gilesknap/podbench` | confirmed by dry run |

Still unproven and belonging to Phase 0: that Argo leaves an untracked pod alone.

## The other cluster: the k3s bench

`ssh podbench-bed` (see [[k3s-bench-access]] in auto-memory for the host and the
stanza). A key was minted and authorised on 2026-08-22 —
`SHA256:p1N9dJgRbzy0FFqdRsAQl3kAtd29TsFMwA3uMKz+B3U`. It does not survive a
devcontainer rebuild; re-mint and ask Giles to install it.

| | bench | pollux |
|---|---|---|
| kubelet | **v1.36.3+k3s1** | **v1.34.5** |
| storage | `local-path`, WaitForFirstConsumer, RWO, node-local | `netapp` Trident, Immediate, RWO/RWX |
| PSA | no labels on `default` — nothing enforced until a namespace is labelled | enforced; the ptrace rung works today |
| privileges | cluster-admin: PVCs, StatefulSets, namespaces all creatable | pods only |
| host | 2 cores, 8 GB, 48 GB free, Ubuntu 26.04, containerd 2.3.2 | a beamline node |

**Run phases 0-5 here first, then confirm on pollux.** The bench has everything
pollux withholds — real PVCs, workload objects, a labelled PSA-`restricted`
namespace to prove where seeding-from-the-seat fails and the Job comes back, and
no blast radius, so none of the duplicate-safety rules above apply. It unblocks
Phase 6 outright.

Four things it cannot stand in for, and the first is a trap:

1. **Its kubelet is two minors ahead of pollux's.** The 15s/23s/45s ladder is
   pollux's number and stays pollux's; the reduced-backoff work (KEP-4603) may
   well be default at 1.36. Measure both, quote each against its own version, and
   never let a bench measurement stand in for a pollux one.

   The bench's own ladder is **not yet measured** — an attempt on 2026-08-22
   using `kubectl get events` was inconclusive, because the kubelet coalesces
   repeated `BackOff` events and the timestamps do not survive it. Use the method
   that worked on pollux instead: poll
   `.status.containerStatuses[0]` and difference
   `lastState.terminated.finishedAt` against `state.running.startedAt` for each
   restart. Do this in Phase 0; it decides whether the hold is as load-bearing on
   a modern kubelet as it is on pollux's, which is the one result that could
   change the design's urgency rather than its shape.
2. No `fastcs-example-debug` target in its real configuration.
3. No Argo, so nothing about self-heal is testable there.
4. No 37887 uid regime and no `hostNetwork` realities.

**Do not build images on the bed.** Push the branch and pull CI's multi-arch
image, `ghcr.io/gilesknap/podbench:<base>-<branch-slug>`. The tag is rewritten on
every push and containerd caches it, so `k3s ctr images rm` then pull, and check
the digest moved. Three podbench images are cached there already.

### Duplicating a target without disturbing the beamline

Every IOC in `p47-beamline` runs `hostNetwork: true` and mounts the shared
`p47-runtime-claim`, `p47-opi-claim` and `p47-autosave-claim` on
`subPath: <release>`. A naive duplicate therefore takes CA and PVA ports that are
already bound, answers UDP broadcast searches for PVs another IOC is serving, and
writes a third party's subPaths. All three are silent from the duplicate's side
and visible only as beamline misbehaviour.

So every duplicate in this plan:

1. sets `hostNetwork: false` and drops `dnsPolicy: ClusterFirstWithHostNet`;
2. drops the three shared claims entirely — nothing here needs them;
3. renames the PV prefix in its config where it has one, so that even on the pod
   network it cannot answer for a live IOC;
4. is named `podbench-test-<something>` and carries
   `podbench.dev/test-duplicate: "true"`, so it is greppable and deletable as a
   set;
5. is deleted at the end of the phase that made it.

Argo prunes only resources it tracks, so an untracked pod in the namespace is
invisible to it. That is what makes this safe — and it is also the thing to
re-verify once, in Phase 0, rather than assume.

### The one target worth duplicating

| pod | image | why |
|---|---|---|
| `bl47p-ea-fastcs-01-0` | `fastcs-example-debug:2025.10.1` | the canonical case: python-copier-template layout (`/app/.venv`, `/python`), a `stdio-socket --ptty` wrapper as PID 1, a real uv project with `.git` and an ssh origin already in the image |

`bl47p-ea-simdet-01/02/03`, `bl47p-mo-ioc-01` and `bl47p-synoptic` are
epics-containers runtime images whose running process is a compiled IOC. They are
useful for one thing only — proving the layout is *inert* when the claim is
unseeded — and are not hotfix targets.

**`p47-blueapi` is deliberately out of scope for this plan.** It already carries an
early version of what Hotfix mode is trying to be, and whether podbench should
replace, converge with, or leave that alone is an open design question — see below.
Every phase here can be completed on fastcs alone.

---

## Phases

Each phase is a branch and a PR. Do not start a phase whose predecessor has not
been proved on the cluster; the whole point of this design is that its failure
modes are silent, and a phase that "looks right" is exactly what this plan exists
to distrust.

### Phase 0 — the harness, before any podbench change

Nothing in podbench changes here. Prove the environment does what this plan
claims.

1. Raise the tunnel; `kubectl auth can-i --list` and record it in the PR, because
   the numbers above will drift.
2. Create one `podbench-test-fastcs` bare pod: the fastcs debug image, the
   duplicate rules above, a generic ephemeral volume at `/podbench/app`, and the
   hold-loop args line with the runtime switch. **Claim left unseeded.**
3. Confirm it runs image code — `readlink /proc/1/root/proc/self/exe`, and the
   IOC's own PV prefix answering on the pod network.
4. Wait out one Argo sync interval and confirm the pod is untouched.
5. Delete it.

**Falsified if:** Argo prunes the pod, the runtime switch activates against an
empty claim, or a generic ephemeral volume is refused by the storage class.

### Phase 1 — the hold loop, measured

Still no podbench change. This phase measures the mechanism the whole design
rests on, against the numbers already taken on 2026-08-22.

1. Recreate `podbench-test-fastcs`.
2. Kill the recorded child pid; confirm the container does **not** restart
   (`restartCount` stays 0) and PID 1 is unchanged.
3. Repeat ten times in quick succession and record relaunch latency. Compare
   against the measured kubelet ladder — 15s, 23s, 45s at restarts 2, 3 and 4 on
   `bl47p-ea-serv-01`, kubelet v1.34.5 — which is what the hold exists to avoid.
4. Remove the hold file, kill the child, confirm the container *does* restart —
   fail-fast is intact.
5. With the wrapped `livenessProbe` in place, hold the pod with a dead child for
   five minutes and confirm no restart. Then unwrap the probe and confirm it
   *does* restart at ~90s, so the wrapper is shown to be load-bearing rather than
   assumed to be.

**Falsified if:** killing the child exits PID 1 anyway (the wrapper is not what
we think), or the probe restarts a held pod despite the wrapper.

**Deliverable:** `docs/explanations/spikes/s7.md` with both ladders, the
`exitCode: 137` seat death from #161, and the Phase 1 numbers. Add it to
`docs/explanations/spikes.md` by hand — that page lists its children explicitly,
so a new page fails `just docs` until it is listed.

### Phase 2 — `--print-values` emits the new layout

First podbench change, and a pure-unit one.

* `values_snippet` emits the five keys above: claim at `/podbench/app`, the args
  line with switch and loop, the wrapped probe, `fsGroup`, `podbench-home`.
* Drop the initContainer, the staging path, and `podbench-identity` from the
  snippet — none survives this design.
* Rename `hotfixVenv.claims` and `<app>-venv`; they no longer describe a venv.
* Tests: doctest the snippet, and add a `helm template` test that the emitted
  values are accepted by `ioc-instance 5.0.1-beta.2` **as pinned** — that claim
  is the whole practical payoff and must be asserted, not believed.

### Phase 3 — seed from the seat

* `hotfix init` performs the seed instead of verifying it: `cp -a
  /proc/1/root/app/.` and `/proc/1/root/python` onto the claim, then `uv sync` in
  the *application* container.
* Refuse when `/proc/1/root` is not traversable, naming the ptrace rung — do not
  fall back silently.
* `checkout_path()` stops hardcoding `<venv>/src`; venv detection reads the
  target rather than requiring `--venv`.
* Cluster proof: seed `podbench-test-fastcs`, restart the *container*, confirm it
  comes up running claim code and that `uv run fastcs-example --version` in the
  seat and in the target agree.

**Falsified if:** the rebuilt venv's console scripts still resolve into the
image (`head -1 /podbench/app/.venv/bin/fastcs-example` must not say `/app`), or
`uv sync` needs egress the pod does not have.

### Phase 4 — apply, by hold

* `apply` writes the hold, kills the recorded pid, clears the hold.
* Delete `annotate()` and `_bounce`'s annotation branch.
* `init` refuses a target with no loop, checked at runtime.
* `rbac.hotfix` collapses; the multi-replica refusal counts pods mounting the
  claim.
* Cluster proof: edit a source file in the seat, `apply`, and see the change in
  the IOC's behaviour with `restartCount` still 0 and the seat still alive.

### Phase 5 — `status` reads the volume

* Filter on pods carrying the claim, not on the annotation.
* `held` column with its deadline; non-zero exit; visible for a held pod that was
  never hotfixed.
* Cluster proof: hold a pod, run `status` from the laptop, confirm the row and
  the exit code. Then kill the seat and confirm `status` still reports it —
  which is the case #161 says will actually happen.

### Phase 6 — survives pod replacement

The one phase that needs a **real PVC**, because a generic ephemeral volume dies
with its pod. Ask Giles for one claim in `p47-beamline`, or for `create` on
`persistentvolumeclaims`.

* Delete the test pod, recreate it against the same claim, confirm the hotfix is
  still running and `status` still reports it.
* Then bump the image on the duplicate and confirm `status` says `image-changed`
  rather than staying quiet.

### Phase 7 — docs, and the P47 report

* Rewrite `docs/explanations/hotfix-flow.md` — its diagram, its "two things about
  the seed", and its "every cluster call" table are all describing the old
  design.
* Correct the module docstring in `hotfix.py`: the PVC's three unfixed problems
  become two, since the interpreter now lives on the claim.
* Re-issue the P47 report against whatever the repo-versus-cluster drift turns
  out to mean.

---

## Open, and blocking nothing yet

* **The repo and the cluster disagree.** `bl47p-ea-panda-01`, both `dcam`s,
  `bl47p-c7-sim-01` and `bl47p-gateways` are in `p47-services/services/` but not
  running; `bl47p-ea-simdet-01/02/03`, `bl47p-synoptic` and `p47-blueapi-oauth2`
  are running but not in the repo. Which is authoritative decides what the P47
  report says. It does not block phases 0-6.
* **A real PVC for Phase 6**, per above.

* **blueapi: converge, replace, or leave alone.** Deferred pending review, and the
  reason it is out of scope above. blueapi already implements most of this mode
  under another name: `setup-scratch` clones the configured repositories onto a
  ReadWriteMany `scratch` claim, editable-installs them into `/venv`, and re-runs
  on every pod start — so an edit to the checkout already outlives a restart.
  What it lacks is the provenance half: no commit discipline, no manifest, no
  `status`, nothing that would stop a diverged worker going unnoticed. What it has
  that podbench does not is a mechanism the application's own maintainers own.

  Three options, none obviously right:

  1. **Leave it.** blueapi keeps its own mode; podbench never targets it. Cheapest,
     and the divergence risk stays unaddressed.
  2. **Converge.** podbench learns to read blueapi's layout — the scratch claim as
     the checkout, `/venv` as the venv — and adds only provenance and `status` on
     top. Needs `--checkout` decoupled from the project root (already in #162) and
     no chart change at all, because nothing new has to be mounted.
  3. **Replace.** blueapi adopts the beside-the-app layout, which needs the `args`
     passthrough and means two mechanisms coexist during the transition.

  Option 2 is the one to argue against first: it is the only one that costs
  nothing and still ends the silent-divergence risk, which is the whole reason the
  mode exists.

  The `args` issue body is drafted but **not filed** — filing it commits to option
  3 by implication. It is at `.claude/plans/blueapi-args-issue-draft.md`; delete
  it if the review lands elsewhere.

* **The blueapi deployment is not on the pinned chart.** `p47-blueapi-0` carries
  `helm.sh/chart: blueapi-1.17.0` with `debug.enabled: true` — hence its debugpy
  entrypoint, and hence all three of its probes disabled — while `p47-services`
  pins 1.7.2. Any review of the above has to start by establishing which is
  authoritative.
