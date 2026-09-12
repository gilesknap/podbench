"""Generate the deploy-time wiring for one workload claim."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from typing import Any

from .hotfix_core import HotfixError, find_container
from .launcher import target_container_name, target_uid_gid
from .model import (
    HOTFIX_APP_PATH,
    HOTFIX_CHILD_PID_PATH,
    HOTFIX_CLAIM_VOLUME,
    HOTFIX_HOLD_PATH,
    as_dict,
)

RESTART_WINDOW_SECONDS = 120


def claim_for(app: str) -> str:
    return f"{app}-podbench-project"[:63].rstrip("-")


def entrypoint(container: Mapping[str, Any]) -> str:
    command = container.get("command")
    if not isinstance(command, list) or not command:
        raise HotfixError("the entrypoint is only in the image; pass --entrypoint")
    words = [str(word) for word in command]
    args = container.get("args")
    if isinstance(args, list):
        words.extend(str(word) for word in args)
    return shlex.join(words)


def supervisor(command: str) -> str:
    launch = shlex.quote(f"exec {command}")
    return "\n".join(
        [
            "while :; do",
            f"  [ ! -x {HOTFIX_APP_PATH}/.venv/bin/python ] || "
            f'export PATH="{HOTFIX_APP_PATH}/.venv/bin:$PATH"',
            f"  setsid bash -c {launch} &",
            "  child=$!",
            f"  echo $child > {HOTFIX_CHILD_PID_PATH}",
            "  wait $child; rc=$?",
            '  kill -TERM -"$child" 2>/dev/null || true',
            f"  [ -e {HOTFIX_HOLD_PATH} ] || exit $rc",
            "done",
        ]
    )


def _liveness(container: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]] | None:
    probe = as_dict(container.get("livenessProbe"))
    if not probe:
        return None
    command = as_dict(probe.get("exec")).get("command")
    if not isinstance(command, list) or not command:
        # HTTP, TCP, and gRPC probes cannot see the hold file. Their failure
        # threshold is extended below instead of replacing the probe action.
        return None
    timings = {key: value for key, value in probe.items() if key != "exec"}
    return [str(word) for word in command], timings


def render_values(
    pod: Mapping[str, Any],
    app: str,
    *,
    container_name: str | None = None,
    command: str | None = None,
    gid: int | None = None,
    size: str = "10Gi",
) -> str:
    chosen = target_container_name(pod, container_name)
    container = find_container(pod, chosen)
    command = command or entrypoint(container)
    _, discovered_gid = target_uid_gid(pod, chosen)
    gid = gid if gid is not None else discovered_gid
    if gid is None:
        raise HotfixError("the target gid is not reported; pass --gid")
    claim = claim_for(app)
    lines = [
        "podbench-hotfix-claim:",
        "  enabled: true",
        f"  size: {size}",
        "",
        "volumes:",
        f"  - name: {HOTFIX_CLAIM_VOLUME}",
        "    persistentVolumeClaim:",
        f"      claimName: {claim}",
        "volumeMounts:",
        f"  - name: {HOTFIX_CLAIM_VOLUME}",
        f"    mountPath: {HOTFIX_APP_PATH}",
        "command: [bash, -c]",
        "args:",
        "  - |",
        *[f"    {line}" for line in supervisor(command).splitlines()],
    ]
    if live := _liveness(container):
        original, timings = live
        wrapped = f"[ -e {HOTFIX_HOLD_PATH} ] && exit 0; exec {shlex.join(original)}"
        lines += [
            "livenessProbe:",
            "  exec:",
            "    command: [bash, -c, " + json.dumps(wrapped) + "]",
        ]
        lines += [f"  {key}: {json.dumps(value)}" for key, value in timings.items()]
    elif probe := as_dict(container.get("livenessProbe")):
        period = probe.get("periodSeconds", 10)
        period = period if isinstance(period, int) and period > 0 else 10
        failures = probe.get("failureThreshold", 3)
        failures = failures if isinstance(failures, int) else 3
        restart_failures = (RESTART_WINDOW_SECONDS + period - 1) // period + 1
        lines += [
            "livenessProbe:",
            f"  failureThreshold: {max(failures, restart_failures)}",
        ]
    lines += ["podSecurityContext:", f"  fsGroup: {gid}"]
    return "\n".join(lines) + "\n"
