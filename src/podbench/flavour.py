"""Which debugger fits this target, and — when none does — which mechanism says no.

The choice is not "which language". It is **language x mode x architecture**,
and only the first is obvious (issue #20):

* **language** decides the adapter — cppdbg/gdb, CodeLLDB, delve, debugpy;
* **mode** decides its *shape*, because a ``dev`` pod's process is a child of
  this container and an Observe-mode target is a process in another container:
  the first is a launch and needs no sysroot, the second is an attach and needs
  one everywhere;
* **architecture** decides whether the mechanism exists at all. debugpy's
  attach-to-pid ships ``attach_linux_amd64.so`` and no arm64 equivalent, and
  debugpy publishes no aarch64 Linux wheel, so on arm64 there is nothing to
  install and nothing to fix.

Everything is measured rather than asked: :mod:`podbench.elf` reads the target's
own binary, ``/proc/<pid>/cmdline`` names the interpreter, ``/proc/<pid>/root``
says whether debugpy is importable *by the target*, and the seat's own
``/proc/self/status`` says whether it holds CAP_SYS_PTRACE.

That last one is the weakest of them, and only ever a fallback: the injection
needs *ptrace*, which the capability is one of four ways to have — a same-uid
attach under ``ptrace_scope=0`` needs none of it. So where an attach has really
been tried, :attr:`Seat.target_attach_ok` decides and the bit does not (issue
#89); where it has not, the bit still decides, because an unmeasured attach is
no evidence that attach works.

**Nothing here decides for the user.** :func:`assess` returns a verdict for
every flavour, so ``debug-config`` can emit each configuration that applies and
name the blocking mechanism for each that does not — the same house style as
``capreport``, and for the same reason: four different mechanisms produce the
same "it just doesn't work at F5", and only naming one of them saves the
afternoon.

## Why the debugpy prerequisites are in this order

The live proof in issue #20 established that on amd64 — where the helper ``.so``
does exist — pid-injection still fails twice before architecture matters:

1. debugpy tells gdb to ``dlopen`` its helper by the path *the driver* sees, and
   driver and target share a PID namespace but **not** a mount namespace. The
   one spelling valid on both sides is ``/proc/<pid>/root/...``, which is why
   the prerequisite is "debugpy importable by the target" and not merely
   "debugpy installed here".
2. debugpy invokes a bare ``gdb --nx --pid <n>``, which reads the *seat's*
   libraries for the *target's* process. That is what ``image/bin/gdb-podbench``
   fixes with ``-iex "set sysroot /proc/<pid>/root"``.

So unmet prerequisites are reported in that order. The one exception is the
architecture check, which is promoted to the headline whenever it fails: every
other prerequisite has a remedy inside this pod and that one has none.
"""

from __future__ import annotations

import enum
import os
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .elf import ElfInfo, debugpy_helper_name, debugpy_helper_published, read_elf
from .proc import (
    DEFAULT_PROC,
    read_cmdline,
    read_comm,
    read_exe,
    same_root,
    self_capabilities,
    sysroot_path,
)
from .provision import PROVISION_DEST, provision_paste

__all__ = [
    "DEBUGPY_PORT",
    "SEAT_DEBUGPY_PATH",
    "SEAT_PYTHON",
    "Assessment",
    "Debugger",
    "Flavour",
    "Language",
    "Mode",
    "Seat",
    "Target",
    "Which",
    "assess",
    "can_ptrace_target",
    "injection_command",
    "detect_mode",
    "format_inventory",
    "inspect_target",
    "inventory",
    "survey_seat",
]

DEBUGPY_PORT = 5678
"""debugpy's own default, and what every published launch.json example uses.

``127.0.0.1`` goes with it: the seat and the app share the pod's network
namespace, so nothing is port-forwarded or tunnelled even though they are
separate containers.
"""

SEAT_DEBUGPY_PATH = "/opt/podbench/debugpy"
"""Where the image installs debugpy, deliberately *not* in podbench's venv.

Two reasons it is a directory of its own. podbench's wheel has exactly one
runtime dependency and that is the CLI, an invariant ``tests/test_packaging.py``
asserts; and this copy has to be put on ``PYTHONPATH`` by hand for the injection
recipe, which is easier to state when it is one path rather than a venv's
site-packages.
"""

SEAT_PYTHON = "/app/.venv/bin/python"
"""The interpreter that drives the injection, named in full rather than found.

The seat ships two: this venv, and the uv-managed CPython under
``/python/cpython-<version>-<triple>/`` that ``UV_PYTHON_INSTALL_DIR`` points at
so a workspace ``uv venv`` does not download a third. Only one of them can run
the driver — the image resolves debugpy against this one
(``uv pip install --python /app/.venv/bin/python --target``
:data:`SEAT_DEBUGPY_PATH`) — and a bare ``python`` names whichever of the two a
``PATH`` happens to reach.

That ``PATH`` is not ours to assume. The transport now carries the image's own
(:data:`podbench.agent.SESSION_ENV_NAMES`), but ``SetEnv`` is a directive an
sshd may refuse, and the recipe is printed to be *pasted* — into a
``kubectl exec``, into a shell the launcher never saw, into a bug report. An
absolute path costs nothing and resolves the same way in all of them.
"""

_HELPER_RELATIVE = "debugpy/_vendored/pydevd/pydevd_attach_to_process"
"""Where debugpy keeps the per-architecture attach helpers inside its package."""

#: Interpreters worth recognising in ``/proc/<pid>/exe`` or ``argv[0]``. The
#: value is what the process *is*, which is a different question from what its
#: script is written in — a CPython running a C extension is still Python here,
#: because the adapter that can see its frames is debugpy's.
_INTERPRETERS = (
    ("python", "python"),
    ("pypy", "python"),
    ("node", "node"),
    ("nodejs", "node"),
)

_SEARCH_ROOTS = (
    "usr/local/lib",
    "usr/lib",
)
"""Where a Debian- or python-image-shaped rootfs keeps site-packages.

Two fixed prefixes rather than a walk: ``/proc/<pid>/root`` is another
container's whole filesystem, and an unbounded walk through it is exactly the
recursive descent that OOMs a seat (skill: vscode-in-a-seat).
"""


