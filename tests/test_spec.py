"""Tests for the spec authors.

The fixture is shaped like the pod S4 ran against: a Deployment-owned pod with
all three probes, a ConfigMap volume and a controller label.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from podbench.model import (
    SEAT_HOME_PATH,
    SEAT_HOME_VOLUME,
    SEAT_IDENTITY_VOLUME,
    Rung,
)
from podbench.spec import (
    AGENT_COMMAND,
    DEVPOD_LABEL,
    ORIGIN_ANNOTATION,
    WORKSPACE_VOLUME,
    InvalidSpecError,
    container_id,
    cutover_selector_patch,
    dev_pod_spec,
    dev_seat_identity,
    ephemeral_container_spec,
    runs_as_non_root,
    seat_identity_volume_mounts,
    service_selector_patch,
    target_uid_gid,
    validate_ephemeral_volume_mounts,
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


def origin_with_identity(**spec_overrides: Any) -> dict[str, Any]:
    """The same origin, deployed by somebody who prepared it for podbench.

    The volume is *declared and not mounted* by the application container, which
    is what the chart's snippet tells them to write: nothing in the pod but a
    seat has any use for it.
    """
    pod = origin_pod()
    pod["spec"]["volumes"].append(
        {
            "name": SEAT_IDENTITY_VOLUME,
            "configMap": {"name": "app-podbench-identity", "defaultMode": 0o444},
        }
    )
    pod["spec"].update(spec_overrides)
    return pod


def dev_pod_spec_with_identity(
    origin: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    """A dev pod authored from an origin that declares the identity volume."""
    defaults: dict[str, Any] = {
        "name": "devpod",
        "target_container": "app",
        "image": "podbench:dev",
        "target_port": 8080,
    }
    defaults.update(kwargs)
    prepared = origin_with_identity() if origin is None else origin
    return dev_pod_spec(prepared, **defaults)


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


def test_seat_gid_root_is_opt_in_and_keeps_everything_else() -> None:
    """GID 0 is what lets the seat give itself the NSS identity sshd needs.

    The target's uid is discovered at attach time, so no image can carry an
    account for it; the seat registers one in a group-writable ``/etc/passwd``
    instead, which needs gid 0 and nothing else. It stays opt-in because it
    drops the target's own group, and the API server has no objection either
    way — the restricted Pod Security Standard does not constrain
    ``runAsGroup`` (measured: uid 1000 / gid 0 admitted under ``restricted``).
    """
    for rung in (Rung.DEGRADED, Rung.SEAT):
        default = ephemeral_container_spec(
            name="podbench-2",
            image="podbench:dev",
            rung=rung,
            target_uid=1000,
            target_gid=3000,
        )["securityContext"]
        assert default["runAsGroup"] == 3000, "the default must not change silently"

        opted_in = ephemeral_container_spec(
            name="podbench-2",
            image="podbench:dev",
            rung=rung,
            target_uid=1000,
            target_gid=3000,
            seat_gid_root=True,
        )["securityContext"]
        assert opted_in["runAsGroup"] == 0
        # Only the group moves: the uid, and every restricted-PSS field, stay.
        assert opted_in == {**default, "runAsGroup": 0}


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


# -- what an ephemeral container's volumeMounts may not carry --------------


def test_a_sub_path_mount_is_refused_before_the_api_server_sees_it() -> None:
    """Measured against a real cluster, and it costs the whole attach.

    ``kubectl ... replace --raw .../pods/demo/ephemeralcontainers`` comes back
    with ``The Pod "demo" is invalid: * spec.ephemeralContainers[0].
    volumeMounts[0].subPath: Forbidden: cannot be set for an Ephemeral
    Container`` — a request-level refusal, so a single such mount means no seat
    lands at all. The seat-identity mounts were exactly that shape, which broke
    `attach` against every pod prepared with the identity volume.
    """
    with pytest.raises(InvalidSpecError, match="Ephemeral Container") as raised:
        ephemeral_container_spec(
            name="podbench-1",
            image="podbench:dev",
            rung=Rung.SEAT,
            volume_mounts=[
                {
                    "name": "podbench-identity",
                    "mountPath": "/etc/passwd",
                    "subPath": "passwd",
                    "readOnly": True,
                }
            ],
        )
    # The way out on a live pod, named where the refusal is read.
    assert "--seat-gid-root" in str(raised.value)


def test_a_sub_path_expr_mount_is_refused_too() -> None:
    """The API server forbids both fields, so both are refused here."""
    with pytest.raises(InvalidSpecError, match="subPathExpr"):
        ephemeral_container_spec(
            name="podbench-1",
            image="podbench:dev",
            rung=Rung.SEAT,
            volume_mounts=[
                {
                    "name": "config",
                    "mountPath": "/etc/app.conf",
                    "subPathExpr": "$(POD_NAME)",
                }
            ],
        )


def test_the_refusal_names_the_offending_mount_by_index() -> None:
    """The API server's message is positional, so this one is too."""
    with pytest.raises(InvalidSpecError, match=r"volumeMounts\[1\]"):
        ephemeral_container_spec(
            name="podbench-1",
            image="podbench:dev",
            rung=Rung.SEAT,
            volume_mounts=[
                {"name": "workspace", "mountPath": "/workspace"},
                {"name": "venv", "mountPath": "/opt/venv", "subPath": "venv"},
            ],
        )


