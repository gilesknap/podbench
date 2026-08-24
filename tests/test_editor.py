"""Tests for the editor half of ``podbench vscode``.

Nothing here starts an editor or touches a cluster. :class:`FakeSeat` is both
the ``kubectl`` and the ``code`` on the far end of the injected runner: it keeps
a dictionary of files so a write is visible to the next read, which is what lets
the merge behaviour be asserted the way a second run would meet it.

Two orderings are asserted rather than implied, because both fail *silently*
when broken: the excludes must be on disk before the window that starts the
walk, and an extension must install with ``--remote``, since a locally installed
one runs the debug adapter on the laptop where no ``/proc/<pid>/root`` path
means anything.

Since #230 the third thing asserted here is a *negative*: this module writes
nothing into the folder it opens and provisions nothing into the target. Both
are silent when broken too — a stray ``launch.json`` is a line in somebody's git
diff on an NFS PVC, and a stray ``--provision`` is 15 MB and a ptrace nobody
asked for — so the fake keeps its file store and the tests read it for
emptiness rather than for content.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from podbench.editor import (
    CONNECTION_HINT,
    OK,
    PROVISION_DEST_FLAG,
    SERVER_CLI_ATTEMPTS,
    SERVER_CLI_INTERVAL,
    UNREACHABLE_CAUSES,
    EditorError,
    is_step,
    open_seat,
    resolve_editor,
)
from podbench.kubectl import CommandResult, Kubectl
from podbench.model import ContainerRef, PodRef
from podbench.vscode import INTERPRETER_NOTE, PYTHON_INTERPRETER_KEY

PROVISION_FLAG = "--provision"
"""Spelled out rather than imported, because nothing on this side of the wire
knows it any more (#230): the tests below assert that podbench never sends it,
and a constant imported from the module under test could not say that."""

SEAT = ContainerRef(PodRef("demo", "api-7f9"), "podbench-1")
ALIAS = "podbench-demo-api-7f9"
HOME = "/root"
CLAIM = "/podbench/app"
CLAIM_INTERPRETER = f"{CLAIM}/.venv/bin/python3"
"""The live p47 target's own, measured 2026-08-24: on a hotfixed pod this
resolves to the same file in the seat and in the application, which is the
property that makes it worth writing anywhere."""

MACHINE = "~/.vscode-server/data/Machine/settings.json"
"""Where the excludes live now, under the *ssh login's* home rather than in the
folder — so the fake stores them under the `~` spelling and a test that finds
them there has also proved they did not go over ``kubectl exec``."""

DEBUGPY_CONFIG: dict[str, Any] = {
    "name": "podbench: attach to app.py (debugpy)",
    "type": "debugpy",
    "request": "attach",
}
SERVER_CLI = (
    "/root/.vscode-server/cli/servers/Stable-6928394f91b684055b873eecb8bc281365131f1c"
    "/server/bin/code-server"
)
"""The seat-side CLI of the server the window is attached to, as measured."""

CPPDBG_CONFIG: dict[str, Any] = {
    "name": "podbench: attach to victim (gdb)",
    "type": "cppdbg",
    "request": "attach",
}


class FakeSeat:
    """One runner for both ends: ``kubectl exec`` into a dict, and ``code``."""

    def __init__(
        self,
        *,
        configurations: Sequence[dict[str, Any]] = (DEBUGPY_CONFIG,),
        debug_config_rc: int = 0,
        debug_config_stderr: str = "",
        files: dict[str, str] | None = None,
        unreadable: Sequence[str] = (),
        unwritable: Sequence[str] = (),
        install_rc: int = 0,
        install_lands: bool = True,
        open_rc: int = 0,
        ssh_rc: int = 0,
        ssh_stderr: str = "",
        list_rc: int = 0,
        server_cli: str | None = SERVER_CLI,
        seat_install_rc: int = 0,
    ) -> None:
        self.configurations = list(configurations)
        self.debug_config_rc = debug_config_rc
        self.debug_config_stderr = debug_config_stderr
        self.provisioned = False
        self.files = dict(files or {})
        self.unreadable = set(unreadable)
        self.unwritable = set(unwritable)
        self.install_rc = install_rc
        # `code --install-extension --remote` exits 0 without a connection, so
        # "the command succeeded" and "the seat has it" are independent facts
        # and the fake has to be able to hold them apart.
        self.install_lands = install_lands
        self.unpacked: set[str] = set()
        self.open_rc = open_rc
        self.ssh_rc = ssh_rc
        self.ssh_stderr = ssh_stderr
        self.list_rc = list_rc
        # `None` is a window that never connected, so no vscode-server was ever
        # bootstrapped and there is nothing to install through.
        self.server_cli = server_cli
        self.seat_install_rc = seat_install_rc
        self.seat_installed: list[str] = []
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(argv))
        if argv[0] == "code":
            return self._editor(list(argv))
        if argv[0] == "ssh":
            return self._ssh(list(argv), stdin)
        command = list(argv)[list(argv).index("--") + 1 :]
        return self._in_seat(command, stdin)

    # -- the seat ----------------------------------------------------------

    def _in_seat(self, command: list[str], stdin: str | None) -> CommandResult:
        # The two-token verb, because that is the only spelling the image
        # resolves: matching a bare `debug-config` here would keep this fake
        # green against a launcher that execs nothing in a real seat.
        if command[:2] == ["podbench", "debug-config"]:
            # Recorded, never honoured: `open_seat` must not send this, and a
            # fake that quietly served it would let the regression through.
            self.provisioned = self.provisioned or PROVISION_FLAG in command
            if self.debug_config_rc != 0:
                return _result(self.debug_config_rc, stderr=self.debug_config_stderr)
            document = {"version": "0.2.0", "configurations": self.configurations}
            # stderr on a *successful* run too: debug-config narrates the
            # assessment there, and relaying it is how the reader learns that
            # the debug step they are about to be offered will need
            # `--provision`.
            return CommandResult((), 0, json.dumps(document), self.debug_config_stderr)
        if command[:2] == ["sh", "-c"] and command[2].startswith("mkdir -p"):
            assert stdin is not None, "a write must carry its content on stdin"
            path = _written_path(command[2])
            if path in self.unwritable:
                return _result(1, stderr=f"sh: cannot create {path}: Permission denied")
            self.files[path] = stdin
            return _result(0)
        if command[:2] == ["sh", "-c"]:
            path = _read_path(command[2])
            if path in self.unreadable:
                return _result(1, stderr=f"cat: {path}: Permission denied")
            text = self.files.get(path)
            # 3 is the script's own "there is no such file", which is the one
            # answer a bare `cat` cannot distinguish from "could not read it".
            return _result(0, stdout=text) if text is not None else _result(3)
        raise AssertionError(f"unexpected exec: {command}")

    # -- the laptop --------------------------------------------------------

    def _ssh(self, argv: list[str], stdin: str | None = None) -> CommandResult:
        if argv[-1] == ALIAS and stdin is not None:
            # Resolving the seat's own vscode-server: the script goes on stdin,
            # so the alias is the last word rather than the second to last.
            if self.server_cli is None:
                return _result(1)
            return _result(0, stdout=f"{self.server_cli}\n")
        assert argv[-2] == ALIAS, (
            "the preflight has to use the alias VS Code will, through the same "
            f"config: {argv}"
        )
        if argv[-1].startswith(("mkdir -p ~/", "test -e ~/")):
            # The machine settings, which travel over ssh rather than over
            # kubectl exec because `~` has to be the *login's* home. Same two
            # scripts as a seat file, so the fake reuses the same store - under
            # the `~` spelling, which is what proves they went the ssh way.
            return self._in_seat(["sh", "-c", argv[-1]], stdin)
        if self.server_cli is not None and argv[-1].startswith(self.server_cli):
            extension = argv[-1].rsplit(" ", 1)[-1]
            if self.seat_install_rc == 0:
                self.seat_installed.append(extension)
                self.unpacked.add(extension.lower())
            return _result(self.seat_install_rc, stderr="no such extension")
        if argv[-1].startswith("ls -1"):
            # The seat's extensions directory, named as vscode-server names it:
            # the id, a version and sometimes a platform triple. Its own return
            # code, because the preflight has already passed by the time this
            # runs — a listing can fail on a connection the probe proved.
            return _result(
                self.list_rc,
                stdout="".join(f"{name}-1.0.0\n" for name in sorted(self.unpacked)),
                stderr=self.ssh_stderr,
            )
        return _result(self.ssh_rc, stderr=self.ssh_stderr)

    def _editor(self, argv: list[str]) -> CommandResult:
        assert argv[1:3] == ["--remote", f"ssh-remote+{ALIAS}"], (
            "every code invocation must name the remote, or the extension "
            "installs on the laptop and the adapter runs there too"
        )
        if "--install-extension" in argv:
            if self.install_rc == 0 and self.install_lands:
                self.unpacked.add(argv[-1].lower())
            return _result(self.install_rc, stderr="no Remote-SSH here")
        return _result(self.open_rc, stderr="cannot resolve host")

    # -- assertions --------------------------------------------------------

    @property
    def installed(self) -> list[str]:
        return [call[-1] for call in self.calls if "--install-extension" in call]

    def index_of_write(self, path: str) -> int:
        for index, call in enumerate(self.calls):
            # `mkdir -p`, so a *read* of the same path a moment earlier is not
            # mistaken for the write whose ordering is being asserted.
            if call[-1].startswith("mkdir -p") and path in call[-1]:
                return index
        raise AssertionError(f"nothing wrote {path}: {self.calls}")

    @property
    def index_of_open(self) -> int:
        for index, call in enumerate(self.calls):
            if call[0] == "code" and "--install-extension" not in call:
                return index
        raise AssertionError(f"nothing opened a folder: {self.calls}")


def _seat_calls(seat: FakeSeat) -> int:
    """How many calls have reached the pod - the ssh preflight reaches nothing."""
    return len([call for call in seat.calls if call[0] not in ("ssh", "code")])


def _result(returncode: int, *, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult((), returncode, stdout, stderr if returncode else "")


def _written_path(script: str) -> str:
    """The path a ``mkdir -p … && cat > …`` script writes to."""
    return script.rsplit("> ", 1)[1].strip("'")


def _read_path(script: str) -> str:
    """The path a ``test -e … || exit 3; cat …`` script reads."""
    return script.rsplit("cat ", 1)[1].strip("'")


def run_open(
    seat: FakeSeat,
    *,
    folder: str = HOME,
    provision_dest: str | None = None,
    naps: list[float] | None = None,
) -> list[str]:
    """Every note, in the order the user saw it — ``open_seat`` reports as it
    goes rather than returning a list at the end, because the install is a
    download and a progress report that arrives afterwards is not one."""
    notes: list[str] = []
    open_seat(
        Kubectl("demo", runner=seat),
        SEAT,
        alias=ALIAS,
        folder=folder,
        report=notes.append,
        editor="code",
        provision_dest=provision_dest,
        runner=seat,
        # Never the real one: waiting for a vscode-server that a fake will never
        # bootstrap is five minutes of a unit suite that is meant to take
        # seconds. `naps` is how a test that cares about the wait sees it.
        sleep=(naps.append if naps is not None else lambda _: None),
    )
    return notes


# -- the preflight -----------------------------------------------------------

DLS_REFUSAL = (
    "/tmp/podbench-home/.podbench/sshd_config: No such file or directory\n"
    "command terminated with exit code 1\n"
    "kex_exchange_identification: Connection closed by remote host\n"
    "Connection closed by UNKNOWN port 65535\n"
)
"""What a seat with no ssh transport says, measured at DLS on 2026-08-16.

Four lines, and the *first* is the one that names the mechanism — which is why
the failure is quoted whole rather than through ``_detail``, whose last line
here is ssh giving up on a port number that means nothing.
"""


def test_an_unreachable_seat_is_reported_rather_than_opened() -> None:
    """The whole point of the preflight.

    ``code --remote`` returns as soon as a window has the argv, so an alias
    that does not work is discovered in the GUI, minutes and one vscode-server
    download later, in a log the user has to know to open. Nothing here is
    written, installed or launched until ssh has answered.
    """
    seat = FakeSeat(ssh_rc=255, ssh_stderr=DLS_REFUSAL)

    with pytest.raises(EditorError) as raised:
        run_open(seat)

    message = str(raised.value)
    assert f"`ssh {ALIAS}` does not reach the seat" in message
    # Quoted whole: the mechanism is on the first line and ssh's own summary,
    # on the last, names a port that does not exist.
    assert "sshd_config: No such file or directory" in message
    assert "kex_exchange_identification" in message
    assert "`--new` lands a fresh seat" in message, (
        "a named way out, not just a diagnosis"
    )
    assert [call[0] for call in seat.calls] == ["ssh"], (
        "nothing may run after the probe fails - the extension install reports "
        "success without a connection, and the window would too"
    )
    assert seat.files == {}


def test_the_ways_out_name_a_flag_and_never_another_verb() -> None:
    """``UNREACHABLE_CAUSES`` is reached only from ``check_reachable``.

    Which makes it ``vscode``'s block and nobody else's — ``attach`` never
    prints it. Two of its bullets used to hard-code ``podbench attach --new``,
    including the one that fires in the field, so a user who ran ``podbench
    vscode`` was told to run a different verb. ``--new`` is spelled the same on
    both, so the remedy is the flag alone.
    """
    assert "podbench attach" not in UNREACHABLE_CAUSES
    assert UNREACHABLE_CAUSES.count("`--new`") == 2, (
        "both seat-replacing bullets still have to name a way out"
    )


def test_the_alias_is_proven_before_the_first_thing_that_needs_it() -> None:
    seat = FakeSeat()

    notes = run_open(seat)

    assert seat.calls[0][0] == "ssh", f"the probe must come first: {seat.calls}"
    assert seat.calls[0][-1] == "true", "the probe has to run a command in the seat"
    assert any("ssh reaches the seat" in note for note in notes)


# -- the OOM guard -----------------------------------------------------------


def test_the_excludes_are_written_into_the_seats_machine_settings() -> None:
    """/proc/<pid>/root is a symlink into another container's root, so a walk
    from a folder that does not exclude it has no bottom — and an OOM-killed
    ephemeral container cannot be restarted.

    Machine scope and not the folder's own file (D1b): the folder is the user's
    committed checkout on a hotfixed pod, and an exclude list is not worth a
    permanent line in their git diff."""
    seat = FakeSeat()
    run_open(seat)

    settings = json.loads(seat.files[MACHINE])
    assert settings["files.watcherExclude"]["**/proc/**"] is True
    assert settings["search.exclude"]["**/sys/**"] is True
    assert "/proc/**" in settings["python.analysis.exclude"]
    # cpptools' tag parser walks on its own account, and cpptools is what
    # `--open` installs for a C/C++ target.
    assert settings["C_Cpp.files.exclude"]["**/proc/**"] is True


def test_the_interpreter_the_seat_measured_answers_the_python_popup() -> None:
    """Issue #219: the window pops "no Python interpreter found" on a pod where
    debug attach then works perfectly, and podbench measured the answer while it
    was emitting the debugpy configuration. It travels on the seat's stderr,
    because `--print-config`'s stdout has to stay a launch document."""
    seat = FakeSeat(
        debug_config_stderr=f"debug-config: {INTERPRETER_NOTE}{CLAIM_INTERPRETER}"
    )
    notes = run_open(seat, folder=CLAIM)

    settings = json.loads(seat.files[MACHINE])
    assert settings[PYTHON_INTERPRETER_KEY] == CLAIM_INTERPRETER
    assert any(PYTHON_INTERPRETER_KEY in note for note in notes)


def test_an_interpreter_outside_the_folder_that_opens_is_not_written() -> None:
    """The gate, and it is about mounts rather than about Python: on a pod with
    no hotfix layout the target's interpreter is in another mount namespace, and
    this seat's file at that path is a different one. Naming it is a confident
    wrong answer where the popup was an annoying right one."""
    seat = FakeSeat(
        debug_config_stderr=f"debug-config: {INTERPRETER_NOTE}/app/.venv/bin/python3"
    )
    notes = run_open(seat)

    assert PYTHON_INTERPRETER_KEY not in json.loads(seat.files[MACHINE])
    assert not any(PYTHON_INTERPRETER_KEY in note for note in notes)


def test_the_excludes_land_before_the_window_does() -> None:
    """The watcher starts walking the moment the folder opens, so configuring
    afterwards is a race whose loser is a seat that cannot be restarted."""
    seat = FakeSeat()
    run_open(seat)

    assert seat.index_of_write(MACHINE) < seat.index_of_open


def test_the_folder_opened_is_the_seats_home_and_never_the_root() -> None:
    seat = FakeSeat()
    notes = run_open(seat)

    assert seat.calls[seat.index_of_open] == (
        "code",
        "--remote",
        f"ssh-remote+{ALIAS}",
        HOME,
    )
    assert any(f"open {HOME} over Remote-SSH" in note for note in notes)


def test_settings_a_user_wrote_are_not_clobbered() -> None:
    seat = FakeSeat(files={MACHINE: json.dumps({"editor.tabSize": 2})})
    run_open(seat)

    settings = json.loads(seat.files[MACHINE])
    assert settings["editor.tabSize"] == 2
    assert settings["search.exclude"]["**/proc/**"] is True


def test_a_settings_file_that_will_not_parse_is_left_alone() -> None:
    """A file that is not JSONC either. Rewriting would drop what this parser
    cannot see, so the file stands and the note says so."""
    seat = FakeSeat(files={MACHINE: "{ mine }"})
    notes = run_open(seat)

    assert seat.files[MACHINE] == "{ mine }"
    assert any("left exactly as it is" in note for note in notes)


def test_a_commented_settings_file_is_merged_into_rather_than_refused() -> None:
    """The 2026-08-24 measurement, on the file that is left. A hand-edited VS
    Code settings file is JSONC — comments and a trailing comma — and a parser
    that refuses one leaves the seat with whatever `podbench agent` managed at
    start-up, which on a re-created ~/.vscode-server is nothing at all."""
    seat = FakeSeat(files={MACHINE: '{\n  // ours\n  "editor.tabSize": 2,\n}\n'})
    notes = run_open(seat)

    assert not any("left exactly as it is" in note for note in notes)
    assert "// ours" in seat.files[MACHINE]
    assert "**/proc/**" in seat.files[MACHINE]


# -- nothing in the folder ---------------------------------------------------


def test_no_launch_json_is_written_where_one_used_to_be() -> None:
    """Slice 1 of #230, at the level the write happened.

    Every configuration podbench can author is pid-named and pid-keyed, and a
    restart changes the pid — measured across restarts on the p47 replica,
    `fastcs-example` was pid 12, then 2446, then 13. So a launch.json written at
    window-open is stale almost immediately, on the workflow that is now the
    common one. Writing nothing cannot go stale.
    """
    seat = FakeSeat()
    notes = run_open(seat)

    assert f"{HOME}/.vscode/launch.json" not in seat.files
    assert not any("launch.json" in note for note in notes)


def test_the_folder_that_opens_is_left_exactly_as_it_was() -> None:
    """The falsification the plan named: after a run against a hotfixed pod,
    `git status` on the user's checkout shows nothing podbench authored. The
    folder there is a committed checkout on an NFS PVC, so anything written is a
    permanent line in somebody's diff."""
    seat = FakeSeat()
    run_open(seat, folder=CLAIM)

    assert [name for name in seat.files if name.startswith(f"{CLAIM}/")] == []
    assert set(seat.files) == {MACHINE}


def test_the_assessment_still_says_which_extensions_the_seat_needs() -> None:
    """What the run keeps, and why it is not a debugger feature.

    `code --install-extension --remote` answers from the *laptop's* install
    list, so the seat's own extensions are unknowable from here; only
    `debug-config` can see the target, and its answer is what says whether this
    is a Python seat or a C++ one. Nothing about asking mutates anything —
    `--print-config` neither writes nor probes.
    """
    seat = FakeSeat()
    run_open(seat)

    assert seat.installed == ["ms-python.python", "ms-python.debugpy"]
    assert seat.files == {MACHINE: seat.files[MACHINE]}


def test_a_target_no_debugger_fits_still_gets_the_guard_and_the_folder() -> None:
    """debug-config already named every mechanism that said no on its stderr.
    The excludes and the folder are the rest of the seat, and they are the half
    that keeps it alive."""
    seat = FakeSeat(
        debug_config_rc=2, debug_config_stderr="no debugger flavour could be emitted"
    )
    notes = run_open(seat)

    assert MACHINE in seat.files
    assert seat.installed == []
    assert any("no debug extension to install" in note for note in notes)


# -- what the seat said ------------------------------------------------------

_REFUSAL = (
    "debug-config: python target, observe mode, x86_64\n"
    "debug-config: debugpy unavailable: debugpy is not importable by the target\n"
    "debug-config:   install it into the target from this seat, which is what\n"
    "  `podbench debug-config --provision` runs\n"
    "debug-config: no debugger flavour could be emitted for this target - every "
    "mechanism that said no is named above\n"
)
"""A live refusal, trimmed. The shape that matters is that the diagnosis is in
the *middle*: the last line points at the ones above it."""

_SUCCESS = (
    "debug-config: python target, observe mode, x86_64\n"
    "debug-config: nothing is listening on 127.0.0.1:5678 yet, so the "
    "configuration above connects to a closed port. `podbench debug-config "
    "--provision` starts the server itself; by hand it is the command below:\n"
    "PYTHONPATH=/proc/1/root/opt/podbench-debugpy \\\n"
    "  /app/.venv/bin/python -m debugpy --listen 127.0.0.1:5678 --pid 1\n"
)
"""A live *successful* run, trimmed. It names the flag, which is the whole
point: the prerequisites are met and there is still nothing to connect to."""


def test_the_whole_refusal_is_relayed_not_just_its_last_line() -> None:
    """ "every mechanism that said no is named above" is the last line, so a
    caller that keeps only the last line points at output it just discarded —
    which is exactly how a missing launch.json read as a podbench bug."""
    seat = FakeSeat(debug_config_rc=2, debug_config_stderr=_REFUSAL)
    notes = run_open(seat)

    assert any("debugpy is not importable by the target" in note for note in notes)


def test_the_injection_command_survives_a_successful_run() -> None:
    """A run that fits a debugger says so, and says what still has to happen
    before F5 connects. Nothing here writes or provisions, so that narration is
    the entire reason the reader knows the offered step needs `--provision`."""
    seat = FakeSeat(debug_config_stderr=_SUCCESS)
    notes = run_open(seat)

    assert not seat.files.keys() - {MACHINE}
    assert any("nothing is listening on 127.0.0.1:5678" in note for note in notes)
    # One note per line: the launcher's report formatter re-wraps on whitespace,
    # so the continuation backslash only survives at the end of its own note.
    assert any(note.endswith("\\") for note in notes)


def test_a_seat_that_never_ran_the_verb_still_says_what_happened() -> None:
    """127 from a `podbench` the image does not resolve carries sh's message and
    no narration at all, and that message is the whole diagnosis."""
    seat = FakeSeat(debug_config_rc=127, debug_config_stderr="")
    notes = run_open(seat)

    assert any(
        "no debug extension to install: it said nothing" in note for note in notes
    )


# -- nothing in the target ---------------------------------------------------


def test_nothing_is_provisioned_however_loudly_the_seat_asks() -> None:
    """Slice 1 of #230, at the level the mutation happened.

    The seat names `--provision` on both of debugpy's blockers, and this side of
    the wire used to answer it: an editor was asked for, and a ~15 MB install
    into the workload's writable layer plus a ptrace of a probed application is
    the debugger's bill. It is now paid by the step the report offers, by
    somebody who asked for a debugger.
    """
    for stderr in (_REFUSAL, _SUCCESS):
        seat = FakeSeat(debug_config_stderr=stderr, debug_config_rc=0)
        run_open(seat)

        assert not seat.provisioned
        assert not any(PROVISION_FLAG in call for call in seat.calls)


def test_the_seats_own_account_of_the_blocker_still_reaches_the_reader() -> None:
    """Nothing here answers the refusal any more, so relaying it is the whole
    of what this run can do about it — and it is enough, because the step the
    report offers carries the flag the seat just named."""
    seat = FakeSeat(debug_config_rc=2, debug_config_stderr=_REFUSAL)
    notes = run_open(seat)

    assert any("debugpy is not importable by the target" in note for note in notes)
    assert not seat.provisioned


def test_there_is_no_second_run_to_answer_anything_with() -> None:
    """The retry is gone with the consent that drove it. One assessment, so the
    extensions installed cannot come from two measurements of one target."""
    seat = FakeSeat(debug_config_stderr=_SUCCESS)
    run_open(seat)

    assert len([call for call in seat.calls if "debug-config" in call]) == 1


def test_a_chosen_destination_reaches_the_assessment() -> None:
    """Only the launcher can see the pod, so only it can know this seat's own
    default is a directory the seat cannot write (issue #189's pod: `/opt` is
    root-owned and the degraded rung is uid 37887).

    Nothing here installs to it. It is passed because it is also the extra path
    `debug-config` searches for the target's own copy — so a window opened after
    the user ran the debug step finds what that step installed, and asks for the
    Python extensions on the strength of it.
    """
    seat = FakeSeat(debug_config_stderr=_SUCCESS)
    dest = "/podbench/app/.podbench-debugpy"
    run_open(seat, provision_dest=dest)

    runs = [call for call in seat.calls if "debug-config" in call]
    assert len(runs) == 1
    assert runs[0][runs[0].index(PROVISION_DEST_FLAG) + 1] == dest
    assert PROVISION_FLAG not in runs[0]


def test_no_destination_leaves_the_argv_as_it_has_always_been() -> None:
    """`None` is "the seat's own default", and it is spelled as an absent flag
    rather than as the constant: a seat landed by a launcher that predates
    `--provision-dest` would refuse the whole run, and podbench meets one only
    on a pod where it had nothing to override anyway."""
    seat = FakeSeat(debug_config_stderr=_SUCCESS)
    run_open(seat)

    assert not any(PROVISION_DEST_FLAG in call for call in seat.calls)


# -- extensions --------------------------------------------------------------


def test_only_the_flavours_own_extensions_are_installed_and_remotely() -> None:
    """In Observe mode these land on the workload's ephemeral-storage budget,
    so a bundle is somebody else's disk. And ``--remote`` is what
    makes it the "Install in SSH:" button rather than a local install whose
    adapter runs on the laptop."""
    seat = FakeSeat()
    run_open(seat)

    assert seat.installed == ["ms-python.python", "ms-python.debugpy"]
    assert "ms-vscode.cpptools" not in seat.installed


def test_a_c_target_asks_for_cpptools_alone() -> None:
    seat = FakeSeat(configurations=[CPPDBG_CONFIG])
    run_open(seat)

    assert seat.installed == ["ms-vscode.cpptools"]


def test_installing_an_extension_writes_nothing_into_the_folder() -> None:
    """The extension install is the one thing left that touches the seat on
    behalf of a debugger, and it lands in ``~/.vscode-server`` — never in the
    folder. D1b moved ``settings.json`` and ``extensions.json`` out; #230 took
    ``launch.json``, which was the last of them."""
    seat = FakeSeat()
    run_open(seat, folder=CLAIM)

    assert seat.installed == ["ms-python.python", "ms-python.debugpy"]
    assert [name for name in seat.files if name.startswith(f"{CLAIM}/")] == []


def test_an_install_that_fails_is_reported_and_the_folder_still_opens() -> None:
    seat = FakeSeat(install_rc=1)
    notes = run_open(seat)

    assert any("could not install ms-python.python" in note for note in notes)
    assert any("open /root over Remote-SSH" in note for note in notes)


def test_the_install_is_announced_before_it_runs() -> None:
    """It bootstraps vscode-server in the seat - a 214 MiB download with egress
    (report 3.8), and uv-style silence without it. The output is captured for
    the failure message, so nothing else would appear while it happened."""
    seat = FakeSeat()
    # Each note against the number of commands that had run when it was said.
    timeline: list[tuple[str, int]] = []
    open_seat(
        Kubectl("demo", runner=seat),
        SEAT,
        alias=ALIAS,
        folder=HOME,
        report=lambda note: timeline.append((note, len(seat.calls))),
        editor="code",
        runner=seat,
    )
    announced = next(
        calls for note, calls in timeline if "installing ms-python" in note
    )
    assert not any("--install-extension" in call for call in seat.calls[:announced])


def test_a_second_open_says_the_window_has_to_be_reloaded() -> None:
    """An extension host that is already running does not pick up an extension
    unpacked into ~/.vscode-server underneath it. Proved in the seat: the host
    started at 16:53 and ms-python.debugpy landed at 17:33, installed and not
    running - so the adapter was missing and nothing said why."""
    seat = FakeSeat()
    notes = run_open(seat)

    assert any("Developer: Reload Window" in note for note in notes)


def test_nothing_is_said_about_reloading_when_nothing_was_installed() -> None:
    seat = FakeSeat(debug_config_rc=2, debug_config_stderr="nothing fits")
    notes = run_open(seat)

    assert not any("Reload Window" in note for note in notes)


def test_no_reload_note_when_every_install_failed() -> None:
    """The note asserts that --install-extension unpacked something into the
    seat. A run whose installs all failed unpacked nothing, so it would send the
    reader to look for an extension the lines above just said is not there."""
    seat = FakeSeat(install_rc=1)
    notes = run_open(seat)

    assert any("could not install ms-python.python" in note for note in notes)
    assert not any("Reload Window" in note for note in notes)


def test_the_open_step_does_not_claim_the_window_connected() -> None:
    """The desktop `code` hands the argv to a window and returns, so its exit
    code is not evidence: the authority is resolved in the window afterwards
    and a failure arrives there as a dialog and here as a zero.

    What is left to warn about is now one thing rather than a list, because the
    preflight has already proven every cause that lives outside VS Code - and
    it is said under `next` rather than here, because it is a thing to do if
    the window fails and not a step this run took. `open_seat` therefore ends
    on what it did, and claims nothing about what happened afterwards.
    """
    seat = FakeSeat()
    notes = run_open(seat)

    assert notes[-1].endswith("asked VS Code to open /root over Remote-SSH")
    assert not any("could not establish connection" in note for note in notes)

    assert "ms-vscode-remote.remote-ssh" in CONNECTION_HINT
    assert "podbench doctor --fix" not in CONNECTION_HINT, (
        "the Include line cannot be the cause: ssh read it a moment ago"
    )


def test_an_extension_is_claimed_only_once_the_seat_has_it() -> None:
    """ "Unpacked", and asserted against the seat's own listing.

    `code --install-extension` exits 0 for "already installed" *and* for "never
    reached the remote", so its exit code cannot tell anyone anything about the
    seat. The directory can.
    """
    seat = FakeSeat()
    notes = run_open(seat)

    # One line for all of them, so the claim is per-extension inside it.
    (unpacked,) = [note for note in notes if "unpacked in the seat" in note]
    assert "ms-python.python" in unpacked
    assert any(
        "ls -1 ~/.vscode-server/extensions" in call[-1]
        for call in seat.calls
        if call[0] == "ssh"
    )


def test_an_install_that_exits_0_without_reaching_the_seat_is_not_believed() -> None:
    """The DLS run of 2026-08-16: three extensions "installed", none present.

    The remedy has to name the *local* install trap, because that is where the
    Extensions view sends someone who does not know to look for "in SSH:" on the
    button - and a locally-installed cpptools runs the adapter on the laptop,
    where `/proc/<pid>/root/...` does not exist.
    """
    seat = FakeSeat(install_lands=False, server_cli=None)
    notes = run_open(seat)

    assert any("did not land in the seat" in note for note in notes)
    assert any("Install in SSH" in note for note in notes)
    assert not any("is unpacked in SSH" in note for note in notes)
    assert not any("Reload Window" in note for note in notes)


def test_a_local_short_circuit_is_answered_by_installing_through_the_seat() -> None:
    """`code --remote <authority> --install-extension` answers from the
    *developer's own* install list: an extension held locally is reported
    "already installed", exit 0, and the seat is never contacted - with or
    without `--force` (measured at DLS 2026-08-21, the seat held no matching
    path anywhere on its filesystem). So every first-time `podbench vscode`
    handed over a seat with no debug adapter, and it failed worst for the people
    most likely to be here: anyone who debugs Python has the Python extension on
    their laptop already.

    The install is made where it has to land instead, by the server's own CLI.
    """
    seat = FakeSeat(install_lands=False)
    notes = run_open(seat)

    assert seat.seat_installed == ["ms-python.python", "ms-python.debugpy"]
    assert any("through the seat's own vscode-server" in note for note in notes)
    (unpacked,) = [note for note in notes if "unpacked in the seat" in note]
    assert "ms-python.debugpy" in unpacked
    # It landed, so the remedy that sends somebody to do it by hand must not.
    assert not any("did not land in the seat" in note for note in notes)


def test_installing_through_the_seat_asks_for_a_reload() -> None:
    """The seat-side install is a *separate process* writing into the extensions
    directory, so the window notices a change rather than performing an install
    - and a `debuggers` contribution is registered when the extension host
    starts, which already happened.

    VS Code says which of the two it was, in its own log (measured on a Diamond
    seat, 2026-08-21):

        Extension installed successfully: <id>        # the window did it
        Extensions added from another source <id>     # the window noticed it

    The second is this path, always, because the seat's vscode-server does not
    exist until a window has bootstrapped it - so the install cannot precede the
    window that needs it. Without the note F5 fails with `could not find a debug
    adapter descriptor` and nothing has explained why.
    """
    seat = FakeSeat(install_lands=False)
    notes = run_open(seat)

    assert any("through the seat's own vscode-server" in note for note in notes)
    assert any("Reload Window" in note for note in notes)


def test_the_seat_install_waits_for_the_window_that_bootstraps_the_server() -> None:
    """The seat's vscode-server does not exist until a window has connected and
    bootstrapped it, so the fallback cannot run before the window is opened -
    and `settings.json` cannot move after it, because the watcher starts walking
    the moment it opens and that race ends in an unrecoverable seat."""
    seat = FakeSeat(install_lands=False)
    run_open(seat)

    settings = next(i for i, call in enumerate(seat.calls) if MACHINE in " ".join(call))
    opened = next(
        i
        for i, call in enumerate(seat.calls)
        if call[0] == "code" and "--install-extension" not in call
    )
    through_seat = next(
        i for i, call in enumerate(seat.calls) if call[-1].startswith(SERVER_CLI)
    )
    assert settings < opened < through_seat


def test_a_window_that_never_connects_leaves_nothing_to_install_through() -> None:
    """No vscode-server means no CLI to install by, and the window's own error is
    the diagnosis - so this says what is missing and does not invent a cause."""
    seat = FakeSeat(install_lands=False, server_cli=None)
    notes = run_open(seat)

    assert any("no vscode-server has started in the seat" in note for note in notes)
    assert seat.seat_installed == []


def test_the_wait_for_a_server_is_bounded_and_is_not_a_busy_loop() -> None:
    """A seat with no server yet is the normal case, not a failure: the
    bootstrap is the 1215 MiB download and this runs the moment the window was
    asked to open. But a wait with no bound is a launcher that never returns,
    and a wait with no pause is a seat asked sixty times in a millisecond."""
    naps: list[float] = []
    seat = FakeSeat(install_lands=False, server_cli=None)
    run_open(seat, naps=naps)

    assert len(naps) == SERVER_CLI_ATTEMPTS - 1, "one fewer pause than attempts"
    assert set(naps) == {SERVER_CLI_INTERVAL}


def test_a_seat_side_install_that_fails_is_reported_and_not_believed() -> None:
    """This CLI reports "already installed" too. The whole defect above was an
    exit code that meant nothing, so the seat is asked again either way."""
    seat = FakeSeat(install_lands=False, seat_install_rc=1)
    notes = run_open(seat)

    assert any(
        "could not install ms-python.python in the seat" in note for note in notes
    )
    assert any("did not land in the seat" in note for note in notes)


def test_an_unlistable_seat_is_unproven_rather_than_a_failure() -> None:
    """ssh worked minutes ago, so a failed listing is a transient.

    Reporting it as "not installed" would send the user to reinstall something
    that is already there; claiming success would be the bug this check exists
    to remove. Neither, then — and say which.
    """
    seat = FakeSeat(list_rc=255)
    notes = run_open(seat)

    assert any("unverified" in note for note in notes)


# -- the two refusals --------------------------------------------------------


def test_no_code_on_path_is_a_sentence_not_a_traceback() -> None:
    with pytest.raises(EditorError, match="VS Code CLI"):
        resolve_editor(lambda _: None)


def test_the_refusal_names_the_installs_that_cannot_follow_the_instruction() -> None:
    """A flatpak VS Code has no route out of its sandbox onto the host PATH, so
    'Shell Command: Install code command in PATH' is not a way out of this for
    the user who most often meets it; nor is it for a `codium` user."""
    with pytest.raises(EditorError, match="flatpak") as raised:
        resolve_editor(lambda _: None)
    assert "codium" in str(raised.value)


@pytest.mark.parametrize(
    "path",
    [
        "/home/dev/.vscode-server/bin/2c9b7f/bin/remote-cli/code",
        "/vscode/vscode-server/bin/linux-x64/2c9b7f/bin/remote-cli/code",
        "/home/dev/.vscode-server-insiders/bin/2c9b7f/bin/remote-cli/code",
    ],
)
def test_the_remote_cli_is_refused_rather_than_driven_at_the_wrong_machine(
    path: str,
) -> None:
    """Inside a Remote-SSH window, a devcontainer or a Codespace, `code` on PATH
    forwards to the window this terminal is already in. --install-extension
    would install into *that* machine, leaving the seat with .vscode files, no
    extensions and breakpoints that never bind."""
    with pytest.raises(EditorError, match="remote. CLI") as raised:
        resolve_editor(lambda _: path)
    assert "never bind" in str(raised.value)


def test_a_desktop_code_is_not_mistaken_for_the_remote_one() -> None:
    """A local window's integrated terminal has VSCODE_IPC_HOOK_CLI set too, and
    its `code` is the desktop CLI — which is why the path is what decides."""
    desktop = "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
    assert resolve_editor(lambda _: desktop) == desktop


def test_a_remote_that_cannot_be_reached_names_remote_ssh() -> None:
    """``--remote`` fails when the local VS Code has no Remote-SSH extension, or
    when ssh cannot resolve the alias — the Include line podbench has always
    asked for."""
    seat = FakeSeat(open_rc=1)
    with pytest.raises(EditorError, match="ms-vscode-remote.remote-ssh"):
        run_open(seat)


@pytest.mark.parametrize("folder", ["/", "//", "-w", "relative/path"])
def test_a_folder_that_could_end_the_seat_is_refused_not_assumed_away(
    folder: str,
) -> None:
    """The home follows a `podbench-home` mount, so it is not a constant, and
    `/` is the one value that cannot be undone: an OOM-killed ephemeral
    container cannot be restarted. A leading dash would be read by `code` as an
    option and open nothing, reported as success."""
    seat = FakeSeat()
    with pytest.raises(EditorError, match="as a folder"):
        run_open(seat, folder=folder)

    assert seat.calls == [], "nothing is written and nothing is opened"


def test_machine_settings_that_cannot_be_written_are_a_line_not_a_refusal() -> None:
    """The one file left, and it fails softly: `podbench agent` already wrote
    the excludes at seat start-up, so what this run adds is the interpreter and
    a repair. Neither is worth refusing to open an editor over — but the line
    says *unmeasured* rather than fine, because a file that could not be read
    might be one that no longer carries `**/proc/**`."""
    seat = FakeSeat(unwritable=[MACHINE])
    notes = run_open(seat)

    assert any("could not write" in note and MACHINE in note for note in notes)
    assert [call for call in seat.calls if call[0] == "code"] != []


def test_a_file_that_cannot_be_read_is_not_replaced_with_a_fresh_one() -> None:
    """`cat` exits non-zero for "not there" and for "could not read it" alike,
    and reading the second as the first turns the merge this promises into a
    replacement of whatever the seat was already carrying."""
    mine = '{"editor.tabSize": 2}'
    seat = FakeSeat(files={MACHINE: mine}, unreadable=[MACHINE])
    notes = run_open(seat)

    assert seat.files[MACHINE] == mine
    assert any("could not read" in note and MACHINE in note for note in notes)


def test_a_file_that_is_simply_absent_is_written_rather_than_refused() -> None:
    """The common case, and the one the exit code has to keep distinct: a first
    run meets a seat with no ~/.vscode-server at all."""
    seat = FakeSeat()
    run_open(seat)

    assert MACHINE in seat.files


def test_the_editor_is_found_on_path_and_named_by_its_absolute_route() -> None:
    assert (
        resolve_editor(lambda name: f"/usr/local/bin/{name}") == "/usr/local/bin/code"
    )


def test_the_only_file_written_anywhere_is_the_machine_settings() -> None:
    """Wherever the folder is. The excludes are the seat's own, under the ssh
    login's home, and #230 left them as the whole of what a run writes."""
    seat = FakeSeat()
    run_open(seat, folder="/home/podbench")

    assert set(seat.files) == {MACHINE}


def test_a_write_creates_the_directory_and_never_redirects_stderr() -> None:
    """Closing or replacing fd 2 in a ``kubectl exec``'d process tears down the
    CRI exec stream and truncates the write with a zero exit (report 3.1).

    Only the machine settings go this way now, and they go over ssh — so the
    scripts are read off the ssh calls rather than the exec ones.
    """
    seat = FakeSeat()
    run_open(seat)

    scripts = [call[-1] for call in seat.calls if call[0] == "ssh" and len(call) > 5]
    writes = [script for script in scripts if "cat > " in script]
    assert writes, scripts
    for script in writes:
        assert script.startswith("mkdir -p ")
        assert "2>" not in script and ">/dev/null" not in script


def test_nothing_here_shells_out_to_a_second_assessment() -> None:
    """One ``debug-config``, so the extensions installed and the configurations
    written cannot come from two measurements of the same target."""
    seat = FakeSeat()
    run_open(seat)

    assessments = [call for call in seat.calls if "debug-config" in call]
    assert len(assessments) == 1
    assert assessments[0][-1] == "--print-config"


def test_every_step_carries_a_tick_and_the_seats_own_words_do_not() -> None:
    """The two shapes `report` emits, and the reason they are laid out apart.

    A step is this module's own claim and wraps under its tick. Relayed stderr
    is somebody else's text: it is printed exactly as it arrived, because one
    of those lines ends in a continuation `\\` that means nothing once anything
    follows it, and because a `debug-config:` at the head of one is a label to
    `console`'s eye and is not one.
    """
    seat = FakeSeat(debug_config_rc=2, debug_config_stderr=_REFUSAL)
    notes = run_open(seat)

    ours = [note for note in notes if is_step(note)]
    relayed = [note for note in notes if not is_step(note)]
    assert ours and relayed
    # Verbatim, indent and all: the seat wraps its own narration, and this
    # line is the continuation of the one above it. Anything on this side that
    # reflowed or stripped it would run the two together.
    assert "  `podbench debug-config --provision` runs" in relayed
    for note in ours:
        # One line each. The mechanism lives in docs/how-to/vscode-remote-ssh.md,
        # because the reliably-skipped part of a report is the prose in it.
        assert "\n" not in note
        assert len(note.split(". ")) <= 2, note


def test_only_one_line_claims_to_have_written_anything() -> None:
    """There is one file left, so there is one such line. It used to be two, and
    before D1b it was four, each carrying the same directory."""
    notes = run_open(FakeSeat())

    wrote = [note for note in notes if note.startswith(f"{OK} wrote")]
    assert wrote == [f"{OK} wrote {MACHINE} in the seat: the folder-walk excludes"]
