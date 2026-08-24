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
    "CONFLICT_ATTEMPTS",
    "CONFLICT_BACKOFF",
    "CONFLICT_MARKERS",
    "CREATE_CONTAINER_CONFIG_ERROR",
    "DEFAULT_CALL_TIMEOUT",
    "DRY_RUN_QUERY",
    "PSA_SYS_PTRACE_DENIAL",
    "REQUEST_TIMEOUT_HEADROOM",
    "STREAMED_SUBCOMMANDS",
    "TIMED_OUT_RETURNCODE",
    "UNBOUNDED",
    "WAIT_GRACE",
    "EXEC_TRAILER",
    "CommandResult",
    "EphemeralContainerError",
    "Kubectl",
    "KubectlConflictError",
    "KubectlError",
    "KubectlTimeoutError",
    "Runner",
    "allowed_run_as_user",
    "command_said",
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

CONFLICT_MARKERS = (
    ("(Conflict)",),
    ("Operation cannot be fulfilled", "the object has been modified"),
)
"""What a *lost write* looks like, as opposed to a verdict about the request.

Read the same way as :data:`ADMISSION_DENIAL_MARKERS`: all fragments in a group,
any group. Two groups because the two halves arrive independently — ``kubectl``
prefixes the API server's ``Status`` with ``Error from server (Conflict):``,
while the sentence after it is the registry's own optimistic-concurrency
wording, and something relaying the ``Status`` body alone still has to be read
as transient.

Narrow for the same reason the admission groups are: this decides whether
podbench *retries*. A conflict is the only refusal where the request was fine
and the read it was built on was stale, so re-reading and re-sending is the
whole remedy; an admission denial, an RBAC ``Forbidden`` and a webhook that
never answered are all answers about the request itself, and retrying them
turns one honest failure into several.

Measured on p47, 2026-08-24: the kubelet's writeback of an in-place resize
lands on the pod between podbench's own read and write, and the first
``podbench vscode`` against any pod it resizes loses that race.
"""

CONFLICT_ATTEMPTS = 4
"""How many times a write may lose the race before podbench gives up.

Bounded rather than deadline-driven: the writer podbench is racing is the
kubelet actuating a resize podbench asked for, which settles in one or two
writebacks, so a fourth attempt that still conflicts is evidence of something
else writing continuously and not of a slow cluster.
"""

CONFLICT_BACKOFF = 0.5
"""Seconds between conflicting attempts. Injectable so tests need not sleep."""

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


EXEC_TRAILER = re.compile(r"^command terminated with exit code \d+$")
"""kubectl's own last word about an exec, which is not the command's.

``kubectl exec`` merges its report of a non-zero exit into the same stream the
command wrote to, and always as the final line — so the last line of a failed
exec's stderr is regularly kubectl's rather than the command's. Measured on the
live p47 pod on 2026-08-24: a ``debug-config`` that refused was quoted back to
the user as ``command terminated with exit code 2``, three lines below the
sentence that said why.

Matched rather than parsed, for the same reason
:data:`PSA_SYS_PTRACE_DENIAL` is a substring: there is nothing to parse. It is
generated by kubectl's own Go, with no localisation and no variable text but
the number.

It is **not always there** — a stream that never opened, or a runtime that
refused the exec, carries a different sentence entirely, and a command that
exited 0 gets no trailer at all — so a caller strips it and then degrades
honestly rather than assuming what is left is a diagnosis.
"""


