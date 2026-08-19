"""Reading the shared PID namespace and the kernel's own permission accounting.

Everything the capability probe knows, it learns from ``/proc``: the debug
container's capability masks, the node's Yama setting, and which of the
processes in the shared PID namespace belong to the container being debugged.

Two properties shape every function here.

First, **a refusal is an answer**. A denied or absent path is the very thing the
probe exists to report, so no reader raises — each returns ``None`` (or
``False``) and lets the caller name the mechanism.

Second, **attribution keys off the container runtime id** (phase 0 report
§3.15). "The target is PID 1" is wrong under ``shareProcessNamespace: true``
where PID 1 is ``/pause``, and "cgroup is not ``0::/``" picks up every *other*
podbench session's processes. Only a substring match of the target's container
id against ``/proc/<pid>/cgroup`` stays correct in all cases — substring,
because the ephemeral container has its own cgroup namespace and therefore sees
a *relative* path (``0::/../cri-containerd-<id>.scope``).
"""

from __future__ import annotations

import enum
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .model import (
    PTRACE_READ_PATHS,
    TARGET_CID_ENV,
    WORLD_READ_PATHS,
    ProcInfo,
)

__all__ = [
    "CAP_SYS_PTRACE_BIT",
    "DEFAULT_PROC",
    "DELETED_SUFFIX",
    "READ_MATRIX_PATHS",
    "Attribution",
    "Capabilities",
    "Credentials",
    "ProcessListing",
    "apparmor_profile",
    "candidate_note",
    "debug_candidates",
    "env_target_container_id",
    "is_shell",
    "list_processes",
    "no_new_privs",
    "proc_read_matrix",
    "read_cgroup",
    "read_cmdline",
    "read_comm",
    "read_exe",
    "read_gid",
    "read_ppid",
    "read_status_field",
    "read_tracer_pid",
    "read_uid",
    "same_root",
    "scan_processes",
    "seccomp_filter_count",
    "seccomp_mode",
    "self_capabilities",
    "status_field",
    "strip_container_scheme",
    "strip_deleted",
    "sysroot_path",
    "yama_scope",
]

DEFAULT_PROC = Path("/proc")
"""Where ``/proc`` is mounted. A parameter so tests can use a synthetic tree."""

CAP_SYS_PTRACE_BIT = 19
"""CAP_SYS_PTRACE's bit position in the 64-bit capability masks."""

READ_MATRIX_PATHS = (*PTRACE_READ_PATHS, *WORLD_READ_PATHS)
"""The six reads that decide whether degraded debugging is usable.

Composed from the two groups rather than written out, because only the first
group is evidence: ``root``, ``maps`` and ``environ`` (with ``exe``) take
``PTRACE_MODE_READ`` and so survive at the target's UID with zero capabilities,
while ``cmdline``, ``status`` and ``fd`` survive even at the wrong UID (report
§3.11) and are therefore true on a pod where nothing works. ``mem`` and
``syscall`` are deliberately absent — they take ``PTRACE_MODE_ATTACH`` and are
denied in the degraded rung, so counting them would understate a working setup.
"""

DELETED_SUFFIX = " (deleted)"
"""What the kernel appends to ``/proc/<pid>/exe`` once the binary is unlinked.

Common in containers — a rebuild replaces the image layer under a running
process — and gdb takes the whole string as a filename, so it must come off.
"""

_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{32,}")


class Attribution(enum.Enum):
    """How processes were attributed to the container being debugged."""

    CONTAINER_ID = "container-id"
    """Substring match on the target's runtime id. Correct in all cases."""

    CGROUP_FALLBACK = "cgroup-fallback"
    """No container id was supplied, so anything in a cgroup other than our own
    was taken to be the target. Wrong when a second podbench session is
    attached to the same pod — its processes are marked as target too."""


@dataclass(frozen=True)
class ProcessListing:
    """The result of walking the shared PID namespace."""

    processes: list[ProcInfo]
    attribution: Attribution
    warning: str | None = None
    """Set whenever the answer is a guess, so callers can warn rather than lie."""

    @property
    def targets(self) -> list[ProcInfo]:
        """Just the processes attributed to the target container."""
        return [p for p in self.processes if p.is_target]


