# Phase 7 — the live walk

2026-08-24, `p47-beamline` on pollux, through
`k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig`, against the real
`bl47p-ea-fastcs-01-0`. Launcher under test:
`/home/giles/code/podbench/.venv/bin/podbench`, version
**0.7.3.dev41+g25fe18bd8**, branch `hotfix/easy-to-drive` at `25fe18b` when this
run started. Seat image:
`ghcr.io/gilesknap/podbench:0.7.3-beta.1-hotfix-easy-to-drive`.

**This is the walk the previous attempt could not perform.** That attempt (same
file, now overwritten) found the token dead before any cluster call succeeded
and made zero mutations. This run used a token minted 2026-08-24T06:00:41Z
(24h TTL) and reached the cluster on the first try — `kubectl get pod
bl47p-ea-fastcs-01-0` at 2026-08-24T06:03:54Z returned `Running`, 2/2, with
`resourceVersion 1054230919`. No `Unauthorized` was seen anywhere in this run.

Cross-reference `.claude/evidence/phase7-the-contradiction.md` for the settled
baseline this run builds on: the git/pod contradiction (`enabled: false` gates
only the claim subchart; the target had been hotfix-wired since
2026-08-23T17:31:54Z) and the byte-for-byte read-only transcripts against this
same target. That contradiction is **resolved by this run** — see §1 and §7.

---

## 1. Resolving the contradiction, from the write side

`hotfix values --from-pod` against the live pod, merged into the deployed
values file with `--values` and `--parent-values`:

```
podbench hotfix values --app bl47p-ea-fastcs-01 \
  --from-pod bl47p-ea-fastcs-01-0 -n p47-beamline \
  --values services/bl47p-ea-fastcs-01/values.yaml \
  --parent-values services/values.yaml
```

stdout was clean (see §2 for the fragment form); the merge printed one note on
stderr (`the application's keys went under ioc-instance ...`) and the diff
against the deployed file was **exactly one line**:

```diff
 podbench-hotfix-claim:
-  enabled: false
+  enabled: true
   size: 2Gi
```

Every other key `values` emitted — `volumes`, `volumeMounts`, `command`,
`args`, `podSecurityContext.fsGroup` — already matched the deployed file byte
for byte. That is the contradiction confirmed from the write side: the pod was
never running anything the claim subchart's `enabled` flag controlled, only
the PVC that outlived the flag via `Prune=false,Delete=false`. Committed as
`1659a4a`, pushed to `podbench-hotfix-claim` at **06:05:30Z**.

Polled for a landing signal (PVC `resourceVersion`, StatefulSet
`.metadata.generation`) every ~10s for 113s: **neither changed**
(`rv=1053856421`, `gen=18/18`, all twelve samples). This is itself the honest
reading, not a stall: live state already equalled desired state for every
object apart from Argo's own internal sync-status bookkeeping, which this
token cannot read (`kubectl get applications.argoproj.io` is invisible here,
per the plan). The re-agreement was confirmed by other means instead —
`hotfix check`/`init` reading the target immediately afterward (§3), and
`hotfix retire`'s "wiring" row reading `[x]` once the wiring was actually
reverted at the end of this run (§7) — both of which corroborate that git and
pod agree from here on, even though no single resource's `resourceVersion`
proves the moment it happened.

---

## 2. `hotfix values` — stdout purity and the read-what's-there path

Fragment form, redirected exactly as a user would:

```
podbench hotfix values --app bl47p-ea-fastcs-01 \
  --from-pod bl47p-ea-fastcs-01-0 -n p47-beamline \
  > /tmp/hotfix-fragment.yaml 2> /tmp/hotfix-fragment.stderr
```

`exit=0`. stdout is the five-key fragment (`podbench-hotfix-claim`,
`volumes`, `volumeMounts`, `command`, `args`, `podSecurityContext`) with
nothing else in it; stderr carries exactly one `WARNING` naming the six
volumes it could not classify (`autosave-volume, beamline-data,
bl47p-ea-fastcs-01-data, config-volume, opis-volume, runtime-volume`) and
telling the reader to merge rather than paste. **9b holds**: nothing extra
reached stdout, the note stayed on stderr.

---

## 3. `check` and `init` agree, on the real target, in the accept direction

`check`, no `--repo`:

