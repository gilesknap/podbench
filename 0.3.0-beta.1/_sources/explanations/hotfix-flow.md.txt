# What `hotfix` does

Hotfix mode makes an edit **outlive the session**. The application's venv lives on a
PersistentVolumeClaim rather than in the image, so a container restart no longer
yields pristine image code — which inverts the rule the other two modes are built on.
Here the restart *is* the relaunch mechanism, probes and all.

It is the one mode that needs deploy-time cooperation, and the only one that touches
the workload. It is Python-only, single-replica-only, and it has been exercised
against unit tests but never against a live cluster.

Four verbs: `init`, `apply`, `status`, `consolidate` — plus `--print-values`, which
emits the chart snippet the whole thing depends on.

## The layout it requires

Nothing works until the claim is mounted over the application's venv path *in both
containers, at the same path*.

```text
  ┌─────────────────────────────── pod ────────────────────────────────┐
  │                                                                    │
  │  initContainer podbench-seed-venv    (the application's own image) │
  │     mounts the claim at /podbench-seed — a *staging* path, because │
  │     this is the only moment the image's own venv is still visible  │
  │     cp -a /opt/venv/. /podbench-seed/                              │
  │                                                                    │
  │  ┌──────────────────────┐         ┌──────────────────────────────┐ │
  │  │ application          │         │ podbench seat                │ │
  │  │                      │         │  (ephemeral, from `attach`)  │ │
  │  │  runs the code       │         │  has git, a shell, podbench  │ │
  │  │                      │         │                              │ │
  │  │  /opt/venv           │         │  /opt/venv                   │ │
  │  │    bin/python        │         │    the SAME mountPath, so    │ │
  │  │    lib/…             │         │    the venv and the checkout │ │
  │  │    src/   ← checkout │         │    resolve identically on    │ │
  │  │    .podbench-hotfix. │         │    both sides                │ │
  │  │      json ← manifest │         │                              │ │
  │  └───────────┬──────────┘         └──────────────┬───────────────┘ │
  │              │                                   │                 │
  │              └─────────────────┬─────────────────┘                 │
  │                                │                                   │
  │                  PVC  <app>-venv  (ReadWriteOnce)                  │
  │                                                                    │
  └────────────────────────────────────────────────────────────────────┘
```

`podbench hotfix --print-values --app NAME --venv-path /opt/venv` emits exactly that:
the claim, the seeding initContainer, the `podbench-home` and `podbench-identity`
volumes, and the `fsGroup` without which the seat's home is present and unwritable.

Two things about the seed are easy to get wrong and both fail silently:

* it **must** run as an initContainer, because once the claim is mounted over the venv
  path the image's copy is behind the mount in every container — a podbench that
  offered to seed after the fact would be offering to copy a directory it cannot see;
* it **must** use the application's own image, or the venv is the wrong one.

Every verb reaches the claim through a `HotfixStore`:

```text
   default        PodStore   → kubectl exec -c <seat> POD -- <cmd>
                              the seat is the container with git and a shell;
                              the application container is assumed to have neither
                              and in this mode is quite likely distroless
   --local        LocalStore → plain filesystem calls, for running the verb from
                              inside the seat's own terminal
```

## `hotfix init` — prepare the claim