class Flavour(enum.Enum):
    """A debugger, in the spelling ``--flavour`` accepts."""

    GDB = "gdb"
    """cpptools' ``cppdbg`` driving gdb. Native code, and podbench's default."""

    LLDB = "lldb"
    """CodeLLDB. Same targets as gdb; the right answer for Rust."""

    DELVE = "delve"
    """The Go extension's ``dlv``. Goroutines and channels are its job."""

    DEBUGPY = "debugpy"
    """Python. The only flavour whose availability turns on architecture."""


class Language(enum.Enum):
    """What the target process runs, as far as a debugger is concerned."""

    NATIVE = "native"
    GO = "go"
    PYTHON = "python"
    NODE = "node"
    UNKNOWN = "unknown"


class Mode(enum.Enum):
    """Whose process is being debugged, which decides the config's shape."""

    OBSERVE = "observe"
    """A process in another container: attach, sysroot, path mappings."""

    DEV = "dev"
    """A process in *this* container — a ``podbench dev`` pod, where the seat
    owns PID 1. Launch rather than attach, and no mappings at all, because the
    editor and the interpreter share the same inodes."""


@dataclass(frozen=True)
class Target:
    """What could be learnt about the process being debugged."""

    pid: int
    language: Language
    program: str | None
    """The executable as the *target's own rootfs* spells it, unprefixed."""

    cmdline: str = ""
    elf: ElfInfo | None = None
    interpreter: str | None = None
    script: str | None = None
    """The first non-flag argument after an interpreter — the file a Python
    developer thinks of as "the program"."""

    python_version: str | None = None
    """The target's CPython ``X.Y``, when it is a CPython.

    Measured because it is what uv resolves against: the seat's image is 3.11
    and the target on the bench was 3.12, and a wheel built for the wrong one
    still imports — pydevd just finds no matching ``pydevd_cython`` accelerator
    and falls back to pure Python, silently (issue #45).
    """

    cwd: str | None = None
    """The target's working directory, in its *own* rootfs's spelling."""

    comm: str | None = None
    """The kernel's own name for the process, or ``None`` if it was unreadable.

    Kept beside :attr:`program` because it is the half of the identity that
    distinguishes two candidates a launch.json entry name would otherwise
    confuse: three children of one entrypoint script are all ``python``,
    and their :attr:`name` is the same word three times.
    """

    notes: tuple[str, ...] = ()

    @property
    def machine(self) -> str | None:
        """The architecture the *binary* was built for.

        The binary rather than the node: a pod can run an amd64 image on an
        arm64 node under emulation, and the node label would then answer a
        question nobody asked.
        """
        return None if self.elf is None else self.elf.machine

    @property
    def name(self) -> str:
        """A short name for the configuration, as a human would say it."""
        for candidate in (self.script, self.program):
            if candidate:
                return Path(candidate).name
        return f"pid {self.pid}"

    @property
    def label(self) -> str:
        """The name *and* the process, for a launch.json entry.

        ``launch.json`` holds one flat list and VS Code's dropdown shows nothing
        but these names, so an entry that names only the program is unusable the
        moment two candidates share a basename — which is the normal case, not
        the exotic one: an entrypoint script's children are three ``python``
        processes, and ``merge_launch_configs`` matches existing entries *by
        name*, so three identical names also silently replace one another in a
        file the user already had.

        >>> Target(12, Language.PYTHON, "/usr/bin/python3",
        ...        script="/app/serve.py", comm="python").label
        'serve.py [pid 12 python]'

        The pid is not repeated when it is already the whole name:

        >>> Target(603, Language.NATIVE, None, comm="victim").label
        'pid 603 [victim]'
        """
        bare = f"pid {self.pid}"
        if self.name == bare:
            return bare if self.comm is None else f"{bare} [{self.comm}]"
        inner = bare if self.comm is None else f"{bare} {self.comm}"
        return f"{self.name} [{inner}]"

    @property
    def source_root(self) -> str | None:
        """The directory the debuggee will report its source paths under.

        The script's directory first and the working directory second: an app
        started as ``python /src/app.py`` reports ``/src/app.py`` whatever its
        cwd happens to be, and it is that spelling — not the cwd — that has to
        appear on the right-hand side of a ``pathMappings`` entry.

        >>> Target(1, Language.PYTHON, "/usr/bin/python3",
        ...        script="/src/app.py", cwd="/").source_root
        '/src'
        """
        if self.script and self.script.startswith("/"):
            return str(Path(self.script).parent)
        return self.cwd


@dataclass(frozen=True)
class Debugger:
    """One row of the image's debugger inventory."""

    name: str
    present: bool
    detail: str

    def line(self) -> str:
        """``capreport``'s one-line rendering."""
        return f"{'yes' if self.present else 'no ':<4} {self.detail}"


