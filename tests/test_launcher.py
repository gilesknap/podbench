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

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from podbench import __version__
from podbench.agent import SEAT_NSS_PATH
from podbench.console import console, wrap, wrap_width
from podbench.hotfix import hold_loop_args, wrapped_liveness_probe
from podbench.kubectl import (
    CREATE_CONTAINER_CONFIG_ERROR,
    DEFAULT_CALL_TIMEOUT,
    CommandResult,
    Kubectl,
    KubectlError,
    KubectlTimeoutError,
)
from podbench.launcher import (
    DEV_SIDECAR_REUSED_NOTE,
    NO_TARGET_CONTAINER,
    OTHER_MODES_NOTE,
    UNKNOWN_SEAT_VERSION,
    UNMOUNTED_HOTFIX_NOTE,
    VERSION_SKEW_WARNING,
    LauncherError,
    SeatInfo,
    Session,
    all_seats,
    attach,
    capability_report_from_json,
    container_names,
    current_namespace,
    default_host_alias,
    dev_seat,
    emit_ssh_config,
    features,
    forget_known_hosts,
    forget_ssh_config,
    format_seats,
    format_session,
    host_alias_in,
    hotfix_claim_mounts,
    is_hotfixed,
    kubectl_for,
    main,
    match_pod_names,
    other_containers,
    parse_mount,
    plan_ladder,
    pod_choices,
    probe_seat_credentials,
    probe_seat_versions,
    recover_seat_reports,
    resolve_pod,
    resolve_pod_name,
    running_seat,
    runs_hotfix_supervisor,
    same_build,
    seat_layout,
    seats,
    shares_workload_volume,
    ssh_config_path,
    superseded_seats,
    target_container_name,
    target_row,
    try_resize,
    wait_for_seats,
)
from podbench.model import (
    DEVPOD_LABEL,
    HOTFIX_APP_PATH,
    HOTFIX_CLAIM_VOLUME,
    PTRACE_READ_PATHS,
    SEAT_HOME_PATH,
    SEAT_HOME_VOLUME,
    SEAT_IDENTITY_VOLUME,
    SEIZE_PROBE,
    Blocker,
    ContainerRef,
    Lsm,
    PodRef,
    Rung,
    SeatKind,
    SeatReport,
    Verdict,
    describe_pause,
)
from podbench.proc import Credentials
from podbench.resize import Headroom
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
KYVERNO_REFUSAL = (
    'Error from server: admission webhook "validate.kyverno.svc-fail" denied '
    "the request: \n\nresource Pod/hgv27681/bl01t-mo-sim-01-0 was blocked due "
    "to the following policies\n\n"
    "kp-validate-block-privileged-containers-standard:\n"
    "  block privilege escalation: 'validation error: Privileged mode is "
    "disallowed. rule block privilege escalation failed at path "
    "/spec/ephemeralContainers/1/securityContext/allowPrivilegeEscalation/'"
)
"""Verbatim from a DLS namespace, 2026-08-16 (issue #77).

Kept whole rather than trimmed to the fragment that is matched: the point of the
test is that podbench recognises what a real policy engine really says, and a
tidied copy would keep passing against a matcher that no longer works.
"""

VAP_REFUSAL = (
    'Error from server (Forbidden): pods "target" is forbidden: '
    "ValidatingAdmissionPolicy 'deny-sys-ptrace' with binding "
    "'deny-sys-ptrace-binding' denied request: SYS_PTRACE is not permitted here"
)
"""A native policy, which names itself and says ``denied request``.

The wording misses the webhook group by one word, which is why it needed its own
group in ``ADMISSION_DENIAL_MARKERS`` (issue #93) - and why the report needed its
own branch, having called it a webhook until now.
"""

UID_ALLOW_LIST_REFUSAL = (
    'admission webhook "validate.kyverno.svc-fail" denied the request: \n'
    "kp-validate-custom-validate-user-and-group-ids:\n"
    "  validate user id: The fields spec.securityContext.runAsUser is set to an "
    'invalid value. Allowed runAsUser values are: "36096|37887".'
)
"""Verbatim from a DLS namespace, 2026-08-19.

Standard policy for a pod that does host mounts, not local drift: the uid a seat
may run as is allow-listed, so no rung may invent one. A refusal that names the
uids it *would* take is a question with an answer, and the report gives it.
"""

WEBHOOK_UNREACHABLE = (
    "Error from server (InternalError): failed calling webhook "
    '"validate.kyverno.svc-fail": context deadline exceeded'
)
"""A webhook that did not answer, which is not a verdict about any rung."""

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
"""The agent's registration refusal, abridged, as `--print-login-user` relays it.

Deliberately the *fallback* shape: an image with no ``extrausers`` database, where
the only writable passwd file is ``/etc/passwd`` and a seat carrying the target's
gid cannot write it (#102). That is the seat the launcher still has to explain,
because a seat on the current image registers itself and has a login — and the
launcher does not parse any of this, it relays it, so the shape it arrives in is
the agent's business and tests/test_agent.py's.
"""


def pod_document(
    *,
    name: str = "target",
    container: str = "app",
    uid: int | None = None,
    non_root: bool = False,
    seccomp: Mapping[str, Any] | None = None,
    ephemeral: Sequence[dict[str, Any]] = (),
    ephemeral_statuses: Sequence[dict[str, Any]] = (),
    volumes: Sequence[dict[str, Any]] = (),
    volume_mounts: Sequence[dict[str, Any]] = (),
    created: str = "2026-08-15T09:00:00Z",
    phase: str = "Running",
    ready: bool = True,
    probes: Mapping[str, Any] | None = None,
    args: Sequence[str] = (),
    host_network: bool = False,
    siblings: Sequence[str] = (),
    reported_uid: int | None = None,
    labels: Mapping[str, str] | None = None,
    annotations: Mapping[str, str] | None = None,
    sidecar: dict[str, Any] | None = None,
    sidecar_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    security: dict[str, Any] = {}
    if uid is not None:
        security["runAsUser"] = uid
    if non_root:
        security["runAsNonRoot"] = True
    if seccomp is not None:
        security["seccompProfile"] = dict(seccomp)
    workload: dict[str, Any] = {"name": container, "securityContext": security}
    if volume_mounts:
        workload["volumeMounts"] = [dict(mount) for mount in volume_mounts]
    if args:
        workload["args"] = list(args)
    workload.update(probes or {})
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": "demo",
            "uid": POD_UID,
            "creationTimestamp": created,
            **({"labels": dict(labels)} if labels else {}),
            **({"annotations": dict(annotations)} if annotations else {}),
        },
        "spec": {
            "nodeName": "node02",
            # Omitted when false, exactly as the API server serializes it: the
            # seat has to read a missing key as "no" and a missing *variable* as
            # "unknown", and those are not the same absence.
            **({"hostNetwork": True} if host_network else {}),
            "containers": [
                workload,
                *({"name": name} for name in siblings),
                *([sidecar] if sidecar is not None else []),
            ],
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
                    # What the kubelet resolved after the image's own USER, and
                    # absent on a cluster older than 1.33 - which is why the
                    # fixture omits the key rather than reporting a null uid.
                    **(
                        {"user": {"linux": {"uid": reported_uid}}}
                        if reported_uid is not None
                        else {}
                    ),
                },
                *([sidecar_status] if sidecar_status is not None else []),
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
        # The name the seat's label was published under before podbench knew
        # which LSM had written it; kept, so an older launcher still finds it.
        "apparmor_profile": "system_u:system_r:spc_t:s0",
        "lsm": "selinux",
        "lsm_label": "system_u:system_r:spc_t:s0",
        "lsm_label_target": "system_u:system_r:spc_t:s0",
        "self_uid": 0,
        "target_uid": 1000,
        # Peers of the two above: __ptrace_may_access() compares the group ids
        # as well, and a payload with only the uids cannot answer the question
        # the launcher's id correction asks (#103).
        "self_gid": 0,
        "target_gid": 1000,
        "target_pid": 17,
        "node_name": "node02",
        "child_attach_ok": True,
        "target_attach_ok": True,
        "attach_method": SEIZE_PROBE,
        # All three, because the launcher recomputes the read-only tick from
        # exactly these and a subset is no longer a yes: the healthy default
        # has to be a healthy matrix.
        "proc_reads": {"root": True, "maps": True, "environ": True},
        "notes": [],
    }
    payload.update(overrides)
    return payload


SERVER_CLI = "/root/.vscode-server/cli/servers/Stable-abc123/server/bin/code-server"
"""The seat-side vscode-server CLI the extension install goes through."""


class FakeCluster:
    """A kubectl that answers from one mutable pod document."""

    def __init__(
        self,
        pod: dict[str, Any],
        *,
        others: Sequence[dict[str, Any]] = (),
        psa_denies_ptrace: bool = False,
        admission_error: str | None = None,
        kubelet_refuses_root: bool = False,
        kubelet_refuses_root_image: bool = False,
        capreport: dict[str, Any] | None = None,
        host_key: str | None = HOST_KEY,
        login_user: str | None = SEAT_USER,
        login_user_returncode: int = 1,
        patch_error: str | None = None,
        ssh_probe_rc: int = 0,
        ssh_probe_err: str = "",
        limit_ranges: Sequence[dict[str, Any]] = (),
        seat_version: str | None = __version__,
        top: str | None = None,
        mutate: Callable[[dict[str, Any]], None] | None = None,
        allowed_uids: Sequence[int] = (),
        seat_status: str | None = None,
        whoami: str | None = "kubernetes-admin",
    ) -> None:
        self.pod = pod
        # The other pods in the namespace, which only the pod-name resolution
        # ever looks at: everything else here happens to one pod, and a
        # namespace with one pod in it cannot tell a substring match from a
        # name.
        self.others = [dict(other) for other in others]
        self.psa_denies_ptrace = psa_denies_ptrace
        self.admission_error = admission_error
        # A mutating admission policy, applied to what a dry run reports back
        # and never to what is stored: it is the half of admission that issues
        # no refusal at all, so the launcher has nothing but the response.
        self.mutate = mutate
        # A policy that allow-lists runAsUser, which refuses every rung rather
        # than one: the ladder has no lower rung that fixes a uid.
        self.allowed_uids = tuple(allowed_uids)
        # What `kubectl top pod` answers, and `None` for the ordinary cluster
        # with no metrics-server: the headroom read has to degrade to
        # "unmeasured" rather than to "fine", so the default here is the
        # unmeasured one.
        self.top = top
        self.kubelet_refuses_root = kubelet_refuses_root
        self.kubelet_refuses_root_image = kubelet_refuses_root_image
        self.capreport = capreport if capreport is not None else capreport_payload()
        self.capreport_output: str | None = None
        self.host_key = host_key
        self.login_user = login_user
        self.login_user_returncode = login_user_returncode
        self.patch_error = patch_error
        self.ssh_probe_rc = ssh_probe_rc
        self.ssh_probe_err = ssh_probe_err
        # The seat's own build, which the launcher asks for over the same
        # exec channel. Defaulted to this launcher's so the common case in
        # every other test is two halves that agree; `None` is the image too
        # old to have a root `--version` at all.
        self.seat_version = seat_version
        # What `cat /proc/self/status` answers. Derived from the landed spec by
        # default (`_seat_status`), so the fake says what the kernel would; set
        # this to make the two disagree, which is the whole of issue #94 and
        # cannot be expressed by a securityContext.
        self.seat_status = seat_status
        self.limit_ranges = [dict(entry) for entry in limit_ranges]
        self.added: list[dict[str, Any]] = []
        self.calls: list[tuple[str, ...]] = []
        # What `--open` reads and writes: the configurations `debug-config`
        # would emit in the seat, and the files it leaves behind there.
        self.configurations: list[dict[str, Any]] = [
            {"name": "podbench: attach to app.py (debugpy)", "type": "debugpy"}
        ]
        self.seat_files: dict[str, str] = {}
        self.unwritable: set[str] = set()
        # What `kubectl logs` answers per container, and the seam issues #94b
        # and #99 travel over. Absent means "derived from the landed spec";
        # `None` means a log that could not be read at all.
        self.seat_logs: dict[str, str | None] = {}
        # Start-up steps that gave up, carried in the seat's own report. #99 is
        # that these used to reach nothing but the container log.
        self.startup_failures: list[str] = []
        # Who the API server says this kubeconfig is, which is what a seat is
        # stamped with and what the next attach selects on. `None` is the
        # cluster that will not answer `auth whoami`, where every seat stays
        # anonymous and nothing may claim one.
        self.whoami = whoami
        # Whether `debug-config` refuses for want of debugpy in the *target*.
        # Off by default, because the interesting default is a target that
        # already has a debugger: a fake that always needed provisioning would
        # make "provisioned nothing" untestable.
        self.unprovisioned = False

    # -- Runner protocol ---------------------------------------------------

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(argv))
        if argv[0].endswith("code"):
            # `--open` drives the VS Code CLI through the same runner, so a unit
            # test never starts an editor.
            return CommandResult(tuple(argv), 0, "", "")
        if argv[0] == "ssh":
            # `--open`'s preflight, for the same reason: it proves the alias
            # before anything is written, and a unit test opens no connection.
            if stdin is not None:
                # Resolving the seat's own vscode-server, which the extension
                # install goes through. Answering "not yet" here is not a
                # harmless default: `_server_cli` would then poll for five
                # minutes, which is right against a cold seat and a hang in a
                # unit test.
                return CommandResult(tuple(argv), 0, f"{SERVER_CLI}\n", "")
            return CommandResult(tuple(argv), self.ssh_probe_rc, "", self.ssh_probe_err)
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
        # Issue #118 put a bound on the call itself, so every argv this fake
        # sees now carries one. It is asserted where it matters
        # (`test_kubectl.py`) rather than in every dispatch here.
        if rest and rest[0].startswith("--request-timeout="):
            rest = rest[1:]
        return rest

    def _dispatch(
        self, rest: list[str], stdin: str | None, argv: Sequence[str]
    ) -> CommandResult:
        if rest[:1] == ["config"]:
            return _ok("demo\n")
        if rest[:2] == ["auth", "whoami"]:
            if self.whoami is None:
                return _fail(
                    "error: the server doesn't have a resource type "
                    '"selfsubjectreviews"'
                )
            return _ok(json.dumps({"status": {"userInfo": {"username": self.whoami}}}))
        if rest[:2] == ["get", "pods"]:
            return _ok(json.dumps({"items": [self.pod, *self.others]}))
        if rest[:2] == ["top", "pod"]:
            if self.top is None:
                return _fail("error: Metrics API not available", returncode=1)
            return _ok(self.top)
        if rest[:2] == ["get", "limitranges"]:
            return _ok(json.dumps({"items": list(self.limit_ranges)}))
        if rest[:2] == ["get", "pod"]:
            found = self._named(rest[2])
            if found is None:
                # The API server's own words, because resolution reads the exit
                # code and everything else here reads the message.
                return _fail(
                    f'Error from server (NotFound): pods "{rest[2]}" not found'
                )
            if any(item.startswith("--subresource=") for item in rest):
                # Keyed on the pod actually asked for, not the primary fixture:
                # resolution can settle on one of `others`, and answering that
                # with the primary's containers would let a test pass while the
                # code read the wrong pod.
                return _ok(
                    json.dumps(
                        {
                            "metadata": found["metadata"],
                            "spec": {
                                "ephemeralContainers": self._ephemeral_specs(found),
                            },
                        }
                    )
                )
            if rest[-2:] == ["-o", "name"]:
                return _ok(f"pod/{rest[2]}\n")
            return _ok(json.dumps(found))
        if rest[:2] == ["replace", "--raw"]:
            # `?dryRun=All` is the whole of the difference between a rehearsal
            # and a name spent, so the fake keeps it: a dry run runs admission
            # and stores nothing, which is the property the launcher relies on.
            return self._add_ephemeral(stdin, dry_run=rest[2].endswith("?dryRun=All"))
        if rest[:1] == ["logs"]:
            return self._logs(rest)
        if rest[:1] == ["exec"]:
            return self._exec(rest, stdin)
        if rest[:1] == ["patch"]:
            if self.patch_error is not None:
                return _fail(self.patch_error)
            # Applied, not merely acknowledged. A resize that the fake accepted
            # and did not store leaves the pod as tight as it was, so anything
            # reading the headroom *after* one - which is where `vscode` learns
            # whether the raise took - would be measuring the fake instead of
            # the code. #107's refusal is `patch_error`, and it is the only way
            # a resize should fail to land.
            self._apply_resize(json.loads(rest[rest.index("-p") + 1]))
            return _ok("pod/target patched")
        raise AssertionError(f"unexpected kubectl call: {list(argv)}")

    # -- pod document ------------------------------------------------------

    def _apply_resize(self, body: Mapping[str, Any]) -> None:
        """Store a strategic-merge resize the way the API server would.

        Addressed by container *name*, which is why `try_resize` sends a
        strategic-merge patch rather than the JSON patch podbench prefers
        everywhere else: a JSON patch would need the container's index, and an
        index is positional.
        """
        wanted = {
            cast(str, entry["name"]): entry
            for entry in cast("list[dict[str, Any]]", dict(body)["spec"]["containers"])
        }
        for container in cast(
            "list[dict[str, Any]]", cast(dict[str, Any], self.pod["spec"])["containers"]
        ):
            patch = wanted.get(cast(str, container["name"]))
            if patch is None:
                continue
            resources = cast("dict[str, Any]", container.setdefault("resources", {}))
            for field, values in cast("dict[str, Any]", patch["resources"]).items():
                cast("dict[str, Any]", resources.setdefault(field, {})).update(
                    cast("dict[str, Any]", values)
                )

    def _named(self, name: str) -> dict[str, Any] | None:
        for pod in (self.pod, *self.others):
            if cast(dict[str, Any], pod["metadata"])["name"] == name:
                return pod
        return None

    def _ephemeral_specs(
        self, pod: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        target = self.pod if pod is None else pod
        return cast(list[dict[str, Any]], target["spec"]["ephemeralContainers"])

    def _statuses(self) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]], self.pod["status"]["ephemeralContainerStatuses"]
        )

    def _add_ephemeral(self, stdin: str | None, *, dry_run: bool) -> CommandResult:
        assert stdin is not None, "the subresource PUT must carry a body"
        body = cast(dict[str, Any], json.loads(stdin))
        submitted = cast(list[dict[str, Any]], body["spec"]["ephemeralContainers"])
        known = {entry["name"] for entry in self._ephemeral_specs()}
        new = [entry for entry in submitted if entry["name"] not in known]
        assert len(new) == 1, f"expected exactly one new container, got {new}"
        spec = new[0]
        # One entry per rung *attempt*, not per API call: a rung is now
        # rehearsed by a dry run and then created, with the same body both
        # times, and what these tests read `added` for is which securityContexts
        # podbench authored.
        if not self.added or self.added[-1] != spec:
            self.added.append(spec)

        security = cast(dict[str, Any], spec.get("securityContext", {}))
        added_caps = cast(
            list[str],
            cast(dict[str, Any], security.get("capabilities", {})).get("add", []),
        )
        if self.psa_denies_ptrace and "SYS_PTRACE" in added_caps:
            return _fail(PSA_REFUSAL, returncode=1)
        if self.admission_error is not None and "SYS_PTRACE" in added_caps:
            return _fail(self.admission_error, returncode=1)
        if self.allowed_uids and security.get("runAsUser") not in self.allowed_uids:
            return _fail(UID_ALLOW_LIST_REFUSAL, returncode=1)

        if dry_run:
            # What admission would store, which is the only place a *mutating*
            # policy shows itself: it refuses nothing, so the response body is
            # the entire signal. Nothing is kept - a dry run that burnt a name
            # would be worse than no dry run at all.
            preview = copy.deepcopy(body)
            containers = cast(
                list[dict[str, Any]], preview["spec"]["ephemeralContainers"]
            )
            if self.mutate is not None:
                for container in containers:
                    self.mutate(container)
            return _ok(json.dumps(preview))

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

    def _seat_status(self, container: str | None) -> str:
        """``/proc/self/status`` as the kernel would write it for that seat.

        Built from the securityContext the seat actually landed with, and with
        report §3.10 built in: capabilities reach ``CapEff`` only at uid 0, so a
        spec that added ``SYS_PTRACE`` beside a non-zero ``runAsUser`` answers
        an effective mask of zero here exactly as the kernel does. The uid
        defaults to 0 for a seat that pinned none, because podbench's image
        declares no ``USER``.
        """
        if self.seat_status is not None:
            return self.seat_status
        empty: dict[str, Any] = {}
        spec = next(
            (entry for entry in self._ephemeral_specs() if entry["name"] == container),
            empty,
        )
        security = cast(dict[str, Any], spec.get("securityContext", {}))
        uid = cast(int, security.get("runAsUser", 0))
        gid = cast(int, security.get("runAsGroup", 0))
        added = cast(
            list[str],
            cast(dict[str, Any], security.get("capabilities", {})).get("add", []),
        )
        effective = 1 << 19 if uid == 0 and "SYS_PTRACE" in added else 0
        return status_text(uid, gid, effective)

    def _logs(self, rest: list[str]) -> CommandResult:
        """The container log a seat's start-up report is recovered from.

        Derived from the same landed spec `_seat_status` reads, so the fake's
        two answers about one seat agree by construction. `seat_logs` overrides
        it per container - `None` for a log that cannot be read at all, which
        is RBAC without `pods/log`, a rotated log, or a seat older than the
        report and is what a listing has to render as *not measured*.
        """
        container = rest[rest.index("-c") + 1]
        if container in self.seat_logs:
            text = self.seat_logs[container]
            return _fail("Error from server: not found") if text is None else _ok(text)
        credentials = Credentials.from_status(self._seat_status(container))
        if credentials is None:
            # A seat whose own `/proc/self/status` says nothing has nothing to
            # report about itself either, so its log carries no report line.
            return _ok("agent: prepared /root\n")
        target = cast(
            dict[str, Any],
            cast(dict[str, Any], self.pod["spec"])["containers"][0].get(
                "securityContext", {}
            ),
        )
        report = SeatReport(
            uid=credentials.uid,
            gid=credentials.gid,
            effective_hex=credentials.capabilities.effective_hex,
            sys_ptrace=credentials.capabilities.sys_ptrace_effective,
            # The target's own ids as the seat reads them from
            # `/proc/<pid>/status` - defaulted to root, because a manifest that
            # pins no uid leaves the container running as its image's user and
            # podbench's fixtures have no `USER`. The gid falls back to the
            # *uid* rather than to that default, so a fixture stating
            # `runAsUser: 1000` and no `runAsGroup` reports 1000:1000 - the
            # p47-blueapi-0 shape, where the target's group comes from the
            # image and the seat lands at 1000:0 beside it.
            target_uid=cast(int, target.get("runAsUser", 0)),
            target_gid=cast(int, target.get("runAsGroup", target.get("runAsUser", 0))),
            target_pid=17,
            failures=tuple(self.startup_failures),
        )
        return _ok(f"agent: prepared /root\nagent: {report.to_line()}\n")

    def _exec(self, rest: list[str], stdin: str | None = None) -> CommandResult:
        command = rest[rest.index("--") + 1 :]
        if command == ["cat", "/proc/self/status"]:
            container = rest[rest.index("-c") + 1] if "-c" in rest else None
            return _ok(self._seat_status(container))
        # Matched as the two-token verb, not as a bare `debug-config`: the
        # image has no per-subcommand aliases on PATH, so a launcher that sent
        # one would exec nothing in a real seat and must miss here too.
        if command[:2] == ["podbench", "debug-config"]:
            # A Python target whose image has no debugpy. The refusal names
            # `--provision` and nothing else does: `debug-config` offers it
            # where debugpy is the blocker and never for a missing delve, which
            # is what lets the laptop side answer it without guessing the
            # target's language a second time.
            if self.unprovisioned and "--provision" not in command:
                return _fail(
                    "the target cannot import debugpy, and the injection "
                    "bootstrap runs in its interpreter: --provision installs "
                    "one there first.",
                    returncode=1,
                )
            return _ok(
                json.dumps({"version": "0.2.0", "configurations": self.configurations})
            )
        if command[:2] == ["sh", "-c"] and command[2].startswith("mkdir -p"):
            path = command[2].rsplit("> ", 1)[1].strip("'")
            if path in self.unwritable:
                return _fail(f"sh: cannot create {path}: Permission denied")
            self.seat_files[path] = stdin or ""
            return _ok("")
        if command[:2] == ["sh", "-c"]:
            text = self.seat_files.get(command[2].rsplit("cat ", 1)[1].strip("'"))
            # 3 is the read script's own "no such file"; anything else means it
            # found one and could not read it, which `--open` refuses to guess.
            return _ok(text) if text is not None else _fail("", returncode=3)
        if command[:2] == ["podbench", "--version"]:
            if self.seat_version is None:
                # click's usage message, which is what an image predating the
                # root `--version` prints - on stdout, and with exit 2. The
                # launcher must read that as unknown rather than as a version.
                return _ok("Usage: podbench [OPTIONS] COMMAND [ARGS]...", returncode=2)
            return _ok(self.seat_version + "\n")
        # Same two-token rule as `debug-config` above, for the same reason.
        if command[:2] == ["podbench", "capreport"]:
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


def status_text(uid: int, gid: int, effective: int = 0) -> str:
    """The four lines of ``/proc/self/status`` the launcher reads."""
    return (
        f"Name:\tcat\n"
        f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        f"Gid:\t{gid}\t{gid}\t{gid}\t{gid}\n"
        f"CapEff:\t{effective:016x}\n"
        f"CapBnd:\t{effective:016x}\n"
        f"CapAmb:\t0000000000000000\n"
    )


def _ok(stdout: str, returncode: int = 0) -> CommandResult:
    return CommandResult((), returncode, stdout, "")


def _fail(stderr: str, returncode: int = 1) -> CommandResult:
    return CommandResult((), returncode, "", stderr)


def talking_to(cluster: FakeCluster) -> Kubectl:
    return Kubectl("demo", runner=cluster)


def security_contexts(cluster: FakeCluster) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], spec.get("securityContext", {})) for spec in cluster.added
    ]


