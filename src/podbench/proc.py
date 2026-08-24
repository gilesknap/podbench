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
from pathlib import Path, PurePosixPath

from .model import (
    HOST_NETWORK_ENV,
    POD_CONTAINERS_ENV,
    PTRACE_READ_PATHS,
    TARGET_CID_ENV,
    TARGET_NAME_ENV,
    WORLD_READ_PATHS,
    Lsm,
    ProcInfo,
)

__all__ = [
    "CAP_SYS_PTRACE_BIT",
    "DEFAULT_PROC",
    "DEFAULT_SYS",
    "DELETED_SUFFIX",
    "NAMED_IN_NOTE",
    "PAUSE_COMM",
    "MAX_MAPPED_OBJECTS",
    "READ_MATRIX_PATHS",
    "Attribution",
    "Capabilities",
    "Credentials",
    "ProcessListing",
    "candidate_note",
    "debug_candidates",
    "env_host_network",
    "parse_host_network",
    "env_pod_containers",
    "parse_pod_containers",
    "env_target_container",
    "env_target_container_id",
    "is_shell",
    "list_processes",
    "lsm_label",
    "no_new_privs",
    "proc_read_matrix",
    "ptrace_readable",
    "read_cgroup",
    "read_cmdline",
    "read_comm",
    "read_credentials",
    "read_exe",
    "read_gid",
    "read_mapped_objects",
    "read_ppid",
    "read_state",
    "read_status_field",
    "read_threads",
    "read_tracer_pid",
    "read_uid",
    "same_root",
    "scan_processes",
    "seat_cwd",
    "seccomp_filter_count",
    "seccomp_mode",
    "shares_pod_pid_namespace",
    "self_capabilities",
    "state_letter",
    "status_field",
    "strip_container_scheme",
    "strip_deleted",
    "sysroot_path",
    "yama_scope",
]

DEFAULT_PROC = Path("/proc")
"""Where ``/proc`` is mounted. A parameter so tests can use a synthetic tree."""

DEFAULT_SYS = Path("/sys")
"""Where ``/sys`` is mounted. Separate from :data:`DEFAULT_PROC` because which
LSM is active is a property of the *node*, and only sysfs answers it - see
:func:`lsm_label`."""

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


def read_credentials(
    pid: int | str = "self", *, proc: Path = DEFAULT_PROC
) -> Credentials | None:
    """A process's ids and capability masks, or ``None`` when ``/proc`` refused.

    The local peer of ``launcher.probe_seat_credentials``, which relays the same
    file over ``kubectl exec``: one decoder (:meth:`Credentials.from_status`),
    two ways of getting the bytes to it. ``self`` by default because the caller
    that needs it in the seat is asking what *this* container is running as -
    which decides whether a refused write is about file modes at all
    (:func:`podbench.provision.blocker_sentence`).
    """
    text = _read_text(_pid_dir(pid, proc) / "status")
    return None if text is None else Credentials.from_status(text)


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


def read_state(pid: int | str, *, proc: Path = DEFAULT_PROC) -> str | None:
    """The one-letter ``State:`` from status, or ``None`` if unreadable.

    Only the letter: the kernel writes ``Z (zombie)`` and the parenthesised
    gloss is neither stable across versions nor needed by anything here.
    """
    field = read_status_field(pid, "State", proc=proc)
    return None if field is None else state_letter(field)


def state_letter(field: str) -> str | None:
    """The letter out of a raw ``State:`` value.

    >>> state_letter("Z (zombie)")
    'Z'
    >>> state_letter("") is None
    True
    """
    stripped = field.strip()
    return stripped[0] if stripped else None


def read_threads(pid: int | str, *, proc: Path = DEFAULT_PROC) -> int | None:
    """``Threads:`` from status, or ``None`` if unreadable."""
    return _parse_int(read_status_field(pid, "Threads", proc=proc))


