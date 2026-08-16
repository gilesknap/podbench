# What `dev` does

Iterate mode. `podbench dev` authors a **sacrificial clone** of a running pod, idles
the application container in the clone, and adds a real sidecar carrying the editor,
the checkout and the interpreter. You then edit and run `podbench run` inside it; the
change is visible through the Service in about a second.

The origin pod is never touched. The clone is a second copy of the workload, which is
why this mode is unavailable to singletons and refused outright against anything a
GitOps controller reconciles.

This page is the mechanism, in order, with the `kubectl` commands each step becomes.

## Creating the dev pod

```text
podbench dev POD -n NS [--container NAME] [--port 8080]
                       [--take-traffic | --cutover SERVICE] [--dry-run]
    │
    ▼
┌──────────────────────────────────────────────────────────────────┐
│ LOCAL                                                            │
│   namespace : -n, DEFAULTING TO LITERAL "default"                │
│              (unlike attach, dev does not read the kubeconfig's) │
│   POD       : required, exact. No substring match, no prompt     │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ READ THE ORIGIN         get pod POD -o json                      │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ IS IT GITOPS-MANAGED?   walk up to 3 owner hops                  │
│                                                                  │
│   pod → ReplicaSet → Deployment, checking each object for        │
│     label      argocd.argoproj.io/instance                       │
│     annotation argocd.argoproj.io/tracking-id                    │
│                                                                  │
│   get replicaset NAME -o json                                    │
│   get deployment NAME -o json                                    │
│                                                                  │
│   The walk is the point: Argo stamps what it applied from git —  │
│   the Deployment — and the pods a controller makes carry none    │
│   of it (measured: 0 of 82 pods).  A failed `get` reads as       │
│   "unmarked" rather than as "managed".                           │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ marked ─────────────────────▶ exit 1
                                  ▼            (absolute; there is no override)
┌──────────────────────────────────────────────────────────────────┐
│ DECIDE THE SHAPE                                                 │
│   target container = --container, else the pod's *only* one      │
│                      (2+ containers and no flag → refuse)        │
│   target port      = --port, else its first containerPort        │
│                      (neither → refuse: the readiness probe      │
│                       needs one and guessing is worse)           │
│   dev pod name     = --name, else POD-podbench (idempotent)      │
│   seat identity    = uid/gid, if the origin declares the         │
│                      podbench-identity volume and pins both      │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ READ YOUR SSH KEY   ~/.ssh/id_ed25519.pub                        │
│                                                                  │
│   Before anything is created, and there is no second chance: an  │
│   ordinary container's env is immutable once the pod exists, so  │
│   a dev pod made without a key can only be deleted and remade.   │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ no .pub file ───────────────▶ exit 1
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ AUTHOR THE CLONE   (pure JSON in, JSON out; see next section)    │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ --dry-run → print it ───────▶ exit 0
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ --cutover SERVICE ?                                              │
│   get service SERVICE -o json → record its current selector      │
│   …onto the dev pod's annotations, BEFORE the Service is touched │
│   so a failure between the two still leaves a pod that knows     │
│   how to undo itself                                             │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ Service has no selector ────▶ exit 1
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ CREATE                                                           │
│   create -f - -o json          (create, never apply: a dev pod   │
│                                 must not adopt something)        │
│   wait pod/NAME --for=jsonpath={.status.phase}=Running           │
│                                                                  │
│   Running, NOT Ready: the sidecar's readiness probe follows your │
│   process, and nothing is listening until your first `run`.      │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ --cutover SERVICE ?                                              │
│   patch service SERVICE --type=json                              │
│     [{"op":"replace","path":"/spec/selector",                    │
│       "value":{"podbench.dev/devpod":"true"}}]                   │
│                                                                  │
│   A JSON *replace*.  A merge patch unions the two maps, which    │
│   adds the dev pod without removing the original — the opposite  │
│   of a cutover, and invisible until half the responses are stale.│
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ WIRE THE CLIENT — the same code path attach uses                 │
│   exec -c podbench -- podbench agent --print-login-user          │
│   get pod NAME -o json           → metadata.uid (HostKeyAlias)   │
│   exec -c podbench -- podbench agent --print-host-key …          │
│   write ~/.podbench/known_hosts and config.d/<ns>-<pod>.conf     │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
                 connection summary + next steps         exit 0
```

