# Hotfix mode becomes a workflow

Hotfix mode works. `hotfix-after-the-first-live-run.md` closed #177-#180 and its
Phase 5 proved the whole of it against the real `bl47p-ea-fastcs-01` — including
the two things no cluster had done, survival across pod replacement and
`consolidate`. Read `.claude/evidence/phase5-live-run-on-p47.md` rather than
re-measuring any of it.

What is *not* yet true is that it is a **workflow**. Adopting it still costs a
hand-written Helm template, a hand-merged values file, and a File → Open. This
plan closes that gap and nothing else.

- **Base:** `main` at or after `9468609`. Released as **0.7.0**.
- **Decided:** 2026-08-22. Four issues, #189-#192, and the design decisions
  behind them are recorded in the issues rather than repeated here.
- **Evidence discipline:** unchanged — *measure the seat, do not restate the
  request.* Every phase states what would falsify it. Where a measurement could
  not be taken, say so; never "fine".

Read `hotfix-beside-the-app.md` for the design and
`hotfix-after-the-first-live-run.md` for the run that produced these four
issues. This plan repeats neither.

---

## The workflow this is for

Stated by the user, 2026-08-22, and it is the acceptance test for the whole plan:

1. `podbench hotfix --print-values` against the target, and paste the result into
   the service's `values.yaml` — **the whole file, not fragments to merge**.
2. The claim comes from a **podbench chart dependency**, declared once in a
   shared `Chart.yaml`, rendering nothing unless a boolean is set.
3. Deploy by pushing to git; Argo syncs.
4. `podbench vscode <pod>` attaches, recognises the hotfix pod, and **opens the
   project**.

**Chart-agnostic, deliberately.** This must work for target charts other than
`ioc-instance`. A chart that cannot express extra volumes will need editing, and
that is accepted — but nothing in this plan may assume ioc-instance's key names
or its layout. `values_snippet` already parameterises every key name and the
indent for exactly this reason; use that machinery rather than working around it.

---

## How to work this plan

Unchanged from the last one, and it worked, so:

**One phase, one branch, one PR. Never two in flight.** Phase 3 defines the
values key Phase 4 has to emit; landing them out of order means guessing at it.

**Delegate every read-only sweep; keep the conclusion, not the transcript.**
`launcher.py` is still a quarter of the repo's churn (#129) and still does not
fit anyone's head.

**Cite `path:line`, never paste the file.** Anchors are listed per phase so
nobody has to go looking.

**Write evidence to disk, link it from the PR.** `.claude/evidence/` is where
the last run's went.

**Batch the cluster.** Phases 1-4 are unit-testable. Phase 5 is the only one
that touches p47 and it exercises everything at once.

**The tunnel is a shared resource and only Giles can raise it.** It dropped once
mid-run on 2026-08-22. If Phase 5 stalls on it, say so and stop.

---

## What the last run established — do not re-derive

Facts that cost cluster time to obtain and are cheap to get wrong from a desk:

**A helm list *replaces* across the parent/child values merge; it does not
merge.** `p47-services/services/values.yaml` gives every IOC a `beamline-data`
volume. `bl47p-ea-fastcs-01` declared no `volumes:` of its own and so inherited
it; the moment a service declares one, the inheritance is gone. The live proof is
`bl47p-mo-ioc-01`, which declares `dev-shm` and whose running pod carries no
`beamline-data` at all. This is the whole of #192's difficulty.

**Argo's value-file order is `../values.yaml` then `values.yaml`** — the service's
own wins. From `argocd-apps` 5.5.0, `templates/_apps.tpl:54-56`. Render the same
way or precedence will mislead you:

```
helm template <svc> . -f ../values.yaml -f values.yaml
```

and add `--set global.location=bl47p --set global.domain=p47` if you skip the
parent file, or the chart fails on a nil `global`.

**Diff the render against the live StatefulSet before pushing anything.** That is
what caught both of the mistakes that would have damaged a beamline IOC.

**`p47-services` has a values schema with `unevaluatedProperties: false`**, so a
new values key must be declared in `.helm-shared/values.schema.json` or helm
refuses the values before templating.

