"""``pids`` and ``dbg`` — find the workload's processes, then debug them correctly.

Both helpers live inside the debug container, on the far side of the ssh
transport, and both exist because the obvious commands are wrong here.

``pids`` is not ``ps``: under ``shareProcessNamespace: true`` every process in
the pod is visible, including other podbench sessions', so the listing has to
say which container owns each one. Attribution keys off the target's runtime id
(report 4.3/3.15); when the launcher did not inject one the fallback marks
*every* other session's processes as target, so that answer is labelled a guess
rather than presented as fact.

Which of the two namespaces a seat is in is not a detail: without that flag a
seat joins the *target container's* namespace through ``targetContainerName``
and PID 1 is the target's own entrypoint rather than the pod's ``pause``
process — the common case in the field, and the one podbench used to describe
backwards (finding 17.2). :func:`_no_container_id_note` states whichever is
true here, from :func:`podbench.proc.shares_pod_pid_namespace`.

``dbg`` is not ``gdb -p``. Section 4.3 fixes seven commands in one order, and
the order is a correctness property rather than a preference: ``set sysroot``
after ``attach`` yields a backtrace that looks plausible and is wrong
(``clock_nanosleep`` reported as ``wcsxfrm_l``, user-code line numbers off by
six). The only way to make that impossible is to generate the whole command
sequence in one place, which is what :func:`attach_commands` is. Nothing here
runs gdb interactively to feed it commands — the sequence is handed over at
exec time, so there is no window in which a half-configured gdb is attached.

The one command in that sequence whose argument is not a sysroot path is
``file``, and :mod:`podbench.execfile` is why: gdb canonicalises the exec file's
name, ``/proc/<pid>/root`` canonicalises to ``/``, and the seat then reads its
own binary of the same name (issue #90).

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
from rich.text import Text

from .cli import new_app, require_subcommand, run
from .console import Column, Row, emit, paragraph, table
from .elf import read_elf
from .execfile import gdb_exec_file
from .model import (
    TARGET_CID_ENV,
    CapabilityReport,
    ProcInfo,
    Verdict,
    and_list,
    describe_gated_fallback,
)
from .probe import Attacher, probe
from .proc import (
    DEFAULT_PROC,
    DELETED_SUFFIX,
    NAMED_IN_NOTE,
    ProcessListing,
    candidate_note,
    debug_candidates,
    env_pod_containers,
    env_target_container,
    env_target_container_id,
    is_shell,
    read_exe,
    scan_processes,
    shares_pod_pid_namespace,
    strip_deleted,
    sysroot_path,
)

__all__ = [
    "DELETED_SUFFIX",
    "EXIT_USAGE",
    "MAX_OFFERED_PIDS",
    "RUST_PRETTY_PRINTERS",
    "SIGURG_COMMAND",
    "GdbRunner",
    "attach_blocked_message",
    "attach_commands",
    "command_file_text",
    "default_pid",
    "exec_gdb",
    "format_process_table",
    "listing_heading",
    "gdb_argv",
    "launch_commands",
    "main",
    "picked_note",
    "pids_payload",
    "read_exe",
    "resolve_target_pid",
    "resolve_target_pids",
    "startup_commands",
    "strip_deleted",
    "sysroot_path",
]

GdbRunner = Callable[[Sequence[str]], int]
"""How ``dbg`` finally starts gdb. A seam so tests never exec a real gdb."""

SIGURG_COMMAND = "handle SIGURG nostop noprint pass"
"""Issued for *every* target, not only for Go ones — and it pins a default.

Measured, not assumed, on the gdb a reader in a seat actually has: the image's
Debian 13.1-3, with no startup commands and no attach, already answers ``info
handle SIGURG`` with ``SIGURG No No Yes``, these three words. So this line is
insurance rather than a repair, and the claim it used to carry — that without
it an attached Go target is a wall of ``Program received signal SIGURG`` — was
describing work it does not do. (The reading is of ``/usr/bin/gdb``: ``gdb`` on
the image's PATH is the ``gdb-podbench`` wrapper, which issues this very
command, so repeating it with a plain ``gdb`` measures the wrapper.)

