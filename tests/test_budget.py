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
    MIB,
    ProbeBudget,
    ProbeKind,
    ProbeSpend,
    format_probe_spend,
    memory_budget,
    memory_warning,
    probe_budgets,
    probe_explanation,
    probe_qualifier,
    probe_spend,
    storage_warning,
    target_status,
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
    restarts: int | None = None,
    ready: bool = True,
    running_since: str | None = None,
    last_terminated: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status: dict[str, Any] = {"name": container, "ready": ready}
    if started is not None:
        status["started"] = started
    if restarts is not None:
        status["restartCount"] = restarts
    if running_since is not None:
        status["state"] = {"running": {"startedAt": running_since}}
    if last_terminated is not None:
        status["lastState"] = {"terminated": last_terminated}
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
    assert "already satisfied" in (
        probe_explanation(APP, tuple(budgets.values())) or ""
    )


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


def test_the_warning_names_both_budgets_and_the_way_out() -> None:
    budgets = probe_budgets(pod_document(DEMO_PROBES), APP)
    warning = probe_explanation(APP, budgets)
    assert warning is not None
    assert "11-16s" in warning
    assert "21-31s" in warning
    assert "stops taking Service traffic" in warning
    assert "Quiet" in warning
    assert "killed and restarted" in warning
    # The seat is in the blast radius too, and its name does not come back.
    assert "attach --new" in warning
    assert "`podbench dev`" in warning
    # The two facts that stop a reader looking for a knob that is not there.
    assert "cannot be changed on a running pod" in warning
    assert "subresource" in warning


def test_an_unprobed_target_is_said_out_loud_and_not_warned_about() -> None:
    """'explore freely' and 'you have thirty seconds' are different facts, and
    the report has to say which one this pod is."""
    budgets = probe_budgets(pod_document(), APP)
    assert budgets == ()
    assert probe_explanation(APP, budgets) is None
    qualifier = probe_qualifier(APP, budgets)
    assert qualifier.startswith("no deadline")
    assert "no readiness, liveness or startup probe" in qualifier


def test_the_qualifier_quotes_the_soonest_deadline() -> None:
    budgets = probe_budgets(pod_document(DEMO_PROBES), APP)
    qualifier = probe_qualifier(APP, budgets)
    assert "readiness at 11-16s" in qualifier


def test_a_container_the_pod_does_not_have_has_no_budget() -> None:
    assert probe_budgets(pod_document(DEMO_PROBES), "sidecar") == ()


def test_initial_delay_is_named_only_when_the_spec_states_one() -> None:
    with_delay = probe_explanation(APP, probe_budgets(pod_document(DEMO_PROBES), APP))
    without = probe_explanation(
        APP,
        probe_budgets(pod_document({"livenessProbe": {"periodSeconds": 10}}), APP),
    )
    assert with_delay is not None and "initialDelaySeconds" in with_delay
    assert without is not None and "initialDelaySeconds" not in without


# -- the budget spent -------------------------------------------------------
#
# The fixtures below are the events of 2026-08-16 (issue #50), transcribed: a
# pause-and-resume in VS Code against the demo Deployment produced four
# BrokenPipeError tracebacks in the application's log and exactly these two
# aggregated events, 1:1 with them. Two liveness failures against a threshold
# of 3, twenty seconds apart on a ten second period - so a probe succeeded
# between them, the counter reset, and nothing was spent. Reporting that as
# "2 of 3" would have been wrong, and wrong in the direction that panics.

LANDED = "2026-08-16T08:52:04Z"
"""When the seat landed on the pod those events came from."""


def unhealthy(
    kind: str,
    count: int,
    first: str,
    last: str,
    *,
    container: str = APP,
    field_path: str | None = None,
) -> dict[str, Any]:
    """One aggregated event, in the shape ``kubectl get events -o json`` returns."""
    involved: dict[str, Any] = {"kind": "Pod", "name": "web-6c9d7f4b8b-hq2vn"}
    path = field_path if field_path is not None else f"spec.containers{{{container}}}"
    if path:
        involved["fieldPath"] = path
    return {
        "type": "Warning",
        "reason": "Unhealthy",
        "message": (
            f'{kind} probe failed: Get "http://10.42.0.23:8080/healthz": '
            "context deadline exceeded (Client.Timeout exceeded while "
            "awaiting headers)"
        ),
        "count": count,
        "firstTimestamp": first,
        "lastTimestamp": last,
        "involvedObject": involved,
    }


