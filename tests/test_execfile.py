"""Issue #90: the exec file gdb is given, and the copy that keeps it honest.

The defect these cover is a *path* collision, not a broken binary, so every
fixture here is two files with one name: one under the target's
``/proc/<pid>/root``, one under our own ``/proc/self/root``. The bytes are
deliberately trivial and deliberately different — what is asserted is which of
them a ``file`` command would end up reading, which is decided before any ELF is
parsed.

Nothing here runs gdb. That the collapse happens at all is measured on a cluster
(``tests/e2e/test_shadowed_exec_file.py``); this file pins the decision podbench
takes because of it, including the two cases where it must *not* stage a copy.
"""

from __future__ import annotations

import os
from pathlib import Path

from podbench.execfile import gdb_exec_file, shadowing_file, stage_exec_file

TARGET_PID = 597

EXE = "/python/cpython-3.11.15-linux-x86_64-gnu/bin/python3.11"
"""The real collision, spelled out. The seat image and any uv-managed workload
both install an interpreter here, which is why a Python target collides by
construction rather than by bad luck."""

TARGET_BYTES = "the target's build\n"
SEAT_BYTES = "this seat's own build, a different file at the same path\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _proc(
    tmp_path: Path, *, ours: str | None, theirs: str | None = TARGET_BYTES
) -> Path:
    """A ``/proc`` with two rootfs views, and ``EXE`` in one or both of them.

    ``ours`` is ``None`` for the target whose path this container knows nothing
    about — the common case, and the one that must keep behaving exactly as it
    did before #90 was fixed.
    """
    proc = tmp_path / "proc"
    (proc / "self" / "root").mkdir(parents=True)
    (proc / str(TARGET_PID) / "root").mkdir(parents=True)
    if ours is not None:
        _write(proc / "self" / "root" / EXE.lstrip("/"), ours)
    if theirs is not None:
        _write(proc / str(TARGET_PID) / "root" / EXE.lstrip("/"), theirs)
    return proc


def test_an_unshadowed_target_keeps_the_sysroot_path(tmp_path: Path) -> None:
    """No collision, no copy, no note: report 4.3's command sequence unchanged.

    The sysroot path is right for every target that does not share a base-image
    convention with the seat, and staging one anyway would spend the pod's
    ephemeral storage on every attach.
    """
    proc = _proc(tmp_path, ours=None)
    staging = tmp_path / "staged"

    path, notes = gdb_exec_file(TARGET_PID, EXE, proc=proc, staging=staging)

    assert path == f"/proc/{TARGET_PID}/root{EXE}"
    assert notes == []
    assert not staging.exists()


def test_a_shadowed_target_gets_a_copy_of_its_own_binary(tmp_path: Path) -> None:
    """The fix, in one assertion: the path gdb is given holds the target's bytes.

    gdb resolves ``/proc/<pid>/root`` to ``/`` and reads whatever is at the bare
    path, so the only way to be sure it reads the target's binary is to hand it
    a name nothing of ours occupies.
    """
    proc = _proc(tmp_path, ours=SEAT_BYTES)
    staging = tmp_path / "staged"

    path, notes = gdb_exec_file(TARGET_PID, EXE, proc=proc, staging=staging)

    assert path != f"/proc/{TARGET_PID}/root{EXE}"
    assert Path(path).read_text() == TARGET_BYTES
    assert Path(path).name == "python3.11", (
        "the basename is what gdb prints back in `info files` and every "
        f"'Symbols from' line, so it is kept: {path}"
    )
    assert len(notes) == 1 and EXE in notes[0] and path in notes[0], (
        f"the note has to name both files for the reader to act on it: {notes}"
    )


def test_the_note_is_one_line(tmp_path: Path) -> None:
    """Its callers pass it to a ``warning:`` printer, which does not wrap."""
    proc = _proc(tmp_path, ours=SEAT_BYTES)

    _, notes = gdb_exec_file(TARGET_PID, EXE, proc=proc, staging=tmp_path / "staged")

    assert notes and all("\n" not in note for note in notes)


