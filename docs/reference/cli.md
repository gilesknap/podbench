# Command-line reference

One binary serves both halves of podbench. On your machine it is reached as
`podbench <verb>`; inside the debug container the same binary is PID 1 and backs
the helpers on `PATH`. Keeping it as one package means the capability logic that
decides what a session can do is the same code in both places, rather than a
launcher's guess and a helper's separate guess.

```
$ podbench --help

 Usage: podbench [OPTIONS] COMMAND [ARGS]...

 A development seat inside a Kubernetes pod.

 Run `podbench VERB --help` for a verb's own options.

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --version  -v        show the launcher's version and exit                                        │
│ --help               Show this message and exit.                                                 │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ On your machine ────────────────────────────────────────────────────────────────────────────────╮
│ attach         add or reconnect a podbench container and print the report                        │
│ ssh-config     regenerate the ssh stanza for an existing session                                 │
│ status         the podbench containers in one pod and what each supports                         │
│ list           every pod in the namespace carrying a podbench container                          │
│ dev            create or delete the dev pod                                                      │
│ patch          durable in-place fixes on a claim-backed venv                                     │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Inside the debug container ─────────────────────────────────────────────────────────────────────╮
│ agent          prepare the container for ssh and idle as its PID 1                               │
│ capreport      name the mechanism that denies ptrace in this container                           │
│ pids           list the pod's processes                                                          │
│ dbg            debug a process                                                                   │
│ debug-config   write VS Code's launch.json for this seat                                         │
│ dev-bootstrap  clone, sync and editable-install a checkout                                       │
│ run            relaunch the app and verify it                                                    │
│ stop           stop the recorded child                                                           │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

| Where it runs | Verbs |
|---|---|
| Your machine | `attach`, `ssh-config`, `status`, `list`, `dev`, `patch` |
| Inside the debug container | `agent`, `capreport`, `pids`, `dbg`, `debug-config`, `dev-bootstrap`, `run`, `stop` |

Every verb below is written as `podbench <verb>`, which is the only spelling
there is — there is no kubectl plugin. How you reach that program is your
choice, and all three run the same code:

| Invocation | Why |
|---|---|
| `uvx podbench <verb>` | the canonical one. uv fetches the launcher for the run and leaves nothing installed |
| `uvx podbench@<version> <verb>` | pinned, so a session is reproducible and the image tag it picks is known in advance |
| `uv tool install podbench` (or pipx, or pip) | for `podbench` permanently on `PATH` |

See [Installation](../tutorials/installation.md) for the details, including how
to run it before the first PyPI release.

The in-pod verbs are also reachable as `podbench <verb>` from a terminal in the
seat; several have shorter aliases on `PATH` (`pids`, `dbg`, `capreport`,
`debug-config`, `dev-bootstrap`, `podbench-run`, `podbench-stop`).

## Common options

The four launcher verbs — `attach`, `ssh-config`, `status`, `list` — take these:

```
--namespace  -n  NAMESPACE  namespace (default: the kubeconfig context's own)
--context        NAME       kubeconfig context
--kubectl        BIN        kubectl binary to use [default: kubectl]
--config-dir     DIR        where the generated ssh config and known_hosts live
                            (default ~/.podbench)
```

`dev` takes `-n`/`--namespace`, `--context` and — because it writes an ssh
config too — `--identity`, `--config-dir` and `--host-alias`. It does not take
`--kubectl`: it shells out to `kubectl` on `PATH`. Under `patch` the same three —
`-n`/`--namespace`, `--context` and `--kubectl` — sit on each **sub-verb**, not
on `patch` itself, so it is `podbench patch status -n demo` and never
`podbench patch -n demo status`. `patch` writes no ssh config, so nothing under
it takes `--config-dir`.

podbench shells out to `kubectl` deliberately, so it inherits your kubeconfig,
your current context and any exec credential plugin. There is no second
credential and no client library.

(naming-the-pod)=
## Naming the pod

`attach`, `ssh-config` and `status` take a `POD`, and none of them needs the
whole name. Resolution is the same in all three:

| you type | what happens |
|---|---|
| the full name, or `pod/NAME` | used as typed, in one `kubectl get pod` — an exact name is never ambiguous, even when it is also a substring of another pod's name |
| a substring matching **one** pod | resolved to that pod, and the name it resolved to is echoed on stderr |
| a substring matching **several** | the matches are listed and you are asked which |
| nothing at all | every pod in the namespace is listed and you are asked which |
| a substring matching **none** | an error naming the namespace searched, with what is in it |

```
$ podbench attach api -n demo
'api' matches 2 pods in namespace demo
      NAME        READY  STATUS   AGE  PODBENCH
  1.  api-7f9     1/1    Running  3h   podbench-1
  2.  api-canary  0/1    Pending  3h   -
which one? [number or name, empty to cancel] 1
```

The listing carries what you choose *by*: ready containers, status, age, and the
podbench container already in the pod — which is the difference between landing
a seat and reconnecting to yours. Answer with the number, the name, or a longer
substring; an empty line cancels.

The prompt is only ever offered on a terminal. **When stdin is not a tty — a
script, a CI job, an `ssh host podbench ...` — a prompt would be a hang**, so
podbench prints the same listing, explains that it will not ask, and exits `2`.
`--no-prompt` asks for that behaviour on a terminal too. Both the listing and
the "matched" echo go to **stderr**, so a redirected stdout still holds only the
report.

Resolution lists every pod in the namespace, which is not what `podbench list`
does: `list` shows the pods that already carry a podbench container, and
resolution offers the pods that could. A fully typed name is answered without
listing at all, so `attach` still works with RBAC that grants `get` on pods but
not `list`.

---

## Cluster-side verbs

### `attach`

Land a debug seat in a **live** pod, walking the capability ladder, and print
what that seat can actually do.

```

 Usage: podbench attach [OPTIONS] [POD]

 add or reconnect a podbench container and print the report

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   POD      <str>  pod/NAME, a bare NAME, or any substring of one. Omitted, or matching more than │
│                   one pod, lists the namespace and asks                                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --target                    NAME             workload container name                             │
│ --image                     REF              debug image (default: $PODBENCH_IMAGE, else the     │
│                                              image built from this launcher's version)           │
│ --target-uid                UID              the target's uid, when its pod spec does not say    │
│ --mount                     CLAIM:MOUNTPATH  mount a volume the pod already declares into the    │
│                                              seat, named by claim or by volume name. MOUNTPATH   │
│                                              defaults to the application container's own, which  │
│                                              Patch mode requires it to equal. Repeatable         │
│ --new                                        add a container even if one is running (its name is │
│                                              permanent)                                          │
│ --seat-gid-root                              land the seat with runAsGroup: 0 so it can register │
│                                              an /etc/passwd entry for the target's uid, which is │
│                                              what sshd needs to let anyone log in, and the only  │
│                                              way to get one on a live pod. Off by default: it    │
│                                              drops the target's own group                        │
│ --no-seat-identity                           do not mount the pod's podbench-home volume, which  │
│                                              is otherwise mounted by convention when the pod     │
│                                              declares it and keeps everything the seat writes    │
│                                              off the workload's ephemeral-storage budget. The    │
│                                              podbench-identity volume is never mounted by        │
│                                              attach: it needs a subPath per file, which an       │
│                                              ephemeral container may not have - use              │
│                                              --seat-gid-root for the seat's /etc/passwd entry    │
│ --no-probe                                   skip capreport; the report then says nothing was    │
│                                              measured                                            │
│ --resize                    MEMORY           raise the target's memory limit in place first,     │
│                                              e.g. 6Gi                                            │
│ --identity                  KEY              ssh key to authorise in the seat and name in the    │
│                                              generated stanza                                    │
│                                              [default: ~/.ssh/id_ed25519]                        │
│ --ssh-user                  NAME             login name to put in the stanza                     │
│ --host-alias                NAME             ssh Host name for the seat                          │
│ --print-config                               print the ssh stanza instead of writing it to the   │
│                                              config dir                                          │
│ --timeout                   SECONDS          seconds to wait for the seat [default: 120.0]       │
│ --no-prompt                                  never ask which pod: an ambiguous or missing POD is │
│                                              refused with the candidates instead. Already        │
│                                              implied when stdin is not a tty                     │
│ --namespace         -n      NAMESPACE        namespace (default: the kubeconfig context's own)   │
│ --context                   NAME             kubeconfig context                                  │
│ --kubectl                   BIN              kubectl binary to use [default: kubectl]            │
│ --config-dir                DIR              where the generated ssh config and known_hosts live │
│                                              (default ~/.podbench)                               │
│ --help                                       Show this message and exit.                         │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Notes:

* `pod` accepts `pod/NAME`, a bare `NAME`, a substring of one, or nothing at
  all — see {ref}`Naming the pod <naming-the-pod>`.
* `--image` has no fixed default to print: the launcher asks for the image
  built from its own version — `ghcr.io/gilesknap/podbench:<launcher version>`,
  and `:main` when the launcher is a dev build off a checkout. `--image` wins
  over `PODBENCH_IMAGE`, which wins over that. See
  [The container image](../how-to/run-container.md).
* Re-running `attach` **reconnects** to a running seat. `--new` appends another
  ephemeral container, whose name is then burnt for the pod's lifetime.
* `--target-uid` matters only for the degraded rung, which must match the
  target's UID exactly and never defaults to root.
* `--mount` is how a seat reaches a Patch-mode claim. An ephemeral container may
  mount the volumes its pod **already declares** and may not introduce one —
  `spec.volumes` is immutable once the pod exists — so a name the pod does not
  carry is refused with that explanation rather than submitted. That immutability
  is the whole reason Patch mode asks for the chart's cooperation at deploy time;
  `podbench patch --print-values` emits the volume, the volumeMount and the
  seeding initContainer that put it there.
  * The argument is a **claim** name or the pod's **volume** name; a claim is
    resolved to the volume entry that references it.
  * `MOUNTPATH` is optional and usually should be. Where the application
    container mounts that volume, its mountPath is copied, because Patch mode
    only works when the claim resolves at the *same* path on both sides — the
    venv's `bin/python` and the checkout's editable install are absolute paths
    recorded on the volume. An explicit path that disagrees is honoured and
    warned about; a volume the application does not mount has no path to copy,
    so one must be given.
  * An application mount that uses a **`subPath` is refused**, before anything
    is submitted. An ephemeral container's volumeMounts may not carry one — the
    API server answers `Forbidden: cannot be set for an Ephemeral Container` and
    rejects the whole request — and dropping it silently would give the seat the
    volume root where the application sees one directory inside it, so every
    path Patch mode recorded would resolve to the wrong thing. Deploy the claim
    mounted whole over the venv path, or use `podbench dev`, whose seat is an
    ordinary container.
  * Mounts are fixed when a container is created, so `--mount` against a
    reconnect warns and does nothing. Use `--new` for a seat with a new mount.
* **The seat's home is mounted by convention, not by flag.** If the pod declares
  a volume named `podbench-home`, `attach` mounts it read-write at
  `/home/podbench` and makes it the seat's `$HOME`, which keeps vscode-server and
  everything else the seat writes off the workload's ephemeral-storage budget.
  * It is a convention because the volume cannot be there by accident: an
    ephemeral container may only mount volumes the pod already declares and
    `spec.volumes` is immutable, so anything called `podbench-home` was put in
    the pod at deploy time on purpose.
  * It needs the pod to set `fsGroup` to the application's gid, or it arrives
    owned by `root:root` and the seat cannot write to it. The agent reports that
    by name at start-up.
  * An explicit `--mount` for the same mountPath **wins** over the convention.
    `--no-seat-identity` turns the convention off.
* **`attach` cannot mount `podbench-identity`, however plainly the pod declares
  it.** The identity has to land as two *files* — `passwd` over `/etc/passwd`,
  `group` over `/etc/group` — and one file at a time takes a `subPath` per
  mount, which an ephemeral container may not have: the API server answers
  `spec.ephemeralContainers[0].volumeMounts[0].subPath: Forbidden: cannot be set
  for an Ephemeral Container` and refuses the *whole* request, so no seat lands
  at all. Mounting the volume whole is not an alternative either; a directory
  mount replaces the path, and over `/etc` it would take `nsswitch.conf` with it
  — the very lookup the identity exists to satisfy.
  * **On a live pod, `--seat-gid-root` is the route to the same identity.** The
    debug image makes `/etc/passwd` group-writable (OpenShift's convention) and
    the agent appends a record for whatever uid the seat turned out to run as,
    which needs `runAsGroup: 0` and nothing else. Verified against a
    PSA-`restricted` pod: `attach --no-seat-identity --seat-gid-root` landed the
    degraded rung and ssh logged in as `uid=1000(podbench)`.
  * The volume is for a seat that is an **ordinary** container, which is what
    `podbench dev` authors — `subPath` is legal there and nothing is written at
    runtime. (The dev sidecar does not mount it yet; see the follow-up note in
    `Charts/podbench/values.yaml`.)
  * The capability report says so where it matters: when the pod declares the
    volume, the `ssh seat` line explains that it cannot be projected into an
    ephemeral container and names `--seat-gid-root`. Where a seat *does* carry
    the identity, the same line credits it.
* `--resize` is opt-in and lightly proven; it prints a warning either way and
  needs `pods/resize` `patch`.
* `--seat-gid-root` is **the** way to an ssh-able seat on a live pod, not a
  fallback from the identity volume: GID 0 lets the agent append its own
  `/etc/passwd` record (the image makes the file group-writable for it), at the
  cost of the target's own group. It is opt-in for that cost, not because a
  cluster would refuse it — the restricted Pod Security Standard does not
  constrain `runAsGroup`.
* Exit code is `0` for any seat that lands, including a degraded one; `2` for a
  real error.

### `ssh-config`

Regenerate the ssh stanza for a seat that is already running, without touching
the pod.

```

 Usage: podbench ssh-config [OPTIONS] [POD]

 regenerate the ssh stanza for an existing session

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   POD      <str>  pod/NAME, a bare NAME, or any substring of one. Omitted, or matching more than │
│                   one pod, lists the namespace and asks                                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --identity              KEY        ssh key to authorise in the seat and name in the generated    │
│                                    stanza                                                        │
│                                    [default: ~/.ssh/id_ed25519]                                  │
│ --ssh-user              NAME       login name to put in the stanza                               │
│ --host-alias            NAME       ssh Host name for the seat                                    │
│ --print-config                     print the ssh stanza instead of writing it to the config dir  │
│ --no-prompt                        never ask which pod: an ambiguous or missing POD is refused   │
│                                    with the candidates instead. Already implied when stdin is    │
│                                    not a tty                                                     │
│ --namespace     -n      NAMESPACE  namespace (default: the kubeconfig context's own)             │
│ --context               NAME       kubeconfig context                                            │
│ --kubectl               BIN        kubectl binary to use [default: kubectl]                      │
│ --config-dir            DIR        where the generated ssh config and known_hosts live (default  │
│                                    ~/.podbench)                                                  │
│ --help                             Show this message and exit.                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Fails if there is no running podbench container in the pod.

### `status`

Every podbench container in one pod, including dead ones whose names remain
burnt.

```

 Usage: podbench status [OPTIONS] [POD]

 the podbench containers in one pod and what each supports

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   POD      <str>  pod/NAME, a bare NAME, or any substring of one. Omitted, or matching more than │
│                   one pod, lists the namespace and asks                                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --no-prompt                      never ask which pod: an ambiguous or missing POD is refused     │
│                                  with the candidates instead. Already implied when stdin is not  │
│                                  a tty                                                           │
│ --namespace   -n      NAMESPACE  namespace (default: the kubeconfig context's own)               │
│ --context             NAME       kubeconfig context                                              │
│ --kubectl             BIN        kubectl binary to use [default: kubectl]                        │
│ --config-dir          DIR        where the generated ssh config and known_hosts live (default    │
│                                  ~/.podbench)                                                    │
│ --help                           Show this message and exit.                                     │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

### `list`

The same, across the namespace.

```

 Usage: podbench list [OPTIONS]

 every pod in the namespace carrying a podbench container

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --namespace   -n      NAMESPACE  namespace (default: the kubeconfig context's own)               │
│ --context             NAME       kubeconfig context                                              │
│ --kubectl             BIN        kubectl binary to use [default: kubectl]                        │
│ --config-dir          DIR        where the generated ssh config and known_hosts live (default    │
│                                  ~/.podbench)                                                    │
│ --help                           Show this message and exit.                                     │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

### `dev`

Author a sacrificial dev pod from a target's spec — Iterate mode.

```

 Usage: podbench dev [OPTIONS] {POD}

 create or delete the dev pod (runs on the laptop)

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    POD      <str>  the pod to clone, or the dev pod to delete [required]                       │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --namespace     -n      NAMESPACE  namespace [default: default]                                  │
│ --context               NAME       kubeconfig context                                            │
│ --container             NAME       container to take over                                        │
│ --name                  NAME       dev pod name (default: POD-podbench)                          │
│ --image                 REF        podbench image (default: the image built from this launcher's │
│                                    version)                                                      │
│ --port                  PORT       the port your app serves                                      │
│ --take-traffic                     copy the origin's labels so the dev pod shares Service        │
│                                    traffic with it. Off by default: joining a production Service │
│                                    silently is a foot-cannon                                     │
│ --cutover               SERVICE    point SERVICE exclusively at the dev pod, recording its       │
│                                    selector for an exact restore at teardown                     │
│ --identity              KEY        ssh key to authorise in the sidecar and name in the generated │
│                                    stanza                                                        │
│                                    [default: ~/.ssh/id_ed25519]                                  │
│ --config-dir            DIR        where the generated ssh config and known_hosts live (default  │
│                                    ~/.podbench)                                                  │
│ --host-alias            NAME       ssh Host name for the sidecar                                 │
│ --delete                           tear the dev pod down                                         │
│ --timeout               SECONDS    seconds to wait [default: 120.0]                              │
│ --dry-run                          print the authored pod instead of creating it                 │
│ --help                             Show this message and exit.                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Notes:

* `pod` accepts `pod/NAME` or a bare `NAME`, the same as the launcher verbs —
  it is the same helper, so the two halves of the CLI cannot drift apart.
* The namespace defaults to `default` here, not to your current context's
  namespace as the launcher verbs do. Pass `-n` explicitly.
* The origin pod is never modified.
* `--take-traffic` and `--cutover` are the only ways the dev pod sees Service
  traffic, and both are explicit. `--cutover` uses a JSON *replace* patch — a
  merge patch would union the selector maps and quietly leave the original pod
  serving half the requests.
* `--identity` is authorised inside the sidecar and named as the stanza's
  `IdentityFile`, exactly as for `attach` — same flag, same default, same
  refusal when the public key is missing. It is read **before** anything is
  created, because the key reaches the sidecar through its environment and a
  container's environment cannot be changed after the pod exists.
* The generated stanza is written to the same `config.d` file `attach` would
  use for that pod, and the summary ends with the alias to `ssh`. The
  `kubectl exec` line is printed as well: it works when ssh does not.
* `--delete` restores any borrowed selector, removes the pod, then removes the
  stanza and the `known_hosts` entry it wrote. `attach` deliberately leaves its
  stanza in place — that seat is reconnectable while its pod lives, this one is
  not.
* `--dry-run` is the best available description of what this mode does. It
  still needs a readable public key, so that what it prints is what `dev` would
  actually create.

### `patch`

Durable in-place fixes: a venv on a ReadWriteOnce claim, every change a git
commit, and a `status` that will not let a patched pod go unnoticed.

:::{warning}
Patch mode has never been run against a cluster. It is unit-tested only.
:::

The seat must mount the claim at the application's own mountPath, since that is
how `patch` reads `pyvenv.cfg` and runs `git` against the checkout. Land it that
way with `attach --mount`:

```
podbench attach myapp-0 --mount myapp-venv --new
```

`--local` remains the alternative when `patch` is run from a terminal inside the
seat, where the claim is already in this process's own mount namespace.

```

 Usage: podbench patch [OPTIONS] COMMAND [ARGS]...

 Durable in-place fixes: a venv on a claim, every change a commit, and a status command that will
 not let a patched pod go unnoticed.

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --print-values              emit the helm values an application's chart needs, and exit          │
│ --app                 NAME  application name, for --print-values                                 │
│ --venv-path           PATH  the application's venv path, for --print-values                      │
│ --size                SIZE  claim size, for --print-values [default: 2Gi]                        │
│ --app-image           REF   image the seeding initContainer runs, for --print-values             │
│                             [default: <the application's own image>]                             │
│ --uid                 UID   the application container's uid, for --print-values                  │
│                             [default: <the application's runAsUser>]                             │
│ --gid                 GID   the application container's gid, for --print-values                  │
│                             [default: <the application's runAsGroup>]                            │
│ --help                      Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────────────────╮
│ init         verify the seeded claim, clone the source, editable-install                         │
│ apply        commit the change on the claim and roll the workload                                │
│ status       every patched pod in the namespace, and its drift                                   │
│ consolidate  push the claim's checkout as a branch for the rebuild                               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

| Sub-verb | Does |
|---|---|
| `init --repo URL --venv PATH TARGET` | verify the claim was seeded from the image's venv, clone the source onto it, editable-install, record the base commit |
| `apply -m MSG --venv PATH TARGET` | commit the checkout, reinstall if packaging metadata changed, write the manifest, annotate, roll the workload |
| `status` | every patched pod in the namespace, its drift, and what is wrong with it |
| `consolidate --branch B --venv PATH TARGET` | push the checkout as a branch and print the retirement checklist |

`TARGET` is `pod/NAME`, `deployment/NAME` or `statefulset/NAME`. Shared flags:
`--venv` (the mountPath the claim is mounted at, which *is* the application's
venv path), `--container`, `--seat`, `--local`, `--author`.

Notes:

* **Single replica only**, refused otherwise: the claim is `ReadWriteOnce`, so a
  second replica either fails to schedule or races on one checkout.
* `init` **verifies** the seed, never performs it. Once the claim is mounted over
  the venv path the image's own venv is hidden in every container, so the copy
  can only happen in an initContainer — which is what `--print-values` emits.
* The editable install runs in the **application** container, not the seat: the
  venv is shared but its interpreter is not. `--no-install` skips it.
* `consolidate` does not open a PR; it prints the `gh pr create` line.
* `status` exits **1** when any pod needs attention, so "no unretired patches" is
  a testable shutdown assertion.

```
$ podbench patch --print-values --app myapp --venv-path /opt/venv
```

emits both halves of the chart wiring: `patchVenv` values for the podbench
release, and the volume, volumeMount and seeding initContainer for the
application's own chart.

---

## In-pod verbs

### `capreport`

Name the mechanism that denies ptrace in this container. The launcher runs it
automatically after every attach; run it yourself when something changes.

```

 Usage: capreport [OPTIONS] [PID]

 Name the mechanism that denies ptrace in this container.

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   [PID]      <int>  target pid; discovered from the target container id if omitted               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --container-id        ID  target container id (default: $PODBENCH_TARGET_CID)                    │
│ --json                    emit the stable JSON form instead of the human report                  │
│ --help                    Show this message and exit.                                            │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

**Exit codes are the interface**, so a shell script can branch without parsing:

| Code | Verdict |
|---|---|
| `0` | live attach available |
| `10` | read-only debugging available (target rootfs, `maps`, `environ`; gdb-launch works) |
| `20` | neither; the seat itself still works |

It reads `CapEff`/`CapBnd`/`CapAmb`, `Seccomp`, `NoNewPrivs`, the AppArmor
profile of both itself and the target, and `yama/ptrace_scope`; then runs a
scratch `PTRACE_ATTACH` on its own forked child (always permitted by Yama, so a
failure there is structural) and a live attach on the target; then a six-path
`/proc` read matrix. Yama is a **node-level** knob that differs by kernel
flavour, so this must be re-run per pod and never cached cluster-wide.

### `pids`

List the processes in the pod's shared PID namespace and say which container
owns each.

```

 Usage: podbench pids [OPTIONS]

 List the processes in this pod's shared PID namespace, and say which container owns each one.

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --container-id        ID  target container id (default: $PODBENCH_TARGET_CID)                    │
│ --targets                 list only the target container's processes                             │
│ --json                    emit the stable JSON form instead of the table                         │
│ --help                    Show this message and exit.                                            │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Attribution substring-matches the target's container runtime ID against
`/proc/<pid>/cgroup`. Without one, every other container's processes look like
targets — the JSON carries `attribution` and `warning` fields, and a consumer
that ignores them is reading a guess as a fact.

### `dbg`

gdb, with sysroot, source path and auto-load path set in the one order that
produces a correct backtrace.

```

 Usage: podbench dbg [OPTIONS] [PID]

 Run gdb against a process in another container of this pod, with the sysroot, source path and
 auto-load path set in the order that produces a correct backtrace.

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   [PID]      <int>  pid to attach to; discovered from the container id if omitted                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --container-id                    ID       target container id used to discover the pid          │
│                                            (default: $PODBENCH_TARGET_CID)                       │
│ --source-dir                      DIR      extra source directory, wired with gdb's `directory`. │
│                                            debuginfod serves symbols but no sources on Debian,   │
│                                            so this is how source text outside the target's       │
│                                            rootfs is found. Repeatable                           │
│ --no-debuginfod                            do not enable debuginfod (it needs ca-certificates    │
│                                            and network)                                          │
│ --run                                      with --launch, start the program immediately          │
│ --dry-run,--print-commands                 print the generated gdb commands and exit, without    │
│                                            probing or starting gdb                               │
│ --launch                          PROGRAM  debug a program gdb starts itself instead of          │
│                                            attaching. Needs no capability. Consumes the rest of  │
│                                            the command line, so put other flags first            │
│ --help                                     Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

`--launch` consumes the remainder of the command line, so any other flag must
come first. See [Debug with gdb](../how-to/debug-with-gdb.md).

### `debug-config`

The VS Code debug configuration for this seat, written the way `attach` writes
the ssh stanza — so nobody hand-fills a pid, a sysroot-prefixed `program` or a
setup ordering, each of which fails *silently* when wrong.

```
                                                                                                    
 Usage: debug-config [OPTIONS] [PID]                                                                
                                                                                                    
 Write the VS Code debug configuration for this seat, with the pid, the sysroot-prefixed program    
 path and the gdb setup order already filled in.                                                    
                                                                                                    
╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   [PID]      <int>  pid to attach to; discovered from the container id if omitted                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --container-id         ID       target container id used to discover the pid (default:           │
│                                 $PODBENCH_TARGET_CID)                                            │
│ --program              PATH     the target's binary as its own rootfs spells it, when            │
│                                 /proc/<pid>/exe cannot be read. It is prefixed with the sysroot  │
│                                 here, so do not prefix it yourself                               │
│ --source-dir           DIR      extra source directory in *this* container, wired with gdb's     │
│                                 `directory`. Repeatable                                          │
│ --source-map           FROM=TO  map a DWARF compilation directory (`info source` prints it) onto │
│                                 a readable path. Repeatable                                      │
│ --no-debuginfod                 do not enable debuginfod (it needs ca-certificates and network)  │
│ --lldb                          emit a CodeLLDB configuration instead of cpptools' cppdbg        │
│ --print-config                  print the configuration instead of writing it                    │
│ --output               PATH     where to write it (default: ./.vscode/launch.json)               │
│ --help                          Show this message and exit.                                      │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

`miDebuggerPath` names `/usr/local/bin/gdb-podbench`, never `/usr/bin/gdb`:
cpptools launches gdb inheriting its own extension directory as a working
directory, which VS Code deletes on extension update, and gdb's libpython then
dies in `getcwd()` during startup with no signal name. `--source-map /` is
refused rather than emitted — gdb re-applies a root substitution on display and
the editor is handed `/proc/<pid>/root/proc/<pid>/root/...`.

Re-running replaces its own entry by name and leaves a hand-written
configuration beside it untouched. A `launch.json` it cannot parse — VS Code
permits comments, `json` does not — is refused rather than rewritten. See
[Debug with gdb](../how-to/debug-with-gdb.md).

### `dev-bootstrap`

Populate the dev pod's workspace: clone, sync, editable install.

```

 Usage: podbench dev-bootstrap [OPTIONS]

 clone, sync and editable-install (runs in the pod)

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ *  --repo               URL      git URL to clone [required]                                     │
│    --ref                REF      branch, tag or commit to check out                              │
│    --dir                DIR      checkout directory (must be in this container)                  │
│                                  [default: /workspace/src]                                       │
│    --python             VERSION  CPython version for uv to use                                   │
│    --no-sync                     skip uv sync --frozen                                           │
│    --no-editable                 skip uv pip install -e .                                        │
│    --help                        Show this message and exit.                                     │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

"must be in this container" is enforced, not advisory: a checkout under
`/proc/<pid>/root/...` is refused, because an editable install whose `.pth`
names a path in another mount namespace is **silently ignored** by `site.py`.

### `run`

Relaunch the workload from the debug container and verify that your child owns
the port.

```

 Usage: podbench run [OPTIONS] [COMMAND]...

 relaunch the app and verify it (runs in the pod)

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   [COMMAND]...      <str>  the command, after `--`                                               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ *  --port             PORT     the port it must serve [required]                                 │
│    --workspace        DIR      workspace root [default: /workspace]                              │
│    --dir              DIR      working directory (default: workspace)                            │
│    --timeout          SECONDS  seconds to verify [default: 15.0]                                 │
│    --help                      Show this message and exit.                                       │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Installed on `PATH` as `podbench-run`. Exits non-zero when the port is not owned
by the process it started — a socket poll alone gives a false PASS, and
`SO_REUSEPORT` will otherwise split traffic between old and new code with
nothing in any log to say so.

### `stop`

Stop it, by recorded pid.

```

 Usage: podbench stop [OPTIONS]

 stop the recorded child (runs in the pod)

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --workspace        DIR      workspace root [default: /workspace]                                 │
│ --grace            SECONDS  seconds before SIGKILL [default: 5.0]                                │
│ --help                      Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Installed on `PATH` as `podbench-stop`. Never `pkill -f`: under
`shareProcessNamespace: true` that matches the invoking shell and every other
container's processes.

### `agent`

The debug container's PID 1. The launcher sets it as the container's command;
you should not need to run it yourself.

```

 Usage: podbench agent [OPTIONS]

 Prepare the debug container for ssh and idle as its PID 1.

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --ensure-only                      prepare the container and exit instead of idling              │
│ --self-check                       run the startup checks and exit; non-zero if any fails        │
│ --print-host-key                   print the host public key for the launcher's known_hosts      │
│ --print-login-user                 print the login name sshd will resolve for this uid; non-zero │
│                                    with the reason on stderr when there is none                  │
│ --no-self-check                    skip the startup checks (they cost a subprocess and ~0.2 s)   │
│ --idle-interval           SECONDS  seconds between reap sweeps while idling [default: 30.0]      │
│ --help                             Show this message and exit.                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Every step is *ensure*, never *create*: running it twice against the same
container is normal operation. The host key, the authorized keys and the sshd
config are rebuilt from the environment or a mounted Secret on each start, which
is what makes "the ephemeral container is strictly disposable" true rather than
aspirational.

No step is fatal either. PID 1 of an unrestartable container that exits burns its
name for the pod's lifetime, so a step that cannot do its job records the reason
and the agent idles anyway — `kubectl exec` needs none of sshd. Two steps are
worth knowing by name:

* **home-dir** creates `$HOME` and the `.ssh` / `.podbench` directories in it. A
  mounted `podbench-home` arrives *empty*, and sshd creates nothing. If the
  directory is not writable the failure names `fsGroup`, which is almost always
  the cause: a projected volume is `root:root` until the pod's `fsGroup` hands it
  to the seat's group, and a seat running as the target's uid can chown nothing.
* **nss-identity** is a no-op when NSS already resolves the seat's uid — what a
  mounted `podbench-identity` achieves for an ordinary container, and it stays a
  no-op even though the projected `/etc/passwd` is read-only. In an ephemeral
  seat, which cannot be given that file at all, it appends one instead, and that
  needs GID 0 (`attach --seat-gid-root`).

`--print-login-user` is how the launcher decides whether an ssh stanza is worth
writing: the name on stdout, or exit 1 with the mechanism and the way out on
stderr. It is a pure read and ensures nothing, so it reports the state sshd will
actually find.

`--self-check` includes the fd-2 tripwire — a `kubectl exec` round trip with a
delayed second line, which fails if anything in the path has broken the CRI exec
stream.

---

## Environment variables

| Variable | Read by | Meaning |
|---|---|---|
| `PODBENCH_IMAGE` | launcher | debug image to attach; `--image` overrides. Both override the default, which is `ghcr.io/gilesknap/podbench:` plus the launcher's own version (`main` for a dev build) |
| `PODBENCH_CONFIG_DIR` | launcher, `dev` | where the ssh config and `known_hosts` go; `--config-dir` overrides. Default `~/.podbench` |
| `PODBENCH_TARGET_CID` | `pids`, `dbg`, `capreport`, `run` | the target container's runtime ID, injected at attach time |
| `PODBENCH_SSH_PUBKEY` | agent | authorized key, injected into the seat's spec by `attach` and by `dev` |
| `PODBENCH_SSH_PUBKEY_FILE` | agent | read it from a file instead. Default mount `/etc/podbench/ssh/authorized_keys` |
| `PODBENCH_SSH_HOST_KEY` | agent | host private key, rather than minting one |
| `PODBENCH_SSH_HOST_KEY_FILE` | agent | the same from a file. Default mount `/etc/podbench/ssh/ssh_host_ed25519_key` |
| `DEBUGINFOD_URLS` | gdb, `dbg` | symbol server. The image sets `https://debuginfod.debian.net` |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success — including a degraded seat, which is an honest outcome and not a failure |
| `1` | an Iterate-mode operation failed (`dev`, `dev-bootstrap`, `run`, `stop`); or `patch status` found a pod needing attention |
| `2` | a launcher error, a `patch` error, an unanswerable `POD` (see {ref}`Naming the pod <naming-the-pod>`), or `podbench` with no verb |
| `0` / `10` / `20` | `capreport` only: the capability verdict |
