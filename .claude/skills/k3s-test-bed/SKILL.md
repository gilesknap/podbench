---
name: k3s-test-bed
description: The persistent single-node k3s box podbench is developed against, the edit-sync-run loop it imposes, and the seven ways a run on it silently tests the wrong thing. Read before reproducing a field defect, running the e2e suite outside CI, or testing anything that needs a real kernel.
---

# The k3s test bed

A single-node k3s VPS kept up between sessions, reachable as **`ssh podbench-bed`**.
It exists because the alternative is diagnosing at a distance: issues #87-#90 were all
found at Diamond, one command per round trip through a human's terminal, and #90's first
cause was filed confidently and then **falsified in the field**. A cluster an agent can
drive directly turns guessing into observing.

It is not a replacement for CI. kind still runs the e2e suite on two nodes at the
default `ptrace_scope`, and that difference is load-bearing — see "the sysctl" below.

## Getting on it

```sh
ssh podbench-bed true
```

The alias, the host and the key (`~/.ssh/podbench_bed_ed25519`) live in the maintainer's
`~/.ssh/config`, deliberately not in this repo. The key is a file on disk, so it survives
a context clear but **not a devcontainer rebuild**. If that command fails, mint a
replacement and *stop* until the user has authorised the public half — there is no way
around it and guessing at one wastes a session:

```sh
ssh-keygen -t ed25519 -N '' -C podbench-test-bed -f ~/.ssh/podbench_bed_ed25519
cat ~/.ssh/podbench_bed_ed25519.pub     # hand this to the user
```

**The box has podman, not docker.** Nothing here uses `docker` or `kind`.

## The loop: edit here, sync, run there

The bed carries its own podbench clone. **Never edit it.** Edits made there are invisible
to the user's review, are not in git, and are silently destroyed by the next sync — the
failure looks like a fix that stops working for no reason.

The clone is at `/root/podbench`. There is **no `rsync` in the devcontainer**, so the sync
is tar over ssh — `--overwrite` because tar otherwise refuses to replace a file it does
not own:

```sh
tar -C <checkout> --exclude=.git --exclude=.venv --exclude=__pycache__ \
    --exclude=.pytest_cache -cf - . \
  | ssh podbench-bed 'tar -C /root/podbench --overwrite -xf -'
```

Then run on the bed. `KUBECONFIG` has to be spelt out: it is exported from
`/etc/profile.d` and `.bashrc`, and a non-login `ssh podbench-bed '<cmd>'` sources
neither, so a command that omits it fails as though the cluster were down.

```sh
ssh podbench-bed 'cd /root/podbench && KUBECONFIG=/etc/rancher/k3s/k3s.yaml \
  PODBENCH_E2E=1 PODBENCH_IMAGE=docker.io/library/podbench:e2e \
  uv run --no-sync pytest tests/e2e -v -rs'
```

`tests/e2e/README.md` documents the rest of the env vars. The bed is single-arch amd64, so
`PODBENCH_E2E_NODE_SELECTOR` is not needed.

Only the *launcher* half of podbench lives in `src/` and is picked up by a sync alone.
The other half ships inside the image.

## Seven ways a run here silently tests the wrong thing

### 1. A stale side-loaded image

Anything under `image/` or the `Dockerfile` only reaches the cluster through a rebuild
and a re-import:

```sh
ssh podbench-bed 'cd /root/podbench && podman build -t docker.io/library/podbench:e2e . \
  && podman save docker.io/library/podbench:e2e | k3s ctr images import -'
```

The tag is `docker.io/library/podbench:e2e`, matching what `_e2e.yml` side-loads into kind,
and the fixtures use `imagePullPolicy: IfNotPresent` so nothing ever goes to a registry for
a tag that exists nowhere else.

This is the k3s equivalent of the `kind load docker-image` step in
`.github/workflows/_e2e.yml`. Skip it after an `image/bin/` change and the cluster keeps
serving the previous copy: the test exercises the old code and you conclude the fix does
not work. Same trap as the moving branch tag described in `tests/e2e/README.md`, and it
fails the same way — quietly, with a plausible result.

### 2. The sysctl, which is the most important knob on the box

`kernel.yama.ptrace_scope` decides what the whole ladder is allowed to do.

```sh
ssh podbench-bed 'sysctl -w kernel.yama.ptrace_scope=0'   # reproduce the DLS node
ssh podbench-bed 'sysctl -w kernel.yama.ptrace_scope=1'   # the CI default
```

**`sysctl -w` is runtime only, and the bed's default is not the distro's.**
`/etc/sysctl.d/99-podbench-bed.conf` holds the scope at 0 across reboots — Ubuntu ships 1
and nothing else on the box sets it — so a flip to 1 lasts until the next boot and is then
silently undone. Always read it back (`sysctl kernel.yama.ptrace_scope`) rather than
trusting the write, and for a lasting change edit that file and `sysctl --system`.

