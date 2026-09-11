"""Degraded-only attach for the prototype."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .kubectl import Kubectl, Runner, run_subprocess
from .model import DEFAULT_IMAGE, HOTFIX_CLAIM_VOLUME, PodRef, as_dict

CONTAINER_BASE = "podbench"
DEFAULT_PULL_POLICY = "IfNotPresent"


class LauncherError(RuntimeError):
    pass


@dataclass(frozen=True)
class SeatInfo:
    name: str
    target: str | None


@dataclass(frozen=True)
class SeatRef:
    pod: PodRef
    container: str


@dataclass(frozen=True)
class Session:
    seat: SeatRef
    target: str
    reused: bool
    uid: int | None
    gid: int | None
    warnings: tuple[str, ...] = ()


def _items(value: object) -> list[dict[str, Any]]:
    return [as_dict(item) for item in value] if isinstance(value, list) else []


def _named(items: object, name: str) -> dict[str, Any] | None:
    return next((item for item in _items(items) if item.get("name") == name), None)


def resolve_pod_name(reference: str) -> str:
    name = reference.removeprefix("pod/").strip()
    if not name or "/" in name:
        raise LauncherError(f"expected a pod name, got {reference!r}")
    return name


def target_container_name(pod: Mapping[str, Any], requested: str | None = None) -> str:
    containers = _items(as_dict(pod.get("spec")).get("containers"))
    if not containers:
        raise LauncherError("the pod has no application containers")
    if requested is None:
        return str(containers[0]["name"])
    if _named(containers, requested) is None:
        choices = ", ".join(str(item.get("name")) for item in containers)
        raise LauncherError(f"container {requested!r} is not in the pod ({choices})")
    return requested


def target_uid_gid(
    pod: Mapping[str, Any], container: str
) -> tuple[int | None, int | None]:
    """Read IDs from the container, pod defaults, then node-reported status."""
    spec = as_dict(pod.get("spec"))
    target = _named(spec.get("containers"), container) or {}
    pod_security = as_dict(spec.get("securityContext"))
    security = {**pod_security, **as_dict(target.get("securityContext"))}
    uid = security.get("runAsUser")
    gid = security.get("runAsGroup")
    status = as_dict(pod.get("status"))
    reported = _named(status.get("containerStatuses"), container) or {}
    linux = as_dict(as_dict(reported.get("user")).get("linux"))
    if not isinstance(uid, int):
        uid = linux.get("uid")
    if not isinstance(gid, int):
        gid = linux.get("gid")
    return (
        uid if isinstance(uid, int) else None,
        gid if isinstance(gid, int) else None,
    )


def application_mount(
    pod: Mapping[str, Any], container: str, volume: str
) -> dict[str, Any]:
    target = _named(as_dict(pod.get("spec")).get("containers"), container) or {}
    return _named(target.get("volumeMounts"), volume) or {}


def declared_volumes(pod: Mapping[str, Any]) -> set[str]:
    return {
        str(item["name"])
        for item in _items(as_dict(pod.get("spec")).get("volumes"))
        if isinstance(item.get("name"), str)
    }


def runs_hotfix_supervisor(container: Mapping[str, Any]) -> bool:
    words = container.get("args", [])
    text = "\n".join(str(word) for word in words) if isinstance(words, list) else ""
    return "/tmp/podbench-child.pid" in text and "/tmp/podbench-hold" in text


def running_seat(pod: Mapping[str, Any]) -> SeatInfo | None:
    spec = as_dict(pod.get("spec"))
    status = as_dict(pod.get("status"))
    running = {
        str(item.get("name"))
        for item in _items(status.get("ephemeralContainerStatuses"))
        if as_dict(as_dict(item.get("state")).get("running"))
    }
    seats = [
        item
        for item in _items(spec.get("ephemeralContainers"))
        if str(item.get("name", "")).startswith(f"{CONTAINER_BASE}-")
        and item.get("name") in running
    ]
    if not seats:
        return None
    seat = seats[-1]
    target = seat.get("targetContainerName")
    return SeatInfo(str(seat["name"]), target if isinstance(target, str) else None)


def _next_seat_name(pod: Mapping[str, Any]) -> str:
    used = {
        str(item.get("name"))
        for key in ("containers", "initContainers", "ephemeralContainers")
        for item in _items(as_dict(pod.get("spec")).get(key))
    }
    number = 1
    while f"{CONTAINER_BASE}-{number}" in used:
        number += 1
    return f"{CONTAINER_BASE}-{number}"


def _target_seccomp(pod: Mapping[str, Any], container: str) -> dict[str, Any] | None:
    spec = as_dict(pod.get("spec"))
    target = _named(spec.get("containers"), container) or {}
    profile = as_dict(as_dict(target.get("securityContext")).get("seccompProfile"))
    if not profile:
        profile = as_dict(as_dict(spec.get("securityContext")).get("seccompProfile"))
    return profile or None


def _seat_spec(
    pod: Mapping[str, Any],
    *,
    name: str,
    target: str,
    image: str,
    uid: int | None,
    gid: int | None,
    pull: str,
) -> dict[str, Any]:
    security: dict[str, Any] = {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "privileged": False,
        "runAsNonRoot": uid != 0,
    }
    if uid is not None:
        security["runAsUser"] = uid
        security["runAsNonRoot"] = uid != 0
    if gid is not None:
        security["runAsGroup"] = gid
    if profile := _target_seccomp(pod, target):
        security["seccompProfile"] = profile

    mount = application_mount(pod, target, HOTFIX_CLAIM_VOLUME)
    mounts = (
        [mount]
        if mount and "subPath" not in mount and "subPathExpr" not in mount
        else []
    )
    spec: dict[str, Any] = {
        "name": name,
        "image": image,
        "imagePullPolicy": pull,
        "command": ["sleep", "infinity"],
        "targetContainerName": target,
        "terminationMessagePolicy": "File",
        "securityContext": security,
        "env": [
            {"name": "HOME", "value": "/tmp"},
            {"name": "UV_CACHE_DIR", "value": "/tmp/uv-cache"},
        ],
    }
    if mounts:
        spec["volumeMounts"] = mounts
    return spec


def attach(
    kubectl: Kubectl,
    pod_reference: str,
    *,
    target: str | None = None,
    image: str = DEFAULT_IMAGE,
    target_uid: int | None = None,
    target_gid: int | None = None,
    force_new: bool = False,
    pull_policy: str = DEFAULT_PULL_POLICY,
    timeout: float = 120.0,
    **_: object,
) -> Session:
    pod_name = resolve_pod_name(pod_reference)
    pod = kubectl.get_pod(pod_name)
    target = target_container_name(pod, target)
    existing = running_seat(pod)
    uid, gid = target_uid_gid(pod, target)
    uid = target_uid if target_uid is not None else uid
    gid = target_gid if target_gid is not None else gid
    warnings: list[str] = []
    if uid is None or gid is None:
        warnings.append(
            "the target identity is incomplete; "
            "degraded attach is being attempted anyway"
        )
    if uid == 0:
        warnings.append(
            "the target runs as root; "
            "this capless degraded seat may not be able to attach"
        )
    if existing is not None and existing.target == target and not force_new:
        return Session(
            SeatRef(PodRef(kubectl.namespace, pod_name), existing.name),
            target,
            True,
            uid,
            gid,
            tuple(warnings),
        )

    name = _next_seat_name(pod)
    kubectl.add_ephemeral_container(
        pod_name,
        _seat_spec(
            pod,
            name=name,
            target=target,
            image=image,
            uid=uid,
            gid=gid,
            pull=pull_policy,
        ),
    )
    kubectl.wait_for_ephemeral_container(pod_name, name, timeout=timeout)
    return Session(
        SeatRef(PodRef(kubectl.namespace, pod_name), name),
        target,
        False,
        uid,
        gid,
        tuple(warnings),
    )


def kubectl_for(
    namespace: str | None,
    *,
    context: str | None = None,
    binary: str = "kubectl",
    runner: Runner | None = None,
) -> Kubectl:
    if namespace is None:
        command = [binary]
        if context:
            command += ["--context", context]
        command += ["config", "view", "--minify", "-o", "jsonpath={..namespace}"]
        result = (runner or run_subprocess)(command)
        if result.returncode:
            raise LauncherError(
                result.stderr.strip() or "could not read the current namespace"
            )
        namespace = result.stdout.strip() or "default"
    return Kubectl(namespace, context=context, binary=binary, runner=runner)
