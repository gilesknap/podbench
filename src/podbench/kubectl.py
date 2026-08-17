"""A thin, typed wrapper over the ``kubectl`` binary.

podbench shells out to ``kubectl`` rather than linking a Kubernetes client
library, on purpose: the launcher then inherits the kubeconfig's auth, its
contexts and its exec credential plugins for free, and "auth is the kubeconfig"
is the product's headline claim. A second credential path would be a second
thing to get wrong, and this module has no runtime dependencies at all.

Everything the launcher needs beyond plain CRUD is here because the spikes
proved it cannot be done naively:

* ephemeral containers are added through the ``ephemeralcontainers``
  subresource, never ``kubectl debug`` (phase0 report 3.14/4.5);
* a dead ephemeral container's name is burnt for the pod's lifetime, so names
  are allocated with an incrementing suffix (report 4.2);
* the kubelet rejects a container *asynchronously*, long after the API call
  returned 0, so readiness is polled rather than assumed (report 3.18).
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from .model import as_dict

__all__ = [
    "ADMISSION_DENIAL_MARKERS",
    "CREATE_CONTAINER_CONFIG_ERROR",
    "PSA_SYS_PTRACE_DENIAL",
    "CommandResult",
    "EphemeralContainerError",
    "Kubectl",
    "KubectlError",
    "Runner",
    "next_container_name",
    "run_subprocess",
]

PSA_SYS_PTRACE_DENIAL = (
    'must not include "SYS_PTRACE" in securityContext.capabilities.add'
)
"""The one substring of a Pod Security Admission refusal that is stable.

The surrounding phrase differs between enforcement levels — ``unrestricted
capabilities`` under ``restricted:latest``, ``non-default capabilities`` under
``baseline:latest`` (report 3.18) — so only this fragment may be matched.
"""

ADMISSION_DENIAL_MARKERS = (
    ("violates PodSecurity",),
    ('admission webhook "', "denied the request"),
)
"""What a *synchronous policy refusal* looks like, whoever issued it.

Each tuple is a set of fragments that must all appear. Two groups, because the
two mechanisms word themselves nothing alike: Pod Security Admission is built in
and says ``violates PodSecurity``, while every webhook — Kyverno, Gatekeeper,
anything else — is announced by the API server's own wrapper naming the webhook
and the verdict.

Deliberately narrow on both counts. ``denied the request`` is required beside
the webhook's name so that a webhook which *failed to answer* — unreachable,
timed out, ``failed calling webhook`` — stays an error rather than being read as
a policy verdict: retrying a lower rung against a broken webhook would replace
one honest failure with three. And neither group matches an RBAC ``Forbidden``
or a missing pod, which are not something a lesser rung can fix.
"""

CREATE_CONTAINER_CONFIG_ERROR = "CreateContainerConfigError"
"""The kubelet's waiting reason when it refuses a container the API server took."""

DEFAULT_POLL_INTERVAL = 0.5


@dataclass(frozen=True)
class CommandResult:
    """The outcome of one ``kubectl`` invocation.

    ``stdout``/``stderr`` are empty strings for a streamed command, whose output
    went straight to this process's own descriptors.
    """

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    """How this module reaches a subprocess.

    Injecting it keeps the tests off a real cluster without monkeypatching
    :mod:`subprocess` globally.
    """

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
    ) -> CommandResult:
        """Run ``argv`` to completion and report how it went."""
        ...