def test_whole_volume_mounts_are_authored_unchanged() -> None:
    """The rule is about ``subPath``, not about mounting: that stays supported.

    Handing a toolchain to a non-root seat that cannot apt-get is the whole
    point of ``--mount`` (report 3.19), and a refusal that swept up ordinary
    mounts would take Patch mode with it.
    """
    mounts = [
        {"name": "podbench-patch-venv", "mountPath": "/opt/venv"},
        {"name": "podbench-home", "mountPath": "/home/podbench"},
    ]
    spec = ephemeral_container_spec(
        name="podbench-1",
        image="podbench:dev",
        rung=Rung.SEAT,
        volume_mounts=mounts,
    )
    assert spec["volumeMounts"] == mounts


def test_an_empty_sub_path_is_not_a_sub_path() -> None:
    """``subPath: ""`` is what the API server itself treats as unset."""
    spec = ephemeral_container_spec(
        name="podbench-1",
        image="podbench:dev",
        rung=Rung.SEAT,
        volume_mounts=[{"name": "logs", "mountPath": "/var/log", "subPath": ""}],
    )
    assert spec["volumeMounts"] == [
        {"name": "logs", "mountPath": "/var/log", "subPath": ""}
    ]


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


# -- the identity a dev pod's sidecar can be given ------------------------


def test_a_declared_identity_is_projected_over_passwd_and_group() -> None:
    """The mounts an ephemeral seat cannot have, on the container that can.

    One ``subPath`` per file and read-only: the volume at ``/etc`` would replace
    the directory and take ``nsswitch.conf`` with it, and nothing in the pod has
    any business rewriting the seat's ``/etc/passwd``.
    """
    sidecar = container(dev_pod_spec_with_identity(), "podbench")
    assert sidecar["volumeMounts"] == [
        {"name": WORKSPACE_VOLUME, "mountPath": "/workspace"},
        {
            "name": SEAT_IDENTITY_VOLUME,
            "mountPath": "/etc/passwd",
            "subPath": "passwd",
            "readOnly": True,
        },
        {
            "name": SEAT_IDENTITY_VOLUME,
            "mountPath": "/etc/group",
            "subPath": "group",
            "readOnly": True,
        },
    ]


