# Hotfix mode becomes easy to drive

Hotfix mode is correct, and since #189-#192 it is adoptable: the four-step
workflow was proved end to end against the real `bl47p-ea-fastcs-01` and is
recorded in `.claude/evidence/phase5-the-workflow-on-p47.md`. **That workflow is
settled. This plan does not redesign it.**

What is still true is that the mode is hard to *drive*. `podbench hotfix --help`
opens on ~14 options belonging to one flag, above the four subcommands that are
the actual mode. Two flags ask for values podbench could measure. Every
prerequisite is discovered one at a time, each costing a chart change and a
redeploy. Retirement is a six-step printed checklist that nothing tracks. And a
run in which nothing went wrong still prints 333 words of warning.

All of that is the same defect wearing five hats: **the mode makes the person in
the emergency do work podbench could do for them, and then explains itself at
length while they do it.** This plan closes that and nothing else.

**Measured before starting** (2026-08-23, reading the module and running every
verb's `--help` at `COLUMNS=80`):

| | |
|---|---|
| `podbench hotfix --help` | **88 lines**, vs 32 for `podbench --help` and 51 for `dev --help`; 14 of its 18 flags are unreachable from any subcommand |
| unwrapped output | **five call sites** bypass `console.paragraph`/`emit` entirely — `TARGET_HAS_NO_PROJECT` is 728 characters on three physical lines, `NON_EXEC_PROBE_WARNING` 474 on one, `EXISTING_MOUNTS_WARNING` 459 on one |
| retirement checklist | 10 lines, one of them **186 characters** |
| `--print-values` fragment | 58 lines, **30 of them comments** |
| a healthy `status` row | says the same number **three times**, four counting `apply` |
| `docs/how-to/` pages for hotfix | **none** — which is why the mechanism lives inside the error strings |

That last row is the enabling fact. The `terminal-reports` rule is that a message
names the fact and the flag while the mechanism is said once in `docs/how-to/`;
hotfix has nowhere to put the mechanism, so it went into the strings.

## What the field already says is broken

Giles ran `podbench vscode` against this IOC on 2026-08-23 and hit **two
failures that are not drivability at all**. They are named here because a plan
about polish that quietly walks past two functional defects would be the wrong
plan:

1. **It refused, and demanded `--new`,** because "something happened to the
   original seat". That is the reconnect path — #204's subject, and
   `attach-endgame`'s deliberate refusal-rather-than-silently-replace decision.
   Whether the refusal was *right* and merely unexplained, or *wrong*, is the
   first thing to establish. The distinction matters: one is a wording fix, the
   other is a defect.
2. **The window opened, `/app` was visible, and debugging failed to start.**
   Unknown cause. The `vscode-in-a-seat` skill already carries the candidates —
   OOM traps, the breakpoint-versus-probe timer, adapter bugs that surface only
   in a container — and #160's `UV_PYTHON_INSTALL_DIR` half is live in this area
   too.

**Sequencing, per Giles:** fix drivability first, because it is the basis for
chasing these. A mode you cannot drive is a mode you cannot debug — every probe
of failure 2 currently costs a hand-typed six-flag command and a wall of prose
to read the result out of.

The bridge is what makes failure 2 tractable. A human sees "debugging failed to
start"; the bridge reads the DAP traffic, the debug console, `vscode.debug`
session state and the extension host's own errors, which is the difference
between a symptom and a cause.

---

- **Base:** `main` at or after `9f8abdb`. Released state is 0.7.1.
- **Decided:** 2026-08-23, by Giles, from the issues below.
- **Issues:** #205 (seven usability findings), #203 (the verbosity rule), #204
  (a warning that guesses where it could measure), #209 (the `--claim-venv`
  correctness defect this survey turned up).
- **Evidence discipline:** unchanged, and it is the constraint this plan is most
  likely to break — *measure the seat, do not restate the request.* Where a
  measurement could not be taken, say so; **never "fine", and never a warning
  invented to fill the gap.**

Read `hotfix-beside-the-app.md` for the design, `hotfix-after-the-first-live-run.md`
for the run that produced #177-#180, and `hotfix-becomes-a-workflow.md` for the
workflow this sits on top of. This plan repeats none of them.

