# The claim holds the seat, not just the fix

Hotfix mode's claim is mounted **at the git checkout**. So the checkout root is
the claim root, and podbench already writes two of its own artefacts inside
somebody's source tree — `.venv` and `.uv-cache`, both named by `install_argv`
(`src/podbench/hotfix.py:1098`). That is backwards today, before anything new is
added to it.

This plan gives the claim a layout — `app/`, `venv/`, `uv-cache/`, `home/` —
and then uses the room that creates to move the seat's home off ephemeral
storage and onto the claim. It closes #42 as a side effect and re-scopes #69.

- **Base:** `main` at `cf4fcd2`.
- **Decided:** 2026-08-24, in conversation. The design arguments are recorded in
  "Decisions taken" below because they are **not** in the issues — #69 and #70
  both currently propose something this plan rejects.
- **No migration, at all.** podbench is a prototype with no users. Existing
  claims are **deleted**, not migrated; existing hotfixed pods are redeployed.
  Any phase that finds itself writing a compatibility path has misread this.
- **Evidence discipline:** unchanged — *measure the seat, do not restate the
  request.* Every phase states what would falsify it. Where a measurement could
  not be taken, say so; never "fine".

---

## Decisions taken, and not to be re-litigated

**The seat does not become a declared sidecar. #70 is rejected.**
Attach mode's ephemeral-container ladder is worth more than what a sidecar buys,
and the sidecar's own case has been eroding: #102 (closed) removed a third of it
by giving the seat a writable NSS source, so the identity ConfigMap it was
supposed to "finally make usable" should simply go. What remains costs a
permanently-root container with `CAP_SYS_PTRACE` in a production pod plus
pod-level `shareProcessNamespace`.

**#69 item 3 (selector-resolving `ProxyCommand`) dies with it.** Ephemeral
containers do not come back after a reschedule, so there is no seat in the
replacement pod to resolve *to*. #69 calls items 1-3 independent; they are
independent mechanically, not in value.

**#69 item 1 is kept, but not as #69 describes it.** #69 and #70 both propose
swapping the `podbench-home` `emptyDir` for the **scratch** PVC. Do not. That
adds a second claim to the *application's* pod spec, which drags in RWO
scheduling constraints, a rolling-update stall, and a scope mismatch — the
scratch claim is per-user/per-namespace scaffolding, while `podbench-home` must
be declared per-workload. The hotfix claim is **already** in that pod spec,
already mounted into the seat, and the seat is already told where it is via
`PODBENCH_CLAIM_PATH` (`spec.py:335`). Use the volume that is already there.

**#42 is subsumed, not a prerequisite.** #42 is that a full-rung seat runs as
root, sshd takes `$HOME` from the passwd record, root's says `/root`, and
`podbench-home` is silently orphaned. A redirect acts on the *path sshd
resolves*, not on the record, so Phase 3 fixes it. This matters more than it
looks: `libnss-extrausers` ignores uid/gid below 500, so the #102 mechanism can
**never** give uid 0 a record. Redirecting the path is the only route to a root
seat's home.

**Attach mode gains nothing here, and that is accepted.** `_claim_path_env`
returns `{}` for "every ordinary `attach` seat". `podbench-home` stays as the
attach-mode fallback. This plan improves the mode ranked second.

---

## How to work this plan

**One phase, one branch, one PR. Never two in flight.** Phase 2 defines the
paths Phase 3 symlinks into; landing them out of order means guessing.

**Cite `path:line`, never paste the file.** Anchors are listed per phase.

**Delegate read-only sweeps; keep the conclusion, not the transcript.**

**`just check` in the worktree, not `uv run`** — `UV_PROJECT_ENVIRONMENT` is
exported by the devcontainer and points elsewhere. Each worktree has its own
`.venv` after `just sync`.

---

## Verifying on a cluster — two beds, and why it is two

**The harness already exists.** `k8s/hotfix-harness/podbench-test-fastcs.yaml`
is a duplicate of `bl47p-ea-fastcs-01-0` built for exactly this, and its five
rules are load-bearing, not decoration: `hostNetwork: false`, the three shared
claims dropped, **the PV prefix renamed** so it cannot answer for a live IOC,
a `podbench-test-*` name plus a `podbench.dev/test-duplicate: "true"` label so
the set is greppable, and deletion at the end of the phase. Reuse it; do not
re-derive it, and do not relax rule 3 — a second IOC answering a live PV prefix
is a beamline problem, not a test problem.

