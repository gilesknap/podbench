"""What a seat spends that nothing in the cluster will bill it for.

Two budgets, and the pairing is not an accident: a seat can reserve neither of
them, and both are spent quietly. The first is *time* — how long a paused
workload has before the kubelet acts on it — and the second is *memory*, which
podbench shares with the pod it lands in because an ephemeral container may not
carry ``resources`` at all (report 3.9).

The memory half is at the bottom of this module and is the shorter one: the
pod's own spec states the ceiling, and :func:`memory_budget` subtracts what the
workload has reserved of it. Having that number is what makes the warning
conditional, and that is the point of computing it — the caution about tightly
limited pods used to be printed at every attach, including one that had just
raised the limit to 4Gi with ``--resize`` (issue #54).

The time half, which is the rest of the module:

A breakpoint in ``attach`` mode stops the target answering its probes, and the
kubelet cannot tell a process stopped in a debugger from one that has hung. Two
deadlines follow, and **the quiet one is worse**: readiness failing stops the
pod taking Service traffic — measured on a live cluster, its EndpointSlice
keeps the address and flips ``conditions.ready`` and ``serving`` to false — with
no restart count, and it re-joins on continue. Quiet rather than silent:
``Unhealthy`` events are emitted while it lasts and outlive it by the API
server's ``--event-ttl``, but *the pod's own status* keeps nothing — no restart
count moves — so unless those events are read back the symptom is traffic that
stopped arriving and it will not look like the debugger did it. Reading them
back is what :func:`probe_spend` is for. Liveness failing kills the container,
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

The other half is what a session has already **spent**, and it is the harder
half because the number that decides the outcome is not published anywhere.
``failureThreshold`` counts *consecutive* failures, and that counter lives in
the kubelet and resets on the first success; the pod's ``Unhealthy`` events are
the only trace, and they are aggregated — ``count=2`` twenty seconds apart on a
ten second period had a success between them and stood at 1/3 the whole time.
So :func:`probe_spend` reports the count and the last timestamp the events
actually carry and refuses to infer a streak from them, because the cheerful
version of that inference ("2 of 3 used") is wrong in the common case and wrong
in the direction that panics the reader.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .model import as_dict

__all__ = [
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_PERIOD_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "PROBE_FAILURE_REASON",
    "SEAT_WORKING_SET",
    "MemoryBudget",
    "ProbeBudget",
    "ProbeKind",
    "ProbeSpend",
    "TargetStatus",
    "format_probe_spend",
    "memory_budget",
    "memory_warning",
    "probe_budgets",
    "probe_qualifier",
    "probe_spend",
    "probe_warning",
    "target_status",
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

    def streak_cost(self, threshold: int) -> str:
        """What the ``threshold``-th *failure in a row* costs, in one clause.

        :attr:`consequence` is written for the warning that precedes a pause and
        has room to explain; this is written for a line reporting failures that
        have already happened, where the reader needs the stake and the word
        "row" and nothing else.

        >>> ProbeKind.LIVENESS.streak_cost(3)
        'a 3rd in a row kills the container, and the seat with it'
        """
        nth = _ordinal(threshold)
        return {
            ProbeKind.READINESS: (
                f"a {nth} in a row takes the pod out of its Service until a "
                "probe succeeds again"
            ),
            ProbeKind.LIVENESS: (
                f"a {nth} in a row kills the container, and the seat with it"
            ),
            ProbeKind.STARTUP: (
                f"a {nth} in a row kills the container, as for liveness"
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
        return self._mechanism("failures")

    @property
    def consecutive_mechanism(self) -> str:
        """The same three numbers, for a line that is reporting failures.

        ``failureThreshold`` is stated as "failures" where the reader has not
        had any yet and the spec's own word is the clearest one. Beside a count
        that has already happened it has to say **consecutive** on the line
        itself, because that line is what gets quoted into a chat window and
        the note underneath is what gets left behind. Same fields either way,
        so they are rendered in one place: the ``vscode-in-a-seat`` rule is
        that the numbers come from :mod:`podbench.budget` and are never
        restated by hand.

        >>> ProbeBudget(ProbeKind.READINESS, 5, 1, 3, 2).consecutive_mechanism
        '3 consecutive x 5s period, 1s timeout'
        """
        return self._mechanism("consecutive")

    def _mechanism(self, unit: str) -> str:
        return (
            f"{self.failure_threshold} {unit} x {self.period_seconds}s "
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


PROBE_FAILURE_REASON = "Unhealthy"
"""The kubelet's event ``reason`` for a failing probe — all three of them.

