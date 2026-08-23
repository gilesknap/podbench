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

Five verbs: `values`, `init`, `apply`, `status`, `consolidate`. `values` reads the
target pod and emits the chart snippet the whole thing depends on.

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

`/app` and `/python` in the application's half of that picture are the *defaults*
podbench assumes and not a requirement — see *The layout is the image's, not
podbench's* under `hotfix init` below, and the `--image-project` flag it describes.
What the mode genuinely requires is the claim, mounted beside.

`podbench hotfix values --app NAME --from-pod POD` emits five keys, and
every one is a passthrough an application's chart already has:

```text
  volumes            the claim, plus podbench-home for the seat
  volumeMounts       the claim at /podbench/app — beside, never over
  command / args     the supervisor, wrapping the entrypoint the pod runs today
  livenessProbe      the target's own exec probe, wrapped to honour the hold and
                     carrying its own timings (nothing is emitted where the
                     target declares no probe — 7 of 18 containers on a real
                     beamline do, and the canonical target is not one)
  podSecurityContext fsGroup, without which the claim is present and unwritable
```

That five-key list is the practical payoff of mounting beside. The earlier design
needed a seeding initContainer at a staging path, and `ioc-instance` — the chart every
EPICS IOC at Diamond is deployed with — cannot express one, because every initContainer
there inherits the main container's `volumeMounts`. `tests/test_ioc_instance_contract.py`
renders that chart at the pinned version and asserts all five arrive.

### `hotfix values` — read the target, emit the snippet

```text
podbench hotfix values --app NAME --from-pod POD [-n NS] [--container NAME]
                       [--entrypoint CMD] [--gid GID] [--context C]
                       [--claim-venv NAME]
    │
    ▼
┌──────────────────────────────────────────────────────────────────┐
│ READ THE TARGET       get pod POD -o json — one call, and the    │
│                       only one `hotfix values` makes             │
│                                                                  │
│   Reading is the default, and #176 is why: the entrypoint, the   │
│   probe and the gid used to be supplied by hand. A chart renders │
│   a supplied livenessProbe wholesale, so a timing left out       │
│   silently became the Kubernetes default and a compiled IOC went │
│   from 120s/30s to 0s/10s — probed from the moment it started,   │
│   before it had reached its hardware.                            │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ no --from-pod, no --no-from-pod ──▶ exit 2
                                  ├─ kubectl could not read it ────────▶ exit 2
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ TAKE THE THREE OFF THE CONTAINER  --container NAME, or the first │
│                                                                  │
│   entrypoint  command + args, shell-quoted back into the one     │
│               string the supervisor execs. A container already   │
│               carrying the layout is *unwrapped*, never wrapped  │
│               twice: a supervisor inside a supervisor would hold │
│               on a file the inner one never sees.                │
│   gid         runAsGroup, or the placeholder where the pod       │
│               states none. Never 0.                              │
│   probe       the whole livenessProbe, timings and all, where it │
│               is an exec one. No probe at all is not an error.   │
│                                                                  │
│   A flag you passed wins over the pod, every time. That is what  │
│   makes --entrypoint an answer to a target whose command lives   │
│   in the image's ENTRYPOINT — nowhere in the pod spec, so        │
│   nothing read from the cluster finds it — without giving up the │
│   other two as well.                                             │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ SAY WHAT THE OUTPUT CANNOT SAY FOR ITSELF        → stderr        │
│                                                                  │
│   a non-exec probe   emitted around, with a warning: an httpGet  │
│                      probe answers from the application, and the │
│                      application is what is down while a pod is  │
│                      held. An absent probe block otherwise looks │
│                      identical to the fastcs case, and the       │
│                      difference is whether a held pod survives.  │
│   the target's own   volumes: and volumeMounts: *replace* the    │
│   volumes            chart's keys rather than adding to them, so │
│                      they are named and the resolution is spelt  │
│                      out. From a live pod podbench cannot merge  │
│                      them itself: a chart-generated volume and   │
│                      one the service declared for itself are     │
│                      indistinguishable from there. --values is   │
│                      the way out - see below.                    │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
                                  the five keys, needing no hand-editing
```

`--values PATH` is the other read, and it closes the one thing `--from-pod`
cannot. A pod cannot say which of its volumes the service asked for; the
service's **values file** can, and says nothing else. Given it, podbench merges
its own keys in and emits the file whole:

```text
  ../values.yaml   ─┐
  (--parent-values) │   a helm list REPLACES across the parent/child merge.
                    │   A service declaring volumes: for the first time takes
                    │   the shared one over completely, so the shared entries
                    │   are absorbed first or a beamline directory is silently
                    ├─► unmounted. Without this file podbench says so rather
                    │   than assuming there is nothing to inherit.
                    │
  values.yaml      ─┤   what this service declares, and nothing else. Its own
  (--values)        │   entries win; its comments survive; matching is by
                    │   `name`, so re-running changes nothing.
                    │
  values_snippet   ─┘   the claim's key, at the root, and the five
                        passthroughs wherever the chart keeps them.
                        --values-under names that; otherwise it is read.
                                  │
                                  ▼
                        the whole file, on stdout. Every note on stderr, so
                        stdout can be redirected straight over the input.
```

