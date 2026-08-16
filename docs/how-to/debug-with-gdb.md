# Debug with gdb

Take a **distroless** container — no shell, no gdb, no libc headers, nothing to
exec into — and land on a breakpoint with source shown and locals readable. Ten
minutes, most of it image pulls.

:::{note}
Commands here are written `podbench <verb>` — the only spelling there is. If you
have not installed the launcher, run each as `uvx podbench <verb>`, or, before
the first PyPI release, as
`uvx --from git+https://github.com/gilesknap/podbench podbench <verb>`. See
[Installation](../tutorials/installation.md).
:::

:::{warning}
**A breakpoint on a probed pod is on a timer.** A process stopped in a debugger
does not answer its probes, and the kubelet cannot tell that from a hang. Two
deadlines follow, and the quiet one is the one that will catch you out. Both are
`(failureThreshold - 1) x periodSeconds + timeoutSeconds` after the pause
begins, plus up to one more period depending on where in the cycle it began:

| deadline | what happens | how visible |
|---|---|---|
| `readinessProbe` | the pod goes not-ready and stops taking Service traffic — the address stays in the EndpointSlice with `conditions.ready: false` | **quiet** — nothing restarts, and it recovers a probe period after you continue, so afterwards nothing points at the debugger |
| `livenessProbe` | the container is killed and restarted, and the seat — which shares its namespaces — is killed with it | loud — an event, a bumped restart count, and a burnt seat name: an ephemeral container cannot be restarted, so coming back needs `attach --new` |

Measured against `tests/e2e/apps/python-service.yaml` (readiness every 5 s,
liveness every 10 s, both `failureThreshold: 3`, both `timeoutSeconds: 1`, so
11–16 s and 21–31 s): a gdb attach held 18 s took the pod out of the Service
after ~12 s and put it back 5 s after `detach`, with no restart. Held 45 s, it
also produced `Container app failed liveness probe, will be restarted` at ~25 s
and `exitCode: 137` on both the workload *and* the seat.

`podbench attach` prints these numbers for the pod you name, computed from its
own spec — including the opposite answer, "no probes, no deadline", when the
target has none.

**Probes cannot be turned off on a running pod.** A pod update may change only
`containers[*].image`, `initContainers[*].image`, `activeDeadlineSeconds`,
`tolerations` and `terminationGracePeriodSeconds`; unlike `resources` — the
asymmetry that makes `--resize` possible — probes have no resize-style
subresource. So live attach on a probed pod is a **short-visit** tool: break,
look, continue. Logpoints and conditional breakpoints never stop the process.
For an unlimited pause use [`podbench dev`](iterate-on-python.md), which strips
all three probes by construction.
:::

Everything here is driven by `podbench dbg`, run from a terminal in the seat.
`podbench dbg` is not `gdb -p`: it fixes seven commands in one order, and the
order is a correctness property rather than a preference. Setting the sysroot
*after* attaching gives you a backtrace that looks entirely believable and is
wrong.

## 1. A distroless target

An initContainer compiles the program into an `emptyDir`; the distroless
container runs it. No local build, no registry.

```
$ kubectl create namespace podbench-gdb
$ kubectl -n podbench-gdb apply -f - <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: victim-src
data:
  victim.c: |
    #include <stdio.h>
    #include <stdlib.h>
    #include <string.h>
    #include <unistd.h>
    #include <math.h>

    struct work { int id; double value; char label[32]; };

    static double transform(struct work *w) {
      double r = sqrt((double)w->id) * w->value;
      return r;
    }
    static int compute(int n, struct work *w) {
      w->id = n;
      w->value = (double)n * 1.5;
      snprintf(w->label, sizeof(w->label), "item-%d", n);
      double t = transform(w);
      return (int)t;
    }
    static void outer_loop(void) {
      struct work w; int i = 0;
      for (;;) {
        memset(&w, 0, sizeof(w));
        int v = compute(i, &w);
        printf("tick %d -> %d (%s)\n", i, v, w.label); fflush(stdout);
        i++; sleep(2);
      }
    }
    int main(int argc, char **argv) {
      printf("victim starting, pid=%d\n", (int)getpid()); fflush(stdout);
      outer_loop(); return 0;
    }
---
apiVersion: v1
kind: Pod
metadata:
  name: victim
  labels: {app: victim}
spec:
  restartPolicy: Never
  volumes:
    - {name: app, emptyDir: {}}
    - {name: src, configMap: {name: victim-src}}
  initContainers:
    - name: build
      image: debian:bookworm-slim
      command: ["/bin/sh", "-c"]
      args:
        - |
          set -ex
          apt-get update -qq
          apt-get install -y -qq --no-install-recommends gcc libc6-dev
          mkdir -p /app/src
          cp /src/victim.c /app/src/victim.c
          cd /app/src && gcc -g -O0 -o /app/victim victim.c -lm
      volumeMounts:
        - {name: app, mountPath: /app}
        - {name: src, mountPath: /src}
  containers:
    - name: victim
      image: gcr.io/distroless/cc-debian12
      command: ["/app/victim"]
      volumeMounts: [{name: app, mountPath: /app}]
YAML
$ kubectl -n podbench-gdb wait --for=condition=Ready pod/victim --timeout=300s
```

