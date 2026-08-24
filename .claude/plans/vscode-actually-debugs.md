# `podbench vscode` actually debugs

Successor to `hotfix-becomes-easy-to-drive.md`, which ended by *naming* two
field failures rather than fixing them. Everything below has a measurement
behind it in `.claude/evidence/phase7-vscode-and-the-two-failures.md` (the
2026-08-24 redo, against a genuinely hotfixed `bl47p-ea-fastcs-01-0`); where
this plan asserts a cause, that file is the citation.

The target is left hotfix-wired on p47 deliberately, so every slice here can be
re-tested without replaying the values→deploy→sync loop first. Read
[[pollux-p47-access]] for the token, and the `vscode-in-a-seat`,
`terminal-reports` and `gdb-across-namespaces` skills before touching the code
each slice names.

---

## The shape of the bug

Failure 2 — "debugging does not start" — is not one defect. On a hotfixed pod
it is a **cascade with three independent links, each fatal on its own**, which
is why the first attempt to chase it found a different (and self-inflicted)
story on a bare pod:

```
--provision writes to /opt/podbench-debugpy   ← root-owned; degraded seat is uid 37887
        │                                        EACCES
        ▼
no debugpy importable by the target
        │
        ▼
_configurations() returns []  ──────────────►  extensions_for([]) == []
        │                                        so the seat gets no debug adapter either
        ▼
[warn] no launch.json: nothing above could be turned into one
        │
        ▼                     ... and even when it can be turned into one:
merge refused: the app's committed .vscode/launch.json is JSONC
        │
        ▼                     ... and even when it is written:
adapter accepts TCP, never answers `initialize`
```

Slices 2, 3 and 4 take one link each. Slice 5 is the one that is still a
question rather than a fix.

---

## 1 — A fixed editor limit, because the current one ratchets

**Measured**: two `podbench vscode` runs against the same pod raised its memory
limit `1Gi → 2Gi` and its request `103Mi → 205Mi`. A third would raise it
again. The cause is `resize.editor_limit` (`resize.py:656`):

```python
return Fraction(math.ceil((current + EDITOR_HEADROOM - free) / GI)) * GI
```

The target is computed from *live* `free`, which the editor itself is consuming
— so every reconnect recomputes a bigger number from the consequences of the
last one. It is not idempotent, and the verb that does this is the only one
that resizes a pod nobody asked it to.

**Decision (Giles, 2026-08-24): a flat default of 6Gi, with `--resize`
overriding it.** Idempotent by construction, and a number a human can check.

- Replace the shortfall arithmetic with a constant — `EDITOR_LIMIT` — and
  return `None` when `current` already meets it, so a big pod is never
  *shrunk* and a second run is a no-op.
- `EDITOR_HEADROOM` (1215 MiB, measured) stays: it is what decides whether a
  resize is needed at all and what the warning quotes. Only the *target*
  becomes fixed.
- The `--resize MEMORY` flag already exists and must keep winning.
- `EDITOR_HEADROOM_WARNING` and the two `WARNING` lines in `launcher.py:388`
  quote the number; read `terminal-reports` before editing them, and keep a
  warning to one line.

**Falsified if** two consecutive `podbench vscode` runs against the same pod
produce different limits, or if `--resize 3Gi` is overridden by the default.

**Test**: a unit test asserting `editor_limit` is idempotent — feeding its own
output back in returns `None` — which is the property the old formula could
never have.

---

## 2 — Provision where the seat can actually write

**Measured**: `PROVISION_DEST = "/opt/podbench-debugpy"` (`provision.py:79`) is
a fixed constant. `/opt` in the target is `drwxr-xr-x root root`; the seat on
the `degraded` rung is uid 37887 with `CapEff 0000000000000000`. The write is
refused by ordinary file permissions. Pointed at the claim instead, every step
succeeded — `installed debugpy for Python 3.11`, `injected in 8.7s`, thread
count 34 → 38, port open.

On a hotfixed pod the claim is the *right* answer and not merely a workaround:

```
$ stat -c '%d:%i %n' /podbench/app /proc/13/root/podbench/app
1048592:64 /podbench/app
1048592:64 /proc/13/root/podbench/app      # same inode in both namespaces
```

