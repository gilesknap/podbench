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
from podbench.proc import candidate_note, debug_candidates, read_ppid, scan_processes

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
