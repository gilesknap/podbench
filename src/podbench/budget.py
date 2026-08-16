"""How long a paused workload has before the kubelet acts on it.

A breakpoint in ``attach`` mode stops the target answering its probes, and the
kubelet cannot tell a process stopped in a debugger from one that has hung. Two
deadlines follow, and **the quiet one is worse**: readiness failing drops the
pod from its Service's endpoints with no event and no restart count, and it
re-joins on continue — so the symptom is traffic that stopped arriving, and it
will not look like the debugger did it. Liveness failing kills the container,
and the seat's debug session goes with it.

Neither can be turned off in place. A pod update may change only
``containers[*].image``, ``initContainers[*].image``, ``activeDeadlineSeconds``,
``tolerations`` (additions to existing ones) and
``terminationGracePeriodSeconds``; probes are not in that list, and unlike
``resources`` — which is what makes ``--resize`` possible — they have no
resize-style subresource. ``podbench dev`` is the way out, because
:func:`podbench.spec.dev_pod_spec` strips all three by construction.

So what is left to podbench is arithmetic. Every number is in the pod spec it
already reads, which makes this an exact deadline rather than a general
caution — and the difference between "you may explore freely" and "you have
thirty seconds" is precisely what someone about to set a breakpoint needs.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .model import as_dict

__all__ = [
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_PERIOD_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "ProbeBudget",
    "ProbeKind",
    "probe_budgets",
    "probe_qualifier",
    "probe_warning",
]

DEFAULT_PERIOD_SECONDS = 10
DEFAULT_TIMEOUT_SECONDS = 1
DEFAULT_FAILURE_THRESHOLD = 3
"""The API server's defaults, restated because a spec may omit them.

A live pod's JSON always carries them — defaulting happens on admission — but
the fixtures in the unit suite are written by hand, and a budget computed from
an omitted field has to come out the same as one the server filled in.
"""


class ProbeKind(enum.Enum):
    """One of the three probes, and what the kubelet does when it gives up."""

    READINESS = "readiness"
    LIVENESS = "liveness"
    STARTUP = "startup"

    @property
    def field(self) -> str:
        """The container-spec key this probe is read from.

        >>> ProbeKind.READINESS.field
        'readinessProbe'
        """
        return f"{self.value}Probe"

    @property
    def consequence(self) -> str:
        """What happens at the end of the budget, and how visible it is."""
        return {
            ProbeKind.READINESS: (
                "the pod goes not-ready and stops taking Service traffic - its "
                "EndpointSlice keeps the address and flips conditions.ready to "
                "false. Quiet: the pod stays Running, the restart count does "
                "not move, and it recovers within a probe period of continuing, "
                "so afterwards nothing points at the debugger"
            ),
            ProbeKind.LIVENESS: (
                "the container is killed and restarted - and the seat, which "
                "shares its namespaces, is killed with it. An ephemeral "
                "container cannot be restarted, so that name is burnt and "
                "coming back takes `attach --new`"
            ),
            ProbeKind.STARTUP: (
                "the container is killed and restarted, as for liveness - a "
                "startup probe that gives up is a start that failed"
            ),
        }[self]


@dataclass(frozen=True)
class ProbeBudget:
    """One probe's numbers, and the deadline they put on a pause."""

    kind: ProbeKind
    period_seconds: int
    timeout_seconds: int
    failure_threshold: int
    initial_delay_seconds: int
    in_force: bool = True
    """Whether this probe can fire at all right now.

    A startup probe that has already succeeded cannot, and while one is *still*
    running the kubelet disables the other two — so on a container that is
    Running but not yet started, the liveness deadline the spec states is not
    the deadline in effect.
    """

    @property
    def pace(self) -> int:
        """Seconds between one failing attempt and the next.

        The period is the tick, but a probe worker does not run two attempts of
        the same probe at once: an attempt against a stopped process is only
        abandoned at ``timeoutSeconds``, so a timeout longer than the period
        paces the attempts itself.

        >>> ProbeBudget(ProbeKind.LIVENESS, 5, 1, 3, 0).pace
        5
        >>> ProbeBudget(ProbeKind.LIVENESS, 5, 30, 3, 0).pace
        30
        """
        return max(self.period_seconds, self.timeout_seconds)

    @property
    def earliest(self) -> int:
        """Seconds of pause after which the consequence *can* land.

        Counting from a pause that begins just as an attempt does: that attempt
        is abandoned at ``timeoutSeconds``, and each of the remaining
        ``failureThreshold - 1`` failures costs another :attr:`pace`.

        >>> ProbeBudget(ProbeKind.READINESS, 5, 1, 3, 2).earliest
        11
        """
        return (self.failure_threshold - 1) * self.pace + self.timeout_seconds

    @property
    def latest(self) -> int:
        """Seconds of pause after which it *has* landed.

        The pause can begin anywhere in the cycle, and at worst it begins just
        after an attempt succeeded — which delays the first failing attempt by
        one full :attr:`pace` and nothing else.

        >>> ProbeBudget(ProbeKind.READINESS, 5, 1, 3, 2).latest
        16
        """
        return self.earliest + self.pace

    @property
    def window(self) -> str:
        """The deadline as a range, because where the pause falls in the probe
        cycle is not knowable from here.

        >>> ProbeBudget(ProbeKind.LIVENESS, 10, 1, 3, 5).window
        '21-31s'
        """
        return f"{self.earliest}-{self.latest}s"

    @property
    def mechanism(self) -> str:
        """The numbers this budget came from, in the spec's own words.

        >>> ProbeBudget(ProbeKind.READINESS, 5, 1, 3, 2).mechanism
        '3 failures x 5s period, 1s timeout'
        """
        return (
            f"{self.failure_threshold} failures x {self.period_seconds}s "
            f"period, {self.timeout_seconds}s timeout"
        )


