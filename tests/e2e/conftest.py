"""Gating and cluster plumbing for the e2e suite.

Two things live here and nothing else does.

**The gate.** These tests mutate a real cluster, so the default ``pytest``
invocation must never reach them: CI runs the unit suite on four Python
versions and a contributor runs it on a laptop, and neither has a cluster.
Collection therefore proceeds normally — so ``--collect-only`` still lists what
exists — and every item under this directory is skipped with a reason unless
both halves of the opt-in hold: ``PODBENCH_E2E=1`` in the environment *and* a
kubectl that can actually reach an API server. Asking for the env var alone
would turn "I forgot to start kind" into a wall of confusing failures.

**The fixtures.** A scratch namespace per test module, a verified image
reference, and a kubectl helper. Everything creates with a random suffix and
deletes in a ``finally``, because the one rule this repo has about clusters is
that podbench never leaves anything behind (CLAUDE.md: "Never mutate a cluster
outside a scratch namespace").

The namespace is created *per module* rather than per session: S5 has to label
its own namespace ``pod-security.kubernetes.io/enforce=restricted``, which
would change what every other test is admitted to do if they shared one.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

import pytest

from podbench.kubectl import Kubectl
from podbench.model import DEFAULT_IMAGE, IMAGE_ENV, as_dict

__all__ = [
    "CONTEXT_ENV",
    "E2E_ENV",
    "KUBECTL_ENV",
    "NAMESPACE_PREFIX",
    "KubectlCli",
    "PodbenchCli",
]

E2E_ENV = "PODBENCH_E2E"
"""The opt-in. Anything in :data:`TRUTHY`; unset means "not this run"."""

KUBECTL_ENV = "PODBENCH_E2E_KUBECTL"
CONTEXT_ENV = "PODBENCH_E2E_CONTEXT"
"""Which cluster to use. Separate from ``KUBECONFIG``/``kubectl config
current-context`` on purpose: a developer's default context is usually a real
cluster, and these tests create pods with ``CAP_SYS_PTRACE``."""

NAMESPACE_PREFIX = "podbench-e2e-"
"""Every namespace this suite creates starts with it, so a crashed run can be
cleaned up with one glob and nothing else can be caught by it."""

TRUTHY = frozenset({"1", "true", "yes", "on"})

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent

DEFAULT_TIMEOUT = 180.0
"""Wall-clock ceiling on one kubectl call. Generous because the demo apps pull
images and ``apt-get install gcc`` in an initContainer, and a timeout that
fires during a cold image pull reads as a podbench bug."""

IMAGE_SMOKE_TIMEOUT = 300.0
"""How long to wait for the podbench image to pull before deciding it is not
available. Long enough for a cold multi-arch pull on a slow node."""


# -- the gate ---------------------------------------------------------------


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY


def kubectl_binary() -> str:
    return os.environ.get(KUBECTL_ENV) or "kubectl"


def kubectl_context() -> str | None:
    return os.environ.get(CONTEXT_ENV) or None


_reachability: dict[str, str | None] = {}


def _probe_cluster(binary: str, context: str | None) -> str | None:
    """``None`` when the API server answers, else why it did not."""
    if shutil.which(binary) is None:
        return f"{binary!r} is not on PATH"
    argv = [binary]
    if context is not None:
        argv += ["--context", context]
    argv += ["get", "--raw", "/version", "--request-timeout=10s"]
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:  # pragma: no cover
        return f"{shlex.join(argv)} did not complete: {error}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        return f"{shlex.join(argv)} failed: {detail[0] if detail else 'no output'}"
    return None


def _cluster_reason(binary: str, context: str | None) -> str | None:
    key = f"{binary}\x00{context}"
    if key not in _reachability:
        _reachability[key] = _probe_cluster(binary, context)
    return _reachability[key]


def gate_reason() -> str | None:
    """Why the e2e suite is not running, or ``None`` when it is."""
    if not _truthy(os.environ.get(E2E_ENV)):
        return (
            f"e2e suite is opt-in: set {E2E_ENV}=1 and point "
            f"{CONTEXT_ENV}/KUBECONFIG at a scratch cluster"
        )
    unreachable = _cluster_reason(kubectl_binary(), kubectl_context())
    if unreachable is not None:
        return f"{E2E_ENV} is set but no cluster is reachable: {unreachable}"
    return None


def pytest_configure(config: pytest.Config) -> None:
    """Register the marker locally.

    ``filterwarnings = "error"`` in pyproject.toml turns pytest's
    unknown-mark warning into a collection error, so the marker has to be
    registered whether or not the central ini has been updated yet.
    """
    config.addinivalue_line(
        "markers",
        "e2e: needs a real cluster; skipped unless PODBENCH_E2E=1 (see "
        "tests/e2e/README.md)",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark, then skip, everything under this directory.

    A skip rather than ``collect_ignore``: the reason is then printed with
    ``-rs`` and "the e2e suite did not run" is visible in CI output rather than
    being indistinguishable from "there are no e2e tests".

    The path filter matters because this hook is a session-wide one even though
    the conftest declaring it is scoped to a directory — without it, a skip
    intended for the cluster suite would land on every unit test.
    """
    reason = gate_reason()
    mark_e2e = pytest.mark.e2e
    skip = None if reason is None else pytest.mark.skip(reason=reason)
    for item in items:
        path = cast(Path | None, getattr(item, "path", None))
        if path is None or HERE not in path.parents:
            continue
        item.add_marker(mark_e2e)
        if skip is not None:
            item.add_marker(skip)


