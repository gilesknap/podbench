# `vscode` is an editor verb, and git is just git

Successor to `vscode-attach-is-slick.md`, which shipped as 0.9.0/0.9.1 and made
the debug path work. This plan is about a turn that conversation forced: **the
verb is named for the editor and behaves like a debugger**, and the workflow most
people actually want — edit the code, restart the process, look again — pays the
entire debugger bill before it starts.

Decided in conversation with Giles, 2026-08-24, over two sittings. The three
questions the first sitting left **OPEN** were all settled in the second, and the
plan records how rather than only what. Nothing here is now waiting on a
decision.

Read `ssh-over-exec`, `ephemeral-containers`, `terminal-reports` and
`vscode-in-a-seat` before touching the code any slice names. The evidence for the
measurements quoted here is `.claude/evidence/phase9-vscode-is-slick-on-a-p47-replica.md`
and, where it says *measured today*, a seat on that replica or on the live p47
pod.

---

## The defect that started it

On the live p47 pod, `podbench vscode` emitted debug configurations for
`stdio-socket` and `pptty` — and **none for `fastcs-example`**, the process
anyone would want. Exactly the wrong subset.

The mechanism is one line. `vscode.py:1874` passes `provision=provision and
primary`, so:

- the **primary** candidate is the only one provisioned. Its injection or
  handshake failed, so 0.9.0's new gate withheld the entry — correctly, by
  #218's rule: never author a port nothing answers;
- the **non-primary** candidates are never provisioned, so the gate never
  applies to them. They are emitted unprobed, each naming a freshly chosen port
  nothing has ever listened on.

The gate and the pre-existing non-primary behaviour compose into something worse
than either alone. **This plan deletes the interaction rather than patching it**,
because slice 1 stops emitting anything at window-open.

Filed as **#228**, because it is live in 0.9.1 and this plan is not written yet.
A related defect found the same day is **#229**: `podbench pids` breaks its own
table when a cmdline contains newlines — pid 1 *is* the supervisor loop, so every
hotfixed pod shows it.

---

## The shape

> **Podbench is doing two jobs in one verb, and charging everyone for both.**
> Editing needs a seat, a mounted claim and a window. Debugging needs ptrace, a
> 15 MB install, a one-shot injection and a port. The second is currently paid
> at window-open, by everyone, on a guessed pid.

The turn is to make the expensive half **opt-in and late**, and to stop wrapping
things that are now ordinary.

| today | after |
|---|---|
| `vscode` provisions debugpy and writes `launch.json` at window-open | `vscode` opens a window and writes **nothing** |
| debugging is automatic and guessed | debugging is a step you run, on a pid you name |
| `hotfix apply` = git commit + relaunch, `-m` required | `hotfix restart` relaunches; `git commit` is git's job |
| `hotfix consolidate` = git push + a manifest field | ordinary `git push` |
| `status` reads records that hand-git invalidates | `status` measures |

The second half is only possible because of what 0.9.0 shipped: **slice 4 gave
the user a working git inside the seat.** Before it, `git` in the claim died with
`fatal: detected dubious ownership`, which is *why* podbench wrapped the commit
at all. That reason is gone, so the wrappers can go with it.

---

## 1 — `podbench vscode` writes nothing

**Decided (Giles).** A plain run leaves the user's checkout untouched. This
completes D1b from the last plan, which already stopped `settings.json` and
`extensions.json`; `launch.json` was the remaining write and it now happens only
on request.

The property that makes this more than tidiness: **restart changes the pid**, and
every configuration podbench authors is pid-named and pid-keyed. Measured across
restarts on the replica, `fastcs-example` was pid 12, then 2446, then 13. In a
world where restarting is the normal inner loop, anything written at window-open
is stale almost immediately — which is #224 arriving through the *common* path
rather than a rare one. Writing nothing cannot go stale.

The report must name the debug step, per `terminal-reports`' rules on offers:

```
  [ok] opening /podbench/app, not the seat's home
  [ok] to debug:  podbench debug-config --provision
```

**Cost, stated plainly**: "one command, a window, F5" becomes two commands for
anyone who wants a debugger. That was the previous plan's own standard and this
knowingly gives it up, on the grounds that most runs never debug.

**Falsified if** a plain `podbench vscode` against a hotfixed pod leaves
`git status` on the claim showing anything at all, or if the debug step is not
discoverable from what the run printed.

**Test**: the `vscode` path writes no file under the claim's `.vscode/`; the
report names the debug command; the existing "exactly one file and it is
launch.json" test moves to the debug step.

---

## 2 — Debugging is a step, on a pid you name

