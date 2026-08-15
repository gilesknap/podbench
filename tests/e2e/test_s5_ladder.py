"""S5 as a regression: a restricted namespace still gets a seat, honestly labelled.

The capability ladder has exactly two rungs plus a seat, and the constraint
that makes it two rather than three is a kernel one: ``CAP_SYS_PTRACE`` on a
container with a non-zero ``runAsUser`` is a *silent* no-op — the pod is
admitted, the container runs, ``CapEff`` is zero and ptrace fails with a bare
``EPERM`` (report §3.10). A launcher that shipped that combination would tell
the user they had live attach when they did not.

So this file checks two things that are easy to regress and impossible to
notice: that a namespace enforcing the ``restricted`` Pod Security Standard
still lands a working seat and reports it as *degraded* rather than erroring,
and that the container it landed is not the invalid combination.

The namespace here is labelled by this test, on a namespace this test created.
Nothing else in the cluster is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from podbench.kubectl import Kubectl
from podbench.launcher import Session, attach
from podbench.model import Rung, Verdict, as_dict

from .conftest import KubectlCli, NamespaceFactory, PodbenchCli, kubectl_binary

TARGET_POD = "restricted-target"
TARGET_CONTAINER = "app"
TARGET_UID = 1000

RESTRICTED_LABELS = {
    "pod-security.kubernetes.io/enforce": "restricted",
    "pod-security.kubernetes.io/enforce-version": "latest",
}

# The target itself has to satisfy `restricted`, or it would not be admitted
# either and the test would be measuring the wrong refusal. This is also the
# shape report §4.5 names for the degraded rung, which is what makes the
# assertion below meaningful: the seat must come back as this, not as root.
TARGET_SECURITY_CONTEXT: dict[str, object] = {
    "runAsNonRoot": True,
    "runAsUser": TARGET_UID,
    "runAsGroup": TARGET_UID,
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
    "seccompProfile": {"type": "RuntimeDefault"},
}


@pytest.fixture(scope="module")
def restricted_namespace(namespace_factory: NamespaceFactory) -> str:
    """A scratch namespace of our own, enforcing ``restricted``."""
    return namespace_factory(labels=RESTRICTED_LABELS, suffix="psa-")


@pytest.fixture(scope="module")
def restricted_kubectl(restricted_namespace: str) -> KubectlCli:
    return KubectlCli(restricted_namespace)


@pytest.fixture(scope="module")
def restricted_kube(restricted_namespace: str) -> Kubectl:
    return Kubectl(restricted_namespace, binary=kubectl_binary())


@pytest.fixture(scope="module")
def restricted_target(restricted_kubectl: KubectlCli) -> str:
    manifest: dict[str, object] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": TARGET_POD, "labels": {"app": TARGET_POD}},
        "spec": {
            "restartPolicy": "Never",
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": TARGET_UID,
                "runAsGroup": TARGET_UID,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": TARGET_CONTAINER,
                    "image": "debian:bookworm-slim",
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/bin/sleep", "infinity"],
                    "securityContext": TARGET_SECURITY_CONTEXT,
                    "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}},
                }
            ],
        },
    }
    restricted_kubectl.run("create", "-f", "-", stdin=json.dumps(manifest))
    restricted_kubectl.wait(f"pod/{TARGET_POD}", "condition=Ready", timeout=300)
    return TARGET_POD


@pytest.fixture(scope="module")
def restricted_session(
    restricted_kube: Kubectl, restricted_target: str, podbench_image: str
) -> Session:
    """One attach, shared by every test here.

    Shared rather than repeated because an ephemeral container's name is burnt
    for the pod's lifetime: four attaches would leave four containers in the
    pod spec and each would have to reason about which one it was looking at.
    """
    return attach(
        restricted_kube,
        restricted_target,
        target=TARGET_CONTAINER,
        image=podbench_image,
    )


def test_restricted_namespace_admits_the_degraded_rung(
    restricted_session: Session,
) -> None:
    """Attach lands, falls to the degraded rung, and says why.

    The full rung is refused synchronously by Pod Security Admission on the
    stable substring ``must not include "SYS_PTRACE" in
    securityContext.capabilities.add``; the surrounding phrase differs between
    enforcement levels, so only that fragment may ever be matched (§3.18).
    Whether the launcher *attempts* it or pre-skips it (the target sets
    ``runAsNonRoot: true``, which report §3.18 says makes the kubelet refuse a
    root container asynchronously and burn the container name) is an
    implementation choice; what must hold is that the outcome is a seat, on the
    degraded rung, with a recorded reason.
    """
    session = restricted_session
    assert session.rung is Rung.DEGRADED, (
        "a restricted namespace should still get the degraded rung — the "
        f"target runs as uid {TARGET_UID}, which is exactly what that rung "
        f"pins. Steps: {[(s.rung.value, s.admitted, s.detail) for s in session.steps]}"
    )
    assert session.uid == TARGET_UID, (
        "the degraded rung must run as the *target's* uid, never default to "
        "root: root without the capability reads 3/6 of the probe paths where "
        f"the target's own uid reads 6/6 (report §3.11). Got {session.uid}"
    )

    full = [step for step in session.steps if step.rung is Rung.FULL]
    assert full and not full[0].admitted, session.steps
    assert full[0].detail.strip(), "the full rung was refused with no reason recorded"


def test_the_landed_container_is_never_the_invalid_combination(
    restricted_kubectl: KubectlCli, restricted_target: str, restricted_session: Session
) -> None:
    """No podbench container carries ``SYS_PTRACE`` with a non-zero uid.

    This is the one shape that must never exist. The kernel grants
    capabilities to non-root uids only through the ambient set, which the CRI
    does not populate, so such a container is admitted, runs, reports
    ``CapEff: 0000000000000000`` and fails ptrace with a bare ``EPERM``
    (§3.10). Everything looks right, which is why it needs a test rather than
    a code review.
    """
    pod = restricted_kubectl.json("get", "pod", restricted_target)
    containers = cast(
        list[Any], as_dict(pod.get("spec")).get("ephemeralContainers") or []
    )
    assert containers, "attach added no ephemeral container"
    for entry in containers:
        container = as_dict(entry)
        security = as_dict(container.get("securityContext"))
        raw_add: Any = as_dict(security.get("capabilities")).get("add")
        added: list[Any] = cast(list[Any], raw_add) if isinstance(raw_add, list) else []
        uid: Any = security.get("runAsUser")
        assert not ("SYS_PTRACE" in added and uid not in (0, None)), (
            f"container {container.get('name')!r} was authored with "
            f"SYS_PTRACE and runAsUser={uid!r}: the kernel silently gives it "
            "CapEff 0 and the user is told they have live attach (§3.10)"
        )


def test_the_probe_names_the_blocker_rather_than_the_errno(
    restricted_session: Session,
) -> None:
    """capreport ran in the seat and reached a verdict with a named mechanism.

    Not an assertion that attach is denied: Yama is a per-node, per-kernel
    setting and two arm64 nodes on the spike cluster disagreed with each other
    (§3.13), so a same-uid attach genuinely succeeds on some nodes and is
    refused on others. What must be true everywhere is that the report says
    which of the four subsystems decided, and on which node.
    """
    report = restricted_session.report
    assert report is not None, "attach was asked to probe and returned no report"
    assert report.node_name, (
        "the report must name the node: Yama differs by kernel flavour, so "
        "'attach worked yesterday' is only explicable with the node in hand "
        "(§3.13)"
    )
    assert report.self_uid == TARGET_UID
    assert not report.cap_sys_ptrace, (
        "the degraded rung asked for no capabilities; if CapEff bit 19 is set "
        "here, the ladder authored something it should have refused"
    )
    if report.verdict is Verdict.LIVE_ATTACH:
        # Legitimate: same uid, and a node with no Yama at all.
        assert report.target_attach_ok
    else:
        assert report.blocker.explanation.strip()
        assert report.verdict in (Verdict.READ_ONLY, Verdict.NONE)


def test_the_cli_reports_a_degraded_seat_as_success(
    restricted_namespace: str,
    restricted_target: str,
    restricted_session: Session,
    podbench_image: str,
    ssh_identity: tuple[Path, str],
) -> None:
    """``podbench attach`` exits 0 and prints the rung it got.

    A non-zero exit for "the cluster would not grant SYS_PTRACE" would make an
    honest capability report look like a tool failure, and the brief asks for
    the opposite: land the best seat available and say plainly what it can do.

    This reconnects to the container ``restricted_session`` already landed
    rather than adding a second one — an ephemeral container cannot be removed,
    so "attach twice" has to mean "reconnect" (report §4.2), and that is worth
    covering here too.
    """
    identity, _ = ssh_identity
    cli = PodbenchCli(restricted_namespace)
    result = cli.run(
        "attach",
        restricted_target,
        "--target",
        TARGET_CONTAINER,
        "--image",
        podbench_image,
        "--identity",
        str(identity),
        "--no-probe",
        "--print-config",
        *cli.common(),
        check=False,
        timeout=420,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert Rung.DEGRADED.description in result.stdout, result.stdout