# -- kubectl ----------------------------------------------------------------


class ClusterCommandError(RuntimeError):
    """A cluster command that failed, with both streams kept.

    ``subprocess.CalledProcessError`` hides stderr behind an attribute pytest
    does not print; an e2e failure whose kubectl error is invisible costs a
    re-run to diagnose.
    """

    def __init__(
        self, argv: Sequence[str], completed: subprocess.CompletedProcess[str]
    ) -> None:
        self.argv = tuple(argv)
        self.returncode = completed.returncode
        self.stdout = completed.stdout or ""
        self.stderr = completed.stderr or ""
        super().__init__(
            f"{shlex.join(argv)} exited {self.returncode}\n"
            f"--- stdout ---\n{self.stdout}\n--- stderr ---\n{self.stderr}"
        )


@dataclass(frozen=True)
class KubectlCli:
    """Everything the e2e tests do to a cluster, bound to one namespace.

    Deliberately *not* :class:`podbench.kubectl.Kubectl`: the tests must be able
    to observe podbench's own client misbehaving, and a harness that shared the
    code under test could not. This one adds the two things the product does not
    need — a wall-clock timeout on every call, and raw manifest application.
    """

    namespace: str
    binary: str = field(default_factory=kubectl_binary)
    context: str | None = field(default_factory=kubectl_context)

    def argv(self, *args: str, namespaced: bool = True) -> list[str]:
        argv = [self.binary]
        if self.context is not None:
            argv += ["--context", self.context]
        if namespaced:
            argv += ["-n", self.namespace]
        return argv + list(args)

    def run(
        self,
        *args: str,
        stdin: str | None = None,
        check: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        namespaced: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        argv = self.argv(*args, namespaced=namespaced)
        completed = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and completed.returncode != 0:
            raise ClusterCommandError(argv, completed)
        return completed

    def run_binary(
        self,
        *args: str,
        stdin: bytes | None = None,
        check: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> subprocess.CompletedProcess[bytes]:
        """Like :meth:`run`, without text decoding.

        The 1 MB transport test compares bytes: decoding them would hide
        exactly the corruption it is looking for.
        """
        argv = self.argv(*args)
        completed = subprocess.run(
            argv, input=stdin, capture_output=True, timeout=timeout, check=False
        )
        if check and completed.returncode != 0:
            raise ClusterCommandError(
                argv,
                subprocess.CompletedProcess(
                    argv,
                    completed.returncode,
                    (completed.stdout or b"").decode(errors="replace"),
                    (completed.stderr or b"").decode(errors="replace"),
                ),
            )
        return completed

    def json(self, *args: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        completed = self.run(*args, "-o", "json", timeout=timeout)
        parsed: object = json.loads(completed.stdout)
        if not isinstance(parsed, dict):
            raise ValueError(f"{shlex.join(self.argv(*args))} did not return an object")
        return cast(dict[str, Any], parsed)

    def apply(self, manifest: Path | str) -> None:
        """Apply a manifest file, or a literal document, into this namespace."""
        if isinstance(manifest, Path):
            self.run("apply", "-f", str(manifest))
        else:
            self.run("apply", "-f", "-", stdin=manifest)

    def exec(
        self,
        pod: str,
        argv: Sequence[str],
        *,
        container: str | None = None,
        stdin: str | None = None,
        check: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        args = ["exec"]
        if stdin is not None:
            args.append("-i")
        if container is not None:
            args += ["-c", container]
        # No `-t`, ever. From a script kubectl silently degrades to non-tty and
        # looks fine; with a real tty on the ssh ProxyCommand the client hangs
        # forever (report 3.19). The suite that is supposed to catch that
        # regression must not itself normalise the flag away.
        args += [pod, "--"]
        return self.run(*args, *argv, stdin=stdin, check=check, timeout=timeout)

    def write_file(
        self, pod: str, path: str, text: str, *, container: str | None = None
    ) -> None:
        """Put a file into a container, without needing a shell in the image."""
        quoted = shlex.quote(path)
        self.exec(
            pod,
            ["sh", "-c", f"mkdir -p $(dirname {quoted}) && cat > {quoted}"],
            container=container,
            stdin=text,
        )

    def wait(
        self, resource: str, condition: str, *, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        self.run(
            "wait",
            resource,
            f"--for={condition}",
            f"--timeout={int(timeout)}s",
            timeout=timeout + 30,
        )

    def describe_everything(self) -> str:
        """A dump for a failure message. Never raises."""
        completed = self.run("get", "all,pods", "-o", "wide", check=False, timeout=60)
        events = self.run(
            "get", "events", "--sort-by=.lastTimestamp", check=False, timeout=60
        )
        return f"{completed.stdout}\n{events.stdout}"


@dataclass(frozen=True)
class PodbenchCli:
    """The launcher, run the way a user runs it.

    A subprocess rather than calling :func:`podbench.launcher.main` in-process,
    because the CLI's contract includes its exit code and its stdout, and an
    in-process call would let a ``SystemExit`` or a stray ``print`` pass
    unnoticed. ``python -m podbench`` rather than the ``kubectl-podbench``
    plugin so the suite does not depend on the package being installed on PATH.
    """

    namespace: str
    binary: str = field(default_factory=kubectl_binary)
    context: str | None = field(default_factory=kubectl_context)
    config_dir: Path | None = None

    def run(
        self,
        *args: str,
        check: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = [sys.executable, "-m", "podbench", *args]
        environment = dict(os.environ)
        if env is not None:
            environment.update(env)
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
            cwd=REPO_ROOT,
        )
        if check and completed.returncode != 0:
            raise ClusterCommandError(argv, completed)
        return completed

    def common(self) -> list[str]:
        """The flags every launcher verb takes, pointed at this namespace."""
        args = ["-n", self.namespace, "--kubectl", self.binary]
        if self.context is not None:
            args += ["--context", self.context]
        if self.config_dir is not None:
            args += ["--config-dir", str(self.config_dir)]
        return args


# -- fixtures ---------------------------------------------------------------


@pytest.fixture(scope="session")
def kubectl_bin() -> str:
    return kubectl_binary()


@pytest.fixture(scope="session")
def kube_context() -> str | None:
    return kubectl_context()


class NamespaceFactory(Protocol):
    """Make a scratch namespace whose deletion is already guaranteed."""

    def __call__(
        self, *, labels: Mapping[str, str] | None = None, suffix: str = ""
    ) -> str:
        """Create ``podbench-e2e-<suffix><random>`` and return its name."""
        ...


@pytest.fixture(scope="session")
def namespace_factory(
    kubectl_bin: str, kube_context: str | None
) -> Iterator[NamespaceFactory]:
    """Create scratch namespaces; delete every one of them at session end.

    Cleanup is registered before the namespace is known to exist and runs in a
    ``finally``, so a test that dies mid-way still takes its namespace with it.
    Deletion does not wait: the API server has accepted the delete by the time
    this returns, and blocking on ~10 s of termination per namespace would add
    a minute to a suite whose whole point is to be run often.
    """
    created: list[str] = []

    def create(*, labels: Mapping[str, str] | None = None, suffix: str = "") -> str:
        name = f"{NAMESPACE_PREFIX}{suffix}{secrets.token_hex(4)}"
        created.append(name)
        manifest = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": name, "labels": dict(labels or {})},
        }
        argv = [kubectl_bin]
        if kube_context is not None:
            argv += ["--context", kube_context]
        argv += ["create", "-f", "-"]
        completed = subprocess.run(
            argv,
            input=json.dumps(manifest),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            raise ClusterCommandError(argv, completed)
        return name

    try:
        yield create
    finally:
        for name in created:
            argv = [kubectl_bin]
            if kube_context is not None:
                argv += ["--context", kube_context]
            argv += [
                "delete",
                "namespace",
                name,
                "--wait=false",
                "--ignore-not-found",
            ]
            subprocess.run(
                argv, capture_output=True, text=True, timeout=60, check=False
            )


@pytest.fixture(scope="module")
def namespace(namespace_factory: NamespaceFactory) -> str:
    """One scratch namespace per test module, with no Pod Security label."""
    return namespace_factory()


@pytest.fixture(scope="module")
def kubectl(namespace: str) -> KubectlCli:
    return KubectlCli(namespace)


@pytest.fixture(scope="module")
def kube(namespace: str, kubectl_bin: str, kube_context: str | None) -> Kubectl:
    """podbench's *own* kubectl client, for the API-level assertions."""
    return Kubectl(namespace, context=kube_context, binary=kubectl_bin)


@pytest.fixture(scope="module")
def podbench(namespace: str, tmp_path_factory: pytest.TempPathFactory) -> PodbenchCli:
    config_dir = tmp_path_factory.mktemp("podbench-client")
    return PodbenchCli(namespace, config_dir=config_dir)


@pytest.fixture(scope="session")
def image_ref() -> str:
    """The image under test, before anything has proved it can be pulled."""
    return os.environ.get(IMAGE_ENV) or DEFAULT_IMAGE


@pytest.fixture(scope="session")
def podbench_image(
    image_ref: str, namespace_factory: NamespaceFactory, kubectl_bin: str
) -> str:
    """``image_ref``, once a pod has run ``podbench --version`` from it.

    Every test here needs the image, and every one of them would otherwise fail
    with a different confusing symptom when it is missing — an ephemeral
    container stuck in ``ImagePullBackOff`` surfaces as a timeout waiting for
    ``state.running``, which reads as a podbench bug. The image is not
    published for every branch, so "not available" is a *skip*, named once,
    here.
    """
    namespace = namespace_factory(suffix="img-")
    cli = KubectlCli(namespace, binary=kubectl_bin)
    pod = "image-smoke"
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod},
        "spec": {
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "smoke",
                    "image": image_ref,
                    "imagePullPolicy": "IfNotPresent",
                    "args": ["--version"],
                }
            ],
        },
    }
    cli.run("create", "-f", "-", stdin=json.dumps(manifest))
    outcome = _await_image_smoke(cli, pod)
    if outcome is not None:
        pytest.skip(
            f"the podbench image {image_ref!r} could not be run in this "
            f"cluster, so the e2e suite has nothing to test ({outcome}). Set "
            f"{IMAGE_ENV} to an image that exists, or `kind load docker-image` "
            "it into the cluster first."
        )
    return image_ref


def _await_image_smoke(cli: KubectlCli, pod: str) -> str | None:
    """Poll the smoke pod. ``None`` means it ran; a string says what went wrong.

    Polled rather than left to ``kubectl wait``, because the interesting
    failure — the image does not exist — is a *terminal* one that ``wait``
    cannot recognise, and sitting out the full timeout on every run of a branch
    whose image has not been published yet is the difference between a suite
    people run and one they do not.
    """
    deadline = time.monotonic() + IMAGE_SMOKE_TIMEOUT
    last = "the pod never reported a status"
    while time.monotonic() < deadline:
        payload = cli.json("get", "pod", pod, timeout=60)
        status = as_dict(payload.get("status"))
        phase = status.get("phase")
        if phase == "Succeeded":
            return None
        if phase == "Failed":
            return f"the pod failed: {status.get('message') or 'no message'}"
        for entry in _as_list(status.get("containerStatuses")):
            waiting = as_dict(as_dict(as_dict(entry).get("state")).get("waiting"))
            reason = str(waiting.get("reason") or "")
            if reason in TERMINAL_PULL_REASONS:
                return f"{reason}: {waiting.get('message') or 'no message'}"
            if reason:
                last = reason
        time.sleep(2)
    return f"timed out after {IMAGE_SMOKE_TIMEOUT:.0f}s (last state: {last})"


TERMINAL_PULL_REASONS = frozenset(
    {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ImageInspectError"}
)
"""kubelet waiting reasons that will never resolve themselves."""


def _as_list(value: Any) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


@pytest.fixture(scope="session")
def ssh_identity(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """A throwaway ed25519 keypair, and its public half.

    Generated per run rather than reusing the developer's own key: podbench
    writes the public key into a container's ``authorized_keys``, and a test
    suite has no business putting a real identity there.
    """
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is not installed, so no key can be authorised")
    directory = tmp_path_factory.mktemp("podbench-ssh")
    private = directory / "id_ed25519"
    subprocess.run(
        [
            "ssh-keygen",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "podbench-e2e",
            "-f",
            str(private),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    private.chmod(0o600)
    return private, private.with_suffix(".pub").read_text().strip()


@pytest.fixture(scope="session")
def apps_dir() -> Path:
    """Where the demo target manifests live."""
    return HERE / "apps"
