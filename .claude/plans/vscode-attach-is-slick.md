# `podbench vscode` is slick

Successor to `vscode-actually-debugs.md`, which ended by *proving* that a
breakpoint in a beamline application binds and is hit from a window podbench
opened. It works. This plan is about what a user still has to notice, tolerate
or repair on the way there: five defects, and the run that proves them gone.

The measurements are in `.claude/evidence/phase8-vscode-actually-debugs-on-p47.md`
(the live run, 2026-08-24, `bl47p-ea-fastcs-01-0` on pollux) and
`.claude/evidence/phase8-why-the-adapter-never-answers.md` (the DAP diagnosis
that preceded it); where this plan asserts a cause, one of those two or a line
of source is the citation. The target is left hotfix-wired on p47 deliberately.
Read [[pollux-p47-access]] for the token, and the `vscode-in-a-seat`,
`terminal-reports`, `ephemeral-containers` and `gdb-across-namespaces` skills
before touching the code each slice names.

**Definition of slick, from the ask**: one command, a window, F5. Nothing along
the way makes the user stop, re-run, read 300 words, hand-fix a file, or wonder
whether something went wrong.

---

## The shape

There is a spine, and it is not the last plan's.

> **Every rough edge below is podbench measuring something and then not using
> the measurement.** In each case the fact already exists inside the process —
> in two of them it is *printed one line earlier* — and the code one layer down
> re-derives it, guesses it, or drops it.

| # | podbench knows | and then |
|---|---|---|
| 1 | it just resized the pod, and that the 409 it lost is its own resize's writeback | reports a bare Kubernetes conflict, retries nothing, and never mentions the mutation |
| 2 | the DAP handshake refused, and the live adapter is a child of the target it just enumerated | authors a `launch.json` naming a port nothing serves |
| 3 | the target's interpreter — it prints it while emitting the debugpy configuration | writes no `python.defaultInterpreterPath`, so the window pops "no Python found" — while writing two files it does not need into the user's committed `.vscode/` |
| 4 | the claim is a git checkout at a uid the seat does not own (`hotfix.git_argv` was written for exactly this), and podbench put 15 MB of debugpy inside it | leaves the user's first `git status` fatal, and their SCM pane full |
| 5 | this seat does not authorise the key being offered — it `cat`s the file and says so | prints a full successful-looking report and then fails at ssh |

**These are not causally chained, and there is no cascade diagram here.** The
last plan earned one because slice 2 made slice 3 reachable and slice 3 made
slice 5 reachable. Nothing of the sort is true now: each of the five is
independently reachable on a run where the other four are fine, and fixing any
one makes none of the others easier. What they share is a *discipline*, not a
dependency — which is why they can land together, and why the ordering below is
chronological in the **user's run** rather than causal in the code.

Rows 3 and 4 have since widened past that spine, and the decisions below are
why: the question stopped being *what podbench fails to write* and became
**where podbench writes at all**. The claim is the user's committed checkout on
an NFS PVC — every file podbench authors there is a permanent line in somebody's
git diff, seen first-hand while hotfix mode was being driven on 2026-08-24. D1b
and D2/D3 act on that, and are this plan's only two slices that *delete* a
behaviour rather than wiring up a measurement.

The one candidate that does *not* fit the spine is #127 (a freshly started pod
is not yet in the metrics API, so the resize is skipped). That is
measurement-*absent*, not measurement-ignored, and it is out of scope for that
reason — see the last section.

---

## 1 — The first run must not need a second

**Measured**: run 1 of the live session, byte-identical to the run that
succeeded, resized `bl47p-ea-fastcs-01` from 256Mi to 6Gi and then lost a 409 on
its own ephemeral-container write (`phase8-vscode-actually-debugs-on-p47.md`
§2). Complete output, one line, on stderr:

```
podbench: kubectl ... replace --raw .../ephemeralcontainers -f -
exited 1: Error from server (Conflict): Operation cannot be fulfilled on pods
"bl47p-ea-fastcs-01-0": the object has been modified; please apply your changes
to the latest version and try again
```

`ephemeralContainers []`, `limits.memory 6Gi`, `requests.memory 615Mi`,
`restartCount 0/0`. **No seat, and nothing said about the resize.**

Two mechanisms, both read out of the source:

- `Kubectl.add_ephemeral_container` (`kubectl.py:993`) does a fresh
  `get_pod_subresource` and then a `raw_put` of the whole pod carrying that
  GET's `resourceVersion`. `grep -n "Conflict\|409\|retry" src/podbench/kubectl.py`
  returns nothing on this path: **a 409 is not anticipated anywhere.**
