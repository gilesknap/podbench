"""S4 as a regression: the edit -> relaunch -> visible-through-the-Service loop.

This is the brief's headline demo, and the spike found that almost nothing
about it works by default. ``kubectl debug --copy-to`` strips every label, so
the clone is invisible to the Service and the user watches the *old* response
forever with no diagnostic (report §3.5). A probe-less clone joins the
endpointslice before anything is listening and blackholes about half the
requests (§3.6). A merge patch on a Service selector unions the maps instead of
replacing them (§4.4). A naive relaunch wrapper reports success for a process
that died with ``EADDRINUSE`` (§3.16).

The whole loop is therefore exercised end to end here, against a Service, with
the assertions placed on the outcomes those four findings corrupt: what the
Service returns, what the selector contains after a cutover, and whether the
idled app container still carries probes that would kill the pod.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from podbench.dev import DevPod, create_dev_pod, delete_dev_pod
from podbench.kubectl import Kubectl
from podbench.model import as_dict

from .conftest import KubectlCli

DEPLOYMENT = "demo-service"
SERVICE = "demo-service"
CONFIGMAP = "demo-service-src"
APP_PORT = 8080

WORKSPACE = "/workspace"
ORIGIN_REPO = f"{WORKSPACE}/origin"
CHECKOUT = f"{WORKSPACE}/src"
VENV_PYTHON = f"{CHECKOUT}/.venv/bin/python"

ORIGINAL_MESSAGE = "PODBENCH-ORIGINAL-v1"
EDITED_MESSAGE = "PODBENCH-EDITED-v2"

CURLER = "curler"
"""A client pod, so requests reach the Service from outside the pod under test.

Curling the Service from the dev pod's own sidecar would hairpin back into the
same netns and would keep passing if Service membership broke entirely."""

ROLLOUT_TIMEOUT = 420.0
BOOTSTRAP_TIMEOUT = 600.0


@pytest.fixture(scope="module")
def origin(kubectl: KubectlCli, apps_dir: Path) -> str:
    """The demo Deployment's pod, Ready and serving through the Service."""
    kubectl.apply(apps_dir / "python-service.yaml")
    kubectl.run(
        "rollout",
        "status",
        f"deployment/{DEPLOYMENT}",
        f"--timeout={int(ROLLOUT_TIMEOUT)}s",
        timeout=ROLLOUT_TIMEOUT + 30,
    )
    pods = kubectl.json("get", "pods", "-l", f"app={DEPLOYMENT}")
    items = cast(list[Any], pods.get("items") or [])
    assert items, "the demo Deployment produced no pods"
    name = as_dict(as_dict(items[0]).get("metadata")).get("name")
    assert isinstance(name, str)
    return name


@pytest.fixture(scope="module")
def sources(kubectl: KubectlCli, origin: str) -> dict[str, str]:
    """The demo app's files, read back from the ConfigMap that serves them.

    Read from the cluster rather than duplicated in Python so the manifest
    stays the single definition of the app: an edit to ``apps/`` cannot leave
    this test asserting on a string the workload no longer contains.
    """
    payload = kubectl.json("get", "configmap", CONFIGMAP)
    data = as_dict(payload.get("data"))
    files = {key: value for key, value in data.items() if isinstance(value, str)}
    assert ORIGINAL_MESSAGE in files["demo_service.py"]
    return files


@pytest.fixture(scope="module")
def curler(kubectl: KubectlCli) -> str:
    manifest: dict[str, object] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": CURLER},
        "spec": {
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "curl",
                    "image": "curlimages/curl:latest",
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["sleep", "infinity"],
                    "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}},
                }
            ],
        },
    }
    kubectl.run("create", "-f", "-", stdin=json.dumps(manifest))
    kubectl.wait(f"pod/{CURLER}", "condition=Ready", timeout=300)
    return CURLER


