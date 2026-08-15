"""``pids`` and ``dbg`` — find the workload's processes, then debug them correctly.

Both helpers live inside the debug container, on the far side of the ssh
transport, and both exist because the obvious commands are wrong here.

``pids`` is not ``ps``: every process in the pod is visible under
``shareProcessNamespace: true``, including other podbench sessions', so the
listing has to say which container owns each one. Attribution keys off the
target's runtime id (report 4.3/3.15); when the launcher did not inject one the
fallback marks *every* other session's processes as target, so that answer is
labelled a guess rather than presented as fact.

``dbg`` is not ``gdb -p``. Section 4.3 fixes seven commands in one order, and
the order is a correctness property rather than a preference: ``set sysroot``
after ``attach`` yields a backtrace that looks plausible and is wrong
(``clock_nanosleep`` reported as ``wcsxfrm_l``, user-code line numbers off by
six). The only way to make that impossible is to generate the whole command
sequence in one place, which is what :func:`attach_commands` is. Nothing here
runs gdb interactively to feed it commands — the sequence is handed over at
exec time, so there is no window in which a half-configured gdb is attached.

Two further constraints from the report shape what is *not* here. Sources are
wired with ``directory``, never ``substitute-path``: the generic substitution
functions, but gdb re-applies it on display and hands the DAP client
``/proc/1/root/proc/1/root/…`` in ``info source``'s ``fullname``, which is
exactly the field the VS Code C++ extension consumes (3.3, R5). And debuginfod
is enabled for *symbols only* — every source fetch against Debian fails, twice
over, so ``dbg`` never promises source text it cannot deliver (3.2, R4).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated

import typer

from .cli import new_app, require_subcommand, run
from .model import CapabilityReport, ProcInfo, Verdict
from .probe import Attacher, probe
from .proc import (
    DEFAULT_PROC,
    ProcessListing,
    env_target_container_id,
    scan_processes,
)

__all__ = [
    "DELETED_SUFFIX",
    "EXIT_USAGE",
    "GdbRunner",
    "attach_blocked_message",
    "attach_commands",
    "command_file_text",
    "exec_gdb",
    "format_process_table",
    "gdb_argv",
    "launch_commands",
    "main",
    "pids_main",
    "pids_payload",
    "dbg_main",
    "read_exe",
    "resolve_target_pid",
    "strip_deleted",
    "sysroot_path",
]

GdbRunner = Callable[[Sequence[str]], int]
"""How ``dbg`` finally starts gdb. A seam so tests never exec a real gdb."""

DELETED_SUFFIX = " (deleted)"
"""What the kernel appends to ``/proc/<pid>/exe`` once the binary is unlinked.

Common in containers — a rebuild replaces the image layer under a running
process — and gdb takes the whole string as a filename, so it must come off.
"""

EXIT_USAGE = 2
"""Exit code for "there is nothing to debug", matching a usage error's own."""

_PIDS_DESCRIPTION = (
    "List the processes in this pod's shared PID namespace, and say which "
    "container owns each one."
)
_DBG_DESCRIPTION = (
    "Run gdb against a process in another container of this pod, with the "
    "sysroot, source path and auto-load path set in the order that produces a "
    "correct backtrace."
)


def strip_deleted(path: str) -> str:
    """Drop the kernel's ``" (deleted)"`` marker from an ``exe`` link target.

    >>> strip_deleted("/app/victim (deleted)")
    '/app/victim'
    >>> strip_deleted("/app/victim")
    '/app/victim'
    """
    return path[: -len(DELETED_SUFFIX)] if path.endswith(DELETED_SUFFIX) else path


def sysroot_path(pid: int) -> str:
    """The target's filesystem as seen from here.

    >>> sysroot_path(597)
    '/proc/597/root'
    """
    return f"/proc/{pid}/root"


def read_exe(pid: int, *, proc: Path = DEFAULT_PROC) -> str | None:
    """The target's executable path *inside its own rootfs*, or ``None``.

    ``None`` is a real answer, not an error: reading this link takes
    ``PTRACE_MODE_READ`` and so fails at the wrong UID (report 3.11). The
    caller has to decide what to do without it, and losing the ``file`` command
    is not fatal — only lossy.
    """
    try:
        target = os.readlink(proc / str(pid) / "exe")
    except OSError:
        return None
    return strip_deleted(target)


