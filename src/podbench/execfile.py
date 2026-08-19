"""Which file gdb is told to read symbols from, and why it cannot be the
obvious one.

The obvious one is ``/proc/<pid>/root`` + the target's ``exe``, and it is what
:func:`podbench.gdbcmd.attach_commands` emitted until issue #90. gdb
canonicalises the exec file's name — and only the exec file's; shared libraries
go through a different path and stay sysrooted, measured in the same failing run
that had libc at ``/proc/12/root/lib/x86_64-linux-gnu/libc.so.6``. The kernel
answers ``readlink("/proc/<pid>/root")`` with ``/``, so canonicalising
``/proc/12/root/python/cpython-3.11.15-linux-x86_64-gnu/bin/python3.11`` yields
``/python/cpython-3.11.15-linux-x86_64-gnu/bin/python3.11``, and BFD reopens
*that* — in the seat's own mount namespace. ``strace`` of the failing case shows
the three opens, the last of them on the collapsed name::

    openat(AT_FDCWD, "/proc/12/root/python/.../python3.11", O_RDONLY|O_CLOEXEC)
    openat(AT_FDCWD, "/proc/12/root/python/.../python3.11", O_RDONLY|O_CLOEXEC)
    openat(AT_FDCWD, "/python/.../python3.11", O_RDONLY)

The sysroot is therefore not ignored — it is *erased*, by a canonicalisation the
seat cannot turn off. What happens next depends on what the seat keeps at that
path, and all three outcomes were measured against a reconstruction of the
Diamond IOC (``tests/e2e/apps/dls-ioc.yaml``):

* **A different file** — the two builds' section tables and string tables are
  mixed, and BFD gives up with ``invalid string offset 65537 >= 40317 for
  section `.dynstr'`` / ``.gnu.version_r invalid entry``. That is issue #90's
  text, and it is the *lucky* case, because it is loud.
* **A different file that is close enough not to error** — gdb silently loads
  the seat's binary. The run that reported #90 also reported ``Entry point:
  0x43c144``, the seat interpreter's, while attached to a process running
  ``0x43bba4``. Report 3.3's "plausible backtrace, wrong symbols" arriving by a
  route that ``set sysroot`` before ``attach`` does not close.
* **Nothing at that path** — gdb keeps the sysroot path and reads the target's
  file correctly.

The third outcome is the cure: give gdb a path that no file of ours shadows.
Copying the target's exec file to a seat-local name and handing gdb *that* was
measured to restore the whole session — symbols, a relocated
``PyGILState_Ensure``, and a Python backtrace — with ``set sysroot`` still in
force and the shared libraries still resolving through it.

Two things make this podbench's problem rather than a curiosity. The seat image
and any uv-managed workload both install an interpreter at
``/python/cpython-<version>-<triple>/``, so a Python target collides *by
construction* and does not have to be unlucky. And the collision is what
falsified the first field diagnosis: running the seat's gdb on
``/python/.../python3.11`` and watching it resolve ``PyGILState_Ensure``
perfectly reads as "the binary is fine, BFD is not too old" — but that command
opened the seat's inode, never the target's.

**The cure is gdb's, and does not transfer to lldb.** That was measured rather
than assumed, because the two debuggers fail the same way for different
reasons. gdb canonicalises *the path it is given*, so a path nothing shadows is
enough. lldb **ignores** the path it is given: after the attach it re-resolves
the executable from the process and overrides the target's exec file with the
name as resolved in the seat's own mount namespace, announcing it as ``warning:
Executable binary changed from "/tmp/podbench-exe/<pid>/victim" to
"/app/victim"``. A staged copy is loaded and then dropped, so there is no path
to hand it that survives; ``settings set target.exec-search-paths`` does not
help either, and moving the seat's own file aside does — the same mechanism, in
a debugger with no remedy for it. So :func:`shadowing_file` is asked twice for
two opposite conclusions: gdb gets a copy, and ``flavour._assess_lldb``
withdraws the CodeLLDB configuration entirely, on the rule that a configuration
is worth emitting only where it debugs the user's own code. lldb is *louder*
than gdb here — it warns — but the warning lands in a debug console and the
session carries the seat's symbols regardless.

Measured 2026-08-19 on the k3s bed with **standalone lldb 21.1.8**, against a
real mount namespace (a chroot does not reproduce this: the kernel then answers
``readlink("/proc/<pid>/root")`` with an expressible path and canonicalisation
is correct). **CodeLLDB's own bundled lldb, running in a remote extension host,
was not observed** — there was no VS Code client in that run. What has not been
tried is ``platform select remote-linux`` against an lldb-server started inside
the target's namespace, which is the shape that would resolve the executable in
the right namespace to begin with.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from .proc import DEFAULT_PROC, same_root, strip_deleted, sysroot_path

__all__ = [
    "DEFAULT_STAGING",
    "gdb_exec_file",
    "shadowing_file",
    "stage_exec_file",
]

DEFAULT_STAGING = Path("/tmp/podbench-exe")
"""Where a shadowed exec file is copied to, one directory per pid.

