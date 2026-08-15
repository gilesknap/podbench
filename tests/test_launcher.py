"""Tests for the launcher.

Nothing here touches a cluster: :class:`FakeCluster` stands in for the
subprocess and keeps a mutable pod document, so an ephemeral container added by
one call is visible to the next exactly as it would be through the API server.
That is what lets the two rejection channels of report 3.18 — Pod Security
Admission's synchronous ``Forbidden`` and the kubelet's asynchronous
``CreateContainerConfigError`` — be asserted separately, including the detail
that only the second burns a container name.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from podbench.kubectl import CommandResult, Kubectl
from podbench.launcher import (
    LauncherError,
    Session,
    attach,
    capability_report_from_json,
    current_namespace,
    emit_ssh_config,
    features,
    forget_known_hosts,
    forget_ssh_config,
    format_session,
    main,
    match_pod_names,
    parse_mount,
    plan_ladder,
    pod_choices,
    resolve_pod,
    resolve_pod_name,
    seats,
    ssh_config_path,
    try_resize,
)
from podbench.model import (
    SEAT_HOME_PATH,
    SEAT_HOME_VOLUME,
    SEAT_IDENTITY_VOLUME,
    Blocker,
    ContainerRef,
    PodRef,
    Rung,
    Verdict,
)
from podbench.sshcfg import SEAT_USER

POD_UID = "11111111-2222-3333-4444-555555555555"
TARGET_CID = "abc123def456"
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 podbench"
CLIENT_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA developer@laptop"

PSA_REFUSAL = (
    'pods "target" is forbidden: violates PodSecurity "restricted:latest": '
    'unrestricted capabilities (container "podbench-1" must not include '
    '"SYS_PTRACE" in securityContext.capabilities.add)'
)
KUBELET_REFUSAL = (
    "container has runAsNonRoot and image will run as root, or container's "
    "runAsUser breaks non-root policy"
)
OLD_IMAGE_REFUSAL = (
    "usage: podbench agent [-h] [--ensure-only]\n"
    "podbench agent: error: unrecognized arguments: --print-login-user"
)
NO_LOGIN_USER = (
    "uid 1000 has no NSS entry and this container cannot add one: /etc/passwd "
    "is not writable by uid 1000 / gid 1000. sshd resolves the login name "
    "through NSS before it will look at a key, so ssh into this seat cannot "
    "work."
)


def pod_document(
    *,
    name: str = "target",
    container: str = "app",
    uid: int | None = None,
    non_root: bool = False,
    ephemeral: Sequence[dict[str, Any]] = (),
    ephemeral_statuses: Sequence[dict[str, Any]] = (),
    volumes: Sequence[dict[str, Any]] = (),
    volume_mounts: Sequence[dict[str, Any]] = (),
    created: str = "2026-08-15T09:00:00Z",
    phase: str = "Running",
    ready: bool = True,
) -> dict[str, Any]:
    security: dict[str, Any] = {}
    if uid is not None:
        security["runAsUser"] = uid
    if non_root:
        security["runAsNonRoot"] = True
    workload: dict[str, Any] = {"name": container, "securityContext": security}
    if volume_mounts:
        workload["volumeMounts"] = [dict(mount) for mount in volume_mounts]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": "demo",
            "uid": POD_UID,
            "creationTimestamp": created,
        },
        "spec": {
            "nodeName": "node02",
            "containers": [workload],
            "volumes": [dict(volume) for volume in volumes],
            "ephemeralContainers": [dict(entry) for entry in ephemeral],
        },
        "status": {
            "phase": phase,
            "containerStatuses": [
                {
                    "name": container,
                    "containerID": f"containerd://{TARGET_CID}",
                    "ready": ready,
                }
            ],
            "ephemeralContainerStatuses": [dict(s) for s in ephemeral_statuses],
        },
    }


def capreport_payload(**overrides: Any) -> dict[str, Any]:
    """The stable JSON shape ``capreport --json`` emits."""
    payload: dict[str, Any] = {
        "verdict": "live_attach",
        "exit_code": 0,
        "summary": "live attach available",
        "blocker": "none",
        "explanation": "nothing is blocking ptrace",
        "cap_sys_ptrace": True,
        "cap_bounding_sys_ptrace": True,
        "yama_scope": 1,
        "seccomp_mode": 2,
        "no_new_privs": False,
        "apparmor_profile": "cri-containerd.apparmor.d (enforce)",
        "self_uid": 0,
        "target_uid": 1000,
        "target_pid": 17,
        "node_name": "node02",
        "child_attach_ok": True,
        "target_attach_ok": True,
        "proc_reads": {"root": True, "maps": True},
        "notes": [],
    }
    payload.update(overrides)
    return payload


class FakeCluster:
    """A kubectl that answers from one mutable pod document."""

    def __init__(
        self,
        pod: dict[str, Any],
        *,
        others: Sequence[dict[str, Any]] = (),
        psa_denies_ptrace: bool = False,
        kubelet_refuses_root: bool = False,
        kubelet_refuses_root_image: bool = False,
        capreport: dict[str, Any] | None = None,
        host_key: str | None = HOST_KEY,
        login_user: str | None = SEAT_USER,
        login_user_returncode: int = 1,
        patch_error: str | None = None,
    ) -> None:
        self.pod = pod
        # The other pods in the namespace, which only the pod-name resolution
        # ever looks at: everything else here happens to one pod, and a
        # namespace with one pod in it cannot tell a substring match from a
        # name.
        self.others = [dict(other) for other in others]
        self.psa_denies_ptrace = psa_denies_ptrace
        self.kubelet_refuses_root = kubelet_refuses_root
        self.kubelet_refuses_root_image = kubelet_refuses_root_image
        self.capreport = capreport if capreport is not None else capreport_payload()
        self.capreport_output: str | None = None
        self.host_key = host_key
        self.login_user = login_user
        self.login_user_returncode = login_user_returncode
        self.patch_error = patch_error
        self.added: list[dict[str, Any]] = []
        self.calls: list[tuple[str, ...]] = []

    # -- Runner protocol ---------------------------------------------------

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
    ) -> CommandResult:
        self.calls.append(tuple(argv))
        rest = self._strip_global_flags(list(argv))
        result = self._dispatch(rest, stdin, argv)
        return CommandResult(
            argv=tuple(argv),
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    @staticmethod
    def _strip_global_flags(argv: list[str]) -> list[str]:
        rest = argv[1:]
        while rest and rest[0] in ("-n", "--namespace", "--context"):
            rest = rest[2:]
        return rest

    def _dispatch(
        self, rest: list[str], stdin: str | None, argv: Sequence[str]
    ) -> CommandResult:
        if rest[:1] == ["config"]:
            return _ok("demo\n")
        if rest[:2] == ["get", "pods"]:
            return _ok(json.dumps({"items": [self.pod, *self.others]}))
        if rest[:2] == ["get", "pod"]:
            found = self._named(rest[2])
            if found is None:
                # The API server's own words, because resolution reads the exit
                # code and everything else here reads the message.
                return _fail(
                    f'Error from server (NotFound): pods "{rest[2]}" not found'
                )
            if any(item.startswith("--subresource=") for item in rest):
                return _ok(
                    json.dumps(
                        {
                            "metadata": self.pod["metadata"],
                            "spec": {
                                "ephemeralContainers": self._ephemeral_specs(),
                            },
                        }
                    )
                )
            if rest[-2:] == ["-o", "name"]:
                return _ok(f"pod/{rest[2]}\n")
            return _ok(json.dumps(found))
        if rest[:2] == ["replace", "--raw"]:
            return self._add_ephemeral(stdin)
        if rest[:1] == ["exec"]:
            return self._exec(rest)
        if rest[:1] == ["patch"]:
            if self.patch_error is not None:
                return _fail(self.patch_error)
            return _ok("pod/target patched")
        raise AssertionError(f"unexpected kubectl call: {list(argv)}")

    # -- pod document ------------------------------------------------------

    def _named(self, name: str) -> dict[str, Any] | None:
        for pod in (self.pod, *self.others):
            if cast(dict[str, Any], pod["metadata"])["name"] == name:
                return pod
        return None

    def _ephemeral_specs(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.pod["spec"]["ephemeralContainers"])

    def _statuses(self) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]], self.pod["status"]["ephemeralContainerStatuses"]
        )

    def _add_ephemeral(self, stdin: str | None) -> CommandResult:
        assert stdin is not None, "the subresource PUT must carry a body"
        body = cast(dict[str, Any], json.loads(stdin))
        submitted = cast(list[dict[str, Any]], body["spec"]["ephemeralContainers"])
        known = {entry["name"] for entry in self._ephemeral_specs()}
        new = [entry for entry in submitted if entry["name"] not in known]
        assert len(new) == 1, f"expected exactly one new container, got {new}"
        spec = new[0]
        self.added.append(spec)

        security = cast(dict[str, Any], spec.get("securityContext", {}))
        added_caps = cast(
            list[str],
            cast(dict[str, Any], security.get("capabilities", {})).get("add", []),
        )
        if self.psa_denies_ptrace and "SYS_PTRACE" in added_caps:
            return _fail(PSA_REFUSAL, returncode=1)

        self._ephemeral_specs().append(spec)
        # The kubelet's two asynchronous refusals: a root container under the
        # pod's runAsNonRoot policy, and runAsNonRoot: true over an image whose
        # USER is root - which podbench's own image is.
        refused = (self.kubelet_refuses_root and security.get("runAsUser") == 0) or (
            self.kubelet_refuses_root_image
            and security.get("runAsNonRoot") is True
            and "runAsUser" not in security
        )
        if refused:
            self._statuses().append(
                {
                    "name": spec["name"],
                    "state": {
                        "waiting": {
                            "reason": "CreateContainerConfigError",
                            "message": KUBELET_REFUSAL,
                        }
                    },
                }
            )
        else:
            self._statuses().append(
                {
                    "name": spec["name"],
                    "state": {"running": {"startedAt": "2026-08-15T10:00:00Z"}},
                }
            )
        return _ok("")

    def _exec(self, rest: list[str]) -> CommandResult:
        command = rest[rest.index("--") + 1 :]
        if command[0] == "capreport":
            if self.capreport_output is not None:
                return _ok(self.capreport_output, returncode=127)
            return _ok(
                json.dumps(self.capreport),
                returncode=int(self.capreport["exit_code"]),
            )
        if "--print-host-key" in command:
            if self.host_key is None:
                return _fail("no host public key available")
            return _ok(self.host_key + "\n")
        if "--print-login-user" in command:
            # The agent's contract: the name on stdout, or exit 1 with the
            # mechanism and the way out on stderr. Exit 2 is argparse in an
            # image that predates the flag, which means "unknown", not "no".
            if self.login_user_returncode == 2:
                return _fail(OLD_IMAGE_REFUSAL, returncode=2)
            if self.login_user is None:
                return _fail(NO_LOGIN_USER)
            return _ok(self.login_user + "\n")
        raise AssertionError(f"unexpected exec: {command}")


def _ok(stdout: str, returncode: int = 0) -> CommandResult:
    return CommandResult((), returncode, stdout, "")


def _fail(stderr: str, returncode: int = 1) -> CommandResult:
    return CommandResult((), returncode, "", stderr)


def kubectl_for(cluster: FakeCluster) -> Kubectl:
    return Kubectl("demo", runner=cluster)


def security_contexts(cluster: FakeCluster) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], spec.get("securityContext", {})) for spec in cluster.added
    ]


def running_status(name: str) -> dict[str, Any]:
    return {"name": name, "state": {"running": {"startedAt": "2026-08-15T09:00:00Z"}}}


# -- the ladder -------------------------------------------------------------


def test_full_rung_lands_when_the_namespace_allows_it() -> None:
    cluster = FakeCluster(pod_document(uid=1000))
    session = attach(kubectl_for(cluster), "pod/target", public_key=CLIENT_KEY)

    assert session.rung is Rung.FULL
    assert session.seat == ContainerRef(PodRef("demo", "target"), "podbench-1")
    assert not session.reused
    assert security_contexts(cluster)[0] == {
        "runAsUser": 0,
        "capabilities": {"add": ["SYS_PTRACE"]},
    }
    env = {entry["name"]: entry["value"] for entry in cluster.added[0]["env"]}
    assert env["PODBENCH_TARGET_CID"] == TARGET_CID
    assert env["PODBENCH_SSH_PUBKEY"] == CLIENT_KEY
    assert env["PODBENCH_NODE_NAME"] == "node02"
    assert "HOME" not in env


def test_psa_refusal_falls_to_the_degraded_rung_without_burning_a_name() -> None:
    cluster = FakeCluster(pod_document(uid=1000), psa_denies_ptrace=True)
    session = attach(kubectl_for(cluster), "target")

    assert session.rung is Rung.DEGRADED
    # A synchronous refusal means the container was never created, so the name
    # it was submitted under is still free (report 3.18/4.2).
    assert session.seat.container == "podbench-1"
    assert session.uid == 1000
    assert security_contexts(cluster)[1] == {
        "capabilities": {"drop": ["ALL"]},
        "allowPrivilegeEscalation": False,
        "seccompProfile": {"type": "RuntimeDefault"},
        "runAsNonRoot": True,
        "runAsUser": 1000,
    }
    steps = {step.rung: step for step in session.steps}
    assert not steps[Rung.FULL].admitted
    assert "Pod Security Admission" in steps[Rung.FULL].detail


def test_kubelet_refusal_falls_through_and_takes_a_fresh_name() -> None:
    cluster = FakeCluster(pod_document(uid=1000), kubelet_refuses_root=True)
    session = attach(kubectl_for(cluster), "target", poll_interval=0.0)

    assert session.rung is Rung.DEGRADED
    # The rejected container exists and is unrestartable, so its name is gone.
    assert session.seat.container == "podbench-2"
    steps = {step.rung: step for step in session.steps}
    assert steps[Rung.FULL].container == "podbench-1"
    assert "CreateContainerConfigError" in steps[Rung.FULL].detail
    assert "runAsUser breaks non-root policy" in steps[Rung.FULL].detail


def test_run_as_non_root_pre_empts_the_full_rung_entirely() -> None:
    cluster = FakeCluster(pod_document(uid=1000, non_root=True))
    session = attach(kubectl_for(cluster), "target")

    assert session.rung is Rung.DEGRADED
    assert session.seat.container == "podbench-1"
    # Nothing carrying SYS_PTRACE was ever submitted: the refusal was read out
    # of the target's securityContext instead of provoked.
    assert all(
        "capabilities" not in ctx or "add" not in ctx["capabilities"]
        for ctx in security_contexts(cluster)
    )
    assert (
        "runAsNonRoot" in {step.rung: step for step in session.steps}[Rung.FULL].detail
    )


def test_a_root_target_never_produces_a_degraded_spec() -> None:
    cluster = FakeCluster(pod_document(uid=0), psa_denies_ptrace=True)
    session = attach(kubectl_for(cluster), "target")

    assert session.rung is Rung.SEAT
    assert session.uid is None
    contexts = security_contexts(cluster)
    assert len(contexts) == 2
    # runAsNonRoot: true beside runAsUser: 0 is admitted and then refused by the
    # kubelet; the seat rung must drop the uid rather than pair them.
    assert "runAsUser" not in contexts[1]
    degraded = {step.rung: step for step in session.steps}[Rung.DEGRADED]
    assert not degraded.admitted
    assert "root" in degraded.detail


def test_an_unknown_target_uid_skips_the_degraded_rung_rather_than_guessing() -> None:
    plan = dict(plan_ladder(pod_document(), "app"))
    degraded = plan[Rung.DEGRADED]
    assert plan[Rung.FULL] is None
    assert degraded is not None
    assert "--target-uid" in degraded
    assert plan[Rung.SEAT] is None


def test_target_uid_override_re_enables_the_degraded_rung() -> None:
    cluster = FakeCluster(pod_document(), psa_denies_ptrace=True)
    session = attach(kubectl_for(cluster), "target", target_uid=1000)

    assert session.rung is Rung.DEGRADED
    assert security_contexts(cluster)[1]["runAsUser"] == 1000


def test_an_exhausted_ladder_raises_with_every_reason() -> None:
    # A root target under runAsNonRoot: no full rung, no degraded rung, and a
    # seat whose runAsNonRoot: true the kubelet refuses over a root image.
    cluster = FakeCluster(
        pod_document(uid=0, non_root=True), kubelet_refuses_root_image=True
    )
    with pytest.raises(LauncherError) as raised:
        attach(kubectl_for(cluster), "target", poll_interval=0.0)
    message = str(raised.value)
    assert "runAsNonRoot" in message
    assert "the target runs as root" in message
    assert "CreateContainerConfigError" in message


# -- reconnection -----------------------------------------------------------


def test_attach_reconnects_to_a_running_container() -> None:
    existing = {
        "name": "podbench-1",
        "image": "ghcr.io/gilesknap/podbench:latest",
        "targetContainerName": "app",
        "securityContext": {"runAsUser": 0, "capabilities": {"add": ["SYS_PTRACE"]}},
    }
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[existing],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    session = attach(kubectl_for(cluster), "target")

    assert session.reused
    assert session.rung is Rung.FULL
    assert session.seat.container == "podbench-1"
    assert cluster.added == [], "reconnecting must not append a container"


def test_force_new_appends_the_next_name() -> None:
    existing = {
        "name": "podbench-1",
        "securityContext": {"runAsUser": 0, "capabilities": {"add": ["SYS_PTRACE"]}},
    }
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[existing],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    session = attach(kubectl_for(cluster), "target", force_new=True)

    assert not session.reused
    assert session.seat.container == "podbench-2"


def test_a_dead_container_is_not_reconnected_to() -> None:
    dead = {"name": "podbench-1", "securityContext": {"runAsUser": 1000}}
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[dead],
            ephemeral_statuses=[
                {
                    "name": "podbench-1",
                    "state": {"terminated": {"reason": "Completed", "exitCode": 0}},
                }
            ],
        )
    )
    session = attach(kubectl_for(cluster), "target")

    assert not session.reused
    assert session.seat.container == "podbench-2"
    listed = {seat.name: seat for seat in seats(cluster.pod)}
    assert listed["podbench-1"].phase == "terminated"
    assert "burnt" in listed["podbench-1"].detail


# -- mounting the pod's own volumes (Patch mode) ----------------------------

PATCH_VOLUME: dict[str, Any] = {
    "name": "podbench-patch-venv",
    "persistentVolumeClaim": {"claimName": "myapp-venv"},
}
APP_MOUNT: dict[str, Any] = {"name": "podbench-patch-venv", "mountPath": "/opt/venv"}


def patch_pod(**overrides: Any) -> dict[str, Any]:
    """A pod wired for Patch mode: the claim is a volume, the app mounts it."""
    settings: dict[str, Any] = {
        "uid": 1000,
        "volumes": [PATCH_VOLUME],
        "volume_mounts": [APP_MOUNT],
    }
    settings.update(overrides)
    return pod_document(**settings)


def test_a_mount_named_by_claim_lands_at_the_applications_own_path() -> None:
    cluster = FakeCluster(patch_pod())
    session = attach(kubectl_for(cluster), "target", mounts=["myapp-venv"])

    # The mount refers to the pod's *volume* entry, but the user has the claim
    # name in hand, so both are accepted.
    assert cluster.added[0]["volumeMounts"] == [
        {"name": "podbench-patch-venv", "mountPath": "/opt/venv"}
    ]
    # Patch mode needs the seat and the application to agree on the path, so it
    # is copied rather than asked for again.
    assert not [w for w in session.warnings if "mountPath" in w]


def test_an_application_sub_path_is_refused_rather_than_copied_or_dropped() -> None:
    """Neither half of "copy it or drop it" is available to an ephemeral seat.

    Copying is forbidden — the API server refuses ``subPath`` on an ephemeral
    container for the whole request — and dropping it would point the seat at
    the volume root where the application sees one directory inside it, so every
    path Patch mode recorded would resolve to the wrong thing. So it refuses,
    before a container name is burnt.
    """
    cluster = FakeCluster(
        patch_pod(
            volume_mounts=[
                {
                    "name": "podbench-patch-venv",
                    "mountPath": "/opt/venv",
                    "subPath": "venv",
                }
            ]
        )
    )
    with pytest.raises(LauncherError) as raised:
        attach(kubectl_for(cluster), "target", mounts=["podbench-patch-venv"])

    message = str(raised.value)
    assert "subPath" in message
    assert "Ephemeral Container" in message
    assert cluster.added == []


def test_a_volume_the_pod_does_not_declare_is_refused_before_anything_is_created() -> (
    None
):
    cluster = FakeCluster(pod_document(uid=1000))
    with pytest.raises(LauncherError) as raised:
        attach(kubectl_for(cluster), "target", mounts=["myapp-venv:/opt/venv"])

    message = str(raised.value)
    assert "myapp-venv" in message
    # The refusal has to explain that this is not podbench being unhelpful: a
    # pod's volumes are immutable, which is why Patch mode needs the chart.
    assert "immutable" in message
    assert "--print-values" in message
    assert cluster.added == [], "nothing may be submitted, and no name burnt"


def test_an_explicit_mount_path_that_disagrees_is_warned_about() -> None:
    cluster = FakeCluster(patch_pod())
    session = attach(
        kubectl_for(cluster), "target", mounts=["myapp-venv:/srv/venv"], probe=False
    )

    assert cluster.added[0]["volumeMounts"] == [
        {"name": "podbench-patch-venv", "mountPath": "/srv/venv"}
    ]
    warning = next(w for w in session.warnings if "/srv/venv" in w)
    assert "/opt/venv" in warning
    assert "different venv" in warning


def test_a_volume_the_application_does_not_mount_needs_an_explicit_path() -> None:
    cluster = FakeCluster(pod_document(uid=1000, volumes=[PATCH_VOLUME]))
    with pytest.raises(LauncherError, match="no mountPath to copy"):
        attach(kubectl_for(cluster), "target", mounts=["myapp-venv"])

    session = attach(kubectl_for(cluster), "target", mounts=["myapp-venv:/opt/venv"])
    assert session.rung is Rung.FULL
    assert cluster.added[0]["volumeMounts"] == [
        {"name": "podbench-patch-venv", "mountPath": "/opt/venv"}
    ]


def test_reconnecting_cannot_add_a_mount_and_says_so() -> None:
    existing = {
        "name": "podbench-1",
        "securityContext": {"runAsUser": 0, "capabilities": {"add": ["SYS_PTRACE"]}},
    }
    cluster = FakeCluster(
        patch_pod(
            ephemeral=[existing],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    session = attach(kubectl_for(cluster), "target", mounts=["myapp-venv"])

    assert session.reused
    assert cluster.added == []
    assert any("--new" in warning for warning in session.warnings)


def test_a_relative_mount_path_is_refused() -> None:
    with pytest.raises(LauncherError, match="absolute"):
        parse_mount("myapp-venv:opt/venv")


def test_attach_mount_is_repeatable_on_the_command_line(tmp_path: Path) -> None:
    logs = {"name": "logs", "persistentVolumeClaim": {"claimName": "myapp-logs"}}
    cluster = FakeCluster(
        patch_pod(
            volumes=[PATCH_VOLUME, logs],
            volume_mounts=[APP_MOUNT, {"name": "logs", "mountPath": "/var/log/app"}],
        )
    )
    code = main(
        [
            "attach",
            "target",
            "-n",
            "demo",
            "--mount",
            "myapp-venv",
            "--mount",
            "logs",
            "--identity",
            identity(tmp_path),
            "--config-dir",
            str(tmp_path / "cfg"),
        ],
        runner=cluster,
    )
    assert code == 0
    assert cluster.added[0]["volumeMounts"] == [
        {"name": "podbench-patch-venv", "mountPath": "/opt/venv"},
        {"name": "logs", "mountPath": "/var/log/app"},
    ]


# -- the seat's identity, and the volume attach cannot use for it -----------

IDENTITY_VOLUME: dict[str, Any] = {
    "name": SEAT_IDENTITY_VOLUME,
    "configMap": {"name": "myapp-podbench-identity"},
}
HOME_VOLUME: dict[str, Any] = {
    "name": SEAT_HOME_VOLUME,
    "persistentVolumeClaim": {"claimName": "myapp-podbench-home"},
}

# The literal path, spelled out once. Everything else in this file uses the
# constants, but the point of this mount is that the launcher, the chart's
# ConfigMap and the image's NSS agree on exact strings — so the contract is
# worth one assertion that would fail if any of them moved.
EXPECTED_SEAT_MOUNTS: list[dict[str, Any]] = [
    {"name": "podbench-home", "mountPath": "/home/podbench"},
]

# What an *ordinary* container's identity mounts look like, and what an
# ephemeral one may never carry: subPath is refused by the API server there.
# Kept as a fixture because a seat can still be found carrying them — a
# `podbench dev` sidecar, or one landed by a launcher old enough to have tried.
IDENTITY_MOUNTS: list[dict[str, Any]] = [
    {
        "name": "podbench-identity",
        "mountPath": "/etc/passwd",
        "subPath": "passwd",
        "readOnly": True,
    },
    {
        "name": "podbench-identity",
        "mountPath": "/etc/group",
        "subPath": "group",
        "readOnly": True,
    },
]


def identity_pod(**overrides: Any) -> dict[str, Any]:
    """A pod deployed with the seat-identity cooperation: uid 1000, both volumes."""
    settings: dict[str, Any] = {
        "uid": 1000,
        "non_root": True,
        "volumes": [IDENTITY_VOLUME, HOME_VOLUME],
    }
    settings.update(overrides)
    return pod_document(**settings)


def test_the_home_volume_is_mounted_by_convention_not_by_flag() -> None:
    """No flag, because the volume cannot be there by accident.

    ``spec.volumes`` is immutable, so anything named ``podbench-home`` was put
    in the pod at deploy time on purpose. Making the user repeat that intent on
    every attach would waste the cooperation.
    """
    cluster = FakeCluster(identity_pod())
    session = attach(kubectl_for(cluster), "target")

    assert session.rung is Rung.DEGRADED
    assert cluster.added[0]["volumeMounts"] == EXPECTED_SEAT_MOUNTS
    env = {entry["name"]: entry["value"] for entry in cluster.added[0]["env"]}
    # The home volume is only worth mounting if the seat is pointed at it: it is
    # what keeps vscode-server off the pod's ephemeral-storage budget.
    assert env["HOME"] == SEAT_HOME_PATH


def test_the_identity_volume_is_never_mounted_into_an_ephemeral_seat() -> None:
    """The regression: a subPath mount fails the whole attach, not just itself.

    ``kubectl ... replace --raw .../ephemeralcontainers`` comes back with
    ``spec.ephemeralContainers[0].volumeMounts[0].subPath: Forbidden: cannot be
    set for an Ephemeral Container`` and no seat lands at all — so authoring the
    identity here does not degrade an attach against a prepared pod, it breaks
    the headline command against precisely the pod the chart teaches people to
    produce.
    """
    cluster = FakeCluster(identity_pod())
    session = attach(kubectl_for(cluster), "target")

    mounts = cast(list[dict[str, Any]], cluster.added[0]["volumeMounts"])
    assert not [mount for mount in mounts if mount["name"] == SEAT_IDENTITY_VOLUME]
    assert not [mount for mount in mounts if "subPath" in mount]
    assert not session.identity_mounted
    # The pod's half of the cooperation is still recorded: the report needs it
    # to explain why the volume the user deployed is doing nothing here.
    assert session.identity_declared


def test_a_pod_that_declares_neither_volume_still_attaches() -> None:
    """A bare attach against an uncooperative pod degrades, it does not fail."""
    cluster = FakeCluster(pod_document(uid=1000))
    session = attach(kubectl_for(cluster), "target")

    assert "volumeMounts" not in cluster.added[0]
    assert not session.identity_mounted
    assert not session.identity_declared
    assert session.rung is Rung.FULL


def test_no_seat_identity_opts_out(tmp_path: Path) -> None:
    cluster = FakeCluster(identity_pod())
    code = main(
        [
            "attach",
            "target",
            "-n",
            "demo",
            "--no-seat-identity",
            "--identity",
            identity(tmp_path),
            "--config-dir",
            str(tmp_path / "cfg"),
        ],
        runner=cluster,
    )
    assert code == 0
    assert "volumeMounts" not in cluster.added[0]
    env = {entry["name"]: entry["value"] for entry in cluster.added[0]["env"]}
    assert env["HOME"] == "/tmp/podbench-home", "the fallback home, not the volume's"


def test_an_explicit_mount_of_the_same_path_wins_over_the_convention() -> None:
    """Two mounts of one mountPath is not a tie the kubelet breaks sensibly."""
    cluster = FakeCluster(identity_pod())
    attach(
        kubectl_for(cluster),
        "target",
        mounts=[f"{SEAT_HOME_VOLUME}:{SEAT_HOME_PATH}"],
    )

    mounts = cast(list[dict[str, Any]], cluster.added[0]["volumeMounts"])
    assert mounts == [{"name": SEAT_HOME_VOLUME, "mountPath": SEAT_HOME_PATH}]


def test_an_explicit_mount_of_the_identity_volume_gets_no_subpath_from_podbench() -> (
    None
):
    """A user may still mount the volume whole; podbench never adds a subPath.

    Mounting a ConfigMap over ``/etc/passwd`` puts a *directory* there and is
    almost certainly not what anyone wants — but it is admissible, it is what
    was typed, and the alternative is podbench quietly authoring the one field
    the API server refuses on an ephemeral container.
    """
    cluster = FakeCluster(identity_pod())
    attach(
        kubectl_for(cluster),
        "target",
        mounts=[f"{SEAT_IDENTITY_VOLUME}:/etc/passwd"],
    )

    mounts = cast(list[dict[str, Any]], cluster.added[0]["volumeMounts"])
    assert mounts == [
        {"name": SEAT_IDENTITY_VOLUME, "mountPath": "/etc/passwd"},
        *EXPECTED_SEAT_MOUNTS,
    ]


def test_a_pod_with_only_the_identity_volume_still_lands_a_seat() -> None:
    """Nothing to mount, and the attach is unaffected by the volume being there."""
    cluster = FakeCluster(identity_pod(volumes=[IDENTITY_VOLUME]))
    session = attach(kubectl_for(cluster), "target")

    assert "volumeMounts" not in cluster.added[0]
    assert session.identity_declared
    env = {entry["name"]: entry["value"] for entry in cluster.added[0]["env"]}
    assert env["HOME"] == "/tmp/podbench-home", "the fallback home, not the volume's"


def test_the_report_says_why_the_declared_identity_volume_is_unused() -> None:
    """Where the user is looking, on the line the volume was meant to decide.

    A pod that declares ``podbench-identity`` is one somebody prepared for
    podbench, so silence here reads as "the preparation failed". It did not: the
    projection is impossible for an ephemeral container, and the live-pod route
    to the same identity is ``--seat-gid-root``.
    """
    cluster = FakeCluster(identity_pod(), login_user=None)
    session = attach(kubectl_for(cluster), "target")

    ssh_seat = features(session)[-2]
    assert not ssh_seat.available
    assert SEAT_IDENTITY_VOLUME in ssh_seat.note
    assert "subPath" in ssh_seat.note
    assert "--seat-gid-root" in ssh_seat.note

    text = format_session(session)
    assert SEAT_IDENTITY_VOLUME in text
    assert "--seat-gid-root" in text

    # …and a pod without the volume says nothing of the sort.
    plain = attach(kubectl_for(FakeCluster(pod_document(uid=1000))), "target")
    assert SEAT_IDENTITY_VOLUME not in format_session(plain)


def test_a_seat_that_already_has_a_login_is_not_told_to_re_attach() -> None:
    """The same fact, without a remedy under a ticked box.

    ``--seat-gid-root`` printed beside a working ssh seat reads as an
    instruction to fix something that is not broken - and the seat cannot pick
    the volume up on a re-attach anyway.
    """
    cluster = FakeCluster(identity_pod())
    session = attach(kubectl_for(cluster), "target")

    ssh_seat = features(session)[-2]
    assert ssh_seat.available
    assert SEAT_IDENTITY_VOLUME in ssh_seat.note
    assert "re-attach" not in ssh_seat.note


def test_a_seat_that_does_carry_the_identity_is_still_credited_with_it() -> None:
    """An ordinary container may subPath, so such a seat can exist - via `dev`.

    Read back from the spec rather than assumed from the pod's volumes, which is
    what keeps "this seat has the identity" and "the pod declares the volume"
    two separate answers.
    """
    existing = {
        "name": "podbench-1",
        "securityContext": {"runAsUser": 1000},
        "env": [{"name": "HOME", "value": SEAT_HOME_PATH}],
        "volumeMounts": [*IDENTITY_MOUNTS, *EXPECTED_SEAT_MOUNTS],
    }
    cluster = FakeCluster(
        identity_pod(
            ephemeral=[existing],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    session = attach(kubectl_for(cluster), "target")

    assert session.identity_mounted
    note = features(session)[-2].note
    assert "mounted read-only" in note
    assert "--seat-gid-root" not in note


def test_the_proxy_command_follows_the_mounted_home(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """sshd's config path is derived from $HOME, and the volume moves it.

    A ProxyCommand naming the config file in the home the seat *would* have had
    fails at the first connection with nothing in the error to say why.
    """
    cluster = FakeCluster(identity_pod())
    assert (
        main(
            [
                "attach",
                "target",
                "-n",
                "demo",
                "--identity",
                identity(tmp_path),
                "--config-dir",
                str(tmp_path / "cfg"),
                "--print-config",
            ],
            runner=cluster,
        )
        == 0
    )
    assert f"{SEAT_HOME_PATH}/.podbench/sshd_config" in capsys.readouterr().out


def test_a_reconnect_is_not_told_to_relaunch_for_the_identity_volume() -> None:
    """``--new`` would pick nothing up: no seat of any age can mount it.

    This used to be a warning telling the user to land a fresh seat, which was
    advice that could not work - a new ephemeral container is refused the
    subPath just as the old one would have been. The report carries the real
    explanation instead, once, for reconnects and new seats alike.
    """
    existing = {"name": "podbench-1", "securityContext": {"runAsUser": 1000}}
    cluster = FakeCluster(
        identity_pod(
            ephemeral=[existing],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    session = attach(kubectl_for(cluster), "target")

    assert session.reused
    assert not session.identity_mounted
    assert cluster.added == []
    assert not [w for w in session.warnings if "--new" in w]
    assert session.identity_declared
    assert SEAT_IDENTITY_VOLUME in features(session)[-2].note


def test_reconnecting_to_a_seat_reads_its_home_back_from_the_spec() -> None:
    existing = {
        "name": "podbench-1",
        "securityContext": {"runAsUser": 1000},
        "env": [{"name": "HOME", "value": SEAT_HOME_PATH}],
        "volumeMounts": EXPECTED_SEAT_MOUNTS,
    }
    cluster = FakeCluster(
        identity_pod(
            ephemeral=[existing],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    session = attach(kubectl_for(cluster), "target")

    assert session.reused
    # Read back from the spec, so a reconnect from another machine — which
    # remembers nothing of the original attach — describes the seat correctly.
    assert session.home == SEAT_HOME_PATH
    assert not [w for w in session.warnings if "--new" in w]


# -- the capability report --------------------------------------------------


def test_the_report_names_the_mechanism_that_blocked_attach() -> None:
    cluster = FakeCluster(
        pod_document(uid=1000),
        psa_denies_ptrace=True,
        capreport=capreport_payload(
            verdict="read_only",
            exit_code=10,
            blocker="yama-scope",
            cap_sys_ptrace=False,
            self_uid=1000,
            notes=["no Yama LSM on this node"],
        ),
    )
    session = attach(kubectl_for(cluster), "target")
    report = session.report
    assert report is not None
    assert report.verdict is Verdict.READ_ONLY
    assert report.blocker is Blocker.YAMA_SCOPE

    live, read_only, iterate, ssh_seat, exec_seat = features(session)
    assert not live.available
    assert "Yama" in live.reason
    assert read_only.available
    assert not iterate.available
    assert ssh_seat.available
    assert exec_seat.available

    text = format_session(session)
    assert "ptrace_scope" in text
    assert "node02" in text
    assert "OOM-kill" in text, "the shared-limits warning is not optional"


def test_a_missing_capreport_is_reported_rather_than_assumed() -> None:
    cluster = FakeCluster(pod_document(uid=1000))
    cluster.capreport_output = "capreport: command not found"
    session = attach(kubectl_for(cluster), "target")
    assert session.report is None
    text = format_session(session)
    assert "not measured" in text
    assert "command not found" in text


def test_capability_report_from_json_survives_an_unknown_blocker() -> None:
    report = capability_report_from_json(
        capreport_payload(blocker="cgroup-devices", explanation="a newer image")
    )
    assert report.blocker is Blocker.UNKNOWN
    assert any("cgroup-devices" in note for note in report.notes)


def test_capability_report_from_json_keeps_an_unreadable_target_uid_none() -> None:
    report = capability_report_from_json(
        capreport_payload(target_uid=None, target_pid=None, yama_scope=None)
    )
    assert report.target_uid is None
    assert report.yama_scope is None


# -- ssh config -------------------------------------------------------------


def identity(tmp_path: Path) -> str:
    key = tmp_path / "id_ed25519"
    key.write_text("PRIVATE")
    key.with_suffix(".pub").write_text(CLIENT_KEY + "\n")
    return str(key)


def test_attach_writes_an_includeable_stanza_and_a_known_hosts_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster = FakeCluster(pod_document(uid=1000))
    code = main(
        [
            "attach",
            "pod/target",
            "-n",
            "demo",
            "--identity",
            identity(tmp_path),
            "--config-dir",
            str(tmp_path / "cfg"),
        ],
        runner=cluster,
    )
    assert code == 0

    stanza = (tmp_path / "cfg" / "config.d" / "demo-target.conf").read_text()
    assert "Host podbench-demo-target" in stanza
    assert f"HostKeyAlias podbench-{POD_UID}" in stanza
    assert "StrictHostKeyChecking yes" in stanza
    # The ProxyCommand shape is the whole transport: -i, -e and a quiet log
    # level, no -t, no redirection (report 4.1).
    assert (
        "ProxyCommand kubectl -n demo exec -i target -c podbench-1 -- "
        "/usr/sbin/sshd -i -e -f /etc/podbench/sshd_config -o LogLevel=ERROR"
    ) in stanza

    known_hosts = (tmp_path / "cfg" / "known_hosts").read_text()
    assert known_hosts.startswith(f"podbench-{POD_UID} ssh-ed25519 ")
    assert "Include" in capsys.readouterr().out


def test_a_degraded_seat_points_at_the_non_root_sshd_layout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster = FakeCluster(pod_document(uid=1000), psa_denies_ptrace=True)
    assert (
        main(
            [
                "attach",
                "target",
                "-n",
                "demo",
                "--identity",
                identity(tmp_path),
                "--config-dir",
                str(tmp_path / "cfg"),
                "--print-config",
            ],
            runner=cluster,
        )
        == 0
    )
    out = capsys.readouterr().out
    # $HOME is pinned in the container spec precisely so this path is knowable.
    assert "/tmp/podbench-home/.podbench/sshd_config" in out
    env = {entry["name"]: entry["value"] for entry in cluster.added[1]["env"]}
    assert env["HOME"] == "/tmp/podbench-home"


def test_ssh_config_subcommand_regenerates_for_an_existing_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = {
        "name": "podbench-1",
        "securityContext": {"runAsUser": 0, "capabilities": {"add": ["SYS_PTRACE"]}},
    }
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[existing],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    code = main(
        [
            "ssh-config",
            "target",
            "-n",
            "demo",
            "--identity",
            identity(tmp_path),
            "--config-dir",
            str(tmp_path / "cfg"),
            "--print-config",
        ],
        runner=cluster,
    )
    assert code == 0
    assert "-c podbench-1" in capsys.readouterr().out
    assert cluster.added == []


def test_a_seat_with_no_login_identity_gets_a_reason_not_a_stanza(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The honest end of the degraded rung.

    The seat runs as the target's uid, no account for it exists in the image
    and ``/etc/passwd`` is unwritable, so sshd would refuse the login before it
    ever looked at a key. A stanza printed anyway would spend the user's next
    hour on ``Permission denied (publickey)`` — pointing at their key rather
    than at the mechanism. Attach still succeeds: the seat is real, it is just
    reachable by ``kubectl exec`` alone.
    """
    cluster = FakeCluster(
        pod_document(uid=1000), psa_denies_ptrace=True, login_user=None
    )
    code = main(
        [
            "attach",
            "target",
            "-n",
            "demo",
            "--identity",
            identity(tmp_path),
            "--config-dir",
            str(tmp_path / "cfg"),
        ],
        runner=cluster,
    )
    assert code == 0, "a seat without ssh is still a seat"
    out = capsys.readouterr().out

    assert "no ssh config was written" in out
    assert "ProxyCommand" not in out, "a stanza that cannot work must not be printed"
    assert not (tmp_path / "cfg" / "config.d").exists()
    # Named mechanism, then the way out, then what does work today.
    assert "not writable" in out
    assert "--seat-gid-root" in out
    assert "kubectl exec -n demo target -c podbench-1 -- capreport" in out


