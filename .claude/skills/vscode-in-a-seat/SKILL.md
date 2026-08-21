---
name: vscode-in-a-seat
description: What breaks when VS Code is the client for a podbench seat — the OOM traps, the breakpoint-versus-probe timer, and the adapter bugs that surface only in a container. Read before changing vscode.py, the image's VS Code defaults, or docs that tell someone to connect an editor.
---

# VS Code in a seat

Every item below was hit for real against a live cluster, and every one of them
fails *silently or misleadingly*: the seat dies with no explanation, breakpoints
never bind, or the kubelet kills the workload and it looks unrelated. The CLI
paths (`podbench dbg`, `podbench pids`) are unaffected by most of this, which is
exactly why they keep working on a seat where the editor cannot.

## Opening a folder can kill the seat

`/proc/<pid>/root` is a symlink into **another container's** root, so pointing
VS Code's file watcher or search indexer at `/` is a recursive walk with no
bottom. An ephemeral container shares the pod's memory limit and cannot reserve
its own (report §3.9), so the walk OOMs it — and an OOM'd ephemeral container
cannot be restarted, so the seat is gone and its **name is burnt** for the pod's
lifetime.

- **Open `/root`, never `/`.** Opening a *file* under `/proc` is fine; opening a
  *folder* is what starts the walk.
- Exclude `/proc` and `/sys` from `files.watcherExclude`, `search.exclude` and
  `python.analysis.exclude`. Machine-level settings live at
  `~/.vscode-server/data/Machine/settings.json`, which the *client* creates on
  first connect — so the image cannot simply pre-place it at build time.
- vscode-server alone is ~700-800 MB RSS, 1.1-1.3 GB with extensions. A pod with
  a 256Mi limit has no chance. `podbench vscode` sizes the pod itself, raising
  the target's limit to cover the shortfall before the seat lands; `attach
  --resize` is the same lever by hand.
- Diagnose after the fact with
  `kubectl get pod -o jsonpath='{.status.ephemeralContainerStatuses[*].state}'` —
  `exitCode 137, reason OOMKilled`. `podbench status` says
  *"OOMKilled: name burnt for this pod's lifetime"*.

## Memory is not the only budget — the unpack can evict the pod

Measured at Diamond, 2026-08-21, on a production `blueapi`: `podbench vscode`
landed a seat, gave a working terminal, then the kubelet evicted the **whole
pod**, twice.

```
Warning  Evicted  Pod ephemeral local storage usage exceeds the total limit
                  of containers 2Gi.
```

vscode-server's ~1215 MiB unpack lands on the **workload's ephemeral-storage**
budget. It presents to the client as a transport fault, because the
StatefulSet recreates the pod and an ephemeral container does not survive that:

```
error: unable to upgrade connection: container not found ("podbench-2")
```

- **`--resize` cannot reach it.** That is the memory lever; in-place resize
  covers cpu/memory only, and an ephemeral container may not carry `resources`
  at all. The remedy is a chart change made *before* the seat lands: raise
  `resources.limits.ephemeral-storage` to cover the workload's own writable
  layer plus ~1.3 GB. 2Gi is not enough for a pod that writes anything.
- **An `emptyDir` `podbench-home` does not fix it.** Kubernetes counts local
  emptyDir volumes against the pod's ephemeral-storage limit, so it moves the
  weight off the *container's* writable layer and leaves it on the *pod's*
  budget. Only a claim actually moves it.
- Why one pod survives and another does not is the app's own writable layer,
  not the limit — every pod in that namespace had the same 2Gi. Nothing measures
  that headroom before the unpack.

On a pod you cannot resize, `attach` plus the CLI (`podbench dbg`, `pids`) is
the 13-23 MiB seat rather than the 1215 MiB one.

## A breakpoint on a probed pod is on a timer

Pausing stops the app answering its probes. Two budgets, both
`(failureThreshold - 1) x periodSeconds + timeoutSeconds` after the pause,
plus up to one more period depending on where in the cycle it began — and
**the quiet one is worse**:

| paused | consequence | visibility |
|---|---|---|
| the readiness budget | pod goes not-ready and stops taking Service traffic; the EndpointSlice keeps the address with `conditions.ready: false` | **quiet** — `Unhealthy` events while it lasts, but nothing restarts and it recovers a period after continue, so afterwards there is no trace |
| the liveness budget | container killed and restarted, **and the seat with it** — it shares the target's namespaces, exits 137 in the same second, and cannot be restarted | loud — event, restart count, burnt seat name |

Measured on the demo Deployment (5 s / 10 s periods, `failureThreshold: 3`,
`timeoutSeconds: 1`, so 11–16 s and 21–31 s): an 18 s gdb attach went not-ready
at ~12 s and recovered 5 s after `detach` with no restart; a 45 s one also hit
`failed liveness probe` at ~25 s. `podbench.budget` computes both from the pod
spec and `attach` prints them, so **do not restate the numbers by hand**.

**Probes cannot be changed on a running pod.** The API permits only
`containers[*].image`, `initContainers[*].image`, `activeDeadlineSeconds`,
`tolerations` (additions) and `terminationGracePeriodSeconds`. Note the
asymmetry: `resources` *is* mutable via the resize subresource, which is what
makes `--resize` work; there is no equivalent for probes.

So live attach is a **short-visit** tool on a probed pod — break, look,
continue. Logpoints and conditional breakpoints never stop the process. For an
unlimited pause use `podbench dev`, which strips all three probes by
construction. This is the same reason `attach`'s report refuses to tick
`iterate`.

## Nothing VS Code reports proves it connected

`code --remote ssh-remote+<alias> <folder>` hands the argv to a window and
returns 0; the authority is resolved *in that window*, afterwards, and every way
it can fail shows up there as a dialog and in the Remote-SSH log. Worse,
`code --install-extension --remote …` also exits 0 without a connection, and
podbench prints "`<ext>` is installed in SSH: `<alias>`" on the strength of it —
which is exactly what a DLS run reported on 2026-08-16 while every connection was
being refused.

So `podbench vscode` proves the alias itself first: one `ssh <alias> true` before anything
is written, installed or launched, and a refusal carries ssh's stderr *whole* —
the mechanism is on the first line (`…/sshd_config: No such file or directory`)
and ssh's own summary, on the last, names a port that does not exist. Do not
"tidy" that into `_detail`, and do not add `BatchMode=yes`: a passphrase-protected
key with no agent prompts and succeeds there, exactly as it would for VS Code, and
a preflight whose false negatives block a working setup is worse than no
preflight.

The same rule applies to a listing: `list`/`status` offer `ssh <alias>` only where
a seat is *running*. The stanza outlives the container — nothing deletes it, and
the name is burnt once it exits — so a terminated seat would otherwise be given a
connect line one row under the words `name burnt for this pod's lifetime`.