@dataclass(frozen=True)
class Seat:
    """What this container can bring to the target, all of it measured."""

    machine: str
    cap_sys_ptrace: bool
    debuggers: tuple[Debugger, ...] = ()
    debugpy_here: str | None = None
    """Directory to put on ``PYTHONPATH`` for the driver half, if there is one."""

    debugpy_there: str | None = None
    """The target's own debugpy, spelled ``/proc/<pid>/root/...`` — the one
    path that resolves in both mount namespaces."""

    debugpy_helper: bool = False
    """Whether the attach helper for the *target's* architecture exists **in the
    tree the injection will load** — the target's copy when there is one, since
    that is what ``PYTHONPATH`` points the driver at, and this seat's only when
    there is not."""

    provision_dest: str = PROVISION_DEST
    """Where a provisioned debugpy would go, as the target spells it. Carried on
    the seat so that the refusal's remedy quotes the destination this run would
    really use rather than the default."""

    sysroot_gdb: bool = False
    """Whether a bare ``gdb`` on PATH sets the sysroot before it attaches."""

    listening_port: int | None = None
    """A debugpy server already accepting connections in this pod, if any."""

    program_load_error: str | None = None
    """What gdb said when asked to load the target's binary, if it objected.

    ``None`` covers both "it loaded" and "nobody asked", which is the same
    answer as far as :func:`assess` is concerned: only a *measured* refusal
    withdraws the gdb flavour.

    Measured because "gdb can read this file" and "this file has debug
    information" are different questions that present identically in VS Code,
    and only the first one is fatal. A binary whose ``.gnu.version_r`` this
    seat's binutils rejects cannot be read at all — see the glossary's *symbol
    versioning*.
    """

    target_attach_ok: bool | None = None
    """Whether a real ptrace attach to the target succeeded from this seat.

    The same measurement ``capreport`` lands in
    :attr:`~podbench.model.CapabilityReport.target_attach_ok`, and it is carried
    here rather than re-derived because the capability bit answers a *different*
    question: ptrace is granted by any of four mechanisms, and uid 0 attaching to
    uid 0 under ``ptrace_scope=0`` is classic ptrace that needs no capability at
    all (issue #89).

    ``None`` means nobody measured it — which is **not** the same as ``False``.
    :func:`assess` falls back to the capability bit in that case, because an
    unmeasured attach is not evidence that attach works, and reporting one as if
    it were is the overclaim spike S5 and issue #51 exist to prevent.
    """

    def has(self, name: str) -> bool:
        """Whether the inventory found ``name``."""
        return any(entry.present for entry in self.debuggers if entry.name == name)


@dataclass(frozen=True)
class Assessment:
    """Whether one flavour applies here, and — if not — what says no."""

    flavour: Flavour
    available: bool
    reason: str
    """One sentence naming the mechanism, in ``capreport``'s house style."""

    remedy: str | None = None
    detail: tuple[str, ...] = field(default_factory=tuple)
    """Every other unmet prerequisite, so a fixed headline does not reveal a
    second wall the user could have been told about at the same time."""

    language_mismatch: bool = False
    """This flavour debugs another language, and nothing here can change that.

    Marked rather than merely worded, so the caller can stay quiet about it:
    telling someone debugging a C binary that delve is for Go is noise, while
    every other refusal names something they could act on.
    """

    def message(self) -> str:
        """The full "cannot emit, and here is why" text."""
        head = f"{self.flavour.value} unavailable: {self.reason}"
        lines = [head, *(f"  also: {item}" for item in self.detail)]
        if self.remedy:
            lines.append(f"  {self.remedy}")
        return "\n".join(lines)


# -- what the target is ----------------------------------------------------


def inspect_target(
    pid: int, *, proc: Path = DEFAULT_PROC, program: str | None = None
) -> Target:
    """Read the target's binary and command line, and name its language.

    ``program`` overrides the ``/proc/<pid>/exe`` read, which needs
    ``PTRACE_MODE_READ`` and so fails at the wrong uid (report §3.11). An
    unreadable exe is not fatal: the command line alone still identifies an
    interpreter, which is the case that decides the Python flavour.
    """
    notes: list[str] = []
    exe = program or read_exe(pid, proc=proc)
    cmdline = read_cmdline(pid, proc=proc) or ""
    if exe is None:
        notes.append(
            f"could not read /proc/{pid}/exe (it needs PTRACE_MODE_READ), so the "
            "language was decided from the command line alone"
        )
    # The ELF is read through the sysroot: /proc/<pid>/root is the target's
    # filesystem, and the same path unprefixed reads *this* image's copy of a
    # binary with the same name (report §3.3). The read goes through ``proc``
    # while the message quotes the real spelling, so a synthetic tree can stand
    # in for /proc without the emitted paths becoming synthetic too.
    elf = read_elf(Path(proc) / str(pid) / "root" / exe.lstrip("/")) if exe else None
    if exe and elf is None:
        notes.append(
            f"{sysroot_path(pid)}{exe} could not be read as ELF, so neither the "
            "target's architecture nor its debug info could be established"
        )

    argv = cmdline.split()
    interpreter = _interpreter_of(exe, argv)
    language = _language(interpreter, elf)
    script = _script_of(argv) if interpreter else None
    root = Path(proc) / str(pid) / "root"
    return Target(
        pid=pid,
        language=language,
        program=exe,
        cmdline=cmdline,
        elf=elf,
        interpreter=interpreter,
        script=script,
        python_version=(
            _python_version(exe, argv, root) if interpreter == "python" else None
        ),
        cwd=_read_cwd(pid, proc=proc),
        comm=read_comm(pid, proc=proc),
        notes=tuple(notes),
    )


def _read_cwd(pid: int, *, proc: Path) -> str | None:
    """The target's cwd as *it* spells it. ``None`` is a real answer.

    The link is readable only with ``PTRACE_MODE_READ``, and it is read for the
    same reason ``exe`` is: the value is a path in the target's own mount
    namespace, so it is the right-hand side of a mapping rather than something
    to open here.
    """
    try:
        return os.readlink(proc / str(pid) / "cwd")
    except OSError:
        return None


def _interpreter_of(exe: str | None, argv: Sequence[str]) -> str | None:
    """The managed runtime this process *is*, from its exe or its ``argv[0]``.

    >>> _interpreter_of("/usr/local/bin/python3.12", [])
    'python'
    >>> _interpreter_of("/app/victim", ["/app/victim"]) is None
    True
    """
    for candidate in (exe, argv[0] if argv else None):
        if not candidate:
            continue
        stem = Path(candidate).name
        for prefix, language in _INTERPRETERS:
            # Prefix, not equality: the exe link resolves to `python3.12`, and
            # every point release would otherwise need its own table entry.
            if stem == prefix or stem.startswith(prefix):
                return language
    return None