def test_a_process_in_our_own_container_is_left_alone(tmp_path: Path) -> None:
    """``dev`` mode debugs a process in this very container.

    ``/proc/<pid>/root`` and ``/proc/self/root`` are then the same directory, so
    the canonicalisation lands back on the file gdb opened and there is nothing
    to work around. Without this guard every ``podbench dbg`` in a dev pod would
    copy the program before debugging it.
    """
    proc = tmp_path / "proc"
    (proc / "self" / "root").mkdir(parents=True)
    _write(proc / "self" / "root" / EXE.lstrip("/"), SEAT_BYTES)
    (proc / str(TARGET_PID)).mkdir()
    (proc / str(TARGET_PID) / "root").symlink_to(proc / "self" / "root")
    staging = tmp_path / "staged"

    assert shadowing_file(TARGET_PID, EXE, proc=proc) is None
    path, notes = gdb_exec_file(TARGET_PID, EXE, proc=proc, staging=staging)
    assert path == f"/proc/{TARGET_PID}/root{EXE}"
    assert notes == [] and not staging.exists()


def test_a_dangling_symlink_of_ours_still_shadows(tmp_path: Path) -> None:
    """``lexists``, not ``exists``.

    gdb opens the collapsed name in *our* rootfs whatever is there; a symlink of
    ours pointing somewhere else is a different file again, and one pointing
    nowhere is an ENOENT — which is the trailing ``: No such file or
    directory.`` in the issue's own transcript.
    """
    proc = _proc(tmp_path, ours=None)
    ours = proc / "self" / "root" / EXE.lstrip("/")
    ours.parent.mkdir(parents=True, exist_ok=True)
    ours.symlink_to(tmp_path / "nothing-here")

    assert shadowing_file(TARGET_PID, EXE, proc=proc) == EXE


def test_a_failed_copy_falls_back_and_says_which_file_gdb_will_read(
    tmp_path: Path,
) -> None:
    """Staging is best-effort: the sysroot path is wrong, but it is not fatal.

    ``podbench dbg`` on the returned path still attaches and still gives a
    backtrace — it just gets the seat's symbols for the main executable, which
    is the state #90 describes. Refusing to debug at all would be worse, so the
    only thing that must not happen is silence.
    """
    proc = _proc(tmp_path, ours=SEAT_BYTES)
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")

    path, notes = gdb_exec_file(TARGET_PID, EXE, proc=proc, staging=blocked / "staged")

    assert path == f"/proc/{TARGET_PID}/root{EXE}"
    assert len(notes) == 1 and EXE in notes[0]
    assert "#90" in notes[0]


def test_the_deleted_marker_never_reaches_a_path(tmp_path: Path) -> None:
    """``/proc/<pid>/exe`` reads ``<path> (deleted)`` after an upgrade in place.

    Both the question and the copy have to be asked about the real name, or a
    redeployed target quietly stops being staged and #90 comes back for the one
    workload most likely to be under active development.
    """
    proc = _proc(tmp_path, ours=SEAT_BYTES)
    staging = tmp_path / "staged"

    path, _ = gdb_exec_file(TARGET_PID, f"{EXE} (deleted)", proc=proc, staging=staging)

    assert "(deleted)" not in path
    assert Path(path).read_text() == TARGET_BYTES


def test_a_stale_copy_is_replaced_rather_than_reused(tmp_path: Path) -> None:
    """Pids are reused, and a seat outlives the processes it staged for.

    Reusing a copy because one is already at the right name would reintroduce
    the defect from our own cache: the right path holding the wrong build.
    """
    proc = _proc(tmp_path, ours=SEAT_BYTES)
    staging = tmp_path / "staged"
    stale = Path(stage_exec_file(TARGET_PID, EXE, proc=proc, staging=staging))
    stale.write_text("what the previous pid 597 was running\n")

    fresh = stage_exec_file(TARGET_PID, EXE, proc=proc, staging=staging)

    assert fresh == str(stale)
    assert Path(fresh).read_text() == TARGET_BYTES


def test_staging_leaves_no_partial_file_behind(tmp_path: Path) -> None:
    """A failed copy must not leave a truncated ELF at a name gdb would trust."""
    proc = _proc(tmp_path, ours=SEAT_BYTES, theirs=None)
    staging = tmp_path / "staged"

    path, notes = gdb_exec_file(TARGET_PID, EXE, proc=proc, staging=staging)

    assert path == f"/proc/{TARGET_PID}/root{EXE}" and notes
    assert os.listdir(staging / str(TARGET_PID)) == []