Confirm there is genuinely no shell in there:

```
$ kubectl -n podbench-gdb exec victim -c victim -- /bin/sh -c 'echo hi'
error: Internal error occurred: error executing command in container:
failed to exec in container: ... exec: "/bin/sh": stat /bin/sh: no such file or directory
```

Note the source is left at `/app/src/victim.c` **inside the target's rootfs**.
That is not incidental — see *Where source text actually comes from* below.

## 2. Attach the seat

```
$ podbench attach pod/victim -n podbench-gdb
```

Check the report says `[x] live attach`. If it does not, skip to
*Without SYS_PTRACE* below — you still get source-level
debugging, just not of the already-running process.

Then ssh in with the alias it printed.

## 3. Find the process

```
$ ssh podbench-podbench-gdb-victim
root@victim:~# podbench pids
PID  UID  TARGET  CONTAINER      COMM    CMDLINE
1    0    yes     87d20e23a1b4   victim  /app/victim
38   0    no      7206c89bf0e1   sleep   sleep infinity
```

`podbench pids` is not `ps`. Under a shared PID namespace every process in the
pod is visible — including other podbench sessions' — so attribution keys
off the target's container runtime ID, which the launcher injected as
`PODBENCH_TARGET_CID`. The rules that look obvious are all wrong: "the target is
PID 1" breaks under `shareProcessNamespace: true` (PID 1 is `/pause`), and
matching mount namespaces breaks there too.

If the `TARGET` column is a guess rather than a fact, `podbench pids` says so.

## 4. Attach gdb

```
root@victim:~# podbench dbg 1
```

With no argument at all, `podbench dbg` discovers the pid from the target
container ID. Before starting gdb it runs the capability probe, so if attach is
going to be denied you are told *which mechanism* denies it rather than being
handed an `EPERM`.

What it feeds gdb, in this order — see it without starting gdb using
`podbench dbg --dry-run 1`:

```
set pagination off
set sysroot /proc/1/root
directory /proc/1/root
add-auto-load-safe-path /proc/1/root
set debuginfod enabled on
file /proc/1/root/app/victim
attach 1
```

Every line earns its place:

| Command | Why |
|---|---|
| `set sysroot /proc/<pid>/root` **before** `attach` | gdb resolves the *target's* loader and shared libraries, not the debug image's. gdb 13's default sysroot is `target:`, which needs `CAP_SYS_ADMIN` and fails loudly without it |
| `directory /proc/<pid>/root` | sysroot does **not** cover source lookup. This is what turns `victim.c: No such file or directory` into real source text |
| `add-auto-load-safe-path /proc/<pid>/root` | setting a sysroot makes gdb decline to auto-load the target's `libthread_db.so.1` — no `info threads`, no per-thread backtraces. Narrow, never `set auto-load safe-path /` |
| `set debuginfod enabled on` | symbols for stripped binaries and system libraries. Symbols only — see below |
| `file /proc/<pid>/root$(readlink /proc/<pid>/exe)` **before** `attach` | this is what recovers the *user* frames. A trailing ` (deleted)` is stripped |

## 5. Breakpoint, source, step

`victim` declares no probes, so this pause is unlimited and you can take as long
over it as you like — `podbench attach` said as much under `supports`. On a pod
that *does* carry probes, read the timer warning at the top of this page before
you break anywhere.

```
(gdb) break compute
Breakpoint 1 at 0x575fbad351f4: file victim.c, line 19.
(gdb) continue
Continuing.

Breakpoint 1, compute (n=23, w=0x7ffc633f7600) at victim.c:19
19	      w->id = n;
(gdb) bt
#0  compute (n=23, w=0x7ffc633f7600) at victim.c:19
#1  0x0000575fbad35297 in outer_loop () at victim.c:31
#2  0x0000575fbad3531b in main (argc=1, argv=0x7ffc633f7778) at victim.c:42
(gdb) next
20	      w->value = (double)n * 1.5;
(gdb) print *w
$1 = {id = 23, value = 0, label = '\000' <repeats 31 times>}
(gdb) info source
Current source file is victim.c
Compilation directory is /app/src
Located in /proc/1/root/app/src/victim.c
```