@dataclass(frozen=True)
class Capabilities:
    """The capability masks from ``/proc/self/status``.

    ``None`` means the mask could not be read at all, which is a different
    answer from a mask of zero.
    """

    effective: int | None
    bounding: int | None
    ambient: int | None

    @property
    def readable(self) -> bool:
        """Whether the masks were readable."""
        return self.effective is not None

    @property
    def sys_ptrace_effective(self) -> bool:
        """Whether CAP_SYS_PTRACE is in the effective set — the only set that
        grants anything. A capability added to a non-root container reaches the
        bounding set alone (report §3.10), which is why this is asked
        separately from :attr:`sys_ptrace_bounding`."""
        return _has_bit(self.effective, CAP_SYS_PTRACE_BIT)

    @property
    def sys_ptrace_bounding(self) -> bool:
        """Whether CAP_SYS_PTRACE is in the bounding set."""
        return _has_bit(self.bounding, CAP_SYS_PTRACE_BIT)

    @property
    def sys_ptrace_ambient(self) -> bool:
        """Whether CAP_SYS_PTRACE is in the ambient set."""
        return _has_bit(self.ambient, CAP_SYS_PTRACE_BIT)

    @property
    def effective_hex(self) -> str:
        """``CapEff`` as the kernel prints it, for the human report."""
        return "unreadable" if self.effective is None else f"{self.effective:016x}"


@dataclass(frozen=True)
class Credentials:
    """What a ``/proc/<pid>/status`` says a process is *running* as.

    The three facts the ptrace credential check turns on, read from the one
    file that cannot be wrong about them. A container spec can be: admission
    rewrites it, and the image's own ``USER`` is not in it at all, so a uid
    read back off a securityContext is what the API server stored rather than
    what the kernel gave the process (measured at DLS 2026-08-19 — a seat whose
    stored spec carries thirteen capabilities reports ``CapEff:
    0000000000000000``, report §3.10).
    """

    uid: int | None
    gid: int | None
    capabilities: Capabilities

    @classmethod
    def from_status(cls, text: str) -> Credentials | None:
        """Decode a ``/proc/<pid>/status``, or ``None`` if that is not one.

        ``None`` is for the empty stdout of an exec that never ran, which must
        not be mistaken for a process with no ids.

        >>> status = "Name:\\tcat\\nUid:\\t0\\t0\\t0\\t0\\nGid:\\t0\\t0\\t0\\t0\\n"
        >>> creds = Credentials.from_status(status + "CapEff:\\t0000000000000000")
        >>> creds.uid, creds.gid, creds.capabilities.effective_hex
        (0, 0, '0000000000000000')
        >>> Credentials.from_status("") is None
        True
        """
        if status_field(text, "Uid") is None:
            return None
        return cls(
            uid=_first_of(status_field(text, "Uid")),
            gid=_first_of(status_field(text, "Gid")),
            capabilities=Capabilities(
                effective=_parse_hex(status_field(text, "CapEff")),
                bounding=_parse_hex(status_field(text, "CapBnd")),
                ambient=_parse_hex(status_field(text, "CapAmb")),
            ),
        )


def _has_bit(mask: int | None, bit: int) -> bool:
    return mask is not None and bool(mask & (1 << bit))


def _read_text(path: Path) -> str | None:
    """Read a file, mapping every way /proc can refuse onto ``None``."""
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _pid_dir(pid: int | str, proc: Path) -> Path:
    return proc / str(pid)


def _parse_hex(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def status_field(text: str, field: str) -> str | None:
    """One field of an already-read ``/proc/<pid>/status``, or ``None``.

    Split out from :func:`read_status_field` because the same file arrives by
    two routes: read from a mounted ``/proc`` in the seat, and relayed over
    ``kubectl exec`` to the launcher, which has no ``/proc`` to point at.

    >>> status_field("Uid:\\t1000\\t1000\\t1000\\t1000", "Uid")
    '1000\\t1000\\t1000\\t1000'
    >>> status_field("Name:\\tcat", "CapEff") is None
    True
    """
    prefix = f"{field}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def read_status_field(
    pid: int | str, field: str, *, proc: Path = DEFAULT_PROC
) -> str | None:
    """Return one field of ``/proc/<pid>/status``, or ``None`` if unavailable."""
    text = _read_text(_pid_dir(pid, proc) / "status")
    return None if text is None else status_field(text, field)