**Decided (Giles).** `podbench debug-config <pid> --provision` **already exists**
and already takes an explicit pid. This slice is mostly about defaults and
discoverability, not new machinery.

- It writes `launch.json` into the claim's `.vscode/` when run — **decided**:
  that is where every VS Code user expects it, and the principle stays clean,
  *podbench writes only what you asked for, when you asked for it*.
- Selection is the user's. With no pid it keeps today's primary-candidate
  behaviour; `podbench pids` is how you choose another.
- The non-primary emission goes away with slice 1, so the dead-port entries stop
  being authored at all.

**A simplification worth recording.** An earlier draft had F5 itself trigger the
injection, via `{"request": "attach", "processId": N}` and the Python extension's
own attach machinery. That needed a measurement nobody has taken — whether that
works when debugpy lives at a non-standard path in another mount namespace,
which is `gdb-across-namespaces` wearing new clothes. **Choosing the explicit
step removed the need for it.** If the two-command cost ever becomes intolerable,
that measurement is the way back, and it is listed under out of scope.

**Falsified if** the emitted configuration names a port that fails a DAP
`initialize`, or if a run that emitted nothing gave no reason.

---

## 3 — `hotfix restart`: relaunch without a commit

**Decided (Giles):** a new verb, seat-first, distinct from `apply`.

The measured friction: `hotfix apply` **requires `-m <message>`**. There is no
relaunch-without-commit anywhere in the CLI, so the inner loop forces a commit
per iteration. That is right for a *hotfix* and wrong for an *inner loop*, where
you restart twenty times and commit once when it works.

```
$ podbench hotfix restart          # inner loop, no message
  stopped pid 13, started pid 2446
  the claim is dirty and running: 2 files uncommitted

$ git commit -am "fix the ramp"    # ordinary git, works since 0.9.0
```

**Two things it must do.**

- **Say the claim is dirty and running.** `hotfix status` exists so a hotfixed
  pod cannot go unnoticed, and an *uncommitted* change on a live process is more
  important to surface than a committed one, not less. This is the constraint
  that makes restart-without-commit sound rather than a hole in the model.
- **Refresh a live debug configuration against the new pid** (decided). If the
  user provisioned a debugger, restart re-provisions so F5 keeps working across
  the loop. Cheap — debugpy's files are already installed, and a *fresh* process
  can be served again, so the one-shot problem does not arise. It does mean
  restart ptraces without an explicit ask at that moment; the justification is
  that the user asked to debug and has not retracted it.

**Mechanism**: the supervisor already supports this. `/tmp/podbench-hold` plus a
`kill` of the recorded child is what `apply` does internally; measured by hand on
the replica, `restartCount` stayed 0 on both containers. **Do not reproduce the
hand version** — killing the recorded child directly orphaned the old tree and
left two `fastcs-example` instances running, which is how that measurement was
obtained and is not what the verb should do.

**Falsified if** a restart bumps `restartCount` on any application container, if
it leaves two app trees running, or if `status` afterwards does not say the claim
is dirty.

---

## 4 — Remove `apply` and `consolidate`

**Decided (Giles).** Both wrap git, and neither needs to.

**`apply`** is `git commit` + relaunch, and nothing else. Its git half is
ordinary git now that #217 is fixed; its relaunch half becomes slice 3. Its
manifest bookkeeping (`ahead`, `commits`) is derivable from local git against
`base_commit`, which slice 5 does.

**`consolidate`** is `git push origin HEAD:refs/heads/<branch>` plus one manifest
field, `consolidated_branch`, plus a printed retirement checklist. It handles no
credentials, so pushing from the seat is exactly as hard with it as without it.

**A likely latent defect, unconfirmed, that bears on this.** Measured today in a
seat on the replica:

```
$ git fetch --dry-run origin
Host key verification failed.
```

The claim's remote is `git@github.com:…` — ssh — and the seat has no key and no
`known_hosts`. `consolidate` pushes through that same remote. If that holds, the
verb being deleted **does not work on the pod it was built for**. Confirm before
citing this as a reason; it is suggestive, not proven.

`hotfix` goes from seven verbs to six: `values`, `check`, `init`, `restart`,
`status`, `retire`.

**Falsified if** anything `apply` or `consolidate` did turns out not to be
reachable with ordinary git plus slice 3 — in which case that capability, not the
verb, is what needs re-homing.

---

## 5 — `status` measures instead of recording

