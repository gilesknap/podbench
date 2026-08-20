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
PyPI, uses your kubeconfig, and shells out to `kubectl` for everything it does
to a cluster.

| | Observe mode | Iterate mode | Hotfix mode |
|---|---|---|---|
| Command | `uvx podbench attach pod/foo` | `uvx podbench dev pod/foo` | `uvx podbench hotfix init pod/foo` |
| What it does | adds an ephemeral container to the **live** pod | authors a **sacrificial clone** with the app idled and podbench as a real sidecar | puts the app's venv on a claim, so an edit survives the restart — and the restart *is* the relaunch |
| Built for | distroless targets with no shell of their own; gdb against the running workload | edit → relaunch → see the change through the Service | an emergency fix that has to outlive the session, with provenance |
| Resources | shares the workload's limits, **cannot reserve its own** | has its own cpu, memory and ephemeral-storage requests | a ReadWriteOnce claim, mounted at the same path on both sides |
| Needs | nothing at deploy time | nothing at deploy time | **deploy-time chart cooperation**, and it is Python-only and single-replica-only |
| Risk to the workload | real — see below | none by default; the origin pod is never touched | it **rolls the workload**: that is how the fix takes |
| Explained in | [What `attach` does](docs/explanations/attach-flow.md) | [What `dev` does](docs/explanations/dev-flow.md) | [What `hotfix` does](docs/explanations/hotfix-flow.md) |

## Try it

```
$ uvx podbench doctor
$ uvx podbench attach pod/web-7d9f8c5b4-x2k9p -n demo
```

`doctor` checks the prerequisites, the cluster-side RBAC and the one-time ssh
`Include` line — the only thing podbench leaves behind — and names whatever is
missing; `--fix` adds the `Include`. `attach` then walks a capability ladder,
lands the best seat the cluster will admit, and runs its probe **inside the
container it just created**, so the report is measured rather than inferred from
the spec it asked for:

```
rung        full - uid 0, gid 0, CapEff 00000000a80c25fb
supports
  [x] live attach (gdb -p <pid>)
  [x] read-only inspect (/proc/<pid>/root, maps, environ)
      root, maps and environ readable
  [x] debug launched processes (podbench dbg --launch ./prog)
  [ ] iterate (edit, relaunch, verify through the Service)
  [x] ssh seat (Remote-SSH: editor, shell, git, sftp)
measured
  verdict     live attach available
  blocker     none

ssh config written to ~/.podbench/config.d/demo-web-7d9f8c5b4-x2k9p.conf
then:  ssh podbench-demo-web-7d9f8c5b4-x2k9p
```

Connect that alias with **Remote-SSH: Connect to Host…**, or let
`podbench vscode` do the whole thing — seat, pod sizing, debugpy and window — in
one command.

The other two modes are a table row away. For the inner loop, `podbench dev`
authors the dev pod and `podbench run` relaunches the app inside it: measured
end to end, **1.18 s** per edit → relaunch → verified-through-the-Service
cycle. When the fix has to survive the pod, `podbench hotfix` moves the venv
onto a claim so a restart no longer restores the image's code, and records
where the change came from.

> **The PyPI name is not published yet**, so `uvx podbench` will not resolve
> until the first release. Until then, run every command as
> `uvx --from git+https://github.com/gilesknap/podbench podbench <verb>`.

## Read this before you attach to a live pod

Not fine print — each of these has bitten a spike on a real cluster.

* **A seat shares the pod's limits and cannot reserve its own — and it is a VS
  Code session, not the seat, that spends them.** An ephemeral container may not
  declare `resources` at all, so the seat lives in the pod's cgroup: exceed
  memory and the kernel OOM-kills something in it, exceed ephemeral storage and
  the kubelet evicts the whole pod. How much that matters is now measured rather
  than assumed. Ten live seats on a Diamond beamline (2026-08-19) cost
  **13–23 MiB** each, against **170–3858 MiB** of headroom per pod, three seats
  to a pod, no OOM anywhere — so `attach` reads *this* pod's headroom, prints it
  on the report's `memory` row, and warns only when it is genuinely thin. A
  **vscode-server** is the case that still bites: 1215 MiB live with a single
  extension, which does not fit in most of those pods, so `podbench vscode` is
  checked against the same number — and raises the target's limit in place to
  cover the shortfall, since it is about to spend it. `attach --resize MEMORY`
  is the same lever by hand, and `podbench dev` gives the seat limits of its
  own. One beamline at one
  moment: that falsifies "always warn", it does not prove no cluster is tight.
* **Being refused `SYS_PTRACE` is normal, not an error.** It is outside both the
  baseline and the restricted Pod Security Standards, so podbench walks a ladder
  with two valid rungs and lands the better one the cluster admits. Four
  unrelated subsystems deny attach with the same `EPERM`, so it probes and
  **names the blocker** instead of leaving you an errno.
* **Ephemeral containers are permanent.** They cannot be removed, restarted or
  edited, and a name once used is burnt for the life of the pod. So `attach`
  reconnects by default, nothing may live only in the writable layer, and
  `--new` is for when you mean it.
* **A dev pod never joins a Service by accident.** `podbench dev` drops the
  origin's selector labels unless you pass `--take-traffic`, and `--cutover`
  records the original selector for an exact restore.

The docs carry the reasoning and the measurements behind all four, and *What is
proven, and what is not* is candid about the gaps — no real VS Code GUI client
has connected yet, and Hotfix mode has never been run against a cluster.

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

* Tutorials — [setup](docs/tutorials/setup.md),
  [your first session](docs/tutorials/first-session.md)
* How-to — [attach to a pod](docs/how-to/attach-to-a-pod.md),
  [debug with gdb](docs/how-to/debug-with-gdb.md),
  [iterate on Python](docs/how-to/iterate-on-python.md),
  [VS Code Remote-SSH](docs/how-to/vscode-remote-ssh.md),
  [the container image](docs/how-to/run-container.md)
* Reference — [CLI](docs/reference/cli.md), [glossary](docs/reference/glossary.md)
* Explanations — [architecture](docs/explanations/architecture.md),
  [security](docs/explanations/security.md),
  [what is proven, and what is not](docs/explanations/status.md),
  [design brief](docs/explanations/design-brief.md),
  [Phase 0 report](docs/explanations/spikes/phase0-report.md)
