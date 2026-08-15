# Command-line reference

One binary serves both halves of podbench. On your machine it is reached as
`kubectl podbench <verb>` (or `podbench <verb>`); inside the debug container the
same binary is PID 1 and backs the helpers on `PATH`. Keeping it as one package
means the capability logic that decides what a session can do is the same code
in both places, rather than a launcher's guess and a helper's separate guess.

```
$ podbench --help
usage: podbench [-h] [-v] [VERB] ...

A development seat inside a Kubernetes pod. Run `podbench <verb> --help` for a
verb's own options.

positional arguments:
  VERB           the subcommand to run
  args           arguments for the verb

options:
  -h, --help     show this help message and exit
  -v, --version  show program's version number and exit
```

| Where it runs | Verbs |
|---|---|
| Your machine | `attach`, `ssh-config`, `status`, `list`, `dev`, `patch` |
| Inside the debug container | `agent`, `capreport`, `pids`, `dbg`, `dev-bootstrap`, `run`, `stop` |

The `kubectl podbench` plugin currently routes `attach`, `ssh-config`, `status`
and `list`. Reach `dev` and `patch` as **`podbench dev`** / **`podbench patch`**
— the same binary, the same arguments.

The in-pod verbs are also reachable as `podbench <verb>` from a terminal in the
seat; several have shorter aliases on `PATH` (`pids`, `dbg`, `capreport`,
`dev-bootstrap`, `podbench-run`, `podbench-stop`).

## Common options

The four launcher verbs — `attach`, `ssh-config`, `status`, `list` — take these:

```
  -n NAMESPACE, --namespace NAMESPACE
  --context CONTEXT
  --kubectl KUBECTL     kubectl binary to use
  --config-dir CONFIG_DIR
                        where the generated ssh config and known_hosts live
                        (default ~/.podbench)
```

`dev` takes only `-n`/`--namespace` and `--context`; `patch` takes those plus
`--kubectl`. Neither writes an ssh config, so neither has `--config-dir`.

podbench shells out to `kubectl` deliberately, so it inherits your kubeconfig,
your current context and any exec credential plugin. There is no second
credential and no client library.

---

## Cluster-side verbs

### `attach`

Land a debug seat in a **live** pod, walking the capability ladder, and print
what that seat can actually do.

```
usage: kubectl podbench attach [-h] [--target TARGET] [--image IMAGE]
                               [--target-uid TARGET_UID]
                               [--mount CLAIM:MOUNTPATH] [--new] [--no-probe]
                               [--resize MEMORY] [--identity IDENTITY]
                               [--ssh-user SSH_USER] [--host-alias HOST_ALIAS]
                               [--print-config] [--timeout TIMEOUT]
                               [-n NAMESPACE] [--context CONTEXT]
                               [--kubectl KUBECTL] [--config-dir CONFIG_DIR]
                               pod

positional arguments:
  pod

options:
  -h, --help            show this help message and exit
  --target TARGET       workload container name
  --image IMAGE
  --target-uid TARGET_UID
                        the target's uid, when its pod spec does not say
  --mount CLAIM:MOUNTPATH
                        mount a volume the pod already declares into the seat,
                        named by claim or by volume name. MOUNTPATH defaults to
                        the application container's own, which Patch mode
                        requires it to equal. Repeatable
  --new                 add a container even if one is running (its name is
                        permanent)
  --no-probe            skip capreport; the report then says nothing was
                        measured
  --resize MEMORY       raise the target's memory limit in place first, e.g.
                        6Gi
  --identity IDENTITY
  --ssh-user SSH_USER
  --host-alias HOST_ALIAS
  --print-config        print the ssh stanza instead of writing it to the
                        config dir
  --timeout TIMEOUT
```

Notes:

* `pod` accepts `pod/NAME` or a bare `NAME`.
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
    container mounts that volume, its mountPath (and any `subPath`) is copied,
    because Patch mode only works when the claim resolves at the *same* path on
    both sides — the venv's `bin/python` and the checkout's editable install are
    absolute paths recorded on the volume. An explicit path that disagrees is
    honoured and warned about; a volume the application does not mount has no
    path to copy, so one must be given.
  * Mounts are fixed when a container is created, so `--mount` against a
    reconnect warns and does nothing. Use `--new` for a seat with a new mount.
* `--resize` is opt-in and lightly proven; it prints a warning either way and
  needs `pods/resize` `patch`.
* Exit code is `0` for any seat that lands, including a degraded one; `2` for a
  real error.

### `ssh-config`

Regenerate the ssh stanza for a seat that is already running, without touching
the pod.

```
usage: kubectl podbench ssh-config [-h] [--identity IDENTITY]
                                   [--ssh-user SSH_USER]
                                   [--host-alias HOST_ALIAS] [--print-config]
                                   [-n NAMESPACE] [--context CONTEXT]
                                   [--kubectl KUBECTL]
                                   [--config-dir CONFIG_DIR]
                                   pod
```

Fails if there is no running podbench container in the pod.

