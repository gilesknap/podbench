"""S1 as a regression: ssh over ``kubectl exec``, and the ways it dies.

The spike proved the transport works once, by hand. What this file is for is
the *shape* of it: every constant in :mod:`podbench.sshcfg` was measured, and
every one of them fails silently or misleadingly when it is wrong — a wrong
flag looks like a network fault, or truncates a stream with ``rc=0``. So the
positive tests here are ordinary (a command returns, a megabyte survives, two
sessions coexist) and the interesting ones are the two negatives at the bottom.

``test_fd2_teardown_truncates_the_exec_stream`` is the mechanism, isolated
without sshd in the picture at all, exactly as report §3.1 isolated it.
``test_transport_dies_without_dash_e`` is the consequence, and is the test
report §4.1 asks for by name: "Ship a regression test that asserts the
transport dies without ``-e``, so the fd-2 reason is documented in code (a
non-``exec`` wrapper masks it)."
"""

from __future__ import annotations

import json
import random
import shlex
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from podbench.launcher import ssh_config_path
from podbench.model import PodRef

from .conftest import FIRST_SEAT, KubectlCli, PodbenchCli

TARGET_POD = "transport-target"

# Authored here rather than in apps/: S1 needs nothing from its target except a
# container that stays up and has a shell, and giving it a demo app would
# suggest the transport cared what it was attached to.
TARGET_MANIFEST: dict[str, object] = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {"name": TARGET_POD, "labels": {"app": TARGET_POD}},
    "spec": {
        "restartPolicy": "Never",
        "containers": [
            {
                "name": "app",
                "image": "debian:bookworm-slim",
                "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/sleep", "infinity"],
                "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}},
            }
        ],
    },
}

STREAM_BYTES = (1 << 20) + 4242
"""Just over 1 MiB. The vscode-server bootstrap is exactly this shape — a
single large payload down one exec channel — and the fd-2 teardown truncates
rather than erroring, so a payload smaller than the pipe buffers would pass
while corrupt."""

SSH_TIMEOUT = 120.0

NO_MULTIPLEXING = (
    "-o",
    "ControlMaster=no",
    "-o",
    "ControlPath=none",
)
"""Most ssh invocations here disable the ControlMaster the generated config
turns on. Not because multiplexing is wrong — it is the measured 6x speedup and
the config is right to default to it — but because a live master bypasses the
ProxyCommand entirely, and a negative test that reuses one would pass while
proving nothing.

Not a workaround for two seats colliding on one socket: that was a real defect,
and ``test_two_seats_on_one_pod_do_not_share_a_control_master`` is the test for
it, with multiplexing deliberately left on."""


@pytest.fixture(scope="module")
def target(kubectl: KubectlCli) -> str:
    """A plain root pod, Ready, in the module's scratch namespace."""
    kubectl.run("create", "-f", "-", stdin=json.dumps(TARGET_MANIFEST))
    kubectl.wait(f"pod/{TARGET_POD}", "condition=Ready", timeout=300)
    return TARGET_POD


@pytest.fixture(scope="module")
def ssh_config(
    kubectl: KubectlCli,
    podbench: PodbenchCli,
    target: str,
    podbench_image: str,
    ssh_identity: tuple[Path, str],
) -> Iterator[tuple[Path, str]]:
    """Attach a seat and return (config path, host alias).

    This is the launcher's own path — ``podbench attach`` writing an ssh
    stanza — rather than a hand-built config, because the thing under test is
    what a user gets, not what the library can be persuaded to emit.
    """
    if shutil.which("ssh") is None:
        pytest.skip("no ssh client installed")
    identity, _ = ssh_identity
    result = podbench.run(
        "attach",
        target,
        "--identity",
        str(identity),
        "--image",
        podbench_image,
        *podbench.common(),
        timeout=420,
    )
    config_dir = podbench.config_dir
    assert config_dir is not None
    # Derived, never spelled: the stanza is named per *seat* since issue #93,
    # and a hand-built path here would go stale the next time that changes -
    # which is exactly how this assertion broke.
    path = ssh_config_path(config_dir, PodRef(kubectl.namespace, target), FIRST_SEAT)
    assert path.is_file(), f"attach wrote no ssh config:\n{result.stdout}"
    alias = _host_alias(path)
    yield path, alias
    # Nothing to undo: the ephemeral container is permanent by construction and
    # goes away with the namespace.


def _host_alias(config: Path) -> str:
    for line in config.read_text().splitlines():
        if line.startswith("Host "):
            return line.split(None, 1)[1].strip()
    raise AssertionError(f"no Host stanza in {config}:\n{config.read_text()}")