def landed_rung(session: Session) -> Rung | None:
    """Which rung of the ladder admission actually took, or ``None`` for none.

    Not ``session.rung``, which since issue #94 is what the seat *measures* as -
    a different question, and on the fixture pod the two genuinely differ. A
    manifest stating ``runAsUser: 1000`` and no ``runAsGroup`` is p47-blueapi-0's
    shape: the degraded rung lands, pinning the uid it can see, and the seat it
    lands sits in the debug image's group 0 against a target in group 1000,
    which ``__ptrace_may_access()`` denies on the group half alone. So a test
    about the *walk* asks the walk.
    """
    return next((step.rung for step in session.steps if step.admitted), None)


def limited_pod(memory: str, *, name: str = "target") -> dict[str, Any]:
    """A pod whose cgroup has a ceiling, which is what makes headroom a number.

    The default fixture declares no limits at all, so the kubelet leaves the pod
    cgroup unbounded and there is nothing for a seat to share. Every pod on the
    beamline C19 was calibrated against does declare one - 19 containers, 19
    limits - so the tests that read headroom say so explicitly.
    """
    pod = pod_document(uid=1000, name=name)
    containers = cast(list[dict[str, Any]], pod["spec"]["containers"])
    containers[0]["resources"] = {"limits": {"memory": memory}}
    return pod


def running_status(name: str) -> dict[str, Any]:
    return {"name": name, "state": {"running": {"startedAt": "2026-08-15T09:00:00Z"}}}


# -- which container the seat entered ---------------------------------------


def test_the_first_container_wins_and_a_named_one_has_to_exist() -> None:
    pod = pod_document(container="ca-gateway", siblings=["pva-gateway"])

    assert container_names(pod) == ["ca-gateway", "pva-gateway"]
    assert target_container_name(pod) == "ca-gateway"
    assert target_container_name(pod, "pva-gateway") == "pva-gateway"
    # Only a *requested* name can be wrong: the default is whatever the spec
    # puts first, which is the rule `kubectl exec` follows.
    with pytest.raises(LauncherError, match="not in pod"):
        target_container_name(pod, "gateway")
    with pytest.raises(LauncherError, match="no containers"):
        target_container_name({"spec": {"containers": []}})


def test_a_three_container_pod_names_every_one_it_did_not_enter() -> None:
    """``p47-proxy``, where the default target is one container of three.

    The flag is spelled out per sibling rather than described, because a reader
    who has to work out the invocation from a name is the reader who does not.
    """
    pod = pod_document(container="panda80", siblings=["panda8080", "pmac1025"])

    assert other_containers(pod, "panda80") == ("panda8080", "pmac1025")
    assert target_row("panda80", other_containers(pod, "panda80")) == [
        "target      panda80; this pod also has panda8080 and pmac1025.",
        "            reach one with `--target panda8080` or `--target pmac1025`",
    ]