def _script_of(argv: Sequence[str]) -> str | None:
    """The script an interpreter was given, skipping its own options.

    >>> _script_of(["python", "-u", "/src/demo_service.py"])
    '/src/demo_service.py'
    >>> _script_of(["python", "-m", "http.server"]) is None
    True
    """
    rest = iter(argv[1:])
    for token in rest:
        if token == "-m":
            # `-m pkg` names a module, not a file, so there is no path to map.
            return None
        if token.startswith("-"):
            continue
        return token
    return None


_PYTHON_VERSION = re.compile(r"^python(\d+\.\d+)$")
"""``python3.12``, and deliberately not ``python3`` or ``python``.

A version that is not in the name is not a measurement, and uv's
``--python-version`` is the one field here where a plausible guess is worse than
an admitted gap.
"""


def _python_version(exe: str | None, argv: Sequence[str], root: Path) -> str | None:
    """The target's CPython ``X.Y``, from its own rootfs. ``None`` is an answer.

    The exe link usually resolves through ``python`` to ``python3.12`` and
    settles it. When it does not — an unreadable ``exe`` on the degraded rung,
    or an ``argv[0]`` of plain ``python`` — the target's own library directory
    is asked instead, over the same two fixed prefixes as
    :func:`_target_debugpy` and for the same reason: a walk of another
    container's filesystem is the OOM the vscode-in-a-seat skill warns about.

    *One* library directory under a prefix is evidence; two are an ambiguity,
    and answering one of them would be the guess this module refuses to make —
    a ``sorted()`` pick puts ``python3.10`` ahead of ``python3.9`` on top of it,
    so the wrong answer would not even have been the plausible one.

    >>> _python_version("/usr/local/bin/python3.12", [], Path("/nonexistent"))
    '3.12'
    >>> _python_version(None, ["python"], Path("/nonexistent")) is None
    True
    """
    for candidate in (exe, argv[0] if argv else None):
        match = _PYTHON_VERSION.match(Path(candidate).name) if candidate else None
        if match:
            return match.group(1)
    for prefix in _SEARCH_ROOTS:
        found: list[str] = []
        for parent in (root / prefix).glob("python3.*"):
            match = _PYTHON_VERSION.match(parent.name)
            if match and match.group(1) not in found:
                found.append(match.group(1))
        if found:
            return found[0] if len(found) == 1 else None
    return None


def _language(interpreter: str | None, elf: ElfInfo | None) -> Language:
    if interpreter == "python":
        return Language.PYTHON
    if interpreter == "node":
        return Language.NODE
    if elf is None:
        return Language.UNKNOWN
    return Language.GO if elf.is_go else Language.NATIVE


def detect_mode(pid: int, *, proc: Path = DEFAULT_PROC) -> Mode:
    """Whether ``pid`` is ours (``dev``) or a neighbour's (``observe``).

    Two processes share a root inode exactly when they share a mount namespace,
    which under ``shareProcessNamespace: true`` is the only thing that still
    distinguishes containers. A ``podbench dev`` pod relaunches the app *from*
    the seat, so its process is on this side of that line — and that single
    fact is what flips every path mapping in the emitted config.
    """
    return Mode.DEV if same_root(pid, proc=proc) else Mode.OBSERVE


# -- what the seat has -----------------------------------------------------

Which = Callable[[str], str | None]
"""``shutil.which``, injected so the unit suite never depends on a real PATH."""


def inventory(
    *, which: Which = shutil.which, debugpy_root: str | None = None
) -> tuple[Debugger, ...]:
    """Which debuggers this image actually ships.

    Printed beside the capability report on purpose: without it the emitted
    configuration silently outruns the image, which is the failure #18 set out
    to remove reappearing one layer up.
    """
    entries: list[Debugger] = []
    for name, note in (
        ("gdb", "cppdbg/MIMode gdb, and `podbench dbg`"),
        ("lldb", "CodeLLDB brings its own to the remote, so this is optional"),
        ("dlv", "delve, for Go targets"),
    ):
        path = which(name)
        entries.append(
            Debugger(name, path is not None, f"{name}: {path or f'absent ({note})'}")
        )
    entries.append(Debugger("gdb-podbench", *_shim_detail(which)))
    entries.append(Debugger("debugpy", *_debugpy_detail(debugpy_root)))
    return tuple(entries)


def sysroot_gdb_on_path(which: Which = shutil.which) -> bool:
    """Whether a bare ``gdb`` resolves to the sysroot-aware wrapper.

    Not "is the wrapper installed": debugpy, and anything else that shells out
    to ``gdb --nx --pid``, gets whichever ``gdb`` PATH resolves to. A wrapper
    sitting beside an unwrapped ``gdb`` fixes nothing, and reporting it as met
    would be a prerequisite that reads green and is not.
    """
    shim = which("gdb-podbench")
    resolved = which("gdb")
    if shim is None or resolved is None:
        return False
    return os.path.realpath(resolved) == os.path.realpath(shim)


def _shim_detail(which: Which) -> tuple[bool, str]:
    """The inventory line for the wrapper, naming what ``gdb`` really is."""
    shim = which("gdb-podbench")
    if shim is None:
        return False, "gdb-podbench: absent (bare `gdb --pid` gets no sysroot)"
    if sysroot_gdb_on_path(which):
        return True, f"gdb-podbench: {shim} (`gdb` on PATH is the shim)"
    return False, (
        f"gdb-podbench: {shim}, but `gdb` on PATH is {which('gdb')} — a tool "
        "that shells out to `gdb --pid` still gets no sysroot"
    )


def _debugpy_detail(root: str | None) -> tuple[bool, str]:
    found = _debugpy_dir(root)
    if found is None:
        return False, (
            f"debugpy: absent from {root or SEAT_DEBUGPY_PATH} "
            "(pid-injection has no driver half)"
        )
    helpers = sorted(
        path.name
        for path in (found / _HELPER_RELATIVE).glob("attach_linux_*.so")
        if path.is_file()
    )
    return True, f"debugpy: {found} (attach helpers: {', '.join(helpers) or 'none'})"


def _debugpy_dir(root: str | None) -> Path | None:
    base = Path(root or SEAT_DEBUGPY_PATH)
    return base if _has_debugpy(base) else None


