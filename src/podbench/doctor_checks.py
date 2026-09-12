"""Read-only prerequisite checks used by ``podbench doctor``."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .kubectl import CommandResult, run_subprocess

MIN_KUBERNETES = (1, 25)


class Status(Enum):
    OK = ("OK", "green")
    FIXED = ("FIXED", "bold green")
    WARN = ("WARN", "yellow")
    FAIL = ("FAIL", "bold red")


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


@dataclass(frozen=True)
class Grant:
    verb: str
    resource: str

    @property
    def arguments(self) -> list[str]:
        resource, _, subresource = self.resource.partition("/")
        suffix = [f"--subresource={subresource}"] if subresource else []
        return [self.verb, resource, *suffix]

    def __str__(self) -> str:
        return f"{self.verb} {self.resource}"


ATTACH_GRANTS = (
    Grant("get", "pods"),
    Grant("get", "pods/ephemeralcontainers"),
    Grant("update", "pods/ephemeralcontainers"),
    Grant("create", "pods/exec"),
)
HOTFIX_GRANTS = (
    Grant("list", "pods"),
    Grant("get", "deployments.apps"),
    Grant("get", "statefulsets.apps"),
    Grant("get", "replicasets.apps"),
    Grant("get", "persistentvolumeclaims"),
    Grant("list", "persistentvolumeclaims"),
    Grant("delete", "persistentvolumeclaims"),
)


def kubectl_run(binary: str, context: str | None, *arguments: str) -> CommandResult:
    command = [binary]
    if context:
        command += ["--context", context]
    return run_subprocess([*command, *arguments])


def _version(value: object) -> tuple[int, int] | None:
    if not isinstance(value, dict) or not isinstance(value.get("gitVersion"), str):
        return None
    match = re.match(r"v?(\d+)\.(\d+)", value["gitVersion"])
    return (int(match.group(1)), int(match.group(2))) if match else None


def cluster_checks(
    binary: str, context: str | None, namespace: str | None
) -> tuple[list[Check], str | None, str | None, bool]:
    path = shutil.which(binary)
    if path is None:
        missing = Check("kubectl", Status.FAIL, f"{binary} is not on PATH")
        return [missing], context, namespace, False

    result = kubectl_run(binary, context, "version", "-o", "json")
    connected = result.returncode == 0
    try:
        versions = json.loads(result.stdout) if connected else {}
    except ValueError:
        versions = {}
    client = _version(versions.get("clientVersion"))
    server = _version(versions.get("serverVersion"))
    if not connected:
        detail = result.stderr.strip() or "cluster did not answer"
        version_check = Check("kubectl", Status.FAIL, f"{path}; {detail}")
    elif client is None or server is None:
        version_check = Check("kubectl", Status.WARN, f"{path}; versions not reported")
    elif min(client, server) < MIN_KUBERNETES:
        detail = f"client {_spelled(client)}, server {_spelled(server)}; need 1.25+"
        version_check = Check("kubectl", Status.FAIL, detail)
    else:
        detail = f"client {_spelled(client)}, server {_spelled(server)} at {path}"
        version_check = Check("kubectl", Status.OK, detail)

    if context is None:
        current = kubectl_run(binary, None, "config", "current-context")
        context = current.stdout.strip() if current.returncode == 0 else None
    context_check = Check(
        "context",
        Status.OK if context else Status.FAIL,
        context or "no current context",
    )
    if namespace is None and context:
        current = kubectl_run(
            binary,
            context,
            "config",
            "view",
            "--minify",
            "-o",
            "jsonpath={..namespace}",
        )
        namespace = current.stdout.strip() if current.returncode == 0 else None
        namespace = namespace or "default"
    namespace_check = Check(
        "namespace",
        Status.OK if namespace else Status.FAIL,
        namespace or "not resolved",
    )
    return (
        [version_check, context_check, namespace_check],
        context,
        namespace,
        connected,
    )


def _spelled(version: tuple[int, int]) -> str:
    return f"{version[0]}.{version[1]}"


def ssh_checks(identity: str) -> list[Check]:
    ssh = shutil.which("ssh")
    checks = [
        Check("ssh client", Status.OK if ssh else Status.FAIL, ssh or "not on PATH")
    ]
    private = Path(identity).expanduser().resolve()
    public = private.with_name(f"{private.name}.pub")
    missing = [str(path) for path in (private, public) if not path.is_file()]
    detail = "missing " + ", ".join(missing) if missing else str(private)
    checks.append(Check("ssh identity", Status.FAIL if missing else Status.OK, detail))

    socket = os.environ.get("SSH_AUTH_SOCK")
    if not socket:
        if private.is_file():
            agent = Check("ssh agent", Status.OK, "unset; ssh will use the key file")
        else:
            agent = Check("ssh agent", Status.WARN, "unset; no key file can sign")
    elif (
        not public.is_file()
        or not shutil.which("ssh-add")
        or not shutil.which("ssh-keygen")
    ):
        agent = Check("ssh agent", Status.WARN, "selected key not compared")
    else:
        fingerprint = run_subprocess(["ssh-keygen", "-lf", str(public)])
        listed = run_subprocess(["ssh-add", "-l"])
        match = re.search(r"\b(?:SHA256|MD5):\S+", fingerprint.stdout)
        listing_failed = listed.returncode not in (0, 1) or (
            listed.returncode == 1 and bool(listed.stderr.strip())
        )
        if listing_failed or match is None:
            agent = Check("ssh agent", Status.WARN, f"could not inspect {socket}")
        elif match.group(0) in listed.stdout:
            agent = Check(
                "ssh agent", Status.WARN, "holds the selected key and may sign for it"
            )
        else:
            agent = Check(
                "ssh agent", Status.OK, "does not hold key; ssh will use its file"
            )
    checks.append(agent)
    return checks


def rbac_check(
    name: str,
    grants: Sequence[Grant],
    *,
    binary: str,
    context: str | None,
    namespace: str,
    blocking: bool,
) -> Check:
    missing: list[str] = []
    unknown = False
    for grant in grants:
        result = kubectl_run(
            binary, context, "-n", namespace, "auth", "can-i", *grant.arguments
        )
        answer = result.stdout.strip().lower()
        if answer == "no":
            missing.append(str(grant))
        elif result.returncode or answer != "yes":
            unknown = True
    if unknown:
        return Check(name, Status.WARN, "not fully measured: kubectl could not answer")
    if missing:
        status = Status.FAIL if blocking else Status.WARN
        return Check(name, status, "missing " + ", ".join(missing))
    return Check(name, Status.OK, f"all {len(grants)} permissions allowed")