```text
podbench hotfix init TARGET --repo URL --venv /opt/venv [--ref REF]
                            [--base-commit SHA] [--no-install] [--seat NAME]
    │
    ▼
┌──────────────────────────────────────────────────────────────────┐
│ RESOLVE THE TARGET   pod/NAME, deployment/NAME or statefulset/…  │
│                                                                  │
│   workload named  → get deployment NAME -o json                  │
│                     replicas != 1 → refuse                       │
│                     get pods -l <matchLabels> -o json            │
│                     exactly 1 live pod, or refuse                │
│   pod named       → get pod NAME -o json, then walk *up*:        │
│                     ReplicaSet → Deployment, checking replicas   │
│                     at each hop                                  │
│                                                                  │
│   The Deployment is what has to be annotated: annotating the     │
│   ReplicaSet's template is overwritten by the next rollout, and  │
│   annotating the pod does not survive the reschedule this mode   │
│   relies on.                                                     │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ replicas != 1 ──────────────▶ exit 2
                                  ▼   RWO claim: a second replica either cannot
                                      schedule, or with RWX races on one checkout
┌──────────────────────────────────────────────────────────────────┐
│ FIND THE SEAT     get pod POD -o json → the running podbench-N   │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ none, and no --seat ────────▶ exit 2
                                  ▼         ("run `podbench attach POD` first")
┌──────────────────────────────────────────────────────────────────┐
│ VERIFY THE SEED — never perform it                               │
│   cat /opt/venv/pyvenv.cfg                                       │
│     absent → the claim is mounted but was never seeded           │
│   record the interpreter version from it (it is on the volume,   │
│   which is the only place that stays true across an image bump)  │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ no pyvenv.cfg ──────────────▶ exit 2
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ PUT THE SOURCE ON THE CLAIM      checkout = /opt/venv/src        │
│   test -e /opt/venv/src/.git                                     │
│     present → left alone                                         │
│     absent  → git clone [--branch REF] URL /opt/venv/src         │
│   git -C … rev-parse HEAD    → the base commit (or --base-commit)│
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ EDITABLE INSTALL — in the APPLICATION container, not the seat    │
│   exec -c APP -- /opt/venv/bin/python -m pip install \           │
│        --no-deps --no-build-isolation -e /opt/venv/src           │
│                                                                  │
│   There, because the venv's bin/python is a symlink to an        │
│   interpreter inside the application image, which podbench's own │
│   image cannot resolve — even though the venv is on a volume     │
│   both can see.  --no-deps because a hotfix is a code change;    │
│   pulling new dependencies mid-run is a release.                 │
│   No pip in that image? --no-install, and install once at build. │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ install fails ──────────────▶ exit 2
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ RECORD THE PROVENANCE — twice, deliberately                      │
│   1. write /opt/venv/.podbench-hotfix.json  (on the claim)       │
│        venv, checkout, repo, base image + digest, interpreter,   │
│        container, commit, base_commit, author, timestamp         │
│   2. patch deployment NAME --type=merge                          │
│        spec.template.metadata.annotations:                       │
│          podbench.dev/hotfixed: "true"                           │
│          podbench.dev/hotfix-manifest: <the JSON>                │
│          podbench.dev/hotfix-applied-at: <timestamp>             │
│                                                                  │
│   A *merge* patch, and deliberately the opposite choice from the │
│   Service cutover in `dev`: here union is exactly right —        │
│   podbench is adding three keys, not replacing the map.          │
│   On the pod template, not the pod: annotations do not survive   │
│   the reschedule this mode depends on.  A bare pod gets its own  │
│   annotation and a warning that it is lost on replacement.       │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
                        actions printed, exit 0
```

## `hotfix apply` — commit, reinstall if needed, roll

```text
podbench hotfix apply TARGET -m "message" --venv /opt/venv [--no-bounce]
    │
    ▼
   resolve the target and the seat (exactly as init does)
    │
    ▼
   cat /opt/venv/.podbench-hotfix.json
    │
    ├─ absent ──────────────────────────────────▶ exit 2  ("run init first")
    ▼
┌──────────────────────────────────────────────────────────────────┐
│ COMMIT                                                           │
│   git -C SRC status --porcelain                                  │
│     dirty → git add -A                                           │
│             git -c user.name=… -c user.email=… commit -m "…"     │
│     clean → "nothing new to commit" (a hand commit in the seat   │
│             is a normal way to get here)                         │
│                                                                  │
│   Not optional: a hotfix that is only a working-tree edit has no │
│   sha, and without a sha the manifest cannot say what is running │
│   and `consolidate` has nothing to push.                         │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ MEASURE THE DRIFT                                                │
│   git -C SRC rev-parse HEAD                                      │
│   git -C SRC log <base_commit>..HEAD     → commits ahead         │
│   git -C SRC diff --name-only <last recorded commit>..HEAD       │
│                                                                  │
│   Keyed on the commit range, not on whether the tree was dirty:  │
│   several hand commits can accumulate between two applies.       │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
                 pyproject.toml / setup.py / setup.cfg / MANIFEST.in
                 among the changed paths?
                    │                          │
                yes │                          │ no
                    ▼                          ▼
     rerun the editable install       "editable install still valid"
     in the application container     (an editable install is a path
                    │                  redirection: new code is free,
                    │                  a new entry point is not)
                    └───────────┬─────────────┘
                                ▼
   write the manifest back      (commit, author, timestamp, ahead, commits[:20])
   patch the pod template's annotations   ← the applied-at timestamp changes,
                                            which is what makes it a real edit
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ BOUNCE  (unless --no-bounce)                                     │
│   Deployment / StatefulSet → nothing more to do: the annotation  │
│       edit has already rolled it.  Doing more would race the     │
│       rollout.  This is the same trick `kubectl rollout restart` │
│       uses.                                                      │
│   other controller         → delete pod NAME; the owner replaces │
│   no controller at all     → refused, and said so: deleting a    │
│       pod nobody owns destroys it.  Restart the process yourself │
│       — with the venv on the claim, the restart is the relaunch. │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
                        actions printed, exit 0
```

## `hotfix status` — the point of the whole mode

Silently-diverged pods are the risk this mode creates, so `status` is cheap enough to
run habitually and exits non-zero when anything needs attention — usable as a
shutdown-checklist assertion.