def command_said(stderr: str) -> tuple[str, ...]:
    """The lines the exec'd *command* wrote, stripped of kubectl's own trailer.

    Empty lines go too, because every caller here is looking for something to
    quote and a blank line is not it.

    >>> command_said("boom\\ncommand terminated with exit code 2\\n")
    ('boom',)
    >>> command_said("command terminated with exit code 2\\n")
    ()
    >>> command_said("error: unable to upgrade connection: pod does not exist")
    ('error: unable to upgrade connection: pod does not exist',)
    """
    lines = (line.strip() for line in stderr.splitlines())
    return tuple(line for line in lines if line and not EXEC_TRAILER.match(line))


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
        return self._says(ADMISSION_DENIAL_MARKERS)

    @property
    def is_conflict(self) -> bool:
        """Whether this was a lost write rather than an answer about the request.

        The one refusal worth retrying, and the only one podbench does: the API
        server compared the ``resourceVersion`` of the object podbench read
        against the one stored and found the object had moved under it. Nothing
        was created, so nothing was spent — in particular no ephemeral-container
        name, which is what lets
        :meth:`Kubectl.add_ephemeral_container` re-send under the *same* name
        rather than climbing the ladder (issue #136).

        Deliberately not true for any of the refusals the ladder acts on. A
        retry there would replace one honest verdict with several, and on a
        webhook that never answered it would also treble the wait.

        >>> from podbench.kubectl import CommandResult, KubectlError
        >>> lost = 'Error from server (Conflict): Operation cannot be \\
        ... fulfilled on pods "bl47p-ea-fastcs-01-0": the object has been \\
        ... modified; please apply your changes to the latest version and \\
        ... try again'
        >>> KubectlError(CommandResult((), 1, "", lost)).is_conflict
        True
        >>> psa = 'Error from server (Forbidden): pods "app" is forbidden: \\
        ... violates PodSecurity "restricted:latest": must not include \\
        ... "SYS_PTRACE" in securityContext.capabilities.add'
        >>> KubectlError(CommandResult((), 1, "", psa)).is_conflict
        False
        >>> unreachable = 'Error from server (InternalError): failed calling \\
        ... webhook "validate.kyverno.svc": context deadline exceeded'
        >>> KubectlError(CommandResult((), 1, "", unreachable)).is_conflict
        False
        """
        return self._says(CONFLICT_MARKERS)

    def _says(self, markers: Sequence[Sequence[str]]) -> bool:
        """Whether either stream matches one whole group of ``markers``.

        Both streams, because ``kubectl`` puts a refusal on stderr and some
        subcommands echo the server's message on stdout instead.
        """
        text = f"{self.stderr}\n{self.stdout}"
        return any(all(fragment in text for fragment in group) for group in markers)

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