At **0** — matching the measured DLS node — a degraded seat with no `CAP_SYS_PTRACE` can
still attach uid 0 to uid 0 by classic ptrace rules. That is the only setting at which
#89 is visible at all: at 1 the degraded rung genuinely cannot attach, the probe fails
honestly, and the bug disappears.

At **1**, S3 and S5 are meaningful. `kind.yaml`'s comments explicitly refuse to force
scope 0 cluster-wide because doing so **makes S3 pass for the wrong reason**. A green
suite at scope 0 therefore proves less than it looks like it proves.

So: reproduce at 0, but **the acceptance run is at 1**. Any change to how capability is
reported has to be green at both, because turning "no capability and Yama says no" into a
false positive is the overclaim S5 and issue #51 exist to prevent. It is the one way this
kind of work does real harm. Always say which setting a result was obtained under.

**Yama gates `PTRACE_ATTACH` only — never `PTRACE_MODE_READ`.** Measured 2026-08-17: at
scope 1, a session whose uid *and* gid match the target read `maps`, `environ`, `exe` and
`root` while the attach was refused, purely because it was not an ancestor. So a seat can
hold complete `/proc` access and still not attach, which reads like a contradiction and is
not one — it is `Verdict.READ_ONLY` (`model.py`), and `--provision` deliberately warns
rather than refuses there, because the tree lands in the target's own rootfs and outlives
the seat. Do not "fix" that apparent inconsistency.

### 3. AppArmor, which denies ptrace before Yama is even consulted

The bed runs with `apparmor=0` on the kernel command line, and that is deliberate. Ubuntu
26.04 converts unprivileged `change_profile` into profile **stacking**, so a
kubectl-exec'd process lands in the label `cri-containerd.apparmor.d//&unconfined`, which
no longer matches the containerd default profile's own same-profile peer rules. The
profile then denies `ptrace` **and** `signal`:

```
apparmor="DENIED" operation="ptrace" profile="cri-containerd.apparmor.d" comm="podbench"
  requested_mask="trace" peer="cri-containerd.apparmor.d//&unconfined"
```

