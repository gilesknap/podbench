"""Interactive process selection and GDB attachment inside a podbench seat."""

from __future__ import annotations

import os
import pwd
import select
import shlex
import shutil
import sys
import termios
import tty
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.live import Live
from rich.table import Table
from rich.text import Text

from .cli import console, error_console, new_app, run


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    uid: int
    state: str
    command: str


def _read_process(path: Path) -> Process | None:
    try:
        fields = {
            key: value.strip()
            for line in (path / "status").read_text().splitlines()
            for key, separator, value in [line.partition(":")]
            if separator
        }
        arguments = (path / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        printable = [
            " ".join(argument.decode(errors="replace").split())
            for argument in arguments
        ]
        command = shlex.join(printable) if arguments else f"[{fields['Name']}]"
        return Process(
            pid=int(path.name),
            ppid=int(fields["PPid"]),
            uid=int(fields["Uid"].split()[0]),
            state=fields["State"].split()[0],
            command=command,
        )
    except (
        FileNotFoundError,
        KeyError,
        PermissionError,
        ProcessLookupError,
        ValueError,
    ):
        return None


def _processes() -> list[Process]:
    found = [
        process
        for path in Path("/proc").iterdir()
        if path.name.isdecimal()
        if (process := _read_process(path)) is not None
        if process.pid != os.getpid()
    ]
    return sorted(found, key=lambda process: process.pid)


def _flatten(processes: list[Process]) -> list[tuple[Process, str]]:
    by_pid = {process.pid: process for process in processes}
    children: dict[int, list[Process]] = defaultdict(list)
    for process in processes:
        children[process.ppid].append(process)
    roots = [
        process
        for process in processes
        if process.ppid not in by_pid or process.ppid == process.pid
    ]
    rows: list[tuple[Process, str]] = []
    visited: set[int] = set()

    def visit(process: Process, prefix: str, branch: str) -> None:
        if process.pid in visited:
            return
        visited.add(process.pid)
        rows.append((process, f"{prefix}{branch}"))
        descendants = children.get(process.pid, [])
        next_prefix = prefix + ("   " if branch == "└─ " else "│  " if branch else "")
        for index, child in enumerate(descendants):
            last = index == len(descendants) - 1
            visit(child, next_prefix, "└─ " if last else "├─ ")

    for root in roots:
        visit(root, "", "")
    for process in processes:
        visit(process, "", "")
    return rows


def _username(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _tree(rows: list[tuple[Process, str]], selected: int | None) -> Table:
    table = Table(title="Select a process to debug", expand=True)
    table.add_column("", width=1)
    table.add_column("PID", justify="right", width=7, no_wrap=True)
    table.add_column("USER", width=12, no_wrap=True)
    table.add_column("S", width=1, no_wrap=True)
    table.add_column("COMMAND", ratio=1, overflow="ellipsis", no_wrap=True)
    start = 0
    visible = rows
    if selected is not None:
        capacity = max(3, console.height - 7)
        start = min(max(0, selected - capacity // 2), max(0, len(rows) - capacity))
        visible = rows[start : start + capacity]
    for offset, (process, prefix) in enumerate(visible):
        index = start + offset
        style = "bold reverse" if index == selected else None
        command = Text(prefix + process.command)
        if process.pid == 1:
            command.append("  main", style="green")
        table.add_row(
            "❯" if index == selected else "",
            str(process.pid),
            _username(process.uid),
            process.state,
            command,
            style=style,
        )
    position = (
        f" • showing {start + 1}-{start + len(visible)} of {len(rows)}"
        if len(visible) < len(rows)
        else ""
    )
    table.caption = f"↑/↓ select • Enter attach • Esc cancel{position}"
    return table


@contextmanager
def _terminal() -> Iterator[None]:
    descriptor = sys.stdin.fileno()
    settings = termios.tcgetattr(descriptor)
    try:
        tty.setcbreak(descriptor)
        yield
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, settings)


def _key() -> str:
    descriptor = sys.stdin.fileno()
    value = os.read(descriptor, 1)
    if value != b"\x1b":
        return {b"\r": "enter", b"\n": "enter", b"k": "up", b"j": "down"}.get(value, "")
    if not select.select([descriptor], [], [], 0.04)[0]:
        return "escape"
    sequence = value + os.read(descriptor, 2)
    return {b"\x1b[A": "up", b"\x1b[B": "down"}.get(sequence, "escape")


def _choose(rows: list[tuple[Process, str]]) -> Process | None:
    selected = next(
        (index for index, (process, _) in enumerate(rows) if process.pid == 1), 0
    )
    try:
        with (
            _terminal(),
            Live(
                _tree(rows, selected),
                console=console,
                auto_refresh=False,
                transient=True,
            ) as live,
        ):
            while True:
                key = _key()
                if key == "escape":
                    return None
                if key == "enter":
                    return rows[selected][0]
                if key == "up":
                    selected = (selected - 1) % len(rows)
                elif key == "down":
                    selected = (selected + 1) % len(rows)
                live.update(_tree(rows, selected), refresh=True)
    except KeyboardInterrupt:
        return None


def _has_ptrace_capability() -> bool:
    try:
        status = Path("/proc/self/status").read_text().splitlines()
        effective = next(
            line.split()[1] for line in status if line.startswith("CapEff:")
        )
        return bool(int(effective, 16) & (1 << 19))
    except (FileNotFoundError, PermissionError, StopIteration, ValueError):
        return False


def _ptrace_warning(process: Process) -> str | None:
    if _has_ptrace_capability():
        return None
    if os.geteuid() != process.uid:
        return "the seat lacks SYS_PTRACE and the process has a different UID"
    try:
        scope = int(Path("/proc/sys/kernel/yama/ptrace_scope").read_text())
    except (FileNotFoundError, PermissionError, ValueError):
        return None
    if scope > 0:
        return f"ptrace_scope={scope} and the seat lacks SYS_PTRACE"
    return None


def debug(pid: int | None) -> int:
    processes = _processes()
    rows = _flatten(processes)
    if not rows:
        error_console.print("no visible processes")
        return 1
    by_pid = {process.pid: process for process, _ in rows}
    if pid is not None:
        process = by_pid.get(pid)
        if process is None:
            error_console.print(f"PID {pid} is not visible")
            return 1
    elif not sys.stdin.isatty() or not console.is_terminal:
        console.print(_tree(rows, None))
        error_console.print("pass a PID when podbench debug is not interactive")
        return 2
    else:
        process = _choose(rows)
        if process is None:
            return 0

    executable = Path(f"/proc/{process.pid}/exe")
    root = Path(f"/proc/{process.pid}/root")
    try:
        executable.readlink()
    except FileNotFoundError:
        error_console.print(f"cannot read {executable}; the process may have exited")
        return 1
    except PermissionError:
        error_console.print(f"cannot access {executable}; permission denied")
        return 1
    if warning := _ptrace_warning(process):
        error_console.print(f"warning: GDB may be denied: {warning}", style="yellow")
    if shutil.which("gdb") is None:
        error_console.print(
            "gdb is not installed; run this command inside a podbench seat"
        )
        return 1
    arguments = [
        "gdb",
        "-q",
        "-iex",
        f"set sysroot {root}",
        "-se",
        str(executable),
        "-p",
        str(process.pid),
    ]
    os.execvp(arguments[0], arguments)


def _build_app() -> typer.Typer:
    app = new_app()

    @app.command()
    def debug_command(
        pid: Annotated[
            int | None, typer.Argument(metavar="PID", help="process to attach to")
        ] = None,
    ) -> None:
        """Select a process and attach GDB from inside a seat."""
        raise typer.Exit(debug(pid))

    return app


def main(args: Sequence[str] | None = None) -> int:
    argv = list(args) if args is not None else None
    if argv and argv[0] == "debug":
        argv.pop(0)
    return run(_build_app(), argv, prog="podbench debug")
