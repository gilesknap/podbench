"""Iterate mode: the dev pod, and the edit → relaunch → verify loop inside it.

Iterate mode is the one mode that *replaces* the workload rather than sitting
beside it. A sacrificial clone of the target pod is authored (never
``kubectl debug --copy-to``, which strips every label and so leaves the clone
invisible to the Service — report §3.5), its app container is idled, and a
podbench sidecar carries the editor, the checkout and the interpreter. The user
edits, runs ``podbench run``, and the change is visible through the Service in
about a second (report §4.4: 1.18 s per edit → relaunch → verify cycle).

This module owns both ends of that:

* **laptop side** — ``dev`` creates the clone and prints how to reach it,
  ``dev --delete`` takes it away and puts any borrowed Service traffic back;
* **in-pod side** — ``dev-bootstrap`` populates the workspace, ``run``/``stop``
  are the inner loop.

Four findings shape almost every function here, and each one is a *silent*
failure if ignored:

1. **Service traffic is opt-in, and a cutover must be a JSON ``replace``.** A
   merge patch unions the selector maps instead of replacing them, which leaves
   the dev pod's label added to the original selector and drops the original pod
   out of the endpointslice (report §4.4). The original selector is recorded on
   the dev pod so teardown can restore it exactly.
2. **Pre-flight the port before every relaunch.** A second ``SO_REUSEPORT`` bind
   succeeds with no error and the kernel splits traffic between old and new code
   (measured: 5 new / 3 old / 2 new), and after such a listener has served
   connections a plain rebind fails for the whole ~60 s ``TIME_WAIT`` window
   *while ``ss -lntp`` shows no listener at all* (report §3.16).
3. **A socket poll alone gives a false PASS.** S4's naive wrapper reported
   ``LISTENING after 1 polls``, exit 0, for a relaunch that had already died with
   ``EADDRINUSE`` — the Service kept serving stale code. So the wrapper tracks
   its own child pid and matches the listening socket's inode against
   ``/proc/<pid>/fd``.
4. **Never ``pkill -f``.** Under ``shareProcessNamespace: true`` it matches the
   invoking shell and every other container's processes. Everything here kills
   by recorded pid.
"""

from __future__ import annotations

import enum
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Any, Protocol, cast

import typer

from . import spec
from .agent import PUBKEY_ENV
from .cli import new_app, require_subcommand, run
from .console import emit
from .kubectl import Kubectl, KubectlError, Runner, run_subprocess
from .launcher import (
    DEFAULT_CLIENT_DIR,
    DEFAULT_IDENTITY,
    LauncherError,
    Session,
    SshSeat,
    client_dir,
    declared_volumes,
    emit_ssh_config,
    forget_ssh_config,
    kubectl_for,
    match_pod_choices,
    pod_choices,
    probe_ssh_identity,
    read_public_key,
    resolve_among,
    resolve_pod,
    resolve_pod_name,
    rung_of_spec,
    spec_env,
)
from .model import (
    DEFAULT_IMAGE,
    SEAT_IDENTITY_VOLUME,
    TARGET_CID_ENV,
    ContainerRef,
    PodRef,
    Rung,
    as_dict,
)
from .proc import DEFAULT_PROC, read_cgroup, read_cmdline, read_comm, same_root
from .sshcfg import host_key_alias

__all__ = [
    "CUTOVER_SELECTOR_ANNOTATION",
    "CUTOVER_SERVICE_ANNOTATION",
    "DEFAULT_IMAGE",
    "DEV_POD_SUFFIX",
    "LISTENERS_COMMAND",
    "TIME_WAIT_COMMAND",
    "BootstrapPlan",
    "Cutover",
    "DevError",
    "DevPod",
    "LaunchResult",
    "ListenerCheck",
    "MountNamespaceError",
    "PortOwner",
    "PortPreflight",
    "ProcessSnapshot",
    "RunState",
    "Side",
    "SocketEntry",
    "Spawner",
    "StopResult",
    "bootstrap",
    "bootstrap_commands",
    "clear_state",
    "connection_summary",
    "create_dev_pod",
    "cutover_annotations",
    "default_target_port",
    "delete_dev_pod",
    "dev_pod_name",
    "gitops_manager",
    "identify_owner",
    "is_dev_pod",
    "listeners_on",
    "load_state",
    "main",
    "parse_ss",
    "refuse_if_gitops_managed",
    "path_side",
    "preflight_port",
    "read_process",
    "recorded_cutover",
    "save_state",
    "seat_ssh_config",
    "sidecar_session",
    "socket_inodes",
    "sole_container",
    "spawn_detached",
    "start",
    "state_path",
    "stop",
    "time_wait_count",
    "validate_workspace_layout",
    "verify_listener",
]

DEV_POD_SUFFIX = "-podbench"
MAX_POD_NAME = 63
"""RFC 1123 label limit; the API server rejects a longer pod name."""

DEFAULT_WORKSPACE = spec.WORKSPACE_MOUNT_PATH
STATE_DIR = ".podbench"
STATE_FILE = "run.json"
LOG_FILE = "run.log"

CUTOVER_SERVICE_ANNOTATION = "podbench.dev/cutover-service"
CUTOVER_SELECTOR_ANNOTATION = "podbench.dev/cutover-selector"
"""Where a borrowed Service's original selector is parked.

On the dev pod rather than in a file on the laptop: teardown then works from any
machine holding the kubeconfig, and "leaves nothing behind" does not depend on
the developer keeping the same shell.
"""

LISTENERS_COMMAND: tuple[str, ...] = ("ss", "-lntpe")
"""``-l`` listeners, ``-n`` numeric, ``-t`` tcp, ``-p`` owning pid, ``-e`` the
socket inode. The pid alone is not enough — the inode is what ties the socket to
*our* child rather than to whoever ``ss`` happened to attribute it to."""

TIME_WAIT_COMMAND: tuple[str, ...] = ("ss", "-tan", "state", "time-wait")
"""``-l`` cannot see ``TIME_WAIT``, and ``TIME_WAIT`` is precisely the state that
makes a port unbindable while looking free (report §3.16). Filtering by state
makes ``ss`` drop the State column, so the parser is told what it is reading."""

_TCP_STATES = frozenset(
    {
        "LISTEN",
        "ESTAB",
        "TIME-WAIT",
        "SYN-SENT",
        "SYN-RECV",
        "FIN-WAIT-1",
        "FIN-WAIT-2",
        "CLOSE-WAIT",
        "LAST-ACK",
        "CLOSING",
        "CLOSED",
        "UNCONN",
        "UNKNOWN",
    }
)

_USERS_RE = re.compile(r'\("(?P<name>[^"]*)",pid=(?P<pid>\d+),fd=(?P<fd>\d+)\)')
_INO_RE = re.compile(r"\bino:(\d+)")
_SOCKET_RE = re.compile(r"socket:\[(\d+)\]")
_TARGET_ROOT_RE = re.compile(r"^/proc/(\d+)/root(/|$)")

Killer = Callable[[int, int], None]
"""How a signal is delivered. Injected so tests never signal a real process."""


class DevError(RuntimeError):
    """Iterate mode refused to do something, and says why."""


class MountNamespaceError(DevError):
    """A layout that would straddle the debug and target mount namespaces."""


# -- the mount-namespace rule ---------------------------------------------


class Side(enum.Enum):
    """Which container's filesystem a path resolves in."""

    DEBUG = "debug"
    """This container: the workspace volume, the checkout, uv's interpreters."""

    TARGET = "target"
    """The application container, reached through ``/proc/<pid>/root``."""


def path_side(path: str) -> Side:
    """Which mount namespace ``path`` names.

    ``/proc/self/root`` and ``/proc/thread-self/root`` are *our* root, so they
    are the debug side; a numeric pid is the bridge into somebody else's.

    >>> path_side("/workspace/src")
    <Side.DEBUG: 'debug'>
    >>> path_side("/proc/1/root/app")
    <Side.TARGET: 'target'>
    >>> path_side("/proc/self/root/workspace")
    <Side.DEBUG: 'debug'>
    """
    return Side.TARGET if _TARGET_ROOT_RE.match(path) else Side.DEBUG


