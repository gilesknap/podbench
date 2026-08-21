"""In-place resize, and the two things a namespace can do about it.

``--resize`` raises the *target* container's limits through the ``resize``
subresource, because that is the only lever a seat has: an ephemeral container
may not declare ``resources`` at all (report 3.9), so it lives inside the pod's
cgroup and the pod's ceiling is the sum of its containers' limits.

Raising a limit alone is not enough, and the way it fails is arithmetic rather
than permission. A ``LimitRange`` with ``maxLimitRequestRatio`` bounds
*limit ÷ request*, so every increase to a limit widens the ratio, and a request
left where it was eventually breaks it::

    pods "bl01t-mo-sim-01-0" is forbidden: memory max limit to request ratio
    per Container is 10, but provided ratio is 96.000000

That is a 64Mi request under a 6Gi limit — measured at Diamond on 2026-08-16,
where it made ``--resize`` unusable on every pod in the namespace. So the
request moves with the limit, and by how much is *read from the namespace*
rather than guessed at.

Two invariants constrain the answer:

* **QoS class is immutable.** A pod whose requests all equal its limits is
  Guaranteed, and the API server refuses a resize that would move a pod between
  classes — measured on k3s v1.34.6::

      The Pod "victim" is invalid: spec: Invalid value: "Burstable": Pod QOS
      Class may not change as a result of resizing

  So a request is never raised to meet its limit unless it was already there.
* **A request is never lowered.** Shrinking one silently gives the workload
  less than it was scheduled with, which is not what a debugging flag is for.

And one container shape cannot be resized at all by any released Kubernetes,
whatever the numbers say: one holding a ``resources.claims`` entry. See
:func:`explain_claim_refusal`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, cast

from .model import as_dict

__all__ = [
    "CPU",
    "EDITOR_HEADROOM",
    "GI",
    "MEMORY",
    "MUTABLE_RESOURCES_REFUSAL",
    "QOS_REFUSAL",
    "SEAT_FOOTPRINT",
    "SEAT_HEADROOM",
    "Headroom",
    "ResizeError",
    "ResizePlan",
    "Want",
    "editor_limit",
    "explain_claim_refusal",
    "format_cpu",
    "format_memory",
    "namespace_limits",
    "parse_quantity",
    "parse_top_memory",
    "parse_want",
    "plan_resize",
    "pod_memory_limit",
    "qos_preserving_flags",
]

MEMORY = "memory"
CPU = "cpu"

QOS_REFUSAL = "QOS Class may not change as a result of resizing"
"""The API server's refusal of a resize that would move a pod between classes.

Matched on the clause rather than the whole sentence, which names the class it
would have become and the field path it was found on::

    Invalid value: "Guaranteed": Pod QOS Class may not change as a result of
    resizing

`Guaranteed` is the one that cannot be worked around by choosing a better
number - see :func:`qos_preserving_flags`.
"""

MUTABLE_RESOURCES_REFUSAL = "only cpu and memory resources are mutable"
"""The API server's whole vocabulary for "your resize changed something else".