### `status`

Every podbench container in one pod, including dead ones whose names remain
burnt.

```
usage: kubectl podbench status [-h] [-n NAMESPACE] [--context CONTEXT]
                               [--kubectl KUBECTL] [--config-dir CONFIG_DIR]
                               pod
```

### `list`

The same, across the namespace.

```
usage: kubectl podbench list [-h] [-n NAMESPACE] [--context CONTEXT]
                             [--kubectl KUBECTL] [--config-dir CONFIG_DIR]
```

### `dev`

Author a sacrificial dev pod from a target's spec — Iterate mode.

```
usage: podbench dev [-h] [-n NAMESPACE] [--context CONTEXT]
                    [--container CONTAINER] [--name NAME] [--image IMAGE]
                    [--port PORT] [--take-traffic] [--cutover SERVICE]
                    [--delete] [--timeout TIMEOUT] [--dry-run]
                    pod

positional arguments:
  pod                   the pod to clone, or the dev pod to delete

options:
  -h, --help            show this help message and exit
  -n NAMESPACE, --namespace NAMESPACE
                        namespace (default: default)
  --context CONTEXT     kubeconfig context
  --container CONTAINER
                        container to take over
  --name NAME           dev pod name (default: <pod>-podbench)
  --image IMAGE         podbench image
  --port PORT           the port your app serves
  --take-traffic        copy the origin's labels so the dev pod shares Service
                        traffic with it. Off by default: joining a production
                        Service silently is a foot-cannon
  --cutover SERVICE     point SERVICE exclusively at the dev pod, recording
                        its selector for an exact restore at teardown
  --delete              tear the dev pod down
  --timeout TIMEOUT     seconds to wait
  --dry-run             print the authored pod instead of creating it
```

Notes:

* Reached as `podbench dev`; the kubectl plugin does not route this verb yet.
* `pod` accepts `pod/NAME` or a bare `NAME`, the same as the launcher verbs —
  it is the same helper, so the two halves of the CLI cannot drift apart.
* The namespace defaults to `default` here, not to your current context's
  namespace as the launcher verbs do. Pass `-n` explicitly.
* The origin pod is never modified.
* `--take-traffic` and `--cutover` are the only ways the dev pod sees Service
  traffic, and both are explicit. `--cutover` uses a JSON *replace* patch — a
  merge patch would union the selector maps and quietly leave the original pod
  serving half the requests.
* `--delete` restores any borrowed selector before removing the pod.
* `--dry-run` is the best available description of what this mode does.

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
kubectl podbench attach myapp-0 --mount myapp-venv --new
```

`--local` remains the alternative when `patch` is run from a terminal inside the
seat, where the claim is already in this process's own mount namespace.

```
usage: podbench patch [-h] [--print-values] [--app APP]
                      [--venv-path VENV_PATH] [--size SIZE]
                      [--app-image APP_IMAGE]
                      {init,apply,status,consolidate} ...
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
usage: capreport [-h] [--container-id CONTAINER_ID] [--json] [pid]

Name the mechanism that denies ptrace in this container.

positional arguments:
  pid                   target pid; discovered from the target container id if
                        omitted

options:
  -h, --help            show this help message and exit
  --container-id CONTAINER_ID
                        target container id (default: $PODBENCH_TARGET_CID)
  --json                emit the stable JSON form instead of the human report
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
usage: podbench pids [-h] [--container-id CONTAINER_ID] [--targets] [--json]

List the processes in this pod's shared PID namespace, and say which container
owns each one.

options:
  -h, --help            show this help message and exit
  --container-id CONTAINER_ID
                        target container id (default: $PODBENCH_TARGET_CID)
  --targets             list only the target container's processes
  --json                emit the stable JSON form instead of the table
```

Attribution substring-matches the target's container runtime ID against
`/proc/<pid>/cgroup`. Without one, every other container's processes look like
targets — the JSON carries `attribution` and `warning` fields, and a consumer
that ignores them is reading a guess as a fact.

### `dbg`

gdb, with sysroot, source path and auto-load path set in the one order that
produces a correct backtrace.

```
usage: podbench dbg [-h] [--container-id CONTAINER_ID] [--source-dir DIR]
                    [--no-debuginfod] [--run] [--dry-run] [--launch ...]
                    [pid]

Run gdb against a process in another container of this pod, with the sysroot,
source path and auto-load path set in the order that produces a correct
backtrace.

positional arguments:
  pid                   pid to attach to; discovered from the container id if
                        omitted

options:
  -h, --help            show this help message and exit
  --container-id CONTAINER_ID
                        target container id used to discover the pid (default:
                        $PODBENCH_TARGET_CID)
  --source-dir DIR      extra source directory, wired with gdb's `directory`.
                        debuginfod serves symbols but no sources on Debian, so
                        this is how source text outside the target's rootfs is
                        found. Repeatable.
  --no-debuginfod       do not enable debuginfod (it needs ca-certificates and
                        network)
  --run                 with --launch, start the program immediately
  --dry-run, --print-commands
                        print the generated gdb commands and exit, without
                        probing or starting gdb
  --launch ...          debug a program gdb starts itself instead of
                        attaching. Needs no capability. Consumes the rest of
                        the command line, so put other flags first.