```text
podbench hotfix status [-n NS] [--no-probe]
    │
    ▼
   get pods -o json                    ← ONE call for the whole namespace.
    │                                    The manifest travels in an annotation,
    │                                    so no exec is needed to read it.
    ▼
   for each pod carrying podbench.dev/hotfixed: "true"
    │
    ├─ read the manifest from the annotation
    ├─ read the container's current image + imageID from status
    │
    ├─ digest differs from the one recorded?
    │      yes → exec -c APP -- python3 -V      (unless --no-probe)
    │            probed only here, because this is the only case in
    │            which the interpreter can have broken
    │      no  → not probed
    ▼
   verdict, worst-first:

     interpreter   the venv's bin/python will not run, or its version
                   moved.  Worst, because the pod is not running what
                   anyone thinks it is
     superseded    consolidated onto a branch AND the image has changed
                   since: the claim is now shadowing the released fix
                   with an older copy of it
     image-changed the image was upgraded under a live hotfix mount, so
                   the upgrade has not reached the running code
     unreadable    marked hotfixed, manifest unreadable — provenance lost
     active        hotfixed, base image unchanged, N commits ahead
    │
    ▼
   exit 0 if every row is `active`, else exit 1
```

## `hotfix consolidate` — get it back into an image

```text
podbench hotfix consolidate TARGET --branch fix/thing --venv /opt/venv [--dry-run]
    │
    ▼
   read the manifest; git -C SRC log <base>..HEAD
    │
    ├─ not ahead ───────────────────────────────▶ exit 2
    ▼        ("if the fix is already in the image, retire the claim instead")
   git -C SRC push origin HEAD:refs/heads/<branch>
    │
    ▼
   record consolidated_branch in the manifest, and re-annotate
    │        → from now on `status` can call a stale claim `superseded`
    ▼        rather than leaving it a mystery
   print the retirement checklist:
     1. gh pr create --head <branch> …
     2. merge; let CI build and publish the image
     3. roll the workload onto it and confirm it is healthy
     4. remove the volume/volumeMount from the application's values
     5. set hotfixVenv.enabled=false and delete the claim
```

The PR is not opened here: that needs a forge client podbench does not depend on, and
a printed `gh pr create` is one paste. Until step 5 the claim keeps shadowing the
image's venv.

## Every cluster call, in order

```text
  init / apply / consolidate:
   1  kubectl config view --minify -o jsonpath={..namespace}   # only without -n
   2  kubectl -n NS get deployment NAME -o json     ┐ or get pod NAME -o json,
   3  kubectl -n NS get pods -l <matchLabels> -o json│ then get replicaset / get
   4  kubectl -n NS get pod POD -o json             ┘ deployment walking upwards
                                                     (+ the seat lookup)
   5  kubectl -n NS exec -c SEAT POD -- <git / cat / test / sh -c 'cat > …'>
                                                     # every claim read and write
   6  kubectl -n NS exec -c APP  POD -- /opt/venv/bin/python -m pip install …
                                                     # init, and apply when
                                                     # packaging metadata moved
   7  kubectl -n NS patch deployment NAME --type=merge \
          -p '{"spec":{"template":{"metadata":{"annotations":{…}}}}}'
   8  kubectl -n NS delete pod POD                   # apply, non-Deployment owners

  status:
   1  kubectl -n NS get pods -o json
   2  kubectl -n NS exec -c APP POD -- python3 -V    # only for a changed digest
```

RBAC — `rbac.hotfix` in the chart, on top of `rbac.observe` — is `get` on
`deployments`, `statefulsets` and `replicasets`, `patch` on `deployments` and
`statefulsets`, and `patch`/`delete` on `pods`. That `patch` on a workload is the most
privileged thing podbench asks for anywhere: the annotation write *is* the rollout, so
the verb deploys code.

## Three things the PVC does not fix

1. **The claim's venv shadows the image's.** An image upgrade under a live hotfix
   mount keeps running the old venv, and an interpreter bump breaks it outright — the
   `bin/python` symlink on the volume points at a path *in the image*. Hence the
   recorded interpreter, and hence `status` measuring the live one.
2. **Single replica only.** RWO, one checkout, one venv; an `apply` on either of two
   pods is a race with no winner.
3. **A stale claim silently reverts provenance.** After consolidation and a rebuild, a
   claim left mounted keeps the *old* code running under a version string that claims
   to contain the fix. That is what `superseded` exists to name.

And one known gap: under a GitOps controller with self-heal, the pod-template
annotations are drift and get stripped — so the fix keeps running while `status` stops
being able to see it
([#32](https://github.com/gilesknap/podbench/issues/32)).

## See also

* [Glossary](../reference/glossary.md) — PSA, Yama, the ambient set, `subPath` and every
  other term used here without explanation.
* [Ways in](ways-in.md) — why you would reach for this rather than `attach` or `dev`.
* [Architecture](architecture.md) — the mount-namespace rule this mode dissolves.
* [What `attach` does](attach-flow.md) — the seat this mode reaches the claim through.
