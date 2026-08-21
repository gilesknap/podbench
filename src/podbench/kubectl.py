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
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from .model import as_dict

__all__ = [
    "ADMISSION_DENIAL_MARKERS",
    "CREATE_CONTAINER_CONFIG_ERROR",
    "DEFAULT_CALL_TIMEOUT",
    "DRY_RUN_QUERY",
    "PSA_SYS_PTRACE_DENIAL",
    "REQUEST_TIMEOUT_HEADROOM",
    "STREAMED_SUBCOMMANDS",
    "TIMED_OUT_RETURNCODE",
    "UNBOUNDED",
    "WAIT_GRACE",
    "CommandResult",
    "EphemeralContainerError",
    "Kubectl",
    "KubectlError",
    "KubectlTimeoutError",
    "Runner",
    "allowed_run_as_user",
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
    ("ValidatingAdmissionPolicy '", "denied request"),
)
"""What a *synchronous policy refusal* looks like, whoever issued it.

Each tuple is a set of fragments that must all appear. Three groups, because the
three mechanisms word themselves nothing alike: Pod Security Admission is built
in and says ``violates PodSecurity``; every webhook — Kyverno, Gatekeeper,
anything else — is announced by the API server's own wrapper naming the webhook
and the verdict; and a native ``ValidatingAdmissionPolicy`` names itself and its
binding and says ``denied request`` — *not* ``denied the request``, so the
webhook group does not cover it.

The third group was measured, not guessed: with
``tests/e2e/apps/deny-sys-ptrace.yaml`` bound to the namespace, an ``attach``
against a root target ended the whole walk on the full rung's refusal rather
than dropping to the seat rung. It is the same defect issue #77 fixed for
Kyverno, in the one policy engine that needs no installing.

Deliberately narrow on all three counts. ``denied the request`` is required
beside the webhook's name so that a webhook which *failed to answer* —
unreachable, timed out, ``failed calling webhook`` — stays an error rather than
being read as a policy verdict: retrying a lower rung against a broken webhook
would replace one honest failure with three. And none of the groups matches an
RBAC ``Forbidden`` or a missing pod, which are not something a lesser rung can
fix.

The one case the third group over-reads is a ``ValidatingAdmissionPolicy`` whose
CEL is *broken* under ``failurePolicy: Fail``: the API server reports that with
the same wrapper, so the ladder will walk down past it. That is accepted rather
than filtered, because the alternative — matching on the tail of the message —
would break the moment upstream reworded an evaluation error, and a broken
policy that refuses everything ends the walk with "no rung was admitted"
anyway.
"""

CREATE_CONTAINER_CONFIG_ERROR = "CreateContainerConfigError"
"""The kubelet's waiting reason when it refuses a container the API server took."""

DRY_RUN_QUERY = "?dryRun=All"
"""Ask the API server to run the whole admission chain and store nothing.

Measured against the target cluster on 2026-08-19: on
``pods/{pod}/ephemeralcontainers`` this returns the pod as admission *would*
have stored it, spends no container name, and leaves the pod's ephemeral
container list unchanged. That is what makes it worth a round trip per rung —
the alternative currency is a name burnt for the pod's lifetime (report 4.2).

What it cannot see is the kubelet, which refuses asynchronously and seconds
later (report 3.18). ``dryRun`` is an API-server verb: it surfaces every
synchronous refusal and every *mutation*, and never a
``CreateContainerConfigError``. The one refusal that actually burns a name is
therefore still pre-empted by reading the target's ``runAsNonRoot`` rather than
provoked.
"""

_ALLOWED_RUN_AS_USER = re.compile(r"Allowed runAsUser values are:\s*\"?([0-9|,\s]+)")
"""How a policy that allow-lists ``runAsUser`` names the uids it will take.

Standard DLS policy for a pod that does host mounts, confirmed by the
maintainer on 2026-08-19 and not local drift, so the wording is worth reading
rather than relaying: a refusal that names admissible uids is a question with an
answer, and ``--target-uid`` is where the answer goes.
"""