def ptrace_readable(pid: int | str, *, proc: Path = DEFAULT_PROC) -> bool:
    """Whether this seat may take ``PTRACE_MODE_READ`` on ``pid``.

    Measured by opening ``/proc/<pid>/root``, which the kernel gates on
    ``ptrace_may_access(PTRACE_MODE_READ_FSCREDS)`` — the same
    ``__ptrace_may_access()`` credential comparison an attach takes. It issues
    **no ptrace syscall and touches the target not at all**: the process is
    neither stopped, signalled nor traced, which is what makes it affordable
    once per process in the namespace rather than once per report.

    The implication runs one way, and the ranking uses it only in that
    direction. A denial here is conclusive — ``PTRACE_MODE_ATTACH`` is strictly
    stronger than ``PTRACE_MODE_READ``, so nothing that refuses the read will
    permit the attach — while a success only means the credentials match, since
    Yama and the LSMs gate attach and not read. So it *demotes* candidates that
    provably cannot be debugged and never promotes one it has not proved.

    ``root`` rather than ``maps`` or ``environ``, which are gated identically:
    those two are served out of the ``mm_struct``, and a zombie has none, so
    they open with no permission check at all. That is precisely how blueapi's
    zombie scored "maps=ok" (issue #52).
    """
    return _listable(_pid_dir(pid, proc) / "root")


def read_comm(pid: int | str, *, proc: Path = DEFAULT_PROC) -> str | None:
    """The process's ``comm``."""
    text = _read_text(_pid_dir(pid, proc) / "comm")
    return None if text is None else text.strip()


PAUSE_COMM = "pause"
"""``comm`` of the pod's infra container, and the one tell for a shared namespace.

Every container in a pod that sets ``shareProcessNamespace: true`` sees the
sandbox's own process as PID 1; nothing else in a pod is called this.
"""


def shares_pod_pid_namespace(*, proc: Path = DEFAULT_PROC) -> bool | None:
    """Whether this seat is in the *pod's* PID namespace or the target's own.

    The two modes differ in what PID 1 is, and every sentence podbench prints
    about PID 1 is false in one of them. ``shareProcessNamespace: true`` puts
    every container in one namespace whose PID 1 is the sandbox's ``pause``
    process; without it an ephemeral container carrying ``targetContainerName``
    joins the *target container's* namespace, where PID 1 is the target's own
    entrypoint. The second is not the exotic case: 15 of 15 pods surveyed on
    the p47 beamline set no ``shareProcessNamespace`` (finding 17.2).

    Asked of ``/proc/1/comm`` rather than of the pod spec, because a seat has
    no pod spec to read and because the kernel answers for the namespace that
    was actually created rather than for the field somebody wrote. It is the
    same tell :func:`_looks_foreign` already keys on.

    ``None`` is "do not know" — an unreadable ``/proc/1/comm`` — and must be
    spent as a hedge rather than as either answer.
    """
    comm = read_comm(1, proc=proc)
    return None if comm is None else comm == PAUSE_COMM


def read_cmdline(pid: int | str, *, proc: Path = DEFAULT_PROC) -> str | None:
    """The NUL-separated command line, rejoined with spaces.

    Kernel threads have an empty ``cmdline``; that reads as ``""``, which is
    still a successful read and must not be confused with ``None``.
    """
    text = _read_text(_pid_dir(pid, proc) / "cmdline")
    if text is None:
        return None
    return " ".join(part for part in text.split("\x00") if part)


MAX_MAPPED_OBJECTS = 128
"""How many distinct file-backed objects :func:`read_mapped_objects` returns.

A JVM maps several hundred files and a seat reads each of the returned ones as
an ELF, so this is what keeps "does anything here carry symbols" a bounded
question. It is a cap on the *answer* and not on the parse: the paths are read
in address order, which is load order, so what falls off the end is whatever a
long-running process mapped last.
"""

