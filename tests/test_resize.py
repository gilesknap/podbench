"""Tests for the in-place resize arithmetic.

The numbers here are Diamond's, measured on 2026-08-16: a namespace whose
``LimitRange`` caps ``maxLimitRequestRatio`` at 10, and a pod requesting 64Mi
of memory. ``--resize 6Gi`` against that is a ratio of 96 and was refused —
which made the flag unusable on every pod in the namespace, and is what these
tests exist to keep fixed.

Nothing here touches a cluster: every function under test takes the JSON a
caller already has.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import pytest

from podbench.resize import (
    CPU,
    MEMORY,
    ResizeError,
    Want,
    explain_claim_refusal,
    format_cpu,
    format_memory,
    namespace_limits,
    parse_quantity,
    parse_want,
    plan_resize,
)

GI = 2**30
MI = 2**20


def ratio_limit_range(**ratios: str) -> dict[str, Any]:
    """A ``LimitRange`` that bounds limit/request and nothing else."""
    return {
        "kind": "LimitRange",
        "spec": {"limits": [{"type": "Container", "maxLimitRequestRatio": ratios}]},
    }


def resources(**kwargs: dict[str, str]) -> dict[str, Any]:
    return dict(kwargs)


def submitted(plan: Any) -> dict[str, Any]:
    """The one container's ``resources`` out of a strategic-merge patch."""
    containers = plan.body["spec"]["containers"]
    assert len(containers) == 1, "the patch names one container, by name"
    return containers[0]["resources"]


# -- quantities --------------------------------------------------------------


def test_quantities_are_exact_rather_than_floating() -> None:
    """6Gi over 10 is not representable in binary floating point, and a request
    one byte under the bound is refused by the rule it was computed to meet."""
    assert parse_quantity("6Gi") / 10 == Fraction(6 * GI, 10)
    assert parse_quantity("500m") == Fraction(1, 2)
    assert parse_quantity("2") == 2


def test_a_quantity_that_is_not_one_says_so_before_anything_is_submitted() -> None:
    with pytest.raises(ResizeError) as raised:
        parse_quantity("6 gigs")
    assert "not a Kubernetes quantity" in str(raised.value)


def test_a_derived_request_is_rounded_up_never_down() -> None:
    # 6Gi/10 is 614.4Mi. 614Mi would leave the ratio at 10.007 and be refused.
    assert format_memory(Fraction(6 * GI, 10)) == "615Mi"
    assert parse_quantity("615Mi") * 10 > 6 * GI


def test_a_whole_unit_is_spelled_the_way_it_was_asked_for() -> None:
    """`--resize 6Gi` answering "resized to 6144Mi" reads like a change."""
    assert format_memory(Fraction(6 * GI)) == "6Gi"
    assert format_cpu(Fraction(4)) == "4"
    assert format_cpu(Fraction(4, 10)) == "400m"


# -- the namespace's numbers -------------------------------------------------


def test_the_most_restrictive_limit_range_is_the_one_that_binds() -> None:
    """All of them are enforced, so the first refusal is the whole answer."""
    limits = namespace_limits(
        [ratio_limit_range(memory="10"), ratio_limit_range(memory="4")]
    )
    assert limits.ratio(MEMORY) == 4


def test_a_pod_scoped_limit_range_is_not_read_as_a_container_one() -> None:
    """A Pod limit bounds the sum across the pod, which moving one container's
    numbers cannot satisfy - and reading it as a container bound would compute
    a request against the wrong ceiling."""
    limits = namespace_limits(
        [
            {
                "spec": {
                    "limits": [
                        {"type": "Pod", "maxLimitRequestRatio": {"memory": "2"}},
                        {"type": "Container", "maxLimitRequestRatio": {"memory": "10"}},
                    ]
                }
            }
        ]
    )
    assert limits.ratio(MEMORY) == 10


def test_no_limit_ranges_constrain_nothing() -> None:
    limits = namespace_limits([])
    assert limits.ratio(MEMORY) is None
    assert limits.ceiling(MEMORY) is None