def attach_commands(
    pid: int,
    *,
    exe: str | None = None,
    source_dirs: Sequence[str] = (),
    debuginfod: bool = True,
) -> list[str]:
    """The gdb command sequence from report 4.3, in the one order that works.

    >>> for command in attach_commands(597, exe="/app/victim (deleted)"):
    ...     print(command)
    set pagination off
    set sysroot /proc/597/root
    directory /proc/597/root
    add-auto-load-safe-path /proc/597/root
    set debuginfod enabled on
    file /proc/597/root/app/victim
    attach 597

    Every line earns its place:

    * ``sysroot`` before ``attach`` — the other way round, libraries are fixed
      up on the fly but the main executable is not, and the frames above libc
      come back as ``?? ()`` while looking entirely believable (3.3).
    * ``directory`` rather than ``substitute-path`` — sysroot does not cover
      source lookup at all, and the substitution form corrupts ``fullname``.
    * ``add-auto-load-safe-path`` — setting a sysroot makes gdb decline to
      auto-load the target's ``libthread_db.so.1``, which costs every
      thread-aware command. Narrow, not ``set auto-load safe-path /``.
    * ``file`` before ``attach`` — this is what recovers the user frames, and
      it is the line that needs the deleted-suffix strip.
    """
    root = sysroot_path(pid)
    commands = [
        "set pagination off",
        f"set sysroot {root}",
        f"directory {root}",
    ]
    # gdb searches the *most recently added* directory first, so caller-supplied
    # sources deliberately follow the target's rootfs and win over it.
    commands += [f"directory {directory}" for directory in source_dirs]
    commands.append(f"add-auto-load-safe-path {root}")
    commands.append(f"set debuginfod enabled {'on' if debuginfod else 'off'}")
    if exe is not None:
        # String concatenation, never a path join: the exe link is absolute, and
        # joining would discard the sysroot and silently read our own binary.
        commands.append(f"file {root}{strip_deleted(exe)}")
    commands.append(f"attach {pid}")
    return commands


def launch_commands(
    program: str,
    args: Sequence[str] = (),
    *,
    source_dirs: Sequence[str] = (),
    debuginfod: bool = True,
    run: bool = False,
) -> list[str]:
    """The ptrace-free inner loop: let gdb *start* the program.

    ``PTRACE_TRACEME`` from a child gdb forked itself needs no capability and
    satisfies Yama unconditionally, so this works at uid 1000 with
    ``CapEff: 0`` under the restricted Pod Security Standard — where attach
    does not. Report 3.12 is explicit that this, not attach, is the inner loop
    to design for; attach is the privileged special case.

    No sysroot: the program runs in *this* container's mount namespace, so its
    libraries are already the ones gdb would find.

    >>> for command in launch_commands("./victim", ["--fast"], run=True):
    ...     print(command)
    set pagination off
    set debuginfod enabled on
    file ./victim
    set args --fast
    run
    """
    commands = ["set pagination off"]
    commands += [f"directory {directory}" for directory in source_dirs]
    commands.append(f"set debuginfod enabled {'on' if debuginfod else 'off'}")
    commands.append(f"file {program}")
    if args:
        commands.append("set args " + " ".join(args))
    if run:
        commands.append("run")
    return commands


def command_file_text(commands: Sequence[str]) -> str:
    """The commands as a ``gdb -x`` file — what the docs paste."""
    return "".join(f"{command}\n" for command in commands)


def gdb_argv(commands: Sequence[str], *, gdb: str = "gdb") -> list[str]:
    """Turn a command sequence into a gdb argv.

    ``-ex`` per command rather than a generated ``-x`` file: gdb runs them in
    argv order, so the ordering that 3.3 made load-bearing survives into the
    process listing where anyone can check it, and no temporary file has to
    outlive the ``execvp`` that would otherwise leak it.
    """
    argv = [gdb, "-q"]
    for command in commands:
        argv += ["-ex", command]
    return argv


def exec_gdb(argv: Sequence[str]) -> int:
    """Replace this process with gdb.

    ``execvp``, not ``subprocess``: gdb must own stdin and the process group or
    ``^C`` interrupts this wrapper instead of the inferior. It also keeps the
    process count in a shared PID namespace honest — nothing here reaps, since
    pid 1 is the target application (see :func:`podbench.agent.reaper_status`).
    """
    os.execvp(argv[0], list(argv))