## Extensions must install in the *remote* window

The button has to read "Install in SSH: `<alias>`". A locally-installed
extension runs the debug adapter on the developer's laptop, where none of the
`/proc/<pid>/root` paths mean anything, and the failure looks like a bad
`launch.json` — cpptools reports `Program path '/proc/1/root/...' is missing or
invalid`, which reads as a wrong path and is really a wrong *machine*. That is
one of **two** causes of that message; see the next section before assuming it.

`code --install-extension --remote` cannot report this: it exits 0 for
"installed", for "already installed" and for "never reached the remote". So
`podbench vscode` asks the seat instead — `ssh <alias> ls -1 ~/.vscode-server/extensions`,
matched by id prefix, since the directory carries a version and a platform
triple. Over ssh and not `kubectl exec`, because the home that matters is the
one NSS gives the *login* user. A failed listing is reported as unverified, never
as missing: sending someone to reinstall what is already there is its own bug.

**And `code --remote … --install-extension` answers from the *laptop's* install
list.** An extension held locally is reported "already installed", exit 0, and
the seat is never contacted — with or without `--force`:

```
$ code --remote ssh-remote+<alias> --install-extension ms-python.python --force
Extension 'ms-python.python' is already installed.      # ...on this machine
```

Measured at Diamond 2026-08-21, with no `ms-python*` path anywhere on the seat's
filesystem. It fails worst for the people most likely to be here: anyone who
debugs Python already has the Python extension locally. It is also why three
pods looked like three different bugs — a cold seat where nothing landed, a seat
where cpptools landed (not held locally) and CodeLLDB did not, and a reconnect
where both were already there from an earlier run.

The install has to be made where it must land, by **the seat's own server CLI**:

```
ssh <alias> '~/.vscode-server/cli/servers/Stable-<commit>/server/bin/code-server \
   --install-extension <id>'
```

Use the server the window is attached to. A seat holds more than one — the
running server's argv carries its own path (`ps -eo args=`, grep
`cli/servers/*/server`), and `~/.vscode-server/cli/servers/lru.json` is a plain
most-recent-first array for before a window has started one.

The server does not exist until a window has bootstrapped it, so this can only
run **after** the window opens — while `.vscode/settings.json` still cannot move
after it, for the walk-OOM reason above.

### That install always needs a reload, and VS Code says why

This is a *separate process* writing into the extensions directory. The window
notices a change rather than performing an install, and a `debuggers`
contribution is registered when the extension host starts — which already
happened. F5 then gives `could not find a debug adapter descriptor for debugpy`
while the Extensions view happily lists it.

VS Code distinguishes the two in its own log, and the wording is the whole
explanation (measured on a Diamond seat, 2026-08-21):