def _ssh(
    config: Path,
    alias: str,
    command: Sequence[str],
    *,
    stdin: bytes | None = None,
    check: bool = True,
    extra: Sequence[str] = (),
    timeout: float = SSH_TIMEOUT,
    multiplexed: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """``ssh`` through a generated stanza, multiplexing off unless asked.

    ``multiplexed`` is for the one test whose subject *is* the multiplexing
    key; everything else here asserts on what the ProxyCommand does, and a
    live master would answer without running it.
    """
    argv = [
        "ssh",
        "-F",
        str(config),
        "-o",
        "BatchMode=yes",
        *(() if multiplexed else NO_MULTIPLEXING),
        *extra,
        alias,
        *command,
    ]
    completed = subprocess.run(
        argv, input=stdin, capture_output=True, timeout=timeout, check=False
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"{shlex.join(argv)} exited {completed.returncode}\n"
            f"{completed.stderr.decode(errors='replace')}"
        )
    return completed


def test_remote_command_returns_output(ssh_config: tuple[Path, str]) -> None:
    """The baseline: a command runs in the pod and its stdout comes back."""
    config, alias = ssh_config
    result = _ssh(config, alias, ["echo", "podbench-e2e-hello"])
    assert result.stdout.decode().strip() == "podbench-e2e-hello"


def test_a_podbench_verb_resolves_on_sshds_own_path(
    ssh_config: tuple[Path, str],
) -> None:
    """The single file #47 left the image, exercised the only way it matters.

    sshd is built ``UsePAM no`` (report 4.1), so ``ssh host <cmd>`` sources no
    profile and inherits none of the image's ``ENV PATH``. The agent's
    generated config now carries the container's ``PATH`` in its ``SetEnv``
    line, which puts the venv within reach of the first call below — so the
    second call takes ``PATH`` back down to what sshd would have supplied on
    its own, which is where ``/usr/local/bin/podbench`` is the only thing that
    resolves the verb. Without it this test would pass with the shim deleted,
    exactly as a ``kubectl exec`` always would.

    ``pids`` rather than ``--version``: it is the verb the README names for
    this path, and its table proves the shim reached the real CLI rather than
    some other program of the same name.
    """
    config, alias = ssh_config
    result = _ssh(config, alias, ["podbench", "pids"])
    listing = result.stdout.decode()
    assert "PID" in listing and "CMDLINE" in listing, listing

    without_the_venv = _ssh(
        config,
        alias,
        [
            "env",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "podbench",
            "pids",
        ],
    )
    listing = without_the_venv.stdout.decode()
    assert "PID" in listing and "CMDLINE" in listing, listing


def test_no_port_forward_and_no_pod_ip_are_involved(
    ssh_config: tuple[Path, str],
) -> None:
    """The product claim, asserted on the artefact the user is handed.

    "Reached only through the kubeconfig" is the brief's headline, and the only
    place it can be checked cheaply is the generated config: a ProxyCommand
    that is a ``kubectl exec``, a HostName that is a pod name rather than an
    address, and no port-forward anywhere.
    """
    config, _ = ssh_config
    text = config.read_text()
    assert "ProxyCommand" in text
    assert " exec " in text
    assert "port-forward" not in text
    # -e is what keeps fd 2 open; its absence is the §3.1 failure. Asserted on
    # the emitted config as well as behaviourally below, because this is the
    # line a tidy-up would touch.
    assert "sshd -i -e " in text or "sshd -i -e\n" in text


def test_megabyte_stream_is_byte_identical(ssh_config: tuple[Path, str]) -> None:
    """A >1 MB payload survives the round trip unchanged.

    Seeded rather than ``os.urandom``, so a failure can be reproduced exactly.
    The comparison is on bytes: the fd-2 teardown of §3.1 truncates silently
    with ``rc=0``, so a check on the exit code alone passes while the stream is
    short.
    """
    config, alias = ssh_config
    payload = random.Random(0xB0DBE).randbytes(STREAM_BYTES)
    result = _ssh(config, alias, ["cat"], stdin=payload)
    assert len(result.stdout) == len(payload), (
        f"stream truncated: sent {len(payload)} bytes, got back "
        f"{len(result.stdout)} with rc={result.returncode}"
    )
    assert result.stdout == payload


def test_the_seat_carries_vscode_machine_settings(
    ssh_config: tuple[Path, str],
) -> None:
    """The agent's machine settings are in the *session's* own ``$HOME``.

    Asserted over ssh rather than over ``kubectl exec``, and it rides this
    module's fixture for that reason: ``~`` in an ssh session is the home the
    passwd record names, which is the one a Remote-SSH client unpacks
    vscode-server into and the only one the settings are read from. A
    ``kubectl exec`` shell would answer about the container's ``$HOME``, which
    is a different directory on a ``podbench dev`` sidecar.

    Without these, File -> Open Folder -> ``/`` walks ``/proc/<pid>/root`` into
    every other container in the pod and OOMs a seat that cannot be restarted.
    """
    config, alias = ssh_config
    result = _ssh(
        config, alias, ["cat", "$HOME/.vscode-server/data/Machine/settings.json"]
    )
    settings = cast("dict[str, Any]", json.loads(result.stdout.decode()))
    assert settings["search.exclude"]["**/proc/**"] is True
    assert settings["files.watcherExclude"]["**/sys/**"] is True
    assert settings["search.followSymlinks"] is False


def test_second_concurrent_session_works(ssh_config: tuple[Path, str]) -> None:
    """Two independent transports over the same container at once.

    Independent, not multiplexed: each opens its own ``kubectl exec``, which is
    what an editor doing a bootstrap while the user holds a shell actually
    looks like.
    """
    config, alias = ssh_config
    argv = [
        "ssh",
        "-F",
        str(config),
        "-o",
        "BatchMode=yes",
        *NO_MULTIPLEXING,
        alias,
    ]
    first = subprocess.Popen(
        [*argv, "sh", "-c", "sleep 3; echo first"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second = subprocess.Popen(
        [*argv, "sh", "-c", "sleep 3; echo second"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        first_out, first_err = first.communicate(timeout=SSH_TIMEOUT)
        second_out, second_err = second.communicate(timeout=SSH_TIMEOUT)
    finally:
        for process in (first, second):
            if process.poll() is None:  # pragma: no cover - only on a hang
                process.kill()
    assert first.returncode == 0, first_err.decode(errors="replace")
    assert second.returncode == 0, second_err.decode(errors="replace")
    assert first_out.decode().strip() == "first"
    assert second_out.decode().strip() == "second"


SECOND_SEAT = f"{FIRST_SEAT[:-1]}2"
"""The name a second ``--new`` attach takes. Ephemeral container names are
burnt for the pod's lifetime, so this is a fact about the pod rather than a
preference."""

SEAT_MARKER = "/tmp/which-seat"
"""Where each seat is told its own name, so an ssh session can say which
container it landed in.

A marker file rather than ``hostname``: two seats in one pod share the UTS
namespace and answer identically, which is the very reason the two stanzas
collided. The seats' writable layers are the thing that genuinely differs."""


@pytest.fixture(scope="module")
def two_seats(
    kubectl: KubectlCli,
    podbench: PodbenchCli,
    target: str,
    podbench_image: str,
    ssh_identity: tuple[Path, str],
    ssh_config: tuple[Path, str],
) -> tuple[tuple[Path, str], tuple[Path, str]]:
    """Two live seats on one pod, each having written its own name to a file.

    Depends on ``ssh_config`` so the first seat is the one every other test in
    this module already uses, and ``--new`` then lands the second beside it -
    which is what a multi-container pod and the gid correction both do.
    """
    identity, _ = ssh_identity
    podbench.run(
        "attach",
        target,
        "--new",
        "--identity",
        str(identity),
        "--image",
        podbench_image,
        *podbench.common(),
        timeout=420,
    )
    config_dir = podbench.config_dir
    assert config_dir is not None
    pod = PodRef(kubectl.namespace, target)
    seats: list[tuple[Path, str]] = []
    for seat in (FIRST_SEAT, SECOND_SEAT):
        path = ssh_config_path(config_dir, pod, seat)
        assert path.is_file(), f"no ssh config for {seat}"
        kubectl.run(
            "exec", target, "-c", seat, "--", "sh", "-c", f"echo {seat} > {SEAT_MARKER}"
        )
        seats.append((path, _host_alias(path)))
    return seats[0], seats[1]


def test_two_seats_on_one_pod_do_not_share_a_control_master(
    two_seats: tuple[tuple[Path, str], tuple[Path, str]],
) -> None:
    """The finding-10 acceptance, with ControlMaster left at its default.

    Every other ssh in this file disables multiplexing, because it is testing
    what the ProxyCommand does and a live master would bypass it. This one must
    not: the defect *is* the multiplexing key. ``%C`` hashes the resolved
    ``HostName``, which both stanzas set to the pod's name, so on a bare ``%C``
    the second alias reuses the first seat's master and answers with the first
    seat's marker - **without repeating the host-key check**, since a
    multiplexed session never does.

    Ordered rather than parallel on purpose: the first ssh has to establish a
    master for the second one to be able to ride it.
    """
    (first_config, first_alias), (second_config, second_alias) = two_seats
    assert _control_path(first_config) != _control_path(second_config)
    try:
        for config, alias, seat in (
            (first_config, first_alias, FIRST_SEAT),
            (second_config, second_alias, SECOND_SEAT),
        ):
            result = _ssh(config, alias, ["cat", SEAT_MARKER], multiplexed=True)
            assert result.stdout.decode().strip() == seat, (
                f"{alias} answered from another container: multiplexing joined "
                "it to a seat whose host key it never checked"
            )
    finally:
        for config, alias in (
            (first_config, first_alias),
            (second_config, second_alias),
        ):
            subprocess.run(
                ["ssh", "-F", str(config), "-O", "exit", alias],
                capture_output=True,
                timeout=SSH_TIMEOUT,
                check=False,
            )


def _control_path(config: Path) -> str:
    for line in config.read_text().splitlines():
        if line.strip().startswith("ControlPath "):
            return line.split(None, 1)[1].strip()
    raise AssertionError(f"no ControlPath in {config}:\n{config.read_text()}")


def test_fd2_teardown_truncates_the_exec_stream(
    kubectl: KubectlCli, target: str
) -> None:
    """The mechanism behind ``-e``, isolated with no sshd in sight.

    Report §3.1 pinned the cause of the transport's most misleading failure
    with exactly this pair of commands: replacing fd 2 in a ``kubectl exec``'d
    process makes containerd tear down the *whole* CRI exec session, so the
    second line is lost and the command still exits 0.

    This is here rather than only in the sshd test because it is the general
    fact. Anything podbench ever runs over ``kubectl exec`` — the agent, the
    capability probe, a future streaming helper — inherits it, and a test that
    only exercised sshd would let the next feature rediscover it.
    """
    # The delay is load-bearing: it forces the second write to happen after the
    # child has already replaced its stderr, which is when the teardown fires.
    # A single stdin buffer full of both lines would arrive before the child
    # had run at all, and both cases would pass.
    script = "( echo one; sleep 4; echo two )"

    piped = subprocess.run(
        f"{script} | "
        + shlex.join(
            kubectl.argv(
                "exec", "-i", target, "-c", "app", "--", "sh", "-c", "exec cat"
            )
        ),
        shell=True,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert piped.stdout.splitlines() == ["one", "two"], piped.stderr

    silenced = subprocess.run(
        f"{script} | "
        + shlex.join(
            kubectl.argv(
                "exec",
                "-i",
                target,
                "-c",
                "app",
                "--",
                "sh",
                "-c",
                "exec 2>/dev/null; exec cat",
            )
        ),
        shell=True,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert "two" not in silenced.stdout, (
        "closing fd 2 no longer tears down the CRI exec stream on this "
        "cluster. That is the whole reason `sshd -i -e` is mandatory (report "
        "§3.1) and the unresolved variable behind R1. Do not delete the `-e` "
        "requirement on the strength of this: find out what changed — "
        "containerd version, CRI, kubectl — and record it, because a cluster "
        "where the teardown still fires will still break.\n"
        f"stdout={silenced.stdout!r} rc={silenced.returncode}"
    )
    # Silently, with rc=0. This is the half that makes it dangerous.
    assert silenced.returncode == 0


def test_transport_dies_without_dash_e(
    ssh_config: tuple[Path, str], tmp_path: Path
) -> None:
    """The consequence: strip ``-e`` from the ProxyCommand and ssh fails.

    The config under test is the *generated* one with a single token removed,
    so this cannot drift away from what podbench actually emits.

    Report §4.1 asks for this test by name. Its value is not that it protects a
    user — nobody edits a generated file — but that it keeps the reason in the
    repository: `-e` reads like a logging flag, `-o LogLevel=ERROR` already
    silences the logs, and the obvious tidy-up is to drop it. Then the
    transport fails at KEX with ``Connection to UNKNOWN port 65535: Broken
    pipe`` and a week goes into the network.

    R1 is unresolved: S2 ran without ``-e`` for an entire spike. If this test
    ever fails, the honest response is to find the variable that flips it —
    not to delete the flag.
    """
    config, alias = ssh_config
    original = config.read_text()
    broken_text = original.replace("sshd -i -e ", "sshd -i ")
    assert broken_text != original, (
        "the generated ProxyCommand no longer contains `sshd -i -e`, so this "
        f"regression test has nothing to break:\n{original}"
    )
    broken = tmp_path / "no-dash-e.conf"
    broken.write_text(broken_text)
    broken.chmod(0o600)

    result = _ssh(
        broken,
        alias,
        ["echo", "should-not-arrive"],
        check=False,
        extra=("-o", "ConnectTimeout=30"),
        timeout=180,
    )
    assert result.returncode != 0, (
        "ssh succeeded with `-e` removed from sshd's argv. See the note in "
        "this test's docstring: report §3.1 measured the fd-2 teardown, R1 "
        "records that S2 did not hit it, and this is the observation that "
        "would settle it. Record the cluster, containerd and kubectl versions "
        "before changing podbench.\n"
        f"stdout={result.stdout!r}"
    )
    assert b"should-not-arrive" not in result.stdout