def resolve_target_pid(
    pid: int | None,
    container_id: str | None,
    *,
    proc: Path = DEFAULT_PROC,
) -> tuple[int | None, list[str]]:
    """Work out which pid to debug, and say how sure we are.

    With neither an explicit pid nor a container id we refuse to guess: "the
    target is PID 1" is wrong under ``shareProcessNamespace: true``, where PID 1
    is ``/pause`` (3.15).
    """
    if pid is not None:
        return pid, []
    cid = container_id or env_target_container_id()
    if cid is None:
        return None, [
            "no pid given and no PODBENCH_TARGET_CID: pass a pid explicitly — "
            "PID 1 is the pod's pause process, not the target"
        ]
    listing = scan_processes(cid, proc=proc)
    targets = listing.targets
    if not targets:
        return None, [f"no process found in a cgroup matching container id {cid}"]
    notes = [] if listing.warning is None else [listing.warning]
    if len(targets) > 1:
        notes.append(
            f"target container has {len(targets)} processes; debugging the "
            f"lowest pid ({targets[0].pid}, {targets[0].comm}). Run `pids` to "
            "choose another."
        )
    return targets[0].pid, notes


def attach_blocked_message(report: CapabilityReport, pid: int) -> str:
    """Say which mechanism denies attach, and offer the way that always works.

    Four subsystems refuse ``PTRACE_ATTACH`` with the same ``EPERM``, so a bare
    "attach failed" costs an afternoon. The probe has already named the
    mechanism; this only has to spend it, and to point at gdb-launch, which
    needs no capability at all (3.12).
    """
    lines = [
        f"dbg: cannot attach to pid {pid}: {report.blocker.value}",
        f"  {report.blocker.explanation}",
        f"  verdict: {report.verdict.summary}",
    ]
    if report.verdict is Verdict.READ_ONLY:
        lines.append(
            "  the target's rootfs, maps and environ are still readable, so "
            "`pids` and read-only inspection work."
        )
    lines.append(
        "  ptrace-free alternative: `dbg --launch ./yourprog [args]`. gdb "
        "forks the inferior itself, which needs no capability and is not "
        "subject to Yama."
    )
    lines.append(
        "  to keep attaching to this process, the target can opt in with one "
        "line: prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)."
    )
    return "\n".join(lines)


_HEADERS = ("PID", "UID", "TARGET", "CONTAINER", "COMM", "CMDLINE")


def _row(process: ProcInfo) -> tuple[str, ...]:
    return (
        str(process.pid),
        # `-`, never `str(None)` and never `0`: ProcInfo.uid is None when
        # /proc/<pid>/status was unreadable, and the whole point of that None
        # (D6) is that an unknown uid must not be shown as a known one.
        "-" if process.uid is None else str(process.uid),
        "yes" if process.is_target else "-",
        # Twelve hex digits is what every other container tool prints, and the
        # full 64 would push cmdline off the terminal.
        (process.container_id or "-")[:12],
        process.comm,
        process.cmdline,
    )


def format_process_table(listing: ProcessListing, *, targets_only: bool = False) -> str:
    """Render the listing as a column-aligned table."""
    processes = listing.targets if targets_only else listing.processes
    rows = [_HEADERS, *(_row(process) for process in processes)]
    widths = [max(len(row[column]) for row in rows) for column in range(len(_HEADERS))]
    return "\n".join(
        "  ".join(
            cell.ljust(width) for cell, width in zip(row, widths, strict=True)
        ).rstrip()
        for row in rows
    )


def pids_payload(
    listing: ProcessListing, *, targets_only: bool = False
) -> dict[str, object]:
    """The stable JSON shape. ``attribution`` and ``warning`` are part of it:
    a consumer that ignores them is reading a guess as if it were a fact."""
    processes = listing.targets if targets_only else listing.processes
    return {
        "attribution": listing.attribution.value,
        "warning": listing.warning,
        "processes": [
            {
                "pid": process.pid,
                "uid": process.uid,
                "comm": process.comm,
                "cmdline": process.cmdline,
                "container_id": process.container_id,
                "is_target": process.is_target,
            }
            for process in processes
        ],
    }


def _warn(message: str) -> None:
    """Warnings go to stderr so ``--json`` stays machine-readable."""
    print(f"warning: {message}", file=sys.stderr)


# -- CLI -------------------------------------------------------------------

_Command = Callable[..., None]
"""A typer command callback, built with its seams already closed over."""

_ContainerIdHelp = "target container id (default: $PODBENCH_TARGET_CID)"


