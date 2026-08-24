# Phase 7 — the live walk (blocked before it started)

2026-08-24, `p47-beamline` on pollux, through
`k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig`. Launcher under test:
`/home/giles/code/podbench/.venv/bin/podbench`, version
**0.7.3.dev10+gdd089982e.d20260823**, branch `hotfix/easy-to-drive` at `6d5d812`
("Stop retire counting steps a finished retirement cannot close" — the last of
the read-only run's six fixes, already reviewed and pushed).

**This file records a blocker, not a walk.** The mutating half of Phase 7 —
`hotfix values` → deploy via git → `hotfix check` → `hotfix init` → edit →
`hotfix apply` → observe → `hotfix status` → `hotfix retire` — was **not
performed**. Every attempt to reach the cluster, from the first command of this
session, failed with the API server refusing the credential. See "What blocked
this" below. Cross-reference `.claude/evidence/phase7-the-contradiction.md` for
the settled baseline this run was supposed to build on (the git/pod
contradiction, and the byte-for-byte read-only transcripts) — nothing here
supersedes it, because nothing here reached the cluster at all.

---

## What blocked this

The kubeconfig's bound ServiceAccount token expired **before this session's
first cluster command ran**, and nothing available to this session can mint a
new one.

Decoded from `k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig`
(`users[0].user.token`, a JWT — `iat`/`exp` read directly from its payload,
no cluster call needed for this part):

| | |
|---|---|
| issued (`iat`) | **2026-08-23 05:21:11 UTC** |
| expired (`exp`) | **2026-08-24 05:21:11 UTC** — a 24h TTL |
| this session's one successful cluster read | ~2026-08-24 05:20–05:22 UTC, straddling `exp` — see below |
| this session's first *refused* cluster command | 2026-08-24 05:23:18 UTC |

(`kubectl`'s own error-log timestamps are in the host's local timezone,
UTC+1 — `E0824 06:23:18` in a transcript below is `05:23:18 UTC`. Every
other timestamp in this file is UTC.)

Every one of the four other kubeconfigs under `k8s/` is a stale copy with an
earlier `exp` (2026-08-22, from an older provisioning run); this was the
freshest credential available and it had already turned over.

**Confirmed repeatedly, not assumed once:**

```
$ export KUBECONFIG=k8s/p47-beamline-claude-hgv27681-tunnel.kubeconfig
$ kubectl get pod bl47p-ea-fastcs-01-0 -n p47-beamline
E... "the server has asked for the client to provide credentials"
error: You must be logged in to the server (Unauthorized)
```

Reproduced at 05:23:19, 05:23:24, 05:23:56, 05:24:24, 05:25:04, 05:27:06,
05:27:32 and 05:27:45 UTC — eight tries spread over five minutes, byte-for-byte
the same refusal every time, including `kubectl auth can-i get pods -n
p47-beamline`, which failed identically before ever reaching the "yes/no"
answer.

**The tunnel itself is up and not the cause.** `ps aux` shows the ssh tunnel
process live since 06:02 local (`ssh -fNT -M ... -L
127.0.0.1:6443:api.pollux.diamond.ac.uk:6443 hgv27681@pc0116`), and `ss -ltn`
shows `127.0.0.1:6443` listening. The TCP path to the API server is open; the
API server is answering; it is answering **Unauthorized**, which is a
server-side verdict on the token's `exp` claim (Kubernetes `TokenReview`
enforces `exp` itself — no clock skew on this end explains a token that was
still 22+ hours from expiry at `iat` and is now checked 6+ minutes after its
own stated `exp`).

**No remedy is available within this session's authority.** The token is
minted by `k8s/make-claude-sa.sh`, whose own header says: *"Run it with YOUR
OWN admin credential: it reads your current context for the API server
address and CA, and needs create rights on serviceaccounts, roles and
rolebindings"* — i.e. it needs Giles' own admin kubeconfig context, not the
namespace-scoped token this session was handed. This session has no admin
credential for pollux, was given none, and was not authorized to go looking
for one; doing so would be an unauthorized privilege escalation outside what
was granted for this task, not a workaround this task's rules permit. Per the
task's own instruction — *"If something fails for reasons that are nobody's
fault ... say so plainly, record how far you got, put the beamline back in a
safe/defined state if you can, and stop rather than working around it"* — that
is what this run does.

