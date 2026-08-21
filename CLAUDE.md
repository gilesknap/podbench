# CLAUDE.md

## What this is

podbench puts a development seat — editor, gdb, a Python inner loop — *inside* a
Kubernetes pod, reached over nothing but the kubeconfig. Two artefacts: a debug
container image, and a `podbench` launcher — canonically run as
`uvx podbench <verb>`, with nothing installed — that launches it.

`docs/explanations/design-brief.md` is the definition of done — each phase's
acceptance criteria are stated there.

## Read the Phase 0 report before changing behaviour

`docs/explanations/spikes/phase0-report.md` is the empirical basis for most of the
non-obvious code here. Five spikes ran against a real k3s cluster and falsified five
of the brief's load-bearing assumptions. **Where the brief and the report disagree,
the report wins.**

Section 4 is the constraint checklist, section 3 the evidence, section 5 what is
still unproven. If a change looks like an obvious simplification, check section 3
first — several of these were *measured*, not reasoned, and the failure modes are
silent.

## Hard rules

- **The transport and the container-spec rules are load-bearing and look
  arbitrary.** Before editing `sshcfg.py`/`agent.py` or anything authoring a
  container spec, read the matching skill. Removing `sshd -e`, emitting
  `SYS_PTRACE` beside a non-root uid, or giving a debug container a short-lived
  command each fail *silently* or as something misleading.
- **The seat's memory cost is measured, and the report is calibrated to it.**
  Ten live seats on a Diamond beamline (2026-08-19) were 13–23 MiB against
  170–3858 MiB of pod headroom, three seats to a pod, no OOM — the repo's own
  "where the brief and the report disagree, the report wins" rule, applied to
  newer evidence. So `attach` warns on **this** pod's headroom
  (`resize.Headroom`, `launcher.headroom_note`) and says nothing when it is
  ample. Do not restore an unconditional memory warning, and do not key one on
  the container's *limit*: the beamline's three smallest limits sit in its
  roomiest pod. A headroom that could not be read is reported as **unmeasured**
  on the `memory` row, never as fine. vscode-server, at a measured 1215 MiB, is
  the one cost that still earns a warning, and only under `podbench vscode` —
  which is also the only verb that resizes a pod nobody asked it to.
- **Never join a Service silently.** A dev pod carrying the origin's selector
  labels takes production traffic. Opt-in, behind an explicit flag, always.
- **What a laptop verb prints is laid out by `console.py`, and a warning is one
  line.** Read the `terminal-reports` skill before adding a `WARNING`, changing
  a report, or touching `console.py`. Wrapping collapses whitespace, so putting
  a row of columns or a `do this:  <command>` offer through `paragraph()`
  silently unaligns the one and unpastes the other; styling a `Text` through
  rich markup instead of by span eats the `[x]` ticks and every bracket in
  somebody else's relayed stderr.
- **A path is ambiguous across two mount namespaces, and gdb will read the wrong
  file without saying so.** Read the `gdb-across-namespaces` skill before
  touching `execfile.py`, `gdbcmd.py`, `image/bin/gdb-podbench`, or anything
  building a gdb argv or a cppdbg configuration. The seat and any uv-managed
  target both keep an interpreter at `/python/cpython-<version>-<triple>/`, so a
  Python target collides by construction — and one of the two failure modes
  prints no error at all.
- **Never mutate a cluster outside a scratch namespace.** Cluster testing happens
  in `podbench-*` namespaces created for the purpose and deleted afterwards.
  There is a persistent k3s box for this: read the `k3s-test-bed` skill before
  reproducing a field defect or running the e2e suite outside CI.
- **`Charts/podbench/values.schema.json` is generated.** A pre-commit hook
  (`helm schema`, driven by `Charts/podbench/.schema.config.yaml`) rewrites it
  from `values.yaml` plus `example.values.yaml`. Edit those; hand-edits are
  reverted on the next commit. A new value with no `# @schema` hint and no
  example entry still lands in the schema, but a list defaulting to `[]` gets no
  item shape, so a typo inside an entry goes back to being accepted silently.
- **One runtime dependency, and it is the CLI.** typer (with click and rich) is
  the whole list, asserted by `tests/test_packaging.py`; the dev group is for
  test-only additions. Adding a second has to be argued for in that test first.
  In particular **never a Kubernetes client library**: the launcher shells out to
  `kubectl` deliberately, so it inherits kubeconfig auth, contexts and exec
  credential plugins.
- **Every verb's CLI is typer, and every `main()` still returns an `int`.**
  `cli.py` is the one place that catches click's `SystemExit`; a command callback
  ends in `raise typer.Exit(code)`, because click discards a returned value and a
  returned exit code is silently lost.

## Conventions

- pyright **strict**, ruff-clean, Python 3.11 floor (CI runs 3.11-3.14).
- `from __future__ import annotations` at the top of every module.
- pytest runs with `--doctest-modules`: any `>>>` in a docstring is executed.
- Comments explain *why* — cite the constraint or spike finding that forced the
  shape, and do not narrate what the code already says. `model.py` sets the bar.
- **`dev.py` and `hotfix.py` both import `launcher.py`, so it can import
  neither.** A constant or helper the launcher needs from one of them goes in
  `model.py`, which imports nothing of podbench and is where the shared
  vocabulary belongs anyway; re-export from the original module so its public
  surface does not move. `DEVPOD_LABEL` and `HOTFIXED_ANNOTATION` went that way
  when a *listing* had to key on both. Duplicating instead is the trap: two
  copies of `dev_pod_name`'s truncation rule is how the launcher comes to print
  a command naming a pod the API server would refuse.
- Commits: one logical change, imperative subject, body explaining the reasoning
  rather than restating the diff.
- Docs build with `just docs` — `sphinx-build -EW` plus `nitpicky = True`, so any
  warning is a CI failure. `docs/explanations.md` and `docs/explanations/spikes.md`
  list their pages explicitly rather than by glob, which is what lets them be
  ordered; the cost is that a new page must be added to one of them by hand, or
  the build fails with "document isn't included in any toctree". Run `just docs`
  before pushing a docs change — CI will not tell you anything you could not have
  learned in 30 seconds locally.

## Testing

- Unit tests must not touch a cluster: synthetic `/proc` trees, fixture pod JSON,
  and an injected runner for anything that shells out.
- e2e is opt-in (`PODBENCH_E2E=1`) and mirrors the spikes, so S1-S5 are regression
  tests. CI runs them on kind.
- On a **mixed-architecture** cluster with a single-arch image, pin the suite:
  `PODBENCH_E2E_NODE_SELECTOR=kubernetes.io/arch=amd64`. Without it the probe pod
  lands where the image cannot run and everything skips.
- No docker, podman or kind in the devcontainer — images are built by CI. To test an
  image against a cluster, **push the branch**: every branch push publishes a
  multi-arch prerelease image named after it, e.g.
  `ghcr.io/gilesknap/podbench:0.1.0-beta.4-my-branch`. See `tests/e2e/README.md`.
  That tag is **overwritten on every push to the branch**, so a second attach on
  a node that already pulled it silently keeps the first copy: pass
  `--pull always` when iterating, or you will test the previous commit and
  conclude the fix does not work.

## Two environment foot-guns

Neither announces itself, and both cost real time in the first session.

- **`UV_PROJECT_ENVIRONMENT` is exported by the devcontainer**, pointing at another
  project's cache venv, so a bare `uv run` here silently loses ruff, pyright and
  pytest partway through a session. Use `just`, which pins it.
- **`pre-commit run --all-files` only sees git-tracked files**, so it passes locally
  and fails in CI when the offending files were still untracked. `git add` first.
