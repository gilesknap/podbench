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
from collections.abc import Sequence
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
    features,
    format_session,
    main,
    parse_mount,
    plan_ladder,
    resolve_pod_name,
    seats,
    try_resize,
)
from podbench.model import Blocker, ContainerRef, PodRef, Rung, Verdict

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
        "metadata": {"name": name, "namespace": "demo", "uid": POD_UID},
        "spec": {
            "nodeName": "node02",
            "containers": [workload],
            "volumes": [dict(volume) for volume in volumes],
            "ephemeralContainers": [dict(entry) for entry in ephemeral],
        },
        "status": {
            "containerStatuses": [
                {"name": container, "containerID": f"containerd://{TARGET_CID}"}
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
        psa_denies_ptrace: bool = False,
        kubelet_refuses_root: bool = False,
        kubelet_refuses_root_image: bool = False,
        capreport: dict[str, Any] | None = None,
        host_key: str | None = HOST_KEY,
        patch_error: str | None = None,
    ) -> None:
        self.pod = pod
        self.psa_denies_ptrace = psa_denies_ptrace
        self.kubelet_refuses_root = kubelet_refuses_root
        self.kubelet_refuses_root_image = kubelet_refuses_root_image
        self.capreport = capreport if capreport is not None else capreport_payload()
        self.capreport_output: str | None = None
        self.host_key = host_key
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
            return _ok(json.dumps({"items": [self.pod]}))
        if rest[:2] == ["get", "pod"]:
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
            return _ok(json.dumps(self.pod))
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


def test_a_mount_named_by_volume_carries_the_applications_sub_path() -> None:
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
    attach(kubectl_for(cluster), "target", mounts=["podbench-patch-venv"])

    # Without the subPath the seat would see the volume root where the
    # application sees one directory inside it, and every path the manifest
    # records would resolve to the wrong thing.
    assert cluster.added[0]["volumeMounts"] == [
        {"name": "podbench-patch-venv", "mountPath": "/opt/venv", "subPath": "venv"}
    ]


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

    live, read_only, iterate, seat = features(session)
    assert not live.available
    assert "Yama" in live.reason
    assert read_only.available
    assert not iterate.available
    assert seat.available

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
    live, read_only, _iterate, seat = features(session)
    assert not live.available
    assert not read_only.available
    assert seat.available
