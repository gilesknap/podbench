"""Pure functions that author Kubernetes specs.

Nothing here touches the cluster: every function takes JSON in and gives JSON
out, so the rules that the phase-0 spikes paid for can be asserted in unit
tests rather than discovered in a namespace.

Two of those rules are hard invariants rather than defaults:

* ``CAP_SYS_PTRACE`` on a container whose ``runAsUser`` is not 0 is a **silent
  no-op** — the capability lands in the bounding set only and ``CapEff`` stays
  zero, so the container looks privileged and behaves unprivileged (report
  3.10). Authoring that combination raises instead.
* A dev pod does **not** carry its origin's Service-selector labels unless the
  caller asks. Silently joining a production Service is a foot-cannon (report
  4.4).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any, cast

from .model import TARGET_CID_ENV, Rung, as_dict

__all__ = [
    "AGENT_COMMAND",
    "CONTROLLER_LABELS",
    "DEVPOD_LABEL",
    "ORIGIN_ANNOTATION",
    "SERVER_OWNED_METADATA",
    "WORKSPACE_MOUNT_PATH",
    "WORKSPACE_VOLUME",
    "InvalidSpecError",
    "container_id",
    "cutover_selector_patch",
    "dev_pod_spec",
    "devpod_selector",
    "ephemeral_container_spec",
    "runs_as_non_root",
    "service_selector_patch",
    "target_uid_gid",
    "validate_security_context",
]

DEVPOD_LABEL = "podbench.dev/devpod"
"""Marks an authored dev pod. Also the selector a cutover flips a Service to."""

ORIGIN_ANNOTATION = "podbench.dev/origin"
"""Records the pod a dev pod was cloned from."""

CONTROLLER_LABELS: tuple[str, ...] = (
    "pod-template-hash",
    "controller-revision-hash",
    "controller-uid",
    "batch.kubernetes.io/controller-uid",
    "batch.kubernetes.io/job-name",
    "job-name",
    "statefulset.kubernetes.io/pod-name",
)
"""Labels that make a controller adopt the clone, and so must never be copied.

Keeping the Service-selector labels while dropping these puts the dev pod in the
endpointslice without making it a ReplicaSet member. Keeping ``pod-template-hash``
instead would give a ``replicas: 1`` ReplicaSet two matching pods, and one of
them would be reaped (report 3.5).
"""

SERVER_OWNED_METADATA: tuple[str, ...] = (
    "uid",
    "resourceVersion",
    "creationTimestamp",
    "generation",
    "managedFields",
    "ownerReferences",
    "selfLink",
    "generateName",
    "deletionTimestamp",
    "deletionGracePeriodSeconds",
    "finalizers",
)
"""Metadata the API server owns; a manifest carrying it back is rejected."""

WORKSPACE_VOLUME = "podbench-workspace"
WORKSPACE_MOUNT_PATH = "/workspace"

AGENT_COMMAND: tuple[str, ...] = ("podbench", "agent")
"""What a podbench container is given to run.