A `Monitor` poll (15s interval, 20 attempts, ~5 minutes) was run in case an
external process was mid-rotation of the credential file; it was not — all 20
polls answered `Unauthorized` identically, and the file's own `iat`/`exp` say
why: nothing rotates a bound-token kubeconfig on its own, by design.

---

## How far this got

Before discovering the credential was dead, this session did the following —
none of it touched the cluster, `p47-services`, or the podbench branch:

* Read `.claude/evidence/phase7-the-contradiction.md` in full (the settled
  baseline).
* Read the plan's "environment", "hard rules", "what must not change" and
  "Phase 7" sections.
* Read `.claude/evidence/phase5-the-workflow-on-p47.md` for the evidence-file
  style and the four-number contract's shape.
* Loaded the `ephemeral-containers` skill.
* Confirmed the branch (`6d5d812`) and PR #208 (`OPEN`, `MERGEABLE`) are as the
  read-only run left them, and that both `podbench` and `p47-services`
  working trees are clean.
* Read the relevant `hotfix.py` sources ahead of the walk, so the actual run —
  once credentials exist — needs no further reading:
  * `init` (`hotfix.py:2528`) — confirmed idempotent on an already-seeded
    claim: a second `init` appends `"claim already seeded at {checkout}"` and
    `"checkout already present at {checkout}"` rather than refusing, which
    matters because this target's claim is already seeded (per the
    contradiction file's `status` transcript: `+0 commit(s)`, base `3d55455`).
  * `_source_check` / `image_name_agrees` (`hotfix.py:4363`, `4370`) —
    confirmed the read-only run's defect (§7.1 of the contradiction file, an
    inherited `ubuntu-devcontainer` label read back as `[ok]`) is fixed on
    this branch: the docstring now names the exact four-state contract
    (`--repo` given → OK; no label, no flag → FAIL; label the image's own name
    corroborates → OK; label nothing corroborates → WARN) and cites this
    target's own measurement as the motivating case. On this image
    (`fastcs-example-debug` naming `ubuntu-devcontainer`), `image_name_agrees`
    should return `False` — the suffix tolerance only extends in the image's
    own name's direction, and `ubuntu-devcontainer` does not extend
    `fastcs-example-debug` — so `check`'s `source` row should now read `WARN`,
    not the prior run's incorrect `OK`. **Not verified against the live target
    — the API server was unreachable before this could be run for real.**
  * `_delete_claim` (`hotfix.py:5379`) — confirmed the three-way safety check
    `retire --delete-claim` performs: `claim.present`, then who mounts it
    (`_pods_mounting`, a namespace-wide `get pods`, deliberately not
    target-scoped so a second replica of a rollout is not missed), and only
    deletes when that list is empty. On this target, which still mounts the
    claim, `--delete-claim` would refuse with `CLAIM_STILL_MOUNTED` — so it
    was never going to be reachable this session regardless of the credential.
  * `apply_hotfix` (`hotfix.py:2719`) and `consolidate` (`hotfix.py:2942`) —
    read for the commit/relaunch and push mechanics; `consolidate` needs a
    real fork push and was parked for the same reason Phase 5 parked it (a PAT
    on a shared beamline claim), so it was not going to be exercised this
    session either way.
  * `HotfixRow.ok` / `format_status` (`hotfix.py:1900`, `2048`) — confirmed the
    exit-code contract (`ok = health.ok and hold is None`) ahead of reading
    `status`'s real exit code.

None of this required cluster access and none of it is a substitute for it —
it is what let this session confirm, from the source, that the six defects
found by the read-only run are fixed, and prepare to move fast once (if) a
credential exists. It is not the walk.