---

## How to execute this plan

**Strongly recommended: run this as a workflow, not inline.** The work is a
CLI reshape plus a string audit across four modules plus a live cluster run, and
the reading alone — `hotfix.py` is 3822 lines — will bury a single context long
before the cluster phase, which is the phase that must not be run by a tired
agent with no room left.

The shape that works, and which produced the last two plans:

- **One phase at a time, and finish it.** Do not open the next phase's files
  while the current one is unmerged.
- **Delegate every read-only sweep to a subagent and keep the conclusion, not
  the transcript.** "Which constants does `hotfix.py` export and how long is
  each" is a subagent's job; the answer is a table, not a file dump.
- **Cite `path:line`. Never paste a file into the main context.**
- **Batch the cluster into one final phase.** The tunnel is shared, Giles raises
  it, and a beamline round trip is ~2 minutes. Do not spend them one at a time
  while iterating on a string.
- **Write evidence to `.claude/evidence/` and link it from the PR.** A
  measurement that lives only in a transcript did not happen.

The main context's job is to hold the plan, the decisions, and the diff under
review. Everything else belongs in a subagent.

---

## The environment this runs in

Stated here because the session executing this plan will be a **fresh one** and
none of it is derivable from the repo.

**Claude runs on the host**, not in the devcontainer — `/home/giles/code/podbench`.
The devcontainer still exists and shares the same bind-mounted files, which
matters for exactly one thing: `.venv` records absolute paths, so a venv built in
the container points at `/root/.local/share/uv/python/...` and the host cannot
even stat it. The symptom is `Permission denied (os error 13)` from `uv`/`just`,
not "wrong interpreter". Fix with `uv venv --clear && just sync`; expect to redo
it on the other side.

**The cluster.** `k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig` reaches the
`p47-beamline` namespace on pollux, v1.35, over a tunnel only Giles can raise.
Verified 2026-08-23: `pods/ephemeralcontainers` create, `pods/exec` create,
`statefulsets` get/patch, `pods` delete, PVCs and events read — everything
podbench needs.

**p47-beamline is Giles' own test beamline, restorable from ArgoCD in minutes.**
Do not hedge about mutating it, and do not ask permission to run the mode it was
prepared for.

**ArgoCD runs in a different cluster and is invisible to this token** —
`kubectl get applications.argoproj.io` answers "the server doesn't have a
resource type". There is no sync status to read and no `argocd app wait`. The
deploy loop is: push to the branch, **wait at most 2 minutes for auto-refresh**,
then observe the cluster — poll the StatefulSet's `.metadata.generation` and the
pod spec for the change you pushed, with a timeout.

**The target** is `bl47p-ea-fastcs-01-0`, container `bl47p-ea-fastcs-01`, image
`ghcr.io/diamondlightsource/fastcs-example-debug:2025.10.1`, running at uid/gid
37887 with `temp-controller-simulator` alongside it. Its service definition is
`../p47-services/services/bl47p-ea-fastcs-01`.

**Git state as this plan opens, and a finding that came with it.**
`p47-services` is on branch `podbench-hotfix-claim`, whose top commit is *"turn
off hotfix mode"* — `values.yaml` carries `podbench-hotfix-claim.enabled: false`
— yet the pod is still hotfix-wired. This was **not** a stale pod: every pod in
the namespace was deleted on 2026-08-23 and ArgoCD recreated them from git, and
`bl47p-ea-fastcs-01-0` came back still mounting `podbench-app` and still running
the supervisor-loop `args`.

The reason is the shape of the retirement checklist. `enabled: false` disables
only the **claim subchart** — the PVC. The `volumes`, `volumeMounts`, `args` and
`podSecurityContext` live in the target's own `ioc-instance` values and are
untouched, and the PVC itself carries `Prune=false,Delete=false` so it outlives
its chart. "Turning off hotfix mode" did step 5 and not step 4, which is exactly
what #205 item 4 predicts nobody does — and the state it leaves is worse than
either end: a pod wired to a claim whose chart no longer declares it, which would
fail to schedule if that PVC were ever actually pruned.