DEFAULT_POLL_INTERVAL = 0.5

DEFAULT_CALL_TIMEOUT = 30.0
"""How long one ``kubectl`` invocation may take before podbench stops waiting.

A bound on *one call*, which is not what any verb's ``--timeout`` means: that
one bounds a polling loop, and cannot bound the single call inside an iteration
of it. Both are needed, because an ``exec`` that wedges never returns to the
loop at all — measured in the field with a stub ``kubectl``, where ``status
--timeout 5`` was still running at 75s (issue #118).

Thirty seconds is chosen against the calls that carry it: a pod read, a patch, a
probe ``exec`` into podbench's own image. Every one of those answers in
milliseconds on a working cluster, so this is a bound on a *stuck* call rather
than a budget for a slow one. Work that legitimately takes longer — a git clone
or an editable install over ``exec`` — names its own bound at the call site, and
an interactive or streamed call passes ``timeout=None`` and is bounded by the
user hanging up.
"""

UNBOUNDED: float | None = None
"""``timeout`` for a call that is deliberately not bounded, said out loud.

The same value the parameter defaults to in :mod:`subprocess`, given a name so
that an exemption reads as a decision and greps as one. Issue #118 is about a
verb that hung because nothing bounded a call; a call that *should* hang — one
holding a user's session, or waiting on their passphrase — must be visibly
chosen rather than left to whatever the signature happened to default to.
"""

TIMED_OUT_RETURNCODE = 124
"""The exit status reported for a call podbench had to stop.

124 is coreutils ``timeout``'s, borrowed rather than invented so that a killed
call is spelled the same way here as it is in
:mod:`podbench.provision`. ``kubectl`` never exits 124 of its own accord.
"""

REQUEST_TIMEOUT_HEADROOM = 5.0
"""How far under a call's bound ``kubectl`` is told to give up by itself.

Both timers start at about the same moment and podbench's starts first, so equal
numbers would mean the kill always won the race and ``kubectl``'s own message —
which names the server that did not answer — was never printed. This hands the
ordinary case back to ``kubectl`` and leaves the kill as the backstop for a
client that has stopped honouring its own deadline.

A bound smaller than this headroom carries no ``--request-timeout`` at all,
which is right: at that point the only enforcement anyone should trust is the
one that does not depend on the process being well.
"""

WAIT_GRACE = 15.0
"""How long past its own deadline a ``kubectl wait`` is given before it is killed.

``kubectl wait --timeout=Ns`` gives up at N and prints why, which is a better
answer than a killed process; this only catches the ``kubectl`` that does not.
"""

STREAMED_SUBCOMMANDS = frozenset({"attach", "exec", "logs", "port-forward", "wait"})
"""``kubectl`` subcommands that must never carry ``--request-timeout``.

``--request-timeout`` bounds *one server request*, and each of these holds a
single request open for its whole duration — an upgraded connection for ``exec``
and ``attach``, a watch for ``wait``, a stream for ``logs`` and
``port-forward``. Applying it there would cut exactly the calls it is meant to
protect, and would do it at the flag's value rather than at the caller's: a
``wait --timeout=120s`` under a 30s request timeout fails at 30s, reporting a
condition that was never given time to become true.

They are bounded by the subprocess timeout instead, which kills a wedged call
without telling the API server how long a legitimate one may take.
"""


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
        timeout: float | None = None,
    ) -> CommandResult:
        """Run ``argv`` to completion and report how it went.

        ``timeout`` is in seconds and ``None`` means *no bound at all*, which is
        :mod:`subprocess`'s own spelling and the right one for a call holding a
        user's session. An implementation that cannot enforce a bound must still
        accept the argument: every caller passes one, and the decision about
        which calls are exempt is made at the call site rather than here.
        """
        ...


