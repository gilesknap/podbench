---
name: gdb-across-namespaces
description: Why gdb in a seat reads the wrong files for a target in another mount namespace, and the two failure modes — one loud, one silent and plausible. Read before touching execfile.py, gdbcmd.py, image/bin/gdb-podbench, or anything that builds a gdb argv or a cppdbg configuration.
---

# gdb across two mount namespaces

A seat and its target share a PID namespace but **not** a mount namespace. Every path gdb
resolves is therefore ambiguous: `/python/…/python3.11` names one file in the target and a
different one in the seat, and gdb will happily read either. Everything in this file is a
consequence of that, and all of it was measured on a real cluster — the wrong answers here
are plausible, so reasoning has a poor record.

## `-iex`, never `-ex`, for anything that must precede the attach

`--pid` attaches **during startup**. An `-ex` command runs after startup, which is after
the attach, which is too late:

```sh
gdb -ex  "set sysroot /proc/13/root" --pid 13   # wrong: libraries already resolved
gdb -iex "set sysroot /proc/13/root" --pid 13   # right
```

Without the sysroot, a bare attach loads *this* container's libc for *that* container's
process — sometimes an error, and otherwise a plausible and wrong backtrace (report §3.3).
The same ordering applies to the `file` command below, for the same reason.

## The sysroot is silently erased for the executable, and only the executable

This is issue #90, and it is the one that cost the most.

gdb finds the exec file from `/proc/<pid>/exe` and **canonicalises that one name**. The
kernel answers `readlink("/proc/13/root") = "/"`, so the sysroot is stripped out of the
path, and BFD reopens the bare name in the **seat's** filesystem. Shared libraries go
through a different resolution path and stay sysrooted correctly in the same run — which
is exactly what makes it so hard to see: `show sysroot` is right, `info sharedlibrary` is
right, and the executable is wrong.

podbench's image and any uv-managed workload both install an interpreter at
`/python/cpython-<version>-<triple>/`, so **a Python target collides by construction**.

### Two failure modes, and only one of them is loud

| how gdb was invoked | what happens |
|---|---|
| explicit `/proc/<pid>/root/...` path | **loud**: `.gnu.version_r invalid entry`, `Can't read symbols`, `bad value` |
| bare `gdb -p <n>` with a sysroot | **silent**: reads the seat's binary, resolves `PyGILState_Ensure@plt` at a wrong address, no error at all |

The loud one is the text #90 was filed with. The silent one is worse and was found only by
`cmp`-ing the two files. Never conclude the exec file is fine because gdb printed no error.

### The check that settles it, and the one that does not

Running gdb on the interpreter path **inside the seat** proves nothing — that reads the
seat's own copy. This is not hypothetical: it is precisely the command that "falsified" the
BFD-too-old hypothesis in the field, and the falsification was itself the bug.

Compare the inodes instead, then read each explicitly:

```sh
cmp /proc/<pid>/root/python/cpython-*/bin/python3.11 /python/cpython-*/bin/python3.11
md5sum /proc/<pid>/root/python/cpython-*/bin/python3.11    # must match what gdb staged
```

The bytes are usually *not* the discriminator either — the same file at a `/tmp` path
parses fine while the identical bytes under `/proc/<pid>/root/` fail. It is the path.

## The cure is a path nothing of ours shadows

`execfile.py` answers "same mount namespace? anything of ours at that path? copy where?"
and stages the target's bytes somewhere unshadowed — 21 MB in 15 ms, measured. It is asked
of podbench rather than reimplemented in `sh` (`image/README.md` deviation 2), because a
second implementation could only drift.

It has to be wired into **every** place podbench names an exec file, and there are three:

- `podbench dbg`
- `image/bin/gdb-podbench`, via `podbench dbg <pid> --print-startup-commands`,
  which carries the exec file as one line of the whole pre-attach sequence
- `debug-config`'s cppdbg `program`

The third is not optional and cannot be fixed any other way: **cpptools sends `program` as
`-file-exec-and-symbols` before any `setupCommands` run**, so no gdb command you add to a
launch configuration executes early enough to correct it.

Targets that shadow nothing are untouched, and report §4.3's sequence stays byte-identical
for them.

## The wrapper is on `PATH` as `gdb`, so it is not only cpptools calling it

`image/bin/gdb-podbench` also works around a cwd that no longer exists (VS Code replaces
its extension directory under a running cpptools; gdb links libpython, `getcwd()` fails,
and gdb dies during startup with no signal name and no backtrace). Read its header before
editing — each of its three blocks names the failure it prevents, and each of those
failures reports something that points somewhere else.

Two details there that look like tidy-ups and are not:

- the liveness test is `-d /proc/<pid>`, deliberately **not** `-e /proc/<pid>/root`.
  Resolving that symlink needs `PTRACE_MODE_READ`, so on the degraded rung it fails for a
  process that exists — and the wrapper would then skip the sysroot exactly where it
  matters most, silently.
- `/bin/pwd`, not the `pwd` builtin: dash's builtin answers from `$PWD` and reports success
  for a directory that has been unlinked, which is the case being detected.

## Symbols are needed for more than backtraces

debugpy's pid injection drives gdb to evaluate `call (void*)dlopen(…)` and then
`call (int)DoAttach(…)` in the inferior. gdb cannot call a function whose symbols it
refused to read, so a symbol failure takes out **injection, `podbench dbg`, and every
`PyGILState_Ensure` / `PyRun_SimpleString` recipe** at once — not just the pretty
backtrace. That is why `program_load_error` has to withdraw the debugpy flavour and not
only the gdb one.

pydevd's injector is gdb-only on Linux (`add_code_to_python_process.py`; the lldb branch is
macOS), so adding lldb to the image would not give injection a second route.

## Related

`docs/how-to/debug-with-gdb.md`, `docs/explanations/spikes/phase0-report.md` §3.3 and §4.3,
the `ephemeral-containers` skill for what the ladder may author, and `k3s-test-bed` for
where to reproduce any of this.