def survey_seat(
    target: Target,
    *,
    proc: Path = DEFAULT_PROC,
    machine: str | None = None,
    which: Which = shutil.which,
    debugpy_root: str | None = None,
    listening_port: int | None = None,
    provision_dest: str = PROVISION_DEST,
    program_load_error: str | None = None,
    target_attach_ok: bool | None = None,
) -> Seat:
    """Gather every fact :func:`assess` needs, with no side effects.

    Deliberately *not* a ptrace probe itself: measuring attach means really
    attaching, and gathering facts must not touch the workload behind the
    caller's back - which is a rule about consent, not about cost, and so
    outlives the seize that made the attach free. So the three facts that cost
    something to learn — ``listening_port``, ``program_load_error`` and
    ``target_attach_ok`` — are handed in by a caller that decided each was worth
    its price. What is read here costs a stat and a read of
    ``/proc/self/status``.
    """
    here = _debugpy_dir(debugpy_root)
    there = _target_debugpy(
        target.pid,
        argv=target.cmdline.split(),
        proc=proc,
        provision_dest=provision_dest,
    )
    return Seat(
        machine=machine or os.uname().machine,
        cap_sys_ptrace=self_capabilities(proc=proc).sys_ptrace_effective,
        debuggers=inventory(which=which, debugpy_root=debugpy_root),
        debugpy_here=None if here is None else str(here),
        debugpy_there=None if there is None else _spelled(target.pid, proc, there),
        # Asked of the tree the *injection* loads, which is the target's copy
        # whenever there is one: the driver runs with PYTHONPATH pointed at it,
        # so the seat's copy answers a question nobody asked, and would misreport
        # the moment the two differ.
        debugpy_helper=_helper_present(there or here, target.machine),
        sysroot_gdb=sysroot_gdb_on_path(which),
        listening_port=listening_port,
        provision_dest=provision_dest,
        program_load_error=program_load_error,
        target_attach_ok=target_attach_ok,
    )


def _helper_present(tree: Path | None, machine: str | None) -> bool:
    if tree is None or machine is None:
        return False
    return (tree / _HELPER_RELATIVE / debugpy_helper_name(machine)).is_file()


def _has_debugpy(directory: Path) -> bool:
    """Whether ``directory`` is a directory an interpreter could import debugpy
    from — the only test that matters, since that import is what the target's
    own interpreter performs."""
    return (directory / "debugpy" / "__init__.py").is_file()


def _spelled(pid: int, proc: Path, local: Path) -> str:
    """``local`` as the driver has to write it on ``PYTHONPATH``.

    The string debugpy injects into the target is the one the *driver* saw, and
    driver and target share no mount namespace, so ``/proc/<pid>/root/...`` is
    the only spelling that resolves on both sides (issue #20's live proof, step
    3). The conversion is here rather than in each branch below so that the
    search and the spelling cannot drift apart.
    """
    relative = local.relative_to(Path(f"{proc}/{pid}/root"))
    return f"{sysroot_path(pid)}/{relative.as_posix()}"


def _venv_site_packages(root: Path, argv: Sequence[str]) -> Path | None:
    """The target's virtualenv ``site-packages``, read off its own ``argv``.

    :data:`_SEARCH_ROOTS` describes a *system* layout, and an epics-containers
    image is not one: the interpreter is ``/app/.venv/bin/python3`` and every
    dependency the app imports — debugpy among them — lives under
    ``/app/.venv/lib/python3.X/site-packages``, which neither prefix reaches.
    Measured on p47's ``fastcs-example``, whose image ships debugpy 1.8.17 and
    whose ``debug-config`` still refused the flavour for want of it.

    ``argv`` rather than ``/proc/<pid>/exe``: a uv-built venv symlinks its
    interpreter out to ``/python/cpython-<version>-<triple>/bin``, so the exe
    link resolves *past* the venv and names the base prefix instead. Two
    entries are read because either can be the one inside the venv — a console
    script is ``argv[0]`` on its own, and ``python /app/.venv/bin/app`` puts it
    in ``argv[1]``.

    ``pyvenv.cfg`` is what makes this a lookup rather than a guess: without it
    an ``argv[0]`` of ``/usr/bin/python3`` would be read as a venv rooted at
    ``/usr``, and the answer would be a system tree the target may not even
    import from.

    A ``..`` disqualifies a candidate outright, because the answer is not just
    a path to stat here: it becomes the ``PYTHONPATH`` of the injection command
    and the prefix of the ``dlopen`` path debugpy writes *into the target*. The
    single property that makes ``/proc/<pid>/root/...`` the one spelling valid
    on both sides is that it names the same file in either mount namespace, and
    a ``..`` is resolved against whatever ``/proc/<pid>/root`` means on each —
    so a component this module cannot resolve is one it must not carry.

    >>> _venv_site_packages(Path("/nonexistent"), ["/app/.venv/bin/python3"])
    >>> _venv_site_packages(Path("/nonexistent"), ["/../../opt/venv/bin/python"])
    """
    for candidate in argv[:2]:
        binary = Path(candidate)
        if not candidate.startswith("/") or ".." in binary.parts:
            continue
        if binary.parent.name != "bin":
            continue
        venv = root / binary.parent.parent.relative_to("/")
        if not (venv / "pyvenv.cfg").is_file():
            continue
        for parent in sorted(venv.glob("lib/python3*")):
            if _has_debugpy(parent / "site-packages"):
                return parent / "site-packages"
    return None