def validate_workspace_layout(
    *,
    interpreter: str | None = None,
    venv: str | None = None,
    checkout: str | None = None,
) -> None:
    """Refuse a layout that puts these three in different mount namespaces.

    An editable install writes a ``.pth`` into the *interpreter's*
    site-packages naming the checkout. If the interpreter is the target's and
    the checkout is ours, that path does not exist in the namespace that
    resolves it, and site.py **silently ignores it**: no warning, just a
    ``ModuleNotFoundError`` for the package much later, in something that looks
    unrelated. The exec-style ``.pth`` that PEP 660 installs emits a traceback
    and then carries on with exit 0 (report §3.17).

    The obvious workaround does not exist either: the ``/proc/<pid>/root``
    bridge is one-directional by capability — the sidecar can read the app's
    rootfs, the app cannot see the sidecar's — so nothing in the target can be
    pointed at the workspace. Podbench therefore standardises on **everything in
    the debug container**, and this is where that is enforced rather than
    discovered.
    """
    supplied = {
        "interpreter": interpreter,
        "venv": venv,
        "checkout": checkout,
    }
    offenders = [
        f"{role} {value!r}"
        for role, value in supplied.items()
        if value is not None and path_side(value) is Side.TARGET
    ]
    if not offenders:
        return
    present = [role for role, value in supplied.items() if value is not None]
    everything = len(offenders) == len(present)
    detail = (
        "the target container's filesystem cannot host any of them: podbench "
        "can read it through /proc/<pid>/root but cannot write there, and the "
        "target cannot see the workspace at all — the bridge is one-directional "
        "by capability"
        if everything
        else "an editable install spanning both namespaces dangles silently: "
        "site.py drops a .pth pointing at a directory that does not exist in "
        "the namespace resolving it, with no warning at all"
    )
    raise MountNamespaceError(
        "interpreter, venv and checkout must all live in the debug container; "
        f"{', '.join(offenders)} resolve(s) in the target's mount namespace. "
        f"{detail}."
    )


# -- reading ss ------------------------------------------------------------


@dataclass(frozen=True)
class SocketEntry:
    """One row of ``ss`` output."""

    state: str | None
    local: str
    peer: str
    port: int | None
    pids: tuple[int, ...] = ()
    processes: tuple[str, ...] = ()
    inode: int | None = None

    @property
    def is_listening(self) -> bool:
        return self.state == "LISTEN"


def parse_ss(text: str, *, default_state: str | None = None) -> list[SocketEntry]:
    """Parse ``ss`` output into rows.

    Both shapes this module runs are handled: ``ss -lntpe`` leads with a State
    column, while filtering by state (``ss -tan state time-wait``) makes ``ss``
    drop that column entirely — hence ``default_state``, which names what the
    filter already decided.

    The process and inode fields are found by pattern rather than by position,
    because ``-e`` appends its own key:value fields around ``users:(…)`` and
    their order is not contractual.

    >>> row = parse_ss("LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:*")[0]
    >>> row.port, row.is_listening
    (8080, True)
    """
    entries: list[SocketEntry] = []
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] in ("State", "Recv-Q", "Netid"):
            continue
        state = default_state
        index = 0
        if fields[0] in _TCP_STATES:
            state = fields[0]
            index = 1
        if len(fields) < index + 4:
            continue
        if not (fields[index].isdigit() and fields[index + 1].isdigit()):
            continue
        local = fields[index + 2]
        peer = fields[index + 3]
        users = _USERS_RE.findall(line)
        inode = _INO_RE.search(line)
        entries.append(
            SocketEntry(
                state=state,
                local=local,
                peer=peer,
                port=_port_of(local),
                pids=tuple(int(pid) for _, pid, _ in users),
                processes=tuple(name for name, _, _ in users),
                inode=int(inode.group(1)) if inode else None,
            )
        )
    return entries


def _port_of(address: str) -> int | None:
    _, _, port = address.rpartition(":")
    return int(port) if port.isdigit() else None


def listeners_on(entries: Sequence[SocketEntry], port: int) -> list[SocketEntry]:
    """The listening sockets bound to ``port``, on any address."""
    return [entry for entry in entries if entry.is_listening and entry.port == port]


def time_wait_count(entries: Sequence[SocketEntry], port: int) -> int:
    """How many sockets are sitting in ``TIME_WAIT`` on ``port``."""
    return sum(
        1
        for entry in entries
        if entry.port == port
        and (entry.state or "").upper() in ("TIME-WAIT", "TIME_WAIT")
    )


# -- attributing a socket to a process and a container ---------------------


@dataclass(frozen=True)
class ProcessSnapshot:
    """The parts of ``/proc/<pid>/stat`` that decide whether a pid is ours."""

    pid: int
    state: str
    start_ticks: int | None
    """Field 22 of ``stat``. Recorded with the pid so a recycled pid number
    cannot be mistaken for the process we launched."""

    @property
    def alive(self) -> bool:
        """Whether the process is running, as opposed to a zombie.

        ``kill -0`` is not this check. Under ``shareProcessNamespace`` pid 1 is
        the target application, which reaps nothing, so an orphaned child stays
        a zombie for the pod's lifetime — and a zombie answers ``kill -0``
        happily. That is exactly how a bootstrap script once reported a dead
        server as healthy and handed a client a dead port (report §3.19).
        """
        return self.state not in ("Z", "X", "x")


def read_process(pid: int, *, proc: Path = DEFAULT_PROC) -> ProcessSnapshot | None:
    """Read ``/proc/<pid>/stat``. ``None`` when the process is gone."""
    try:
        text = (proc / str(pid) / "stat").read_text(errors="replace")
    except OSError:
        return None
    # comm sits in parentheses and may itself contain spaces and parens, so the
    # split point is the *last* ')' and never a naive field index.
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 1 :].split()
    if not fields:
        return None
    start = fields[19] if len(fields) > 19 else None
    return ProcessSnapshot(
        pid=pid,
        state=fields[0],
        start_ticks=int(start) if start is not None and start.isdigit() else None,
    )


def socket_inodes(pid: int, *, proc: Path = DEFAULT_PROC) -> set[int]:
    """Every socket inode this process holds open, from ``/proc/<pid>/fd``."""
    directory = proc / str(pid) / "fd"
    try:
        names = os.listdir(directory)
    except OSError:
        return set()
    inodes: set[int] = set()
    for name in names:
        try:
            link = os.readlink(directory / name)
        except OSError:
            continue
        match = _SOCKET_RE.match(link)
        if match:
            inodes.add(int(match.group(1)))
    return inodes


@dataclass(frozen=True)
class PortOwner:
    """Who is holding a port, named well enough to act on."""

    pid: int
    comm: str | None = None
    cmdline: str | None = None
    container_id: str | None = None
    same_container: bool | None = None
    is_target: bool = False

    def describe(self) -> str:
        """One line naming the process and which container it lives in."""
        where = (
            "this container"
            if self.same_container
            else "the target container"
            if self.is_target
            else "another container in this pod"
            if self.same_container is False
            else "an unidentified container"
        )
        what = self.cmdline or self.comm or "?"
        return f"pid {self.pid} ({what}) in {where}"


def identify_owner(
    pid: int,
    *,
    proc: Path = DEFAULT_PROC,
    target_cid: str | None = None,
) -> PortOwner:
    """Attribute a pid to a container.

    ``/proc/<pid>/root`` is the discriminator the report names: two processes
    share a root inode exactly when they share a mount namespace, which under
    ``shareProcessNamespace: true`` is the only thing that still distinguishes
    containers. The target container id refines "not mine" into "the app", and
    is a substring match because the sidecar's own cgroup namespace makes the
    path it reads relative (report §3.15).
    """
    cgroup = read_cgroup(pid, proc=proc)
    return PortOwner(
        pid=pid,
        comm=read_comm(pid, proc=proc),
        cmdline=read_cmdline(pid, proc=proc) or None,
        container_id=cgroup,
        same_container=same_root(pid, proc=proc),
        is_target=bool(target_cid and cgroup and target_cid in cgroup),
    )


# -- the port pre-flight ---------------------------------------------------


