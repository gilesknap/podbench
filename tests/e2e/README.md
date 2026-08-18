# The e2e suite

Spikes S1–S5 proved podbench's five load-bearing mechanisms once, by hand,
against a real k3s cluster. This directory turns them into regression tests, so
that a refactor which breaks one of them fails loudly instead of shipping.

The findings these tests exist to protect are the ones that fail *silently*:
a transport that truncates a stream with `rc=0`, a debug container whose
`CapEff` is zero while everything looks right, a clone the Service cannot see,
a relaunch wrapper that reports success for a process that died. None of them
is caught by a unit test, because in every case the code is correct in
isolation and wrong against a kernel or an API server.

Two modules here came from the field rather than from a spike — `test_dls_ioc.py`
(#89) and `test_shadowed_exec_file.py` (#90) — and they are here for the same
reason: both defects are a disagreement between what podbench says and what the
kernel does, and nothing that builds its own inputs can see one.

## Running it

Nothing here runs by default. `pytest` on a laptop or in the unit-test matrix
must stay cluster-free, so `conftest.py` skips every item in this directory
unless **both** of these hold:

* `PODBENCH_E2E=1` is set, and
* `kubectl get --raw /version` succeeds.

Asking for the env var alone would turn "I forgot to start kind" into a wall of
timeouts. Items are still *collected* — `--collect-only` lists them, and `-rs`
prints the skip reason — so "the e2e suite did not run" is visible rather than
indistinguishable from "there are no e2e tests".

```bash
export PODBENCH_E2E=1
export PODBENCH_IMAGE=ghcr.io/gilesknap/podbench:main   # or a locally loaded tag
uv run --no-sync pytest tests/e2e -v
```

| variable | meaning |
|---|---|
| `PODBENCH_E2E` | the opt-in. `1`/`true`/`yes`/`on` |
| `PODBENCH_IMAGE` | image under test. Defaults to `podbench.model.DEFAULT_IMAGE` |
| `PODBENCH_E2E_NODE_SELECTOR` | `key=value[,key=value]` pinned onto every pod the suite creates. Needed on a mixed-architecture cluster when the image under test is single-arch: without it the probe pod lands on a node that cannot run the image and the whole suite skips with `no match for platform in manifest`. E.g. `kubernetes.io/arch=amd64` |
| `PODBENCH_E2E_CONTEXT` | kubeconfig context. Deliberately *not* `current-context`: a developer's default is usually a real cluster, and these tests create containers with `CAP_SYS_PTRACE` |
| `PODBENCH_E2E_KUBECTL` | kubectl binary, if it is not on `PATH` as `kubectl` |

No knob turns a module on or off. Where a test needs something of the cluster
that the rest of the suite must *not* have — `test_dls_ioc.py` needs
`kernel.yama.ptrace_scope=0`, which would make S3 pass for the wrong reason —
the module reads the property and skips itself, and CI gives it a runner of its
own rather than an environment variable. An env-var switch would mean a green
run that quietly tested less, which is the failure mode this whole directory
exists to avoid.

### Testing an image built from your branch

Half of podbench ships *inside* the image — `image/bin/*`, and `podbench agent`
itself — and none of it can be exercised from a checkout: the devcontainer has
no docker, and the image the e2e job side-loads into kind never leaves the
runner. So a change to the image is only reviewable once a registry has it.

Pushing the branch is what publishes it. Every branch push runs the same build,
smoke tests and multi-arch merge as `main`, and tags the result after the
branch — the next release's SemVer with the branch appended as a further
prerelease identifier:

```bash
git push -u origin my-branch          # CI builds and publishes
just e2e ghcr.io/gilesknap/podbench:0.1.0-beta.4-my-branch
```

Two properties are deliberate. The tag sorts *after* the release it descends
from and can never collide with it, so a branch image is unmistakably not a
release; and `latest` and `main` are asserted unreachable from a branch build,
because `main` is `podbench.model.FLOATING_TAG` — the tag every launcher run
from a checkout pulls.

The branch tag **moves with every push**. A run that pulls it twice can get two
different images, and losing that race inside an ephemeral container is not
recoverable. Every branch build therefore also publishes `sha-<full-sha>`; pin
that when a run must be reproducible.

## What it touches

Every namespace is created by the suite, named `podbench-e2e-<random>`, and
deleted in a `finally` — one per test module, plus one for the image smoke
check, plus one labelled `pod-security.kubernetes.io/enforce=restricted` for
S5. Deletion does not block; the API server has accepted it by the time the run
ends.

**One exception, and it is cluster-scoped.** Two modules bind admission
policies, which are not namespaced objects. `test_dls_ioc.py` applies
`podbench-strip-sys-ptrace` (a `MutatingAdmissionPolicy`) and
`podbench-deny-sys-ptrace` (a `ValidatingAdmissionPolicy`), each with its
binding, and deletes all four in a session-scoped `finally`;
`test_nonroot_gid_identity.py` applies and deletes the *deny* pair only, which
is all it needs against a non-root target. Both fixtures are session-scoped for
the teardown rather than the setup: the names are fixed, so a concurrent apply
is idempotent while a concurrent delete is not, and the two co-running leave
nothing behind (measured on the k3s bed, whole suite in one session).

No policy here does anything to a namespace that has not opted in by label, so a
cluster that keeps them after a killed run is not silently changed — but they are
the one thing this suite leaves outside `podbench-e2e-*`.

If a run is killed hard enough to skip teardown:

```bash
kubectl get ns -o name | grep podbench-e2e- | xargs -r kubectl delete
kubectl delete mutatingadmissionpolicybinding/podbench-strip-sys-ptrace \
                mutatingadmissionpolicy/podbench-strip-sys-ptrace \
                validatingadmissionpolicybinding/podbench-deny-sys-ptrace \
                validatingadmissionpolicy/podbench-deny-sys-ptrace \
                --ignore-not-found
```

The `type/name` slashes are load-bearing. `kubectl delete
mutatingadmissionpolicybinding A mutatingadmissionpolicy B` reads *both* words
as names of a binding: it deletes the binding, says nothing, and leaves the
policy on the cluster.

## Prerequisites beyond a cluster

* **The podbench image must be pullable by the cluster.** It is checked once,
  by running `podbench --version` in a throwaway pod; when that fails the whole
  suite *skips* with the image name in the reason, rather than failing four
  different ways. On kind, `kind load docker-image` it first.
* **An ssh client and `ssh-keygen`** on the machine running pytest (S1 only).
  A throwaway ed25519 key is generated per run — the suite never authorises a
  developer's real key inside a container.
* **Egress from the cluster** to Docker Hub / gcr.io / ghcr.io for the demo
  images, to Debian's apt mirrors (the distroless target's initContainer
  compiles its binary), and to PyPI (S4's `uv pip install -e .` fetches a build
  backend, and `test_dls_ioc.py`'s `--provision` downloads a debugpy wheel).
* **`kernel.yama.ptrace_scope=0` on the node, for `test_dls_ioc.py` only.**
  Issue #89 is a seat with no `CAP_SYS_PTRACE` that can ptrace its target
  anyway, which is classic same-uid ptrace and exists only at scope 0. Debian,
  Ubuntu and the GitHub runner images all ship `1`, where a capless seat
  genuinely cannot attach — so at the default there is no defect to regress and
  the module **skips**, naming the sysctl. Nothing here sets it for you: it is
  global to a kernel, kind nodes share the host's, and forcing it to 0 would
  make S3 pass without `CAP_SYS_PTRACE` doing anything (`kind.yaml` says so at
  length). To run that module locally:

  ```bash
  sudo sysctl -w kernel.yama.ptrace_scope=0     # and put it back afterwards
  ```

  In CI it has a job of its own, `e2e-dls` in `ci.yml`, which sets the sysctl,
  checks it took, and runs that one file.
* **A `MutatingAdmissionPolicy` at `admissionregistration.k8s.io/v1`, for
  `test_dls_ioc.py` only.** The field shape #89 comes from is an engine that
  *strips* `SYS_PTRACE` rather than refusing it; a refusing policy lands no seat
  at all on a root target, so there is nothing left to mis-describe. A cluster
  serving the policy only at `v1alpha1`/`v1beta1`, or not at all, skips the
  module. kind 0.32 (Kubernetes 1.36.1) serves it, which is why `_e2e.yml` pins
  that kind version rather than taking the action's default.

## The tests

| file | proves | what breaks if it is deleted |
|---|---|---|
| `test_s1_transport.py` | S1 | the ssh transport's exact shape: a remote command, a >1 MB byte-identical stream, a second concurrent session — and the two negatives, which are the point |
| `test_s3_gdb.py` | S3 | gdb's five-element incantation and its *order*, against a target with no shell |
| `test_s4_iterate.py` | S4 | the authored dev pod, the editable install, the relaunch, and the edit being visible through the Service |
| `test_s5_ladder.py` | S5 | the two-rung ladder under `restricted` PSA, and the invalid rung never being authored |
| `test_uvx_detached.py` | #1 | that a seat outlives the launcher. The obvious tidy-up — routing the ProxyCommand through `podbench proxy` — leaves every other test green and breaks `uvx`, because a developer running from a clone has podbench on `PATH` and the user it breaks does not |
| `test_dls_ioc.py` | #89 | nothing else can tell whether a verb answers from the probe or from the capability bit. Every unit test here builds the `Seat` itself, so it can only check that a stated field is read; that a seat lands capless *and attaches anyway* takes a real admission policy and a real kernel. Delete it and `debug-config` can go back to refusing an injection that works, and `status` to calling a live-attaching seat "read-only", against every Diamond-shaped cluster and no test |
| `test_nonroot_gid_identity.py` | #102 | that a seat on a target with a non-zero *gid* gets a login at all. Every unit test injects the passwd path and fakes NSS, so none of them can see the two things that actually decide it: that the image really installs a second NSS source and consults it, and that the real `libnss-extrausers` has the `MINUID`/`MINGID` 500 floors `agent.extrausers_serves` carries as literals. Delete it and a rebuild that shipped different floors would route seats to a database that will not answer them — the append succeeds, the record is in the file, and nothing resolves — or the fallback that keeps `--seat-gid-root` working at gid 0 could go and only Diamond would find out |
| `test_shadowed_exec_file.py` | #90 | the one assertion that gdb read the *target's* interpreter and not the seat's copy at the same absolute path. It is checked by content — three checksums — because the defect is invisible in the command sequence: podbench names a plausible path, gdb canonicalises `/proc/<pid>/root` away and opens the seat's file, and on two builds close enough not to error there is no message at all, just the wrong symbols |

### Why CI runs this directory twice

`ci.yml` calls `_e2e.yml` twice. `e2e` builds a kind cluster from `kind.yaml`
and runs everything at whatever `kernel.yama.ptrace_scope` the runner shipped —
`1`, where `CAP_SYS_PTRACE` is doing real work, which is the case S3 is about.
`e2e-dls` sets the sysctl to `0`, reads it back, and runs `test_dls_ioc.py`
alone.

Two runners for one sysctl looks extravagant, and the alternatives were both
worse. Setting `0` in `kind.yaml` would apply it to every test, and S3 would
then pass whether or not the capability was ever granted. Skipping
`test_dls_ioc.py` unless the scope happens to be `0` costs nothing to write and
means CI never runs it — a regression test that has quietly stopped being one,
which is the exact failure this suite exists to prevent. The sysctl is a
property of a kernel and kind nodes share the runner's, so there is no third
option inside one job.

The `e2e-dls` job's `sysctl -w` is followed by a read-back that fails the job on
a mismatch, for the same reason: a hardened runner that silently ignored it
would leave a green job that tested nothing.

### The two negatives in S1

`test_fd2_teardown_truncates_the_exec_stream` reproduces report §3.1's
sshd-free experiment: replacing fd 2 in a `kubectl exec`'d process tears down
the whole CRI exec session, so a delayed second line is lost and the command
still exits `0`. That is the general fact — anything podbench streams over
`kubectl exec` inherits it.

`test_transport_dies_without_dash_e` is the consequence, and is the test report
§4.1 asks for by name. `-e` reads like a logging flag, `-o LogLevel=ERROR`
already silences the logs, and the obvious tidy-up is to drop it; the transport
then fails at KEX with `Connection to UNKNOWN port 65535: Broken pipe` and the
next week goes into the network.

**Note R1.** The phase-0 report records an unresolved contradiction: S2 ran
without `-e` for an entire spike and never hit the teardown. If either negative
test ever *fails* — i.e. the transport survives without `-e` — the honest
response is to record the cluster, containerd and kubectl versions and settle
R1, not to delete the flag. Both tests say so in their failure messages.

## Status

The spike modules run on every push, against a kind cluster built from
`kind.yaml` with the image from that commit side-loaded into it.

The two issue modules are newer and their evidence is thinner. Both were written
and made to fail — then pass — on a single-node k3s bed (Kubernetes 1.36,
`ptrace_scope=0`, AppArmor off) rather than on kind, because that is where the
Diamond shape could be reproduced. Two consequences worth knowing before reading
a red run:

* `test_dls_ioc.py` has never run on kind. `e2e-dls` is its first CI job, and
  the first thing to check on a failure there is the `-rs` output: a cluster
  that does not serve `MutatingAdmissionPolicy` at `v1`, or a runner whose
  sysctl did not take, skips or fails the job for an environment reason and
  says which.
* `test_shadowed_exec_file.py` asserts a *collision* — the seat's own
  interpreter sitting at the same absolute path as the target's — and its first
  test exists to fail loudly if a future image stops creating one. That is not a
  regression in the fix; it means the fixture needs a target that collides with
  whatever the image ships instead.