Matched rather than parsed because it is the *entire* message: the check behind
it is a struct comparison over the whole container, and the field path it
reports is ``spec``. See :func:`explain_claim_refusal` for the one cause that
names cpu and memory while being neither.
"""

_BINARY: dict[str, int] = {
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "Ei": 2**60,
}
_DECIMAL: dict[str, Fraction] = {
    "n": Fraction(1, 10**9),
    "u": Fraction(1, 10**6),
    "m": Fraction(1, 10**3),
    "": Fraction(1),
    "k": Fraction(10**3),
    "M": Fraction(10**6),
    "G": Fraction(10**9),
    "T": Fraction(10**12),
    "P": Fraction(10**15),
    "E": Fraction(10**18),
}

_QUANTITY = re.compile(r"^(?P<number>[0-9]+(?:\.[0-9]+)?)(?P<suffix>[A-Za-z]*)$")

MI = _BINARY["Mi"]
KI = _BINARY["Ki"]
GI = _BINARY["Gi"]


class ResizeError(ValueError):
    """A resize podbench refuses to submit, with the arithmetic that refused it.

    Raised rather than reported so the caller can print the numbers *instead of*
    the API server's, which names a ratio and not the value that would work.
    """


def parse_quantity(text: str) -> Fraction:
    """A Kubernetes quantity as an exact :class:`~fractions.Fraction`.

    Exact because every use here is a comparison or a division, and 6Gi over a
    ratio of 10 is not representable in binary floating point — a request that
    is off by one byte is refused by the same rule it was computed to satisfy.

    >>> parse_quantity("6Gi") == 6 * 2**30
    True
    >>> parse_quantity("500m")
    Fraction(1, 2)
    >>> parse_quantity("2")
    Fraction(2, 1)
    """
    match = _QUANTITY.match(text.strip())
    if match is None:
        raise ResizeError(
            f"{text!r} is not a Kubernetes quantity: expected a number with an "
            "optional suffix, like 6Gi, 512Mi, 500m or 2"
        )
    number = Fraction(match.group("number"))
    suffix = match.group("suffix")
    if suffix in _BINARY:
        return number * _BINARY[suffix]
    if suffix in _DECIMAL:
        return number * _DECIMAL[suffix]
    raise ResizeError(
        f"{text!r} has an unknown suffix {suffix!r}: use Ki/Mi/Gi/Ti for "
        "memory, m for millicores, or no suffix at all"
    )


def format_memory(value: Fraction) -> str:
    """``value`` bytes in the largest unit that spells it exactly.

    The largest unit, so that what the user typed is what the report says back:
    ``--resize 6Gi`` that returned "resized to 6144Mi" reads like podbench
    changed the request. Where no unit is exact — every derived request — the
    value is rounded **up** at Mi, because it was computed to satisfy a ratio
    and rounding down would put it back under the bound.

    >>> format_memory(Fraction(6 * 2**30))
    '6Gi'
    >>> format_memory(Fraction(6 * 2**30, 10))
    '615Mi'
    >>> format_memory(Fraction(4096))
    '4Ki'
    """
    for suffix in ("Ti", "Gi", "Mi", "Ki"):
        unit = _BINARY[suffix]
        if value >= unit and value % unit == 0:
            return f"{int(value // unit)}{suffix}"
    if value >= MI:
        return f"{math.ceil(value / MI)}Mi"
    if value >= KI:
        return f"{math.ceil(value / KI)}Ki"
    return str(math.ceil(value))


def format_cpu(value: Fraction) -> str:
    """``value`` cores, as cores when whole and millicores otherwise.

    >>> format_cpu(Fraction(4))
    '4'
    >>> format_cpu(Fraction(4, 10))
    '400m'
    """
    if value.denominator == 1:
        return str(value.numerator)
    return f"{math.ceil(value * 1000)}m"


@dataclass(frozen=True)
class Want:
    """What the user asked for on one resource: a limit, and maybe a request."""

    limit: Fraction
    request: Fraction | None = None
    """``None`` means "whatever the namespace requires", which is what
    :func:`plan_resize` works out. An explicit value is used as-is and only
    checked, because a number the user typed is a decision, not a default."""


def parse_want(text: str, *, resource: str) -> Want:
    """``LIMIT`` or ``REQUEST:LIMIT``, in the order they are written on a pod.

    Request first because that is the order ``resources`` prints in and the
    order the two are read in — ``1Gi:6Gi`` says "reserve one, allow six".

    >>> parse_want("6Gi", resource=MEMORY).request is None
    True
    >>> parse_want("1Gi:6Gi", resource=MEMORY).limit == 6 * 2**30
    True
    """
    request_text, separator, limit_text = text.partition(":")
    if not separator:
        return Want(limit=parse_quantity(text))
    if not limit_text.strip():
        raise ResizeError(
            f"--resize{'' if resource == MEMORY else '-cpu'} {text!r} names a "
            "request with no limit after the colon: write REQUEST:LIMIT, or "
            "just the limit"
        )
    request = parse_quantity(request_text)
    limit = parse_quantity(limit_text)
    if request > limit:
        raise ResizeError(
            f"{text!r} asks for a {resource} request above its limit "
            f"({request_text.strip()} > {limit_text.strip()})"
        )
    return Want(limit=limit, request=request)


@dataclass(frozen=True)
class NamespaceLimits:
    """What the namespace's ``LimitRange`` objects say about one container.

    The most restrictive value across every range is the one that decides, since
    all of them are enforced and the first refusal is the whole answer.
    """

    max_limit: dict[str, Fraction]
    max_ratio: dict[str, Fraction]

    def ratio(self, resource: str) -> Fraction | None:
        return self.max_ratio.get(resource)

    def ceiling(self, resource: str) -> Fraction | None:
        return self.max_limit.get(resource)


def namespace_limits(documents: Sequence[Mapping[str, Any]]) -> NamespaceLimits:
    """Read ``kubectl get limitrange -o json`` down to the numbers that bind.

    Only ``type: Container`` entries: a ``Pod`` limit bounds the sum across the
    pod, which podbench cannot satisfy by moving one container's numbers, and a
    ``PersistentVolumeClaim`` one has nothing to do with this.

    An empty list is the normal case and gives an object that constrains
    nothing, so every caller can treat "no LimitRange" and "a LimitRange with
    nothing to say about memory" identically.
    """
    max_limit: dict[str, Fraction] = {}
    max_ratio: dict[str, Fraction] = {}
    for document in documents:
        entries: Any = as_dict(document.get("spec")).get("limits")
        for entry in cast(list[Any], entries) if isinstance(entries, list) else []:
            limit = as_dict(entry)
            if limit.get("type") != "Container":
                continue
            _absorb(max_limit, as_dict(limit.get("max")))
            _absorb(max_ratio, as_dict(limit.get("maxLimitRequestRatio")))
    return NamespaceLimits(max_limit=max_limit, max_ratio=max_ratio)


def _absorb(into: dict[str, Fraction], values: Mapping[str, Any]) -> None:
    """Keep the smaller of what is there and what this range says."""
    for resource, raw in values.items():
        try:
            value = parse_quantity(str(raw))
        except ResizeError:
            # A quantity the API server accepted and this parser did not is not
            # worth failing an attach over: the resize is submitted anyway and
            # refused with the cluster's own message if it really was binding.
            continue
        current = into.get(resource)
        if current is None or value < current:
            into[resource] = value


def explain_claim_refusal(current: Mapping[str, Any], stderr: str) -> str:
    """Why a resize of *this* container was refused, when the reason is a claim.

    Validating a resize rebuilds the incoming container's ``resources`` from
    limits and requests alone::

        container.Resources = core.ResourceRequirements{Limits: lim, Requests: req}

    so a ``claims`` entry is dropped from the value compared against the stored
    container, which still has one. The two can never compare equal, and the
    only error that comparison knows how to raise names cpu and memory. Every
    released Kubernetes does this — checked in ``pkg/apis/core/validation`` on
    ``release-1.32`` through ``release-1.35``; ``master`` preserves ``Claims``
    and so will 1.36.

    Measured at Diamond on 2026-08-18 on an IOC holding a claim for its usbip
    device: a strategic-merge patch, a JSON patch of the single memory limit and
    a JSON patch rewriting 256Mi as 256Mi were refused identically, while a
    claim-free pod in the same namespace — same ``LimitRange``, same admission
    policies — took a no-op resize. So the refusal is the container's shape, and
    no patch podbench could author gets through.

    Returns the empty string unless both halves hold, because the same message
    is the API server's answer to any other non-cpu/memory change and guessing
    at a claim would send the reader after the wrong thing.
    """
    if MUTABLE_RESOURCES_REFUSAL not in stderr:
        return ""
    claims = current.get("claims")
    if not isinstance(claims, list) or not claims:
        return ""
    named = ", ".join(
        sorted(
            str(name)
            for claim in cast(list[Any], claims)
            if (name := as_dict(claim).get("name")) is not None
        )
    )
    return (
        f"the target container holds a resource claim{f' ({named})' if named else ''}"
        ", which no released Kubernetes can resize: the claim is dropped from "
        "the comparison that validates the patch, so even one changing nothing "
        "is refused. Raise the limits in the workload's own template instead."
    )


@dataclass(frozen=True)
class ResizePlan:
    """The patch to submit, and what to tell the user it did."""

    body: dict[str, Any]
    notes: tuple[str, ...]


def plan_resize(
    container: str,
    *,
    current: Mapping[str, Any],
    wants: Mapping[str, Want],
    limits: NamespaceLimits,
) -> ResizePlan:
    """Work out requests and limits that the namespace will actually admit.

    ``current`` is the container's own ``resources`` as the pod carries them,
    and it decides two things beyond the arithmetic: a request is never lowered,
    and a request that already equals its limit may be raised to meet the new
    one, because that container is Guaranteed already and would stay so.

    Everything refusable is refused *here*, with the value that would have
    worked, rather than at the API server, which answers with a ratio.
    """
    requests = as_dict(current.get("requests"))
    current_limits = as_dict(current.get("limits"))
    patch_requests: dict[str, str] = {}
    patch_limits: dict[str, str] = {}
    notes: list[str] = []

    for resource in (MEMORY, CPU):
        want = wants.get(resource)
        if want is None:
            continue
        ceiling = limits.ceiling(resource)
        if ceiling is not None and want.limit > ceiling:
            raise ResizeError(
                f"the namespace's LimitRange caps a container's {resource} "
                f"limit at {_show(resource, ceiling)}, and --resize asked for "
                f"{_show(resource, want.limit)}. Ask for at most the cap, or "
                "use `podbench dev`, whose pod is sized when it is created"
            )
        patch_limits[resource] = _show(resource, want.limit)
        request = _request_for(
            resource,
            want=want,
            existing=requests.get(resource),
            existing_limit=current_limits.get(resource),
            ratio=limits.ratio(resource),
            notes=notes,
        )
        if request is not None:
            patch_requests[resource] = _show(resource, request)

    resources: dict[str, Any] = {"limits": patch_limits}
    if patch_requests:
        resources["requests"] = patch_requests
    return ResizePlan(
        body={"spec": {"containers": [{"name": container, "resources": resources}]}},
        notes=tuple(notes),
    )


def _request_for(
    resource: str,
    *,
    want: Want,
    existing: Any,
    existing_limit: Any,
    ratio: Fraction | None,
    notes: list[str],
) -> Fraction | None:
    """The request to submit beside ``want.limit``, or ``None`` to leave it."""
    if want.request is not None:
        return want.request
    if ratio is None:
        # Nothing binds the two together, so the request stays where the
        # workload was scheduled with it.
        return None
    needed = want.limit / ratio
    have = parse_quantity(str(existing)) if existing is not None else None
    if have is not None and have >= needed:
        return None
    was_guaranteed = (
        have is not None
        and existing_limit is not None
        and have == parse_quantity(str(existing_limit))
    )
    if needed >= want.limit and not was_guaranteed:
        raise ResizeError(
            f"the namespace's LimitRange allows a {resource} limit of at most "
            f"{_ratio(ratio)}x its request, so a limit of "
            f"{_show(resource, want.limit)} needs a request of "
            f"{_show(resource, needed)} - which equals the limit and would make "
            "the pod Guaranteed. A resize may not change a pod's QoS class, so "
            "this one cannot be submitted at all"
        )
    notes.append(
        f"{resource} request raised to {_show(resource, needed)} alongside the "
        f"limit: this namespace's LimitRange caps limit/request at "
        f"{_ratio(ratio)}, and raising a limit on its own only ever widens that "
        "ratio"
    )
    return needed


def _show(resource: str, value: Fraction) -> str:
    return format_memory(value) if resource == MEMORY else format_cpu(value)


def _ratio(value: Fraction) -> str:
    """A ratio as the cluster states it — a plain number, not a fraction."""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{float(value):g}"


_FLAG = {MEMORY: "--resize", CPU: "--resize-cpu"}
"""The flag that spells one resource's ``REQUEST:LIMIT``."""