SIGCHLD and SIGWINCH read identically, so ``No No Yes`` is gdb's default class
for a routine signal rather than anything gdb decided about SIGURG — which is
what makes this line insurance against an unusual configuration and not a bet
against the next release. What it insures against is a gdb configured
otherwise: a system ``gdbinit``, a distribution patch, a build with different
defaults. Go's runtime preempts goroutines by
sending SIGURG at up to a few hundred a second on a busy process, and a gdb
that stops and announces each one turns an attached session into that wall and
nothing else — not slow, unusable, and nothing in it says why. One command is
cheap against a failure that reports itself as the program misbehaving.

Unconditional because the sequence has no language to switch on: ``podbench
dbg`` and ``gdb-podbench`` attach to whatever pid they are handed, and a config
that only helped when a *previous* step had identified the binary as Go would
be missing on exactly the degraded rung where the ELF could not be read.

``pass`` rather than ``ignore``: the signal is still delivered to the inferior,
because it is how the Go runtime works and swallowing it changes the program's
scheduling. Elsewhere SIGURG means out-of-band socket data, which is delivered
and rarely watched, so the same three words are right there too.
"""

RUST_PRETTY_PRINTERS = "/opt/podbench/gdb/rust_printers.py"
"""Where the image keeps the Rust printers, sourced only for a Rust target.

Rust binaries name ``gdb_load_rust_pretty_printers.py`` in
``.debug_gdb_scripts``, which resolves against a rustup toolchain that a
production container does not have and this seat does not ship. Without them
``Vec``, ``String`` and ``Option`` print as their raw ``RawVecInner`` fields —
readable, wrong-looking, and no use for deciding anything.

Sourced conditionally rather than always, because ``source`` of a path that is
not there is an error in the debug console on every unrelated attach.
"""

EXIT_USAGE = 2
"""Exit code for "there is nothing to debug", matching a usage error's own."""

MAX_OFFERED_PIDS = 5
"""How many candidates :func:`resolve_target_pids` will offer at once.

The offering used to be unbounded, which is fine on the four-process IOC pod it
was written for and useless on the opis pod: 112 processes, 111 of them
identical ``nginx`` workers, each contributing a full set of launch.json entries
to a dropdown that is scrolled rather than read.

Five, because a ranked list nobody scrolls past the top of is worth exactly as
much as its first few entries, and what the cap leaves out is *named* in a note
rather than silently gone.

It also bounds what ``debug-config`` does to the workload. That verb runs one
``PTRACE_SEIZE`` probe *per candidate*, so the cap is what turns an unbounded
number of probes on a 112-process pod into five. The *ranking* itself issues no
ptrace at all — see :func:`podbench.proc.ptrace_readable`.
"""

_PIDS_DESCRIPTION = (
    "List the processes in the target container's PID namespace, and say which "
    "container owns each one."
)
_DBG_DESCRIPTION = (
    "Run gdb against a process in another container of this pod, with the "
    "sysroot, source path and auto-load path set in the order that produces a "
    "correct backtrace."
)


