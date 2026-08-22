# Ways in

Podbench has three modes. They are not three interfaces to the same thing — each one
makes a different trade against the pod it is aimed at, and the trade decides which is
available to you before taste does.

| | [`attach`](attach-flow.md) | [`dev`](dev-flow.md) | [`hotfix`](hotfix-flow.md) |
|---|---|---|---|
| **What it does** | puts a seat in the running pod | runs a sacrificial clone of it | makes an edit outlive the session |
| **Touches the workload** | no — adds a container to the pod | no — the origin is left alone | yes — needs a claim in the chart |
| **Singleton-safe** | **yes** | **no** — the clone is a second copy | **yes** |
| **GitOps-safe** | **yes** | **no** — refused outright | mostly ([#32](https://github.com/gilesknap/podbench/issues/32)) |
| **Languages** | any | any, but only Python is set up for you | Python only ([#34](https://github.com/gilesknap/podbench/issues/34)) |
| **Survives a restart** | no | no | **yes** |
| **Inner loop** | no | **yes**, ~1 s | yes, one rollout per edit |
| **What it actually does** | [step by step](attach-flow.md) | [step by step](dev-flow.md) | [step by step](hotfix-flow.md) |

`podbench vscode` is not a fourth mode. It is `attach` — the same seat, the same
ladder, the same pod — dressed for an editor: it sizes the pod's memory for
vscode-server, installs debugpy into the target where the target says that is the
blocker, and opens VS Code on the seat. Those two mutations are why it is a verb of
its own rather than a flag: the **Touches the workload** row above is a promise
`attach` keeps, and choosing a verb named for the editor is how you ask to spend it.
See [VS Code over Remote-SSH](../how-to/vscode-remote-ssh.md).

The last row goes to a page per mode: every check it makes, in order, and the `kubectl`
commands each step becomes. Anything in them you have not met before — PSA, Yama,
`subPath`, the ambient set — is in the [Glossary](../reference/glossary.md).

## Singleton-safe

A workload is a singleton when a second copy of it is not merely wasteful but wrong: it
holds a device that accepts one connection, claims a name on the network, or takes a lock.
An EPICS IOC is usually all three at once.

`attach` and `hotfix` never make a second copy — one works inside the running pod, the
other restarts it in place. `dev` is built on a clone, so for a singleton it produces two
processes competing for the same device and the same names. There is no flag for this;
the clone *is* the mode. Use `hotfix` for a singleton that needs an inner loop.

## GitOps-safe

Under a controller like Argo CD with self-heal on, anything podbench writes to a
git-managed object is drift and gets reverted, usually within seconds and always without
telling you.

`attach` is clear because its mutations are pod-level — an ephemeral container and an
in-place resize — and pods made by a controller are not compared against git.

`dev` **refuses to run** against a workload carrying a GitOps mark, and there is no
override. The reason is `--take-traffic`: a Service is a tracked object straight out of
git, so self-heal reverts the selector within seconds and the traffic you think you have
returns to the original pod with no error anywhere. Making the dev pod itself survive is
possible and was rejected — it works by hiding the pod from the controller, which turns a
loud failure into a silent one and leaves the Service problem untouched. Detection looks
at the *workload*, not the pod, because that is where the mark is; a cluster that leaves
Argo's `instanceLabelKey` at its default is not detected, which is the safer direction to
be wrong in.

`hotfix` works, but records its provenance on the pod template, which self-heal strips —
so the fix keeps running while `hotfix status` stops being able to see it
([#32](https://github.com/gilesknap/podbench/issues/32)).

## Languages

`attach` is language-agnostic: it is a seat with a debugger, gdb for native code and
debugpy for Python, and it needs nothing from the target but a process.

`dev` splits. Building the dev pod, idling the app container, the relaunch and the
listening-socket check are all language-agnostic — `podbench run -- <command>` will start
anything. What is Python-specific is `dev-bootstrap`, which clones, runs `uv sync` and
does an editable install. For another language, prepare the workspace yourself and use
`podbench run`.

`hotfix` is Python throughout: it reads `pyvenv.cfg`, mounts a claim over the application's
venv, and re-runs an editable install. Generalising it is [#34](https://github.com/gilesknap/podbench/issues/34).

## If you only remember one thing

Start with `attach`. It is safe against anything, needs nothing from the chart, and
answers most questions. Reach for `dev` when you need a fast edit-and-rerun loop and the
workload tolerates a second copy; reach for `hotfix` when the fix has to survive a restart,
or when it needs a loop and a second copy is out of the question.