THE_PAUSE = [
    unhealthy("Liveness", 2, "2026-08-16T10:04:11Z", "2026-08-16T10:04:31Z"),
    unhealthy("Readiness", 2, "2026-08-16T10:04:13Z", "2026-08-16T10:04:33Z"),
]


def spend_report(
    events: list[dict[str, Any]],
    *,
    probes: dict[str, Any] | None = None,
    since: str | None = LANDED,
    seat: str | None = None,
    **status: Any,
) -> str:
    pod = pod_document(
        probes if probes is not None else DEMO_PROBES,
        restarts=status.pop("restarts", 0),
        **status,
    )
    return format_probe_spend(
        APP,
        probe_budgets(pod, APP),
        probe_spend(events, APP, since=since),
        since=since,
        seat=seat,
        target=target_status(pod, APP),
    )


def test_the_events_of_the_pause_are_reported_as_the_events_have_them() -> None:
    spend = {item.kind: item for item in probe_spend(THE_PAUSE, APP, since=LANDED)}
    assert spend[ProbeKind.LIVENESS] == ProbeSpend(
        kind=ProbeKind.LIVENESS,
        failures=2,
        first="2026-08-16T10:04:11Z",
        last="2026-08-16T10:04:31Z",
    )
    assert spend[ProbeKind.READINESS].failures == 2
    assert spend[ProbeKind.READINESS].last == "2026-08-16T10:04:33Z"


def test_readiness_leads_here_as_it_does_in_the_budget() -> None:
    assert [item.kind for item in probe_spend(THE_PAUSE, APP)] == [
        ProbeKind.READINESS,
        ProbeKind.LIVENESS,
    ]


def test_a_count_is_never_reported_as_a_streak() -> None:
    """The whole point: `count` is aggregated and cannot show consecutiveness."""
    report = spend_report(THE_PAUSE)
    assert "2 failures, last 2026-08-16T10:04:31Z" in report
    # Anything of this shape would be an assertion the events cannot support.
    for fiction in ("2 of 3", "2/3", "one away", "2 consecutive failures"):
        assert fiction not in report
    assert "A count is not a streak" in report
    assert "resets on the first success" in report
    assert "Only a restart proves a liveness streak ever completed" in report


def test_the_threshold_is_stated_with_the_word_consecutive_on_the_line() -> None:
    report = spend_report(THE_PAUSE)
    assert "3 consecutive x 10s period, 1s timeout" in report
    assert "a 3rd in a row kills the container, and the seat with it" in report
    # One renderer for the three numbers, so the two lines cannot drift apart.
    budget = by_kind(probe_budgets(pod_document(DEMO_PROBES), APP))[ProbeKind.LIVENESS]
    assert budget.mechanism == "3 failures x 10s period, 1s timeout"
    assert budget.consecutive_mechanism == "3 consecutive x 10s period, 1s timeout"


def test_a_count_past_the_threshold_still_refuses_to_call_it_a_streak() -> None:
    """The rendering the whole design turns on: five failures against a
    threshold of three is not five-thirds of anything, and the line must not
    start reading as an overdraft once the numbers cross."""
    events = [unhealthy("Liveness", 5, "2026-08-16T10:00:00Z", "2026-08-16T10:20:00Z")]
    report = spend_report(events)
    assert "liveness: 5 failures, last 2026-08-16T10:20:00Z" in report
    for fiction in ("5 of 3", "over budget", "exhausted", "5 consecutive"):
        assert fiction not in report
    assert "restarts: 0 - nothing has been killed under this seat" in report


def test_a_probe_that_cannot_fire_has_no_stake_to_state() -> None:
    """`_budget_line` already refuses to call a satisfied startup probe a
    deadline; the spent half stating one for the same probe on the same pod
    would contradict it, and in the alarming direction."""
    report = spend_report([], probes=DEMO_PROBES)
    assert "startup: none retained" in report
    assert "already satisfied, so nothing is at stake" in report
    assert "a 30th in a row kills the container" not in report