def _target_debugpy(
    pid: int,
    *,
    argv: Sequence[str] = (),
    proc: Path = DEFAULT_PROC,
    provision_dest: str = PROVISION_DEST,
) -> Path | None:
    """Where the *target* keeps debugpy, as a path this container can stat.

    ``provision_dest`` is **one** extra fixed path, not a widened glob: it is
    where ``--provision`` puts a copy, and it is checked first because it was
    put there deliberately, for this, and matched to the target's interpreter by
    ``uv pip install --python-version``. Anything broader would be a walk of
    another container's rootfs, which is the OOM the vscode-in-a-seat skill
    warns about and which an ephemeral container cannot be restarted from.

    The target's own venv is asked next and before the system prefixes, because
    that is the tree its interpreter really imports from: a venv built with
    ``include-system-site-packages = false`` cannot reach ``/usr/lib`` at all,
    so answering with a system copy would name a debugpy the bootstrap could
    not load.
    """
    root = Path(f"{proc}/{pid}/root")
    provisioned = root / provision_dest.lstrip("/")
    if _has_debugpy(provisioned):
        return provisioned
    venv = _venv_site_packages(root, argv)
    if venv is not None:
        return venv
    for prefix in _SEARCH_ROOTS:
        for parent in sorted((root / prefix).glob("python3*")):
            for leaf in ("site-packages", "dist-packages"):
                if _has_debugpy(parent / leaf):
                    return parent / leaf
        # Debian's own layout puts dist-packages one level up from the versioned
        # directory, so it is checked separately rather than folded into the
        # glob above.
        for leaf in ("python3/dist-packages",):
            if _has_debugpy(root / prefix / leaf):
                return root / prefix / leaf
    return None


# -- what applies ----------------------------------------------------------


def assess(target: Target, mode: Mode, seat: Seat) -> list[Assessment]:
    """One verdict per flavour, in the order they are worth trying.

    Every flavour is judged, including the ones that plainly do not apply, so
    that ``--flavour delve`` against a Python process gets a sentence rather
    than an empty file.
    """
    return [
        _assess_gdb(target, mode, seat),
        _assess_lldb(target, mode, seat),
        _assess_delve(target, mode, seat),
        _assess_debugpy(target, mode, seat),
    ]


def _assess_gdb(target: Target, mode: Mode, seat: Seat) -> Assessment:
    if target.language is Language.PYTHON:
        return Assessment(
            Flavour.GDB,
            False,
            "the target is a CPython interpreter, so gdb sees interpreter "
            "frames and not your Python source",
            remedy="use --flavour debugpy, or `podbench dbg` if the interpreter "
            "itself is what you are debugging",
            language_mismatch=True,
        )
    if target.language is Language.NODE:
        return Assessment(
            Flavour.GDB,
            False,
            "the target is a Node process; podbench has no Node adapter and "
            "gdb would show V8's frames",
            language_mismatch=True,
        )
    if not seat.has("gdb"):
        return Assessment(
            Flavour.GDB, False, "no gdb on PATH in this seat", remedy=_IMAGE_REMEDY
        )
    if seat.program_load_error is not None:
        # Measured by really asking gdb, because the failure is fatal *only* in
        # the editor: at the CLI a failed `file` prints and carries on, so
        # `podbench dbg` attaches and cpptools aborts on the identical fault.
        # cpptools then reports it as `Program path '<x>' is missing or invalid`
        # with gdb's message appended - and the path is fine, which sends the
        # reader after the one thing that is not wrong (issue #92).
        return Assessment(
            Flavour.GDB,
            False,
            f"gdb cannot read {target.program}, so cpptools would abort on "
            f"startup rather than attach. gdb said: {seat.program_load_error}",
            remedy=(
                "use the lldb entry beside this one - CodeLLDB brings its own "
                "reader and does not go through binutils. A `.gnu.version_r` "
                "complaint means this seat's binutils is older than the "
                "toolchain that linked the target, which is an image change, "
                "not a flag. `podbench dbg` still attaches: a failed symbol "
                "load is not fatal at the CLI, it just leaves you without "
                "symbols for the main executable"
            ),
        )
    return _no_program(Flavour.GDB, target) or Assessment(
        Flavour.GDB, True, _gdb_reason(target, mode)
    )


def _no_program(flavour: Flavour, target: Target) -> Assessment | None:
    """Refuse rather than guess when the target's binary is unknown.

    cpptools, CodeLLDB and delve all require ``program``, and a wrong one is the
    silent failure this whole module exists to prevent: it reads *this* image's
    binary of the same name and produces an entirely believable backtrace off
    the wrong symbols (report §3.3).
    """
    if target.program:
        return None
    return Assessment(
        flavour,
        False,
        f"could not read /proc/{target.pid}/exe (it needs PTRACE_MODE_READ), so "
        "the target's binary is unknown and `program` cannot be filled in",
        remedy="pass --program with the path inside the target's own rootfs",
    )


def _gdb_reason(target: Target, mode: Mode) -> str:
    if target.elf is None:
        return (
            "the target's binary could not be read, so this is gdb on the "
            "assumption that it is native code"
        )
    if not target.elf.has_debug_info:
        return "native target with no .debug_info in the binary; " + (
            "the build-id is present, so debuginfod can still serve symbols "
            "(report §3.2)"
            if target.elf.has_build_id
            else "and no build-id either, so expect addresses rather than names"
        )
    return f"native target, {mode.value} mode"


def _assess_lldb(target: Target, mode: Mode, seat: Seat) -> Assessment:
    # Ruled out by a language that is *known* to be managed, never by an
    # unknown one: reading the target's ELF needs PTRACE_MODE_READ, so on the
    # degraded rung the language is unknown for a perfectly ordinary C binary,
    # and withdrawing a working configuration on that basis would be the silent
    # wrong answer in a new place.
    if target.language in (Language.PYTHON, Language.NODE):
        return Assessment(
            Flavour.LLDB,
            False,
            f"CodeLLDB debugs native code and this target is {target.language.value}",
            language_mismatch=True,
        )
    if mode is Mode.DEV:
        # Not a shortcoming of lldb: podbench has never measured a CodeLLDB
        # launch inside a dev pod, and emitting an unmeasured config is how a
        # silent wrong answer gets shipped.
        return Assessment(
            Flavour.LLDB,
            False,
            "no dev-mode CodeLLDB configuration is emitted; only the attach "
            "shape has been measured in a seat",
            remedy="use --flavour gdb for the dev-mode launch",
        )
    # No image check: CodeLLDB ships its own lldb and installs it into the
    # *remote* extension host, so the seat having none decides nothing.
    return _no_program(Flavour.LLDB, target) or Assessment(
        Flavour.LLDB, True, "native target; CodeLLDB brings its own lldb to the seat"
    )


