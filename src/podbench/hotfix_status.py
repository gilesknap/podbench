"""Status and retirement for workload-scoped hotfix claims."""

from __future__ import annotations

from .hotfix_core import MANIFEST, HotfixError, claim_name, exec_target, resolve_target
from .kubectl import Kubectl
from .model import HOTFIX_APP_PATH, HOTFIX_CLAIM_VOLUME, HOTFIX_HOLD_PATH, as_dict


def _containers(pod: dict) -> list[dict]:
    value = as_dict(pod.get("spec")).get("containers", [])
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def hotfix_container(pod: dict) -> str | None:
    for container in _containers(pod):
        mounts = container.get("volumeMounts", [])
        if not isinstance(mounts, list):
            continue
        if any(
            isinstance(mount, dict)
            and mount.get("name") == HOTFIX_CLAIM_VOLUME
            and mount.get("mountPath") == HOTFIX_APP_PATH
            for mount in mounts
        ):
            name = container.get("name")
            return name if isinstance(name, str) else None
    return None


def status(kube: Kubectl) -> tuple[list[str], bool]:
    lines: list[str] = []
    healthy = True
    for pod in kube.list_pods():
        container = hotfix_container(pod)
        name = as_dict(pod.get("metadata")).get("name")
        if not container or not isinstance(name, str):
            continue
        target, _ = resolve_target(kube, name, container)
        manifest = exec_target(kube, target, f"cat {MANIFEST}", check=False)
        held = (
            exec_target(
                kube, target, f"test -e {HOTFIX_HOLD_PATH}", check=False
            ).returncode
            == 0
        )
        state = "initialized" if manifest.returncode == 0 else "empty"
        if manifest.returncode:
            healthy = False
        if held:
            state += ", HELD"
            healthy = False
        lines.append(f"{name}/{container}: {claim_name(pod) or '?'} ({state})")
    return lines or [f"no hotfixes in namespace {kube.namespace}"], healthy


def _mounting_pods(kube: Kubectl, claim: str) -> list[str]:
    mounted: list[str] = []
    for pod in kube.list_pods():
        if claim_name(pod) == claim:
            name = as_dict(pod.get("metadata")).get("name")
            if isinstance(name, str):
                mounted.append(name)
    return mounted


def retire(
    kube: Kubectl, target_or_claim: str, *, delete_claim: bool = False
) -> tuple[list[str], bool]:
    pod_name = target_or_claim.removeprefix("pod/")
    probe = kube.run("get", "pod", pod_name, "-o", "json", check=False)
    claim = target_or_claim.removeprefix("pvc/")
    if probe.returncode == 0:
        pod = __import__("json").loads(probe.stdout)
        claim = claim_name(pod) or ""
        if hotfix_container(pod):
            return (
                [
                    f"{pod_name} is still wired for hotfix",
                    "remove the hotfix values and redeploy before deleting its claim",
                ],
                False,
            )
        if not claim:
            raise HotfixError("the pod is unwired; pass the PVC name to retire")
    if not claim:
        raise HotfixError("no PVC name was provided")
    holders = _mounting_pods(kube, claim)
    if holders:
        return ([f"PVC {claim} is still mounted by {', '.join(holders)}"], False)
    exists = kube.run("get", "pvc", claim, "-o", "name", check=False).returncode == 0
    if not exists:
        return ([f"PVC {claim} is gone; retirement is complete"], True)
    if not delete_claim:
        return (
            [
                f"PVC {claim} is unmounted and ready to delete",
                f"run hotfix retire {claim} --delete-claim",
            ],
            False,
        )
    kube.run("delete", "pvc", claim)
    return ([f"deleted PVC {claim}"], True)