```

`--launch` consumes the remainder of the command line, so any other flag must
come first. See [Debug with gdb](../how-to/debug-with-gdb.md).

### `dev-bootstrap`

Populate the dev pod's workspace: clone, sync, editable install.

```
usage: podbench dev-bootstrap [-h] --repo REPO [--ref REF] [--dir DIR]
                              [--python PYTHON] [--no-sync] [--no-editable]

options:
  -h, --help       show this help message and exit
  --repo REPO      git URL to clone
  --ref REF        branch, tag or commit to check out
  --dir DIR        checkout directory (must be in this container)
  --python PYTHON  CPython version for uv to use
  --no-sync        skip uv sync --frozen
  --no-editable    skip uv pip install -e .
```

"must be in this container" is enforced, not advisory: a checkout under
`/proc/<pid>/root/...` is refused, because an editable install whose `.pth`
names a path in another mount namespace is **silently ignored** by `site.py`.

### `run`

Relaunch the workload from the debug container and verify that your child owns
the port.

```
usage: podbench run [-h] --port PORT [--workspace WORKSPACE] [--dir DIR]
                    [--timeout TIMEOUT]
                    [command ...]

positional arguments:
  command               the command, after `--`

options:
  -h, --help            show this help message and exit
  --port PORT           the port it must serve
  --workspace WORKSPACE
                        workspace root
  --dir DIR             working directory (default: workspace)
  --timeout TIMEOUT     seconds to verify
```

Installed on `PATH` as `podbench-run`. Exits non-zero when the port is not owned
by the process it started — a socket poll alone gives a false PASS, and
`SO_REUSEPORT` will otherwise split traffic between old and new code with
nothing in any log to say so.

### `stop`

Stop it, by recorded pid.

```
usage: podbench stop [-h] [--workspace WORKSPACE] [--grace GRACE]

options:
  -h, --help            show this help message and exit
  --workspace WORKSPACE
                        workspace root
  --grace GRACE         seconds before SIGKILL
```

Installed on `PATH` as `podbench-stop`. Never `pkill -f`: under
`shareProcessNamespace: true` that matches the invoking shell and every other
container's processes.

### `agent`

The debug container's PID 1. The launcher sets it as the container's command;
you should not need to run it yourself.

```
usage: podbench agent [-h] [--ensure-only] [--self-check] [--print-host-key]
                      [--no-self-check] [--idle-interval IDLE_INTERVAL]

Prepare the debug container for ssh and idle as its PID 1.

options:
  -h, --help            show this help message and exit
  --ensure-only         prepare the container and exit instead of idling
  --self-check          run the startup checks and exit; non-zero if any fails
  --print-host-key      print the host public key for the launcher's
                        known_hosts
  --no-self-check       skip the startup checks (they cost a subprocess and
                        ~0.2 s)
  --idle-interval IDLE_INTERVAL
                        seconds between reap sweeps while idling
```

Every step is *ensure*, never *create*: running it twice against the same
container is normal operation. The host key, the authorized keys and the sshd
config are rebuilt from the environment or a mounted Secret on each start, which
is what makes "the ephemeral container is strictly disposable" true rather than
aspirational.

`--self-check` includes the fd-2 tripwire — a `kubectl exec` round trip with a
delayed second line, which fails if anything in the path has broken the CRI exec
stream.

---

## Environment variables

| Variable | Read by | Meaning |
|---|---|---|
| `PODBENCH_IMAGE` | launcher | debug image to attach; `--image` overrides |
| `PODBENCH_CONFIG_DIR` | launcher | where the ssh config and `known_hosts` go; `--config-dir` overrides. Default `~/.podbench` |
| `PODBENCH_TARGET_CID` | `pids`, `dbg`, `capreport`, `run` | the target container's runtime ID, injected at attach time |
| `PODBENCH_SSH_PUBKEY` | agent | authorized key, injected by the launcher |
| `PODBENCH_SSH_PUBKEY_FILE` | agent | read it from a file instead. Default mount `/etc/podbench/ssh/authorized_keys` |
| `PODBENCH_SSH_HOST_KEY` | agent | host private key, rather than minting one |
| `PODBENCH_SSH_HOST_KEY_FILE` | agent | the same from a file. Default mount `/etc/podbench/ssh/ssh_host_ed25519_key` |
| `DEBUGINFOD_URLS` | gdb, `dbg` | symbol server. The image sets `https://debuginfod.debian.net` |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success — including a degraded seat, which is an honest outcome and not a failure |
| `1` | an Iterate-mode operation failed (`dev`, `dev-bootstrap`, `run`, `stop`); or `patch status` found a pod needing attention |
| `2` | a launcher error, a `patch` error, or `podbench` with no verb |
| `0` / `10` / `20` | `capreport` only: the capability verdict |