**Decided (Giles):** keep `status`; make it truthful. Dropping the verb outright
was considered and rejected — it does **two** jobs, and only one of them is the
repo-state recording this plan objects to. The other is cluster state that
nothing else can answer: *which pods in this namespace are hotfixed at all*
(a colleague's hotfix is otherwise invisible to anyone who was not there), and
*whether the image moved under the mount*, from `base_image_digest` against the
live `imageID`. Giles: "not knowing what has happened upstream is not a huge
hole" — so the remote row is best-effort and the rest is not.

The rule that forces this: **"let users use normal git" and "a manifest that
records what git did" are incompatible.** A hand-push makes a recorded field a
lie, and telling people to use normal git is an invitation to hand-push.

Measured today, in a seat, with no network:

```
$ git branch -r --contains HEAD
  origin/main
$ git status --porcelain
 M .vscode/launch.json
?? .podbench-hotfix.json
```

`hotfix init` **clones**, so the claim carries a real `origin` and a full set of
remote-tracking refs. Local git therefore already answers all three questions —
dirty, ahead-of-base, and *is this commit on a known remote branch* — more
truthfully than `consolidated_branch` can, because a push updates the tracking
ref and the manifest field would not.

The manifest keeps only what git cannot know: `repo`, `base_commit` (and
`base_commit_assumed`), `base_image_digest`, `interpreter`, `claim_venv`.

**The most valuable row involves no git at all**: `base_image_digest` against the
live container's `imageID` says the image moved under your mount — i.e. the claim
may now be shadowing a fix that is already released.

**Every row a day-to-day reader looks at is free.** Measured in a seat this
afternoon with the network demonstrably broken — the `git fetch` in the same
session died on host verification — these are reads of local objects and refs and
need no credential, no agent and no egress:

| row | needs | availability |
|---|---|---|
| dirty, and which files | nothing | always |
| commits ahead of `base_commit` | nothing | always |
| on a remote branch *as of the clone* | nothing | always |
| **is that still true now** | network + auth | best-effort, **from the laptop** |

**The freshness row is done on the laptop, not in the seat** (decided). `status`
reaches the claim through `kubectl exec`, and **an exec session has no
`SSH_AUTH_SOCK`** — agent forwarding exists only inside an *ssh* session — so even
after slice 6 lands, a `status` run from the laptop could not use a forwarded
agent. Routing it through ssh instead would need a seat that authorises *you*,
which fails on precisely the pods `status` is most useful for (a colleague's
hotfix), would need an ssh stanza that exists only for seats you attached to, and
would depend on pod egress to the forge that a beamline NetworkPolicy may not
permit. The laptop already holds both halves — credentials *and* connectivity —
so it reads the claim's shas out over exec and runs `git ls-remote` locally.

That is the same shape as the `consolidate` insight: **do the network half where
the credentials already are.**

Two constraints on that row: it is time-bounded, because a status verb that hangs
on a network call is worse than one that says less; and a failed query reports
**unmeasured**, never "not pushed". Also `ls-remote` returns ref *tips*, not
ancestry — so it can say "your commit is the tip of a remote branch" and cannot
cheaply say "it has been merged". The wording must not imply the second.

```
claim   3 commits ahead of 603392d
dirty   2 files uncommitted, and they are what is running
remote  HEAD is the tip of origin/hotfix-ramp (checked just now)
image   unchanged since the hotfix was made
```

**Falsified if** `status` reports anything it did not measure, or reports a thing
it could not measure as fine rather than as unmeasured — the repo's standing rule,
and the one the memory-headroom row already follows.

---

## 6 — Real git in the seat: agent forwarding

The seat cannot reach a forge. Making it able to is what would let "just do git
yourself" be true rather than aspirational, and it is **two changes, not one**.

**The key.** Measured in the source today: podbench writes neither `ForwardAgent`
(client) nor `AllowAgentForwarding` (sshd). OpenSSH's defaults do the rest —
client `ForwardAgent` defaults to **no**, server `AllowAgentForwarding` defaults
to **yes**. So the seat is already willing and the stanza podbench generates
never asks. One line in `sshcfg.py`, and VS Code picks it up because Remote-SSH
uses that stanza.

**The exposure, stated exactly.** `authorized_keys` gates ssh; it does **not**
gate `kubectl exec`. Anyone with `pods/exec` in the namespace can enter the seat,
and podbench's own report advertises that path. Today there is nothing there to
steal — the seat holds no credentials of its own.

A forwarded agent adds one thing, and it is **wider than "your git identity"**,
which an earlier draft of this plan said and got wrong: an agent forwards *keys*,
not a destination, so whoever reaches the socket can authenticate as you to **any
host that trusts your key** — jump boxes, other beamline machines, other orgs.
Bounded in time to the session, unbounded in reach within it.

**Why it is still the least-bad credential to put in a seat** (Giles,
2026-08-24, and this is the argument that decides it): nothing is written to
disk, and `SSH_AUTH_SOCK` is set only in the *ssh session's* environment, so a
colleague's `kubectl exec` session does not have it and cannot stumble into it.
Deliberate use is easy; accidental use essentially does not happen. A cached `gh`
token or `.git-credentials` on a shared path is the opposite on every count —
persistent, copyable, silently reused, and still working next week. **The private
key never enters the pod**, so the exposure ends with the session rather than
leaving something behind.

**Two mitigations to document rather than build**, both user-side:

- `ssh-add -c` makes the agent prompt locally on every use, so silent use becomes
  impossible.
- Destination-constrained keys (OpenSSH 8.9+, `ssh-add -h`) bind the key to named
  destinations, which closes the "unbounded in reach" half above.

**One caution before enabling it**: RBAC groups at a facility often include
service accounts and CI identities alongside humans, so "who can exec here" may
be a larger set than "my colleagues". Read the rolebinding rather than assuming.

**The host trust half, which agent forwarding does not solve.** The measured
failure is `Host key verification failed` — `known_hosts`, not authentication.
`ssh-over-exec` is explicit that podbench manages `known_hosts` programmatically
rather than teaching `StrictHostKeyChecking no`, and that principle should hold
for the forge too. On p47 `podbench-home` is an `emptyDir`, so anything accepted
interactively dies with the pod.

Note the ssh remote came from what `init` was told, so an https clone would fetch
a public repo with no key at all. **That may make this slice unnecessary for some
users and not others**, which is itself worth settling.

**Forward only the git keys** (decided). An agent forwards *an agent*, not
individual keys, so the granularity comes from pointing at a different agent:

```
ssh-agent -a /run/user/1000/git-agent.sock
SSH_AUTH_SOCK=/run/user/1000/git-agent.sock ssh-add ~/.ssh/id_git
```

podbench launches `code`, so it controls that child's environment: the flag takes
an optional socket path and sets `SSH_AUTH_SOCK` for that invocation, and only
those keys ever reach the pod. No OpenSSH version floor, and it composes with
`ssh-add -c`. Giles' reasoning: most people who follow the usual advice already
keep a separate key for git, so this is practical rather than theoretical — and
it collapses the "unbounded in reach" objection above to one repository host.

**Host trust: seed the seat's `known_hosts` from the user's own** (decided —
this was Q2). The trust decision has already been made on their laptop; copying
the relevant entries in neither goes stale like baked-in forge keys nor weakens
verification like accept-on-first-use, which `ssh-over-exec` explicitly refuses
to teach. Done **only when the forwarding flag is on** — a seat that cannot
authenticate has no use for host keys.

**Context worth recording** (Giles): the established route into these pods is
`kubectl exec` anyway, so everyone with exec is already inside. podbench's ssh
layer is *adding* a boundary here, not removing one — which is the right frame
for judging what this flag costs.

---

## 6b — declined: pushing from the laptop instead



Raised by Giles, 2026-08-24, and it may be a better answer than slice 6 entirely.

**How `consolidate` worked, for the record.** It never brought anything to the
laptop. `HotfixStore`'s own docstring says the claim is only ever mounted in the
cluster, so the default `PodStore` sends *every* read, write and git invocation
through `kubectl exec` into the seat. `consolidate` therefore read the manifest
in the seat, computed `drift_commits` in the seat, and ran `git push origin
HEAD:refs/heads/<branch>` **in the seat** — a remote-control button for a push
executed by a container with no credentials, no key and no `known_hosts`. Only
the manifest write and the printed checklist happened locally.

**The alternative**: bring the commits *out* and push them from the laptop, where
the user's credentials already work.

- `git bundle create` in the seat, copy the bundle out over exec, `git fetch` it
  into a local clone, push from there; or
- use `kubectl exec` as a git **transport**, so the laptop fetches directly from
  the claim's checkout — the same trick the ssh transport already plays, and the
  more idiomatic of the two here.

**Why it may beat slice 6.** The seat never needs a credential at all. That
removes Q1 and Q2 outright — no forwarded agent, so no lending of a git identity
to anyone holding `pods/exec`; no host-trust problem, because the seat never
talks to a forge. And it makes Q3 answerable, because the *laptop* can fetch.

**Declined (Giles, 2026-08-24), and the reason is slice 4.** This existed to give
podbench's *tooling* a way to push. With `consolidate` gone, nothing in podbench
pushes at all, so it has no caller. The only remaining pusher is the human in the
window, and a laptop-side mechanism does nothing for them — a `git push` typed
into the seat's terminal still fails without slice 6.

Recorded rather than deleted because the underlying insight is reused: slice 5's
freshness row does exactly this, doing the network half on the laptop where the
credentials already are. If podbench ever needs to push again, this is the shape.

---

## The three questions, and how they were settled

All three were left open by the first sitting and answered in the second.

**Q1 — is `ForwardAgent` a flag or a default? → an opt-in flag**, and what decided
it was Giles' observation about what *else* could be put in a seat. A forwarded
agent writes nothing to disk and sets `SSH_AUTH_SOCK` only in the ssh session's
own environment, so a colleague's exec session cannot stumble into it: deliberate
use is easy, accidental use essentially does not happen. A cached `gh` token or
`.git-credentials` on a shared path is the opposite on every count — persistent,
copyable, silently reused, and still working next week. It is a flag rather than a
default because the exposure is real; it is agent forwarding rather than any other
credential because it is the only one that leaves nothing behind.

**Q2 — where does host trust come from? → seeded from the user's own
`known_hosts`**, and only when the flag is on. See slice 6.

**Q3 — what does `status` say on the remote axis? → dissolved.** Once `status`
stops recording and starts measuring, there is no stale field to be honest about:
the local rows are free and always available, and the freshness row is a
best-effort `ls-remote` from the laptop that says **unmeasured** when it cannot
run. Giles: not knowing what has happened upstream is not a huge hole.

---

## Multi-user, and what it is waiting on

Recorded so a future reader does not rediscover it as a surprise.

Seats are **per-user by construction**: `authorized_keys` is written when the
container is created and an ephemeral container's spec is immutable, so a running
seat can never gain a second key. Two mechanisms already cope, and they compose
by accident rather than design:

- `reconnect_seat` skips a seat whose recorded **owner** is somebody else
  (#113), so a reconnect finds *your* seat rather than the first one;
- for seats landed before that stamp existed, which carry no owner and so are
  still candidates, slice 5 of the last plan reads `authorized_keys` and refuses
  naming `--new` (#204).

**The cost is real**: every colleague who cannot reuse a seat spends an
ephemeral-container name, permanently, for the pod's lifetime. On a long-lived
beamline StatefulSet that accumulates.

**The durable fix is #70** — *seat as a declared sidecar: a warm home, a stable
ssh identity, and the identity ConfigMap finally used*. A declared sidecar
reading keys from a ConfigMap can authorise many people and be updated **without
landing a new container**, which is the thing ephemeral containers structurally
cannot do. Until then, per-user seats are the workaround, not the design.

---

## Ordering

1. **Slices 1 and 2 together.** Splitting them ships a release where `vscode`
   writes nothing and nothing tells you how to debug.
2. **Slice 3 next.** It is what makes the editor-first workflow usable at all,
   and slice 1's report should already name it.
3. **Slices 4 and 5 together.** Removing the verbs without making `status`
   measure leaves it reading fields nothing maintains.
4. **Slice 6 last, and separable.** It is the only one that touches the
   transport, the only one with an unresolved security question, and the only
   one that could be dropped entirely without the rest making less sense.

---

## Deliberately not in scope

- **#70, the declared sidecar.** The real multi-user answer; a different shape of
  work, and this plan only records that it is what the story waits on.
- **`processId` attach.** Removed from the design by the choice in slice 2, not
  refuted. If two commands ever becomes intolerable, measure it then.
- **#221, the reload before F5 works.** Unchanged and still out for the reasons
  the last plan gave.
- **#225, the intermittent gdb `DoAttach` failure.** Real and still unexplained;
  0.9.1 contained its consequence rather than fixing it. Slice 2 makes it less
  visible — nothing is provisioned unless asked — which is a reason to keep the
  issue open, not to close it.
- **#223 and #224.** Both are decisions this plan touches without settling:
  slice 1 removes #224's mechanism at window-open, and #223's
  `.podbench-hotfix.json` question is untouched.
- **#228 is fixed by slice 1 as a side effect**, not as its own change — nothing
  is authored at window-open, so there are no non-primary entries to be wrong.
  The issue stays open until the slice lands, because it is live in 0.9.1.
- **#229, the `pids` table.** A one-line whitespace fix, unrelated to this plan's
  spine, and not worth folding into a PR about the shape of the verbs.
- **Retiring `hotfix init`.** It clones, seeds the claim from the running
  container and builds the venv. None of that is ordinary git and it stays.
