"""Initialize and restart a hotfix checkout."""

from __future__ import annotations

import json
import shlex

from .cli import console
from .hotfix_core import (
    MANIFEST,
    HotfixError,
    Target,
    exec_target,
    load_json,
    resolve_target,
)
from .kubectl import Kubectl
from .launcher import attach, running_seat
from .model import HOTFIX_APP_PATH, HOTFIX_CHILD_PID_PATH, HOTFIX_HOLD_PATH


def _seat(kube: Kubectl, target: Target, pod: dict) -> str:
    current = running_seat(pod)
    if current:
        return current.name
    console.print(
        f"landing a degraded seat in {target.pod.name}",
        style="yellow",
    )
    return attach(kube, target.pod.name, target=target.container).seat.container


def _seat_run(
    kube: Kubectl,
    target: Target,
    seat: str,
    command: str,
    *,
    check: bool = True,
    timeout: float | None = 600.0,
):
    return kube.exec_(
        target.pod.name,
        ["bash", "-c", command],
        container=seat,
        check=check,
        timeout=timeout,
    )


def init(
    kube: Kubectl,
    pod_name: str,
    repo: str,
    *,
    ref: str | None = None,
    container: str | None = None,
) -> list[str]:
    target, pod = resolve_target(kube, pod_name, container)
    if not target.claim:
        raise HotfixError("the podbench volume does not name a PVC")
    if exec_target(
        kube, target, f"test -s {HOTFIX_CHILD_PID_PATH}", check=False
    ).returncode:
        raise HotfixError(
            "the hotfix supervisor is not running; deploy hotfix values first"
        )
    seat = _seat(kube, target, pod)
    if (
        _seat_run(kube, target, seat, f"test -f {MANIFEST}", check=False).returncode
        == 0
    ):
        raise HotfixError(f"{target.claim} is already initialized")
    branch = f"--branch {shlex.quote(ref)} " if ref else ""
    script = "\n".join(
        [
            "set -euo pipefail",
            f"find {HOTFIX_APP_PATH} -mindepth 1 -delete",
            f"git clone {branch}{shlex.quote(repo)} {HOTFIX_APP_PATH}",
            f"cd {HOTFIX_APP_PATH}",
            'mkdir -p "$HOME"',
            f"git config --global --add safe.directory {HOTFIX_APP_PATH}",
            f"[ ! -f pyproject.toml ] || "
            f"UV_PYTHON_INSTALL_DIR={HOTFIX_APP_PATH}/.python uv sync --managed-python",
            "git rev-parse HEAD",
        ]
    )
    result = _seat_run(kube, target, seat, script)
    commit = result.stdout.strip().splitlines()[-1]
    manifest = (
        json.dumps(
            {
                "version": 1,
                "repo": repo,
                "base_commit": commit,
                "base_image": target.image,
                "base_image_digest": target.image_digest,
                "container": target.container,
            },
            indent=2,
        )
        + "\n"
    )
    kube.exec_(
        target.pod.name,
        ["bash", "-c", f"cat > {MANIFEST}"],
        container=seat,
        stdin=manifest,
    )
    return [
        f"initialized {target.claim} from {repo} at {commit[:7]}",
        f"edit {HOTFIX_APP_PATH} in seat {seat}",
        f"run podbench hotfix restart {target.pod.name} when ready",
    ]


def _manifest(kube: Kubectl, target: Target) -> dict:
    result = exec_target(kube, target, f"cat {MANIFEST}", check=False)
    if result.returncode:
        raise HotfixError("no hotfix manifest; run hotfix init first")
    return load_json(result.stdout)


def restart(
    kube: Kubectl,
    pod_name: str,
    *,
    container: str | None = None,
    reinstall: bool = False,
    deadline: int = 120,
) -> list[str]:
    target, _ = resolve_target(kube, pod_name, container)
    _manifest(kube, target)
    if reinstall:
        sync = (
            f"cd {HOTFIX_APP_PATH} && "
            "if [ -f pyproject.toml ]; then "
            f"UV_PYTHON_INSTALL_DIR={HOTFIX_APP_PATH}/.python "
            "uv sync --managed-python; "
            "else echo 'no pyproject.toml; nothing to reinstall'; fi"
        )
        seat = running_seat(kube.get_pod(target.pod.name))
        if not seat:
            raise HotfixError(
                "--reinstall needs a running seat; run podbench attach first"
            )
        _seat_run(kube, target, seat.name, sync, timeout=600.0)
    before = exec_target(kube, target, f"cat {HOTFIX_CHILD_PID_PATH}").stdout.strip()
    script = "\n".join(
        [
            "set -eu",
            f"expires=$(( $(date +%s) + {deadline} ))",
            f"echo $expires > {HOTFIX_HOLD_PATH}",
            f"trap 'rm -f {HOTFIX_HOLD_PATH}' EXIT",
            f"child=$(cat {HOTFIX_CHILD_PID_PATH})",
            'kill -TERM -"$child" 2>/dev/null || kill -TERM "$child"',
            f"attempts=$(( {deadline} * 10 ))",
            "grace=100",
            '[ "$grace" -lt "$attempts" ] || grace=$(( attempts / 2 ))',
            'for attempt in $(seq 1 "$attempts"); do',
            f"  new=$(cat {HOTFIX_CHILD_PID_PATH} 2>/dev/null || true)",
            '  [ -n "$new" ] && [ "$new" != "$child" ] && exit 0',
            '  if [ "$attempt" -eq "$grace" ]; then',
            '    kill -KILL -"$child" 2>/dev/null || kill -KILL "$child"',
            "  fi",
            "  sleep 0.1",
            "done",
            "exit 1",
        ]
    )
    exec_target(kube, target, script, timeout=float(deadline + 10))
    after = exec_target(kube, target, f"cat {HOTFIX_CHILD_PID_PATH}").stdout.strip()
    seat = running_seat(kube.get_pod(target.pod.name))
    state = "not measured"
    if seat:
        result = _seat_run(
            kube,
            target,
            seat.name,
            f"cd {HOTFIX_APP_PATH} && git status --short",
            check=False,
        )
        state = "clean" if not result.stdout.strip() else "modified"
    return [
        f"restarted {target.container}: pid {before} -> {after}",
        f"checkout is {state}",
    ]