def startup_commands(
    pid: int,
    *,
    exe: str | None = None,
    exec_file: str | None = None,
    source_dirs: Sequence[str] = (),
    debuginfod: bool | None = True,
    rust: bool = False,
) -> list[str]:
    """:func:`attach_commands` without the attach — for a caller doing its own.

    ``gdb --pid <n>`` attaches during *startup*, so a third-party caller that
    supplies its own ``--pid`` still needs every line below it, and needs them
    as ``-iex``. That caller is ``image/bin/gdb-podbench``, which is ``gdb`` on
    the seat's PATH and therefore stands behind cpptools, debugpy's
    attach-to-pid and anyone who types ``gdb -p``. It had two of these lines
    hand-copied into ``sh`` and was missing the other four, which is why they
    are generated here instead: a wrapper that quietly lacks
    ``add-auto-load-safe-path`` loses ``libthread_db`` and every thread-aware
    command with it, and says nothing (finding 17.6).

    >>> for command in startup_commands(597, exe="/app/victim", debuginfod=None):
    ...     print(command)
    set pagination off
    handle SIGURG nostop noprint pass
    set sysroot /proc/597/root
    directory /proc/597/root
    add-auto-load-safe-path /proc/597/root
    file /proc/597/root/app/victim

    ``debuginfod=None`` emits no ``set debuginfod`` line at all, which is the
    wrapper's choice and not an oversight: enabling it would have gdb fetch
    symbols with the workload *stopped* — a pause podbench's own verbs choose
    to spend and a pause a third party has not agreed to.

    What each line is for, and why the order is a correctness property rather
    than a preference, is in :func:`attach_commands`.
    """
    root = sysroot_path(pid)
    commands = [
        "set pagination off",
        SIGURG_COMMAND,
        f"set sysroot {root}",
        f"directory {root}",
    ]
    # gdb searches the *most recently added* directory first, so caller-supplied
    # sources deliberately follow the target's rootfs and win over it.
    commands += [f"directory {directory}" for directory in source_dirs]
    commands.append(f"add-auto-load-safe-path {root}")
    if debuginfod is not None:
        commands.append(f"set debuginfod enabled {'on' if debuginfod else 'off'}")
    if rust:
        commands.append(f"source {RUST_PRETTY_PRINTERS}")
    if exec_file is not None:
        commands.append(f"file {exec_file}")
    elif exe is not None:
        # String concatenation, never a path join: the exe link is absolute, and
        # joining would discard the sysroot and silently read our own binary.
        commands.append(f"file {root}{strip_deleted(exe)}")
    return commands


def attach_commands(
    pid: int,
    *,
    exe: str | None = None,
    exec_file: str | None = None,
    source_dirs: Sequence[str] = (),
    debuginfod: bool = True,
    rust: bool = False,
) -> list[str]:
    """The gdb command sequence from report 4.3, in the one order that works.

    >>> for command in attach_commands(597, exe="/app/victim (deleted)"):
    ...     print(command)
    set pagination off
    handle SIGURG nostop noprint pass
    set sysroot /proc/597/root
    directory /proc/597/root
    add-auto-load-safe-path /proc/597/root
    set debuginfod enabled on
    file /proc/597/root/app/victim
    attach 597

    ``exec_file`` overrides the path the ``file`` command is given, and is how
    issue #90 is kept out of the sequence: where the seat has a file of its own
    at the target's ``exe`` path, gdb canonicalises the sysroot away and reads
    ours instead, so :func:`podbench.execfile.gdb_exec_file` hands a copy in.

    >>> attach_commands(597, exe="/app/victim", exec_file="/tmp/x/victim")[-2:]
    ['file /tmp/x/victim', 'attach 597']

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
    * ``set debuginfod`` before both — the executable's symbols are fetched on
      ``file``, but a library's only after ``attach``, which is with the
      workload *stopped*. gdb waits ``DEBUGINFOD_TIMEOUT`` per library and its
      own default for that is 90 seconds, so the seat bounds it two ways:
      :data:`podbench.agent.DEBUGINFOD_TIMEOUT_SECONDS` in the environment, and
      a start-up probe that drops ``DEBUGINFOD_URLS`` from an ssh session
      whose seat cannot reach the server at all. ``--no-debuginfod`` is the
      per-run switch, for a server that is reachable and slow.
    * ``handle SIGURG`` first — see :data:`SIGURG_COMMAND`. It has to precede
      the attach, because the flood starts the moment the process resumes.

    ``rust`` sources :data:`RUST_PRETTY_PRINTERS`, and is the one thing in this
    sequence that turns on the target's *language* rather than on the two mount
    namespaces:

    >>> attach_commands(597, exe="/app/victim", rust=True)[-3:-2]
    ['source /opt/podbench/gdb/rust_printers.py']

    Everything but the last line comes from :func:`startup_commands`, so the
    sequence has exactly one author and a caller that attaches for itself
    cannot drift away from it.
    """
    return [
        *startup_commands(
            pid,
            exe=exe,
            exec_file=exec_file,
            source_dirs=source_dirs,
            debuginfod=debuginfod,
            rust=rust,
        ),
        f"attach {pid}",
    ]