def test_a_running_startup_probe_leaves_the_other_two_nothing_at_stake() -> None:
    pod = pod_document(DEMO_PROBES, started=False)
    report = format_probe_spend(APP, probe_budgets(pod, APP), (), since=LANDED)
    assert "liveness: none retained - 3 consecutive x 10s period, 1s timeout; held off"
    assert report.count("held off while the startup probe is still running") == 2
    assert "kills the container, and the seat with it" not in report


def test_the_header_names_the_seat_whose_landing_the_window_is() -> None:
    """`status` lists every seat the pod has carried and only one of them sets
    this window; "the seat" does not say which."""
    report = spend_report(THE_PAUSE, seat="podbench-3")
    assert f"probes on 'app' since podbench-3 landed ({LANDED})" in report


def test_the_traceback_the_application_logs_is_named_as_a_missed_probe() -> None:
    """Four BrokenPipeErrors and four missed probes were the same four events;
    working that out from scratch is the cost this note exists to remove."""
    report = spend_report(THE_PAUSE)
    assert "BrokenPipeError" in report
    assert "One traceback, one missed probe" in report
    # And it is there for the reader who has the traceback but no events left.
    assert "BrokenPipeError" in spend_report([])


def test_nothing_retained_is_not_nothing_happened() -> None:
    report = spend_report([])
    assert "none retained" in report
    assert "--event-ttl" in report
    assert "A count is not a streak" not in report


def test_a_sidecars_probe_failures_are_not_charged_to_the_target() -> None:
    events = [
        unhealthy(
            "Liveness",
            9,
            "2026-08-16T10:04:11Z",
            "2026-08-16T10:04:31Z",
            container="istio-proxy",
        )
    ]
    assert probe_spend(events, APP, since=LANDED) == ()


def test_an_event_naming_no_container_is_not_attributed_to_one() -> None:
    """The kubelet always sets fieldPath on these, so an event without one is
    not a probe failure of this container's - and a count credited to the
    wrong container is worse than a count that is missing."""
    events = [
        unhealthy(
            "Liveness",
            2,
            "2026-08-16T10:04:11Z",
            "2026-08-16T10:04:31Z",
            field_path="",
        )
    ]
    assert probe_spend(events, APP, since=LANDED) == ()


def test_a_series_that_ended_before_the_seat_landed_is_not_this_sessions() -> None:
    events = [unhealthy("Liveness", 6, "2026-08-16T07:10:00Z", "2026-08-16T07:11:00Z")]
    assert probe_spend(events, APP, since=LANDED) == ()
    # Without a landing to measure from, it is the pod's whole retained history.
    assert probe_spend(events, APP)[0].failures == 6


def test_a_series_straddling_the_landing_says_so_rather_than_splitting_it() -> None:
    """An aggregated count cannot be apportioned, so it is reported whole and
    labelled rather than silently credited to this session."""
    events = [unhealthy("Liveness", 5, "2026-08-16T08:00:00Z", "2026-08-16T09:30:00Z")]
    (spend,) = probe_spend(events, APP, since=LANDED)
    assert spend.began_before
    assert spend.failures == 5
    assert "before the seat landed" in spend_report(events)


def test_the_events_api_shape_counts_too() -> None:
    """An events.k8s.io record carries eventTime and series, and none of
    firstTimestamp, lastTimestamp or a top-level count. `kubectl get events`
    asks for core/v1, so this shape is insurance rather than a case that has
    been seen - but counting four failures as one would be silent."""
    event = unhealthy("Readiness", 0, "", "")
    del event["firstTimestamp"]
    del event["lastTimestamp"]
    event["count"] = None
    event["eventTime"] = "2026-08-16T10:04:13Z"
    event["series"] = {"count": 4, "lastObservedTime": "2026-08-16T10:04:33Z"}
    (spend,) = probe_spend([event], APP, since=LANDED)
    assert (spend.failures, spend.last) == (4, "2026-08-16T10:04:33Z")


def test_the_events_api_keeps_an_unaggregated_count_under_another_name() -> None:
    """The same shape without a `series`: that record's repeat count is in
    `deprecatedCount`, and reading only `count` reports six failures as one."""
    event = unhealthy("Liveness", 0, "", "")
    del event["firstTimestamp"]
    del event["lastTimestamp"]
    del event["count"]
    event["eventTime"] = "2026-08-16T10:04:11Z"
    event["deprecatedCount"] = 6
    event["deprecatedLastTimestamp"] = "2026-08-16T10:05:11Z"
    (spend,) = probe_spend([event], APP, since=LANDED)
    assert (spend.failures, spend.last) == (6, "2026-08-16T10:05:11Z")