@dataclass(frozen=True)
class PortPreflight:
    """What is standing between the relaunch and the port."""

    port: int
    listeners: tuple[SocketEntry, ...] = ()
    owners: tuple[PortOwner, ...] = ()
    time_wait: int = 0
    error: str | None = None

    @property
    def clear(self) -> bool:
        """Whether it is safe to bind.

        An unreadable ``ss`` counts as not clear. Failing open here would put
        back exactly the guess this pre-flight exists to remove.
        """
        return self.error is None and not self.listeners

    def message(self) -> str:
        """The line the user is shown, which must tell them what to do next."""
        if self.error is not None:
            return f"port {self.port}: pre-flight failed: {self.error}"
        if self.listeners:
            owners = "; ".join(owner.describe() for owner in self.owners) or "unknown"
            return (
                f"port {self.port} is already bound by {owners}. Relaunching "
                "anyway is not safe: if that listener set SO_REUSEPORT the "
                "second bind succeeds silently and the kernel splits traffic "
                "between old and new code, with nothing in any log to say so. "
                "Stop the owner first (podbench stop, or kill by that pid)."
            )
        if self.time_wait:
            return (
                f"port {self.port} is free but {self.time_wait} socket(s) are in "
                "TIME_WAIT. If the previous listener used SO_REUSEPORT a plain "
                "rebind will fail for up to ~60 s from its last connection, "
                "while ss shows no listener at all. Waiting is the fix; "
                "EADDRINUSE here is not a bug in your code."
            )
        return f"port {self.port} is free"


def preflight_port(
    port: int,
    *,
    runner: Runner | None = None,
    proc: Path = DEFAULT_PROC,
    target_cid: str | None = None,
) -> PortPreflight:
    """Look at the port before binding it, every single relaunch.

    Two ``ss`` invocations because one cannot answer both questions: ``-l``
    never shows ``TIME_WAIT``, and that is the state that makes a port refuse a
    bind while appearing free.
    """
    run = runner if runner is not None else run_subprocess
    listing = run(list(LISTENERS_COMMAND))
    if listing.returncode != 0:
        return PortPreflight(
            port=port,
            error=(
                f"{' '.join(LISTENERS_COMMAND)} exited {listing.returncode}: "
                f"{listing.stderr.strip() or listing.stdout.strip()}"
            ),
        )
    listeners = listeners_on(parse_ss(listing.stdout), port)

    waiting = run(list(TIME_WAIT_COMMAND))
    time_wait = (
        time_wait_count(parse_ss(waiting.stdout, default_state="TIME-WAIT"), port)
        if waiting.returncode == 0
        else 0
    )

    owners = tuple(
        identify_owner(pid, proc=proc, target_cid=target_cid)
        for entry in listeners
        for pid in entry.pids
    )
    return PortPreflight(
        port=port,
        listeners=tuple(listeners),
        owners=owners,
        time_wait=time_wait,
    )


@dataclass(frozen=True)
class ListenerCheck:
    """Whether *our* child owns the listening socket."""

    ok: bool
    detail: str
    entry: SocketEntry | None = None


def verify_listener(
    pid: int,
    port: int,
    *,
    runner: Runner | None = None,
    proc: Path = DEFAULT_PROC,
    target_cid: str | None = None,
) -> ListenerCheck:
    """Confirm ``pid`` — and nobody else — is serving ``port``.

    Polling the port is not enough and never was: S4's naive wrapper printed
    ``LISTENING after 1 polls`` and exited 0 for a relaunch that had died with
    ``EADDRINUSE``, while the Service happily served the old code. So the check
    is two-sided — ``ss`` must attribute the socket to our pid, and that pid
    must hold the socket's inode open in ``/proc/<pid>/fd``.
    """
    run = runner if runner is not None else run_subprocess
    listing = run(list(LISTENERS_COMMAND))
    if listing.returncode != 0:
        return ListenerCheck(
            False, f"could not read listeners: {listing.stderr.strip()}"
        )
    entries = listeners_on(parse_ss(listing.stdout), port)
    if not entries:
        return ListenerCheck(False, f"nothing is listening on port {port}")

    ours = [entry for entry in entries if pid in entry.pids]
    if not ours:
        owners = "; ".join(
            identify_owner(other, proc=proc, target_cid=target_cid).describe()
            for entry in entries
            for other in entry.pids
        )
        return ListenerCheck(
            False,
            f"port {port} is served by {owners or 'an unattributable process'}, "
            f"not by our child (pid {pid}). Either the relaunch lost the race or "
            "SO_REUSEPORT split the port between two processes.",
            entries[0],
        )

    entry = ours[0]
    if entry.inode is None:
        return ListenerCheck(
            True,
            f"pid {pid} owns the listener on port {port} "
            "(ss attributed it; no inode reported, so it was not cross-checked)",
            entry,
        )
    if entry.inode not in socket_inodes(pid, proc=proc):
        return ListenerCheck(
            False,
            f"ss attributes port {port} to pid {pid} but socket inode "
            f"{entry.inode} is not open in /proc/{pid}/fd — the attribution is "
            "stale, so the process answering is not the one we started.",
            entry,
        )
    return ListenerCheck(
        True, f"pid {pid} owns the listening socket on port {port}", entry
    )


# -- the recorded child ----------------------------------------------------


@dataclass(frozen=True)
class RunState:
    """The one thing that makes ``stop`` safe: the pid we actually started."""

    pid: int
    port: int
    command: tuple[str, ...]
    cwd: str
    log: str
    started_at: float
    start_ticks: int | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "pid": self.pid,
                "port": self.port,
                "command": list(self.command),
                "cwd": self.cwd,
                "log": self.log,
                "started_at": self.started_at,
                "start_ticks": self.start_ticks,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> RunState | None:
        """Parse state, returning ``None`` for anything unusable.

        A corrupt state file must not be fatal: the loop has to remain usable
        after a container restart wiped the writable layer mid-write.
        """
        try:
            parsed: object = json.loads(text)
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        data = cast(dict[str, Any], parsed)
        pid = data.get("pid")
        port = data.get("port")
        if not isinstance(pid, int) or not isinstance(port, int):
            return None
        command = data.get("command")
        ticks = data.get("start_ticks")
        return cls(
            pid=pid,
            port=port,
            command=tuple(str(part) for part in cast(list[Any], command or [])),
            cwd=str(data.get("cwd", "")),
            log=str(data.get("log", "")),
            started_at=float(cast(float, data.get("started_at", 0.0))),
            start_ticks=ticks if isinstance(ticks, int) else None,
        )

    def is_live(self, *, proc: Path = DEFAULT_PROC) -> bool:
        """Whether the recorded process is still the one we started and running.

        The start time is compared as well as the pid: pids are recycled, and
        signalling a stranger because a number came round again is precisely the
        class of accident ``pkill -f`` is banned for.
        """
        snapshot = read_process(self.pid, proc=proc)
        if snapshot is None or not snapshot.alive:
            return False
        if self.start_ticks is None or snapshot.start_ticks is None:
            return True
        return snapshot.start_ticks == self.start_ticks


def state_path(workspace: str = DEFAULT_WORKSPACE) -> Path:
    """Where the recorded pid lives. On the workspace volume, not in ``/tmp``."""
    return Path(workspace) / STATE_DIR / STATE_FILE


def load_state(workspace: str = DEFAULT_WORKSPACE) -> RunState | None:
    """The last recorded child, or ``None``."""
    path = state_path(workspace)
    try:
        return RunState.from_json(path.read_text())
    except OSError:
        return None


def save_state(state: RunState, workspace: str = DEFAULT_WORKSPACE) -> None:
    """Record the child before verifying it, so a crash still leaves a pid."""
    path = state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.to_json())


def clear_state(workspace: str = DEFAULT_WORKSPACE) -> None:
    """Forget the recorded child."""
    state_path(workspace).unlink(missing_ok=True)


class Spawner(Protocol):
    """How the inner loop starts a process. A seam, so tests fork nothing."""

    def __call__(self, argv: Sequence[str], *, cwd: str, log: str) -> int:
        """Start ``argv`` detached and return its pid."""
        ...


