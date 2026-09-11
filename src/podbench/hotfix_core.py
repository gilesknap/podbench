"""Shared hotfix state and target discovery."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from .kubectl import Kubectl
from .launcher import application_mount, target_container_name
from .model import HOTFIX_APP_PATH, HOTFIX_CLAIM_VOLUME, PodRef, as_dict

MANIFEST = f"{HOTFIX_APP_PATH}/.podbench-hotfix.json"


class HotfixError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    pod: PodRef
    container: str
    image: str
    image_digest: str
    claim: str | None


def _items(value: object) -> list[dict[str, Any]]:
    return (
        [cast(dict[str, Any], item) for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def load_json(text: str) -> dict[str, Any]:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise HotfixError("expected a JSON object")
    return cast(dict[str, Any], value)


def find_container(pod: Mapping[str, Any], name: str) -> dict[str, Any]:
    for container in _items(as_dict(pod.get("spec")).get("containers")):
        if container.get("name") == name:
            return container
    raise HotfixError(f"container {name!r} is not in the pod")


def claim_name(pod: Mapping[str, Any]) -> str | None:
    spec = as_dict(pod.get("spec"))
    for volume in _items(spec.get("volumes")):
        if volume.get("name") != HOTFIX_CLAIM_VOLUME:
            continue
        name = as_dict(volume.get("persistentVolumeClaim")).get("claimName")
        return name if isinstance(name, str) else None
    return None


def _replicas(kube: Kubectl, pod: Mapping[str, Any]) -> int | None:
    owners = _items(as_dict(pod.get("metadata")).get("ownerReferences"))
    owner = next((item for item in owners if item.get("controller") is True), None)
    if not owner:
        return 1
    kind = str(owner.get("kind", "")).lower()
    name = str(owner.get("name", ""))
    if kind == "replicaset":
        replica_set = load_json(
            kube.run("get", "replicaset", name, "-o", "json").stdout
        )
        rs_owners = _items(as_dict(replica_set.get("metadata")).get("ownerReferences"))
        parent = next(
            (item for item in rs_owners if item.get("controller") is True), None
        )
        if parent and str(parent.get("kind", "")).lower() == "deployment":
            kind, name = "deployment", str(parent.get("name", ""))
        else:
            return as_dict(replica_set.get("spec")).get("replicas")
    if kind not in ("deployment", "statefulset"):
        return None
    workload = load_json(kube.run("get", kind, name, "-o", "json").stdout)
    replicas = as_dict(workload.get("spec")).get("replicas", 1)
    return replicas if isinstance(replicas, int) else None


def resolve_target(
    kube: Kubectl,
    pod_name: str,
    container: str | None = None,
    *,
    require_wiring: bool = True,
) -> tuple[Target, dict[str, Any]]:
    name = pod_name.removeprefix("pod/")
    pod = kube.get_pod(name)
    chosen = target_container_name(pod, container)
    spec = find_container(pod, chosen)
    replicas = _replicas(kube, pod)
    if replicas != 1:
        detail = "unknown" if replicas is None else str(replicas)
        raise HotfixError(
            f"hotfix requires exactly one replica; this workload has {detail}"
        )
    mount = application_mount(pod, chosen, HOTFIX_CLAIM_VOLUME)
    if require_wiring and mount.get("mountPath") != HOTFIX_APP_PATH:
        raise HotfixError(
            f"{chosen} must mount volume {HOTFIX_CLAIM_VOLUME} at {HOTFIX_APP_PATH}; run hotfix values and redeploy"
        )
    statuses = _items(as_dict(pod.get("status")).get("containerStatuses"))
    status = next((item for item in statuses if item.get("name") == chosen), {})
    target = Target(
        PodRef(kube.namespace, name),
        chosen,
        str(spec.get("image", "")),
        str(status.get("imageID", "")),
        claim_name(pod),
    )
    return target, pod


def exec_target(
    kube: Kubectl,
    target: Target,
    command: str,
    *,
    check: bool = True,
    timeout: float | None = 30.0,
):
    return kube.exec_(
        target.pod.name,
        ["bash", "-c", command],
        container=target.container,
        check=check,
        timeout=timeout,
    )
