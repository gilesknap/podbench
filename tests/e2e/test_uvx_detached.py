"""Issue #1 as a regression: a seat outlives the process that attached it.

The whole premise of ``uvx podbench attach`` is that the launcher is a *tool*,
not a daemon. uvx materialises an ephemeral environment, runs the command, and
tears the environment down; a moment later there is no podbench on the machine
at all. Everything the user then does — ``ssh <alias>``, VS Code Remote-SSH,
``scp`` — has to work with nothing but ssh, the generated stanza and kubectl.

That holds today because :func:`podbench.sshcfg.proxy_command` emits a bare
``kubectl … exec -i … sshd -i -e …``: the launcher writes a config file and
exits, and the config it wrote names no podbench binary. Nothing in the code
says so, and the obvious tidy-up — routing the ProxyCommand through
``podbench proxy`` so the flags live in one place instead of in a quoted string
— would break uvx while leaving every other test green, because a developer
running from a clone has podbench on PATH.

So the three assertions here are the three ways that would show up:

* the emitted ProxyCommand invokes kubectl directly and ends in ``sshd``;
* ``attach`` leaves no process of its own running;
* ``ssh <alias> true`` connects with the launcher scrubbed off ``PATH``.

The last one is the real claim. It is *not* enough to observe that the attaching
subprocess has exited, because a ProxyCommand naming ``podbench`` would still
find it on a developer's PATH — the seat has to work in an environment where
podbench cannot be found at all, which is what uvx leaves behind.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from .conftest import KubectlCli, PodbenchCli

# The pod name lands inside the ProxyCommand (`kubectl … exec -i <pod> …`), and
# the scan below rejects a handful of substrings anywhere in that string — so a
# target called `uvx-target` would fail this module's own assertion. Keep the
# name clear of every entry in FORBIDDEN_IN_PROXY.
TARGET_POD = "detached-target"

# Same shape as S1's target and for the same reason: this test cares about the
# transport the launcher hands over, not about what it is attached to.
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

SSH_TIMEOUT = 120.0

NO_MULTIPLEXING = (
    "-o",
    "ControlMaster=no",
    "-o",
    "ControlPath=none",
)
"""A live ControlMaster bypasses the ProxyCommand entirely, so a test about
what the ProxyCommand contains has to refuse one (S1 makes the same point)."""

LAUNCHER_NAMES = ("podbench", "kubectl-podbench")
"""Every name the launcher can appear under on ``PATH``.

``podbench`` is the console script ``pyproject.toml`` declares. ``kubectl-podbench``
is the kubectl plugin alias this project shipped through 0.1.0-alpha.6 and has since
dropped (ADR 0003) — it stays in the scrub because anyone who installed one of those
releases still has the file on ``PATH``, and a ProxyCommand reaching for it would
otherwise pass this test for the wrong reason."""

FORBIDDEN_IN_PROXY = (
    "uvx",
    "uv run",
    "uv tool",
    "-m podbench",
    "kubectl-podbench",
    "kubectl podbench",
)
"""Substrings that would each mean the seat needs podbench to reconnect.

Plain ``podbench`` is not in this list and cannot be: the scratch namespace is
``podbench-e2e-…`` and the ephemeral container is called ``podbench``, so the
word is legitimately all over a correct ProxyCommand. The structural assertion
on argv[0] and on the remote command is what actually pins the property.

For the same reason the scan is a substring match over the whole string, so
``TARGET_POD`` has to avoid every entry here."""


@pytest.fixture(scope="module")
def target(kubectl: KubectlCli) -> str:
    """A plain root pod, Ready, in the module's scratch namespace."""
    kubectl.run("create", "-f", "-", stdin=json.dumps(TARGET_MANIFEST))
    kubectl.wait(f"pod/{TARGET_POD}", "condition=Ready", timeout=300)
    return TARGET_POD