def test_a_probe_the_kubelet_could_not_run_is_a_failure_too() -> None:
    """The kubelet writes two Unhealthy messages: "probe failed" when the probe
    answered wrongly and "probe errored" when the attempt could not be made -
    an exec the runtime timed out, a connection it could not open. Both come
    back as a failure and both move failureThreshold's counter, so counting
    only the first undercounts the spend against the threshold printed beside
    it, on exactly the exec-probed pods where a pause errors rather than
    fails."""
    errored = unhealthy("Liveness", 3, "2026-08-16T10:04:11Z", "2026-08-16T10:04:31Z")
    errored["message"] = (
        "Liveness probe errored: rpc error: code = DeadlineExceeded "
        "desc = context deadline exceeded"
    )
    (spend,) = probe_spend([errored], APP, since=LANDED)
    assert (spend.kind, spend.failures) == (ProbeKind.LIVENESS, 3)


def test_an_unaggregated_event_stands_for_the_one_failure_it_is() -> None:
    event = unhealthy("Liveness", 2, "2026-08-16T10:04:11Z", "2026-08-16T10:04:31Z")
    event["count"] = 0
    assert probe_spend([event], APP, since=LANDED)[0].failures == 1


def test_two_series_for_one_probe_are_summed_and_the_latest_timestamp_wins() -> None:
    events = [
        unhealthy("Liveness", 2, "2026-08-16T10:04:11Z", "2026-08-16T10:04:31Z"),
        unhealthy("Liveness", 3, "2026-08-16T11:00:00Z", "2026-08-16T11:00:20Z"),
    ]
    (spend,) = probe_spend(events, APP, since=LANDED)
    assert spend.failures == 5
    assert (spend.first, spend.last) == (
        "2026-08-16T10:04:11Z",
        "2026-08-16T11:00:20Z",
    )


def test_only_probe_failures_are_counted() -> None:
    other = {
        "reason": "Killing",
        "message": "Stopping container app",
        "count": 1,
        "firstTimestamp": "2026-08-16T10:04:31Z",
        "lastTimestamp": "2026-08-16T10:04:31Z",
        "involvedObject": {"kind": "Pod", "fieldPath": "spec.containers{app}"},
    }
    assert probe_spend([other], APP, since=LANDED) == ()


def test_a_restart_under_the_seat_is_reported_with_what_it_did_to_it() -> None:
    report = spend_report(
        THE_PAUSE, restarts=1, running_since="2026-08-16T10:04:41Z", seat="podbench-1"
    )
    assert "restarts: 1" in report
    assert "after the seat landed, so at least one of those happened under it" in report
    assert "cannot come back" in report


def test_a_restart_that_predates_the_seat_is_not_charged_to_the_session() -> None:
    """`restartCount` is the container's whole life, and an unrelated OOM
    yesterday reported bare is an alarm about a session that spent nothing.
    The current instance's startedAt dates the last restart exactly."""
    report = spend_report(THE_PAUSE, restarts=1, running_since="2026-08-15T04:00:00Z")
    assert "all of them before this seat" in report
    assert "nothing has been killed under it" in report
    assert "burnt one" not in report


def test_an_undated_restart_says_it_cannot_place_it() -> None:
    report = spend_report(THE_PAUSE, restarts=1)
    assert "does not date the last one" in report
    assert "burnt one" in report


def test_the_last_instances_cause_of_death_is_named() -> None:
    """A seat walking / OOMs the pod, and the counter that a liveness streak
    moves is the same one - so a restart presented bare reads as proof of a
    streak that never happened."""
    report = spend_report(
        THE_PAUSE,
        restarts=1,
        running_since="2026-08-16T10:04:41Z",
        last_terminated={"reason": "OOMKilled", "exitCode": 137},
    )
    assert "The last one ended OOMKilled, exit 137" in report
    assert "not the only one" in report


def test_no_restarts_is_said_plainly() -> None:
    assert "restarts: 0 - nothing has been killed under this seat" in spend_report([])


def test_an_unstated_restart_count_costs_the_line_not_the_report() -> None:
    pod = pod_document(DEMO_PROBES)
    assert target_status(pod, APP).restarts is None
    report = format_probe_spend(APP, probe_budgets(pod, APP), (), since=LANDED)
    assert "restarts" not in report


