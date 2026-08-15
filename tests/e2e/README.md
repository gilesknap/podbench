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
export PODBENCH_IMAGE=ghcr.io/gilesknap/podbench:latest   # or a locally loaded tag
uv run --no-sync pytest tests/e2e -v
```

| variable | meaning |
|---|---|
| `PODBENCH_E2E` | the opt-in. `1`/`true`/`yes`/`on` |
| `PODBENCH_IMAGE` | image under test. Defaults to `podbench.model.DEFAULT_IMAGE` |
| `PODBENCH_E2E_NODE_SELECTOR` | `key=value[,key=value]` pinned onto every pod the suite creates. Needed on a mixed-architecture cluster when the image under test is single-arch: without it the probe pod lands on a node that cannot run the image and the whole suite skips with `no match for platform in manifest`. E.g. `kubernetes.io/arch=amd64` |
| `PODBENCH_E2E_CONTEXT` | kubeconfig context. Deliberately *not* `current-context`: a developer's default is usually a real cluster, and these tests create containers with `CAP_SYS_PTRACE` |
| `PODBENCH_E2E_KUBECTL` | kubectl binary, if it is not on `PATH` as `kubectl` |

## What it touches

Every namespace is created by the suite, named `podbench-e2e-<random>`, and
deleted in a `finally` — one per test module, plus one for the image smoke
check, plus one labelled `pod-security.kubernetes.io/enforce=restricted` for
S5. Nothing outside those namespaces is read or written. Deletion does not
block; the API server has accepted it by the time the run ends.

If a run is killed hard enough to skip teardown:

```bash
kubectl get ns -o name | grep podbench-e2e- | xargs -r kubectl delete
```

## Prerequisites beyond a cluster

* **The podbench image must be pullable by the cluster.** It is checked once,
  by running `podbench --version` in a throwaway pod; when that fails the whole
  suite *skips* with the image name in the reason, rather than failing four
  different ways. On kind, `kind load docker-image` it first.
* **An ssh client and `ssh-keygen`** on the machine running pytest (S1 only).
  A throwaway ed25519 key is generated per run — the suite never authorises a
  developer's real key inside a container.
* **Egress from the cluster** to Docker Hub / gcr.io for the demo images, to
  Debian's apt mirrors (the distroless target's initContainer compiles its
  binary), and to PyPI (S4's `uv pip install -e .` fetches a build backend).

## The tests

| file | spike | what breaks if it is deleted |
|---|---|---|
| `test_s1_transport.py` | S1 | the ssh transport's exact shape: a remote command, a >1 MB byte-identical stream, a second concurrent session — and the two negatives, which are the point |
| `test_s3_gdb.py` | S3 | gdb's five-element incantation and its *order*, against a target with no shell |
| `test_s4_iterate.py` | S4 | the authored dev pod, the editable install, the relaunch, and the edit being visible through the Service |
| `test_s5_ladder.py` | S5 | the two-rung ladder under `restricted` PSA, and the invalid rung never being authored |

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

Written against the module APIs as they stand, and **not yet executed end to
end**: at the time of writing the podbench image is not published, so the
`podbench_image` fixture skips the whole suite. What has been exercised is the
gate (default `pytest` stays clean), collection, typing and lint. Treat the
first green run against a published image as the real acceptance.