def qos_preserving_flags(current: Mapping[str, Any], plan: ResizePlan) -> str | None:
    """How to ask for ``plan`` without changing a Guaranteed pod's QoS class.

    A Guaranteed pod is one whose every request equals its limit, so a patch
    that moves a limit and leaves the request behind *must* change the class,
    and the API server refuses every resize that does (:data:`QOS_REFUSAL`).
    There is no value of the limit for which that patch succeeds — measured on
    the k3s bed, 2026-08-21, issue #124 — so the only useful thing to say about
    it is the spelling that does work.

    Returns the flags that pin each patched resource's request to its own new
    limit:

    >>> both = {"limits": {"memory": "256Mi"}, "requests": {"memory": "256Mi"}}
    >>> want = {MEMORY: parse_want("2Gi", resource=MEMORY)}
    >>> plan = plan_resize("app", current=both, wants=want,
    ...                    limits=namespace_limits([]))
    >>> qos_preserving_flags(both, plan)
    '--resize 2Gi:2Gi'

    and ``None`` when the plan already pins them — asked for as
    ``REQUEST:LIMIT``, or moved there by a ``LimitRange`` ratio of one — because
    that patch keeps the class and has nothing to be remedied:

    >>> pinned = plan_resize("app", current=both,
    ...                      wants={MEMORY: parse_want("2Gi:2Gi", resource=MEMORY)},
    ...                      limits=namespace_limits([]))
    >>> qos_preserving_flags(both, pinned) is None
    True

    ``None`` also for a quantity neither side can parse, which is not knowledge
    that the class would change and must not be reported as if it were: the
    patch goes to the API server, which answers for itself.
    """
    resources = as_dict(as_dict(plan.body["spec"]["containers"][0]).get("resources"))
    limits = as_dict(resources.get("limits"))
    requests = {
        **as_dict(current.get("requests")),
        **as_dict(resources.get("requests")),
    }
    flags: list[str] = []
    pinned = True
    for resource in (MEMORY, CPU):
        limit = limits.get(resource)
        if limit is None:
            continue
        request = requests.get(resource)
        try:
            same = request is not None and parse_quantity(
                str(request)
            ) == parse_quantity(str(limit))
        except ResizeError:
            return None
        pinned = pinned and same
        flags.append(f"{_FLAG[resource]} {limit}:{limit}")
    return None if pinned or not flags else " ".join(flags)


