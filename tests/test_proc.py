"""Choosing which of a container's processes to debug.

The rest of ``proc.py`` is covered by its doctests and by the probe's suite;
this file is about :func:`podbench.proc.debug_candidates`, which exists because
an image whose entrypoint is a script has a **shell** as its lowest pid. The
tree below is the one that produced the bug, from a live EPICS IOC pod: pid 1
is ``/bin/bash /epics/ioc/start.sh`` and the process anybody wants is pid 13,
three levels down.
"""

from __future__ import annotations

from pathlib import Path

from podbench.model import ProcInfo
from podbench.proc import (
    candidate_note,
    debug_candidates,
    is_shell,
    read_ppid,
    scan_processes,
)
from proc_samples import CID, sample, targets, without_measurements, write_tree

TARGET_CID = "cafe1234cafe1234cafe1234cafe1234"


def ioc_tree() -> list[ProcInfo]:
    """The observed pod: bash -> python -> sh -c -> ioc."""
    return [
        ProcInfo(1, 0, "bash", "/bin/bash /epics/ioc/start.sh", ppid=0),
        ProcInfo(8, 0, "python", "/venv/bin/python /venv/bin/stdio-expose", ppid=1),
        ProcInfo(11, 0, "sh", "/bin/sh -c /epics/ioc/start.sh", ppid=8),
        ProcInfo(13, 0, "ioc", "/epics/ioc/bin/linux-x86_64/ioc", ppid=11),
    ]


def test_the_entrypoint_shell_is_not_the_target() -> None:
    """The whole bug: lowest-pid picks bash, and gdb on bash tells you nothing.

    ``ioc`` wins over the equally non-shell ``python`` on depth — an entrypoint
    script is an *ancestor* of the thing it starts, so the deepest process is
    the one the pod exists to run.
    """
    assert [info.pid for info in debug_candidates(ioc_tree())] == [13, 8, 1, 11]


def test_shells_sort_last_rather_than_out() -> None:
    """A container that is only a shell still has to have a target.

    ``podbench dev`` lands exactly here — a seat with a login shell and nothing
    else — and a discovery that returned nothing would refuse to emit anything
    at all rather than emit something limited.
    """
    only_shells = [
        ProcInfo(1, 0, "bash", "/bin/bash", ppid=0),
        ProcInfo(9, 0, "sh", "/bin/sh", ppid=1),
    ]
    assert [info.pid for info in debug_candidates(only_shells)] == [1, 9]


def test_a_shell_head_is_described_as_such_and_not_as_the_deepest() -> None:
    note = candidate_note(
        debug_candidates(
            [
                ProcInfo(1, 0, "bash", "/bin/bash", ppid=0),
                ProcInfo(9, 0, "sh", "/bin/sh", ppid=1),
            ]
        ),
        "debugging",
    )
    assert note is not None
    assert "the only process here" in note
    assert "the deepest" not in note


def test_the_note_names_what_was_skipped_rather_than_counting_it() -> None:
    """A reader who disagrees with the choice needs the other pid, not a total."""
    note = candidate_note(debug_candidates(ioc_tree()), "debugging")
    assert note is not None
    assert "debugging pid 13 (ioc)" in note
    assert "Also debuggable: 8 (python)" in note
    assert "Skipped as a wrapper: 1 (bash), 11 (sh)" in note


def test_one_process_gets_no_note() -> None:
    """Nothing was chosen, so there is nothing to justify."""
    assert candidate_note([ProcInfo(13, 0, "ioc", "ioc", ppid=0)], "debugging") is None


def test_a_reused_pid_cannot_hang_the_walk() -> None:
    """``ppid`` is read per process, so two reads can disagree.

    A process tree has no cycles, but a pid reused between the read of pid 5's
    parent and pid 6's can produce one in the *snapshot* — and a naive walk of
    that snapshot never terminates.
    """
    cyclic = [
        ProcInfo(5, 0, "a", "a", ppid=6),
        ProcInfo(6, 0, "b", "b", ppid=5),
    ]
    assert [info.pid for info in debug_candidates(cyclic)] == [5, 6]


def test_ppid_comes_from_status_and_survives_an_unreadable_one(
    tmp_path: Path,
) -> None:
    """An unreadable ``status`` is a real outcome: depth 0, never an exception."""
    readable = tmp_path / "13"
    readable.mkdir()
    (readable / "status").write_text("Name:\tioc\nPPid:\t11\n")
    assert read_ppid(13, proc=tmp_path) == 11
    assert read_ppid(99, proc=tmp_path) is None


def test_the_scan_carries_the_parent_through(tmp_path: Path) -> None:
    """Ranking is only as good as the field it ranks on, so the scan must fill it."""
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "cgroup").write_text("0::/\n")
    for pid, comm, ppid in ((1, "bash", 0), (13, "ioc", 1)):
        entry = tmp_path / str(pid)
        entry.mkdir()
        (entry / "comm").write_text(f"{comm}\n")
        (entry / "cmdline").write_text(f"{comm}\x00")
        (entry / "cgroup").write_text(f"0::/../cri-containerd-{TARGET_CID}.scope\n")
        (entry / "status").write_text(
            f"Name:\t{comm}\nPPid:\t{ppid}\nUid:\t0\t0\t0\t0\n"
        )

    targets = scan_processes(TARGET_CID, proc=tmp_path).targets
    assert {info.pid: info.ppid for info in targets} == {1: 0, 13: 1}
    assert [info.pid for info in debug_candidates(targets)] == [13, 1]


