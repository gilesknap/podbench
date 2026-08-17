"""Tests for the probe budget.

The numbers here are not arbitrary: the fixtures are the probes of
``tests/e2e/apps/python-service.yaml``, which is the workload the budget was
measured against on a live cluster — a pause held past the readiness deadline
emptied the Service's endpoints, and one held past the liveness deadline
restarted the container. Both landed inside the window this module computes,
so an edit that moves the arithmetic will move it away from a measurement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from podbench.budget import (
    ProbeBudget,
    ProbeKind,
    probe_budgets,
    probe_qualifier,
)

APP = "app"

DEMO_PROBES: dict[str, Any] = {
    # As the manifest states them: no failureThreshold and no timeoutSeconds,
    # so the budget has to supply the values the API server would have.
    "readinessProbe": {
        "httpGet": {"path": "/healthz", "port": 8080},
        "initialDelaySeconds": 2,
        "periodSeconds": 5,
    },
    "livenessProbe": {
        "httpGet": {"path": "/healthz", "port": 8080},
        "initialDelaySeconds": 5,
        "periodSeconds": 10,
    },
    "startupProbe": {
        "httpGet": {"path": "/healthz", "port": 8080},
        "failureThreshold": 30,
        "periodSeconds": 2,
    },
}


def pod_document(
    probes: dict[str, Any] | None = None,
    *,
    started: bool | None = True,
    container: str = APP,
) -> dict[str, Any]:
    status: dict[str, Any] = {"name": container, "ready": True}
    if started is not None:
        status["started"] = started
    return {
        "spec": {"containers": [{"name": container, **(probes or {})}]},
        "status": {"containerStatuses": [status]},
    }


def by_kind(budgets: tuple[ProbeBudget, ...]) -> dict[ProbeKind, ProbeBudget]:
    return {budget.kind: budget for budget in budgets}


def test_demo_service_budgets_match_what_the_cluster_did() -> None:
    budgets = probe_budgets(pod_document(DEMO_PROBES), APP)
    windows = {budget.kind: budget.window for budget in budgets}
    assert windows[ProbeKind.READINESS] == "11-16s"
    assert windows[ProbeKind.LIVENESS] == "21-31s"


def test_the_manifest_the_measurement_came_from_still_says_this() -> None:
    """The arithmetic is pinned to the app it was measured against.

    Read out of the manifest rather than restated, so an edit to the probes in
    ``apps/`` cannot leave the fixtures above asserting a budget nothing has.
    """
    apps = Path(__file__).resolve().parent / "e2e" / "apps" / "python-service.yaml"
    documents = cast(
        list[dict[str, Any]], list(yaml.safe_load_all(apps.read_text(encoding="utf-8")))
    )
    deployment = next(doc for doc in documents if doc.get("kind") == "Deployment")
    template = deployment["spec"]["template"]["spec"]
    container = cast(dict[str, Any], template["containers"][0])
    assert {
        key: value for key, value in container.items() if key.endswith("Probe")
    } == DEMO_PROBES


def test_readiness_comes_first_because_it_is_the_quiet_one() -> None:
    budgets = probe_budgets(pod_document(DEMO_PROBES), APP)
    assert [budget.kind for budget in budgets] == [
        ProbeKind.READINESS,
        ProbeKind.LIVENESS,
        ProbeKind.STARTUP,
    ]


def test_an_omitted_period_takes_the_api_servers_default() -> None:
    budgets = probe_budgets(pod_document({"livenessProbe": {}}), APP)
    assert budgets[0].period_seconds == 10
    assert budgets[0].window == "21-31s"


@pytest.mark.parametrize("value", [0, -1, "5s", None, True])
def test_a_value_the_server_would_not_hold_falls_back(value: Any) -> None:
    budgets = probe_budgets(
        pod_document({"livenessProbe": {"periodSeconds": value}}), APP
    )
    assert budgets[0].period_seconds == 10


def test_a_timeout_longer_than_the_period_paces_the_attempts() -> None:
    """A probe worker cannot start the next attempt while one is outstanding,
    so the deadline stretches rather than staying at threshold x period."""
    budgets = probe_budgets(
        pod_document(
            {"livenessProbe": {"periodSeconds": 2, "timeoutSeconds": 30}},
        ),
        APP,
    )
    assert budgets[0].pace == 30
    assert budgets[0].window == "90-120s"


def test_a_satisfied_startup_probe_is_not_the_deadline() -> None:
    budgets = by_kind(probe_budgets(pod_document(DEMO_PROBES, started=True), APP))
    assert not budgets[ProbeKind.STARTUP].in_force
    assert budgets[ProbeKind.LIVENESS].in_force
    qualifier = probe_qualifier(APP, tuple(budgets.values()))
    assert "startup" not in qualifier, "a probe that cannot fire is not a deadline"


def test_a_running_startup_probe_holds_the_other_two_off() -> None:
    """The kubelet disables readiness and liveness until a startup probe
    succeeds, so quoting the liveness deadline there would be the wrong one."""
    budgets = by_kind(probe_budgets(pod_document(DEMO_PROBES, started=False), APP))
    assert budgets[ProbeKind.STARTUP].in_force
    assert not budgets[ProbeKind.LIVENESS].in_force
    assert not budgets[ProbeKind.READINESS].in_force
    qualifier = probe_qualifier(APP, tuple(budgets.values()))
    assert qualifier.startswith("TIME-LIMITED")
    assert "startup" in qualifier


def test_a_missing_started_field_reads_as_started() -> None:
    budgets = by_kind(probe_budgets(pod_document(DEMO_PROBES, started=None), APP))
    assert budgets[ProbeKind.LIVENESS].in_force
    assert not budgets[ProbeKind.STARTUP].in_force


def test_readiness_and_liveness_stand_alone_without_a_startup_probe() -> None:
    probes = {key: DEMO_PROBES[key] for key in ("readinessProbe", "livenessProbe")}
    budgets = probe_budgets(pod_document(probes, started=False), APP)
    assert all(budget.in_force for budget in budgets)


def test_the_qualifier_names_every_budget_and_the_way_out() -> None:
    """Both deadlines, not just the soonest.

    The readiness one arrives first and is the quiet one, so a reader shown
    only that would take "drops out of the Service" for the whole cost and
    never learn that holding on restarts the container underneath them.
    """
    qualifier = probe_qualifier(APP, probe_budgets(pod_document(DEMO_PROBES), APP))
    assert "readiness at 11-16s" in qualifier
    assert "liveness at 21-31s" in qualifier
    assert "drops out of the Service" in qualifier
    # The seat is in the blast radius of the liveness half.
    assert "killing the seat" in qualifier
    # The two facts that stop a reader looking for a knob that is not there.
    assert "cannot be changed on a running pod" in qualifier
    assert "`podbench dev`" in qualifier


def test_an_unprobed_target_is_said_out_loud_and_not_warned_about() -> None:
    """'explore freely' and 'you have thirty seconds' are different facts, and
    the report has to say which one this pod is."""
    budgets = probe_budgets(pod_document(), APP)
    assert budgets == ()
    qualifier = probe_qualifier(APP, budgets)
    assert qualifier.startswith("no deadline")
    assert "no readiness, liveness or startup probe" in qualifier


def test_the_qualifier_leads_with_the_soonest_deadline() -> None:
    qualifier = probe_qualifier(APP, probe_budgets(pod_document(DEMO_PROBES), APP))
    assert qualifier.index("readiness at") < qualifier.index("liveness at")


def test_a_container_the_pod_does_not_have_has_no_budget() -> None:
    assert probe_budgets(pod_document(DEMO_PROBES), "sidecar") == ()