That last block is the check that matters. `Located in
/proc/1/root/app/src/victim.c` is a single, clean sysroot prefix — the field a
DAP client hands to the editor.

Detach and leave the workload running:

```
(gdb) detach
(gdb) quit
```

## Where source text actually comes from

Be clear-eyed about this: **debuginfod delivers symbols, not sources, on Debian
and Ubuntu targets.** Both halves of that were measured.

* Symbols work, and they are cheap: a fully symbolised, source-line-annotated
  backtrace across coreutils *and* glibc for a stripped binary cost **4.7 MB**
  of `~/.cache/debuginfod_client`, and it works *through* the sysroot including
  for distroless libraries.
* Sources fail, twice over, and both causes are fatal on their own:

  ```
  Download failed: Invalid argument.  Continuing without source file ./src/sleep.c.
  142	src/sleep.c: Inappropriate ioctl for device.
  ```

  Debian's `-dbgsym` packages carry `DW_AT_comp_dir : .` from
  reproducible-builds normalisation, and the debuginfod protocol requires an
  absolute path, so gdb's `./src/sleep.c` is rejected client-side with `EINVAL`.
  And the server has no sources anyway: `/buildid/<id>/debuginfo` returns
  **200**, `/buildid/<id>/source/src/sleep.c` returns **404**.

Fedora/RHEL debuginfod *is* known to serve sources. Debian and Ubuntu targets
will not. So source text has to come from one of these, and you have to pick
one deliberately:

1. **Source in the target image** — what the demo above does. Ship
   `/app/src` in the workload image (or on a volume) and `directory
   /proc/<pid>/root` finds it with no path mapping at all. Simplest, and the
   only option that needs nothing on the client.
2. **A checkout in the debug container.** Clone the source into the seat and
   point `podbench dbg` at it:

   ```
   root@victim:~# git clone https://github.com/you/app /workspace/src
   root@victim:~# podbench dbg --source-dir /workspace/src 1
   ```

   `--source-dir` is repeatable and is wired with gdb's `directory`. gdb searches
   the most recently added directory first, so your checkout wins over the
   target's rootfs.
3. **Client-side mapping** to a checkout on your laptop, via `sourceFileMap` in
   `launch.json` — see below. This is the only one that keeps a full clone off
   the pod, and it is also the least proven.

None of these is a finished design. Source provisioning for Observe mode is an
open problem, not a solved one.

## VS Code attach templates

These go in `.vscode/launch.json` **in the remote window** — the debug adapter
runs inside the debug container, next to gdb, so every path below is a path in
that container.

### Let podbench write it

Do not hand-copy the templates below unless you have to. In the seat:

```
root@victim:~# podbench debug-config
debug-config: native target, observe mode, x86_64
debug-config: emitting gdb: native target, observe mode
debug-config: emitting lldb: native target; CodeLLDB brings its own lldb to the seat
debug-config written to /root/.vscode/launch.json
  Run and Debug -> "podbench: attach to victim (gdb)"
  Run and Debug -> "podbench: attach to victim (lldb)"
```

It fills in the pid, the sysroot-prefixed `program`, the setup ordering, the
architecture, `miDebuggerPath` and the mode's path mappings from what it can
already see, which is the whole point: every one of those fails *silently* when
wrong.

**Every flavour that applies is emitted**, named for its debugger, because
`launch.json` holds a list and VS Code's dropdown is a better chooser than a
guess. A flavour that cannot be emitted gets a sentence naming the mechanism
instead — `--flavour gdb|lldb|delve|debugpy` asks for one by name and makes it
say why if it cannot. `--print-config` emits instead of writing, `--output` puts
it beside the folder you actually opened, and re-running replaces its own
entries and leaves any hand-written configuration alone.

For a **Python** target the answer is debugpy rather than gdb, and it depends on
the architecture as well as the language — the
[CLI reference](../reference/cli.md) has the three axes and what each one
changes.

VS Code reads `.vscode/launch.json` from the folder that is **open**, not from
`$HOME`. A config written to `~` when you opened `/` never appears in the Run
and Debug list, and nothing reports an error.

