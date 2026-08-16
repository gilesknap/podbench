"""``image/bin/gdb-podbench``, exercised as the shell script it is.

The wrapper is now installed as ``gdb`` on PATH, which changes who calls it:
not just cpptools with an absolute ``program``, but anything in a seat that
types ``gdb``. Both of its behaviours are therefore load-bearing for arguments
it never used to see, and neither announces itself when it goes wrong — a
sysroot that was not set produces a plausible backtrace off this container's
libraries (report 3.3), and a cwd that moved turns ``gdb ./a.out`` into a file
that is not found.

The script ``exec``s ``/usr/bin/gdb`` by absolute path, which is right for the
image and untestable here, so each test runs a copy with that one string
rewritten to a stub. :func:`_wrapper` asserts the rewrite actually matched, so
the day the script calls gdb differently these tests fail rather than quietly
testing nothing.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "image" / "bin" / "gdb-podbench"

REAL_GDB = "/usr/bin/gdb"
"""The one spelling the script uses to reach gdb. Rewritten to the stub."""

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


def _wrapper(tmp_path: Path) -> Path:
    """A runnable copy of the wrapper whose gdb is a stub that reports its argv."""
    stub = tmp_path / "stub-gdb"
    stub.write_text(_STUB)
    stub.chmod(0o755)

    source = WRAPPER.read_text()
    assert REAL_GDB in source, (
        f"the wrapper no longer execs {REAL_GDB}; this test's rewrite is stale"
    )
    script = tmp_path / "gdb-podbench"
    script.write_text(source.replace(REAL_GDB, str(stub)))
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
