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
from dataclasses import dataclass
from pathlib import Path

from .model import (
    PTRACE_READ_PATHS,
    TARGET_CID_ENV,
    WORLD_READ_PATHS,
    Lsm,
    LsmStatus,
    ProcInfo,
)

__all__ = [
    "APPARMOR_ENABLED_PATH",
    "CAP_SYS_PTRACE_BIT",
    "DEFAULT_PROC",
    "DEFAULT_SYSFS",
    "DELETED_SUFFIX",
    "READ_MATRIX_PATHS",
    "SELINUX_ENFORCE_PATH",
    "Attribution",
    "Capabilities",
    "ProcessListing",
    "detect_lsm",
    "env_target_container_id",
    "list_processes",
    "lsm_context",
    "no_new_privs",
    "proc_read_matrix",
    "read_cgroup",
    "read_cmdline",
    "read_comm",
    "read_exe",
    "read_status_field",
    "read_tracer_pid",
    "read_uid",
    "same_root",
    "scan_processes",
    "seccomp_filter_count",
    "seccomp_mode",
    "self_capabilities",
    "strip_container_scheme",
    "strip_deleted",
    "sysroot_path",
    "yama_scope",
]

DEFAULT_PROC = Path("/proc")
"""Where ``/proc`` is mounted. A parameter so tests can use a synthetic tree."""

DEFAULT_SYSFS = Path("/sys")
"""Where ``/sys`` is mounted, and the only place an LSM says which one it is.

A second root rather than a path under :data:`DEFAULT_PROC`, for the same
reason: a test that let this default through would ask the *runner's* kernel
which module is loaded, and CI, the devcontainer and the cluster nodes all
answer differently.
"""

SELINUX_ENFORCE_PATH = Path("fs/selinux/enforce")
"""Under :data:`DEFAULT_SYSFS`: present only when SELinux is loaded, ``1`` when
it is enforcing and ``0`` when it is permissive — and a permissive policy logs
the AVC denial while allowing the call, so it can never be the blocker."""

APPARMOR_ENABLED_PATH = Path("module/apparmor/parameters/enabled")
"""Under :data:`DEFAULT_SYSFS`: ``Y`` when the AppArmor module is enabled."""

_APPARMOR_MODES = {"(enforce)": True, "(complain)": False, "(unconfined)": False}
"""AppArmor states its mode in the profile string, unlike SELinux.

Read once the module is known to be AppArmor, and never as a way of *deciding*
that it is: taking meaning from the shape of ``/proc/self/attr/current`` is the
mistake this whole file now avoids (issue #52).
"""

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


def read_status_field(
    pid: int | str, field: str, *, proc: Path = DEFAULT_PROC
) -> str | None:
    """Return one field of ``/proc/<pid>/status``, or ``None`` if unavailable."""
    text = _read_text(_pid_dir(pid, proc) / "status")
    if text is None:
        return None
    prefix = f"{field}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def read_uid(pid: int | str, *, proc: Path = DEFAULT_PROC) -> int | None:
    """The process's real UID — the first of the four on the ``Uid:`` line."""
    field = read_status_field(pid, "Uid", proc=proc)
    if field is None:
        return None
    parts = field.split()
    return _parse_int(parts[0]) if parts else None


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


def lsm_context(pid: int | str = "self", *, proc: Path = DEFAULT_PROC) -> str | None:
    """The security context confining a process, whoever wrote it.

    ``/proc/<pid>/attr/current`` belongs to the *active* LSM, so this is an
    AppArmor profile on a containerd/Ubuntu node and an SELinux context
    (``user_u:role_r:type_t:level``) on a RHEL-family one. Which of the two it
    is comes from :func:`detect_lsm` and never from this string; podbench called
    this function ``apparmor_profile`` and a Diamond SELinux denial went
    unrecognised for it (issue #52).

    ``None`` means the attribute could not be read (no LSM writes it, or the
    read was denied); an empty attribute means the process is genuinely
    unconfined.
    """
    text = _read_text(_pid_dir(pid, proc) / "attr" / "current")
    if text is None:
        return None
    context = text.replace("\x00", "").strip()
    return context or "unconfined"


def detect_lsm(*, proc: Path = DEFAULT_PROC, sysfs: Path = DEFAULT_SYSFS) -> LsmStatus:
    """Which LSM is active here, and whether it is in a position to deny.

    Each module publishes its own state, so this asks them: SELinux exposes
    :data:`SELINUX_ENFORCE_PATH` only when it is loaded, AppArmor answers ``Y``
    at :data:`APPARMOR_ENABLED_PATH`. Both absent is a real answer — no LSM —
    and is not the same as an unreadable ``/sys``, where nothing was ruled out
    and the honest verdict is :attr:`~podbench.model.Lsm.UNKNOWN`.

    SELinux is checked first because it is the one that was being misread: a
    node can have the AppArmor module compiled in and inactive, but a readable
    ``enforce`` file means SELinux is the module writing the contexts.
    """
    context = lsm_context("self", proc=proc)

    enforce = _read_text(sysfs / SELINUX_ENFORCE_PATH)
    if enforce is not None:
        # ``None`` rather than "permissive" for a value that did not parse:
        # `confines` reads False either way, but the report says out loud that
        # `enforce` is 0, and it must not say that about a string it never read
        # as 0.
        mode = _parse_int(enforce)
        return LsmStatus(Lsm.SELINUX, context, None if mode is None else mode == 1)

    apparmor = _read_text(sysfs / APPARMOR_ENABLED_PATH)
    if apparmor is not None and apparmor.strip().upper().startswith("Y"):
        return LsmStatus(Lsm.APPARMOR, context, _apparmor_mode(context))

    if context not in (None, "unconfined"):
        # Something wrote that context, so "no LSM is loaded" is not an answer
        # available here whatever /sys did or did not say - and it is the one
        # `confines` reads as "nothing denied anything". Neither module owned
        # up, which happens when the file is unreadable at this uid, when
        # selinuxfs is not mounted into the container, or when a third module
        # (Smack, TOMOYO) wrote it. All three are `unknown`.
        return LsmStatus(Lsm.UNKNOWN, context)
    if apparmor is not None or _listable(sysfs):
        return LsmStatus(Lsm.NONE, context)
    return LsmStatus(Lsm.UNKNOWN, context)


def _apparmor_mode(context: str | None) -> bool | None:
    """Whether an AppArmor profile enforces, from the mode it names itself.

    A complain-mode profile logs and permits, so blaming it for a denial sends
    someone to edit a profile that allowed the call.
    """
    if context is None:
        return None
    return _APPARMOR_MODES.get(context.rsplit(" ", 1)[-1])


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