Every container, root or not, `CAP_SYS_PTRACE` or not. The suite does not fail cleanly —
six S1 tests error on a fixture and S3 wedges for thirteen minutes, because a denied
`kill(2)` also orphans a probe child onto the exec pipes (issue #92). Worse, it makes a
capless seat unable to attach for a reason that has nothing to do with the capability, so
**#89 vanishes exactly as it does at `ptrace_scope=1`** and a green run proves nothing.

Turning it off is the faithful choice, not a workaround: the modelled DLS node is RHEL 9
with SELinux and no AppArmor. Check it before believing a result —
`cat /sys/module/apparmor/parameters/enabled` must read `N` — and check `dmesg` or the
audit log for `apparmor="DENIED"` whenever an attach fails for no visible reason. Any
Ubuntu-noded cluster has this; the DLS one does not.

### 4. Cluster-scoped leftovers

The repo rule is that podbench never leaves anything behind, and the suite honours it for
namespaces (`podbench-e2e-*`, deleted in a `finally`). **A `ValidatingAdmissionPolicy`
and its binding are cluster-scoped and do not go away when a namespace does.** A leaked
binding silently changes what every later run is admitted to do — a subsequent seat lands
degraded and the reason is nowhere in that run's output.

**And `kubectl delete` will lie to you about having removed them.** Given two kinds and
two names as bare words, it parses everything after the first word as names of the *first*
kind: it deletes the binding, says nothing about the rest, and leaves the policy on the
cluster. Use the slash form.

```sh
# WRONG — deletes the binding only, silently
kubectl delete validatingadmissionpolicybinding NAME validatingadmissionpolicy NAME
# right
kubectl delete validatingadmissionpolicy/NAME validatingadmissionpolicybinding/NAME
```

Teardown of those two objects also belongs in a **session**-scoped fixture, not the
module fixture that made the namespace. The names are fixed so a concurrent `apply` is
idempotent, but a concurrent `delete` is not — and deleting the namespace merely un-scopes
the policy (the label goes with it) without removing anything.

```sh
ssh podbench-bed 'kubectl get ns -o name | grep podbench- | xargs -r kubectl delete'
ssh podbench-bed 'kubectl get validatingadmissionpolicybindings,validatingadmissionpolicies'
```

Check the second one when a result surprises you.

### 5. One node, and `hostNetwork`

The bed is deliberately single-node, unlike `kind.yaml`'s two. Every property tested here
is node-local and the kind suite still covers the cluster-wide-cache case, so this is a
sound trade — but it means a `hostNetwork: true` fixture (the DLS IOC is one, because
EPICS Channel Access needs UDP broadcast) has the node's port space entirely to itself.
Two such pods clash, and issue #87 is about exactly that. One at a time.

### 6. A tracee that dropped its own privileges

The one on this list that is not about the box at all — it is about the fixture, and it
produced two consecutive false negatives on 2026-08-17 before it was spotted.

**A process that reaches its uid via `setuid()` from root is left non-dumpable, and
tracing a non-dumpable process needs `CAP_SYS_PTRACE` whatever the credentials.** So a
target built by dropping privileges refuses every attach with `EPERM` — which is
indistinguishable from the credential failure you set out to measure, and just as
plausible. `setpriv --reuid 1000 --regid 1000` has the same problem by a different route:
the flag is cleared at `execve`.

The tell is ownership of the target's own `/proc` files:

```sh
stat -c %u:%g /proc/<pid>/status   # 0:0 => non-dumpable, the fixture is lying to you
                                   # 1000:1000 => dumpable, like a real workload
```

A **kubelet-started** container process at that uid is dumpable, which is why this never
appears against a real fixture pod and only ever bites a hand-rolled one. Prefer a real
container at the uid you want; where that is impractical, neutralise both other gates in
the tracee so only the credential check is left standing:

```c
prctl(PR_SET_DUMPABLE, 1);                    /* undo the setuid flag */
prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY);    /* take Yama out — see 2 */
```

With those two in place the credential matrix is clean: uid **and** gid matching succeeds,
and a gid mismatch alone denies in either direction (measured 2026-08-17). Without them
every case denies and the matrix reads as though the kernel ignores credentials entirely.

### 7. A synced tree with a stale git HEAD

**A build can be genuinely fresh while `podbench --version` inside the image reports the
version of whatever the bed's own clone last had checked out.** The sync excludes `.git`,
so setuptools-scm on the bed derives the version from the bed clone's stale HEAD, not from
the files the tar just overwrote — and it does so silently, because the exclusion is
otherwise exactly right (`.git` has no business travelling with source).

Measured 2026-08-21 01:00: a freshly built image reported `0.2.0b3.dev4+g99a3312b1` — the
tip of PR #86, the bed clone's stale HEAD — while every module inside that image was 36
commits newer. This defeats the exact check `.claude/plans/attach-endgame.md` §7 tells you
to run to prove freshness: on the bed, after a plain sync, `podbench --version` certifies a
stale build as fresh. It is not the moving-tag trap in `tests/e2e/README.md` and this repo's
own CLAUDE.md — that one serves a stale *image layer*; this one serves a fresh layer under a
stale *version string*, so the two traps fail in opposite directions.

Prove freshness with content, not the version string: md5 a module inside the image against
the laptop's copy (`/app/.venv/lib/python3.11/site-packages/podbench/<module>.py` in the
image), or grep the image for a symbol that cannot exist in the old tree. Tonight
`STREAMED_SUBCOMMANDS` and `KubectlTimeoutError` both grepped 0 on the bed before the sync
and non-zero inside the new image.

Fix for the branch in front of you: `git fetch origin <branch> && git reset <sha>` on the
bed, leaving the working tree untouched — the sync just wrote it. That repairs `--version`
for that branch only; a later sync from a different branch reintroduces the mismatch.

## What the bed does not model

Worth stating, because a green run here is not a green run at Diamond:

- **Kyverno.** The real cluster refuses the `SYS_PTRACE` rung via Kyverno; the bed uses a
  native `ValidatingAdmissionPolicy` to force the same ladder walk. That models
  "somebody else's policy refuses the full rung" faithfully and Kyverno's own behaviour
  not at all.
- **Argo CD.** The DLS workload is managed by an app-of-apps, so anything podbench
  mutates in place is subject to being reconciled away. Nothing here reproduces that.
- **arm64.** Single arch. debugpy publishes no aarch64 Linux attach helper (issue #20),
  so the architecture axis cannot be exercised on this box at all.
- **The real IOC image's environment.** `tests/e2e/apps/dls-ioc.yaml` reproduces the
  measured *shape* — `hostNetwork`, uid 0, and an app that is **not PID 1** because it
  sits behind a two-deep wrapper chain. It does not reproduce the beamline.

## Related

`tests/e2e/README.md` for the env vars, the gate and what each module protects.
`docs/explanations/spikes/phase0-report.md` §4 for the constraint checklist the suite
encodes, and §5 for what is still unproven. CLAUDE.md's rule stands here unchanged:
never mutate a cluster outside a scratch namespace.
