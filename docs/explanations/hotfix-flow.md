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

Six verbs: `values`, `check`, `init`, `restart`, `status`, `retire`.
`values` reads the target pod and emits the chart snippet the whole thing depends on;
`check` says whether the deployed result is one `init` can work on; `retire` says how
far the fix has got back out again.

**Committing and pushing are ordinary git in the seat.** There were two more verbs,
`apply` and `consolidate`, and each was git plus one field written into the manifest.
Both are gone ([#232](https://github.com/gilesknap/podbench/issues/232)): the seat has
a working git since 0.9.0, and a manifest that records what git did is a record one
hand commit or one hand push makes false without anything noticing. What is left is
the work git cannot do from in there — relaunching the application, and measuring
where the claim has got to.

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
  │  │  /podbench           │         │    the SAME mountPath, so    │ │
  │  │    app/    ← checkout│         │    the checkout resolves     │ │
  │  │    venv/   ← rebuilt │         │    identically on both sides │ │
  │  │    python/ ← the     │         │                              │ │
  │  │              interpreter       │                              │ │
  │  │    uv-cache/         │         │                              │ │
  │  │    home/   ← $HOME   │         │                              │ │
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

`home/` is the **seat's**, and it is on the claim for the reason nothing else here is
in the checkout: `podbench-home` is an `emptyDir`, so it dies with the pod, and
everything in it — `~/.vscode-server` at 1.1–1.3 GB — counts against the pod's
ephemeral-storage budget, whose overrun evicts the *pod*, application included. On the
claim, a seat re-attached after a pod replacement finds the server already unpacked
rather than downloading it again. One directory per seat *user* — `home/root`,
`home/podbench` — and never one shared home: two seats at different uids would share
the directory and not its ownership, which is what `podbench-home` already did.

Which mechanism puts it there depends on whether the seat is root, and that split is
the part worth getting right. A non-root seat is simply **told**: the passwd record
podbench writes for its uid names the claim. A root seat cannot be told — sshd takes a
session's `$HOME` from the passwd record, root's record says `/root`, and
`libnss-extrausers` ignores every uid and gid below 500, so no record for uid 0 can be
written at all. So `/root` is made a **symlink** onto the claim at seat start-up,
idempotently, with the image's own dotfiles copied across on the first run and anything
already on the claim winning ([#42](https://github.com/gilesknap/podbench/issues/42)).
Both ends go on naming `/root`, which is the point: moving the launcher's half alone
would leave a ProxyCommand naming sshd's config in a home that is not there. Either
mechanism checks the claim is really *mounted* first, because a seat can carry the
mountPath in its environment and not the mount — a `subPath` refusal is degraded to a
note — and a home created anyway would land on the container layer under a path saying
it was on the claim, which is the one thing the move was for.

`podbench-home` stays: it is what an `attach` seat on a pod with no claim uses, and the
claim wins where both exist without displacing the volume.

`podbench hotfix values --app NAME --from-pod POD` emits five keys, and
every one is a passthrough an application's chart already has:

```text
  volumes            the claim, plus podbench-home for the seat
  volumeMounts       the claim at /podbench — beside, never over
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
│   Reading is the only way, and #176 is why: the entrypoint, the  │
│   probe and the gid used to be supplied by hand. A chart renders │
│   a supplied livenessProbe wholesale, so a timing left out       │
│   silently became the Kubernetes default and a compiled IOC went │
│   from 120s/30s to 0s/10s — probed from the moment it started,   │
│   before it had reached its hardware.                            │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ no --from-pod ────────────────────▶ exit 2
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
                        stdout is exactly the file - redirect it to a new
                        name and move that over the input. Not `> input`:
                        a shell truncates a redirect target before podbench
                        starts, so the merge reads an empty file and the
                        output silently loses everything the input had.
```

There is no escape from the read. `--no-from-pod` emitted from `--entrypoint`,
`--gid`, `--liveness` and `--liveness-probe` alone; both it and `--liveness` were
removed in #205 item 6, with no alias and no deprecation, because it was the
exact route that produced #176 and its offline emission was strictly the
lower-fidelity one. What is left of stating a value by hand is stating **one** of
them on top of the read, where the pod cannot answer for it: `--entrypoint` for a
target whose command lives in the image's `ENTRYPOINT`, `--gid`, and
`--liveness-probe`, which unlike `--liveness` carries a whole probe's timings.

So every failure reading the pod says why the read is not optional — quoting
#176, because that is now the reason there is nothing to fall back to rather than
the price of falling back — and names what will get the reader going. kubectl
tells a missing kubeconfig, an absent pod and a forbidden `get pods` apart only
in the text of its own message, so podbench relays that verbatim rather than
guessing at a category and getting it wrong.

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
      if [ -x /podbench/venv/bin/python ]; then
        export PATH="/podbench/venv/bin:$PATH"          ← the runtime switch
      fi
      exec <your entrypoint>
    ) &
    child=$!
    echo $child > /tmp/podbench-child.pid                ← what `restart` kills
    wait $child; rc=$?
    kill -TERM -"$child" 2>/dev/null || true             ← reap the strays
    [ -e /tmp/podbench-hold ] || exit $rc                ← fail-fast by default
  done
```

Three things about it, each of which failed silently before it was measured:

* **The runtime switch is inside the loop.** Evaluated once at container start it can
  never see a claim seeded afterwards, so the first `restart` after an `init` would
  relaunch the *image's* code and report success — new pids, `restartCount` 0, and the
  old binary still serving. The `venv` it looks for is `--claim-venv`'s default, and
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

## `hotfix check` — every prerequisite at once

```text
podbench hotfix check TARGET [-n NS] [--container NAME] [--seat NAME]
                             [--repo URL]
                             [--image-project PATH] [--image-interpreter PATH]
```

Every prerequisite this mode has used to be discovered serially, and mostly at the
moment it bit: no supervisor, no claim, a second replica, a root the seat cannot read,
no project where podbench looked, a probe the hold cannot short-circuit. Each of those
is a chart change and a redeploy, in an emergency, discovered one per attempt
([#205](https://github.com/gilesknap/podbench/issues/205)). `check` asks all of them in
one read-only pass and gives the answer an exit code.

```text
  [ok]    target         bl47p-ea-fastcs-01-0, container bl47p-ea-fastcs-01
  [ok]    claim          … mounts podbench-app at /podbench
  [FAIL]  supervisor     container … is not running the podbench supervisor: …
  [warn]  seat           no podbench container is running in … Not a blocker …
  [warn]  target root    not measured: listing /proc/1/root is a property of …
  [ok]    project        the image keeps one at /app
  [ok]    interpreter    the image keeps one at /python
  [warn]  liveness       a httpGet livenessProbe cannot be short-circuited …
  [warn]  source         the image names https://github.com/…/ubuntu-devcontainer,
                         which its own repository … does not correspond to …
------------------------------------------------------------------------
VERDICT: 1 blocker before `podbench hotfix init` can work (exit 1)
BLOCKERS: supervisor
```

Nothing here is a new measurement: each row is the function that already enforces the
thing, asked early and caught rather than raised. Six properties of it are deliberate
and each has a failure mode behind it:

* **It is read-only, and it lands no seat.** `init` lands one when none is running,
  because that is its job; an ephemeral container cannot be taken back off a pod, so a
  verb somebody runs to *ask a question* must not spend one.
* **The project and the interpreter are asked in the application container** — `test -d`
  beside the supervisor's own probe — and not through the seat's `/proc/1/root`. It is a
  question about the *image's layout*, it needs no seat, and it is therefore answerable
  before the attach. `test` exiting 126 or 127 is `test` itself not running, which on a
  distroless container is a real possibility, and is reported as **not measured** rather
  than as a project that is absent.
* **A `warn` is not a blocker**, the way it is not in `doctor`. A non-exec
  `livenessProbe` is one: `init` accepts such a target and it is the relaunch's hold the
  kubelet will cut short, so it is a thing to deal with rather than a reason for this
  command to stop.
* **It asks `init`'s questions in `init`'s terms, which is why it takes `--repo`.**
  An image naming no source repository is a state `init` refuses outright, before it
  seeds anything, so this row is a blocker and not a note — a `check` that passed it
  would be sending the reader into precisely the second attempt the verb exists to
  remove. Hearing the flag is the other half of the same rule: without it the row
  would refuse a target `init --repo URL` accepts.
* **A source label is corroborated, never taken on trust.** OCI labels are inherited
  from the base image unless the build overrides them, and the IOC this mode was proved
  against does not override them: `fastcs-example-debug:2025.10.1` advertises
  `ubuntu-devcontainer`'s repository and revision, and that revision provably does not
  exist in `fastcs-example`. `check` used to print `[ok] the image names
  …/ubuntu-devcontainer` under "nothing measured here blocks `hotfix init`" — a
  pre-flight *more* confident than the verb it speaks for, since `init` with no
  `--repo` clones that repository and records an `ASSUMED` base. The corroborator it
  can always take is the image's own registry path, which is not in the config blob and
  so cannot have been inherited; a `-debug` or `-runtime` variant of the named
  repository counts as corresponding. What it cannot take on an unseeded claim is the
  checkout's `origin` — there is no checkout — so the row says which corroboration it
  had, and a label nothing corroborates is a `warn` and not a blocker. It settles the
  **repository** and nothing else: the suffix is tolerated in one direction only, an
  image named *after its base* corresponds to the base's own inherited label, and
  `init`'s revision label stays gated on `corroborate_source`, which deliberately does
  not take this naming. So the row says what corresponds, never that the labels are
  this image's own.
* **A claim that is already seeded retires three rows.** `init` short-circuits its
  whole seed on `{checkout}/pyproject.toml`, which makes the target root, the project
  and the interpreter moot — so `check` does not ask them either. This is the state a
  second run is in: after a fix, on a pod already hotfixed, whose seat may well be a
  degraded one that cannot list `/proc/1/root`. Measuring those rows anyway is how
  `check` came to fail a target `init` accepts.
* **`--seat NAME` is corroborated against the pod, not restated.** A name nobody
  landed was reported `[ok] … is running`, and the row it then broke was `target
  root` — which sent the reader to CAP_SYS_PTRACE and `doctor` for a typo:
  [#178](https://github.com/gilesknap/podbench/issues/178)'s false trail, by a new
  route.
* **What could not be measured says so.** With no seat running there is nothing to
  measure the ptrace rung *with*: whether a seat will be able to list `/proc/1/root` is
  a property of a container that does not exist yet. That row reads `not measured`, and
  the verdict says "nothing **measured here** blocks" rather than claiming more than was
  asked.

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
│   relaunch, a restart would kill the application, the kubelet    │
│   would restart the container, and your seat would go with it.   │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ no supervisor ──────────────▶ exit 2
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ SEED THE CLAIM — from the container that is already running      │
│   already seeded (pyproject.toml present) → left alone           │
│   otherwise:                                                     │
│     cp -a /proc/1/root/app/*    /podbench/app/                   │
│     cp -a /proc/1/root/python   /podbench/python                 │
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
│ PUT THE SOURCE ON THE CLAIM      the checkout is /podbench/app   │
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
│   exec -c APP -- env UV_CACHE_DIR=/podbench/uv-cache \           │
│                     UV_PROJECT_ENVIRONMENT=/podbench/venv \      │
│        uv sync --project /podbench/app \                         │
│                --python /podbench/python/cpython-…/bin/python3 \ │
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
│   whose HOME is unset. UV_PROJECT_ENVIRONMENT is unconditional   │
│   now that the venv is never at uv's own default. Both name the  │
│   claim and not the checkout, which is what keeps uv from        │
│   writing into the source tree.                                  │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ rebuild fails ──────────────▶ exit 2
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ RECORD THE PROVENANCE — on the claim, and only there             │
│   write /podbench/.podbench-hotfix.json                          │
│     checkout, repo, base image + digest, interpreter, container, │
│     base_commit (+ whether it was assumed), claim venv           │
│                                                                  │
│   At the claim's root and not in the checkout, so podbench's     │
│   own provenance file is not an untracked file in somebody's     │
│   working tree.                                                  │
│                                                                  │
│   Only what git cannot answer. No commit, no count, no author,   │
│   no timestamp: the checkout is a clone with a real origin, and  │
│   `status` asks it (#232).                                       │
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
`UV_PROJECT_ENVIRONMENT` unconditionally — the venv sits beside the checkout rather
than inside it, so it is never at uv's own default and there is no case left in which
leaving uv to itself lands the rebuild where the supervisor is looking. A rebuild that
landed beside that venv would leave the pod quietly running the image's code — the one
failure this whole mode exists to avoid. Passing it to `init` means passing the same
value to `hotfix values`.

`init` records it in the manifest, and `restart --reinstall` reads it from there
rather than taking a flag of its own
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
`mountPath` of `/podbench`, so any other value used to write a manifest
`status` could never see, and a hotfixed pod invisible to `status` is the precise
failure this mode exists to prevent. A claim genuinely mounted elsewhere is still
honoured by the flag; the warning says out loud both that `status` will not list
it and that `init` will refuse it, because the seed, the copied interpreter and
the supervisor's runtime switch all name `/podbench`.

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
or no checkout in the image to ask — the base is recorded as **assumed** and the
`claim` row says `an assumed base, so the count is a guess`. That is the point of the
item rather than a fallback from it: a derived count printed as though it were
measured is worse than one that admits what it stands on.

## `hotfix restart` — relaunch the application in place

The inner loop: you restart twenty times and commit once, when it works. It writes
nothing at all — no `add`, no `commit`, no manifest — because committing is `git
commit` in the seat and a verb that required `-m` per iteration is what pushed the
edit-run-look loop out of podbench altogether.

```text
podbench hotfix restart TARGET [--reinstall]
    │
    ▼
   resolve the target and the seat (exactly as init does)
    │
    ▼
   cat /podbench/.podbench-hotfix.json
    │
    ├─ absent ──────────────────────────────────▶ exit 2  ("run init first")
    ▼
   require_supervisor — before anything is killed, not after: with no
   supervisor the tree-kill takes the application down with nothing
   behind it, and the kubelet restarts the container and the seat with it
    │
    ▼
   --reinstall?  yes → env UV_CACHE_DIR=… uv sync … in the APPLICATION
                       container, into the venv the manifest records.
                       Before the kill: a relaunch onto a half-built venv
                       is the state this mode exists to avoid.
    │
    ▼
   cat /tmp/podbench-child.pid                       → the pid before
    │
    ▼
┌──────────────────────────────────────────────────────────────────┐
│ RELAUNCH — one exec, three steps                                 │
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
   cat /tmp/podbench-child.pid                       → the pid after
    │
    ▼
   git -C SRC status --porcelain      ← one read, two readings
     dirty → "the claim is dirty and running: N files uncommitted (…)"
     clean → "the claim is clean and running: the new process loaded <sha>"
     pyproject.toml / setup.py / setup.cfg / MANIFEST.in among the
     uncommitted paths, and no --reinstall → "the editable install still
     describes the old packaging … re-run with `--reinstall`"
    │
    ▼
   SRC/.vscode/launch.json exists?
     no  → nothing (a restart is not an ask for a debugger)
     yes → podbench debug-config --provision --provision-dest <claim>
                                 --output SRC/.vscode/launch.json
           failure → a line in the report, not a failed restart
    │
    ▼
                        actions printed, exit 0
```

What `restart` owes in exchange for writing nothing is the **dirty line**. An
uncommitted change on a live process is the one divergence no repository anywhere
records — a larger one than a committed change, not a smaller — so it is said on every
restart, clean or dirty. `status` measures the same thing from the other end.

**`--reinstall` is where `apply`'s one non-git step went.** An editable install is a
path redirection: new *code* is picked up for free, which is what makes this loop
cheap, but a new entry point or a renamed package is baked into the `.dist-info` at
install time. `apply` inferred that from the range of commits it had just made, and
that input is exactly what a mode with no recorded commit does not have — so it is a
flag. The half the working tree *can* see is said as a line rather than acted on: a
`uv sync` on every restart would cost the inner loop the thing that makes it one.

Two things the report is careful about:

* **Which pid.** `/tmp/podbench-child.pid` holds the *supervisor's* child, and
  on the measured target (`bl47p-ea-fastcs-01-0`, 2026-08-24) that was pid 7,
  `stdio-socket --ptty`, with the `fastcs-example` anybody would debug at pid 13
  three levels below it. The line says `stopped the supervisor child pid 7 and
  its tree` because the kill is a tree kill and because calling 7 "the
  application" would send a reader to the wrong process.
* **Which debug configuration.** Every configuration podbench authors is
  pid-keyed, so an untouched `launch.json` after a relaunch names a closed port
  — which presents as a broken adapter rather than as a stale file. Refreshing
  it is cheap: debugpy's files are already on the claim and a fresh process can
  be served again, so the one-shot problem does not arise. The gate is the
  document's existence and nothing weaker, because `--provision` ptraces the
  workload and installs into it; the justification for doing that without asking
  again is entirely that the user ran the debug step and has not retracted it.

## `hotfix status` — the point of the whole mode

Silently-diverged pods are the risk this mode creates, so `status` is cheap enough to
run habitually and exits non-zero when anything needs attention — usable as a
shutdown-checklist assertion.

```text
podbench hotfix status [-n NS] [-A] [--no-probe]
    │
    ▼
   get pods -o json                    ← ONE call for the whole namespace
                                         (`get pods --all-namespaces` under -A,
                                          and then every exec below is issued
                                          in that pod's own namespace)
    │
    ▼
   for each pod with a container mounting /podbench
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
    │      cat /podbench/.podbench-hotfix.json       ← the manifest
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
    ├─ a SECOND exec, only where there is a manifest — ordinary git in
    │  the claim, reading local objects and refs:
    │      command -v git                            ← 90: unmeasured
    │      git -C SRC status --porcelain             ← dirty, and which
    │      git -C SRC rev-parse HEAD                 ← what is running
    │      git -C SRC rev-list --count <base>..HEAD  ← how far ahead
    │      git -C SRC branch -r --contains HEAD      ← as of the clone
    │
    │   Two execs and not one because the count is measured from the
    │   manifest's base commit, and the manifest arrives in the first
    │   one. No network, no credential, no agent: measured in a seat on
    │   the live p47 pod with `git fetch` in the same session dying on
    │   host verification, and every one of these answered.
    │
    ├─ ONE `git ls-remote --heads <repo>` per distinct repository, run
    │  HERE, on the laptop, under a 5s bound (unless --no-remote)
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
     image-changed the image was upgraded under a live hotfix, so the
                   upgrade has not reached the running code
     unreadable    a manifest is present on the claim and will not parse
     not-hotfixed  held, but nothing hotfixed here
     active        hotfixed, and nothing here needs attention
    │
    ▼
   and four measured rows under each pod:

     claim   <head> is N commits ahead of <base>
     dirty   N files uncommitted, and they are what is running (…)
     remote  <head> is the tip of <branch>, checked just now
     image   unchanged since the hotfix was made (sha256:…)

   with the first three replaced by one `git` row saying `unmeasured`
   wherever the claim's container has no git
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

**The four rows are measured on every run, and nothing about them is recorded.** The
manifest used to carry the commit, the count, the author, the timestamp and the branch,
and #232 removed all five: the seat has ordinary git, so one hand commit or one hand
push makes a recorded field false while everything goes on printing it. What the
manifest keeps is what git cannot know — where the claim came from, which commit and
which image it was seeded against, how it is laid out.

**Three of the four need a git in the claim's container, and say so when there is
none.** `status` reads the claim through the *application* container rather than
through a seat, because the whole value of the listing is finding a hotfix nobody told
you about — which is a pod you have not attached to. A distroless application image
has no git; the report then prints one `git  unmeasured …` row rather than three rows
reporting an unread claim as clean. (Measured on `bl47p-ea-fastcs-01-0`: that container
*does* carry `/usr/bin/git`, which is a property of that image and not a rule.)

**The `remote` row is asked from the laptop.** An exec session has no `SSH_AUTH_SOCK`
— agent forwarding exists only inside an ssh session — so the pod could not use a
forwarded agent even once one is offered, and the claim's remote on the live target is
ssh: `git fetch --dry-run origin` in its seat answers `Host key verification failed`.
The laptop already holds both halves, credentials and connectivity, so podbench reads
the shas out over exec and runs `git ls-remote` here. Three rules on that row, each
with the failure it stops:

* **It is bounded** (5s, once per repository). A status verb that hangs on a network
  call is worse than one that says less.
* **A failure is `unmeasured`, never "not pushed".** A forge that is down, a
  repository that wants a credential this laptop does not have, a `git` that is not
  installed: none of those is evidence that nobody pushed. Where the query fails the
  row falls back to what the *clone* last fetched, and says that is what it is.
* **`ls-remote` returns ref tips, not ancestry.** The row can say a commit is the tip
  of a branch. It cannot cheaply say it has been merged, so it does not imply it.

**Nothing on those rows moves the exit code.** A dirty claim is the ordinary inner
loop, an unpushed one is the ordinary state of a fix made an hour ago, and a row that
could not be measured is not an assertion in either direction. The exit code stays what
it has always been: does a hotfix in this namespace need somebody today?

`-A`/`--all-namespaces` is the same listing over the cluster, with the same exit code.
That is the difference between the shutdown-checklist assertion this command's exit
code has always sold and a shell loop over namespaces the operator has to write and
keep correct ([#205](https://github.com/gilesknap/podbench/issues/205) item 5). Each
pod is still read through a client bound to *its own* namespace: one `-n` wrong on an
exec reads a claim out of a different pod, or out of none.

A row whose **image has moved** also carries a **retirement** line naming which steps
are left — see `hotfix retire` below. That gate used to be the recorded
`consolidated_branch`; the image is the better one of the two, because it turns on the
cluster in front of you rather than on somebody having run a verb.

The hold is a column and not a clause in the health sentence because they are
different questions and either can be true alone: a perfectly healthy hotfix can sit in
a pod nobody released, and a pod that was **never hotfixed at all** can be left held by
a relaunch that died mid-flight. That second case is real and nothing else will notice
it — its liveness probe is short-circuited and its supervisor is relaunching without
backoff — which is why a held pod is listed whether or not it carries a manifest, and
why the hold moves the exit code.

## Getting it back into an image

podbench has no verb for this half, and deleting the one it had is
[#232](https://github.com/gilesknap/podbench/issues/232). `consolidate` was
`git push origin HEAD:refs/heads/<branch>` plus a manifest field, and it handled no
credentials — so pushing from the seat was exactly as hard with it as without it, and
on the pod it was built for it did not work at all: the claim's remote is
`git@github.com:…`, the seat has no key and no `known_hosts`, and the push dies on
`Host key verification failed` (measured on `bl47p-ea-fastcs-01-0`, 2026-08-24). The
manifest field it wrote was worse than useless once anybody pushed by hand.

The steps themselves have not changed, and they are still the order:

1. `git push` the claim's checkout as a branch — from the seat once it can reach the
   forge, or from a laptop clone;
2. `gh pr create`, merge, and let CI build and publish the image;
3. roll the workload onto the new image and confirm it is healthy;
4. take the volume, volumeMount, `command`, `args` and `podSecurityContext` back out
   of the *application's* own values, and redeploy;
5. turn the claim off (`podbench-hotfix-claim.enabled=false`, or
   `hotfixProject.enabled=false` on the central route) **and** delete it — it is
   annotated `Prune=false`, so the flip alone leaves it.

Steps 4 and 5 are the two nobody does, and they are two because the first lives in the
application's values and the second in the claim's chart: turning the claim off leaves
the pod wired, which is how a claim goes on shadowing a fixed image. `podbench hotfix
status` says whether step 1 has happened — it asks the forge — and `podbench hotfix
retire` measures 3, 4 and 5.

Until step 5 the claim keeps shadowing the image's project — the runtime switch prefers
a seeded claim, and it does not care that the fix is now in the image too.

Step 5 is two actions and not one, deliberately. The claim carries both
`helm.sh/resource-policy: keep` and
`argocd.argoproj.io/sync-options: Prune=false,Delete=false`, so turning the
boolean off takes the claim out of the desired state and leaves the object
standing. That is the point — a hotfix has to survive somebody reverting a
repoint mid-beamtime — and it means the deletion is a separate, deliberate act.
Measured: a claim carrying Helm's annotation *alone* is pruned about three
minutes after it leaves the desired state, and a `Delete`-reclaim PV goes with
it (issue #190).

## `hotfix retire` — the checklist becomes a measurement

```text
podbench hotfix retire TARGET [-n NS] [--container NAME] [--delete-claim]
```

Steps 4 and 5 above are the ones nobody does, and nothing tracked them
([#205](https://github.com/gilesknap/podbench/issues/205) item 4). `retire` asks the
cluster where a retirement has actually got to, and performs the one step podbench can:

```text
  [x]     image          the deployed image is sha256:bbbb…, and the hotfix was
                         made against sha256:aaaa…. Whether the rebuild included
                         the fix is *not* measured — podbench compares digests,
                         not contents.
  [ ]     wiring         bl47p-ea-fastcs-01-0 still carries the podbench-app
                         volume, a volumeMount at /podbench and the
                         supervisor loop in command and args. Those are fields
                         in the application's own pod template, not in the
                         claim's chart, so turning the claim off does not
                         remove them: take those entries — and not the whole
                         `volumes` and `volumeMounts` keys, which carry the
                         service's own — back out of the application's values
                         and redeploy. podbench-home is declared as well, and
                         it is the seat's rather than the hotfix's …
                         podSecurityContext.fsGroup is 37887, which `hotfix
                         values` emits too; whether this pod had one before the
                         hotfix is not measured here …
  [ ]     claim          bl47p-ea-fastcs-01-podbench-project still exists …
------------------------------------------------------------------------
VERDICT: 2 of 3 steps of retirement remain (exit 1)
REMAINING: wiring, claim
```

**That report is the live specimen.** On 2026-08-23 `p47-services` was on a branch
whose top commit turned hotfix mode *off* — `podbench-hotfix-claim.enabled: false` —
every pod in the namespace was deleted, and `bl47p-ea-fastcs-01-0` came back still
mounting the claim and still running the supervisor loop. The boolean disables the
**subchart**, which is the PVC; `volumes`, `volumeMounts`, `command`, `args` and
`podSecurityContext` live in the target's own `ioc-instance` values and were untouched.
Somebody had done step 5 and not step 4, and the state that leaves — a pod wired to a
claim its chart no longer declares — is worse than either end of the checklist, because
it fails only when that PVC is finally pruned and only at the next reschedule.

Three rows and not five, because these are the three a cluster can be *asked* about:
getting the fix onto a branch, opening the PR and merging it leave no trace podbench
can read, while rolling the image, unwiring the pod and deleting the claim each do.

There was a fourth, `branch`, and #232 dropped it with the field it read.
`consolidated_branch` was written by `consolidate` and could be made a lie by one hand
push — the whole objection that deleted the verb. Whether the fix exists anywhere but
the claim is a live question now, asked by `status` against the forge and reported as
`unmeasured` when the forge cannot be reached; retirement turns only on what can be
measured from the cluster in front of you.

The rules the report keeps, each with the failure it exists to stop:

* **`[x]` only for a step that was measured done.** An unmeasured step is `[ ]` with a
  detail saying why, and it moves the exit code in neither direction. A retirement that
  lies is worse than the checklist it replaces, which is #205 item 4's own
  falsification.
* **The manifest can only be read while something mounts the claim**, so `image` goes
  unmeasured the moment the pod is unwired — which is exactly when the deletion becomes
  safe. The verb says so rather than carrying the last answer forward.
* **The `wiring` row names what `values` emits, and does not send anyone at the keys.**
  It named three things when six values were wired, and its closing clause pointed at
  "the values `podbench hotfix values` emitted" — which under `--from-pod` is
  podbench's own entries only, while the service's `volumes` and `volumeMounts` carry
  its own as well. A helm list *replaces* across the parent/child merge, so deleting
  those keys wholesale unmounts a beamline directory. The `fsGroup` is named rather
  than counted: podbench emits one and an application may have declared its own, and
  the pod cannot say which.
* **A claim that could not be read is not a claim that is gone.** kubectl tells a 403
  from a 404 in its text alone, and ticking `claim` on a refusal would tick the one step
  nobody can undo.
* **Nor is a label listing that found nothing.** Once the pod is unwired the only thing
  pointing at the claim is `podbench.dev/hotfix-target`, and the central chart sets that
  from the `hotfixProject.claims[].name` entry — a free-form name that nothing requires
  to match the container, the workload or the pod. So an empty listing is an answer
  about labels, and the `claim` row says so and stays unmeasured. The subchart route
  labels from the release name and usually *does* match, which is what would have made
  reading it as "gone" silent.
* **`retire` is the one verb that does not refuse a multi-replica workload.** That
  refusal guards a write to a `ReadWriteOnce` claim, and the state a retirement report
  exists to confirm — the wiring out, the team scaled back up — is exactly the one it
  forbids. Of several live pods it measures the one still carrying the wiring, so a
  rollout in flight cannot tick that step off the replica that was rolled first.
* **`--delete-claim` declines while anything still mounts the claim**, and the question
  is asked of the *namespace* rather than of the target: the second pod of a rollout
  holds the claim just as hard. A claim deleted out from under a running pod stays
  `Terminating` while that pod holds its reference and then fails to bind on the next
  reschedule — the specimen's failure, reached deliberately. A pod listing that could
  not be read declines too: the precondition is a negative one, and "found no mounters"
  and "could not look" must not be one answer.
* **Both caveats about the deletion are printed by the path that made it**: nothing
  mounted the claim, so its manifest could not be read first and what was on it is
  unverified; and if the chart still declares the claim, the next sync recreates it.

Everything above the claim is somebody else's system — a PR, a merge, a rebuild, a
values change — so this verb's honest job is to say which of them have landed. It is
read-only without `--delete-claim`, and it lands no seat: the claim's manifest is read
through the *application* container, the way `status` reads it.

Exit **1** while any measured step is outstanding, **0** once the pod is unwired and the
claim is gone.

## Every cluster call, in order

```text
  init / restart:
   1  kubectl config view --minify -o jsonpath={..namespace}   # only without -n
   2  kubectl -n NS get deployment NAME -o json     ┐ or get pod NAME -o json,
   3  kubectl -n NS get pods -l <matchLabels> -o json│ then get replicaset / get
   4  kubectl -n NS get pod POD -o json             ┘ deployment walking upwards
                                                     (+ the seat lookup)
   5  kubectl -n NS exec -c APP  POD -- test -e /tmp/podbench-child.pid
                                                     # init and restart: require
                                                     # the supervisor
   6  kubectl -n NS exec -c SEAT POD -- <cp / git / cat / test / sh -c 'cat > …'>
                                                     # the seed, and every claim
                                                     # read and write
   7  kubectl -n NS exec -c APP  POD -- env UV_CACHE_DIR=… uv sync …
                                                     # init, and restart under
                                                     # --reinstall
   8  kubectl -n NS exec -c APP  POD -- cat /tmp/podbench-child.pid
                                                     # restart, either side of 9
   9  kubectl -n NS exec -c APP  POD -- bash -c '<hold; kill the tree; release>'
                                                     # every restart
  10  kubectl -n NS exec -c SEAT POD -- podbench debug-config --provision …
                                                     # restart, only where the
                                                     # claim already has a
                                                     # .vscode/launch.json

  status:
   1  kubectl -n NS get pods -o json
   2  kubectl -n NS exec -c APP POD -- sh -c 'cat manifest; cat hold; date'
                                                     # per candidate pod
   3  kubectl -n NS exec -c APP POD -- sh -c '<git status/rev-parse/rev-list/
                                               branch -r>'
                                                     # only where there is a
                                                     # manifest to count from
   4  kubectl -n NS exec -c APP POD -- python3 -V    # only for a changed digest
   5  git ls-remote --heads REPO                     # NOT kubectl: run here,
                                                     # once per repository,
                                                     # bounded, and skipped
                                                     # under --no-remote

  values:
   1  kubectl config view --minify -o jsonpath={..namespace}   # only without -n
   2  kubectl -n NS get pod POD -o json                # nothing else, and
                                                       # never skipped: the read
                                                       # is not optional

  retire:
   1  the same target walk as init, rows 1-4 above
   2  kubectl -n NS exec -c APP POD -- sh -c 'cat manifest; …'
                                                     # only while a container
                                                     # mounts the claim
   3  kubectl -n NS get pvc NAME -o name             ┐ the pod names the claim
   4  kubectl -n NS get pvc -l podbench.dev/hotfix-target -o json
                                                     ┘ or, once unwired, the
                                                       label is the only way
                                                       back to it
   5  kubectl -n NS get pods -o json                 # only with --delete-claim:
                                                     # who holds the claim is a
                                                     # question about the
                                                     # namespace, not the target
   6  kubectl -n NS delete pvc NAME                  # only once nothing does

  check:
   1  the same target walk as init, rows 1-4 above, and nothing re-read after
                                                     # it: the walk's own pod
                                                     # JSON is what the rows
                                                     # are measured against
   2  kubectl -n NS exec -c APP  POD -- test -e /podbench/app/pyproject.toml
   3  kubectl -n NS exec -c APP  POD -- test -e /tmp/podbench-child.pid
   4  kubectl -n NS exec -c SEAT POD -- ls /proc/1/root/   ┐ only with a seat,
   5  kubectl -n NS exec -c APP  POD -- test -d /app       ┘ and only where 2
                                                       # said the claim is not
                                                       # seeded yet (and /python
                                                       # beside /app)
                                                       # plus one anonymous
                                                       # registry read for the
                                                       # image's own labels,
                                                       # skipped under --repo
```

Nothing patches a workload and nothing deletes a pod. `rbac.hotfix` — on top of
`rbac.observe` — is **`get` on `deployments`, `statefulsets` and `replicasets`, plus
`get`/`list`/`delete` on `persistentvolumeclaims` for `retire`, and nothing else**.
That delete is the single write in the grant, and it is the act the whole checklist
ends in: a claim carries `Prune=false,Delete=false` precisely so that no sync will ever
do it for anybody. It used to add `patch` on workloads and `patch`/`delete` on pods,
because the annotation write *was* the rollout and that verb therefore deployed code —
the most privileged thing podbench asked for anywhere. Moving the provenance onto the
claim and the relaunch inside the container removed the need for all of it, so in
cluster terms Hotfix mode is now watching, plus the one deletion that ends it.

## Two things the claim does not fix

1. **The claim's project shadows the image's.** An image upgrade under a live hotfix
   keeps running the claim's code, so the upgrade does not reach what is executing.
   The manifest records the digest it was made against and `status` compares it, which
   is what turns a silent shadow into `image-changed`.
2. **Single replica only.** RWO, one checkout, one venv; an edit and a restart on
   either of two pods is a race with no winner.

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

* [Hotfix a running pod](../how-to/hotfix-a-running-pod.md) — the same mode as a
  sequence to follow rather than a mechanism to understand.
* [Glossary](../reference/glossary.md) — PSA, Yama, the ambient set, `subPath` and every
  other term used here without explanation.
* [Ways in](ways-in.md) — why you would reach for this rather than `attach` or `dev`.
* [Architecture](architecture.md) — the mount-namespace rule this mode dissolves.
* [What `attach` does](attach-flow.md) — the seat this mode reaches the claim through.
