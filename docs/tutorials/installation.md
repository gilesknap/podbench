# Installation

podbench has two halves, and you only install one of them. The **launcher** goes
on your machine; the **image** is pulled by the cluster when you attach. Nothing
is installed into the target pod, and no application chart has to change.

## What you need first

| | Why |
|---|---|
| Python **3.11 or later** | the launcher is pure stdlib — no runtime dependencies at all |
| `kubectl` **1.25 or later** | ephemeral containers are stable from 1.25; podbench posts to the `ephemeralcontainers` subresource itself |
| a working kubeconfig | this *is* podbench's authentication. There is no second credential |
| an ssh client and an ssh keypair | `~/.ssh/id_ed25519` by default; override with `--identity` |
| VS Code with the **Remote - SSH** extension | only if you want the editor. gdb and the shell work over plain `ssh` |

Check the first two:

```
$ python3 --version
Python 3.11.9
$ kubectl version --client
```

## Install the launcher

podbench is not on PyPI yet, so install from the repository or from a wheel
attached to a [GitHub release](https://github.com/gilesknap/podbench/releases).

The important part is that **two** executables land on `PATH`: `podbench` and
`kubectl-podbench`. kubectl treats any `kubectl-*` executable on `PATH` as a
plugin and hands it the remaining argv, which is what turns
`kubectl podbench attach` into a call to podbench.

::::{tab-set}

:::{tab-item} uv

```
$ uv tool install git+https://github.com/gilesknap/podbench.git
```

:::

:::{tab-item} pipx

```
$ pipx install git+https://github.com/gilesknap/podbench.git
```

:::

:::{tab-item} pip + venv

```
$ python3 -m venv ~/.venvs/podbench
$ source ~/.venvs/podbench/bin/activate
$ python3 -m pip install git+https://github.com/gilesknap/podbench.git
```

With a venv you must keep it activated, or symlink both executables onto your
`PATH`, or kubectl will not find the plugin.

:::

::::

Verify both halves of the install:

```
$ podbench --version
$ kubectl podbench list
no podbench containers in namespace default
```

If `kubectl podbench` reports `unknown command`, `kubectl-podbench` is not on
your `PATH`. `kubectl plugin list` will say what kubectl can see.

## Add the ssh include, once

podbench writes a generated stanza per pod into `~/.podbench/config.d/` rather
than editing `~/.ssh/config`, so it can regenerate wholesale on every attach
without ever owning a file you also edit. Point ssh at that directory once:

```
$ mkdir -p ~/.podbench/config.d
$ printf 'Include ~/.podbench/config.d/*.conf\n' | cat - ~/.ssh/config > ~/.ssh/config.new
$ mv ~/.ssh/config.new ~/.ssh/config
```

The `Include` must come **before** any `Host *` block in `~/.ssh/config`;
OpenSSH takes the first value it sees for each keyword.

Change the directory with `--config-dir` or `PODBENCH_CONFIG_DIR` if
`~/.podbench` does not suit you.

## The image

The launcher defaults to `ghcr.io/gilesknap/podbench:latest`, built and pushed
by CI on tag. You do not pull it yourself — the kubelet does, when the ephemeral
container starts.

Override it per invocation with `--image`, or globally with the `PODBENCH_IMAGE`
environment variable. Pin a **digest** in anything permanent: an admission
policy that allows `CAP_SYS_PTRACE` for one specific image is only writable
against a pinned one. See [The container image](../how-to/run-container.md).

## What the cluster has to allow

podbench needs a small, boring set of verbs in the namespace you are debugging.
Read the whole list, with the reasoning for each, in
[Security model](../explanations/security.md); the short version is:

| Resource | Verbs | For |
|---|---|---|
| `pods`, `pods/log` | `get`, `list`, `watch` | read the target's spec before picking a rung |
| `pods/ephemeralcontainers` | `get`, `patch`, `update` | attach the seat |
| `pods/exec` | `create` | the ssh transport — this is the entire network story |
| `pods` | `create`, `delete` | Iterate mode only |
| `services` | `get`, `list`, `patch` | Iterate mode with `--take-traffic`/`--cutover` only |
| `pods/resize` | `patch` | `attach --resize` only |

A chart is provided for clusters that would rather grant these through Helm than
by hand. It is in the repository at `Charts/podbench/` and is not published to a
chart registry:

```
$ helm install podbench ./Charts/podbench \
    --namespace demo \
    --set rbac.create=true \
    --set 'rbac.subjects[0].kind=Group' \
    --set 'rbac.subjects[0].name=developers' \
    --set 'rbac.subjects[0].apiGroup=rbac.authorization.k8s.io'
```

`rbac.observe` is on by default; `rbac.iterate` and `rbac.resize` are separate
flags because they are genuinely different levels of trust. The chart also
carries the optional scratch PVC for Iterate-mode workspaces.

**Nothing in that chart is required to use podbench.** Observe and Iterate mode
work against any pod from any chart, unmodified — that is the design principle
the tool is built on.

## Next

[Your first session](first-session.md) takes you from here to a connected
editor.