def test_the_projected_identity_and_the_sidecar_are_the_same_user() -> None:
    """The mismatch this feature exists to prevent, asserted as an equality.

    The ConfigMap's record is for the *application's* uid. A sidecar mounting it
    while running as root would have an ``/etc/passwd`` describing somebody it
    is not: sshd resolves the login name to that uid, cannot read the
    ``authorized_keys`` the agent wrote as root, and answers ``Permission denied
    (publickey)`` — which names nothing about identity and would be debugged
    anywhere but here.
    """
    origin = origin_with_identity()
    seat_uid, seat_gid = target_uid_gid(origin, "app")
    sidecar = container(dev_pod_spec_with_identity(origin), "podbench")

    assert sidecar["securityContext"]["runAsUser"] == seat_uid
    assert sidecar["securityContext"]["runAsGroup"] == seat_gid
    assert sidecar["securityContext"]["runAsUser"] != 0
    # SYS_PTRACE is the price: a capability added to a non-root uid reaches the
    # bounding set only, so it cannot be kept beside the identity's uid.
    assert "add" not in sidecar["securityContext"]["capabilities"]
    assert sidecar["securityContext"]["runAsNonRoot"] is True


def test_the_home_the_identity_names_is_mounted_when_the_pod_has_one() -> None:
    """The passwd record says ``/home/podbench``; sshd believes it.

    A session lands in the home NSS gives it, so without the volume the login
    works and arrives at ``Could not chdir to home directory``, in a container
    whose vscode-server then has nowhere to unpack. ``$HOME`` for the sidecar
    itself stays the workspace, which is the volume sized for uv's caches.
    """
    origin = origin_with_identity()
    origin["spec"]["volumes"].append(
        {"name": SEAT_HOME_VOLUME, "emptyDir": {"sizeLimit": "2Gi"}}
    )
    sidecar = container(dev_pod_spec_with_identity(origin), "podbench")
    assert {
        "name": SEAT_HOME_VOLUME,
        "mountPath": SEAT_HOME_PATH,
    } in sidecar["volumeMounts"]
    assert {"name": "HOME", "value": "/workspace"} in sidecar["env"]


def test_a_home_volume_without_an_identity_is_left_to_attach() -> None:
    """No projected record means no ``/home/podbench`` in the record either.

    The root sidecar resolves as ``root``, whose home is ``/root``; mounting the
    seat's home over a path nothing names would be a volume for nobody. The
    convention that *does* use it is ``attach``'s, on a live pod.
    """
    origin = origin_pod()
    origin["spec"]["volumes"].append(
        {"name": SEAT_HOME_VOLUME, "emptyDir": {"sizeLimit": "2Gi"}}
    )
    sidecar = container(
        dev_pod_spec(
            origin,
            name="devpod",
            target_container="app",
            image="podbench:dev",
            target_port=8080,
        ),
        "podbench",
    )
    assert [mount["name"] for mount in sidecar["volumeMounts"]] == [WORKSPACE_VOLUME]


def test_the_identity_volume_is_carried_onto_the_clone() -> None:
    """A mount is worth nothing if the clone drops the volume it names."""
    pod = dev_pod_spec_with_identity()
    assert [volume["name"] for volume in pod["spec"]["volumes"]] == [
        "src",
        SEAT_IDENTITY_VOLUME,
        WORKSPACE_VOLUME,
    ]
    mounted = {mount["name"] for mount in container(pod, "podbench")["volumeMounts"]}
    declared = {volume["name"] for volume in pod["spec"]["volumes"]}
    assert mounted <= declared


def test_a_pod_with_no_fsgroup_is_given_the_identitys_gid() -> None:
    """The sidecar has just stopped being root, and the workspace is root:root.

    An emptyDir is created ``root:root`` and stays that way until ``fsGroup``
    makes the kubelet chgrp it. Without one the seat's ``$HOME`` is present and
    unwritable, which fails later and further away than no home at all.
    """
    origin = origin_with_identity()
    assert "fsGroup" not in origin["spec"]["securityContext"]
    pod = dev_pod_spec_with_identity(origin)
    assert pod["spec"]["securityContext"]["fsGroup"] == 3000
    # The rest of the origin's pod-level context comes over untouched.
    assert pod["spec"]["securityContext"]["runAsUser"] == 1000