@pytest.fixture(scope="module")
def dev_pod(
    kube: Kubectl, kubectl: KubectlCli, origin: str, podbench_image: str
) -> Iterator[DevPod]:
    """An authored dev pod with the Service cut over to it, torn down after.

    ``--cutover`` rather than ``--take-traffic`` alone: with both the origin
    and the clone in the endpointslice, a request could be served by either and
    "did my edit land?" would be a coin flip. The cutover makes the answer
    deterministic and exercises the selector patch, which is its own finding.
    """
    before = _service_selector(kubectl)
    pod, _manifest = create_dev_pod(
        kube,
        origin,
        image=podbench_image,
        port=APP_PORT,
        cutover_service=SERVICE,
        timeout=ROLLOUT_TIMEOUT,
    )
    try:
        yield pod
    finally:
        actions = delete_dev_pod(kube, pod.ref.name, timeout=ROLLOUT_TIMEOUT)
        assert any("restored selector" in action for action in actions), actions
        after = _service_selector(kubectl)
        assert after == before, (
            "teardown did not put the Service selector back exactly as it was: "
            f"{before} -> {after}"
        )


@pytest.fixture(scope="module")
def workspace(kubectl: KubectlCli, dev_pod: DevPod, sources: dict[str, str]) -> DevPod:
    """A git repo, a clone, a venv and an editable install, in the sidecar.

    The repo is seeded inside the pod rather than cloned from the internet:
    ``dev-bootstrap``'s contract is "clone, sync, editable-install", and a
    public URL would make this a test of network egress. Both arms of
    :func:`podbench.dev.bootstrap_commands` get exercised — the fresh-clone arm
    and the existing-checkout arm — because a dev pod that is reconnected to is
    the common case, not the rare one.
    """
    sidecar = dev_pod.sidecar
    pod = dev_pod.ref.name
    for name, text in sources.items():
        kubectl.write_file(pod, f"{ORIGIN_REPO}/{name}", text, container=sidecar)
    kubectl.exec(
        pod,
        [
            "sh",
            "-c",
            f"cd {ORIGIN_REPO} && git init -q -b main && git add -A && "
            "git -c user.email=e2e@podbench.invalid -c user.name=podbench "
            "commit -q -m seed",
        ],
        container=sidecar,
        timeout=BOOTSTRAP_TIMEOUT,
    )

    # Clone only: `uv pip install -e .` needs an environment to install into,
    # and the checkout directory must not exist before `git clone`.
    kubectl.exec(
        pod,
        [
            "podbench",
            "dev-bootstrap",
            "--repo",
            ORIGIN_REPO,
            "--dir",
            CHECKOUT,
            "--no-sync",
            "--no-editable",
        ],
        container=sidecar,
        timeout=BOOTSTRAP_TIMEOUT,
    )
    kubectl.exec(
        pod,
        ["sh", "-c", f"cd {CHECKOUT} && uv venv"],
        container=sidecar,
        timeout=BOOTSTRAP_TIMEOUT,
    )
    result = kubectl.exec(
        pod,
        [
            "podbench",
            "dev-bootstrap",
            "--repo",
            ORIGIN_REPO,
            "--dir",
            CHECKOUT,
            "--no-sync",
        ],
        container=sidecar,
        timeout=BOOTSTRAP_TIMEOUT,
    )
    assert "pip install -e ." in result.stdout, result.stdout + result.stderr
    return dev_pod


def test_cutover_replaced_the_selector_rather_than_unioning_it(
    kubectl: KubectlCli, dev_pod: DevPod
) -> None:
    """The Service points at the dev pod and at nothing else.

    A *merge* patch unions selector maps, which in S4 left
    ``{app: ..., podbench.dev/devpod: "true"}`` and silently dropped the
    original pod out of the endpointslice while looking like it had worked
    (report §4.4). The observable difference is the selector's key set, so that
    is what is asserted rather than the patch type.
    """
    selector = _service_selector(kubectl)
    assert selector == {"podbench.dev/devpod": "true"}, selector


