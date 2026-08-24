# 4. Adopt typer for the CLI, and spend the one runtime dependency on it

Date: 2026-08-15

## Status

Accepted

## Context

podbench declared `dependencies = []` and built every verb's command line out of
argparse. The rule had a test behind it (`tests/test_packaging.py`) and a CI step
that ran the built wheel through `uvx --no-index`, so nothing could be added by
accident.

What the rule was really protecting was never argparse. It was two things: that a
cold `uvx podbench <verb>` resolves in one step on a machine whose only promised
tools are uv, helm, kubectl and VS Code, and that podbench talks to Kubernetes by
shelling out to `kubectl` — inheriting kubeconfig contexts, exec credential
plugins and cloud auth rather than reimplementing any of it. A Kubernetes client
library would have reversed a real decision. argparse was only ever the incumbent.

And argparse was costing something. The CLI is the product's surface: thirteen
verbs across two machines, several of which a developer meets for the first time
while a pod is broken. `podbench --help` was a flat list of verb names with an
epilog splitting them into "cluster-side" and "in-pod" in prose, and a dozen
flags across the launcher verbs had no help text at all because argparse makes
that easy to leave out and impossible to notice.

## Decision

Depend on `typer`, and give every verb — laptop-side and in-pod — a typer app.

The dependency budget goes from "none" to "the CLI, and nothing else": typer,
which brings click, rich and shellingham. `tests/test_packaging.py` now asserts
the whole direct set rather than that it is empty, so a second dependency still
has to be argued for in a diff. The `--no-index` half of the `_dist.yml` smoke
test is dropped — it cannot survive a dependency — and the step keeps proving
what only it can, that a cold-cache resolve reaches `--version`.

Three shapes hold the conversion together:

* `cli.py` owns the one adapter between click's world and this package's. Every
  `main()` is still `(argv) -> int`, because the tests drive them directly and
  the `image/bin/podbench` wrapper passes an argv straight through; click's
  standalone mode
  renders its own errors and then exits, so exactly one place catches
  `SystemExit`. Command callbacks end in `raise typer.Exit(code)`, since click
  discards a callback's return value on purpose.
* The top-level dispatcher stays dumb. Each verb is a typer command declared with
  `add_help_option=False` that swallows its tail and hands it to the owning
  module, so `podbench dev --help` reaches `dev`'s own parser and
  `podbench --version` still imports no verb — it remains the image's build-time
  smoke test, and must not depend on every module importing cleanly.
* `--launch` keeps its `argparse.REMAINDER` contract by having the program's own
  arguments lifted out of argv before click sees them. Click claims every option
  it knows wherever it appears, and `podbench dbg --launch ./prog --fast` has to
  hand `--fast` to the program.

## Consequences

- `podbench --help` lists the verbs in two rich panels, "On your machine" and
  "Inside the debug container", which is the distinction a reader needs first and
  which prose in an epilog was not delivering. Every flag now has help text.
- A cold `uvx podbench` resolves four extra pure-Python wheels. The debug image
  is unaffected in shape: it already installs the project with
  `uv sync --locked --no-dev`, so the dependency arrives with it.
- Two behaviours changed, both of them exit codes that were previously raised
  rather than returned. `main()` now *returns* 2 for a usage error and 0 for
  `--help` instead of raising `SystemExit`; the codes are the ones argparse used,
  so nothing outside the test suite can tell.
- `pyright` needs `reportUnusedFunction = false`. Typer registers a command by
  decorating a nested function whose name is then never read, and pyright cannot
  see the registration.
- `docs/reference/cli.md` carries verbatim help for every verb, so it has to be
  regenerated when a flag changes — as it always did.