| how it was installed | log line | reload |
|---|---|---|
| the window's own service — Extensions view button | `Extension installed successfully:` | **no** |
| a separate process writing the files | `Extensions added from another source` | **yes** |

Out-of-process is **structural** here, not a timing accident: the seat's
vscode-server does not exist until a window has bootstrapped it, so the install
cannot precede the window that needs it. Say "Reload Window" and mean it.

### The in-process route exists, works, and is deliberately not taken

Do not rediscover this and build it. `remote-cli/code` forwards to the window
over `VSCODE_IPC_HOOK_CLI`; the socket is in `/tmp/vscode-ipc-*.sock` and the
live one is whichever a running extension host carries in `/proc/<pid>/environ`.
Installing through it was **proved** to produce `Extension installed
successfully` on a live seat, no reload — it is genuinely the button.

It was rejected on cost. It needs a scan of another process's environment for an
undocumented variable, a path inside a version-stamped server directory, and a
rule for which window wins when a seat has several — and the out-of-process
fallback is still needed for when any of that is missing, so it is *additional*
code, not a replacement. What it buys is one Command Palette action.

The failure mode settles it: if VS Code renames the variable or moves
`remote-cli`, podbench believes it installed in-process, **suppresses the reload
note**, and F5 fails with nothing said — this bug, with the hint removed.

Two smaller things measured alongside: `--force` does not bypass "already
installed" on *either* path, so verify-then-install still earns its place; and
an uninstall is deferred to the next window start, so the directory lingers.

**"X is unpacked in SSH" reports presence, not that this run installed it.**
Right check, different question — and on a warm seat it launders "was already
there" into "we did it", which is what hid the defect above for two more pods.

## `Program path … is missing or invalid` usually means neither

cpptools composes that sentence for **any** failure to load the program, with
gdb's real error appended:

```
Program path '/proc/1/root/usr/bin/bash' is missing or invalid.
GDB failed with message: "<gdb's own error>"
```

So the named path is the *last* thing to suspect. Check it (`ls -lL`, `test -r`)
and then read the second half, which is where the fault actually is. Three
causes, in order of likelihood:

1. **gdb read the wrong file — one of *ours* at the target's path** (issue #90).
   gdb canonicalises the exec file's name before BFD opens it, and the kernel
   resolves `/proc/<pid>/root` to `/`. The sysroot is therefore *erased* for this
   one command and gdb opens the bare path in the **seat's** mount namespace.
   Where the seat keeps a different build there, the section table and the
   string tables come from different files and BFD says exactly what cause 2
   says: `invalid string offset … for section '.dynstr'` → `.gnu.version_r
   invalid entry` → `Can't read symbols: bad value`. It is not a rare
   coincidence — the seat image and any uv-managed workload both install an
   interpreter at `/python/cpython-<version>-<triple>/`.

   The discriminator is one command in the seat, and it is worth typing before
   believing cause 2:

   ```
   sha256sum "$exe" "/proc/<pid>/root$exe"    # $exe = readlink /proc/<pid>/exe
   ```

   Two digests means gdb was reading ours. `podbench dbg` and `debug-config`
   now stage a copy and point `file`/`program` at it, and `gdb-podbench` feeds
   the same path to a third-party `gdb --pid`; the fix is in the seat, so an
   **older seat image against a newer launcher still has it**.
2. **gdb could not read the file.** gdb reads ELF through **BFD**, the binutils
   library, so its reach is pinned to the *image's* binutils rather than the
   user's. Measured: a `debian:bookworm-slim` seat (binutils 2.40) against a
   RHEL-family target gave `BFD: /usr/bin/bash: .gnu.version_r invalid entry` →
   `Can't read symbols: bad value`, and the file would not open at all. This is
   **not** "no debug information" — a stripped binary loads silently and debugs
   fine at the address level. The asymmetry is the giveaway: a failed symbol load
   is *non-fatal* at the CLI, so `podbench dbg` attaches and prints the
   complaint while cpptools aborts. "Works in `dbg`, fails in VS Code" is this.
   `debug-config` now probes with `gdb -batch -ex file` before emitting a
   `cppdbg` entry — note that `gdb -batch` exits **0** either way and prints
   nothing at all on success, so the check reads the text, never the exit code.
   The remedy is the CodeLLDB entry (its own reader, no binutils) or a newer
   image; nothing in the launcher can fix it.
3. **The adapter is on the laptop**, as the previous section describes.

Causes 1 and 2 print the same BFD line, and telling them apart by reading is
impossible: **the path BFD names is the target's spelling either way**, because
it is the collapsed name. So run the `sha256sum` above rather than reading.

That ambiguity has already cost a round trip. #90 was filed as cause 2, then
"falsified" by running gdb on `/python/…/python3.11` in the seat and watching it
resolve symbols fine — except that command opened the *seat's* inode, so it
tested nothing. Both readings were wrong: the answer was cause 1, which neither
the filing nor the falsification had considered.