Which probe failed is only in the message (``Liveness probe failed: ...``), so
the reason narrows the stream and the message decides the kind.
"""

STREAK_NOTE = (
    "A count is not a streak. failureThreshold counts *consecutive* failures "
    "and the kubelet's counter resets on the first success, while these events "
    "are aggregated: failures further apart than the probe's own period had a "
    "success between them and never stood above 1, and nothing in an event "
    "says which case this is. Only a restart proves a liveness streak ever "
    "completed - and not every restart is one, which is why the last "
    "termination's reason is printed beside the count when the pod carries it."
)
"""Printed whenever a failure is reported, because the count invites the wrong
inference and the wrong inference is the alarming one.

Deliberately without the numbers from the incident that prompted this (two
failures 20s apart on a 10s period). They are the right illustration in a
commit message and the wrong one in a line printed under the reader's own
count, where "two of them" reads as *their* two and the arithmetic does not
hold on a 5s or a 60s period."""

TRACEBACK_NOTE = (
    "A pause longer than a probe's timeoutSeconds misses that probe, and the "
    "application sees it: a BrokenPipeError - or any broken-connection "
    "traceback - logged as you continue is the kubelet having hung up on a "
    "probe, not a bug in your code. One traceback, one missed probe; an "
    "application that swallows the exception leaves these events as the only "
    "evidence there was one."
)
"""Printed either way. It answers the question the events *provoke* - four
identical tracebacks appeared in the demo app's log during the pause that
prompted this, and matching them to the probe failures 1:1 took a session."""

EVENT_TTL_NOTE = (
    "Events expire, after the API server's --event-ttl (an hour by default), "
    "so nothing here means nothing retained rather than nothing happened."
)


@dataclass(frozen=True)
class ProbeSpend:
    """What one probe has cost so far, exactly as the events record it.

    Every field is transcribed rather than derived, because the one number a
    reader wants — how many *consecutive* failures — is not in the events at
    all. ``failures`` is the aggregated total, and two of them may be an hour
    apart.
    """

    kind: ProbeKind
    failures: int
    first: str
    """The event's own ``firstTimestamp``, kept because it is what says whether
    the count spans a moment the caller cares about — the seat landing."""

    last: str
    began_before: bool = False
    """Whether the series had already started before the window asked for, so
    part of ``failures`` belongs to something that happened before the seat."""


def probe_spend(
    events: Sequence[Mapping[str, Any]],
    container: str,
    *,
    since: str | None = None,
) -> tuple[ProbeSpend, ...]:
    """The probe failures ``container`` has accumulated, readiness first.

    ``since`` drops any series that had already finished before that moment,
    which is what makes this "spent by this session" rather than "spent by this
    pod". A series that straddles it is kept and flagged, because an aggregated
    count cannot be split.

    >>> events = [{"reason": "Unhealthy", "count": 2,
    ...     "message": 'Liveness probe failed: Get "http://10.42.0.23:8080/"',
    ...     "firstTimestamp": "2026-08-16T10:04:11Z",
    ...     "lastTimestamp": "2026-08-16T10:04:31Z",
    ...     "involvedObject": {"fieldPath": "spec.containers{app}"}}]
    >>> [(s.kind.value, s.failures, s.last) for s in probe_spend(events, "app")]
    [('liveness', 2, '2026-08-16T10:04:31Z')]
    >>> probe_spend(events, "app", since="2026-08-16T11:00:00Z")
    ()
    """
    start = _moment(since)
    seen: dict[ProbeKind, list[_Series]] = {}
    for event in events:
        series = _series(event, container)
        if series is None or (start is not None and series.last_at < start):
            continue
        seen.setdefault(series.kind, []).append(series)
    return tuple(
        ProbeSpend(
            kind=kind,
            failures=sum(item.count for item in group),
            first=min(group, key=lambda item: item.first_at).first,
            last=max(group, key=lambda item: item.last_at).last,
            began_before=start is not None
            and min(item.first_at for item in group) < start,
        )
        for kind in ProbeKind
        if (group := seen.get(kind))
    )


def format_probe_spend(
    container: str,
    budgets: Sequence[ProbeBudget],
    spend: Sequence[ProbeSpend],
    *,
    since: str | None = None,
    seat: str | None = None,
    target: TargetStatus | None = None,
) -> str:
    """The budget already spent, joined to the budget still available.

    Printed by ``status`` because this is the one budget in podbench that is
    spent *silently*: an ephemeral container cannot be restarted, so a liveness
    kill burns the seat's name for the pod's lifetime, and until now counting
    how close a session had come to that took two ``kubectl`` calls and knowing
    which fields to read.

    ``seat`` names the container whose landing ``since`` is, because ``status``
    lists every seat the pod has ever carried and only one of them sets this
    window.
    """
    landed = f"{seat} landed" if seat else "the seat landed"
    where = (
        f"since {landed} ({since})"
        if since is not None
        else "over every event the cluster still holds"
    )
    lines = [f"probes on {container!r} {where}"]
    spent = {item.kind: item for item in spend}
    lines.extend(
        f"  {_spend_line(budget, spent.get(budget.kind))}" for budget in budgets
    )
    declared = {budget.kind for budget in budgets}
    lines.extend(
        f"  {item.kind.value}: {_failures(item)}, last {item.last} - but "
        f"{container!r} declares no {item.kind.field} now, so this was spent "
        "against a spec that has since changed"
        for item in spend
        if item.kind not in declared
    )
    state = target if target is not None else TargetStatus()
    lines.extend(f"  {line}" for line in _proof_lines(state, budgets, since=since))
    if spend:
        lines.append(STREAK_NOTE)
    lines.append(TRACEBACK_NOTE)
    lines.append(EVENT_TTL_NOTE)
    return "\n".join(lines)


@dataclass(frozen=True)
class TargetStatus:
    """What the target container's *own status* proves, beside what the events
    only suggest.

    The events cannot show a streak, so everything here is the measured half of
    the report: a restart is what a completed liveness streak leaves behind, a
    container that is not ready has completed a readiness one, and the reason
    the last instance ended is what says whether a restart was a probe at all.
    Read by :func:`target_status`; every field is ``None`` when the pod does not
    say, because a missing field must cost its line and not the block.
    """

    restarts: int | None = None
    started_at: str | None = None
    """When the *current* instance started, which is what dates the last
    restart. :attr:`restarts` counts the container's whole life, so on its own
    it cannot tell a kill under this seat from one that happened yesterday;
    this stamp against the seat's landing can."""

    last_reason: str | None = None
    """``lastState.terminated.reason`` — ``Error`` for a liveness kill,
    ``OOMKilled`` for the pod hitting its memory limit, and so on."""

    last_exit_code: int | None = None
    ready: bool | None = None
    """The kubelet's own readiness verdict, which is the only *proof* on the
    readiness half: it goes false once a readiness streak completes, and back
    true on the first success afterwards."""