## What the authored clone changes

The launcher builds the manifest itself rather than shelling out to `kubectl debug
--copy-to`, which strips every label and annotation — leaving the clone invisible to
the Service, so the headline demo cannot work at all — and offers no way to give the
debug container `resources` or a workspace volume, and no `--dry-run` to preview any
of it.

```text
  origin pod JSON                          dev pod JSON
  ───────────────                          ────────────
  metadata.name          ────────────────▶ POD-podbench
  metadata.labels        ──── dropped, unless --take-traffic
                              and then minus every controller label:
                              pod-template-hash, controller-revision-hash,
                              controller-uid, job-name, statefulset…pod-name
                         ────────────────▶ + podbench.dev/devpod: "true"
  metadata.annotations   ──── dropped ───▶ only podbench.dev/origin: POD
  metadata.uid, resourceVersion, ownerReferences, …  ──── dropped
  status                 ──── dropped
  spec.nodeName          ──── dropped
  spec.ephemeralContainers ── dropped   (forbidden on create, and spent anyway)
                         ────────────────▶ restartPolicy: Never
                         ────────────────▶ shareProcessNamespace: true
  spec.volumes           ──── copied ────▶ + podbench-workspace (emptyDir 4Gi)
  the target container:
    command/args         ────────────────▶ ["sleep","infinity"]
    readiness/liveness/startup probes, lifecycle ──── stripped
    everything else      ──── copied
                         ────────────────▶ + the podbench sidecar (below)
```

Four of those are things `--copy-to` cannot express, and each is load-bearing:

1. **Label policy.** Keeping the Service-selector labels while dropping
   `pod-template-hash` puts the dev pod in the EndpointSlice without making it a
   ReplicaSet member. Keeping the hash would give a `replicas: 1` ReplicaSet two
   matching pods, and one of them would be reaped.
2. **A readiness probe on the sidecar**, `tcpSocket` on the app's port. Without it a
   dev pod is Ready the instant it starts and joins the Service while nothing is
   listening — roughly half the requests blackholed. With it, Service membership
   tracks the inner loop in both directions.
3. **Real `resources` and a workspace volume** on the sidecar. This is what makes
   Iterate mode immune to the OOM and eviction traps Observe mode has to warn about.
4. **An inert PID 1 by construction.** The command becomes `sleep infinity` *in the
   authored spec*, never by pausing a live pod: if PID 1 exits the kubelet restarts
   the container with pristine image code, and a SIGSTOPped process still holds its
   listening socket while liveness probes kill it anyway.

The sidecar itself:

```text
  name: podbench            command: ["podbench","agent"]     (never sleep)
  workingDir / HOME: /workspace        ← uv's caches, toolchains and venvs
  volumeMounts: podbench-workspace at /workspace
                (+ podbench-home and the two identity subPaths, when the
                 origin declared them — see the securityContext below)
  resources: 200m/512Mi requests, 2 CPU/3Gi limits
  readinessProbe: tcpSocket <target port>, period 2s, failureThreshold 1
  env: PODBENCH_TARGET=<app container>, PODBENCH_PUBKEY=<your public key>

  securityContext — one of two, and they are mutually exclusive:
    origin declares podbench-identity and pins a non-root uid+gid
        → runAsUser/runAsGroup = the app's own, drop ALL, no SYS_PTRACE,
          /etc/passwd and /etc/group projected by subPath (an *ordinary*
          container may do this; an ephemeral one may not)
        → admissible under the restricted Pod Security Standard
    otherwise
        → runAsUser: 0, runAsNonRoot: false, capabilities.add [SYS_PTRACE]
```

