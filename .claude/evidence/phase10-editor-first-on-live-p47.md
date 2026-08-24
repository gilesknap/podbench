# Phase 10 evidence: the editor-first turn, measured on live p47

Measured 2026-08-24 against `p47-beamline/bl47p-ea-fastcs-01-0` on pollux — the
live hotfixed pod, seat `podbench-1`, claim mounted at `/podbench/app`. This is
the pod the plan `.claude/plans/editor-first-and-ordinary-git.md` was written
about, not the replica.

## 1. `consolidate` cannot push from the pod it was built for — confirmed

The plan called this "a likely latent defect, unconfirmed" and asked for
confirmation before citing it as a reason to delete the verb. Confirmed:

```
$ kubectl exec bl47p-ea-fastcs-01-0 -c podbench-1 -- sh -c 'cd /podbench/app && git remote -v && git fetch --dry-run origin'
origin	git@github.com:DiamondLightSource/fastcs-example.git (fetch)
origin	git@github.com:DiamondLightSource/fastcs-example.git (push)
Host key verification failed.
fatal: Could not read from remote repository.
```

The remote is ssh, the seat has no key and no `known_hosts`, and `consolidate`'s
`git push` goes through exactly that remote. So the verb being removed in #232
does not work on the live pod — its failure mode is `Host key verification
failed`, which is #233's territory (host trust), not authentication.

## 2. `vscode` dirties a *tracked* file, not just an untracked one — new

The plan argued slice 1 from staleness: a pid-keyed configuration written at
window-open is stale after the first restart. The live claim shows a second,
sharper reason. `.vscode/launch.json` is **committed in fastcs-example**, so
podbench's write is not an addition to an ignored path — it is a 32-line
modification to a file the user's repository tracks:

```
$ git status --porcelain
 M .gitignore
 M .podbench-hotfix.json
 M .vscode/launch.json

$ git diff --stat
 .gitignore            |  1 +
 .podbench-hotfix.json |  2 +-
 .vscode/launch.json   | 32 ++++++++++++++++++++++++++++++++

$ git ls-files .vscode
.vscode/extensions.json
.vscode/launch.json
.vscode/settings.json
.vscode/tasks.json
```

"A plain run leaves the user's checkout untouched" is therefore not tidiness on
this pod: today a plain run puts a modification into a tracked file that the next
`git commit -am` would sweep up silently.

## 3. Slice 5's local rows, measured on the live claim

Every row the plan calls free is free here, and the interesting one is empty:

```
$ git rev-parse --short HEAD
51c3dcc
$ git branch -r --contains HEAD
                                  # empty — the hotfix is on no remote branch
```

`git branch -r --contains HEAD` returning nothing is the honest answer
`consolidated_branch` cannot give: the commit exists, it is one ahead of an
assumed base, and it has never been pushed. Compare what `hotfix status` says
today from the manifest alone:

```
[ok]  p47-beamline/bl47p-ea-fastcs-01-0  +1 commit(s) from an assumed base  51c3dcc  active — hotfixed, base image unchanged
```

## 4. Where slice 5 can run git — measured, and it is not free

`status` reaches the claim through the **application** container
(`read_pod_state`), not through a seat. On this pod that container has a git:

```
$ kubectl exec … -c bl47p-ea-fastcs-01 -- command -v git
/usr/bin/git
```

But that is a property of `fastcs-example-debug:2025.10.1`, not of application
images in general, and `status` is most useful on a colleague's pod where you may
have no seat at all. So the git-derived rows must degrade to **unmeasured** when
no git is reachable in the claim's container — the same rule the memory-headroom
row already follows — rather than being reported as clean.

## 5. The branch, run against the live pod — what held and what did not

