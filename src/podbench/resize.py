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
    "MEMORY",
    "CPU",
    "MUTABLE_RESOURCES_REFUSAL",
    "ResizeError",
    "ResizePlan",
    "Want",
    "explain_claim_refusal",
    "format_cpu",
    "format_memory",
    "namespace_limits",
    "parse_quantity",
    "parse_want",
    "plan_resize",
]

MEMORY = "memory"
CPU = "cpu"

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
