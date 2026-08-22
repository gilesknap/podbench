# Phase 5 — the workflow, run as a user would

2026-08-22, `p47-beamline` on pollux, against the real `bl47p-ea-fastcs-01`,
through `k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig`.

The acceptance test is the four steps at the top of
`.claude/plans/hotfix-becomes-a-workflow.md`, performed in order, with **no
hand-editing of yaml at any point**. That held: every line of
`services/bl47p-ea-fastcs-01/values.yaml` on the deployed branch is
`--print-values` output, and the claim came from a chart dependency.

Released as **0.7.1** first, because Phase 5 needs the Phase 3 subchart on a
registry Argo can reach and CI only publishes charts on a tag.

---

## Starting state

Cleaner than the plan expected. **Both claims the last run left on the beamline
had already been deleted**, so this was a genuine first run rather than a re-run
over a seeded claim — and the "wipe or keep" decision the plan asked for did not
arise.

`p47-deployment` was on `main` for every service. `bl47p-ea-fastcs-01-0` was up
with its original entrypoint, `restartCount` 0, and six volumes, `beamline-data`
among them and no podbench ones.

The stale seat image the plan flags is gone too: the run pinned
`--image ghcr.io/gilesknap/podbench:0.7.1` and the seat reported
`version 0.7.1, the same build as this launcher`.

---

## Step 1 — `--print-values --values` emits a file that is deployed as emitted

```
podbench hotfix --print-values --app bl47p-ea-fastcs-01 \
  --from-pod bl47p-ea-fastcs-01-0 -n p47-beamline \
  --values services/bl47p-ea-fastcs-01/values.yaml \
  --parent-values services/values.yaml
```

stdout was redirected over the file it came from and committed unedited. On
stderr, three notes and nothing else needed:

```
podbench: the application's keys went under ioc-instance, where this
file already declares them. --values-under puts them somewhere else.
podbench: volumes came from services/values.yaml and has been copied
into this file. ... Copied: beamline-data.
podbench: volumeMounts came from services/values.yaml and has been
copied into this file. ... Copied: beamline-data.
```

The service declares none of the five keys itself, so where they go was read
from the **shared** file — which is the case #192 said would need it.

### The diff against the live StatefulSet

Rendered as `argocd-apps` 5.5.0 renders it, `-f ../values.yaml -f values.yaml`:

| | live | rendered |
|---|---|---|
| volumes | 6 | 8 — `podbench-app`, `podbench-home` added |
| volumes lost | | **none** |
| mounts | 6 | 7 — `podbench-app` added |
| mounts lost | | **none** |
| `securityContext.fsGroup` | unset | `37887` |
| claims | `…-data` | `…-data`, `…-podbench-project` |

`beamline-data` is present in both, which is the whole reason `--parent-values`
exists. Everything else that differs is render-versus-live noise the API server
adds: `terminationMessagePath`, `dnsPolicy`, the container's default
capabilities, and Argo's `$ARGOCD_*` substitutions.

## Step 2 — the claim comes from the dependency, and `p47-services` has no template

`.helm-shared/Chart.yaml` gained one entry:

```yaml
  - name: podbench-hotfix-claim
    version: 0.7.1
    repository: "oci://ghcr.io/gilesknap/charts"
```

and `.helm-shared/templates/hotfix_claim.yaml` — hand-written by the last run —
is **gone**. The branch was reset to `main` and rebuilt from generated output
rather than edited, so nothing hand-written survives on it.

`.helm-shared/values.schema.json` gained one key, `$ref`ing the schema the
release attaches. That is the one consumer-side cost #191 predicted and it is
one entry.

**The other seven services pay nothing.** Rendered locally, each with the shared
`Chart.yaml` that now carries the dependency:

```
bl47p-mo-ioc-01        total objects: 3   podbench objects: 0
bl47p-synoptic         total objects: 3   podbench objects: 0
bl47p-ea-dcam-01       total objects: 2   podbench objects: 0
bl47p-ea-panda-01      total objects: 3   podbench objects: 0
```

That is #191's falsification, checked against the real consumer rather than a
fixture.

## Step 3 — push, Argo syncs, the pod comes back carrying the layout

`p47-services` `podbench-hotfix-claim` force-pushed at `e469bcc`;
`p47-deployment` repointed at `4892566`, **20:35:10Z**.