- The report prints from `vscode_command` *after* `_land` returns
  (`launcher.py:6635`), and `try_resize`'s outcome is a string `_land` appends
  to `session.warnings` (`launcher.py:6242-6256`). A run that aborts between the
  two says nothing about a mutation it made to a live production-shaped pod.

Slice 1 of #214 makes this **more** reachable, not less: with a flat
`EDITOR_LIMIT` of 6Gi the *first* `podbench vscode` against any pod under 6Gi
resizes, and therefore runs straight into the kubelet's resize writeback. On p47
that was the very first invocation. #136 and #210 are the same defect from two
directions.

**Decision.**

- **Retry the ephemeral-container PUT on conflict, inside
  `add_ephemeral_container`, bounded (3-5 attempts, brief backoff), re-GETing
  each time.** Inside, and not in `_walk_ladder`, for a reason
  `ephemeral-containers` makes load-bearing: the ladder burns a container name
  per attempt (`launcher.py:1085`), and this retry must reuse the same name. It
  can: #136 records that the name is *not* burnt by a 409 because the container
  was never created, and the spec already filters an existing entry of the same
  name before appending (`kubectl.py:1036-1042`), so the operation is idempotent
  by construction.
- **Recognise the conflict rather than the string at the call site.** A
  `KubectlError.is_conflict` property beside `is_psa_ptrace_denial` and
  `is_admission_denial` (`kubectl.py:321-360`), with doctests, in that house
  style. It must not match an admission denial, an RBAC forbidden, or a webhook
  timeout — the ladder acts on those and a retry there would be wrong.
