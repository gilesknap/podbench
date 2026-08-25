"""The claim the whole beside-the-app layout rests on, asserted rather than believed.

`ioc-instance` is the chart every EPICS IOC at Diamond is deployed with, and the
design's practical payoff is that it needs **no upstream change**: the five keys
:func:`podbench.hotfix.values_snippet` emits are ordinary passthroughs the chart
already has. That is a claim about somebody else's chart at a version we do not
control, so it is rendered here rather than reasoned about.

The version is the one `p47-services` pins. It was **5.6.1** when this was
written; the plan that produced this file said `5.0.1-beta.2`, which does not
exist in the registry at all - which is exactly why this renders the chart
instead of trusting a note.

Two things about the shape, both of which cost time to find:

* `ioc-instance` is a **library** chart. `helm template` on it directly fails
  with "library charts are not installable", so this builds the same umbrella
  `p47-services` does - a dependency plus ``{{ include "ioc-instance" . }}``.
* The per-IOC values live under an ``ioc-instance:`` key, not under ``global:``
  (which supplies only location and domain). That is what
  :func:`values_snippet`'s *indent* parameter is for.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from podbench.hotfix import hotfix_claim, merged_values, values_snippet
from podbench.model import (
    HOTFIX_CLAIM_PATH,
    HOTFIX_CLAIM_VOLUME,
    HOTFIX_HOLD_PATH,
    SEAT_HOME_VOLUME,
)

CHART_REPO = "oci://ghcr.io/epics-containers/charts"
CHART_VERSION = "5.6.1"
APP = "bl47p-ea-fastcs-01"
ENTRYPOINT = (
    'stdio-socket --ptty "fastcs-example run /epics/ioc/config/controller.yaml"'
)
LIVENESS = ["/bin/bash", "/epics/ioc/liveness.sh"]
GID = 37887

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is not present"
)


def umbrella(tmp: Path) -> Path:
    """The wrapper chart `p47-services` builds around the library chart."""
    (tmp / "templates").mkdir(parents=True, exist_ok=True)
    (tmp / "Chart.yaml").write_text(
        "apiVersion: v2\n"
        "name: ec-service\n"
        "version: 1.0.0\n"
        "type: application\n"
        "dependencies:\n"
        "  - name: ioc-instance\n"
        f"    version: {CHART_VERSION}\n"
        f'    repository: "{CHART_REPO}"\n'
        "    import-values:\n"
        "      - child: ioc-instance\n"
        "        parent: ioc-instance\n"
    )
    (tmp / "templates" / "ioc_instance.yaml").write_text(
        '{{ include "ioc-instance" . }}\n'
    )
    snippet = values_snippet(
        APP, ENTRYPOINT, gid=str(GID), liveness_exec=LIVENESS, indent=2
    )
    # The podbench-release half is for a different chart; only the application
    # half is pasted into an ioc-instance values file.
    application_half = "\n".join(
        snippet.split("# 2. values for", 1)[1].splitlines()[2:]
    )
    (tmp / "values.yaml").write_text(
        "global:\n"
        "  location: bl47p\n"
        "  domain: p47\n"
        "ioc-instance:\n"
        "  image: ghcr.io/diamondlightsource/fastcs-example-debug:2025.10.1\n"
        '  livenessExecutable: ""\n'
        '  preStopExecutable: ""\n' + application_half + "\n"
    )
    return tmp


@pytest.fixture(scope="module")
def statefulset(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """`ioc-instance` as pinned, rendered with the snippet pasted in.

    Skipped rather than failed when the registry cannot be reached: an offline
    run should not look like a broken contract. A *render* failure is a real
    failure, because that is the contract this file exists to check.
    """
    chart = umbrella(tmp_path_factory.mktemp("ec-service"))
    built = subprocess.run(
        ["helm", "dependency", "build", str(chart)],
        capture_output=True,
        text=True,
        cwd=chart,
    )
    if built.returncode != 0:
        pytest.skip(f"cannot fetch {CHART_REPO}/ioc-instance: {built.stderr.strip()}")
    rendered = subprocess.run(
        ["helm", "template", "t", str(chart)],
        capture_output=True,
        text=True,
        cwd=chart,
        check=True,
    ).stdout
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "StatefulSet":
            return cast(dict[str, Any], doc)
    raise AssertionError("ioc-instance rendered no StatefulSet")


def container(statefulset: dict[str, Any]) -> dict[str, Any]:
    return cast(
        dict[str, Any], statefulset["spec"]["template"]["spec"]["containers"][0]
    )


def pod_spec(statefulset: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], statefulset["spec"]["template"]["spec"])


def test_the_supervisor_becomes_the_containers_command(
    statefulset: dict[str, Any],
) -> None:
    """No upstream change: `command`/`args` are passthroughs the chart already has."""
    main = container(statefulset)
    assert main["command"] == ["bash", "-c"]
    args = "\n".join(cast(list[str], main["args"]))
    assert ENTRYPOINT in args
    assert HOTFIX_HOLD_PATH in args


def test_the_claim_mounts_beside_the_projects_own_paths(
    statefulset: dict[str, Any],
) -> None:
    """Beside, not over. The chart's own mounts must survive alongside ours."""
    mounts = {
        str(m["name"]): str(m["mountPath"])
        for m in cast(list[Any], container(statefulset)["volumeMounts"])
    }
    assert mounts[HOTFIX_CLAIM_VOLUME] == HOTFIX_CLAIM_PATH
    # the chart's own, still there
    assert "/epics/runtime" in mounts.values()
    assert "/data" in mounts.values()