# -- the four pods the ranking was corrected against -------------------------
#
# Each of these is a `/proc` capture from the p47 beamline (2026-08-19), not an
# invented tree: see tests/proc_samples.py. Ranking on depth alone picked the
# wrong process on three of the four, and each got a different predicate wrong.


def test_a_zombie_never_outranks_the_process_that_forked_it() -> None:
    """blueapi, and the whole of issue #52.

    An unreaped ``multiprocessing`` child sits two levels below pid 1, so depth
    put it first on every fresh pod. It has no ``mm_struct``, which means
    ``maps`` opens with no ptrace check and the read matrix scores it 3/6 — a
    number that reads exactly like a partial LSM denial. ``PTRACE_ATTACH`` to it
    is EPERM; to pid 1, from the same seat in the same second, it is not.
    """
    candidates = debug_candidates(targets("blueapi"))
    assert [info.pid for info in candidates] == [1, 233, 10, 232, 518]
    zombie = candidates[-1]
    assert zombie.pid == 518
    assert zombie.state == "Z"
    assert not zombie.alive


def test_a_throwaway_helper_does_not_outrank_the_broker() -> None:
    """rabbitmq: ``beam.smp`` at depth 1, ``inet_gethost`` at depth 3.

    A DNS resolver the VM forks and reaps continuously, with one thread against
    the broker's 208. Depth cannot tell them apart and thread count does not
    have to try.
    """
    candidates = debug_candidates(targets("rabbitmq"))
    assert candidates[0].pid == 1
    assert candidates[0].comm == "beam.smp"
    assert candidates[0].threads == 208
    # The two helpers are still offered, just not first: they are live, and a
    # process nobody wants to debug is not a process nobody may debug.
    assert [info.comm for info in candidates[1:3]] == ["inet_gethost"] * 2


def test_a_worker_this_seat_cannot_ptrace_does_not_outrank_the_master() -> None:
    """opis: an nginx master at uid 0 and 97 workers at uid 101.

    The seat here is root and *capless*, so the kernel's credential check lets
    it ptrace the master and none of the workers — while depth prefers a worker,
    every one of which is one level below the master.
    """
    candidates = debug_candidates(targets("opis"))
    assert candidates[0].pid == 1
    assert candidates[0].uid == 0
    assert candidates[0].ptrace_readable
    workers = [info for info in candidates if info.uid == 101]
    assert len(workers) > 90
    assert not any(info.ptrace_readable for info in workers)


def test_a_start_script_in_the_command_line_is_not_a_shell() -> None:
    """simdet: one process, ``comm=stdio-socket``, argv ending in ``start.sh``.

    Only ``argv[0]``'s basename is consulted, so the ``/epics/ioc/start.sh``
    further along the line does not disqualify the single process this pod has.
    """
    only = targets("simdet")
    assert len(only) == 1
    assert not is_shell(only[0])
    assert debug_candidates(only) == only


def test_a_seat_that_measured_nothing_falls_back_to_depth() -> None:
    """An older image reports no state, threads or readability.

    ``None`` there must mean "not measured" and not "denied": demoting every
    process for a question that was never asked would leave the ranking worse
    than the depth-only one it replaced.
    """
    unmeasured = without_measurements(targets("rabbitmq"))
    ranked = [info.pid for info in debug_candidates(unmeasured)]
    # The pre-C8 answer: deepest non-shell first, the entrypoint shell last.
    assert ranked[0] == 224
    assert ranked[-1] == 447


def test_the_note_separates_the_disqualified_from_the_alternatives() -> None:
    """ "Choose another" is only advice for a process that could be chosen."""
    note = candidate_note(debug_candidates(targets("blueapi")), "probing")
    assert note is not None
    assert "probing pid 1 (python), the only live process here" not in note
    assert "Skipped as dead: 518 (python)" in note
    assert "518" not in note.split("Skipped as dead")[0]


def test_the_note_counts_the_workers_it_does_not_name() -> None:
    """opis names four runners-up and counts the rest.

    Naming 97 identical nginx workers turns a one-line note into a paragraph,
    and the line is relayed into the attach report where a paragraph is against
    the house style.
    """
    note = candidate_note(debug_candidates(targets("opis")), "probing")
    assert note is not None
    assert "the only process here this seat can ptrace-read" in note
    assert "Not ptrace-readable from this seat: 29 (nginx)" in note
    assert "more" in note
    assert len(note.splitlines()) == 1


def test_the_scan_reads_state_threads_and_readability(tmp_path: Path) -> None:
    """The ranking is only as good as the fields it ranks on."""
    proc = write_tree(tmp_path, sample("blueapi"))
    listing = scan_processes(CID, proc=proc)
    by_pid = {info.pid: info for info in listing.targets}
    assert by_pid[1].state == "S"
    assert by_pid[1].threads == 195
    assert by_pid[1].ptrace_readable is True
    assert by_pid[518].state == "Z"
    assert by_pid[518].ptrace_readable is False
    assert [info.pid for info in debug_candidates(listing.targets)][0] == 1