def launch_commands(
    program: str,
    args: Sequence[str] = (),
    *,
    source_dirs: Sequence[str] = (),
    debuginfod: bool = True,
    run: bool = False,
    rust: bool = False,
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
    handle SIGURG nostop noprint pass
    set debuginfod enabled on
    file ./victim
    set args --fast
    run
    """
    commands = ["set pagination off", SIGURG_COMMAND]
    commands += [f"directory {directory}" for directory in source_dirs]
    commands.append(f"set debuginfod enabled {'on' if debuginfod else 'off'}")
    if rust:
        commands.append(f"source {RUST_PRETTY_PRINTERS}")
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
    """The one pid to debug, and how sure we are.

    For ``podbench dbg``, which runs a single gdb: the best candidate only. Use
    :func:`resolve_target_pids` where every candidate can be offered at once.
    """
    pids, notes = resolve_target_pids(pid, container_id, proc=proc)
    return (pids[0] if pids else None), notes


def resolve_target_pids(
    pid: int | None,
    container_id: str | None,
    *,
    proc: Path = DEFAULT_PROC,
) -> tuple[list[int], list[str]]:
    """The processes in the target container worth offering, best first.

    With neither an explicit pid nor a container id we refuse to guess, and
    :func:`_no_container_id_note` says why in the terms of the namespace this
    seat is actually in (3.15, finding 17.2). An explicit pid is taken as given
    and is the only answer — the caller named a process, so offering it three
    others is not helpfulness.

    Otherwise at most :data:`MAX_OFFERED_PIDS`, and never a dead one. This is
    where :func:`podbench.proc.debug_candidates`'s "sort, never exclude"
    discipline is spent: a report has to name something whatever the container
    looks like, but every pid returned here becomes a *selectable entry* in a
    dropdown, and the two are not the same obligation.
    """
    if pid is not None:
        return [pid], []
    cid = container_id or env_target_container_id()
    if cid is None:
        return [], [_no_container_id_note(shares_pod_pid_namespace(proc=proc))]
    listing = scan_processes(cid, proc=proc)
    targets = listing.targets
    if not targets:
        return [], [f"no process found in a cgroup matching container id {cid}"]
    notes = [] if listing.warning is None else [listing.warning]
    candidates = debug_candidates(targets)
    note = candidate_note(candidates, "debugging")
    if note is not None:
        notes.append(note)
    # A zombie is dropped outright and a shell is not. The asymmetry is the
    # point: a shell is a legitimate last resort - a `podbench dev` seat has
    # nothing else - while a dead process cannot be attached by any seat with
    # any capability, so an entry for one is an entry that can only fail.
    alive = [info for info in candidates if info.alive]
    if not alive:
        return [], [
            *notes,
            "every process in the target container is dead (a zombie awaiting a "
            "reaper): nothing here can be attached by any seat. Run `podbench "
            "pids` to see the container's state",
        ]
    # Shells are a *fallback* target, not an extra one. Where something else
    # ran they are dropped rather than offered, because "attach to bash" in the
    # dropdown beside "attach to ioc" is the original bug with one more step:
    # the entry is selectable, it attaches, and it shows nothing.
    ranked = [info for info in alive if not is_shell(info)] or alive
    offered = ranked[:MAX_OFFERED_PIDS]
    if len(ranked) > MAX_OFFERED_PIDS:
        notes.append(_dropped_note(ranked[MAX_OFFERED_PIDS:]))
    return [info.pid for info in offered], notes


def _no_container_id_note(shared: bool | None) -> str:
    """Why to pass a pid, said for the PID namespace this seat is in.

    podbench used to state "PID 1 is the pod's pause process, not the target"
    unconditionally. That is true only where the pod sets
    ``shareProcessNamespace: true``; a seat carrying ``targetContainerName``
    on a pod that does not joins the *target container's* namespace, where PID
    1 is the workload itself. 15 of 15 pods surveyed on the p47 beamline are in
    that second mode, so the unconditional sentence was false on every pod it
    was read on (finding 17.2), and it was false in the direction that sends a
    reader away from the right answer.

    >>> "PID 1 is the pod's pause process" in _no_container_id_note(True)
    True
    >>> "PID 1 is the target container's entrypoint" in _no_container_id_note(False)
    True
    >>> "not reliably the target" in _no_container_id_note(None)
    True
    """
    if shared is True:
        fact = (
            "this pod shares its process namespace, so PID 1 is the pod's "
            "pause process, not the target"
        )
    elif shared is False:
        fact = (
            "this pod does not share its process namespace, so PID 1 is the "
            "target container's entrypoint, which is the target unless "
            "something wraps it"
        )
    else:
        fact = (
            "PID 1 is not reliably the target, and /proc/1/comm could not be "
            "read to say which"
        )
    return (
        f"no pid given and no {TARGET_CID_ENV}: pass a pid explicitly — "
        f"{fact}. `podbench pids` lists them"
    )


def _dropped_note(dropped: Sequence[ProcInfo]) -> str:
    """Name what the cap left out, so the cap is not a silent decision."""
    named = ", ".join(f"{info.pid} ({info.comm})" for info in dropped[:NAMED_IN_NOTE])
    extra = len(dropped) - NAMED_IN_NOTE
    tail = named if extra <= 0 else f"{named} and {extra} more"
    return (
        f"offering the best {MAX_OFFERED_PIDS} of "
        f"{MAX_OFFERED_PIDS + len(dropped)} candidates; not offered: {tail}. "
        "Run `podbench pids` for the full list and pass one as the pid argument"
    )


def attach_blocked_message(report: CapabilityReport, pid: int) -> str:
    """Say which mechanism denies attach, and offer the way that always works.

    Four subsystems refuse ``PTRACE_ATTACH`` with the same ``EPERM``, so a bare
    "attach failed" costs an afternoon. The probe has already named the
    mechanism; this only has to spend it, and to point at gdb-launch, which
    needs no capability at all (3.12).
    """
    lines = [
        f"podbench dbg: cannot attach to pid {pid}: {report.blocker.value}",
        f"  {report.blocker.explanation}",
        f"  verdict: {report.verdict.summary}",
    ]
    if report.verdict is Verdict.READ_ONLY:
        lines.append(
            "  the target's rootfs, maps and environ are still readable, so "
            "`podbench pids` and read-only inspection work."
        )
    elif report.proc_reads:
        # Keyed on the matrix, not on the verdict: saying only "attach is
        # denied" leaves the reader to try the sysroot next and lose the
        # afternoon to a second denial (issue #51). The matrix goes in the line
        # rather than a word for it, because "/proc is closed" is untrue of the
        # world-readable half — and a matrix that lost only one gated path
        # still fails the all-or-nothing rule, so the fallback clause is built
        # from which gated paths survived rather than from the verdict.
        lines.append(
            f"  the reads that take PTRACE_MODE_READ went with it "
            f"({report.reads_summary}), "
            f"{describe_gated_fallback(report.proc_reads)}."
        )
    lines.append(_launch_alternative(report))
    lines.append(
        "  to keep attaching to this process, the target can opt in with one "
        "line: prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)."
    )
    return "\n".join(lines)


def _launch_alternative(report: CapabilityReport) -> str:
    """Offer ``dbg --launch`` only where the scratch attach earned the offer.

    This line used to be unconditional, and it is the last thing a denied user
    reads — so on a seat whose own forked child refused to be traced it sent
    them to a second identical denial. gdb traces an inferior it forked, which
    is precisely the attach the probe measured (report 3.12).
    """
    if report.can_debug_launched:
        return (
            "  ptrace-free alternative: `podbench dbg --launch ./yourprog "
            "[args]`. gdb forks the inferior itself, which needs no "
            "capability and is not subject to Yama below ptrace_scope=2."
        )
    if report.child_attach_ok is False:
        return (
            "  and `podbench dbg --launch` is not the way out either: this "
            "seat could not ptrace a child it forked itself, so gdb cannot "
            "trace an inferior it starts. Run `podbench capreport` for the "
            "mechanism."
        )
    return (
        "  `podbench dbg --launch ./yourprog [args]` is the usual way out - "
        "gdb forks the inferior itself, which needs no capability - but the "
        "scratch attach was skipped here, so this is not measured."
    )


PICKED = ">"
"""What marks the row :func:`resolve_target_pids` would choose on its own.

The listing exists because a note told the reader to "run `podbench pids` to
choose another", and it showed every input to that choice while never showing
its *output* — so the one question a reader actually arrives with, which pid do
I type, was the one thing the table did not answer. The mark is a column rather
than a colour because it has to survive being pasted into an issue, which is
where most of these listings end up.
"""

_COLUMNS = (
    Column(""),
    Column("PID"),
    Column("UID"),
    Column("TARGET", {"yes": "green"}),
    # `Z` is the disqualifier: a zombie is a process no seat can attach to,
    # whatever else its row says. `D` is uninterruptible sleep, where an attach
    # blocks rather than refuses, so it is a caution and not a denial.
    Column("ST", {"Z": "yellow", "D": "yellow"}),
    Column("THR"),
    Column("PTRACE", {"ok": "green", "DENIED": "red", "?": "yellow"}),
    Column("CONTAINER"),
    Column("COMM"),
    Column("CMDLINE", fill=True),
)
"""``ST``, ``THR`` and ``PTRACE`` are the three signals the ranking added.

The note this listing answers is "run `podbench pids` to choose another", so it
has to show what the ranking ranked on — otherwise the reader who disagrees with
the choice is picking from raw pid order with no more information than the pid.

``CMDLINE`` fills and is cut to what is left, which is the fix for the thing
that actually made this listing unreadable: an argv is unbounded, so on a real
workload it overran and the *terminal* wrapped it, putting the next row's PID
under this row's PTRACE and destroying the alignment of every row below. One
cut cell costs the tail of one argv; not cutting it costs the whole table.
``--json`` carries the untruncated form.
"""

_TARGET_ROW = None
"""Style for a row in the target container: the ordinary one.

Named beside :data:`_OTHER_ROW` so the pair reads as the decision it is. The
seat's own processes and the pod's other containers are *also true* and are not
what anybody ran this for, so they recede — but ``TARGET`` stays a column, and
that is deliberate: dimming is a tty affordance and colour is never allowed to
be the only place a fact is stated, since this output is read as often from a
pasted log as from a terminal.
"""

_OTHER_ROW = "dim"
"""Style for a row outside the target container. See :data:`_TARGET_ROW`."""


def _cells(process: ProcInfo, *, picked: int | None) -> list[str]:
    return [
        PICKED if process.pid == picked else "",
        str(process.pid),
        # `-`, never `str(None)` and never `0`: ProcInfo.uid is None when
        # /proc/<pid>/status was unreadable, and the whole point of that None
        # (D6) is that an unknown uid must not be shown as a known one.
        "-" if process.uid is None else str(process.uid),
        "yes" if process.is_target else "-",
        process.state or "?",
        "-" if process.threads is None else str(process.threads),
        # Same rule as uid: unmeasured is `?` and not `no`, since "this seat
        # cannot read that process" is the sentence a reader will act on.
        {True: "ok", False: "DENIED", None: "?"}[process.ptrace_readable],
        # Twelve hex digits is what every other container tool prints, and the
        # full 64 would push cmdline off the terminal.
        (process.container_id or "-")[:12],
        process.comm,
        process.cmdline,
    ]


def default_pid(listing: ProcessListing) -> int | None:
    """The pid ``podbench dbg`` attaches to when given none, or ``None``.

    The same two filters :func:`resolve_target_pids` applies, and deliberately
    *not* a second ranking: this marks a row with the answer another function
    computes, so the two agreeing is the whole value of the mark. If they ever
    disagree the table is worse than no table, because it points at a pid the
    verb it names will not use.
    """
    alive = [info for info in debug_candidates(listing.targets) if info.alive]
    ranked = [info for info in alive if not is_shell(info)] or alive
    return ranked[0].pid if ranked else None


def format_process_table(
    listing: ProcessListing, *, targets_only: bool = False
) -> list[Text]:
    """Render the listing as a column-aligned table, one styled line per row."""
    processes = listing.targets if targets_only else listing.processes
    picked = default_pid(listing)
    return table(
        _COLUMNS,
        [
            Row(
                _cells(process, picked=picked),
                style=_TARGET_ROW if process.is_target else _OTHER_ROW,
            )
            for process in processes
        ],
    )


def picked_note(listing: ProcessListing, *, targets_only: bool = False) -> str | None:
    """The line under the table that says what :data:`PICKED` means.

    ``None`` where nothing was marked, rather than a sentence explaining the
    absence of a mark: a container whose every process is dead already gets a
    note saying exactly that from :func:`resolve_target_pids`, and this one
    would be the second telling.
    """
    picked = default_pid(listing)
    if picked is None:
        return None
    shown = listing.targets if targets_only else listing.processes
    return (
        f"{PICKED} is the process `podbench dbg` attaches to with no `--pid`. "
        f"{len(shown)} shown, {len(listing.targets)} in the target container."
    )


def listing_heading(target: str | None, containers: Sequence[str] = ()) -> list[str]:
    """The lines above the table: whose PID namespace this listing is of.

    ``pids`` used to offer "the pod's processes" while showing one container's
    namespace. On ``p47-proxy``, three containers deep, that is a third of them,
    and the reader concludes the pod holds three socats and nothing else
    (finding 15). So the container is named, and so are the ones the seat is not
    in - with the flag that lands a seat in each, because a name on its own
    leaves the reader to guess how to get there.

    Both facts are the launcher's to supply
    (:data:`~podbench.model.TARGET_NAME_ENV`,
    :data:`~podbench.model.POD_CONTAINERS_ENV`) and a seat landed by an older
    one has neither. Missing, it says nothing at all rather than guessing: the
    old listing with no heading is honest, and "this pod has one container" is
    a claim this must never make from an absent variable.

    Neither line says what is *not* below, which would be a claim about the
    other containers' namespaces this seat cannot check: under
    ``shareProcessNamespace: true`` the pod's containers share one namespace and
    their processes are all here.

    >>> for line in listing_heading("ca-gw", ["ca-gw", "pva-gw"]):
    ...     print(line)
    container ca-gw: the processes in its PID namespace
    this pod also has pva-gw - re-attach with `--target pva-gw` for a seat in that one
    >>> listing_heading(None, ["ca-gw"])
    []
    """
    if target is None:
        return []
    lines = [f"container {target}: the processes in its PID namespace"]
    others = [name for name in containers if name != target]
    if others:
        flags = " or ".join(f"`--target {name}`" for name in others)
        one = "that one" if len(others) == 1 else "one of those"
        lines.append(
            f"this pod also has {and_list(others)} - re-attach with {flags} "
            f"for a seat in {one}"
        )
    return lines


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
                # Added after the shape was published, so a consumer must treat
                # a missing key as "this seat is older than the question" and
                # never as `null` meaning "measured, and the answer is no" -
                # which is also what a present `null` means here.
                "ppid": process.ppid,
                "state": process.state,
                "threads": process.threads,
                "ptrace_readable": process.ptrace_readable,
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
                help="do not enable debuginfod (it needs ca-certificates and "
                "network). Library symbols are fetched after the attach, with "
                "the target stopped, so this is the flag to reach for when the "
                "pause is what costs",
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
        print_exec_file: Annotated[
            bool,
            typer.Option(
                "--print-exec-file",
                help="print the one path to give gdb's `file` command and exit. "
                "It is the target's own path under the sysroot unless this "
                "container has a file of its own at that path, in which case gdb "
                "would read ours (issue #90) and a copy is staged instead. "
                "`--print-startup-commands` carries it as one line of the whole "
                "sequence, which is what `gdb-podbench` asks for",
            ),
        ] = False,
        print_startup_commands: Annotated[
            bool,
            typer.Option(
                "--print-startup-commands",
                help="print the gdb commands a caller doing its own attach must "
                "pass as `-iex`, one per line, and exit. Every line of `--dry-run` "
                "except the `attach` itself. What `gdb-podbench` calls, so that a "
                "third-party `gdb --pid` gets the same sysroot, exec file, "
                "auto-load path and SIGURG handling that `podbench dbg` does",
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
                print_exec_file=print_exec_file,
                print_startup_commands=print_startup_commands,
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
        # Above the table rather than under it, and on stdout with it: this is
        # the sentence the table is an answer to, and a reader who stops after
        # the first screen is exactly the one it is for.
        for line in listing_heading(env_target_container(), env_pod_containers()):
            emit(line)
        emit(format_process_table(listing, targets_only=targets_only))
        # Under the table, because it is a legend for a column in it: read
        # before the rows it explains, `>` is a mark with nothing yet marked.
        note = picked_note(listing, targets_only=targets_only)
        if note is not None:
            emit("\n".join(paragraph(note)))
    return 0


def _is_rust(path: str | None) -> bool:
    """Whether the file gdb will really read was built by rustc.

    Asked of the exact path handed to ``file`` — which is the staged copy
    wherever issue #90 forced one — and never of the target's own spelling: that
    path names *this* container's binary from in here, and answering from it is
    report §3.3's substitution in a new place.
    """
    info = read_elf(path) if path else None
    return info is not None and info.is_rust


def _run_dbg(
    pid: int | None,
    container_id: str | None,
    *,
    source_dirs: Sequence[str],
    debuginfod: bool,
    run_it: bool,
    dry_run: bool,
    print_exec_file: bool,
    print_startup_commands: bool,
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
            rust=_is_rust(launch),
        )
        if dry_run or print_startup_commands:
            # A launched program has no attach for the startup commands to
            # precede, so its startup sequence *is* the whole sequence. Answered
            # rather than ignored, for the reason --print-exec-file is answered
            # here: a flag that asks for text must not start a debugger.
            print(command_file_text(commands), end="")
            return 0
        if print_exec_file:
            # A launched program runs in *this* container, so there is no
            # sysroot to canonicalise away and nothing to stage. Answered
            # rather than ignored: the alternative is a flag that asks for a
            # path and starts a debugger instead.
            print(launch)
            return 0
        return runner(gdb_argv(commands))

    pid, notes = resolve_target_pid(pid, container_id, proc=proc)
    for note in notes:
        _warn(note)
    if pid is None:
        return EXIT_USAGE

    exe = read_exe(pid, proc=proc)
    file_path: str | None = None
    if exe is None:
        _warn(
            f"could not read /proc/{pid}/exe (it needs PTRACE_MODE_READ): gdb "
            "gets no `file` command, so frames in the main executable may come "
            "back as ?? ()"
        )
    else:
        # Resolved before `--dry-run` returns, and with the same side effect,
        # because the point of printing the sequence is that it can be pasted:
        # a dry run that showed `file /proc/<pid>/root<exe>` would document the
        # one path issue #90 says gdb misreads. On a laptop there is no
        # /proc/<pid> at all, `read_exe` has already answered None, and nothing
        # here runs.
        file_path, staged_notes = gdb_exec_file(pid, exe, proc=proc)
        for note in staged_notes:
            _warn(note)
    if print_exec_file:
        # gdb-podbench's answer: one path on stdout for the caller to feed to
        # `file`, or nothing and a usage exit when the exe could not be read.
        if file_path is None:
            return EXIT_USAGE
        print(file_path)
        return 0
    if print_startup_commands:
        # gdb-podbench's other answer, and the reason the sequence is generated
        # in one place: the wrapper supplies its own --pid, so it needs every
        # line of the attach sequence except the attach. Printed even where the
        # exe could not be read - the sysroot alone is still the difference
        # between this container's libraries and the target's (3.3).
        print(
            command_file_text(
                startup_commands(
                    pid,
                    exe=exe,
                    exec_file=file_path,
                    # No `set debuginfod` line at all: see startup_commands.
                    debuginfod=None,
                    rust=_is_rust(file_path),
                )
            ),
            end="",
        )
        return 0
    commands = attach_commands(
        pid,
        exe=exe,
        exec_file=file_path,
        source_dirs=list(source_dirs),
        debuginfod=debuginfod,
        rust=_is_rust(file_path),
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


def main(
    args: Sequence[str] | None = None,
    *,
    proc: Path = DEFAULT_PROC,
    attacher: Attacher | None = None,
    runner: GdbRunner = exec_gdb,
) -> int:
    """``podbench pids`` / ``podbench dbg`` — the same two commands, one prog.

    ``proc``, ``attacher`` and ``runner`` are test seams; the CLI passes none of
    them.
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