### C/C++ with `ms-vscode.cpptools` (gdb)

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "podbench: attach to the workload",
      "type": "cppdbg",
      "request": "attach",
      "processId": "1",
      "program": "/proc/1/root/app/victim",
      "cwd": "/root",
      "MIMode": "gdb",
      "miDebuggerPath": "/usr/local/bin/gdb-podbench",
      "targetArchitecture": "arm64",
      "setupCommands": [
        { "text": "set sysroot /proc/1/root" },
        { "text": "directory /proc/1/root" },
        { "text": "add-auto-load-safe-path /proc/1/root" },
        { "text": "set debuginfod enabled on" }
      ],
      "sourceFileMap": {
        "/app/src": "/proc/1/root/app/src"
      }
    }
  ]
}
```

Four things are load-bearing.

`program` must be the **sysroot-prefixed** path, or gdb reads the debug image's
idea of the binary. `setupCommands` run before the attach, which is the ordering
the CLI sequence depends on — do not move the sysroot line into a post-attach
hook.

`miDebuggerPath` must be **`/usr/local/bin/gdb-podbench`**, the image's wrapper,
and never `/usr/bin/gdb`. cpptools launches gdb as a child, so gdb inherits
cpptools' own working directory — its extension directory, which VS Code
replaces wholesale on extension update. gdb links libpython;
`-enable-pretty-printing` (which cpptools always sends) initialises CPython;
CPython calls `getcwd()`; and gdb dies during startup:

```
gdb: warning: error finding working directory: No such file or directory
Fatal signal:
A fatal error internal to GDB has been detected, further
debugging is not possible.  GDB will now terminate.
```

No signal name, no backtrace — it crashes before it can format either — and
VS Code surfaces only `ERROR: Unable to start debugging. GDB exited
unexpectedly`, which points at the attach rather than at startup. `podbench dbg`
never hits this because it never enables pretty-printing, so the CLI works
perfectly on a seat where the VS Code debugger cannot start at all. Reproduce it
in any seat with:

```
mkdir -p /tmp/gone && cd /tmp/gone && rmdir /tmp/gone
printf -- "-enable-pretty-printing\n-gdb-exit\n" | gdb --interpreter=mi
```

`cwd` must be set. On a developer's machine `${workspaceFolder}` always exists
so nobody sets it; in a seat it can resolve to nothing, and the result is that
same unformattable crash.

:::{note}
`targetArchitecture` is worth setting on arm64 — without it cpptools logs
`Debuggee TargetArchitecture not detected, assuming x86_64` — but it is **not**
what causes the crash above. It is a plausible-looking red herring that sits
directly next to the real symptom in the log.
:::

:::{warning}
The VS Code C++ extension consumes `info source`'s `fullname`, which is exactly
the field the wrong source-mapping approach corrupts, so if paths come out
doubled (`/proc/1/root/proc/1/root/…`) that is the failure to look for. Map the
compilation directory, never `/`.
:::

`sourceFileMap` keys are the compilation directories recorded in the DWARF
(`info source` prints them as *Compilation directory*); values are where those
directories can be read from *in the debug container*.

### Rust with `vadimcn.vscode-lldb` (CodeLLDB)

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "podbench: attach to the Rust workload",
      "type": "lldb",
      "request": "attach",
      "pid": "1",
      "program": "/proc/1/root/app/myapp",
      "sourceMap": {
        "/app/src": "/proc/1/root/app/src",
        "/rustc/<commit-hash>": "/workspace/rust-src/library"
      },
      "initCommands": [
        "settings set target.exec-search-paths /proc/1/root/usr/lib /proc/1/root/lib"
      ]
    }
  ]
}
```

CodeLLDB spells the key `sourceMap`, not `sourceFileMap`. lldb has no exact
analogue of gdb's `set sysroot` for `/proc/<pid>/root`, so the executable path
must be sysroot-prefixed explicitly and the library search paths set by hand.
The `/rustc/<commit-hash>` key is what `rustc` bakes into standard-library debug
info; `rustup component add rust-src` provides the right-hand side.

## The wrong-sysroot failure, verbatim

This is the most dangerous failure in this whole workflow, because it does not
look like a failure. `set sysroot /` against a target whose glibc differs from
the debug image's (here Ubuntu 24.04's 2.39 against the image's 2.36):

```
### WRONG: set sysroot /
0x000077f6377dcb7a in wcsxfrm_l () from /lib/x86_64-linux-gnu/libc.so.6
#2  0x00005c459789e35f in outer_loop () at victim.c:29        <- wrong line, too
#5  0x00005c45978a0d80 in __frame_dummy_init_array_entry ()

### RIGHT: set sysroot /proc/1/root
0x000077f6377dcb7a in clock_nanosleep () from /proc/1/root/lib/x86_64-linux-gnu/libc.so.6
#3  0x00005c459789e35c in outer_loop () at victim.c:35
```

