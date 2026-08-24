# `vscode` is an editor verb, and git is just git

Successor to `vscode-attach-is-slick.md`, which shipped as 0.9.0/0.9.1 and made
the debug path work. This plan is about a turn that conversation forced: **the
verb is named for the editor and behaves like a debugger**, and the workflow most
people actually want — edit the code, restart the process, look again — pays the
entire debugger bill before it starts.

Decided in conversation with Giles, 2026-08-24. Where a decision is his, it says
so; where something is still open, it says that instead, and there are three of
those. They are marked **OPEN** and none of them blocks the shape.

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

Two related defects found the same day, both still unfiled:

- `podbench pids` breaks its own table when a cmdline contains newlines — pid 1
  is the supervisor loop, and the row is truncated on width without flattening
  whitespace first.
- The wrong-subset behaviour above deserves its own issue even though this plan
  removes it, because it is live in 0.9.1 today.

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

**Decided (Giles):** keep `status`; make it truthful.

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

```
claim   3 commits ahead of 603392d
dirty   2 files uncommitted, and they are what is running
remote  <OPEN - see Q3>
image   unchanged since the hotfix was made
```

**Falsified if** `status` reports anything it did not measure, or reports a thing
it could not measure as fine rather than as unmeasured — the repo's standing rule,
and the one the memory-headroom row already follows.

---

## 6 — Real git in the seat: agent forwarding **OPEN**

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
steal — the seat holds no credentials of its own. A forwarded agent adds exactly
one thing: **your personal git identity**, usable by anyone with exec rights for
as long as the session is open. That is a specific, bounded escalation, not "the
seat is insecure", and on a namespace that is your own team it may be entirely
acceptable.

**The host trust half, which agent forwarding does not solve.** The measured
failure is `Host key verification failed` — `known_hosts`, not authentication.
`ssh-over-exec` is explicit that podbench manages `known_hosts` programmatically
rather than teaching `StrictHostKeyChecking no`, and that principle should hold
for the forge too. On p47 `podbench-home` is an `emptyDir`, so anything accepted
interactively dies with the pod.

Note the ssh remote came from what `init` was told, so an https clone would fetch
a public repo with no key at all. **That may make this slice unnecessary for some
users and not others**, which is itself worth settling.

---

## The three open questions

**Q1 — is `ForwardAgent` a flag or a default?** Recommendation: an opt-in flag
whose one-line warning names the git-identity exposure specifically.

**Q2 — where does host trust come from?** Bake the common forges' keys into the
image (they rotate, so this goes stale); seed from the user's own
`~/.ssh/known_hosts` at attach (podbench reading the user's files, which it does
not do today); or leave it and accept once per pod. Recommendation: seed from the
user's own, **and only when the forwarding flag is on** — the trust decision is
already made on their laptop, and it neither goes stale nor weakens verification.

**Q3 — what does `status` say on the remote axis?** Report the tracking refs and
state when it was last true and why it cannot refresh; or have `init` clone over
https so it can; or drop the axis. Recommendation: the first, unless slice 6
lands, in which case a real fetch is available and the question dissolves.

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
- **Retiring `hotfix init`.** It clones, seeds the claim from the running
  container and builds the venv. None of that is ordinary git and it stays.