---

## The four-number contract

**Not established. Falsifies the task as stated**, honestly:

1. `restartCount` before/after — **not measured**. The only number in hand is
   this session's own single working query, run once before the token's
   expiry was discovered (and possibly right at the boundary of it — see the
   timestamp note above, this read cannot be pinned to a second): `kubectl get
   pod bl47p-ea-fastcs-01-0 -n p47-beamline -o json`, parsed for
   `status.containerStatuses[].restartCount`, returned `bl47p-ea-fastcs-01`
   **0** (unchanged since pod creation, 2026-08-23T17:31:54Z) and the sidecar
   `temp-controller-simulator` **1**, running since 2026-08-24T02:18:56Z — a
   restart roughly three hours before this session started, nowhere near
   anything this session did (this session never got far enough to do
   anything to the pod). That is the **before** number for the target
   container, consistent with the contradiction file's own baseline; there is
   no **after**, because `apply` never ran. The task brief's note that
   `bl47p-ea-fastcs-01-0`'s `restartCount` stood at 1 going into this session
   is not reproduced by this reading — this session's own read found the
   *target* container's `restartCount` at 0 and the *sidecar's* at 1, and
   records both rather than picking one, since the note did not say which
   container it meant.
2. Recorded child pid moved — **not measured**. `apply` never ran.
3. The edit live in the running process — **not measured**. No edit was made.
4. Every seat alive at the end — **not measured**. No seat was landed.

---

## State left behind

**Unchanged, and verified as unchanged rather than assumed:**

* `bl47p-ea-fastcs-01-0` — last successfully read approximately
  2026-08-24T05:22 UTC:
  `bl47p-ea-fastcs-01` `restartCount` 0, `temp-controller-simulator`
  `restartCount` 1 (from before this session), `resourceVersion 1054230919`.
  Nothing after that read succeeded, so nothing after that read is claimed.
* `p47-services`, branch `podbench-hotfix-claim` — `git status --short` empty,
  HEAD still `94b74d2` ("turn off hotfix mode"), the same commit the
  contradiction file describes. **Not pushed to.**
* `podbench`, branch `hotfix/easy-to-drive` — `git status --short` empty, HEAD
  `6d5d812`, matching `origin/hotfix/easy-to-drive` (fetched and compared this
  session). **Not pushed to** — this file is the only new content, and this
  commit is the first one this session makes.
* No seat was landed, no values file was written to the cluster, no PVC was
  touched, `--delete-claim` was never reached. The contradiction the previous
  run found (git says `enabled: false`, the pod is still hotfix-wired) is
  **still open** — this session neither resolved it nor made it worse.

---

## Not measured

Everything the task asked for, because the credential was already dead when
this session's first cluster command ran:

* `hotfix values --from-pod` output and its stdout purity.
* The git-driven deploy loop and its sync duration.
* `hotfix check` and `hotfix init` agreeing in both directions, for real,
  against this target.
* Whether `init` now handles the inherited-label image correctly (refuses,
  warns, or records an assumed base) — the source-reading says it should WARN
  and record an assumed base with `corroborated=False`; **this was read from
  the source, not observed**.
* A real, observable edit to the running application.
* `hotfix apply`'s relaunch, and the four-number contract in full (see above).
* `hotfix status` after `apply`, and its exit code.
* `hotfix retire`, with or without `--delete-claim`.
* Sync timings for any values change — there were none to time.
* Whether `hotfix values`'s emitted values or any verb's exit code contradicts
  the unit tests, against a live target — only readable from the source this
  session (see "How far this got"), not from a live run.

---

## What would unblock this

A new bound-token kubeconfig for `p47-beamline`, minted with Giles' own admin
context via `k8s/make-claude-sa.sh p47-beamline --podbench=hotfix` (re-running
it is documented as safe and is "how you refresh an expired token" per the
script's own header) — or any other credential Giles chooses to hand this
session. Nothing else in this session's toolset can produce one.