def test_the_capability_report_marks_the_ssh_seat_unavailable() -> None:
    cluster = FakeCluster(
        pod_document(uid=1000), psa_denies_ptrace=True, login_user=None
    )
    session = attach(kubectl_for(cluster), "target")

    assert session.ssh is not None and not session.ssh.usable
    ssh_seat, exec_seat = features(session)[-2:]
    assert not ssh_seat.available
    assert "not writable" in ssh_seat.reason
    # The half that never needed sshd is still claimed, because it still works.
    assert exec_seat.available

    text = format_session(session)
    assert "[ ] ssh seat" in text
    assert "[x] exec seat" in text


def test_an_image_that_cannot_answer_still_gets_its_stanza(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "Unknown" is not "no".

    An image older than this launcher exits 2 from argparse rather than 1 from
    the agent. Withholding the stanza there would break every seat that works
    today — including root ones, where the login name is ``root`` and always
    resolves — in the name of a question the image was never asked.
    """
    cluster = FakeCluster(pod_document(uid=1000), login_user_returncode=2)
    code = main(
        [
            "attach",
            "target",
            "-n",
            "demo",
            "--identity",
            identity(tmp_path),
            "--config-dir",
            str(tmp_path / "cfg"),
            "--print-config",
        ],
        runner=cluster,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "ProxyCommand" in out
    # …but it is not claimed as measured either.
    assert "[ ] ssh seat" in out
    assert "not measured" in out


def test_seat_gid_root_lands_a_seat_that_can_register_itself() -> None:
    """Opt-in, and the only difference it makes to the spec is the group."""
    cluster = FakeCluster(pod_document(uid=1000, non_root=True))
    session = attach(kubectl_for(cluster), "target", seat_gid_root=True)

    assert session.rung is Rung.DEGRADED
    assert security_contexts(cluster)[0] == {
        "capabilities": {"drop": ["ALL"]},
        "allowPrivilegeEscalation": False,
        "seccompProfile": {"type": "RuntimeDefault"},
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 0,
    }


def test_the_default_seat_keeps_the_targets_group() -> None:
    cluster = FakeCluster(pod_document(uid=1000, non_root=True))
    attach(kubectl_for(cluster), "target")
    assert "runAsGroup" not in security_contexts(cluster)[0]


def test_reconnecting_cannot_change_the_seats_group_and_says_so() -> None:
    existing = {
        "name": "podbench-1",
        "securityContext": {"runAsUser": 1000},
    }
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[existing],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    session = attach(kubectl_for(cluster), "target", seat_gid_root=True)
    assert session.reused
    assert any("--seat-gid-root" in warning for warning in session.warnings)


def test_ssh_config_without_a_session_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster = FakeCluster(pod_document(uid=1000))
    code = main(
        ["ssh-config", "target", "-n", "demo", "--identity", identity(tmp_path)],
        runner=cluster,
    )
    assert code == 2
    assert "attach" in capsys.readouterr().err


def test_a_missing_public_key_is_a_message_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster = FakeCluster(pod_document(uid=1000))
    code = main(
        ["attach", "target", "-n", "demo", "--identity", str(tmp_path / "absent")],
        runner=cluster,
    )
    assert code == 2
    assert "ssh-keygen" in capsys.readouterr().err


def test_the_stanza_generator_is_shared_and_takes_a_measured_login_name(
    tmp_path: Path,
) -> None:
    """One generator, two callers.

    ``podbench dev``'s sidecar is an ordinary container whose login name comes
    from a projected passwd record, so it names the user explicitly rather than
    taking the rung's default. Everything else — the host-key probe, the
    ``known_hosts`` entry, the ProxyCommand, the ``Include`` advice — is the
    same code as ``attach``, which is the point of it being a function.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    kubectl = kubectl_for(cluster)
    session = attach(kubectl, "target")

    seat = emit_ssh_config(
        kubectl,
        session,
        identity=identity(tmp_path),
        config_dir=str(tmp_path / "cfg"),
        user="somebody-else",
    )

    assert seat.alias == "podbench-demo-target"
    assert seat.path is not None
    stanza = seat.path.read_text()
    assert "User somebody-else" in stanza
    assert "ProxyCommand" in stanza
    assert f"then:  ssh {seat.alias}" in seat.note
    assert seat.path == ssh_config_path(tmp_path / "cfg", PodRef("demo", "target"))


def test_forgetting_a_seat_takes_its_stanza_and_its_pinned_key(
    tmp_path: Path,
) -> None:
    """What ``dev --delete`` calls, and what ``attach`` deliberately does not.

    An attach seat is reconnectable for its pod's lifetime; a deleted dev pod is
    not, and its alias is keyed on a UID no pod will ever have again.
    """
    directory = tmp_path / "cfg"
    pod = PodRef("demo", "target")
    path = ssh_config_path(directory, pod)
    path.parent.mkdir(parents=True)
    path.write_text("Host podbench-demo-target\n")
    known_hosts = directory / "known_hosts"
    known_hosts.write_text(f"podbench-{POD_UID} ssh-ed25519 AAAA\nother ssh-rsa BBBB\n")

    removed = forget_ssh_config(pod, directory=directory, alias=f"podbench-{POD_UID}")

    assert not path.exists()
    assert known_hosts.read_text() == "other ssh-rsa BBBB\n"
    assert len(removed) == 2
    # Idempotent: a second teardown of the same pod says nothing and fails at
    # nothing.
    assert (
        forget_ssh_config(pod, directory=directory, alias=f"podbench-{POD_UID}") == []
    )


def test_forgetting_the_last_pinned_key_removes_the_file(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("podbench-abc ssh-ed25519 AAAA\n")
    assert forget_known_hosts("podbench-abc", known_hosts)
    assert not known_hosts.exists()
    assert not forget_known_hosts("podbench-abc", known_hosts)


# -- choosing which pod the user meant --------------------------------------

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
"""Three hours after every pod_document is created, so the age column is fixed."""


def namespace_of(*names: str, **overrides: Any) -> FakeCluster:
    """A namespace whose pods differ only in name, for the matching tests."""
    first, *rest = names
    return FakeCluster(
        pod_document(name=first, **overrides),
        others=[pod_document(name=name, **overrides) for name in rest],
    )


def answers(*lines: str) -> Callable[[], str]:
    """The prompt's reader, spelled as a script rather than a terminal.

    An exhausted script raises ``StopIteration`` rather than blocking, so a
    prompt loop that never accepts an answer fails the test instead of hanging
    it.
    """
    return iter(lines).__next__


def test_an_exact_name_wins_outright_and_never_lists_the_namespace() -> None:
    # `api` is a substring of `api-canary`, and typing it in full is still not a
    # question. It is also answered by one `get pod`, which is what keeps the
    # long form working for a user whose RBAC has get on pods but not list.
    cluster = namespace_of("api", "api-canary")
    assert resolve_pod(kubectl_for(cluster), "api", interactive=False) == "api"
    assert not any("pods" in call for call in cluster.calls)


def test_the_pod_slash_name_form_still_resolves(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = namespace_of("api-7f9", "web-3c1")
    kube = kubectl_for(cluster)
    assert resolve_pod(kube, "pod/api-7f9", interactive=False) == "api-7f9"
    # And the kind prefix is stripped before the substring search too, so
    # `pod/api` narrows exactly as `api` does.
    assert resolve_pod(kube, "pod/api", interactive=False) == "api-7f9"
    assert "api-7f9" in capsys.readouterr().err


def test_one_substring_hit_resolves_and_says_what_it_resolved_to(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = namespace_of("web-6c9d7f4b8b-hq2vn", "api-7f9")
    name = resolve_pod(kubectl_for(cluster), "hq2", interactive=False)
    assert name == "web-6c9d7f4b8b-hq2vn"
    # On stderr, so a redirected stdout still carries only the report.
    err = capsys.readouterr().err
    assert "web-6c9d7f4b8b-hq2vn" in err
    assert capsys.readouterr().out == ""


def test_several_hits_are_listed_with_enough_to_choose_by(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = FakeCluster(
        pod_document(
            name="api-7f9",
            ephemeral=[{"name": "podbench-1"}],
            ephemeral_statuses=[running_status("podbench-1")],
        ),
        others=[pod_document(name="api-canary", phase="Pending", ready=False)],
    )
    name = resolve_pod(
        kubectl_for(cluster),
        "api",
        interactive=True,
        ask=answers("2"),
        now=NOW,
    )
    assert name == "api-canary"
    listing = capsys.readouterr().err
    # Name alone is not a choice: the whole point of the prompt is the columns
    # beside it, including the seat that is already there.
    assert "1.  api-7f9     1/1    Running  3h   podbench-1" in listing
    assert "2.  api-canary  0/1    Pending  3h   -" in listing


def test_the_prompt_takes_a_name_or_a_narrower_substring_too() -> None:
    cluster = namespace_of("api-7f9", "api-canary")
    kube = kubectl_for(cluster)
    assert (
        resolve_pod(kube, "api", interactive=True, ask=answers("api-canary"))
        == "api-canary"
    )
    assert resolve_pod(kube, "api", interactive=True, ask=answers("7f9")) == "api-7f9"


def test_an_answer_that_is_still_ambiguous_is_asked_again(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = namespace_of("api-7f9", "api-canary", "web-3c1")
    name = resolve_pod(
        kubectl_for(cluster), "api", interactive=True, ask=answers("nope", "api", "1")
    )
    assert name == "api-7f9"
    err = capsys.readouterr().err
    assert "'nope' is not one of the choices" in err
    assert "'api' still matches 2 of them" in err


def test_an_empty_answer_cancels_rather_than_picking_the_first() -> None:
    # Every verb behind this mutates a pod permanently, so a shrug is not a
    # default.
    cluster = namespace_of("api-7f9", "api-canary")
    with pytest.raises(LauncherError, match="no pod chosen"):
        resolve_pod(kubectl_for(cluster), "api", interactive=True, ask=answers(""))


def test_no_argument_offers_every_pod_in_the_namespace_not_only_seats() -> None:
    # The difference between this and `podbench list`: list enumerates the pods
    # that already carry a seat, and this one enumerates the pods that could.
    cluster = namespace_of("api-7f9", "web-3c1")
    name = resolve_pod(kubectl_for(cluster), None, interactive=True, ask=answers("2"))
    assert name == "web-3c1"


def test_no_argument_in_a_one_pod_namespace_resolves_without_asking(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # There is nothing to disambiguate, so there is nothing to ask — but the
    # echo still has to read as English. It used to interpolate the absent
    # query and say "None matched pod only-pod".
    cluster = namespace_of("only-pod")
    assert resolve_pod(kubectl_for(cluster), None, interactive=False) == "only-pod"
    err = capsys.readouterr().err
    assert "only-pod" in err
    assert "None" not in err


def test_nothing_matches_names_the_namespace_and_shows_what_is_there() -> None:
    cluster = namespace_of("api-7f9", "web-3c1")
    with pytest.raises(LauncherError) as caught:
        resolve_pod(kubectl_for(cluster), "postgres", interactive=False)
    message = str(caught.value)
    assert "'postgres'" in message
    assert "namespace demo" in message
    assert "api-7f9" in message and "web-3c1" in message


def empty_namespace(
    argv: Sequence[str], *, stdin: str | None = None, capture: bool = True
) -> CommandResult:
    """A namespace kubectl finds nothing in — the one shape FakeCluster, which
    is built around a pod, cannot express."""
    if list(argv[-3:]) == ["pods", "-o", "json"]:
        return CommandResult(tuple(argv), 0, json.dumps({"items": []}), "")
    return CommandResult(tuple(argv), 1, "", "Error from server (NotFound)")


def test_an_empty_namespace_says_so_rather_than_prompting_for_nothing() -> None:
    kube = Kubectl("demo", runner=empty_namespace)
    with pytest.raises(LauncherError, match="namespace demo has no pods"):
        resolve_pod(kube, None, interactive=True, ask=answers("1"))


def test_a_pipe_is_refused_with_the_candidates_rather_than_hung() -> None:
    # The case that matters: podbench is scripted and run over ssh, where a
    # prompt is not a prompt but a hang.
    cluster = namespace_of("api-7f9", "api-canary")
    with pytest.raises(LauncherError) as caught:
        resolve_pod(kubectl_for(cluster), "api", interactive=False)
    message = str(caught.value)
    assert "not a tty" in message
    assert "api-7f9" in message and "api-canary" in message
    assert "name one exactly" in message


def test_no_prompt_refuses_on_a_terminal_too() -> None:
    cluster = namespace_of("api-7f9", "api-canary")
    with pytest.raises(LauncherError, match="--no-prompt was given"):
        resolve_pod(
            kubectl_for(cluster),
            "api",
            prompt=False,
            interactive=True,
            ask=answers("1"),
        )


def test_the_verbs_share_one_seam(capsys: pytest.CaptureFixture[str]) -> None:
    cluster = FakeCluster(
        pod_document(
            name="api-7f9",
            uid=1000,
            ephemeral=[{"name": "podbench-1", "securityContext": {"runAsUser": 1000}}],
            ephemeral_statuses=[running_status("podbench-1")],
        ),
        others=[pod_document(name="web-3c1")],
    )
    assert main(["status", "7f9", "-n", "demo"], runner=cluster) == 0
    captured = capsys.readouterr()
    assert "demo/api-7f9" in captured.out
    assert "api-7f9" in captured.err


def test_an_ambiguous_verb_argument_exits_non_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # pytest's stdin is not a tty, which is the same shape as the CI job and the
    # ssh session this has to refuse rather than hang in.
    cluster = namespace_of("api-7f9", "api-canary")
    assert main(["status", "api", "-n", "demo"], runner=cluster) == 2
    assert "api-canary" in capsys.readouterr().err


def test_no_prompt_is_a_flag_on_the_verbs_that_take_a_pod(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = namespace_of("api-7f9", "api-canary")
    assert main(["status", "--no-prompt", "-n", "demo"], runner=cluster) == 2
    assert "--no-prompt was given" in capsys.readouterr().err


def test_matching_prefers_an_exact_name_over_every_substring() -> None:
    assert match_pod_names(["web", "web-canary"], "web") == ["web"]
    assert match_pod_names(["web", "web-canary"], "web-") == ["web-canary"]


def test_an_unreadable_creation_stamp_costs_a_column_not_the_listing() -> None:
    cluster = namespace_of("api-7f9", created="last tuesday")
    (choice,) = pod_choices(kubectl_for(cluster), now=NOW)
    assert choice.age == "?"


# -- status, list, resize, namespace ----------------------------------------


def test_status_lists_every_container_live_or_burnt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[
                {"name": "podbench-1", "securityContext": {"runAsUser": 1000}},
                {"name": "other-sidecar"},
            ],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    assert main(["status", "target", "-n", "demo"], runner=cluster) == 0
    out = capsys.readouterr().out
    assert "podbench-1" in out
    assert "degraded" in out
    assert "read-only inspection" in out
    assert "other-sidecar" not in out


def test_list_finds_pods_carrying_a_seat(capsys: pytest.CaptureFixture[str]) -> None:
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[{"name": "podbench-1"}],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    assert main(["list", "-n", "demo"], runner=cluster) == 0
    assert "demo/target" in capsys.readouterr().out


def test_resize_failure_is_a_warning_not_a_dead_end() -> None:
    cluster = FakeCluster(pod_document(uid=1000), patch_error="unknown subresource")
    note = try_resize(kubectl_for(cluster), "target", "app", "6Gi")
    assert "refused" in note
    assert "unknown subresource" in note


def test_resize_asks_for_the_resize_subresource() -> None:
    cluster = FakeCluster(pod_document(uid=1000))
    try_resize(kubectl_for(cluster), "target", "app", "6Gi")
    patch = next(call for call in cluster.calls if "patch" in call)
    assert "--subresource=resize" in patch
    assert "--type=strategic" in patch


def test_current_namespace_falls_back_to_default() -> None:
    def runner(
        argv: Sequence[str], *, stdin: str | None = None, capture: bool = True
    ) -> CommandResult:
        return CommandResult(tuple(argv), 0, "", "")

    assert current_namespace(runner=runner) == "default"


def test_current_namespace_reads_the_kubeconfig() -> None:
    cluster = FakeCluster(pod_document())
    assert current_namespace(runner=cluster) == "demo"


def test_resolve_pod_name_refuses_other_kinds() -> None:
    with pytest.raises(LauncherError):
        resolve_pod_name("deployment/api")


def test_features_without_a_report_claim_nothing() -> None:
    session = Session(
        seat=ContainerRef(PodRef("demo", "target"), "podbench-1"),
        workload="app",
        rung=Rung.SEAT,
        reused=False,
    )
    live, read_only, _iterate, ssh_seat, exec_seat = features(session)
    assert not live.available
    assert not read_only.available
    # The ssh half is not claimed either: nothing asked the seat whether sshd
    # can resolve a login name for the uid it ended up running as.
    assert not ssh_seat.available
    assert "was not asked" in ssh_seat.reason
    assert exec_seat.available