def run_subprocess(
    argv: Sequence[str],
    *,
    stdin: str | None = None,
    capture: bool = True,
    timeout: float | None = None,
) -> CommandResult:
    """Run ``argv``, capturing its output unless ``capture`` is false.

    Raises :class:`KubectlTimeoutError` when ``timeout`` seconds pass with the
    child still running. Killing it is the point: before issue #118 nothing bounded a
    single invocation, so one wedged ``kubectl exec`` — an API server that never
    answers, or an orphaned child holding the exec pipes open (issue #92) — hung
    a laptop verb for as long as the user was willing to watch it.
    """
    try:
        completed = subprocess.run(
            list(argv),
            input=stdin,
            capture_output=capture,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as expired:
        # Whatever the call managed to say before it stopped saying anything is
        # the only evidence there is about where it wedged, so it is carried
        # into the error rather than dropped with the child.
        raise KubectlTimeoutError(
            argv,
            timeout if timeout is not None else 0.0,
            stdout=_partial(expired.stdout),
            stderr=_partial(expired.stderr),
        ) from expired
    return CommandResult(
        argv=tuple(argv),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def _partial(captured: Any) -> str:
    """Whatever a timed-out child had written, as text.

    ``TimeoutExpired`` carries ``str`` under ``text=True`` and ``bytes``
    otherwise, and carries ``None`` when the stream was not captured at all.
    """
    if isinstance(captured, bytes):
        return captured.decode("utf-8", "replace")
    return captured if isinstance(captured, str) else ""


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
        super().__init__(self._message(result))

    def _message(self, result: CommandResult) -> str:
        """The one line a verb prints for this. Overridden, not reformatted."""
        return (
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
        >>> vap = 'Error from server (Forbidden): pods "app" is forbidden: \\
        ... ValidatingAdmissionPolicy \\'deny-sys-ptrace\\' with binding \\
        ... \\'deny-sys-ptrace\\' denied request: no SYS_PTRACE here'
        >>> KubectlError(CommandResult((), 1, "", vap)).is_admission_denial
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

    @property
    def allowed_run_as_user(self) -> tuple[int, ...]:
        """Uids this refusal says it would have admitted, if it named any.

        See :func:`allowed_run_as_user`. Empty for every refusal that is not
        about the uid, which is most of them.
        """
        return allowed_run_as_user(f"{self.stderr}\n{self.stdout}")


class KubectlTimeoutError(KubectlError):
    """A ``kubectl`` invocation that had to be killed because it never returned.

    A :class:`KubectlError` on purpose, so that every verb's existing ``except``
    clause turns it into one sentence on stderr rather than a traceback: giving
    up is an outcome the launcher already knows how to report, and a new
    exception type would have to be caught in a dozen places to be printed once.

    ``returncode`` is :data:`TIMED_OUT_RETURNCODE` rather than the child's,
    which there is not: the process was killed, so nothing exited.
    """

    def __init__(
        self,
        argv: Sequence[str],
        timeout: float,
        *,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.timeout = timeout
        super().__init__(
            CommandResult(
                argv=tuple(argv),
                returncode=TIMED_OUT_RETURNCODE,
                stdout=stdout,
                stderr=stderr,
            )
        )

    def _message(self, result: CommandResult) -> str:
        # One line, because that is what `podbench: <error>` prints and what
        # every warning in this CLI is. The argv is the diagnosis — which call
        # wedged — and the partial output, when there is any, is the only clue
        # about how far it got.
        said = (result.stderr.strip() or result.stdout.strip()).splitlines()
        return (
            f"{' '.join(result.argv)} did not answer within {self.timeout:g}s "
            "and was stopped" + (f", having said: {said[-1].strip()}" if said else "")
        )


def allowed_run_as_user(message: str) -> tuple[int, ...]:
    """The uids an admission refusal says it *would* have taken, if it says.

    A policy that allow-lists ``runAsUser`` refuses every other value, so the
    seat rung cannot invent one: podbench pins the target's own uid and the
    cluster has the last word on whether that is admissible. When it is not,
    this turns the refusal into an instruction — the caller can name the uids
    and the flag that selects one, instead of relaying a paragraph of policy.

    Measured against a DLS namespace on 2026-08-19: a seat authored at uid 4000
    was refused with the message below, and the same pod admitted one at 37887.

    >>> allowed_run_as_user(
    ...     "validate user id: The fields spec.securityContext.runAsUser is "
    ...     'set to an invalid value. Allowed runAsUser values are: '
    ...     '"36096|37887".'
    ... )
    (36096, 37887)
    >>> allowed_run_as_user("pods are forbidden in this namespace")
    ()
    """
    match = _ALLOWED_RUN_AS_USER.search(message)
    if match is None:
        return ()
    return tuple(
        int(value)
        for value in re.split(r"[|,\s]+", match.group(1).strip())
        if value.isdigit()
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

    def base_argv(self, *, request_timeout: float | None = None) -> list[str]:
        """The prefix every invocation shares.

        ``--kubeconfig`` and ``--context`` are selected here rather than left to
        the environment so that the API calls and the ssh transport
        (:func:`podbench.sshcfg.proxy_command`) can be pointed at the same
        cluster by the same two arguments. A launcher that could name a
        kubeconfig for one and not the other would let a session's control plane
        and its data plane drift apart.

        ``request_timeout`` is the *other half* of issue #118's bound, and it
        covers a different failure from the subprocess one: an API server that
        accepts the connection and never answers leaves ``kubectl`` waiting
        happily, so it is told to give up itself. It then exits with its own
        message about which server did not answer, which is a better sentence
        than anything a killed process can leave behind. ``None`` emits no flag
        at all — the calls that must not carry one are
        :data:`STREAMED_SUBCOMMANDS`.
        """
        argv = [self.binary]
        if self.kubeconfig is not None:
            argv += ["--kubeconfig", self.kubeconfig]
        if self.context is not None:
            argv += ["--context", self.context]
        argv += ["-n", self.namespace]
        if request_timeout is not None and request_timeout > 0:
            argv.append(f"--request-timeout={request_timeout:g}s")
        return argv

    def run(
        self,
        *args: str,
        stdin: str | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = DEFAULT_CALL_TIMEOUT,
    ) -> CommandResult:
        """Run ``kubectl`` with this instance's context and namespace.

        Bounded by default and unbounded only where a caller says so with
        ``timeout=None``: the failure this defends against is silent — a call
        that never returns produces no output, no error and no exit — so
        omitting a bound must not be how a call comes to have none.

        The subprocess bound is the outer one, and ``--request-timeout`` is set
        :data:`REQUEST_TIMEOUT_HEADROOM` under it, so on any call carrying one
        (:meth:`base_argv`) ``kubectl`` gives up first and says so in its own
        words. The kill is the backstop for a ``kubectl`` that has stopped
        listening to anything, including its own deadline.
        """
        streamed = bool(args) and args[0] in STREAMED_SUBCOMMANDS
        request = (
            None if streamed or timeout is None else timeout - REQUEST_TIMEOUT_HEADROOM
        )
        argv = self.base_argv(request_timeout=request)
        argv += list(args)
        result = self._runner(argv, stdin=stdin, capture=capture, timeout=timeout)
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

    def top_pod(self, name: str) -> str | None:
        """What ``kubectl top`` says this pod is using, or ``None`` if it will not.

        ``check=False`` and a ``None`` rather than an error: the metrics API is
        an add-on, ``pods.metrics.k8s.io`` is a resource of its own that
        podbench's chart does not grant, and a pod the sampler has not reached
        yet answers with nothing at all. Every one of those is ordinary on a
        working cluster.

        The distinction the caller must keep is that ``None`` means *unmeasured*
        and never *fine*: podbench has now twice reported an unreadable thing as
        a good one (issue #89's ``/proc`` path, C14's address-space check), and
        this is read to decide whether to stay quiet about memory.

        Text, not JSON: ``kubectl top`` has no ``-o json`` — it renders the
        metrics API itself — so :func:`podbench.resize.parse_top_memory` reads
        the column.
        """
        result = self.run("top", "pod", name, "--no-headers", check=False)
        if result.returncode != 0:
            return None
        return result.stdout

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

        ``path`` is opaque, query string included, which is what lets
        :data:`DRY_RUN_QUERY` be appended without a second code path.
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
        timeout: float | None = DEFAULT_CALL_TIMEOUT,
    ) -> CommandResult:
        """Run a command in a container and capture its output.

        Bounded like every other call, because a captured ``exec`` is a question
        with an answer — a probe, a ``cat``, a ``test`` — and the caller is
        waiting on the answer with nothing on the terminal. A caller running
        something that genuinely takes minutes in the pod (a clone, an editable
        install) passes its own ``timeout``; one holding a user's session uses
        :meth:`exec_stream`, which is exempt.
        """
        return self.run(
            *self._exec_argv(
                pod, argv, container=container, stdin_open=stdin is not None
            ),
            stdin=stdin,
            check=check,
            timeout=timeout,
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

        **Exempt from issue #118's bound, deliberately.** This process's stdio
        is the session: sshd in inetd mode, or a shell somebody is typing into.
        Blocking for as long as the user holds it is the contract, and a bound
        here would drop an ssh connection mid-edit at whatever number seemed
        reasonable to the launcher. It ends when the user hangs up, so
        ``timeout=None`` is stated rather than defaulted.
        """
        return self.run(
            *self._exec_argv(pod, argv, container=container, stdin_open=stdin_open),
            capture=False,
            check=check,
            timeout=UNBOUNDED,
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
        """``kubectl wait`` on one resource, e.g. ``condition=Ready``.

        The bound is ``kubectl``'s own, and the subprocess one is set past it by
        :data:`WAIT_GRACE`: this is the one call whose long block is the point,
        so cutting it at :data:`DEFAULT_CALL_TIMEOUT` would report "not ready"
        about a pod that was still pulling. ``wait`` is in
        :data:`STREAMED_SUBCOMMANDS` for the same reason — its watch must
        outlive any ``--request-timeout``.
        """
        return self.run(
            "wait",
            resource,
            f"--for={condition}",
            f"--timeout={int(timeout)}s",
            timeout=timeout + WAIT_GRACE,
        )

    # -- ephemeral containers ---------------------------------------------

    def add_ephemeral_container(
        self, pod: str, spec: Mapping[str, Any], *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Add one ephemeral container through the subresource.

        Not ``kubectl debug``: that merges the chosen debug profile *after* any
        ``--custom`` JSON, so asking for ``runAsUser: 1000`` yields a container
        that also carries ``SYS_PTRACE`` — the combination the kernel turns into
        ``CapEff: 0`` (report 3.14). The subresource takes the spec verbatim.

        Returns the pod as the API server reports it back, which is the whole
        point of ``dry_run``: the response is the pod as *admission* would store
        it, so a policy that rewrites the request instead of refusing it can be
        read out of it (:data:`DRY_RUN_QUERY`). The response of a real add is
        parsed by the same path and returned for free; ``{}`` where the server
        said nothing parseable, since no caller needs it.

        The body is the **whole pod**, not just the containers being added, and
        that is load-bearing rather than incidental: admission judges the object
        it is handed, so a body carrying only ``spec.ephemeralContainers`` is
        missing ``spec.hostNetwork`` and every policy keyed on it takes the
        wrong branch. Measured at DLS 2026-08-19: the same probe container is
        admitted when the pod object is preserved and refused — for a capability
        the request never asked for, which a mutating policy added on the
        strength of the absent field — when it is not. The failure is silent and
        points at the cluster rather than at the caller, so keep the pod the
        subresource GET returned and splice into it.

        The list must also carry the existing entries; omitting one is refused
        with ``existing ephemeral containers "podbench-1" may not be removed``.
        So a caller comparing what it asked for against what came back must find
        its own container by name — admission rewrites the others too.

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
        path = f"/api/v1/namespaces/{self.namespace}/pods/{pod}/ephemeralcontainers"
        result = self.raw_put(f"{path}{DRY_RUN_QUERY}" if dry_run else path, current)
        try:
            return _parse_json_object(result.stdout, result.argv)
        except (KubectlError, json.JSONDecodeError):
            return {}

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