def read_uid(pid: int | str, *, proc: Path = DEFAULT_PROC) -> int | None:
    """The process's real UID — the first of the four on the ``Uid:`` line."""
    return _first_of(read_status_field(pid, "Uid", proc=proc))


def read_gid(pid: int | str, *, proc: Path = DEFAULT_PROC) -> int | None:
    """The process's real GID — the first of the four on the ``Gid:`` line.

    The peer of :func:`read_uid`, and needed for the same reason: the kernel's
    ``__ptrace_may_access()`` compares ``gid``, ``egid`` and ``sgid`` exactly as
    it compares the three user ids, so a seat that knows only the uids cannot
    say whether the credential check will pass.

    It is also the *only* source of the truth in the common case. A manifest
    that sets ``runAsUser`` and no ``runAsGroup`` leaves
    :func:`podbench.spec.target_uid_gid` returning a gid of ``None`` while the
    process runs in whatever group the image's user carries — p47-blueapi-0 sets
    ``runAsUser: 1000`` and runs at gid 1000. ``status`` is world-readable and
    needs no ptrace permission, so a seat landed at the *wrong* ids can still
    read it, which is what makes the correction possible at all.
    """
    return _first_of(read_status_field(pid, "Gid", proc=proc))


def _first_of(field: str | None) -> int | None:
    """The first whitespace-separated number of a ``Uid:``/``Gid:`` line.

    Both lines carry real, effective, saved and filesystem ids in that order,
    and the *real* one is what the credential check compares.
    """
    if field is None:
        return None
    parts = field.split()
    return _parse_int(parts[0]) if parts else None


def read_ppid(pid: int | str, *, proc: Path = DEFAULT_PROC) -> int | None:
    """The parent pid from ``/proc/<pid>/status``. ``None`` is a real answer."""
    return _parse_int(read_status_field(pid, "PPid", proc=proc))


def read_tracer_pid(pid: int | str, *, proc: Path = DEFAULT_PROC) -> int | None:
    """The pid already tracing this process, or 0. ``None`` if unreadable."""
    return _parse_int(read_status_field(pid, "TracerPid", proc=proc))


def read_comm(pid: int | str, *, proc: Path = DEFAULT_PROC) -> str | None:
    """The process's ``comm``."""
    text = _read_text(_pid_dir(pid, proc) / "comm")
    return None if text is None else text.strip()


def read_cmdline(pid: int | str, *, proc: Path = DEFAULT_PROC) -> str | None:
    """The NUL-separated command line, rejoined with spaces.

    Kernel threads have an empty ``cmdline``; that reads as ``""``, which is
    still a successful read and must not be confused with ``None``.
    """
    text = _read_text(_pid_dir(pid, proc) / "cmdline")
    if text is None:
        return None
    return " ".join(part for part in text.split("\x00") if part)


def read_cgroup(pid: int | str, *, proc: Path = DEFAULT_PROC) -> str | None:
    """The raw contents of ``/proc/<pid>/cgroup``."""
    text = _read_text(_pid_dir(pid, proc) / "cgroup")
    return None if text is None else text.strip()


def strip_container_scheme(container_id: str) -> str:
    """Turn a Kubernetes ``containerID`` into the id containerd puts in cgroup
    paths: ``containerd://abc…`` becomes ``abc…``."""
    return container_id.rsplit("://", 1)[-1].strip()


def env_target_container_id() -> str | None:
    """The target container id injected at debug time as ``PODBENCH_TARGET_CID``.

    The launcher has to supply this (report §4.3) because nothing inside the
    container can work out which sibling it was pointed at.
    """
    raw = os.environ.get(TARGET_CID_ENV, "")
    stripped = strip_container_scheme(raw)
    return stripped or None


def _container_id_from_cgroup(cgroup: str | None) -> str | None:
    if cgroup is None:
        return None
    match = _CONTAINER_ID_RE.search(cgroup)
    return match.group(0) if match else None


