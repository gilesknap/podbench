# Phase 5 — the cluster run, against the real `bl47p-ea-fastcs-01`

2026-08-22, `p47-beamline` on pollux, through
`k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig`. Both targets were on
`main` with their original entrypoints at the start, and are back on `main` with
their original entrypoints at the end.

Delivered the only way there is: values committed to `p47-services`
`podbench-hotfix-claim`, `p47-deployment` repointed at it, Argo synced. Repoint
reverted afterwards (`12556a5`).

**This run used a real per-service `PersistentVolumeClaim`**, not the generic
ephemeral volume the first run used — which is what made "survives pod
replacement" measurable for the first time.

---

## Re-proving phases 1-4 on the pod

### Phase 1 — `attach` mounts the claim, `init` lands its own seat

`podbench attach bl47p-ea-fastcs-01-0` with **no** `--mount`:

```
WARNING  this pod carries the hotfix layout, so the claim 'podbench-app'
         was mounted into the seat at /podbench/app without being asked
         for - Hotfix mode needs the claim at the same path in both, and
         an ephemeral container's volumeMounts are fixed once it is
         created, so there is no adding it afterwards
```

Same path in both containers, and writable from the seat:

```
seat: drwxrwxrwx. 2 99 99 4096 /podbench/app   -> touch succeeded
app : drwxrwxrwx. 2 99 99 4096 /podbench/app   uid=37887 gid=37887
```

`hotfix init` against a pod with **no** seat (`bl47p-mo-ioc-01-0`, freshly
replaced, `ephemeralContainers` empty):

```
podbench: no seat is running in bl47p-mo-ioc-01-0, so one is being landed -
hotfix mode reaches the claim through a seat, and `attach` mounts the claim
itself on a pod carrying the layout.
```

The refusal that started #177 never appeared. And the deliberate asymmetry
holds: `consolidate` on the same seatless pod refused rather than landing one,
which is what the plan asked for.

### Phase 2 — `--from-pod` needs no hand-editing

Measured before deployment, against both pods on `main`. Full output in
`phase2-from-pod-on-p47.md`. The end-to-end assertion for #176:

```
  initialDelaySeconds: 120
  periodSeconds: 30
  timeoutSeconds: 1
  successThreshold: 1
  failureThreshold: 3
```

Nobody typed 120 or 30. The emitted probe's five fields match the **live** pod
exactly, where the chart's own values state only two.

All four failure paths were exercised against the real cluster, and each named
`--no-from-pod` and what it costs:

| Broken deliberately | kubectl said |
|---|---|
| absent pod | `Error from server (NotFound): pods "no-such-pod-0" not found` |
| wrong context | `error: context "nope" does not exist` |
| refused credential | `error: You must be logged in to the server (Unauthorized)` |
| container not in pod | `container 'nope' not in pod` |

A target with no `livenessProbe` (fastcs) emitted no probe block and **no error**.

### Phase 3 — a missing project is not a ptrace denial

From `bl47p-mo-ioc-01-0`'s seat, measured directly:

```
/proc/1/root  ->  LISTS CLEANLY
  autosave bin boot data dev epics etc home lib ... podbench proc python
  root run sbin srv sys tmp usr var venv
/app   ABSENT
/venv  EXISTS
```

So the distinction is real, not a reworded guess. `hotfix init` there:

```
podbench: the target's image has no project at /app, so there is nothing to
seed the claim from. Its filesystem reads fine (/proc/1/root lists) - this is
a layout difference, not a permission one.
```

Asserted mechanically: names the missing project ✓, names `--image-project` ✓,
does **not** contain "ptrace" ✓, does **not** contain "doctor" ✓.

### Phase 4 — the two reports

On the hotfixed fastcs pod:

```
  [x] iterate (edit, relaunch, verify through the Service)
      `podbench hotfix apply` relaunches the application's own child in
      place: this pod carries the supervisor, so the loop runs on the
      live workload without a second pod and without restarting the
      container.
```

On `bl47p-mo-ioc-01`, whose wrapped probe was in place — the `61-91s` restart
deadline is gone, and survives only as what applies once the hold is lifted:

```
  [x] live attach (gdb -p <pid>)
      no deadline while the hold is in place: 'bl47p-mo-ioc-01' answers
      its liveness probe through podbench's hold-aware wrapper, which
      returns 0 whenever /tmp/podbench-hold exists - so nothing restarts
      it while it is held. Once the hold is gone the target's own check
      applies again, at 61-91s
```

---

## The full workflow, end to end

`init` → edit → `apply` → `status`, holding the same four numbers the first run
took. They are the contract; everything else is commentary.

| | before | after |
|---|---|---|
| `restartCount` | `0, 0` | `0, 0` |
| recorded child pid | `7` | `505` |
| running interpreter | `/python/cpython-3.11.13-…` (the image's) | `/podbench/app/.python/cpython-3.11.13-…` (the claim's) |
| `HOTFIX_MARKER` in running code | absent | present |
| seats | `podbench-1` since 17:32:12Z | same, alive |

`apply` said `relaunched the application in bl47p-ea-fastcs-01 without a
restart`, and the pod stayed `ready` throughout. `init` ran for several minutes
with the IOC untouched — `restartCount 0`, child pid still 7 — because it only
ever writes to the claim.

---

## The two things no cluster had done

### Survives pod replacement

`kubectl delete pod bl47p-ea-fastcs-01-0`, then measured on the replacement:

```
pod UID before : 9d588994-57dd-4ff5-9366-b53336a0f770
pod UID after  : af554681-cb5e-482b-a879-efb582a4779c   <- a different pod
restartCount   : 0 0
seats          : []                                     <- ephemeral, gone
child pid      : 7                                      <- a fresh supervisor
interpreter    : /podbench/app/.python/cpython-3.11.13-linux-x86_64-gnu/bin/python3.11
log            : "podbench: running the hotfixed project"
                 "podbench: podbench-live-test-phase5"
```

**The hotfix survived.** The runtime switch fired on a brand-new pod and took
the claim's interpreter, which is exactly what a real PVC buys and the generic
ephemeral volume could not. `hotfix status` still reported it, on a pod with no
seat at all.

The claim also survived the repoint being reverted — Argo left both PVCs
`Bound` while pruning everything else the branch added. That is #67's pair of
annotations working: `helm.sh/resource-policy: keep` and
`argocd.argoproj.io/sync-options: Prune=false,Delete=false`.

### `consolidate`, and the `superseded` verdict

Run by a cluster for the first time. Pushed to a bare repo created for the
purpose and reached through `--remote`, so that no credential was written to a
claim on a shared beamline:

```
pushed 1 commit(s) to phase5/podbench-phase5-consolidate
```

and the branch really landed, with the hotfix commit on top of the base:

```
d48471a phase 5 live test: HOTFIX_MARKER in __main__
3d55455 Merge pull request #8 from DiamondLightSource/adopt-uv
```

`--dry-run` first, which emitted the five-step retirement checklist unchanged.

`status` verdicts, both measured:

```
[ok]  active — hotfixed, base image unchanged            exit 0
[!]   image-changed — image upgraded under the hotfix mount   exit 1
[!]   superseded — consolidated; claim is probably stale      exit 1
```

`superseded` names the branch and states the risk: "If the rebuild included it,
the claim is now shadowing the released fix with an older copy of it".

---

## What was **not** measured, and why

**The image was never actually bumped.** `image-changed` and `superseded` were
produced by editing the digest the manifest records — which is precisely the
comparison `status` makes — and the manifest was restored afterwards. A real
bump needs a known-good tag for a live beamline IOC, and the credential
available could not list the registry's tags. The comparison is exercised; the
rollout that would trigger it is not.

**`consolidate` did not push to GitHub.** The mechanism is proved against a real
git remote, but pushing to a `gilesknap` fork would mean writing a PAT into the
claim's git config on a shared beamline volume, readable by anyone who can exec
into the pod. That is a decision for a human, not a step to take unattended.

---

## State the run was left in

Both services back on `main`, both pods `Running` and `ready` on their original
entrypoints, no seats, `restartCount 0`, all 14 pods in the namespace Running.

**Two claims are deliberately still there** — `bl47p-ea-fastcs-01-podbench-project`
and `bl47p-mo-ioc-01-podbench-project`, 2Gi each, `Bound`. Surviving the revert
is the annotations working as designed. The test service account has
`get list watch` on persistentvolumeclaims and no `delete`, so retiring them
needs someone who can.