```
  [ok]    target         bl47p-ea-fastcs-01-0, container bl47p-ea-fastcs-01, statefulset/bl47p-ea-fastcs-01
  [ok]    claim          ... it already carries a project: `hotfix init` seeds nothing over one ...
  [ok]    supervisor     bl47p-ea-fastcs-01 is running it: /tmp/podbench-child.pid exists
  [warn]  seat           no podbench container is running ... `hotfix init` lands one itself
  [ok]    target root / project / interpreter   not asked: already seeded
  [ok]    liveness       no livenessProbe, so nothing cuts a hold short
  [warn]  source         the image names https://github.com/DiamondLightSource/ubuntu-devcontainer,
                          which its own repository ghcr.io/diamondlightsource/fastcs-example-debug
                          does not correspond to: `hotfix init` with no `--repo` would clone that
                          repository, so pass `--repo URL` if it is not this application's source.
VERDICT: nothing measured here blocks `podbench hotfix init` (exit 0)
```

`init`, same target, no `--repo`, run right after:

```
claim already seeded at /podbench/app
claim seeded, venv interpreter 3.11.13
checkout already present at /podbench/app
the image's labels name https://github.com/DiamondLightSource/ubuntu-devcontainer, not
git@github.com:DiamondLightSource/fastcs-example.git: inherited from its base image, so its
revision is not this repository's
base commit c317383 ASSUMED (the image names 603392d, but nothing outside the image confirms
its labels are this repository's); pass --base-commit SHA
rebuilt the venv at /podbench/app/.venv
wrote /podbench/app/.podbench-hotfix.json
(exit 0)
```

**Both directions this target can exercise agree.** `check`'s only `WARN` is
`source`, worded as the exact condition #205/Phase 7 fixed
(`SOURCE_LABEL_UNCORROBORATED`); `init` does not refuse on it — it proceeds,
and records exactly what the WARN said it would: an **assumed** base
(`baseCommitAssumed: true`), not a silently-trusted foreign revision. The
`FAIL`-refuses-and-check-must-have-said-so direction was **not exercised
live**: this target never presents a shape `check` fails on (it already has a
seeded claim and a resolvable origin), so there was no live case to refuse.
That half of the contract stands on the unit tests
(`tests/test_hotfix.py`, the `check`/`init` pairs) and on the source reading
recorded in the previous (blocked) run's "How far this got" section — not
re-verified against a live cluster this run. Recorded as **not measured
live**.

### Is `init`'s call the right one?

Yes, and it is more careful than the WARN text alone suggests. The claim
already had a checkout with its own `origin` (`git@github.com:
DiamondLightSource/fastcs-example.git`, seeded on 2026-08-23 by an earlier
session), so `corroborate_source` used *that* — not the wrong label — as the
independent naming: the manifest's `repo` field came out correct
(`git@github.com:DiamondLightSource/fastcs-example.git`), not the mislabelled
`ubuntu-devcontainer`. What stayed uncorroborated was only the **revision**:
the label's `603392d` is foreign to this repository and `_has_commit` cannot
find it, so `init` did not guess a translation — it recorded the checkout's
own `HEAD` (`c317383`, a leftover local-only commit from the earlier session)
as an **assumed** base and said so, in both the CLI output and
`.podbench-hotfix.json` (`"baseCommitAssumed": true`). That is the module's
own "honest-uncertainty path" (`hotfix.py`, the `base_commit_from` docstring)
doing exactly what it says: neither refusing an emergency fix over a foreign
label, nor pretending precision it does not have. `check` and `init` therefore
disagree with nothing here — `init` is not a rubber stamp on `check`'s WARN,
it is the more careful version of the same judgement.

---

## 4. Landing the seat — a real defect, worked around, digest confirmed

`init`'s auto-land path (`seat_container(..., land=True)`, `hotfix.py:1580`)
calls `attach(kube, pod)` with **no `--image` and no `--pull` passthrough** —
`hotfix init`'s own `--help` has no such flags. The first `init` run above
therefore landed `podbench-1` at `ghcr.io/gilesknap/podbench:main`
(`imageID sha256:3080e3fd...`), not this branch's build, and printed exactly
the warning that exists for this: *"this seat and this launcher are different
builds of podbench ... `--pull always --new` re-lands from the tag as it
stands now."* This is a real gap against the task's own hard rule (pass the
branch image explicitly to any verb that lands a seat) that `hotfix init` has
no lever for.

**Worked around, not silently accepted**: landed a second seat by hand, then
pointed every later `hotfix` command at it with `--seat`:

```
podbench attach bl47p-ea-fastcs-01-0 -n p47-beamline \
  --image ghcr.io/gilesknap/podbench:0.7.3-beta.1-hotfix-easy-to-drive \
  --pull always --new
```

`seat p47-beamline/bl47p-ea-fastcs-01-0[podbench-2] (new)`, `version
0.7.3.dev41+g25fe18bd8, the same build as this launcher`, rung `degraded`
(uid/gid 37887, `CapEff 0000000000000000`), landed 06:11:10Z. **Digest
confirmed against the tag's current resolution**, not merely trusted: the
seat's own `imageID` was `sha256:37f398ddb8c2e3aaafeff12326429237771ad4609
6dd1a08f6f669717f628f2c`; a bare anonymous-token GHCR manifest HEAD for
`0.7.3-beta.1-hotfix-easy-to-drive` returned `docker-content-digest:
sha256:37f398dd...` — the same digest, byte for byte. `--pull always` pulled
what the tag resolves to *right now*, not a stale copy.

`check` and `init` were re-run with `--seat podbench-2` and produced
byte-identical output to the auto-landed run (§3) — the wrong-build seat had
not corrupted anything, it was simply the wrong seat to keep using.
`podbench-1` could not be un-landed (ephemeral containers are permanent, see
the `ephemeral-containers` skill) and stayed running, unused, for the rest of
this session; it is accounted for in §8.

**This is a defect worth fixing**: `hotfix init`'s auto-land should accept
`--image`/`--pull` (or read `$PODBENCH_IMAGE`, which `attach` already
honours) so an operator following the branch-image discipline this project
requires is not forced to pre-land a seat by hand before `init` can be told
which build to use.

---

## 5. The edit — live in the running process, proven through the application's own interface

`fastcs-example` exposes its controller over both PVAccess and Channel Access
(`controller.yaml`, `pv_prefix: T01-EA-FASTCS-01`). The edit added a new
read-only attribute to `TemperatureController`
(`src/fastcs_example/controllers.py`, on the claim, through the seat):

```python
hotfix_marker = AttrR(
    String(),
    initial_value="PODBENCH_PHASE7_LIVE",
    description="podbench phase 7: live edit marker",
)
```

Small, safe (a new read-only PV, nothing existing touched) and reversible (the
whole checkout is retired at the end of this run, §7). Written via `kubectl
exec -i ... podbench-2 -- python3 -`, confirmed on disk and in `git status`
(`M src/fastcs_example/controllers.py`) before `apply`.

Before `apply` (06:14:41Z): `bl47p-ea-fastcs-01` `restartCount` **0**, child
pid (`/tmp/podbench-child.pid`) **6**.

```
podbench hotfix apply pod/bl47p-ea-fastcs-01-0 -n p47-beamline --seat podbench-2 \
  -m "Phase 7: add a live-observable marker attribute to TemperatureController"
```

```
committed as podbench <podbench@local>
no packaging metadata changed; editable install still valid
1 commit(s) ahead of c317383 (an assumed base)
relaunched the application in bl47p-ea-fastcs-01 without a restart
```

06:14:50Z → 06:14:55Z, 5 seconds.

After `apply` (06:15:02Z): `restartCount` **0** (unchanged), child pid
**687** (moved). `ps` inside the container confirmed the new process tree:
PID 696, `/podbench/app/.venv/bin/python3
/podbench/app/.venv/bin/fastcs-example run ...` — running from the **claim's**
rebuilt venv, not the image's `/app/.venv`.

**The edit, read from the application's own interface, not the filesystem**:

```
$ kubectl exec -i bl47p-ea-fastcs-01-0 -c bl47p-ea-fastcs-01 -- /app/.venv/bin/python -
>>> from p4p.client.thread import Context
>>> Context('pva').get("T01-EA-FASTCS-01:HotfixMarker", timeout=3)
'PODBENCH_PHASE7_LIVE'
```

06:15:22Z. (The client also logged `pvxs.client.dup` warnings for the same PV
name from several other addresses on the shared beamline network — other PVA
responders answering the same name elsewhere on `192.168.250.0/24`, unrelated
to this pod and not investigated further; the `get` still resolved this pod's
own answer.) This is proof #3 of the four-number contract: a brand new PV,
naming a string this run wrote, read back over the wire the application
itself serves — not a file that changed, a running server that answers
differently than it did five minutes earlier.

---

## 6. `status` after `apply`

```
podbench hotfix status -n p47-beamline
```

```
  [ok]    p47-beamline/bl47p-ea-fastcs-01-0  +1 commit(s) from an assumed base  51c3dcc  active — hotfixed, base image unchanged
    base c317383 · podbench <podbench@local> · 2026-08-24T06:14:53+00:00
      51c3dcc  Phase 7: add a live-observable marker attribute to TemperatureController