def proc_read_matrix(pid: int | str, *, proc: Path = DEFAULT_PROC) -> dict[str, bool]:
    """Which of :data:`READ_MATRIX_PATHS` this container can actually read.

    This is the measurement that separates "read-only inspection works" from
    "nothing works", so it does the real read rather than predicting it from
    UIDs.
    """
    base = _pid_dir(pid, proc)
    result: dict[str, bool] = {}
    for name in READ_MATRIX_PATHS:
        if name in ("root", "fd"):
            result[name] = _listable(base / name)
        else:
            result[name] = _read_text(base / name) is not None
    return result


def _listable(path: Path) -> bool:
    try:
        os.listdir(path)
    except OSError:
        return False
    return True


def strip_deleted(path: str) -> str:
    """Drop the kernel's ``" (deleted)"`` marker from an ``exe`` link target.

    >>> strip_deleted("/app/victim (deleted)")
    '/app/victim'
    >>> strip_deleted("/app/victim")
    '/app/victim'
    """
    return path[: -len(DELETED_SUFFIX)] if path.endswith(DELETED_SUFFIX) else path


def sysroot_path(pid: int) -> str:
    """The target's filesystem as seen from here.

    >>> sysroot_path(597)
    '/proc/597/root'
    """
    return f"/proc/{pid}/root"


def read_exe(pid: int, *, proc: Path = DEFAULT_PROC) -> str | None:
    """The target's executable path *inside its own rootfs*, or ``None``.

    ``None`` is a real answer, not an error: reading this link takes
    ``PTRACE_MODE_READ`` and so fails at the wrong UID (report 3.11). The
    caller has to decide what to do without it, and losing the ``file`` command
    is not fatal — only lossy.
    """
    try:
        target = os.readlink(proc / str(pid) / "exe")
    except OSError:
        return None
    return strip_deleted(target)


def same_root(pid: int | str, *, proc: Path = DEFAULT_PROC) -> bool | None:
    """Whether ``pid`` shares this process's mount namespace. ``None`` if unknown.

    Two processes share a root inode exactly when they share a mount namespace,
    which under ``shareProcessNamespace: true`` is the only thing that still
    distinguishes one container from another — the pid namespace no longer
    does, and the cgroup only says which container *started* the process.

    It is the discriminator two callers need for opposite reasons: ``dev``
    attributes a port to "this container" or "the target", and ``debug-config``
    picks a launch shape over an attach shape, with every path mapping in the
    emitted configuration hanging off the answer.
    """
    try:
        mine = os.stat(proc / "self" / "root")
        theirs = os.stat(proc / str(pid) / "root")
    except OSError:
        return None
    return (mine.st_dev, mine.st_ino) == (theirs.st_dev, theirs.st_ino)


def self_capabilities(*, proc: Path = DEFAULT_PROC) -> Capabilities:
    """The debug container's own capability masks."""
    return Capabilities(
        effective=_parse_hex(read_status_field("self", "CapEff", proc=proc)),
        bounding=_parse_hex(read_status_field("self", "CapBnd", proc=proc)),
        ambient=_parse_hex(read_status_field("self", "CapAmb", proc=proc)),
    )


def yama_scope(*, proc: Path = DEFAULT_PROC) -> int | None:
    """Yama's ``ptrace_scope``, or ``None`` when the LSM is not present.

    Absent is a real and *different* answer from 0: the rockchip 6.1 nodes in
    the test cluster have no Yama at all while their raspi and x86 peers report
    1, so a cluster-wide answer cannot be cached (report §3.13).
    """
    return _parse_int(_read_text(proc / "sys" / "kernel" / "yama" / "ptrace_scope"))


def apparmor_profile(
    pid: int | str = "self", *, proc: Path = DEFAULT_PROC
) -> str | None:
    """The AppArmor profile confining a process.

    ``None`` means the attribute could not be read (no AppArmor, or denied);
    an empty attribute means the process is genuinely unconfined.
    """
    text = _read_text(_pid_dir(pid, proc) / "attr" / "current")
    if text is None:
        return None
    profile = text.replace("\x00", "").strip()
    return profile or "unconfined"


def seccomp_mode(*, proc: Path = DEFAULT_PROC) -> int | None:
    """``Seccomp`` from ``/proc/self/status``: 0 off, 1 strict, 2 filter."""
    return _parse_int(read_status_field("self", "Seccomp", proc=proc))


def seccomp_filter_count(*, proc: Path = DEFAULT_PROC) -> int | None:
    """How many seccomp filters are attached, if the kernel reports it."""
    return _parse_int(read_status_field("self", "Seccomp_filters", proc=proc))


