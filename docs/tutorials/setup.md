# Setup

podbench has two halves, and you install neither. The **launcher** runs on your
machine straight from the index; the **image** is pulled by the cluster when you
attach. Nothing is installed into the target pod, and no application chart has
to change.

So this page is not an installation. It is the four things that are still true
without one: what you need on your machine, the single ssh line that outlives
the command, which image the launcher will ask for, and what the cluster has to
allow.

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

Or let podbench check most of them once you have `uv`: `uvx podbench doctor`
measures kubectl and its version, the context and namespace, the ssh client,
both halves of your key, the ssh agent, the config directory and the `Include`,
plus RBAC. It does not check `uv` or `helm`. See *Check the machine*, below.

## Run the launcher

Every laptop-side verb is spelled `podbench <verb>`, and the canonical way to
reach it installs nothing:

```
$ uvx podbench --version
$ uvx podbench list
no podbench containers in namespace default
```

:::{note}
Only prereleases are published so far, so `uvx podbench` resolves the newest
beta today. To run an unreleased checkout instead, put
`--from git+https://github.com/gilesknap/podbench` in front of the verb:

```
$ uvx --from git+https://github.com/gilesknap/podbench podbench --version
```

That builds from repository head, so it is a dev build — see *The image*, below,
for which image such a launcher asks for.
:::

That is the whole setup. `uvx` resolves the wheel from PyPI and runs it;
podbench declares one runtime dependency — its CLI, typer — so that is four
small pure-Python wheels alongside it and nothing else, and nothing is
installed — no environment you have to manage, and nothing on your `PATH`.

It is not quite "nothing on disk": uv keeps the environment in its own cache
(`uv cache dir`), which is what makes the second run fast. That has one
consequence worth knowing — an unpinned `uvx podbench` keeps using the cached
version rather than checking PyPI for a newer one. Ask for `podbench@latest`, or
pass `--refresh`, when you want the newest release.

Pin it as `uvx podbench@0.4.0b1 <verb>` in anything that has to be reproducible —
a script, a runbook, a shared incident channel.

If you would rather have `podbench` on your `PATH` and manage upgrades yourself,
`uv tool install podbench` does that, as do `pipx install podbench` and a plain
`pip install podbench` into a virtualenv you keep activated. They all run the
same program and all read the same name from PyPI. Until a final release
exists, each needs the prerelease asked for explicitly —
`pip install --pre podbench`, `uv tool install podbench --prerelease allow`.
Only the `pip` route does not fetch its own interpreter, so build
that venv with **3.11 or later** — pip refuses the wheel otherwise.

A release carries two spellings of its version: the wheel is PEP 440
(`1.0.0b1`), while the git tag and the chart are SemVer (`1.0.0-beta.1`). The
**image carries both**, pushed onto one digest, which is what lets the launcher
ask for its own version verbatim. uv prefers a stable release and falls back to
a prerelease only when no stable one exists — which is the case today, so a bare
`uvx podbench` currently gets `0.4.0b1`. Once a stable release ships, testing a
beta will mean asking for it by its wheel spelling,
`uvx podbench@1.0.0b1 attach ...`.