def probe_budgets(
    pod_json: Mapping[str, Any], container: str
) -> tuple[ProbeBudget, ...]:
    """Every probe on ``container``, readiness first.

    Readiness leads because it is the deadline that arrives soonest and says
    least: the liveness restart is at least loud enough to be noticed.

    >>> pod = {"spec": {"containers": [{"name": "app", "readinessProbe": {
    ...     "periodSeconds": 5, "initialDelaySeconds": 2}}]}}
    >>> [(b.kind.value, b.window) for b in probe_budgets(pod, "app")]
    [('readiness', '11-16s')]
    >>> probe_budgets(pod, "sidecar")
    ()
    """
    spec = _container_spec(pod_json, container)
    # A startup probe that has not yet succeeded holds the other two off
    # entirely, so this decides which deadline is the one actually in effect
    # rather than merely annotating the startup line.
    holding = spec.get(ProbeKind.STARTUP.field) is not None and not _started(
        pod_json, container
    )
    return tuple(
        _budget(
            kind,
            as_dict(spec.get(kind.field)),
            in_force=holding if kind is ProbeKind.STARTUP else not holding,
        )
        for kind in (ProbeKind.READINESS, ProbeKind.LIVENESS, ProbeKind.STARTUP)
        if spec.get(kind.field) is not None
    )


def probe_warning(container: str, budgets: Sequence[ProbeBudget]) -> str | None:
    """The WARNING text for a probed target, or ``None`` when it has none.

    Silence is the right output for an unprobed pod: a warning that says
    nothing is wrong teaches the reader to skip the block that one day will
    say something is. The good news goes to :func:`probe_qualifier` instead,
    which prints either way.
    """
    if not budgets:
        return None
    lines = [
        f"a breakpoint on {container!r} is on a timer: it answers probes, and "
        "a process stopped in a debugger does not - which the kubelet cannot "
        "tell from a hang."
    ]
    lines.extend(f"  {_budget_line(budget)}" for budget in budgets)
    lines.append(
        "Probes cannot be changed on a running pod - a pod update may only "
        "change image, activeDeadlineSeconds, tolerations and "
        "terminationGracePeriodSeconds, and unlike resources they have no "
        "resize-style subresource. So live attach here is a short-visit tool: "
        "break, look, continue, or use logpoints, which never stop the "
        "process. For an unlimited pause use `podbench dev`, which strips all "
        "three probes."
    )
    if any(budget.initial_delay_seconds for budget in budgets):
        lines.append(
            "initialDelaySeconds is not in these numbers on purpose: it shifts "
            "when probing begins after a start or a restart, and adds nothing "
            "to a pause on a container that is already up."
        )
    return "\n".join(lines)


