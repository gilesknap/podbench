# Demo targets

Three workloads for the e2e suite to debug. Two are lifted from the spikes that
first proved the mechanism; the third reconstructs a measured production
workload. All three obey the same constraint: **no local container runtime**. CI
has kubectl and nothing else, so no app here may require a `docker build`.

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

## `dls-ioc.yaml` — the DLS-alike IOC

Not from a spike. A reconstruction of `b01-1-beamline/bl01c-ea-flip-02-0` at
Diamond, read out of the live cluster on 2026-08-17, because issues #89 and #90
reproduce only against the combination of properties it carries — every one of
which podbench assumed away somewhere.

It runs Diamond's own published image,
`ghcr.io/diamondlightsource/fastcs-thorlabs-mff:0.2.0`, rather than a script
under a public base image. That is a deliberate exception to the pattern above:
the two things under test are properties of *that* image and cannot be faked.

* **PID 1 is not the app.** The entrypoint is overridden to reproduce the real
  four-deep wrapper chain, using the image's own `stdio-expose` and `pptty` out
  of `/app/.venv/bin`. Anything reasoning from "the target is PID 1" is wrong
  here. The pids reproduced Diamond's exactly (1, 9, 11, 12) — but a second copy
  of the same pod came up 1, 8, 10, 11, so assert on the depth and on `exe`,
  never on the number.
* **The interpreter is a uv-managed CPython 3.11 at
  `/python/cpython-3.11.15-linux-x86_64-gnu`** — the same path podbench's seat
  image installs *its* interpreter at, with a different file behind it. That
  collision is where #90 lives.
* **`hostNetwork: true`**, because Channel Access resolves PVs by UDP broadcast.
  Loopback and ports are the node's.
* **uid 0, target and seat alike.** With the node at
  `kernel.yama.ptrace_scope=0` that is classic ptrace and needs no capability —
  which is exactly what #89 gets wrong when it prints `CAP_SYS_PTRACE (eff) no`
  beside an attach that succeeded.

`port: SIM` in the mounted `fastcs.yaml` is what lets it start with no Thorlabs
flipper attached (`controllers.py:86` swaps in a `SimSerialConnection`).

**One copy per node, and the clash is silent.** Two copies both reach Running
and both log `iocRun: All initialization complete`: the CA name-resolution
socket takes `SO_REUSEADDR` so both bind UDP 5064, and the second's TCP listener
quietly falls back to an ephemeral port. Nothing errors, so a test asserting on
PVs while a stale copy is up reads the wrong IOC. That is issue #87's shape, and
on the single-node bed it bites immediately.

Two modules apply it, and deliberately not one. `test_dls_ioc.py` binds the two
admission policies #89 needs and skips whole where the cluster does not serve
`MutatingAdmissionPolicy`; `test_shadowed_exec_file.py` takes the pod plain,
because #90 is a filesystem-path collision that happens on every rung and must
keep its coverage where those policies cannot run. Running both puts two copies
on the node, which the paragraph above says is silent — harmless for a debugger
assertion, and the reason neither module asserts on a PV.

## `nonroot-gid.yaml` — the non-zero-gid target

`{runAsUser: 36070, runAsGroup: 36070}`, read off
`b01-1-beamline/bl01c-di-dcam-04-0` at Diamond, and the fixture for issue #102:
a seat on the degraded rung runs as the target's uid **and gid**, so on this
target it could not append to the image's GID-0-writable `/etc/passwd` and landed
with no ssh at all. `test_nonroot_gid_identity.py` is the only module that asserts
a seat gets a login here.

The numbers are the measured ones and both are above libnss-extrausers' floors
(`MINUID`/`MINGID` 500) — which is the case the field target is, and the case the
fix has to serve. Below-floor credentials are probed separately, inside the seat,
rather than by a second target pod.

Two things it deliberately does *not* set:

* **No `runAsNonRoot: true`.** With it, `spec.rung_of_spec` refuses the full rung
  before the API server sees it and burns no container name (report 3.18) — a
  different path from the one this fixture is for. Without it podbench asks for
  the full rung, `deny-sys-ptrace.yaml` refuses it, and the ladder walks down to
  `degraded`. **That walk is the whole setup: on an unrestricted cluster the full
  rung lands, the seat is root, `/etc/passwd` is writable and #102 cannot appear
  at all.** Bind the policy with this target or the module asserts nothing.
* **No `hostNetwork`.** Unlike `dls-ioc.yaml`, nothing here needs Channel Access,
  so this target does not own the node's port space and can share a node (issue
  #87 is about the pods that cannot).

