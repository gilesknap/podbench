"""The chart's half of two contracts: seat identity, and values.schema.json.

Everything else in the suite asserts the *Python* side agrees with itself: the
launcher mounts what :mod:`podbench.model` names, the snippet points at what
:func:`podbench.patch.identity_configmap` derives. None of it renders the chart,
so the one failure this feature is actually exposed to - the ConfigMap emitting
a key, a login name, a home or a name the launcher does not look for - passes
every one of those tests and ``helm lint`` too, and surfaces at runtime as
``Permission denied (publickey)``.

The mounts are asked of :func:`podbench.launcher.seat_identity_mounts` against a
pod built from :func:`podbench.patch.values_snippet`, rather than written out
here, so this file cannot drift from either half by being edited to match.

``values.schema.json`` is checked the same way, by asking helm rather than by
reading the JSON: helm is what enforces it, and what it enforces - a refusal at
``--set``, rather than a release that silently grants nothing - is the whole
reason the file exists.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from podbench.agent import GROUP_PATH, PASSWD_PATH
from podbench.launcher import SEAT_IDENTITY_MOUNTS, seat_identity_mounts
from podbench.model import (
    SEAT_GROUP_KEY,
    SEAT_HOME_PATH,
    SEAT_HOME_VOLUME,
    SEAT_IDENTITY_VOLUME,
    SEAT_PASSWD_KEY,
)
from podbench.patch import identity_configmap, values_snippet
from podbench.spec import dev_pod_spec
from podbench.sshcfg import SEAT_USER

CHART = Path(__file__).resolve().parent.parent / "Charts" / "podbench"
APP = "myapp"
UID = 1000
GID = 1000

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None or not CHART.is_dir(),
    reason="helm (or the chart) is not present, so the chart cannot be rendered",
)


def render(*args: str) -> subprocess.CompletedProcess[str]:
    """``helm template`` with the given extra arguments, run to completion.

    Not ``check=True``: half these calls are asserting that helm *refuses*, and
    the refusal arrives as a non-zero exit with the reason on stderr.
    """
    return subprocess.run(
        ["helm", "template", "pb", str(CHART), *args],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def configmap() -> dict[str, Any]:
    """The seat-identity ConfigMap as helm actually renders it."""
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "pb",
            str(CHART),
            "--set",
            "seatIdentity.enabled=true",
            "--set",
            f"seatIdentity.apps[0].name={APP}",
            "--set",
            f"seatIdentity.apps[0].uid={UID}",
            "--set",
            f"seatIdentity.apps[0].gid={GID}",
            "--show-only",
            "templates/configmap-seat-identity.yaml",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return cast(dict[str, Any], yaml.safe_load(rendered))


@pytest.fixture(scope="module")
def snippet() -> dict[str, Any]:
    """``patch --print-values``, parsed - the pod spec the user is told to write."""
    return cast(
        dict[str, Any],
        yaml.safe_load(values_snippet(APP, "/venv", uid=str(UID), gid=str(GID))),
    )


def seat_line(text: str) -> list[str]:
    """The seat's own record out of a passwd or group file."""
    line = next(one for one in text.splitlines() if one.startswith(f"{SEAT_USER}:"))
    return line.split(":")


def test_the_chart_names_the_configmap_the_snippet_points_at(
    configmap: dict[str, Any], snippet: dict[str, Any]
) -> None:
    """The helper and :func:`identity_configmap` derive one name in two places."""
    volume = next(
        entry
        for entry in snippet["extraVolumes"]
        if entry["name"] == SEAT_IDENTITY_VOLUME
    )
    assert configmap["metadata"]["name"] == identity_configmap(APP)
    assert volume["configMap"]["name"] == identity_configmap(APP)


def test_the_chart_emits_exactly_the_keys_the_launcher_subpaths(
    configmap: dict[str, Any],
) -> None:
    """A key the launcher does not name is a file the seat never gets."""
    assert set(configmap["data"]) == {SEAT_PASSWD_KEY, SEAT_GROUP_KEY}