def _split_launch(args: Sequence[str] | None) -> tuple[list[str], list[str]]:
    """Pull the program's own arguments out of argv before click sees them.

    ``--launch`` was an ``argparse.REMAINDER``, and the contract it bought is
    the documented one: ``dbg --launch ./prog --fast`` must hand ``--fast`` to
    the program, not to ``dbg``. Click has no equivalent — every option it knows
    is claimed wherever it appears — so the tail after ``--launch PROGRAM`` is
    removed here and handed back separately. Everything up to and including the
    program name is still parsed normally, which is what keeps ``--launch`` in
    the help with a metavar and an error message click writes itself.

    ``None`` is resolved to ``sys.argv`` here rather than left to click, so that
    a command line typed at a terminal and one passed in as a list are split the
    same way.

    Both spellings of the option have to be recognised. ``--launch=./prog``
    carries the program in the same token, so the tail starts one place earlier
    than in the separated form; missing that spelling sent ``--fast`` to click,
    which refused it as an unknown option.

    >>> _split_launch(["--dry-run", "--launch", "./prog", "--fast"])
    (['--dry-run', '--launch', './prog'], ['--fast'])
    >>> _split_launch(["--dry-run", "--launch=./prog", "--fast"])
    (['--dry-run', '--launch=./prog'], ['--fast'])
    >>> _split_launch(["603", "--dry-run"])
    (['603', '--dry-run'], [])
    """
    argv = list(sys.argv[1:] if args is None else args)
    for index, token in enumerate(argv):
        if token == "--launch":
            # The program name is the next token, and belongs to click so that
            # a missing one is its error message rather than ours.
            return argv[: index + 2], argv[index + 2 :]
        if token.startswith("--launch="):
            return argv[: index + 1], argv[index + 1 :]
    return argv, []


def _pids_command(*, proc: Path) -> _Command:
    def pids(
        container_id: Annotated[
            str | None,
            typer.Option("--container-id", metavar="ID", help=_ContainerIdHelp),
        ] = None,
        targets: Annotated[
            bool,
            typer.Option(
                "--targets", help="list only the target container's processes"
            ),
        ] = False,
        json_output: Annotated[
            bool,
            typer.Option(
                "--json", help="emit the stable JSON form instead of the table"
            ),
        ] = False,
    ) -> None:
        raise typer.Exit(
            _run_pids(
                container_id, targets_only=targets, json_output=json_output, proc=proc
            )
        )

    return pids


def _dbg_command(
    *,
    proc: Path,
    attacher: Attacher | None,
    runner: GdbRunner,
    program_args: Sequence[str],
) -> _Command:
    def dbg(
        pid: Annotated[
            int | None,
            typer.Argument(
                metavar="[PID]",
                help="pid to attach to; discovered from the container id if omitted",
            ),
        ] = None,
        container_id: Annotated[
            str | None,
            typer.Option(
                "--container-id",
                metavar="ID",
                help="target container id used to discover the pid "
                "(default: $PODBENCH_TARGET_CID)",
            ),
        ] = None,
        source_dir: Annotated[
            list[str] | None,
            typer.Option(
                "--source-dir",
                metavar="DIR",
                help="extra source directory, wired with gdb's `directory`. "
                "debuginfod serves symbols but no sources on Debian, so this is "
                "how source text outside the target's rootfs is found. Repeatable",
            ),
        ] = None,
        no_debuginfod: Annotated[
            bool,
            typer.Option(
                "--no-debuginfod",
                help="do not enable debuginfod (it needs ca-certificates and network)",
            ),
        ] = False,
        run_it: Annotated[
            bool,
            typer.Option("--run", help="with --launch, start the program immediately"),
        ] = False,
        dry_run: Annotated[
            bool,
            typer.Option(
                "--dry-run",
                "--print-commands",
                help="print the generated gdb commands and exit, without probing "
                "or starting gdb",
            ),
        ] = False,
        launch: Annotated[
            str | None,
            typer.Option(
                "--launch",
                metavar="PROGRAM",
                help="debug a program gdb starts itself instead of attaching. "
                "Needs no capability. Consumes the rest of the command line, so "
                "put other flags first",
            ),
        ] = None,
    ) -> None:
        raise typer.Exit(
            _run_dbg(
                pid,
                container_id,
                source_dirs=source_dir or (),
                debuginfod=not no_debuginfod,
                run_it=run_it,
                dry_run=dry_run,
                launch=launch,
                program_args=program_args,
                proc=proc,
                attacher=attacher,
                runner=runner,
            )
        )

    return dbg


