"""Tests for ``podbench doctor``.

Nothing here touches a cluster, a PATH or the developer's own ``~``:
:class:`FakeMachine` answers both seams — the kubectl ``Runner`` and the
``which`` — and every test runs with ``HOME`` pointed at a ``tmp_path``, because
the one thing this verb *writes* is a file in the user's home directory and a
test that got that wrong would edit the machine running it.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest

from podbench.doctor import (
    FEATURES,
    Grant,
    IncludeState,
    Report,
    Status,
    diagnose,
    format_report,
    include_state,
    main,
)
from podbench.kubectl import CommandResult
from podbench.launcher import ssh_include_line

VERSION_JSON = json.dumps({"clientVersion": {"gitVersion": "v1.31.2"}})

MINE = "SHA256:0kQxHhr+mine"
"""The fingerprint of the identity under test."""

SOMEONE_ELSES = "SHA256:9pLwZzv+theirs"

EVERYTHING = frozenset(
    (grant.verb, grant.resource) for feature in FEATURES for grant in feature.grants
)
"""Every permission any feature asks for, i.e. a fully entitled kubeconfig."""


def without(*grants: Grant) -> frozenset[tuple[str, str]]:
    """:data:`EVERYTHING` minus the named grants."""
    return EVERYTHING - {(grant.verb, grant.resource) for grant in grants}


class FakeMachine:
    """A kubectl binary and a PATH, neither of which exists.

    One object answers both seams so that a test says "this machine" once: the
    ``which`` decides whether a program is found, the ``__call__`` is the
    :class:`~podbench.kubectl.Runner`.
    """

    def __init__(
        self,
        *,
        binaries: Sequence[str] = ("kubectl", "ssh", "ssh-add", "ssh-keygen"),
        version_json: str = VERSION_JSON,
        context: str = "kind-kind",
        namespace: str = "demo",
        allowed: frozenset[tuple[str, str]] = EVERYTHING,
        can_i_answers: bool = True,
        identity_fingerprint: str = MINE,
        identity_algorithm: str = "ED25519",
        identity_listing: str | None = None,
        keygen_error: str = "",
        agent_keys: Sequence[str] | None = (),
        agent_error: tuple[int, str] | None = None,
        identity_agent: str | None = None,
    ) -> None:
        self.binaries = set(binaries)
        self.version_json = version_json
        self.context = context
        self.namespace = namespace
        self.allowed = allowed
        self.can_i_answers = can_i_answers
        self.identity_fingerprint = identity_fingerprint
        self.identity_algorithm = identity_algorithm
        self.identity_listing = identity_listing
        """The whole of ``ssh-keygen -lf``'s stdout, for a ``.pub`` holding more
        than one key. ``None`` builds the single line the other two describe."""

        self.keygen_error = keygen_error
        self.agent_keys = agent_keys
        """What ``ssh-add -l`` lists, or ``None`` for an agent that never answers."""

        self.agent_error = agent_error
        """``(returncode, stderr)`` for a listing that failed rather than came
        back empty, which ``ssh-add`` reports with the same exit code."""

        self.identity_agent = identity_agent
        """What ``ssh -G`` reports the ``IdentityAgent`` keyword resolves to.
        ``None`` prints no such line, which is what ssh does when it is unset."""

        self.calls: list[tuple[str, ...]] = []

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.binaries else None

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
    ) -> CommandResult:
        del stdin, capture
        self.calls.append(tuple(argv))
        return CommandResult(tuple(argv), *self._answer(list(argv)))

    def _answer(self, argv: list[str]) -> tuple[int, str, str]:
        if argv[0] == "ssh-keygen":
            if self.keygen_error:
                return 255, "", self.keygen_error
            assert argv[1] == "-lf", f"unexpected ssh-keygen call: {argv}"
            return (
                0,
                self.identity_listing
                or f"256 {self.identity_fingerprint} dev@laptop "
                f"({self.identity_algorithm})\n",
                "",
            )
        if argv[0] == "ssh-add":
            if self.agent_error is not None:
                returncode, stderr = self.agent_error
                return returncode, "", stderr
            if self.agent_keys is None:
                return 2, "", "Could not open a connection to your agent."
            if not self.agent_keys:
                return 1, "The agent has no identities.\n", ""
            listed = "".join(
                f"256 {key} dev@laptop (ED25519)\n" for key in self.agent_keys
            )
            return 0, listed, ""
        if argv[0] == "ssh":
            assert argv[1] == "-G", f"unexpected ssh call: {argv}"
            printed = f"host {argv[-1]}\nuser root\n"
            if self.identity_agent is not None:
                # ssh -G prints the keyword only when it is set.
                printed += f"identityagent {self.identity_agent}\n"
            return 0, printed, ""
        if "version" in argv:
            return 0, self.version_json, ""
        if argv[1:] == ["config", "current-context"] or argv[2:] == [
            "config",
            "current-context",
        ]:
            return (0, f"{self.context}\n", "") if self.context else (1, "", "error")
        if "view" in argv:
            return 0, f"{self.namespace}\n", ""
        if "can-i" in argv:
            if not self.can_i_answers:
                return 1, "", "error: the server could not be reached"
            index = argv.index("can-i")
            verb, resource = argv[index + 1], argv[index + 2]
            # A subresource arrives as --subresource=exec, so put it back into
            # the `pods/exec` spelling `allowed` is keyed by. Reading only the
            # positional would make every subresource grant look like a request
            # for the bare resource, which is the defect this fake must not
            # reproduce.
            for arg in argv[index + 3 :]:
                if arg.startswith("--subresource="):
                    resource = f"{resource}/{arg.split('=', 1)[1]}"
            allowed = (verb, resource) in self.allowed
            return (0, "yes\n", "") if allowed else (1, "no\n", "")
        raise AssertionError(f"unexpected kubectl call: {argv}")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory with an ssh keypair in it and nothing else."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PODBENCH_CONFIG_DIR", raising=False)
    monkeypatch.delenv("PODBENCH_IMAGE", raising=False)
    # The machine running the suite very likely has an agent, and every test
    # that does not ask about one must not inherit its answers.
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("private")
    (ssh / "id_ed25519.pub").write_text("ssh-ed25519 AAAA developer@laptop")
    return tmp_path


def flowed(report: Report) -> str:
    """``format_report`` with its wrapping undone, for asserting on phrases.

    The report wraps to the terminal now, so a sentence it prints is not a
    contiguous substring of what it printed. A test that cares *what* was said
    collapses the whitespace and matches the sentence; the ones that care how it
    is laid out live in ``tests/test_console.py``.
    """
    return " ".join(format_report(report).split())


def statuses(report: Report) -> dict[str, Status]:
    """Every check and feature verdict by name, which is what a test asserts on."""
    return {check.name: check.status for check in report.checks} | {
        verdict.feature.name: verdict.status for verdict in report.verdicts
    }


def wired(home: Path) -> None:
    """The include line the docs ask for, in the position they ask for it."""
    include = ssh_include_line(home / ".podbench")
    (home / ".ssh" / "config").write_text(f"{include}\n")
    (home / ".podbench" / "config.d").mkdir(parents=True)


# -- the happy machine ------------------------------------------------------


def test_a_wired_machine_reports_no_blockers_and_exits_zero(home: Path) -> None:
    wired(home)
    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which)
    assert report.blockers == ()
    assert report.exit_code == 0
    assert set(statuses(report).values()) == {Status.OK}


def test_the_report_names_the_launcher_the_image_and_where_it_is_pointed(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Under uvx the launcher's version floats between two attaches with nothing
    # to announce it, and the image tag follows it - so "which launcher, and
    # which image" is a question about this machine, not about the pod.
    wired(home)
    monkeypatch.setenv("PODBENCH_IMAGE", "registry.example/podbench@sha256:abc")
    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which)
    rendered = format_report(report)
    assert report.image == "registry.example/podbench@sha256:abc"
    assert report.version in rendered
    assert "registry.example/podbench@sha256:abc" in rendered
    assert "kind-kind" in rendered
    assert "demo" in rendered


# -- kubectl ----------------------------------------------------------------


def test_kubectl_missing_blocks_and_stops_every_cluster_question(home: Path) -> None:
    # Nothing below the binary can be measured without it, and reporting "you
    # are not allowed" for a question nobody asked would be an invention.
    wired(home)
    machine = FakeMachine(binaries=("ssh",))
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["kubectl"] is Status.FAIL
    assert machine.calls == []
    assert all(verdict.unknown == verdict.feature.grants for verdict in report.verdicts)
    assert "kubectl" in report.blockers
    assert "attach" not in report.blockers


def test_kubectl_below_the_ephemeral_container_floor_blocks(home: Path) -> None:
    wired(home)
    machine = FakeMachine(
        version_json=json.dumps({"clientVersion": {"gitVersion": "v1.24.17"}})
    )
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["kubectl"] is Status.FAIL
    assert "1.25" in format_report(report)


def test_an_unreadable_kubectl_version_warns_rather_than_blocking(home: Path) -> None:
    # A vendored kubectl that prints something else is not a reason to refuse to
    # attach; it is a reason to say the check could not be made.
    wired(home)
    machine = FakeMachine(version_json="Client Version: v1.31.2\n")
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["kubectl"] is Status.WARN
    assert report.exit_code == 0


def test_a_kubeconfig_with_no_current_context_blocks(home: Path) -> None:
    wired(home)
    machine = FakeMachine(context="")
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["kubeconfig"] is Status.FAIL
    assert report.context is None


def test_an_explicit_context_is_taken_without_asking_the_kubeconfig(
    home: Path,
) -> None:
    wired(home)
    machine = FakeMachine(context="")
    report = diagnose(runner=machine, which=machine.which, context="prod")
    assert report.context == "prod"
    assert statuses(report)["kubeconfig"] is Status.OK


# -- ssh --------------------------------------------------------------------


def test_a_missing_ssh_client_blocks(home: Path) -> None:
    wired(home)
    machine = FakeMachine(binaries=("kubectl",))
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh client"] is Status.FAIL


def test_a_missing_identity_is_named_and_never_generated(home: Path) -> None:
    # The whole point of naming it: a key doctor minted would be a credential
    # the user never chose, which attach would then authorise in their cluster.
    wired(home)
    (home / ".ssh" / "id_ed25519.pub").unlink()
    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh identity"] is Status.FAIL
    assert not (home / ".ssh" / "id_ed25519.pub").exists()

    fixed = diagnose(runner=machine, which=machine.which, fix=True)
    assert statuses(fixed)["ssh identity"] is Status.FAIL
    assert not (home / ".ssh" / "id_ed25519.pub").exists()
    assert "ssh-keygen" in format_report(fixed)


# -- the ssh agent ----------------------------------------------------------


@pytest.fixture
def agent(home: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """An ssh-agent socket in the environment, of the shape ssh-agent makes.

    Depends on ``home`` rather than merely coexisting with it: that fixture
    *unsets* SSH_AUTH_SOCK, and without an ordering between them a test that
    asks for both silently gets whichever ran last.
    """
    del home
    socket = "/tmp/ssh-XXXXaBcD/agent.4242"
    monkeypatch.setenv("SSH_AUTH_SOCK", socket)
    return socket


def test_no_agent_at_all_says_the_file_will_sign_and_asks_nothing(home: Path) -> None:
    # No SSH_AUTH_SOCK, no question to ask: nothing can stand between ssh and
    # the file, and shelling out to ssh-add would only be able to agree.
    wired(home)
    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh agent"] is Status.OK
    assert not [call for call in machine.calls if call[0].startswith("ssh")]
    assert "passphrase prompt" in format_report(report)


def test_no_agent_and_no_key_does_not_promise_the_file_will_sign(home: Path) -> None:
    # check_identity has already failed here; an `[ok] ssh agent  ... ssh signs
    # with /home/dev/.ssh/id_ed25519 itself` under it names a file that is not
    # there.
    wired(home)
    (home / ".ssh" / "id_ed25519").unlink()
    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh identity"] is Status.FAIL
    assert statuses(report)["ssh agent"] is Status.WARN
    assert "nothing left that could sign" in flowed(report)


def test_an_agent_holding_the_identity_is_named_with_both_escapes(
    home: Path, agent: str
) -> None:
    # The one that opened #53: doctor passed, attach succeeded, and ssh then
    # failed client-side with `agent refused operation` followed by a
    # `Permission denied (publickey,...)` that reads as the seat's fault.
    wired(home)
    machine = FakeMachine(agent_keys=(SOMEONE_ELSES, MINE))
    report = diagnose(runner=machine, which=machine.which)
    rendered = flowed(report)

    assert statuses(report)["ssh agent"] is Status.WARN
    assert report.exit_code == 0
    assert f"agent on {agent} holds {MINE}" in rendered
    assert "sign with the AGENT" in rendered
    # The public half is what gets fingerprinted: the private one would prompt
    # for a passphrase, or refuse.
    assert (
        "ssh-keygen",
        "-lf",
        str(home / ".ssh" / "id_ed25519.pub"),
    ) in machine.calls
    # Both escapes, in the house spelling: one settles who refuses, the other
    # changes it. The alias carries the namespace doctor resolved, and `<pod>`
    # stays a placeholder because doctor runs before there is a pod.
    assert "SSH_AUTH_SOCK= ssh podbench-demo-<pod>" in rendered
    assert "IdentityAgent none" in rendered
    # Below the Include line, because a Host block above it leaves doctor's own
    # include check reporting `shadowed` on the next run.
    assert "below the Include line" in rendered
    # ...and never unqualified, because a FIDO key or a smartcard can only sign
    # through an agent.
    assert "sk-*" in rendered


@pytest.mark.usefixtures("agent")
def test_an_agent_without_the_identity_leaves_the_file_signing(home: Path) -> None:
    wired(home)
    machine = FakeMachine(agent_keys=(SOMEONE_ELSES,))
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh agent"] is Status.OK
    assert "IdentityAgent" not in format_report(report)


@pytest.mark.usefixtures("agent")
def test_an_empty_agent_leaves_the_file_signing(home: Path) -> None:
    wired(home)
    machine = FakeMachine(agent_keys=())
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh agent"] is Status.OK


def test_a_gnome_keyring_socket_is_named_with_the_ed25519_caveat(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # gnome-keyring stands in for ssh-agent on most desktop logins and has a
    # long history of refusing ED25519 keys with exactly that message.
    wired(home)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/keyring/ssh")
    machine = FakeMachine(agent_keys=(MINE,))
    rendered = format_report(diagnose(runner=machine, which=machine.which))
    assert "gnome-keyring" in rendered
    assert "ED25519" in rendered
    assert "agent refused operation" in rendered


def test_an_ordinary_agent_socket_is_not_blamed_on_gnome_keyring(
    home: Path, agent: str
) -> None:
    wired(home)
    assert agent.startswith("/tmp/")
    machine = FakeMachine(agent_keys=(MINE,))
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh agent"] is Status.WARN
    assert "gnome-keyring" not in format_report(report)


@pytest.mark.parametrize("algorithm", ["ED25519-SK", "ECDSA-SK", "ED25519-SK-CERT"])
@pytest.mark.usefixtures("agent")
def test_a_fido_key_is_never_told_to_bypass_its_agent(
    home: Path, algorithm: str
) -> None:
    # An sk-* key has no private half on disk: `IdentityAgent none` does not
    # make it sign with the file, it stops it signing at all. A certificate over
    # one is the same key underneath, and ssh-keygen spells it -SK-CERT.
    wired(home)
    machine = FakeMachine(agent_keys=(MINE,), identity_algorithm=algorithm)
    rendered = format_report(diagnose(runner=machine, which=machine.which))
    assert "do not set IdentityAgent none" in rendered
    assert "Host podbench-*" not in rendered


@pytest.mark.usefixtures("agent")
def test_a_second_key_in_the_pub_file_cannot_mislabel_the_first(home: Path) -> None:
    # `ssh-keygen -lf` prints one line per key in the file. Read as a whole, the
    # fingerprint comes from the first line and the algorithm — anchored to the
    # end of its line — from the last, which is how an sk key gets told to set
    # `IdentityAgent none` and stop signing altogether.
    wired(home)
    machine = FakeMachine(
        agent_keys=(MINE,),
        identity_listing=f"256 {MINE} dev (ED25519-SK)\n3072 {SOMEONE_ELSES} d (RSA)\n",
    )
    rendered = format_report(diagnose(runner=machine, which=machine.which))
    assert "do not set IdentityAgent none" in rendered
    assert "Host podbench-*" not in rendered


@pytest.mark.usefixtures("agent")
def test_identity_agent_none_already_in_the_config_ends_the_warning(
    home: Path,
) -> None:
    # The check has to be able to see its own advice taken: ssh -G resolves the
    # user's config the way the connection will, and with the keyword set the
    # agent is out of play whatever it holds.
    wired(home)
    machine = FakeMachine(agent_keys=(MINE,), identity_agent="none")
    report = diagnose(runner=machine, which=machine.which)
    rendered = format_report(report)
    assert statuses(report)["ssh agent"] is Status.OK
    # The alias that was asked about, so a --host-alias user can see what was
    # measured rather than guess.
    assert "IdentityAgent none for podbench-demo-pod" in rendered
    assert "sign with the AGENT" not in rendered
    assert ("ssh", "-G", "-o", "CanonicalizeHostname=no", "podbench-demo-pod") in (
        machine.calls
    )


@pytest.mark.usefixtures("agent")
def test_an_identity_agent_pointing_somewhere_else_still_warns(home: Path) -> None:
    # Only `none` takes the agent out of play; any other value is another agent.
    wired(home)
    machine = FakeMachine(agent_keys=(MINE,), identity_agent="/run/user/1000/other")
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh agent"] is Status.WARN


@pytest.mark.usefixtures("agent")
def test_no_ssh_client_leaves_the_agent_warning_standing(home: Path) -> None:
    # ssh -G is how the fix is observed; without ssh there is nothing to ask,
    # and an unanswerable question must not become a clean report.
    wired(home)
    machine = FakeMachine(
        binaries=("kubectl", "ssh-add", "ssh-keygen"), agent_keys=(MINE,)
    )
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh agent"] is Status.WARN
    assert not [call for call in machine.calls if call[0] == "ssh"]


@pytest.mark.usefixtures("agent")
def test_a_dead_socket_warns_without_claiming_the_agent_will_sign(
    home: Path,
) -> None:
    wired(home)
    machine = FakeMachine(agent_keys=None)
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh agent"] is Status.WARN
    assert "no agent answered" in format_report(report)
    assert report.exit_code == 0


@pytest.mark.parametrize("absent", ["ssh-add", "ssh-keygen"])
@pytest.mark.usefixtures("agent")
def test_a_missing_command_is_not_measured_rather_than_guessed(
    home: Path, absent: str
) -> None:
    wired(home)
    binaries = ["kubectl", "ssh", "ssh-add", "ssh-keygen"]
    binaries.remove(absent)
    machine = FakeMachine(binaries=binaries)
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh agent"] is Status.WARN
    assert "not measured" in format_report(report)


@pytest.mark.parametrize(
    "agent_error",
    [
        (1, "error fetching identities: communication with agent failed"),
        (255, "boom"),
    ],
)
@pytest.mark.usefixtures("agent")
def test_a_listing_that_failed_is_not_read_as_an_agent_without_the_key(
    home: Path, agent_error: tuple[int, str]
) -> None:
    # ssh-add exits 1 both for the empty agent and for a listing that failed,
    # and the second one has no fingerprints in it either — so "the agent does
    # not hold your key" would be the reassuring half of this check, guessed.
    wired(home)
    machine = FakeMachine(agent_error=agent_error)
    report = diagnose(runner=machine, which=machine.which)
    rendered = flowed(report)
    assert statuses(report)["ssh agent"] is Status.WARN
    assert "not measured" in rendered
    assert agent_error[1] in rendered
    assert "does not hold" not in rendered


@pytest.mark.usefixtures("agent")
def test_a_refused_pub_file_reports_what_ssh_keygen_said(home: Path) -> None:
    wired(home)
    machine = FakeMachine(keygen_error="the public half is not a key file.")
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh agent"] is Status.WARN
    assert "the public half is not a key file." in format_report(report)


@pytest.mark.usefixtures("agent")
def test_a_missing_public_half_is_not_measured_rather_than_guessed(
    home: Path,
) -> None:
    wired(home)
    (home / ".ssh" / "id_ed25519.pub").unlink()
    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh agent"] is Status.WARN
    assert statuses(report)["ssh identity"] is Status.FAIL
    assert not [call for call in machine.calls if call[0] == "ssh-add"]


# -- the Include splice -----------------------------------------------------


def test_an_absent_include_blocks_and_prints_the_exact_line(home: Path) -> None:
    (home / ".ssh" / "config").write_text("Host build\n    User me\n")
    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh include"] is Status.FAIL
    assert ssh_include_line(home / ".podbench") in format_report(report)


def test_no_ssh_config_at_all_is_the_same_blocker(home: Path) -> None:
    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh include"] is Status.FAIL


def test_an_include_below_a_host_star_block_warns_but_does_not_block(
    home: Path,
) -> None:
    # ssh takes the first value it sees for each keyword and `Host *` matches
    # everything, so the stanza is read but every keyword that block also sets
    # has already won - a ControlPath from someone's dotfiles silently replaces
    # the short one podbench chose to stay under the 108-byte AF_UNIX limit.
    include = ssh_include_line(home / ".podbench")
    (home / ".ssh" / "config").write_text(
        f"Host *\n    ControlPath ~/.ssh/%C\n{include}\n"
    )
    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh include"] is Status.WARN
    assert report.exit_code == 0


def test_an_include_already_at_the_top_is_left_alone_by_fix(home: Path) -> None:
    wired(home)
    config = home / ".ssh" / "config"
    before = config.read_text()
    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which, fix=True)
    assert statuses(report)["ssh include"] is Status.OK
    assert config.read_text() == before


def test_an_include_written_with_a_tilde_counts(home: Path) -> None:
    # The line podbench prints is absolute; the line a user types is not.
    (home / ".ssh" / "config").write_text("Include ~/.podbench/config.d/*.conf\n")
    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh include"] is Status.OK


def test_fix_inserts_the_include_above_an_existing_host_star_block(home: Path) -> None:
    config = home / ".ssh" / "config"
    config.write_text("Host *\n    ForwardAgent yes\n")
    config.chmod(0o640)
    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which, fix=True)

    text = config.read_text()
    assert statuses(report)["ssh include"] is Status.OK
    assert include_state(text, ssh_include_line(home / ".podbench")) is (
        IncludeState.ACTIVE
    )
    # The user's own file survives verbatim, and keeps the mode ssh cares about.
    assert text.endswith("Host *\n    ForwardAgent yes\n")
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert [
        path.name for path in (home / ".ssh").iterdir() if "podbench" in path.name
    ] == []


def test_fix_creates_an_ssh_config_when_there_is_none(home: Path) -> None:
    machine = FakeMachine()
    diagnose(runner=machine, which=machine.which, fix=True)
    config = home / ".ssh" / "config"
    assert include_state(config.read_text(), ssh_include_line(home / ".podbench")) is (
        IncludeState.ACTIVE
    )
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_fix_follows_a_symlinked_ssh_config_rather_than_replacing_it(
    home: Path,
) -> None:
    # ~/.ssh/config is very often a link into a dotfiles repository. Replacing
    # the link with a regular file would leave the user's own edits going to a
    # file ssh has stopped reading - podbench taking ownership of a config it
    # has always refused to own.
    dotfiles = home / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "ssh_config"
    real.write_text("Host *\n    ForwardAgent yes\n")
    config = home / ".ssh" / "config"
    config.symlink_to(real)

    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which, fix=True)

    assert statuses(report)["ssh include"] is Status.OK
    assert config.is_symlink() and config.readlink() == real
    text = real.read_text()
    assert text.endswith("Host *\n    ForwardAgent yes\n")
    assert include_state(text, ssh_include_line(home / ".podbench")) is (
        IncludeState.ACTIVE
    )
    assert [path.name for path in dotfiles.iterdir() if "podbench" in path.name] == []


def test_fix_is_idempotent(home: Path) -> None:
    machine = FakeMachine()
    diagnose(runner=machine, which=machine.which, fix=True)
    once = (home / ".ssh" / "config").read_text()
    diagnose(runner=machine, which=machine.which, fix=True)
    assert (home / ".ssh" / "config").read_text() == once


def test_fix_moves_a_shadowed_include_into_effect_without_deleting_it(
    home: Path,
) -> None:
    # Conservative means prepending, never rewriting: the user's line stays
    # where they put it and ours is the one ssh reads first.
    include = ssh_include_line(home / ".podbench")
    (home / ".ssh" / "config").write_text(f"Host *\n    User me\n{include}\n")
    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which, fix=True)
    text = (home / ".ssh" / "config").read_text()
    assert statuses(report)["ssh include"] is Status.OK
    assert text.count(include) == 2


def test_a_commented_out_include_does_not_count(home: Path) -> None:
    include = ssh_include_line(home / ".podbench")
    (home / ".ssh" / "config").write_text(f"# {include}\n")
    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["ssh include"] is Status.FAIL


def test_the_config_directory_is_a_warning_and_fix_creates_it(home: Path) -> None:
    (home / ".ssh" / "config").write_text(ssh_include_line(home / ".podbench") + "\n")
    machine = FakeMachine()
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["config dir"] is Status.WARN
    assert report.exit_code == 0

    fixed = diagnose(runner=machine, which=machine.which, fix=True)
    directory = home / ".podbench" / "config.d"
    assert statuses(fixed)["config dir"] is Status.OK
    assert directory.is_dir()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_a_client_dir_with_a_space_in_it_is_quoted_and_still_settles(
    home: Path,
) -> None:
    # ssh_config splits a directive's arguments on whitespace and so does the
    # parser here, so an unquoted `/Users/Jo Smith/...` would read back as
    # MISSING every time - and --fix would prepend the line again on every run
    # instead of settling, which is the one thing this fix must never do.
    elsewhere = str(home / "Jo Smith" / ".podbench")
    machine = FakeMachine()
    report = diagnose(
        runner=machine, which=machine.which, config_dir=elsewhere, fix=True
    )
    once = (home / ".ssh" / "config").read_text()
    assert statuses(report)["ssh include"] is Status.OK
    assert f'Include "{Path(elsewhere) / "config.d" / "*.conf"}"' in once

    diagnose(runner=machine, which=machine.which, config_dir=elsewhere, fix=True)
    assert (home / ".ssh" / "config").read_text() == once


def test_the_config_dir_flag_moves_both_the_directory_and_the_include(
    home: Path,
) -> None:
    elsewhere = home / "elsewhere"
    machine = FakeMachine()
    diagnose(runner=machine, which=machine.which, config_dir=str(elsewhere), fix=True)
    assert (elsewhere / "config.d").is_dir()
    assert ssh_include_line(elsewhere) in (home / ".ssh" / "config").read_text()


# -- the can-i matrix -------------------------------------------------------


def test_every_grant_is_asked_of_the_namespace_in_play(home: Path) -> None:
    wired(home)
    machine = FakeMachine()
    diagnose(runner=machine, which=machine.which)
    asked = [call for call in machine.calls if "can-i" in call]
    assert len(asked) == sum(len(feature.grants) for feature in FEATURES)
    assert all(("-n", "demo") == call[1:3] for call in asked)


def test_a_subresource_is_asked_for_as_a_subresource(home: Path) -> None:
    # kubectl reads a positional `pods/exec` as a pod *named* exec, so asking
    # that way reports the whole attach path as blocked against a namespace
    # where it works. Invisible from an entitled kubeconfig, which is allowed
    # both readings — so this asserts the argv, not the verdict.
    wired(home)
    machine = FakeMachine()
    diagnose(runner=machine, which=machine.which)
    asked = [call for call in machine.calls if "can-i" in call]
    assert ("create", "pods", "--subresource=exec") in {call[-3:] for call in asked}
    assert ("update", "pods", "--subresource=ephemeralcontainers") in {
        call[-3:] for call in asked
    }
    assert ("patch", "pods", "--subresource=resize") in {call[-3:] for call in asked}
    # No grant reaches kubectl as one slashed word, whatever the table says.
    assert not [
        arg for call in asked for arg in call if arg.count("/") and arg[0] != "-"
    ]


def test_a_denied_attach_verb_blocks_the_headline_path(home: Path) -> None:
    # Today this is discovered mid-attach, after a container name has been
    # burnt for the life of the pod.
    wired(home)
    machine = FakeMachine(allowed=without(Grant("create", "pods/exec")))
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["attach"] is Status.FAIL
    assert report.exit_code == 1
    assert "create pods/exec" in format_report(report)
    assert "rbac.observe=true" in format_report(report)


def test_a_denied_iterate_verb_is_reported_but_does_not_block(home: Path) -> None:
    # A cluster that will not grant Iterate mode is a fact about that cluster.
    wired(home)
    machine = FakeMachine(allowed=without(Grant("create", "pods")))
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["iterate"] is Status.WARN
    assert statuses(report)["attach"] is Status.OK
    assert report.exit_code == 0


def test_each_feature_is_reported_separately(home: Path) -> None:
    wired(home)
    machine = FakeMachine(allowed=without(Grant("patch", "pods/resize")))
    report = diagnose(runner=machine, which=machine.which)
    verdicts = statuses(report)
    assert verdicts["resize"] is Status.WARN
    assert verdicts["iterate"] is Status.OK
    assert verdicts["hotfix"] is Status.OK


def test_resize_needs_the_read_kubectl_makes_before_the_write(home: Path) -> None:
    """The argus defect: `doctor` said `resize [ok]` where resize was Forbidden.

    `kubectl patch --subresource=resize` issues a GET on the subresource before
    it sends the PATCH, so a service account holding `pods/resize: [patch]` and
    no `get` fails on the read (measured with `kubectl --v=8` on hgv27681: the
    GET is Forbidden and the PATCH is never sent). The feature declared only
    `patch`, four lines under an `attach` feature that declares both verbs on
    `pods/ephemeralcontainers` for exactly this reason.

    Not asserted through `can-i`: on that same cluster
    `kubectl auth can-i get pods/resize` answers yes while the real GET is
    refused, so what has to be right is the grant list, and this pins it.
    """
    wired(home)
    machine = FakeMachine(allowed=without(Grant("get", "pods/resize")))
    report = diagnose(runner=machine, which=machine.which)

    assert statuses(report)["resize"] is Status.WARN
    assert "missing: get pods/resize" in flowed(report)


def test_a_cluster_that_cannot_be_reached_is_unknown_and_not_denied(
    home: Path,
) -> None:
    # "you are not allowed" and "nobody was asked" send a reader to different
    # places, so an unreachable apiserver must not read as an RBAC denial.
    wired(home)
    machine = FakeMachine(can_i_answers=False)
    report = diagnose(runner=machine, which=machine.which)
    assert statuses(report)["attach"] is Status.WARN
    assert report.exit_code == 0
    assert "not measured" in format_report(report)


# -- the exit code contract -------------------------------------------------


def test_warnings_alone_never_change_the_exit_code(home: Path) -> None:
    (home / ".ssh" / "config").write_text(ssh_include_line(home / ".podbench") + "\n")
    machine = FakeMachine(allowed=without(Grant("patch", "pods/resize")))
    report = diagnose(runner=machine, which=machine.which)
    assert Status.WARN in statuses(report).values()
    assert report.exit_code == 0


def test_the_cli_returns_the_reports_exit_code(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wired(home)
    machine = FakeMachine()
    assert main([], runner=machine, which=machine.which) == 0
    assert "VERDICT: nothing blocks" in capsys.readouterr().out

    denied = FakeMachine(allowed=without(Grant("get", "pods")))
    assert main([], runner=denied, which=denied.which) == 1
    assert "BLOCKERS: attach" in capsys.readouterr().out


def test_a_usage_error_is_2_and_measures_nothing(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The third documented exit code, pinned here and not only at the dispatcher
    # because what matters about it is local: a mistyped flag must not reach
    # `diagnose`. A verb that asked the cluster and wrote to ~/.ssh on its way
    # to rejecting the command line would have done the work anyway.
    machine = FakeMachine()
    assert main(["--not-a-flag"], runner=machine, which=machine.which) == 2
    assert machine.calls == []
    assert not (home / ".ssh" / "config").exists()
    assert "--not-a-flag" in capsys.readouterr().err


def test_the_cli_fixes_only_when_asked(home: Path) -> None:
    machine = FakeMachine()
    assert main([], runner=machine, which=machine.which) == 1
    assert not (home / ".ssh" / "config").exists()

    assert main(["--fix"], runner=machine, which=machine.which) == 0
    assert (home / ".ssh" / "config").is_file()


def test_the_dispatcher_reaches_the_verb(capsys: pytest.CaptureFixture[str]) -> None:
    from podbench.__main__ import main as dispatch

    assert dispatch(["doctor", "--help"]) == 0
    out = capsys.readouterr().out
    assert "Usage: podbench doctor" in out
    assert "--fix" in out


# -- the Include parser, on its own -----------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", IncludeState.MISSING),
        ("Include /pb/config.d/*.conf\n", IncludeState.ACTIVE),
        ("include=/pb/config.d/*.conf\n", IncludeState.ACTIVE),
        ('Include "/pb/config.d/*.conf"\n', IncludeState.ACTIVE),
        ("Include /other/*.conf\n", IncludeState.MISSING),
        ("Match host x\nInclude /pb/config.d/*.conf\n", IncludeState.SHADOWED),
        (
            "Include /pb/config.d/*.conf\nHost *\nInclude /pb/config.d/*.conf\n",
            IncludeState.ACTIVE,
        ),
        ("Host x\n    ProxyJump 'unbalanced\n", IncludeState.MISSING),
    ],
)
def test_include_state_reads_an_ssh_config_the_way_ssh_does(
    text: str, expected: IncludeState
) -> None:
    assert include_state(text, "Include /pb/config.d/*.conf") is expected


def test_the_include_line_is_the_one_attach_prints(home: Path) -> None:
    # One definition, or the advice comes to name a directory nothing writes to.
    from podbench.launcher import CONFIG_D, client_dir

    assert ssh_include_line(client_dir(None)) == (
        f"Include {home / '.podbench' / CONFIG_D / '*.conf'}"
    )
    assert os.path.expanduser("~") == str(home)