class KubectlConflictError(KubectlError):
    """A write that lost the same optimistic-concurrency race every time.

    A :class:`KubectlError` for :class:`KubectlTimeoutError`'s reason — every
    verb already catches that and prints one sentence — and a distinct type
    only so that the sentence can be *this* one. Relaying the API server's
    ``Operation cannot be fulfilled`` unadorned is what the p47 run did on
    2026-08-24, and it reads as a defect in podbench rather than as the one
    failure where doing exactly the same thing again works.

    ``attempts`` is carried so the message can say how hard podbench tried: a
    reader deciding whether to re-run wants to know that this was not the first
    go.
    """

    def __init__(self, error: KubectlError, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(
            CommandResult(
                argv=error.argv,
                returncode=error.returncode,
                stdout=error.stdout,
                stderr=error.stderr,
            )
        )

    def _message(self, result: CommandResult) -> str:
        # The three beats a user-facing note gets (#203): what happened, what it
        # means here, what to do. The cluster's own words go last with nothing
        # after them, because relayed text may or may not end in a stop and a
        # sentence following it reads as more of the API server's.
        said = result.stderr.strip() or result.stdout.strip()
        return (
            f"{' '.join(result.argv)} hit a conflict on all {self.attempts} "
            "attempts: something else wrote to this pod between each read and "
            "each write. A conflict is transient and creates nothing, so "
            "nothing was changed and no container name was spent - re-run the "
            f"same command. The API server said: {said}"
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
        self._whoami: str | None = None
        self._whoami_asked = False

    # -- plumbing ---------------------------------------------------------

    @property
    def runner(self) -> Runner:
        """The subprocess seam this client was built with.

        Exposed for the one caller that has to run something *other than*
        kubectl on the same machine and must not fork it in a test:
        ``hotfix status`` asks a git remote from the laptop, because an exec
        session in the pod has no credentials for it. Reusing this seam is what
        keeps that call injected everywhere the cluster calls already are — a
        second default would be a unit suite that quietly reaches a forge.
        """
        return self._runner

    def base_argv(
        self, *, request_timeout: float | None = None, cluster_wide: bool = False
    ) -> list[str]:
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

        ``cluster_wide`` leaves the ``-n`` off, for the one read that spans
        namespaces. kubectl resolves ``-n X --all-namespaces`` in favour of the
        latter, so this changes no behaviour — it changes what podbench can be
        quoted as having asked. The argv is relayed verbatim when the call
        fails, and an RBAC refusal that arrives naming both a namespace and
        every namespace reads as podbench having asked for two contradictory
        things (measured 2026-08-23 against ``p47-beamline``).
        """
        argv = [self.binary]
        if self.kubeconfig is not None:
            argv += ["--kubeconfig", self.kubeconfig]
        if self.context is not None:
            argv += ["--context", self.context]
        if not cluster_wide:
            argv += ["-n", self.namespace]
        if request_timeout is not None and request_timeout > 0:
            argv.append(f"--request-timeout={request_timeout:g}s")
        return argv

    def for_namespace(self, namespace: str) -> Kubectl:
        """The same client, bound to another namespace.

        For the one listing that spans them: ``podbench hotfix status
        --all-namespaces`` finds its pods in one cluster-wide ``get pods`` and
        then has to ``exec`` into each, and an exec carrying the *caller's*
        namespace reads a different pod - or none. Everything else about the
        client is carried over deliberately, kubeconfig, context, binary and
        the injected runner included: a second client built from defaults would
        be a second set of credentials pointed at a second cluster.

        Returns ``self`` where the namespace is already this one, so the
        namespace-scoped caller's argv is unchanged.

        >>> here = Kubectl("demo")
        >>> here.for_namespace("demo") is here
        True
        >>> here.for_namespace("other").namespace
        'other'
        """
        if namespace == self.namespace:
            return self
        return Kubectl(
            namespace,
            context=self.context,
            kubeconfig=self.kubeconfig,
            binary=self.binary,
            runner=self._runner,
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

        ``cluster_wide`` is for a call that carries ``--all-namespaces`` and
        must not also carry a ``-n``; see :meth:`base_argv`.
        """
        streamed = bool(args) and args[0] in STREAMED_SUBCOMMANDS
        request = (
            None if streamed or timeout is None else timeout - REQUEST_TIMEOUT_HEADROOM
        )
        argv = self.base_argv(request_timeout=request, cluster_wide=cluster_wide)
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

    def logs(self, pod: str, container: str, *, tail: int = 500) -> str | None:
        """A container's log, or ``None`` if it cannot be read.

        A **read**, and that is the whole point of it: the seat's own start-up
        report is recovered from here rather than over an exec, so ``list`` and
        ``status`` stay one API call per pod against a namespace instead of one
        exec per seat (issue #99, #94b).

        ``check=False`` because every way this fails is a normal answer for the
        caller: RBAC granting ``get pods`` and not ``pods/log``, a container
        that has not started, a log the kubelet has already rotated. Each of
        them means "no measurement", which the caller reports as *not measured*
        - never as the rung the securityContext asked for.

        ``tail`` bounds what a wedged workload can make this cost. The agent
        writes its report at start-up and says almost nothing afterwards, so
        the bound is generous rather than tight; a report that has scrolled
        past it is simply not there, which is the same "no measurement".
        """
        result = self.run("logs", pod, "-c", container, f"--tail={tail}", check=False)
        return None if result.returncode != 0 else result.stdout

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

    def whoami(self) -> str | None:
        """Which user the API server attributes this kubeconfig to, or ``None``.

        Asked of the cluster rather than of the laptop, because the answer has
        to mean the same thing to everyone reading it back: ``$USER`` is a fact
        about a machine, and two people on one workstation - or one person on
        two - would collide or split under it. This is the identity that
        actually authored the container, spelled the way every other party to
        the pod already spells it.

        ``check=False`` and ``None`` rather than an error, in the shape
        :meth:`top_pod` sets: ``SelfSubjectReview`` is a 1.28 resource, an exec
        credential plugin can decline to run, and a role that grants
        ``pods/ephemeralcontainers`` need not grant ``create
        selfsubjectreviews``. None of those is a reason to refuse an attach -
        they are a reason for the seat to carry no owner and to be reported as
        having none.

        Cached because the answer cannot change within a run and every verb
        that picks a seat wants it; a failure is cached too, so a cluster
        without the resource costs one subprocess rather than one per pod.
        """
        if not self._whoami_asked:
            self._whoami = self._read_whoami()
            self._whoami_asked = True
        return self._whoami

    def _read_whoami(self) -> str | None:
        result = self.run("auth", "whoami", "-o", "json", check=False)
        if result.returncode != 0:
            return None
        try:
            review = _parse_json_object(result.stdout, result.argv)
        except (KubectlError, json.JSONDecodeError):
            return None
        name = as_dict(as_dict(review.get("status")).get("userInfo")).get("username")
        return name.strip() or None if isinstance(name, str) else None

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

        Formatted rather than truncated, because ``--timeout`` is a ``float`` on
        every verb that reaches here and Go's ``ParseDuration`` takes a decimal
        for any unit. ``int()`` turned ``--timeout 0.5`` into ``--timeout=0s``,
        which ``kubectl wait`` reads as "check once and do not wait" — the one
        value that means something else entirely.
        """
        return self.run(
            "wait",
            resource,
            f"--for={condition}",
            f"--timeout={timeout:g}s",
            timeout=timeout + WAIT_GRACE,
        )

    # -- ephemeral containers ---------------------------------------------

    def add_ephemeral_container(
        self,
        pod: str,
        spec: Mapping[str, Any],
        *,
        dry_run: bool = False,
        attempts: int = CONFLICT_ATTEMPTS,
        backoff: float = CONFLICT_BACKOFF,
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

        The whole body carries the ``resourceVersion`` of the GET it was built
        from, so anything writing to the pod in between costs this call a 409 —
        and the writer podbench most often races is *itself*, since ``--resize``
        patches the pod moments earlier and the kubelet writes the actuated
        resources back (issue #136, measured on p47 2026-08-24). That is retried
        here rather than by the caller, and under the **same container name**:
        the ladder spends a fresh name per rung because a name is burnt once
        used, but a conflict creates no container, and the splice above filters
        an existing entry of the same name before appending — so re-sending is
        idempotent by construction. Retrying a rung's *refusal* would be wrong
        for the opposite reason, which is why only
        :attr:`KubectlError.is_conflict` is retried.

        Raises :class:`KubectlError` on a synchronous refusal; check
        :attr:`KubectlError.is_psa_ptrace_denial` to tell a capability refusal
        from any other failure, and :class:`KubectlConflictError` for a race
        that never settled.
        """
        name = spec.get("name")
        path = f"/api/v1/namespaces/{self.namespace}/pods/{pod}/ephemeralcontainers"
        if dry_run:
            path += DRY_RUN_QUERY
        attempt = 0
        while True:
            attempt += 1
            # Re-read every time round: the point of a retry is the fresh
            # `resourceVersion`, and re-sending the body that just lost would
            # lose again for the same reason.
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
            try:
                result = self.raw_put(path, current)
            except KubectlError as error:
                if not error.is_conflict:
                    raise
                if attempt >= attempts:
                    raise KubectlConflictError(error, attempt) from error
                time.sleep(backoff)
            else:
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