@pytest.mark.parametrize("columns", ["60", "80", "100", "120"])
def test_the_target_flag_is_never_parted_from_its_argument(
    columns: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The C15 defect, at the four widths the field round was read at.

    On `p47-epics-gateways-0` at 80 columns the row put ``--target`` on one line
    and `p47-epics-gateways-pva-gateway` on the next; at 100 it broke
    `p47-proxy`'s `panda8080` off the same way. Either way the offer stops being
    pasteable, which is the only reason it is printed.
    """
    monkeypatch.setenv("COLUMNS", columns)
    for workload, siblings in (
        ("p47-epics-gateways-ca-gateway", ("p47-epics-gateways-pva-gateway",)),
        ("panda80", ("panda8080", "pmac1025")),
    ):
        lines = target_row(workload, siblings)
        offer = lines[-1]
        for name in siblings:
            assert f"`--target {name}`" in offer
        # And the prose above it still wraps to this terminal, so the row is
        # not simply one long line that happens to keep the flag together.
        assert all(len(line) <= max(int(columns) - 8, 48) for line in lines[:-1])


def test_a_single_container_pod_gains_no_line_at_all() -> None:
    """The common case, and the reason this is a value and not a warning.

    Twelve of the fifteen pods on the beamline have one container; a sentence
    about the default on each of them is a report growing a line to say nothing.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    text = format_session(attach(talking_to(cluster), "target"))

    assert "target      app" in text
    assert "this pod also has" not in text


def test_the_report_names_the_sibling_and_the_flag_that_reaches_it() -> None:
    cluster = FakeCluster(
        pod_document(uid=1000, container="ca-gateway", siblings=["pva-gateway"])
    )
    session = attach(talking_to(cluster), "target")

    assert session.workload == "ca-gateway"
    assert session.siblings == ("pva-gateway",)
    # Flattened: the row wraps, and the fact is the sentence rather than any
    # one line of it.
    # Flattened for the prose half only: the sentence naming the sibling wraps
    # and the fact is the sentence, not any one line of it.
    text = format_session(session)
    assert "target ca-gateway; this pod also has pva-gateway" in " ".join(text.split())
    # The offer is asserted as a *line*, because the thing under test is that
    # nothing broke it.
    assert "            reach it with `--target pva-gateway`" in text.splitlines()


def test_the_seat_carries_the_container_it_entered_and_the_pods_others() -> None:
    """Neither fact is derivable in the seat: it sees namespaces, not the pod.

    Without them ``pids`` can only offer "the pod's processes" over one
    container's namespace, which is the reading finding 15 is about.
    """
    cluster = FakeCluster(
        pod_document(uid=1000, container="ca-gateway", siblings=["pva-gateway"])
    )
    attach(talking_to(cluster), "target")

    env = {entry["name"]: entry["value"] for entry in cluster.added[0]["env"]}
    assert env["PODBENCH_TARGET"] == "ca-gateway"
    assert env["PODBENCH_POD_CONTAINERS"] == "ca-gateway,pva-gateway"


def test_a_reconnect_reports_the_container_the_seat_is_really_in() -> None:
    """``--target`` cannot move a seat, so the report must not say it did.

    An ephemeral container's ``targetContainerName`` is fixed for its lifetime.
    A reconnect that was handed another name is still in the first container,
    and the siblings named beside it have to be that container's.
    """
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            container="ca-gateway",
            siblings=["pva-gateway"],
            ephemeral=[{"name": "podbench-1", "targetContainerName": "pva-gateway"}],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    session = attach(talking_to(cluster), "target", target="ca-gateway")

    assert session.reused
    assert session.workload == "pva-gateway"
    assert session.siblings == ("ca-gateway",)


# -- the ladder -------------------------------------------------------------


def test_full_rung_lands_when_the_namespace_allows_it() -> None:
    # Asked for, because it is no longer where the walk starts: a target at a
    # known non-root uid has its own rung tried first. `--max-rung full` is how
    # somebody who needs the capability - a node with Yama ptrace_scope >= 1
    # exempts nothing else - says so.
    cluster = FakeCluster(pod_document(uid=1000))
    session = attach(
        talking_to(cluster), "pod/target", public_key=CLIENT_KEY, max_rung=Rung.FULL
    )

    assert session.rung is Rung.FULL
    assert session.seat == ContainerRef(PodRef("demo", "target"), "podbench-1")
    assert not session.reused
    assert security_contexts(cluster)[0] == {
        "runAsUser": 0,
        "capabilities": {"add": ["SYS_PTRACE"]},
        "privileged": False,
        "allowPrivilegeEscalation": False,
    }
    env = {entry["name"]: entry["value"] for entry in cluster.added[0]["env"]}
    assert env["PODBENCH_TARGET_CID"] == TARGET_CID
    assert env["PODBENCH_SSH_PUBKEY"] == CLIENT_KEY
    assert env["PODBENCH_NODE_NAME"] == "node02"
    assert "HOME" not in env
    # Written both ways round, always. Nothing inside a container can work out
    # whether 127.0.0.1 is the pod's loopback or the node's, and a seat that
    # simply finds the variable missing has to keep reading that as "an older
    # launcher landed me" rather than as "no" (issue #87).
    assert env["PODBENCH_HOST_NETWORK"] == "false"


def test_a_host_network_pod_says_so_in_the_seats_environment() -> None:
    """The six hostNetwork IOCs on one beamline node are the case: there,
    127.0.0.1 in the seat is the *node's* loopback, so a port found on it is
    not evidence of anything in this pod and a debugpy server started on it is
    reachable by every other pod on the node."""
    cluster = FakeCluster(pod_document(uid=1000, host_network=True))
    attach(talking_to(cluster), "pod/target", public_key=CLIENT_KEY)

    env = {entry["name"]: entry["value"] for entry in cluster.added[0]["env"]}
    assert env["PODBENCH_HOST_NETWORK"] == "true"


def test_psa_refusal_falls_to_the_degraded_rung_without_burning_a_name() -> None:
    cluster = FakeCluster(pod_document(uid=1000), psa_denies_ptrace=True)
    session = attach(talking_to(cluster), "target", max_rung=Rung.FULL)

    assert landed_rung(session) is Rung.DEGRADED
    # A synchronous refusal means the container was never created, so the name
    # it was submitted under is still free (report 3.18/4.2).
    assert session.seat.container == "podbench-1"
    assert session.uid == 1000
    assert security_contexts(cluster)[1] == {
        "capabilities": {"drop": ["ALL"]},
        "runAsNonRoot": True,
        "privileged": False,
        "allowPrivilegeEscalation": False,
        "runAsUser": 1000,
    }
    steps = {step.rung: step for step in session.steps}
    assert not steps[Rung.FULL].admitted
    assert "Pod Security Admission" in steps[Rung.FULL].detail


def test_the_seat_imposes_no_seccomp_profile_its_target_does_not_have() -> None:
    """The DLS shape, 2026-08-18.

    The target declares no ``seccompProfile``, so neither may the seat. Landing
    it at ``RuntimeDefault`` put the seat under a filter the workload did not
    have, and on that node the filter denied ``ptrace`` — including on a child
    the seat forked itself, which costs the rung ``dbg --launch`` as well as
    live attach.
    """
    cluster = FakeCluster(pod_document(uid=1000), psa_denies_ptrace=True)
    attach(talking_to(cluster), "target")

    assert "seccompProfile" not in security_contexts(cluster)[0]


def test_the_seat_mirrors_a_seccomp_profile_its_target_does_have() -> None:
    """Which is what keeps the rung admissible under restricted PSA.

    A namespace enforcing ``restricted`` would have refused the workload
    without one, so mirroring the target is enough to comply wherever
    compliance is actually required.
    """
    profile = {"type": "Localhost", "localhostProfile": "profiles/audit.json"}
    cluster = FakeCluster(
        pod_document(uid=1000, seccomp=profile), psa_denies_ptrace=True
    )
    attach(talking_to(cluster), "target")

    assert security_contexts(cluster)[0]["seccompProfile"] == profile


def test_kubelet_refusal_falls_through_and_takes_a_fresh_name() -> None:
    cluster = FakeCluster(pod_document(uid=1000), kubelet_refuses_root=True)
    session = attach(
        talking_to(cluster), "target", poll_interval=0.0, max_rung=Rung.FULL
    )

    assert landed_rung(session) is Rung.DEGRADED
    # The rejected container exists and is unrestartable, so its name is gone.
    assert session.seat.container == "podbench-2"
    steps = {step.rung: step for step in session.steps}
    assert steps[Rung.FULL].container == "podbench-1"
    assert "CreateContainerConfigError" in steps[Rung.FULL].detail
    assert "runAsUser breaks non-root policy" in steps[Rung.FULL].detail


def test_run_as_non_root_pre_empts_the_full_rung_entirely() -> None:
    cluster = FakeCluster(pod_document(uid=1000, non_root=True))
    session = attach(talking_to(cluster), "target")

    assert landed_rung(session) is Rung.DEGRADED
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
    session = attach(talking_to(cluster), "target")

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


def test_a_root_target_the_spec_does_not_pin_is_not_offered_the_flag() -> None:
    """argus (hgv27681, 2026-08-20): no pod in the namespace states runAsUser.

    The spec saying nothing is not the same as the uid being unknown - the
    image's USER decides it and the node publishes the answer. On all eight pods
    there the answer was 0, and this line was advising `--target-uid` four lines
    above a capability report that had read the target's uid as 0, for a rung
    the same sentence says uid 0 cannot express. Running it confirmed it:
    `--target-uid 0 --new` prints the refusal and spends a container name.
    """
    plan = dict(plan_ladder(pod_document(reported_uid=0), "app"))

    degraded = plan[Rung.DEGRADED]
    assert degraded is not None
    assert "--target-uid" not in degraded
    assert "runAsNonRoot: true cannot express" in degraded


def test_a_non_root_target_the_spec_does_not_pin_is_offered_the_uid() -> None:
    """And where the flag *can* change the outcome it carries the number, so the
    re-run is a paste rather than a hunt through /proc."""
    plan = dict(plan_ladder(pod_document(reported_uid=37887), "app"))

    degraded = plan[Rung.DEGRADED]
    assert degraded is not None
    assert "--target-uid 37887" in degraded


def test_a_uid_no_source_reports_still_asks_for_the_flag() -> None:
    """The genuinely unknown case, which is a cluster older than 1.33 as often
    as it is anything about the pod. The advice stands there - the capability
    report reads the uid from /proc, and the flag is how it gets used."""
    plan = dict(plan_ladder(pod_document(), "app"))

    degraded = plan[Rung.DEGRADED]
    assert degraded is not None
    assert "neither the pod spec nor the node" in degraded
    assert "--target-uid" in degraded


def test_target_uid_override_re_enables_the_degraded_rung() -> None:
    cluster = FakeCluster(pod_document(), psa_denies_ptrace=True)
    session = attach(talking_to(cluster), "target", target_uid=1000)

    assert landed_rung(session) is Rung.DEGRADED
    # And is where the walk starts: a uid the launcher was handed is a uid the
    # degraded rung can match, so nothing is spent on the rung above it.
    assert security_contexts(cluster)[0]["runAsUser"] == 1000


def test_an_exhausted_ladder_raises_with_every_reason() -> None:
    # A root target under runAsNonRoot: no full rung, no degraded rung, and a
    # seat whose runAsNonRoot: true the kubelet refuses over a root image.
    cluster = FakeCluster(
        pod_document(uid=0, non_root=True), kubelet_refuses_root_image=True
    )
    with pytest.raises(LauncherError) as raised:
        attach(talking_to(cluster), "target", poll_interval=0.0)
    message = str(raised.value)
    assert "runAsNonRoot" in message
    assert "the target runs as root" in message
    assert "CreateContainerConfigError" in message


# -- the ceiling ------------------------------------------------------------

DLS_MUTATED_CAPABILITIES: dict[str, Any] = {
    "add": [
        "AUDIT_WRITE",
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "FSETID",
        "KILL",
        "MKNOD",
        "SETFCAP",
        "SETPCAP",
        "SETGID",
        "SETUID",
        "SYS_CHROOT",
    ],
    "drop": ["ALL"],
}
"""What a DLS namespace left behind after admitting the full rung, 2026-08-18.

Verbatim from the pod. The policy *mutates* rather than refuses: it replaced
`capabilities` wholesale with the house default, so `SYS_PTRACE` is simply gone
while `runAsUser: 0` survived. Nothing in the walk sees a refusal, and the seat
that lands reads back as the degraded rung while running as root (issue #94).
"""


def test_max_rung_skips_the_rungs_above_it() -> None:
    plan = dict(plan_ladder(pod_document(uid=1000), "app", max_rung=Rung.DEGRADED))

    full = plan[Rung.FULL]
    assert full is not None
    assert "--max-rung degraded" in full
    assert plan[Rung.DEGRADED] is None
    assert plan[Rung.SEAT] is None


def test_a_ceiling_submits_no_capability_where_one_would_be_admitted() -> None:
    cluster = FakeCluster(pod_document(uid=1000))
    session = attach(talking_to(cluster), "target", max_rung=Rung.DEGRADED)

    assert landed_rung(session) is Rung.DEGRADED
    assert session.uid == 1000
    # The whole value of the flag on a mutating cluster: the full rung is never
    # submitted, so no permanent name is spent finding out it lands neutered.
    assert len(cluster.added) == 1
    assert "add" not in security_contexts(cluster)[0].get("capabilities", {})


def test_a_ceiling_is_a_ceiling_and_not_a_choice() -> None:
    # A root target has no degraded rung to author, so the walk must still fall
    # through to the one below rather than stop at the ceiling it was given.
    cluster = FakeCluster(pod_document(uid=0))
    session = attach(talking_to(cluster), "target", max_rung=Rung.DEGRADED)

    assert session.rung is Rung.SEAT


def test_a_stripped_full_seat_is_not_reused_under_a_ceiling() -> None:
    existing = {
        "name": "podbench-1",
        "targetContainerName": "app",
        "securityContext": {
            "runAsUser": 0,
            "privileged": False,
            "allowPrivilegeEscalation": False,
            "capabilities": DLS_MUTATED_CAPABILITIES,
        },
    }
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[existing],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    session = attach(talking_to(cluster), "target", max_rung=Rung.DEGRADED)

    assert not session.reused
    assert session.seat.container == "podbench-2"
    assert session.uid == 1000
    # The seat it declined reads back as the degraded rung, so the rung label
    # cannot be what decided — the uid is.
    assert seats(cluster.pod)[0].rung is Rung.DEGRADED
    declined = [note for note in session.warnings if "podbench-1" in note]
    assert declined and "uid 0" in declined[0]


def test_a_stripped_full_seat_is_not_reused_against_a_root_target() -> None:
    # The same mutated seat as above, but the target is root too, so there is no
    # uid for the pin comparison to disagree with. Judged on `runAsUser: 0`
    # alone, which no rung below full authors: the degraded rung refuses a root
    # target and the seat rung drops the uid rather than pin it.
    existing = {
        "name": "podbench-1",
        "targetContainerName": "app",
        "securityContext": {
            "runAsUser": 0,
            "privileged": False,
            "allowPrivilegeEscalation": False,
            "capabilities": DLS_MUTATED_CAPABILITIES,
        },
    }
    cluster = FakeCluster(
        pod_document(
            uid=0,
            ephemeral=[existing],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    session = attach(talking_to(cluster), "target", max_rung=Rung.DEGRADED)

    assert not session.reused
    assert session.seat.container == "podbench-2"
    assert session.rung is Rung.SEAT
    declined = [note for note in session.warnings if "podbench-1" in note]
    assert declined and "uid 0" in declined[0]


def test_a_seat_ceiling_reuses_the_seat_it_would_have_landed() -> None:
    # The seat rung honours a non-zero target uid too, so the seat rung and the
    # degraded rung are indistinguishable through `rung_of_spec` - both read
    # back as `degraded`. Declining this one would burn a container name on
    # every reconnect, and land the same securityContext again.
    existing = {
        "name": "podbench-1",
        "targetContainerName": "app",
        "securityContext": {"runAsUser": 1000, "runAsNonRoot": True},
    }
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[existing],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    session = attach(talking_to(cluster), "target", max_rung=Rung.SEAT)

    assert session.reused
    assert cluster.added == []


def test_a_seat_that_honours_the_ceiling_is_still_reused() -> None:
    existing = {
        "name": "podbench-1",
        "targetContainerName": "app",
        "securityContext": {"runAsUser": 1000},
    }
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[existing],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    session = attach(talking_to(cluster), "target", max_rung=Rung.DEGRADED)

    assert session.reused
    assert cluster.added == [], "a seat at the target's uid is what was asked for"


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
    session = attach(talking_to(cluster), "target")

    assert session.reused
    assert session.rung is Rung.FULL
    assert session.seat.container == "podbench-1"
    assert cluster.added == [], "reconnecting must not append a container"


def test_a_reconnect_reports_the_capability_admission_took() -> None:
    """Reconnecting is the common case, and it said nothing about the strip.

    Both admission warnings were produced inside the ladder walk, which only the
    *first* attach runs. Somebody who attaches on Monday and reconnects all week
    was never told the seat had lost SYS_PTRACE - and the evidence was in the
    pod document the reconnect had already fetched: a seat at uid 0 with an
    empty capabilities.add is a full rung admission stripped, since no rung
    below full ever writes runAsUser: 0.
    """
    cluster = stripped_root_seat()
    session = attach(talking_to(cluster), "target")

    assert session.reused
    assert cluster.added == [], "a reconnect must not rehearse a new container"
    assert any("admission removed SYS_PTRACE" in text for text in session.warnings)


def test_a_reconnect_reports_the_capabilities_admission_added() -> None:
    """The other half of the same pass: thirteen capabilities beside a
    `drop: [ALL]` that asked for none of them (DLS, 2026-08-19). podbench adds
    nothing but SYS_PTRACE and only on the full rung, so anything else in a
    landed seat's capabilities.add came from a policy."""
    seat = {
        "name": "podbench-1",
        "securityContext": {
            "runAsUser": 1000,
            "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "SETGID"]},
        },
    }
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[seat],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    session = attach(talking_to(cluster), "target")

    rewrite = [
        warning for warning in session.warnings if "admission rewrote" in warning
    ]
    assert len(rewrite) == 1
    assert "added CHOWN, SETGID to capabilities.add" in rewrite[0]


def test_a_reconnect_to_an_unmutated_seat_says_nothing_about_admission() -> None:
    """The scalars are not reconstructed, so nothing is inferred from their
    absence: a seat landed by a podbench that stated fewer fields than this one
    must not have the difference reported as a policy's work."""
    seat = {
        "name": "podbench-1",
        "securityContext": {"runAsUser": 0, "capabilities": {"add": ["SYS_PTRACE"]}},
    }
    cluster = FakeCluster(
        pod_document(
            uid=0,
            ephemeral=[seat],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    session = attach(talking_to(cluster), "target")

    assert not any("admission" in warning for warning in session.warnings)


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
    session = attach(talking_to(cluster), "target", force_new=True)

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
    session = attach(talking_to(cluster), "target")

    assert not session.reused
    assert session.seat.container == "podbench-2"
    listed = {seat.name: seat for seat in seats(cluster.pod)}
    assert listed["podbench-1"].phase == "terminated"
    assert "burnt" in listed["podbench-1"].detail


# -- mounting the pod's own volumes (Hotfix mode) ----------------------------

PATCH_VOLUME: dict[str, Any] = {
    "name": "podbench-patch-venv",
    "persistentVolumeClaim": {"claimName": "myapp-venv"},
}
APP_MOUNT: dict[str, Any] = {"name": "podbench-patch-venv", "mountPath": "/opt/venv"}


def patch_pod(**overrides: Any) -> dict[str, Any]:
    """A pod wired for Hotfix mode: the claim is a volume, the app mounts it."""
    settings: dict[str, Any] = {
        "uid": 1000,
        "volumes": [PATCH_VOLUME],
        "volume_mounts": [APP_MOUNT],
    }
    settings.update(overrides)
    return pod_document(**settings)


def test_a_mount_named_by_claim_lands_at_the_applications_own_path() -> None:
    cluster = FakeCluster(patch_pod())
    session = attach(talking_to(cluster), "target", mounts=["myapp-venv"])

    # The mount refers to the pod's *volume* entry, but the user has the claim
    # name in hand, so both are accepted.
    assert cluster.added[0]["volumeMounts"] == [
        {"name": "podbench-patch-venv", "mountPath": "/opt/venv"}
    ]
    # Hotfix mode needs the seat and the application to agree on the path, so it
    # is copied rather than asked for again.
    assert not [w for w in session.warnings if "mountPath" in w]


def test_an_application_sub_path_is_refused_rather_than_copied_or_dropped() -> None:
    """Neither half of "copy it or drop it" is available to an ephemeral seat.

    Copying is forbidden — the API server refuses ``subPath`` on an ephemeral
    container for the whole request — and dropping it would point the seat at
    the volume root where the application sees one directory inside it, so every
    path Hotfix mode recorded would resolve to the wrong thing. So it refuses,
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
        attach(talking_to(cluster), "target", mounts=["podbench-patch-venv"])

    message = str(raised.value)
    assert "subPath" in message
    assert "Ephemeral Container" in message
    assert cluster.added == []


def test_a_volume_the_pod_does_not_declare_is_refused_before_anything_is_created() -> (
    None
):
    cluster = FakeCluster(pod_document(uid=1000))
    with pytest.raises(LauncherError) as raised:
        attach(talking_to(cluster), "target", mounts=["myapp-venv:/opt/venv"])

    message = str(raised.value)
    assert "myapp-venv" in message
    # The refusal has to explain that this is not podbench being unhelpful: a
    # pod's volumes are immutable, which is why Hotfix mode needs the chart.
    assert "immutable" in message
    assert "--print-values" in message
    assert cluster.added == [], "nothing may be submitted, and no name burnt"


def test_an_explicit_mount_path_that_disagrees_is_warned_about() -> None:
    cluster = FakeCluster(patch_pod())
    session = attach(
        talking_to(cluster), "target", mounts=["myapp-venv:/srv/venv"], probe=False
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
        attach(talking_to(cluster), "target", mounts=["myapp-venv"])

    session = attach(talking_to(cluster), "target", mounts=["myapp-venv:/opt/venv"])
    assert landed_rung(session) is Rung.DEGRADED
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
    session = attach(talking_to(cluster), "target", mounts=["myapp-venv"])

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


# -- the claim, mounted by the same convention (issue #177) -----------------

HOTFIX_CLAIM: dict[str, Any] = {
    "name": HOTFIX_CLAIM_VOLUME,
    "persistentVolumeClaim": {"claimName": "myapp-podbench"},
}


def layout_pod(**overrides: Any) -> dict[str, Any]:
    """A pod deployed with the hotfix layout: the claim, mounted, supervised.

    Exactly what `podbench hotfix --print-values` emits and Argo renders, and
    the two halves `is_hotfixed` reads. The home volume comes along because a
    real deployment has both - the values emit them together - and because the
    claim has to be shown not to displace it.
    """
    settings: dict[str, Any] = {
        "uid": 1000,
        "volumes": [HOME_VOLUME, HOTFIX_CLAIM],
        "volume_mounts": [{"name": HOTFIX_CLAIM_VOLUME, "mountPath": HOTFIX_APP_PATH}],
        "args": [hold_loop_args("myapp serve")],
    }
    settings.update(overrides)
    return pod_document(**settings)


def test_the_report_says_iterate_is_available_on_a_hotfixed_pod() -> None:
    """Issue #179. On both p47 pods `attach` reported `[ ] iterate ... needs a
    sacrificial dev pod`, which is true of an ordinary pod and false in both its
    halves here: the supervisor is PID 1, so killing the child does not restart
    the container, and the probe is the hold-aware wrapper."""
    cluster = FakeCluster(layout_pod())
    session = attach(talking_to(cluster), "target", probe=False)

    assert session.hotfixed
    (iterate,) = [f for f in features(session) if f.name.startswith("iterate")]
    assert iterate.available
    assert "hotfix apply" in iterate.note
    # The sentence that was wrong here is gone, not merely outvoted.
    assert "sacrificial" not in iterate.note
    assert "sacrificial" not in iterate.reason


def test_an_ordinary_pod_still_says_iterate_needs_a_dev_pod() -> None:
    """The reason is right about a pod with no supervisor, and stays."""
    cluster = FakeCluster(identity_pod())
    session = attach(talking_to(cluster), "target", probe=False)

    assert not session.hotfixed
    (iterate,) = [f for f in features(session) if f.name.startswith("iterate")]
    assert not iterate.available
    assert "sacrificial dev pod" in iterate.reason


def test_a_hold_aware_probe_costs_the_report_its_restart_deadline() -> None:
    """The other half of #179, end to end through `attach`: `bl47p-mo-ioc-01`
    was told `liveness at 61-91s` on a pod whose probe already returned 0 while
    held."""
    pod = layout_pod()
    pod["spec"]["containers"][0]["livenessProbe"] = {
        "exec": {"command": ["bash", "-c", wrapped_liveness_probe(["/bin/true"])]},
        "initialDelaySeconds": 120,
        "periodSeconds": 30,
    }
    cluster = FakeCluster(pod)

    session = attach(talking_to(cluster), "target", probe=False)
    text = format_session(session)

    # The framing is what was wrong, not the arithmetic: 61-91s is a true
    # statement about this probe once the hold is gone, and it survives as
    # exactly that. What must not survive is it being called a deadline on a
    # pause, which is the thing that cannot happen while the pod is held.
    # Flowed, because the report wraps the note across lines.
    flowed = " ".join(text.split())
    assert "TIME-LIMITED" not in flowed
    assert "no deadline while the hold is in place" in flowed
    assert "Once the hold is gone the target's own check applies again, at" in flowed
    (live,) = [f for f in features(session) if f.name.startswith("live attach")]
    assert "TIME-LIMITED" not in live.note


def test_a_seat_on_a_hotfixed_pod_gets_the_claim_without_being_asked() -> None:
    """The whole of #177 from the attach end.

    The claim was mounted, the pod was prepped and the PVC was Bound, and
    `hotfix init` still refused with "/podbench/app is not present" - because
    `attach` mounted `podbench-home` and nothing else, so the *seat* was the one
    container in the pod that could not see it. An ephemeral container's
    volumeMounts are fixed at creation, so this is the only moment it can be
    fixed at all.
    """
    cluster = FakeCluster(layout_pod())
    session = attach(talking_to(cluster), "target", probe=False)

    mounts = cluster.added[0]["volumeMounts"]
    assert {"name": HOTFIX_CLAIM_VOLUME, "mountPath": HOTFIX_APP_PATH} in mounts
    # And it did not cost the home volume its place.
    assert {"name": SEAT_HOME_VOLUME, "mountPath": SEAT_HOME_PATH} in mounts
    # A mount nobody typed is only acceptable if the output names it.
    assert any(HOTFIX_CLAIM_VOLUME in warning for warning in session.warnings)
    assert any("/podbench/app" in warning for warning in session.warnings)


def test_the_seat_that_gets_the_claim_reads_back_as_a_hotfix_seat() -> None:
    """The mount is not merely present, it is the one `seat_kind` keys on -
    which is what makes `SeatKind.HOTFIX` reachable again. It would not be if
    the claim had been added to `_CONVENTION_VOLUMES` beside the home volume."""
    pod = layout_pod()
    cluster = FakeCluster(pod)
    attach(talking_to(cluster), "target", probe=False)

    assert shares_workload_volume(pod, cluster.added[0], "app")


def test_a_pod_without_the_layout_is_attached_to_exactly_as_before() -> None:
    """The falsification this convention has to survive: a plain `gdb` or
    `vscode` seat on a pod that knows nothing about hotfix mode must land with
    the mounts it always had. The claim is authored off `is_hotfixed`, so a pod
    that fails either half of it is untouched by this."""
    cluster = FakeCluster(identity_pod())
    session = attach(talking_to(cluster), "target", probe=False)

    assert cluster.added[0]["volumeMounts"] == EXPECTED_SEAT_MOUNTS
    assert not any(HOTFIX_CLAIM_VOLUME in warning for warning in session.warnings)


def test_a_claim_volume_without_a_supervisor_is_not_mounted_by_convention() -> None:
    """Half the layout is not the layout. A pod that happens to carry a volume
    of that name gets no mount it did not ask for - the seat is the user's, and
    podbench may only take a liberty where the deployment plainly invited it."""
    cluster = FakeCluster(layout_pod(args=[]))
    attach(talking_to(cluster), "target", probe=False)

    assert not any(
        mount["name"] == HOTFIX_CLAIM_VOLUME
        for mount in cluster.added[0].get("volumeMounts", [])
    )


def test_the_claim_is_mounted_at_the_applications_own_path_not_the_constant() -> None:
    """Hotfix mode's premise is that the claim resolves identically in both: the
    venv's bin/python and the checkout's editable install are absolute paths
    recorded on the claim. So the application's mountPath is copied, and the
    constant is only the fallback."""
    cluster = FakeCluster(
        layout_pod(
            volume_mounts=[{"name": HOTFIX_CLAIM_VOLUME, "mountPath": "/srv/podbench"}]
        )
    )
    attach(talking_to(cluster), "target", probe=False)

    assert {
        "name": HOTFIX_CLAIM_VOLUME,
        "mountPath": "/srv/podbench",
    } in cluster.added[0]["volumeMounts"]


def test_an_explicit_mount_still_wins_over_the_claim_convention() -> None:
    """`--mount` is what somebody typed on purpose, and two mounts of one path
    is not a tie the kubelet breaks sensibly."""
    cluster = FakeCluster(layout_pod())
    attach(
        talking_to(cluster),
        "target",
        mounts=[f"{HOTFIX_CLAIM_VOLUME}:{HOTFIX_APP_PATH}"],
        probe=False,
    )

    claims = [
        mount
        for mount in cluster.added[0]["volumeMounts"]
        if mount["name"] == HOTFIX_CLAIM_VOLUME
    ]
    assert claims == [{"name": HOTFIX_CLAIM_VOLUME, "mountPath": HOTFIX_APP_PATH}]


def test_a_claim_the_seat_cannot_carry_is_a_note_and_not_a_dead_attach() -> None:
    """`resolve_mounts` refuses an application mount carrying a subPath, because
    an ephemeral container may not have one and dropping it silently would
    resolve a different tree at the same path. That refusal is right for a mount
    somebody typed and wrong for one podbench added on its own initiative."""
    cluster = FakeCluster(
        layout_pod(
            volume_mounts=[
                {
                    "name": HOTFIX_CLAIM_VOLUME,
                    "mountPath": HOTFIX_APP_PATH,
                    "subPath": "app",
                }
            ]
        )
    )
    session = attach(talking_to(cluster), "target", probe=False)

    assert cluster.added, "the attach must still land a seat"
    assert any("could not be mounted" in warning for warning in session.warnings), (
        session.warnings
    )


def test_hotfix_claim_mounts_authors_nothing_on_an_ordinary_pod() -> None:
    """The pure half, stated on its own: absence is not an error."""
    assert hotfix_claim_mounts(identity_pod(), "app") == ([], [])


def test_the_home_volume_is_mounted_by_convention_not_by_flag() -> None:
    """No flag, because the volume cannot be there by accident.

    ``spec.volumes`` is immutable, so anything named ``podbench-home`` was put
    in the pod at deploy time on purpose. Making the user repeat that intent on
    every attach would waste the cooperation.
    """
    cluster = FakeCluster(identity_pod())
    session = attach(talking_to(cluster), "target")

    assert landed_rung(session) is Rung.DEGRADED
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
    session = attach(talking_to(cluster), "target")

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
    session = attach(talking_to(cluster), "target")

    assert "volumeMounts" not in cluster.added[0]
    assert not session.identity_mounted
    assert not session.identity_declared
    assert landed_rung(session) is Rung.DEGRADED


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
        talking_to(cluster),
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
        talking_to(cluster),
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
    session = attach(talking_to(cluster), "target")

    assert "volumeMounts" not in cluster.added[0]
    assert session.identity_declared
    env = {entry["name"]: entry["value"] for entry in cluster.added[0]["env"]}
    assert env["HOME"] == "/tmp/podbench-home", "the fallback home, not the volume's"


def test_the_report_says_why_the_declared_identity_volume_is_unused() -> None:
    """Where the user is looking, on the line the volume was meant to decide.

    A pod that declares ``podbench-identity`` is one somebody prepared for
    podbench, so silence here reads as "the preparation failed". It did not: the
    projection is impossible for an ephemeral container, and the live-pod route
    to the same identity is the seat's own record in
    :data:`podbench.agent.SEAT_NSS_PATH`, which asks nothing of the pod.

    That route needs no flag, so the note names none — and in particular it must
    not name a group. Pinning the seat to gid 0 to win a writable
    ``/etc/passwd`` costs the ptrace credential match (#102, #103), so offering
    it here, on the line about a volume, would be the second half of the trap
    issue #102 walked into.
    """
    cluster = FakeCluster(identity_pod(), login_user=None)
    session = attach(talking_to(cluster), "target")

    ssh_seat = features(session)[-2]
    assert not ssh_seat.available
    assert SEAT_IDENTITY_VOLUME in ssh_seat.note
    assert "subPath" in ssh_seat.note
    assert SEAT_NSS_PATH in ssh_seat.note, "the route that does work on a live pod"
    assert "--seat-gid-root" not in ssh_seat.note

    text = format_session(session)
    assert SEAT_IDENTITY_VOLUME in text
    assert SEAT_NSS_PATH in text
    # The retired flag is nowhere in the report, and nor is the thing it did:
    # a reader who cannot get a login will take any offer, so the group-0 route
    # is not one this verb makes (#103).
    assert "--seat-gid-root" not in text
    assert "runAsGroup: 0" not in text

    # …and a pod without the volume says nothing of the sort.
    plain = attach(talking_to(FakeCluster(pod_document(uid=1000))), "target")
    assert SEAT_IDENTITY_VOLUME not in format_session(plain)


def test_a_seat_that_already_has_a_login_is_not_told_to_re_attach() -> None:
    """The same fact, without a remedy under a ticked box.

    A re-attach command printed beside a working ssh seat reads as an
    instruction to fix something that is not broken - and the seat cannot pick
    the volume up on a re-attach anyway. What it says instead is that this seat
    has a login, and where a seat that needs one gets it.

    Not that this seat *registered* one, which the launcher has no way of
    knowing: all it asked for was a login name, and a seat whose uid the image
    already has an account for - root on the full rung, ``nobody`` on a hardened
    workload - resolves it and appends nothing. The stronger claim sent readers
    to ``cat`` an empty file.
    """
    cluster = FakeCluster(identity_pod())
    session = attach(talking_to(cluster), "target")

    ssh_seat = features(session)[-2]
    assert ssh_seat.available
    assert SEAT_IDENTITY_VOLUME in ssh_seat.note
    assert SEAT_NSS_PATH in ssh_seat.note
    assert "re-attach" not in ssh_seat.note
    assert "--seat-gid-root" not in ssh_seat.note
    assert "this seat registered" not in ssh_seat.note


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
    session = attach(talking_to(cluster), "target")

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
    session = attach(talking_to(cluster), "target")

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
    session = attach(talking_to(cluster), "target")

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
    session = attach(talking_to(cluster), "target")
    report = session.report
    assert report is not None
    assert report.verdict is Verdict.READ_ONLY
    assert report.blocker is Blocker.YAMA_SCOPE

    live, read_only, launch, iterate, ssh_seat, exec_seat = features(session)
    assert not live.available
    assert "Yama" in live.reason
    assert read_only.available
    assert launch.available
    assert not iterate.available
    assert ssh_seat.available
    assert exec_seat.available

    text = format_session(session)
    assert "ptrace_scope" in text
    assert "node02" in text
    # And the pod's memory is stated as a measurement rather than as a caution:
    # this fixture declares no limit, so there is no ceiling for the seat to
    # share and nothing to warn about (C19, finding 18).
    assert "memory      no pod memory limit" in text
    assert "OOM" not in text


DIAMOND_READS = {
    "root": False,
    "maps": False,
    "environ": False,
    "cmdline": True,
    "status": True,
    "fd": True,
}
"""The matrix measured on b01-1-beamline/b01-1-blueapi-0, 2026-08-16 (issue #51).

Pinned here as well as in ``tests/test_probe.py`` because the two halves fail
this differently: the probe used to *derive* the wrong verdict from it, and the
launcher used to *ignore* it and tick a box naming the three reads it says are
gone.
"""


def test_the_read_only_tick_comes_from_the_reads_it_names() -> None:
    """A seat that can read none of `root`, `maps` and `environ` must not be
    told it can inspect them — even by an image whose verdict says read_only.

    The launcher recomputes rather than trusting the verdict, so it corrects an
    older image instead of repeating its overclaim.
    """
    cluster = FakeCluster(
        pod_document(uid=1000),
        psa_denies_ptrace=True,
        capreport=capreport_payload(
            verdict="read_only",
            exit_code=10,
            blocker="yama-scope",
            cap_sys_ptrace=False,
            self_uid=1000,
            child_attach_ok=True,
            target_attach_ok=False,
            proc_reads=DIAMOND_READS,
        ),
    )
    session = attach(talking_to(cluster), "target")

    _live, read_only, launch, *_rest = features(session)
    assert not read_only.available
    assert launch.available

    text = format_session(session)
    assert "[ ] read-only inspect" in text
    assert "[x] debug launched processes" in text
    # The evidence travels with the tick, so the two cannot drift apart again.
    assert "cmdline, status and fd only; root, maps and environ denied" in text


def test_a_launch_only_verdict_survives_the_json_round_trip() -> None:
    cluster = FakeCluster(
        pod_document(uid=1000),
        psa_denies_ptrace=True,
        capreport=capreport_payload(
            verdict="launch_only",
            exit_code=15,
            blocker="yama-scope",
            summary=Verdict.LAUNCH_ONLY.summary,
            cap_sys_ptrace=False,
            self_uid=1000,
            child_attach_ok=True,
            target_attach_ok=False,
            proc_reads=DIAMOND_READS,
        ),
    )
    session = attach(talking_to(cluster), "target")
    report = session.report

    assert report is not None
    assert report.verdict is Verdict.LAUNCH_ONLY
    assert "podbench dbg --launch" in format_session(session)


def test_an_unmeasured_matrix_is_not_reported_as_a_refusal() -> None:
    """An empty box is not a denied one, and the report must not say it is.

    `capreport` with no target pid resolves no matrix and still verdicts
    live_attach — no `PODBENCH_TARGET_CID`, or nothing in a cgroup matching the
    target's container id. Deriving the tick from the reads made that shape
    print an unticked box blaming "the mechanism that refused attach", three
    lines above `blocker  none`.
    """
    cluster = FakeCluster(
        pod_document(uid=1000),
        capreport=capreport_payload(target_pid=None, target_uid=None, proc_reads={}),
    )
    session = attach(talking_to(cluster), "target")

    _live, read_only, *_rest = features(session)
    assert not read_only.available
    assert "this is not a refusal" in read_only.reason
    assert "refused attach" not in read_only.reason

    text = format_session(session)
    assert "no /proc reads were measured" in text
    assert "blocker     none" in text


def test_a_seat_that_cannot_ptrace_at_all_does_not_claim_gdb_launch() -> None:
    """`dbg --launch` used to ride along on the exec-seat tick, which is always
    true. gdb traces an inferior it forked, and here that forked attach failed."""
    cluster = FakeCluster(
        pod_document(uid=1000),
        psa_denies_ptrace=True,
        capreport=capreport_payload(
            verdict="none",
            exit_code=20,
            blocker="seccomp",
            cap_sys_ptrace=False,
            self_uid=1000,
            child_attach_ok=False,
            target_attach_ok=False,
            proc_reads=dict.fromkeys(DIAMOND_READS, False),
        ),
    )
    session = attach(talking_to(cluster), "target")

    _live, read_only, launch, *_rest = features(session)
    assert not read_only.available
    assert not launch.available
    assert "forked itself" in launch.reason


PROBES: dict[str, Any] = {
    "readinessProbe": {"periodSeconds": 5, "initialDelaySeconds": 2},
    "livenessProbe": {"periodSeconds": 10, "initialDelaySeconds": 5},
}


def test_a_probed_target_is_given_its_deadline_before_it_costs_anything() -> None:
    """The readiness half of this is invisible after the fact — the pod stops
    taking Service traffic, leaves no restart behind and recovers on continue —
    so the report has to state it up front or not at all."""
    cluster = FakeCluster(pod_document(uid=1000, probes=PROBES))
    session = attach(talking_to(cluster), "target")

    assert [budget.kind.value for budget in session.probes] == [
        "readiness",
        "liveness",
    ]
    text = format_session(session)
    assert "TIME-LIMITED" in text, "a bare [x] means two different things"
    # Both deadlines, on the line the tick is on. There is no WARNING block
    # holding the arithmetic any more: it was the longest thing this verb
    # printed, and everything in it that was about *this* pod is these numbers.
    assert "readiness at 11-16s" in text
    assert "liveness at 21-31s" in text
    assert "`podbench dev`" in text
    assert "WARNING" not in text.split("supports")[1].split("measured")[0]


def test_an_unprobed_target_is_told_so_rather_than_left_to_infer_it() -> None:
    cluster = FakeCluster(pod_document(uid=1000))
    session = attach(talking_to(cluster), "target")

    assert session.probes == ()
    text = format_session(session)
    assert "no deadline: 'app' declares no readiness" in text
    assert "on a timer" not in text, "there is nothing to warn about here"


def test_a_missing_capreport_is_reported_rather_than_assumed() -> None:
    cluster = FakeCluster(pod_document(uid=1000))
    cluster.capreport_output = "capreport: command not found"
    session = attach(talking_to(cluster), "target")
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


def test_an_unknown_verdict_is_said_rather_than_flattened() -> None:
    """The sibling of the unknown-blocker note. This PR added a rung, so a
    launcher one release behind an image is a shape that now exists: reading
    `no access to the target process` off a verdict it has never heard of hides
    exactly what the newer image was trying to report."""
    report = capability_report_from_json(
        capreport_payload(
            verdict="single_step_only",
            exit_code=17,
            summary="single-step only",
        )
    )
    assert report.verdict is Verdict.NONE
    assert any("single_step_only" in note for note in report.notes)
    assert any("single-step only" in note for note in report.notes)


def test_the_pause_a_probe_cost_is_stated_and_the_flag_that_skips_it_named() -> None:
    """Both halves of finding 14: the probe stops nothing now, and it says so.

    The complaint was never the stop — it was that a pause happened silently,
    on pods where a pause is forbidden. A line that appears only when something
    went wrong cannot be checked beforehand, so this one is unconditional, and
    it sits under a heading naming the flag that skips the whole block.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    text = format_session(attach(talking_to(cluster), "target"))
    assert "measured    --no-probe skips this block" in text
    assert "pause       none - PTRACE_SEIZE does not stop the tracee" in text


def test_an_image_older_than_the_seize_reports_an_unmeasured_pause() -> None:
    """A missing key is "this seat is older than the question", and must not be
    rendered as an assurance that nothing was stopped."""
    payload = capreport_payload()
    del payload["attach_method"]
    report = capability_report_from_json(payload)
    assert report.attach_method is None
    assert describe_pause(report.attach_method, report.target_attach_ok) == (
        "not measured"
    )


def test_an_image_older_than_the_lsm_question_keeps_its_label() -> None:
    """#104 renamed the key, and the versioning rule cuts both ways.

    A seat older than the rename sends only ``apparmor_profile``, and its
    string is still the seat's label - so it is read, not discarded. What is
    *not* invented is which LSM wrote it: an absent ``lsm`` is ``NONE``, the
    same "this image never said" silence ``self_gid`` and ``attach_method``
    use, and it is not grounds for reporting a mismatch.
    """
    payload = capreport_payload()
    for key in ("lsm", "lsm_label", "lsm_label_target"):
        del payload[key]
    report = capability_report_from_json(payload)
    assert report.lsm_label == "system_u:system_r:spc_t:s0"
    assert report.lsm is Lsm.NONE
    assert report.lsm_label_target is None


def test_an_lsm_this_launcher_has_never_heard_of_is_not_invented() -> None:
    report = capability_report_from_json(capreport_payload(lsm="tomoyo"))
    assert report.lsm is Lsm.NONE


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
            # The ProxyCommand under test is the root sshd's, which is the full
            # rung's; a target at a known non-root uid otherwise gets the rung
            # below and the layout that goes with it.
            "--max-rung",
            "full",
        ],
        runner=cluster,
    )
    assert code == 0

    stanza = (tmp_path / "cfg" / "config.d" / "demo-target-1.conf").read_text()
    assert "Host podbench-demo-target-1" in stanza
    # Seat-qualified too: two live seats mint two host keys, and one alias
    # over both is a host-key mismatch on the second (issue #93).
    assert f"HostKeyAlias podbench-{POD_UID}-1" in stanza
    assert "StrictHostKeyChecking yes" in stanza
    # The ProxyCommand shape is the whole transport: -i, -e and a quiet log
    # level, no -t, no redirection (report 4.1).
    assert (
        "ProxyCommand kubectl -n demo exec -i target -c podbench-1 -- "
        "/usr/sbin/sshd -i -e -f /etc/podbench/sshd_config -o LogLevel=ERROR"
    ) in stanza

    known_hosts = (tmp_path / "cfg" / "known_hosts").read_text()
    assert known_hosts.startswith(f"podbench-{POD_UID}-1 ssh-ed25519 ")
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
    env = {entry["name"]: entry["value"] for entry in cluster.added[0]["env"]}
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


def superseded_pair() -> FakeCluster:
    """The two live seats the gid correction leaves behind.

    A target that pins ``runAsUser: 1000`` and no ``runAsGroup`` puts the first
    seat in the image's group 0 against a target in group 1000, so podbench
    measures the real gid and lands a corrected seat beside it. Both are
    running; the earlier one cannot trace and its agent wrote sshd's config for
    a *different* ``$HOME``, and it is the one listed first, because ephemeral
    containers are appended in order and never removed.
    """
    return FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[
                {
                    "name": "podbench-1",
                    "targetContainerName": "app",
                    "securityContext": {"runAsUser": 1000},
                },
                {
                    "name": "podbench-2",
                    "targetContainerName": "app",
                    "securityContext": {"runAsUser": 1000, "runAsGroup": 1000},
                },
            ],
            ephemeral_statuses=[
                running_status("podbench-1"),
                running_status("podbench-2"),
            ],
        )
    )


def test_ssh_config_on_a_two_seat_pod_names_the_corrected_seat(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #117. ``ssh-config`` asked :func:`running_seat` with no ids where
    ``attach`` passes the ones it wants, so it took the first live container it
    found - the superseded one - and emitted a stanza whose ProxyCommand named
    the sshd config path of a seat nobody should be connecting to."""
    cluster = superseded_pair()
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
    out = capsys.readouterr().out

    assert code == 0
    assert "-c podbench-2" in out
    assert "-c podbench-1" not in out


def test_a_superseded_seat_is_never_the_seat_to_reconnect_to() -> None:
    """The same selection, one layer down and for every caller of it: a stanza
    is not the only thing derived from "which seat is this pod's". The pod
    chooser's SEAT column and ``attach``'s own reconnect read the same answer."""
    cluster = superseded_pair()
    session = attach(talking_to(cluster), "target")

    assert session.reused
    assert session.seat.container == "podbench-2"
    assert cluster.added == []


def legacy_corrected_pair() -> FakeCluster:
    """The pair an older podbench left on six p47 pods, read 2026-08-21.

    ``bl47p-mo-ioc-01-0`` and ``p47-epics-pvcs-...-jffpp``: a full-rung seat at
    uid 0 whose ``capabilities.add`` a mutating policy rewrote without
    ``SYS_PTRACE``, and beside it the seat a later build landed at the target's
    own 37887:37887. The uids **differ**, so the pair carries none of
    :func:`superseded_seats`' signature, and neither seat records an owner - the
    stamp is younger than both.
    """
    return FakeCluster(
        pod_document(
            uid=37887,
            ephemeral=[
                {
                    "name": "podbench-1",
                    "targetContainerName": "app",
                    "securityContext": {"runAsUser": 0},
                },
                {
                    "name": "podbench-2",
                    "targetContainerName": "app",
                    "securityContext": {"runAsUser": 37887, "runAsGroup": 37887},
                },
            ],
            ephemeral_statuses=[
                running_status("podbench-1"),
                running_status("podbench-2"),
            ],
        )
    )


def test_ssh_config_prefers_the_seat_that_can_reach_the_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`6ffac34` reached only the pairs this build creates.

    Its guard is effectively ``(later.target, later.uid) != (earlier.target,
    earlier.uid)``, and the leftover p47 pairs differ in the uid, so the pair was
    skipped and ``ssh-config`` emitted a stanza for ``podbench-1`` - whose
    verdict reads "launch-only ... no read-only inspection" - in preference to
    ``podbench-2``, which reads "live attach available".
    """
    cluster = legacy_corrected_pair()
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
    out = capsys.readouterr().out

    assert code == 0
    assert "-c podbench-2" in out
    assert "-c podbench-1" not in out


def test_a_reconnect_takes_the_same_answer_as_the_stanza() -> None:
    """One selection for every caller, which is `6ffac34`'s own argument: a
    stanza is not the only thing derived from "which seat is this pod's"."""
    cluster = legacy_corrected_pair()
    session = attach(talking_to(cluster), "target")

    assert session.reused
    assert session.seat.container == "podbench-2"
    assert cluster.added == []


def test_a_capability_seat_is_not_passed_over_for_its_uid() -> None:
    """The full rung is root against every target by construction, and
    ``CAP_SYS_PTRACE`` is exempt from the credential check entirely. Demoting it
    for a uid it does not need would trade the best seat on the pod for one that
    merely matches - the same trade :func:`id_correction` refuses."""
    cluster = FakeCluster(
        pod_document(
            uid=37887,
            ephemeral=[
                {
                    "name": "podbench-1",
                    "targetContainerName": "app",
                    "securityContext": {
                        "runAsUser": 0,
                        "capabilities": {"add": ["SYS_PTRACE"]},
                    },
                },
                {
                    "name": "podbench-2",
                    "targetContainerName": "app",
                    "securityContext": {"runAsUser": 37887, "runAsGroup": 37887},
                },
            ],
            ephemeral_statuses=[
                running_status("podbench-1"),
                running_status("podbench-2"),
            ],
        )
    )
    seat = running_seat(cluster.pod)

    assert seat is not None and seat.name == "podbench-1"


def test_the_only_seat_is_offered_even_where_it_cannot_reach_the_target() -> None:
    """A preference, not a filter. A seat that cannot inspect is still an
    editor, a shell and a git checkout, and refusing to name it would replace a
    working `ssh-config` with an error on a pod that has one seat."""
    cluster = FakeCluster(
        pod_document(
            uid=37887,
            ephemeral=[
                {
                    "name": "podbench-1",
                    "targetContainerName": "app",
                    "securityContext": {"runAsUser": 0},
                }
            ],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    seat = running_seat(cluster.pod)

    assert seat is not None and seat.name == "podbench-1"


def test_a_seat_landed_by_somebody_else_is_still_never_borrowed() -> None:
    """The tie-break runs *after* `25cd006`'s owner rule and cannot undo it.

    A colleague's seat matching the target perfectly is still a seat whose
    ``authorized_keys`` was written for another key, so preferring it would hand
    back a stanza that gets `Permission denied (publickey)`.
    """
    cluster = FakeCluster(
        pod_document(
            uid=37887,
            ephemeral=[
                {
                    "name": "podbench-1",
                    "targetContainerName": "app",
                    "securityContext": {"runAsUser": 0},
                    "env": [{"name": "PODBENCH_OWNER", "value": "bob"}],
                },
                {
                    "name": "podbench-2",
                    "targetContainerName": "app",
                    "securityContext": {"runAsUser": 37887, "runAsGroup": 37887},
                    "env": [{"name": "PODBENCH_OWNER", "value": "alice"}],
                },
            ],
            ephemeral_statuses=[
                running_status("podbench-1"),
                running_status("podbench-2"),
            ],
        )
    )
    seat = running_seat(cluster.pod, owner="bob")

    assert seat is not None and seat.name == "podbench-1"


def stripped_root_seat() -> FakeCluster:
    """A running root seat that reads back as the degraded rung.

    The shape a mutating admission webhook leaves behind when it drops
    ``capabilities.add`` from a full-rung seat: ``runAsUser: 0`` and nothing
    added, which is what :func:`podbench.launcher.rung_of_spec` calls degraded.
    A reconnect sees only the spec, so this is the seat it has to describe —
    and it is a *root* one, whose agent chose the root layout from
    ``os.geteuid()`` and whose login name NSS resolves as ``root``.
    """
    return FakeCluster(
        pod_document(
            uid=0,
            ephemeral=[{"name": "podbench-1", "securityContext": {"runAsUser": 0}}],
            ephemeral_statuses=[running_status("podbench-1")],
        ),
        login_user="root",
    )


def argus_shaped_pod() -> FakeCluster:
    """A root seat beside a root target, on a pod that pins no uid at all.

    argus (`hgv27681`), where no workload sets ``runAsUser`` and every target
    runs as root. The seat landed at the full rung and a mutating policy took
    ``capabilities.add`` off it, so the spec reads ``runAsUser: 0`` and nothing
    added - and the seat measurably reads all six ``/proc`` paths and runs gdb
    to a symbolised backtrace, because its uid matches the target's.
    """
    return FakeCluster(
        pod_document(
            ephemeral=[{"name": "podbench-1", "securityContext": {"runAsUser": 0}}],
            ephemeral_statuses=[running_status("podbench-1")],
        ),
        login_user="root",
        capreport=capreport_payload(
            self_uid=0, self_gid=0, target_uid=0, target_gid=0, cap_sys_ptrace=False
        ),
    )


def test_a_root_seat_beside_a_root_target_measures_as_degraded() -> None:
    """Issue #94's first cause: ``uid and uid == target_uid`` read uid 0 as
    falsy, so the one credential match the kernel honours everywhere - root
    tracing root - fell through to the rung that claims no ``/proc`` access at
    all. On argus that is every pod."""
    session = attach(talking_to(argus_shaped_pod()), "target")

    assert session.rung is Rung.DEGRADED


def test_one_seat_carries_one_label_on_a_reconnect() -> None:
    """The whole of #94's defect: the same container read ``full`` in the attach
    header, ``degraded`` on the reconnect line and ``degraded`` in the RUNG
    column. The header and the ladder line are both here, and they are the same
    reading of the same seat - the *measured* one, since the ladder did not run
    and its rung was ``rung_of_spec``'s answer about a securityContext."""
    session = attach(talking_to(argus_shaped_pod()), "target")

    assert session.reused
    assert len(session.steps) == 1
    assert session.steps[0].rung is session.rung is Rung.DEGRADED


def test_a_first_attach_keeps_the_rungs_the_walk_submitted() -> None:
    """The relabelling is a reconnect's, and only a reconnect's. A walk's steps
    are what admission was asked for and what it said, which is a different
    question from what the seat turned out to be - and it is the only record of
    a refusal, whose rung never landed and can never have been measured."""
    cluster = FakeCluster(pod_document(uid=1000), psa_denies_ptrace=True)
    session = attach(talking_to(cluster), "target")

    assert [step.rung for step in session.steps] == [Rung.FULL, Rung.DEGRADED]
    assert [step.admitted for step in session.steps] == [False, True]


def test_a_root_seat_read_back_as_degraded_keeps_the_root_sshd_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Measured at DLS on 2026-08-16, and silent until an editor said so.

    The uid decides the layout, not the rung: ``session.uid or _UNPINNED_UID``
    read a seat pinned to uid 0 as one that pinned nothing, so the reconnect
    named ``/tmp/podbench-home/.podbench/sshd_config`` in a container whose
    agent had written ``/etc/podbench/sshd_config``. The landing attach got it
    right — the ladder remembers the rung it asked for — so only the *second*
    session broke, with ``No such file or directory`` arriving on the ssh
    client's stderr as a Remote-SSH resolver error.
    """
    cluster = stripped_root_seat()

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
    assert "-f /etc/podbench/sshd_config" in out
    assert "/tmp/podbench-home" not in out, "the non-root home of a seat that is root"


def test_the_login_name_is_the_one_the_seat_answered_not_the_rung_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The second half of the same reconnect, and the failure behind the fix.

    The rung's default login name for anything but the full rung is
    ``podbench``, which the agent registers only for a uid NSS cannot already
    resolve — and uid 0 always resolves, as ``root``. So a root seat read back
    as degraded got a stanza naming an account that does not exist, and sshd
    refuses an unresolvable login before it looks at a key. The seat was asked
    the question all along; the answer just went unused.
    """
    cluster = stripped_root_seat()

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
    assert "User root" in capsys.readouterr().out


def test_a_stripped_root_seat_is_reported_as_what_it_measures() -> None:
    """The rung line's half of #89, which the verdict column's fix left behind.

    ``rung_of_spec`` reads a pinned ``runAsUser`` and no added capability as
    :attr:`Rung.DEGRADED`, which is true of a seat authored at that rung *and*
    of a full-rung seat a mutating policy took the capability from. Only the
    first is at the target's uid — this one is root against a uid-1000 target.
    The rung line used to weaken its wording to survive that ambiguity ("a
    pinned UID"); the ambiguity is in the *source*, so the reconnect measures
    the seat instead and the line names both numbers.

    The target is uid 1000 here rather than the root one
    :func:`stripped_root_seat` builds, because against a root target "the
    target's UID" happens to be true and the claim cannot be caught.
    """
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[{"name": "podbench-1", "securityContext": {"runAsUser": 0}}],
            ephemeral_statuses=[running_status("podbench-1")],
        ),
        login_user="root",
    )
    session = attach(talking_to(cluster), "target")

    assert session.rung is Rung.SEAT, (
        "root with no effective capability is not the degraded rung: it reads "
        "three of the six probe paths where that rung reads all six (3.11)"
    )
    assert session.uid == 0, "the seat admission left behind is root"
    text = format_session(session)
    assert "rung        seat - uid 0, gid 0, CapEff 0000000000000000" in text
    assert "the target's UID" not in text
    assert "a pinned UID" not in text


def test_the_rung_line_is_measured_where_the_spec_says_otherwise() -> None:
    """The opposite direction, and the one a dry run cannot reach.

    A seat whose spec pins the target's uid and adds nothing reads back as the
    degraded rung, and this one is running as root with CAP_SYS_PTRACE anyway —
    a node whose runtime granted more than the stored spec asked for. The spec
    is not the container in either direction, so the label comes from the only
    place that can settle it.
    """
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[{"name": "podbench-1", "securityContext": {"runAsUser": 1000}}],
            ephemeral_statuses=[running_status("podbench-1")],
        ),
        seat_status=status_text(0, 0, 1 << 19),
        login_user="root",
    )
    session = attach(talking_to(cluster), "target")

    assert session.rung is Rung.FULL
    assert "rung        full - uid 0, gid 0, CapEff 0000000000080000" in format_session(
        session
    )


def test_an_unreadable_seat_reports_the_rung_as_asked_for_and_says_so() -> None:
    """``cat`` is universal, but an exec can still be refused.

    Nothing may fail an attach that has landed (report 4.2), so the rung
    reverts to the walk's own claim — labelled as one, because a rung that is
    not a measurement is exactly what issue #94 was.
    """
    cluster = FakeCluster(pod_document(uid=1000), seat_status="")
    session = attach(talking_to(cluster), "target")

    assert session.credentials is None
    assert session.rung is Rung.DEGRADED
    text = format_session(session)
    assert "rung        degraded - as asked for; the seat's own" in text


def test_no_probe_still_measures_the_rung() -> None:
    """The cheap question and the expensive one are not the same flag.

    ``--no-probe`` declines to ptrace the target. Reading the seat's own
    ``/proc/self/status`` attempts nothing against anybody, so it stays — which
    is what leaves a ``--no-probe`` attach with one measured line rather than
    with none.
    """
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[{"name": "podbench-1", "securityContext": {"runAsUser": 0}}],
            ephemeral_statuses=[running_status("podbench-1")],
        ),
        login_user="root",
    )
    session = attach(talking_to(cluster), "target", probe=False)

    assert session.report is None
    assert session.rung is Rung.SEAT
    text = format_session(session)
    assert "rung        seat - uid 0, gid 0, CapEff 0000000000000000" in text
    assert "\nmeasured\n" not in text, "the capability report block still ran"


def test_the_ssh_stanza_names_the_login_the_seat_runs_as_not_the_rungs() -> None:
    """The #94 seat, reached by the verb that only ever sees the spec.

    ``ssh-config`` builds its session from the container's securityContext, and
    this one pins uid 1000 while the container runs as root — a node's runtime
    granting more than the stored spec asked for. Both halves of the transport
    turn on that uid: sshd's config lives in ``/etc/podbench`` for root and
    under ``$HOME`` for anybody else, and ``podbench`` is a login name NSS
    cannot resolve at uid 0.
    """
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[{"name": "podbench-1", "securityContext": {"runAsUser": 1000}}],
            ephemeral_statuses=[running_status("podbench-1")],
        ),
        seat_status=status_text(0, 0),
        # The seat cannot say either: an image with no account for uid 0 beyond
        # `root` answers with nothing, which is what leaves the fallback in
        # charge of the whole transport.
        login_user=None,
    )
    session = Session(
        seat=ContainerRef(PodRef("demo", "target"), "podbench-1"),
        workload="app",
        rung=Rung.DEGRADED,
        reused=True,
        uid=1000,
        credentials=probe_seat_credentials(
            talking_to(cluster), ContainerRef(PodRef("demo", "target"), "podbench-1")
        ),
    )

    assert session.root_seat
    assert seat_layout(session).run_as_root
    assert seat_layout(session).config_path.startswith("/etc/podbench")


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
    assert "group of 500 or more" in out
    assert "--seat-gid-root" not in out, "the flag retired with #103"
    # Every remedy offered has to be able to work. A released launcher computes
    # its image tag from its own version, so `--new --pull always` re-pulls the
    # identical tag and lands the identical seat - having burnt a second
    # ephemeral container name on the pod for good. What can produce a newer
    # image is a newer launcher or a named tag, so those are what is offered.
    assert "--new --pull always" not in out
    assert "uvx podbench@latest" in out
    assert "--image" in out
    # Spelled as the verb: the image ships no per-subcommand aliases (#47), so
    # a hint the user pastes has to name the one program that is on PATH.
    assert "kubectl exec -n demo target -c podbench-1 -- podbench capreport" in out
    assert "kubectl exec -n demo target -c podbench-1 -- podbench pids" in out


def test_the_capability_report_marks_the_ssh_seat_unavailable() -> None:
    cluster = FakeCluster(
        pod_document(uid=1000), psa_denies_ptrace=True, login_user=None
    )
    session = attach(talking_to(cluster), "target")

    assert session.ssh is not None and not session.ssh.usable
    ssh_seat, exec_seat = features(session)[-2:]
    assert not ssh_seat.available
    assert "not writable" in ssh_seat.reason
    # The half that never needed sshd is still claimed, because it still works.
    assert exec_seat.available
    # …and it is named the way it is invoked. The image ships no per-subcommand
    # aliases (#47), so a label reading "capreport, pids" would be teaching a
    # spelling that resolves nowhere.
    assert "podbench capreport" in exec_seat.name

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


def test_a_webhook_denial_drops_a_rung_rather_than_ending_the_walk() -> None:
    """Issue #77: a Kyverno refusal of the full rung ended the attach.

    Every non-PSA admission engine took that path, in namespaces where the
    degraded rung would have been admitted — which is the entire promise of a
    ladder. A denial is a verdict about one rung, not about the attach.
    """
    cluster = FakeCluster(pod_document(uid=1000), admission_error=KYVERNO_REFUSAL)
    session = attach(talking_to(cluster), "target", max_rung=Rung.FULL)

    assert landed_rung(session) is Rung.DEGRADED
    # Nothing was stored, so the name the refused rung was submitted under is
    # still free — the same accounting as a PSA refusal.
    assert session.seat.container == "podbench-1"
    steps = {step.rung: step for step in session.steps}
    assert not steps[Rung.FULL].admitted
    # "the dry run", because that is where it was refused now: the rung is
    # rehearsed before a name is committed to it (report 4.2).
    assert "an admission webhook refused the dry run" in steps[Rung.FULL].detail
    # The policy's own words, whole: they name the rule and the field, which is
    # the only thing in the message a cluster admin can act on.
    assert "kp-validate-block-privileged-containers-standard" in (
        steps[Rung.FULL].detail
    )


def stored(cluster: FakeCluster) -> list[dict[str, Any]]:
    """The ephemeral containers the pod actually carries.

    Distinct from ``cluster.added``, which is every spec podbench *authored*: a
    rung refused or withdrawn at the dry run is authored and never stored, and
    the difference between the two lists is the container names C2 stops
    spending.
    """
    return cast(list[dict[str, Any]], cluster.pod["spec"]["ephemeralContainers"])


def test_a_dry_run_refusal_costs_no_container_name() -> None:
    """The rehearsal, and the reason for it (report 4.2).

    A synchronous refusal never stored anything either, so what this pins is the
    mechanism: the refused rung goes through ``?dryRun=All``, and the pod ends
    the attach carrying exactly the one container that landed.
    """
    cluster = FakeCluster(pod_document(uid=1000), psa_denies_ptrace=True)
    session = attach(talking_to(cluster), "target", max_rung=Rung.FULL)

    assert any("dryRun=All" in arg for call in cluster.calls for arg in call)
    assert session.seat.container == "podbench-1"
    assert [entry["name"] for entry in stored(cluster)] == ["podbench-1"]


def test_a_validating_admission_policy_is_not_reported_as_a_webhook() -> None:
    """Issue #93's other half. The ladder already *acted* on a native policy
    correctly; the report called it a webhook, which sends a reader looking for
    a webhook that does not exist."""
    cluster = FakeCluster(pod_document(uid=1000), admission_error=VAP_REFUSAL)
    session = attach(talking_to(cluster), "target", max_rung=Rung.FULL)

    assert landed_rung(session) is Rung.DEGRADED
    detail = {step.rung: step for step in session.steps}[Rung.FULL].detail
    assert "a ValidatingAdmissionPolicy refused" in detail
    assert "webhook" not in detail


def test_a_stripped_capability_is_seen_before_the_rung_is_spent() -> None:
    """The mutating half of admission, which refuses nothing (issue #94).

    A policy that strips ``capabilities.add`` leaves the update succeeding and a
    root seat landing with ``CapEff: 0``, which reads three of the six probe
    paths where the degraded rung at the target's own uid reads all six (report
    3.11). Landing it is worse than not landing it, and the dry run is the only
    place the strip is visible.
    """

    def strip(container: dict[str, Any]) -> None:
        security = cast(dict[str, Any], container.get("securityContext", {}))
        cast(dict[str, Any], security.get("capabilities", {})).pop("add", None)

    cluster = FakeCluster(pod_document(uid=1000), mutate=strip)
    session = attach(talking_to(cluster), "target", max_rung=Rung.FULL)

    assert landed_rung(session) is Rung.DEGRADED
    assert [entry["name"] for entry in stored(cluster)] == ["podbench-1"]
    detail = {step.rung: step for step in session.steps}[Rung.FULL].detail
    assert "remove SYS_PTRACE" in detail
    assert "--max-rung degraded" in detail


def test_a_stripped_capability_against_a_root_target_is_taken_anyway() -> None:
    """Report 3.11 compares two rungs, and a root target only has one.

    The strip above drops the full rung because the rung below, at the target's
    own uid, reads more. Against a root target there is no rung below:
    ``runAsNonRoot: true`` cannot express uid 0, so every lower rung is ruled
    out before the walk starts. Refusing there leaves nothing admitted at all —
    which is what the ``dls-ioc`` e2e fixture measured, losing its seat.
    """

    def strip(container: dict[str, Any]) -> None:
        security = cast(dict[str, Any], container.get("securityContext", {}))
        cast(dict[str, Any], security.get("capabilities", {})).pop("add", None)

    cluster = FakeCluster(pod_document(uid=0), mutate=strip)
    session = attach(talking_to(cluster), "target", max_rung=Rung.FULL)

    # A seat, rather than the "no rung of the capability ladder was admitted"
    # the refusal produced. One name, and the strip is said out loud.
    assert [entry["name"] for entry in stored(cluster)] == ["podbench-1"]
    full = {step.rung: step for step in session.steps}[Rung.FULL]
    assert full.admitted
    assert any("admission removed SYS_PTRACE" in text for text in session.warnings)
    assert any("no rung below to prefer" in text for text in session.warnings)
    # And it does not cite the number report 3.11 measured in the case this
    # line never covers. §3.11 put a root seat against a *non-root* target, a
    # uid mismatch; here the walk found no non-root uid to pin, so the uids
    # agree and the reads are unaffected. On argus (hgv27681) `capreport`
    # measured 6/6 while this warning claimed three, four lines under a
    # `supports` block that had already said all six were readable.
    assert not any("three of the six" in text for text in session.warnings)


def test_a_strip_and_a_rewrite_are_both_reported() -> None:
    """Both DLS clusters do both, in one pass, and only the strip was printed.

    The stripped-capability arm used to return before the rewrite check, so on a
    policy that removes ``SYS_PTRACE`` *and* hands the seat the thirteen
    capabilities of its own baseline - which is every attach on p47-beamline and
    on hgv27681 - the rewrite was dropped on the floor. Two facts, two lines: a
    warning is one line, and these have two different remedies.
    """

    def strip_and_add(container: dict[str, Any]) -> None:
        security = cast(dict[str, Any], container["securityContext"])
        cast(dict[str, Any], security["capabilities"])["add"] = ["CHOWN", "SETGID"]

    cluster = FakeCluster(pod_document(uid=0), mutate=strip_and_add)
    session = attach(talking_to(cluster), "target", max_rung=Rung.FULL)

    assert any("admission removed SYS_PTRACE" in text for text in session.warnings)
    rewrite = [
        warning for warning in session.warnings if "admission rewrote" in warning
    ]
    assert len(rewrite) == 1
    assert "added CHOWN, SETGID to capabilities.add" in rewrite[0]
    # And the strip is named once. It has its own warning above; repeating it
    # inside the rewrite list would be one event answered twice.
    assert "removed SYS_PTRACE" not in rewrite[0]


def test_a_mutation_that_would_wedge_the_kubelet_is_not_spent() -> None:
    """The expensive one: ``runAsNonRoot: true`` beside uid 0.

    The API server takes it and the kubelet refuses it seconds later, which is
    the one refusal that burns a container name for the pod's lifetime (report
    3.18). ``spec.py`` never authors that pair; a mutating policy assembles it,
    and the dry run reads it back before a name is committed.
    """

    def add_non_root(container: dict[str, Any]) -> None:
        cast(dict[str, Any], container["securityContext"])["runAsNonRoot"] = True

    cluster = FakeCluster(pod_document(uid=0), mutate=add_non_root)
    session = attach(talking_to(cluster), "target", poll_interval=0.0)

    # The seat rung authors that pair itself against a root target, having no
    # uid it may pin, so it is not read as a rewrite and still lands.
    assert session.rung is Rung.SEAT
    assert [entry["name"] for entry in stored(cluster)] == ["podbench-1"]
    detail = {step.rung: step for step in session.steps}[Rung.FULL].detail
    assert "CreateContainerConfigError" in detail


def test_a_rewrite_that_costs_nothing_is_one_warning_and_not_a_dropped_rung() -> None:
    """DLS, 2026-08-19: podbench asks for ``drop: [ALL]`` and adds nothing, and
    the pod comes back with thirteen capabilities added to it. Harmless to the
    rung, and still not what was asked for - so it is said once, on the line the
    rung is reported on, rather than acted on."""

    def add_baseline(container: dict[str, Any]) -> None:
        capabilities = cast(
            dict[str, Any],
            cast(dict[str, Any], container["securityContext"])["capabilities"],
        )
        capabilities["add"] = ["CHOWN", "SETGID"]

    cluster = FakeCluster(pod_document(uid=1000), mutate=add_baseline)
    session = attach(talking_to(cluster), "target")

    assert landed_rung(session) is Rung.DEGRADED
    rewrite = [
        warning for warning in session.warnings if "admission rewrote" in warning
    ]
    assert len(rewrite) == 1
    assert "added CHOWN, SETGID to capabilities.add" in rewrite[0]
    # The stored spec is not the container, so the warning does not send the
    # reader to `status` to find out what landed - the rung line above it is
    # already a measurement, and the capabilities admission added are in
    # `CapBnd` at this uid rather than in the effective set (report 3.10).
    assert "read from the seat itself" in rewrite[0]
    assert session.credentials is not None
    assert session.credentials.capabilities.effective == 0


def test_an_allow_listed_run_as_user_refusal_names_the_uids_and_the_flag() -> None:
    """The refusal with an answer (DLS, 2026-08-19).

    The seat rung may not invent a uid: this cluster allow-lists the ones a
    host-mounting pod may run as, so the fix is to pick one, and the report is
    where the reader finds out which and how.
    """
    cluster = FakeCluster(pod_document(uid=1000), allowed_uids=(36096, 37887))
    with pytest.raises(LauncherError) as raised:
        attach(talking_to(cluster), "target")

    message = str(raised.value)
    assert "takes only 36096, 37887" in message
    assert "--target-uid 36096" in message
    assert stored(cluster) == []


def test_a_target_at_a_known_uid_starts_at_the_rung_that_matches_it() -> None:
    """Sweep finding 07, and report 3.11 behind it.

    A seat at the target's own uid satisfies ``PTRACE_MODE_ATTACH`` by
    credential match, which is where all nine of the sweep's symbolised
    backtraces came from. Trying the capability rung first spends a permanent
    container name proving something the target already implied.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    session = attach(talking_to(cluster), "target")

    assert landed_rung(session) is Rung.DEGRADED
    assert security_contexts(cluster) == [
        {
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
            "privileged": False,
            "allowPrivilegeEscalation": False,
            "runAsUser": 1000,
        }
    ]
    # Said rather than left out: the full rung is above this one and was never
    # tried, and the ladder line is where that is explained.
    full = {step.rung: step for step in session.steps}[Rung.FULL]
    assert not full.admitted
    assert "--max-rung full" in full.detail


def test_a_root_target_still_starts_at_the_capability_rung() -> None:
    """There is no degraded rung for uid 0 to fall back to - ``runAsNonRoot:
    true`` cannot express it - so the capability rung is the only one worth a
    name, and it is kept."""
    plan = [rung for rung, _ in plan_ladder(pod_document(uid=0), "app")]
    assert plan[0] is Rung.FULL


def test_a_webhook_that_did_not_answer_is_still_an_error() -> None:
    """ "Unreachable" is not "denied", and retrying lower rungs against a broken
    webhook would turn one honest failure into three."""
    cluster = FakeCluster(pod_document(uid=1000), admission_error=WEBHOOK_UNREACHABLE)
    with pytest.raises(KubectlError, match="failed calling webhook"):
        attach(talking_to(cluster), "target", max_rung=Rung.FULL)


def test_target_gid_pins_the_group_the_manifest_did_not_state() -> None:
    """``--target-gid`` for the manifest that names a uid and no group.

    Which is the shape that costs the debugger: the seat lands in the debug
    image's group 0 against a target in its own image's group, and
    ``__ptrace_may_access()`` compares the group ids as peers of the user ids.
    The flag is the way to spend one container name instead of two - podbench
    measures the gid and lands a corrected seat by itself, but only after the
    first one has landed and been probed.
    """
    cluster = FakeCluster(pod_document(uid=1000, non_root=True))
    session = attach(talking_to(cluster), "target", target_gid=1000)

    assert session.rung is Rung.DEGRADED
    assert session.gid == 1000
    assert security_contexts(cluster)[0] == {
        "capabilities": {"drop": ["ALL"]},
        "runAsNonRoot": True,
        "privileged": False,
        "allowPrivilegeEscalation": False,
        "runAsUser": 1000,
        "runAsGroup": 1000,
    }


MISMATCHED = capreport_payload(
    verdict="none",
    exit_code=20,
    blocker="gid-mismatch",
    cap_sys_ptrace=False,
    self_uid=1000,
    self_gid=0,
    target_uid=1000,
    target_gid=1000,
    target_attach_ok=False,
    proc_reads={"root": False, "maps": False, "environ": False},
)
"""The seat #103 was reported for: the uid mirrors, the group does not.

Measured on p47-blueapi-0 (2026-08-19), whose manifest sets ``runAsUser: 1000``
and no ``runAsGroup`` while the process runs at gid 1000 out of its image's own
``ubuntu`` user. The seat lands at 1000:0 and reads nothing."""


def test_a_seat_in_the_wrong_group_is_replaced_by_a_corrected_one() -> None:
    """The whole of #103's remedy, and it costs the second name deliberately.

    The manifest states a uid and no group, so nothing laptop-side knows the
    target's gid: only ``/proc/<pid>/status`` does, and only a landed seat can
    read it. The securityContext of an ephemeral container is fixed for its
    lifetime, so acting on that measurement means a new container - which
    podbench lands itself, because leaving it to the user means printing a
    warning nobody reads and a seat that cannot trace.
    """
    cluster = FakeCluster(pod_document(uid=1000, non_root=True), capreport=MISMATCHED)
    session = attach(talking_to(cluster), "target")

    assert session.seat.container == "podbench-2", "the corrected seat, not the first"
    assert session.gid == 1000
    assert [context.get("runAsGroup") for context in security_contexts(cluster)] == [
        None,
        1000,
    ]
    warning = next(w for w in session.warnings if "corrected seat" in w)
    assert "podbench-1" in warning, "which name was spent"
    assert "1000:0" in warning and "1000:1000" in warning
    assert "--no-correct-ids" in warning, "every remedy names its flag"
    assert "--target-gid" in warning


def test_the_correction_is_made_once_and_never_recurses() -> None:
    """One extra name, whatever the second seat measures.

    The fake answers every probe with the same mismatch, so a correction that
    corrected its own correction would spend names until the container list was
    refused. The recursion carries ``correct_ids=False`` for exactly that.
    """
    cluster = FakeCluster(pod_document(uid=1000, non_root=True), capreport=MISMATCHED)
    session = attach(talking_to(cluster), "target")

    assert len(cluster.added) == 2
    assert session.seat.container == "podbench-2"
    assert sum("corrected seat" in w for w in session.warnings) == 1


def test_a_later_attach_finds_the_corrected_seat_instead_of_landing_another() -> None:
    """The bound that matters, and it is across invocations rather than within one.

    The first seat is still *running* - an ephemeral container cannot be stopped
    - so every later attach reconnects to it, measures the same mismatch and
    wants the same corrected ids. Without :func:`running_seat`'s ``ids`` filter
    that is one permanent name per attach, for ever.
    """
    cluster = FakeCluster(pod_document(uid=1000, non_root=True), capreport=MISMATCHED)
    attach(talking_to(cluster), "target")
    assert len(cluster.added) == 2

    again = attach(talking_to(cluster), "target")
    assert len(cluster.added) == 2, "the corrected seat was found, not landed again"
    assert again.seat.container == "podbench-2"
    assert again.reused


def test_no_correct_ids_leaves_the_first_seat_alone() -> None:
    """The opt-out, for the user who would rather keep the name than the group."""
    cluster = FakeCluster(pod_document(uid=1000, non_root=True), capreport=MISMATCHED)
    session = attach(talking_to(cluster), "target", correct_ids=False)

    assert len(cluster.added) == 1
    assert session.seat.container == "podbench-1"
    assert not any("corrected seat" in w for w in session.warnings)
    # The reason is still stated - the seat reported the blocker and the report
    # prints it - so the user is told what they are keeping.
    assert session.report is not None
    assert session.report.blocker is Blocker.GID_MISMATCH


def test_a_gid_mismatched_seat_is_not_labelled_the_degraded_rung() -> None:
    """The label and the verdict on one report have to be the same seat.

    Measured on p47-blueapi-0 (2026-08-21): a 1000:0 seat against a 1000:1000
    target read ``degraded`` in the RUNG column while the verdict beside it said
    "launch-only ... no read-only inspection". ``Rung.DEGRADED`` is grounded in
    what ``__ptrace_may_access()`` checks, and that call compares ``gid``,
    ``egid`` and ``sgid`` as peers of the user ids - which is the entire reason
    podbench lands a gid-corrected seat - so a rung that read the uid alone
    claimed a kernel check the kernel had already failed. The bottom rung is
    what this seat is, and the ``GID_MISMATCH`` arm says why in the same words.
    """
    cluster = FakeCluster(pod_document(uid=1000, non_root=True), capreport=MISMATCHED)
    session = attach(talking_to(cluster), "target", correct_ids=False)

    assert session.report is not None
    assert session.report.blocker is Blocker.GID_MISMATCH
    assert session.rung is Rung.SEAT
    # And the walk is not what changed: the degraded rung is still the one that
    # landed. The two answers differ because they are answers to two questions.
    assert landed_rung(session) is Rung.DEGRADED
    assert "rung        seat - uid 1000, gid 0" in format_session(session)


def test_a_listing_labels_a_gid_mismatched_seat_by_what_it_can_do() -> None:
    """The same seat, at the two verbs that never exec anything.

    ``list`` and ``status`` read the rung out of the seat's own start-up report
    (issue #99), and that report stores the four numbers rather than a label
    precisely so the launcher may change its mind about them - which is what
    happened here.
    """
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[{"name": "podbench-1", "securityContext": {"runAsUser": 1000}}],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    reports = recover_seat_reports(
        talking_to(cluster), PodRef("demo", "target"), seats(cluster.pod)
    )

    report = reports["podbench-1"]
    assert (report.uid, report.gid) == (1000, 0)
    assert (report.target_uid, report.target_gid) == (1000, 1000)
    assert report.rung is Rung.SEAT


def test_an_unread_target_gid_is_not_a_rung_either() -> None:
    """``--no-probe`` against the manifest shape that hides the group.

    ``runAsUser`` with no ``runAsGroup`` is what a hardened workload declares,
    so the target's group lives only in its own ``/proc`` and nothing
    laptop-side has it. The uids matching is half of the comparison, and half a
    comparison is not a rung: the report names the rung that was *asked* for and
    says which of the two numbers it never read.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    session = attach(talking_to(cluster), "target", probe=False)

    assert not session.rung_measured
    assert "the target unread" in format_session(session)


def test_an_unread_target_uid_is_not_a_rung_at_all() -> None:
    """The same rule, one number earlier, on the manifest shape argus is made of.

    A pod that states no ``runAsUser`` leaves the launcher with no target uid
    to compare against, so the credential match the rung below full *is* was
    never made at all - which is weaker evidence than the unread gid above, not
    stronger. ``measured_rung`` names the floor there for a caller with a
    second source to fall back on, and the launcher is that source: it reports
    ``seat``, measured, for a root seat beside a root target that the very next
    capreport calls ``degraded``. One seat, two labels, which is issue #94.

    ``ssh-config`` builds its session through the same helper on the same
    shape, so this holds it for both verbs.
    """
    session = attach(talking_to(argus_shaped_pod()), "target", probe=False)

    assert not session.rung_measured
    assert "the target unread" in format_session(session)
    # And the probe still settles it: the capreport reads the target's own
    # `/proc/<pid>/status`, which is where an unstated uid actually lives.
    probed = attach(talking_to(argus_shaped_pod()), "target")
    assert probed.rung_measured
    assert probed.rung is Rung.DEGRADED


def test_a_capability_is_measured_without_any_target_at_all() -> None:
    """The full rung is exempt from the credential check, so it needs no target.

    Guarding the unknown target uid must not take this with it: an effective
    ``CAP_SYS_PTRACE`` is the whole of the full rung and is compared against
    nothing (report 3.10).
    """
    cluster = FakeCluster(
        pod_document(
            ephemeral=[
                {
                    "name": "podbench-1",
                    "securityContext": {
                        "runAsUser": 0,
                        "capabilities": {"add": ["SYS_PTRACE"]},
                    },
                }
            ],
            ephemeral_statuses=[running_status("podbench-1")],
        ),
        login_user="root",
    )
    session = attach(talking_to(cluster), "target", probe=False)

    assert session.rung_measured
    assert session.rung is Rung.FULL


def test_a_pinned_gid_is_not_overridden_by_the_measurement() -> None:
    """``--target-gid`` is an instruction, not a hint.

    A launcher that quietly re-authored a seat the user had pinned would be the
    ``--max-rung`` mistake again: the flag exists for the cluster or the target
    podbench reads wrongly, so the measurement disagreeing with it is the
    expected case rather than the surprising one.
    """
    cluster = FakeCluster(pod_document(uid=1000, non_root=True), capreport=MISMATCHED)
    session = attach(talking_to(cluster), "target", target_gid=0)

    assert len(cluster.added) == 1
    assert security_contexts(cluster)[0]["runAsGroup"] == 0
    assert not any("corrected seat" in w for w in session.warnings)


def test_a_seat_too_old_to_report_its_gid_is_not_corrected() -> None:
    """The versioning rule the capreport JSON shape carries.

    ``self_gid`` was added after the shape was published, so an older image
    sends a payload without it. ``None`` there means "this seat cannot answer",
    which is not "gid 0" - reading it as 0 would spend a permanent container
    name on a seat that may well be right already.
    """
    old = capreport_payload(self_uid=1000, target_uid=1000)
    del old["self_gid"]
    del old["target_gid"]
    cluster = FakeCluster(pod_document(uid=1000, non_root=True), capreport=old)
    session = attach(talking_to(cluster), "target")

    assert len(cluster.added) == 1
    assert session.report is not None
    assert session.report.self_gid is None
    assert "ids         seat 1000:?, target 1000:?" in format_session(session)


def test_the_report_prints_both_ids_for_both_sides() -> None:
    """The line that made #103 invisible for a year.

    It said ``uids  seat 1000, target 1000`` over a seat that could not read a
    single ptrace-gated path, because the half that differed was not printed.
    """
    cluster = FakeCluster(pod_document(uid=1000, non_root=True), capreport=MISMATCHED)
    session = attach(talking_to(cluster), "target", correct_ids=False)

    assert "ids         seat 1000:0, target 1000:1000" in format_session(session)


def test_the_default_seat_keeps_the_targets_group() -> None:
    cluster = FakeCluster(pod_document(uid=1000, non_root=True))
    attach(talking_to(cluster), "target")
    assert "runAsGroup" not in security_contexts(cluster)[0]


def test_reconnecting_cannot_change_the_seats_group_so_it_is_not_reused() -> None:
    """An ephemeral container's group is fixed, so the running seat is refused.

    The precedent is ``--max-rung``: a flag that cannot be honoured by
    reconnecting must not be honoured by pretending. Here the currency is the
    same and the reason is the kernel's - a seat pinned to another group can log
    in and cannot trace - so the warning names the seat, both id pairs, and what
    the new container costs.
    """
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
    session = attach(talking_to(cluster), "target", target_gid=1000)
    assert not session.reused
    warning = next(w for w in session.warnings if "podbench-1 is running" in w)
    assert "1000:?" in warning, "what the running seat is pinned to"
    assert "1000:1000" in warning, "and what was asked for"
    assert session.gid == 1000


# -- whose seat is this (issue #113) ----------------------------------------


def as_user(cluster: FakeCluster, who: str | None) -> Kubectl:
    """A launcher run by ``who``, against the same cluster.

    A fresh :class:`Kubectl` per identity, because the identity is cached on the
    instance - which is what a second person running podbench is: a second
    process.
    """
    cluster.whoami = who
    return Kubectl("demo", runner=cluster)


def test_a_seat_records_the_identity_that_landed_it() -> None:
    """The stamp goes in the container's env, which is where it has to be.

    A name is burnt when the container exits and a securityContext cannot be
    corrected in place, so the two things already on a seat that a later reader
    can key on are both unusable for this. The ``env`` is neither: the API
    server keeps the spec entry for the pod's lifetime and hands it back
    verbatim.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    attach(as_user(cluster, "alice"), "target")

    env = {entry["name"]: entry["value"] for entry in cluster.added[0]["env"]}
    assert env["PODBENCH_OWNER"] == "alice"
    assert seats(cluster.pod)[0].owner == "alice"


def test_two_identities_on_one_pod_get_one_seat_each() -> None:
    """The acceptance for #113, and the case the beamline already has.

    The second person's seat cannot be the first person's: an ephemeral
    container's ``authorized_keys`` is written from the env it started with and
    cannot be added to from the launcher, so a reconnect would hand out a stanza
    whose only outcome is `Permission denied (publickey)`.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    first = attach(as_user(cluster, "alice"), "target")
    second = attach(as_user(cluster, "bob"), "target")

    assert first.seat.container == "podbench-1"
    assert second.seat.container == "podbench-2"
    assert not second.reused
    assert second.owner == "bob"
    warning = next(w for w in second.warnings if "podbench-1" in w)
    assert "alice" in warning, "and it names who has it"


def test_each_identity_gets_its_own_seat_back() -> None:
    """The other half: owning a seat has to be worth something.

    bob is the discriminating case, because ephemeral containers are listed in
    the order they were appended and alice's is always the first one reached.
    Without the owner in the filter bob's second attach reconnects into alice's
    seat - a container his key cannot log into - and his own is left running
    beside it.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    attach(as_user(cluster, "alice"), "target")
    attach(as_user(cluster, "bob"), "target")

    hers = attach(as_user(cluster, "alice"), "target")
    his = attach(as_user(cluster, "bob"), "target")

    assert len(cluster.added) == 2, "no third seat was landed"
    assert (hers.seat.container, hers.reused) == ("podbench-1", True)
    assert (his.seat.container, his.reused) == ("podbench-2", True)


def test_a_seat_with_no_owner_is_reported_as_unknown_and_not_claimed() -> None:
    """Every seat that existed before the stamp did, including six at DLS.

    Reused, because refusing them would spend a permanent container name on
    each - but never attributed to whoever is reading, which is the same rule
    the memory row and the rung label are held to.
    """
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[{"name": "podbench-1", "securityContext": {"runAsUser": 1000}}],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    session = attach(as_user(cluster, "alice"), "target")

    assert session.reused
    assert session.owner is None
    assert len(cluster.added) == 0
    report = format_session(session)
    assert "owner       unknown" in report
    assert "alice" not in report


def test_a_cluster_that_will_not_name_its_user_stamps_nothing() -> None:
    """``auth whoami`` is a 1.28 resource and an RBAC grant of its own.

    A launcher that could not ask must land a seat that says so, rather than
    inventing a local name: two people on one workstation share ``$USER``, and
    one person on two laptops does not.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    session = attach(as_user(cluster, None), "target")

    assert "env" not in cluster.added[0] or not any(
        entry["name"] == "PODBENCH_OWNER" for entry in cluster.added[0]["env"]
    )
    assert session.owner is None
    assert "kubectl auth whoami" in format_session(session)


def test_a_nameless_launcher_still_reuses_the_seat_it_finds() -> None:
    """Unknown is not a verdict, in either direction.

    Declining on one missing name would make a cluster without the resource pay
    a permanent container name on every attach, for ever.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    attach(as_user(cluster, "alice"), "target")
    session = attach(as_user(cluster, None), "target")

    assert session.reused
    assert len(cluster.added) == 1
    assert session.owner == "alice", "and the report says whose it is"


def test_the_listing_says_whose_each_seat_is(tmp_path: Path) -> None:
    """What `podbench list` could not answer at all before the stamp."""
    document = pod_document(
        uid=1000,
        ephemeral=[
            {
                "name": "podbench-1",
                "securityContext": {"runAsUser": 1000},
                "env": [{"name": "PODBENCH_OWNER", "value": "alice"}],
            },
            {"name": "podbench-2", "securityContext": {"runAsUser": 1000}},
        ],
        ephemeral_statuses=[running_status("podbench-1"), running_status("podbench-2")],
    )
    text = format_seats(PodRef("demo", "target"), seats(document), directory=tmp_path)

    assert "owner     alice" in text
    assert "owner     unknown" in text


def test_ssh_config_writes_a_stanza_for_the_callers_own_seat(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#117 selected the right seat of one owner; this is the other axis.

    The stanza names *this* kubeconfig's ``IdentityFile``, so a colleague's seat
    is the one seat it cannot be written for - and the failure arrives as
    `Permission denied (publickey)` with nothing to read it against.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    attach(as_user(cluster, "alice"), "target")
    attach(as_user(cluster, "bob"), "target")

    # bob, because alice's seat is the first one listed and would be picked by
    # a selection that read nothing at all.
    cluster.whoami = "bob"
    code = main(
        [
            "ssh-config",
            "target",
            "-n",
            "demo",
            "--identity",
            identity(tmp_path),
            "--print-config",
        ],
        runner=cluster,
    )

    assert code == 0
    printed = capsys.readouterr().out
    assert "demo/target[podbench-2]" in printed
    assert "demo/target[podbench-1]" not in printed


def test_two_owners_are_not_read_as_a_gid_correction() -> None:
    """The signature #103 leaves and two people leave by accident.

    alice's uncorrected seat beside bob's corrected one has the same shape as
    one seat superseding another. Called a supersession, it would tell alice her
    own seat had been replaced by a container she cannot log into, and send her
    next attach off to land a third.
    """
    hers = SeatInfo(
        "podbench-1",
        Rung.DEGRADED,
        "running",
        "",
        uid=1000,
        target="app",
        owner="alice",
    )
    his = SeatInfo(
        "podbench-2",
        Rung.DEGRADED,
        "running",
        "",
        uid=1000,
        gid=1000,
        target="app",
        owner="bob",
    )

    assert superseded_seats([hers, his]) == {}
    reached = running_seat(_pod_of([hers, his]), owner="alice")
    assert reached is not None and reached.name == "podbench-1"


def _pod_of(present: Sequence[SeatInfo]) -> dict[str, Any]:
    """A pod document carrying exactly these seats, live."""
    return pod_document(
        uid=1000,
        ephemeral=[
            {
                "name": seat.name,
                "securityContext": {
                    key: value
                    for key, value in (
                        ("runAsUser", seat.uid),
                        ("runAsGroup", seat.gid),
                    )
                    if value is not None
                },
                "targetContainerName": seat.target,
                "env": (
                    []
                    if seat.owner is None
                    else [{"name": "PODBENCH_OWNER", "value": seat.owner}]
                ),
            }
            for seat in present
        ],
        ephemeral_statuses=[running_status(seat.name) for seat in present],
    )


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


# -- the vscode verb ---------------------------------------------------------


def attach_argv(tmp_path: Path, *extra: str) -> list[str]:
    return _argv("attach", tmp_path, *extra)


def vscode_argv(tmp_path: Path, *extra: str) -> list[str]:
    """``podbench vscode``, which is ``attach`` plus what an editor costs.

    Spelled through the same helper because the two verbs take the same options
    for the same seat: a test that drifts between them would be measuring the
    argv rather than the behaviour.
    """
    return _argv("vscode", tmp_path, *extra)


def _argv(verb: str, tmp_path: Path, *extra: str) -> list[str]:
    return [
        verb,
        "pod/target",
        "-n",
        "demo",
        "--identity",
        identity(tmp_path),
        "--config-dir",
        str(tmp_path / "cfg"),
        *extra,
    ]


def test_open_configures_the_seats_home_and_opens_that(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The folder is the seat's own home, never ``/``: a walk from there goes
    through ``/proc/<pid>/root`` into every other container in the pod and OOMs
    a seat that cannot be restarted."""
    cluster = FakeCluster(pod_document(uid=1000))
    code = main(
        # `/root` is the full rung's home, and that rung is now asked for
        # rather than assumed: the target's uid implies the one below it.
        vscode_argv(tmp_path, "--max-rung", "full"),
        runner=cluster,
        which=lambda name: f"/usr/bin/{name}",
    )
    assert code == 0

    settings = json.loads(cluster.seat_files["/root/.vscode/settings.json"])
    assert settings["files.watcherExclude"]["**/proc/**"] is True
    assert "/root/.vscode/launch.json" in cluster.seat_files
    editor = [call for call in cluster.calls if call[0] == "/usr/bin/code"]
    assert editor[-1] == (
        "/usr/bin/code",
        "--remote",
        "ssh-remote+podbench-demo-target-1",
        "/root",
    )
    # The flavour's extensions, installed in the remote window and nowhere else.
    assert [call[-1] for call in editor if "--install-extension" in call] == [
        "ms-python.python",
        "ms-python.debugpy",
    ]
    assert "open /root over Remote-SSH" in capsys.readouterr().out


def test_open_separates_what_it_did_from_what_to_do_next(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The block was a wall because both were in it, interleaved.

    Everything under `editor` is the past tense and every line carries a tick;
    everything under `next` is a thing the reader might act on, including the
    two lines that exist to be pasted. They used to arrive in the other order,
    with the offers above thirteen sentences of progress.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    assert main(vscode_argv(tmp_path), runner=cluster, which=lambda n: f"/bin/{n}") == 0

    lines = capsys.readouterr().out.splitlines()
    steps = lines[lines.index("editor") + 1 : lines.index("next")]
    offers = lines[lines.index("next") + 1 :]

    assert [line for line in steps if line.strip()], "the checklist is not empty"
    for line in (line.strip() for line in steps if line.strip()):
        # A step, or a continuation of one, or the seat's own relayed stderr -
        # never one of the offers, which is what the split is for.
        assert not line.startswith(("add this to", "or let podbench", "reconnect"))
    assert any(line.lstrip().startswith("[ok]") for line in steps)

    flowed = " ".join(" ".join(offers).split())
    assert "add this to ~/.ssh/config once:" in flowed
    assert "or let podbench check and add it:  podbench doctor --fix" in " ".join(
        offers
    )
    assert "reconnect later with:  ssh podbench-demo-target-1" in " ".join(offers)
    # `then:` points forward, and by here the window has already been opened.
    assert "then:  ssh" not in flowed


def test_the_offers_survive_an_editor_step_that_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The seat is real whether or not an editor could be pointed at it.

    A run that ends at "ssh does not reach the seat" still has a stanza, an
    alias and the `kubectl exec` helpers, so withholding how to use them from
    that reader in particular is backwards.
    """
    cluster = FakeCluster(
        pod_document(uid=1000),
        ssh_probe_rc=255,
        ssh_probe_err="/tmp/podbench-home/.podbench/sshd_config: No such file\n",
    )
    assert main(vscode_argv(tmp_path), runner=cluster, which=lambda n: f"/bin/{n}") == 2

    captured = capsys.readouterr()
    assert "next" in captured.out
    assert "add this to ~/.ssh/config once:" in captured.out
    # ...but not a remedy for a window that was never opened.
    assert "could not establish connection" not in captured.out


def test_open_refuses_a_seat_ssh_cannot_reach_and_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The seat lands, the stanza is written, and VS Code is not started.

    ``code --remote`` returns as soon as a window has the argv, so an alias
    that does not work costs a vscode-server download and is diagnosed in a log
    the user has to know to open. One ``ssh <alias> true`` beforehand settles
    it, and the failure it prints is ssh's own.
    """
    cluster = FakeCluster(
        pod_document(uid=1000),
        ssh_probe_rc=255,
        ssh_probe_err=(
            "/tmp/podbench-home/.podbench/sshd_config: No such file or directory\n"
            "kex_exchange_identification: Connection closed by remote host\n"
        ),
    )

    code = main(
        vscode_argv(tmp_path),
        runner=cluster,
        which=lambda name: f"/usr/bin/{name}",
    )

    assert code == 2
    captured = capsys.readouterr()
    assert "does not reach the seat" in captured.err
    assert "sshd_config: No such file or directory" in captured.err
    assert not [call for call in cluster.calls if call[0] == "/usr/bin/code"], (
        "the editor must not be started, nor asked to install anything"
    )
    assert cluster.seat_files == {}, "nothing is written into a seat ssh cannot reach"
    # The seat is still real, and the block above still says how to use it.
    assert "ssh config written to" in captured.out


def test_open_without_the_vs_code_cli_never_burns_a_container_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An ephemeral container's name is permanent, so a run that was always
    going to end at "no `code`" must not spend one on the way."""
    cluster = FakeCluster(pod_document(uid=1000))
    code = main(vscode_argv(tmp_path), runner=cluster, which=lambda _: None)

    assert code == 2
    assert cluster.added == []
    assert "Shell Command" in capsys.readouterr().err


def test_vscode_has_no_print_config_to_open_a_host_ssh_cannot_find(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--print-config` writes no stanza and `code --remote ssh-remote+<alias>`
    resolves the alias through ssh, so the pair could only ever land a seat and
    then fail on a host that exists nowhere. It used to be a refusal the verb
    raised; now it is a flag the verb does not have, which is the same answer
    given by the parser instead of after a round trip."""
    cluster = FakeCluster(pod_document(uid=1000))
    code = main(
        vscode_argv(tmp_path, "--print-config"),
        runner=cluster,
        which=lambda name: f"/usr/bin/{name}",
    )

    assert code == 2
    assert cluster.added == []
    assert "--print-config" in capsys.readouterr().err


def test_without_open_no_editor_is_touched(tmp_path: Path) -> None:
    cluster = FakeCluster(pod_document(uid=1000))
    assert main(attach_argv(tmp_path), runner=cluster, which=lambda _: None) == 0
    assert cluster.seat_files == {}


def test_open_does_not_ask_for_the_verb_it_has_just_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A step the reader has already had done for them reads as a step that did
    not happen - and `--open` reports what debug-config actually got, which the
    static line cannot."""
    cluster = FakeCluster(pod_document(uid=1000))
    assert (
        main(
            vscode_argv(tmp_path),
            runner=cluster,
            which=lambda name: f"/usr/bin/{name}",
        )
        == 0
    )
    captured = capsys.readouterr()

    assert "run `podbench debug-config` in the seat" not in captured.out
    # The rest of the block is what the reader needs either way.
    assert "ssh config written to" in captured.out


def test_without_open_the_seat_is_still_told_how_to_get_a_launch_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster = FakeCluster(pod_document(uid=1000))
    assert main(attach_argv(tmp_path), runner=cluster, which=lambda _: None) == 0

    assert "run `podbench debug-config` in the seat" in capsys.readouterr().out


def test_attach_carries_no_flag_that_mutates_the_workload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`attach` adds a container to the pod and touches the workload not at all,
    which is the promise the mode table makes for it. Provisioning writes ~15 MB
    into the target and ptraces it, so it lives on the verb whose name asks for
    an editor and everything an editor costs."""
    cluster = FakeCluster(pod_document(uid=1000))
    code = main(
        attach_argv(tmp_path, "--provision"),
        runner=cluster,
        which=lambda name: f"/usr/bin/{name}",
    )

    assert code == 2
    # Refused by the parser, before the attach: an ephemeral container's name is
    # permanent, and this run was never going to reach a config author.
    assert cluster.added == []
    assert "--provision" in capsys.readouterr().err


def test_vscode_provisions_a_target_that_says_it_needs_it(tmp_path: Path) -> None:
    """The seat is the only thing that can see the target, and it names the flag
    in its own refusal. So the answer is already in hand when the refusal
    arrives, and the round trip it used to cost was podbench asking the user to
    retype a fact it had just measured."""
    cluster = FakeCluster(pod_document(uid=1000))
    cluster.unprovisioned = True
    code = main(
        vscode_argv(tmp_path),
        runner=cluster,
        which=lambda name: f"/usr/bin/{name}",
    )
    assert code == 0

    asked = [call for call in cluster.calls if "debug-config" in call]
    assert len(asked) == 2, "the refusal is answered once, not looped on"
    assert "--provision" not in asked[0]
    assert "--provision" in asked[1]


def test_vscode_provisions_nothing_a_target_did_not_ask_for(tmp_path: Path) -> None:
    """A target that already has a debugger is not a target to install one into.
    `--provision` was never "always"; the consent the verb carries is spent only
    where the seat said it is the blocker."""
    cluster = FakeCluster(pod_document(uid=1000))
    assert (
        main(
            vscode_argv(tmp_path),
            runner=cluster,
            which=lambda name: f"/usr/bin/{name}",
        )
        == 0
    )

    assert not any("--provision" in call for call in cluster.calls)


def test_no_provision_leaves_the_target_exactly_as_it_is(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #45's decision, kept as an opt-out: ~15 MB into the workload's
    writable layer is a mutation, and a user who declines it gets the offer they
    used to get instead of the act."""
    cluster = FakeCluster(pod_document(uid=1000))
    cluster.unprovisioned = True
    assert (
        main(
            vscode_argv(tmp_path, "--no-provision"),
            runner=cluster,
            which=lambda name: f"/usr/bin/{name}",
        )
        == 0
    )

    assert not any("--provision" in call for call in cluster.calls)
    assert "--provision" in " ".join(capsys.readouterr().out.split())


def test_open_follows_the_home_volume_rather_than_assuming_root(
    tmp_path: Path,
) -> None:
    """A pod carrying `podbench-home` moves the seat's home, and the folder has
    to move with it: it comes from the same `SshdLayout` the ProxyCommand does,
    so the two cannot disagree about where the seat lives."""
    cluster = FakeCluster(identity_pod())
    code = main(
        vscode_argv(tmp_path),
        runner=cluster,
        which=lambda name: f"/usr/bin/{name}",
    )
    assert code == 0

    assert set(cluster.seat_files) == {
        "/home/podbench/.vscode/settings.json",
        "/home/podbench/.vscode/launch.json",
        "/home/podbench/.vscode/extensions.json",
    }
    opened = [
        call
        for call in cluster.calls
        if call[0] == "/usr/bin/code" and "--install-extension" not in call
    ]
    assert opened[-1][-1] == "/home/podbench"


def test_open_on_a_seat_with_no_stanza_says_so_rather_than_opening_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Remote-SSH needs a host, and a degraded seat got no stanza to name one.
    The block above has already named the mechanism, so this only has to say
    that `--open` is the part that cannot go on - the exec helpers still work."""
    cluster = FakeCluster(
        pod_document(uid=1000), psa_denies_ptrace=True, login_user=None
    )
    code = main(
        vscode_argv(tmp_path),
        runner=cluster,
        which=lambda name: f"/usr/bin/{name}",
    )

    assert code == 2
    assert [call for call in cluster.calls if call[0] == "/usr/bin/code"] == []
    captured = capsys.readouterr()
    assert "no ssh alias" in captured.err
    # The seat itself landed and was reported: only --open is refused.
    assert "no ssh config was written" in captured.out


def test_open_stops_at_a_seat_file_it_cannot_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused write is the layer's own sentence, not `kubectl exec`'s argv -
    and it ends the run, because a window opened without the exclude list is the
    walk that OOMs a seat which cannot be restarted."""
    cluster = FakeCluster(pod_document(uid=1000))
    cluster.unwritable.add("/root/.vscode/settings.json")
    code = main(
        vscode_argv(tmp_path, "--max-rung", "full"),
        runner=cluster,
        which=lambda name: f"/usr/bin/{name}",
    )

    assert code == 2
    assert [call for call in cluster.calls if call[0] == "/usr/bin/code"] == []
    assert "cannot write /root/.vscode/settings.json" in capsys.readouterr().err


def test_open_leaves_the_probe_deadline_as_the_last_thing_on_screen(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The report carrying the numbers is several blocks up by the time the
    window opens, and the reader is about to stop reading the terminal. The
    readiness half is the one with no trace afterwards."""
    cluster = FakeCluster(pod_document(uid=1000, probes=PROBES))
    code = main(
        vscode_argv(tmp_path),
        runner=cluster,
        which=lambda name: f"/usr/bin/{name}",
    )
    assert code == 0

    out = capsys.readouterr().out
    assert "before the first breakpoint" in out
    assert out.index("over Remote-SSH") < out.index("before the first breakpoint")
    # A pointer, not a second copy: the deadlines stay in the one report that
    # computes them, or there are two things to keep true.
    assert out.count("readiness at 11-16s") == 1


def test_an_unprobed_target_gets_no_reminder_it_cannot_act_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty means "no probes", not "not looked at" — so a pause here costs
    nothing and a deadline would be an invented one."""
    cluster = FakeCluster(pod_document(uid=1000))
    assert (
        main(
            vscode_argv(tmp_path),
            runner=cluster,
            which=lambda name: f"/usr/bin/{name}",
        )
        == 0
    )
    assert "before the first breakpoint" not in capsys.readouterr().out


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
    kubectl = talking_to(cluster)
    session = attach(kubectl, "target")

    seat = emit_ssh_config(
        kubectl,
        session,
        identity=identity(tmp_path),
        config_dir=str(tmp_path / "cfg"),
        user="somebody-else",
    )

    assert seat.alias == "podbench-demo-target-1"
    assert seat.path is not None
    stanza = seat.path.read_text()
    assert "User somebody-else" in stanza
    assert "ProxyCommand" in stanza
    assert f"then:  ssh {seat.alias}" in seat.note
    assert seat.path == ssh_config_path(
        tmp_path / "cfg", PodRef("demo", "target"), "podbench-1"
    )


def test_a_side_loaded_image_is_not_asked_for_from_a_registry() -> None:
    """The e2e regression, pinned. `kind load` puts an image on the node without
    pulling it, so `Always` — the one policy that *requires* a registry — turns
    a working cluster into `pull access denied, repository does not exist`.

    Asserted through `attach` rather than on the spec author alone, because the
    default is the whole point: nothing on this path may reach for `Always` on
    the strength of the tag looking like a development one.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    attach(talking_to(cluster), "target", image="docker.io/library/podbench:e2e")

    assert cluster.added[0]["imagePullPolicy"] == "IfNotPresent"


def test_the_seat_is_asked_its_version_rather_than_guessed_about() -> None:
    """No symptom is the problem: the seat starts, and is simply older.

    A moving tag used to be the only signal, and it is a guess in both
    directions — it cannot confirm a skew and cannot rule one out. The seat is
    on the other end of an open exec channel, so it is asked, and where it
    answers the guess is not printed beside the answer.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    session = attach(talking_to(cluster), "target", image="ghcr.io/x/podbench:main")

    assert session.seat_version == __version__
    assert not any("a tag that moves" in note for note in session.warnings)
    assert VERSION_SKEW_WARNING not in session.warnings


def test_a_seat_that_will_not_say_falls_back_to_the_tag_guess() -> None:
    """The guess survives for exactly the case that has no measurement."""
    cluster = FakeCluster(pod_document(uid=1000), seat_version=None)
    session = attach(talking_to(cluster), "target", image="ghcr.io/x/podbench:main")

    assert session.seat_version is None
    assert any("a tag that moves" in note for note in session.warnings)
    assert any("--pull always" in note for note in session.warnings)


def test_asking_for_always_says_nothing_about_staleness() -> None:
    """The warning is about the policy that cannot notice, not about the tag."""
    cluster = FakeCluster(pod_document(uid=1000), seat_version=None)
    session = attach(
        talking_to(cluster),
        "target",
        image="ghcr.io/x/podbench:main",
        pull_policy="Always",
    )

    assert cluster.added[0]["imagePullPolicy"] == "Always"
    assert not any("a tag that moves" in note for note in session.warnings)


def test_a_matching_seat_version_is_reported_without_a_warning() -> None:
    """The row is printed either way: agreement is a fact worth reading."""
    cluster = FakeCluster(pod_document(uid=1000))
    text = format_session(attach(talking_to(cluster), "target"))

    # Flattened: the row wraps at a narrow terminal, and where the break lands
    # must not decide whether this holds (`console.wrap` collapses runs).
    assert f"version {__version__}, the same build as this launcher" in " ".join(
        text.split()
    )
    assert "different build of podbench" not in text


def test_a_skewed_seat_version_is_one_warning_carrying_no_numbers() -> None:
    """Both numbers on the `version` row, the consequence in one warning line.

    The evidence this exists for: a seat from an image tagged 0.4.0b1 reported
    `0.4.0b2.dev0+g01d9ac8f8.d20260818`, a post-tag build of a dirty tree. The
    launcher and the seat share the agent's argv and capreport's JSON, so that
    is a hazard rather than untidiness — and a fix-test cycle that cannot see it
    burns rounds asking why the fix did not work.
    """
    cluster = FakeCluster(pod_document(uid=1000), seat_version="0.0.1b1")
    session = attach(talking_to(cluster), "target")
    text = format_session(session)

    assert session.warnings.count(VERSION_SKEW_WARNING) == 1
    assert f"version 0.0.1b1 in the seat, {__version__} in this launcher" in " ".join(
        text.split()
    )
    # One leader per warning, which is what makes the block a list rather than
    # an essay: the numbers stay on the row above and are not repeated here.
    leaders = [line for line in text.splitlines() if line.startswith("WARNING")]
    assert len(leaders) == len(session.warnings)
    assert "0.0.1b1" not in VERSION_SKEW_WARNING


def test_a_dirty_build_of_our_own_commit_is_not_a_skew() -> None:
    """The field defect: the warning fired on all eight attaches of the
    2026-08-19 round, and its own remedy could not clear it.

    setuptools_scm marks a dirty tree with a `.d<date>` component in the local
    segment, which says when the wheel was built and nothing about what went
    into it. The seat reported `...+g87be67bc8.d20260819` where this launcher
    reported `...+g87be67bc8` — one commit, two wheels — and an exact string
    comparison called that a skew.
    """
    seat = f"{__version__}.d20260819" if "+" in __version__ else __version__
    cluster = FakeCluster(pod_document(uid=1000), seat_version=seat)
    session = attach(talking_to(cluster), "target")

    assert VERSION_SKEW_WARNING not in session.warnings
    # The row agrees with the warning block, which is the other half of the
    # defect: a `version` line calling this a skew is the same false positive.
    assert "in this launcher" not in " ".join(format_session(session).split())


def test_same_build_reads_the_commit_and_not_the_build_date() -> None:
    """Unit-level, because `__version__` here may be a clean checkout's and
    then the attach above has no `.d<date>` to strip."""
    assert same_build("0.4.0b2.dev24+g87be67bc8.d20260819", "0.4.0b2.dev24+g87be67bc8")
    assert same_build(
        "0.4.0b2.dev24+g87be67bc8.d20260819", "0.4.0b2.dev24+g87be67bc8.d20260820"
    )
    # The case the warning exists for, in both halves of the version.
    assert not same_build("0.4.0b2.dev24+g87be67bc8", "0.4.0b2.dev25+gdeadbeef1")
    assert not same_build("0.4.0b2.dev24+g87be67bc8", "0.4.0b1.dev0+g87be67bc8")
    # No hash on either side leaves the whole string as the only evidence.
    assert same_build("0.4.0", "0.4.0")
    assert not same_build("0.4.0", "0.4.1")
    assert not same_build("0.4.0", "0.4.0b2.dev24+g87be67bc8")


def test_a_seat_that_cannot_be_asked_its_version_still_attaches() -> None:
    """Report 4.2: the seat is already in the pod and cannot be relanded.

    So a diagnostic that did not arrive degrades to unknown. An image predating
    the root `--version` answers with click's usage text and exit 2, which is
    neither a version nor a reason to fail an attach that has landed.
    """
    cluster = FakeCluster(pod_document(uid=1000), seat_version=None)
    session = attach(talking_to(cluster), "target")

    assert landed_rung(session) is Rung.DEGRADED
    assert session.seat_version is None
    assert VERSION_SKEW_WARNING not in session.warnings
    assert UNKNOWN_SEAT_VERSION in format_session(session)


def test_two_seats_on_one_pod_get_two_aliases_and_two_stanzas() -> None:
    """Issue #93. An ephemeral container is never removed, so `--new` leaves the
    old seat *running* — and one alias over both was wrong twice.

    The stanza is one file per name, so the second seat overwrote the first
    rather than joining it; and the stanza sets `ControlMaster auto`, whose
    multiplexing key is the host as typed, so every ssh kept riding the
    connection already open to the old seat. Measured at DLS: `--open` wrote
    launch.json into podbench-2 while VS Code read podbench-1's copy.
    """
    first = default_host_alias(PodRef("demo", "target"), "podbench-1")
    second = default_host_alias(PodRef("demo", "target"), "podbench-2")
    assert first != second
    assert (first, second) == ("podbench-demo-target-1", "podbench-demo-target-2")

    directory = Path("/h/.podbench")
    assert ssh_config_path(directory, PodRef("demo", "target"), "podbench-1") != (
        ssh_config_path(directory, PodRef("demo", "target"), "podbench-2")
    )


def test_a_dev_pods_sole_sidecar_keeps_the_unqualified_name() -> None:
    """A dev pod has exactly one seat, named `podbench`, so there is nothing to
    disambiguate — and qualifying it would rename every existing dev alias and
    orphan the stanza that goes with it."""
    assert default_host_alias(PodRef("demo", "dev"), "podbench") == "podbench-demo-dev"


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
    assert resolve_pod(talking_to(cluster), "api", interactive=False) == "api"
    assert not any("pods" in call for call in cluster.calls)


def test_the_pod_slash_name_form_still_resolves(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = namespace_of("api-7f9", "web-3c1")
    kube = talking_to(cluster)
    assert resolve_pod(kube, "pod/api-7f9", interactive=False) == "api-7f9"
    # And the kind prefix is stripped before the substring search too, so
    # `pod/api` narrows exactly as `api` does.
    assert resolve_pod(kube, "pod/api", interactive=False) == "api-7f9"
    assert "api-7f9" in capsys.readouterr().err


def test_one_substring_hit_resolves_and_says_what_it_resolved_to(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = namespace_of("web-6c9d7f4b8b-hq2vn", "api-7f9")
    name = resolve_pod(talking_to(cluster), "hq2", interactive=False)
    assert name == "web-6c9d7f4b8b-hq2vn"
    # On stderr, so a redirected stdout still carries only the report. One
    # readouterr() for both halves: the first call drains the capture, so a
    # second one reads a fresh empty buffer and asserts nothing.
    captured = capsys.readouterr()
    assert "web-6c9d7f4b8b-hq2vn" in captured.err
    assert captured.out == ""


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
        talking_to(cluster),
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
    kube = talking_to(cluster)
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
        talking_to(cluster), "api", interactive=True, ask=answers("nope", "api", "1")
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
        resolve_pod(talking_to(cluster), "api", interactive=True, ask=answers(""))


def test_no_argument_offers_every_pod_in_the_namespace_not_only_seats() -> None:
    # The difference between this and `podbench list`: list enumerates the pods
    # that already carry a seat, and this one enumerates the pods that could.
    cluster = namespace_of("api-7f9", "web-3c1")
    name = resolve_pod(talking_to(cluster), None, interactive=True, ask=answers("2"))
    assert name == "web-3c1"


def test_no_argument_in_a_one_pod_namespace_resolves_without_asking(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # There is nothing to disambiguate, so there is nothing to ask — but the
    # echo still has to read as English. It used to interpolate the absent
    # query and say "None matched pod only-pod".
    cluster = namespace_of("only-pod")
    assert resolve_pod(talking_to(cluster), None, interactive=False) == "only-pod"
    err = capsys.readouterr().err
    assert "only-pod" in err
    assert "None" not in err


def test_nothing_matches_names_the_namespace_and_shows_what_is_there() -> None:
    cluster = namespace_of("api-7f9", "web-3c1")
    with pytest.raises(LauncherError) as caught:
        resolve_pod(talking_to(cluster), "postgres", interactive=False)
    message = str(caught.value)
    assert "'postgres'" in message
    assert "namespace demo" in message
    assert "api-7f9" in message and "web-3c1" in message


def empty_namespace(
    argv: Sequence[str],
    *,
    stdin: str | None = None,
    capture: bool = True,
    timeout: float | None = None,
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
        resolve_pod(talking_to(cluster), "api", interactive=False)
    message = str(caught.value)
    assert "not a tty" in message
    assert "api-7f9" in message and "api-canary" in message
    assert "name one exactly" in message


def test_no_prompt_refuses_on_a_terminal_too() -> None:
    cluster = namespace_of("api-7f9", "api-canary")
    with pytest.raises(LauncherError, match="--no-prompt was given"):
        resolve_pod(
            talking_to(cluster),
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
    (choice,) = pod_choices(talking_to(cluster), now=NOW)
    assert choice.age == "?"


# -- status, list, resize, namespace ----------------------------------------


def waiting_status(name: str, reason: str = "ContainerCreating") -> dict[str, Any]:
    """A seat the node has taken and not yet started."""
    return {
        "name": name,
        "state": {"waiting": {"reason": reason, "message": "pulling the image"}},
    }


def test_a_waiting_reason_with_no_message_is_not_a_dangling_colon() -> None:
    """`state     PodInitializing:` - a colon introducing nothing.

    The kubelet sends `PodInitializing` and `ContainerCreating` bare; only the
    error reasons carry prose. Formatting every reason as `reason: message`
    printed the punctuation for a message that was never there, which reads to
    a user as one this code dropped on the floor.
    """
    cluster = FakeCluster(
        pod_document(
            ephemeral=[
                {"name": "podbench-1", "securityContext": {"runAsUser": 1000}},
                {"name": "podbench-2", "securityContext": {"runAsUser": 1000}},
            ],
            ephemeral_statuses=[
                {"name": "podbench-1", "state": {"waiting": {"reason": "Pending"}}},
                {
                    "name": "podbench-2",
                    "state": {"waiting": {"reason": "Blank", "message": "   "}},
                },
            ],
        )
    )

    listed = {seat.name: seat for seat in seats(cluster.pod)}

    assert listed["podbench-1"].detail == "Pending"
    # Whitespace is not a message either: `console.wrap` would collapse it and
    # leave the same colon at the end of the line.
    assert listed["podbench-2"].detail == "Blank"


def test_a_waiting_message_is_still_joined_to_its_reason() -> None:
    """The colon earns its place wherever the kubelet did send prose."""
    cluster = starting_cluster()

    listed = {seat.name: seat for seat in seats(cluster.pod)}

    assert listed["podbench-1"].detail == "ContainerCreating: pulling the image"


class FakeTime:
    """A clock that moves only when the code under test sleeps.

    The pod is mutated from ``sleep`` too, so a test says "this is what the
    next poll finds" in the one place the wait can observe a change.
    """

    def __init__(self, on_sleep: Callable[[], None] | None = None) -> None:
        self.now = 0.0
        self.sleeps = 0
        self._on_sleep = on_sleep

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        if self._on_sleep is not None:
            self._on_sleep()


def starting_cluster(reason: str = "ContainerCreating") -> FakeCluster:
    return FakeCluster(
        pod_document(
            ephemeral=[{"name": "podbench-1", "securityContext": {"runAsUser": 1000}}],
            ephemeral_statuses=[waiting_status("podbench-1", reason)],
        )
    )


class WedgedRunner:
    """A ``kubectl`` that never comes back, standing in rather than blocking.

    Issue #118's failure was measured in the field: with a stub ``kubectl`` that
    slept for ever, ``status --timeout 5`` was still running at 75 s, because
    nothing bounded the single call inside the polling loop. What is asserted
    here is the half that lives in this process — that the verb gives the call a
    bound and turns the expiry into a sentence — so this raises what
    :func:`podbench.kubectl.run_subprocess` would raise instead of really
    hanging: a test that proved it by hanging would prove it to nobody.

    An unbounded call is the failure this class exists to catch, so it is an
    assertion rather than a wait. The exemptions are real but none of them is
    reachable from ``status``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], float | None]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        self.calls.append((tuple(argv), timeout))
        if timeout is None:
            raise AssertionError(f"this call would hang for ever: {list(argv)}")
        raise KubectlTimeoutError(argv, timeout)


def test_a_wedged_kubectl_ends_the_verb_instead_of_hanging(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every call `status` makes is bounded, and the expiry is one sentence."""
    runner = WedgedRunner()

    assert (
        main(["status", "target", "-n", "demo", "--timeout", "5"], runner=runner) == 2
    )

    said = capsys.readouterr().err
    assert f"did not answer within {DEFAULT_CALL_TIMEOUT:g}s" in said
    assert "was stopped" in said
    # The argv is the diagnosis: which call wedged, not merely that one did.
    assert "get pod target" in said
    assert [timeout for _, timeout in runner.calls] == [DEFAULT_CALL_TIMEOUT]


def test_a_plain_status_reads_the_pod_once_and_never_sleeps() -> None:
    """The default has to cost exactly what it did before the flag existed."""
    cluster = starting_cluster()
    clock = FakeTime()

    present = wait_for_seats(
        talking_to(cluster), "target", timeout=0.0, clock=clock, sleep=clock.sleep
    )

    assert [seat.phase for seat in present] == ["waiting"]
    assert clock.sleeps == 0


def test_status_can_wait_for_a_seat_that_is_still_starting() -> None:
    """Finding 17.1: 15 of 15 agents typed `--timeout` at `status` and were
    refused, having just needed it for `attach` on a cluster whose image pull
    is slow. Waiting is what it means here."""
    cluster = starting_cluster()

    def lands() -> None:
        statuses = cast(dict[str, Any], cluster.pod["status"])
        statuses["ephemeralContainerStatuses"] = [running_status("podbench-1")]

    clock = FakeTime(on_sleep=lands)

    present = wait_for_seats(
        talking_to(cluster),
        "target",
        timeout=120.0,
        poll_interval=1.0,
        clock=clock,
        sleep=clock.sleep,
    )

    assert [seat.phase for seat in present] == ["running"]
    assert clock.sleeps == 1


def test_a_seat_that_never_starts_is_reported_rather_than_refused() -> None:
    """`status` exists to say what is there, so a passed deadline is not an
    error: `waiting` is the true answer the caller asked for longer to
    improve on."""
    cluster = starting_cluster()
    clock = FakeTime()

    present = wait_for_seats(
        talking_to(cluster),
        "target",
        timeout=3.0,
        poll_interval=1.0,
        clock=clock,
        sleep=clock.sleep,
    )

    assert [seat.phase for seat in present] == ["waiting"]
    assert clock.now >= 3.0


def test_a_kubelet_refusal_is_not_something_to_wait_for() -> None:
    """A container the kubelet refused sits in `waiting` for the pod's lifetime
    (report 3.18), so polling it would spend the whole timeout on a state that
    has already settled."""
    cluster = starting_cluster(CREATE_CONTAINER_CONFIG_ERROR)
    clock = FakeTime()

    present = wait_for_seats(
        talking_to(cluster), "target", timeout=120.0, clock=clock, sleep=clock.sleep
    )

    assert [seat.phase for seat in present] == ["waiting"]
    assert clock.sleeps == 0


def test_status_accepts_the_flag_attach_needed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The papercut itself: the same spelling `attach` takes, on the verb
    reached for next."""
    cluster = FakeCluster(
        pod_document(
            ephemeral=[{"name": "podbench-1", "securityContext": {"runAsUser": 1000}}],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )

    code = main(
        [
            "status",
            "target",
            "-n",
            "demo",
            "--timeout",
            "300",
            "--config-dir",
            str(tmp_path / "cfg"),
        ],
        runner=cluster,
    )

    assert code == 0
    assert "podbench-1" in capsys.readouterr().out


def test_status_lists_every_container_live_or_burnt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
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
    assert (
        main(
            ["status", "target", "-n", "demo", "--config-dir", str(tmp_path / "cfg")],
            runner=cluster,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "podbench-1" in out
    # The measured rung, and `seat` is the honest one here: this seat pins the
    # target's uid and no group, so it runs in the image's group 0 against a
    # target in group 1000 and the credential check denies it (p47-blueapi-0's
    # shape). `rung_of_spec` reads the same securityContext as `degraded`.
    assert "podbench-1   attach  running     seat" in out
    assert "other-sidecar" not in out


def status_of(
    cluster: FakeCluster,
    capsys: pytest.CaptureFixture[str],
    *flags: str,
    config: Path,
) -> str:
    """What ``podbench status`` prints for ``cluster``'s one pod.

    Flattened, because every fact under a seat is wrapped to the terminal and a
    line break inside a verdict must not decide whether an assertion holds.
    """
    assert (
        main(
            ["status", "target", "-n", "demo", "--config-dir", str(config), *flags],
            runner=cluster,
        )
        == 0
    )
    return " ".join(capsys.readouterr().out.split())


def degraded_seat(**capreport: Any) -> FakeCluster:
    """The issue #89 shape: a seat that reads back as the degraded rung.

    Which is what admission left behind, not what the seat can do — at Diamond
    a mutating webhook stripped `capabilities.add` from a root seat, so the
    spec is indistinguishable from the rung podbench falls to. What capreport
    then measures in it is the argument.
    """
    return FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[{"name": "podbench-1", "securityContext": {"runAsUser": 1000}}],
            ephemeral_statuses=[running_status("podbench-1")],
        ),
        capreport=capreport_payload(**capreport),
    )


def test_status_reports_the_probe_rather_than_the_rung_it_landed_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Issue #89: the verdict column was `Rung.description`, so a seat that had
    # just ptraced the target was described as read-only inspection because of
    # the securityContext it reads back with.
    out = status_of(degraded_seat(), capsys, config=tmp_path / "cfg")

    assert "live attach available" in out
    assert "read-only" not in out
    # The per-rung prose this column once borrowed is gone outright: a rung has
    # no description of its own any more, and `attach` cites the seat's own
    # /proc/self/status instead (`podbench.model.describe_credentials`).
    assert "all capabilities dropped" not in out


def test_status_says_not_probed_and_claims_nothing_when_it_measured_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster = degraded_seat()
    out = status_of(cluster, capsys, "--no-probe", config=tmp_path / "cfg")

    assert "verdict not probed" in out
    # Nothing that could be read as a capability, from either direction: the
    # unprobed seat is not claimed to attach and not claimed to be shut out.
    for claim in ("live attach", "read-only", "launch-only", "no access"):
        assert claim not in out
    assert not any("exec" in call for call in cluster.calls), (
        "--no-probe still exec'd into the seat"
    )


def test_status_reports_read_only_only_when_every_gated_read_landed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reads = dict.fromkeys(PTRACE_READ_PATHS, True)
    out = status_of(
        degraded_seat(target_attach_ok=False, proc_reads=reads),
        capsys,
        config=tmp_path / "cfg",
    )
    assert "read-only inspection of the target" in out

    # One gated read gone is not read-only, however the probe worded its own
    # verdict: `ptrace_reads_ok` is all-or-nothing because the claim names all
    # three paths and a partial tick sends someone to a sysroot that will not
    # open.
    partial = status_of(
        degraded_seat(target_attach_ok=False, proc_reads={"root": True}),
        capsys,
        config=tmp_path / "cfg2",
    )
    assert "verdict launch-only" in partial
    assert Verdict.READ_ONLY.summary not in partial


def test_status_does_not_claim_an_attach_that_was_never_attempted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `derive_verdict` answers LIVE_ATTACH when there is no target pid at all -
    # honest as a session banner ("live attach was not tested"), a false
    # positive in a column. The column is recomputed from what was attempted.
    out = status_of(
        degraded_seat(
            target_pid=None, target_attach_ok=None, proc_reads={}, child_attach_ok=True
        ),
        capsys,
        config=tmp_path / "cfg",
    )
    assert "live attach" not in out
    assert "verdict launch-only" in out


def test_status_says_why_a_running_seat_could_not_be_probed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster = degraded_seat()
    # The shape an image older than the launcher gives, and the one an exec
    # RBAC refusal gives: a running seat that answers, but not with JSON.
    cluster.capreport_output = "podbench: unrecognised arguments: --json"
    out = status_of(cluster, capsys, config=tmp_path / "cfg")

    assert "not probed - capreport produced no parsable JSON" in out
    assert "live attach" not in out


def test_status_reports_each_running_seats_own_build() -> None:
    """The same measurement `attach` prints, per seat.

    Read from the seat and not from its image reference: an ephemeral
    container's `image` is what the spec *asked* for, and `IfNotPresent` over a
    tag that moves serves whatever the node had cached.
    """
    cluster = degraded_seat()
    pod = PodRef("demo", "target")
    present = seats(cluster.pod)

    assert probe_seat_versions(talking_to(cluster), pod, present) == {
        "podbench-1": __version__
    }


def test_status_asks_only_the_seats_that_could_answer() -> None:
    """A container that is not running has no process to ask."""
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[{"name": "podbench-1"}, {"name": "podbench-2"}],
            ephemeral_statuses=[running_status("podbench-2")],
        )
    )
    pod = PodRef("demo", "target")

    assert probe_seat_versions(talking_to(cluster), pod, seats(cluster.pod)) == {
        "podbench-2": __version__
    }


def test_status_keeps_a_seat_that_would_not_say_its_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Asked and unanswered is not the same fact as never asked."""
    cluster = degraded_seat()
    cluster.seat_version = None
    out = status_of(cluster, capsys, config=tmp_path / "cfg")

    assert "version unknown - this seat did not answer" in out
    assert "version not probed" not in out


def test_status_says_not_probed_for_a_version_it_did_not_ask_for(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--no-probe` is for a listing that touches nothing, versions included."""
    out = status_of(degraded_seat(), capsys, "--no-probe", config=tmp_path / "cfg")

    assert "version not probed" in out


def test_a_listing_that_measured_nothing_claims_no_version(tmp_path: Path) -> None:
    """`list` has no probe at all, and an image tag is not a version."""
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[{"name": "podbench-1", "image": "ghcr.io/x/podbench:main"}],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    text = format_seats(
        PodRef("demo", "target"), seats(cluster.pod), directory=tmp_path
    )

    assert "version   not probed" in text
    assert "ghcr.io/x/podbench:main" not in text


LONG_POD = "fastcs-p47-ioc-example-01-5d8b9c7f4-xk2wq"
"""A pod name of the length a Deployment on a real beamline produces.

Long enough that the kubelet's own reference to it does not fit under the
``state`` label, which is the whole defect: the length is the field's, not a
number chosen to make a test fail.
"""

KUBELET_REFUSAL_WITH_POD_REF = (
    f"{KUBELET_REFUSAL} "
    f'(pod: "{LONG_POD}_hgv27681(2b1f8a3e-9c4d-4f21-b8e7-1a2c3d4e5f60)", '
    "container: podbench-1)"
)
"""What the kubelet really sends: `format.Pod` appended to the message."""


def refused_seat_listing(directory: Path) -> str:
    """One pod whose first seat the kubelet refused, formatted for `list`."""
    document = pod_document(
        name=LONG_POD,
        uid=1000,
        ephemeral=[{"name": "podbench-1"}],
        ephemeral_statuses=[
            {
                "name": "podbench-1",
                "state": {
                    "waiting": {
                        "reason": CREATE_CONTAINER_CONFIG_ERROR,
                        "message": KUBELET_REFUSAL_WITH_POD_REF,
                    }
                },
            }
        ],
    )
    return format_seats(
        PodRef("hgv27681", LONG_POD), seats(document), directory=directory
    )


def test_a_refused_seats_listing_does_not_run_past_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole listing, because a refused seat overflowed it twice.

    `"<pod>_<ns>(<uid>)",` has no space in it, so `console.wrap` breaks
    nowhere in the `state` row; and the offer below it is deliberately never
    wrapped, so the prose that used to trail the pod name could not move
    either. Neither has a width that fixes it - both were fixed by printing
    less - which is why this asserts against the terminal and not against a
    number.
    """
    monkeypatch.setenv("COLUMNS", "100")

    lines = refused_seat_listing(tmp_path / "cfg").splitlines()

    assert lines
    assert max(len(line) for line in lines) <= console().width


def test_the_offer_under_a_seatless_pod_ends_with_what_to_paste(
    tmp_path: Path,
) -> None:
    """The shape `ssh_connect_line`'s own missing-stanza answers already use.

    This line is never wrapped, because wrapping would break the command on a
    space, so anything after the command is columns nothing can shorten - and
    it also has to be selected around before the command can be pasted.
    """
    text = refused_seat_listing(tmp_path / "cfg")

    assert text.endswith(
        f"nothing to ssh to here: podbench attach -n hgv27681 {LONG_POD}"
    )


def test_only_the_kubelets_reference_to_the_pod_is_dropped(tmp_path: Path) -> None:
    """Elided because the reader has it twice already, not to save columns.

    The listing is headed `namespace/pod` and the row above names the seat, so
    what the tail adds is a pod UID nobody reading a pod by name can use. What
    the kubelet actually said about the container has to survive it.
    """
    text = refused_seat_listing(tmp_path / "cfg")
    # Flattened: the message wraps, so a phrase assertion would measure the
    # wrap and not the elision (see tests/test_doctor.py::flowed).
    flowed = " ".join(text.split())

    assert CREATE_CONTAINER_CONFIG_ERROR in flowed
    assert KUBELET_REFUSAL in flowed, "the refusal itself is the reason"
    assert "2b1f8a3e" not in flowed
    assert "(pod:" not in flowed


def test_a_message_with_ordinary_parentheses_is_left_alone(tmp_path: Path) -> None:
    """The elision is one kubelet format, not a rule about brackets.

    Relayed text is not ours to edit generally; this is one machine-generated
    tail whose every field is already on screen.
    """
    document = pod_document(
        uid=1000,
        ephemeral=[{"name": "podbench-1"}],
        ephemeral_statuses=[
            {
                "name": "podbench-1",
                "state": {
                    "waiting": {
                        "reason": CREATE_CONTAINER_CONFIG_ERROR,
                        "message": 'secret "ioc-creds" not found (check the pod)',
                    }
                },
            }
        ],
    )

    text = format_seats(
        PodRef("demo", "target"), seats(document), directory=tmp_path / "cfg"
    )

    assert "(check the pod)" in text


def test_a_listing_says_which_container_each_seat_is_in(tmp_path: Path) -> None:
    """Two seats on one pod can be in different containers, so it is per seat.

    An ephemeral container with no ``targetContainerName`` is in none of them -
    it runs in the pod's own namespaces - and that is a third answer rather
    than a synonym for the first container.
    """
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            container="ca-gateway",
            siblings=["pva-gateway"],
            ephemeral=[
                {"name": "podbench-1", "targetContainerName": "pva-gateway"},
                {"name": "podbench-2"},
            ],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    text = format_seats(
        PodRef("demo", "target"), seats(cluster.pod), directory=tmp_path
    )

    assert "target    pva-gateway" in text
    assert f"target    {NO_TARGET_CONTAINER}" in text


def test_a_listing_marks_the_seat_the_gid_correction_replaced(
    tmp_path: Path,
) -> None:
    """Two live seats, and nothing else on the rows says which one to use."""
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            container="app",
            ephemeral=[
                {
                    "name": "podbench-1",
                    "targetContainerName": "app",
                    "securityContext": {"runAsUser": 1000},
                },
                {
                    "name": "podbench-2",
                    "targetContainerName": "app",
                    "securityContext": {"runAsUser": 1000, "runAsGroup": 1000},
                },
            ],
            ephemeral_statuses=[
                running_status("podbench-1"),
                running_status("podbench-2"),
            ],
        )
    )
    text = format_seats(
        PodRef("demo", "target"), seats(cluster.pod), directory=tmp_path
    )

    assert "note      superseded by podbench-2" in text
    assert "--target-gid" in " ".join(text.split())
    # Only the earlier one, and the note is not repeated under the seat that
    # replaced it.
    assert text.count("superseded by") == 1


def test_a_deliberate_second_seat_is_not_called_superseded(tmp_path: Path) -> None:
    """``--new`` lands a seat pinned exactly as the first one is.

    Nothing replaced anything, so a listing that said otherwise would be
    inventing a reason to distrust a seat the user asked for.
    """
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            container="app",
            ephemeral=[
                {
                    "name": "podbench-1",
                    "targetContainerName": "app",
                    "securityContext": {"runAsUser": 1000, "runAsGroup": 1000},
                },
                {
                    "name": "podbench-2",
                    "targetContainerName": "app",
                    "securityContext": {"runAsUser": 1000, "runAsGroup": 1000},
                },
            ],
            ephemeral_statuses=[
                running_status("podbench-1"),
                running_status("podbench-2"),
            ],
        )
    )
    text = format_seats(
        PodRef("demo", "target"), seats(cluster.pod), directory=tmp_path
    )

    assert "superseded" not in text


def test_list_finds_pods_carrying_a_seat(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[{"name": "podbench-1"}],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )
    assert (
        main(
            ["list", "-n", "demo", "--config-dir", str(tmp_path / "cfg")],
            runner=cluster,
        )
        == 0
    )
    assert "demo/target" in capsys.readouterr().out


def seated_cluster() -> FakeCluster:
    """One pod in ``demo`` carrying a running seat."""
    return FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[{"name": "podbench-1"}],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )


def written_stanza(directory: Path, alias: str, seat: str = "podbench-1") -> Path:
    """The stanza ``attach`` would have left for one seat, under ``alias``."""
    path = ssh_config_path(directory, PodRef("demo", "target"), seat)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(f"Host {alias}\n    HostName target\n    User root\n")
    return path


def test_list_reports_the_alias_the_stanza_carries_not_the_computed_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The failure this exists to catch: `attach --host-alias mine` is recorded
    # nowhere in the cluster, so a recomputed default would name a Host that
    # does not resolve for the one user who chose otherwise.
    written_stanza(tmp_path / "cfg", "mine")
    assert (
        main(
            ["list", "-n", "demo", "--config-dir", str(tmp_path / "cfg")],
            runner=seated_cluster(),
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "ssh mine" in out
    assert "podbench-demo-target" not in out


def test_status_reports_the_alias_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    written_stanza(tmp_path / "cfg", "podbench-demo-target")
    assert (
        main(
            ["status", "target", "-n", "demo", "--config-dir", str(tmp_path / "cfg")],
            runner=seated_cluster(),
        )
        == 0
    )
    assert "ssh podbench-demo-target" in capsys.readouterr().out


def test_a_seat_landed_elsewhere_is_named_as_such_not_guessed_at(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            ["list", "-n", "demo", "--config-dir", str(tmp_path / "cfg")],
            runner=seated_cluster(),
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "no ssh config here" in out
    assert "podbench ssh-config -n demo target" in out
    assert "ssh podbench-demo-target" not in out


def test_a_pod_whose_seat_is_gone_is_not_offered_an_alias(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The stanza outlives the container it was written for.

    Nothing deletes it, and an ephemeral container's name is burnt for the
    pod's lifetime once it exits — so the alias is still on disk and still
    resolves, and the connection it makes cannot work. Measured at DLS on
    2026-08-16, where a listing printed `ssh <alias>` one line under
    ``terminated ... name burnt for this pod's lifetime``.
    """
    written_stanza(tmp_path / "cfg", "podbench-demo-target")
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[{"name": "podbench-1"}],
            ephemeral_statuses=[
                {
                    "name": "podbench-1",
                    "state": {"terminated": {"reason": "Error", "exitCode": 1}},
                }
            ],
        )
    )

    assert (
        main(
            ["list", "-n", "demo", "--config-dir", str(tmp_path / "cfg")],
            runner=cluster,
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "name burnt for this pod's lifetime" in out, "the row still says why"
    assert "ssh podbench-demo-target" not in out
    assert "podbench attach -n demo target" in out


def test_an_unreadable_config_dir_costs_the_alias_not_the_listing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --config-dir pointed at a file, which is what a typo looks like: the read
    # fails with NotADirectoryError rather than "missing", and a listing that
    # died on it would lose the seats as well as the alias.
    not_a_dir = tmp_path / "cfg"
    not_a_dir.write_text("")
    assert (
        main(
            ["list", "-n", "demo", "--config-dir", str(not_a_dir)],
            runner=seated_cluster(),
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "demo/target" in out
    assert "cannot read" in out and "Not a directory" in out


def test_a_stanza_with_no_host_line_is_reported_as_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = ssh_config_path(tmp_path / "cfg", PodRef("demo", "target"), "podbench-1")
    path.parent.mkdir(parents=True)
    path.write_text("    User root\n")
    assert (
        main(
            ["list", "-n", "demo", "--config-dir", str(tmp_path / "cfg")],
            runner=seated_cluster(),
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "no Host line" in out
    assert str(path) in out


def test_list_still_writes_no_ssh_config(tmp_path: Path) -> None:
    # Reading the config dir is a change of contract for this verb; writing to
    # it would be another one, and `list` is a verb people run in a loop.
    directory = tmp_path / "cfg"
    assert (
        main(
            ["list", "-n", "demo", "--config-dir", str(directory)],
            runner=seated_cluster(),
        )
        == 0
    )
    assert not directory.exists()


def test_list_and_status_recover_the_rung_without_an_exec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole reason the start-up report travels over `kubectl logs`.

    The rung a seat *is* can only be measured inside it, and these two verbs
    read pod JSON. An exec per seat is the thing the shape was chosen to avoid:
    `list` runs against a whole namespace, and `status --no-probe` exists for a
    listing that must touch nothing at all.
    """
    for argv in (
        ["list", "-n", "demo", "--config-dir", str(tmp_path / "cfg")],
        [
            "status",
            "target",
            "-n",
            "demo",
            "--no-probe",
            "--config-dir",
            str(tmp_path / "cfg"),
        ],
    ):
        cluster = argus_shaped_pod()
        assert main(argv, runner=cluster) == 0
        assert [call for call in cluster.calls if "exec" in call] == []
        assert "podbench-1   attach  running     degraded" in capsys.readouterr().out


def test_a_seat_whose_log_cannot_be_read_is_not_measured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A seat older than the report, a rotated log, RBAC without `pods/log`.

    Each of them is a listing with no measurement, and the one answer that is
    not available is the authored rung wearing a measured label - which is the
    column issue #94 was filed about.
    """
    cluster = argus_shaped_pod()
    cluster.seat_logs["podbench-1"] = None
    assert (
        main(
            ["status", "target", "-n", "demo", "--config-dir", str(tmp_path / "cfg")],
            runner=cluster,
        )
        == 0
    )
    out = capsys.readouterr().out

    assert "podbench-1   attach  running     not measured" in out
    assert "request   degraded" in out
    assert "read from the seat's securityContext" in out


def test_an_older_seat_against_a_newer_launcher_lists_rather_than_raises(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An image built before the report existed writes a log with no report in
    it, and one built after this launcher may write keys it has never heard of.
    Neither may cost the listing: the first is *not measured*, and the second is
    read for the fields it does carry."""
    cluster = argus_shaped_pod()
    cluster.seat_logs["podbench-1"] = "agent: prepared /root\nagent: idling\n"
    assert (
        main(["list", "-n", "demo", "--config-dir", str(tmp_path)], runner=cluster) == 0
    )
    out = capsys.readouterr().out
    assert "not measured" in out

    newer = argus_shaped_pod()
    newer.seat_logs["podbench-1"] = (
        'agent: podbench-report: {"uid": 0, "gid": 0, "target_uid": 0, '
        '"target_gid": 0, "sys_ptrace": false, "version": 99, '
        '"something_new": {"nested": true}}'
    )
    assert (
        main(["list", "-n", "demo", "--config-dir", str(tmp_path)], runner=newer) == 0
    )
    assert "podbench-1   attach  running     degraded" in capsys.readouterr().out

    # And a report carrying only half of the credential check is *not measured*
    # either. The uids matching is half a comparison, and half a comparison is
    # not a rung - `__ptrace_may_access()` wants the group ids too.
    half = argus_shaped_pod()
    half.seat_logs["podbench-1"] = (
        'agent: podbench-report: {"uid": 0, "target_uid": 0, "sys_ptrace": false}'
    )
    assert main(["list", "-n", "demo", "--config-dir", str(tmp_path)], runner=half) == 0
    assert "podbench-1   attach  running     not measured" in capsys.readouterr().out


def test_a_refused_startup_step_reaches_a_laptop_verb(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #99. `ensure_all` records a refused step rather than raising, and
    the record went to the container log - which is the place the issue is about.
    A refused `SetEnv` costs the ssh transport a variable, and the seat is up and
    looks fine."""
    refusal = (
        "[FAIL] ensure-sshd-config: PATH contains whitespace, so SetEnv would "
        "end the pair early"
    )
    cluster = argus_shaped_pod()
    cluster.startup_failures = [refusal]

    session = attach(talking_to(cluster), "target")
    assert any(refusal in warning for warning in session.warnings)

    assert (
        main(
            [
                "status",
                "target",
                "-n",
                "demo",
                "--no-probe",
                "--config-dir",
                str(tmp_path / "cfg"),
            ],
            runner=cluster,
        )
        == 0
    )
    out = " ".join(capsys.readouterr().out.split())
    assert refusal in out
    assert [call for call in cluster.calls[-3:] if "exec" in call] == []


def test_the_host_line_is_read_however_it_was_spelled() -> None:
    # ssh_config keywords are case-insensitive and a leading indent is legal, so
    # a stanza someone tidied by hand still names the alias they connect with.
    assert host_alias_in("\thost  tidied\n\tuser root\n") == "tidied"
    # A commented-out Host is not one ssh would match, so neither is it one to
    # tell the user to type.
    assert host_alias_in("# Host old\nHost real\n") == "real"


def test_resize_failure_is_a_warning_not_a_dead_end() -> None:
    cluster = FakeCluster(pod_document(uid=1000), patch_error="unknown subresource")
    note = try_resize(talking_to(cluster), "target", "app", "6Gi")
    assert "refused" in note
    assert "unknown subresource" in note


def test_resize_asks_for_the_resize_subresource() -> None:
    cluster = FakeCluster(pod_document(uid=1000))
    try_resize(talking_to(cluster), "target", "app", "6Gi")
    patch = next(call for call in cluster.calls if "patch" in call)
    assert "--subresource=resize" in patch
    assert "--type=strategic" in patch


def test_a_successful_resize_says_the_controller_still_asks_for_the_old_limit() -> None:
    """The resize writes to the pod and nothing reconciles the template (R13).

    A rollout then reverts it with no symptom but a seat that OOMs again, so the
    success note has to carry the caveat: nobody reads a warning they were not
    shown.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    note = try_resize(talking_to(cluster), "target", "app", "6Gi")
    assert "6Gi" in note
    assert "template" in note
    assert "rollout" in note


def test_the_resize_warning_keeps_the_quota_caveats_it_has_not_narrowed() -> None:
    """R13 was narrowed by measurement, not retired.

    A `LimitRange` and a `ResourceQuota` are still untested — the namespace that
    was measured had neither — so narrowing the controller half of the warning
    must not quietly drop the half that still stands. It is stated on the path
    that took the resize, which is where somebody has just mutated a live pod;
    the line the *other* path prints is an offer of the flag.
    """
    cluster = FakeCluster(pod_document(uid=1000))
    note = try_resize(talking_to(cluster), "target", "app", "6Gi")
    assert "LimitRange" in note
    assert "ResourceQuota" in note
    assert "partly proven" in note
    assert "ReplicaSet" in note


def test_a_refused_resize_says_once_that_it_probably_did_not_matter() -> None:
    """Detecting the refusal was always right; the alarm around it was not.

    The sweep filed this as "not podbench" - the service account lacked
    `pods/resize`, podbench printed the API error and carried on - but a reader
    who is told only that a mutation failed goes looking for the RBAC to make it
    succeed. Naming what a seat actually costs is what stops that, and it is
    said on this path only, where somebody just asked for the resize.
    """
    cluster = FakeCluster(pod_document(uid=1000), patch_error="pods is forbidden")
    note = try_resize(talking_to(cluster), "target", "app", "6Gi")

    assert "was refused" in note
    assert "13-23 MiB" in note, "the measured cost, so the refusal can be sized"
    assert "2026-08-19" in note, "dated, because it is one beamline at one moment"
    assert "vscode-server" in note, "the one cost that is still worth acting on"


def test_the_clusters_own_words_end_the_refusal_and_are_not_run_into() -> None:
    """`...namespace "hgv27681" A seat itself measured...` - one seam, invisible.

    Relayed stderr is not ours to reflow and may or may not end in a stop, so
    anything podbench appends after it reads as more of the API server's
    message. Nothing follows it now, and what introduces it is ours.
    """
    cluster = FakeCluster(
        pod_document(uid=1000),
        patch_error='pods "app" is forbidden in namespace "hgv27681"',
    )

    note = try_resize(talking_to(cluster), "target", "app", "6Gi")

    assert note.endswith(
        'The API server said: pods "app" is forbidden in namespace "hgv27681"'
    )
    # The seat's own cost still precedes it: the reader is told the refusal
    # probably did not matter before being handed the cluster's wording.
    assert note.index("13-23 MiB") < note.index("The API server said:")


def test_a_claim_explanation_joins_our_sentence_and_not_the_clusters() -> None:
    """The other seam the refusal used to sit between.

    `explain_claim_refusal` opens in lower case because it continues a sentence;
    continuing the *cluster's* is what made it unreadable, so it now continues
    podbench's own.
    """
    document = pod_document(uid=1000)
    document["spec"]["containers"][0]["resources"] = {
        "claims": [{"name": "bl01c-ea-flip-02"}]
    }
    cluster = FakeCluster(
        document, patch_error="only cpu and memory resources are mutable"
    )

    note = try_resize(talking_to(cluster), "target", "app", "6Gi")

    assert "existing limits - the target container holds a resource claim" in note
    assert note.index("bl01c-ea-flip-02") < note.index("The API server said:")


def guaranteed_document() -> dict[str, Any]:
    """A pod the kubelet called Guaranteed: request equals limit, and it says so."""
    document = pod_document(uid=1000)
    document["spec"]["containers"][0]["resources"] = {
        "limits": {"memory": "256Mi", "cpu": "500m"},
        "requests": {"memory": "256Mi", "cpu": "500m"},
    }
    document["status"]["qosClass"] = "Guaranteed"
    return document


def test_a_guaranteed_pod_is_offered_the_spelling_that_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #124, half one: a doomed patch replaced by the command to paste.

    Guaranteed means request == limit, so raising the limit alone must change
    the QoS class and the API server refuses it at every number. Nothing is
    submitted, and what the user is told is `--resize REQUEST:LIMIT`.
    """
    cluster = FakeCluster(guaranteed_document())

    note = try_resize(talking_to(cluster), "target", "app", "2Gi")

    assert "Guaranteed" in note
    assert "`--resize 2Gi:2Gi`" in note
    assert [call for call in cluster.calls if "patch" in call] == []
    # And it survives the wrap that made #120: the half to be pasted is still
    # one line at the two widths anybody runs a terminal at.
    for columns in ("80", "60"):
        monkeypatch.setenv("COLUMNS", columns)
        lines = wrap(note, width=wrap_width())
        assert len(lines) > 1, columns
        assert any("`--resize 2Gi:2Gi`" in line for line in lines), columns


def test_a_guaranteed_pod_asked_for_both_halves_is_resized() -> None:
    """The capability exists; only the limit-alone spelling is unavailable.

    Measured on the bed 2026-08-21: `--resize 2Gi:2Gi` keeps `qos=Guaranteed`
    and takes effect. So the check has to be "would this patch change the
    class", never "is this pod Guaranteed".
    """
    cluster = FakeCluster(guaranteed_document())

    note = try_resize(talking_to(cluster), "target", "app", "2Gi:2Gi")

    assert "resized app to memory 2Gi/2Gi" in note
    assert [call for call in cluster.calls if "patch" in call] != []


def test_a_burstable_pod_still_takes_a_limit_only_resize() -> None:
    """The pre-check reads the class the kubelet published, and nothing else.

    A container whose request happens to equal its limit inside a Burstable pod
    changes no class by being raised, and refusing it here would break the
    ordinary case to protect the rare one.
    """
    document = pod_document(uid=1000)
    document["spec"]["containers"][0]["resources"] = {
        "limits": {"memory": "256Mi"},
        "requests": {"memory": "256Mi"},
    }
    cluster = FakeCluster(document)

    note = try_resize(talking_to(cluster), "target", "app", "2Gi")

    assert "resized app to memory 2Gi" in note


def test_a_qos_refusal_from_the_api_server_names_the_remedy_too() -> None:
    """The backstop, for a pod object that carried no `status.qosClass`.

    The pre-check reads a field, and a document fetched by somebody else may
    not have one. The refusal then arrives from the API server, and it names
    the class and not the flag - so podbench names the flag.
    """
    document = pod_document(uid=1000)
    document["spec"]["containers"][0]["resources"] = {
        "limits": {"memory": "256Mi"},
        "requests": {"memory": "256Mi"},
    }
    cluster = FakeCluster(
        document,
        patch_error=(
            'Pod "target" is invalid: spec: Invalid value: "Guaranteed": Pod '
            "QOS Class may not change as a result of resizing"
        ),
    )

    note = try_resize(talking_to(cluster), "target", "app", "2Gi")

    assert "`--resize 2Gi:2Gi`" in note
    # The cluster's own words still end the note, with ours before them.
    assert note.index("`--resize 2Gi:2Gi`") < note.index("The API server said:")


def test_an_attach_that_changed_no_limits_never_offers_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #54, and the measurement that made it safe to close.

    The report used to spend two warnings on memory: an offer of a mode nobody
    had asked for, and a caution written against an assumption. Ten live seats
    on DLS p47 (2026-08-19) cost 13-23 MiB against 170-3858 MiB of pod headroom,
    so both are gone and what is left is a row stating the number.
    """
    cluster = FakeCluster(pod_document(uid=1000))
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
    assert code == 0
    out = capsys.readouterr().out
    assert "resize" not in out
    assert "WARNING" not in out
    assert "memory      " in out, "the fact stays; only the alarm goes"


def test_a_pod_with_room_gets_the_number_and_no_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`bl47p-ea-simdet-03-0`: the *tightest* pod on the beamline, and fine.

    256Mi of limit against 86Mi in use, carrying a seat already. A threshold
    keyed on the container's limit - which is how the plan worded it - would
    have flagged p47's three 100Mi socat containers, which sit in the pod with
    the most room per byte used. Headroom is the number that decides.
    """
    cluster = FakeCluster(limited_pod("256Mi"), top="target   1m   86Mi\n")
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
    assert code == 0
    out = capsys.readouterr().out
    assert "memory      170Mi free of 256Mi (86Mi in use)" in out
    assert "WARNING" not in out


def test_a_pod_with_no_room_left_is_warned_about_with_its_own_numbers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The case the threshold is kept for: a 100Mi pod really using 80.

    No pod on p47 is in this state, which is the point - the warning has to be
    able to fire, and has to stay silent on every pod that was measured.
    """
    cluster = FakeCluster(limited_pod("100Mi"), top="target   1m   80Mi\n")
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
    assert code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "WARNING only 20Mi of memory headroom" in out
    assert "13-23 MiB" in out, "what a seat costs, so 20Mi can be judged"
    assert "--resize MEMORY" in out, "the flag that buys headroom in place"


def test_an_unreadable_headroom_reads_as_unmeasured_and_warns_about_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No metrics-server is ordinary, and it is not evidence of anything.

    Both halves matter and they have bitten this branch twice already (#89's
    `/proc` path, C14's address space): a missing measurement must not be
    reported as a good one, and it must not be turned into a caution either -
    that would put the always-on memory warning straight back on every cluster
    without a metrics API.
    """
    cluster = FakeCluster(limited_pod("256Mi"))
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
    assert code == 0
    out = capsys.readouterr().out
    assert "memory      limit 256Mi; in use not measured (no metrics API here)" in out
    assert "WARNING" not in out


def test_the_metrics_api_is_not_asked_about_a_pod_with_no_ceiling() -> None:
    """An unbounded pod cgroup has no headroom question, so it costs no call."""
    cluster = FakeCluster(pod_document(uid=1000))
    session = attach(talking_to(cluster), "target")

    assert session.headroom == Headroom(limit=None, used=None)
    assert not [call for call in cluster.calls if "top" in call]


def test_vscode_sizes_the_pod_from_the_headroom_it_already_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The number was always computable and podbench used to ask for it back.

    1Gi of headroom carries a 13-23 MiB seat with room to spare; vscode-server
    measured 1215 MiB live with a single extension (2026-08-16), so this pod is
    1215Mi - 1Gi short. `editor_limit` raises the *target's* limit by that
    shortfall - the only limit a seat can move, since an ephemeral container may
    not declare `resources` at all - and rounds up to the next whole GiB.
    """
    cluster = FakeCluster(limited_pod("4Gi"), top="target   1m   3Gi\n")
    code = main(
        vscode_argv(tmp_path),
        runner=cluster,
        which=lambda name: f"/usr/bin/{name}",
    )
    assert code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "1Gi free is under the 1215Mi vscode-server measured" in out
    assert "app's memory limit was raised to 5Gi" in out
    # Read back after the raise, which is the whole point of reading it there:
    # the editor now fits, so the OOM warning has nothing to say.
    assert "memory 2Gi free of 5Gi (3Gi in use)" in out
    assert "vscode-server lands here" not in out

    # …and the same pod, attached without the editor, is neither warned nor
    # resized: nothing in `attach` decides to spend this pod's memory.
    plain = FakeCluster(limited_pod("4Gi"), top="target   1m   3Gi\n")
    assert main(attach_argv(tmp_path, "--no-prompt"), runner=plain) == 0
    assert "vscode-server" not in capsys.readouterr().out
    assert not [call for call in plain.calls if "patch" in call]


def test_no_resize_declines_the_raise_and_keeps_the_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Declining the mutation is not declining to be told about it. The hazard
    is exactly as it was, so the line that names it comes back - which is also
    what a pod whose resize was *refused* (#107) sees."""
    cluster = FakeCluster(limited_pod("4Gi"), top="target   1m   3Gi\n")
    code = main(
        vscode_argv(tmp_path, "--no-resize"),
        runner=cluster,
        which=lambda name: f"/usr/bin/{name}",
    )
    assert code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert not [call for call in cluster.calls if "patch" in call]
    assert "memory 1Gi free of 4Gi (3Gi in use)" in out
    assert "vscode-server lands here and it measured 1215Mi" in out


def test_a_pod_with_room_is_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Sizing is a shortfall, not a policy: a pod that already carries the
    editor gets no patch and no line about one."""
    cluster = FakeCluster(limited_pod("8Gi"), top="target   1m   1Gi\n")
    assert (
        main(
            vscode_argv(tmp_path), runner=cluster, which=lambda name: f"/usr/bin/{name}"
        )
        == 0
    )
    out = " ".join(capsys.readouterr().out.split())
    assert not [call for call in cluster.calls if "patch" in call]
    assert "sized for the editor" not in out
    assert "vscode-server lands here" not in out


def test_an_unmeasured_pod_is_told_that_nothing_sized_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one unmeasured case that earns a line. Everywhere else podbench
    states an unreadable headroom on the report's row and leaves it, because a
    caution there would fire on every cluster with no metrics-server - but this
    verb undertook to size the pod, and quietly not doing it leaves the user
    believing it did."""
    # `top` of None is the ordinary cluster with no metrics-server.
    cluster = FakeCluster(limited_pod("256Mi"))
    assert (
        main(
            vscode_argv(tmp_path), runner=cluster, which=lambda name: f"/usr/bin/{name}"
        )
        == 0
    )
    out = " ".join(capsys.readouterr().out.split())
    assert not [call for call in cluster.calls if "patch" in call]
    assert "not measured (no metrics API here), so nothing sized it" in out


def test_vscode_names_the_storage_cost_no_flag_of_its_can_pay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Memory can be raised in place and disk cannot: `spec.volumes` is
    immutable, so a pod that was not deployed with `podbench-home` cannot be
    given one now. It is worth a line anyway - the overrun evicts the *pod*,
    application included, and nothing else in the run mentions it."""
    cluster = FakeCluster(pod_document(uid=1000))
    assert (
        main(
            vscode_argv(tmp_path), runner=cluster, which=lambda name: f"/usr/bin/{name}"
        )
        == 0
    )
    out = " ".join(capsys.readouterr().out.split())
    assert "declares no 'podbench-home' volume" in out
    assert "evicts the whole pod" in out


def test_a_pod_deployed_with_the_home_volume_is_not_warned_about_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The case the volume was deployed for. Warning here would be a caution
    about something working."""
    cluster = FakeCluster(identity_pod())
    assert (
        main(
            vscode_argv(tmp_path), runner=cluster, which=lambda name: f"/usr/bin/{name}"
        )
        == 0
    )
    out = " ".join(capsys.readouterr().out.split())
    assert "declares no 'podbench-home' volume" not in out
    assert "a root seat does not use it" not in out


def test_a_root_seat_orphans_the_home_volume_and_is_told_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#42: the volume is mounted and unused. sshd takes a session's $HOME from
    the passwd record, and for uid 0 the image's own record already says /root -
    so every layer is individually correct and the join is what fails. The pod
    looks prepared, which is exactly why silence would be wrong."""
    # The home volume without the non-root pin, so the full rung actually
    # lands: `identity_pod` carries `runAsNonRoot`, which refuses it.
    cluster = FakeCluster(pod_document(uid=1000, volumes=[HOME_VOLUME]))
    assert (
        main(
            vscode_argv(tmp_path, "--max-rung", "full"),
            runner=cluster,
            which=lambda name: f"/usr/bin/{name}",
        )
        == 0
    )
    out = " ".join(capsys.readouterr().out.split())
    assert "a root seat does not use it" in out
    assert "'/root'" in out


def test_an_unbounded_pod_has_no_ceiling_to_raise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A container declaring no memory limit leaves the pod cgroup unbounded,
    so there is no ceiling for the seat to share and nothing worth patching.
    The report's memory row says so already."""
    cluster = FakeCluster(pod_document(uid=1000))
    assert (
        main(
            vscode_argv(tmp_path), runner=cluster, which=lambda name: f"/usr/bin/{name}"
        )
        == 0
    )
    out = " ".join(capsys.readouterr().out.split())
    assert not [call for call in cluster.calls if "patch" in call]
    assert "sized for the editor" not in out
    assert "no pod memory limit" in out


def test_current_namespace_falls_back_to_default() -> None:
    def runner(
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        return CommandResult(tuple(argv), 0, "", "")

    assert current_namespace(runner=runner) == "default"


def test_current_namespace_reads_the_kubeconfig() -> None:
    cluster = FakeCluster(pod_document())
    assert current_namespace(runner=cluster) == "demo"


def test_kubectl_for_asks_the_kubeconfig_only_when_the_flag_did_not() -> None:
    """One helper, so no verb can quietly mean the literal `default` (issue #44).

    The second half matters as much as the first: the lookup is itself a
    `kubectl` call, so a verb given `-n` must not pay for it.
    """
    cluster = FakeCluster(pod_document())
    assert kubectl_for(None, runner=cluster).namespace == "demo"

    def refuses(
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        raise AssertionError(f"nothing should have run: {list(argv)}")

    assert kubectl_for("chosen", runner=refuses).namespace == "chosen"


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
    live, read_only, launch, _iterate, ssh_seat, exec_seat = features(session)
    assert not live.available
    assert not read_only.available
    assert not launch.available
    # The ssh half is not claimed either: nothing asked the seat whether sshd
    # can resolve a login name for the uid it ended up running as.
    assert not ssh_seat.available
    assert "was not asked" in ssh_seat.reason
    assert exec_seat.available


# -- which of the three modes a seat is serving (issue #141) ----------------


HOTFIX_MOUNT = {"name": HOTFIX_CLAIM_VOLUME, "mountPath": HOTFIX_APP_PATH}


def hotfixed_pod(
    *, mounted: bool, claim: bool = True, supervisor: bool = True
) -> dict[str, Any]:
    """A pod deployed to carry a hotfix, with a seat that shares it or does not.

    Mirrors what `podbench hotfix --print-values` emits and Argo then renders:
    the claim in `spec.volumes`, the application mounting it at
    `HOTFIX_APP_PATH`, and the supervisor loop as the container's `args`. Not
    an annotation - podbench never got to write one, because Argo strips
    pod-template annotations on self-heal (#32), which is the whole of #177.

    `claim` and `supervisor` drop one half each, because the predicate is an
    `and` and a test that only ever builds both halves cannot see that.
    """
    return pod_document(
        uid=1000,
        volumes=(
            [
                {
                    "name": HOTFIX_CLAIM_VOLUME,
                    "persistentVolumeClaim": {"claimName": "myapp-podbench"},
                }
            ]
            if claim
            else []
        ),
        volume_mounts=[HOTFIX_MOUNT],
        args=[hold_loop_args("myapp serve")] if supervisor else [],
        ephemeral=[
            {
                "name": "podbench-1",
                "targetContainerName": "app",
                "securityContext": {"runAsUser": 1000},
                **({"volumeMounts": [HOTFIX_MOUNT]} if mounted else {}),
            }
        ],
        ephemeral_statuses=[running_status("podbench-1")],
    )


def dev_pod(*, ephemeral: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    """Iterate mode's clone: the seat is an ordinary sidecar, not an ephemeral
    container, and ``spec.ephemeralContainers`` is empty because the API server
    refuses one on create."""
    return pod_document(
        name="demo-podbench",
        labels={DEVPOD_LABEL: "true"},
        sidecar={
            "name": "podbench",
            "image": "ghcr.io/gilesknap/podbench:0.1.0",
            "env": [{"name": "PODBENCH_TARGET", "value": "app"}],
            "securityContext": {"runAsUser": 1000, "runAsGroup": 1000},
        },
        sidecar_status=running_status("podbench"),
        ephemeral=list(ephemeral),
        ephemeral_statuses=[running_status(entry["name"]) for entry in ephemeral],
    )


def test_a_dev_pods_sidecar_is_invisible_to_the_ephemeral_read() -> None:
    """The premise of issue #141: ``seats()`` walks ``spec.ephemeralContainers``,
    Iterate mode's seat is in ``spec.containers``, so a listing built on the
    former reported a dev pod as carrying no podbench container at all - which
    dropped the pod from ``list`` entirely."""
    pod = dev_pod()

    assert seats(pod) == []
    assert [seat.name for seat in all_seats(pod)] == ["podbench"]


def test_the_dev_sidecar_reads_back_as_a_dev_seat() -> None:
    seat = dev_seat(dev_pod())

    assert seat is not None
    assert seat.kind is SeatKind.DEV
    assert seat.running
    # From PODBENCH_TARGET, since an ordinary container has no
    # targetContainerName to read - so the column says the same thing for both
    # container kinds.
    assert seat.target == "app"
    assert (seat.uid, seat.gid) == (1000, 1000)


def test_an_ephemeral_seat_in_a_dev_pod_is_not_a_dev_seat() -> None:
    """The distinction ``seat_kind`` takes ``sidecar=`` for. Nothing stops an
    attach landing an ephemeral seat in a dev pod, and that seat is an
    Observe-mode seat whatever the pod is labelled: the application is not its
    child, so ``flavour.detect_mode`` measures OBSERVE and every path mapping is
    the Observe-mode one."""
    pod = dev_pod(
        ephemeral=[{"name": "podbench-1", "securityContext": {"runAsUser": 1000}}]
    )

    kinds = {seat.name: seat.kind for seat in all_seats(pod)}

    assert kinds == {"podbench": SeatKind.DEV, "podbench-1": SeatKind.ATTACH}


def test_a_seat_sharing_the_workloads_claim_is_a_hotfix_seat() -> None:
    (seat,) = seats(hotfixed_pod(mounted=True))

    assert seat.kind is SeatKind.HOTFIX
    assert not seat.in_hotfixed_pod


def test_a_seat_in_a_hotfixed_pod_that_shares_nothing_is_flagged() -> None:
    """The combination that is silent in both directions: nothing refuses the
    attach, the seat works, and it resolves the image's venv while the
    application runs the claim's."""
    (seat,) = seats(hotfixed_pod(mounted=False))

    assert seat.kind is SeatKind.ATTACH
    assert seat.in_hotfixed_pod


def test_the_hotfix_kind_needs_the_layout_as_well_as_the_mount() -> None:
    """A ``--mount`` on a pod nobody hotfixed is somebody mounting a volume, and
    calling it Hotfix mode would claim a provenance that is not there."""
    (seat,) = seats(hotfixed_pod(mounted=True, claim=False, supervisor=False))

    assert seat.kind is SeatKind.ATTACH
    assert not seat.in_hotfixed_pod


def test_a_claim_by_that_name_alone_is_not_the_hotfix_layout() -> None:
    """The half of the predicate a spec-derived read could get wrong. Somebody
    else's volume happening to be called ``podbench-app`` is not evidence that
    podbench deployed anything, and reading only the volume would turn three
    features on against a pod that never carried a hotfix."""
    pod = hotfixed_pod(mounted=True, supervisor=False)

    assert not is_hotfixed(pod)
    assert seats(pod)[0].kind is SeatKind.ATTACH


def test_the_supervisor_alone_is_not_the_hotfix_layout() -> None:
    """And the other half. A container can wrap its entrypoint in a relaunch
    loop for its own reasons; without the claim there is nothing to hotfix."""
    assert not is_hotfixed(hotfixed_pod(mounted=True, claim=False))


def test_the_supervisor_is_read_from_both_of_its_paths() -> None:
    """Not from the entrypoint it wraps, which is the application's and differs
    per deployment, and not from either path alone: a container that merely
    records a child pid, or merely reads a flag file, is not this loop."""
    assert runs_hotfix_supervisor({"args": [hold_loop_args("anything at all")]})
    assert not runs_hotfix_supervisor({"args": ["echo $! > /tmp/podbench-child.pid"]})
    assert not runs_hotfix_supervisor({"args": ["[ -e /tmp/podbench-hold ] || exit"]})
    assert not runs_hotfix_supervisor({})


def test_the_convention_volumes_do_not_make_a_seat_a_hotfix_seat() -> None:
    """``seat_identity_mounts`` mounts the home volume by convention, and a pod
    prepared for podbench has the application mounting it too - so a kind
    derived from "shares a mount with the target" has to discount it, or every
    seat in a cooperating pod reads as ``hotfix``."""
    home = {"name": SEAT_HOME_VOLUME, "mountPath": SEAT_HOME_PATH}
    pod = pod_document(
        uid=1000,
        volumes=[
            {"name": SEAT_HOME_VOLUME, "emptyDir": {}},
            {
                "name": HOTFIX_CLAIM_VOLUME,
                "persistentVolumeClaim": {"claimName": "myapp-podbench"},
            },
        ],
        volume_mounts=[home, HOTFIX_MOUNT],
        args=[hold_loop_args("myapp serve")],
        ephemeral=[
            {
                "name": "podbench-1",
                "targetContainerName": "app",
                "volumeMounts": [home],
            }
        ],
        ephemeral_statuses=[running_status("podbench-1")],
    )

    (seat,) = seats(pod)

    assert seat.kind is SeatKind.ATTACH
    assert seat.in_hotfixed_pod


def test_the_sidecar_is_never_paired_with_an_ephemeral_seat_as_superseded() -> None:
    """``superseded_seats``'s signature - same target, same uid, a gid the later
    seat pins - is now reachable across container kinds, because ``all_seats``
    folds the sidecar in beside anything attached into the same dev pod. The
    correction it detects only ever happens between two ephemeral containers."""
    pod = dev_pod(
        ephemeral=[{"name": "podbench-1", "securityContext": {"runAsUser": 1000}}]
    )

    assert superseded_seats(all_seats(pod)) == {}


def test_a_listing_names_each_seats_kind(tmp_path: Path) -> None:
    text = format_seats(
        PodRef("demo", "demo-podbench"),
        all_seats(
            dev_pod(
                ephemeral=[
                    {"name": "podbench-1", "securityContext": {"runAsUser": 1000}}
                ]
            )
        ),
        directory=tmp_path,
    )

    assert "KIND" in text
    assert "podbench     dev" in text
    assert "podbench-1   attach" in text


def test_a_listing_says_when_a_seat_misses_its_pods_hotfix(tmp_path: Path) -> None:
    text = format_seats(
        PodRef("demo", "target"), seats(hotfixed_pod(mounted=False)), directory=tmp_path
    )

    flowed = " ".join(text.split())
    assert " ".join(UNMOUNTED_HOTFIX_NOTE.split()) in flowed


def test_a_listing_is_silent_about_a_seat_that_shares_the_hotfix(
    tmp_path: Path,
) -> None:
    text = format_seats(
        PodRef("demo", "target"), seats(hotfixed_pod(mounted=True)), directory=tmp_path
    )

    assert "hotfixed" not in text


def test_attach_on_a_dev_pod_reconnects_to_the_sidecar() -> None:
    """Rather than landing an ephemeral seat beside it. In a dev pod the
    workload container is idled and the application runs as a child of the
    sidecar, so the seat that would land there attaches to a container with
    nothing in it - and spends a permanent name doing so."""
    cluster = FakeCluster(dev_pod())

    session = attach(
        kubectl_for("demo", runner=cluster),
        "demo-podbench",
        image="ghcr.io/gilesknap/podbench:test",
        public_key=None,
        probe=False,
    )

    assert session.seat.container == "podbench"
    assert session.reused
    flowed = [" ".join(warning.split()) for warning in session.warnings]
    assert " ".join(DEV_SIDECAR_REUSED_NOTE.format(seat="podbench").split()) in flowed


# -- vscode opens the seat that is already there (issue #141) ---------------


def test_new_lands_an_ephemeral_seat_in_a_dev_pod_after_all() -> None:
    """The preference for the sidecar is a preference, not a refusal: it gives
    up SYS_PTRACE with the root it does not have, so an Observe-mode seat beside
    it is worth a permanent name on a cluster that admits the capability."""
    cluster = FakeCluster(dev_pod())

    session = attach(
        kubectl_for("demo", runner=cluster),
        "demo-podbench",
        image="ghcr.io/gilesknap/podbench:test",
        public_key=None,
        force_new=True,
        probe=False,
    )

    assert session.seat.container == "podbench-1"
    assert not session.reused


def test_reconnecting_to_a_sidecar_points_re_keying_at_the_dev_verb() -> None:
    """`--new` re-keys an ephemeral seat by landing another one. A sidecar's
    authorized_keys is written when the pod is authored, so the same advice
    there sends the reader to land a seat that does not fix it."""
    cluster = FakeCluster(dev_pod())

    session = attach(
        kubectl_for("demo", runner=cluster),
        "demo-podbench",
        image="ghcr.io/gilesknap/podbench:test",
        public_key="ssh-ed25519 AAAA test",
        probe=False,
    )

    keying = [w for w in session.warnings if "authorized_keys" in w]
    assert keying
    assert all("podbench dev --identity" in warning for warning in keying)
    assert not any("needs --new" in warning for warning in keying)


def test_landing_a_seat_names_the_other_two_modes_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Said, not asked. With no seat in the pod there is nothing ambiguous to
    resolve and `attach` is the only one of the three this verb could carry
    out, so a prompt's default would have been its only actionable answer."""
    cluster = FakeCluster(pod_document(uid=1000))

    main(
        vscode_argv(tmp_path),
        runner=cluster,
        which=lambda name: f"/usr/bin/{name}",
    )

    flowed = " ".join(capsys.readouterr().out.split())
    assert " ".join(OTHER_MODES_NOTE.split()) in flowed


def test_a_reconnect_does_not_name_the_other_modes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The mode was settled on whatever day the seat was landed, and it is
    already reported - by the KIND column, and by the reconnect's own line."""
    cluster = FakeCluster(
        pod_document(
            uid=1000,
            ephemeral=[{"name": "podbench-1", "securityContext": {"runAsUser": 1000}}],
            ephemeral_statuses=[running_status("podbench-1")],
        )
    )

    main(
        vscode_argv(tmp_path),
        runner=cluster,
        which=lambda name: f"/usr/bin/{name}",
    )

    flowed = " ".join(capsys.readouterr().out.split())
    assert " ".join(OTHER_MODES_NOTE.split()) not in flowed