def test_a_container_out_of_its_service_right_now_is_said_so_outright() -> None:
    """The one fact in the block that is about *now* rather than a window, and
    the only proof there is on the readiness half - `ready` goes false when a
    readiness streak completes. A count of failures does not say it."""
    report = spend_report(THE_PAUSE, ready=False)
    assert "not ready: the kubelet has this container out of its Service" in report
    # Silence is the healthy answer: a tick here is the line readers skip.
    assert "not ready" not in spend_report(THE_PAUSE)


def test_readiness_is_not_reported_on_a_target_that_declares_no_readiness_probe() -> (
    None
):
    report = spend_report(
        [], probes={"livenessProbe": {"periodSeconds": 10}}, ready=False
    )
    assert "not ready" not in report


def test_a_failure_against_a_probe_the_spec_no_longer_declares_is_labelled() -> None:
    """Probes cannot be changed on a running pod, but the events outlive the
    pod itself and the field selector matches on its *name*: a StatefulSet pod
    or a bare one recreated under the same name inherits the retained events of
    its predecessor, whose spec may have declared a probe this one does not.
    Calling that a startup budget would be a lie."""
    events = [unhealthy("Startup", 4, "2026-08-16T09:00:00Z", "2026-08-16T09:01:00Z")]
    report = spend_report(
        events, probes={"livenessProbe": {"periodSeconds": 10}}, since=LANDED
    )
    assert "startup: 4 failures" in report
    assert "declares no startupProbe now" in report


def test_an_unreadable_timestamp_drops_the_event_rather_than_the_report() -> None:
    events = [unhealthy("Liveness", 2, "last tuesday", "last tuesday")]
    assert probe_spend(events, APP, since=LANDED) == ()


def test_the_window_says_so_when_there_is_no_landing_to_measure_from() -> None:
    report = spend_report(THE_PAUSE, since=None)
    assert "over every event the cluster still holds" in report


# -- the memory budget ------------------------------------------------------


def memory_pod(*containers: dict[str, Any]) -> dict[str, Any]:
    """A pod whose containers state only what this half reads."""
    return {"spec": {"containers": list(containers)}}


def sized(
    name: str, limit: str | None = None, request: str | None = None
) -> dict[str, Any]:
    resources: dict[str, Any] = {}
    if limit is not None:
        resources["limits"] = {"memory": limit}
    if request is not None:
        resources["requests"] = {"memory": request}
    return {"name": name, "resources": resources}


def test_the_ceiling_is_the_sum_of_the_containers_limits() -> None:
    """The pod's cgroup gets that sum, and the seat is inside it: an ephemeral
    container is added afterwards and never widens it."""
    budget = memory_budget(
        memory_pod(sized("app", "512Mi", "256Mi"), sized("sidecar", "256Mi", "128Mi"))
    )
    assert budget.limit == 768 * MIB
    assert budget.reserved == 384 * MIB
    assert budget.free == 384 * MIB


def test_one_unlimited_container_leaves_the_pod_unlimited() -> None:
    budget = memory_budget(memory_pod(sized("app", "512Mi"), sized("sidecar")))
    assert budget.limit is None
    assert budget.free is None
    assert budget.fits, "there is no ceiling here for a seat to hit"
    assert memory_warning(budget) is None


def test_a_limit_with_no_request_is_reserved_in_full() -> None:
    """The API server's own defaulting: a container that states a limit and no
    request has the limit as its request, so nothing here is spare."""
    budget = memory_budget(memory_pod(sized("app", "512Mi")))
    assert budget.reserved == 512 * MIB
    assert budget.free == 0


def test_nothing_unreserved_is_said_as_a_reservation_not_as_free_memory() -> None:
    """The number is limits less requests, so on a Guaranteed pod it is 0
    however little the workload is resident. "0 free" would be a claim about
    usage that nothing here measures; "reserves all of it" is what the spec
    says, and a bare `0` in a column of `512Mi` reads as truncated anyway."""
    warning = memory_warning(memory_budget(memory_pod(sized("app", "512Mi"))))
    assert warning is not None
    assert "the workload reserves all 512Mi of the pod limit" in warning
    assert "free" not in warning
    assert "0 " not in warning