It is a bare `kubectl apply`d Pod carrying no `app.kubernetes.io/instance`, so
Argo never tracks it and never prunes it. That is the ArgoCD independence this
plan wants, achieved by omission. **Keep it that way**: anything rendered
through a chart in `p47-services` inherits the tracking label and becomes Argo's
to prune.

**One adaptation is required.** The harness's claim is an
`ephemeral: volumeClaimTemplate`, which is deleted with the pod. Phase 3's whole
proposition is *the home survives a pod replacement*; against an ephemeral claim
that is untestable **by construction**, and it would read as the feature failing
rather than the harness being wrong. These phases need a standalone PVC.

**And p47 cannot prove all of it.** Every attach recorded on p47 lands
`rung degraded — uid 37887, gid 37887, CapEff 0000000000000000`
(`.claude/evidence/phase8-why-the-adapter-never-answers.md:83`). #42 bites only
on the **full** rung, where the seat is root and sshd reads `/root` from the
image's passwd. So:

| bed | what only it can prove |
|---|---|
| p47 harness, degraded rung | the layout, the non-root home via the passwd record, netapp + LimitRange realism, Argo non-interference |
| k3s bench (`k3s-test-bed` skill) | the root-seat redirect — #42's actual fix, which p47 never exercises |

**This deviates from a hard rule, deliberately.** CLAUDE.md says cluster testing
happens in `podbench-*` namespaces created for the purpose and deleted
afterwards; the harness runs in `p47-beamline`, which is not one. Decided by the
user, 2026-08-24, on the grounds that the realism that matters here — netapp,
the LimitRange, PSA, the real image, a live Argo — is exactly what a scratch
namespace does not reproduce. The five duplicate rules above **are** the
compensating control, which is why none of them is negotiable. Do not "correct"
this back to a scratch namespace, and do not drop the rules because the
namespace is now the real one.

**Prove which build you are testing before trusting any cluster result.** Every
branch push overwrites the same `0.1.0-beta.N-<slug>` tag and a seat pulls
`IfNotPresent`, so a node that pulled it an hour ago silently serves the old
layer. `--pull always`, or pin the index digest.

**Write evidence to `.claude/evidence/`, link it from the PR.** Measure the
seat; do not restate the request. Where a measurement could not be taken, say
so — never "fine".

---

## Phase 1 — the claim default becomes 10Gi

Independent of everything else, zero risk, so it goes first and gets out of the
way. 2Gi was sized for "a python-copier-template checkout plus a venv"
(`Charts/podbench-hotfix-claim/values.yaml:27`). Real targets are not all that
small, storage is the cheap resource, and by the end of this plan the claim also
holds a uv cache and a seat home.

Six live copies of `"2Gi"` and they must not drift — introduce one constant and
derive the rest. **Do not touch `SEAT_HOME_SIZE` (`hotfix.py:277`)**: that is
the `emptyDir` `sizeLimit`, a different 2Gi that bounds the node's ephemeral
storage and has to stay tight.

`Charts/podbench/values.schema.json` is generated by a pre-commit hook from
`values.yaml` plus `example.values.yaml`. Edit those; hand-edits are reverted.

**Verify:** no cluster. `just check`, plus `helm template` on both charts
showing 10Gi, plus the regenerated `values.schema.json` surviving a second
pre-commit run unchanged. `k8s/hotfix-harness/podbench-test-fastcs.yaml`
hard-codes `storage: 2Gi` and is hand-written, so it moves with this phase.

**Falsified if:** the emptyDir's `sizeLimit` moves, or the schema regenerates to
something the hook then reverts, or any copy is left saying 2Gi.

**Anchors:** `hotfix.py:3346` (`values_snippet` default), `hotfix.py:5834`
(`--size`), `Charts/podbench-hotfix-claim/values.yaml:29`,
`Charts/podbench-hotfix-claim/templates/pvc.yaml:62`,
`Charts/podbench/templates/pvc-hotfix-project.yaml:43`,
`Charts/podbench/values.yaml:103`, `Charts/podbench/example.values.yaml:26`,
`tests/test_hotfix.py:3522`, `docs/reference/cli.md:1244` (hand-maintained, no
generator).

---

## Phase 2 — the claim gets a layout, and the checkout stops being its root