def run_subprocess(
    argv: Sequence[str],
    *,
    stdin: str | None = None,
    capture: bool = True,
) -> CommandResult:
    """Run ``argv``, capturing its output unless ``capture`` is false."""
    completed = subprocess.run(
        list(argv),
        input=stdin,
        capture_output=capture,
        text=True,
        check=False,
    )
    return CommandResult(
        argv=tuple(argv),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


class KubectlError(RuntimeError):
    """A ``kubectl`` invocation that exited non-zero.

    The full argv and both streams are carried along: the launcher has to match
    admission refusals on their text, and a swallowed stderr turns a one-line
    diagnosis into an afternoon.
    """

    def __init__(self, result: CommandResult) -> None:
        self.argv = result.argv
        self.returncode = result.returncode
        self.stdout = result.stdout
        self.stderr = result.stderr
        super().__init__(
            f"{' '.join(result.argv)} exited {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    @property
    def is_psa_ptrace_denial(self) -> bool:
        """Whether Pod Security Admission refused the container over SYS_PTRACE.

        A true answer means the *synchronous* channel said no and the launcher
        should drop to the degraded rung immediately. It is a narrower question
        than :attr:`is_admission_denial`, which is what the ladder acts on;
        this one is kept because it identifies the *mechanism*, and the report
        is better for naming PSA where PSA is what refused.
        """
        return PSA_SYS_PTRACE_DENIAL in self.stderr

    @property
    def is_admission_denial(self) -> bool:
        """Whether *some* admission policy refused this, synchronously.

        The ladder exists to act on exactly this: a denial is an answer about
        one rung, not about the attach. Before issue #77 only the Pod Security
        Admission wording was recognised, so a Kyverno refusal of the full rung
        ended the walk with a traceback — in a namespace where the degraded rung
        would have been admitted, and where the whole design promises a working
        seat rather than an error.

        >>> from podbench.kubectl import CommandResult, KubectlError
        >>> kyverno = 'Error from server: admission webhook \\
        ... "validate.kyverno.svc-fail" denied the request: blocked'
        >>> KubectlError(CommandResult((), 1, "", kyverno)).is_admission_denial
        True
        >>> unreachable = 'failed calling webhook "validate.kyverno.svc": \\
        ... context deadline exceeded'
        >>> KubectlError(CommandResult((), 1, "", unreachable)).is_admission_denial
        False
        >>> rbac = 'Error from server (Forbidden): pods "api" is forbidden'
        >>> KubectlError(CommandResult((), 1, "", rbac)).is_admission_denial
        False
        """
        text = f"{self.stderr}\n{self.stdout}"
        return any(
            all(fragment in text for fragment in group)
            for group in ADMISSION_DENIAL_MARKERS
        )


class EphemeralContainerError(RuntimeError):
    """The kubelet refused, or lost, an ephemeral container the API server took.

    ``kubectl`` exits 0 for these: the pod update is valid, and only the node
    discovers later that (for example) a root container violates the pod's
    ``runAsNonRoot: true``. The kubelet's own message is the useful part, so it
    is preserved verbatim.
    """

    def __init__(self, container: str, reason: str, message: str) -> None:
        self.container = container
        self.reason = reason
        self.message = message
        super().__init__(
            f"ephemeral container {container!r} failed ({reason}): {message}"
        )


def next_container_name(pod_json: Mapping[str, Any], base: str = "podbench") -> str:
    """Allocate an unused ``<base>-<n>`` container name for this pod.

    An ephemeral container cannot be removed or restarted, so its name is burnt
    for the pod's lifetime — a container that died, or was created with a
    short-lived command and reached ``Completed``, permanently occupies the name
    (report 4.2). Reconnecting therefore has to take the next number rather than
    reuse the one that looks idle.

    Every container in a pod shares one name space, so init, regular, ephemeral
    and merely-reported containers all count as taken.

    >>> next_container_name({"spec": {"ephemeralContainers": [{"name": "podbench-1"}]}})
    'podbench-2'
    """
    taken: set[str] = set()
    spec = as_dict(pod_json.get("spec"))
    status = as_dict(pod_json.get("status"))
    for key in ("containers", "initContainers", "ephemeralContainers"):
        taken |= _names(spec.get(key))
    for key in (
        "containerStatuses",
        "initContainerStatuses",
        "ephemeralContainerStatuses",
    ):
        taken |= _names(status.get(key))

    index = 1
    while f"{base}-{index}" in taken:
        index += 1
    return f"{base}-{index}"


def _entry_name(entry: Any) -> str | None:
    if isinstance(entry, dict):
        name = cast(dict[str, Any], entry).get("name")
        if isinstance(name, str):
            return name
    return None


def _names(entries: Any) -> set[str]:
    if not isinstance(entries, list):
        return set()
    return {
        name
        for name in (_entry_name(entry) for entry in cast(list[Any], entries))
        if name is not None
    }


def _parse_json_object(text: str, argv: Sequence[str]) -> dict[str, Any]:
    parsed: object = json.loads(text)
    if not isinstance(parsed, dict):
        raise KubectlError(
            CommandResult(
                argv=tuple(argv),
                returncode=0,
                stdout=text,
                stderr="expected a JSON object",
            )
        )
    return cast(dict[str, Any], parsed)


class Kubectl:
    """Every cluster interaction podbench makes, bound to one namespace."""

    def __init__(
        self,
        namespace: str,
        *,
        context: str | None = None,
        kubeconfig: str | None = None,
        binary: str = "kubectl",
        runner: Runner | None = None,
    ) -> None:
        self.namespace = namespace
        self.context = context
        self.kubeconfig = kubeconfig
        self.binary = binary
        self._runner: Runner = runner if runner is not None else run_subprocess

    # -- plumbing ---------------------------------------------------------

    def base_argv(self) -> list[str]:
        """The prefix every invocation shares.

        ``--kubeconfig`` and ``--context`` are selected here rather than left to
        the environment so that the API calls and the ssh transport
        (:func:`podbench.sshcfg.proxy_command`) can be pointed at the same
        cluster by the same two arguments. A launcher that could name a
        kubeconfig for one and not the other would let a session's control plane
        and its data plane drift apart.
        """
        argv = [self.binary]
        if self.kubeconfig is not None:
            argv += ["--kubeconfig", self.kubeconfig]
        if self.context is not None:
            argv += ["--context", self.context]
        argv += ["-n", self.namespace]
        return argv

    def run(
        self,
        *args: str,
        stdin: str | None = None,
        check: bool = True,
        capture: bool = True,
    ) -> CommandResult:
        """Run ``kubectl`` with this instance's context and namespace."""
        argv = self.base_argv() + list(args)
        result = self._runner(argv, stdin=stdin, capture=capture)
        if check and result.returncode != 0:
            raise KubectlError(result)
        return result

    # -- reads ------------------------------------------------------------

    def get_pod(self, name: str) -> dict[str, Any]:
        """The pod's full JSON, spec and status."""
        result = self.run("get", "pod", name, "-o", "json")
        return _parse_json_object(result.stdout, result.argv)

    def pod_exists(self, name: str) -> bool:
        """Whether a pod of exactly this name is there, in one cheap call.

        ``-o name`` rather than ``-o json`` because the answer is the exit code:
        this is asked before every substring search, to let a fully typed pod
        name resolve without listing the namespace at all. That matters for more
        than speed — a user whose RBAC grants ``get`` on pods but not ``list``
        could always name a pod outright, and must keep being able to.

        Any failure reads as "not this pod": kubectl distinguishes a 404 from a
        403 only in text, and the caller's next step (list the namespace) will
        surface the real refusal in kubectl's own words.
        """
        return self.run("get", "pod", name, "-o", "name", check=False).returncode == 0

    def list_pods(self) -> list[dict[str, Any]]:
        """Every pod in the namespace, as the full JSON documents.

        One lister, because podbench has two questions to ask of the same
        output — which pods carry a podbench container
        (:func:`podbench.launcher.list_seats`) and which pod the user meant
        (:func:`podbench.launcher.resolve_pod`) — and a second ``get pods``
        spelled slightly differently is a second thing to keep true.
        """
        result = self.run("get", "pods", "-o", "json")
        items = _parse_json_object(result.stdout, result.argv).get("items")
        if not isinstance(items, list):
            return []
        return [
            cast(dict[str, Any], item)
            for item in cast(list[Any], items)
            if isinstance(item, dict)
        ]

    def list_limit_ranges(self) -> list[dict[str, Any]]:
        """Every ``LimitRange`` in the namespace, or none if they cannot be read.

        ``check=False`` because this is asked to *improve* a resize, not to
        permit one: RBAC that grants pod writes and no ``list`` on limitranges
        is ordinary, and a namespace with no LimitRange is the common case. Both
        answer "nothing constrains this", and the resize is submitted either
        way — the cluster gets the last word, as it did before this was read at
        all.
        """
        result = self.run("get", "limitranges", "-o", "json", check=False)
        if result.returncode != 0:
            return []
        try:
            items = _parse_json_object(result.stdout, result.argv).get("items")
        except (KubectlError, json.JSONDecodeError):
            return []
        if not isinstance(items, list):
            return []
        return [
            cast(dict[str, Any], item)
            for item in cast(list[Any], items)
            if isinstance(item, dict)
        ]

    def get_pod_subresource(self, name: str, subresource: str) -> dict[str, Any]:
        """A pod subresource's JSON, e.g. ``ephemeralcontainers``."""
        result = self.run(
            "get", "pod", name, f"--subresource={subresource}", "-o", "json"
        )
        return _parse_json_object(result.stdout, result.argv)

    # -- writes -----------------------------------------------------------

    def create_from_spec(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """Create one object from an authored manifest, returning it as stored.

        ``create`` rather than ``apply``: podbench authors dev pods that must
        not silently adopt or overwrite something already there.
        """
        result = self.run("create", "-f", "-", "-o", "json", stdin=json.dumps(spec))
        return _parse_json_object(result.stdout, result.argv)

    def patch(
        self,
        kind: str,
        name: str,
        body: str | Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        patch_type: str = "json",
        subresource: str | None = None,
    ) -> CommandResult:
        """Patch an object.

        ``patch_type`` defaults to ``json`` because a *merge* patch unions map
        keys rather than replacing them: patching a Service selector by merge
        leaves the old keys in place and silently drops the original pod out of
        the endpointslice (report 4.4).
        """
        payload = body if isinstance(body, str) else json.dumps(body)
        args = [
            "patch",
            kind,
            name,
            f"--type={patch_type}",
            "-p",
            payload,
        ]
        if subresource is not None:
            args.append(f"--subresource={subresource}")
        return self.run(*args)

    def delete_pod(
        self, name: str, *, ignore_not_found: bool = True, wait: bool = False
    ) -> CommandResult:
        """Delete a pod, by default without blocking on its termination."""
        args = ["delete", "pod", name, f"--wait={str(wait).lower()}"]
        if ignore_not_found:
            args.append("--ignore-not-found")
        return self.run(*args)

    def raw_put(self, path: str, body: Mapping[str, Any]) -> CommandResult:
        """PUT a JSON body to an arbitrary API path.

        ``kubectl replace --raw`` reads the body from stdin when the filename is
        ``-``, which keeps the manifest out of a temp file the caller would have
        to clean up.
        """
        return self.run("replace", "--raw", path, "-f", "-", stdin=json.dumps(body))

    # -- exec -------------------------------------------------------------

    def exec_(
        self,
        pod: str,
        argv: Sequence[str],
        *,
        container: str | None = None,
        stdin: str | None = None,
        check: bool = True,
    ) -> CommandResult:
        """Run a command in a container and capture its output."""
        return self.run(
            *self._exec_argv(
                pod, argv, container=container, stdin_open=stdin is not None
            ),
            stdin=stdin,
            check=check,
        )

    def exec_stream(
        self,
        pod: str,
        argv: Sequence[str],
        *,
        container: str | None = None,
        stdin_open: bool = True,
        check: bool = False,
    ) -> CommandResult:
        """Run a command in a container with this process's stdio attached.

        This is the shape the ssh ProxyCommand needs. Note what is *not* here:
        no ``-t``. From a script kubectl silently degrades to non-tty and looks
        fine, but with a real TTY on the ProxyCommand the ssh client hangs
        forever (report 3.19), and closing or replacing the child's stderr tears
        down the whole CRI exec stream (report 3.1). So stderr is left alone and
        a tty is never requested.
        """
        return self.run(
            *self._exec_argv(pod, argv, container=container, stdin_open=stdin_open),
            capture=False,
            check=check,
        )

    def _exec_argv(
        self,
        pod: str,
        argv: Sequence[str],
        *,
        container: str | None,
        stdin_open: bool,
    ) -> list[str]:
        args = ["exec"]
        if stdin_open:
            args.append("-i")
        if container is not None:
            args += ["-c", container]
        args.append(pod)
        args.append("--")
        args += list(argv)
        return args

    # -- waiting ----------------------------------------------------------

    def wait_for(
        self,
        resource: str,
        condition: str,
        *,
        timeout: float = 120.0,
    ) -> CommandResult:
        """``kubectl wait`` on one resource, e.g. ``condition=Ready``."""
        return self.run(
            "wait",
            resource,
            f"--for={condition}",
            f"--timeout={int(timeout)}s",
        )

    # -- ephemeral containers ---------------------------------------------

    def add_ephemeral_container(self, pod: str, spec: Mapping[str, Any]) -> None:
        """Add one ephemeral container through the subresource.

        Not ``kubectl debug``: that merges the chosen debug profile *after* any
        ``--custom`` JSON, so asking for ``runAsUser: 1000`` yields a container
        that also carries ``SYS_PTRACE`` — the combination the kernel turns into
        ``CapEff: 0`` (report 3.14). The subresource takes the spec verbatim.

        Raises :class:`KubectlError` on a synchronous refusal; check
        :attr:`KubectlError.is_psa_ptrace_denial` to tell a capability refusal
        from any other failure.
        """
        name = spec.get("name")
        current = self.get_pod_subresource(pod, "ephemeralcontainers")
        pod_spec = as_dict(current.get("spec"))
        existing = pod_spec.get("ephemeralContainers")
        kept: list[Any] = []
        if isinstance(existing, list):
            kept = [
                container
                for container in cast(list[Any], existing)
                if _entry_name(container) != name
            ]
        pod_spec["ephemeralContainers"] = [*kept, dict(spec)]
        current["spec"] = pod_spec
        self.raw_put(
            f"/api/v1/namespaces/{self.namespace}/pods/{pod}/ephemeralcontainers",
            current,
        )

    def wait_for_ephemeral_container(
        self,
        pod: str,
        name: str,
        *,
        timeout: float = 120.0,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> str:
        """Block until the named ephemeral container is running; return startedAt.

        Readiness is ``state.running.startedAt`` and nothing else, because the
        API server accepting the pod update says nothing about the node
        accepting the container. The kubelet's refusal arrives later as
        ``state.waiting.reason == CreateContainerConfigError`` with a message
        naming the real cause, which is raised as
        :class:`EphemeralContainerError` (report 3.18).
        """
        deadline = time.monotonic() + timeout
        while True:
            status = self._ephemeral_status(pod, name)
            state = as_dict(status.get("state"))
            running = as_dict(state.get("running"))
            started_at = running.get("startedAt")
            if isinstance(started_at, str):
                return started_at

            waiting = as_dict(state.get("waiting"))
            reason = waiting.get("reason")
            if reason == CREATE_CONTAINER_CONFIG_ERROR:
                raise EphemeralContainerError(
                    name, str(reason), str(waiting.get("message", ""))
                )

            terminated = as_dict(state.get("terminated"))
            if terminated:
                # The name is now burnt for the pod's lifetime; a retry must
                # pick a fresh one via next_container_name().
                raise EphemeralContainerError(
                    name,
                    str(terminated.get("reason", "Terminated")),
                    str(terminated.get("message", "container exited")),
                )

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"ephemeral container {name!r} in pod {pod!r} did not start "
                    f"within {timeout}s"
                )
            time.sleep(poll_interval)

    def _ephemeral_status(self, pod: str, name: str) -> dict[str, Any]:
        statuses = as_dict(self.get_pod(pod).get("status")).get(
            "ephemeralContainerStatuses"
        )
        if not isinstance(statuses, list):
            return {}
        for status in cast(list[Any], statuses):
            if _entry_name(status) == name:
                return as_dict(status)
        return {}
