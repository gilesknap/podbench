"""Tests for the kubectl wrapper.

No test here touches a cluster: :class:`FakeRunner` stands in for the
subprocess, so what is asserted is argv construction and the parsing of the two
rejection channels the spikes found (phase0 report 3.18).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from podbench.kubectl import (
    PSA_SYS_PTRACE_DENIAL,
    CommandResult,
    EphemeralContainerError,
    Kubectl,
    KubectlError,
    next_container_name,
)
from podbench.sshcfg import KubectlInvocation


class FakeRunner:
    """Returns canned results in order and records how it was called."""

    def __init__(self, *results: CommandResult) -> None:
        self.queued: list[CommandResult] = list(results)
        self.calls: list[tuple[tuple[str, ...], str | None, bool]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
    ) -> CommandResult:
        self.calls.append((tuple(argv), stdin, capture))
        queued = self.queued.pop(0) if self.queued else CommandResult((), 0, "", "")
        return CommandResult(
            argv=tuple(argv),
            returncode=queued.returncode,
            stdout=queued.stdout,
            stderr=queued.stderr,
        )

    @property
    def argv(self) -> tuple[str, ...]:
        """The argv of the single call made so far."""
        assert len(self.calls) == 1
        return self.calls[0][0]


def ok(stdout: str = "") -> CommandResult:
    return CommandResult((), 0, stdout, "")


def fail(stderr: str, returncode: int = 1) -> CommandResult:
    return CommandResult((), returncode, "", stderr)


def pod_json(**status: Any) -> str:
    return json.dumps({"metadata": {"name": "target"}, "status": status})


def test_base_argv_carries_context_and_namespace() -> None:
    runner = FakeRunner(ok("{}"))
    Kubectl("demo", context="prod", binary="kubectl.bin", runner=runner).run("version")
    assert runner.argv == ("kubectl.bin", "--context", "prod", "-n", "demo", "version")


def test_base_argv_carries_a_kubeconfig() -> None:
    # The same two selectors the ssh ProxyCommand takes, in the same order: the
    # API calls and the transport must be able to name one cluster between them.
    runner = FakeRunner(ok("{}"))
    Kubectl(
        "demo", context="prod", kubeconfig="/home/dev/my kubeconfig", runner=runner
    ).run("version")
    assert runner.argv[:5] == (
        "kubectl",
        "--kubeconfig",
        "/home/dev/my kubeconfig",
        "--context",
        "prod",
    )


def test_base_argv_omits_an_unset_context() -> None:
    runner = FakeRunner(ok("{}"))
    Kubectl("demo", runner=runner).run("version")
    assert runner.argv == ("kubectl", "-n", "demo", "version")


def test_the_two_kubectl_types_agree_on_their_auth_arguments() -> None:
    # They are different objects — one is a namespaced client, the other is
    # argv for an ssh ProxyCommand — but a session that authenticated its API
    # calls one way and its transport another would be a bug nobody could see.
    client = Kubectl(
        "demo", context="prod", kubeconfig="/tmp/kubeconfig", binary="kubectl.bin"
    )
    invocation = KubectlInvocation(
        binary="kubectl.bin", context="prod", kubeconfig="/tmp/kubeconfig"
    )
    assert client.base_argv()[:-2] == invocation.base()


def test_get_pod_parses_json() -> None:
    runner = FakeRunner(ok(json.dumps({"metadata": {"name": "target"}})))
    pod = Kubectl("demo", runner=runner).get_pod("target")
    assert pod["metadata"]["name"] == "target"
    assert runner.argv[-5:] == ("get", "pod", "target", "-o", "json")


def test_a_failed_command_raises_with_both_streams() -> None:
    runner = FakeRunner(fail("boom", returncode=3))
    with pytest.raises(KubectlError) as caught:
        Kubectl("demo", runner=runner).run("get", "pod", "nope")
    assert caught.value.returncode == 3
    assert caught.value.stderr == "boom"
    assert "boom" in str(caught.value)
    assert not caught.value.is_psa_ptrace_denial


def test_psa_refusal_is_recognised_by_its_stable_substring() -> None:
    # The surrounding phrase differs between baseline and restricted, so only
    # this fragment may be matched (report 3.18).
    stderr = (
        'Error from server (Forbidden): pods "victim4" is forbidden: violates '
        'PodSecurity "restricted:latest": unrestricted capabilities (container '
        '"rung1b" must not include "SYS_PTRACE" in '
        "securityContext.capabilities.add)"
    )
    runner = FakeRunner(fail(stderr))
    with pytest.raises(KubectlError) as caught:
        Kubectl("demo", runner=runner).run("replace", "--raw", "/x", "-f", "-")
    assert PSA_SYS_PTRACE_DENIAL in stderr
    assert caught.value.is_psa_ptrace_denial


def test_check_false_returns_the_failure() -> None:
    runner = FakeRunner(fail("nope"))
    result = Kubectl("demo", runner=runner).run("get", "pod", "x", check=False)
    assert result.returncode == 1


def test_exec_passes_stdin_and_never_requests_a_tty() -> None:
    runner = FakeRunner(ok("hello\n"))
    result = Kubectl("demo", runner=runner).exec_(
        "target", ["sh", "-c", "cat"], container="podbench-1", stdin="hello\n"
    )
    assert runner.argv == (
        "kubectl",
        "-n",
        "demo",
        "exec",
        "-i",
        "-c",
        "podbench-1",
        "target",
        "--",
        "sh",
        "-c",
        "cat",
    )
    assert runner.calls[0][1] == "hello\n"
    assert result.stdout == "hello\n"


def test_exec_without_stdin_omits_the_interactive_flag() -> None:
    runner = FakeRunner(ok(""))
    Kubectl("demo", runner=runner).exec_("target", ["true"])
    assert "-i" not in runner.argv
    assert "-t" not in runner.argv


def test_exec_stream_inherits_stdio() -> None:
    runner = FakeRunner(ok(""))
    Kubectl("demo", runner=runner).exec_stream(
        "target", ["/usr/sbin/sshd", "-i", "-e"], container="podbench-1"
    )
    argv, stdin, capture = runner.calls[0]
    assert "-i" in argv
    assert "-t" not in argv
    assert stdin is None
    assert capture is False


def test_patch_defaults_to_a_json_patch() -> None:
    runner = FakeRunner(ok(""))
    Kubectl("demo", runner=runner).patch(
        "svc", "app", [{"op": "replace", "path": "/spec/selector", "value": {"a": "b"}}]
    )
    argv = runner.argv
    assert "--type=json" in argv
    assert json.loads(argv[-1]) == [
        {"op": "replace", "path": "/spec/selector", "value": {"a": "b"}}
    ]


def test_patch_can_address_a_subresource() -> None:
    runner = FakeRunner(ok(""))
    Kubectl("demo", runner=runner).patch(
        "pod",
        "target",
        {"spec": {"containers": []}},
        patch_type="strategic",
        subresource="resize",
    )
    assert "--subresource=resize" in runner.argv
    assert "--type=strategic" in runner.argv


def test_create_from_spec_feeds_the_manifest_on_stdin() -> None:
    runner = FakeRunner(ok(json.dumps({"metadata": {"name": "devpod"}})))
    created = Kubectl("demo", runner=runner).create_from_spec(
        {"kind": "Pod", "metadata": {"name": "devpod"}}
    )
    assert created["metadata"]["name"] == "devpod"
    argv, stdin, _ = runner.calls[0]
    assert argv[-5:] == ("create", "-f", "-", "-o", "json")
    assert stdin is not None
    assert json.loads(stdin)["metadata"]["name"] == "devpod"


def test_delete_pod_is_non_blocking_and_forgiving_by_default() -> None:
    runner = FakeRunner(ok(""))
    Kubectl("demo", runner=runner).delete_pod("devpod")
    assert runner.argv[-5:] == (
        "delete",
        "pod",
        "devpod",
        "--wait=false",
        "--ignore-not-found",
    )


def test_wait_for_builds_a_condition() -> None:
    runner = FakeRunner(ok(""))
    Kubectl("demo", runner=runner).wait_for("pod/devpod", "condition=Ready", timeout=30)
    assert runner.argv[-3:] == ("pod/devpod", "--for=condition=Ready", "--timeout=30s")


def test_add_ephemeral_container_uses_the_subresource() -> None:
    existing = json.dumps(
        {
            "metadata": {"name": "target"},
            "spec": {"ephemeralContainers": [{"name": "podbench-1"}]},
        }
    )
    runner = FakeRunner(ok(existing), ok(""))
    Kubectl("demo", runner=runner).add_ephemeral_container(
        "target", {"name": "podbench-2", "image": "podbench:dev"}
    )

    get_argv = runner.calls[0][0]
    assert "--subresource=ephemeralcontainers" in get_argv

    put_argv, stdin, _ = runner.calls[1]
    assert put_argv[3:] == (
        "replace",
        "--raw",
        "/api/v1/namespaces/demo/pods/target/ephemeralcontainers",
        "-f",
        "-",
    )
    assert stdin is not None
    sent = json.loads(stdin)
    assert [c["name"] for c in sent["spec"]["ephemeralContainers"]] == [
        "podbench-1",
        "podbench-2",
    ]


def test_add_ephemeral_container_replaces_a_same_named_entry() -> None:
    existing = json.dumps(
        {"spec": {"ephemeralContainers": [{"name": "podbench-1", "image": "old"}]}}
    )
    runner = FakeRunner(ok(existing), ok(""))
    Kubectl("demo", runner=runner).add_ephemeral_container(
        "target", {"name": "podbench-1", "image": "new"}
    )
    stdin = runner.calls[1][1]
    assert stdin is not None
    containers = json.loads(stdin)["spec"]["ephemeralContainers"]
    assert containers == [{"name": "podbench-1", "image": "new"}]


def test_wait_for_ephemeral_container_returns_started_at() -> None:
    running = pod_json(
        ephemeralContainerStatuses=[
            {
                "name": "podbench-1",
                "state": {"running": {"startedAt": "2026-08-15T10:00:00Z"}},
            }
        ]
    )
    runner = FakeRunner(ok(running))
    started = Kubectl("demo", runner=runner).wait_for_ephemeral_container(
        "target", "podbench-1", timeout=0.0, poll_interval=0.0
    )
    assert started == "2026-08-15T10:00:00Z"


def test_kubelet_rejection_surfaces_its_own_message() -> None:
    # The API server took this container; the node refused it seconds later.
    message = (
        "container's runAsUser breaks non-root policy "
        '(pod: "victim_podbench-s5(b89a16a0)", container: podbench-1)'
    )
    waiting = pod_json(
        ephemeralContainerStatuses=[
            {
                "name": "podbench-1",
                "state": {
                    "waiting": {
                        "reason": "CreateContainerConfigError",
                        "message": message,
                    }
                },
            }
        ]
    )
    runner = FakeRunner(ok(waiting))
    with pytest.raises(EphemeralContainerError) as caught:
        Kubectl("demo", runner=runner).wait_for_ephemeral_container(
            "target", "podbench-1", timeout=0.0, poll_interval=0.0
        )
    assert caught.value.reason == "CreateContainerConfigError"
    assert caught.value.message == message


def test_a_terminated_ephemeral_container_is_a_failure_not_a_wait() -> None:
    terminated = pod_json(
        ephemeralContainerStatuses=[
            {
                "name": "podbench-1",
                "state": {
                    "terminated": {"reason": "Completed", "exitCode": 0},
                },
            }
        ]
    )
    runner = FakeRunner(ok(terminated))
    with pytest.raises(EphemeralContainerError):
        Kubectl("demo", runner=runner).wait_for_ephemeral_container(
            "target", "podbench-1", timeout=0.0, poll_interval=0.0
        )


def test_wait_for_ephemeral_container_times_out() -> None:
    runner = FakeRunner(ok(pod_json()))
    with pytest.raises(TimeoutError):
        Kubectl("demo", runner=runner).wait_for_ephemeral_container(
            "target", "podbench-1", timeout=0.0, poll_interval=0.0
        )


def test_next_container_name_skips_every_burnt_name() -> None:
    # A dead or Completed ephemeral container keeps its name for the pod's
    # lifetime, and status may know of one the spec has not caught up with.
    pod = {
        "spec": {
            "containers": [{"name": "app"}],
            "ephemeralContainers": [{"name": "podbench-1"}, {"name": "podbench-2"}],
        },
        "status": {"ephemeralContainerStatuses": [{"name": "podbench-3"}]},
    }
    assert next_container_name(pod) == "podbench-4"


def test_next_container_name_on_a_fresh_pod() -> None:
    assert (
        next_container_name({"spec": {"containers": [{"name": "app"}]}}) == "podbench-1"
    )
    assert next_container_name({}, base="seat") == "seat-1"


def test_the_default_runner_really_executes(tmp_path: Path) -> None:
    # The one test that exercises subprocess: a fake kubectl that echoes its
    # argv back and fails loudly on demand.
    fake = tmp_path / "kubectl"
    fake.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@"\nfor arg in "$@"; do\n'
        '  [ "$arg" = "--boom" ] || continue\n'
        '  echo "it went wrong" >&2\n  exit 7\ndone\nexit 0\n'
    )
    fake.chmod(0o755)
    kubectl = Kubectl("demo", binary=str(fake))

    result = kubectl.run("get", "pod", "target")
    assert result.returncode == 0
    assert result.stdout.split() == ["-n", "demo", "get", "pod", "target"]

    with pytest.raises(KubectlError) as caught:
        kubectl.run("--boom")
    assert caught.value.returncode == 7
    assert caught.value.stderr.strip() == "it went wrong"