@pytest.fixture(scope="module")
def seat(
    kubectl: KubectlCli,
    podbench: PodbenchCli,
    target: str,
    podbench_image: str,
    ssh_identity: tuple[Path, str],
) -> tuple[Path, str]:
    """Attach a seat, then let the attaching process die. Returns (config, alias).

    ``PodbenchCli.run`` is a ``subprocess.run``, so by the time this fixture
    returns the launcher has exited and been reaped: what follows is talking to
    a seat whose creator no longer exists, which is exactly the post-uvx state.
    Nothing is torn down afterwards — an ephemeral container is permanent by
    construction and goes away with the namespace.
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
    path = config_dir / "config.d" / f"{kubectl.namespace}-{target}.conf"
    assert path.is_file(), f"attach wrote no ssh config:\n{result.stdout}"
    return path, _host_alias(path)


@pytest.fixture(scope="module")
def launcher_free_env(
    kubectl_bin: str, tmp_path_factory: pytest.TempPathFactory
) -> Mapping[str, str]:
    """This process's environment, with the launcher taken off ``PATH``.

    Only ``PATH`` is altered: ``KUBECONFIG``, ``HOME`` and whatever an exec
    credential plugin needs all have to survive, because the ProxyCommand is a
    real kubectl call and stripping the environment wholesale would fail for
    reasons that have nothing to do with the property under test.

    Directories holding a launcher entry point are dropped rather than filtered
    per-binary, then a tripwire directory is prepended so that a ProxyCommand
    which *does* reach for podbench fails with a sentence saying so instead of
    an anonymous ``not found``.
    """
    tripwire = tmp_path_factory.mktemp("podbench-tripwire")
    for name in LAUNCHER_NAMES:
        shim = tripwire / name
        shim.write_text(
            "#!/bin/sh\n"
            'echo "$0: the ssh ProxyCommand invoked the podbench launcher; a '
            'seat attached by uvx would be dead here (issue #1)" >&2\n'
            "exit 127\n"
        )
        shim.chmod(0o755)

    kept = [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and not _holds_launcher(entry)
    ]
    path = os.pathsep.join([str(tripwire), *kept])
    if shutil.which(kubectl_bin, path=path) is None:
        pytest.skip(
            f"{kubectl_bin!r} shares a directory with the podbench launcher, so "
            "PATH cannot be scrubbed of one without losing the other"
        )
    environment = dict(os.environ)
    environment["PATH"] = path
    return environment


def _holds_launcher(entry: str) -> bool:
    directory = Path(entry)
    return any((directory / name).exists() for name in LAUNCHER_NAMES)


def _host_alias(config: Path) -> str:
    return _directive(config, "Host")


def _directive(config: Path, keyword: str) -> str:
    """The value of one ssh config keyword, from the stanza attach wrote."""
    for line in config.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{keyword} "):
            return stripped[len(keyword) :].strip()
    raise AssertionError(f"no {keyword} line in {config}:\n{config.read_text()}")


def test_proxy_command_invokes_kubectl_and_not_podbench(
    seat: tuple[Path, str], kubectl_bin: str
) -> None:
    """The stanza the user keeps names kubectl and sshd, and nothing else.

    Asserted structurally — argv[0] is the kubectl binary, the remote command
    after ``--`` is sshd — because that is the shape that survives the launcher
    being uninstalled. A ProxyCommand of ``podbench proxy …`` would read as a
    tidy-up, pass every other test in this suite, and make ``uvx podbench
    attach`` produce a seat that dies the moment uvx cleans up.
    """
    config, _ = seat
    proxy = _directive(config, "ProxyCommand")
    argv = shlex.split(proxy)
    assert Path(argv[0]).name == Path(kubectl_bin).name, (
        f"the ProxyCommand no longer starts with kubectl: {argv[0]!r}"
    )
    assert "--" in argv, proxy
    separator = argv.index("--")
    local, remote = argv[:separator], argv[separator + 1 :]
    assert "exec" in local, local
    assert remote and Path(remote[0]).name == "sshd", (
        f"the remote command is no longer sshd: {remote!r}"
    )
    for token in (*FORBIDDEN_IN_PROXY, sys.executable):
        assert token not in proxy, (
            f"the ProxyCommand references {token!r}, so reconnecting needs "
            "something other than kubectl. See this module's docstring: that "
            f"is what breaks `uvx podbench attach`.\n{proxy}"
        )


def test_attach_leaves_no_process_running(
    seat: tuple[Path, str], namespace: str
) -> None:
    """``attach`` is a tool, not a daemon: nothing of it is still alive.

    A helper left running — a port-forward, a watch, a reconnect loop — would
    make the seat depend on the machine that created it, which is the same
    failure as a podbench in the ProxyCommand arriving by a different route.
    Matched on the scratch namespace, which is unique to this module and
    appears in the launcher's own argv, so no other test's kubectl can be
    mistaken for one of ours.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        pytest.skip("no /proc on this platform, so processes cannot be enumerated")
    survivors: list[str] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:  # pragma: no cover - the process exited mid-scan
            continue
        cmdline = raw.replace(b"\0", b" ").decode(errors="replace").strip()
        if namespace in cmdline and "podbench" in cmdline:
            survivors.append(cmdline)
    assert not survivors, (
        "attach left a process behind, so the seat is not self-contained:\n"
        + "\n".join(survivors)
    )


def test_ssh_connects_with_the_launcher_off_path(
    seat: tuple[Path, str], launcher_free_env: Mapping[str, str], tmp_path: Path
) -> None:
    """The claim itself: ``ssh <alias> true`` works with no podbench anywhere.

    Run from a scratch directory as well as a scrubbed ``PATH``, so a
    ProxyCommand that had come to depend on the repo checkout — a relative
    path, a ``python -m podbench`` resolved from the working directory — fails
    here rather than passing by accident on a developer's machine.

    ``BatchMode=yes`` keeps a broken transport from waiting on a passphrase
    prompt, and multiplexing is off so this genuinely opens a fresh
    ``kubectl exec`` rather than reusing a master from an earlier test.
    """
    config, alias = seat
    argv = [
        "ssh",
        "-F",
        str(config),
        "-o",
        "BatchMode=yes",
        *NO_MULTIPLEXING,
        alias,
        "true",
    ]
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=SSH_TIMEOUT,
        check=False,
        cwd=tmp_path,
        env=dict(launcher_free_env),
    )
    assert completed.returncode == 0, (
        f"{shlex.join(argv)} exited {completed.returncode} with podbench off "
        "PATH. A seat attached by `uvx podbench attach` is exactly this: the "
        "launcher is gone and only the generated stanza remains.\n"
        f"{completed.stderr}"
    )
