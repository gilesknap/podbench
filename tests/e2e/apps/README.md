# Demo targets

Two workloads for the e2e suite to debug. Both are lifted from the spikes that
first proved the mechanism, and both obey the same constraint: **no local
container runtime**. CI has kubectl and nothing else, so neither app may
require a `docker build`.

That constraint is what shapes them.

## `distroless-c.yaml` — the S3 target

From `docs/explanations/spikes/s3.md` §0, "Namespace + target pod (distroless,
no image build)".

The trick is an initContainer with a staged binary:

* the C source lives in a **ConfigMap**;
* an **initContainer** running `debian:bookworm-slim` `apt-get install`s `gcc`
  and compiles it with `-g -O0` into a shared **emptyDir**;
* the **runtime container** is `gcr.io/distroless/cc-debian12` and runs the
  staged binary.

So the target has debug symbols, and genuinely has no shell, no gdb and no libc
headers — the `cc` variant carries the C runtime and nothing else. It is a real
proof that podbench debugs a target that cannot help it, and it costs one
ConfigMap and two public image pulls.

`test_s3_gdb.py` asserts the absence of `/bin/sh` explicitly. That assertion is
not decoration: swapping the runtime image for a debian one to "simplify the
manifest" would leave every other test in that file passing while testing the
easy case.

The pod deliberately runs as root with no `securityContext`. It is the
*full-rung* target; the restricted-PSA case is authored inline in
`test_s5_ladder.py`, in a namespace that test labels itself.

## `python-service.yaml` — the S4 target

From `docs/explanations/spikes/s4.md` §1, "Target workload", with one change.

`python:3.12-slim` runs a script straight out of a ConfigMap, behind a
`ClusterIP` Service — again, no image build. The change is that the ConfigMap
now carries an **installable package** (`pyproject.toml` + a flat
`demo_service.py`) rather than a loose script, because S4's headline claim is
"edit code, `curl` the Service, see your change" *through a `uv pip install
-e .`*, and that needs something installable. The same two ConfigMap keys are
the seed for the git repo the dev pod clones, so the manifest stays the single
definition of the app and the test reads the sources back from the cluster
rather than duplicating them.

Two details are load-bearing rather than incidental:

* **All three probes are on the app container.** Report §4.4 requires the
  authored dev pod to strip `readinessProbe`, `livenessProbe`, `startupProbe`
  and `lifecycle` from the idled container — a liveness probe against `sleep
  infinity` kills the pod — so the origin has to carry them for that to be
  worth asserting.
* **`allow_reuse_address`, never `SO_REUSEPORT`.** A second `SO_REUSEPORT` bind
  succeeds with no error and the kernel splits traffic between old and new code
  (report §3.16 measured 5 new / 3 old / 2 new through the Service). A demo app
  must not be able to hide the relaunch failure the test is looking for.

Flat module, not a package directory: ConfigMap keys cannot contain `/`.

## Applying them

Neither manifest names a namespace. The fixtures apply them into a scratch
`podbench-e2e-*` namespace with `kubectl apply -n <ns> -f <file>`, and the
namespace takes them away again.
