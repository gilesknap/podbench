# Hotfix mode, after the first live run

Hotfix mode met a cluster on 2026-08-22 — `p47-beamline` on pollux, podbench 0.6.0,
through `k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig`. The core mechanism
holds. Five defects surfaced, one of them a blocker, and this plan closes them.

- **Base:** `main` at or after `fc636d5` (#176 merged).
- **Decided:** 2026-08-22, five decisions, recorded below and in the issues they name.
- **Evidence discipline:** the same rule as `hotfix-beside-the-app.md` — *measure the
  seat, do not restate the request.* Every phase states what would falsify it. Where
  a measurement could not be taken, say so; never "fine".

Read `hotfix-beside-the-app.md` first for the design this builds on, and
`docs/explanations/spikes/phase0-report.md` for the constraints that outrank the
brief. This plan does **not** repeat either.

---

## How to work this plan

The work is five independent phases across two files that everything else imports.
The failure mode is not difficulty, it is a main context stuffed with file dumps
until the agent stops seeing the thing it is holding. So:

**One phase, one branch, one PR. Never two in flight.** Phase 1 moves
`is_hotfixed`, which phases 3 and 4 both read. Landing them out of order means
rebasing onto a moved definition for no gain.

**Delegate every read-only sweep; keep the conclusion, not the transcript.**
"Find every caller of `is_hotfixed` and say which would change behaviour if it
were spec-derived" is a subagent task whose answer is four lines. Reading
`launcher.py` into the main context to find out is 2,800 lines you then carry
through the whole phase. `launcher.py` is a quarter of the repo's churn (#129) and
does not fit anyone's head.

**Cite `path:line`, never paste the file.** When a phase needs an anchor, name it.
The anchors this plan depends on are listed per phase precisely so nobody has to
go looking.

**Write evidence to disk, link it from the PR.** Cluster output goes in a file and
the PR references it. A 200-line `kubectl` dump pasted into a PR body is
unreviewable and, pasted into context, is unrecoverable.

**Batch the cluster.** Phases 1-4 are unit-testable and need no cluster at all.
Phase 5 is the only one that touches p47, and it exercises everything at once.
Raising the tunnel per phase wastes the one resource that needs a human.

**The token expires.** The SA token is 24h and only Giles can raise the tunnel. If
Phase 5 stalls on it, say so and stop rather than working around it.

---

## What the live run proved — do not re-derive

`hotfix apply` on `bl47p-ea-fastcs-01-0`, measured either side:

| | before | after |
|---|---|---|
| `restartCount` | `0, 0` | `0, 0` |
| recorded child pid | `7` | `577` |
| `HOTFIX_MARKER` in running code | absent | `podbench-live-test-0.6.0` |
| seats alive | `podbench-1`, `podbench-2` | both |

The running process's interpreter resolved to
`/podbench/app/.python/cpython-3.11.13-linux-x86_64-gnu/bin/python3.11` — the
claim's, not the image's. Hold-plus-kill works; #161's `exitCode: 137` seat death
did not occur. `init` seeded and rebuilt the venv; `status` read it and reported
`active`, then `+1 commit(s)`.

**The layout is inert on a compiled IOC.** With the claim mounted and unseeded,
`bl47p-mo-ioc-01` ran the supervisor as PID 1, recorded its child, kept
`restartCount: 0` and stayed ready. It can be deployed to a cpp IOC today; it just
cannot be seeded.

Two chart facts, measured, that constrain what is deployable:

* **`ioc-group` varies only env vars.** It deep-copies one shared `ioc-instance`
  block per entry in `iocs`. A group's IOCs cannot be given different volumes, and
  would collide on one RWO claim. Single-IOC services only.
* **`extraContainers`/`initContainers` get `volumeMounts: *volumeMounts` forced on
  them.** A per-container mount in values is silently discarded; they inherit the
  main container's. Benign here, but the layout cannot be applied to one container
  of a pod and not another.

Corrections to `hotfix-beside-the-app.md`'s preflight, which has drifted:
`pods/ephemeralcontainers` is now `get patch update` (it records `update: no`), and
p47-services pins **`ioc-instance 5.6.1`**, not `5.0.1-beta.2`. All five values are
present in 5.6.1 and its `volumes` key `$ref`s the full upstream podspec schema, so
generic ephemeral volumes are accepted.

---

## Phase 1 — revive `is_hotfixed`, and stop making the user arrange the mount

**Issue #177. The blocker, and the one with a non-obvious root cause.**

The symptom is that `hotfix init` refuses with "`/podbench/app` is not present: the
claim is not mounted. Run `podbench hotfix --print-values` and deploy the five
values it emits" — on a pod where the values *were* deployed, the claim *was*
mounted and writable in all three application containers, and the PVC was `Bound`.
Only the seat could not see it, because `attach` mounts `podbench-home` and nothing
else.

The root cause is one dead signal:

* `launcher.py:963` `is_hotfixed()` reads `HOTFIXED_ANNOTATION` from the pod.
* Nothing writes it. `hotfix.py:441` `manifest_annotations()` is referenced only by
  `__all__` and its own unit test — Phase 4 of the previous plan deleted
  `annotate()`, deliberately, because Argo self-heal strips pod-template
  annotations (#32).
* So `is_hotfixed` is permanently `False`. Confirmed on the live run: zero
  `hotfixed` annotations on a genuinely hotfixed pod.

Three features are silently off as a result:

1. `launcher.py:2247` emits `UNMOUNTED_HOTFIX_NOTE` when attaching to a hotfixed
   pod without the claim — written for exactly this failure, with a comment noting
   it is "the last moment the answer can be acted on". It cannot fire.
2. `seat_kind()` (`launcher.py:1032`) can never return `SeatKind.HOTFIX`. That enum
   member is unreachable.
3. The listing keyed on it (`launcher.py:1056`) shows nothing.

### The work

* **Re-ground `is_hotfixed` on the pod spec**, not an annotation: the pod declares
  the claim volume *and* a container's args carry the supervisor loop. Both are
  already emitted by `values_snippet` and both survive Argo, which is the whole
  reason the annotation went. Keep the docstring's warning about not reading the
  manifest — that reasoning still holds.
* **`attach` mounts the claim when the pod is prepped for hotfix**, at
  `HOTFIX_APP_PATH`, by the same convention `podbench-home` already follows
  (`_CONVENTION_VOLUMES`, `launcher.py:978`). **Say so in the report** — a mount the
  user did not ask for is only acceptable if the output names it.
* **Keep `UNMOUNTED_HOTFIX_NOTE` for reconnects.** An ephemeral container's
  `volumeMounts` are fixed at creation, so a reconnect to a seat that lacks the
  claim cannot be repaired — that path must still warn, and point at `--new`.
  podbench already words this correctly for a hand-passed `--mount`; match it.
* **`hotfix init` lands its own seat** when none is running, as `podbench vscode`
  does ("land a seat sized and provisioned for an editor, and open it"). The user
  should never type `attach` to do a `hotfix`. `--seat` stays for an odd name.
* **Delete `manifest_annotations()`** or wire it to something. Dead code that looks
  live is what caused this.

**Falsified if:** a spec-derived `is_hotfixed` returns true for a pod that merely
mounts a claim called something similar (test the supervisor half too), or the
auto-mount changes what a plain `gdb`/`vscode` seat does on a pod with no hotfix
layout.

**Anchors:** `launcher.py:963` `is_hotfixed`, `:978` `_CONVENTION_VOLUMES`, `:985`
`shares_workload_volume`, `:1013` `seat_kind`, `:2247` where the note is emitted,
`:4871` where it is worded; `hotfix.py:441` `manifest_annotations`, `:1067`
`seat_container`.

---

## Phase 2 — `--from-pod` becomes the default, and its failure names the way out

**Follow-on to #176, which is merged.**

`--print-values` currently takes everything by hand, and that is why #176 existed:
a human had to supply the probe, and omitting its timings silently moved a compiled
IOC from 120s/30s to the Kubernetes defaults 0s/10s. The gid has the same shape —
it is a `<the application's runAsGroup>` placeholder for the same reason.

Reading the pod removes the whole class. **User experience is king: reading the pod
is the default.**

* **`--from-pod` reads the entrypoint, the whole `livenessProbe` and the gid** off
  the named pod, and emits values needing no hand-editing.
* **`values_snippet` stays pure.** It keeps its current signature and its doctests;
  `--from-pod` is a thin cluster-reading wrapper that fills the same arguments.
  This is not negotiable — it is what makes the emitter testable without a cluster.
* **`--no-from-pod` is the escape**, for CI, an offline machine, or a pod that does
  not exist yet. The manual flags keep working exactly as they do today.

### The error discipline this phase exists for

Making a cluster read the default means every `kubectl` failure now lands on a user
who did not ask for one. **Every such error must name `--no-from-pod` and say what
it costs.** Not a bare `kubectl` relay, and not a generic "try `--no-from-pod`" —
the consequence is the point:

> podbench: could not read pod `bl47p-mo-ioc-01-0` in `p47-beamline`: \<the kubectl error>.
>
> `--print-values` reads the target by default so the emitted values need no
> hand-editing. To emit them without a cluster, pass `--no-from-pod` and supply
> `--entrypoint`, `--gid` and `--liveness-probe` yourself.
>
> Supplying them by hand is how #176 happened: a chart renders a supplied
> `livenessProbe` wholesale, so a timing you leave out becomes the Kubernetes
> default and the target is probed sooner and more often than it was before.

Cover, at minimum: no kubeconfig or no current context; the pod not existing;
`get pods` forbidden; the container named by `--target` not existing; and the pod
existing but declaring no `livenessProbe` (which is **not** an error — the
canonical fastcs target has none — and must not be reported as one).

**Falsified if:** any `--from-pod` failure path exits without naming
`--no-from-pod`, or `values_snippet` acquires a cluster dependency.

**Anchors:** `hotfix.py:2193` `values_snippet`, the group callback `root()` at
`hotfix.py:2434`, `launcher.py:2431` `id_correction` for how gid is read elsewhere.

---

## Phase 3 — a missing project is not a ptrace denial

**Issue #178, whose parent is #34 (parked).**

`init` against `bl47p-mo-ioc-01-0` exits 2 with "`/proc/1/root/app` is not
readable, so the seed cannot run … reaching it needs the ptrace rung … `podbench
doctor` names the mechanism."

The ptrace rung was fine. From that same seat `/proc/1/root` listed cleanly;
`/app` simply does not exist on an epics-containers image, whose venv is at `/venv`
with a separate `/python`. So the message sends you to `doctor`, which reports the
rung healthy, leaving a contradiction and no next step.

* **Separate the two failures.** "I cannot see the target's root" is a real ptrace
  problem and needs its current message. "The target's root has no project at
  `<path>`" is a different thing with a different fix. Test for the first
  explicitly — `/proc/1/root` traversable — rather than inferring it from the
  second.
* **Make the project root and venv path configurable** rather than the `/app` and
  `/app/.venv` constants. `seed_source()` (`hotfix.py:~436`) hardcodes both, and the
  runtime switch `values_snippet` emits hardcodes `/podbench/app/.venv/bin/python`.
  A layout that differs should be expressible without a code change.

**Out of scope, and stays on #34:** what hotfixing a compiled IOC *means*. The
running process is a compiled binary; the plausible targets are a Python support
module installed into `/venv` or the ibek `ioc.yaml`, and choosing between them is
a design question this phase does not settle.

**Falsified if:** the new message fires on a genuinely ptrace-denied seat, or the
configurable paths change the default behaviour for a python-copier-template image.

**Anchor:** `hotfix.py:420` `seed_source`, which hardcodes both paths.

---

## Phase 4 — the two reports that describe something else

Small, and one PR. Both are `attach`/help text; neither changes behaviour.

**#179 — `attach`'s capability report contradicts hotfix mode.** On both p47 pods
it reported `[ ] iterate … The relaunch loop needs a sacrificial dev pod
(`podbench dev`), never the live workload`, and on `bl47p-mo-ioc-01` a `61-91s`
restart deadline. Both false: the supervisor was PID 1 so killing the child does not
kill it, and the probe was the hold-aware wrapper that returns 0 while the hold
exists. After Phase 1, `attach` can tell — the same spec-derived predicate. Report
`[x] iterate` naming `hotfix apply`, and suppress the probe deadline when the
probe's exec command references `HOTFIX_HOLD_PATH`. Related to #21, whose budget is
correct in general and wrong for a hold-aware probe.

**#180 — `hotfix init --help` describes the pre-Phase-3 design.** It says "verify
the seeded claim" (it *performs* the seed), and calls `--venv` "the application's
venv path" (the claim mounts *beside* the project — that renaming was the whole
design). Sweep `hotfix`'s user-visible strings for the old vocabulary; Phase 3
touches the same register, so do them together if it is one diff.

---

## Phase 5 — the cluster run, against the real `bl47p-ea-fastcs-01`

The only phase that needs p47, and the one that decides whether phases 1-4 actually
work. Unit tests cannot falsify any of them: every defect in this plan was found by
a cluster, and three of the four were things that *looked* right in the source.

**The target is the live `bl47p-ea-fastcs-01` on `p47-beamline`**, not a duplicate
and not the k3s bench. It is the canonical case — python-copier-template layout,
`/app/.venv`, a real uv project with `.git`, a `stdio-socket --ptty` wrapper as
PID 1, two Python containers — and it is the pod every measurement in this plan was
taken against, so a regression is visible as a difference rather than a new
unknown. `bl47p-mo-ioc-01` comes along as the compiled-IOC probe for Phase 3.

### Re-prove each phase on the pod, in order

Run these against `bl47p-ea-fastcs-01-0` before the new work below. Each names the
observation that would falsify its phase — not "it ran", but the specific thing the
phase claims.

| Phase | Assertion on the live pod |
|---|---|
| 1 | `podbench attach bl47p-ea-fastcs-01-0` with **no** `--mount` lands a seat that already has `/podbench/app`, and the report says it mounted it. Then `hotfix init` with **no** running seat lands its own and succeeds — the refusal that started #177 never appears. |
| 2 | `--print-values --from-pod bl47p-ea-fastcs-01-0` emits values that need no editing; diff them against the hand-written ones on the `podbench-hotfix-test` branch and account for every difference. Repeat against `bl47p-mo-ioc-01-0` and confirm `initialDelaySeconds: 120` / `periodSeconds: 30` appear without being asked for — **this is the end-to-end assertion for #176.** Then break it deliberately: wrong context, forbidden `get pods`, absent pod, and confirm each error names `--no-from-pod` and its consequence. |
| 3 | `hotfix init` against `bl47p-mo-ioc-01-0` says the image has no project at `/app`, and does **not** mention ptrace or `podbench doctor`. Confirm `/proc/1/root` is still traversable from that seat, so the distinction is real and not a reworded guess. |
| 4 | `attach` on the hotfixed fastcs pod reports `[x] iterate` naming `hotfix apply`, and reports no probe deadline on `bl47p-mo-ioc-01-0` while its wrapped probe is in place. |

Then the full workflow once more end to end — `init`, edit, `apply`, `status` —
holding the same measurements the first run took: `restartCount` unchanged, the
recorded child pid moved, the edit live in the running process, and every seat still
alive. Those four numbers are the contract; anything else is commentary.

### Then the two things no cluster has ever done

#### A real per-pod claim

The previous plan left "survives pod replacement" blocked on a real PVC, because a
generic ephemeral volume dies with its pod. The route, decided 2026-08-22:

**The claim is declared per hotfix-enabled service, alongside its pod, and
annotated against deletion by both Helm and Argo.** Not a central claim — its
lifecycle should match the pod it serves.

```yaml
metadata:
  annotations:
    helm.sh/resource-policy: keep
    argocd.argoproj.io/sync-options: Prune=false,Delete=false
```

This is #67 (both annotations, because Argo does not take Helm's uninstall path) and
#68 (declared once, toggled by one boolean) arriving together.

`ioc-instance 5.6.1` cannot express it: it creates exactly one PVC
(`<release>-data`, `_datavolume.tpl`), has no hook for a second and no
`volumeClaimTemplates`. But **`.helm-shared/templates/` in p47-services is a real
Helm templates directory** — currently one file, `{{ include "ioc-instance" . }}`,
symlinked into every service. Add a second template there rendering the claim when
a value is set and empty otherwise, exactly #68's pattern. No upstream chart change,
nothing central touched.

Then: delete the pod, confirm the hotfix is still running and `status` still reports
it. Bump the image on the target and confirm `status` says `image-changed` rather
than going quiet.

#### `consolidate`, still completely untested

It is the only verb no cluster has run. The checkout's origin is
`DiamondLightSource/fastcs-example`, which is not ours to push branches to.
**Re-run `init` with `--repo` pointing at a `gilesknap` fork**, then `consolidate`
pushes there. Exercise the `superseded` verdict afterwards — consolidate, then bump
the image, and confirm `status` names the claim as shadowing the released fix.

#### Getting back onto the cluster

The p47-services branch `podbench-hotfix-test` still carries the working layout for
`bl47p-ea-fastcs-01` and `bl47p-mo-ioc-01`. In p47-deployment, `git revert a55fdf4`
repoints both services at it. The beamline was left clean: both services on `main`,
original entrypoints, ephemeral claims collected.

Two things that bit last time. `hotfix init` takes longer than a 2-minute tool
timeout — run it backgrounded. And `mo-ioc-01`'s wrapped probe needs
`initialDelaySeconds: 120` / `periodSeconds: 30` carried over; after Phase 2 that is
automatic, and **that is the end-to-end assertion for Phase 2** — emit its values
with `--from-pod` and diff them against the hand-written ones in the branch.

**Falsified if:** the hotfix does not survive pod replacement against a real claim,
Argo prunes the annotated claim anyway, or `status` stays quiet on a changed image.

---

## What this plan does not touch

* **blueapi.** Still out of scope, still #34-adjacent, still the open
  converge/replace/leave question in `hotfix-beside-the-app.md`.
* **`ioc-group` targets.** Cannot carry the layout at all — see above.
* **The repo-versus-cluster drift in p47.** `bl47p-ea-panda-01`, both `dcam`s,
  `bl47p-c7-sim-01` and `bl47p-gateways` are in `p47-services/services/` and not
  running. It does not block anything here.