def target_status(pod_json: Mapping[str, Any], container: str) -> TargetStatus:
    """``container``'s status, as far as the pod JSON states it.

    >>> pod = {"status": {"containerStatuses": [{"name": "app",
    ...     "restartCount": 2, "ready": False,
    ...     "state": {"running": {"startedAt": "2026-08-16T09:00:00Z"}},
    ...     "lastState": {"terminated": {"reason": "OOMKilled",
    ...                                  "exitCode": 137}}}]}}
    >>> target_status(pod, "app")
    TargetStatus(restarts=2, started_at='2026-08-16T09:00:00Z', \
last_reason='OOMKilled', last_exit_code=137, ready=False)
    >>> target_status(pod, "sidecar")
    TargetStatus(restarts=None, started_at=None, last_reason=None, \
last_exit_code=None, ready=None)
    """
    for entry in _as_entries(as_dict(pod_json.get("status")).get("containerStatuses")):
        if entry.get("name") != container:
            continue
        terminated = as_dict(as_dict(entry.get("lastState")).get("terminated"))
        return TargetStatus(
            restarts=_whole(entry.get("restartCount")),
            started_at=_text(
                as_dict(as_dict(entry.get("state")).get("running")).get("startedAt")
            ),
            last_reason=_text(terminated.get("reason")),
            last_exit_code=_whole(terminated.get("exitCode")),
            ready=entry.get("ready") if isinstance(entry.get("ready"), bool) else None,
        )
    return TargetStatus()


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


# -- the spent half ---------------------------------------------------------


@dataclass(frozen=True)
class _Series:
    """One ``Unhealthy`` event, parsed far enough to be merged with its peers."""

    kind: ProbeKind
    count: int
    first: str
    first_at: datetime
    last: str
    last_at: datetime