Run 2026-08-24 evening from the merged branch (PR #234), against the same pod.

### `status` measures, and the freshness row really is free

```
  [ok]    p47-beamline/bl47p-ea-fastcs-01-0  active — hotfixed, and nothing here needs attention
    claim   51c3dcc is 1 commit ahead of c317383 — an assumed base, so the count is a guess
    dirty   3 files uncommitted, and they are what is running (.gitignore,
            .podbench-hotfix.json and .vscode/launch.json)
    remote  no branch on the remote is at 51c3dcc, checked just now. That is tips only —
            podbench does not measure whether it has been merged
    image   unchanged since the hotfix was made (sha256:e803e316b14f)
```

Read against the **v2** manifest still on the claim, unmodified. The remote row
was a real `git ls-remote` from the laptop and agrees with the seat's own
`git branch -r --contains HEAD`, which is empty.

### `vscode` writes nothing — confirmed on a claim that was watched

`.vscode/launch.json` was restored to HEAD first, then the verb run. Afterwards
`git status --porcelain` still showed only the two files that were already dirty,
and `.vscode/launch.json`'s mtime was the restore, not the run.

### `hotfix restart` — every invariant held

```
stopped the supervisor child pid 7 and its tree, started pid 2206
the claim is dirty and running: 2 files uncommitted (.gitignore and .podbench-hotfix.json)
```
`restartCount` stayed **0** on both containers and exactly one application tree
was left (2206 → 2212 → 2213 → 2221). The debug refresh failed and said so
without failing the restart — correct behaviour, exercised by a real failure:
this seat is degraded (`CapEff 0000000000000000`) and Yama forbids attaching to a
non-descendant, so `debug-config` can emit nothing here at all.

### Agent forwarding works, and the last barrier was the one the plan named

With the stanza's `ForwardAgent yes` and three `github.com` entries seeded into
the seat's `known_hosts` — extracted from the laptop's own **hashed** 168-line
file by podbench's own matcher, which found them correctly:

```
$ ssh <alias> 'cd /podbench/app && git fetch --dry-run origin'
SSH_AUTH_SOCK=/tmp/ssh-WN68Z62b2K/agent.2572
256 SHA256:/hmXxTNc24y9A… giles.knap@gmail.com (ED25519)
From github.com:DiamondLightSource/fastcs-example
 * [new branch]      dependabot/github_actions/actions-aa222f8dfb -> origin/…
```

`Host key verification failed` — the failure that opens this document — is gone,
and git in the seat reaches the forge. Both halves were needed: seeding alone
left `Permission denied (publickey)`, and forwarding alone leaves host trust.

## 6. Four defects the live run found, none of which a unit test would have

1. **The editor verb still sounded like a debugger.** 15 of the report's 90 lines
   were `debug-config:` prose relayed from the internal assessment — a port
   number, a paste-me injection command, and "also emitting for pid 7", which
   reads as the very #228 symptom the slice removes. Nothing was emitted or
   written; only the notes leaked.
2. **The refresh-failure line quoted kubectl, not the command.** It said
   `command terminated with exit code 2` where `debug-config`'s own last line was
   `no debugger flavour could be emitted for this target`.
3. **`--forward-agent` scanned the wrong directory.** `editor_folder` is gated on
   `session.hotfixed`, which the attach path sets and `ssh-config` does not, so
   the forge scan saw only `/home/podbench` and reported "no ssh git remote found
   in the seat" on a claim whose `origin` is
   `git@github.com:DiamondLightSource/fastcs-example.git`. A false statement of
   fact, on exactly the pod the feature exists for.
4. **A live `ControlMaster` silently defeats `ForwardAgent`.** The first attempt
   returned `SSH_AUTH_SOCK=unset` and `Permission denied (publickey)` with the
   flag on and the stanza correct, because an earlier `podbench vscode` had left
   a master open without forwarding and the new session multiplexed onto it.
   `ssh -O exit` and the identical command worked. `ControlPersist` is in every
   stanza podbench writes, so this is the *normal* path for anyone who adds the
   flag to a pod they have already attached to — and nothing says a word.

## 7. The four defects, fixed and re-measured on the same pod

### The forge scan now finds the claim

```
$ podbench ssh-config bl47p-ea-fastcs-01-0 -n p47-beamline --forward-agent
seeded 3 known_hosts entries in the seat for github.com
```

against the false `no ssh git remote found in the seat` it printed before. The
scan reads the seat's own `volumeMounts` plus its home rather than the editor's
folder, so it no longer depends on `session.hotfixed`, which the attach path sets
and `ssh-config` did not.

### The multiplexing trap announces itself

With a master deliberately opened without forwarding:

```
WARNING  an ssh session for podbench-p47-beamline-bl47p-ea-fastcs-01-0-1 is already open and
         has no agent on it, and a new connection multiplexes onto that master rather than
         reading the stanza just written - so `ForwardAgent yes` reaches nothing, and git in
         the seat stops at `Permission denied (publickey)`.
close it first:  ssh -O exit podbench-p47-beamline-bl47p-ea-fastcs-01-0-1
```

Following that advice works, on this pod, tonight:

```
$ ssh -O exit <alias> && ssh <alias> 'cd /podbench/app && git fetch --dry-run origin'
From github.com:DiamondLightSource/fastcs-example
 * [new branch]      dependabot/github_actions/actions-aa222f8dfb -> origin/…
SOCK=set
```

And with a healthy forwarding master already open, the warning does **not** fire —
podbench asks the master what `SSH_AUTH_SOCK` a session on it gets rather than
assuming from the socket's existence, so a second `--forward-agent` run on a
working seat is silent.

## 8. What this branch did *not* prove on p47

**The debugger provisioning path.** `podbench debug-config --provision` cannot
emit anything on this pod: the seat is degraded (`CapEff 0000000000000000`) and
`/proc/sys/kernel/yama/ptrace_scope` forbids attaching to a non-descendant, so
every candidate is refused before an injection is attempted. Slice 2's write —
that the explicit step authors `launch.json` into the claim's `.vscode/` — is
covered by the unit suite and by the `e2e-dls` job on kind, which passed on this
branch, and not by a run here. The p47 replica on the k3s bench, where 0.9.1
measured the debug path, was unreachable from this machine tonight.
