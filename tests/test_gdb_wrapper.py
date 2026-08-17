"""``image/bin/gdb-podbench``, exercised as the shell script it is.

The wrapper is now installed as ``gdb`` on PATH, which changes who calls it:
not just cpptools with an absolute ``program``, but anything in a seat that
types ``gdb``. Both of its behaviours are therefore load-bearing for arguments
it never used to see, and neither announces itself when it goes wrong — a
sysroot that was not set produces a plausible backtrace off this container's
libraries (report 3.3), and a cwd that moved turns ``gdb ./a.out`` into a file
that is not found.

The script ``exec``s ``/usr/bin/gdb`` and calls ``/usr/local/bin/podbench`` by
absolute path, both right for the image and untestable here, so each test runs a
copy with those two strings rewritten to stubs. :func:`_wrapper` asserts each
rewrite actually matched, so the day the script calls either of them differently
these tests fail rather than quietly testing nothing.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "image" / "bin" / "gdb-podbench"

REAL_GDB = "/usr/bin/gdb"
"""The one spelling the script uses to reach gdb. Rewritten to the stub."""

REAL_PODBENCH = "/usr/local/bin/podbench"
"""How the script asks for the exec file to give ``file`` (issue #90).

Absolute, and the image's shim rather than the venv, for the same reason
``gdb-podbench`` is reached absolutely: debugpy shells out through ``sh -c``
with whatever ``PATH`` the workload's process had.
"""

_STUB = """#!/bin/sh
# Report what gdb would have been given, and from where: the cwd on the first
# line, then one argument per line. Line-per-argument rather than a quoted
# format because the shell has no json and every argument under test is a
# single line.
/bin/pwd
for argument in "$@"; do
	printf '%s\\n' "$argument"
done
"""

_PODBENCH_STUB = """#!/bin/sh
# Stand in for `podbench dbg <pid> --print-exec-file`: record the argv so a test
# can assert it was not called at all, then answer with whatever the test put in
# ANSWER (an empty file standing for "nothing to say").
printf '%s\\n' "$*" >> "ARGV"
cat "ANSWER"
"""


def _wrapper(tmp_path: Path, *, exec_file: str | None = None) -> Path:
    """A runnable copy of the wrapper whose gdb and podbench are both stubs.

    ``exec_file`` is what the stub podbench answers with; ``None`` is the seat
    that could not read ``/proc/<pid>/exe``, which has to leave gdb exactly as
    it was.
    """
    stub = tmp_path / "stub-gdb"
    stub.write_text(_STUB)
    stub.chmod(0o755)

    answer = tmp_path / "answer"
    answer.write_text("" if exec_file is None else f"{exec_file}\n")
    podbench = tmp_path / "stub-podbench"
    podbench.write_text(
        _PODBENCH_STUB.replace("ARGV", str(tmp_path / "podbench-argv")).replace(
            "ANSWER", str(answer)
        )
    )
    podbench.chmod(0o755)

    source = WRAPPER.read_text()
    for real, replacement in ((REAL_GDB, stub), (REAL_PODBENCH, podbench)):
        assert real in source, (
            f"the wrapper no longer calls {real}; this test's rewrite is stale"
        )
        source = source.replace(real, str(replacement))
    script = tmp_path / "gdb-podbench"
    script.write_text(source)
    script.chmod(0o755)
    return script


@dataclass(frozen=True)
class Invocation:
    """What the stub standing in for gdb was given."""

    cwd: str
    args: list[str]

    @classmethod
    def parse(cls, stdout: str) -> Invocation:
        cwd, *args = stdout.splitlines()
        return cls(cwd, args)


def _run(script: Path, *args: str, cwd: Path | str | None = None) -> Invocation:
    """Run the wrapper and return what the stub gdb saw."""
    result = subprocess.run(
        [str(script), *args],
        capture_output=True,
        text=True,
        check=True,
        cwd=None if cwd is None else str(cwd),
    )
    return Invocation.parse(result.stdout)


def test_a_usable_cwd_is_left_alone(tmp_path: Path) -> None:
    """The regression that made the wrapper unsafe to be ``gdb`` on PATH.

    gdb resolves a relative argument against its own working directory, so
    moving to /root unconditionally turned an ordinary ``gdb ./a.out`` into a
    missing file. The cwd is only abandoned when it is genuinely gone.
    """
    work = tmp_path / "src"
    work.mkdir()
    (work / "a.out").touch()

    seen = _run(_wrapper(tmp_path), "./a.out", cwd=work)

    assert seen.cwd == str(work)
    assert seen.args == ["./a.out"]


def test_a_deleted_cwd_is_escaped(tmp_path: Path) -> None:
    """gdb links libpython, whose init calls getcwd() and dies without one.

    VS Code replaces an extension directory wholesale on update, so a
    long-lived cpptools really does end up in an unlinked cwd.
    """
    script = _wrapper(tmp_path)
    gone = tmp_path / "gone"
    gone.mkdir()

    result = subprocess.run(
        ["sh", "-c", f'cd "{gone}" && rmdir "{gone}" && exec "{script}" --version'],
        capture_output=True,
        text=True,
        check=True,
    )
    seen = Invocation.parse(result.stdout)

    assert seen.cwd != str(gone)
    # /root when it is reachable, / when the tests run as an ordinary user.
    assert seen.cwd in ("/root", "/")


LIVE_PID = 1
"""A pid that is present in whatever ``/proc`` the tests can see.

Deliberately not :func:`os.getpid`: a sandboxed runner can put the test process
in a pid namespace whose ``/proc`` is not the one mounted, and the wrapper's
liveness test would then fail for a process that plainly exists. PID 1 is
always there, and is the pid a seat's target usually has anyway.
"""


@pytest.mark.parametrize("spelling", ["--pid {pid}", "--pid={pid}", "-p {pid}"])
def test_a_live_pid_gets_a_sysroot(tmp_path: Path, spelling: str) -> None:
    """Every spelling gdb accepts, because a caller picks its own.

    ``-iex`` rather than ``-ex``: ``--pid`` attaches during startup, so an
    ``-ex`` command runs after the attach and is too late.
    """
    pid = LIVE_PID
    given = spelling.format(pid=pid).split()
    seen = _run(_wrapper(tmp_path), *given)

    assert seen.args[:2] == ["-iex", f"set sysroot /proc/{pid}/root"]
    assert seen.args[2:] == given


def test_a_live_pid_also_gets_an_exec_file(tmp_path: Path) -> None:
    """Issue #90: without a ``file`` command gdb reads *this* container's binary.

    ``gdb --pid <n>`` finds the exec file from /proc/<n>/exe and canonicalises
    the name; /proc/<n>/root canonicalises to ``/``, so the sysroot injected
    above is erased and BFD opens our own file of the same name. Both commands
    are ``-iex`` because --pid attaches during startup, and both must precede
    the caller's own arguments.
    """
    seen = _run(
        _wrapper(tmp_path, exec_file="/tmp/podbench-exe/12/python3.11"),
        "--nx",
        "--pid",
        str(LIVE_PID),
    )

    assert seen.args[:4] == [
        "-iex",
        f"set sysroot /proc/{LIVE_PID}/root",
        "-iex",
        "file /tmp/podbench-exe/12/python3.11",
    ]
    assert seen.args[4:] == ["--nx", "--pid", str(LIVE_PID)]


def test_a_seat_with_no_answer_still_gets_its_sysroot(tmp_path: Path) -> None:
    """The exec file is an improvement, never a prerequisite.

    A degraded rung cannot read /proc/<pid>/exe at all, and a seat whose
    podbench is missing or broken answers nothing either. Both must leave the
    wrapper doing exactly what it did before the exec file was staged, because
    the sysroot alone is still the difference between this container's
    libraries and the target's (report 3.3).
    """
    seen = _run(_wrapper(tmp_path, exec_file=None), "--pid", str(LIVE_PID))

    assert seen.args == [
        "-iex",
        f"set sysroot /proc/{LIVE_PID}/root",
        "--pid",
        str(LIVE_PID),
    ]
    assert "file" not in " ".join(seen.args)


def test_podbench_is_not_asked_when_there_is_no_pid(tmp_path: Path) -> None:
    """``gdb ./a.out`` must not pay for a subprocess it cannot use.

    The wrapper is ``gdb`` on PATH for everything in the seat, so the question
    is only worth asking where an attach is actually happening.
    """
    _run(_wrapper(tmp_path, exec_file="/tmp/whatever"), "--nx", "/app/victim")

    assert not (tmp_path / "podbench-argv").exists()


def test_the_exec_file_is_asked_for_by_pid(tmp_path: Path) -> None:
    """The wrapper knows the pid and nothing else; podbench works out the rest."""
    _run(_wrapper(tmp_path, exec_file="/tmp/x"), "--pid", str(LIVE_PID))

    argv = (tmp_path / "podbench-argv").read_text().split()
    assert argv == ["dbg", str(LIVE_PID), "--print-exec-file"]


@pytest.mark.parametrize("pid", ["notanumber", "999999999"])
def test_an_unusable_pid_is_left_to_gdb(tmp_path: Path, pid: str) -> None:
    """A bad pid gets gdb's own error, which beats one invented by a wrapper.

    ``999999999`` is above every reachable ``pid_max``, so it stands in for a
    process that has already exited.
    """
    seen = _run(_wrapper(tmp_path), "--pid", pid)

    assert seen.args == ["--pid", pid]


def test_no_pid_means_no_sysroot(tmp_path: Path) -> None:
    """The ordinary ``gdb ./prog`` case must be passed through untouched."""
    seen = _run(_wrapper(tmp_path), "--nx", "/app/victim")

    assert seen.args == ["--nx", "/app/victim"]