_MAPS_SKIPPED = ("/dev/", "/memfd:", "/anon_hugepage")
"""Map entries with a path-shaped name that is not a file to read.

``/dev/zero`` and an anonymous huge page are named like paths and are not ELF
files; opening them from a seat is at best wasted and at worst blocking.
"""


def read_mapped_objects(
    pid: int | str, *, proc: Path = DEFAULT_PROC, limit: int = MAX_MAPPED_OBJECTS
) -> tuple[str, ...] | None:
    """The distinct files mapped into ``pid``, as *it* spells them.

    ``None`` is the refusal and is a different answer from ``()``: ``maps`` is
    one of :data:`podbench.model.PTRACE_READ_PATHS`, gated on the same
    ``ptrace_may_access()`` comparison as ``root`` and ``exe``, so a seat at the
    wrong credentials gets nothing here while a kernel thread legitimately maps
    no files at all. Reporting "no symbols anywhere in the address space" on the
    strength of a read that was refused is precisely the overclaim this package
    exists to prevent.

    The paths are the target's own spelling and are not openable from here
    without the sysroot prefix, exactly like :func:`read_exe`'s answer.

    >>> read_mapped_objects(1, proc=Path("/nonexistent")) is None
    True
    """
    text = _read_text(_pid_dir(pid, proc) / "maps")
    if text is None:
        return None
    found: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        # The path is the sixth field and is the only one that may contain
        # spaces, so the split is bounded rather than greedy.
        fields = line.split(maxsplit=5)
        if len(fields) < 6:
            continue
        path = strip_deleted(fields[5].strip())
        if not path.startswith("/") or path.startswith(_MAPS_SKIPPED):
            continue
        if path in seen:
            continue
        seen.add(path)
        found.append(path)
        if len(found) >= limit:
            break
    return tuple(found)


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


def env_target_container() -> str | None:
    """The target container's *name*, from :data:`~podbench.model.TARGET_NAME_ENV`.

    ``None`` means the launcher that landed this seat did not say, which is not
    the same as "there is no target": every seat has one. A caller must say
    nothing rather than name the container id in its place - twelve hex digits
    identify a container to the runtime and to no reader.
    """
    return os.environ.get(TARGET_NAME_ENV, "").strip() or None


def env_pod_containers() -> tuple[str, ...]:
    """Every container in this pod, from :data:`~podbench.model.POD_CONTAINERS_ENV`.

    Empty means "not said", never "this pod has one container": a seat landed by
    a launcher older than the variable carries none, and reading that absence as
    a single-container pod is exactly the claim finding 15 is about.
    """
    return parse_pod_containers(os.environ.get(POD_CONTAINERS_ENV, ""))


def parse_pod_containers(raw: str) -> tuple[str, ...]:
    """Split the comma-separated list :data:`POD_CONTAINERS_ENV` carries.

    Its own function for the reason :func:`parse_host_network` is: the parse is
    the part worth testing, and doing it here keeps the test out of the process
    environment.

    >>> parse_pod_containers("ca, pva ,")
    ('ca', 'pva')
    >>> parse_pod_containers("")
    ()
    """
    return tuple(name.strip() for name in raw.split(",") if name.strip())


def env_host_network() -> bool | None:
    """Whether this pod shares the node's network namespace, or ``None``.

    ``None`` is a third answer and not a synonym for ``False``: a seat landed by
    a launcher that predates :data:`~podbench.model.HOST_NETWORK_ENV` carries no
    such variable, and the whole point of the flag is that guessing here is what
    made podbench announce a *stranger's* debugpy server as this pod's own
    (issue #87). Callers must say "unknown" where this says ``None``.
    """
    return parse_host_network(os.environ.get(HOST_NETWORK_ENV, ""))


def parse_host_network(raw: str) -> bool | None:
    """The tri-state :data:`~podbench.model.HOST_NETWORK_ENV` spells.

    >>> parse_host_network("true"), parse_host_network("FALSE")
    (True, False)
    >>> parse_host_network("") is None, parse_host_network("maybe") is None
    (True, True)
    """
    value = raw.strip().lower()
    if value in ("1", "true", "yes"):
        return True
    if value in ("0", "false", "no"):
        return False
    return None


