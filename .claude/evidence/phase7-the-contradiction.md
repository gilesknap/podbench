# Phase 7, step 1 — git says off, the pod says on

2026-08-23/24, `p47-beamline` on pollux, through
`k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig`, against the real
`bl47p-ea-fastcs-01-0`. Launcher under test:
`/home/giles/code/podbench/.venv/bin/podbench`, version
**0.7.3.dev10+gdd089982e.d20260823**, branch `hotfix/easy-to-drive` at `38090fd`.

**This is the read-only half of Phase 7 and only that.** The walk
(`values` → deploy → `check` → `init` → edit → `apply` → observe → `status` →
`retire`) was **not** performed, and the VS Code half — the `--new` refusal and
the debugging-does-not-start failure — was **not** attempted; driving VS Code is
a human-present exception in this project and Giles was AFK. Nothing in this run
wrote to the cluster, to `p47-services`, or to the claim. `--delete-claim` was
not passed.

The one question this file answers is Phase 7's first: **git says hotfix mode is
off and the pod says it is on — which is true?** Along the way it runs the three
new read-only verbs against a real API server for the first time, and settles
Phase 2's registry read against a real registry.

---

## 1. What the pod says, right now

`kubectl get pod bl47p-ea-fastcs-01-0 -n p47-beamline -o json`, read at
2026-08-23T23:4xZ.

| | |
|---|---|
| created / `startTime` | **2026-08-23T17:31:54Z** (age 6h05m at read time) |
| phase | `Running`, 2/2 ready |
| `bl47p-ea-fastcs-01` `restartCount` | **0**, running since 17:32:04Z |
| `temp-controller-simulator` `restartCount` | **0**, running since 17:32:04Z |
| `imageID` | `ghcr.io/diamondlightsource/fastcs-example-debug@sha256:e803e316b14f42d136305ba5adaaa2e020c4a322c9329567283ccfb8ca1555d3` |
| ephemeral containers | **none** — no seat is running |
| StatefulSet | `bl47p-ea-fastcs-01`, `replicas: 1`, generation 18 = observedGeneration 18 |
| `livenessProbe` / `readinessProbe` / `startupProbe` | **absent on both containers** |

**It is hotfix-wired, on all four counts.**

Volumes — eight, the last two podbench's:

```
runtime-volume            pvc p47-runtime-claim
opis-volume               pvc p47-opi-claim
autosave-volume           pvc p47-autosave-claim
bl47p-ea-fastcs-01-data   pvc bl47p-ea-fastcs-01-data
config-volume             configMap bl47p-ea-fastcs-01-config
beamline-data             hostPath /exports/mybeamline/data/
podbench-app              pvc bl47p-ea-fastcs-01-podbench-project
podbench-home             emptyDir sizeLimit 2Gi
```

