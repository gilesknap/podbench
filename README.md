[![CI](https://github.com/gilesknap/podbench/actions/workflows/ci.yml/badge.svg)](https://github.com/gilesknap/podbench/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/gilesknap/podbench/branch/main/graph/badge.svg)](https://codecov.io/gh/gilesknap/podbench)

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# podbench

**A development seat — editor, gdb, a Python inner loop — inside a Kubernetes
pod, reached over nothing but your kubeconfig.**

podbench launches a debug container into a running pod (or into a sacrificial
clone of one), tunnels ssh through `kubectl exec`, and hands you the config
stanza that VS Code Remote-SSH needs. There is no port-forward to babysit, no
pod IP to reach, no file sync and no traffic interception: the editor is already
inside the cluster.

```
              laptop
     VS Code · Remote-SSH · kubectl
                 |
                 |  ssh over kubectl exec (API server only)
                 |
  +--------------+--------------------------------------+
  |  pod                                                 |
  |                                                      |
  |  +----------------------+  +----------------------+  |
  |  | app container        |  | podbench             |  |
  |  |                      |  |                      |  |
  |  | workload process     |  | sshd · vscode-server |  |
  |  |   (maybe distroless) |  | gdb · git · uv       |  |
  |  | fs visible at        |  |                      |  |
  |  |   /proc/<pid>/root   |  |                      |  |
  |  +----------------------+  +----------------------+  |
  |                                                      |
  |  shared: PID namespace (via the target)              |
  |          network namespace (always)                  |
  +------------------------------------------------------+
```

Two artefacts: a debug **container image** (`ghcr.io/gilesknap/podbench`) and a
**launcher you never have to install** — `uvx podbench` runs it straight from
PyPI, uses your kubeconfig, and puts nothing on your `PATH`.

| | Observe mode | Iterate mode |
|---|---|---|
| Command | `uvx podbench attach pod/foo` | `uvx podbench dev pod/foo` |
| What it does | adds an ephemeral container to the **live** pod | authors a **sacrificial clone** with the app idled and podbench as a real sidecar |
| Built for | distroless targets with no shell of their own; gdb against the running workload | edit → relaunch → see the change through the Service |
| Resources | shares the workload's limits, **cannot reserve its own** | has its own memory and ephemeral-storage requests |
| Risk to the workload | real — see below | none by default; the origin pod is never touched. `--cutover` moves Service traffic, and is opt-in |

## Run it

The launcher's **only runtime dependency is its CLI**, so `uvx` resolves and
runs it in one cold start, with nothing installed and nothing on your `PATH`
afterwards (uv does keep the environment in its own cache, so an unpinned
`uvx podbench` reuses that version until you ask for `podbench@latest`).
Everything it does to a cluster goes through `kubectl`; there is no client
library:

```
$ uvx podbench --version
$ uvx podbench list
no podbench containers in namespace default
```

**That will not resolve until the first PyPI release** — the name is not
published yet — so until then, run it from git instead, here and everywhere
below:

```
$ uvx --from git+https://github.com/gilesknap/podbench podbench list
```

Once published, pin it with `uvx podbench@1.0.0 <verb>`, or put it on `PATH`
permanently with `uv tool install podbench` if you would rather type `podbench`.
It is the same program either way.

Full prerequisites, cluster-side RBAC and the one-time ssh `Include` line — the
only thing that does outlive the command — are in the *Installation* tutorial.
`uvx podbench doctor` checks all three from your machine and names whatever is
missing; `--fix` adds the `Include`.

## Observe mode in one command

```
$ uvx podbench attach pod/web-7d9f8c5b4-x2k9p -n demo
```

podbench walks a capability ladder, lands the best seat the cluster will admit,
runs its probe **inside the container it just created** and prints what that
seat can actually do:

```
seat        demo/web-7d9f8c5b4-x2k9p[podbench-1]  (new)
target      web
rung        full - root + CAP_SYS_PTRACE (live attach)
ladder
  full      landed   admitted by the API server and the kubelet
supports
  [x] live attach (gdb -p <pid>)
  [x] read-only inspect (/proc/<pid>/root, maps, environ)
  [ ] iterate (edit, relaunch, verify through the Service)
      attach shares a live pod, where killing PID 1 restarts the container...
  [x] seat (editor, shell, git)
measured
  verdict     live attach available
  blocker     none
  node        node02
  yama        absent (no Yama LSM on this node - not the same as scope 0)
  uids        seat 0, target 0

ssh config written to ~/.podbench/config.d/demo-web-7d9f8c5b4-x2k9p.conf
add this to ~/.ssh/config once:  Include ~/.podbench/config.d/*.conf
or let podbench check and add it:  podbench doctor --fix
then:  ssh podbench-demo-web-7d9f8c5b4-x2k9p
```

Then **Remote-SSH: Connect to Host…** in VS Code and pick that alias.

## Iterate mode in one command

```
$ uvx podbench dev api-5f6c9b7d8-qz4tn -n demo --port 8080
$ kubectl -n demo exec -it api-5f6c9b7d8-qz4tn-podbench -c podbench -- bash
# podbench dev-bootstrap --repo https://github.com/you/api
# podbench run --port 8080 -- python -m api
```

Edit in VS Code, re-run `podbench run`, and the change is live on the pod IP.
Measured end to end: **1.18 s** per edit → relaunch → verified-through-the-Service
cycle.

---

## Read this before you attach to a live pod

These are not fine print. Each one has bitten a spike on a real cluster.

### 1. On a live pod, podbench can get your workload OOM-killed

**This is the biggest footgun.** An ephemeral container may not declare
`resources` at all — the field is rejected outright — so in Observe mode
podbench shares the workload's memory and ephemeral-storage limits and **cannot
reserve its own**. Measured working set of a VS Code session:

| | amd64 | arm64 |
|---|---|---|
| vscode-server, extracted | 680.8 MiB | 638.3 MiB |
| `ms-vscode.cpptools` | 330 MiB | 261 MiB |
| server idle RSS | ~97 MiB | ~92 MiB |
| **realistic footprint** | **1.1–1.3 GB of node disk** | |

Two ways that hurts:

* **Memory.** Exceed the pod's memory limit and the kernel OOM-kills something
  in the pod cgroup. It killed the ephemeral container in testing, but there is
  no guarantee it picks podbench rather than your workload — and **an OOM
  inside an ephemeral container is unrecoverable**: it cannot be restarted, and
  a replacement comes up with a completely fresh rootfs (server, extensions and
  host keys gone).
* **Ephemeral storage.** The debug container's writes count against the pod's
  ephemeral-storage limit. Exceed it and the kubelet **evicts the whole pod**,
  workload included. 2 GB is a realistic facility default; the numbers above
  fill most of it.

What to do about it:

* **Iterate mode is immune.** `podbench dev` authors the pod itself, so the
  sidecar carries its own memory and ephemeral-storage requests and a workspace
  volume. Anything heavier than looking belongs in a dev pod.
* **Observe mode manages it by staying slim** — a cockpit, not a workshop. Read
  the target's files through the sysroot, fetch symbols over debuginfod, install
  as few extensions as you can live with.
* **`attach --resize 6Gi`** raises the target container's memory limit in place
  before attaching. It is opt-in because it is only partly proven — three pods,
  two of them Deployment-managed, but one Kubernetes version and never against a
  `LimitRange` or a `ResourceQuota` — and because the raised limit lives on the
  pod, not on its controller: a rollout regenerates the pod from an unchanged
  template and silently reverts it.

### 2. `SYS_PTRACE` is outside the Pod Security Standards, so refusal is normal

gdb *attach* needs `CAP_SYS_PTRACE`, which is in neither the **baseline** nor
the **restricted** allowed-capability list. Being refused is a mainstream
scenario, not an error state, so podbench treats it as one: it walks a ladder
with exactly two valid rungs and lands the best one the cluster admits.

| Rung | Shape | What you get |
|---|---|---|
| **full** | `runAsUser: 0` **and** `capabilities.add: [SYS_PTRACE]` | live attach to the workload's processes |
| **degraded** | the **target's own UID**, `drop: [ALL]`, `runAsNonRoot`, `RuntimeDefault` seccomp | `/proc/<pid>/root`, `maps`, `environ`; full source-level debugging of processes gdb starts itself |
| *(seat)* | whatever is admitted | editor, shell, git |

There is deliberately no rung in between. `capabilities.add: [SYS_PTRACE]` on a
container with a non-zero `runAsUser` is a **silent no-op** — the capability
lands in the bounding set only, `CapEff` stays `0`, the pod is admitted, the
container runs and ptrace fails with a bare `EPERM`. podbench refuses to author
that combination rather than tell you that you have live attach when you do not.

Even on the full rung, attach can still be denied — by Yama's `ptrace_scope`,
by seccomp, or by AppArmor, all of which are **per node** and none of which can
be cached cluster-wide. All four subsystems refuse with the same `EPERM`, so
podbench probes and **names the blocker** instead of leaving you an errno.
See the *Security model* explanation for the RBAC verbs and the
admission-policy escape hatch.

### 3. Ephemeral containers are permanent

They cannot be removed, restarted or edited. Every attach appends a container to
the pod spec for the rest of the pod's life, and a name once used is **burnt**
until the pod dies. So:

* `attach` **reconnects** to a running podbench container by default. `--new`
  appends another one; use it only when you mean it.
* Nothing may live only in the debug container's writable layer. The host key,
  the authorized keys and the sshd config are rebuilt on every start, and
  re-bootstrapping after a pod restart (~6 s) is the documented reconnect path,
  not a surprise.
* A pod restart mints a **new ssh host key**. podbench manages its own
  `known_hosts` keyed on the pod UID rather than shipping
  `StrictHostKeyChecking no`.

### 4. A dev pod never joins a Service by accident

`podbench dev` does not copy the origin's Service-selector labels unless you
pass `--take-traffic`, and `--cutover` (which repoints a Service exclusively at
the dev pod) records the original selector for an exact restore. Silently
joining a production Service is a foot-cannon; it is always opt-in.

## What is proven, and what is not

Five spikes ran against a real 6-node k3s cluster and all passed; they are kept
verbatim in the docs, along with the
*Phase 0 gate report* that is the empirical basis for most of the non-obvious behaviour here. Where
the design brief and that report disagree, the report wins.

Known-unproven, stated plainly:

* **No real VS Code GUI client has connected yet.** The transport was verified
  at the protocol level (HTTP 200 + WebSocket `101` through `ssh -L`) and the
  server was driven headlessly. Every RSS figure above is a **lower bound**; no
  extension host or language server has been measured.
* **Source provisioning for Observe mode is an open design problem.** Debian's
  debuginfod serves symbols but **not** sources, and `set sysroot` does not
  cover source lookup at all. See the
  *Debug with gdb* how-to for where sources
  actually come from.
* **In-place pod resize is partly proven, and it diverges a pod from its
  controller** — see `--resize` above.
* **Patch mode has never been run against a cluster.** The workflow exists
  (`podbench hotfix init|apply|status|consolidate`, and `hotfix --print-values`
  for the chart snippet) and is unit-tested, but every one of those tests drives
  a temp directory and a fake `kubectl`. `attach --mount` now puts the claim
  into the seat at the application's own mountPath, so the workflow is
  reachable end to end — but reachable is not the same as demonstrated.

What            | Where
:---:           | :---:
Source          | <https://github.com/gilesknap/podbench>
Launcher        | <https://pypi.org/project/podbench> (not published yet)
Image           | `ghcr.io/gilesknap/podbench`
Chart           | `oci://ghcr.io/gilesknap/charts/podbench`
Documentation   | <https://gilesknap.github.io/podbench>
Releases        | <https://github.com/gilesknap/podbench/releases>

<!-- README only content. Anything below this line won't be included in index.md -->

## Documentation

See <https://gilesknap.github.io/podbench> for the full documentation.

* Tutorials — [installation](docs/tutorials/installation.md),
  [your first session](docs/tutorials/first-session.md)
* How-to — [attach to a pod](docs/how-to/attach-to-a-pod.md),
  [debug with gdb](docs/how-to/debug-with-gdb.md),
  [iterate on Python](docs/how-to/iterate-on-python.md),
  [VS Code Remote-SSH](docs/how-to/vscode-remote-ssh.md),
  [the container image](docs/how-to/run-container.md)
* Reference — [CLI](docs/reference/cli.md)
* Explanations — [architecture](docs/explanations/architecture.md),
  [security](docs/explanations/security.md),
  [design brief](docs/explanations/design-brief.md),
  [Phase 0 report](docs/explanations/spikes/phase0-report.md)