STATUS EXIT=0
```

06:15:34Z. Truthful against everything measured directly above: one commit
ahead of the assumed base, the commit that made the observed PV change, image
unchanged (still the digest the hotfix was made against).

**Exit-code contract**: this run's own row was all-`ok`, so it only exercises
the `0` side live. The `1`-on-any-non-ok-row side, including a merely-held pod,
was **not induced live** — deliberately: manufacturing a held pod means
killing the live application's child and holding the supervisor open on a
shared test beamline for no operational reason, which the task's own
proportionality (small, safe, reversible) argues against doing just to watch
an exit code. It is directly unit-tested instead and was read from source this
session: `HotfixRow.ok` (`hotfix.py:1901`) is `self.health.ok and self.hold is
None` — a doctest at `hotfix.py:1912` asserts a held row's `.ok` is `False` on
its own — and `tests/test_hotfix.py::test_status_exits_non_zero_when_a_pod_
needs_attention` / `::test_status_exits_zero_when_everything_is_accounted_for`
assert `code == 1` / `code == 0` against fixture pods for exactly this. Recorded
as **not measured live**, backed by a passing unit-test contract.

`podbench hotfix` with no subcommand: **exit 2**, confirmed live
(`cli.require_subcommand`).

`hotfix status -A`: this session's token has no cluster-scope `pods` list
(`Forbidden`), so the facility-wide form could not be exercised — a namespace-
scoped credential doing exactly what a namespace-scoped credential should,
not a podbench defect. Recorded as **not measured** (RBAC, not a bug).

---

## 7. `retire` — the checklist, then closing the loop for real

First `retire`, before touching git again — this is the state the earlier
sync (§1) plus this run's own hotfix left the target in:

```
VERDICT: 4 of 4 steps of retirement remain (exit 1)
REMAINING: branch, image, wiring, claim
```

The `wiring` row named every field precisely: the `podbench-app`/
`podbench-home` volumes, the `/podbench/app` mount, the supervisor loop in
`command`/`args`, and `podSecurityContext.fsGroup: 37887` — all in
`ioc-instance`'s own values, not the claim subchart's, exactly the finding
`phase7-the-contradiction.md` opened with. **This time the wiring was actually
taken out**, not just the flag: `services/bl47p-ea-fastcs-01/values.yaml`
reverted to the pre-hotfix `ioc-instance` block (`git show e4e1724`) and
`podbench-hotfix-claim.enabled: false`, committed as `51d6440`, pushed
**06:17:06Z**.

Polled the StatefulSet for the args to change back, every ~10s: landed at
**06:20:19Z**, generation 18 → 19 — **3m13s** after the push, longer than the
"up to 2 minutes" the plan describes but within the range Phase 5 measured for
a claim-creation sync; this one was a plain spec change and still took
noticeably longer than §1's (admittedly unmeasurable) one. New pod
(`bl47p-ea-fastcs-01-0`, `creationTimestamp 2026-08-24T06:20:22Z`) reached
`Running`, both containers ready, by 06:20:37Z — **15s** from creation to
ready, both `restartCount 0` (a fresh pod, not a restart). No
`ephemeralContainers`, no `podbench-app`/`podbench-home` volumes, args back to
`stdio-socket --ptty "fastcs-example run ..."`, `podSecurityContext` back to
the chart's own `runAsUser/runAsGroup: 36096` (not the hotfix's `fsGroup:
37887`).

`hotfix status -n p47-beamline`: `no hotfixed pods in this namespace` (exit
0), 06:20:47Z. A namespace-wide pod scan for anything mounting a
`podbench`-named PVC found **none**.

### `--delete-claim` — the three-way check passed; RBAC refused the delete

```
podbench hotfix retire pod/bl47p-ea-fastcs-01-0 -n p47-beamline --delete-claim
```

```
podbench: kubectl -n p47-beamline --request-timeout=25s delete pvc bl47p-ea-fastcs-01-podbench-project exited 1:
Error from server (Forbidden): persistentvolumeclaims "bl47p-ea-fastcs-01-podbench-project" is forbidden:
User "system:serviceaccount:p47-beamline:claude-hgv27681" cannot delete resource "persistentvolumeclaims" ...
(exit 2)
```

06:20:52Z. Reading `_delete_claim` (`hotfix.py:5379`): it checks `claim.
present`, then calls `_pods_mounting` — a namespace-wide `get pods`, exactly
what the manual scan above also found empty — and only issues the `delete` once
that list is empty. **The safety check passed** (this session independently
confirmed zero mounters, above); the delete itself was refused by the
cluster's RBAC, not by podbench's own gate, and not forced past. This is the
same refusal Phase 5 recorded: *"the test service account has no delete on
persistentvolumeclaims by design."* The PVC was re-read immediately after and
is untouched:

```
NAME                                  STATUS   VOLUME                                      CAPACITY   AGE
bl47p-ea-fastcs-01-podbench-project   Bound    pvc-e69a71fe-5d23-45b3-9656-c996b477d842    2Gi        24h
```

Same volume, same age, same `Bound` status as at the start of this session.

Final `retire`, no flags:

```
  [ ]     branch    not measured: nothing mounts the claim, so its manifest cannot be read
  [ ]     image     not measured: nothing here records which image this hotfix was made against
  [x]     wiring    bl47p-ea-fastcs-01-0 carries none of the hotfix wiring: no podbench-app
                     volume, no mount at /podbench/app, no supervisor loop.
  [ ]     claim     bl47p-ea-fastcs-01-podbench-project still exists. ... deleting it is a
                     separate, deliberate act (`... --delete-claim`).
