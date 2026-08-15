# CLAUDE.md

## What this is

podbench puts a development seat — editor, gdb, a Python inner loop — *inside* a
Kubernetes pod, reached over nothing but the kubeconfig. Two artefacts:

- a **debug container image** (`ghcr.io/gilesknap/podbench`), and
- a **kubectl plugin** (`kubectl podbench`) that launches it and prints the ssh stanza.

The design brief is `docs/explanations/design-brief.md`. It is the definition of
done: each phase's acceptance criteria are stated there.

## Read the Phase 0 report before changing behaviour

`docs/explanations/spikes/phase0-report.md` is the empirical basis for most of the
non-obvious code in this repo. Five spikes ran against a real k3s cluster and
falsified five of the brief's load-bearing assumptions. **Where the brief and the
report disagree, the report wins.**

Section 4 is a checklist of implementation constraints; section 3 explains why each
exists; section 5 lists what is still unproven. If a change looks like an obvious
simplification, check section 3 first — several of these were *measured*, not
reasoned, and the failure modes are silent.

## Hard rules

- **Never redirect, merge or close sshd's stderr.** `sshd -i -e` is mandatory and
  `-e` is not about logging: closing fd 2 in a `kubectl exec`'d process tears down
  the whole CRI exec stream and truncates stdio with `rc=0`. A wrapper shell that
  does not `exec` masks this, so it will pass a casual test and fail in the field.
  Never pass `-t` — against a real TTY the client hangs forever.
- **Never emit `SYS_PTRACE` alongside a non-zero `runAsUser`.** The capability is a
  silent no-op there (`CapEff: 0`); shipping it would tell the user they have live
  attach when they do not. The ladder has exactly two valid rungs.
- **Never join a Service silently.** A dev pod carrying the origin's selector labels
  takes production traffic. That is opt-in, behind an explicit flag, always.
- **Ephemeral containers are permanent and unrestartable.** A name, once used, is
  burnt for the pod's lifetime; an OOM inside one is unrecoverable. Everything in
  the startup path is "ensure", never "create", and nothing may live only in the
  writable layer that cannot be rebuilt.
- **Never mutate a cluster outside a scratch namespace.** Cluster testing happens in
  `podbench-*` namespaces created for the purpose and deleted afterwards.
- **No runtime dependencies.** The launcher shells out to `kubectl` deliberately so
  it inherits kubeconfig auth, contexts and exec credential plugins. argparse and
  the stdlib only; the dev group is the place for test-only additions.

## Conventions

- pyright **strict**, ruff-clean, Python 3.11 floor (CI matrix runs 3.11-3.14).
- `from __future__ import annotations` at the top of every module.
- pytest runs with `--doctest-modules`: any `>>>` in a docstring is executed.
- Comments explain *why*. The bar is set by `src/podbench/model.py` — cite the
  constraint or the spike finding that forced the shape of the code, and do not
  narrate what the code already says.
- Commits: one logical change each, imperative subject, body explaining the
  reasoning rather than restating the diff.

## Key paths

- `src/podbench/model.py` — types shared by both halves. Change with care.
- `src/podbench/probe.py` — the capability probe. Its job is to *name the blocker*:
  four subsystems deny ptrace with the same `EPERM`.
- `src/podbench/spec.py` — pure spec authoring. The launcher authors pod specs
  itself rather than shelling out to `kubectl debug --copy-to`, which strips all
  labels and cannot express resources or volumes.
- `Charts/podbench/` — the chart (scratch PVC, RBAC, Patch-mode venv mount).
- `docs/explanations/spikes/` — the findings notes, kept verbatim as evidence.

## Skills

Two areas have enough non-obvious, hard-won rules to be worth reading before you
touch them. Both are in `.claude/skills/`:

- **`ephemeral-containers`** — what the API will and will not let an ephemeral
  container do. Read before changing `spec.py`, `launcher.py`, or anything that
  authors a container spec.
- **`ssh-over-exec`** — the transport's invariants, every one of which fails
  silently or misleadingly. Read before touching `sshcfg.py` or `agent.py`.

## Testing

- Unit tests must not touch a cluster: build synthetic `/proc` trees and fixture pod
  JSON, and monkeypatch anything that shells out.
- Cluster/e2e tests are separate and opt-in; they mirror the spikes so that S1-S5
  become regression tests. CI runs them on kind.
- No docker, podman or kind in the devcontainer — images are built by CI. To get an
  image to test against the cluster, push a tag.
- On a **mixed-architecture** cluster with a single-arch image, pin the suite:
  `PODBENCH_E2E_NODE_SELECTOR=kubernetes.io/arch=amd64`. Without it the probe pod
  lands where the image cannot run and everything skips.

## Two environment foot-guns

Both cost real time in this repo's first session, and neither announces itself.

- **`UV_PROJECT_ENVIRONMENT` is exported by the devcontainer**, pointing at another
  project's cache venv. A bare `uv run` in this repo silently loses ruff, pyright and
  pytest partway through a session. Use `just`, which pins it, or
  `export UV_PROJECT_ENVIRONMENT=$PWD/.venv` before `uv run --no-sync ...`.
- **`pre-commit run --all-files` only sees git-tracked files.** It passes locally
  while CI fails, because the offending files were still untracked when it ran.
  `git add` first, then run it.