Mount the claim at `/podbench`. Beneath it:

```
/podbench/          claim root — podbench's territory
├── app/            the checkout, and nothing else in it
├── venv/
└── uv-cache/
```

`HOTFIX_APP_PATH` (`model.py:787`) is currently doing three jobs at once, and
they have to be separated into **two derived constants, never two literals**:

* **The mount path**, which is also the **discovery key**. `hotfix status` finds
  hotfixed pods by scanning for a `mountPath` of `/podbench/app`
  (`hotfix.py:1728`), and `VENV_INVISIBLE_TO_STATUS` (`hotfix.py:1237`) states
  the coupling out loud: "the seed, the copied interpreter and the supervisor's
  runtime switch all name it".
* **The checkout path**, which is what git, the seed and the supervisor's
  `PYTHONPATH` care about. `checkout_path()` (`hotfix.py:604`) is the place to
  derive one from the other.

Three things that fail *silently* if missed:

* **`safe.directory` must name the checkout, not the claim root.**
  `ensure_seat_gitconfig` (`agent.py:1295`) authorises `PODBENCH_CLAIM_PATH`.
  Once that path is no longer the repository, git refuses it —
  `fatal: detected dubious ownership` — and the failure lands mid-`apply`,
  after the edit has been made. Measured on the beamline 2026-08-22.
* **The supervisor's runtime switch** (`hotfix.py:3517`) names
  `{checkout}/{venv}/bin/python`. Left pointing at a venv that moved, the pod
  runs the image's code under a version string claiming the fix — the failure
  this whole mode exists to prevent.
* **`install_argv`'s doctests** (`hotfix.py:1098`) bake in
  `/podbench/app/.uv-cache` and `/podbench/app/env`. pytest runs
  `--doctest-modules`, so they are executable spec.

One simplification falls out: `UV_PROJECT_ENVIRONMENT` is currently set only
when the venv is not uv's default `.venv`. With the venv outside the checkout it
is **always** set, which removes that conditional and the "left to disagree, the
pod quietly runs the image's code" branch with it.

Delete every existing claim on the bench and on p47 rather than reasoning about
what an old layout looks like under a new mount.

**Verify:** p47 harness, with the claim converted to a standalone PVC. Deploy
the duplicate, `hotfix init`, then read back four things that are each silent
when wrong: `hotfix status` lists the pod under the new mountPath; `git status`
in `/podbench/app` is clean and reports no dubious ownership; `uv sync` built
into `/podbench/venv` and nothing podbench wrote is inside the checkout; and
the supervisor is running the claim's interpreter, not the image's. Delete
every pre-existing claim first rather than reasoning about an old layout under
a new mount.

**Falsified if:** `hotfix status` stops finding a freshly deployed hotfix pod,
or a `git status` in the checkout reports dubious ownership, or `uv sync` builds
a venv anywhere but `/podbench/venv`, or the checkout contains anything podbench
put there.

**Anchors:** `model.py:787` `HOTFIX_APP_PATH`, `model.py:775`
`HOTFIX_CLAIM_VOLUME`, `model.py:279` `CLAIM_PATH_ENV`, `hotfix.py:604`
`checkout_path`, `hotfix.py:595` `manifest_path`, `hotfix.py:315`
`CLAIM_VENV_DIR`, `hotfix.py:1098` `install_argv`, `hotfix.py:1237`
`VENV_INVISIBLE_TO_STATUS`, `hotfix.py:1728` (the scan), `hotfix.py:3342`
`values_snippet`, `hotfix.py:3517` (supervisor switch), `spec.py:335`
`_claim_path_env`, `agent.py:1295` `ensure_seat_gitconfig`.

---

## Phase 3 — the seat's home moves onto the claim

`/podbench/home/<user>` — `root/` and `podbench/` beneath it, because three
seats to a pod was observed at Diamond and they must not share one home.

Two mechanisms, because there are two kinds of seat:

* **Non-root seat** — `ensure_passwd_entry` (`agent.py:593`) already writes the
  record. Point it at the claim. No symlink needed.
* **Root seat (the full rung)** — the image's `/etc/passwd` says `/root` and
  `libnss-extrausers` cannot serve uid 0 at all (it floors at 500). So redirect
  `/root` to the claim at seat creation, idempotently, preserving whatever
  dotfiles the image ships on first run.