VERDICT: 1 of 4 steps of retirement remain (exit 1)
```

The `wiring` row flipping to `[x]` is the contradiction closed: git and pod
agree again, for the first time since 2026-08-23.

---

## 8. The four-number contract

| # | what | before | after | held |
|---|---|---|---|---|
| 1 | `restartCount`, `bl47p-ea-fastcs-01`, across `apply` | 0 (06:14:41Z) | **0** (06:15:02Z) | **unchanged**, as the contract requires |
| 2 | child pid (`/tmp/podbench-child.pid`) | 6 | **687** | **moved** — the supervisor relaunched the child in place |
| 3 | the edit, in the running process | absent | **`T01-EA-FASTCS-01:HotfixMarker` = `PODBENCH_PHASE7_LIVE`, read via PVA** | live, proven through the app's own interface |
| 4 | every seat, at the end | — | **none** — see below | truthful, not "should be none" |

**#4 in full.** At the moment `apply` ran, two seats were alive:
`podbench-1` (`ghcr.io/gilesknap/podbench:main`, landed unintentionally by
`init`'s auto-land, §4) and `podbench-2` (this branch's own build, the one
every command after §4 used). Both died with the pod they belonged to when
`retire`'s wiring revert replaced it (06:20:22Z) — ephemeral containers cannot
outlive their pod, and a StatefulSet replacement is a new pod. Re-read at
06:21:15Z: `spec.ephemeralContainers: None`,
`status.ephemeralContainerStatuses: None`. **Zero seats alive at the end** is
the correct, verified answer for a fully-retired target, not an omission —
Phase 5 recorded the identical outcome for the identical reason ("seat gone
with the pod").

The exact commands that produced each number are in §5 (`kubectl exec ... cat
/tmp/podbench-child.pid`, the `p4p` `Context.get`, `kubectl get pod ... -o
json` for `restartCount`) and §7 (`kubectl get pod ... ephemeralContainers`).

---

## 9. Sync timings

| sync | pushed | landed | duration | what changed |
|---|---|---|---|---|
| §1, `enabled: false → true` | 06:05:30Z | not independently observable (§1) | — | claim subchart re-enabled; no live object differed, so no resourceVersion moved |
| §7, wiring revert | 06:17:06Z | 06:20:19Z (StatefulSet gen 18→19) | **3m13s** | pod template reverted to pre-hotfix `ioc-instance`; new pod ready 15s after creation |

Both exceed the plan's "wait up to 2 minutes" framing for at least the second
sync; this run waited past it rather than declaring a timeout, since the plan
also says to poll "with a timeout" and the second sync visibly progressed
(never an error, just slow) once past the two-minute mark. Worth flagging for
whoever reads this: **2 minutes is optimistic for this cluster today** — plan
for up to ~3.5 minutes when scripting around this deploy loop.

---

## 10. Every seat landed this session, and why

| seat | image | landed | ended | why |
|---|---|---|---|---|
| `podbench-1` | `ghcr.io/gilesknap/podbench:main` | 06:09:52Z | pod replaced 06:20:22Z | `init`'s auto-land, no `--image` lever (§4 defect) |
| `podbench-2` | `ghcr.io/gilesknap/podbench:0.7.3-beta.1-hotfix-easy-to-drive` | 06:11:10Z | pod replaced 06:20:22Z | landed by hand once §4 was noticed; digest-confirmed against the tag; used for every command from §3 onward |

Neither seat is running now (§8, #4). Neither name (`podbench-1`,
`podbench-2`) can ever be reused on this specific pod instance again — moot,
since the pod itself is gone too.

---

## State left behind

Verified by re-reading, not assumed:

* **`bl47p-ea-fastcs-01-0`** — a pod created 2026-08-24T06:20:22Z, `Running`,
  both containers `Ready`, `restartCount 0/0`. No `ephemeralContainers`. No
  `podbench-app`/`podbench-home` volumes; the six original volumes only. Args
  back to the pre-hotfix entrypoint. `podSecurityContext` back to the chart's
  own `runAsUser/runAsGroup: 36096`. `hotfix status -n p47-beamline` says `no
  hotfixed pods in this namespace` (exit 0). Read at 06:21:15Z.
* **`bl47p-ea-fastcs-01-podbench-project`** (PVC) — `Bound`, 2Gi,
  `pvc-e69a71fe-5d23-45b3-9656-c996b477d842`, unchanged since before this
  session (`creationTimestamp 2026-08-23T05:53:32Z`). Carries the checkout at
  commit `51c3dcc` (the marker edit, on top of the assumed base `c317383`) and
  is mounted by **nothing**. `--delete-claim` was attempted honestly, its
  three-way safety check passed (present, zero mounters — independently
  confirmed), and the delete itself was refused by RBAC
  (`system:serviceaccount:p47-beamline:claude-hgv27681` has no `delete` on
  `persistentvolumeclaims`, by the same design Phase 5 recorded). **This is
  the design working as intended for a test service account, not a
  workaround-needed failure**: deleting this claim needs Giles.
* **`p47-services`, branch `podbench-hotfix-claim`** — `HEAD 51d6440`
  ("Phase 7: retire the live hotfix for real, not just the flag"),
  `git status --short` empty. Two commits landed this session: `1659a4a`
  (re-enable, §1) then `51d6440` (full revert, §7). **Not a PR** — pushed
  directly, per the hard rule.
* **`podbench`, branch `hotfix/easy-to-drive`** — this file is the only
  content change; committed and pushed after this write-up (see the commit
  this evidence file ships in). PR #208 was `OPEN`/`MERGEABLE` against `main`
  before this commit.

The contradiction `phase7-the-contradiction.md` opened with — git says off,
pod says on — is **closed**. Git and the pod now agree: hotfix mode is off,
and the pod carries none of its wiring. One claim remains, `Bound`, holding
the record of what this run did to it, exactly as designed for a hotfix that
has not yet been formally deleted by someone with the rights to do it.

---

## Not measured

* **`check`/`init` agreement in the refusal direction**, live — this target
  never presents a shape `check` fails on. Backed by the unit tests and the
  source reading recorded in the previous (blocked) session, not by a live
  refusal.
* **`status`'s exit 1 on a non-ok row, including a merely-held pod**, live —
  not induced deliberately, to avoid manufacturing a hold on a shared test
  beamline's live application for no operational reason. Backed by
  `HotfixRow.ok`'s doctest and the two `test_status_exits_*` unit tests.
* **`status -A`** — this session's token has no cluster-scope `pods` list
  (`Forbidden`), which is the RBAC working as scoped, not a podbench defect.
* **`consolidate`** — not exercised this session, for the same reason Phase 5
  parked it: pushing to a real fork writes a PAT onto a shared beamline claim.
  Unrelated to anything this run found.
* **VS Code / the two named field failures** (`--new` refusal,
  debugging-does-not-start) — outside the scope this task specified; driving
  VS Code needs Giles present per this project's own convention, and the task
  that produced this file asked for the values→check→init→edit→apply→
  status→retire walk only.
* **`hotfix values`'s exit code and stdout against every unit-tested shape**
  — spot-checked (§2, §3), not exhaustively diffed against
  `tests/test_hotfix.py` line by line.
* **§1's sync duration** — no object's `resourceVersion` moved inside the
  113s this session polled, so there is no landing timestamp to report beyond
  "sometime before the next thing that depended on it read correctly."