def spawn_detached(argv: Sequence[str], *, cwd: str, log: str) -> int:
    """Start the app in its own session, output appended to ``log``.

    ``start_new_session`` is ``setsid``: ``podbench run`` is a short-lived CLI,
    and the app must neither die with it nor take a Ctrl-C aimed at it. stdin is
    ``/dev/null`` for the same reason — a server inheriting a closed-then-reused
    terminal is a debugging session nobody enjoys.
    """
    log_path = Path(log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as handle:
        process = subprocess.Popen(  # noqa: S603 - argv is the user's own command
            list(argv),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
    return process.pid


@dataclass(frozen=True)
class StopResult:
    """What ``stop`` did."""

    ok: bool
    detail: str
    pid: int | None = None


def stop(
    *,
    workspace: str = DEFAULT_WORKSPACE,
    proc: Path = DEFAULT_PROC,
    killer: Killer | None = None,
    grace: float = 5.0,
    poll: float = 0.05,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> StopResult:
    """Kill the recorded child, by pid, and forget it.

    There is no pattern match anywhere in this function and there must never be
    one. ``pkill -f`` under ``shareProcessNamespace: true`` matched the invoking
    shell (which died with 143) and every other container's processes; the
    recorded pid is the only safe target.
    """
    kill = killer if killer is not None else os.kill
    state = load_state(workspace)
    if state is None:
        return StopResult(True, "nothing recorded to stop")
    if not state.is_live(proc=proc):
        clear_state(workspace)
        return StopResult(
            True, f"pid {state.pid} is already gone; cleared the record", state.pid
        )

    kill(state.pid, signal.SIGTERM)
    deadline = now() + grace
    while now() < deadline:
        if not state.is_live(proc=proc):
            clear_state(workspace)
            return StopResult(True, f"stopped pid {state.pid} (SIGTERM)", state.pid)
        sleep(poll)

    kill(state.pid, signal.SIGKILL)
    clear_state(workspace)
    return StopResult(
        True,
        f"pid {state.pid} ignored SIGTERM for {grace:g}s; sent SIGKILL",
        state.pid,
    )


@dataclass(frozen=True)
class LaunchResult:
    """The outcome of one relaunch, and enough detail to act on a failure."""

    ok: bool
    detail: str
    state: RunState | None = None
    preflight: PortPreflight | None = None


def start(
    command: Sequence[str],
    *,
    port: int,
    workspace: str = DEFAULT_WORKSPACE,
    cwd: str | None = None,
    log: str | None = None,
    runner: Runner | None = None,
    spawner: Spawner | None = None,
    proc: Path = DEFAULT_PROC,
    target_cid: str | None = None,
    timeout: float = 15.0,
    poll: float = 0.05,
    grace: float = 5.0,
    killer: Killer | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> LaunchResult:
    """Stop the previous child, pre-flight the port, relaunch, and *verify*.

    The whole cycle is budgeted at 1.18 s end to end (report §4.4), so the
    verification is a tight poll rather than a fixed sleep — but it is a real
    verification: the child must be alive *and* own the listening socket before
    this returns ok. Every failure path names what happened, because the
    alternative is the user reading a stale response through the Service and
    concluding their edit did not take.
    """
    if not command:
        raise DevError("nothing to run: pass the command after `--`")
    validate_workspace_layout(interpreter=command[0], checkout=cwd or workspace)

    stopped = stop(
        workspace=workspace,
        proc=proc,
        killer=killer,
        grace=grace,
        poll=poll,
        sleep=sleep,
        now=now,
    )

    preflight = preflight_port(port, runner=runner, proc=proc, target_cid=target_cid)
    if not preflight.clear:
        return LaunchResult(False, preflight.message(), preflight=preflight)

    working_dir = cwd or workspace
    log_path = log or str(Path(workspace) / STATE_DIR / LOG_FILE)
    spawn = spawner if spawner is not None else spawn_detached
    pid = spawn(list(command), cwd=working_dir, log=log_path)

    snapshot = read_process(pid, proc=proc)
    state = RunState(
        pid=pid,
        port=port,
        command=tuple(command),
        cwd=working_dir,
        log=log_path,
        started_at=time.time(),
        start_ticks=snapshot.start_ticks if snapshot is not None else None,
    )
    # Recorded before it is verified: a child that never binds still has to be
    # killable by `podbench stop` rather than left for pkill to find.
    save_state(state, workspace)

    deadline = now() + timeout
    last = "the process did not bind before the timeout"
    while True:
        snapshot = read_process(pid, proc=proc)
        if snapshot is None or not snapshot.alive:
            return LaunchResult(
                False,
                f"pid {pid} exited before it served port {port}. {_log_tail(log_path)}",
                state,
                preflight,
            )
        check = verify_listener(
            pid, port, runner=runner, proc=proc, target_cid=target_cid
        )
        if check.ok:
            note = (
                f" (stopped previous: {stopped.detail})"
                if stopped.pid is not None
                else ""
            )
            return LaunchResult(True, check.detail + note, state, preflight)
        last = check.detail
        if check.entry is not None and pid not in check.entry.pids:
            # Somebody else answered on our port: stop rather than poll on,
            # because SO_REUSEPORT would otherwise serve half the traffic from
            # the old code and nothing would say so.
            return LaunchResult(False, last, state, preflight)
        if now() >= deadline:
            return LaunchResult(
                False,
                f"{last} after {timeout:g}s. {_log_tail(log_path)}",
                state,
                preflight,
            )
        sleep(poll)


def _log_tail(path: str, lines: int = 15) -> str:
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return f"No output was captured in {path}."
    tail = text.splitlines()[-lines:]
    return (
        f"Last output from {path}:\n" + "\n".join(tail) if tail else f"{path} is empty."
    )


# -- bootstrap -------------------------------------------------------------


@dataclass(frozen=True)
class BootstrapPlan:
    """What ``dev-bootstrap`` should put in the workspace."""

    repo: str
    checkout: str
    ref: str | None = None
    sync: bool = True
    editable: bool = True
    python: str | None = None

    @property
    def venv(self) -> str:
        """Where ``uv sync`` will put the environment."""
        return str(Path(self.checkout) / ".venv")


def bootstrap_commands(
    plan: BootstrapPlan, *, checkout_exists: bool
) -> list[list[str]]:
    """The exact commands ``dev-bootstrap`` runs. Nothing is installed by apt.

    The image bakes ``git``, ``curl``, ``uv``, ``iproute2``, ``procps`` and a
    pre-seeded CPython for one measured reason: ``apt-get`` was 10.6 s of a 19 s
    loop (55 %) in S4. Anything added here at runtime is paid for on every cold
    pod, so this list only ever moves *the user's* code.

    ``uv sync --frozen`` rather than ``uv sync``: the lockfile belongs to the
    application, and a dev pod that silently re-resolves it is no longer running
    what production runs. ``--directory`` keeps every command independent of the
    caller's cwd.
    """
    commands: list[list[str]] = []
    if checkout_exists:
        commands.append(["git", "-C", plan.checkout, "fetch", "--prune", "--tags"])
        if plan.ref is not None:
            commands.append(
                ["git", "-C", plan.checkout, "checkout", "--force", plan.ref]
            )
        else:
            commands.append(["git", "-C", plan.checkout, "pull", "--ff-only"])
    else:
        commands.append(["git", "clone", plan.repo, plan.checkout])
        if plan.ref is not None:
            commands.append(
                ["git", "-C", plan.checkout, "checkout", "--force", plan.ref]
            )
    if plan.python is not None:
        commands.append(["uv", "python", "install", plan.python])
    if plan.sync:
        sync = ["uv", "--directory", plan.checkout, "sync", "--frozen"]
        if plan.python is not None:
            sync += ["--python", plan.python]
        commands.append(sync)
    if plan.editable:
        commands.append(
            ["uv", "--directory", plan.checkout, "pip", "install", "-e", "."]
        )
    return commands


def bootstrap(
    plan: BootstrapPlan,
    *,
    runner: Runner | None = None,
    exists: Callable[[str], bool] | None = None,
    echo: Callable[[str], None] | None = None,
) -> int:
    """Clone, sync and editable-install into the workspace. Idempotent."""
    validate_workspace_layout(checkout=plan.checkout, venv=plan.venv)
    run = runner if runner is not None else run_subprocess
    present = exists if exists is not None else _is_checkout
    say = echo if echo is not None else _say
    for argv in bootstrap_commands(plan, checkout_exists=present(plan.checkout)):
        say("+ " + " ".join(argv))
        result = run(argv)
        if result.returncode != 0:
            raise DevError(
                f"{' '.join(argv)} exited {result.returncode}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
    return 0


def _is_checkout(path: str) -> bool:
    return (Path(path) / ".git").exists()


# -- laptop side -----------------------------------------------------------


@dataclass(frozen=True)
class Cutover:
    """A Service whose selector podbench borrowed, and what it was before."""

    service: str
    selector: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True)
class DevPod:
    """A created dev pod, described well enough to print and to tear down."""

    ref: PodRef
    origin: str
    target_container: str
    sidecar: str
    target_port: int
    take_traffic: bool
    cutover: Cutover | None = None
    seat_identity: tuple[int, int] | None = None
    """The uid/gid the sidecar was authored at because the origin declares
    :data:`podbench.model.SEAT_IDENTITY_VOLUME`, or ``None`` for the root
    sidecar. It changes what the user is logged in as and whether the seat has
    ``SYS_PTRACE``, so it is reported rather than left to be discovered."""


def is_dev_pod(pod_json: Mapping[str, Any]) -> bool:
    """Whether podbench authored this pod as a dev pod.

    The one fact that decides both directions of Iterate mode: a dev pod is the
    only thing ``--delete`` may tear down, and the only thing ``dev`` must not
    be asked to clone. Read from the label rather than from the name, because
    ``--name`` means the name proves nothing either way.

    >>> is_dev_pod({"metadata": {"labels": {spec.DEVPOD_LABEL: "true"}}})
    True
    >>> is_dev_pod({"metadata": {"name": "demo-podbench"}})
    False
    """
    return (
        as_dict(as_dict(pod_json.get("metadata")).get("labels")).get(spec.DEVPOD_LABEL)
        == "true"
    )


def dev_pod_name(origin: str, *, suffix: str = DEV_POD_SUFFIX) -> str:
    """The dev pod's name, derived from its origin and idempotent.

    Idempotent so that ``dev --delete`` accepts either the origin's name or the
    dev pod's own without the user having to remember which they typed.

    >>> dev_pod_name("demo")
    'demo-podbench'
    >>> dev_pod_name("demo-podbench")
    'demo-podbench'
    """
    if origin.endswith(suffix):
        return origin
    return origin[: MAX_POD_NAME - len(suffix)].rstrip("-") + suffix


def sole_container(pod_json: Mapping[str, Any]) -> str:
    """The name of the pod's only container, or an error listing the choices."""
    names = [
        str(entry.get("name"))
        for entry in _containers(pod_json)
        if entry.get("name") is not None
    ]
    if len(names) == 1:
        return names[0]
    raise DevError(
        f"pod has {len(names)} containers ({', '.join(names) or 'none'}); "
        "name the one to take over with --container"
    )


def default_target_port(pod_json: Mapping[str, Any], container: str) -> int | None:
    """The container's first declared ``containerPort``, if it declares one.

    Used only as a default: the readiness probe that makes Service membership
    track the inner loop (report §3.6) needs a port, and guessing wrong is worse
    than asking.
    """
    for entry in _containers(pod_json):
        if entry.get("name") != container:
            continue
        ports = entry.get("ports")
        if not isinstance(ports, list):
            return None
        for port in cast(list[Any], ports):
            if isinstance(port, dict):
                value = cast(dict[str, Any], port).get("containerPort")
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
    return None


def cutover_annotations(service: str, selector: Mapping[str, str]) -> dict[str, str]:
    """Annotations recording a Service's selector before podbench replaced it.

    Exact restoration is the whole point: the replacement is a JSON ``replace``
    of the entire selector, so a partial or merged restore leaves the Service
    selecting the dev pod as well as — or instead of — the original.
    """
    return {
        CUTOVER_SERVICE_ANNOTATION: service,
        CUTOVER_SELECTOR_ANNOTATION: json.dumps(dict(selector), sort_keys=True),
    }


def recorded_cutover(pod_json: Mapping[str, Any]) -> Cutover | None:
    """Read back what :func:`cutover_annotations` wrote, or ``None``."""
    annotations = as_dict(as_dict(pod_json.get("metadata")).get("annotations"))
    service = annotations.get(CUTOVER_SERVICE_ANNOTATION)
    if not isinstance(service, str) or not service:
        return None
    raw = annotations.get(CUTOVER_SELECTOR_ANNOTATION)
    selector: dict[str, str] = {}
    if isinstance(raw, str):
        try:
            parsed: object = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            selector = {
                str(key): str(value)
                for key, value in cast(dict[str, Any], parsed).items()
            }
    return Cutover(service=service, selector=selector)


_WORKLOAD_HOPS = 3
"""How far up the ownership chain to look for a GitOps mark.

Pod → ReplicaSet → Deployment is two, and the third is slack for an operator
that inserts a kind of its own. Bounded rather than unbounded because a
malformed ownerReferences cycle would otherwise hang the launcher.
"""


def gitops_manager(
    kube: Kubectl, pod_json: Mapping[str, Any]
) -> tuple[str, str] | None:
    """The workload behind this pod and the GitOps mark on it, or ``None``.

    The walk is the whole point. Argo CD stamps what it applied *from git* —
    the Deployment — and the pods a controller makes carry none of it: measured
    on a live install, 0 of 82 pods carried the tracking label while their
    owning workloads did. Inspecting the pod alone finds nothing, and would let
    Iterate mode proceed on precisely the workloads it must not.

    Errors are swallowed deliberately. A user who cannot ``get deployments`` is
    not thereby entitled to a dev pod that a controller will fight, but neither
    should a missing RBAC verb be reported as "this is GitOps-managed" — so an
    unreadable owner reads as unmarked, and the failure stays the one the user
    already has.
    """
    obj: Mapping[str, Any] = pod_json
    for _ in range(_WORKLOAD_HOPS):
        owner = _controller_ref(obj)
        if owner is None:
            return None
        kind, name = owner
        try:
            result = kube.run("get", kind, name, "-o", "json")
        except KubectlError:
            return None
        try:
            parsed: object = json.loads(result.stdout)
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        obj = cast(dict[str, Any], parsed)
        mark = spec.gitops_owner(obj)
        if mark is not None:
            return f"{kind}/{name}", mark
    return None


def _controller_ref(obj: Mapping[str, Any]) -> tuple[str, str] | None:
    """The ``kind, name`` of the controller that owns an object."""
    metadata = as_dict(obj.get("metadata"))
    for entry in metadata.get("ownerReferences") or ():
        owner = as_dict(entry)
        if owner.get("controller") is not True:
            continue
        kind = owner.get("kind")
        name = owner.get("name")
        if isinstance(kind, str) and isinstance(name, str):
            return kind.lower(), name
    return None


def refuse_if_gitops_managed(kube: Kubectl, pod_json: Mapping[str, Any]) -> None:
    """Refuse Iterate mode on a workload a GitOps controller reconciles.

    Absolute, with no override, because there is no version of this that works
    and every mitigation makes it worse. The clone itself is usually safe — the
    tracking mark is on the workload, not the pod template — but ``--cutover``
    patches a Service selector, and a Service *is* a tracked object straight out
    of git, so self-heal reverts it within seconds and traffic returns to the
    origin with nothing logged anywhere. Annotating the dev pod
    ``IgnoreExtraneous`` was tried and rejected (#38): it protects the pod by
    hiding it from the operator's source of truth, which turns a loud failure
    into a silent one and does nothing about the Service.
    """
    managed = gitops_manager(kube, pod_json)
    if managed is None:
        return
    workload, mark = managed
    raise DevError(
        f"{workload} is managed by a GitOps controller ({mark}), and Iterate "
        "mode does not work against one. A Service cutover is drift on a "
        "git-managed object, so self-heal reverts it within seconds and the "
        "traffic you think you have goes back to the original pod without an "
        "error anywhere; the dev pod can also be pruned as an extraneous "
        "resource, depending on how tracking is configured.\n\n"
        "Use `podbench attach` to debug the workload in place — it adds an "
        "ephemeral container, which no GitOps controller reconciles — or "
        "`podbench hotfix` when the change has to survive a restart. If this "
        "workload really is not reconciled, remove the mark from it rather "
        "than working around this."
    )


def create_dev_pod(
    kube: Kubectl,
    origin: str,
    *,
    name: str | None = None,
    container: str | None = None,
    image: str = DEFAULT_IMAGE,
    port: int | None = None,
    public_key: str | None = None,
    take_traffic: bool = False,
    cutover_service: str | None = None,
    timeout: float = 120.0,
    dry_run: bool = False,
) -> tuple[DevPod, dict[str, Any]]:
    """Author the clone, create it, and wait for it to be *Running*.

    Not ``--for=condition=Ready``: the sidecar carries a ``tcpSocket``
    readiness probe on the target port precisely so that Service membership
    follows the inner loop, and nothing is listening until the first
    ``podbench run``. Waiting for Ready here would hang until the user did
    something they have not been told how to do yet.

    ``public_key`` is authorised inside the sidecar by the agent at start-up,
    from the environment variable authored here. It has to go in at *authoring*
    time and there is no second chance: an ordinary container's ``env`` is
    immutable once the pod exists, so a dev pod created without a key is a dev
    pod that has to be deleted and made again to get one. That is why ``dev``
    reads the key before it creates anything, exactly as ``attach`` does.
    """
    origin_json = kube.get_pod(origin)
    if is_dev_pod(origin_json):
        # Reachable since `dev` took substring resolution: with the origin gone
        # and its clone still there, `dev demo` matches `demo-podbench`. Cloning
        # that copies the sidecar in as an ordinary container, so the result is
        # a pod with two podbench containers and a target that is already idle —
        # and the failures it produces name neither.
        raise DevError(
            f"{origin} is itself a podbench dev pod; clone the workload it was "
            "made from instead"
        )
    # Before anything is authored, and before the Service is touched: the point
    # of refusing is that the user finds out now rather than from traffic that
    # quietly stopped arriving.
    refuse_if_gitops_managed(kube, origin_json)
    target = container or sole_container(origin_json)
    target_port = port or default_target_port(origin_json, target)
    if target_port is None:
        raise DevError(
            f"container {target!r} declares no containerPort; pass --port so the "
            "readiness probe can follow the relaunched process"
        )
    pod_name = name or dev_pod_name(origin)

    identity = spec.dev_seat_identity(origin_json, target)
    if identity is None and SEAT_IDENTITY_VOLUME in declared_volumes(origin_json):
        # Declared and unusable is worth a line: somebody prepared this pod for
        # podbench and the sidecar is about to ignore what they prepared.
        _say(
            f"note: {origin} declares the {SEAT_IDENTITY_VOLUME!r} volume, and "
            f"the sidecar cannot use it: container {target!r} pins no non-root "
            "uid *and* gid, so the uid the identity was written for is not "
            "knowable from the manifest. The sidecar runs as root instead."
        )

    manifest = spec.dev_pod_spec(
        origin_json,
        name=pod_name,
        target_container=target,
        image=image,
        target_port=target_port,
        take_traffic=take_traffic,
        env={PUBKEY_ENV: public_key} if public_key else None,
    )

    cutover: Cutover | None = None
    if cutover_service is not None:
        cutover = Cutover(cutover_service, _service_selector(kube, cutover_service))
        # Recorded on the manifest *before* the Service is touched, so a failure
        # between the two leaves a dev pod that still knows how to undo itself.
        as_dict(manifest["metadata"]).setdefault("annotations", {}).update(
            cutover_annotations(cutover.service, cutover.selector)
        )

    result = DevPod(
        ref=PodRef(kube.namespace, pod_name),
        origin=origin,
        target_container=target,
        sidecar="podbench",
        target_port=target_port,
        take_traffic=take_traffic,
        cutover=cutover,
        seat_identity=identity,
    )
    if dry_run:
        return result, manifest

    kube.create_from_spec(manifest)
    kube.wait_for(
        f"pod/{pod_name}", "jsonpath={.status.phase}=Running", timeout=timeout
    )
    if cutover is not None:
        kube.patch("service", cutover.service, spec.cutover_selector_patch())
    return result, manifest


def sidecar_session(pod: DevPod, manifest: Mapping[str, Any]) -> Session:
    """The dev pod's sidecar, described the way the launcher describes a seat.

    A dev pod's sidecar *is* a seat — it runs the same agent, writes the same
    sshd config and answers the same ``--print-login-user`` — so the client
    wiring is the launcher's :func:`podbench.launcher.emit_ssh_config` and not a
    second implementation of it. All that is needed here is the translation, and
    it is read back out of the authored manifest rather than remembered, so the
    stanza describes the container that was actually submitted.

    ``$HOME`` is the one field with a real decision in it. Two homes are in play
    and they are not the same path:

    * the sidecar's ``HOME`` env is the workspace volume (``/workspace``), where
      uv's caches, toolchains and venvs belong;
    * a projected ``podbench-identity`` passwd record names ``/home/podbench``,
      and an *ssh session* lands there, because sshd puts the session in the home
      NSS gives it.

    The agent settles it inside the container with
    ``SshdLayout.for_uid(os.geteuid())``, which consults ``$HOME`` for a non-root
    seat and ignores it entirely for a root one — root's files live in ``/root``
    and ``/etc/podbench`` whatever ``HOME`` says. **So the env wins for a
    non-root sidecar and nothing wins for a root one**, and that is what is
    reproduced here: the ProxyCommand names sshd's config file by absolute path,
    and ``authorized_keys`` has to be looked for where the agent actually wrote
    it. Disagreeing costs the whole transport at the first connection, with
    ``sshd: no such file`` or ``Permission denied (publickey)`` to explain it.

    The passwd home is left to be what it is for — the session's landing
    directory, which is why :func:`podbench.spec.dev_pod_spec` mounts the
    ``podbench-home`` volume there. ``AuthorizedKeysFile`` in the generated sshd
    config is absolute, so the two homes differing is not a problem for key auth;
    it would only become one if this function guessed at the wrong one.
    """
    sidecar = _sidecar_container(manifest, pod.sidecar)
    rung = rung_of_spec(sidecar)
    return Session(
        seat=ContainerRef(pod.ref, pod.sidecar),
        workload=pod.target_container,
        rung=rung,
        reused=False,
        uid=_as_int(as_dict(sidecar.get("securityContext")).get("runAsUser")),
        home=None if rung is Rung.FULL else spec_env(sidecar).get("HOME"),
        # Mounted and declared are different questions, and for a dev pod they
        # can differ: the clone carries the origin's volumes wholesale, but the
        # sidecar only mounts the identity when the manifest said which uid it
        # was written for.
        identity_mounted=pod.seat_identity is not None,
        identity_declared=SEAT_IDENTITY_VOLUME in declared_volumes(manifest),
    )


def seat_ssh_config(
    kube: Kubectl,
    pod: DevPod,
    manifest: Mapping[str, Any],
    *,
    identity: str,
    config_dir: str | None = None,
    host_alias: str | None = None,
) -> SshSeat:
    """Give the dev pod's sidecar the same seat ``attach`` gives an ephemeral one.

    Iterate mode's whole premise is that the editor is inside the cluster, so a
    dev pod reachable only by ``kubectl exec`` is the mode with its point
    removed. The sidecar already runs the agent; this is the other half — the
    client stanza, the ``known_hosts`` entry and the alias to type.

    The login name is *measured* rather than derived from the rung, because on a
    dev pod it is neither of the launcher's two answers: it comes from the passwd
    record the ``podbench-identity`` ConfigMap carries, and that names whatever
    the chart called the application's uid. Carried on the session rather than
    passed as ``user``: :func:`podbench.launcher.emit_ssh_config` prefers what
    the seat answered for every seat now, so naming it again here would be a
    second spelling of the same decision.
    """
    session = sidecar_session(pod, manifest)
    identified = probe_ssh_identity(kube, session.seat)
    return emit_ssh_config(
        kube,
        replace(session, ssh=identified),
        identity=identity,
        config_dir=config_dir,
        host_alias=host_alias,
    )


def _sidecar_container(manifest: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    for container in _containers(manifest):
        if container.get("name") == name:
            return container
    raise DevError(f"the authored pod has no container named {name!r}")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def delete_dev_pod(
    kube: Kubectl,
    name: str,
    *,
    timeout: float = 120.0,
    config_dir: str | None = None,
) -> list[str]:
    """Tear the dev pod down, leaving the cluster as it was found.

    Order matters: the Service selector goes back *before* the pod is deleted,
    so there is no window in which the Service selects nothing and every request
    fails. The origin pod is never touched — and this refuses to run against
    anything that is not a podbench dev pod, because ``dev --delete`` typed with
    the origin's name would otherwise delete production.

    "Leaving nothing behind" includes the laptop: the ssh stanza and the
    ``known_hosts`` entry ``dev`` wrote go too. Unlike an ``attach`` seat, which
    is reconnectable for its pod's lifetime and whose stanza is regenerated, this
    pod is gone — and its ``HostKeyAlias`` is keyed on a pod UID no pod will ever
    have again, so a stanza left behind can only ever fail.
    """
    actions: list[str] = []
    try:
        pod_json = kube.get_pod(name)
    except KubectlError:
        return [f"no pod {name} in {kube.namespace}; nothing to delete"]

    if not is_dev_pod(pod_json):
        raise DevError(
            f"{name} is not a podbench dev pod (no {spec.DEVPOD_LABEL} label); "
            "refusing to delete it"
        )

    cutover = recorded_cutover(pod_json)
    if cutover is not None:
        kube.patch(
            "service", cutover.service, spec.service_selector_patch(cutover.selector)
        )
        actions.append(
            f"restored selector {json.dumps(cutover.selector, sort_keys=True)} "
            f"on service/{cutover.service}"
        )

    kube.delete_pod(name, wait=True)
    actions.append(f"deleted pod/{name}")

    # After the delete, not before: a teardown that failed half way through
    # should leave the user with a working stanza for the pod that is still
    # there.
    pod_uid = as_dict(pod_json.get("metadata")).get("uid")
    actions.extend(
        forget_ssh_config(
            PodRef(kube.namespace, name),
            directory=client_dir(config_dir),
            alias=host_key_alias(pod_uid) if isinstance(pod_uid, str) else None,
        )
    )
    return actions


def connection_summary(pod: DevPod, seat: SshSeat | None = None) -> str:
    """What to type next. Printed once, after the dev pod comes up.

    ``seat`` is the ssh wiring :func:`seat_ssh_config` produced. Both routes in
    are listed when there is one, and in this order: ssh is what Iterate mode is
    *for* — the editor lives in the cluster — and ``kubectl exec`` is what still
    works when ssh does not, which is the reason it is not dropped.
    """
    lines = [
        f"dev pod {pod.ref} is running (clone of {pod.origin}; "
        f"{pod.origin} itself is untouched)",
        f"  target container : {pod.target_container} (idled with sleep infinity)",
        f"  debug container  : {pod.sidecar}",
        f"  workspace        : {DEFAULT_WORKSPACE} (emptyDir, also $HOME)",
        f"  app port         : {pod.target_port} (readiness follows your process)",
    ]
    if pod.seat_identity is not None:
        uid, gid = pod.seat_identity
        lines.append(
            f"  seat identity    : {SEAT_IDENTITY_VOLUME} projected over "
            f"/etc/passwd and /etc/group, so the sidecar runs as {uid}:{gid} "
            "(the app's own) with no SYS_PTRACE"
        )
    if pod.cutover is not None:
        lines.append(
            f"  service          : {pod.cutover.service} cut over to this pod; "
            "its selector is recorded for an exact restore"
        )
    elif pod.take_traffic:
        lines.append(
            "  service          : carrying the origin's labels, so traffic is "
            "shared with the original pod"
        )
    else:
        lines.append(
            "  service          : none — this pod receives no traffic until you "
            "pass --take-traffic or --cutover"
        )
    if seat is not None:
        lines += ["", *seat.note.splitlines()]
    lines += ["", "next:"]
    if seat is not None and seat.alias is not None:
        lines.append(
            f"  ssh {seat.alias}   (or Remote-SSH: Connect to Host -> {seat.alias})"
        )
    lines += [
        f"  kubectl -n {pod.ref.namespace} exec -it {pod.ref.name} "
        f"-c {pod.sidecar} -- bash"
        + ("   # works when ssh does not" if seat is not None else ""),
        "  podbench dev-bootstrap --repo <url> [--ref <ref>]",
        f"  podbench run --port {pod.target_port} -- <your command>",
        "",
        "teardown (restores any borrowed Service selector, removes the pod, and "
        "takes the ssh config with it):",
        f"  podbench dev --delete {pod.ref.name} -n {pod.ref.namespace}",
    ]
    return "\n".join(lines)


def _service_selector(kube: Kubectl, service: str) -> dict[str, str]:
    result = kube.run("get", "service", service, "-o", "json")
    parsed: object = json.loads(result.stdout)
    if not isinstance(parsed, dict):
        raise DevError(f"unexpected JSON for service/{service}")
    selector = as_dict(
        as_dict(cast(dict[str, Any], parsed).get("spec")).get("selector")
    )
    if not selector:
        raise DevError(
            f"service/{service} has no selector, so there is nothing to cut over "
            "(a headless or externalName Service cannot be borrowed)"
        )
    return {str(key): str(value) for key, value in selector.items()}


def _containers(pod_json: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = as_dict(pod_json.get("spec")).get("containers")
    if not isinstance(entries, list):
        return []
    return [
        cast(dict[str, Any], entry)
        for entry in cast(list[Any], entries)
        if isinstance(entry, dict)
    ]


def _say(message: str) -> None:
    """Progress goes to stderr so stdout stays parseable."""
    emit(message, stderr=True)


# -- CLI -------------------------------------------------------------------


def _delete_target(kube: Kubectl, reference: str | None, *, prompt: bool) -> str | None:
    """Which pod ``dev --delete`` is about to tear down, or ``None`` for none.

    :func:`dev_pod_name` is idempotent, so the dev pod's own name and the
    origin's both derive to it, and confirming it is one ``get pod``: the
    scripted teardown path neither lists the namespace nor is asked a question.
    It is derived through :func:`resolve_pod_name` first because ``pod/api``
    used to fail twice over here — kubectl refused the argument, and the derived
    name carried a slash, which is not an RFC 1123 label.

    Everything else is resolved against *dev pods only*. That is the whole
    difference from :func:`podbench.launcher.resolve_pod`, and it is what keeps
    teardown idempotent: `api` still matching two ordinary workload pods after
    `api-podbench` is gone must stay "nothing to delete", not the ambiguity
    refusal the create path owes the same reference. Listing only what
    ``--delete`` can act on is also the only way the prompt is worth asking —
    every row in it is deletable, and a pod created with ``--name`` is in it,
    which is the one target no derived name can reach.
    """
    derived: str | None = None
    named: str | None = None
    if reference is not None:
        named = resolve_pod_name(reference)
        derived = dev_pod_name(named)
        if kube.pod_exists(derived):
            return derived
    choices = pod_choices(kube, where=is_dev_pod)
    matches = choices if named is None else match_pod_choices(choices, named)
    if not matches:
        # `derived` when there was a reference, so the miss keeps saying which
        # pod it went looking for; `None` when there was not, and the caller
        # has the namespace to name instead.
        return derived
    return resolve_among(kube.namespace, matches, named, noun="dev pod", prompt=prompt)


def _identity_argument(identity: str) -> tuple[str, str]:
    """The launcher's own key discovery, refusing in this CLI's voice.

    One implementation, so ``dev`` and ``attach`` authorise the same key from
    the same flag and refuse a missing one with the same sentence — the message
    names ``ssh-keygen`` and ``--identity``, and it would be worth nothing if
    each verb had its own version of it.
    """
    try:
        return read_public_key(identity)
    except LauncherError as error:
        raise DevError(str(error)) from error


_Namespace = Annotated[
    str | None,
    typer.Option(
        "-n",
        "--namespace",
        metavar="NAMESPACE",
        help="namespace (default: the kubeconfig context's own)",
    ),
]
_NoPrompt = Annotated[
    bool,
    typer.Option(
        "--no-prompt",
        help="never ask which pod: an ambiguous or missing POD is refused with "
        "the candidates instead. Already implied when stdin is not a tty",
    ),
]
_Context = Annotated[
    str | None, typer.Option("--context", metavar="NAME", help="kubeconfig context")
]
_Workspace = Annotated[
    str, typer.Option("--workspace", metavar="DIR", help="workspace root")
]


def _build_app() -> typer.Typer:
    app = new_app()

    @app.callback(invoke_without_command=True)
    def root(ctx: typer.Context) -> None:
        """Iterate mode: a dev pod and the relaunch loop inside it."""
        require_subcommand(ctx)

    @app.command(help="create or delete the dev pod (runs on the laptop)")
    def dev(
        pod: Annotated[
            str | None,
            typer.Argument(
                metavar="POD",
                help="the pod to clone, or the dev pod to delete: pod/NAME, a "
                "bare NAME, or any substring of one. Anything that does not "
                "settle on a single pod lists the candidates and asks — every "
                "pod in the namespace, or with --delete only the dev pods",
            ),
        ] = None,
        namespace: _Namespace = None,
        context: _Context = None,
        container: Annotated[
            str | None,
            typer.Option("--container", metavar="NAME", help="container to take over"),
        ] = None,
        name: Annotated[
            str | None,
            typer.Option(
                "--name", metavar="NAME", help="dev pod name (default: POD-podbench)"
            ),
        ] = None,
        image: Annotated[
            str,
            typer.Option(
                "--image",
                metavar="REF",
                # The resolved value is derived from this launcher's version,
                # so printing it would advertise a checkout's `:main` as the
                # release tag.
                show_default=False,
                help="podbench image (default: the image built from this "
                "launcher's version)",
            ),
        ] = DEFAULT_IMAGE,
        port: Annotated[
            int | None,
            typer.Option("--port", metavar="PORT", help="the port your app serves"),
        ] = None,
        take_traffic: Annotated[
            bool,
            typer.Option(
                "--take-traffic",
                help="copy the origin's labels so the dev pod shares Service "
                "traffic with it. Off by default: joining a production Service "
                "silently is a foot-cannon",
            ),
        ] = False,
        cutover: Annotated[
            str | None,
            typer.Option(
                "--cutover",
                metavar="SERVICE",
                help="point SERVICE exclusively at the dev pod, recording its "
                "selector for an exact restore at teardown",
            ),
        ] = None,
        identity: Annotated[
            str,
            typer.Option(
                "--identity",
                metavar="KEY",
                help="ssh key to authorise in the sidecar and name in the "
                "generated stanza",
            ),
        ] = DEFAULT_IDENTITY,
        config_dir: Annotated[
            str | None,
            typer.Option(
                "--config-dir",
                metavar="DIR",
                help="where the generated ssh config and known_hosts live "
                f"(default {DEFAULT_CLIENT_DIR})",
            ),
        ] = None,
        host_alias: Annotated[
            str | None,
            typer.Option(
                "--host-alias", metavar="NAME", help="ssh Host name for the sidecar"
            ),
        ] = None,
        delete: Annotated[
            bool, typer.Option("--delete", help="tear the dev pod down")
        ] = False,
        timeout: Annotated[
            float, typer.Option("--timeout", metavar="SECONDS", help="seconds to wait")
        ] = 120.0,
        dry_run: Annotated[
            bool,
            typer.Option(
                "--dry-run", help="print the authored pod instead of creating it"
            ),
        ] = False,
        no_prompt: _NoPrompt = False,
    ) -> None:
        kube = kubectl_for(namespace, context=context)
        if delete:
            target = _delete_target(kube, pod, prompt=not no_prompt)
            if target is None:
                print(f"no podbench dev pod in {kube.namespace}; nothing to delete")
                raise typer.Exit(0)
            for action in delete_dev_pod(
                kube, target, timeout=timeout, config_dir=config_dir
            ):
                print(action)
            raise typer.Exit(0)
        # The key before the pod, exactly as `attach` orders it: a missing key
        # refuses this run whichever pod is chosen, and asking someone which pod
        # they meant first would spend their answer on it. It is also the last
        # moment it can be refused at all — the key is authored into the
        # sidecar's env, and an ordinary container's env is immutable once the
        # pod exists, so a dev pod created without one can only be deleted and
        # made again.
        key_path, public_key = _identity_argument(identity)
        origin = resolve_pod(kube, pod, prompt=not no_prompt)
        created, manifest = create_dev_pod(
            kube,
            origin,
            name=name,
            container=container,
            image=image,
            port=port,
            public_key=public_key,
            take_traffic=take_traffic,
            cutover_service=cutover,
            timeout=timeout,
            dry_run=dry_run,
        )
        if dry_run:
            print(json.dumps(manifest, indent=2, sort_keys=True))
            raise typer.Exit(0)
        seat = seat_ssh_config(
            kube,
            created,
            manifest,
            identity=key_path,
            config_dir=config_dir,
            host_alias=host_alias,
        )
        emit(connection_summary(created, seat))
        raise typer.Exit(0)

    @app.command(
        name="dev-bootstrap",
        help="clone, sync and editable-install (runs in the pod)",
    )
    def dev_bootstrap(
        repo: Annotated[
            str, typer.Option("--repo", metavar="URL", help="git URL to clone")
        ],
        ref: Annotated[
            str | None,
            typer.Option(
                "--ref", metavar="REF", help="branch, tag or commit to check out"
            ),
        ] = None,
        directory: Annotated[
            str,
            typer.Option(
                "--dir",
                metavar="DIR",
                help="checkout directory (must be in this container)",
            ),
        ] = str(Path(DEFAULT_WORKSPACE) / "src"),
        python: Annotated[
            str | None,
            typer.Option(
                "--python", metavar="VERSION", help="CPython version for uv to use"
            ),
        ] = None,
        no_sync: Annotated[
            bool, typer.Option("--no-sync", help="skip uv sync --frozen")
        ] = False,
        no_editable: Annotated[
            bool, typer.Option("--no-editable", help="skip uv pip install -e .")
        ] = False,
    ) -> None:
        raise typer.Exit(
            bootstrap(
                BootstrapPlan(
                    repo=repo,
                    checkout=directory,
                    ref=ref,
                    sync=not no_sync,
                    editable=not no_editable,
                    python=python,
                )
            )
        )

    # Named explicitly: the function cannot be called `run`, which this
    # module already imports from .cli, nor `stop`, which is the relaunch
    # loop's own.
    # `ignore_unknown_options` is what lets the app's own flags through, and it
    # is needed because by the time argv reaches here the `--` is gone: the
    # top-level dispatcher forwards a verb's arguments verbatim by declaring it
    # with that same setting, and click consumes the separator doing so. Without
    # it the relaunch loop's documented form — `podbench run --port 8080 --
    # python -m api` — exits 2 on `No such option: -m`, having started nothing.
    # The cost is that a mistyped flag of podbench's own joins the command
    # rather than being refused; that is the right way round here, because
    # everything after `--` belongs to the app and `start()` verifies what it
    # actually launched.
    @app.command(
        name="run",
        help="relaunch the app and verify it (runs in the pod)",
        context_settings={"ignore_unknown_options": True},
    )
    def run_app(
        port: Annotated[
            int, typer.Option("--port", metavar="PORT", help="the port it must serve")
        ],
        workspace: _Workspace = DEFAULT_WORKSPACE,
        directory: Annotated[
            str | None,
            typer.Option(
                "--dir", metavar="DIR", help="working directory (default: workspace)"
            ),
        ] = None,
        timeout: Annotated[
            float,
            typer.Option("--timeout", metavar="SECONDS", help="seconds to verify"),
        ] = 15.0,
        command: Annotated[
            list[str] | None,
            typer.Argument(metavar="[COMMAND]...", help="the command, after `--`"),
        ] = None,
    ) -> None:
        result = start(
            list(command or ()),
            port=port,
            workspace=workspace,
            cwd=directory,
            timeout=timeout,
            target_cid=os.environ.get(TARGET_CID_ENV) or None,
        )
        print(result.detail)
        raise typer.Exit(0 if result.ok else 1)

    @app.command(name="stop", help="stop the recorded child (runs in the pod)")
    def stop_app(
        workspace: _Workspace = DEFAULT_WORKSPACE,
        grace: Annotated[
            float,
            typer.Option("--grace", metavar="SECONDS", help="seconds before SIGKILL"),
        ] = 5.0,
    ) -> None:
        result = stop(workspace=workspace, grace=grace)
        print(result.detail)
        raise typer.Exit(0 if result.ok else 1)

    return app


def main(args: Sequence[str] | None = None) -> int:
    """Entry point for ``dev``, ``dev-bootstrap``, ``run`` and ``stop``.

    One app for all four because they are one workflow: a top-level CLI can
    forward its argv verbatim once it recognises the verb.
    """
    try:
        return run(_build_app(), args, prog="podbench")
    except (DevError, KubectlError, LauncherError, spec.InvalidSpecError) as exc:
        print(f"podbench: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
