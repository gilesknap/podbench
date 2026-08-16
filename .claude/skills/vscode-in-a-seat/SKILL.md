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
  a 256Mi limit has no chance; `--resize` first.
- Diagnose after the fact with
  `kubectl get pod -o jsonpath='{.status.ephemeralContainerStatuses[*].state}'` —
  `exitCode 137, reason OOMKilled`. `podbench status` says
  *"OOMKilled: name burnt for this pod's lifetime"*.

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

## Extensions must install in the *remote* window

The button has to read "Install in SSH: `<alias>`". A locally-installed
extension runs the debug adapter on the developer's laptop, where none of the
`/proc/<pid>/root` paths mean anything, and the failure looks like a bad
`launch.json`.

## `pathMappings` / `sourceFileMap` is mode-dependent

Getting it wrong does not error — breakpoints simply never bind.

- **Observe mode (attach):** the target is a *different container*. The editor
  sees source through `/proc/<pid>/root`, the debuggee reports its own path, so
  a mapping is required — e.g. `/proc/1/root/src` -> `/src`.
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
