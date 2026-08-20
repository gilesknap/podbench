"""The four ``/proc`` trees the ranking was corrected against, as fixtures.

``tests/data/proc/*.txt`` are captures taken inside the seat on the p47 beamline
on 2026-08-19, one line per process, with a two-line ``#`` header naming the pod
and any redaction::

    pid|ppid|comm|state|uid|gid|threads|cmdline

Verbatim apart from blueapi's debugpy session tokens, which are 64 hex digits
and which nothing here reads.

They are data, not illustrations. Ranking on depth alone picked a zombie on
blueapi, a DNS helper on rabbitmq and an unattachable worker on opis, and each
of those is a row in one of these files rather than a shape someone invented to
justify a predicate.

Two facts the capture does not carry, and how they are supplied here:

* **which container owns a process.** The files cover the whole shared PID
  namespace, so the seat's own processes are in them. In the field
  :func:`podbench.proc.scan_processes` attributes by cgroup; here the seat's
  pids are named per pod in :data:`SEATS`.
* **ptrace-readability.** Reconstructed from the uid, which is the kernel's own
  rule for a seat holding no CAP_SYS_PTRACE: ``__ptrace_may_access()`` under
  ``PTRACE_MODE_READ`` demands the uids match, and nothing here has the
  capability that would excuse a mismatch. A dead process is never readable —
  ``/proc/<pid>/root`` answers ENOENT once the task has no ``fs_struct``, which
  is exactly why the ranking's liveness test comes first and does not lean on
  this one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from podbench.model import ProcInfo

DATA = Path(__file__).parent / "data" / "proc"

CID = "cafe1234cafe1234cafe1234cafe1234"
"""The container id the synthetic trees attribute the target's processes to."""


@dataclass(frozen=True)
class Sample:
    """One captured pod, and what the seat looking at it could do."""

    name: str
    seat_uid: int
    """The uid the podbench seat ran as, from the ``podbench agent`` row."""

    seat_pids: frozenset[int]
    """The capture's own processes — the seat's agent, its sshd, the shell that
    walked ``/proc``. The cgroup attribution excludes these in the field."""


SEATS = {
    # `525|0|podbench|S|1000|0|1|…` and the sshd it left behind.
    "blueapi": Sample("blueapi", seat_uid=1000, seat_pids=frozenset({525, 624})),
    # `125|0|podbench|S|0|0|1|…` — root, and capless, which is the whole of why
    # this pod's uid-101 workers cannot be ptraced from it.
    "opis": Sample("opis", seat_uid=0, seat_pids=frozenset({125})),
    # `3537551|0|podbench|S|999|999|1|…`, plus the `sh -c` that ran the capture.
    "rabbitmq": Sample(
        "rabbitmq", seat_uid=999, seat_pids=frozenset({3537551, 631538})
    ),
    # A single-process pod with no agent row in the capture; the seat mirrors
    # the target's uid, which is what makes the one process readable.
    "simdet": Sample("simdet", seat_uid=37887, seat_pids=frozenset()),
}


def sample(name: str) -> list[ProcInfo]:
    """Every process in the captured namespace, target attribution included.

    >>> [p.pid for p in sample("simdet")]
    [1]
    >>> sample("simdet")[0].comm, sample("simdet")[0].threads
    ('stdio-socket', 2)
    """
    seat = SEATS[name]
    processes: list[ProcInfo] = []
    for line in (DATA / f"{name}.txt").read_text().splitlines():
        fields = line.split("|")
        # The capture's own `sh -c` carries a multi-line script, so its
        # continuation lines are in the file too and are not records.
        if len(fields) < 8 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        pid, ppid, comm, state, uid, _gid, threads = fields[:7]
        cmdline = "|".join(fields[7:]).strip()
        alive = state not in ("Z", "X")
        processes.append(
            ProcInfo(
                pid=int(pid),
                uid=int(uid),
                comm=comm,
                cmdline=cmdline or f"[{comm}]",
                container_id=CID,
                is_target=int(pid) not in seat.seat_pids,
                ppid=int(ppid),
                state=state,
                threads=int(threads),
                ptrace_readable=alive and int(uid) == seat.seat_uid,
            )
        )
    return processes


def targets(name: str) -> list[ProcInfo]:
    """Just the captured pod's own processes, as the cgroup attribution sees it.

    >>> [p.pid for p in targets("rabbitmq")]
    [1, 16, 223, 224, 234, 447]
    """
    return [info for info in sample(name) if info.is_target]


def write_tree(root: Path, processes: list[ProcInfo]) -> Path:
    """Lay a sample out as a ``/proc`` :func:`podbench.proc.scan_processes` reads.

    ``root`` is created as a symlink for a process this seat may ptrace-read and
    left absent for one it may not, because that link is what
    :func:`podbench.proc.ptrace_readable` measures.
    """
    (root / "self").mkdir(parents=True, exist_ok=True)
    (root / "self" / "cgroup").write_text("0::/\n")
    for info in processes:
        entry = root / str(info.pid)
        entry.mkdir()
        (entry / "comm").write_text(f"{info.comm}\n")
        (entry / "cmdline").write_text("\x00".join(info.cmdline.split()) + "\x00")
        own = f"0::/../cri-containerd-{CID}.scope" if info.is_target else "0::/"
        (entry / "cgroup").write_text(f"{own}\n")
        (entry / "status").write_text(
            f"Name:\t{info.comm}\n"
            f"State:\t{info.state} (whatever)\n"
            f"PPid:\t{info.ppid}\n"
            f"Uid:\t{info.uid}\t{info.uid}\t{info.uid}\t{info.uid}\n"
            f"Threads:\t{info.threads}\n"
        )
        if info.ptrace_readable:
            (entry / "root").symlink_to(root)
    return root


def without_measurements(processes: list[ProcInfo]) -> list[ProcInfo]:
    """The same processes as a seat too old to report state or threads saw them.

    The ranking has to degrade to the old depth-and-shell answer rather than
    demote everything, so "not measured" must not read as "denied".
    """
    return [
        replace(info, state=None, threads=None, ptrace_readable=None)
        for info in processes
    ]