def _spend_line(budget: ProbeBudget, item: ProbeSpend | None) -> str:
    """One probe: what it has cost, then what it still costs.

    Prose rather than columns, as :func:`_budget_line` is and for the same
    reason: the caller wraps this on whitespace, so a column would not survive
    to be compared. The word "consecutive" is repeated on every line rather
    than left to the note below, because the line is what gets quoted into a
    chat window and the note is what gets left behind.
    """
    threshold = budget.consecutive_mechanism
    # A probe that is not in force cannot exact its stake, and saying it would
    # contradicts what `_budget_line` prints for the same probe on the same
    # pod: a satisfied startup probe is done with this container instance, and
    # readiness and liveness are held off entirely while a startup probe is
    # still running.
    stake = (
        budget.kind.streak_cost(budget.failure_threshold)
        if budget.in_force
        else _held_off(budget)
    )
    if item is None:
        return f"{budget.kind.value}: none retained - {threshold}; {stake}"
    span = (
        f" (a series that began {item.first}, before the seat landed, so part "
        "of this count is not this session's)"
        if item.began_before
        else ""
    )
    return (
        f"{budget.kind.value}: {_failures(item)}, last {item.last}{span} - "
        f"{threshold}; {stake}"
    )


def _failures(item: ProbeSpend) -> str:
    """The count, pluralised.

    >>> _failures(ProbeSpend(ProbeKind.LIVENESS, 1, "a", "a"))
    '1 failure'
    """
    return f"{item.failures} failure{'' if item.failures == 1 else 's'}"


def _held_off(budget: ProbeBudget) -> str:
    """What a probe that cannot fire right now has at stake: nothing yet."""
    if budget.kind is ProbeKind.STARTUP:
        return (
            "already satisfied, so nothing is at stake on it until a restart "
            "brings the container up again"
        )
    return (
        "held off while the startup probe is still running, so nothing is at "
        "stake on it yet"
    )


def _proof_lines(
    target: TargetStatus, budgets: Sequence[ProbeBudget], *, since: str | None
) -> list[str]:
    """The measured half: what the container's own status says happened.

    Ordered after the counts because it is what the counts cannot prove, and
    each line is omitted rather than guessed at when the pod does not say.
    """
    lines: list[str] = []
    if any(budget.kind is ProbeKind.READINESS for budget in budgets):
        lines.extend(_ready_line(target))
    if target.restarts is not None:
        lines.append(f"restarts: {_restart_line(target, since=since)}")
    return lines


def _ready_line(target: TargetStatus) -> list[str]:
    """Printed only when the answer is bad news.

    A tick beside a healthy pod is the line the reader learns to skip, and this
    is the one place in the block that is a fact about *now* rather than about
    a window - so silence has to mean "in its Service".
    """
    if target.ready is not False:
        return []
    return [
        "not ready: the kubelet has this container out of its Service as of "
        "this call, which is what a completed readiness streak leaves behind. "
        "It clears on the first probe that succeeds, and nothing restarts"
    ]


def _restart_line(target: TargetStatus, *, since: str | None) -> str:
    """The restart count, dated against the seat and attributed where it can be.

    ``restartCount`` is the container's whole life, and reported bare it turns
    an unrelated OOM from yesterday into an alarm about this session. The
    current instance's ``startedAt`` dates the last one exactly, and
    ``lastState.terminated.reason`` says whether it was a probe at all - a
    liveness kill is ``Error``, while OOM, eviction and the process simply
    exiting move the same counter.
    """
    if target.restarts == 0:
        return "0 - nothing has been killed under this seat"
    ended = _termination(target)
    started, landed = _moment(target.started_at), _moment(since)
    if started is not None and landed is not None and started <= landed:
        return (
            f"{target.restarts} - all of them before this seat: the running "
            f"instance started {target.started_at}, which is not after the "
            f"seat landed, so nothing has been killed under it{ended}"
        )
    if started is not None and landed is not None:
        return (
            f"{target.restarts} - and the running instance started "
            f"{target.started_at}, after the seat landed, so at least one of "
            "those happened under it. A restart takes any seat in the pod with "
            "it and an ephemeral container cannot come back: check the seat "
            f"names above for a burnt one{ended}"
        )
    return (
        f"{target.restarts} - the container has been killed and restarted at "
        "least once. The pod does not date the last one, so this is over its "
        "whole life rather than this session, but a restart takes any seat in "
        "the pod with it and an ephemeral container cannot come back: check "
        f"the seat names above for a burnt one{ended}"
    )