def test_both_podbench_volumes_are_declared_on_the_pod(
    statefulset: dict[str, Any],
) -> None:
    names = [str(v["name"]) for v in cast(list[Any], pod_spec(statefulset)["volumes"])]
    assert HOTFIX_CLAIM_VOLUME in names
    assert SEAT_HOME_VOLUME in names
    claim = next(
        v
        for v in cast(list[Any], pod_spec(statefulset)["volumes"])
        if v["name"] == HOTFIX_CLAIM_VOLUME
    )
    assert claim["persistentVolumeClaim"]["claimName"] == hotfix_claim(APP)


def test_the_wrapped_probe_survives_the_render(statefulset: dict[str, Any]) -> None:
    probe = cast(list[str], container(statefulset)["livenessProbe"]["exec"]["command"])
    assert probe[:2] == ["bash", "-c"]
    assert probe[2].startswith(f"if [ -e {HOTFIX_HOLD_PATH} ]; then exit 0; fi;")
    assert "/epics/ioc/liveness.sh" in probe[2]


def test_fsgroup_reaches_the_pod_security_context(statefulset: dict[str, Any]) -> None:
    assert pod_spec(statefulset)["securityContext"]["fsGroup"] == GID


# -- --values, rendered the way Argo renders it (#192) ----------------------


SHARED = """\
global:
  domain: p47
  location: bl47p

ioc-instance:
  hostNetwork: true
  volumes:
    - name: beamline-data
      hostPath:
        path: /exports/mybeamline/data/
  volumeMounts:
    - name: beamline-data
      mountPath: /exports/mybeamline/data/
"""
"""What `p47-services/services/values.yaml` gives every IOC on the beamline."""

SERVICE = """\
ioc-instance:
  image: ghcr.io/diamondlightsource/fastcs-example-debug:2025.10.1
  livenessExecutable: ""
  preStopExecutable: ""
"""
"""`bl47p-ea-fastcs-01` before podbench touched it: it declares no `volumes:`,
which is exactly why it inherits `beamline-data` and exactly why declaring one
for the first time is the dangerous move."""