# -- the plan ----------------------------------------------------------------


def test_the_request_is_raised_with_the_limit_under_a_ratio() -> None:
    """The Diamond case. Raising the limit alone only widens the ratio: 6Gi
    over the pod's 64Mi request is 96, against a cap of 10."""
    plan = plan_resize(
        "app",
        current=resources(requests={"memory": "64Mi"}, limits={"memory": "512Mi"}),
        wants={MEMORY: Want(limit=parse_quantity("6Gi"))},
        limits=namespace_limits([ratio_limit_range(memory="10")]),
    )

    patch = submitted(plan)
    assert patch["limits"]["memory"] == "6Gi"
    assert patch["requests"]["memory"] == "615Mi"
    assert (
        parse_quantity(patch["limits"]["memory"])
        / parse_quantity(patch["requests"]["memory"])
        <= 10
    ), "the ratio the LimitRange enforces, checked on the patch itself"
    assert any("LimitRange caps limit/request at 10" in note for note in plan.notes)


def test_a_request_that_already_satisfies_the_ratio_is_left_alone() -> None:
    """A request is a scheduling promise the workload was placed on. Nothing
    here needs to move it, so nothing moves it."""
    plan = plan_resize(
        "app",
        current=resources(requests={"memory": "2Gi"}, limits={"memory": "4Gi"}),
        wants={MEMORY: Want(limit=parse_quantity("6Gi"))},
        limits=namespace_limits([ratio_limit_range(memory="10")]),
    )

    assert "requests" not in submitted(plan)


def test_without_a_ratio_the_request_is_not_touched_at_all() -> None:
    """The common case, and the behaviour every cluster had before this: with
    nothing binding the two together, raising a limit is the whole job."""
    plan = plan_resize(
        "app",
        current=resources(requests={"memory": "64Mi"}, limits={"memory": "512Mi"}),
        wants={MEMORY: Want(limit=parse_quantity("6Gi"))},
        limits=namespace_limits([]),
    )

    assert submitted(plan) == {"limits": {"memory": "6Gi"}}
    assert plan.notes == ()


def test_an_explicit_request_is_used_as_written() -> None:
    """A number the user typed is a decision, not a default to improve on."""
    plan = plan_resize(
        "app",
        current=resources(requests={"memory": "64Mi"}, limits={"memory": "512Mi"}),
        wants={MEMORY: parse_want("1Gi:6Gi", resource=MEMORY)},
        limits=namespace_limits([ratio_limit_range(memory="10")]),
    )

    assert submitted(plan)["requests"]["memory"] == "1Gi"


def test_cpu_and_memory_travel_in_one_patch() -> None:
    """Two patches would be two chances to half-apply, and the second would be
    computed against a pod the first had already changed."""
    plan = plan_resize(
        "app",
        current=resources(
            requests={"cpu": "100m", "memory": "64Mi"},
            limits={"cpu": "200m", "memory": "512Mi"},
        ),
        wants={
            MEMORY: Want(limit=parse_quantity("6Gi")),
            CPU: Want(limit=parse_quantity("4")),
        },
        limits=namespace_limits([ratio_limit_range(memory="10", cpu="10")]),
    )

    patch = submitted(plan)
    assert patch["limits"] == {"cpu": "4", "memory": "6Gi"}
    assert patch["requests"] == {"cpu": "400m", "memory": "615Mi"}


def test_a_resize_that_would_change_the_qos_class_is_refused_with_the_numbers() -> None:
    """A resize may not move a pod between QoS classes, and a ratio of 1 forces
    request == limit - which is Guaranteed. Refused here, with the arithmetic,
    rather than by the API server, which answers with a ratio and no way out.
    """
    with pytest.raises(ResizeError) as raised:
        plan_resize(
            "app",
            current=resources(requests={"memory": "64Mi"}, limits={"memory": "512Mi"}),
            wants={MEMORY: Want(limit=parse_quantity("6Gi"))},
            limits=namespace_limits([ratio_limit_range(memory="1")]),
        )

    message = str(raised.value)
    assert "would make the pod Guaranteed" in message
    assert "QoS class" in message