**The two targets, and what each is for.** `bl47p-ea-fastcs-01-0` is the
canonical case — python-copier-template, `/app` + `/app/.venv`, a real uv project
with `.git`, `stdio-socket --ptty` as PID 1, no livenessProbe, gid 37887.
`bl47p-mo-ioc-01-0` is the compiled-IOC probe — `/epics/ioc/start.sh`, venv at
`/venv` with a separate `/python`, **no `/app`**, and a 120s/30s probe. The layout
is deployable and **inert** on it, and what hotfixing a compiled IOC would mean
stays on #34.

**A working prototype of Phase 3 already exists**, deployed and measured: the
`podbench-hotfix-claim` branch of `p47-services`, `.helm-shared/templates/hotfix_claim.yaml`.
Phase 3 is largely a matter of moving it into podbench and generalising it.

---

## Phase 1 — the claim survives Argo

**Issue #190. Two lines, and a data-loss bug, so it goes first.**

`Charts/podbench/templates/pvc-hotfix-project.yaml` carries
`helm.sh/resource-policy: keep` and nothing else, and `commonAnnotations`
defaults to `{}`. Helm's resource policy is honoured on *Helm's* uninstall path;
Argo prunes from its own diff and never takes it. So the claim that the
template's own comment calls unlosable is prunable by the controller this mode
was designed around.

* **Carry both annotations**, for the reason the prototype does: they guard
  different paths and neither implies the other.
* **While here**, the chart's `description` still says the PVC is "mounted **over**
  an application's site-packages". Beside, never over — the same stale vocabulary
  #180 swept out of the CLI.

**Falsified if:** the added annotation changes what `helm uninstall` does, or a
claim survives that the user explicitly asked to be removed.

**Anchors:** `Charts/podbench/templates/pvc-hotfix-project.yaml`,
`Charts/podbench/values.yaml:156` `commonAnnotations`,
`Charts/podbench/Chart.yaml` description.

---

## Phase 2 — `vscode` opens the project, not an empty home

**Issue #189. The one a user hits on their first real try.**

`launcher.py:6126` passes `folder=seat_layout(session).home` unconditionally, so
the verb that exists to put an editor on the code opens the one directory that
does not contain it — and a user who does not notice edits the image's copy
through `/proc/1/root`, where nothing they write ever runs.