Not ``sleep infinity``: the sshd config, the authorized keys and the host key
are written by the agent at start-up, and the ProxyCommand's
``sshd -i -f <config>`` fails without them. It is long-running, so it still
satisfies the rule that a debug container is never given a short-lived command
— one that exits reaches ``Completed`` and burns its name for the pod's
lifetime (report 4.2).
"""

_IDLE_COMMAND: tuple[str, ...] = ("sleep", "infinity")
"""What the *target* container is given in a dev pod, so it stops serving."""
_TARGET_PROBES: tuple[str, ...] = (
    "readinessProbe",
    "livenessProbe",
    "startupProbe",
    "lifecycle",
)

_DEFAULT_SIDECAR_RESOURCES: dict[str, dict[str, str]] = {
    "requests": {"cpu": "200m", "memory": "512Mi"},
    "limits": {"cpu": "2", "memory": "3Gi"},
}


class InvalidSpecError(ValueError):
    """A spec podbench refuses to author, because it would not do what it says."""


def validate_security_context(security_context: Mapping[str, Any]) -> None:
    """Reject a securityContext whose ``SYS_PTRACE`` would be a silent no-op.

    The kernel grants capabilities to a non-root uid only through the ambient
    set, which no CRI populates, so ``capabilities.add: [SYS_PTRACE]`` with a
    non-zero ``runAsUser`` produces ``CapEff: 0000000000000000`` — measured, on
    a container the API server happily admitted (report 3.10). Shipping that
    would tell the user they have live attach when every ptrace call will return
    a bare ``EPERM``.

    An absent ``runAsUser`` is refused too: the effective uid then comes from
    the image or the pod-level securityContext, and neither is knowable here.
    """
    capabilities = as_dict(security_context.get("capabilities"))
    added = capabilities.get("add")
    if not isinstance(added, list) or "SYS_PTRACE" not in cast(list[Any], added):
        return
    run_as_user = security_context.get("runAsUser")
    if run_as_user != 0:
        raise InvalidSpecError(
            "SYS_PTRACE requires runAsUser: 0 — a capability added to a "
            f"container running as {run_as_user!r} reaches the bounding set "
            "only, leaving CapEff: 0 and every ptrace call failing with EPERM. "
            "Use the degraded rung (the target's own uid, no capabilities) "
            "instead."
        )


def ephemeral_container_spec(
    *,
    name: str,
    image: str,
    rung: Rung,
    target_container: str | None = None,
    target_container_id: str | None = None,
    target_uid: int | None = None,
    target_gid: int | None = None,
    command: Sequence[str] = AGENT_COMMAND,
    env: Mapping[str, str] | None = None,
    volume_mounts: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Author one ephemeral container for a rung of the capability ladder.

    ``command`` must be long-running: an ephemeral container that exits reaches
    ``Completed`` and its name is then unusable for the pod's lifetime (report
    4.2). It defaults to :data:`AGENT_COMMAND` rather than an idle sleep because
    the transport's server-side files do not exist until the agent has written
    them.

    ``target_container_id`` is the target's ``containerID`` with the runtime
    scheme stripped. It is passed through as ``PODBENCH_TARGET_CID`` because the
    only attribution of a process to its container that stays correct under
    ``shareProcessNamespace`` — and with a second podbench session attached — is
    a substring match of that id against ``/proc/<pid>/cgroup`` (report 3.15).

    Ephemeral containers carry no ``resources``: the field is rejected, and the
    container is confined by the pod's cgroup regardless (report 3.9).
    """
    spec: dict[str, Any] = {
        "name": name,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": list(command),
        "terminationMessagePolicy": "File",
        "securityContext": _rung_security_context(rung, target_uid, target_gid),
    }
    if target_container is not None:
        spec["targetContainerName"] = target_container

    environment = dict(env or {})
    if target_container_id is not None:
        environment.setdefault(TARGET_CID_ENV, target_container_id)
    if environment:
        spec["env"] = [
            {"name": key, "value": value} for key, value in sorted(environment.items())
        ]

    if volume_mounts:
        # Ephemeral containers may mount the pod's *existing* volumes, which is
        # the only practical way to hand a toolchain to a non-root debug
        # container that cannot apt-get (report 3.19).
        spec["volumeMounts"] = [dict(mount) for mount in volume_mounts]

    validate_security_context(as_dict(spec["securityContext"]))
    return spec


