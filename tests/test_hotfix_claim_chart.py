"""The subchart a target chart depends on, and the contract it has to keep.

Issue #191. podbench's own chart can create hotfix claims from a central
``hotfixProject.claims[]`` list and still does — but that is a namespace-wide
release naming every application that might ever be hotfixed, and a claim there
outlives every service that ever used it. This chart is the other shape: one
claim, declared by the target's own chart, with the pod's lifecycle.

Its whole contract is that a chart which *depends* on it is not changed by
depending on it, so that is what most of this module asserts — from the outside,
through a real umbrella chart with a real ``dependencies:`` entry, because the
guard being tested only means anything through helm's subchart machinery.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from podbench.hotfix import CLAIM_DEFAULT_SIZE, hotfix_claim

CHARTS = Path(__file__).resolve().parent.parent / "Charts"
CHART = CHARTS / "podbench-hotfix-claim"
CENTRAL = CHARTS / "podbench"
APP = "bl47p-ea-fastcs-01"

KEEP = "helm.sh/resource-policy"
SYNC_OPTIONS = "argocd.argoproj.io/sync-options"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None or not CHART.is_dir(),
    reason="helm (or the chart) is not present, so the chart cannot be rendered",
)


def render(chart: Path, release: str, *args: str) -> subprocess.CompletedProcess[str]:
    """``helm template`` run to completion.

    Not ``check=True``: some of these calls assert that helm *refuses*, and the
    refusal arrives as a non-zero exit with the reason on stderr.
    """
    return subprocess.run(
        ["helm", "template", release, str(chart), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def objects(stdout: str) -> list[dict[str, Any]]:
    """Every Kubernetes object in a render, comments and separators dropped."""
    return [
        cast(dict[str, Any], doc)
        for doc in yaml.safe_load_all(stdout)
        if isinstance(doc, dict)
    ]


def umbrella(tmp: Path) -> Path:
    """A target chart that declares the dependency, as a shared Chart.yaml does.

    ``file://`` rather than the OCI repository, so this asserts the chart in the
    working tree rather than the last one published.
    """
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "Chart.yaml").write_text(
        "apiVersion: v2\n"
        "name: ec-service\n"
        "version: 1.0.0\n"
        "type: application\n"
        "dependencies:\n"
        "  - name: podbench-hotfix-claim\n"
        "    version: 0.0.0\n"
        f'    repository: "file://{CHART}"\n'
    )
    (tmp / "values.yaml").write_text("")
    subprocess.run(
        ["helm", "dependency", "build", str(tmp)],
        capture_output=True,
        text=True,
        check=True,
    )
    return tmp


@pytest.fixture(scope="module")
def target(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return umbrella(tmp_path_factory.mktemp("ec-service"))


def test_a_chart_that_depends_on_it_is_not_changed_by_depending_on_it(
    target: Path,
) -> None:
    """#191's falsification, and the reason the dependency can be shared.

    It has to sit in one ``Chart.yaml`` inherited by every service a site runs,
    hotfix-enabled or not. A service that never sets the boolean must render
    exactly what it rendered before the dependency existed.
    """
    rendered = render(target, APP)
    assert rendered.returncode == 0, rendered.stderr
    assert objects(rendered.stdout) == []


def test_one_boolean_is_the_whole_of_what_a_service_has_to_say(
    target: Path,
) -> None:
    """The other half of the contract: asking for it costs one line."""
    rendered = render(target, APP, "--set", "podbench-hotfix-claim.enabled=true")
    assert rendered.returncode == 0, rendered.stderr
    (claim,) = objects(rendered.stdout)
    assert claim["kind"] == "PersistentVolumeClaim"
    assert claim["metadata"]["name"] == hotfix_claim(APP)


def test_the_claim_is_named_what_hotfix_values_tells_the_pod_to_mount() -> None:
    """The two have to agree or the workload mounts a claim that is not there.

    ``hotfix values`` writes ``claimName`` into the target's ``volumes:`` entry
    from :func:`podbench.hotfix.hotfix_claim`, and the chart derives the same
    name from the release. Nothing enforces that but this.
    """
    rendered = render(CHART, APP, "--set", "enabled=true")
    assert rendered.returncode == 0, rendered.stderr
    (claim,) = objects(rendered.stdout)
    assert claim["metadata"]["name"] == hotfix_claim(APP)


def test_the_claim_is_annotated_against_both_ways_it_can_be_taken_away() -> None:
    """Measured on p47-beamline, 2026-08-22 — see issue #190.

    A claim carrying ``helm.sh/resource-policy: keep`` alone was pruned three
    minutes after it left the desired state, and its PV was deleted with it.
    Argo honours Helm's annotation only as ``Delete=false``, which is the
    Application being deleted; ending a hotfix is the other event.
    """
    rendered = render(CHART, APP, "--set", "enabled=true")
    (claim,) = objects(rendered.stdout)
    annotations = claim["metadata"]["annotations"]
    assert annotations[KEEP] == "keep"
    assert annotations[SYNC_OPTIONS] == "Prune=false,Delete=false"


def test_neither_keep_annotation_can_be_overridden_from_values() -> None:
    """``annotations`` adds; it does not replace.

    A claim that can be pruned is a hotfix silently lost, which is the one
    failure this whole mode exists to prevent, so the two that prevent it are
    not a value.
    """
    # helm's --set splits on the unescaped dots in a key path, and this key has
    # three of its own.
    escaped = SYNC_OPTIONS.replace(".", "\\.")
    rendered = render(
        CHART,
        APP,
        "--set",
        "enabled=true",
        "--set",
        f"annotations.{escaped}=overridden",
    )
    assert rendered.returncode == 0, rendered.stderr
    (claim,) = objects(rendered.stdout)
    assert claim["metadata"]["annotations"][SYNC_OPTIONS] == "Prune=false,Delete=false"


def test_the_subchart_and_the_central_route_produce_the_same_claim() -> None:
    """Two routes to one object, and #191 keeps the older one working.

    Anything they disagree about is a site that gets a different claim
    depending on which one it happened to adopt.
    """
    mine = objects(render(CHART, APP, "--set", "enabled=true").stdout)[0]
    central = [
        obj
        for obj in objects(
            render(
                CENTRAL,
                "pb",
                "--set",
                "hotfixProject.enabled=true",
                "--set",
                f"hotfixProject.claims[0].name={APP}",
            ).stdout
        )
        if obj["kind"] == "PersistentVolumeClaim"
    ][0]

    assert mine["metadata"]["name"] == central["metadata"]["name"]
    for key in (KEEP, SYNC_OPTIONS):
        assert (
            mine["metadata"]["annotations"][key]
            == (central["metadata"]["annotations"][key])
        )
    assert mine["spec"] == central["spec"]
    # Both label the claim with the application, which is what makes a stray
    # claim attributable to the service it was for.
    assert (
        mine["metadata"]["labels"]["podbench.dev/hotfix-target"]
        == central["metadata"]["labels"]["podbench.dev/hotfix-target"]
    )


def test_a_second_replica_is_refused_by_the_access_mode_rather_than_corrupted() -> None:
    """ReadWriteOnce deliberately, and not a value.

    One checkout cannot serve two writers, so the access mode is what makes a
    second replica fail loudly.
    """
    (claim,) = objects(render(CHART, APP, "--set", "enabled=true").stdout)
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]


def test_a_mistyped_key_is_refused_rather_than_silently_rendering_nothing() -> None:
    """Which matters more here than in most charts.

    Without the schema, ``enabld: true`` renders nothing and is
    indistinguishable from a claim the cluster refused — and the user finds out
    when `hotfix init` meets a pod with no `/podbench/app`.
    """
    rendered = render(CHART, APP, "--set", "enabld=true")
    assert rendered.returncode != 0
    assert "enabld" in rendered.stderr


def test_the_generated_schema_is_committed_and_covers_every_default() -> None:
    """``values.schema.json`` is generated by pre-commit, never hand-written.

    ``global`` is helm's, added by the generator so the chart can be a
    dependency at all — which is the only thing this chart is ever used as.
    """
    schema = json.loads((CHART / "values.schema.json").read_text())
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(values) | {"global"}


def test_the_chart_default_size_is_the_launcher_default_size() -> None:
    """The one number this chart and the launcher both spell.

    Helm cannot import python, so ``size`` is written twice - here and as
    :data:`podbench.hotfix.CLAIM_DEFAULT_SIZE` - and a site gets whichever of
    the two it happened to use: ``hotfix values`` writes the launcher's into the
    target's values file, while a chart that depends on this one and never
    mentions ``size`` gets the chart's. Drift between them is a claim sized by
    accident.

    Both spellings are rendered: ``values.yaml`` supplies ``size`` on an
    ordinary install, and the template's own fallback is only reached by a
    values file that sets it to null.
    """
    (claim,) = objects(render(CHART, APP, "--set", "enabled=true").stdout)
    assert claim["spec"]["resources"]["requests"]["storage"] == CLAIM_DEFAULT_SIZE

    (fallback,) = objects(
        render(CHART, APP, "--set", "enabled=true", "--set", "size=null").stdout
    )
    assert fallback["spec"]["resources"]["requests"]["storage"] == CLAIM_DEFAULT_SIZE
