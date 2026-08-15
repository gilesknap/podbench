"""Tests for the spec authors.

The fixture is shaped like the pod S4 ran against: a Deployment-owned pod with
all three probes, a ConfigMap volume and a controller label.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from podbench.model import Rung
from podbench.spec import (
    AGENT_COMMAND,
    DEVPOD_LABEL,
    ORIGIN_ANNOTATION,
    WORKSPACE_VOLUME,
    InvalidSpecError,
    container_id,
    cutover_selector_patch,
    dev_pod_spec,
    ephemeral_container_spec,
    runs_as_non_root,
    service_selector_patch,
    target_uid_gid,
    validate_security_context,
)


def origin_pod() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "podbench-target-7b8d54747c-ltmtd",
            "namespace": "podbench-s4",
            "uid": "b75ba7ec-0000-0000-0000-000000000000",
            "resourceVersion": "38532067",
            "creationTimestamp": "2026-08-15T10:40:55Z",
            "generateName": "podbench-target-7b8d54747c-",
            "managedFields": [{"manager": "kube-controller-manager"}],
            "ownerReferences": [
                {"kind": "ReplicaSet", "name": "podbench-target-7b8d5"}
            ],
            "labels": {
                "app": "podbench-target",
                "tier": "demo",
                "pod-template-hash": "7b8d54747c",
            },
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{}"},
        },
        "spec": {
            "nodeName": "nuc2",
            "restartPolicy": "Always",
            "securityContext": {"runAsUser": 1000, "runAsGroup": 3000},
            "containers": [
                {
                    "name": "app",
                    "image": "python:3.12-slim",
                    "command": ["python", "/src/app.py"],
                    "args": ["--serve"],
                    "ports": [{"name": "http", "containerPort": 8080}],
                    "volumeMounts": [{"name": "src", "mountPath": "/src"}],
                    "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
                    "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
                    "startupProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
                    "lifecycle": {"preStop": {"exec": {"command": ["true"]}}},
                    "resources": {"limits": {"cpu": "500m", "memory": "256Mi"}},
                }
            ],
            "volumes": [{"name": "src", "configMap": {"name": "app-src"}}],
        },
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "app",
                    "containerID": "containerd://87d20e2312ab",
                }
            ],
        },
    }


def devpod(**kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "name": "devpod",
        "target_container": "app",
        "image": "podbench:dev",
        "target_port": 8080,
    }
    defaults.update(kwargs)
    return dev_pod_spec(origin_pod(), **defaults)


def container(pod: dict[str, Any], name: str) -> dict[str, Any]:
    for entry in pod["spec"]["containers"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"no container {name}")


# -- the capability ladder ------------------------------------------------


def test_full_rung_is_root_plus_sys_ptrace() -> None:
    spec = ephemeral_container_spec(
        name="podbench-1",
        image="podbench:dev",
        rung=Rung.FULL,
        target_container="app",
    )
    assert spec["securityContext"] == {
        "runAsUser": 0,
        "capabilities": {"add": ["SYS_PTRACE"]},
    }
    assert spec["targetContainerName"] == "app"
    # Not `sleep infinity`: nothing would ever write the sshd config, the
    # authorized keys or the host key, and the ProxyCommand's
    # `sshd -i -f /etc/podbench/sshd_config` would fail on a file that is not
    # there. `podbench agent` is long-running, so the name is not burnt.
    assert spec["command"] == ["podbench", "agent"]
    # An ephemeral container spec cannot carry resources at all.
    assert "resources" not in spec


def test_degraded_rung_matches_the_restricted_psa_shape() -> None:
    spec = ephemeral_container_spec(
        name="podbench-2",
        image="podbench:dev",
        rung=Rung.DEGRADED,
        target_container="app",
        target_uid=1000,
        target_gid=3000,
    )
    assert spec["securityContext"] == {
        "capabilities": {"drop": ["ALL"]},
        "allowPrivilegeEscalation": False,
        "seccompProfile": {"type": "RuntimeDefault"},
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 3000,
    }


def test_degraded_rung_refuses_to_default_to_root() -> None:
    with pytest.raises(InvalidSpecError, match="target's own uid"):
        ephemeral_container_spec(
            name="podbench-2", image="podbench:dev", rung=Rung.DEGRADED
        )


def test_degraded_rung_is_impossible_against_a_root_target() -> None:
    with pytest.raises(InvalidSpecError, match="runs as root"):
        ephemeral_container_spec(
            name="podbench-2",
            image="podbench:dev",
            rung=Rung.DEGRADED,
            target_uid=0,
        )


def test_seat_rung_pins_no_uid() -> None:
    spec = ephemeral_container_spec(
        name="podbench-3", image="podbench:dev", rung=Rung.SEAT
    )
    assert "runAsUser" not in spec["securityContext"]
    assert spec["securityContext"]["runAsNonRoot"] is True


def test_seat_rung_never_pins_root_beside_run_as_non_root() -> None:
    """The rung the ladder falls to for a root target must stay admissible.

    ``runAsNonRoot: true`` with ``runAsUser: 0`` passes the API server and is
    then refused by the kubelet, asynchronously, with
    ``CreateContainerConfigError`` — and the ephemeral container's name is burnt
    by then (report 3.18).
    """
    context = ephemeral_container_spec(
        name="podbench-3",
        image="podbench:dev",
        rung=Rung.SEAT,
        target_uid=0,
        target_gid=0,
    )["securityContext"]
    assert "runAsUser" not in context
    assert context["runAsNonRoot"] is True


def test_seat_rung_honours_a_non_root_target_uid() -> None:
    context = ephemeral_container_spec(
        name="podbench-3", image="podbench:dev", rung=Rung.SEAT, target_uid=1000
    )["securityContext"]
    assert context["runAsUser"] == 1000


def test_sys_ptrace_with_a_non_root_uid_is_rejected() -> None:
    # Measured: this combination leaves CapEff 0000000000000000 while the pod is
    # admitted and the container runs (report 3.10).
    with pytest.raises(InvalidSpecError, match="runAsUser: 0"):
        validate_security_context(
            {"runAsUser": 1000, "capabilities": {"add": ["SYS_PTRACE"]}}
        )


def test_sys_ptrace_without_an_explicit_uid_is_rejected() -> None:
    with pytest.raises(InvalidSpecError):
        validate_security_context({"capabilities": {"add": ["SYS_PTRACE"]}})


def test_the_full_rung_cannot_be_asked_for_a_non_root_uid() -> None:
    with pytest.raises(InvalidSpecError, match="runs as root"):
        ephemeral_container_spec(
            name="podbench-1",
            image="podbench:dev",
            rung=Rung.FULL,
            target_uid=1000,
        )


def test_a_dropped_capability_is_not_mistaken_for_an_added_one() -> None:
    validate_security_context({"capabilities": {"drop": ["ALL"]}, "runAsUser": 1000})


def test_the_target_container_id_is_injected_without_its_scheme() -> None:
    spec = ephemeral_container_spec(
        name="podbench-1",
        image="podbench:dev",
        rung=Rung.FULL,
        target_container="app",
        target_container_id=container_id(origin_pod(), "app"),
    )
    assert {"name": "PODBENCH_TARGET_CID", "value": "87d20e2312ab"} in spec["env"]


# -- the dev pod ----------------------------------------------------------


def test_controller_labels_are_dropped_but_selector_labels_survive() -> None:
    pod = devpod(take_traffic=True)
    assert pod["metadata"]["labels"] == {
        "app": "podbench-target",
        "tier": "demo",
        DEVPOD_LABEL: "true",
    }


def test_service_traffic_is_opt_in() -> None:
    pod = devpod()
    assert pod["metadata"]["labels"] == {DEVPOD_LABEL: "true"}


def test_the_origin_is_recorded_and_its_annotations_are_not_copied() -> None:
    pod = devpod()
    assert pod["metadata"]["annotations"] == {
        ORIGIN_ANNOTATION: "podbench-target-7b8d54747c-ltmtd"
    }


def test_server_owned_metadata_and_status_are_dropped() -> None:
    pod = devpod()
    metadata = pod["metadata"]
    for key in (
        "uid",
        "resourceVersion",
        "creationTimestamp",
        "generateName",
        "managedFields",
        "ownerReferences",
    ):
        assert key not in metadata
    assert "status" not in pod
    assert metadata["name"] == "devpod"


def test_the_target_container_is_idled_and_stripped_of_everything_fatal() -> None:
    app = container(devpod(), "app")
    assert app["command"] == ["sleep", "infinity"]
    assert "args" not in app
    for probe in ("readinessProbe", "livenessProbe", "startupProbe", "lifecycle"):
        assert probe not in app
    # What the app keeps: its ports, mounts and limits.
    assert app["ports"] == [{"name": "http", "containerPort": 8080}]
    assert app["resources"]["limits"]["memory"] == "256Mi"


def test_scheduling_fields_are_reset() -> None:
    spec = devpod()["spec"]
    assert "nodeName" not in spec
    assert spec["restartPolicy"] == "Never"
    assert spec["shareProcessNamespace"] is True


def test_the_sidecar_carries_what_copy_to_cannot_give_it() -> None:
    sidecar = container(devpod(), "podbench")
    assert sidecar["resources"]["limits"]["memory"] == "3Gi"
    assert sidecar["volumeMounts"] == [
        {"name": WORKSPACE_VOLUME, "mountPath": "/workspace"}
    ]
    assert {"name": "HOME", "value": "/workspace"} in sidecar["env"]
    assert sidecar["securityContext"]["capabilities"]["add"] == ["SYS_PTRACE"]
    # The origin pod pins runAsUser 1000 pod-wide, which would void the
    # capability, and runAsNonRoot would let the kubelet refuse this container.
    assert sidecar["securityContext"]["runAsUser"] == 0
    assert sidecar["securityContext"]["runAsNonRoot"] is False


def test_the_sidecar_runs_the_agent_and_the_target_is_the_one_idled() -> None:
    pod = devpod()
    # The sidecar is this pod's ssh endpoint, so it must write the server-side
    # files; the target is the container that stops serving.
    assert container(pod, "podbench")["command"] == list(AGENT_COMMAND)
    assert container(pod, "app")["command"] == ["sleep", "infinity"]


def test_the_readiness_probe_is_on_the_podbench_container() -> None:
    pod = devpod()
    assert container(pod, "podbench")["readinessProbe"] == {
        "tcpSocket": {"port": 8080},
        "periodSeconds": 2,
        "failureThreshold": 1,
    }
    assert "readinessProbe" not in container(pod, "app")


def test_the_workspace_volume_is_added_alongside_the_originals() -> None:
    volumes = devpod()["spec"]["volumes"]
    assert [v["name"] for v in volumes] == ["src", WORKSPACE_VOLUME]
    assert volumes[1]["emptyDir"] == {"sizeLimit": "4Gi"}


def test_the_sidecar_can_be_authored_without_the_capability() -> None:
    sidecar = container(devpod(sidecar_ptrace=False), "podbench")
    assert sidecar["securityContext"] == {
        "capabilities": {"drop": ["ALL"]},
        "allowPrivilegeEscalation": False,
        "seccompProfile": {"type": "RuntimeDefault"},
        "runAsNonRoot": True,
    }


def test_the_origin_json_is_not_mutated() -> None:
    pod = origin_pod()
    before = copy.deepcopy(pod)
    dev_pod_spec(
        pod,
        name="devpod",
        target_container="app",
        image="podbench:dev",
        target_port=8080,
    )
    assert pod == before


def test_an_unknown_target_container_is_refused() -> None:
    with pytest.raises(InvalidSpecError, match="not found"):
        devpod(target_container="sidecar")


def test_a_colliding_sidecar_name_is_refused() -> None:
    with pytest.raises(InvalidSpecError, match="already has a container"):
        devpod(sidecar_name="app")


# -- service traffic ------------------------------------------------------


def test_the_selector_patch_replaces_rather_than_merges() -> None:
    assert service_selector_patch({"app": "podbench-target"}) == [
        {
            "op": "replace",
            "path": "/spec/selector",
            "value": {"app": "podbench-target"},
        }
    ]


def test_cutover_points_the_service_only_at_the_dev_pod() -> None:
    assert cutover_selector_patch()[0]["value"] == {DEVPOD_LABEL: "true"}


# -- reading the target ---------------------------------------------------


def test_target_ids_fall_back_to_the_pod_level_context() -> None:
    assert target_uid_gid(origin_pod(), "app") == (1000, 3000)


def test_container_ids_win_over_pod_ids() -> None:
    pod = origin_pod()
    pod["spec"]["containers"][0]["securityContext"] = {"runAsUser": 65532}
    assert target_uid_gid(pod, "app") == (65532, 3000)


def test_unset_ids_are_reported_as_unknown() -> None:
    pod = origin_pod()
    del pod["spec"]["securityContext"]
    assert target_uid_gid(pod, "app") == (None, None)


def test_run_as_non_root_is_read_for_the_pre_flight() -> None:
    pod = origin_pod()
    assert runs_as_non_root(pod, "app") is False
    pod["spec"]["securityContext"]["runAsNonRoot"] = True
    assert runs_as_non_root(pod, "app") is True
    pod["spec"]["containers"][0]["securityContext"] = {"runAsNonRoot": False}
    assert runs_as_non_root(pod, "app") is False


def test_a_missing_container_id_is_none() -> None:
    assert container_id(origin_pod(), "nope") is None