Treat this as the live specimen for Phase 5's `hotfix retire`, and as the first
thing that phase should be able to detect and report.

**Driving VS Code.** The bridge lives in this repo at `tools/vscode-bridge/`;
read its `README.md` first. `vsc.py` is the client, `code-with-bridge` a shim
that adds `--extensionDevelopmentPath` so nothing is installed into
`~/.vscode/extensions`. Symlink the shim as `code` into a directory, put that
directory first on `PATH`, and `podbench vscode` opens a window carrying the
bridge — because `editor.py:423` resolves the editor with `shutil.which("code")`
and offers no override flag, and that directory's path must contain neither
`/remote-cli/` nor `/.vscode-server/` or `resolve_editor` refuses it.

`vsc.py ls` finds windows, `info` reports remote name and folders, and it can
open files, run any palette command, set breakpoints, start a launch config, and
read the frames and locals where a session stopped. It is a **development tool**
— not shipped, not run by CI — but it *is* type-checked, so a change to it must
keep `just types` clean.

**Two things the bridge cannot do:** see the screen (GNOME refuses the
screenshot D-Bus call) and synthesise keyboard input. Anything that exists only
as a mouse gesture or inside a webview is out of reach, and a phase that depends
on one must say so rather than claim a pass.

---

## Hard rules

1. **Never push to `main`, either repo, at any point.** Not "branch first" —
   never.
2. **`gilesknap/podbench`: one new branch, one PR.** Push every phase's fixes to
   that same PR rather than opening more. The first push carries the working-tree
   fix described under "Phase 0" plus this plan.
3. **`epics-containers/p47-services`: stay on `podbench-hotfix-claim`.** ArgoCD
   tracks that branch for this IOC; a new branch would simply not be deployed and
   the loop would silently test nothing. Commit onto it, push, do not open a PR.
4. **Both remotes are configured for SSH, which Giles' rules forbid.** Push over
   HTTPS naming the URL explicitly:
   `GIT_CONFIG_GLOBAL=/dev/null git -c credential.helper='!gh auth git-credential' push https://github.com/<owner>/<repo>.git <branch>`.
   A branch pushed by URL has no upstream, so `gh pr create` needs
   `--head <branch> --base main`. `gh pr merge --auto` does **not** queue behind
   CI on these repos — gate with `gh pr checks <n> --watch`.
5. **No other repos.** Not `ec-helm-charts`, not `ioc-instance`.
6. **`just` for everything local** — `just lint`, `just types`, `just test`,
   `just docs`. `docs` builds with `-EW` and `nitpicky`, and a new docs page must
   be added to `docs/how-to.md` or the equivalent toctree by hand or the build
   fails.

---

## What must not change

These are measured, and each exists because the failure it catches is **silent**.
A plan about trimming output is exactly the plan that breaks them.