| | |
|---|---|
| claim created | 20:36:04Z, ~54s after the push |
| claim `Bound` | 20:37:02Z, 2Gi, `netapp` |
| pod replaced and `Ready` | 20:37:32Z, `restartCount` 0 |
| total | **under two and a half minutes**, hands-off |

The claim as Argo left it, both annotations intact — from the subchart, not from
anything in `p47-services`:

```
helm.sh/resource-policy: keep
argocd.argoproj.io/sync-options: Prune=false,Delete=false
argocd.argoproj.io/tracking-id: p47-beamline_bl47p-ea-fastcs-01:/PersistentVolumeClaim:...
labels: app.kubernetes.io/name=podbench-hotfix-claim
        podbench.dev/hotfix-target=bl47p-ea-fastcs-01
```

The pod carries eight volumes — the six it had, plus `podbench-app` and
`podbench-home` — `securityContext.fsGroup: 37887`, and the supervisor loop as
its `args`.

## Step 4 — `vscode` lands a seat, mounts the claim unasked, and opens the project

`podbench vscode bl47p-ea-fastcs-01-0 -n p47-beamline`. The mount, unasked:

```
WARNING  this pod carries the hotfix layout, so the claim 'podbench-app'
         was mounted into the seat at /podbench/app without being asked
         for - Hotfix mode needs the claim at the same path in both, and
         an ephemeral container's volumeMounts are fixed once it is
         created, so there is no adding it afterwards
```

The mode, from the report:

```
  [x] iterate (edit, relaunch, verify through the Service)
      `podbench hotfix apply` relaunches the application's own child in
      place: this pod carries the supervisor, so the loop runs on the
      live workload without a second pod and without restarting the
      container.
```

And the folder — #189, on a real pod:

```
editor
  [ok] opening /podbench/app and not the seat's home /home/podbench -
       this pod carries the hotfix layout, and the claim is the only
       tree here where an edit reaches the running process
  [ok] ssh reaches the seat, so Remote-SSH will too
```

**Measured, not inferred.** This machine is headless, so `code` was a stub on
`PATH` that records its argv and exits 0 — it opens no window, and that is
stated rather than glossed. What podbench asked it to do is the assertion, and
that is exactly what the stub recorded:

```
--remote ssh-remote+podbench-p47-beamline-bl47p-ea-fastcs-01-0-1 --install-extension ms-python.python
--remote ssh-remote+podbench-p47-beamline-bl47p-ea-fastcs-01-0-1 --install-extension ms-python.debugpy
--remote ssh-remote+podbench-p47-beamline-bl47p-ea-fastcs-01-0-1 /podbench/app
```

Corroborated from the other end: before `hotfix init` ran, the only thing on the
claim was `/podbench/app/.vscode` — the settings the editor step wrote, in the
claim and not in the seat's home.

**One thing the run needed that the plan did not predict.** The first `vscode`
exited 2 at the ssh check: this container had no `~/.ssh/config` at all, so
ssh never read podbench's stanza. `podbench doctor --fix` inserted the `Include`
line and the second run went through. Worth recording because podbench refused
*before* the vscode-server download rather than after it, which is the behaviour
that step exists for.

### A defect this step found

`--print-values --from-pod POD --values FILE` printed `EXISTING_MOUNTS_WARNING`
— telling the user to hand-merge — and then printed a correctly merged file.
That warning exists because a pod cannot say which of its volumes the service
asked for; under `--values` the values file has just said. Fixed in
gilesknap/podbench#199 before this write-up, and it is the only thing running
the workflow end to end turned up.

---

## The loop, on the live IOC

`hotfix init pod/bl47p-ea-fastcs-01-0 --repo https://github.com/DiamondLightSource/fastcs-example
--ref 2025.10.1 --venv /podbench/app`, ~4 minutes, with `uv sync` running in the
application container and surviving the launcher's own wait:

```
seeded /podbench/app from /proc/1/root/app
copied the interpreter to /podbench/app/.python
claim seeded, venv interpreter 3.11.13
checkout already present at /podbench/app
rebuilt the venv at /podbench/app/.venv
wrote /podbench/app/.podbench-hotfix.json
```