def test_the_passwd_record_is_the_login_the_client_stanza_asks_for(
    configmap: dict[str, Any],
) -> None:
    """Name, uid, gid and home, in the seven fields ``getpwnam`` expects.

    The login name has to be :data:`podbench.sshcfg.SEAT_USER` because that is
    what the generated ``Host`` stanza puts in ``User``, and the home has to be
    :data:`SEAT_HOME_PATH` because sshd puts the session in the home NSS gives
    it - which is where the launcher told the agent to write sshd's config.
    """
    fields = seat_line(configmap["data"][SEAT_PASSWD_KEY])
    assert len(fields) == 7
    assert fields[0] == SEAT_USER
    assert int(fields[2]) == UID
    assert int(fields[3]) == GID
    assert fields[5] == SEAT_HOME_PATH


def test_the_group_record_carries_the_same_gid_as_the_snippets_fsgroup(
    configmap: dict[str, Any], snippet: dict[str, Any]
) -> None:
    """Four fields, and the gid the home volume is chgrped to."""
    fields = seat_line(configmap["data"][SEAT_GROUP_KEY])
    assert len(fields) == 4
    assert fields[0] == SEAT_USER
    assert int(fields[2]) == GID
    assert snippet["podSecurityContext"]["fsGroup"] == GID


def test_the_identity_layout_names_the_keys_the_chart_emits(
    configmap: dict[str, Any],
) -> None:
    """The layout an *ordinary* container would mount, against the rendered keys.

    ``SEAT_IDENTITY_MOUNTS`` is one mount per file, each with its own
    ``subPath``, because mounting the volume at ``/etc`` would replace the
    directory and take ``nsswitch.conf`` with it. Every ``subPath`` has to be a
    key of the ConfigMap the chart rendered, or the seat gets an empty file
    where its identity should be. No ephemeral container may carry this - see
    the mount test below - so this is the contract for a ``podbench dev``
    sidecar, and the reason the chart's keys are still checked.
    """
    assert [path for path, _ in SEAT_IDENTITY_MOUNTS] == [PASSWD_PATH, GROUP_PATH]
    assert {key for _, key in SEAT_IDENTITY_MOUNTS} == set(configmap["data"])


def test_the_launcher_mounts_what_the_snippet_declares_and_attach_may_carry(
    snippet: dict[str, Any],
) -> None:
    """The join: a pod written as the snippet says, mounted as the launcher does.

    The home volume, whole and writable, and nothing else. The identity is left
    unmounted however plainly the pod declares it: it needs a ``subPath`` per
    file, and the API server refuses ``subPath`` on an ephemeral container for
    the whole request - so authoring it here would cost the attach the seat
    rather than the identity.
    """
    pod = {
        "spec": {
            "volumes": snippet["extraVolumes"],
            "containers": [{"name": APP, "volumeMounts": snippet["extraVolumeMounts"]}],
        }
    }
    mounts, warnings = seat_identity_mounts(pod, APP)
    assert warnings == []
    assert mounts == [{"name": SEAT_HOME_VOLUME, "mountPath": SEAT_HOME_PATH}]
    assert not [mount for mount in mounts if mount["name"] == SEAT_IDENTITY_VOLUME]


def origin_from_snippet(snippet: dict[str, Any]) -> dict[str, Any]:
    """A pod deployed as ``patch --print-values`` says, in one place.

    The application's own uid and gid are on the container because that is what
    the snippet's comments *require* of whoever pastes it: the numbers in
    ``seatIdentity.apps[]`` have to be the application container's own, or the
    record resolves to a user nothing runs as.
    """
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": APP, "namespace": "demo"},
        "spec": {
            "securityContext": snippet["podSecurityContext"],
            "volumes": snippet["extraVolumes"],
            "containers": [
                {
                    "name": APP,
                    "image": f"{APP}:1",
                    "securityContext": {"runAsUser": UID, "runAsGroup": GID},
                    "volumeMounts": snippet["extraVolumeMounts"],
                }
            ],
        },
    }