- **Print a mutation on the path that made it** (D5, decided). `try_resize`'s
  outcome stops being a string `_land` appends to `warnings`
  (`launcher.py:6247-6256`) and is emitted at the moment of the patch. This is
  `terminal-reports`' own rule — *"a caveat about a mutation belongs on the path
  that made it"* — which the code has drifted from, and it also removes one
  block from every report (#203's rule 2: do not announce one event twice).
  It changes the shape of **every** `attach --resize` and `vscode` run, not only
  the failing one, and existing tests assert the current ordering: expect to
  update them, and do not read a red test there as a regression.

**Falsified if** a deliberate reproduction — pod put back to its template 256Mi,
then one `podbench vscode` — still loses the seat, or if a run aborted between
the resize and the seat still says nothing about the resize. Also falsified if
the retry ever lands a second ephemeral container, which is the one way this fix
could be worse than the bug.

**Test**: an injected runner that answers the `ephemeralcontainers` PUT with a
409 once and then succeeds, asserting one container in the final body and the
same name throughout; a second that answers 409 every time, asserting the error
says it is a conflict, that it is transient, and that re-running is the remedy;
doctests on `is_conflict` covering a conflict, a PSA denial and a webhook
timeout. Plus a unit test that a `try_resize` success is emitted before `attach`
is called, not after it.

---

## 2 — Never author a port nothing answers

**Measured**: run 3, a plain reconnect, replaced a working `launch.json` with
one pointing at a closed port (`phase8-...-on-p47.md` §8.2). The diff between
run 2's file and run 3's is three port numbers and nothing else:

```diff
-            "port": 40448        +            "port": 37516     # pid 13, live -> dead
```

and podbench itself said so, correctly, on the line above:

```
--provision: injected in 1.8s, but nothing is listening on 127.0.0.1:37516
(Connection refused): the injector returned 0 and left no server behind
```

That is `_proof`'s `Handshake.REFUSED` sentence (`provision.py:596`). **The
handshake result is computed and then discarded.** `_inject` returns
`injected.ok` — the injector's exit code — and not `injected.proved`
(`vscode.py:1499`, `1507`), and the configuration author downstream never consults
either.

Why it could not find the live server, read out of the source rather than
inferred: `probe_at = DEBUGPY_PORT if port is None else port`
(`vscode.py:1922`). A run with no `--port` probes the conventional 5678 and
moves that probe **only** to a port it has itself injected on. Run 2's server
was on an ephemeral 40448, so run 3 looked at 5678, found nothing, chose a fresh
port, injected into a pid debugpy declines to serve twice, and emitted the new
number.

**Decision.**

- **Find the adapter where it actually is: as a child of the target.**
  `api.py:181` spawns the adapter from the *debuggee*, so its `ppid` is the
  target pid, and its argv carries `debugpy/adapter … --port <n>
  --server-access-token …` — the exact string podbench already prints
  (§3.1 quotes it doing so). `ProcInfo` carries `ppid` and `cmdline`
  (`proc.py:871`) and the enumeration has already happened. `ppid`, not the
  port, is the discriminator: under `hostNetwork: true` a port proves nothing
  about which pod holds it, which is the whole of #87, while the pid namespace
  is genuinely the pod's.
- **Gate the write on the handshake.** A configuration must never name a port
  that failed `initialize`. Where a live adapter is found, author against *its*
  port; where an injection's handshake fails, **keep what is already in
  `launch.json`** and say why, rather than replacing a file that worked with one
  that does not. This is the honest answer to the case
  `phase8-...-on-p47.md`'s "not measured" list leaves open — whether a third
  injection into a pid whose pydevd threads have exited would succeed — because
  it does not need that question answered: the handshake decides.
- Do **not** infer "already provisioned" from debugpy being mapped in the
  target's address space. Ten `debugpy`/`pydevd` mappings survived the adapter's
  death (§9), so mapped modules are not evidence of a live server.

### 2b — and stop reporting on the process podbench started

Run 3 listed pid 181 — **the debug adapter podbench itself had just started** —
among the debuggable pids, and spent three lines on it:

```
debug-config: could not read /proc/181/exe (it needs PTRACE_MODE_READ) ...
debug-config: pid 181 (pid 181): unknown target, observe mode
debug-config: pid 181 (pid 181): nothing emitted ...
```

Cosmetic in cost and not in effect: it is podbench reporting that it cannot read
a process podbench created, which is exactly the "wonder whether something went
wrong" this plan exists to delete. It is the same enumeration slice 2 now reads
the adapter's argv out of, so the fix is one predicate in the place that already
has the answer: a `debugpy/adapter` child of a candidate is this run's
plumbing, not a candidate.

**Falsified if** a second `podbench vscode` against the same pod leaves a
`launch.json` whose port fails a DAP `initialize`; if the adapter podbench finds
belongs to a pid other than the one the configuration names; or if a run that
found a live adapter still ptraces the workload to inject a second one.

**Test**: unit tests over a synthetic `/proc` tree carrying an adapter child of
the target with a `--port` in its argv — one asserting the emitted configuration
names that port and no injection is attempted, one where the adapter's `ppid` is
some other pid asserting it is ignored, one where the handshake is stubbed to
`REFUSED` asserting the existing `launch.json` is left alone and the reason is
printed, and one asserting the adapter is absent from the candidate list.

---

## 3 — Tell the Python extension what podbench measured, in the seat's own file

**Measured**: the window raises a **"no Python interpreter found"** popup on a
pod where debug attach then works perfectly (#219, from the live run).
`SEAT_MACHINE_SETTINGS` / `SEAT_FOLDER_SETTINGS` (`vscode.py:256`, `285`) write
`python.analysis.exclude`, the watcher and search excludes and
`C_Cpp.files.exclude` — and **no `python.defaultInterpreterPath`**.

Podbench knows the answer. `Target.interpreter` and `Target.python_version` are
measured in the seat (`flavour.py:366`, `371`), and the same run printed the
target's interpreter while emitting the debugpy configuration. The live target's
is `/podbench/app/.venv/bin/python3`, which on a hotfixed pod resolves to the
same file in both mount namespaces — that is the property the whole layout
exists for, and slice 2 of the previous plan measured the inode identity.

**Decision** (D1 and D1b, decided).

- Write `python.defaultInterpreterPath` **only where the pod carries the hotfix
  layout**, from the interpreter read off the target. On a pod without it, leave
  it unset: the seat's own interpreter is not the one being debugged, and
  pointing the extension at it is worse than the popup. #219 says this and it is
  right.
- **Into the seat's machine settings** — `MACHINE_SETTINGS_PATH`
  (`vscode.py:225`), the file `agent.ensure_vscode_settings` already owns
  (`agent.py:1185`) — and never into the claim's `.vscode/settings.json`.
- **And podbench stops writing the claim's `settings.json` and
  `extensions.json` altogether** (`editor.py:632-655`). `launch.json` is the one
  file that stays on the claim; see D1b for why the obvious extension of this —
  a global `launch` object in settings — is *not* taken here.
- **The wire has one constraint**: `debug-config --print-config` prints a launch
  document to stdout and `terminal-reports` requires it stay pasteable byte for
  byte. A second top-level key would land in a pasted `launch.json` and draw a
  schema squiggle. The precedent that avoids it is already in the codebase —
  `_author` reads `PROVISION_FLAG` out of the seat's **stderr** narration
  (`editor.py:1055`) — and the interpreter should travel the same way.

**The launcher has no machine-settings writer today, and this slice must build
one.** `merge_machine_settings` has exactly one caller — the agent, at *seat
start-up* (`agent.py:1327`) — while the interpreter is measured from the target
*after* the seat exists. So the launcher writes it, through the same
`_merge_into` the folder files use (`editor.py:1135`), spelled against the **ssh
login's** home: the precedent is `EXTENSIONS_DIR = "~/.vscode-server/extensions"`
(`editor.py:752`), and it is the only home that matches the agent's
(`agent.session_home`). The excludes travel in the same merge.

**Two consequences to state rather than discover.**

- `SEAT_FOLDER_SETTINGS`' docstring (`vscode.py:285`) says the folder copy
  exists because `~/.vscode-server` is a directory the *client* owns and
  **Kill/Uninstall VS Code Server on Host** deletes wholesale. Machine-only
  means that action silently drops podbench's excludes — not cosmetic:
  `**/proc/**` is what stops the recursive walk that OOMs an unrestartable seat.
  Mitigation: the agent writes them at start-up, and — *once the writer above
  exists* — a re-run of `podbench vscode` restores them on a reconnect too.
  Residual risk, and it is real: a window kept open through a Kill Server and
  reconnected by VS Code rather than by podbench opens a folder with no excludes.
- It leaves `SEAT_FOLDER_SETTINGS` and `merge_folder_settings` with no caller.
  **`merge_launch_configs` is untouched and stays on the critical path**, since
  `launch.json` is still written to the claim — so #214's JSONC handling
  (`vscode.py:1002`) is **not** retired. Delete the folder-settings pair if the
  review wants it gone; do not take the JSONC module with it.

**Falsified if** the popup still appears on the live target; if the key
overwrites one the user had set; if it is written on a pod with no hotfix
layout; if `--print-config`'s stdout stops being a launch document VS Code
accepts without a schema warning; or if, after a `podbench vscode` run against a
hotfixed pod, `git status` on the claim shows anything podbench authored **other
than `.vscode/launch.json`**.

**Test**: a unit test that the merge adds the key when absent and leaves a
user-set value alone (the `_merge_settings` "clobbering none" contract,
`vscode.py:428`); a test that no key is emitted for a pod without the layout; a
test that the `vscode` path writes exactly one file under the claim's `.vscode/`
and that its name is `launch.json`; and a doctest pinning `--print-config`'s
stdout as a parseable launch document with exactly the keys a launch document
has.

---

## 4 — git works in the tree `vscode` opens

**Measured**, in a seat on the live hotfixed pod, in the tree `podbench vscode`
deliberately opens (#217):

```
podbench@bl47p-ea-serv-01:/podbench/app$ git status
fatal: detected dubious ownership in repository at '/podbench/app'
$ ls -l -d .git
drwxr-xr-x. 7 podbench 37887 4096 Aug 24 07:21 .git
```

The *first* thing a user does in the folder — in the SCM pane or in the seat's
terminal — is the thing that fails. **Podbench already solved this for its own
git**: `hotfix.git_argv` (`hotfix.py:2182`) passes
`-c safe.directory=<checkout>` on every invocation, and its docstring records
the same measurement from 2026-08-22. The user's git gets nothing. The decision
is stated in 4b, because one mechanism now closes this and #216 together.

### 4b — one `/etc/gitconfig`, for this and the 15 MB beside it

Slice 2 of the previous plan moved the provisioned debugpy onto the claim, which
is the right destination and is also the application's own checkout, so ~15 MB
appears untracked in a repository the user did not ask podbench to write into
(#216).

**Decision** (D2 and D3, decided, and they are now **one mechanism**): an
`/etc/gitconfig` in the seat carrying both halves.

- `safe.directory` naming the claim, which is #217.
- `core.excludesFile` pointing at a podbench-owned ignore file listing
  `.podbench-debugpy` (`provision.py:119`), which is #216 — and takes the 15 MB
  out of the SCM pane **without writing anything into the user's repository**:
  no `.gitignore`, no `.git/info/exclude`, no repository metadata at all. This
  is better than either option #216 was filed with, and better than the
  `.gitignore`-of-`*` this plan carried before it: that one still put a file
  podbench authored inside the user's working tree, and this one puts nothing
  there.
- **Why the system config and not `~/.gitconfig`**: it is the container's own,
  it dies with the container, it touches no volume — so there is no question of
  a config podbench wrote outliving the seat on a `podbench-home` PVC — and it
  cannot drift, because the image is rebuilt each release.

**Which half is baked and which is written at runtime.** `core.excludesFile`
names two fixed paths (its own, and a pattern for a fixed directory *name*), so
it is baked into the image and never touched again — the mountPath varying does
not matter, since a gitignore pattern matches at any depth. `safe.directory`
cannot be baked: the claim's mountPath is the *application's* choice, discovered
at runtime (`hotfix.claim_mount_path:1186`, mirrored into the seat and read back
by `launcher.seat_claim_path:1157`), so it is written per seat. The agent's
start-up is the place, as a `step(...)` beside `vscode-settings`
(`agent.py:1327`); the mountPath reaches it the way the seat's other launcher-known
facts do, in the seat's container spec `env` (`spec.py:398`, and `spec.py:862`
for the `dev` sidecar).

**One thing that does not work as written, and the slice must handle it**: a
degraded seat is a non-root uid with an empty effective set — 37887 on p47 — and
`/etc` is root-owned, so **that agent cannot create `/etc/gitconfig` at all**.
This is slice 2 of the previous plan's `/opt` failure, again — the image already
solves the shape twice, at `Dockerfile:227` (`chmod g=u /etc/passwd`) and
`:231` (a 0666 extrausers database), with `agent.restrict_seat_nss_database`
taking the write bits off on a root seat. Two ways out, and the first is
cheaper:

- **Bake `/etc/gitconfig` with `core.excludesFile` plus an `include.path` under
  `/tmp`**, and let the agent write only that include. `/tmp` is world-writable
  on every rung, container-local and on no volume — the same properties that
  made the system config the right home. Git documents a missing `include.path`
  as silently ignored: **read from the docs, not measured here**, so measure it
  in a seat first.
- Or pre-create `/etc/gitconfig` writable — which needs the #98-shaped argument
  the extrausers database carries and a root-seat counterpart to
  `restrict_seat_nss_database`, since git config is code execution
  (`core.pager`, aliases). More argument, same outcome.

**`hotfix.git_argv` keeps its `-c safe.directory=` and is not touched.** Its
docstring (`hotfix.py:2182`) argues that a HOME config is *insufficient* for
hotfix mode — a hotfix spans seats, and a config in the first seat's HOME is
gone by the second. That is an argument for `-c`, not against a config, and the
two are complementary: `-c` for podbench's own invocations in **any** seat,
`/etc/gitconfig` for the human in **this** one. A future reader will otherwise
read this slice as contradicting that docstring.

**Caveat to record**: this is git *in the seat*, and only there. From anywhere
else — another pod, or the NFS share from a laptop — `.podbench-debugpy` is
untracked again and the ownership check still fires. In the seat, which is where
the work happens, neither is visible.

**Falsified if** `git -C <claim> status --porcelain` in a seat on the hotfixed
pod still says `dubious ownership`; if it lists anything podbench created other
than the `launch.json` change; if the `safe.directory` podbench writes names any
path other than the claim; or if a seat with no claim gets one written at all.

**Test**: a unit test that start-up writes the `safe.directory` line for a
mounted claim and writes nothing on a seat with no claim; one asserting the
config it writes names the claim's mountPath and not `HOTFIX_APP_PATH`
(`model.py:762`), which is the assumption `seat_claim_path` exists to avoid; and
one pinning the baked ignore file's pattern against `CLAIM_DEST_NAME`, so the
two cannot drift apart.

---

## 5 — Do not open a window that cannot connect

**Measured**: the first half of #204 has landed — `seat_authorises`
(`launcher.py:4220`) `cat`s the seat's `authorized_keys`, `reconnect_key_note`
says nothing when the key is there and states *unmeasured* when the file could
not be read. The measurement exists. **It is then appended to
`session.warnings` (`launcher.py:2654-2659`) and the run continues**, so
`podbench vscode` on a reconnect whose key is measurably absent prints a full,
successful-looking report, wires a stanza, and only then fails at
`check_reachable` (`editor.py:623`) with `Permission denied (publickey)`.

For `attach` that is right: ssh is one feature among several and the exec
helpers still work. For `vscode`, **ssh is the deliverable**. A run podbench has
already measured cannot succeed should not print a report that reads like one.

Nothing here touches ssh logic, and nothing mutates `authorized_keys`: an
ephemeral container's spec is immutable, `attach-endgame` refused auto-landing a
replacement because silently replacing a seat a colleague is using is worse than
a refusal, and slice 4 of the previous plan settled the wording. This slice
changes **when** the existing measurement is acted on, and that is all.

**Decision** (D4, decided): on `vscode` and only on `vscode`, a *measured
absent* key **refuses** before the report, naming `--new`, and does not land a
seat itself. Unmeasured keeps today's behaviour — a warning and a run — because
an unreadable file is not evidence of a refusal. No flag for now. The cost is
paid honestly: it is one more command to type on a path where `--new` is the
only thing that can work, so this plan's own "one command" promise is **not**
kept for that user.

**Falsified if** a reconnect whose key *is* present is refused, or one whose
`authorized_keys` could not be read is refused; either turns a working setup
into a blocked one, which is the failure `vscode-in-a-seat` warns of for
preflights generally.

**Test**: three unit tests over a stubbed `seat_authorises` — present (runs),
absent (refuses, message names `--new`, no wiring written and no editor opened),
unmeasured (runs, warning kept) — plus one asserting `attach` is unchanged in
all three.

---

## 6 — Prove it on the live target

One run on the still-wired `bl47p-ea-fastcs-01-0`, landing **exactly one seat**.

**Delete the pod first and let the StatefulSet recreate it.** This is not
tidiness. `vscode-in-a-seat` is explicit — *"Test this path on a cold seat or you
have not tested it"* — and the phase-8 run did **not**: that pod had carried a
seat earlier the same day, which is why §5.3's reload warning and the extension
install path both went untested there, and it is recorded on that file's "not
measured" list. A recreated pod also puts the memory limit back to its template
256Mi, which is the only state in which slice 1 can be reproduced at all.

`podbench vscode <pod> -n p47-beamline --image <branch tag> --pull always`,
then, with no hand-fixes at any step:

- [ ] **the first run lands a seat** — no 409, no re-run
- [ ] the resize is announced *when it happens*, not only in the report
- [ ] the window opens with **no "no Python found" popup**
- [ ] `git status` in the seat's terminal answers — no `dubious ownership` —
      and `.podbench-debugpy` appears neither in `git status --porcelain` nor
      in the window's SCM pane
- [ ] `git status` on the claim is **clean except `.vscode/launch.json`**:
      nothing else podbench authored, tracked or untracked
- [ ] a breakpoint in `src/fastcs_example/controllers.py` binds and is hit
- [ ] a **second, identical run** leaves `launch.json`'s port still answering a
      DAP `initialize`, and the memory limit unchanged
- [ ] podbench's own adapter is absent from the `notes` row and emits nothing
- [ ] `restartCount` is still 0 on both application containers

**Do not ask the bridge for `dap.*` events.** The previous plan's third checkbox
only half-passed for a reason that is now understood and is not a podbench
defect: `extension.js:368` registers `registerDebugAdapterTrackerFactory('*')`
in the **laptop's** UI extension host, while a seat-side adapter's descriptor is
created in the **seat's** workspace host, so no DAP traffic ever reaches the
laptop tracker. The session is proved by the breakpoint and by the seat's own
`Python Debugger.log` reaching `Received 'debugpySockets' event from debugpy`
(§6). Record that in `tools/vscode-bridge/README.md` while you are there — it is
a bridge limitation and the next person will otherwise read it as a failure.

**Falsified if** any step needs a manual repair. That is the whole point.

---

## Decisions taken

Each of these changes something outside podbench's own scratch space, or changes
what an existing verb prints, so none was taken quietly. All five are **decided
(Giles, 2026-08-24)**; the reasoning is kept because a decision with its
rationale deleted is worth much less to whoever reads this next.

**D1 — `python.defaultInterpreterPath` goes in the seat's machine settings
(#219).** `MACHINE_SETTINGS_PATH` (`vscode.py:225`), the file
`agent.ensure_vscode_settings` already owns (`agent.py:1185`), not the claim's
committed `.vscode/settings.json`. The key is declared `machine-overridable`, so
a machine value answers the popup and a folder value the user sets still wins —
exactly what #219 asks for. **Cost**: the interpreter and the excludes now live
in two files rather than one.

**D1b — and podbench stops writing `settings.json` and `extensions.json` into
the claim's `.vscode/` at all.** The motivation is first-hand: driving hotfix
mode on 2026-08-24 dirtied this repo's own `.vscode` folder with podbench's
writes. The claim is the user's committed checkout on an NFS PVC, so a folder
write is a permanent line in their git diff, not a scratch file.
`extensions.json` is recommendations only, and podbench installs the extensions
itself with `code --remote … --install-extension` and then verifies them in the
seat (`editor.py:688`, then `_verify_installed` at `editor.py:863`), so dropping
it costs nothing.

**`launch.json` stays on the claim**, and that is the interesting half. VS Code
*does* support a global `launch` object inside `settings.json` — but whether it
is honoured at **machine** scope on a **remote** is **unverified**, and F5 is
the deliverable, so it is not asserted either way and not guessed at. Measuring
it is a candidate future slice, listed under out of scope. D1b's two
consequences — what *Kill VS Code Server on Host* now deletes, and
`merge_launch_configs` staying load-bearing — are in slice 3.

**D2 + D3 — one `/etc/gitconfig` in the seat closes both #217 and #216.** Two
decisions, now one mechanism: `safe.directory` naming the claim, and
`core.excludesFile` pointing at a podbench-owned ignore file listing the
provision directory. The second was Giles' own suggestion and is better than
both options #216 was filed with, and better than the `.gitignore`-of-`*` this
plan carried before it, because it puts **nothing** inside the user's repository
— no ignore file, no `.git/info/exclude`, no metadata. `/etc/gitconfig` rather
than `~/.gitconfig` because it is the container's own system config: it dies
with the container, touches no volume, so nothing podbench wrote can outlive the
seat on a `podbench-home` PVC, and it cannot drift, since the image is rebuilt
each release. **Cost**: it is git *in the seat* only — from another pod, or from
the NFS share on a laptop, both symptoms come back. Slice 4b carries the
implementation constraints, including one that does not work as written.

**D4 — refuse, naming `--new`, when the key is measured absent (#204, slice 5).**
Auto-landing replaces a seat a colleague may be using and spends an
ephemeral-container name from a finite per-pod supply; `attach-endgame` already
refused it for `attach`, and the reasoning has not changed. **Cost**: one more
command to type on a path where `--new` is the only thing that can work, so the
"one command" promise is not kept for that user. No flag for now — if one is
ever wanted, the flag is the thing that should be remembered.

**D5 — `try_resize`'s outcome moves to the point of mutation (slice 1).** Chosen
over the narrower exception-path-only alternative: it is `terminal-reports`'
stated rule, it is the only way an aborted run can report what it already
changed, and it removes one of the two blocks announcing a single resize twice.
**Cost**: it changes the shape of every `attach --resize` and `vscode` run, not
only the failing one, and existing tests assert the current ordering — the slice
must expect to update them.

---

## Ordering, and why

There is no cascade, so the order is not causal. It is **chronological in the
user's run**, which is the only ordering a plan about smoothness can defend:

1. **Slice 1 is before the seat exists.** It is the only defect here that fails
   the run outright, and until it is fixed every later slice's live test is a
   retry — which is exactly what made the phase-8 run's checklist read "five of
   six on the first attempt". It also has to go first for a mechanical reason:
   slice 6 recreates the pod at 256Mi, so *every* proof run from here on passes
   through the resize-then-write window.
2. **Slice 5 is before the window.** It decides whether the run continues at
   all, and it is the one slice that changes control flow on a path the others
   all traverse. Doing it after them means re-testing them through a path that
   has moved.
3. **Slices 3 and 4 are the first thirty seconds inside the window** — the
   popup, and the first `git status`. They are independent of each other and of
   everything else; either can be dropped without touching the rest. One
   asymmetry now matters: slice 4 changes the **image** as well as the agent, so
   it is proved only by a branch push with `--pull always`, while slice 3 is
   launcher-side. Land 4 early enough that its image is on the node by slice 6.
4. **Slice 2 needs a second run to see**, so it is tested by the same run that
   tests slice 1's idempotence. Last of the code slices for that reason, not
   because anything depends on it.
5. **Slice 6 last, always**, and never begun with a context that is nearly full.
   It is the only slice that can fail for reasons that are nobody's fault, and
   the only one whose result is evidence rather than code.

Slices 3, 4 and 5 could each ship alone. Slices 1 and 2 could not usefully be
split further, because each is one measurement wired into one decision.

## Deliberately not in scope

- **#203, the 33-constant warning audit.** Half of it has already landed —
  `ADMISSION_MUTATION_WARNING` is gone and #204's hedge is now a measurement —
  and what remains is a prose pass over four modules read end to end as a body of
  text, which `terminal-reports` explicitly says cannot be done one constant at a
  time. Different shape of work, and I have no fresh word count for the current
  build to prioritise it against.
- **#204's second half, the severity colour tier.** Its own issue says it only
  works once the yellow volume comes down, which is #203.
- **#127, the freshly started pod.** The one candidate that breaks this plan's
  spine: podbench cannot measure, rather than measuring and ignoring. The fix is
  a poll, and how long a verb may block before giving up is a decision, not a
  wiring change.
- **#124, the Guaranteed pod.** The issue says outright that moving the *request*
  is not an agent's call: it reserves memory on a beamline node rather than
  capping it, which is a different blast radius from anything in this plan.
- **#142, the ephemeral-storage eviction.** There is no runtime fix — resize
  reaches cpu and memory only — so the remedy is a chart change made before the
  seat lands, and the work is a documentation pass plus a warning rewrite that
  belongs with #203.
- **#161, the seat SIGKILLed by a target restart.** Real, and it applies to every
  `attach` against every pod rather than to this path.
- **#155 (multi-root workspace), #160 (`/app/.venv` collision), #202
  (devcontainer-native hotfix), #69/#70 (seat survives a reschedule).** Features.
  #155 in particular can *remove* the `search.followSymlinks` guard that stops a
  seat OOMing, so it is not a smoothness change.
- **#212, tier-1 GUI-free e2e.** The durable answer to needing a human at a
  screen for slice 6, tracked on its own, and unchanged since the last plan said
  so.
- **#221, the manual *Reload Window* a first run needs before F5 works.**
  Decided out (Giles, 2026-08-24), and it is the one exclusion that genuinely
  costs the plan's own standard: a first-time user does have to visit the
  Command Palette before the debugger exists, which is not "one command, a
  window, F5". It fits the spine — podbench knows exactly which extensions are
  missing and installs them where the running extension host will not see them
  (`editor.py:722`, after the window opened at `:708`, because the seat's
  vscode-server does not exist until a window bootstraps it).

  It is out because the fix is a different *shape* of work from the other five.
  Each of those uses a measurement podbench already holds; this one would have
  podbench reproduce part of VS Code's own install — unpacking a VSIX into
  `~/.vscode-server/extensions` with the version-suffixed directory name
  `unpacked_extensions` already parses, and the manifest entry beside it — and
  would very likely need a marketplace URL in the runtime, which this project
  has kept out of it. That is a design decision with its own blast radius, not a
  wiring change, and folding it in would unbalance a PR whose other five slices
  are each a few hundred lines.

  **The measurement it waits on** is one question: is a hand-unpacked extension
  honoured by a server that has not yet started? Until somebody answers that,
  every design above it is speculation. `_SEAT_INSTALL_RELOAD_NOTE`
  (`editor.py:336`) remains the honest answer meanwhile — it names the symptom
  and the action, and it is correct.
- **Whether a global `launch` object is honoured at machine scope on a remote.**
  The one thing that would take `launch.json` off the claim too and leave
  podbench writing *nothing* into the user's checkout. VS Code supports the
  object in `settings.json`; nobody here has measured it at machine scope over
  Remote-SSH, and D1b refuses to guess with F5 as the stake. A future slice, and
  it is a **measurement** before it is a change — with the JSONC merge and
  `merge_launch_configs` retiring only if it passes.
- **The bridge's `dap.*` blind spot.** Understood (§5.1), tooling rather than
  product, and it changes slice 6's checklist rather than any code under `src/`.
- **The flat 4.00 s `attach` response.** Reproducible to the centisecond across
  four sessions and two pids and traced to no line of debugpy; VS Code's own
  deadline is longer, so nothing on this path meets it. Recorded on
  `phase8-why-the-adapter-never-answers.md`'s "not measured" list, and left
  there.
- **`podbench vscode` against a pod with no claim.** Everything measured on p47
  is the hotfixed shape. `/opt/podbench-debugpy` and the
  `gdb-across-namespaces` interpreter collision it can create are still
  unexercised, and slice 3 deliberately stays silent on that pod rather than
  guessing.