def seat_cwd() -> str:
    """A directory this seat can start a debugger in, read from its own ``$HOME``.

    Something has to be named: ``${workspaceFolder}`` resolves to nothing in a
    seat and gdb dies during startup with no signal name (:mod:`podbench.vscode`
    has the mechanism). What was named until now was the constant ``/root``, on
    the reasoning that the image guarantees it — and the image guarantees it for
    a **root** seat only. Every rung below full runs at the target's uid, for
    which this image has no account, which is exactly why the launcher pins
    ``HOME`` to ``launcher.NON_ROOT_HOME`` under ``/tmp``. ``/root`` is mode
    0700 and owned by root, so at any other uid it is not merely unwritable but
    **unenterable** — measured in a uid-36070 seat on the k3s bed, where
    ``test -x /root`` fails and ``cd /root`` is refused. A cwd that cannot be
    entered is worse than no cwd, and every seat below the full rung was being
    handed one.

    So the seat's own ``$HOME`` — the value the launcher already computed and
    put in the container's environment, rather than a second copy of it here —
    and ``/tmp`` where there is none: mode 1777 in every image, so enterable at
    whatever uid the seat turned out to run as.

    Enterability is measured rather than assumed, because ``$HOME`` can name a
    directory the seat cannot use: a ``podbench-home`` volume mounted with no
    ``fsGroup`` stays ``root:root``, and an older launcher may have pinned a
    home nothing created. This runs *in the seat* at the seat's uid — the only
    place the question has a true answer.
    """
    home = os.environ.get("HOME", "").strip()
    if home and _enterable(home):
        return home
    return "/tmp"


def _enterable(path: str) -> bool:
    """Whether the calling process could ``chdir`` into ``path``.

    Search permission, not write permission: gdb's ``getcwd()`` is all that
    depends on it, so a home the seat can enter and not write still beats the
    fallback.
    """
    return os.path.isdir(path) and os.access(path, os.X_OK)


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


def lsm_label(
    pid: int | str = "self",
    *,
    proc: Path = DEFAULT_PROC,
    sysfs: Path = DEFAULT_SYS,
) -> tuple[Lsm, str | None]:
    """Which LSM is active on this node, and the label it gives ``pid``.

    ``/proc/<pid>/attr/current`` is one slot shared by every label-based LSM,
    so the string in it does not say whose it is. podbench read it as an
    AppArmor profile, which on the RHEL9 nodes it runs on printed an SELinux
    context under an "AppArmor" heading (#104).

    **The LSM is identified from sysfs, not from the per-LSM attribute files.**
    The obvious discriminator - ``attr/selinux/current`` versus
    ``attr/apparmor/current`` - does not work. Measured 2026-08-19 in a seat on
    a p47-beamline RHEL9 node: ``attr/current`` held the context while *both*
    per-LSM paths read empty. The k3s bed answers the same way from the other
    end: no ``/sys/fs/selinux`` at all, ``apparmor`` compiled in but disabled,
    an ``attr/apparmor`` directory present regardless, and ``attr/current``
    failing outright with ``EINVAL``.

    ``None`` for the label means "no label" - absent, unreadable or empty. It
    is deliberately *not* rendered as "unconfined": AppArmor spells that state
    itself, and inventing it for an empty slot is how a node with no LSM came
    to look like a confined one.
    """
    text = _read_text(_pid_dir(pid, proc) / "attr" / "current")
    label = None if text is None else text.replace("\x00", "").strip() or None
    return _active_lsm(sysfs=sysfs), label