def _run_pids(
    container_id: str | None,
    *,
    targets_only: bool,
    json_output: bool,
    proc: Path,
) -> int:
    listing = scan_processes(container_id or env_target_container_id(), proc=proc)
    if json_output:
        print(json.dumps(pids_payload(listing, targets_only=targets_only), indent=2))
    else:
        if listing.warning is not None:
            _warn(listing.warning)
        print(format_process_table(listing, targets_only=targets_only))
    return 0


def _run_dbg(
    pid: int | None,
    container_id: str | None,
    *,
    source_dirs: Sequence[str],
    debuginfod: bool,
    run_it: bool,
    dry_run: bool,
    launch: str | None,
    program_args: Sequence[str],
    proc: Path,
    attacher: Attacher | None,
    runner: GdbRunner,
) -> int:
    if launch is not None:
        commands = launch_commands(
            launch,
            list(program_args),
            source_dirs=list(source_dirs),
            debuginfod=debuginfod,
            run=run_it,
        )
        if dry_run:
            print(command_file_text(commands), end="")
            return 0
        return runner(gdb_argv(commands))

    pid, notes = resolve_target_pid(pid, container_id, proc=proc)
    for note in notes:
        _warn(note)
    if pid is None:
        return EXIT_USAGE

    exe = read_exe(pid, proc=proc)
    if exe is None:
        _warn(
            f"could not read /proc/{pid}/exe (it needs PTRACE_MODE_READ): gdb "
            "gets no `file` command, so frames in the main executable may come "
            "back as ?? ()"
        )
    commands = attach_commands(
        pid, exe=exe, source_dirs=list(source_dirs), debuginfod=debuginfod
    )
    if dry_run:
        # Deliberately before the probe: generating the documented command file
        # must work on a laptop, with no target and no ptrace anywhere.
        print(command_file_text(commands), end="")
        return 0

    report = probe(pid, proc=proc, attacher=attacher)
    if report.verdict is not Verdict.LIVE_ATTACH:
        print(attach_blocked_message(report, pid), file=sys.stderr)
        # capreport's exit codes, so a script can branch on the same numbers.
        return report.verdict.value
    return runner(gdb_argv(commands))


def pids_main(args: Sequence[str] | None = None, *, proc: Path = DEFAULT_PROC) -> int:
    """Entry point for the image's ``pids`` command."""
    app = new_app()
    app.command(help=_PIDS_DESCRIPTION)(_pids_command(proc=proc))
    return run(app, args, prog="pids")


def dbg_main(
    args: Sequence[str] | None = None,
    *,
    proc: Path = DEFAULT_PROC,
    attacher: Attacher | None = None,
    runner: GdbRunner = exec_gdb,
) -> int:
    """Entry point for the image's ``dbg`` command.

    ``proc``, ``attacher`` and ``runner`` are test seams; the CLI passes none
    of them.
    """
    argv, program_args = _split_launch(args)
    app = new_app()
    app.command(help=_DBG_DESCRIPTION)(
        _dbg_command(
            proc=proc, attacher=attacher, runner=runner, program_args=program_args
        )
    )
    return run(app, argv, prog="dbg")


def main(
    args: Sequence[str] | None = None,
    *,
    proc: Path = DEFAULT_PROC,
    attacher: Attacher | None = None,
    runner: GdbRunner = exec_gdb,
) -> int:
    """``podbench pids`` / ``podbench dbg`` — the same two commands, one prog.

    The image installs them under their short names as well, which is what the
    walkthrough uses; :func:`pids_main` and :func:`dbg_main` are those entry
    points, so ``pids --help`` and ``dbg --help`` are self-describing.
    """
    argv, program_args = _split_launch(args)
    app = new_app()

    @app.callback(invoke_without_command=True)
    def root(ctx: typer.Context) -> None:
        """In-pod debugging helpers."""
        require_subcommand(ctx)

    app.command(name="pids", help=_PIDS_DESCRIPTION)(_pids_command(proc=proc))
    app.command(name="dbg", help=_DBG_DESCRIPTION)(
        _dbg_command(
            proc=proc, attacher=attacher, runner=runner, program_args=program_args
        )
    )
    return run(app, argv, prog="podbench")