def test_a_container_that_is_already_guaranteed_keeps_its_equal_numbers() -> None:
    """The other side of the same rule: this pod is Guaranteed already, so
    request == limit is where it started and where it stays."""
    plan = plan_resize(
        "app",
        current=resources(requests={"memory": "512Mi"}, limits={"memory": "512Mi"}),
        wants={MEMORY: Want(limit=parse_quantity("2Gi"))},
        limits=namespace_limits([ratio_limit_range(memory="1")]),
    )

    patch = submitted(plan)
    assert patch["requests"]["memory"] == patch["limits"]["memory"] == "2Gi"


def test_a_limit_above_the_namespace_ceiling_is_refused_with_the_ceiling() -> None:
    """The cluster's refusal names the cap; podbench names it before spending a
    call, and says what else there is to do."""
    ranges = [{"spec": {"limits": [{"type": "Container", "max": {"memory": "2Gi"}}]}}]
    with pytest.raises(ResizeError) as raised:
        plan_resize(
            "app",
            current=resources(requests={"memory": "64Mi"}),
            wants={MEMORY: Want(limit=parse_quantity("6Gi"))},
            limits=namespace_limits(ranges),
        )

    message = str(raised.value)
    assert "caps a container's memory limit at 2Gi" in message
    assert "podbench dev" in message


def test_a_request_above_its_own_limit_is_refused_where_it_is_typed() -> None:
    with pytest.raises(ResizeError) as raised:
        parse_want("8Gi:6Gi", resource=MEMORY)
    assert "above its limit" in str(raised.value)


# -- a container holding a resource claim -------------------------------------

FLIPPER = {
    "claims": [{"name": "bl01c-ea-flip-02"}],
    "limits": {"cpu": "500m", "ephemeral-storage": "2Gi", "memory": "256Mi"},
    "requests": {"cpu": "100m", "ephemeral-storage": "100Mi", "memory": "64Mi"},
}
"""``bl01c-ea-flip-02-0`` as the cluster carried it on 2026-08-18: a Diamond IOC
whose claim attaches the usbip device its `fastcs-thorlabs` driver talks to."""

MUTABLE = (
    'The Pod "bl01c-ea-flip-02-0" is invalid: spec: Forbidden: '
    "only cpu and memory resources are mutable"
)
"""What that pod answered to every patch tried against it, including a JSON
patch rewriting its memory limit as the value it already had."""


def test_a_claim_is_named_as_the_reason_the_api_server_would_not_name() -> None:
    """The refusal says cpu and memory, and is about neither: the validator
    drops `claims` from the container it compares, so nothing compares equal."""
    explanation = explain_claim_refusal(FLIPPER, MUTABLE)

    assert "bl01c-ea-flip-02" in explanation, "which claim, so it can be found"
    assert "no released Kubernetes can resize" in explanation
    assert "template" in explanation, "the one thing left to do"


def test_the_same_refusal_without_a_claim_is_left_as_the_cluster_worded_it() -> None:
    """This message is also the API server's answer to any other non-cpu/memory
    change. Guessing at a claim would send the reader after the wrong thing."""
    assert explain_claim_refusal(resources(limits={"memory": "256Mi"}), MUTABLE) == ""
    assert explain_claim_refusal({"claims": []}, MUTABLE) == ""


def test_a_claim_explains_nothing_about_some_other_refusal() -> None:
    assert explain_claim_refusal(FLIPPER, "pods is forbidden: exceeded quota") == ""


def test_a_claim_does_not_stop_a_patch_being_planned() -> None:
    """Submitted rather than pre-empted: the fix is upstream on master, so a
    1.36 cluster resizes this container and podbench must not refuse first."""
    plan = plan_resize(
        "bl01c-ea-flip-02",
        current=FLIPPER,
        wants={MEMORY: Want(limit=parse_quantity("6Gi"))},
        limits=namespace_limits([ratio_limit_range(memory="10")]),
    )

    assert submitted(plan)["limits"]["memory"] == "6Gi"