def test_a_dev_pods_sidecar_logs_in_as_the_uid_the_chart_wrote(
    configmap: dict[str, Any], snippet: dict[str, Any]
) -> None:
    """The other join, and the one that fails as ``Permission denied (publickey)``.

    A dev pod's sidecar is an ordinary container, so it *can* be given the two
    files — and must then run as the uid the passwd record names. If the chart
    writes 1000 and podbench authors a root sidecar, sshd resolves the login
    name to 1000, cannot read the ``authorized_keys`` root wrote, and refuses
    the key without mentioning identity at all. Both numbers are read back from
    the rendered ConfigMap rather than from :data:`UID`, so a chart that starts
    emitting a different record fails here.
    """
    pod = dev_pod_spec(
        origin_from_snippet(snippet),
        name=f"{APP}-podbench",
        target_container=APP,
        image="podbench:test",
        target_port=8080,
    )
    sidecar = next(
        entry for entry in pod["spec"]["containers"] if entry["name"] == "podbench"
    )
    projected = {
        mount["mountPath"]: mount["subPath"]
        for mount in sidecar["volumeMounts"]
        if mount["name"] == SEAT_IDENTITY_VOLUME
    }
    assert projected == dict(SEAT_IDENTITY_MOUNTS)
    assert set(projected.values()) <= set(configmap["data"])

    assert sidecar["securityContext"]["runAsUser"] == int(
        seat_line(configmap["data"][SEAT_PASSWD_KEY])[2]
    )
    assert sidecar["securityContext"]["runAsGroup"] == int(
        seat_line(configmap["data"][SEAT_GROUP_KEY])[2]
    )
    # And the home that gid owns is the one the snippet's fsGroup hands over.
    assert (
        pod["spec"]["securityContext"]["fsGroup"]
        == snippet["podSecurityContext"]["fsGroup"]
    )


def test_the_generated_schema_is_committed_and_covers_every_default() -> None:
    """The file exists, closes the root, and names every key ``values.yaml`` has.

    ``values.schema.json`` is generated by the ``helm-schema`` pre-commit hook,
    so a key added to ``values.yaml`` without the hook having run leaves a
    schema that refuses the chart's own defaults. ``global`` is the exception:
    the generator injects it, because helm passes a subchart's ``global`` map
    through whatever the schema says.
    """
    text = (CHART / "values.schema.json").read_text()
    schema = cast(dict[str, Any], json.loads(text))
    values = cast(dict[str, Any], yaml.safe_load((CHART / "values.yaml").read_text()))

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(values) | {"global"}


def test_the_chart_renders_against_its_own_defaults() -> None:
    """Which is helm validating ``values.yaml`` against the schema, not a render.

    helm checks the *coalesced* values, so a plain ``helm template`` is already
    the assertion that every default in ``values.yaml`` satisfies the schema
    generated from it.
    """
    assert render().returncode == 0


def test_the_documented_example_entries_are_ones_the_schema_accepts() -> None:
    """``example.values.yaml`` is the item shape, so it has to install.

    It exists to teach the schema what one ``rbac.subjects`` /
    ``patchVenv.claims`` / ``seatIdentity.apps`` entry looks like. Feeding it
    back in is what catches an example edited to document a field the generator
    never saw - which would otherwise read as documentation and behave as a
    refusal.
    """
    rendered = render(
        "--values",
        str(CHART / "example.values.yaml"),
        "--set",
        "rbac.create=true",
        "--set",
        "patchVenv.enabled=true",
        "--set",
        "seatIdentity.enabled=true",
    )
    assert rendered.returncode == 0, rendered.stderr
    assert "kind: RoleBinding" in rendered.stdout


def test_a_mistyped_top_level_key_is_refused_rather_than_ignored() -> None:
    """The plain case: helm without a schema accepts any key and drops it."""
    rendered = render("--set", "scratchPvcs.enabled=true")
    assert rendered.returncode != 0
    assert "additional properties 'scratchPvcs' not allowed" in rendered.stderr


def test_a_mistyped_rbac_verb_is_refused_at_the_point_of_the_typo() -> None:
    """The case the schema is actually for.

    ``rbac.iterat`` is accepted silently without a schema: the release grants
    the observe verbs only, and the mistake surfaces much later as an attach
    failing with an RBAC error naming a verb the user believes they were given.
    """
    rendered = render("--set", "rbac.create=true", "--set", "rbac.iterat=true")
    assert rendered.returncode != 0
    assert "additional properties 'iterat' not allowed" in rendered.stderr
    assert "/rbac" in rendered.stderr


def test_a_mistyped_field_inside_a_list_entry_is_refused_too() -> None:
    """Which is what ``example.values.yaml`` buys.

    A list that defaults to ``[]`` infers no item schema at all, so without the
    example this misspelling of ``name`` would produce a RoleBinding subject
    with no name - rejected by the API server at install, or silently binding
    nothing.
    """
    rendered = render(
        "--set",
        "rbac.create=true",
        "--set",
        "rbac.subjects[0].kind=Group",
        "--set",
        "rbac.subjects[0].nmae=developers",
    )
    assert rendered.returncode != 0
    assert "additional properties 'nmae' not allowed" in rendered.stderr