def _termination(target: TargetStatus) -> str:
    """What the previous instance died of, when the pod still remembers."""
    if target.last_reason is None:
        return ""
    code = "" if target.last_exit_code is None else f", exit {target.last_exit_code}"
    return (
        f". The last one ended {target.last_reason}{code} - a liveness kill is "
        "one cause of that counter and not the only one"
    )


def _series(event: Mapping[str, Any], container: str) -> _Series | None:
    """One event as a failure series, or ``None`` if it is not this probe's.

    The container is read from ``involvedObject.fieldPath``
    (``spec.containers{app}``), and an event that does not name a container is
    dropped rather than assumed to be the target's: only the kubelet emits
    these and it always sets the field, so an unattributable one is not this,
    and a count credited to the wrong container is worse than a missing one.
    """
    if event.get("reason") != PROBE_FAILURE_REASON:
        return None
    kind = _probe_kind(event.get("message"))
    if kind is None or _container_of(event) != container:
        return None
    # What arrives is core/v1: `kubectl get events` asks for that resource, and
    # the field selector this is fetched with (`involvedObject.name`) exists
    # nowhere else. So the shape read first is the core one - a top-level
    # `count` with first/lastTimestamp. The events.k8s.io keys are read after
    # it because the conversion out of the shared storage is the API server's
    # and not ours: an aggregated record that arrived in that shape carries its
    # repeat in `series.count`, or in `deprecatedCount` when it never
    # aggregated, and counting six failures as one is the wrong direction to be
    # wrong in. An event carrying only one usable stamp counts as a moment
    # rather than being dropped.
    series = as_dict(event.get("series"))
    last = _text(
        event.get("lastTimestamp"),
        event.get("deprecatedLastTimestamp"),
        series.get("lastObservedTime"),
        event.get("eventTime"),
        event.get("firstTimestamp"),
    )
    first = _text(
        event.get("firstTimestamp"),
        event.get("deprecatedFirstTimestamp"),
        event.get("eventTime"),
        last,
    )
    if first is None or last is None:
        return None
    first_at, last_at = _moment(first), _moment(last)
    if first_at is None or last_at is None:
        return None
    return _Series(
        kind=kind,
        count=_repeats(
            series.get("count"), event.get("count"), event.get("deprecatedCount")
        ),
        first=first,
        first_at=first_at,
        last=last,
        last_at=last_at,
    )


def _probe_kind(message: Any) -> ProbeKind | None:
    """Which probe an ``Unhealthy`` message is about.

    The prefix stops at ``"<Kind> probe "`` because the kubelet writes *two*
    of these under this reason: ``"%s probe failed: %s"`` when the probe
    answered wrongly, and ``"%s probe errored: %v"`` when the attempt could not
    be made at all — an exec the runtime timed out, a connection it could not
    open. Both come back from the prober as a failure and both move
    ``failureThreshold``'s counter, so matching only "failed" undercounts the
    spend against the very threshold it is printed beside, and does it on the
    exec-probed pods where a pause is most likely to error rather than fail.
    Newer kubelets lengthen the errored wording again, which the shorter prefix
    also survives; the ``ProbeWarning`` reason has its own and is filtered out
    before this by :data:`PROBE_FAILURE_REASON`.

    >>> _probe_kind("Liveness probe failed: HTTP probe failed").value
    'liveness'
    >>> _probe_kind("Readiness probe errored: rpc error: DeadlineExceeded").value
    'readiness'
    >>> _probe_kind("Back-off restarting failed container") is None
    True
    """
    if not isinstance(message, str):
        return None
    for kind in ProbeKind:
        if message.startswith(f"{kind.value.capitalize()} probe "):
            return kind
    return None


def _container_of(event: Mapping[str, Any]) -> str | None:
    """The container an event's ``fieldPath`` names, if it names one.

    >>> _container_of({"involvedObject": {"fieldPath": "spec.containers{app}"}})
    'app'
    """
    path = as_dict(event.get("involvedObject")).get("fieldPath")
    if not isinstance(path, str) or not path.endswith("}"):
        return None
    _, _, name = path.partition("{")
    return name[:-1] or None