def test_a_pod_too_small_for_a_seat_is_told_how_short_and_what_to_type() -> None:
    warning = memory_warning(memory_budget(memory_pod(sized("app", "512Mi", "256Mi"))))
    assert warning is not None
    assert "256Mi unreserved of a 512Mi pod limit" in warning
    assert "--resize 2Gi" in warning
    assert "\n" not in warning, "one line, so it can be scanned beside the others"


def test_a_pod_with_room_says_nothing_at_all() -> None:
    """The whole point of measuring it. This is the bench pod after
    `--resize 4Gi`, which used to be told that tightly limited pods OOM."""
    assert (
        memory_warning(memory_budget(memory_pod(sized("app", "4Gi", "256Mi")))) is None
    )


def test_an_unreadable_quantity_is_unmeasured_rather_than_unlimited() -> None:
    """Nothing the API server would accept looks like this, so reaching it means
    the field was not what podbench thinks it is - and a warning computed from a
    guess is the thing this change exists to remove."""
    budget = memory_budget(memory_pod(sized("app", "512 megabytes")))
    assert budget.limit is None
    assert memory_warning(budget) is None


def test_a_binary_kilobyte_limit_is_a_limit_and_not_an_absence() -> None:
    """`Ki` is uppercase and `k` is not, and a suffix class that took `[Mk]i?`
    accepted the nonexistent `ki` while rejecting the real `Ki` - so a pod
    stating `600000Ki` read as having no memory limit at all and was told
    nothing."""
    budget = memory_budget(memory_pod(sized("app", "600000Ki")))
    assert budget.limit == 600000 * 1024
    warning = memory_warning(budget)
    assert warning is not None
    assert "585.9Mi" in warning


def test_the_applied_limit_wins_over_the_one_the_resize_patch_asked_for() -> None:
    """The resize subresource writes the *spec* and the kubelet may defer or
    refuse it, so a pod read back straight after `--resize` can state a limit
    its cgroup has not been given. R13 verified the applied value rather than
    the spec, and `status.containerStatuses[].resources` is where the kubelet
    publishes it."""
    pod = memory_pod(sized("app", "4Gi", "256Mi"))
    pod["status"] = {
        "containerStatuses": [
            {
                "name": "app",
                "resources": {
                    "limits": {"memory": "512Mi"},
                    "requests": {"memory": "256Mi"},
                },
            }
        ]
    }
    budget = memory_budget(pod)
    assert budget.limit == 512 * MIB, "the kubelet has not applied the 4Gi"
    assert memory_warning(budget) is not None


def test_a_status_that_publishes_no_resources_leaves_the_spec_in_charge() -> None:
    """The field arrived with in-place resize and a cluster without it reports
    nothing, which must not read as a container with no limits."""
    pod = memory_pod(sized("app", "512Mi", "256Mi"))
    pod["status"] = {"containerStatuses": [{"name": "app", "restartCount": 0}]}
    assert memory_budget(pod).limit == 512 * MIB


# -- the ephemeral-storage budget -------------------------------------------


def stored(name: str, limit: str | None = None) -> dict[str, Any]:
    resources: dict[str, Any] = {}
    if limit is not None:
        resources["limits"] = {"ephemeral-storage": limit}
    return {"name": name, "resources": resources}


def test_a_pod_whose_disk_a_seat_cannot_fit_in_is_told_so() -> None:
    """Report 3.8: disk, not memory, is the constraint - and the failure is the
    worse of the two, because exceeding the sum of the ephemeral-storage limits
    evicts the whole pod rather than only the seat."""
    warning = storage_warning(memory_pod(stored("app", "1Gi")))
    assert warning is not None
    assert "a 1Gi pod limit" in warning
    assert "evicts the whole pod" in warning
    assert "--resize raises memory only" in warning, "R13: --resize is memory-only"


def test_a_pod_with_disk_to_spare_is_told_nothing_about_it() -> None:
    assert storage_warning(memory_pod(stored("app", "4Gi"))) is None


def test_a_pod_with_no_ephemeral_storage_limit_is_told_nothing_either() -> None:
    """No pod-level ceiling to overrun; the node's own eviction thresholds are
    not something the pod spec states or podbench can read."""
    assert storage_warning(memory_pod(stored("app"))) is None
    assert storage_warning(memory_pod(stored("app", "1Gi"), stored("sidecar"))) is None