1. **`is_hotfixed` is derived from the pod spec, never an annotation.** Argo
   strips pod-template annotations (#32); the predicate is the claim volume plus
   a container whose args carry the supervisor loop.
2. **A mount the user did not ask for must be named** (`HOTFIX_CLAIM_MOUNTED_NOTE`,
   `launcher.py:803`). That licenses *naming* it, not *arguing* for it — and it
   is a note, not a warning, because nothing went wrong.
3. **A chart renders a supplied `livenessProbe` wholesale** (#176, measured):
   an omitted timing becomes a Kubernetes default in the restart-sooner
   direction. This is why `--from-pod` is the default and why every failure of it
   must say what the offline path costs.
4. **`helm.sh/resource-policy: keep` alone is pruned by Argo within ~3 minutes**,
   taking its PV with it (`.claude/evidence/phase1-prune-on-sync.md`). The claim
   survives only because the subchart carries
   `argocd.argoproj.io/sync-options: Prune=false,Delete=false`.
5. **A helm list replaces across the parent/child merge; it does not merge.**
   The failure mode is silently unmounting a beamline directory. This is why
   `EXISTING_MOUNTS_WARNING` exists.
6. **`status`'s "held expiry unmeasured" is never "no deadline"**, and a held pod
   with no manifest is still listed.
7. **The claim's project shadows the image's**, so an image upgrade under a live
   hotfix does not reach what is executing; the manifest records the digest and
   `status` compares it.
8. **`values_snippet` stays a pure, cluster-free function.** It is what makes
   every shape of the output unit-testable without a cluster. `--from-pod` and
   `--values` are thin wrappers over it.
9. **Chart-agnostic.** Nothing here may assume `ioc-instance`'s key names or
   layout; `values_snippet` parameterises every key name and the indent for
   exactly that reason.
9b. **`--print-values` stdout is redirected straight over a values file** and the
   p47 run did exactly that. Nothing may be added to stdout, every note stays on
   stderr, and the trailing-newline behaviour of
   `print(snippet, end="" if values is not None else "\n")` is not to be tidied.
9c. **Exit codes are assertions, not cosmetics.** `podbench hotfix` with no
   subcommand exits **2** (`cli.require_subcommand`); `hotfix status` exits **1**
   when any row is not `ok`, including a merely-held pod (`HotfixRow.ok`,
   `1474-1489`). That is the facility shutdown-checklist contract.
9d. **Strings that must survive any trim, with the reason:**
   `FROM_POD_ESCAPE` must name `--no-from-pod` *and* what it costs (it is the
   route that produced #176); `TARGET_HAS_NO_PROJECT` must mention neither ptrace
   nor doctor even to rule them out (#178's false trail) and must print the
   in-image path, not the `/proc/1/root/...` form; it and `TARGET_ROOT_UNREADABLE`
   stay two messages from two probes; `NON_EXEC_PROBE_WARNING` must be printed
   because an absent probe block looks identical to the healthy fastcs case;
   `EXISTING_MOUNTS_WARNING` stays printed under `--from-pod` and stays suppressed
   under `--values` (#199, found by running the workflow for real);
   `MERGED_UNDER_NOTE`, `ABSORBED_FROM_PARENT_NOTE` and `NO_PARENT_NOTE` all stay.
9e. **Layout invariants:** a warning is one line under the coloured leader — not a
   bulleted explainer; `format_status`'s row stays an authored f-string and never
   goes through `paragraph()`; the two-space column and `_FLAG = 10` alignment are
   load-bearing and match `doctor`'s; styles go on by span, never markup, or the
   `[ok]` ticks and every bracket in relayed stderr are eaten; width comes from
   `wrap_width()` per call, never a constant.
10. **The four-number contract for any live run:** `restartCount` unchanged, the
    recorded child pid moved, the edit live in the running process, every seat
    alive. Anything else is commentary.

---

## Phase 0 — the branch, the plan, and one test fix

Already in the working tree, uncommitted: `tests/test_agent.py` had
`test_a_seat_the_extrausers_floors_reject_takes_etc_passwd_instead` hard-coding
uid 1000 while `FakePasswd` seeds a record for the *running* euid. As root in the
devcontainer and in CI, 1000 is unknown and the test passes; run as uid 1000 on a
developer host, the fixture's own seed answers, `ensure_passwd_entry` takes its
early `return False`, and the test fails. It now uses the file's existing
`UNKNOWN_UID = 4242` — above the `MINUID 500` floor, so the same case — with a
docstring line saying why 1000 cannot be used.

Open the branch, commit that fix and this plan as two commits, push, open the PR.

**Falsified if** `just test` does not pass on both a host at uid 1000 and in the
devcontainer as root. The whole point of the fix is that it passes in both.

---

## Phase 1 — `hotfix values` becomes a verb (#205 item 7)

`--print-values` is the step everything else depends on, and it is the one part
of the mode that is not a subcommand. As a root-callback flag it drags ~14
companion options onto the root (`hotfix.py:3397-3535`), so `podbench hotfix
--help` opens on a wall of options above the four subcommands that are the mode.
The callback then has to rule the flag out before it can require a subcommand,
which is why `require_subcommand(ctx)` sits at the bottom of a hundred lines of
emission logic.

Move it to `podbench hotfix values`. Each flag sits next to the verb that uses
it, both help pages become readable, and the mode reads as the five steps it
actually is: **values, check, init, apply, consolidate/retire**.

This phase is the one with the largest blast radius on tests and docs, and it is
first on purpose: every later phase's help output is read against the new shape.

**Delete `--print-values`. No alias, no deprecation period, no hidden flag.**
Giles' instruction, 2026-08-23, and it is the governing principle for this whole
plan: *hotfix mode is a new tool nobody outside this repo drives yet, so prune
aggressively rather than carry compatibility for a surface no user has learned.*
Update `docs/explanations/hotfix-flow.md` and any runbook text in the same
commit — a doc naming a flag that no longer exists is worse than either
spelling. The same principle licenses the removals in Phase 4 (`--no-from-pod`,
`--liveness`) and the dead `--author` on `consolidate`: take them out, do not
hide them.

**Falsified if** the emitted values change at all. This is a relocation of a CLI
surface, not a change to what it emits — diff the output of the old and new
spellings against the same target, before and after, and require them
byte-identical.

---

## Phase 2 — stop asking for what the pod can answer (#205 items 1 and 2)

**`--venv`** is required by `init`, `apply` and `consolidate` (`hotfix.py:3127`)
but never by `status`, which reads the manifest at the fixed `HOTFIX_APP_PATH`
(`hotfix.py:1421`) and finds the mounting container by scanning for exactly that
mountPath (`hotfix.py:1388`). So the flag is redundant *and* dangerous: any value
other than `/podbench/app` writes a manifest `status` cannot see, and a hotfixed
pod invisible to `status` is the precise failure the mode exists to prevent.
Nothing warns.

Default it to `HOTFIX_APP_PATH`; better, resolve it off the pod spec the way
`status` does, and refuse a value that disagrees with what the pod mounts. Keep
the flag as an override for a claim mounted elsewhere, and make *that* path say
out loud that `status` will not report it.

**`--base-commit`** is recorded as `base_commit or git rev-parse HEAD` of the
fresh clone (`hotfix.py:1839`) — without `--ref`, the default branch's tip, which
is almost never the commit the released image was built from. Everything
downstream is measured from it: `status`'s `+N commit(s)`, `drift_commits`, and
the set `consolidate` pushes. Read `org.opencontainers.image.revision` and
`.source` off the target image to default `--base-commit` and `--repo`. Where
neither label exists, record the base as **assumed** and have `status` say so,
rather than printing a derived commit count as though it were measured.

**`--claim-venv` is a silent correctness bug — now #209, filed separately
because it is correctness rather than usability and must not vanish if this plan
is descoped.** It is documented as a cross-command contract that
must match between `init` and `values` (`hotfix.py:3529-3538`, `3162-3170`), and
nothing checks it. Worse: `init` does not record it in the manifest
(`hotfix.py:1848-1862`) and **`apply` has no such flag at all**, so `apply`'s
reinstall calls `_install(...)` without it (`hotfix.py:1962`) and defaults to
`.venv`. A user who ran `init --claim-venv env` gets a packaging-change rebuild
landing in `.venv` while the supervisor's runtime switch looks in `env` — the
exact silent failure `install_argv`'s docstring (`hotfix.py:929-933`) says the
flag exists to prevent. Record it in the manifest at `init` and pass it at
`apply`. New manifest fields default and load leniently (`from_mapping`,
`343-383`); the version rule at `175-183` governs whether `MANIFEST_VERSION`
moves.

**Falsified if** a target whose image carries no OCI labels ends up with a
manifest that looks measured. The honest-uncertainty path is the point of the
item, not a fallback. Also falsified if an existing claim carrying a recorded
non-default `venv` stops loading — `manifest_path`/`write_manifest`
(`399-405`, `673-675`) must keep honouring it.

---

## Phase 3 — `hotfix check` (#205 item 3)

Every prerequisite is discovered serially and mostly at the moment it bites: no
supervisor (`require_supervisor`, `hotfix.py:1786`), claim not mounted,
multi-replica target (`_refuse_multi_replica`), `/proc/1/root` unreadable, no
project at `--image-project`, and a non-exec `livenessProbe` the hold cannot
short-circuit. Each is a round trip through a chart change and a redeploy, in an
emergency, discovered one per attempt.

Add a read-only `hotfix check TARGET` that reports all of them at once and exits
non-zero if any would block. Every check it needs already exists as a function in
the module; this is composition, not new measurement.

**Falsified if** `check` passes on a target that `init` then refuses, or refuses
one `init` would have accepted. It has one job: make the second attempt
unnecessary.

---

## Phase 4 — drop `--no-from-pod` (#205 item 6)

A flag whose help must talk you out of using it has already failed. It is the
exact route that produced #176; "a pod that does not exist yet" is close to
vacuous for a mode applied to something running and broken; and the offline
emission is strictly lower fidelity — hand-supplied fields lose the probe's
timings, the gid and the real entrypoint.

**What stays:** `--entrypoint`, `--gid` and `--liveness-probe` as overrides *on
top of* the pod read (`tests/test_hotfix.py:1894` says `--entrypoint` is the right
answer for an image-`ENTRYPOINT` target). `--liveness CMD` retires with the
offline path; `--liveness-probe JSON` covers it with fidelity.

**Cost to name:** roughly eight CLI-level unit tests drive the emitter offline
through this flag (`tests/test_hotfix.py:2223` onwards) precisely so the run
touches no cluster. They move to an injected runner over fixture pod JSON — the
pattern is already threaded through `_build_app(runner)`.

**Falsified if** the unit suite ends up needing a cluster. `values_snippet` stays
pure; only its wrapper learns to read a fixture.

---

## Phase 5 — retirement stops being prose (#205 items 4 and 5)

`consolidate` ends with a six-step hand checklist (`_retirement_checklist`,
`hotfix.py:2160`), after which `status` reports `superseded` correctly and
indefinitely. There is no way to say "step 3 is done", and steps 4-6 — drop the
values keys, disable the subchart, delete the `Prune=false` claim — are the ones
nobody does, which is exactly how a claim goes on shadowing a fixed image.

Add a `hotfix retire` verb that performs what it can and reports what it cannot,
and a `status` row tracking which steps remain instead of one undifferentiated
`superseded`. Add `status --all-namespaces` with the same exit-code contract, so
the shutdown-checklist assertion `main`'s docstring sells (`hotfix.py:3801`) is a
command a facility can run rather than a shell loop the operator must write.

**Falsified if** `retire` silently does a step it could not verify. The mode's
purpose is that a diverged pod is never silent; a retirement that lies is worse
than the checklist.

---

## Phase 6a — make the output go through the console at all

Before any string is shortened, five call sites must stop bypassing the layout
machinery. `console.emit`/`paragraph` exist to wrap (`console.py:251-303`) and the
skill says every laptop-side verb goes through them; these print raw:

| site | what escapes |
|---|---|
| `_print_values_failure`, `hotfix.py:3274-3276` | three lines of 105, 192 and 136 chars, terminal-wrapped mid-token — which also breaks the backticked `` `--from-pod POD` `` that `console._TOKEN` exists to keep pasteable |
| `main`'s except clause, `hotfix.py:3816-3818` | every `HotfixError`, including `TARGET_HAS_NO_PROJECT` at 728 chars |
| `EXISTING_MOUNTS_WARNING`, printed at `3366-3372` | 459 chars on one line |
| `NON_EXEC_PROBE_WARNING`, printed at `3385-3390` | 474 chars on one line |
| `_report`, `hotfix.py:3220-3222` | `emit` styles but never wraps, so any action longer than the window overruns |

`_merge_print_values_into_file` (`3113-3114`) already wraps through
`paragraph()`. Two conventions in one module; make it one.

While here, give the two `--print-values` stderr warnings the `WARNING`
vocabulary (`console.WARNING_LEAD`) instead of an unstyled `podbench:` prefix —
they are warnings, and today they cannot be picked out of a paste.

**Do not reflow relayed stderr.** Messages embedding kubectl's or uv's output
(`1899-1904`, `2100-2107`, `3332-3335`) keep the relayed half on its own
unwrapped line, the way `launcher._editor_step` keeps the two shapes apart.
Reflowing somebody else's error is how a paste stops matching what they saw.

**Falsified if** a test that asserts on a substring starts passing for the wrong
reason. Several do (`tests/test_hotfix.py:764, 800, 885, …`); they flatten the
way `tests/test_doctor.py::flowed` does, and flattening must not hide a line that
should have wrapped.

---

## Phase 6b — `docs/how-to/hotfix-a-running-pod.md`

The enabling change for every compression below it, and the reason to do it
before the string edits rather than after. There is **no how-to page for hotfix**
— `docs/how-to/` carries attach, gdb, iterate-on-python, run-container, vscode and
contribute, and hotfix exists only as `docs/explanations/hotfix-flow.md`, a
500-line reference. The skill's compression strategy has no destination, so the
mechanism ended up inside the error strings.

Write the sequence start to finish, copy-pasteable, then shorten the strings that
currently carry the mechanism — `hotfix.py:485-531` and `2358-2375` — pointing at
the page instead. One string at a time.

`just docs` runs `sphinx-build -EW` with `nitpicky = True`, and the toctree is
explicit, so the page must be added to it by hand or the build fails.

---

## Phase 6c — the note and warning audit (#203, #204)

The rule, from #203, and it belongs in the `terminal-reports` skill once written:

> Every user-facing note gets at most three beats — **what happened**, **whether
> it matters here**, **what to do about it**. Anything that is *why podbench is
> confident this is fine* belongs in the docstring, not on the terminal.

The work, in order of leverage:

1. **Warn only when it changed the outcome.** This deletes blocks rather than
   shortening them. `ADMISSION_MUTATION_WARNING` (`launcher.py:2943`) fires
   whether or not the rewrite cost anything; when it cost nothing the seat is
   exactly the seat asked for. `CAPABILITY_STRIPPED_WARNING` (`launcher.py:2910`)
   already covers the case that matters. Demote the harmless case to the debug
   report.
2. **Do not announce the same event twice.** `EDITOR_HEADROOM_WARNING`
   (`launcher.py:381`) and `EDITOR_RESIZE_NOTE` (`launcher.py:403`) are intent
   and outcome for one resize, 219 words between them.
3. **Audit the 33 constants** — `launcher.py` 22, `hotfix.py` 5, `editor.py` 5,
   `proc.py` 1 — worst-by-word-count first.
4. **Keep the reasoning.** Every sentence removed from a constant lands in its
   docstring if it is not already there. Nothing here is wrong; it is in the
   wrong place.
5. **#204:** replace the reconnect ssh-key guess with a `cat` of
   `authorized_keys`, and say nothing when the key is present.

Measured baseline to beat, from `podbench vscode bl47p-ea-fastcs-01` on
2026-08-22: **101 lines, four WARNING blocks, 333 words, nothing wrong in any of
them.**

**Rewriting all 33 is sanctioned** — Giles, 2026-08-23, on the same
new-tool-prune-aggressively grounds as Phase 1. The bar is not "change as little
as possible"; it is that **what comes out is a consistent, intelligible set**.
One voice, one shape, the same three beats in the same order, the same
vocabulary for the same concept across `launcher.py`, `hotfix.py`, `editor.py`
and `proc.py`. A half-rewritten set where six constants follow the rule and
twenty-seven do not is the worse outcome, and is what "minimal diff" would
produce here.

So treat this as one deliberate pass with a written style, not 33 independent
edits. Draft the rewritten set and read it end to end as a body of text *before*
applying it — the property being checked is consistency across the set, which is
invisible one constant at a time.

**Falsified if** shortening a note makes a real failure harder to act on.
`VERSION_SKEW_WARNING`, `CAPABILITY_STRIPPED_WARNING` and
`HOTFIX_CLAIM_UNMOUNTABLE_NOTE` describe states where the next action depends on
the detail; the three-beat rule bounds beat 3, it does not delete it. Every
sentence removed lands in the docstring if it is not already there — that is what
makes this a move rather than a loss, and it is the reason a wholesale rewrite is
safe here.

Read the `terminal-reports` skill before touching any of this. Wrapping collapses
whitespace, so a column row or a `do this:  <command>` offer put through
`paragraph()` silently unaligns or unpastes; styling a `Text` through rich markup
instead of by span eats the `[x]` ticks and every bracket in relayed stderr.

---

## Phase 7 — the live run

One phase, batched, on `bl47p-ea-fastcs-01`. Everything above is unit-tested; this
is where it meets a kernel and an API server.

**First, resolve the contradiction.** Git says hotfix is off; the pod says it is
on. Establish which is true, and record it — it is the first evidence this plan
produces and it is a live instance of #205 item 4.

**Then walk the mode with the new surface**, in the order a user would:
`hotfix values` → deploy via git → `hotfix check` → `hotfix init` → edit →
`hotfix apply` → observe → `hotfix status` → `hotfix retire`. Between any values
change and the cluster: push to `podbench-hotfix-claim`, wait up to 2 minutes,
poll for the change.

**Then drive VS Code for real, and chase the two field failures.** The Phase 5
run never opened a window — `code` was an argv-recording stub — so neither
failure could have been seen then.

*Failure 1, the `--new` refusal.* Reproduce it, then decide which of two things
it is: a correct refusal that failed to explain itself, or a seat that should
have been reusable. #204's rule applies either way — replace the guess with a
measurement (`cat` the `authorized_keys`) and say nothing when the key is
present. Do not "fix" it by auto-landing a new seat: `attach-endgame` refused
that deliberately, because silently replacing a seat a colleague is using is
worse than a refusal.

*Failure 2, debugging not starting.* This is what the bridge is for. Capture, in
this order: `vsc info` for the window's remote name and folders; the
`launch.json` podbench wrote and whether its `type` matches an adapter the seat
actually has; `vsc debug <name>` and the resulting `dap.*` events; the debug
console and extension-host errors. Read the `vscode-in-a-seat` skill first — the
OOM trap and the breakpoint-versus-probe timer are both live candidates, and
#160's `UV_PYTHON_INSTALL_DIR` half is in the same area. Name the cause; do not
report "works now" after an unexplained retry.

**Both of the bridge's unproven assumptions were settled on 2026-08-23** against
`bl47p-ea-simdet-01-0`, so this phase does not need to re-establish them: a bare
path resolves into the seat (reading the seat's own `launch.json`, not a
same-named laptop file), and `startDebugging` from the laptop-side extension host
does resolve an adapter living in the seat (a `cppdbg` session started and
stayed up). See `tools/vscode-bridge/README.md`.

That run also found, and fixed, a silent shim defect worth knowing about because
it will happen again: `code` with no `--user-data-dir` hands off to an
already-running VS Code over its IPC socket, which was started without
`--extensionDevelopmentPath`, so the window opens with no bridge in it while
podbench reports `[ok]`. If `vsc.py ls` finds nothing after a successful
`podbench vscode`, that is the cause.

**Falsified if** the four numbers do not hold: `restartCount` unchanged, the
recorded child pid moved, the edit live in the running process, every seat alive.
Also falsified if either field failure is closed without a named cause.

Write the result to `.claude/evidence/` and link it from the PR. Where a
measurement could not be taken, say **not measured**.

---

## Ordering, and why

1 first because every later help page is read against the new CLI shape. 2 and 3
next because they are what makes the mode survivable in an emergency, and 3 is
cheap once 2 has taught the code to read the pod. 4 removes a path before the
output phases have to audit its strings. 5 is independent and can move.

6a before 6b before 6c, and the order is load-bearing: routing the output through
the console is mechanical and safe, the how-to is where the mechanism goes once
there is somewhere to put it, and only then is shortening a string a compression
rather than a deletion. 6 comes after 1-5 so that auditing strings those phases
have just rewritten is one pass instead of two.

7 last, always, and never begun with a context that is nearly full. It is the
only phase that can fail for reasons that are nobody's fault — a tunnel down, a
sync that did not land — and the only one carrying two named defects it is
expected to diagnose rather than merely observe.