def _repeats(*values: Any) -> int:
    """How many failures one event stands for, defaulting to the one it is.

    A count of 0 is what an un-aggregated event carries in some API versions,
    and reporting the pod as having failed zero times because it failed once is
    the wrong direction to be wrong in.

    >>> _repeats(None, 4), _repeats(None, 0)
    (4, 1)
    """
    for value in values:
        if not isinstance(value, bool) and isinstance(value, int) and value > 0:
            return value
    return 1


def _text(*values: Any) -> str | None:
    """The first of ``values`` that is a non-empty string.

    Every field it reads is one the API server writes and a hand-built fixture
    may not, so the fallbacks are the point rather than the type check.
    """
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _whole(value: Any) -> int | None:
    """An integer field, or ``None`` when the pod does not state one.

    ``bool`` is excluded because it is an ``int`` in Python and never one here:
    a ``restartCount`` of ``True`` is a malformed pod, not one restart.

    >>> _whole(0), _whole(True), _whole("2")
    (0, None, None)
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _moment(value: str | None) -> datetime | None:
    """An RFC 3339 stamp as a comparable instant, or ``None`` if unreadable.

    A naive stamp is read as UTC: everything the API server writes is Zulu, and
    the alternative is a ``TypeError`` on the comparison rather than an answer.

    >>> _moment("2026-08-16T10:04:31Z").hour
    10
    >>> _moment("last tuesday") is None
    True
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _ordinal(number: int) -> str:
    """``3`` as ``3rd``, so a threshold can be named in the sentence about it.

    >>> [_ordinal(n) for n in (1, 2, 3, 4, 11, 21)]
    ['1st', '2nd', '3rd', '4th', '11th', '21st']
    """
    if number % 100 in (11, 12, 13):
        return f"{number}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _as_entries(value: Any) -> list[dict[str, Any]]:
    """A JSON list of objects, or an empty one."""
    if not isinstance(value, list):
        return []
    return [as_dict(entry) for entry in value]  # pyright: ignore[reportUnknownVariableType]


# -- the memory budget ------------------------------------------------------


MIB = 1024 * 1024
GIB = 1024 * MIB

SEAT_WORKING_SET = 1300 * MIB
"""What a seat costs when VS Code is the client, in bytes.

Measured rather than budgeted for: vscode-server alone is 700-800 MB RSS, and
1.1-1.3 GB once the extensions are in (the ``vscode-in-a-seat`` skill). The top
of that range is the number used, because the failure this decides whether to
warn about is unrecoverable - an OOM-killed ephemeral container cannot be
restarted, and its name is burnt for the pod's lifetime.
"""


