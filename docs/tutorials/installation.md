# Installation

podbench has two halves, and you install neither. The **launcher** runs on your
machine straight from the index; the **image** is pulled by the cluster when you
attach. Nothing is installed into the target pod, and no application chart has
to change.

## What you need first

| | Why |
|---|---|
| [`uv`](https://docs.astral.sh/uv/) | `uvx` fetches and runs the launcher in one command, installing nothing. Skip it only if you would rather install. It fetches a suitable Python (3.11+) too, so you need none of your own |
| `kubectl` **1.25 or later** | ephemeral containers are stable from 1.25; podbench posts to the `ephemeralcontainers` subresource itself |
| `helm` **3.8 or later** | only for the optional RBAC chart at the end of this page. OCI registry support is what the 3.8 floor is for |
| a working kubeconfig | this *is* podbench's authentication. There is no second credential |
| an ssh client and an ssh keypair | `~/.ssh/id_ed25519` by default; override with `--identity`. podbench authorises the `.pub` half inside the container, so both halves must be present |
| VS Code with the **Remote - SSH** extension | only if you want the editor. gdb and the shell work over plain `ssh` |

Check the tools you are expected to have already:

```
$ uv --version
$ kubectl version --client
$ helm version --short
$ ls ~/.ssh/id_ed25519 || ssh-keygen -t ed25519
```

## Run the launcher

Every laptop-side verb is spelled `podbench <verb>`, and the canonical way to
reach it installs nothing:

```
$ uvx podbench --version
$ uvx podbench list
no podbench containers in namespace default
```

:::{important}
**Before the first PyPI release, `uvx podbench` cannot resolve anything** — the
name is not published yet, and `uvx` will tell you so. Until it is, put
`--from git+https://github.com/gilesknap/podbench` in front of every
`uvx podbench` on this site:

```
$ uvx --from git+https://github.com/gilesknap/podbench podbench --version
$ uvx --from git+https://github.com/gilesknap/podbench podbench list
```

That builds the launcher from the repository head, so it is a dev build — see
*The image*, below, for which image such a launcher asks for.
:::

That is the whole setup. `uvx` resolves the wheel from PyPI and runs it; because
podbench declares no runtime dependencies there is nothing else to resolve, and
nothing is installed — no environment you have to manage, and nothing on your
`PATH`.

It is not quite "nothing on disk": uv keeps the environment in its own cache
(`uv cache dir`), which is what makes the second run fast. That has one
consequence worth knowing — an unpinned `uvx podbench` keeps using the cached
version rather than checking PyPI for a newer one. Ask for `podbench@latest`, or
pass `--refresh`, when you want the newest release.

Three shapes are supported, and they run the same program:

| Invocation | When |
|---|---|
| `uvx podbench <verb>` | the default. Nothing installed; the version is whatever uv has cached |
| `uvx podbench@1.0.0 <verb>` | pinned and reproducible — a script, a runbook, a shared incident channel |
| `uv tool install podbench` | you want `podbench` on `PATH` permanently, and will manage upgrades yourself |

If you would rather install it, any of these will do. The first two put
`podbench` on your `PATH`; the third does so only while its venv is activated:

::::{tab-set}

:::{tab-item} uv

```
$ uv tool install podbench
```

:::

:::{tab-item} pipx

```
$ pipx install podbench
```

:::

:::{tab-item} pip + venv

```
$ python3.11 -m venv ~/.venvs/podbench    # or any later 3.x
$ source ~/.venvs/podbench/bin/activate
$ python3 -m pip install podbench
```

This is the one route that does not fetch its own interpreter, so the venv has
to be built with **3.11 or later** — pip refuses the wheel otherwise. And you
must keep the venv activated, or symlink `podbench` onto your `PATH`.

:::

::::

All three read the name from PyPI, so all three wait on the first release; until
then use the `--from git+...` form above.

A release carries two spellings of its version: the wheel is PEP 440
(`1.0.0b1`), while the git tag and the chart are SemVer (`1.0.0-beta.1`). The
**image carries both**, pushed onto one digest, which is what lets the launcher
ask for its own version verbatim. A bare `uvx podbench` will not select a
prerelease, which is the behaviour you want — so testing a beta means asking for
it by its wheel spelling, `uvx podbench@1.0.0b1 attach ...`.

The current release string is on the
[releases page](https://github.com/gilesknap/podbench/releases); you need it
below for `helm --version`.

## Add the ssh include, once

This is the one setup step that outlives the command, and it is why it is worth
doing before your first attach.

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

The launcher attaches the image built from its own source: the default tag
follows the launcher's version, falling back to
`ghcr.io/gilesknap/podbench:main` — the branch-tip image, rebuilt on every
commit to the default branch — when you are running a dev build rather than a
release. That includes the `--from git+...` invocation above. The two halves
author and understand the same container spec, which matters most under `uvx`,
where the launcher's version can change between two attaches with nothing to
announce it.

`:latest` is not the fallback and is not a good thing to pin to by hand: CI
moves it only on a **final** release, so it lags every prerelease and, before
1.0.0, does not move at all. Pin a version or a digest instead.

You do not pull the image yourself — the kubelet does, when the ephemeral
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
by hand. It is published to an OCI registry on every release, so this needs no
clone either.

`--version` is **not optional** and takes the SemVer spelling of the release
(`0.1.0-alpha.6`, `1.0.0-beta.1`, `1.0.0`). Helm will not resolve a prerelease
implicitly, so omitting it fails with `Could not locate a version matching
provided version string` for as long as every published chart is a prerelease.
Read the current one from the
[releases page](https://github.com/gilesknap/podbench/releases) — or, if you
have `gh`, take it from there and confirm the chart exists before installing it:

```
$ PODBENCH_VERSION=$(gh release view --repo gilesknap/podbench --json tagName -q .tagName)
$ helm show chart oci://ghcr.io/gilesknap/charts/podbench --version "$PODBENCH_VERSION"
```

Then:

```
$ helm upgrade --install podbench \
    oci://ghcr.io/gilesknap/charts/podbench --version "$PODBENCH_VERSION" \
    --namespace demo \
    --set rbac.create=true \
    --set 'rbac.subjects[0].kind=Group' \
    --set 'rbac.subjects[0].name=developers' \
    --set 'rbac.subjects[0].apiGroup=rbac.authorization.k8s.io'
```

From a checkout, the same values work against the chart directory:
`helm upgrade --install podbench ./Charts/podbench ...`.

`rbac.observe` is on by default; `rbac.iterate`, `rbac.resize` and `rbac.patch`
are separate flags because they are genuinely different levels of trust. The
chart also carries the optional scratch PVC for Iterate-mode workspaces.

**Nothing in that chart is required to use podbench.** Observe and Iterate mode
work against any pod from any chart, unmodified — that is the design principle
the tool is built on.

## Next

[Your first session](first-session.md) takes you from here to a connected
editor.