def _active_lsm(*, sysfs: Path = DEFAULT_SYS) -> Lsm:
    if (sysfs / "fs" / "selinux").is_dir():
        # selinuxfs is mounted only where SELinux is enabled, and the seat sees
        # it: it is the one signal that was present on the measured node.
        return Lsm.SELINUX
    enabled = _read_text(sysfs / "module" / "apparmor" / "parameters" / "enabled")
    if enabled is not None and enabled.strip().upper().startswith("Y"):
        # The parameter exists wherever AppArmor is compiled in and reads `N`
        # when it is off - which is how the k3s bed is booted (`apparmor=0`).
        return Lsm.APPARMOR
    return Lsm.NONE


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
        # One read of `status` for the four fields taken from it, rather than
        # one read each. The walk now covers four fields instead of two and
        # runs over every process in the namespace — 112 of them on the opis
        # pod — so re-opening the same file per field is the difference between
        # a listing that is free and one that is noticeable.
        status = _read_text(_pid_dir(pid, proc) / "status") or ""
        state = status_field(status, "State")
        processes.append(
            ProcInfo(
                pid=pid,
                # Not `or 0`: an unreadable uid is not root. Collapsing the two
                # would hand the degraded rung a runAsUser of 0, which the
                # report forbids outright (3.11).
                uid=_first_of(status_field(status, "Uid")),
                comm=comm,
                cmdline=cmdline or f"[{comm}]",
                container_id=_container_id_from_cgroup(cgroup),
                is_target=_is_target(cid, cgroup, own_cgroup, comm),
                ppid=_parse_int(status_field(status, "PPid")),
                state=None if state is None else state_letter(state),
                threads=_parse_int(status_field(status, "Threads")),
                ptrace_readable=ptrace_readable(pid, proc=proc),
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
"""Process names that are never the thing anyone came to debug.

Matched against ``comm`` *and* against ``argv[0]``'s basename — see
:func:`is_shell` for why one of the two is not enough.

Entrypoint shells and init shims: they have no debug info, breaking in one stops
the supervisor rather than the workload, and on any image whose entrypoint is a
script one of them is guaranteed to be the *lowest* pid in the container.
``tini`` and ``dumb-init`` are here for the same reason and not because they are
shells.
"""


def is_shell(info: ProcInfo) -> bool:
    """Whether this process is an entrypoint shell or an init shim.

    Two sources, because neither alone is reliable. ``comm`` is truncated at 15
    characters and any process may rewrite it with ``PR_SET_NAME``, so it can
    fail to say "shell" when the process is one; ``argv[0]`` is the image's own
    spelling and says what was actually exec'd.

    Only ``argv[0]``, and only its basename — never the rest of the line. The
    simdet IOC runs ``/venv/bin/python /venv/bin/stdio-socket
    /epics/ioc/start.sh`` under ``comm=stdio-socket``: a rule that looked for
    ``start.sh`` anywhere in the command line would drop the one process that
    pod has (measured on ``p47-simdet`` 2026-08-19).

    >>> is_shell(ProcInfo(447, 999, "sh", "/bin/sh -s rabbit_disk_monitor"))
    True
    >>> is_shell(ProcInfo(1, 37887, "stdio-socket",
    ...                   "/venv/bin/python /venv/bin/stdio-socket "
    ...                   "/epics/ioc/start.sh"))
    False
    >>> is_shell(ProcInfo(1, 999, "beam.smp", "/opt/erlang/bin/beam.smp -W w"))
    False
    """
    if info.comm in _SHELLS:
        return True
    argv = info.cmdline.split()
    return bool(argv) and PurePosixPath(argv[0]).name in _SHELLS


def debug_candidates(targets: Sequence[ProcInfo]) -> list[ProcInfo]:
    """The target container's processes worth debugging, best first.

    **Sorted, never filtered.** A container that is only a shell, or only
    zombies, still has to name something, and the degraded rung has to have a
    pid to report on. What gets *dropped* is a decision for the consumer —
    :func:`podbench.gdbcmd.resolve_target_pids` drops the dead and caps the
    rest, because a dropdown entry is selectable and a report is not.

    The key, best first. Every element after the first is a measured correction
    to a pod where depth alone picked the wrong process (2026-08-19, p47):

    #. **not a shell.** An entrypoint script is a wrapper; see :func:`is_shell`.
    #. **alive.** blueapi leaves an unreaped ``multiprocessing`` child two levels
       below pid 1, and depth picked it on every fresh pod. It has no
       ``mm_struct``, so the read matrix scored it 3/6 and the verdict came back
       "ptrace denied, blocker unknown" — while ``PTRACE_ATTACH`` to pid 1 in the
       same second succeeded (issue #52 is this, not an LSM).
    #. **ptrace-readable from this seat.** opis runs an ``nginx`` master at uid 0
       and 111 workers at uid 101; the seat is root but capless, so it can ptrace
       the master and none of the workers — and depth prefers a worker. See
       :func:`ptrace_readable` for why a denial counts and a success does not.
    #. **thread count, descending.** rabbitmq's ``beam.smp`` carries 208 threads
       at depth 1 and loses to the ``inet_gethost`` DNS helper it forks, which
       carries one at depth 3. A comparator only: see
       :attr:`~podbench.model.ProcInfo.threads`.
    #. **depth, deepest first** — the original signal, now the tie-break it was
       always better suited to being.
    #. **pid**, so the answer is stable across two calls a second apart.

    Shells contribute neither threads nor depth to the key, so among themselves
    they stay in pid order and the fallback is the container's own entrypoint.
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

    The zombie blueapi produces on every restart, which depth put first:

    >>> blueapi = [
    ...     ProcInfo(1, 1000, "python", "python -m blueapi serve",
    ...              state="S", threads=195, ptrace_readable=True),
    ...     ProcInfo(233, 1000, "python", "python -c spawn_main", ppid=1,
    ...              state="S", threads=81, ptrace_readable=True),
    ...     ProcInfo(518, 1000, "python", "[python]", ppid=233,
    ...              state="Z", threads=1, ptrace_readable=False),
    ... ]
    >>> [p.pid for p in debug_candidates(blueapi)]
    [1, 233, 518]
    """
    depths = _depths(targets)

    def rank(info: ProcInfo) -> tuple[bool, bool, bool, int, int, int]:
        shell = is_shell(info)
        return (
            shell,
            not info.alive,
            # `is False`, not `not`: None means the readability was never
            # measured, and an unmeasured process must rank with the readable
            # ones rather than be demoted for a question nobody asked.
            info.ptrace_readable is False,
            0 if shell else -(info.threads or 0),
            0 if shell else -depths[info.pid],
            info.pid,
        )

    return sorted(targets, key=rank)


NAMED_IN_NOTE = 4
"""How many of the runners-up :func:`candidate_note` names before counting.

Four, so the note lists exactly what ``debug-config`` offers
(:data:`podbench.gdbcmd.MAX_OFFERED_PIDS`, the chosen one plus these). opis
carries 111 identical ``nginx`` workers, and naming them all turned a one-line
note into a paragraph nobody would read.
"""


def candidate_note(candidates: Sequence[ProcInfo], verb: str) -> str | None:
    """Say which of several processes was picked, and what it was picked over.

    ``None`` when there was nothing to choose. One line, unwrapped: every caller
    folds notes itself, and a newline here would land inside ``debug-config:``'s
    per-line prefix.

    The runners-up are *named* rather than counted, up to
    :data:`NAMED_IN_NOTE`. "4 processes, debugging pid 13" reads like a coin
    toss, and the whole point of :func:`debug_candidates` is that it is not one
    — while the reader who disagrees with the choice needs the pid of the thing
    they would rather have.

    The disqualified are named under what disqualified them rather than lumped
    in with the alternatives, because "attach to it explicitly" is only advice
    worth taking for a process that could be attached.

    >>> tree = [
    ...     ProcInfo(13, 0, "ioc", "ioc", ppid=11, threads=27),
    ...     ProcInfo(8, 0, "python", "python", ppid=1, threads=3),
    ...     ProcInfo(1, 0, "bash", "bash", ppid=0, threads=1),
    ... ]
    >>> for sentence in candidate_note(tree, "debugging").split(". "):
    ...     print(sentence)
    target container has 3 processes; debugging pid 13 (ioc), 27 threads against 3
    Also debuggable: 8 (python)
    Skipped as a wrapper: 1 (bash)
    Run `podbench pids` to choose another.

    blueapi, whose zombie used to be the answer:

    >>> blueapi = debug_candidates([
    ...     ProcInfo(1, 1000, "python", "python -m blueapi",
    ...              state="S", threads=195, ptrace_readable=True),
    ...     ProcInfo(518, 1000, "python", "[python]", ppid=1,
    ...              state="Z", threads=1, ptrace_readable=False),
    ... ])
    >>> for sentence in candidate_note(blueapi, "probing").split(". "):
    ...     print(sentence)
    target container has 2 processes; probing pid 1 (python), the only live process here
    Skipped as dead: 518 (python)
    Run `podbench pids` to choose another.
    """
    if len(candidates) < 2:
        return None
    chosen, rest = candidates[0], candidates[1:]
    parts = [
        f"target container has {len(candidates)} processes; "
        f"{verb} pid {chosen.pid} ({chosen.comm}), {_why(chosen, rest[0])}"
    ]
    for label, group in (
        ("Also debuggable", [p for p in rest if _usable(p)]),
        ("Skipped as a wrapper", [p for p in rest if is_shell(p)]),
        ("Skipped as dead", [p for p in rest if not is_shell(p) and not p.alive]),
        (
            "Not ptrace-readable from this seat",
            [
                p
                for p in rest
                if not is_shell(p) and p.alive and p.ptrace_readable is False
            ],
        ),
    ):
        if group:
            parts.append(f"{label}: {_named(group)}")
    parts.append("Run `podbench pids` to choose another.")
    return ". ".join(parts)


def _usable(info: ProcInfo) -> bool:
    """Whether this candidate is one the reader could actually switch to."""
    return not is_shell(info) and info.alive and info.ptrace_readable is not False


def _named(group: Sequence[ProcInfo]) -> str:
    named = ", ".join(f"{p.pid} ({p.comm})" for p in group[:NAMED_IN_NOTE])
    extra = len(group) - NAMED_IN_NOTE
    return named if extra <= 0 else f"{named} and {extra} more"


def _why(chosen: ProcInfo, runner_up: ProcInfo) -> str:
    """The predicate that actually separated the winner from the next one.

    Not a fixed phrase and not a recital of the winner's own fields: on opis
    every process has one thread, so "1 threads" would be true, useless, and
    would name the wrong reason. The list is sorted on the key in
    :func:`debug_candidates`, so the first element of that key on which these
    two differ *is* the reason, and every phrase below is exactly true because
    of the sort — "the only live process here" is safe precisely because a
    second live one would have been the runner-up.
    """
    # A shell at the head means every candidate was one, since shells sort last.
    if is_shell(chosen):
        return "the only process here"
    if not chosen.alive:
        return "dead, and nothing here is alive - no seat can attach to this pod"
    if chosen.ptrace_readable is False:
        return "not ptrace-readable from this seat, and nothing here is"
    if is_shell(runner_up):
        return "the only process here that is not a wrapper"
    if not runner_up.alive:
        return "the only live process here"
    if runner_up.ptrace_readable is False:
        return "the only process here this seat can ptrace-read"
    if chosen.threads and runner_up.threads and chosen.threads > runner_up.threads:
        return f"{chosen.threads} threads against {runner_up.threads}"
    return "the deepest live process this seat can read"


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
    if comm == PAUSE_COMM:
        # PID 1 under shareProcessNamespace is the pod's pause process, never a
        # debugging target (report §3.15).
        return False
    if own_cgroup is not None:
        return cgroup != own_cgroup
    return cgroup != "0::/"