def _assess_delve(target: Target, mode: Mode, seat: Seat) -> Assessment:
    if target.language is not Language.GO:
        return Assessment(
            Flavour.DELVE,
            False,
            "delve debugs Go and this binary has no .gopclntab or Go build-id "
            f"({target.language.value} target)",
            language_mismatch=True,
        )
    if not seat.has("dlv"):
        return Assessment(
            Flavour.DELVE,
            False,
            "no dlv on PATH in this seat, and the Go extension runs dlv on the "
            "remote rather than shipping one",
            remedy=_IMAGE_REMEDY,
        )
    return _no_program(Flavour.DELVE, target) or Assessment(
        Flavour.DELVE, True, f"Go target, {mode.value} mode"
    )


_IMAGE_REMEDY = (
    "the image ships what `capreport` lists under DEBUGGERS; adding one is a "
    "deliberate change to the image, not something a seat can install for the "
    "session"
)


def _assess_debugpy(target: Target, mode: Mode, seat: Seat) -> Assessment:
    if target.language is not Language.PYTHON:
        return Assessment(
            Flavour.DEBUGPY,
            False,
            f"debugpy debugs CPython and this target is {target.language.value}",
            language_mismatch=True,
        )
    if mode is Mode.DEV:
        if seat.debugpy_here is None:
            return Assessment(
                Flavour.DEBUGPY,
                False,
                "no debugpy in this container, and in dev mode the adapter runs "
                "here rather than in the target",
                remedy=f"`uv pip install debugpy`, or check {SEAT_DEBUGPY_PATH}",
            )
        return Assessment(
            Flavour.DEBUGPY, True, "Python target in this container's own namespace"
        )
    if seat.listening_port is not None:
        # The app called debugpy.listen() itself. Pure Python, so this path is
        # architecture-independent and needs no capability at all.
        return Assessment(
            Flavour.DEBUGPY,
            True,
            f"a debugpy server is already listening on 127.0.0.1:"
            f"{seat.listening_port} in this pod",
        )
    unmet = _injection_prerequisites(target, seat)
    if not unmet:
        return Assessment(
            Flavour.DEBUGPY,
            True,
            "every pid-injection prerequisite is met; start the server with the "
            "command printed below, then connect",
        )
    # Live-failure order, except that a structural blocker is promoted: every
    # other prerequisite has a remedy inside this pod, and the architecture one
    # has none anywhere (there is no arm64 wheel to install).
    ordered = sorted(unmet, key=lambda item: not item[0])
    _, reason, remedy = ordered[0]
    return Assessment(
        Flavour.DEBUGPY,
        False,
        reason,
        remedy=remedy,
        detail=tuple(item[1] for item in ordered[1:]),
    )


def _provision_paste(target: Target, seat: Seat) -> str:
    """The install command with this run's pid, destination and version in it.

    Every field filled in, because a remedy the reader has to complete is a
    remedy the reader gets wrong: the version in particular is the target's and
    not this seat's, and the two differ on the very bench this was proved on.
    """
    return provision_paste(
        target.pid,
        dest=seat.provision_dest,
        python_version=target.python_version,
    )


def can_ptrace_target(seat: Seat) -> bool:
    """Whether this seat may ptrace the target — measured wherever it was.

    The one place that decision is made, because it is made twice: the debugpy
    pid-injection prerequisite here, and ``--provision``'s note about what the
    install will and will not buy. Two spellings of it drifted apart once
    already (issue #89), and the reader sees both sentences in one run.

    >>> capless = Seat(machine="x86_64", cap_sys_ptrace=False)
    >>> can_ptrace_target(capless)  # nobody attached, so the bit is all there is
    False
    >>> can_ptrace_target(Seat(machine="x86_64", cap_sys_ptrace=False,
    ...                        target_attach_ok=True))
    True
    """
    if seat.target_attach_ok is not None:
        return seat.target_attach_ok
    return seat.cap_sys_ptrace