`clock_nanosleep` reported as `wcsxfrm_l`, interleaved frames, and even the
*user-code line number* wrong. Against a Debian-12 distroless target the bug is
**invisible**, because both glibcs share a build ID and the backtrace comes out
correct — a matched debug image hides this rather than fixing it.
`podbench dbg` is what makes it impossible.

## Three anti-patterns

* **`CAP_SYS_ADMIN`.** It makes gdb's default `target:` sysroot work with zero
  configuration, which is exactly why someone rediscovers it every year. It also
  breaks `libthread_db` (`Expected absolute pathname for libpthread in the
  inferior, but got target:/lib/…`), is container-escape-adjacent and is
  rejected by any restricted Pod Security Standard. podbench never asks for it.
* **`set substitute-path / /proc/<pid>/root/`.** It functions, but gdb
  re-applies the substitution on display and emits
  `/proc/1/root/proc/1/root/proc/1/root/…` — which is the string a DAP client
  hands to your editor. Use `directory`.
* **`set sysroot` after `attach`.** Libraries get fixed up on the fly, so it
  looks like it worked; the main executable does not, and the frames above libc
  come back as `?? ()`.

## Without `SYS_PTRACE`

Losing the capability costs exactly one thing: attach to an **already running**
process. Full source-level debugging survives, because gdb starting the program
itself needs no capability and is exempt from Yama:

```
root@victim:/workspace# podbench dbg --launch ./myprog --some-flag
```

`--launch` consumes the rest of the command line, so put any other
`podbench dbg` flags before it. Add `--run` to start the program immediately.

`podbench dbg` will tell you this itself when attach is denied — it names the
mechanism and points at the alternative:

```
podbench dbg: cannot attach to pid 1: yama-scope
  Yama's ptrace_scope forbids attaching to a non-descendant...
  verdict: read-only inspection of the target; no live attach
  the target's rootfs, maps and environ are still readable, so `podbench pids`
  and read-only inspection work.
  ptrace-free alternative: `podbench dbg --launch ./yourprog [args]`. gdb forks the
  inferior itself, which needs no capability and is not subject to Yama.
  to keep attaching to this process, the target can opt in with one line:
  prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY).
```

That last line is worth knowing: the natural workflow `myprog & ; gdb -p $!`
makes gdb a **sibling**, which Yama denies at `ptrace_scope=1`. A sibling that
called `prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)` is attachable with zero
capabilities. It is a one-line, capability-free, node-change-free opt-in for a
program you control.

On a pod where the ptrace-gated reads went with the attach — the **launch-only**
verdict — the consolation line says *that* instead, because offering a sysroot
that will not open costs a second afternoon:

```
  verdict: launch-only: `podbench dbg --launch` works; no read-only inspection
  the reads that take PTRACE_MODE_READ went with it (cmdline, status and fd
  only; root, maps and environ denied), so a sysroot, `environ` or `maps` is not
  the fallback here.
```

The matrix is in the line because it is the honest form of the answer: `cmdline`,
`status` and `fd` are still readable — they always are — so `podbench pids` works
and "the target is closed" would be the same overclaim as issue #51, pointing
the other way.

`--launch` survives all of that: gdb forks the inferior and traces its own
descendant, which needs no capability and no Yama exemption. It is still
*measured* rather than assumed, and the tick beside **debug launched processes**
is the measurement — `capreport` attaches to a child it forked itself before it
claims the rung. Two things take it away with everything else: a seccomp filter
that rejects `ptrace(2)` outright, and `ptrace_scope=3`, neither of which cares
whose descendant the inferior is.

## Gotchas

* **ASLR cannot be disabled.** `RuntimeDefault` seccomp permits `ptrace(2)` but
  blocks `personality(ADDR_NO_RANDOMIZE)`, so gdb warns `Error disabling address
  space randomization` and addresses vary run to run. Set breakpoints by symbol.
* **Attach works on one node and not the next.** Yama's `ptrace_scope` is a
  node-level knob and differs by kernel *flavour*, not by architecture — two
  arm64 nodes in the same cluster disagreed in testing. podbench probes per
  node and prints the node name for this reason.
* **A target already being traced** refuses with the same `EPERM` as a policy
  denial, because a tracee has exactly one tracer. `podbench capreport` reports
  `already-traced` when it can tell.
* **Multithreaded targets are unproven.** `libthread_db` loaded and `info
  threads` listed the single LWP in testing; a genuinely multithreaded target
  was never tried. Nor was a target in a user namespace.
* **`elfutils`/`binutils` are in the image** — `readelf --debug-dump=info` and
  `eu-readelf` are how a build-ID miss gets diagnosed.

## Clean up

```
$ kubectl delete namespace podbench-gdb
```