It is also the target where `deny-sys-ptrace.yaml` behaves as the section below
describes it for a *non-root* uid: the full rung is refused and the degraded rung
is authorable, so a seat does land.

## `deny-sys-ptrace.yaml` — not a workload

The odd one out: a `ValidatingAdmissionPolicy` and its binding, refusing any
ephemeral container that adds `SYS_PTRACE`. It is what makes a seat land on the
**degraded** rung on a cluster that would otherwise give it the full one, and
without it issue #89 — a capability report written from the rung admitted rather
than from the probes that ran — cannot be reproduced at all.

That holds for a target running as a **non-root** uid. Against a root target,
which is what `dls-ioc.yaml` and every IOC at Diamond is, this policy lands no
seat at all: measured on the bed, `podbench: no rung of the capability ladder was
admitted`, because the full rung is refused here, the degraded rung cannot be
authored at uid 0 (`runAsNonRoot: true` contradicts it) and the seat rung is then
refused by the kubelet. Reproducing #89 against a root target needs the file
below instead — and both of them together, which is what `test_dls_ioc.py` binds.
`test_nonroot_gid_identity.py` is the other half of that sentence: its target
(`nonroot-gid.yaml`) *is* non-root, so this file alone is enough there and it
binds no mutating policy at all.

Native admission rather than Kyverno (which is what the real cluster runs)
because it is core API: no controller to install, no CRDs to wait for, no
webhook certificate, on a runner that has only kubectl. The ladder cannot tell
the two apart; both arrive as a `Forbidden` from the same API call.

Two things about it that are easy to get wrong and were measured on a cluster
rather than reasoned about:

* **It matches `pods/ephemeralcontainers` on UPDATE, not `pods` on CREATE.** The
  same expression bound to `pods`/CREATE admits a `SYS_PTRACE` ephemeral
  container with exit 0 and no message. It fails *open*, so every test
  downstream keeps passing against the wrong rung.
* **podbench did not recognise the refusal.** A `ValidatingAdmissionPolicy`
  denial reads `... denied request:` — not `denied the request`, and it names no
  webhook — so it matched neither of the two patterns issue #77 taught
  `kubectl.ADMISSION_DENIAL_MARKERS`, and the ladder ended the walk instead of
  dropping a rung. There is a third group there now.

## `strip-sys-ptrace.yaml` — not a workload either

A `MutatingAdmissionPolicy` that *removes* `SYS_PTRACE` from an ephemeral
container instead of refusing it, which is the shape issue #89 actually happened
in: the seat lands, runs as root with no capability, and reads back off the pod
as the degraded rung — `launcher.py::seat_layout` describes exactly this,
attributed to DLS on 2026-08-16. uid 0 tracing uid 0 under
`kernel.yama.ptrace_scope=0` is classic ptrace, so the probe attaches while every
capability-derived sentence beside it says the seat cannot.

Bind it *with* `deny-sys-ptrace.yaml`, not instead of it. Mutation runs before
validation, so the two compose, and the denying policy is what stops a broken
expression here from quietly admitting an ordinary full-rung seat that satisfies
every #89 assertion for the wrong reason.

Measured, and neither is obvious:

* **`patchType: ApplyConfiguration` cannot express this.** `capabilities.add`
  carries no patch strategy, so server-side apply calls it atomic and refuses:
  `may not mutate atomic arrays, maps or structs`. JSONPatch is the only
  mechanism that can remove the field, and it needs an index CEL has no
  `enumerate` for — hence the literal index list filtered by `size()`.
* **MutatingAdmissionPolicy is newer than its validating sibling**, so a cluster
  may serve one and not the other, and may serve it at an older version. The
  fixture asks discovery for `admissionregistration.k8s.io/v1` by name — the
  version these files are written against — and skips with a reason rather than
  failing. `kubectl api-resources` answers without a version, so a cluster
  offering only `v1beta1` would have looked supported and then failed inside the
  fixture with `no matches for kind`.

## Applying them

No manifest here names a namespace. The fixtures apply them into a scratch
`podbench-e2e-*` namespace with `kubectl apply -n <ns> -f <file>`, and the
namespace takes them away again.

`deny-sys-ptrace.yaml` and `strip-sys-ptrace.yaml` are the exceptions on both
counts, because all four of their objects are cluster-scoped. `-n <ns>` is
harmless (and ignored), the namespace opts in with a label per policy
(`podbench.dev/deny-sys-ptrace: enforce`, `podbench.dev/strip-sys-ptrace:
enforce`), and deleting the namespace does *not* delete the objects — a
session-scoped fixture has to, spelling each one `type/name` or kubectl deletes
only the bindings.
The file's own header comment has the recipe.