The current release string is on the
[releases page](https://github.com/gilesknap/podbench/releases); you need it
below for the `--version` flag on `helm show chart` and `helm upgrade
--install`.

## Check the machine, and add the ssh include

There is no install step under `uvx`, so there is nowhere for first-run setup to
happen by itself. `podbench doctor` is that step: it says whether this machine
can attach at all, and names whatever cannot.

```
$ uvx podbench doctor -n demo
```

It checks `kubectl` and its version, the context and namespace in play, the ssh
client and both halves of your key, the `Include` below, and — one
`kubectl auth can-i` at a time — the RBAC each podbench feature needs, reported
per feature — `attach`, `iterate`, `resize` and `hotfix`, each as `[ok]`,
`[warn]` or `[FAIL]`. Only `attach` can fail; the rest warn. It exits `0` only when
nothing blocks an attach. See the
[command-line reference](../reference/cli.md) for the full list.

One of those checks is the only setup step that outlives the command. podbench
writes a generated stanza per pod into `~/.podbench/config.d/` rather than
editing `~/.ssh/config`, so it can regenerate wholesale on every attach without
ever owning a file you also edit — and ssh has to be pointed at that directory
once. `--fix` does it:

```
$ uvx podbench doctor --fix
```

That creates `~/.podbench/config.d` and prepends a comment and the `Include`
line at the **top** of `~/.ssh/config`, with the path expanded:

```
# Added by podbench doctor --fix.
Include /home/you/.podbench/config.d/*.conf
```

At the top because the `Include` must come **before** any `Host *` block:
OpenSSH takes the first value it sees for each keyword, so a `ControlPath` or
`ProxyCommand` in a block above it would silently replace podbench's. `--fix`
adds nothing else, moves nothing you wrote, and is safe to run twice. If you
would rather make the edit yourself — a managed dotfile, say — `doctor` prints
the exact line and changes nothing without `--fix`.

`--fix` will **not** generate an ssh key. A missing one is named, with the
`ssh-keygen` command to run, because podbench authorises that key inside your
containers and it should be one you chose.

Change the directory with `--config-dir` or `PODBENCH_CONFIG_DIR` if
`~/.podbench` does not suit you; `doctor` follows the same flag.

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
| `pods/resize` | `get`, `patch` | `attach --resize`, and `podbench vscode` unless `--no-resize` |
| `persistentvolumeclaims` | `get`, `list` | Iterate mode, optional scratch claim |
| `apps`: `deployments`, `statefulsets`, `replicasets` | `get` | Hotfix mode — a read-only walk to refuse a multi-replica target, and all it needs |

`podbench doctor` asks the cluster this table one verb at a time and reports it
per feature, so you find out before the attach rather than during it.

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

`rbac.observe` is on by default; `rbac.iterate`, `rbac.resize` and `rbac.hotfix`
are separate flags because they are genuinely different levels of trust. The
chart also carries the optional scratch PVC for Iterate-mode workspaces.

The chart ships a `values.schema.json`, so a misspelt `--set` is refused by
`helm` rather than accepted and dropped:

```
$ helm upgrade --install podbench ./Charts/podbench --set rbac.iterat=true
Error: values don't meet the specifications of the schema(s) in the following chart(s):
podbench:
- at '/rbac': additional properties 'iterat' not allowed
```

Without it that install would have succeeded, granted the observe verbs only,
and the mistake would have surfaced at `podbench dev` as an RBAC error naming a
verb you thought you had. If you keep your values in a file, the same schema
drives editor completion — every release attaches `values.schema.json`, so point
at the one matching the chart version you deploy:

```yaml
# yaml-language-server: $schema=https://github.com/gilesknap/podbench/releases/download/0.1.0-alpha.6/values.schema.json
rbac:
  create: true
```

**Nothing in that chart is required to use podbench.** Observe and Iterate mode
work against any pod from any chart, unmodified — that is the design principle
the tool is built on.

### The hotfix claim, declared beside the pod

Hotfix mode is the one exception, and it needs exactly one cluster object a
target's own chart has to create: a `PersistentVolumeClaim` carrying the
project. The chart above can create these from a central
`hotfixProject.claims[]` list, and that still works — but it is a namespace-wide
release naming every application that might ever be hotfixed, and a claim there
outlives every service that ever used it.

A second chart is published for the other shape: one claim, declared by the
target's own chart, with the pod's lifecycle. It is a **dependency**, not a
release:

```yaml
# the target's Chart.yaml
dependencies:
  - name: podbench-hotfix-claim
    version: "0.7.1"                          # the podbench release
    repository: "oci://ghcr.io/gilesknap/charts"
```

It renders **nothing at all** unless `enabled` is set, which is the whole point:
the dependency can sit in one shared `Chart.yaml` that every service inherits,
hotfix-enabled or not, and a service that never asks for it is not changed by
having it. Asking costs one line in that service's values:

```yaml
podbench-hotfix-claim:
  enabled: true
```

That renders `<release>-podbench-project`, which is the name `podbench hotfix
values` puts in the pod's `volumes:` entry. `size` and
`storageClassName` are the only other keys; `claimName` exists for a claim that
already exists under another name. Its schema is attached to every release as
`podbench-hotfix-claim.values.schema.json`.

Only the claim moves. `volumes:`, `volumeMounts:`, `command`/`args` and
`podSecurityContext` are fields inside the target's *own* pod template — a
subchart cannot reach into a sibling's StatefulSet, and a pod's volumes are
immutable once it exists — so those are in the target's values at deploy time or
not at all. `podbench hotfix values` is what makes writing them cost
nothing.

## Next

[Your first session](first-session.md) takes you from here to a connected
editor.