def _injection_prerequisites(target: Target, seat: Seat) -> list[tuple[bool, str, str]]:
    """The unmet prerequisites, as ``(structural, reason, remedy)``.

    In the order the live proof hit them: the dlopen path first, the sysroot
    second, the architecture third, ptrace fourth and the symbols gdb has to
    read once it is attached last. Reporting all of them is the point — fixing
    the headline only to meet the next wall is the experience this replaces.
    """
    unmet: list[tuple[bool, str, str]] = []
    if seat.debugpy_here is None:
        unmet.append(
            (
                False,
                "no debugpy in this seat to drive the injection",
                f"the image installs it at {SEAT_DEBUGPY_PATH}; `capreport` "
                "lists what this image actually has",
            )
        )
    if seat.debugpy_there is None:
        unmet.append(
            (
                False,
                "debugpy is not importable by the target: the bootstrap runs "
                "inside the target's interpreter, and debugpy injects a dlopen "
                "of the path the *driver* sees, which the target's mount "
                "namespace does not have",
                # The remedy has to be runnable where it is printed: "install it
                # into the app image" is a rebuild, and nobody attached to a
                # running pod at 3 a.m. can do that (issue #45). uv resolves for
                # an interpreter it is not running, so a 3.11 seat lands the
                # accelerators a 3.12 target will load.
                "install it into the target from this seat: "
                f"`{_provision_paste(target, seat)}` - uv resolves for an "
                "interpreter it is not running, so what lands is the wheel the "
                "target's own version loads, and `podbench debug-config "
                "--provision` runs exactly that after probing the target's "
                "rootfs for writability. It needs egress from the pod, "
                "~15 MB of the ephemeral-storage budget this seat shares with "
                "the workload, and it survives no restart - baking "
                "`debugpy.listen()` into the app image is the durable answer",
            )
        )
    if not seat.sysroot_gdb:
        unmet.append(
            (
                False,
                "no sysroot-aware gdb on PATH: debugpy shells out to a bare "
                "`gdb --nx --pid`, which reads this seat's libraries for the "
                "target's process",
                "the image puts gdb-podbench on PATH as `gdb`; it adds "
                '-iex "set sysroot /proc/<pid>/root", which -ex cannot do '
                "because --pid attaches during startup",
            )
        )
    # The helper is only asked about when there is a debugpy to ask: with none
    # installed its absence says nothing about the architecture, and reporting
    # "not published for x86_64" — which is false — would be worse than saying
    # nothing. The tree asked is the one the injection loads, and it is named in
    # the message, because with a copy on each side they can disagree.
    tree = seat.debugpy_there or seat.debugpy_here
    if tree is not None and not seat.debugpy_helper:
        machine = target.machine or seat.machine
        helper = debugpy_helper_name(machine)
        if debugpy_helper_published(machine):
            unmet.append(
                (
                    False,
                    f"no {helper} in {tree}, which is the debugpy the injection "
                    "loads: PYTHONPATH points the driver at the target's copy "
                    "whenever there is one, not at this seat's",
                    f"re-install it there: `{_provision_paste(target, seat)}`",
                )
            )
        else:
            unmet.append(
                (
                    True,
                    f"no {helper} in {tree}: debugpy ships the amd64 helper "
                    "alone and publishes no aarch64 Linux wheel at all, so on "
                    f"{machine} there is nothing to install",
                    "bake `debugpy.listen()` into the app image, or use "
                    "`podbench dev` - both are pure Python and work on any "
                    "architecture",
                )
            )
    # The injection is `gdb --pid`, so what it needs is *ptrace*, and the
    # capability is only one of the four ways to have it: uid 0 attaching to
    # uid 0 under ptrace_scope=0 is classic ptrace and needs no capability at
    # all. A Diamond seat whose admission policy strips SYS_PTRACE was refused
    # the injection while `capreport`, in that same seat and that same minute,
    # printed `live attach (pid 12) OK` and `BLOCKER: none` (issue #89) - and
    # the remedy the refusal offered, relaunching on the `full` rung, lands the
    # same capless seat again, because the policy strips it every time.
    #
    # So the measurement wins where there is one. Where there is not, the
    # answer is still the capability bit: an unmeasured attach is not evidence,
    # and inferring one from the accounting is the overclaim spike S5 and issue
    # #51 exist to prevent.
    if not can_ptrace_target(seat):
        if seat.target_attach_ok is False:
            unmet.append(
                (
                    False,
                    "ptrace to the target was refused when this seat measured "
                    "it, and the injection drives gdb to attach to the target",
                    "`capreport` names which of the four mechanisms said no - "
                    "it is not always the capability, and only the one it "
                    "names is worth changing",
                )
            )
        else:
            unmet.append(
                (
                    False,
                    "CAP_SYS_PTRACE is not in this seat's effective set, and "
                    "the injection drives gdb to attach to the target",
                    "relaunch on the `full` rung (root + SYS_PTRACE); "
                    "`capreport` names which rung this seat landed on, and "
                    "measures whether this seat can attach without the "
                    "capability at all",
                )
            )
    # Last, because it is the last wall in live-failure order: the driver starts
    # gdb, gdb attaches, and only then does it read the symbols the `call`
    # needs. Not promoted to the headline the way the arm64 refusal is - that
    # one has no remedy anywhere, and this one has an image whose binutils is
    # new enough.
    if seat.program_load_error is not None:
        unmet.append(
            (
                False,
                # Withdrawn from *this* flavour too, not only from gdb's. The
                # injection is not a symbol-free ptrace: debugpy drives gdb to
                # evaluate `call (void*)dlopen(...)` and then `call
                # (int)DoAttach(...)` in the inferior, and gdb cannot call a
                # function whose symbols it just refused to read. Emitting a
                # configuration anyway leaves the user at the fault issue #92
                # is about, in cpptools' misleading spelling of it.
                f"gdb cannot read {target.program}, and the injection drives "
                "gdb to `call (void*)dlopen(...)` inside the target - a call it "
                "cannot make without the symbols it just refused to load. gdb "
                f"said: {seat.program_load_error}",
                "bake `debugpy.listen()` into the app image, or use `podbench "
                "dev` - both are pure Python and start no gdb at all. A "
                "`.gnu.version_r` complaint means this seat's binutils is older "
                "than the toolchain that linked the target, which is an image "
                "change, not a flag",
            )
        )
    return unmet


def injection_command(target: Target, seat: Seat, port: int = DEBUGPY_PORT) -> str:
    """The command that starts a debugpy server *inside* an uncooperative app.

    Printed rather than run: it attaches to the workload with ptrace and leaves
    a server running in it, which is not something authoring a launch.json may
    do on its own. The ``PYTHONPATH`` is the whole trick — the driver has to
    import the *target's* debugpy so that the path it injects resolves in the
    target's mount namespace too.

    The interpreter is :data:`SEAT_PYTHON` and not a bare ``python``: this line
    is as often pasted as run, and it has to reach the same one of the seat's
    two interpreters wherever it is pasted.

    >>> target = Target(pid=7, language=Language.PYTHON, program="/usr/bin/python3")
    >>> seat = Seat(machine="x86_64", cap_sys_ptrace=True,
    ...             debugpy_there="/proc/7/root/usr/lib/python3/dist-packages")
    >>> print(injection_command(target, seat))
    PYTHONPATH=/proc/7/root/usr/lib/python3/dist-packages \\
      /app/.venv/bin/python -m debugpy --listen 127.0.0.1:5678 --pid 7
    """
    return (
        f"PYTHONPATH={seat.debugpy_there or seat.debugpy_here} \\\n"
        f"  {SEAT_PYTHON} -m debugpy --listen 127.0.0.1:{port} --pid {target.pid}"
    )


def format_inventory(entries: Sequence[Debugger]) -> list[str]:
    """The DEBUGGERS block ``capreport`` prints."""
    return [entry.line() for entry in entries]