def test_an_fsgroup_the_origin_sets_is_inherited_rather_than_replaced() -> None:
    """The application's chart chose that number; podbench does not know better."""
    origin = origin_with_identity(
        securityContext={"runAsUser": 1000, "runAsGroup": 3000, "fsGroup": 2000}
    )
    pod = dev_pod_spec_with_identity(origin)
    assert pod["spec"]["securityContext"]["fsGroup"] == 2000


def test_an_identity_whose_uid_the_manifest_does_not_pin_is_not_projected() -> None:
    """A guessed uid is a login for the wrong user, so nothing is guessed.

    The effective uid then comes from the image, which only the running process
    knows — and the record in the ConfigMap was written for a number this
    manifest does not carry.
    """
    origin = origin_with_identity(securityContext={})
    assert dev_seat_identity(origin, "app") is None
    sidecar = container(dev_pod_spec_with_identity(origin), "podbench")
    assert sidecar["securityContext"]["runAsUser"] == 0
    assert [mount["name"] for mount in sidecar["volumeMounts"]] == [WORKSPACE_VOLUME]


def test_a_root_application_is_not_given_a_projected_identity() -> None:
    """uid 0 resolves in every image already; the file would cost SYS_PTRACE."""
    origin = origin_with_identity(securityContext={"runAsUser": 0, "runAsGroup": 0})
    assert dev_seat_identity(origin, "app") is None


def test_the_identity_convention_can_be_turned_off() -> None:
    origin = origin_with_identity()
    sidecar = container(
        dev_pod_spec(
            origin,
            name="devpod",
            target_container="app",
            image="podbench:dev",
            target_port=8080,
            seat_identity=False,
        ),
        "podbench",
    )
    assert sidecar["securityContext"]["capabilities"]["add"] == ["SYS_PTRACE"]
    assert [mount["name"] for mount in sidecar["volumeMounts"]] == [WORKSPACE_VOLUME]


def test_an_origin_without_the_volume_is_authored_exactly_as_before() -> None:
    """The regression that matters most: nothing changes for everybody else."""
    pod = devpod()
    sidecar = container(pod, "podbench")
    assert sidecar["volumeMounts"] == [
        {"name": WORKSPACE_VOLUME, "mountPath": "/workspace"}
    ]
    assert sidecar["securityContext"]["runAsUser"] == 0
    assert sidecar["securityContext"]["capabilities"]["add"] == ["SYS_PTRACE"]
    assert "fsGroup" not in pod["spec"]["securityContext"]


def test_the_projected_mounts_are_exactly_what_an_ephemeral_seat_is_refused() -> None:
    """One layout, and the two containers it means opposite things to.

    The same mounts an ordinary sidecar is authored with are the ones
    :func:`validate_ephemeral_volume_mounts` refuses, so this file cannot end up
    asserting a layout that only one half believes in.
    """
    with pytest.raises(InvalidSpecError, match="Ephemeral Container"):
        validate_ephemeral_volume_mounts(seat_identity_volume_mounts())


def test_the_origins_ephemeral_containers_are_not_cloned() -> None:
    """A pod that has been attached to must still be clonable.

    ``spec.ephemeralContainers`` is refused on create — ``Forbidden: cannot be
    set on create`` — so copying the origin's seats fails the whole ``dev`` on a
    field the user never typed, and does it only for the pods most likely to be
    debugged twice. Measured against a real cluster: an origin carrying
    ``podbench-1..3``.
    """
    origin = origin_with_identity()
    origin["spec"]["ephemeralContainers"] = [
        {"name": "podbench-1", "image": "podbench:dev"}
    ]
    assert "ephemeralContainers" not in dev_pod_spec_with_identity(origin)["spec"]


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