The edit was a marker printed at import of `src/fastcs_example/__init__.py`,
made **on the claim through the seat**, and confirmed absent from the image's
own copy at `/app/src/fastcs_example/__init__.py` before `apply` ran.

```
$ podbench hotfix apply pod/bl47p-ea-fastcs-01-0 --venv /podbench/app -m ...
committed as giles knap <gilesknap@gmail.com>
no packaging metadata changed; editable install still valid
1 commit(s) ahead of 3d55455
relaunched the application in bl47p-ea-fastcs-01 without a restart
```

### The four numbers — the contract, and all four held

| | before | after |
|---|---|---|
| `restartCount` | 0 | **0** — the container never restarted |
| recorded child pid | 7 | **1065** — the supervisor relaunched its child |
| the edit, in the running process | absent | **present** |
| seats | `podbench-1` running since 20:38:14Z | **same seat, same start time** |

The application container's logs, after the relaunch:

```
podbench: running the hotfixed project
PODBENCH_PHASE5_MARKER phase-5-workflow
```

Both lines matter. The first is the runtime switch choosing
`/podbench/app/.venv/bin/python` — so the process is resolving the claim. The
second is code that exists nowhere but the claim, running in a container whose
`restartCount` is still 0.

`hotfix status` agrees, unprompted:

```
[ok]  p47-beamline/bl47p-ea-fastcs-01-0  +1 commit(s)  c9cf51c  active — hotfixed, base image unchanged
  1 commit(s) ahead of the image
  base 3d55455 · giles knap <gilesknap@gmail.com> · 2026-08-22T20:46:05+00:00
    c9cf51c  Phase 5: prove an edit on the claim reaches the running process
```

---

## Re-proving #190: the revert prunes everything except the claim

The repoint reverted at **20:46:41Z** (`682da5b`). Argo synced in **20 seconds**,
and the pod came back on the image's own code:

| | after the revert |
|---|---|
| pod volumes | 6 — `podbench-app` and `podbench-home` **pruned**, `beamline-data` still there |
| entrypoint | `stdio-socket --ptty "fastcs-example run …"` — the original, supervisor gone |
| `securityContext.fsGroup` | unset again |
| memory limits | back to the chart's `256Mi`/`64Mi` — the editor's resize went with the roll, exactly as its own warning said it would |
| seat | gone with the pod |
| `PODBENCH_PHASE5_MARKER` in the logs | **absent** — running the released image again |
| pod | `Ready`, `restartCount` 0, `iocRun: All initialization complete` |
| **the claim** | **`Bound`.** |

That is the prune-on-sync path, and this time the survival *is* attributable:
Phase 1 measured that `helm.sh/resource-policy: keep` alone would have been
pruned within about three minutes and taken its PV with it. The claim survived
because the `podbench-hotfix-claim` subchart carries
`argocd.argoproj.io/sync-options: Prune=false,Delete=false` — nothing in
`p47-services` says so.

---

## State left behind

`p47-deployment` is on `main` for every service. `p47-services`
`podbench-hotfix-claim` still holds the generated values, which is the point of
the branch. `bl47p-ea-fastcs-01-0` is on its original entrypoint, `Ready`, and
serving.

**One 2Gi claim, `bl47p-ea-fastcs-01-podbench-project`, is still `Bound`,
carrying the checkout and commit `c9cf51c`.** That is the design working, not
litter: it is step 6 of the retirement checklist and a deliberate act, and the
test service account has no `delete` on `persistentvolumeclaims` by design.
Deleting it needs Giles. Left in place it is also a ready-made `status` case and
a warm claim for the next run.

---

## What was not measured

* **A window did not open.** This machine is headless. `code` was a stub that
  records argv and exits 0, so what is proven is the argv podbench built, not
  VS Code's behaviour on receiving it.
* **`consolidate`** — unchanged from the last run and still parked: pushing to a
  real fork would write a PAT onto a shared beamline claim.
* **`bl47p-mo-ioc-01`** was not touched at all this run. The compiled-IOC probe
  had nothing new to answer; #34 still owns what hotfixing one would mean.
* **A pod replacement mid-hotfix** was not re-run — the last run proved it and
  this plan explicitly says not to re-measure it.
* **`--values` against a chart that nests its keys somewhere other than
  `ioc-instance`** exists only as a unit test. No second real chart was to hand.