## The lowest pid is the entrypoint script, not the workload

Most images start `ENTRYPOINT ["/start.sh"]`, so pid 1 in the target container is
`bash` — no debug info, and breaking in it stops the supervisor rather than the
app. A real EPICS IOC pod ran `bash` (1) → `python stdio-expose` (8) → `sh -c`
(11) → `ioc` (13), and the thing anybody wants is the *deepest*, which is the
only signal in `/proc` that separates a wrapper from a workload.

`podbench.proc.debug_candidates` sorts on that: shells (`sh`, `bash`, `dash`,
`ash`, `busybox`, and the `tini`/`dumb-init` shims) last, everything else deepest
first. Shells are dropped from `launch.json` where anything else ran — an entry
in the dropdown gets picked — but kept as the *fallback* target, since a `dev`
pod is legitimately a login shell and nothing else. Every surviving candidate
gets its own entry rather than one winning: two children of an entrypoint script
are usually two languages, so the flavours do not compete for the slot. An
explicit `--pid` is taken as given and is the only answer, which is the way back
to a shell.

## `pathMappings` / `sourceFileMap` is mode-dependent

Getting it wrong does not error — breakpoints simply never bind.

- **Observe mode (attach):** the target is a *different container*. The editor
  sees source through `/proc/<pid>/root`, the debuggee reports its own path, so
  a mapping is required — and it is the **mount namespace**, `/proc/<pid>/root`
  -> `/`, never a root guessed from `argv`. A guessed root is `/app/.venv/bin`
  for a console script, which holds no source, and the seat's own image
  installs under `/app/.venv` as well: same path, different file, so the wrong
  mapping *resolves* and the editor opens this container's copy of the
  workload's frame with no error at all (issue #112). Same collision as the
  `/python/cpython-*` one below, arriving through debugpy instead of BFD.
- **Dev mode:** editor and interpreter are the same container and the same
  inodes, so mappings should be **empty**. A spurious one is another silent
  wrong answer.

`127.0.0.1` is always right for reaching the app from the seat: they share the
pod's network namespace, so no port-forward or tunnel is involved even though
they are separate containers.

## Anything that shells out to `gdb --pid` in a seat is broken twice

Both bugs are worth recognising because other tools hit them, not just the two
below.

1. **A cwd that no longer exists kills gdb.** gdb links libpython; anything that
   sends `-enable-pretty-printing` initialises CPython, whose init calls
   `getcwd()`. A deleted cwd then kills gdb *during startup* with no signal name
   and no backtrace. cpptools inherits its own extension directory as cwd, and
   VS Code replaces that directory on extension update. Fixed by
   `image/bin/gdb-podbench`; `podbench dbg` never hit it because it never enables
   pretty-printing. Reproduce anywhere with:

   ```sh
   mkdir -p /tmp/gone && cd /tmp/gone && rmdir /tmp/gone
   printf -- "-enable-pretty-printing\n-gdb-exit\n" | gdb --interpreter=mi
   ```

2. **No sysroot means the wrong libraries.** A bare `gdb --pid <n>` reads the
   *seat's* libraries for the *target's* process (`Error while mapping shared
   library sections`). It must be `-iex "set sysroot /proc/<pid>/root"`, not
   `-ex`: `--pid` attaches during startup, so `-ex` runs too late — the same
   sysroot-before-attach ordering report §3.3 made load-bearing.

## debugpy pid-injection needs four things

It works — proven on amd64 against an uncooperative `python:3.12-slim` — but
never out of the box. Architecture is the *third* obstacle, not the first.

1. debugpy on the driver side. The image has no `pip`; use `uv venv` + `uv pip`.
2. debugpy importable by the **target**, since the bootstrap runs in its
   interpreter.
3. `PYTHONPATH=/proc/<pid>/root/<target-site-packages>` so the driver loads the
   *target's* debugpy. debugpy injects a `dlopen` of the path **the driver
   sees**, and driver and target share a PID namespace but **not a mount
   namespace**. `/proc/<pid>/root` is the one spelling valid on both sides.
4. A `gdb` shim on `PATH` adding `-iex "set sysroot ..."` — see above.

`debugpy.listen()` baked into an app is pure Python and works anywhere.
**Injection into an uncooperative process is amd64-only**: debugpy ships
`attach_linux_amd64.so` and no arm64 equivalent, and publishes no aarch64 Linux
wheels at all.

## Cores do not exist in a pod

`core_pattern` is the **host's** (`|/usr/share/apport/apport …` on Ubuntu) and is
not namespaced, so apport is absent in the container and every core is piped
into nothing. "core dumped" leaves no core anywhere — do not go looking.