def probe_qualifier(container: str, budgets: Sequence[ProbeBudget]) -> str:
    """One line qualifying what live attach means on this pod.

    A bare tick beside "live attach" says the same thing on a pod that will
    restart under you in twenty seconds as on one that will wait all afternoon,
    and those are different products. The arithmetic stays in
    :func:`probe_warning`; this is the qualifier on the tick.

    """
    live = [budget for budget in budgets if budget.in_force]
    if not live:
        if budgets:
            return (
                f"no deadline right now: {container!r} declares probes but none "
                "is in force, so nothing removes it from a Service or restarts "
                "it while it is stopped"
            )
        return (
            f"no deadline: {container!r} declares no readiness, liveness or "
            "startup probe, so nothing removes it from a Service or restarts "
            "it while it is stopped"
        )
    soonest = min(live, key=lambda budget: budget.earliest)
    return (
        f"TIME-LIMITED: {container!r} answers probes, so a pause has a "
        f"deadline - the first is {soonest.kind.value} at {soonest.window}. "
        "The WARNING below has the arithmetic and the way out"
    )


def _budget_line(budget: ProbeBudget) -> str:
    """One probe, deadline first.

    Not a table: the warning block wraps on whitespace, so a column that
    survived here would not survive the wrap. The number leads instead, which
    is the field being compared anyway.
    """
    name = budget.kind.value
    if budget.in_force:
        return (
            f"{name}, {budget.window} into a pause: {budget.kind.consequence}. "
            f"{budget.mechanism}."
        )
    if budget.kind is ProbeKind.STARTUP:
        return (
            f"{name}: already satisfied, so it applies again only to the "
            f"container a restart would bring up. {budget.mechanism}."
        )
    return (
        f"{name}: held off while the startup probe is still running, so it is "
        f"not the deadline in effect yet. {budget.mechanism}."
    )


def _budget(
    kind: ProbeKind, probe: Mapping[str, Any], *, in_force: bool
) -> ProbeBudget:
    return ProbeBudget(
        kind=kind,
        period_seconds=_count(probe.get("periodSeconds"), DEFAULT_PERIOD_SECONDS),
        timeout_seconds=_count(probe.get("timeoutSeconds"), DEFAULT_TIMEOUT_SECONDS),
        failure_threshold=_count(
            probe.get("failureThreshold"), DEFAULT_FAILURE_THRESHOLD
        ),
        # The only field whose floor is its default, so a stated 0 and an
        # omitted one are the same number.
        initial_delay_seconds=_count(probe.get("initialDelaySeconds"), 0, minimum=0),
        in_force=in_force,
    )


def _count(value: Any, default: int, *, minimum: int = 1) -> int:
    """A probe field, or the number the API server would have defaulted it to.

    A value the server would have rejected is not podbench's to repair: a
    budget computed from one would be a confident wrong number, where the
    default is at least the number the field would have had.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return default
    return value


def _container_spec(pod_json: Mapping[str, Any], container: str) -> Mapping[str, Any]:
    containers = as_dict(pod_json.get("spec")).get("containers")
    if not isinstance(containers, list):
        return {}
    for entry in containers:  # pyright: ignore[reportUnknownVariableType]
        spec = as_dict(entry)
        if spec.get("name") == container:
            return spec
    return {}


def _started(pod_json: Mapping[str, Any], container: str) -> bool:
    """Whether the kubelet says ``container``'s startup probe has succeeded.

    Unknown reads as started, which is the answer for every pod ``attach`` can
    land a seat in: a container still working through its startup probe is one
    the kubelet may be about to restart, and a seat added to it would be racing
    that. Defaulting the other way would tell a normal, long-running pod that
    its liveness deadline was suspended.
    """
    statuses = as_dict(pod_json.get("status")).get("containerStatuses")
    if not isinstance(statuses, list):
        return True
    for entry in statuses:  # pyright: ignore[reportUnknownVariableType]
        status = as_dict(entry)
        if status.get("name") == container:
            started = status.get("started")
            return started if isinstance(started, bool) else True
    return True