@pytest.fixture(scope="module")
def merged_statefulset(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """`ioc-instance` rendered from a values file `--values` produced, whole.

    Rendered the way `argocd-apps` 5.5.0 renders it - `-f ../values.yaml -f
    values.yaml`, in that order, the service's own winning
    (`Charts/argocd-apps/templates/_apps.tpl`). Rendering it any other way would
    hide the very thing this is checking, because the failure mode is a list
    that replaces rather than merges across exactly that boundary.
    """
    chart = tmp_path_factory.mktemp("merged")
    (chart / "templates").mkdir()
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\n"
        "name: ec-service\n"
        "version: 1.0.0\n"
        "type: application\n"
        "dependencies:\n"
        "  - name: ioc-instance\n"
        f"    version: {CHART_VERSION}\n"
        f'    repository: "{CHART_REPO}"\n'
        "    import-values:\n"
        "      - child: ioc-instance\n"
        "        parent: ioc-instance\n"
    )
    (chart / "templates" / "ioc_instance.yaml").write_text(
        '{{ include "ioc-instance" . }}\n'
    )
    (chart / "shared.yaml").write_text(SHARED)
    emitted, _ = merged_values(
        SERVICE,
        values_snippet(APP, ENTRYPOINT, gid=str(GID), liveness_exec=LIVENESS),
        parent=SHARED,
    )
    (chart / "values.yaml").write_text(emitted)

    built = subprocess.run(
        ["helm", "dependency", "build", str(chart)],
        capture_output=True,
        text=True,
        cwd=chart,
    )
    if built.returncode != 0:
        pytest.skip(f"cannot fetch {CHART_REPO}/ioc-instance: {built.stderr.strip()}")
    rendered = subprocess.run(
        # The service's own file last, as argocd-apps orders them.
        ["helm", "template", APP, str(chart), "-f", "shared.yaml", "-f", "values.yaml"],
        capture_output=True,
        text=True,
        cwd=chart,
        check=True,
    ).stdout
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "StatefulSet":
            return cast(dict[str, Any], doc)
    raise AssertionError("ioc-instance rendered no StatefulSet")


def test_the_emitted_file_keeps_the_beamline_directory_it_inherited(
    merged_statefulset: dict[str, Any],
) -> None:
    """The measurement #192 exists for, now asserted against the real chart.

    `bl47p-ea-fastcs-01` declared no `volumes:` and inherited `beamline-data`
    wholesale. A helm list replaces across the parent/child merge rather than
    merging, so the emitted file declaring one takes that inheritance over
    completely - and if it did not absorb the shared entry first, the IOC comes
    back with its data directory silently unmounted.
    """
    volumes = {v["name"] for v in pod_spec(merged_statefulset)["volumes"]}
    mounts = {m["name"] for m in container(merged_statefulset)["volumeMounts"]}
    assert "beamline-data" in volumes
    assert "beamline-data" in mounts


def test_the_emitted_file_still_carries_everything_podbench_needs(
    merged_statefulset: dict[str, Any],
) -> None:
    """Deployed as emitted, with no hand-editing - which is the acceptance test
    for the whole workflow, not just for this merge."""
    volumes = {v["name"] for v in pod_spec(merged_statefulset)["volumes"]}
    assert {HOTFIX_CLAIM_VOLUME, SEAT_HOME_VOLUME} <= volumes
    main = container(merged_statefulset)
    assert main["command"] == ["bash", "-c"]
    assert HOTFIX_HOLD_PATH in "\n".join(cast(list[str], main["args"]))
    assert {
        m["mountPath"] for m in main["volumeMounts"] if m["name"] == HOTFIX_CLAIM_VOLUME
    } == {HOTFIX_CLAIM_PATH}
    assert pod_spec(merged_statefulset)["securityContext"]["fsGroup"] == GID


def test_the_service_keeps_the_keys_it_had_before_podbench_touched_it(
    merged_statefulset: dict[str, Any],
) -> None:
    """The plan's first falsification, through a real render rather than a diff."""
    assert (
        container(merged_statefulset)["image"]
        == "ghcr.io/diamondlightsource/fastcs-example-debug:2025.10.1"
    )
    assert pod_spec(merged_statefulset)["hostNetwork"] is True
