"""Tests for ``attach --open``.

Nothing here starts an editor or touches a cluster. :class:`FakeSeat` is both
the ``kubectl`` and the ``code`` on the far end of the injected runner: it keeps
a dictionary of files so a write is visible to the next read, which is what lets
the merge behaviour be asserted the way a second ``--open`` would meet it.

Two orderings are asserted rather than implied, because both fail *silently*
when broken: the excludes must be on disk before the window that starts the
walk, and an extension must install with ``--remote``, since a locally installed
one runs the debug adapter on the laptop where no ``/proc/<pid>/root`` path
means anything.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from podbench.editor import PROVISION_FLAG, EditorError, open_seat, resolve_editor
from podbench.kubectl import CommandResult, Kubectl
from podbench.model import ContainerRef, PodRef

SEAT = ContainerRef(PodRef("demo", "api-7f9"), "podbench-1")
ALIAS = "podbench-demo-api-7f9"
HOME = "/root"

DEBUGPY_CONFIG: dict[str, Any] = {
    "name": "podbench: attach to app.py (debugpy)",
    "type": "debugpy",
    "request": "attach",
}
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
        provision_rc: int = 0,
        provisioned_configurations: Sequence[dict[str, Any]] = (DEBUGPY_CONFIG,),
        files: dict[str, str] | None = None,
        unreadable: Sequence[str] = (),
        unwritable: Sequence[str] = (),
        install_rc: int = 0,
        install_lands: bool = True,
        open_rc: int = 0,
        ssh_rc: int = 0,
        ssh_stderr: str = "",
        list_rc: int = 0,
    ) -> None:
        self.configurations = list(configurations)
        self.debug_config_rc = debug_config_rc
        self.debug_config_stderr = debug_config_stderr
        self.provision_rc = provision_rc
        self.provisioned_configurations = list(provisioned_configurations)
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
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
    ) -> CommandResult:
        self.calls.append(tuple(argv))
        if argv[0] == "code":
            return self._editor(list(argv))
        if argv[0] == "ssh":
            return self._ssh(list(argv))
        command = list(argv)[list(argv).index("--") + 1 :]
        return self._in_seat(command, stdin)

    # -- the seat ----------------------------------------------------------

    def _in_seat(self, command: list[str], stdin: str | None) -> CommandResult:
        # The two-token verb, because that is the only spelling the image
        # resolves: matching a bare `debug-config` here would keep this fake
        # green against a launcher that execs nothing in a real seat.
        if command[:2] == ["podbench", "debug-config"]:
            if PROVISION_FLAG in command:
                self.provisioned = True
                self.debug_config_rc = self.provision_rc
                self.configurations = list(self.provisioned_configurations)
            if self.debug_config_rc != 0:
                return _result(self.debug_config_rc, stderr=self.debug_config_stderr)
            document = {"version": "0.2.0", "configurations": self.configurations}
            # stderr on a *successful* run too: debug-config narrates the
            # assessment and prints the injection command there, and relaying
            # that is the difference between a launch.json that connects and one
            # whose F5 meets a closed port.
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

    def _ssh(self, argv: list[str]) -> CommandResult:
        assert argv[-2] == ALIAS, (
            "the preflight has to use the alias VS Code will, through the same "
            f"config: {argv}"
        )
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
    seat: FakeSeat, *, folder: str = HOME, provision: bool = False
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
        provision=provision,
        runner=seat,
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
    assert "attach --new" in message, "a named way out, not just a diagnosis"
    assert [call[0] for call in seat.calls] == ["ssh"], (
        "nothing may run after the probe fails - the extension install reports "
        "success without a connection, and the window would too"
    )
    assert seat.files == {}


def test_the_alias_is_proven_before_the_first_thing_that_needs_it() -> None:
    seat = FakeSeat()

    notes = run_open(seat)

    assert seat.calls[0][0] == "ssh", f"the probe must come first: {seat.calls}"
    assert seat.calls[0][-1] == "true", "the probe has to run a command in the seat"
    assert any(f"`ssh {ALIAS}` reaches the seat" in note for note in notes)


# -- the OOM guard -----------------------------------------------------------


def test_the_excludes_are_written_into_the_folder_that_opens() -> None:
    """/proc/<pid>/root is a symlink into another container's root, so a walk
    from a folder that does not exclude it has no bottom — and an OOM-killed
    ephemeral container cannot be restarted."""
    seat = FakeSeat()
    run_open(seat)

    settings = json.loads(seat.files[f"{HOME}/.vscode/settings.json"])
    assert settings["files.watcherExclude"]["**/proc/**"] is True
    assert settings["search.exclude"]["**/sys/**"] is True
    assert "/proc/**" in settings["python.analysis.exclude"]
    # cpptools' tag parser walks on its own account, and cpptools is what
    # `--open` installs for a C/C++ target.
    assert settings["C_Cpp.files.exclude"]["**/proc/**"] is True


def test_the_excludes_land_before_the_window_does() -> None:
    """The watcher starts walking the moment the folder opens, so configuring
    afterwards is a race whose loser is a seat that cannot be restarted."""
    seat = FakeSeat()
    run_open(seat)

    assert seat.index_of_write("settings.json") < seat.index_of_open


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
    seat = FakeSeat(
        files={f"{HOME}/.vscode/settings.json": json.dumps({"editor.tabSize": 2})}
    )
    run_open(seat)

    settings = json.loads(seat.files[f"{HOME}/.vscode/settings.json"])
    assert settings["editor.tabSize"] == 2
    assert settings["search.exclude"]["**/proc/**"] is True


def test_a_settings_file_that_will_not_parse_is_left_alone() -> None:
    """VS Code permits comments and :mod:`json` does not. Rewriting would drop
    what this parser cannot see, so the file stands and the note says so."""
    seat = FakeSeat(files={f"{HOME}/.vscode/settings.json": "{ // mine\n}"})
    notes = run_open(seat)

    assert seat.files[f"{HOME}/.vscode/settings.json"] == "{ // mine\n}"
    assert any("left exactly as it is" in note for note in notes)


# -- launch.json -------------------------------------------------------------


def test_the_targets_launch_json_is_written_into_the_same_folder() -> None:
    seat = FakeSeat()
    run_open(seat)

    document = json.loads(seat.files[f"{HOME}/.vscode/launch.json"])
    assert document["configurations"] == [DEBUGPY_CONFIG]


def test_a_second_open_updates_its_own_entry_rather_than_appending() -> None:
    seat = FakeSeat()
    run_open(seat)
    run_open(seat)

    document = json.loads(seat.files[f"{HOME}/.vscode/launch.json"])
    assert document["configurations"] == [DEBUGPY_CONFIG]


def test_a_target_no_debugger_fits_still_gets_the_guard_and_the_folder() -> None:
    """debug-config already named every mechanism that said no on its stderr.
    The excludes and the folder are the rest of the seat, and they are the half
    that keeps it alive."""
    seat = FakeSeat(
        debug_config_rc=2, debug_config_stderr="no debugger flavour could be emitted"
    )
    notes = run_open(seat)

    assert f"{HOME}/.vscode/launch.json" not in seat.files
    assert f"{HOME}/.vscode/settings.json" in seat.files
    assert seat.installed == []
    assert any("no launch.json" in note for note in notes)


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
    "debug-config: nothing is listening on 127.0.0.1:5678 yet. Start the server "
    "inside the app with:\n"
    "PYTHONPATH=/proc/1/root/opt/podbench-debugpy \\\n"
    "  /app/.venv/bin/python -m debugpy --listen 127.0.0.1:5678 --pid 1\n"
)


def test_the_whole_refusal_is_relayed_not_just_its_last_line() -> None:
    """ "every mechanism that said no is named above" is the last line, so a
    caller that keeps only the last line points at output it just discarded —
    which is exactly how a missing launch.json read as a podbench bug."""
    seat = FakeSeat(debug_config_rc=2, debug_config_stderr=_REFUSAL)
    notes = run_open(seat)

    assert any("debugpy is not importable by the target" in note for note in notes)


def test_the_injection_command_survives_a_successful_run() -> None:
    """The debugpy configuration is emitted once the prerequisites are met, and
    nothing is listening until the injection runs. Dropping stderr on success
    writes a launch.json whose F5 meets a closed port with nothing said."""
    seat = FakeSeat(debug_config_stderr=_SUCCESS)
    notes = run_open(seat)

    assert f"{HOME}/.vscode/launch.json" in seat.files
    assert any("nothing is listening on 127.0.0.1:5678" in note for note in notes)
    # One note per line: the launcher's report formatter re-wraps on whitespace,
    # so the continuation backslash only survives at the end of its own note.
    assert any(note.endswith("\\") for note in notes)


def test_a_seat_that_never_ran_the_verb_still_says_what_happened() -> None:
    """127 from a `podbench` the image does not resolve carries sh's message and
    no narration at all, and that message is the whole diagnosis."""
    seat = FakeSeat(debug_config_rc=127, debug_config_stderr="")
    notes = run_open(seat)

    assert any("no launch.json: it said nothing" in note for note in notes)


# -- provisioning ------------------------------------------------------------


def test_the_provision_flag_is_offered_where_the_seat_named_it() -> None:
    """The remedy existed on the in-pod verb and was unreachable from here, so
    the refusal has to name the flag that reaches it."""
    seat = FakeSeat(debug_config_rc=2, debug_config_stderr=_REFUSAL)
    notes = run_open(seat)

    assert any("--open --provision" in note for note in notes)


def test_no_provision_offer_for_a_flavour_it_cannot_help() -> None:
    """There is no --provision for a missing delve, and an offer that does not
    apply is a step the reader takes and learns nothing from."""
    seat = FakeSeat(
        debug_config_rc=2,
        debug_config_stderr="debug-config: no gdb in this image, and no lldb\n",
    )
    notes = run_open(seat)

    assert not any("--provision" in note for note in notes)


def test_provision_asks_the_seat_and_gets_a_launch_json() -> None:
    seat = FakeSeat(debug_config_rc=2, debug_config_stderr=_REFUSAL, provision_rc=0)
    run_open(seat, provision=True)

    assert seat.provisioned
    document = json.loads(seat.files[f"{HOME}/.vscode/launch.json"])
    assert document["configurations"] == [DEBUGPY_CONFIG]
    assert seat.installed == ["ms-python.python", "ms-python.debugpy"]


def test_provision_is_announced_with_its_costs_before_it_runs() -> None:
    """It is a uv resolve and download into somebody else's writable layer. A
    cost named after the write is not a cost the user got to decline.

    Counted in *seat* calls rather than in calls: the reachability preflight
    runs first and deliberately so - provisioning writes into the workload and
    ptraces it, which is not worth spending on a seat no editor can reach - but
    it opens one ssh connection and changes nothing.
    """
    seat = FakeSeat(debug_config_rc=2, debug_config_stderr=_REFUSAL)
    timeline: list[tuple[str, int]] = []
    open_seat(
        Kubectl("demo", runner=seat),
        SEAT,
        alias=ALIAS,
        folder=HOME,
        report=lambda note: timeline.append((note, _seat_calls(seat))),
        editor="code",
        provision=True,
        runner=seat,
    )
    notice, announced = next(
        (note, calls) for note, calls in timeline if note.startswith("--provision")
    )
    assert announced == 0
    assert "egress" in notice and "restart" in notice and "15 MB" in notice


def test_nothing_is_provisioned_unless_it_is_asked_for() -> None:
    """Issue #45: writing ~15 MB into the workload is the larger of the two
    mutations a config author must be asked for."""
    seat = FakeSeat(debug_config_rc=2, debug_config_stderr=_REFUSAL)
    run_open(seat)

    assert not seat.provisioned
    assert not any(PROVISION_FLAG in call for call in seat.calls)


# -- extensions --------------------------------------------------------------


def test_only_the_flavours_own_extensions_are_installed_and_remotely() -> None:
    """In Observe mode these land on the workload's ephemeral-storage budget
    (issue #42), so a bundle is somebody else's disk. And ``--remote`` is what
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


def test_the_same_extensions_are_recommended_to_the_folder() -> None:
    seat = FakeSeat()
    run_open(seat)

    document = json.loads(seat.files[f"{HOME}/.vscode/extensions.json"])
    assert document["recommendations"] == ["ms-python.python", "ms-python.debugpy"]


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
        calls for note, calls in timeline if note.startswith("installing ms-python")
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
    preflight has already proven every cause that lives outside VS Code.
    """
    seat = FakeSeat()
    notes = run_open(seat)

    assert any("could not establish connection" in note for note in notes)
    assert any("ms-vscode-remote.remote-ssh" in note for note in notes)
    assert not any("podbench doctor --fix" in note for note in notes), (
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

    assert any("ms-python.python is unpacked in SSH" in note for note in notes)
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
    seat = FakeSeat(install_lands=False)
    notes = run_open(seat)

    assert any("did not land in the seat" in note for note in notes)
    assert any("Install in SSH" in note for note in notes)
    assert not any("is unpacked in SSH" in note for note in notes)
    assert not any("Reload Window" in note for note in notes)


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


def test_a_file_that_cannot_be_written_ends_the_run_before_anything_opens() -> None:
    """The first of these files is the exclude list, and a window opened without
    it walks /proc/<pid>/root. The sentence names the layer that refused, which
    a raw KubectlError - argv and all - does not."""
    seat = FakeSeat(unwritable=[f"{HOME}/.vscode/settings.json"])
    with pytest.raises(EditorError, match="cannot write /root/.vscode/settings.json"):
        run_open(seat)

    assert [call for call in seat.calls if call[0] == "code"] == []


def test_a_file_that_cannot_be_read_is_not_replaced_with_a_fresh_one() -> None:
    """`cat` exits non-zero for "not there" and for "could not read it" alike,
    and reading the second as the first turns the merge this promises into a
    replacement of whatever the seat was already carrying."""
    settings = '{"files.watcherExclude": {"**/mine/**": true}}'
    seat = FakeSeat(
        files={f"{HOME}/.vscode/settings.json": settings},
        unreadable=[f"{HOME}/.vscode/settings.json"],
    )
    with pytest.raises(EditorError, match="cannot read /root/.vscode/settings.json"):
        run_open(seat)

    assert seat.files[f"{HOME}/.vscode/settings.json"] == settings


def test_a_file_that_is_simply_absent_is_written_rather_than_refused() -> None:
    """The common case, and the one the exit code has to keep distinct: a first
    --open meets a seat with no .vscode directory at all."""
    seat = FakeSeat()
    run_open(seat)

    assert f"{HOME}/.vscode/settings.json" in seat.files


def test_the_editor_is_found_on_path_and_named_by_its_absolute_route() -> None:
    assert (
        resolve_editor(lambda name: f"/usr/local/bin/{name}") == "/usr/local/bin/code"
    )


def test_the_files_are_written_where_the_folder_is_and_nowhere_else() -> None:
    seat = FakeSeat()
    run_open(seat, folder="/home/podbench")

    assert set(seat.files) == {
        "/home/podbench/.vscode/settings.json",
        "/home/podbench/.vscode/launch.json",
        "/home/podbench/.vscode/extensions.json",
    }


def test_a_write_creates_the_directory_and_never_redirects_stderr() -> None:
    """Closing or replacing fd 2 in a ``kubectl exec``'d process tears down the
    CRI exec stream and truncates the write with a zero exit (report 3.1)."""
    seat = FakeSeat()
    run_open(seat)

    scripts = [call[-1] for call in seat.calls if call[-2:-1] == ("-c",)]
    assert scripts, seat.calls
    for script in scripts:
        assert "2>" not in script and ">/dev/null" not in script
    writes = [script for script in scripts if "cat > " in script]
    assert writes, scripts
    for script in writes:
        assert script.startswith("mkdir -p ")


def test_nothing_here_shells_out_to_a_second_assessment() -> None:
    """One ``debug-config``, so the extensions installed and the configurations
    written cannot come from two measurements of the same target."""
    seat = FakeSeat()
    run_open(seat)

    assessments = [call for call in seat.calls if "debug-config" in call]
    assert len(assessments) == 1
    assert assessments[0][-1] == "--print-config"
