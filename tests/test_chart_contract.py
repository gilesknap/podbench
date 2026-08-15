"""The chart's half of the seat-identity contract, checked against the code's.

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
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from podbench.agent import GROUP_PATH, PASSWD_PATH
from podbench.launcher import seat_identity_mounts
from podbench.model import (
    SEAT_GROUP_KEY,
    SEAT_HOME_PATH,
    SEAT_HOME_VOLUME,
    SEAT_IDENTITY_VOLUME,
    SEAT_PASSWD_KEY,
)
from podbench.patch import identity_configmap, values_snippet
from podbench.sshcfg import SEAT_USER

CHART = Path(__file__).resolve().parent.parent / "Charts" / "podbench"
APP = "myapp"
UID = 1000
GID = 1000

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None or not CHART.is_dir(),
    reason="helm (or the chart) is not present, so the chart cannot be rendered",
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


def test_the_launcher_mounts_what_the_chart_and_snippet_between_them_produce(
    configmap: dict[str, Any], snippet: dict[str, Any]
) -> None:
    """The join: a pod written as the snippet says, mounted as the launcher does.

    The identity is read-only and lands one file at a time by ``subPath``; the
    home is writable and whole. Both ``subPath`` values have to be keys of the
    ConfigMap the chart rendered, or the seat gets an empty file where its
    identity should be.
    """
    pod = {
        "spec": {
            "volumes": snippet["extraVolumes"],
            "containers": [{"name": APP, "volumeMounts": snippet["extraVolumeMounts"]}],
        }
    }
    mounts, warnings = seat_identity_mounts(pod, APP)
    assert warnings == []
    by_path = {mount["mountPath"]: mount for mount in mounts}
    assert set(by_path) == {PASSWD_PATH, GROUP_PATH, SEAT_HOME_PATH}

    passwd = by_path[PASSWD_PATH]
    group = by_path[GROUP_PATH]
    assert passwd["name"] == group["name"] == SEAT_IDENTITY_VOLUME
    assert passwd["readOnly"] is True
    assert group["readOnly"] is True
    assert {passwd["subPath"], group["subPath"]} == set(configmap["data"])

    home = by_path[SEAT_HOME_PATH]
    assert home["name"] == SEAT_HOME_VOLUME
    assert "readOnly" not in home
    assert "subPath" not in home