def no_new_privs(*, proc: Path = DEFAULT_PROC) -> bool | None:
    """Whether ``no_new_privs`` is set. ``None`` when the field is missing."""
    value = _parse_int(read_status_field("self", "NoNewPrivs", proc=proc))
    return None if value is None else bool(value)


def list_processes(
    target_container_id: str | None = None,
    *,
    proc: Path = DEFAULT_PROC,
) -> list[ProcInfo]:
    """Every process in the shared PID namespace, attributed to a container.

    Use :func:`scan_processes` instead when the caller needs to know that the
    attribution was a guess.
    """
    return scan_processes(target_container_id, proc=proc).processes


def scan_processes(
    target_container_id: str | None = None,
    *,
    proc: Path = DEFAULT_PROC,
) -> ProcessListing:
    """:func:`list_processes` plus how the attribution was made."""
    cid = strip_container_scheme(target_container_id) if target_container_id else None
    own_cgroup = read_cgroup("self", proc=proc)

    processes: list[ProcInfo] = []
    for pid in _pids(proc):
        comm = read_comm(pid, proc=proc)
        if comm is None:
            continue  # the process exited while we walked /proc
        cgroup = read_cgroup(pid, proc=proc)
        cmdline = read_cmdline(pid, proc=proc)
        processes.append(
            ProcInfo(
                pid=pid,
                # Not `or 0`: an unreadable uid is not root. Collapsing the two
                # would hand the degraded rung a runAsUser of 0, which the
                # report forbids outright (3.11).
                uid=read_uid(pid, proc=proc),
                comm=comm,
                cmdline=cmdline or f"[{comm}]",
                container_id=_container_id_from_cgroup(cgroup),
                is_target=_is_target(cid, cgroup, own_cgroup, comm),
                ppid=read_ppid(pid, proc=proc),
            )
        )

    if cid is not None:
        return ProcessListing(processes, Attribution.CONTAINER_ID)
    return ProcessListing(
        processes,
        Attribution.CGROUP_FALLBACK,
        warning=(
            f"no target container id supplied (set {TARGET_CID_ENV}): processes "
            "were attributed by cgroup difference, which also marks any other "
            "podbench session's processes as target"
        ),
    )


_SHELLS = frozenset(
    {"sh", "bash", "dash", "ash", "ksh", "mksh", "zsh", "busybox", "tini", "dumb-init"}
)
"""``comm`` values that are never the thing anyone came to debug.

Entrypoint shells and init shims: they have no debug info, breaking in one stops
the supervisor rather than the workload, and on any image whose entrypoint is a
script one of them is guaranteed to be the *lowest* pid in the container.
``tini`` and ``dumb-init`` are here for the same reason and not because they are
shells.
"""


def is_shell(info: ProcInfo) -> bool:
    """Whether this process is an entrypoint shell or an init shim.

    >>> is_shell(ProcInfo(1, 0, "bash", "/bin/bash /epics/ioc/start.sh"))
    True
    >>> is_shell(ProcInfo(13, 0, "ioc", "/epics/ioc/bin/linux-x86_64/ioc"))
    False
    """
    return info.comm in _SHELLS


def debug_candidates(targets: Sequence[ProcInfo]) -> list[ProcInfo]:
    """The target container's processes worth debugging, best first.

    Two rules, in order, and both of them come from a real IOC pod whose lowest
    pid was ``/bin/bash /epics/ioc/start.sh`` (issue #92):

    * **shells sort last**, never out — a container that is *only* a shell still
      has to have a target, and the degraded rung has to name something. Among
      themselves they keep pid order rather than depth order, so the fallback
      stays the container's own entrypoint;
    * among the rest, **deepest first**. An entrypoint script is an ancestor of
      the thing it starts, so depth is what separates a wrapper from a workload,
      and it is the only signal in ``/proc`` that does. Ties break on pid, so the
      answer is stable across two calls a second apart.

    A process whose parent is outside ``targets`` has depth 0, which is what puts
    the container's own pid 1 at the top when nothing else survives.

    >>> tree = [
    ...     ProcInfo(1, 0, "bash", "/bin/bash /epics/ioc/start.sh", ppid=0),
    ...     ProcInfo(8, 0, "python", "/venv/bin/python …/stdio-expose", ppid=1),
    ...     ProcInfo(11, 0, "sh", "/bin/sh -c /epics/ioc/start.sh", ppid=8),
    ...     ProcInfo(13, 0, "ioc", "…/linux-x86_64/ioc", ppid=11),
    ... ]
    >>> [p.pid for p in debug_candidates(tree)]
    [13, 8, 1, 11]
    """
    depths = _depths(targets)

    def rank(info: ProcInfo) -> tuple[bool, int, int]:
        shell = is_shell(info)
        return (shell, 0 if shell else -depths[info.pid], info.pid)

    return sorted(targets, key=rank)