Do it **on seat creation**, not by recreating a home each time. `podbench-home`
stays for attach-mode seats with no claim; the claim wins where both exist.

This is where the payoff is stated plainly, so it can be checked: a seat that is
re-attached after a pod replacement finds `~/.vscode-server` already unpacked,
and nothing the seat writes counts against the pod's ephemeral-storage budget —
the budget whose overrun evicts the *pod*, application included.

**Verify:** both beds, and neither is optional. On the **p47 harness**
(degraded rung, uid 37887): the home lands on the claim via the passwd record,
and — the actual proposition — `delete` the pod, let it come back against the
same standalone PVC, re-attach, and find `~/.vscode-server` already unpacked.
Record the second attach's elapsed time against the first. On the **k3s
bench**, which grants `runAsUser: 0`: a full-rung seat writes its home to the
claim and not to `/root` on the container layer. p47 cannot show this — it
never lands a root seat.

**Falsified if:** a full-rung (root) seat still writes `~/.vscode-server` to the
container layer, or two seats on one pod share a home, or a seat on a pod with
no claim stops getting `podbench-home`, or `session_home` (`agent.py:1149`) and
the passwd record disagree about where `$HOME` is.

**Anchors:** `agent.py:593` `ensure_passwd_entry`, `agent.py:1149`
`session_home`, `agent.py:285` `SEAT_NSS_PATH`, `launcher.py:797`
`seat_identity_mounts`, `model.py:193` `SEAT_HOME_PATH`, `model.py:184`
`SEAT_HOME_VOLUME`, `hotfix.py:277` `SEAT_HOME_SIZE`.

---

## Phase 4 — resetting the fix must not delete the developer's home

The new design question this plan creates, and it is not optional once Phase 3
lands. Today the claim **is** the fix, so "clear the claim" is coherent and
total. Afterwards the claim holds three lifecycles:

| | lifecycle |
|---|---|
| `app/` | re-seeded from the running container |
| `venv/` | rebuilt on an interpreter bump |
| `home/` | precious — an afternoon's editor state |

The flat layout from Phase 2 is what makes a scoped reset expressible as paths:
clear `app/` and `venv/`, keep `home/`. Audit every path that clears the claim
and make the scope explicit rather than inherited.

**Verify:** p47 harness. Put a marker file in `home/`, run every path that
clears the claim, and confirm the marker survives while `app/` and `venv/` are
gone and rebuild cleanly.

**Falsified if:** any reset path removes `home/`, or a scoped reset leaves a
stale venv the supervisor then runs.

**Anchors:** to be established by the phase — the sweep is "everything that
writes to or clears `CLAIM_PATH_ENV`".

---

## Phase 5 — say so in the issues

Not code. The issues currently propose the thing this plan rejects, and a reader
who finds #69 or #70 first will build the wrong thing.

* **#70** — re-scope or close. #102 already killed its identity argument; this
  plan kills its home argument by using the claim instead. What survives is a
  stable host key, and that is #69 item 2.
* **#69** — item 1 is superseded by Phases 2-4. Item 3 is dropped, with the
  reason. Item 2 (`HostKeyPolicy.SECRET`) is untouched and still stands on its
  own.
* **#42** — closed by Phase 3, with the extrausers-floor reasoning recorded, so
  nobody re-opens it as a passwd-record problem.
* **#67** — still needed under Argo, unchanged by this plan. The claim it
  protects already matters more than the home now on it.

**Verify:** no cluster. Each issue's closing comment cites the phase, the PR
and the evidence file that earned it.

**Falsified if:** an issue is closed whose actual content this plan did not
deliver.

---

## What this plan does not touch

* **`attach` mode's ladder** — unchanged, deliberately. It is the mode ranked
  first and the one thing an ephemeral container is genuinely better at.
* **The scratch PVC** (`pvc-scratch.yaml`) — still unwired, still read by no
  launcher code. This plan uses the hotfix claim instead and leaves the scratch
  claim's purpose to dev mode.
* **dev / iterate mode** — `podbench-workspace` is an `emptyDir` that is both
  venv and `$HOME`, and has the same shape of problem. Out of scope: it is
  ranked last and is not Argo-compatible.
* **#69 item 2**, the stable host key. Still worth doing, not part of this.
* **The seat identity ConfigMap.** #102 made it unnecessary and it should be
  deleted, but that is its own change.
