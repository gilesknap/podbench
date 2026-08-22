# What `hotfix` does

Hotfix mode makes an edit **outlive the session**, and does it without restarting the
container. The application's project lives on a PersistentVolumeClaim mounted *beside*
the image's own copy, and a small supervisor in the deployment's values relaunches the
application in place.

Not restarting is the point rather than a nicety. A podbench seat is an ephemeral
container sharing the target container's namespaces, so a container restart does not
orphan it — it SIGKILLs it, `exitCode: 137`
([#161](https://github.com/gilesknap/podbench/issues/161)), taking any attached editor
and debugger with it. Measured on a Diamond beamline, a restart also costs the
kubelet's CrashLoopBackOff ladder: 15s, then 23s, then 45s. The in-place relaunch is
~6.8s and costs the seat nothing ([S7](spikes/s7.md)).

It is the one mode that needs deploy-time cooperation. It is Python-only and
single-replica-only.

Four verbs: `init`, `apply`, `status`, `consolidate` — plus `--print-values`, which
emits the chart snippet the whole thing depends on.

## The layout it requires

The claim mounts **beside** the application's project, never over it.

```text
  ┌─────────────────────────────── pod ────────────────────────────────┐
  │                                                                    │
  │  ┌──────────────────────┐         ┌──────────────────────────────┐ │
  │  │ application          │         │ podbench seat                │ │
  │  │                      │         │  (ephemeral, from `attach`)  │ │
  │  │  PID 1 = supervisor  │         │  has git, a shell, podbench  │ │
  │  │    └ child = the app │         │                              │ │
  │  │                      │         │  reads the target's own      │ │
  │  │  /app      ← image's │         │  filesystem through          │ │
  │  │    .venv     project │         │  /proc/1/root, which is what │ │
  │  │              (never  │         │  the seed copies from        │ │
  │  │              hidden) │         │                              │ │
  │  │                      │         │  /podbench/app               │ │
  │  │  /podbench/app       │         │    the SAME mountPath, so    │ │
  │  │    src/    ← checkout│         │    the checkout resolves     │ │
  │  │    .venv   ← rebuilt │         │    identically on both sides │ │
  │  │    .python ← the     │         │                              │ │
  │  │              interpreter       │                              │ │
  │  │    .podbench-hotfix. │         │                              │ │
  │  │      json  ← manifest│         │                              │ │
  │  └───────────┬──────────┘         └──────────────┬───────────────┘ │
  │              │                                   │                 │
  │              └─────────────────┬─────────────────┘                 │
  │                                │                                   │
  │        PVC  <app>-podbench-project  (ReadWriteOnce)                │
  │                                                                    │
  └────────────────────────────────────────────────────────────────────┘
```

`podbench hotfix --print-values --app NAME --entrypoint 'CMD'` emits five keys, and
every one is a passthrough an application's chart already has:

```text
  volumes            the claim, plus podbench-home for the seat
  volumeMounts       the claim at /podbench/app — beside, never over
  command / args     the supervisor, wrapping the entrypoint you named
  livenessProbe      your existing exec probe, wrapped to honour the hold
                     (emitted only with --liveness; omit it if the target
                      declares no probe — 7 of 18 containers on a real
                      beamline do, and the canonical target is not one)
  podSecurityContext fsGroup, without which the claim is present and unwritable
```

That five-key list is the practical payoff of mounting beside. The earlier design
needed a seeding initContainer at a staging path, and `ioc-instance` — the chart every
EPICS IOC at Diamond is deployed with — cannot express one, because every initContainer
there inherits the main container's `volumeMounts`. `tests/test_ioc_instance_contract.py`
renders that chart at the pinned version and asserts all five arrive.

### The supervisor

```text
  while :; do
    (
      if [ -x /podbench/app/.venv/bin/python ]; then
        export PATH="/podbench/app/.venv/bin:$PATH"      ← the runtime switch
      fi
      exec <your entrypoint>
    ) &
    child=$!
    echo $child > /tmp/podbench-child.pid                ← what `apply` kills
    wait $child; rc=$?
    kill -TERM -"$child" 2>/dev/null || true             ← reap the strays
    [ -e /tmp/podbench-hold ] || exit $rc                ← fail-fast by default
  done
```

Three things about it, each of which failed silently before it was measured:

* **The runtime switch is inside the loop.** Evaluated once at container start it can
  never see a claim seeded afterwards, so the first `apply` after an `init` would
  relaunch the *image's* code and report success — new pids, `restartCount` 0, and the
  old binary still serving.
* **With no hold file it is fail-fast.** It exits with the child's status and the
  kubelet restarts exactly as it does today, so a deployment carrying this behaves
  identically to one that does not until somebody deliberately holds the pod.
* **It has no backoff.** The hold does not merely avoid the kubelet's ladder, it
  replaces it with none at all, so a hold left standing over a child that cannot start
  is a spin. That is why the hold carries an absolute deadline.

Every verb reaches the claim through a `HotfixStore`:

```text
   default        PodStore   → kubectl exec -c <seat> POD -- <cmd>
                              the seat is the container with git and a shell;
                              the application container is assumed to have neither
                              and in this mode is quite likely distroless
   --local        LocalStore → plain filesystem calls, for running the verb from
                              inside the seat's own terminal
```

## `hotfix init` — seed the claim

```text
podbench hotfix init TARGET --repo URL [--ref REF] [--base-commit SHA]
                            [--no-install] [--seat NAME]
    │
    ▼
┌──────────────────────────────────────────────────────────────────┐
│ RESOLVE THE TARGET   pod/NAME, deployment/NAME or statefulset/…  │
│                                                                  │
│   The walk up to the owning workload is read-only now, and its   │
│   only job is the multi-replica refusal: RWO claim, one          │
│   checkout, and two writers is a race with no winner.            │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ replicas != 1 ──────────────▶ exit 2
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ FIND THE SEAT     get pod POD -o json → the running podbench-N   │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ none, and no --seat ────────▶ exit 2
                                  ▼         ("run `podbench attach POD` first")
┌──────────────────────────────────────────────────────────────────┐
│ REQUIRE THE SUPERVISOR                                           │
│   exec -c APP -- test -e /tmp/podbench-child.pid                 │
│                                                                  │
│   Checked against the *live container*, not the pod spec: a spec │
│   can carry the supervisor while the container runs something    │
│   else — an image whose ENTRYPOINT wins, values that never       │
│   reached this pod. A refusal, not a warning: with nothing to    │
│   relaunch, `apply` would kill the application, the kubelet      │
│   would restart the container, and your seat would go with it.   │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ no supervisor ──────────────▶ exit 2
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ SEED THE CLAIM — from the container that is already running      │
│   already seeded (pyproject.toml present) → left alone           │
│   otherwise:                                                     │
│     cp -a /proc/1/root/app/*    /podbench/app/                   │
│     cp -a /proc/1/root/python   /podbench/app/.python            │
│                                                                  │
│   Through PID 1's root, and NOT from the seat's own /app: the    │
│   seat is a different image and its venv is podbench's, not the  │
│   application's. If /proc/1/root is not traversable this is      │
│   refused, naming the ptrace rung — never quietly substituted.   │
│                                                                  │
│   The *entries*, never the mount root: `cp -a` onto the claim's  │
│   own directory fails with "preserving times for '.': Operation  │
│   not permitted", after copying everything, which reads as an    │
│   inexplicable failure at the end of a long copy.                │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ no ptrace / no claim ───────▶ exit 2
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ PUT THE SOURCE ON THE CLAIM      the claim *is* the checkout     │
│   .git present (the image usually ships one) → left alone        │
│   absent → git clone [--branch REF] URL /podbench/app            │
│   git -C … rev-parse HEAD    → the base commit (or --base-commit)│
│                                                                  │
│   Every git call names the checkout safe: the seed is a copy, so │
│   the files carry the image's ownership and git refuses the      │
│   repository outright — "detected dubious ownership".            │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ REBUILD THE VENV — in the APPLICATION container, not the seat    │
│   exec -c APP -- env UV_CACHE_DIR=/podbench/app/.uv-cache \      │
│        uv sync --project /podbench/app \                         │
│                --python /podbench/app/.python/cpython-…/bin/python3 \
│                --frozen                                          │
│                                                                  │
│   A rebuild and not a copy. A venv records absolute paths, so a  │
│   copied one names an interpreter that is no longer where it     │
│   says — and the symptom is a console script whose shebang       │
│   points back into the image, which keeps working until the      │
│   restart that was the whole point.                              │
│                                                                  │
│   --python is the interpreter *binary*, discovered rather than   │
│   assumed: uv nests its installs one level down, so the obvious  │
│   <root>/bin/python3 does not exist.                             │
│   UV_CACHE_DIR is explicit because uv otherwise wants $HOME and  │
│   falls back to /.cache/uv, which is a permission error in a pod │
│   whose HOME is unset.                                           │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ rebuild fails ──────────────▶ exit 2
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ RECORD THE PROVENANCE — on the claim, and only there             │
│   write /podbench/app/.podbench-hotfix.json                      │
│     checkout, repo, base image + digest, interpreter, container, │
│     commit, base_commit, author, timestamp                       │
│                                                                  │
│   Not on the workload's pod template. A GitOps controller        │
│   reconciles a volume towards its spec but strips an annotation  │
│   as drift, so provenance written there went quiet within one    │
│   sync interval — with the fix still running.                    │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
                        actions printed, exit 0
```

## `hotfix apply` — commit, rebuild if needed, relaunch

```text
podbench hotfix apply TARGET -m "message" [--no-bounce]
    │
    ▼
   resolve the target and the seat (exactly as init does)
    │
    ▼
   cat /podbench/app/.podbench-hotfix.json
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
       rerun the venv rebuild          "editable install still valid"
       in the application container    (an editable install is a path
                    │                   redirection: new code is free,
                    │                   a new entry point is not)
                    └───────────┬─────────────┘
                                ▼
   write the manifest back      (commit, author, timestamp, ahead, commits[:20])
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ RELAUNCH  (unless --no-bounce) — one exec, three steps           │
│                                                                  │
│   1  date -u -d "+120 seconds" +%s > /tmp/podbench-hold          │
│      trap 'rm -f /tmp/podbench-hold' EXIT                        │
│   2  collect the child's descendant TREE through ppid links,     │
│      signal deepest-first, SIGKILL any straggler                 │
│   3  wait for the pid file to change; the trap releases the hold │
│                                                                  │
│   One exec and not three: a seat that died between them would    │
│   leave the pod held — probe short-circuited, supervisor         │
│   spinning without backoff. The deadline in the file is the belt │
│   to the trap's brace.                                           │
│                                                                  │
│   The TREE and not the pid. A target that allocates a pty —      │
│   `stdio-socket --ptty`, i.e. every epics-containers IOC — puts  │
│   its real process in its own session, where it survives a       │
│   signal aimed at the pid *or its process group* and is          │
│   reparented onto PID 1 still holding the port. The relaunch     │
│   then comes up deaf and the pod serves the old code with        │
│   restartCount still 0. Verify by asking whether the port        │
│   changed owner; "the pid file moved" and "the port answers"     │
│   both pass while broken.                                        │
│                                                                  │
│   No pod is deleted and no workload is patched. Ownership stops  │
│   mattering, which is what lets this work on a bare pod.         │
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
   get pods -o json                    ← ONE call for the whole namespace
    │
    ▼
   for each pod with a container mounting /podbench/app
    │                    ↑
    │                    the filter is the CLAIM. It used to be a
    │                    podbench.dev/hotfixed annotation on the pod
    │                    template, which Argo self-heal strips — so a
    │                    hotfixed pod under GitOps went quiet within one
    │                    sync interval, which is the precise failure this
    │                    command exists to prevent. A controller
    │                    reconciles a volume towards its spec; it does
    │                    not remove it.
    │
    ├─ ONE exec per candidate pod, returning three things at once:
    │      cat /podbench/app/.podbench-hotfix.json   ← the manifest
    │      cat /tmp/podbench-hold                    ← the hold, if any
    │      date -u +%s                               ← the pod's clock
    │
    │   An exec per candidate rather than none at all is the price of
    │   not being strippable. It is affordable because the claim is the
    │   filter: only pods that mount one are exec'd.
    │
    ├─ neither a manifest nor a hold → not listed
    │      (a deployed-but-unused claim is not a hotfix)
    │
    ├─ read the container's current image + imageID from status
    │
    ├─ digest differs from the one recorded?
    │      yes → exec -c APP -- python3 -V      (unless --no-probe)
    │      no  → not probed
    ▼
   verdict, worst-first:

     interpreter   the venv's bin/python will not run, or its version
                   moved
     superseded    consolidated onto a branch AND the image has changed
                   since: the claim is now shadowing the released fix
                   with an older copy of it
     image-changed the image was upgraded under a live hotfix, so the
                   upgrade has not reached the running code
     unreadable    a manifest is present on the claim and will not parse
     not-hotfixed  held, but nothing hotfixed here
     active        hotfixed, base image unchanged, N commits ahead
    │
    ▼
   plus a `held` column, orthogonal to all of the above:

     held 284s left            a hold with time still on it
     held EXPIRED              past its deadline; the supervisor has
                               stopped honouring it
     held expiry unmeasured    a hold file podbench could not parse —
                               unmeasured, never "no deadline"
    │
    ▼
   exit 0 only if every row is `active` AND unheld
```

The hold is a column and not a clause in the health sentence because they are
different questions and either can be true alone: a perfectly healthy hotfix can sit in
a pod nobody released, and a pod that was **never hotfixed at all** can be left held by
an `apply` that died mid-flight. That second case is real and nothing else will notice
it — its liveness probe is short-circuited and its supervisor is relaunching without
backoff — which is why a held pod is listed whether or not it carries a manifest, and
why the hold moves the exit code.

## `hotfix consolidate` — get it back into an image

```text
podbench hotfix consolidate TARGET --branch fix/thing [--dry-run]
    │
    ▼
   read the manifest; git -C SRC log <base>..HEAD
    │
    ├─ not ahead ───────────────────────────────▶ exit 2
    ▼        ("if the fix is already in the image, retire the claim instead")
   git -C SRC push origin HEAD:refs/heads/<branch>
    │
    ▼
   record consolidated_branch in the manifest (on the claim)
    │        → from now on `status` can call a stale claim `superseded`
    ▼        rather than leaving it a mystery
   print the retirement checklist:
     1. gh pr create --head <branch> …
     2. merge; let CI build and publish the image
     3. roll the workload onto it and confirm it is healthy
     4. remove the five values from the application's chart
     5. set hotfixProject.enabled=false and delete the claim
```

The PR is not opened here: that needs a forge client podbench does not depend on, and
a printed `gh pr create` is one paste. Until step 5 the claim keeps shadowing the
image's project — the runtime switch prefers a seeded claim, and it does not care that
the fix is now in the image too.

## Every cluster call, in order

```text
  init / apply / consolidate:
   1  kubectl config view --minify -o jsonpath={..namespace}   # only without -n
   2  kubectl -n NS get deployment NAME -o json     ┐ or get pod NAME -o json,
   3  kubectl -n NS get pods -l <matchLabels> -o json│ then get replicaset / get
   4  kubectl -n NS get pod POD -o json             ┘ deployment walking upwards
                                                     (+ the seat lookup)
   5  kubectl -n NS exec -c APP  POD -- test -e /tmp/podbench-child.pid
                                                     # init: require the supervisor
   6  kubectl -n NS exec -c SEAT POD -- <cp / git / cat / test / sh -c 'cat > …'>
                                                     # the seed, and every claim
                                                     # read and write
   7  kubectl -n NS exec -c APP  POD -- env UV_CACHE_DIR=… uv sync …
                                                     # init, and apply when
                                                     # packaging metadata moved
   8  kubectl -n NS exec -c APP  POD -- bash -c '<hold; kill the tree; release>'
                                                     # apply, unless --no-bounce

  status:
   1  kubectl -n NS get pods -o json
   2  kubectl -n NS exec -c APP POD -- sh -c 'cat manifest; cat hold; date'
                                                     # per candidate pod
   3  kubectl -n NS exec -c APP POD -- python3 -V    # only for a changed digest
```

Nothing patches a workload and nothing deletes a pod. `rbac.hotfix` — on top of
`rbac.observe` — is now **`get` on `deployments`, `statefulsets` and `replicasets`, and
nothing else**. It used to add `patch` on workloads and `patch`/`delete` on pods,
because the annotation write *was* the rollout and that verb therefore deployed code —
the most privileged thing podbench asked for anywhere. Moving the provenance onto the
claim and the relaunch inside the container removed the need for all of it, so in
cluster terms Hotfix mode is now barely more privileged than watching.

## Two things the claim does not fix

1. **The claim's project shadows the image's.** An image upgrade under a live hotfix
   keeps running the claim's code, so the upgrade does not reach what is executing.
   The manifest records the digest it was made against and `status` compares it, which
   is what turns a silent shadow into `image-changed`.
2. **Single replica only.** RWO, one checkout, one venv; an `apply` on either of two
   pods is a race with no winner.

There used to be a third — an interpreter bump breaking the venv, because `bin/python`
pointed at a path inside the image. That is now *fixed* rather than reported: the
interpreter is copied onto the claim and the venv is rebuilt against it, so the console
scripts name a path that survives a restart. `head -1 <venv>/bin/<script>` saying `/app`
is the check that it has not regressed.

The old known gap is closed too. Under a GitOps controller with self-heal the
pod-template annotations were drift and got stripped, so the fix kept running while
`status` stopped being able to see it
([#32](https://github.com/gilesknap/podbench/issues/32)). Keying the listing on the
claim is the fix for exactly that.

## See also

* [Glossary](../reference/glossary.md) — PSA, Yama, the ambient set, `subPath` and every
  other term used here without explanation.
* [Ways in](ways-in.md) — why you would reach for this rather than `attach` or `dev`.
* [Architecture](architecture.md) — the mount-namespace rule this mode dissolves.
* [What `attach` does](attach-flow.md) — the seat this mode reaches the claim through.