# -- headroom ---------------------------------------------------------------

SEAT_FOOTPRINT = "13-23 MiB"
"""What a podbench seat actually costs the pod it landed in.

Ten live seats across eight pods on the Diamond p47 beamline, 2026-08-19:
13, 13, 14, 14, 14, 15, 15, 16, 16 and 23 MiB. It is a *measurement* and it
replaces an assumption — the report used to warn about the pod's memory on
every attach, and the beamline it was written for has 170-3858 MiB of headroom
per pod, three seats in one pod at once and no OOM in any of them.

One beamline at one moment, so it falsifies "always warn" without proving that
no cluster anywhere is tight. Which is why the warning it calibrates still
exists, and reads *this* pod rather than quoting these numbers at it.
"""

SEAT_HEADROOM = Fraction(64 * MI)
"""Below this much free pod memory, a seat is worth a word.

Three times the largest seat measured (23 MiB), because three concurrent seats
in one pod is the most that has been seen. It is deliberately far below the
tightest pod on p47 (170 MiB): every pod there is fine, and a threshold that
fired on any of them would be the fear this replaced. A 100Mi-limited pod
really using 80 is the case it is for.
"""

EDITOR_HEADROOM = Fraction(1215 * MI)
"""The one memory cost that is still a hazard at these headrooms.

vscode-server plus a single extension measured 1215 MiB live (2026-08-16,
report R2), which does not fit in most of the beamline's pods. So the seat's
own footprint stopped earning a warning and this did not: ``podbench
vscode`` asks for
both, and only this one has to be checked against the pod.
"""