* **Open the claim when `session.hotfixed`.** Everything needed is already there:
  the field is set in `attach` from the spec-derived predicate (#177), and
  `vscode` shares that seat-landing path, so the claim is mounted before the
  folder is chosen.
* **Say which folder was chosen and why**, by the same rule the auto-mount
  follows: an answer the user did not ask for is only acceptable if the output
  names it.
* **Keep the home for every pod without the layout**, and keep the existing
  guarantee that the folder is never `/` and never relative.

**Falsified if:** a plain `vscode` on a pod with no layout opens anything but the
seat's home, or the claim is opened on a pod where the seat did not actually
mount it — the `subPath` refusal degrades to a note, so `hotfixed` is true there
while the mount is absent, and that case must still open the home.

**Anchors:** `launcher.py:6126` and the docstring above it, `launcher.py:3940`
`seat_layout`, `editor.py:529` `open_seat`, `editor.py:567` the folder guard.

---

## Phase 3 — podbench ships the claim

**Issue #191.**

The chart creates claims from one central `hotfixProject.claims[]` list — a
namespace-wide release naming every application that might ever be hotfixed,
whose lifecycle is not the pod's. Phase 5 of the last plan decided per-service
instead, and did it by hand-writing a template into the *consumer's* repo, so
every site that adopts hotfix mode copies and maintains its own.

* **Ship it as a subchart** a target chart depends on, rendering nothing unless a
  boolean is set — so it can sit in a shared `Chart.yaml` that every service
  inherits, hotfix-enabled or not.
* **Keep the central route working** for anyone already using it.
* **Do not assume ioc-instance.** The boolean and the claim name are the whole
  contract.
* **Only the PVC moves.** `volumes:`, `volumeMounts:`, `command`/`args` and
  `podSecurityContext` are fields inside the *target's* pod template; a subchart
  cannot reach into a sibling's StatefulSet, and pod volumes are immutable after
  creation. That split is forced and this phase does not change it — Phase 4 is
  what makes the target's side cost nothing to write.

**Falsified if:** the subchart renders anything on a service that has not asked
for it, or the central route breaks.

**Anchors:** `Charts/podbench/templates/pvc-hotfix-project.yaml`,
`Charts/podbench/values.yaml:99` `hotfixProject`, and the deployed prototype at
`p47-services` `podbench-hotfix-claim`.

---

## Phase 4 — `--print-values` emits the whole values file

**Issue #192. The largest, and the one that makes the workflow a paste.**

`--from-pod` emits values needing no hand-editing except for one thing a pod
cannot settle: `volumes:` and `volumeMounts:` are a whole key each, and read from
a live pod a chart-generated volume is indistinguishable from one the service
declared. Read from the **values file**, it is decidable.

* **`--values <path>` emits that file, complete**, with podbench's keys merged in.
* **Handle the shared parent file.** A service declaring `volumes:` for the first
  time must absorb what the parent declared, or the emitted file silently
  unmounts a beamline directory. See *What the last run established*.
* **Emit whatever key Phase 3 settled on** for the claim, so the output is a
  complete deployment and not a deployment minus one line.
* **`values_snippet` stays pure.** It keeps its signature and acquires no file
  access, for the same reason it acquires no cluster access. The merge is a
  wrapper, exactly as `--from-pod` is.

**Falsified if:** the emitted file loses any key the input had, `values_snippet`
gains a file or cluster dependency, or the merge assumes a key name
`values_snippet` would have let the user rename.

**Anchors:** `hotfix.py` `values_snippet` and its key-name parameters,
`_read_print_values_from_pod`, `EXISTING_MOUNTS_WARNING`, and the root callback.

---

## Phase 5 — the workflow, run as a user would

The acceptance test is the four steps at the top of this plan, performed in
order, with **no hand-editing of yaml at any point**.

Against `bl47p-ea-fastcs-01` on `p47-beamline`, from a clean start:

| Step | Assertion |
|---|---|
| 1 | `--print-values --values services/bl47p-ea-fastcs-01/values.yaml` emits a file that is deployed **as emitted**. Diff it against the live StatefulSet: only podbench's additions, and `beamline-data` still present. |
| 2 | The claim comes from the podbench dependency in `.helm-shared/Chart.yaml`, and `p47-services` carries **no hotfix template of its own**. |
| 3 | Push, Argo syncs, the pod comes back carrying the layout. |
| 4 | `podbench vscode bl47p-ea-fastcs-01-0` lands a seat, mounts the claim unasked, reports `[x] iterate`, and **opens `/podbench/app`**. |

Then edit, `apply`, and hold the same four numbers the last run did —
`restartCount` unchanged, the recorded child pid moved, the edit live in the
running process, every seat alive. Those four are the contract; anything else is
commentary.

Finally re-prove #190 the way it was proved before: revert the repoint and
confirm the claim is still `Bound` while everything else the branch added is
pruned.

### Starting state, and what needs clearing first

**Two claims are still on the beamline** — `bl47p-ea-fastcs-01-podbench-project`
and `bl47p-mo-ioc-01-podbench-project`, 2Gi, `Bound` — and the fastcs one still
carries the last run's checkout with commit `d48471a` on it, a `HOTFIX_MARKER`
print. Repointing without clearing it means the IOC comes up running that
immediately. Decide deliberately: wipe for a clean first run, or keep it as a
ready-made `status` case. **The test service account has no `delete` on
persistentvolumeclaims** — clearing them needs Giles.

**The seat image on the node is stale.** During the last run the seat reported
`0.2.0b3.dev27+…d20260818` against a `0.6.2.dev3` launcher. Use `--pull always
--new`, or pin `--image` to the 0.7.0 tag.

**`p47-deployment` is on `main` for both services**, and the `p47-services`
branch `podbench-hotfix-claim` still holds the last run's hand-written layout.
Phase 5 should replace that branch's contents with generated output, not add to
it — the point is that nothing is hand-written any more.

**Put it back when done.** Revert the repoint; confirm both pods return to their
original entrypoints. The branch may stay; the repoint must not.

---

## What this plan does not touch

* **`ioc-instance` growing a `hotfixMode` boolean.** It would collapse the
  target's values change to one line, because that chart owns the pod template
  *and* the entrypoint. It is deliberately not the route: this has to work for
  charts podbench does not control, and an upstream chart change would block
  every adopter on a chart release. Worth doing separately, on its own merits,
  in `ec-helm-charts`.
* **What hotfixing a compiled IOC means.** Still #34, still parked.
* **blueapi.** Still out of scope.
* **`ioc-group` targets.** Still cannot carry the layout — it deep-copies one
  shared block per entry, so a group's IOCs would collide on one RWO claim.
