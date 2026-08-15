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

import argparse
import enum
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from . import spec
from .kubectl import Kubectl, KubectlError, Runner, run_subprocess
from .model import DEFAULT_IMAGE, TARGET_CID_ENV, PodRef, as_dict
from .proc import DEFAULT_PROC, read_cgroup, read_cmdline, read_comm

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
    "identify_owner",
    "listeners_on",
    "load_state",
    "main",
    "parse_ss",
    "path_side",
    "preflight_port",
    "read_process",
    "recorded_cutover",
    "save_state",
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
        same_container=_same_root(pid, proc=proc),
        is_target=bool(target_cid and cgroup and target_cid in cgroup),
    )


def _same_root(pid: int, *, proc: Path) -> bool | None:
    try:
        mine = os.stat(proc / "self" / "root")
        theirs = os.stat(proc / str(pid) / "root")
    except OSError:
        return None
    return (mine.st_dev, mine.st_ino) == (theirs.st_dev, theirs.st_ino)


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


def create_dev_pod(
    kube: Kubectl,
    origin: str,
    *,
    name: str | None = None,
    container: str | None = None,
    image: str = DEFAULT_IMAGE,
    port: int | None = None,
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
    """
    origin_json = kube.get_pod(origin)
    target = container or sole_container(origin_json)
    target_port = port or default_target_port(origin_json, target)
    if target_port is None:
        raise DevError(
            f"container {target!r} declares no containerPort; pass --port so the "
            "readiness probe can follow the relaunched process"
        )
    pod_name = name or dev_pod_name(origin)

    manifest = spec.dev_pod_spec(
        origin_json,
        name=pod_name,
        target_container=target,
        image=image,
        target_port=target_port,
        take_traffic=take_traffic,
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


def delete_dev_pod(kube: Kubectl, name: str, *, timeout: float = 120.0) -> list[str]:
    """Tear the dev pod down, leaving the cluster as it was found.

    Order matters: the Service selector goes back *before* the pod is deleted,
    so there is no window in which the Service selects nothing and every request
    fails. The origin pod is never touched — and this refuses to run against
    anything that is not a podbench dev pod, because ``dev --delete`` typed with
    the origin's name would otherwise delete production.
    """
    actions: list[str] = []
    try:
        pod_json = kube.get_pod(name)
    except KubectlError:
        return [f"no pod {name} in {kube.namespace}; nothing to delete"]

    labels = as_dict(as_dict(pod_json.get("metadata")).get("labels"))
    if labels.get(spec.DEVPOD_LABEL) != "true":
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
    return actions


def connection_summary(pod: DevPod) -> str:
    """What to type next. Printed once, after the dev pod comes up."""
    lines = [
        f"dev pod {pod.ref} is running (clone of {pod.origin}; "
        f"{pod.origin} itself is untouched)",
        f"  target container : {pod.target_container} (idled with sleep infinity)",
        f"  debug container  : {pod.sidecar}",
        f"  workspace        : {DEFAULT_WORKSPACE} (emptyDir, also $HOME)",
        f"  app port         : {pod.target_port} (readiness follows your process)",
    ]
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
    lines += [
        "",
        "next:",
        f"  kubectl -n {pod.ref.namespace} exec -it {pod.ref.name} "
        f"-c {pod.sidecar} -- bash",
        "  podbench dev-bootstrap --repo <url> [--ref <ref>]",
        f"  podbench run --port {pod.target_port} -- <your command>",
        "",
        "teardown (restores any borrowed Service selector, then removes the pod):",
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
    print(message, file=sys.stderr)


# -- CLI -------------------------------------------------------------------

_Handler = Callable[[argparse.Namespace], int]


def _cmd_dev(opts: argparse.Namespace) -> int:
    kube = Kubectl(opts.namespace, context=opts.context)
    if opts.delete:
        for action in delete_dev_pod(
            kube, dev_pod_name(opts.pod), timeout=opts.timeout
        ):
            print(action)
        return 0
    pod, manifest = create_dev_pod(
        kube,
        opts.pod,
        name=opts.name,
        container=opts.container,
        image=opts.image,
        port=opts.port,
        take_traffic=opts.take_traffic,
        cutover_service=opts.cutover,
        timeout=opts.timeout,
        dry_run=opts.dry_run,
    )
    if opts.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    print(connection_summary(pod))
    return 0


def _cmd_bootstrap(opts: argparse.Namespace) -> int:
    plan = BootstrapPlan(
        repo=opts.repo,
        checkout=opts.dir,
        ref=opts.ref,
        sync=not opts.no_sync,
        editable=not opts.no_editable,
        python=opts.python,
    )
    return bootstrap(plan)


def _cmd_run(opts: argparse.Namespace) -> int:
    result = start(
        opts.command,
        port=opts.port,
        workspace=opts.workspace,
        cwd=opts.dir,
        timeout=opts.timeout,
        target_cid=os.environ.get(TARGET_CID_ENV) or None,
    )
    print(result.detail)
    return 0 if result.ok else 1


def _cmd_stop(opts: argparse.Namespace) -> int:
    result = stop(workspace=opts.workspace, grace=opts.grace)
    print(result.detail)
    return 0 if result.ok else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="podbench",
        description="Iterate mode: a dev pod and the relaunch loop inside it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    dev = sub.add_parser(
        "dev", help="create or delete the dev pod (runs on the laptop)"
    )
    dev.add_argument("pod", help="the pod to clone, or the dev pod to delete")
    dev.add_argument(
        "-n", "--namespace", default="default", help="namespace (default: default)"
    )
    dev.add_argument("--context", default=None, help="kubeconfig context")
    dev.add_argument("--container", default=None, help="container to take over")
    dev.add_argument(
        "--name", default=None, help="dev pod name (default: <pod>-podbench)"
    )
    dev.add_argument("--image", default=DEFAULT_IMAGE, help="podbench image")
    dev.add_argument("--port", type=int, default=None, help="the port your app serves")
    dev.add_argument(
        "--take-traffic",
        action="store_true",
        help="copy the origin's labels so the dev pod shares Service traffic "
        "with it. Off by default: joining a production Service silently is a "
        "foot-cannon",
    )
    dev.add_argument(
        "--cutover",
        metavar="SERVICE",
        default=None,
        help="point SERVICE exclusively at the dev pod, recording its selector "
        "for an exact restore at teardown",
    )
    dev.add_argument("--delete", action="store_true", help="tear the dev pod down")
    dev.add_argument("--timeout", type=float, default=120.0, help="seconds to wait")
    dev.add_argument(
        "--dry-run",
        action="store_true",
        help="print the authored pod instead of creating it",
    )
    dev.set_defaults(handler=_cmd_dev)

    boot = sub.add_parser(
        "dev-bootstrap", help="clone, sync and editable-install (runs in the pod)"
    )
    boot.add_argument("--repo", required=True, help="git URL to clone")
    boot.add_argument("--ref", default=None, help="branch, tag or commit to check out")
    boot.add_argument(
        "--dir",
        default=str(Path(DEFAULT_WORKSPACE) / "src"),
        help="checkout directory (must be in this container)",
    )
    boot.add_argument("--python", default=None, help="CPython version for uv to use")
    boot.add_argument("--no-sync", action="store_true", help="skip uv sync --frozen")
    boot.add_argument(
        "--no-editable", action="store_true", help="skip uv pip install -e ."
    )
    boot.set_defaults(handler=_cmd_bootstrap)

    run = sub.add_parser("run", help="relaunch the app and verify it (runs in the pod)")
    run.add_argument("--port", type=int, required=True, help="the port it must serve")
    run.add_argument("--workspace", default=DEFAULT_WORKSPACE, help="workspace root")
    run.add_argument(
        "--dir", default=None, help="working directory (default: workspace)"
    )
    run.add_argument("--timeout", type=float, default=15.0, help="seconds to verify")
    run.add_argument("command", nargs="*", help="the command, after `--`")
    run.set_defaults(handler=_cmd_run)

    halt = sub.add_parser("stop", help="stop the recorded child (runs in the pod)")
    halt.add_argument("--workspace", default=DEFAULT_WORKSPACE, help="workspace root")
    halt.add_argument("--grace", type=float, default=5.0, help="seconds before SIGKILL")
    halt.set_defaults(handler=_cmd_stop)
    return parser


def main(args: Sequence[str] | None = None) -> int:
    """Entry point for ``dev``, ``dev-bootstrap``, ``run`` and ``stop``.

    One parser for all four because they are one workflow: a top-level CLI can
    forward its argv verbatim once it recognises the verb.
    """
    opts = _build_parser().parse_args(args)
    handler = cast(_Handler, opts.handler)
    try:
        return handler(opts)
    except (DevError, KubectlError, spec.InvalidSpecError) as exc:
        print(f"podbench: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