which is exactly the property `gdb-across-namespaces` says a cross-namespace
path must have, and podbench already knows the pod is hotfixed — it prints
`this pod carries the hotfix layout, so the claim 'podbench-app' was mounted
into the seat at /podbench/app` in the same report.

- Default the provision destination to the claim when the pod carries the
  hotfix layout, keeping `/opt/podbench-debugpy` for a pod that does not.
- `--provision-dest` keeps overriding both.
- Do **not** silently fall back from one to the other on failure: a fallback
  turns a permissions bug into a mystery about which path is live. Choose up
  front from the layout, and say which was chosen.

### 2b — and stop explaining it as if the seat were root

`blocker_sentence`'s `EACCES`/`EPERM` branch (`provision.py:211-225`) opens
"uid 0 in this seat carries `CAP_DAC_OVERRIDE`, so the target's own file modes
are not it" and sends the reader to `PTRACE_MODE_READ` and LSMs. True on the
`full` rung. On `degraded` — a non-root uid with an empty effective set, which
is what this beamline admits and what the report printed four lines earlier —
file modes are the *entire* cause and the message rules out the true one. The
module docstring (`provision.py:9`) and `writable_blocker`'s carry the same
assumption.

The seat knows its own uid and capabilities. The sentence should branch on
them: name file modes when the seat is not root, and keep today's text for
when it is.

**Falsified if** a `--provision` on the live hotfixed target still fails, or if
its failure message on a degraded seat does not name file ownership.

---

## 3 — Merge into a real project's `.vscode`, which is JSONC

**Measured**: on a hotfixed pod `podbench vscode` opens the claim — the
application's own checkout — and `fastcs-example` ships all four
`.vscode/*.json` files *committed and unmodified*. Both merges podbench
attempts refuse:

| file | podbench says | actually |
|---|---|---|
| `launch.json` | `Expecting property name enclosed in double quotes: line 2 column 5` | `//` comments, which VS Code's own scaffold writes |
| `settings.json` | `… line 11 column 5 (char 325)` | a trailing comma before `}` |

Both are valid JSONC. This is the **common case on a hotfixed pod**, not an
edge case, and it is fatal on its own: no `launch.json` means no F5 even when
everything in slice 2 works.

- Parse JSONC — comments and trailing commas — where podbench currently
  requires strict JSON, in `merge_launch_configs` and the settings merge.
- **Preserve what is already there.** These are the user's committed files;
  a merge that reformats them, drops their comments, or reorders their keys is
  a worse outcome than the refusal it replaces. The bridge's own shim already
  faces this and solves it by inserting after the opening brace rather than
  round-tripping (`tools/vscode-bridge/README.md`) — the same restraint
  applies here.
- Keep refusing, with today's message, on something that is not JSONC either.

**Falsified if** the app's `.vscode/launch.json` comes back with its comments
stripped, its two existing configurations disturbed, or its diff larger than
the podbench block that was added.

---

## 4 — Failure 1 is a message, and only a message

**Giles' reading is correct, and the measurement backs it**: a reconnect
offering the key the seat already carries just works — that is what every
ordinary `podbench vscode` reconnect does, and it was re-measured this run
(`ssh … SEAT_REACHED` after restoring the identity). The refusal fires *only*
when a genuinely different key is offered, and an ephemeral container's
`authorized_keys` is immutable, so **there is nothing to mutate and podbench
must not try.** `attach-endgame` already refused auto-landing a replacement,
because silently replacing a seat a colleague is using is worse than a
refusal. None of that changes.

What is wrong is one string. `UNREACHABLE_CAUSES` (`editor.py:389-401`) is
reached only from `check_reachable` → `open_seat` → `_open_editor`
(`launcher.py:6300`) — the `vscode` verb, and nothing else; `attach` never
reaches it. Two of its four bullets nonetheless hard-code `podbench attach
--new` as the remedy, including the one that actually fires. A user who ran
`podbench vscode` is told to run a different verb.

- Name the verb that was actually run, or word the remedy verb-agnostically —
  the `WARNING` beside it already manages this (`\`--new\` lands a seat that
  takes it.`) and is correct as it stands.
- Read `terminal-reports` first: this text is relayed stderr inside a report,
  and the bracket-eating trap applies.

**Falsified if** the fix changes any behaviour at all. This slice adds no
tests beyond the string, and touches no ssh logic.

---

## 5 — Why does the adapter never answer `initialize`?