Inside the seat, so it counts against the pod's shared ephemeral-storage budget
(report 3.9) — 21 MB and 15 ms for the CPython interpreter this was measured
against. The basename is kept because gdb prints this path back at the user in
``info files`` and in every "Symbols from" line.

The seat's own ``/tmp``, not a shared one: an ephemeral container has its own
writable layer, so this is not a path any other container in the pod can reach.
:func:`stage_exec_file` still creates it ``0o700`` — see the comment there.
"""


def shadowing_file(pid: int, exe: str, *, proc: Path = DEFAULT_PROC) -> str | None:
    """The file *of ours* that gdb would read instead of the target's, if any.

    >>> shadowing_file(597, "/app/victim", proc=Path("/nonexistent")) is None
    True

    The answer is a path rather than a bool so the warning can name it: it is
    the path BFD prints, which is what makes issue #90's message look like a
    complaint about the target's binary. It is also what the lldb refusal names,
    since there the path is the whole of the explanation and there is no staged
    copy to point at instead.

    "gdb" in the summary line is the caller this was written for and no longer
    the only one — lldb reads the same wrong file for a different reason, and
    the module docstring says why one of them can be worked around and the
    other cannot.
    """
    path = strip_deleted(exe)
    if same_root(pid, proc=proc):
        # A `dev` pod debugs a process in its own container, where the collapse
        # lands back on the very file gdb opened. Nothing to work around, and
        # staging a copy would cost a needless megabyte per debug session.
        return None
    # Asked through `/proc/self/root` rather than of `path` directly. It is the
    # same view — our own rootfs — but it keeps the whole decision behind the
    # `proc` seam, so a unit test builds the collision instead of depending on
    # whatever the machine running the suite happens to keep at /app/victim.
    #
    # String concatenation, never a path join: `path` is absolute, and joining
    # would discard the prefix and quietly ask the question about a third file.
    #
    # `lexists`, not `exists`: a dangling symlink of ours still shadows, and a
    # symlink to something else of ours shadows with a different file again.
    return path if os.path.lexists(f"{proc}/self/root{path}") else None


def stage_exec_file(
    pid: int,
    exe: str,
    *,
    proc: Path = DEFAULT_PROC,
    staging: Path = DEFAULT_STAGING,
) -> str:
    """Copy the target's exec file where no file of ours can shadow it.

    Raises ``OSError`` — the caller has a usable answer without it (the sysroot
    path, misread but honest about being misread), so this must not be the
    thing that ends a debugging session.
    """
    path = strip_deleted(exe)
    destination = staging / str(pid) / PurePosixPath(path).name
    # 0o700 on both levels. Nothing in a seat has any business reading the
    # target's binary through us that could not read it through
    # `/proc/<pid>/root` already, and the name is fixed and lives under /tmp,
    # so the default umask would let a second process in the seat replace what
    # gdb is about to open. `exist_ok=True` skips the mode when the directory
    # is already there, hence the explicit chmod after it.
    staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    for directory in (staging, destination.parent):
        directory.chmod(0o700)
    # Copied on every call rather than reused when one is already there: pids
    # are reused, and a seat that outlives the process it staged for would
    # otherwise hand gdb an old binary at the right path — the exact failure
    # this module exists to end, arriving from our own cache.
    #
    # Written to a temporary name and renamed, so a gdb already reading the
    # previous copy keeps its own inode and a failed copy cannot leave a
    # truncated ELF at a name something else will trust.
    handle, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".staging-")
    os.close(handle)
    try:
        shutil.copyfile(f"{proc}/{pid}/root{path}", temporary)
        os.replace(temporary, destination)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return str(destination)


def gdb_exec_file(
    pid: int,
    exe: str,
    *,
    proc: Path = DEFAULT_PROC,
    staging: Path = DEFAULT_STAGING,
) -> tuple[str, list[str]]:
    """The path to give gdb's ``file`` command, and what to say about it.

    >>> gdb_exec_file(597, "/app/victim (deleted)", proc=Path("/nonexistent"))
    ('/proc/597/root/app/victim', [])

    The sysroot form is returned unchanged wherever nothing shadows it, which is
    every target that does not share a base-image convention with the seat. Only
    the collision is worked around, and it is worked around out loud: the note
    is one line, and it names the file of ours that would have been read.
    """
    path = strip_deleted(exe)
    through_sysroot = f"{sysroot_path(pid)}{path}"
    shadow = shadowing_file(pid, exe, proc=proc)
    if shadow is None:
        return through_sysroot, []
    try:
        staged = stage_exec_file(pid, exe, proc=proc, staging=staging)
    except OSError as error:
        return through_sysroot, [
            f"this container has its own {shadow}, which gdb reads instead of "
            f"the target's (issue #90); copying the target's aside failed "
            f"({error}), so expect `.gnu.version_r invalid entry` or, worse, "
            f"symbols from the wrong build"
        ]
    return staged, [
        f"this container has its own {shadow}, which gdb would read instead of "
        f"the target's (issue #90): its `file` command gets a copy of the "
        f"target's at {staged}"
    ]