`SYS_PTRACE` needs uid 0, and the projected passwd record names the application's uid.
Running as root with that file mounted would leave sshd resolving the login name to a
uid the container is not, and your `authorized_keys` unreadable by it. So the identity
wins, and gives up ptrace with the root it no longer has.

## The inner loop, inside the pod

`dev-bootstrap`, `run` and `stop` make no API calls at all — they are ordinary
processes in the sidecar, reached over ssh or `kubectl exec`.

```text
podbench dev-bootstrap --repo URL [--ref REF] [--dir /workspace/src] [--python 3.12]
    │
    ├─ refuse if the checkout or venv path resolves under /proc/<pid>/root
    │     (the mount-namespace rule — see below)
    ▼
   checkout exists?  no → git clone URL DIR         [+ git checkout --force REF]
                     yes→ git -C DIR fetch --prune --tags
                          git -C DIR checkout --force REF   (or pull --ff-only)
    │
    ├─ uv python install VERSION                    (--python only)
    ├─ uv --directory DIR sync --frozen             (--frozen: the lockfile is
    │                                                the application's; a dev pod
    │                                                that re-resolves is no longer
    │                                                running what production runs)
    └─ uv --directory DIR pip install -e .

   Nothing is apt-installed. git, curl, uv, iproute2, procps and a pre-seeded
   CPython are baked into the image because apt-get was 10.6 s of a 19 s loop.
```

```text
podbench run --port 8080 -- python -m myapp
    │
    ▼
┌──────────────────────────────────────────────────────────────────┐
│ 1 · MOUNT-NAMESPACE CHECK                                        │
│     interpreter and cwd must not resolve under /proc/<pid>/root  │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ they do ────────────────────▶ exit 1
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ 2 · STOP THE RECORDED CHILD                                      │
│     read /workspace/.podbench/run.json → pid                     │
│     SIGTERM, wait --grace (5s), then SIGKILL                     │
│                                                                  │
│     By recorded pid, never `pkill -f`: under                     │
│     shareProcessNamespace that matches the invoking shell and    │
│     every other container's processes.                           │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ 3 · PRE-FLIGHT THE PORT — two `ss` calls, because one cannot     │
│     answer both questions                                        │
│                                                                  │
│     ss -lntpe            → is anything LISTENING on the port?    │
│     ss -tan state time-wait → how many sockets are in TIME_WAIT? │
│                                                                  │
│     a listener → refuse, naming its pid, its command line, and   │
│       which container in the pod it lives in.                    │
│       A second SO_REUSEPORT bind succeeds silently and the       │
│       kernel splits traffic between old and new code.            │
│     TIME_WAIT → warn: a rebind can fail for ~60 s while          │
│       `ss -l` shows no listener at all.                          │
│     ss unreadable → refuse. Failing open puts back the guess     │
│       this pre-flight exists to remove.                          │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ not clear ──────────────────▶ exit 1
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ 4 · SPAWN, DETACHED                                              │
│     stdout+stderr → /workspace/.podbench/run.log                 │
│     record pid, port, command, cwd, start_ticks                  │
│       → /workspace/.podbench/run.json, BEFORE it is verified,    │
│         so a child that never binds is still killable by `stop`  │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ 5 · VERIFY — poll every 50 ms up to --timeout (15 s)             │
│                                                                  │
│     is the pid still alive?          /proc/<pid>                 │
│     does `ss -lntpe` attribute the port to *our* pid?            │
│     is that socket's inode open in /proc/<pid>/fd?               │
│                                                                  │
│     All three, because a socket poll alone gives a false PASS:   │
│     the spike's naive wrapper printed "LISTENING after 1 polls"  │
│     and exited 0 for a relaunch that had already died with       │
│     EADDRINUSE, while the Service happily served the old code.   │
│                                                                  │
│     Somebody else owns the port → stop immediately, do not poll  │
│     on: SO_REUSEPORT would serve half the traffic from old code. │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ any check fails → tail run.log ▶ exit 1
                                  ▼
                     "pid N owns the listening socket"      exit 0
                     the readiness probe passes ~2 s later,
                     and the pod (re)joins the Service
```