@dataclass(frozen=True)
class Headroom:
    """How much of the pod's memory ceiling is not already spoken for.

    Headroom rather than the limit, because the limit alone answers the wrong
    question: p47's *smallest* limits are three 100Mi socat containers, and
    they sit in the pod with the most room per byte used (300 MiB limit, 15 MiB
    used). What a seat has to fit in is the gap.

    Both halves are optional and they mean different things. ``limit`` is
    ``None`` when some container declares no memory limit, which leaves the pod
    cgroup unbounded — there is no ceiling to share. ``used`` is ``None`` when
    the metrics API would not answer, which is *unmeasured* and not the same as
    fine; :attr:`summary` says which, on the report's own line.
    """

    limit: Fraction | None
    used: Fraction | None

    @property
    def free(self) -> Fraction | None:
        """Bytes a seat can grow into, or ``None`` when that is not knowable.

        >>> Headroom(Fraction(256 * MI), Fraction(86 * MI)).free / MI
        Fraction(170, 1)
        >>> Headroom(Fraction(256 * MI), None).free is None
        True
        >>> Headroom(None, Fraction(86 * MI)).free is None
        True
        """
        if self.limit is None or self.used is None:
            return None
        # Clamped at zero: a pod momentarily over its own ceiling is reported as
        # having nothing left rather than as owing memory, and the warning that
        # reads this only ever asks whether it is small.
        return max(Fraction(0), self.limit - self.used)

    @property
    def summary(self) -> str:
        """This pod's memory ceiling as one report row.

        >>> Headroom(Fraction(256 * MI), Fraction(86 * MI)).summary
        '170Mi free of 256Mi (86Mi in use)'
        >>> Headroom(Fraction(256 * MI), None).summary
        'limit 256Mi; in use not measured (no metrics API here)'
        >>> Headroom(None, None).summary
        'no pod memory limit, so no ceiling for the seat to share'
        """
        if self.limit is None:
            return "no pod memory limit, so no ceiling for the seat to share"
        limit = format_memory(self.limit)
        if self.used is None:
            # Named as unmeasured, never left to read as fine. The metrics API
            # is an add-on and its absence is ordinary; a row that quietly
            # showed the limit alone would be the same mistake as #89's
            # unreadable /proc path and C14's unreadable address space.
            return f"limit {limit}; in use not measured (no metrics API here)"
        free = self.free
        assert free is not None  # noqa: S101 - both halves are present here
        return (
            f"{format_memory(free)} free of {limit} ({format_memory(self.used)} in use)"
        )

    def below(self, needed: Fraction) -> bool:
        """Whether the measured headroom is under ``needed``.

        False when there is nothing to compare, which is both the unbounded pod
        and the unmeasured one: neither is evidence of a thin margin, and the
        unmeasured one is reported as unmeasured by :attr:`summary` instead of
        being turned into a caution that would fire on every cluster without a
        metrics-server.

        >>> Headroom(Fraction(256 * MI), Fraction(240 * MI)).below(SEAT_HEADROOM)
        True
        >>> Headroom(Fraction(256 * MI), Fraction(86 * MI)).below(SEAT_HEADROOM)
        False
        >>> Headroom(Fraction(256 * MI), None).below(SEAT_HEADROOM)
        False
        """
        free = self.free
        return free is not None and free < needed