`volumeMounts` — `/podbench/app` ← `podbench-app`, **on both containers**
(`bl47p-ea-fastcs-01` *and* `temp-controller-simulator`; the values file's
`volumeMounts` key is the pod template's, so the simulator inherits it).
`podbench-home` is declared and mounted by nothing, which is exactly what
`hotfix values` says it is for.

`command: ["bash","-c"]`, `args` — the supervisor loop, verbatim:

```
while :; do
  (
    if [ -x /podbench/app/.venv/bin/python ]; then
      export PATH="/podbench/app/.venv/bin:$PATH"
      echo "podbench: running the hotfixed project"
    fi
    exec bash -c 'stdio-socket --ptty "fastcs-example run /epics/ioc/config/controller.yaml"'
  ) &
  child=$!
  echo $child > /tmp/podbench-child.pid
  wait $child; rc=$?
  kill -TERM -"$child" 2>/dev/null || true
  [ -e /tmp/podbench-hold ] || exit $rc
done
```

`podSecurityContext`:

```
fsGroup: 37887
runAsGroup: 36096
runAsUser: 36096
supplementalGroupsPolicy: Strict
```

Container `securityContext` on both containers is `runAsUser/runAsGroup 37887`,
`allowPrivilegeEscalation: false`, `drop: [ALL]` plus the twelve default
capabilities admission adds.

> The pod-level `runAsUser/runAsGroup: 36096` and `supplementalGroupsPolicy:
> Strict` are **not** in the chart (see §4) and are **not attributed** by this
> run. They are admission-injected on pollux; which policy injects them was
> **not measured** — the token cannot read policy objects.

## 2. The claim

`kubectl get pvc bl47p-ea-fastcs-01-podbench-project -n p47-beamline -o yaml`:

| | |
|---|---|
| phase | **`Bound`** |
| created | **2026-08-23T05:53:32Z** — 11h38m *before* the pod |
| capacity / class | 2Gi, `netapp`, `ReadWriteOnce` |
| bound PV | `pvc-e69a71fe-5d23-45b3-9656-c996b477d842`, created 05:53:51Z, phase `Bound`, **`persistentVolumeReclaimPolicy: Delete`** |
| finalizers | `kubernetes.io/pvc-protection` |
| who mounts it | **exactly one pod in the namespace**: `bl47p-ea-fastcs-01-0`, as `podbench-app` |

Annotations, both intact:

```
argocd.argoproj.io/sync-options: Prune=false,Delete=false
helm.sh/resource-policy: keep
argocd.argoproj.io/tracking-id: p47-beamline_bl47p-ea-fastcs-01:/PersistentVolumeClaim:p47-beamline/bl47p-ea-fastcs-01-podbench-project
```

Labels: `app.kubernetes.io/name=podbench-hotfix-claim`,
`app.kubernetes.io/managed-by=Helm`, `app.kubernetes.io/instance=bl47p-ea-fastcs-01`,
`podbench.dev/hotfix-target=bl47p-ea-fastcs-01`.

`managedFields`: `argocd-controller` last wrote it at **2026-08-23T05:53:32Z**,
i.e. **before** the "turn off hotfix mode" commit and never since. The PV's
reclaim policy is `Delete`, so §4's note about a prune taking the volume with it
is live here too.

## 3. What git declares

`/home/giles/code/p47-services`, branch `podbench-hotfix-claim`, HEAD
`94b74d2394be93b508b5d13c38d16e20418292d2` *"turn off hotfix mode"*, authored
2026-08-23T06:34:15Z. Confirmed identical to the remote:

```
$ gh api repos/epics-containers/p47-services/branches/podbench-hotfix-claim
{"date":"2026-08-23T06:34:15Z","msg":"turn off hotfix mode",
 "name":"podbench-hotfix-claim","sha":"94b74d2394be93b508b5d13c38d16e20418292d2"}
```

The whole of that commit is one line:

```
services/bl47p-ea-fastcs-01/values.yaml | 2 +-
-  enabled: true
+  enabled: false
```

**Which chart each key belongs to**, from
`services/bl47p-ea-fastcs-01/values.yaml`:

| key | file:line | chart |
|---|---|---|
| `ioc-instance.args` (supervisor loop) | `values.yaml:5-20` | the target's own `ioc-instance` |
| `ioc-instance.volumes` (`beamline-data`, `podbench-app`, `podbench-home`) | `values.yaml:44-57` | the target's own `ioc-instance` |
| `ioc-instance.volumeMounts` (`/podbench/app`) | `values.yaml:62-66` | the target's own `ioc-instance` |
| `ioc-instance.command` (`bash -c`) | `values.yaml:67-69` | the target's own `ioc-instance` |
| `ioc-instance.podSecurityContext.fsGroup: 37887` | `values.yaml:73-74` | the target's own `ioc-instance` |
| `podbench-hotfix-claim.enabled: false` | **`values.yaml:81`** | the **claim subchart** |

`ioc-instance:` opens at `values.yaml:3`; `podbench-hotfix-claim:` at
`values.yaml:80`. The commit touched line 81 and nothing else.

## 4. Which is true — decided by rendering the chart

Neither "git" nor "the pod" is lying. **Git HEAD declares a hotfix-wired pod**,
and that is what is running. The only thing `enabled: false` removed is the PVC,
and the PVC did not go.

Rendered from a scratch copy of the real chart root (the service directory, with
its `Chart.yaml`/`templates` symlinks resolved and `helm dependency update` run
into the copy — **`p47-services` itself was not written to**):

```
helm template bl47p-ea-fastcs-01 ./svc -n p47-beamline \
  -f parent-values.yaml -f svc/values.yaml --skip-schema-validation
```

*(`--skip-schema-validation` was needed: `.helm-shared/values.schema.json` as
committed rejects this service's own values — 50-odd `false schema` errors. That
is a separate finding about the consumer repo, not about podbench, and it is
**not** how Argo renders it, since Argo plainly does render this service.)*

At HEAD the render produces **exactly three objects** — one ConfigMap, one
StatefulSet and **one PVC**:

```
$ python3 -c "...yaml.safe_load_all(head2.yaml)... kind==PersistentVolumeClaim"
bl47p-ea-fastcs-01-data
```

`bl47p-ea-fastcs-01-podbench-project` is **not rendered at HEAD**. Re-rendering
the same inputs with `--set podbench-hotfix-claim.enabled=true` adds **one**
object and changes nothing else — the entire diff is the PVC, its two
annotations and its comments:

```
$ diff head.yaml enabled.yaml
1a2,46
> # Source: ec-service/charts/podbench-hotfix-claim/templates/pvc.yaml
> kind: PersistentVolumeClaim
>   name: bl47p-ea-fastcs-01-podbench-project
...
>     helm.sh/resource-policy: keep
>     argocd.argoproj.io/sync-options: Prune=false,Delete=false
```

The StatefulSet is **byte-identical** under both. So `enabled` gates the claim
subchart and nothing else — measured, not reasoned.

Then the render at HEAD against the live StatefulSet:

```
MATCH   configHash                     cd39d292a4c72f5c385e473c9fa948807cd7763e (both)
MATCH   bl47p-ea-fastcs-01.args        (the supervisor loop, character for character)
MATCH   bl47p-ea-fastcs-01.command
MATCH   bl47p-ea-fastcs-01.image
MATCH   bl47p-ea-fastcs-01.volumeMounts
MATCH   temp-controller-simulator.{args,command,image,volumeMounts}
DIFFER  volumes            only `configMap.defaultMode: 420` and `hostPath.type: ""`
DIFFER  podSecurityContext rendered {fsGroup: 37887}; live adds runAsUser/runAsGroup
                           36096 and supplementalGroupsPolicy: Strict
DIFFER  container securityContext  live adds the default capability list and
                           `privileged: false`
```

Every difference is something the API server or an admission policy adds. Every
podbench-relevant field matches, and so does `configHash` — which is computed
from the service's `config/` directory, so it is a hash of the same inputs.

**Verdict: the plan's explanation is correct, and there is no stale sync.** The
live StatefulSet is what HEAD renders. The contradiction is between the *commit
message* and the *values file*: "turn off hotfix mode" did step 5 (the claim's
chart) and not step 4 (the application's own pod template), and the claim's
`Prune=false,Delete=false` then kept the object standing after it left the
desired state.

The state that leaves is worse than either end: **the pod mounts a PVC that git
no longer declares.** Argo will not prune it — that is the annotation working as
designed — but nothing in the desired state would recreate it either. If it were
ever removed, this pod's replacement would not schedule. That last sentence is a
**prediction, not a measurement**: no pod replacement was attempted.

Two things the plan's account states that this run could **not** confirm
directly:

* **"ArgoCD recreated them from git."** All fourteen pods in the namespace are
  the same age (6h05m), so a namespace-wide delete did happen at ~17:31Z. But
  the object that recreated *this* pod is the StatefulSet controller, from
  controller-revision `bl47p-ea-fastcs-01-6b59595ddb`, and `argocd-controller`'s
  last write to the StatefulSet was 05:53:33Z. Reading the controller-revision's
  own age would settle it; the token cannot
  (`controllerrevisions.apps is forbidden`). **Not measured.** It does not
  change the verdict: the template the controller replayed is provably HEAD's.
* **Argo's sync status.** ArgoCD is in another cluster and invisible to this
  token, as the plan says. **Not measured**, and not measurable from here.

---

## 5. The new read-only verbs, against a real API server

### `hotfix check` — exit 0

```
$ podbench hotfix check pod/bl47p-ea-fastcs-01-0 -n p47-beamline
  [ok]    target         bl47p-ea-fastcs-01-0, container
                         bl47p-ea-fastcs-01,
                         statefulset/bl47p-ea-fastcs-01
  [ok]    claim          bl47p-ea-fastcs-01 mounts podbench-app at
                         /podbench/app, and it already carries a
                         project: `hotfix init` seeds nothing over one,
                         so the target root, the project and the
                         interpreter are not asked.
  [ok]    supervisor     bl47p-ea-fastcs-01 is running it:
                         /tmp/podbench-child.pid exists
  [warn]  seat           no podbench container is running in
                         bl47p-ea-fastcs-01-0. Not a blocker -
                         `hotfix init` lands one itself - but it is why
                         `target root` below is unmeasured.
  [ok]    target root    not asked: /podbench/app is already seeded
  [ok]    project        not asked: /podbench/app is already seeded
  [ok]    interpreter    not asked: /podbench/app is already seeded
  [ok]    liveness       no livenessProbe, so nothing cuts a hold short
  [ok]    source         the image names
                         https://github.com/DiamondLightSource/ubuntu-devcontainer
------------------------------------------------------------------------
VERDICT: nothing measured here blocks `podbench hotfix init` (exit 0)
```

stderr was empty. Eight of the nine rows are true against §1: the target and
container resolve, the claim is mounted at `/podbench/app`, no ephemeral
container is running, neither container has a `livenessProbe`. The `seat` row is
a `[warn]` and not a blocker, correctly. **The `source` row is a defect — see
§7.1.**

### `hotfix status` — exit 0

```
$ podbench hotfix status -n p47-beamline
  [ok]    p47-beamline/bl47p-ea-fastcs-01-0  +0 commit(s)  3d55455  active — hotfixed, base image unchanged
    0 commit(s) ahead of the image
    base 3d55455 · podbench <podbench@local> · 2026-08-23T06:24:58+00:00
    note: manifest is schema version 1; it was written by an older
          podbench and some provenance may be absent
```

stderr empty. True: the claim is seeded and the manifest names base `3d55455`,
which is `DiamondLightSource/fastcs-example`'s tag `2025.10.1` (verified in
§6). `+0` is right — this claim carries no hotfix commits. Phase 5's `c9cf51c`
is **gone**: this is a *different* claim, created 05:53:32Z on 2026-08-23 and
re-seeded at 06:24:58Z, not the one Phase 5 left.

The schema-version note is truthful and is exactly rule 6's shape.

### `hotfix status -A` — exit **2**

```
$ podbench hotfix status -A
podbench: kubectl -n p47-beamline --request-timeout=25s get pods --all-namespaces -o json exited 1: Error from server (Forbidden): pods is forbidden: User "system:serviceaccount:p47-beamline:claude-hgv27681" cannot list resource "pods" in API group "" at the cluster scope
```

stdout empty; the whole message on stderr. Corroborated:
`kubectl auth can-i list pods --all-namespaces` → **no**;
`-n p47-beamline` → **yes**.

**This tells the truth, and the important half is what it did *not* do**: it did
not print an empty listing and exit 0. A facility checklist would have read that
as "no hotfixes anywhere", which on this token would be a lie. Two smaller
observations rather than defects:

* Exit **2** is documented (`hotfix.main`: "Exit 2 stays what it has always been
  — podbench refusing the command it was given"), but an RBAC refusal is not a
  malformed command; a checklist that distinguishes 0 from 1 sees the same code
  as a typo. It is at least loud and specific.
* The relayed argv carries **both** `-n p47-beamline` and `--all-namespaces`.
  kubectl resolves that in favour of `--all-namespaces`, so the behaviour is
  right, but the message reads as though podbench asked for two things at once.

### `hotfix retire`, without `--delete-claim` — exit 1

```
$ podbench hotfix retire pod/bl47p-ea-fastcs-01-0 -n p47-beamline
  [ ]     branch         0 commit(s) on this claim are on no branch:
                         `podbench hotfix consolidate pod/bl47p-ea-fastcs-01-0 -n p47-beamline --branch NAME`
                         first, or retiring the claim discards them.
  [ ]     image          the deployed image is still
                         sha256:e803e316b14f, the one this hotfix was
                         made against: the fix is not in a released
                         image yet.
  [ ]     wiring         bl47p-ea-fastcs-01-0 still carries the
                         podbench-app volume, a volumeMount at
                         /podbench/app and the supervisor loop in args.
                         Those are fields in the application's own pod
                         template, not in the claim's chart, so turning
                         the claim off does not remove them: take them
                         back out of the values `podbench hotfix values`
                         emitted, and redeploy.
  [ ]     claim          bl47p-ea-fastcs-01-podbench-project still
                         exists. It is annotated
                         Prune=false,Delete=false so that a hotfix
                         survives somebody reverting a repoint
                         mid-beamtime, which means turning the claim off
                         leaves the object standing: deleting it is a
                         separate, deliberate act
                         (`podbench hotfix retire pod/bl47p-ea-fastcs-01-0 -n p47-beamline --delete-claim`).
------------------------------------------------------------------------
VERDICT: 4 of 4 steps of retirement remain (exit 1)
REMAINING: branch, image, wiring, claim
```

stderr empty. **This is the verb Phase 5 was built for and it found the
specimen unaided.** The `wiring` row is, word for word, the diagnosis §4 took a
chart render to establish — including *why* turning the claim off did not remove
it. The `claim` row is exactly right about `Prune=false,Delete=false`. The
`image` row is right: the manifest's digest and the live `imageID` are both
`sha256:e803e316b14f…`, and §6 confirms the tag still resolves to it, so the tag
has not moved either.

Two of the four rows carry defects — §7.2 and §7.3.

### `hotfix values --from-pod`, run on an already-hotfixed pod

Not asked for, but it is free and it settles an idempotency question the walk
would otherwise have to. `podbench hotfix values --app bl47p-ea-fastcs-01
--from-pod bl47p-ea-fastcs-01-0 -n p47-beamline` exits **0** and emits the six
keys `podbench-hotfix-claim, volumes, volumeMounts, command, args,
podSecurityContext`. The emitted `args` are **identical to the live `args`** —
it extracted the inner entrypoint `stdio-socket --ptty "fastcs-example run …"`
out of the supervisor loop rather than wrapping the loop in a second loop:

```
args identical to live: True
command identical: True
podSecurityContext: {'fsGroup': 37887}
```

`EXISTING_MOUNTS_WARNING` fired on stderr, naming the six mounts podbench cannot
attribute — correct under `--from-pod` per #199.

---

## 6. Phase 2 against a real registry, and the labels are wrong

**podbench's anonymous-token flow works.** `podbench.oci.image_labels` returned
in **0.59–0.72 s** per reference, with no credentials, for all three of: the tag,
the digest, and the sidecar image.

**Cross-checked against a direct registry query**, independent of podbench:

```
curl "https://ghcr.io/token?scope=repository:diamondlightsource/fastcs-example-debug:pull&service=ghcr.io"
curl -H "Authorization: Bearer …" https://ghcr.io/v2/…/manifests/2025.10.1
  docker-content-digest: sha256:e803e316b14f42d136305ba5adaaa2e020c4a322c9329567283ccfb8ca1555d3
  mediaType: application/vnd.oci.image.index.v1+json
    sha256:037669f4358eec…  {arch: amd64, os: linux}
    sha256:88dec3f81f1f80…  {arch: unknown}  attestation-manifest
curl … /blobs/sha256:1aa0aee8a873f3cd…    (config blob, amd64)
```

Two results worth having:

1. **The tag has not moved.** `2025.10.1` resolves to
   `sha256:e803e316b14f…`, which is the pod's live `imageID`.
2. **The direct config-blob labels are byte-identical to podbench's.** podbench
   reads the registry correctly.

But **the labels themselves name the wrong repository**, and loudly:

```
org.opencontainers.image.title       = ubuntu-devcontainer
org.opencontainers.image.source      = https://github.com/DiamondLightSource/ubuntu-devcontainer
org.opencontainers.image.url         = https://github.com/DiamondLightSource/ubuntu-devcontainer
org.opencontainers.image.revision    = 603392d2fd2f3c583e149f4d1266553ccc7a2d90
org.opencontainers.image.version     = noble-20250925
org.opencontainers.image.description = Opinionated Ubuntu based devcontainer for python and EPICS development
```

They are the base image's, inherited. Proved rather than inferred:

```
$ gh api repos/DiamondLightSource/ubuntu-devcontainer/commits/603392d2fd2f…
  2025-10-02T08:30:27Z  "Update ubuntu Docker tag to noble-20250925 (#14)"   ← exists
$ gh api repos/DiamondLightSource/fastcs-example/commits/603392d2fd2f…
  422  No commit found for SHA                                              ← does not exist
$ gh api repos/DiamondLightSource/fastcs-example/git/refs/tags/2025.10.1
  3d55455ec615cb9ccd55c1a93cfc19088aed23bd                                  ← the truth
```

The sidecar `ghcr.io/diamondlightsource/fastcs-example:2025.9.1` is the control:
its labels are correct (`source=…/fastcs-example`,
`revision=40de4f0ef8c7bf40df95190457c908c6177d2444`, `title=fastcs-example`).
So this is the `-debug` variant's build not overriding what it inherited.

podbench already knows this can happen — `LABELS_FROM_BASE_IMAGE` in
`hotfix.py:2262` cites this exact image, measured 2026-08-23, and
`corroborate_source` exists so that `init` believes a label only where something
independent of the image agrees. **`check` does not use it. See §7.1.**

---

## 7. Defects this read found

### 7.1 `hotfix check`'s `source` row reports an inherited label as `[ok]`, with no caveat

`_source_check` (`src/podbench/hotfix.py:4265`):

```python
source = (labels.get(SOURCE_LABEL) or "").strip()
if source:
    return PreflightCheck("source", CheckStatus.OK, f"the image names {source}")
```

Any non-empty label is `[ok]`. On this target that prints
`the image names https://github.com/DiamondLightSource/ubuntu-devcontainer`
under a verdict of *"nothing measured here blocks `podbench hotfix init`"* — and
the named repository is not the application's source, as §6 proves twice over.

The consequence is not cosmetic. On an **unseeded** claim — the state `check` is
written for, "before starting one" — `hotfix init` with no `--repo` and no
`--base-commit` would clone `ubuntu-devcontainer` and record `603392d2…`, a
commit in a different repository, as the base the drift is measured against.
`init` has `corroborate_source` for exactly this and would at least record an
assumed base; `check`, which exists to say `init`'s answers earlier, says
something *more* confident than `init` would. That is the wrong direction for a
pre-flight.

Cheapest fix consistent with what is already there: run `check`'s label through
`corroborate_source`, and where nothing independent corroborates it, report the
row as `[warn]`/unmeasured carrying `LABELS_FROM_BASE_IMAGE` rather than `[ok]`.
On this target there are two independent contradictions available for free — the
claim's own manifest records base `3d55455`, and
`org.opencontainers.image.title=ubuntu-devcontainer` does not match the image
repository's own last path segment `fastcs-example-debug`.

### 7.2 `hotfix retire`'s `branch` row counts a step that has nothing in it, and tells you to run a command that will refuse

The row printed:

> `[ ]  branch  **0 commit(s)** on this claim are on no branch:
> `podbench hotfix consolidate … --branch NAME` first, **or retiring the claim
> discards them**.`

There are zero commits. Nothing would be discarded. And `consolidate` on this
exact claim would **refuse** (`hotfix.py:2869`):

```python
if not commits:
    raise HotfixError(
        f"the checkout is not ahead of {manifest.base_commit[:7]}: there is "
        "no hotfix to consolidate. If the fix is already in the image, retire "
        "the claim instead — see `hotfix status`."
    )
```

So `retire` says *consolidate first* and `consolidate` says *retire instead*,
about the same claim, in the same minute. `_branch_check` (`hotfix.py:4778`)
returns `False` whenever `consolidated_branch is None`, with no `ahead == 0`
arm. The verdict inherits it: **"4 of 4 steps of retirement remain" is wrong;
three remain.** Exit 1 is still correct, so the assertion holds — but the count
and the instruction do not, and this is a verb whose whole value is that its
count is trustworthy.

### 7.3 `hotfix retire`'s `wiring` row under-enumerates what has to come out

It names three things: the `podbench-app` volume, the `/podbench/app` mount, and
the supervisor loop in `args`. The values that actually have to be removed are
**six**, and `hotfix values` emitted all six in the same session:
`podbench-hotfix-claim`, `volumes` (**both** `podbench-app` *and*
`podbench-home`), `volumeMounts`, `command`, `args`, `podSecurityContext.fsGroup`.

Somebody who removes exactly what the row names leaves the `podbench-home`
emptyDir and `fsGroup: 37887` in the pod template. The row's closing clause —
"take them back out of the values `podbench hotfix values` emitted" — does point
at the whole snippet, so this is an incomplete enumeration rather than a false
one. It is still the row a person reads as a checklist.

### 7.4 Contract tension: `status` exits 0 on a pod `retire` says is 4 steps from retired

Same pod, same minute: `hotfix status` → **0**, `hotfix retire` → **1**.

This is *documented*. `HotfixRow.retirement` says "Deliberately not part of
:attr:`ok`. A consolidated fix whose image has not moved yet is a live hotfix
doing its job, and a shutdown assertion that went red the moment `consolidate`
ran would be one nobody could leave in CI." That reasoning is sound.

But `hotfix.main`'s own docstring sells `status` as the assertion *"no pod is
still carrying an unretired hotfix"* — and on this pod, that sentence is false
while `status` exits 0. Both sentences are in the repo; they cannot both be
right. Flagging it rather than resolving it: which one is meant is a decision,
not a measurement. If `status` is the shutdown gate, this specimen is precisely
the thing a shutdown gate should catch — a pod wired to a claim git no longer
declares, carrying no fix at all.

### 7.5 (consumer repo, not podbench) `.helm-shared/values.schema.json` rejects its own service's values

`helm template` at HEAD fails schema validation with ~50 errors against
`services/bl47p-ea-fastcs-01/values.yaml`, including
`additional properties 'fsGroup' not allowed` under
`ioc-instance.podSecurityContext` and a doubly-nested
`'/ioc-instance/ioc-instance'`. Argo evidently does not enforce it (the service
is deployed), so this is a local-tooling papercut in `p47-services`, recorded
because the next person to render this chart will hit it in the first minute.
**Nothing was changed in `p47-services`.**

---

## 8. Not measured

* **The walk.** `init`, `apply`, `consolidate`, `retire --delete-claim`,
  `attach`/`dev`/`vscode`, and any values change and redeploy — **not run**,
  read-only session.
* **The VS Code half.** The `--new` refusal and the debugging-does-not-start
  failure — **not attempted**. Deferred to a session with Giles present.
* **The four numbers.** `restartCount` is 0 and both containers have run since
  17:32:04Z, but no relaunch was performed, so *"the recorded child pid moved"*
  and *"the edit live in the running process"* are **not measured**, and *"every
  seat alive"* is vacuous — there is no seat.
* **Argo's sync status / target revision for this service.** ArgoCD is in
  another cluster and invisible to this token. Which commit Argo believes it is
  on is **not measured**; §4 settles the question without it.
* **The controller-revision that recreated the pod**, and therefore whether
  ArgoCD or the StatefulSet controller did it. `controllerrevisions.apps` is
  forbidden to this service account.
* **Namespace events.** `kubectl get events -n p47-beamline` returns nothing for
  this pod or claim — the retention window has passed. Whether Argo ever
  attempted a prune on the claim is **not measured**; the `managedFields`
  timestamp (05:53:32Z, before the turn-off) is the only trace and it is
  consistent with "never touched it again".
* **Which admission policy injects `runAsUser/runAsGroup: 36096` and
  `supplementalGroupsPolicy: Strict`** at pod level. Not in the chart, not
  readable by this token.
* **The claim's git remote.** Reading `origin` inside the checkout would have
  given `check` an independent corroborator for §7.1; that needs a seat or a
  direct exec and was not taken. The manifest's `base 3d55455` is the
  independent evidence used instead.
* **Whether a replacement pod would fail to schedule if the claim were pruned.**
  Stated as a prediction in §4; no pod replacement was attempted.

## 9. State left behind

Unchanged. `bl47p-ea-fastcs-01-0` is `Running`, 2/2, `restartCount` 0, still
hotfix-wired, still mounting a `Bound` 2Gi claim that git no longer declares.
`p47-services` is on `podbench-hotfix-claim` at `94b74d2` with a clean working
tree. No seat was landed. Nothing was deleted.