def test_idled_target_container_carries_no_probes(
    kubectl: KubectlCli, dev_pod: DevPod
) -> None:
    """The app container is idled and stripped; the sidecar has the readiness probe.

    Both halves matter. A liveness probe left on a container running ``sleep
    infinity`` kills the pod; and without a ``tcpSocket`` readiness probe on the
    *podbench* container the dev pod is Ready before anything is listening and
    about half the requests are blackholed (report §3.6, §4.4).
    """
    pod = kubectl.json("get", "pod", dev_pod.ref.name)
    containers = cast(list[Any], as_dict(pod.get("spec")).get("containers") or [])
    by_name = {str(as_dict(entry).get("name")): as_dict(entry) for entry in containers}

    app = by_name[dev_pod.target_container]
    assert app.get("command") == ["sleep", "infinity"], app.get("command")
    for probe in ("readinessProbe", "livenessProbe", "startupProbe", "lifecycle"):
        assert probe not in app, f"{probe} survived onto the idled container"

    sidecar = by_name[dev_pod.sidecar]
    readiness = as_dict(sidecar.get("readinessProbe"))
    assert as_dict(readiness.get("tcpSocket")).get("port") == APP_PORT, readiness


def test_edit_relaunch_and_see_it_through_the_service(
    kubectl: KubectlCli, workspace: DevPod, curler: str
) -> None:
    """The headline loop, from the outside.

    Every assertion here is made through the Service from a third pod, because
    that is the only vantage point from which the §3.5 failure — a clone the
    Service cannot see — is distinguishable from success.
    """
    pod = workspace.ref.name
    sidecar = workspace.sidecar

    _relaunch(kubectl, workspace)
    assert ORIGINAL_MESSAGE in _through_service(kubectl, curler)

    edited = _read_file(kubectl, pod, sidecar, f"{CHECKOUT}/demo_service.py").replace(
        ORIGINAL_MESSAGE, EDITED_MESSAGE
    )
    assert EDITED_MESSAGE in edited, "the edit did not apply to the checked-out file"
    kubectl.write_file(pod, f"{CHECKOUT}/demo_service.py", edited, container=sidecar)

    _relaunch(kubectl, workspace)
    body = _through_service(kubectl, curler)
    assert EDITED_MESSAGE in body, (
        "the Service is still serving the old code. Either the relaunch did "
        "not take (report §3.16: a second SO_REUSEPORT bind succeeds silently "
        "and the kernel splits traffic) or the dev pod is not in the "
        f"endpointslice (§3.5). Body was: {body!r}"
    )
    assert ORIGINAL_MESSAGE not in body


def _relaunch(kubectl: KubectlCli, dev_pod: DevPod) -> None:
    """``podbench run``: stop the recorded child, rebind, verify the socket.

    Verification is the point. S4's naive wrapper printed ``LISTENING after 1
    polls`` and exited 0 for a relaunch that had already died, so a non-zero
    exit here is a real regression and the detail line is worth keeping in the
    failure output.
    """
    kubectl.exec(
        dev_pod.ref.name,
        ["podbench", "stop", "--workspace", WORKSPACE],
        container=dev_pod.sidecar,
        check=False,
    )
    result = kubectl.exec(
        dev_pod.ref.name,
        [
            "podbench",
            "run",
            "--port",
            str(APP_PORT),
            "--workspace",
            WORKSPACE,
            "--dir",
            CHECKOUT,
            "--",
            VENV_PYTHON,
            "-c",
            "import demo_service; demo_service.main()",
        ],
        container=dev_pod.sidecar,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"podbench run refused to confirm the relaunch:\n{result.stdout}\n"
        f"{result.stderr}"
    )


def _through_service(kubectl: KubectlCli, curler_pod: str) -> str:
    """One request to the Service, retried while the endpointslice catches up.

    The readiness probe runs every 2 s, so there is a short window after a
    relaunch in which the dev pod is correct but not yet a member. Retrying is
    honest here; retrying past ~30 s would start hiding §3.6.
    """
    last = ""
    for _ in range(15):
        result = kubectl.exec(
            curler_pod,
            ["curl", "-s", "-m", "5", f"http://{SERVICE}/"],
            check=False,
            timeout=60,
        )
        last = result.stdout
        if result.returncode == 0 and last.strip():
            return last
        time.sleep(2)
    return last


def _read_file(kubectl: KubectlCli, pod: str, container: str, path: str) -> str:
    return kubectl.exec(pod, ["cat", path], container=container).stdout


def _service_selector(kubectl: KubectlCli) -> dict[str, str]:
    service = kubectl.json("get", "service", SERVICE)
    selector = as_dict(as_dict(service.get("spec")).get("selector"))
    return {key: str(value) for key, value in selector.items()}