def candidate_note(candidates: Sequence[ProcInfo], verb: str) -> str | None:
    """Say which of several processes was picked, and what it was picked over.

    ``None`` when there was nothing to choose. One line, unwrapped: every caller
    folds notes itself, and a newline here would land inside ``debug-config:``'s
    per-line prefix.

    The skipped wrappers are *named* rather than counted. "4 processes, debugging
    pid 13" reads like a coin toss, and the whole point of
    :func:`debug_candidates` is that it is not one — while the reader who
    disagrees with the choice needs the pid of the thing they would rather have.

    >>> tree = [
    ...     ProcInfo(13, 0, "ioc", "ioc", ppid=11),
    ...     ProcInfo(8, 0, "python", "python", ppid=1),
    ...     ProcInfo(1, 0, "bash", "bash", ppid=0),
    ... ]
    >>> for sentence in candidate_note(tree, "debugging").split(". "):
    ...     print(sentence)
    target container has 3 processes; debugging pid 13 (ioc), the deepest non-shell
    Also debuggable: 8 (python)
    Skipped as a wrapper: 1 (bash)
    Run `podbench pids` to choose another.
    """
    if len(candidates) < 2:
        return None
    chosen, rest = candidates[0], candidates[1:]
    # A shell at the head means every candidate was one, since shells sort last.
    why = "the only process here" if is_shell(chosen) else "the deepest non-shell"
    parts = [
        f"target container has {len(candidates)} processes; "
        f"{verb} pid {chosen.pid} ({chosen.comm}), {why}"
    ]
    others = [f"{p.pid} ({p.comm})" for p in rest if not is_shell(p)]
    shells = [f"{p.pid} ({p.comm})" for p in rest if is_shell(p)]
    if others:
        parts.append(f"Also debuggable: {', '.join(others)}")
    if shells:
        parts.append(f"Skipped as a wrapper: {', '.join(shells)}")
    parts.append("Run `podbench pids` to choose another.")
    return ". ".join(parts)


def _depths(targets: Sequence[ProcInfo]) -> dict[int, int]:
    """How far each process sits below the shallowest process in ``targets``.

    Walked with a visited set rather than recursion: ``ppid`` is read per process
    and a pid can be reused between two of those reads, so a cycle is possible
    even though a process tree cannot contain one.
    """
    by_pid = {info.pid: info for info in targets}
    depths: dict[int, int] = {}
    for info in targets:
        depth = 0
        seen = {info.pid}
        parent = info.ppid
        while parent is not None and parent in by_pid and parent not in seen:
            seen.add(parent)
            depth += 1
            parent = by_pid[parent].ppid
        depths[info.pid] = depth
    return depths


def _pids(proc: Path) -> list[int]:
    try:
        entries = os.listdir(proc)
    except OSError:
        return []
    return sorted(int(entry) for entry in entries if entry.isdigit())


def _is_target(
    cid: str | None, cgroup: str | None, own_cgroup: str | None, comm: str
) -> bool:
    if cid is not None:
        # Substring, never equality: the ephemeral container has its own cgroup
        # namespace, so it sees a relative path (report §3.15).
        return cgroup is not None and cid in cgroup
    return _looks_foreign(cgroup, own_cgroup, comm)


def _looks_foreign(cgroup: str | None, own_cgroup: str | None, comm: str) -> bool:
    if cgroup is None:
        return False
    if comm == "pause":
        # PID 1 under shareProcessNamespace is the pod's pause process, never a
        # debugging target (report §3.15).
        return False
    if own_cgroup is not None:
        return cgroup != own_cgroup
    return cgroup != "0::/"