`podbench stop` is step 2 on its own.

### The mount-namespace rule

Interpreter, venv and checkout must all live in the debug container. An editable
install writes a `.pth` into the *interpreter's* site-packages naming the checkout; if
the interpreter is the target's and the checkout is the sidecar's, that path does not
exist in the namespace that resolves it, and `site.py` **silently drops it** — the
symptom is a `ModuleNotFoundError` much later, in something that looks unrelated.

There is no workaround, because the `/proc/<pid>/root` bridge is one-directional by
capability: the sidecar can read the app's rootfs, the app cannot see the sidecar's.
So `dev-bootstrap` and `run` refuse the layout rather than let it be discovered.

## Teardown

```text
podbench dev --delete POD-podbench -n NS
    │
    ▼
   get pod NAME -o json
    │
    ├─ no such pod ───────────────────────────▶ "nothing to delete", exit 0
    ├─ no podbench.dev/devpod label ──────────▶ exit 1
    │      `dev --delete` typed with the origin's name would otherwise
    │      delete production
    ▼
   recorded cutover?  patch service SVC --type=json  ← the *exact* old selector
    │                   Selector first, pod second: there is then no window
    ▼                   in which the Service selects nothing
   delete pod NAME --wait=true
    │
    ▼
   remove ~/.podbench/config.d/<ns>-<pod>.conf
   drop the HostKeyAlias line from ~/.podbench/known_hosts
       (unlike an attach seat, this pod is never coming back, and its alias is
        keyed on a pod UID no pod will ever have again)
```

## Every cluster call, in order

```text
  create:
   1  kubectl -n NS get pod POD -o json
   2  kubectl -n NS get replicaset NAME -o json     ┐ GitOps ownership walk,
   3  kubectl -n NS get deployment NAME -o json     ┘ up to 3 hops, errors ignored
   4  kubectl -n NS get service SVC -o json         # --cutover only
   5  kubectl -n NS create -f - -o json             # the authored manifest, on stdin
   6  kubectl -n NS wait pod/NAME \
          --for=jsonpath={.status.phase}=Running --timeout=120s
   7  kubectl -n NS patch service SVC --type=json -p '[{"op":"replace",…}]'
                                                    # --cutover only
   8  kubectl -n NS exec -c podbench NAME -- podbench agent --print-login-user
   9  kubectl -n NS get pod NAME -o json            # metadata.uid
  10  kubectl -n NS exec -c podbench NAME -- podbench agent --print-host-key …

  delete:
   1  kubectl -n NS get pod NAME -o json
   2  kubectl -n NS patch service SVC --type=json -p '[…original selector…]'
   3  kubectl -n NS delete pod NAME --wait=true --ignore-not-found

  the inner loop:  no API calls at all
```

RBAC — `rbac.iterate` in the chart, on top of `rbac.observe` — is `create`/`delete` on
`pods`, `get`/`list`/`patch` on `services` (only for `--take-traffic` and `--cutover`)
and `get`/`list` on `persistentvolumeclaims`. The ownership walk's `get` on
replicasets and deployments is optional: a refusal reads as "not GitOps-managed".

## See also

* [Ways in](ways-in.md) — singleton-safety, GitOps-safety, and when not to use this.
* [Architecture](architecture.md) — why the launcher authors pod specs itself.
* [Phase 0 gate report](spikes/phase0-report.md) — §3.5 `--copy-to`, §3.6 the
  readiness probe, §3.16 SO_REUSEPORT and TIME_WAIT, §3.17 the `.pth`, §4.4 the 1.18 s
  cycle.
* [Iterate on Python](../how-to/iterate-on-python.md) — the same thing as instructions.