def editor_limit(headroom: Headroom, current: Fraction | None) -> Fraction | None:
    """The memory limit ``current`` has to become for an editor to fit, or ``None``.

    ``podbench vscode`` is the caller. The number was always computable — the
    headroom is read on every attach and :data:`EDITOR_HEADROOM` has been the
    threshold since it was measured — and the only thing standing between the
    two was that a warning named ``--resize`` instead of using it.

    The raise goes on the *target's* limit because that is the only limit a seat
    can move: an ephemeral container may not declare ``resources`` at all, so
    the pod's ceiling is the sum of its containers' and the seat is charged
    against it. Raising the target by the shortfall therefore raises the pod's
    ceiling by exactly the same amount.

    Rounded **up to the next whole GiB**, which is margin and a legible number
    rather than arithmetic exactly on the edge — 1301Mi is the answer nobody can
    check, 2Gi is the one they can.

    ``None`` for every case that is not a measured shortfall, so a caller can
    treat it as "nothing to do":

    >>> tight = Headroom(Fraction(256 * MI), Fraction(86 * MI))
    >>> format_memory(editor_limit(tight, Fraction(256 * MI)))
    '2Gi'
    >>> editor_limit(Headroom(Fraction(8 * GI), Fraction(MI)), Fraction(GI)) is None
    True
    >>> editor_limit(Headroom(Fraction(256 * MI), None), Fraction(MI)) is None
    True
    >>> editor_limit(Headroom(None, None), Fraction(MI)) is None
    True

    ``current`` of ``None`` is the container that declares no memory limit. It
    cannot arise beside a measured headroom — :func:`pod_memory_limit` answers
    ``None`` as soon as any container is unbounded — but it is answered rather
    than asserted, because the two reads are of the same pod at different
    moments and a crash is a poor way to report a resized pod:

    >>> editor_limit(tight, None) is None
    True
    """
    free = headroom.free
    if free is None or current is None or free >= EDITOR_HEADROOM:
        return None
    return Fraction(math.ceil((current + EDITOR_HEADROOM - free) / GI)) * GI