`--no-from-pod` is the escape, for CI, an offline machine, or a pod that does not
exist yet; it emits from `--entrypoint`, `--gid`, `--liveness` and
`--liveness-probe` alone. It is also the exact route that produced #176, so every
failure reading the pod names it *and* what taking it costs. kubectl tells a
missing kubeconfig, an absent pod and a forbidden `get pods` apart only in the
text of its own message, so podbench relays that verbatim rather than guessing at
a category and getting it wrong.

`values_snippet` itself takes no cluster **and no file**: `--from-pod` and
`--values` are both thin reading wrappers, and `merged_values` — which does the
whole of the merge — takes two strings and returns one. Tests assert both
functions acquire neither dependency. That is what lets every shape of the
output be asserted without a cluster or a filesystem, and it is why the one
thing this mode cannot get wrong from a desk is checked from one.

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
  old binary still serving. The `.venv` it looks for is `--claim-venv`'s default, and
  the same value has to reach `init`: a switch looking for one directory and a rebuild
  landing in another is the same silent failure by another route.
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
podbench hotfix init TARGET [--repo URL] [--ref REF] [--base-commit SHA]
                            [--no-install] [--seat NAME] [--venv PATH]
                            [--image-project PATH] [--image-interpreter PATH]
                            [--claim-venv NAME]
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
│ FIND THE SEAT     get pod POD -o json → the running podbench-N,  │
│                    or --seat NAME for one called something else  │
│                                                                  │
│   None running? `init` lands one itself, through the same        │
│   `attach` you would have typed — which mounts the claim on a    │
│   pod carrying the layout, so the seat it lands is one this      │
│   mode can use. The verbs after `init` still refuse: by then     │
│   a missing seat means it died or the pod was replaced, and      │
│   that is a thing to be told about rather than to paper over     │
│   by spending another ephemeral container name.                  │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
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
│   application's — never quietly substituted.                     │
│                                                                  │
│   Where the project is not there, the root question is asked     │
│   directly rather than inferred from it (#178):                  │
│                                                                  │
│     ls /proc/1/root/                                             │
│       fails → the seat cannot see the target's root at all, so   │
│               the seed cannot run. That is the ptrace rung, and  │
│               the refusal names CAP_SYS_PTRACE and `podbench     │
│               doctor`.                                           │
│       lists → the root reads fine and this image simply keeps no │
│               project at /app. A layout difference and not a     │
│               permission one, so the refusal names               │
│               --image-project and --image-interpreter — and      │
│               names neither ptrace nor doctor.                   │
│                                                                  │
│   `ls` and not `test -e`: test -e follows the /proc/1/root       │
│   symlink and answers for the target of the link, so it says yes │
│   on precisely the seat that cannot traverse it.                 │
│                                                                  │
│   The *entries*, never the mount root: `cp -a` onto the claim's  │
│   own directory fails with "preserving times for '.': Operation  │
│   not permitted", after copying everything, which reads as an    │
│   inexplicable failure at the end of a long copy.                │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ root unreadable ────────────▶ exit 2
                                  ├─ no project / no claim ──────▶ exit 2
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ PUT THE SOURCE ON THE CLAIM      the claim *is* the checkout     │
│   .git present (the image usually ships one) → left alone        │
│   absent → git clone [--branch REF] URL /podbench/app            │
│           URL is --repo, or the image's …image.source label      │
│                                                                  │
│ THE BASE COMMIT, in descending order of confidence:              │
│   --base-commit SHA                              → measured      │
│   the image's …image.revision — only where the checkout's own    │
│     origin says the labels are this repository's, and the clone  │
│     has the commit                               → measured      │
│   git -C … rev-parse HEAD                        → ASSUMED       │
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

### The layout is the image's, not podbench's

`/app` and `/python` are
[python-copier-template](https://github.com/DiamondLightSource/python-copier-template)'s
convention and the defaults of `--image-project` and `--image-interpreter`; they are
not a requirement. An epics-containers image has no `/app` at all — its venv is at
`/venv`, with a separate `/python` — which is how the seed came to report a layout
difference as a ptrace denial. Measured on `bl47p-mo-ioc-01` on 2026-08-22:
`/proc/1/root` listed cleanly from that seat and `/app` did not exist, so podbench
blamed the rung and sent the user to `podbench doctor`, which correctly reported the
rung healthy ([#178](https://github.com/gilesknap/podbench/issues/178)). A
contradiction with no next step, and the reason the two failures are now asked about
separately.

The refusal prints the path **inside the image** rather than the `/proc/1/root/...`
form podbench reads it through, because `--image-project` takes the former and an
error should print what you would type. It names neither *ptrace* nor *doctor*, and
not merely because neither is the cause: both are the false trail the old message
opened, and a message that mentions a mechanism at all — even to rule it out — is one
the reader will go and chase.

This makes the paths *expressible*; it does not make a compiled IOC hotfixable. That
process is a compiled binary, and whether the thing to fix is a Python support module
installed into its venv or the ibek `ioc.yaml` beside it is a design question that
stays on [#34](https://github.com/gilesknap/podbench/issues/34).

`--claim-venv` is the third of the set and belongs to the *claim* rather than the
image: it is the venv directory the runtime switch in the supervisor above looks
for and the one `uv sync` builds, so both ends have to agree. `init` sets
`UV_PROJECT_ENVIRONMENT` whenever it is not uv's own `.venv`, because a rebuild that
landed beside the venv the supervisor is looking for would leave the pod quietly
running the image's code — the one failure this whole mode exists to avoid. Passing it
to `init` means passing the same value to `hotfix values`.

`init` records it in the manifest, and `apply` reads it from there rather than
taking a flag of its own
([#209](https://github.com/gilesknap/podbench/issues/209)). Before it did, a
packaging change under `init --claim-venv env` was rebuilt into `.venv` while the
switch went on looking in `env` — the same silent revert, arrived at from the one
direction the flag existed to close.

### Two values the flags no longer ask for

`--venv` and `--base-commit` were required and were both things something else
already knew ([#205](https://github.com/gilesknap/podbench/issues/205) items 1
and 2).

`--venv` is the mountPath of the claim, which is on the pod: the volume is
podbench's own `podbench-app`, so its `mountPath` in the application container is
the answer, and it is read rather than asked for. A value that disagrees with the
pod is **refused** — `hotfix status` finds a hotfixed pod by scanning for a
`mountPath` of `/podbench/app`, so any other value used to write a manifest
`status` could never see, and a hotfixed pod invisible to `status` is the precise
failure this mode exists to prevent. A claim genuinely mounted elsewhere is still
honoured by the flag; the warning says out loud both that `status` will not list
it and that `init` will refuse it, because the seed, the copied interpreter and
the supervisor's runtime switch all name `/podbench/app`.

`--base-commit` is the number every drift figure is a difference against, and its
old default was `git rev-parse HEAD` of the fresh clone — without `--ref`, the
default branch's tip, which is almost never what the released image was built
from. The image states it: podbench reads `org.opencontainers.image.revision` and
`org.opencontainers.image.source` off the target image, over the registry API and
with no credentials, and uses them to default `--base-commit` and `--repo`. A
label naming a commit the clone does not contain is not believed — `--repo` may
be a fork or a mirror, and `git log base..HEAD` would fail later, which is a
worse place to find out.

**Labels are corroborated before they are believed, because OCI labels are
inherited.** A derived image carries its base image's `org.opencontainers.image.*`
unless the build overrides them, and many builds do not: measured 2026-08-23,
`ghcr.io/diamondlightsource/fastcs-example-debug:2025.10.1` advertises
`source=…/ubuntu-devcontainer` and a revision in that repository, while the IOC's
own source is `…/fastcs-example`. Checking the revision against the clone cannot
catch this, because a clone made from the label's own repository contains the
label's own revision. So podbench asks the seeded checkout's `origin` — the one
naming of the repository that did not come out of the image. Where `origin`
disagrees it wins, and the base is recorded as assumed.

Where nothing corroborates them — no labels, a registry that wants credentials,
or no checkout in the image to ask — the base is recorded as **assumed** and `status` says
`+N commit(s) from an assumed base`. That is the point of the item rather than a
fallback from it: a derived count printed as though it were measured is worse
than one that admits what it stands on.

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
    │                    not remove it. Nothing reads that annotation
    │                    any more either: `is_hotfixed` was left reading
    │                    a key nothing wrote, so it answered False on
    │                    every pod podbench ever hotfixed. It now reads
    │                    the same two things from the pod spec — the
    │                    claim volume, and a container whose args are
    │                    the supervisor loop — which is the filter above,
    │                    applied everywhere else that signal is needed
    │                    (#177).
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
     5. turn the claim's boolean off and delete the claim
```

The PR is not opened here: that needs a forge client podbench does not depend on, and
a printed `gh pr create` is one paste. Until step 5 the claim keeps shadowing the
image's project — the runtime switch prefers a seeded claim, and it does not care that
the fix is now in the image too.

Step 5 is two actions and not one, deliberately. The claim carries both
`helm.sh/resource-policy: keep` and
`argocd.argoproj.io/sync-options: Prune=false,Delete=false`, so turning the
boolean off takes the claim out of the desired state and leaves the object
standing. That is the point — a hotfix has to survive somebody reverting a
repoint mid-beamtime — and it means the deletion is a separate, deliberate act.
Measured: a claim carrying Helm's annotation *alone* is pruned about three
minutes after it leaves the desired state, and a `Delete`-reclaim PV goes with
it (issue #190).

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

  values:
   1  kubectl config view --minify -o jsonpath={..namespace}   # only without -n
   2  kubectl -n NS get pod POD -o json                # nothing else, and
                                                       # nothing at all under
                                                       # --no-from-pod
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