def _rung_security_context(
    rung: Rung, target_uid: int | None, target_gid: int | None
) -> dict[str, Any]:
    if rung is Rung.FULL:
        if target_uid not in (None, 0):
            raise InvalidSpecError(
                f"the full rung runs as root; it cannot also run as uid "
                f"{target_uid}. Use Rung.DEGRADED for the target's own uid."
            )
        return {"runAsUser": 0, "capabilities": {"add": ["SYS_PTRACE"]}}

    restricted: dict[str, Any] = {
        "capabilities": {"drop": ["ALL"]},
        "allowPrivilegeEscalation": False,
        "seccompProfile": {"type": "RuntimeDefault"},
        "runAsNonRoot": True,
    }
    if rung is Rung.SEAT:
        # Whatever the cluster will admit. The seat needs nothing from the
        # target, so it pins no uid of its own and the image's user will do; a
        # non-zero target uid is honoured when the caller offers one, because
        # sharing the target's uid costs nothing and buys the /proc reads.
        #
        # A target uid of 0 is dropped rather than honoured. This is exactly the
        # case the ladder reaches SEAT for — DEGRADED refuses a root target — and
        # runAsNonRoot: true beside runAsUser: 0 is admitted by the API server
        # and then refused by the kubelet, asynchronously, with
        # CreateContainerConfigError (report 3.18). The whole point of this rung
        # is to be admissible.
        if target_uid:
            restricted["runAsUser"] = target_uid
        # The gid is dropped alongside the uid rather than on its own merit. It
        # is admissible by itself — restricted PSS does not constrain
        # runAsGroup — but inheriting the target's group while deliberately not
        # inheriting its user describes a process that does not exist, and the
        # /proc reads a shared gid might buy are gated on the uid anyway.
        if target_uid and target_gid is not None:
            restricted["runAsGroup"] = target_gid
        return restricted

    if target_uid is None:
        raise InvalidSpecError(
            "the degraded rung must run as the target's own uid — discover it "
            "from the target container's securityContext or /proc/<pid>/status. "
            "Defaulting to root would cost the sysroot, maps, environ and exe, "
            "which is the entire value of this rung."
        )
    if target_uid == 0:
        raise InvalidSpecError(
            "the target runs as root, so the degraded rung (runAsNonRoot: true "
            "at the target's uid) cannot be authored. Use Rung.FULL if the "
            "namespace admits SYS_PTRACE, otherwise Rung.SEAT."
        )
    restricted["runAsUser"] = target_uid
    if target_gid is not None:
        restricted["runAsGroup"] = target_gid
    return restricted