@dataclass(frozen=True)
class MemoryBudget:
    """The pod's memory ceiling, and how much of it a seat can hope for."""

    limit: int | None
    """The pod's ceiling in bytes, or ``None`` when it has none.

    The sum of the containers' memory limits, because that is what the pod's
    own cgroup is given - and ``None`` as soon as one container declares no
    limit, since a single unlimited container leaves the pod unlimited too. An
    ephemeral container is added after that sum is computed and never changes
    it, which is the whole reason the seat's allocations come out of the
    workload's ceiling.
    """

    reserved: int
    """What the workload has reserved of it, in bytes.

    Requests, not usage: usage needs metrics-server, and podbench shells out to
    ``kubectl`` alone. A container that states a limit and no request has the
    limit as its request, which is the API server's own defaulting, so a pod
    that pins limits without requests correctly comes out with nothing spare.
    """

    @property
    def free(self) -> int | None:
        """Bytes inside the ceiling that nothing has reserved.

        An upper bound on what a seat can take without pushing the workload out
        of its guaranteed share, and ``None`` when there is no ceiling to be
        inside.

        >>> MemoryBudget(512 * MIB, 256 * MIB).free == 256 * MIB
        True
        >>> MemoryBudget(None, 0).free is None
        True
        """
        if self.limit is None:
            return None
        return max(0, self.limit - self.reserved)

    @property
    def fits(self) -> bool:
        """Whether a seat has room here, and so whether to say anything at all.

        An unlimited pod fits by construction: there is no cgroup ceiling for
        the seat to hit, and warning about one would be the report asserting
        something it has not measured.

        >>> MemoryBudget(4 * GIB, 256 * MIB).fits
        True
        >>> MemoryBudget(512 * MIB, 256 * MIB).fits
        False
        """
        free = self.free
        return free is None or free >= SEAT_WORKING_SET

    @property
    def suggestion(self) -> str:
        """A ``--resize`` value that would make this pod fit, in whole GiB.

        Whole GiB because the reader is going to type it, and a limit stated to
        the megabyte would be a false precision on a working set that is a
        range.

        >>> MemoryBudget(512 * MIB, 256 * MIB).suggestion
        '2Gi'
        >>> MemoryBudget(4 * GIB, 4 * GIB).suggestion
        '6Gi'
        """
        needed = self.reserved + SEAT_WORKING_SET
        return _human(-(-needed // GIB) * GIB)


def memory_budget(pod_json: Mapping[str, Any]) -> MemoryBudget:
    """The ceiling this pod puts on a seat, read from the spec it already has.

    >>> pod = {"spec": {"containers": [{"name": "app", "resources": {
    ...     "limits": {"memory": "512Mi"}, "requests": {"memory": "256Mi"}}}]}}
    >>> budget = memory_budget(pod)
    >>> _human(budget.limit or 0), _human(budget.free or 0)
    ('512Mi', '256Mi')
    >>> memory_budget({"spec": {"containers": [{"name": "app"}]}}).limit is None
    True
    """
    limit = 0
    reserved = 0
    unbounded = False
    for container in _as_entries(as_dict(pod_json.get("spec")).get("containers")):
        resources = as_dict(container.get("resources"))
        stated = _quantity(as_dict(resources.get("limits")).get("memory"))
        request = _quantity(as_dict(resources.get("requests")).get("memory"))
        if stated is None:
            unbounded = True
        else:
            limit += stated
        reserved += request if request is not None else (stated or 0)
    return MemoryBudget(limit=None if unbounded else limit, reserved=reserved)


def memory_warning(budget: MemoryBudget) -> str | None:
    """The one line an attach owes a pod too small for a seat, or ``None``.

    Silence when it fits is the whole change: the caution this replaces was
    unconditional, so an attach that had just raised the limit to 4Gi with
    ``--resize`` was told in ten lines that tightly limited pods OOM (issue
    #54). The reasoning - that the seat may not carry ``resources`` of its own,
    and that an OOM'd ephemeral container is unrecoverable - is one command
    away in ``podbench status --explain``.

    >>> memory_warning(MemoryBudget(4 * GIB, 256 * MIB)) is None
    True
    >>> memory_warning(MemoryBudget(512 * MIB, 256 * MIB))
    'memory: 256Mi free of a 512Mi pod limit, and a VS Code seat needs 1.1-1.3Gi - re-attach with --resize 2Gi'
    """  # noqa: E501
    free = budget.free
    if budget.fits or free is None or budget.limit is None:
        return None
    return (
        f"memory: {_human(free)} free of a {_human(budget.limit)} pod limit, "
        "and a VS Code seat needs 1.1-1.3Gi - re-attach with "
        f"--resize {budget.suggestion}"
    )


_SUFFIXES = {
    "": 1,
    "k": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "E": 10**18,
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
}

_QUANTITY = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(?:[eE]([+-]?[0-9]+))?([EPTGMk]i?)?$")


def _quantity(value: Any) -> int | None:
    """A Kubernetes quantity as bytes, or ``None`` if it is not one.

    Unreadable is not zero and not unlimited: it means unmeasured, and every
    caller here treats that as nothing to say. A quantity the API server would
    have rejected cannot reach a live pod's JSON anyway.

    >>> [_quantity(text) for text in ("512Mi", "1.5Gi", "128974848", "129e6")]
    [536870912, 1610612736, 128974848, 129000000]
    >>> _quantity("a lot") is None
    True
    """
    if not isinstance(value, str):
        return None
    match = _QUANTITY.match(value.strip())
    if match is None:
        return None
    mantissa, exponent, suffix = match.groups()
    scale = _SUFFIXES[suffix or ""] * 10 ** int(exponent or 0)
    return int(float(mantissa) * scale)


def _human(size: int) -> str:
    """Bytes back in the units the pod spec would have stated them in.

    >>> [_human(n) for n in (536870912, 4 * GIB, 1610612736, 0)]
    ['512Mi', '4Gi', '1.5Gi', '0']
    """
    for unit, name in ((GIB, "Gi"), (MIB, "Mi"), (1024, "Ki")):
        if size >= unit:
            if size % unit == 0:
                return f"{size // unit}{name}"
            return f"{size / unit:.1f}{name}"
    return str(size)
