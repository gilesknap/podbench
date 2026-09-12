"""The thin kubectl boundary used by both prototype modes."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from .model import as_dict

DEFAULT_CALL_TIMEOUT = 30.0


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = DEFAULT_CALL_TIMEOUT,
    ) -> CommandResult: ...


def run_subprocess(
    argv: Sequence[str],
    *,
    stdin: str | None = None,
    capture: bool = True,
    timeout: float | None = DEFAULT_CALL_TIMEOUT,
) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            input=stdin,
            text=True,
            capture_output=capture,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise KubectlError(f"{argv[0]} was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise KubectlTimeoutError(f"command timed out after {timeout:g}s") from exc
    return CommandResult(
        tuple(argv),
        completed.returncode,
        completed.stdout or "",
        completed.stderr or "",
    )


def command_said(stderr: str) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in stderr.splitlines()
        if line.strip() and not line.startswith("command terminated")
    )


class KubectlError(RuntimeError):
    def __init__(self, value: CommandResult | str):
        self.result = value if isinstance(value, CommandResult) else None
        if isinstance(value, CommandResult):
            detail = (
                value.stderr.strip()
                or value.stdout.strip()
                or f"exit {value.returncode}"
            )
            message = f"{' '.join(value.argv)}: {detail}"
        else:
            message = value
        super().__init__(message)


class KubectlTimeoutError(KubectlError):
    pass


class Kubectl:
    def __init__(
        self,
        namespace: str,
        *,
        context: str | None = None,
        binary: str = "kubectl",
        runner: Runner | None = None,
    ) -> None:
        self.namespace = namespace
        self.context = context
        self.binary = binary
        self._runner = runner or run_subprocess

    @property
    def runner(self) -> Runner:
        return self._runner

    def for_namespace(self, namespace: str) -> Kubectl:
        return (
            self
            if namespace == self.namespace
            else Kubectl(
                namespace, context=self.context, binary=self.binary, runner=self._runner
            )
        )

    def run(
        self,
        *args: str,
        stdin: str | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = DEFAULT_CALL_TIMEOUT,
        cluster_wide: bool = False,
    ) -> CommandResult:
        argv = [self.binary]
        if self.context:
            argv += ["--context", self.context]
        if not cluster_wide:
            argv += ["-n", self.namespace]
        argv += args
        result = self._runner(argv, stdin=stdin, capture=capture, timeout=timeout)
        if check and result.returncode:
            raise KubectlError(result)
        return result

    def get_pod(self, name: str) -> dict[str, Any]:
        return self._json(self.run("get", "pod", name, "-o", "json"))

    def list_pods(self) -> list[dict[str, Any]]:
        items = self._json(self.run("get", "pods", "-o", "json")).get("items", [])
        return (
            [cast(dict[str, Any], item) for item in items if isinstance(item, dict)]
            if isinstance(items, list)
            else []
        )

    def exec_(
        self,
        pod: str,
        argv: Sequence[str],
        *,
        container: str | None = None,
        stdin: str | None = None,
        check: bool = True,
        timeout: float | None = DEFAULT_CALL_TIMEOUT,
    ) -> CommandResult:
        args = ["exec"]
        if stdin is not None:
            args.append("-i")
        if container:
            args += ["-c", container]
        args += [pod, "--", *argv]
        return self.run(*args, stdin=stdin, check=check, timeout=timeout)

    def add_ephemeral_container(self, pod: str, container: Mapping[str, Any]) -> None:
        current = self._json(
            self.run(
                "get", "pod", pod, "--subresource=ephemeralcontainers", "-o", "json"
            )
        )
        spec = as_dict(current.get("spec"))
        existing = spec.get("ephemeralContainers", [])
        containers = (
            [item for item in existing if isinstance(item, dict)]
            if isinstance(existing, list)
            else []
        )
        spec["ephemeralContainers"] = [*containers, dict(container)]
        current["spec"] = spec
        path = f"/api/v1/namespaces/{self.namespace}/pods/{pod}/ephemeralcontainers"
        self.run("replace", "--raw", path, "-f", "-", stdin=json.dumps(current))

    def wait_for_ephemeral_container(
        self, pod: str, name: str, *, timeout: float = 120.0
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            statuses = as_dict(self.get_pod(pod).get("status")).get(
                "ephemeralContainerStatuses", []
            )
            for raw in statuses if isinstance(statuses, list) else []:
                status = as_dict(raw)
                if status.get("name") != name:
                    continue
                state = as_dict(status.get("state"))
                if as_dict(state.get("running")):
                    return
                failed = as_dict(state.get("waiting")) or as_dict(
                    state.get("terminated")
                )
                if failed and failed.get("reason") not in (
                    "ContainerCreating",
                    "PodInitializing",
                ):
                    raise KubectlError(
                        f"ephemeral container {name} failed: {failed.get('reason')}: "
                        f"{failed.get('message', '')}"
                    )
            time.sleep(0.5)
        raise KubectlTimeoutError(
            f"ephemeral container {name} did not start within {timeout:g}s"
        )

    @staticmethod
    def _json(result: CommandResult) -> dict[str, Any]:
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise KubectlError(f"kubectl returned invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise KubectlError("kubectl returned JSON that was not an object")
        return cast(dict[str, Any], value)