def dev_pod_spec(
    origin_pod_json: Mapping[str, Any],
    *,
    name: str,
    target_container: str,
    image: str,
    target_port: int,
    take_traffic: bool = False,
    sidecar_name: str = "podbench",
    sidecar_ptrace: bool = True,
    workspace_size: str = "4Gi",
    resources: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Author Iterate mode's sacrificial clone of ``origin_pod_json``.

    The launcher authors this rather than shelling out to ``kubectl debug
    --copy-to``, which strips every label and annotation — leaving the clone
    invisible to the Service, so the headline "edit code, curl the Service, see
    your change" demo cannot work at all — and which offers no way to give the
    debug container ``resources`` or a workspace volume, and no ``--dry-run`` to
    preview any of it (report 3.5).

    ``take_traffic`` is off by default. With it off the dev pod carries only its
    podbench label and receives nothing until a cutover is made explicitly.

    The readiness probe on the *podbench* container is not optional. Without it
    a probe-less dev pod is Ready the instant its containers start and joins the
    endpointslice while nothing is listening, blackholing about half the
    requests; with it, Service membership tracks the inner loop in both
    directions, joining when the relaunched process binds and dropping out
    within ~6 s when it dies (report 3.6).

    The origin pod is not modified.
    """
    pod = copy.deepcopy(dict(origin_pod_json))
    metadata = as_dict(pod.get("metadata"))
    origin_name = metadata.get("name")
    spec = as_dict(pod.get("spec"))
    if not spec:
        raise InvalidSpecError("origin pod JSON has no spec")

    pod["apiVersion"] = "v1"
    pod["kind"] = "Pod"
    pod.pop("status", None)

    for key in SERVER_OWNED_METADATA:
        metadata.pop(key, None)
    metadata["name"] = name

    labels = as_dict(metadata.get("labels")) if take_traffic else {}
    for key in CONTROLLER_LABELS:
        labels.pop(key, None)
    labels[DEVPOD_LABEL] = "true"
    metadata["labels"] = labels
    # The origin's annotations are dropped rather than copied: they carry
    # last-applied-configuration and sidecar-injection directives that would be
    # untrue, or actively harmful, on an orphan copy.
    metadata["annotations"] = (
        {ORIGIN_ANNOTATION: origin_name} if isinstance(origin_name, str) else {}
    )
    pod["metadata"] = metadata

    spec.pop("nodeName", None)
    # A crashed dev pod must leave a corpse to inspect rather than looping.
    spec["restartPolicy"] = "Never"
    # gdb, ss -lntp and /proc/<pid>/root all depend on it.
    spec["shareProcessNamespace"] = True

    containers = spec.get("containers")
    if not isinstance(containers, list):
        raise InvalidSpecError("origin pod JSON has no containers")
    container_list = cast(list[Any], containers)
    names = {
        cast(dict[str, Any], c).get("name")
        for c in container_list
        if isinstance(c, dict)
    }
    if sidecar_name in names:
        raise InvalidSpecError(
            f"the origin pod already has a container named {sidecar_name!r}"
        )
    if target_container not in names:
        raise InvalidSpecError(
            f"container {target_container!r} not found in pod "
            f"{origin_name!r} (has {sorted(str(n) for n in names)})"
        )

    for entry in container_list:
        if not isinstance(entry, dict):
            continue
        container = cast(dict[str, Any], entry)
        if container.get("name") != target_container:
            continue
        container["command"] = list(_IDLE_COMMAND)
        container.pop("args", None)
        # Probes and lifecycle hooks against an idled container would fail
        # forever and, for a liveness probe, kill the pod.
        for probe in _TARGET_PROBES:
            container.pop(probe, None)

    volumes = spec.get("volumes")
    volume_list: list[Any] = (
        cast(list[Any], volumes) if isinstance(volumes, list) else []
    )
    if any(
        isinstance(v, dict) and cast(dict[str, Any], v).get("name") == WORKSPACE_VOLUME
        for v in volume_list
    ):
        raise InvalidSpecError(
            f"the origin pod already has a volume named {WORKSPACE_VOLUME!r}"
        )
    volume_list.append(
        {"name": WORKSPACE_VOLUME, "emptyDir": {"sizeLimit": workspace_size}}
    )
    spec["volumes"] = volume_list

    container_list.append(
        _sidecar(
            name=sidecar_name,
            image=image,
            target_container=target_container,
            target_port=target_port,
            ptrace=sidecar_ptrace,
            resources=resources,
            env=env,
        )
    )
    spec["containers"] = container_list
    pod["spec"] = spec
    return pod


def _sidecar(
    *,
    name: str,
    image: str,
    target_container: str,
    target_port: int,
    ptrace: bool,
    resources: Mapping[str, Any] | None,
    env: Mapping[str, str] | None,
) -> dict[str, Any]:
    if ptrace:
        # runAsUser must be explicit even though this is a normal container: the
        # origin pod's pod-level securityContext may pin a non-root uid, which
        # would apply here too and silently void the capability. runAsNonRoot is
        # overridden at container level for the same reason — a pod-level
        # runAsNonRoot: true otherwise makes the kubelet reject this container
        # asynchronously with CreateContainerConfigError (report 3.18).
        security_context: dict[str, Any] = {
            "runAsUser": 0,
            "runAsNonRoot": False,
            "capabilities": {"add": ["SYS_PTRACE"]},
        }
    else:
        security_context = {
            "capabilities": {"drop": ["ALL"]},
            "allowPrivilegeEscalation": False,
            "seccompProfile": {"type": "RuntimeDefault"},
            "runAsNonRoot": True,
        }
    validate_security_context(security_context)

    environment = {
        "HOME": WORKSPACE_MOUNT_PATH,
        "PODBENCH_TARGET": target_container,
        **dict(env or {}),
    }
    return {
        "name": name,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        # The sidecar is this pod's ssh endpoint too, so it runs the agent for
        # the same reason the ephemeral container does: nothing writes the sshd
        # config, the authorized keys or the host key otherwise.
        "command": list(AGENT_COMMAND),
        "workingDir": WORKSPACE_MOUNT_PATH,
        # HOME lives on the workspace volume so uv's toolchains, caches and
        # venvs land there rather than in the container's writable layer.
        "env": [
            {"name": key, "value": value} for key, value in sorted(environment.items())
        ],
        "volumeMounts": [{"name": WORKSPACE_VOLUME, "mountPath": WORKSPACE_MOUNT_PATH}],
        "resources": copy.deepcopy(dict(resources or _DEFAULT_SIDECAR_RESOURCES)),
        "securityContext": security_context,
        "readinessProbe": {
            "tcpSocket": {"port": target_port},
            "periodSeconds": 2,
            "failureThreshold": 1,
        },
    }


def devpod_selector() -> dict[str, str]:
    """The label selector that matches only podbench dev pods."""
    return {DEVPOD_LABEL: "true"}


def service_selector_patch(selector: Mapping[str, str]) -> list[dict[str, Any]]:
    """A JSON patch replacing a Service's selector.

    It must be a *replace*, and therefore a JSON patch: ``kubectl patch
    --type=merge`` unions map keys instead, so restoring ``{app: foo}`` over
    ``{app: foo, podbench.dev/devpod: "true"}`` leaves both keys in place and
    silently drops the original pod out of the endpointslice (report 3.5).

    >>> service_selector_patch({"app": "demo"})
    [{'op': 'replace', 'path': '/spec/selector', 'value': {'app': 'demo'}}]
    """
    return [{"op": "replace", "path": "/spec/selector", "value": dict(selector)}]


def cutover_selector_patch() -> list[dict[str, Any]]:
    """A JSON patch pointing a Service exclusively at the dev pod.

    Record the Service's original selector first: the restore has to be exact.
    """
    return service_selector_patch(devpod_selector())


def target_uid_gid(
    pod_json: Mapping[str, Any], container: str
) -> tuple[int | None, int | None]:
    """The uid/gid the named container runs as, container settings winning.

    ``None`` means the manifest does not say, and the effective id comes from
    the image — which only the running process knows, so the caller must read
    ``/proc/<pid>/status`` instead of assuming root.
    """
    spec = as_dict(pod_json.get("spec"))
    pod_context = as_dict(spec.get("securityContext"))
    container_context = as_dict(_find_container(spec, container).get("securityContext"))

    def pick(key: str) -> int | None:
        for source in (container_context, pod_context):
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    return pick("runAsUser"), pick("runAsGroup")


def runs_as_non_root(pod_json: Mapping[str, Any], container: str) -> bool:
    """Whether ``runAsNonRoot`` is set on the container or its pod.

    A pre-flight read of this lets the launcher skip the full rung outright.
    Otherwise the API server accepts a root debug container and the kubelet
    refuses it seconds later with ``CreateContainerConfigError: container's
    runAsUser breaks non-root policy`` (report 3.18).
    """
    spec = as_dict(pod_json.get("spec"))
    container_context = as_dict(_find_container(spec, container).get("securityContext"))
    value = container_context.get("runAsNonRoot")
    if isinstance(value, bool):
        return value
    return as_dict(spec.get("securityContext")).get("runAsNonRoot") is True


def container_id(pod_json: Mapping[str, Any], container: str) -> str | None:
    """The named container's runtime id, with the ``runtime://`` scheme stripped."""
    statuses = as_dict(pod_json.get("status")).get("containerStatuses")
    if not isinstance(statuses, list):
        return None
    for entry in cast(list[Any], statuses):
        if not isinstance(entry, dict):
            continue
        status = cast(dict[str, Any], entry)
        if status.get("name") != container:
            continue
        value = status.get("containerID")
        if isinstance(value, str):
            return value.split("://", 1)[-1]
    return None


def _find_container(spec: Mapping[str, Any], name: str) -> dict[str, Any]:
    for key in ("containers", "initContainers", "ephemeralContainers"):
        entries = spec.get(key)
        if not isinstance(entries, list):
            continue
        for entry in cast(list[Any], entries):
            if (
                isinstance(entry, dict)
                and cast(dict[str, Any], entry).get("name") == name
            ):
                return cast(dict[str, Any], entry)
    return {}