def pod_memory_limit(pod_json: Mapping[str, Any]) -> Fraction | None:
    """The pod cgroup's memory ceiling, or ``None`` when it has none.

    The sum of the workload containers' limits, because that is what a seat
    lives under: an ephemeral container may not declare ``resources`` at all
    (report 3.9), so it is charged against the pod's ceiling and contributes
    nothing to it. A sidecar — an init container with ``restartPolicy:
    Always`` — runs for the pod's whole life and counts; an ordinary init
    container has exited by the time a seat lands and does not.

    ``None`` as soon as one container declares no memory limit, since the
    kubelet then leaves the pod cgroup unbounded and the sum of the rest is not
    a ceiling. Guessing one from the containers that did declare would invent a
    number smaller than the truth, which is the direction that produces a
    warning nobody needs.

    >>> pod = {"spec": {"containers": [
    ...     {"resources": {"limits": {"memory": "100Mi"}}},
    ...     {"resources": {"limits": {"memory": "200Mi"}}}]}}
    >>> pod_memory_limit(pod) == 300 * MI
    True
    >>> pod["spec"]["containers"].append({"name": "unbounded"})
    >>> pod_memory_limit(pod) is None
    True
    """
    spec = as_dict(pod_json.get("spec"))
    total = Fraction(0)
    for entry in _containers(spec.get("containers")):
        limit = as_dict(as_dict(entry.get("resources")).get("limits")).get(MEMORY)
        if limit is None:
            return None
        try:
            total += parse_quantity(str(limit))
        except ResizeError:
            # A quantity the API server accepted and this parser did not says
            # nothing about the ceiling, and reporting a smaller one would be a
            # confident wrong number.
            return None
    for entry in _containers(spec.get("initContainers")):
        if entry.get("restartPolicy") != "Always":
            continue
        limit = as_dict(as_dict(entry.get("resources")).get("limits")).get(MEMORY)
        if limit is None:
            return None
        try:
            total += parse_quantity(str(limit))
        except ResizeError:
            return None
    return total if total > 0 else None


def _containers(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [as_dict(entry) for entry in cast(list[Any], value)]


def parse_top_memory(text: str) -> Fraction | None:
    """The memory column of ``kubectl top pod --no-headers``, in bytes.

    ``NAME  CPU(cores)  MEMORY(bytes)``, one row for the pod. Parsed rather
    than asked for as JSON because ``kubectl top`` has no ``-o json``: it
    renders the metrics API itself, and the columns are its whole output.

    ``None`` for anything that is not that shape — including the empty output a
    pod with no sample yet produces — because the caller reports *unmeasured*
    and a misparse would report a number instead.

    >>> parse_top_memory("target   1m   67Mi\\n") == 67 * MI
    True
    >>> parse_top_memory("") is None
    True
    >>> parse_top_memory("target   1m") is None
    True
    """
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            return parse_quantity(fields[2])
        except ResizeError:
            return None
    return None
