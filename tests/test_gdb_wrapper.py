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

What the stub podbench answers with comes from
:func:`podbench.gdbcmd.startup_commands` rather than from a list typed here, so
these tests measure the wrapper's *handling* of the sequence and can never
disagree with the sequence itself. That is the whole point of finding 17.6: the
two lines this script used to carry by hand were a copy, and the copy silently
fell two lines behind.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from podbench.gdbcmd import startup_commands

WRAPPER = Path(__file__).resolve().parents[1] / "image" / "bin" / "gdb-podbench"

REAL_GDB = "/usr/bin/gdb"
"""The one spelling the script uses to reach gdb. Rewritten to the stub."""

REAL_PODBENCH = "/usr/local/bin/podbench"
"""How the script asks for the commands to run before the caller's own attach.

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
# Stand in for `podbench dbg <pid> --print-startup-commands`: record the argv so
# a test can assert it was not called at all, then answer with whatever the test
# put in ANSWER (an empty file standing for "nothing to say").
printf '%s\\n' "$*" >> "ARGV"
cat "ANSWER"
"""


def _wrapper(tmp_path: Path, *, commands: Sequence[str] = ()) -> Path:
    """A runnable copy of the wrapper whose gdb and podbench are both stubs.

    ``commands`` is what the stub podbench answers with; empty is the seat
    whose podbench is missing, older, or could not read ``/proc/<pid>/exe``,
    which has to leave the wrapper doing what it did before it asked.
    """
    stub = tmp_path / "stub-gdb"
    stub.write_text(_STUB)
    stub.chmod(0o755)

    answer = tmp_path / "answer"
    answer.write_text("".join(f"{command}\n" for command in commands))
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


SEQUENCE = startup_commands(
    LIVE_PID, exec_file="/tmp/podbench-exe/12/python3.11", debuginfod=None
)
"""What ``podbench dbg --print-startup-commands`` answers, from its own author.

Never a list typed out here. The sequence is a correctness property of report
4.3 and of finding 17.6, and a fixture copy of it would be one more copy to
fall behind — which is the defect this test module now guards.
"""


@pytest.mark.parametrize("spelling", ["--pid {pid}", "--pid={pid}", "-p {pid}"])
def test_a_live_pid_gets_the_whole_startup_sequence(
    tmp_path: Path, spelling: str
) -> None:
    """Every spelling gdb accepts, because a caller picks its own.

    ``-iex`` rather than ``-ex`` throughout: ``--pid`` attaches during startup,
    so an ``-ex`` command runs after the attach and is too late — and the same
    is true of every line, not only the sysroot. The caller's own arguments
    stay last and stay in order.
    """
    pid = LIVE_PID
    given = spelling.format(pid=pid).split()

    seen = _run(_wrapper(tmp_path, commands=SEQUENCE), *given)

    expected: list[str] = []
    for command in SEQUENCE:
        expected += ["-iex", command]
    assert seen.args == [*expected, *given]


def test_the_sequence_carries_the_lines_the_copy_used_to_miss(
    tmp_path: Path,
) -> None:
    """Finding 17.6, stated as the two commands that were silently absent.

    ``add-auto-load-safe-path`` is what lets a sysrooted gdb auto-load the
    target's ``libthread_db``; without it every thread-aware command is gone
    and gdb says nothing about why. ``handle SIGURG`` is what keeps an attached
    Go target from becoming a wall of ``Program received signal SIGURG``. Both
    reach gdb through the wrapper now, and neither is spelled in the script.
    """
    seen = _run(_wrapper(tmp_path, commands=SEQUENCE), "--pid", str(LIVE_PID))

    assert f"add-auto-load-safe-path /proc/{LIVE_PID}/root" in seen.args
    assert "handle SIGURG nostop noprint pass" in seen.args


def test_the_exec_file_reaches_gdb_before_the_attach(tmp_path: Path) -> None:
    """Issue #90: without a ``file`` command gdb reads *this* container's binary.

    ``gdb --pid <n>`` finds the exec file from /proc/<n>/exe and canonicalises
    the name; /proc/<n>/root canonicalises to ``/``, so the sysroot is erased
    and BFD opens our own file of the same name. The staged path podbench
    answers with has to arrive as ``-iex``, ahead of the caller's ``--pid``.
    """
    seen = _run(_wrapper(tmp_path, commands=SEQUENCE), "--nx", "--pid", str(LIVE_PID))

    file_command = "file /tmp/podbench-exe/12/python3.11"
    assert file_command in seen.args
    assert seen.args[seen.args.index(file_command) - 1] == "-iex"
    assert seen.args[-3:] == ["--nx", "--pid", str(LIVE_PID)]


def test_a_seat_with_no_answer_still_gets_its_sysroot(tmp_path: Path) -> None:
    """The sequence is an improvement, never a prerequisite.

    A degraded rung cannot read /proc/<pid>/exe at all, a podbench older than
    this flag refuses it, and a seat whose podbench is missing or broken
    answers nothing either. All three must leave the wrapper doing what it did
    before it asked, because the sysroot alone is still the difference between
    this container's libraries and the target's (report 3.3).
    """
    seen = _run(_wrapper(tmp_path), "--pid", str(LIVE_PID))

    assert seen.args == [
        "-iex",
        f"set sysroot /proc/{LIVE_PID}/root",
        "--pid",
        str(LIVE_PID),
    ]


def test_the_fallback_is_the_only_command_the_script_spells_out() -> None:
    """The guard on finding 17.6: one hand-written command, and it is the last.

    Everything else is generated, so the only ``-iex`` literals in the script
    are the loop's ``$command`` and the fallback's sysroot. A third one means
    somebody has started keeping a copy again — which is exactly how
    ``add-auto-load-safe-path`` came to be missing for as long as it was.

    The fallback itself is checked against the generator rather than against a
    string, so it cannot drift either.
    """
    literals = re.findall(r'-iex "([^"]*)"', WRAPPER.read_text())

    assert literals == ["$command", "set sysroot /proc/$pid/root"]
    assert literals[1].replace("$pid", str(LIVE_PID)) in startup_commands(
        LIVE_PID, debuginfod=None
    )


def test_podbench_is_not_asked_when_there_is_no_pid(tmp_path: Path) -> None:
    """``gdb ./a.out`` must not pay for a subprocess it cannot use.

    The wrapper is ``gdb`` on PATH for everything in the seat, so the question
    is only worth asking where an attach is actually happening.
    """
    _run(_wrapper(tmp_path, commands=SEQUENCE), "--nx", "/app/victim")

    assert not (tmp_path / "podbench-argv").exists()


def test_the_sequence_is_asked_for_by_pid(tmp_path: Path) -> None:
    """The wrapper knows the pid and nothing else; podbench works out the rest."""
    _run(_wrapper(tmp_path, commands=SEQUENCE), "--pid", str(LIVE_PID))

    argv = (tmp_path / "podbench-argv").read_text().split()
    assert argv == ["dbg", str(LIVE_PID), "--print-startup-commands"]


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
