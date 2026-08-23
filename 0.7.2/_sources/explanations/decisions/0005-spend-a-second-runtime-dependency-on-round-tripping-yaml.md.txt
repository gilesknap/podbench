# 5. Spend a second runtime dependency on round-tripping YAML

Date: 2026-08-22

## Status

Accepted

## Context

`podbench hotfix --print-values` emitted a fragment. Two of its keys —
`volumes:` and `volumeMounts:` — are a whole key each in helm, so pasting them
over a values file that already sets one drops whatever it declared. Read from a
live pod that is undecidable: a chart-generated volume and one the service
declared for itself look identical from there. So the output carried
`EXISTING_MOUNTS_WARNING`, named the volumes it saw, and asked the user to merge
by hand.

Issue #192 is the observation that read from the **values file** it is perfectly
decidable — the file says exactly what the service declares and everything else
came from the chart — and that the workflow the tool is for is *paste the whole
file*, not *merge these fragments*. Doing that means podbench reading somebody
else's values file and handing it back.

The obstacle is what a values file is. `bl47p-ea-fastcs-01/values.yaml` is mostly
comments explaining why each key is there, several of them recording things that
cost a cluster run to learn. So is the shared `services/values.yaml` it inherits
from, which additionally uses YAML anchors and a merge key.

podbench had **no runtime YAML library at all**. Everything it parses is
`kubectl -o json`; the only YAML it produces is `values_snippet`, which builds
lines of text. `dependencies = ["typer>=0.12"]` had a test behind it
(`tests/test_packaging.py`) asserting the *whole* direct set, precisely so that a
second entry has to be argued for in a diff. This is that argument.

Three ways to do it, and the third is the only honest one:

* **`pyyaml`** — already a dev dependency, so the smallest step. It drops every
  comment and reflows everything. The user gets back a file that is no longer
  recognisably theirs, and the explanations that made it maintainable are gone.
  Rejected: "the whole file" that is missing most of what was in it is not the
  whole file.
* **Splice the text** — keep the input's bytes and replace only the specific key
  blocks. No dependency, and it preserves the input exactly. It also puts a YAML
  block editor — flow style, anchors, tabs, comments between entries — between
  podbench and a beamline's `volumes:` list, hand-written and owned here. The
  failure mode of getting it subtly wrong is an IOC coming back with its data
  directory silently unmounted. Rejected: not a thing to hand-roll where that is
  the cost of a bug.
* **`ruamel.yaml`** — a round-trip loader. Comments and quoting survive the
  parse, so the merge is structural and the output is the user's file with keys
  changed.

## Decision

Depend on `ruamel.yaml`, and confine it to one section of `hotfix.py`.

The direct runtime set becomes `{"ruamel.yaml", "typer"}` and
`tests/test_packaging.py` still asserts the whole of it, so a third still has to
be argued for. What that test is really protecting is untouched: podbench talks
to Kubernetes by shelling out to `kubectl`, and `ruamel.yaml` buys no opinion
about Kubernetes at all. It is pure Python.

Two shapes keep it contained:

* **`merged_values` is pure.** Strings in, a string and its notes out, with no
  filesystem and no cluster — for the same reason `values_snippet` takes no
  cluster. Reading the two files is a wrapper's job, exactly as `--from-pod` is a
  wrapper. A test asserts neither function acquires either dependency.
* **Four calls touch ruamel**, all in `_round_trip`, `_load_yaml`, `_dump_yaml`
  and the two comment helpers. Everything past them is ordinary dicts and lists.
  `ruamel.yaml` ships no complete type information, so those lines carry narrow
  `# pyright: ignore` comments and the rest of the section stays strict-checked.

## Consequences

- A cold `uvx podbench` resolves one more pure-Python wheel. The debug image is
  unaffected in shape: it installs the project with `uv sync --locked --no-dev`.
- Comments survive a merge, and that is asserted. So is the inverse: the *shared*
  file's comments are stripped from entries absorbed out of it. A ruamel comment
  attaches to the node after it, so copying one entry out of a parent's list
  otherwise brings the sentence that introduced the next key along and lands it
  mid-list — measured on `p47-services`, where absorbing `beamline-data` dragged
  "shared setting for all legacy IOCs" into the middle of `volumeMounts`.
- The snippet's own comments are stripped too, and a short deliberate set is
  written instead. Round-tripping re-emits a comment at the column it was parsed
  at, which is not the column it ends up in — and twenty lines of podbench prose
  in a service's values file is not what the user asked for either. What is
  written instead is the handful of sentences a human wrote by hand on
  `p47-services` after working the same things out the expensive way.
- Round-tripping preserves comments and quoting but re-emits the layout, so
  `_round_trip` pins `mapping=2, sequence=4, offset=2` and a wide `width`. A
  long `args:` block is not podbench's to re-wrap.