The one thread the redo left open, and the only item here that is a question
rather than a fix. With debugpy provisioned into the claim, a valid
`launch.json`, and `ms-python.debugpy` installed in the seat:

```
connect 127.0.0.1:37189  -> OK
send    initialize
recv    -> TIMEOUT, 0 bytes in 15s
```

The seat's extension log stops at `Connecting to DAP Server at: 127.0.0.1:37189`.
The adapter is alive, holds three socket fds, and its **server-facing port
(`--for-server 33215`) has no listener and no connection at all**, while the
client port is in `LISTEN`. The target's own log shows pydevd starting and
records no error. So the debuggee half of the session was never established
and the adapter blocks with nothing to attach an IDE to.

**Do not guess at this one.** Instrument first:

- Turn on debugpy's own logging in the target (`DEBUGPY_LOG_DIR`) and in the
  adapter, and read both — the target's stdout is not where debugpy reports
  its connect failures.
- Establish whether the injected `debugpy.connect()` ever runs, and against
  which address. `hostNetwork: true` makes `127.0.0.1` the *node's* loopback,
  which podbench already warns about; whether the target resolves the same
  `127.0.0.1:33215` the adapter bound is the first thing to check.
- Note the interpreter split this shape creates: the driver ran
  `/app/.venv/bin/python` (the image's) with `PYTHONPATH` reaching the claim's
  debugpy through `/proc/13/root`, injecting into a target running
  `/podbench/app/.python/cpython-3.11.13-…`. That is the `gdb-across-namespaces`
  collision by construction, and it is a live candidate — but it is a
  candidate, not the finding.

**Falsified if** a session is reported as working after an unexplained retry.
Name the cause or record it as still not measured; the redo's discipline
applies to its own successor.

### 5b — and stop reporting success from an exit code

`provision.py`'s success line is returned whenever the injector subprocess
exits 0 — `injected in {seconds}s; the app now serves debugpy on
127.0.0.1:{port}`. The redo tightened the existing criticism: **an open port is
not proof either**, since here the port was open and the session still could
not start. Whatever slice 5 concludes, the success line should assert something
the redo could not falsify — at minimum a DAP `initialize` that gets an answer.

---

## 6 — Prove it on the live target

One run, on the still-wired `bl47p-ea-fastcs-01-0`, landing **exactly one
seat** — the redo's own rule, because two seats against one pid is what made
the first attempt untrustworthy.

`podbench vscode <pod> -n p47-beamline --image <branch tag> --pull always`,
then, with no hand-fixes at any step:

- [ ] a `launch.json` exists, and the app's own two configurations survive
      it with their comments
- [ ] `ms-python.debugpy` is installed in the seat
- [ ] `vsc.py debug …` returns `started: true` and `dap.*` events follow
- [ ] a breakpoint in `src/fastcs_example/controllers.py` binds and is hit
- [ ] the memory limit is unchanged by a second, identical run
- [ ] `restartCount` is still 0 on both application containers

**Falsified if** any step needs a manual repair of the kind the redo had to
perform — the whole point of this plan is that those repairs move into the
code.

---

## Ordering, and why

1 first: it is independent of the cascade, it is a decision already taken, and
it changes a number every later run's report prints — doing it first means
every subsequent live run reads against the final shape.

2 before 3 before 5, because that is the cascade's own order and each link
makes the next one *reachable*: there is no launch.json to merge until slice 2
emits configurations, and no DAP session to diagnose until slice 3 writes the
file. Attempting 5 first is what produces a diagnosis of the wrong pod state,
which is the mistake this plan exists downstream of.

4 anywhere — it is one string and touches nothing else.

6 last, always, and never begun with a context that is nearly full. It is the
only slice that can fail for reasons that are nobody's fault, and the only one
whose result is evidence rather than code.

## Deliberately not in scope

- **Mutating `authorized_keys`**, for the reason slice 4 gives.
- **Retiring the p47 target.** It stays wired; `retire` is already evidenced
  end to end in `phase7-the-live-walk.md`.
- **`hotfix init`'s missing `--image`/`--pull` passthrough** — a real defect
  found in the mutating walk, but a `hotfix` defect, not a `vscode` one. File
  it separately.
- **Tier 1 GUI-free e2e (#212)**, which is the durable answer to needing a
  human at a screen for slice 6 and is tracked on its own.
