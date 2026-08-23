"""Tests for Hotfix mode.

Nothing here touches a cluster, a git remote or a real PVC. The claim is a
``tmp_path`` directory or a fake store that answers ``git`` from a script, and
``kubectl`` is the same injected :class:`podbench.kubectl.Runner` seam the rest
of the suite uses.

The fixtures are the shapes that make Hotfix mode dangerous rather than the ones
that make it work: a Deployment with three replicas (the claim is RWO, so this
must be refused), a pod whose deployed ``imageID`` has moved on from the one the
hotfix was made against (the venv now shadows a newer image), and a manifest
written by a schema version that predates half its fields.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
import yaml

from podbench import console, hotfix, model, oci
from podbench.kubectl import DEFAULT_CALL_TIMEOUT, CommandResult, Kubectl
from podbench.launcher import Session
from podbench.model import ContainerRef, PodRef, Rung

VENV = "/opt/venv"
CHECKOUT = "/podbench/app"
PROJECT_SRC = "/proc/1/root/app"
BASE_SHA = "1111111111111111111111111111111111111111"
HEAD_SHA = "2222222222222222222222222222222222222222"
BASE_DIGEST = "ghcr.io/acme/api@sha256:aaaa"
NEW_DIGEST = "ghcr.io/acme/api@sha256:bbbb"

GIT = f"git -c safe.directory={CHECKOUT} -C {CHECKOUT}"

SEEDED_PYTHON = "/podbench/app/.python/cpython-3.11.13-linux-x86_64-gnu/bin/python3"

PYVENV_CFG = (
    "home = /usr/local/bin\ninclude-system-site-packages = false\nversion = 3.12.7\n"
)


def pod_json(
    *,
    name: str = "api-7f9-abc",
    digest: str = BASE_DIGEST,
    owner: str | None = "replicaset",
    annotations: dict[str, str] | None = None,
    seat: bool = True,
    claim: bool = False,
) -> dict[str, Any]:
    """One application pod, optionally carrying a podbench seat and a claim."""
    metadata: dict[str, Any] = {"name": name, "annotations": annotations or {}}
    if owner is not None:
        metadata["ownerReferences"] = [
            {"kind": owner.title(), "name": "api-7f9", "controller": True}
        ]
    container: dict[str, Any] = {"name": "app", "image": "ghcr.io/acme/api:1.4.0"}
    if claim:
        # The listing's whole filter: a volume, which a GitOps controller
        # reconciles towards rather than strips.
        container["volumeMounts"] = [
            {"name": model.HOTFIX_CLAIM_VOLUME, "mountPath": model.HOTFIX_APP_PATH}
        ]
    spec: dict[str, Any] = {"containers": [container]}
    status: dict[str, Any] = {
        "phase": "Running",
        "containerStatuses": [
            {"name": "app", "image": "ghcr.io/acme/api:1.4.0", "imageID": digest}
        ],
    }
    if seat:
        spec["ephemeralContainers"] = [{"name": "podbench-1"}]
        status["ephemeralContainerStatuses"] = [
            {"name": "podbench-1", "state": {"running": {"startedAt": "now"}}}
        ]
    return {"metadata": metadata, "spec": spec, "status": status}


def workload_json(kind: str = "Deployment", replicas: int = 1) -> dict[str, Any]:
    return {
        "kind": kind,
        "metadata": {"name": "api"},
        "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": "api"}}},
    }


def replicaset_json(replicas: int = 1) -> dict[str, Any]:
    return {
        "kind": "ReplicaSet",
        "metadata": {
            "name": "api-7f9",
            "ownerReferences": [
                {"kind": "Deployment", "name": "api", "controller": True}
            ],
        },
        "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": "api"}}},
    }


class FakeRunner:
    """A ``kubectl`` that answers from a table and records every call.

    Keys are the argv with this module's fixed ``kubectl -n demo`` prefix
    removed, matched by prefix so that a long ``exec`` line can be scripted by
    its first few words.
    """

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.responses = dict(responses or {})
        self.failures: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []
        self.stdins: list[str | None] = []
        self.timeouts: list[float | None] = []

    def key(self, argv: Sequence[str]) -> str:
        # The bound issue #118 added rides in the global flags, so it is dropped
        # here rather than counted: this key is what every canned response is
        # matched on, and an offset that moved would match none of them.
        rest = [word for word in argv if not word.startswith("--request-timeout=")]
        return " ".join(rest[3:])

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(argv))
        self.stdins.append(stdin)
        self.timeouts.append(timeout)
        key = self.key(argv)
        for prefix, message in self.failures.items():
            if key.startswith(prefix):
                return CommandResult(tuple(argv), 1, "", message)
        for prefix, payload in self.responses.items():
            if key.startswith(prefix):
                return CommandResult(tuple(argv), 0, payload, "")
        return CommandResult(tuple(argv), 0, "", "")

    def matching(self, prefix: str) -> list[tuple[str, ...]]:
        return [argv for argv in self.calls if self.key(argv).startswith(prefix)]


class FakeStore:
    """A claim whose files are a dict and whose git is a script."""

    def __init__(
        self,
        files: dict[str, str] | None = None,
        outputs: dict[str, str] | None = None,
    ) -> None:
        self.files = dict(files or {})
        self.outputs = dict(outputs or {})
        self.failures: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], *, check: bool = True) -> CommandResult:
        self.calls.append(tuple(argv))
        line = " ".join(argv)

        for prefix, message in self.failures.items():
            if line.startswith(prefix):
                if check:
                    raise hotfix.HotfixError(message)
                return CommandResult(tuple(argv), 1, "", message)
        for prefix, payload in self.outputs.items():
            if line.startswith(prefix):
                return CommandResult(tuple(argv), 0, payload, "")
        # uv nests its interpreters, so seeded_python discovers rather than
        # assumes; a store that says nothing answers with the real shape.
        if line.startswith("bash -c ls -d") and "bin/python3" in line:
            return CommandResult(tuple(argv), 0, SEEDED_PYTHON + "\n", "")
        return CommandResult(tuple(argv), 0, "", "")

    def read_text(self, path: str) -> str | None:
        return self.files.get(path)

    def write_text(self, path: str, text: str) -> None:
        self.files[path] = text

    def exists(self, path: str) -> bool:
        return path in self.files

    def ran(self, prefix: str) -> bool:
        return any(" ".join(argv).startswith(prefix) for argv in self.calls)


def kube(runner: FakeRunner) -> Kubectl:
    return Kubectl("demo", runner=runner)


def seeded_store(**extra: str) -> FakeStore:
    files = {
        CHECKOUT: "",
        f"{CHECKOUT}/pyproject.toml": "",
        f"{CHECKOUT}/.venv/pyvenv.cfg": PYVENV_CFG,
        f"{CHECKOUT}/.git": "",
    }
    files.update(extra)
    return FakeStore(
        files=files,
        outputs={
            f"{GIT} rev-parse HEAD": HEAD_SHA,
            f"{GIT} log": f"{HEAD_SHA}\x1fmake the beam behave\n",
        },
    )


def a_manifest(**overrides: Any) -> hotfix.HotfixManifest:
    defaults: dict[str, Any] = {
        "venv": VENV,
        "checkout": CHECKOUT,
        "repo": "https://example.invalid/acme/api.git",
        "base_image": "ghcr.io/acme/api:1.4.0",
        "base_image_digest": BASE_DIGEST,
        "interpreter": "3.12.7",
        "container": "app",
        "commit": BASE_SHA,
        "base_commit": BASE_SHA,
        "author": "Ada <ada@example.invalid>",
        "timestamp": "2026-08-15T09:00:00+00:00",
    }
    defaults.update(overrides)
    return hotfix.HotfixManifest(**defaults)


# -- the manifest ----------------------------------------------------------


def test_manifest_round_trips_through_json() -> None:
    manifest = a_manifest(
        ahead=2,
        commits=(hotfix.HotfixCommit(HEAD_SHA, "fix the thing"),),
        consolidated_branch="patch/beamtime-14",
    )
    assert hotfix.HotfixManifest.from_json(manifest.to_json()) == manifest


def test_a_v1_manifest_loads_with_the_new_fields_defaulted() -> None:
    """The falsification for #205 item 2: an existing claim carrying a recorded
    non-default venv must keep loading, and keep being honoured."""
    payload = json.loads(a_manifest().to_json())
    payload["version"] = 1
    del payload["claimVenv"]
    del payload["baseCommitAssumed"]

    loaded = hotfix.HotfixManifest.from_mapping(payload)

    assert loaded.venv == VENV
    assert hotfix.manifest_path(loaded.venv) == f"{VENV}/.podbench-hotfix.json"
    assert loaded.claim_venv == hotfix.CLAIM_VENV_DIR
    assert loaded.stale_schema


def test_manifest_round_trips_on_disk(tmp_path: Path) -> None:
    """The claim, standing in as a directory: write it, read it back."""
    store = hotfix.LocalStore()
    manifest = a_manifest(venv=str(tmp_path), ahead=1)
    hotfix.write_manifest(store, manifest)
    assert (tmp_path / hotfix.MANIFEST_FILENAME).is_file()
    assert hotfix.read_manifest(store, str(tmp_path)) == manifest


def test_read_manifest_is_none_on_an_unused_claim(tmp_path: Path) -> None:
    assert hotfix.read_manifest(hotfix.LocalStore(), str(tmp_path)) is None


def test_manifest_from_an_older_schema_version_loads_with_defaults() -> None:
    """A manifest with no ``version`` key at all is version 0.

    A future podbench meeting this volume has to keep the provenance it can read
    rather than refuse the file, and has to be able to *say* the manifest is old.
    """
    older = {
        "venv": VENV,
        "checkout": CHECKOUT,
        "commit": HEAD_SHA,
        "baseCommit": BASE_SHA,
        "commits": [{"sha": HEAD_SHA, "subject": "fix the thing"}],
    }
    manifest = hotfix.HotfixManifest.from_json(json.dumps(older))
    assert manifest.schema_version == 0
    assert manifest.stale_schema
    assert manifest.commit == HEAD_SHA
    # Absent from the old schema, and not invented:
    assert manifest.base_image_digest == ""
    assert manifest.interpreter == ""
    # ``ahead`` post-dates the commit list, so it is derived rather than zeroed.
    assert manifest.ahead == 1


def test_rewriting_an_older_manifest_stamps_the_current_version() -> None:
    older = hotfix.HotfixManifest.from_json(json.dumps({"commit": HEAD_SHA}))
    assert hotfix.HotfixManifest.from_json(older.to_json()).schema_version == (
        hotfix.MANIFEST_VERSION
    )


def test_manifest_from_a_newer_schema_version_is_refused() -> None:
    payload = json.dumps({"version": hotfix.MANIFEST_VERSION + 1, "commit": HEAD_SHA})
    with pytest.raises(hotfix.ManifestVersionError, match="Upgrade podbench"):
        hotfix.HotfixManifest.from_json(payload)


# -- paths and small pure helpers ------------------------------------------


def test_the_checkout_is_the_claim_itself() -> None:
    """No longer buried in the venv: the claim mounts beside the project."""
    assert hotfix.checkout_path() == CHECKOUT == model.HOTFIX_APP_PATH
    assert not hotfix.checkout_path().endswith("/src")


def test_install_pins_the_interpreter_on_the_claim() -> None:
    """Left to itself uv finds the image's, and the venv names a path that goes
    away on the next restart - the fix reverting rather than an error."""
    argv = hotfix.install_argv(SEEDED_PYTHON, CHECKOUT)
    assert argv[:4] == ["env", f"UV_CACHE_DIR={CHECKOUT}/.uv-cache", "uv", "sync"]
    assert CHECKOUT in argv
    assert SEEDED_PYTHON in argv


def test_the_interpreter_is_discovered_because_uv_nests_it() -> None:
    """``<root>/bin/python3`` does not exist: uv installs one level down."""
    store = FakeStore()
    assert hotfix.seeded_python(store).startswith(model.HOTFIX_INTERPRETER_PATH)
    assert "/cpython-" in hotfix.seeded_python(store)


def test_a_seed_with_no_interpreter_refuses_rather_than_guessing() -> None:
    store = FakeStore()
    store.outputs["bash -c ls -d"] = "\n"
    with pytest.raises(hotfix.HotfixError, match="no interpreter under"):
        hotfix.seeded_python(store)


def test_metadata_changed_only_for_packaging_files() -> None:
    assert not hotfix.metadata_changed(["src/api/beam.py", "README.md"])
    assert hotfix.metadata_changed(["src/api/beam.py", "pyproject.toml"])


def test_changed_paths_covers_the_whole_range_since_the_last_apply() -> None:
    """Several hand commits can accumulate between two applies, so the metadata
    question is asked of the range and not of HEAD alone."""
    store = FakeStore(
        outputs={
            f"{GIT} diff --name-only {BASE_SHA}..{HEAD_SHA}": (
                "setup.cfg\nsrc/api/beam file.py\n"
            )
        }
    )
    assert hotfix.changed_paths(store, CHECKOUT, BASE_SHA, HEAD_SHA) == (
        "setup.cfg",
        # One path, not two: --name-only is line-separated and paths may contain
        # spaces.
        "src/api/beam file.py",
    )


def test_changed_paths_is_empty_when_head_has_not_moved() -> None:
    store = FakeStore()
    assert hotfix.changed_paths(store, CHECKOUT, HEAD_SHA, HEAD_SHA) == ()
    assert not store.calls


def test_changed_paths_falls_back_to_head_without_a_recorded_commit() -> None:
    """A manifest old enough to have no ``commit`` gives no range to walk."""
    store = FakeStore(outputs={f"{GIT} show": "\npyproject.toml\n"})
    assert hotfix.changed_paths(store, CHECKOUT, "", HEAD_SHA) == ("pyproject.toml",)


def test_changed_paths_falls_back_when_the_recorded_commit_is_gone() -> None:
    """A rewritten history over-installs rather than under-installing: a
    redundant editable install costs seconds, a skipped one costs the hotfix."""
    store = FakeStore(outputs={f"{GIT} show": "pyproject.toml\n"})
    store.failures[f"{GIT} diff"] = "bad revision"
    assert hotfix.changed_paths(store, CHECKOUT, BASE_SHA, HEAD_SHA) == (
        "pyproject.toml",
    )


def test_pyvenv_cfg_gives_the_interpreter() -> None:
    assert hotfix.parse_pyvenv_cfg(PYVENV_CFG)["version"] == "3.12.7"


# -- drift -----------------------------------------------------------------


def test_parse_log_keeps_subjects_containing_anything_printable() -> None:
    text = f"{HEAD_SHA}\x1ffix: beam | trip, take 2\n{BASE_SHA}\x1fwip\n"
    commits = hotfix.parse_log(text)
    assert commits[0] == hotfix.HotfixCommit(HEAD_SHA, "fix: beam | trip, take 2")
    assert commits[1].subject == "wip"


def test_drift_counts_commits_the_image_does_not_have() -> None:
    store = FakeStore(
        outputs={
            f"{GIT} log": (f"{HEAD_SHA}\x1fsecond fix\n{BASE_SHA[:-1]}3\x1ffirst fix\n")
        }
    )
    commits = hotfix.drift_commits(store, CHECKOUT, BASE_SHA)
    assert len(commits) == 2
    assert commits[0].subject == "second fix"
    assert f"{BASE_SHA}..HEAD" in " ".join(store.calls[0])


def test_drift_with_no_base_commit_is_no_drift() -> None:
    assert hotfix.drift_commits(FakeStore(), CHECKOUT, "") == ()


def test_drift_names_the_repository_mismatch() -> None:
    store = FakeStore()
    store.failures[f"{GIT} log"] = "unknown revision"
    with pytest.raises(hotfix.HotfixError, match="different repository"):
        hotfix.drift_commits(store, CHECKOUT, BASE_SHA)


# -- interpreters ----------------------------------------------------------


def test_interpreter_warning_is_silent_when_they_match() -> None:
    assert hotfix.interpreter_warning("3.12.7", "Python 3.12.7") is None


def test_interpreter_warning_is_loud_across_a_feature_release() -> None:
    warning = hotfix.interpreter_warning("3.11.9", "Python 3.12.7")
    assert warning is not None
    assert "will not import" in warning


def test_interpreter_warning_is_a_note_across_a_micro_release() -> None:
    warning = hotfix.interpreter_warning("3.12.4", "Python 3.12.7")
    assert warning is not None
    assert "compiled extensions" in warning


def test_probe_reads_the_running_interpreter() -> None:
    runner = FakeRunner({"exec -c app": "Python 3.13.1\n"})
    probe = hotfix.probe_interpreter(kube(runner), "api-7f9-abc", "app")
    assert probe.ok
    assert probe.version == "3.13.1"


def test_probe_reports_an_interpreter_that_will_not_run() -> None:
    runner = FakeRunner()
    runner.failures["exec -c app"] = "exec: python3: not found"
    probe = hotfix.probe_interpreter(kube(runner), "api-7f9-abc", "app")
    assert not probe.ok
    assert "not found" in probe.detail


# -- health ----------------------------------------------------------------


def test_assess_active_when_the_image_has_not_moved() -> None:
    health, detail = hotfix.assess(a_manifest(ahead=2), current_digest=BASE_DIGEST)
    assert health is hotfix.HotfixHealth.ACTIVE
    assert "2 commit(s) ahead" in detail


def test_assess_flags_an_image_upgrade_under_the_mount() -> None:
    health, detail = hotfix.assess(a_manifest(), current_digest=NEW_DIGEST)
    assert health is hotfix.HotfixHealth.IMAGE_CHANGED
    assert "shadows" in detail


def test_assess_flags_a_stale_claim_after_consolidation() -> None:
    """The risk the brief calls out by name: the fix is in the image, and the
    claim is still shadowing it with an older copy."""
    manifest = a_manifest(consolidated_branch="patch/beamtime-14")
    health, detail = hotfix.assess(manifest, current_digest=NEW_DIGEST)
    assert health is hotfix.HotfixHealth.SUPERSEDED
    assert "retire" in detail


def test_assess_puts_a_broken_interpreter_above_everything() -> None:
    manifest = a_manifest(consolidated_branch="patch/beamtime-14")
    probe = hotfix.InterpreterProbe(ok=False, version=None, detail="not found")
    health, detail = hotfix.assess(manifest, current_digest=NEW_DIGEST, probe=probe)
    assert health is hotfix.HotfixHealth.INTERPRETER_MISMATCH
    assert "not found" in detail


def test_assess_warns_on_a_measured_interpreter_mismatch() -> None:
    probe = hotfix.InterpreterProbe(ok=True, version="3.13.1", detail="Python 3.13.1")
    health, detail = hotfix.assess(a_manifest(), current_digest=NEW_DIGEST, probe=probe)
    assert health is hotfix.HotfixHealth.INTERPRETER_MISMATCH
    assert "3.13" in detail


def test_assess_reports_lost_provenance() -> None:
    """A manifest that is present and will not parse."""
    health, _ = hotfix.assess(None, current_digest=BASE_DIGEST, unreadable=True)
    assert health is hotfix.HotfixHealth.UNREADABLE
    assert not health.ok


def test_assess_does_not_blame_a_manifest_that_was_never_written() -> None:
    """No manifest is a different answer from an unreadable one.

    A pod reaches the listing by being held, and a hold with no fix behind it is
    real - the supervisor relaunches without backoff and the probe is
    short-circuited. Reporting it as lost provenance would blame the wrong
    thing and send somebody looking for a fix that does not exist.
    """
    health, detail = hotfix.assess(None, current_digest=BASE_DIGEST)
    assert health is hotfix.HotfixHealth.NOT_HOTFIXED
    assert not health.ok
    assert "carries no hotfix" in detail


# -- targets and the multi-replica refusal ---------------------------------


def test_resolve_a_single_replica_deployment() -> None:
    runner = FakeRunner(
        {
            "get deployment api -o json": json.dumps(workload_json()),
            "get pods -l app=api -o json": json.dumps({"items": [pod_json()]}),
        }
    )
    target = hotfix.resolve_target(kube(runner), "deployment/api")
    assert target.pod.name == "api-7f9-abc"
    assert target.workload == "deployment/api"
    assert target.container == "app"
    assert target.image_digest == BASE_DIGEST


def test_multi_replica_deployment_is_refused() -> None:
    runner = FakeRunner(
        {"get deployment api -o json": json.dumps(workload_json(replicas=3))}
    )
    with pytest.raises(hotfix.MultiReplicaError) as caught:
        hotfix.resolve_target(kube(runner), "deployment/api")
    message = str(caught.value)
    assert "3 replicas" in message
    assert "ReadWriteOnce" in message
    assert "Scale to 1" in message


def test_multi_replica_is_refused_when_reached_through_the_pod() -> None:
    """The reference the user types must not decide whether the check runs."""
    runner = FakeRunner(
        {
            "get pod api-7f9-abc -o json": json.dumps(pod_json()),
            "get replicaset api-7f9 -o json": json.dumps(replicaset_json(replicas=4)),
        }
    )
    with pytest.raises(hotfix.MultiReplicaError, match="4 replicas"):
        hotfix.resolve_target(kube(runner), "pod/api-7f9-abc")


def test_a_pod_resolves_through_its_replicaset_to_the_deployment() -> None:
    runner = FakeRunner(
        {
            "get pod api-7f9-abc -o json": json.dumps(pod_json()),
            "get replicaset api-7f9 -o json": json.dumps(replicaset_json()),
            "get deployment api -o json": json.dumps(workload_json()),
        }
    )
    target = hotfix.resolve_target(kube(runner), "api-7f9-abc")
    assert target.workload == "deployment/api"


def test_an_unowned_pod_has_no_workload() -> None:
    runner = FakeRunner(
        {"get pod solo -o json": json.dumps(pod_json(name="solo", owner=None))}
    )
    target = hotfix.resolve_target(kube(runner), "pod/solo")
    assert target.workload is None


def test_other_kinds_are_refused() -> None:
    with pytest.raises(hotfix.HotfixError, match="not 'service'"):
        hotfix.resolve_target(kube(FakeRunner()), "service/api")


def test_replica_count_reads_the_spec() -> None:
    assert hotfix.replica_count(workload_json(replicas=2)) == 2


# -- the store's two deadlines ---------------------------------------------


def test_a_pod_read_keeps_the_bound_the_clone_is_exempt_from() -> None:
    """`read_text` and `exists` are questions, and questions keep #118's bound.

    Every git command Hotfix mode sends goes through `PodStore.run`, which is
    why that one carries `POD_WORK_TIMEOUT`: a `git clone` of somebody's
    application repo honestly takes minutes. A `cat` of the manifest does not,
    and routing it through the same method would let a wedged exec sit for
    fifteen minutes - which is the failure #118's bound exists to stop.
    """
    runner = FakeRunner()
    store = hotfix.PodStore(kube(runner), "api-7f9-abc", "podbench")

    store.read_text(hotfix.manifest_path(VENV))
    store.exists(CHECKOUT)
    store.write_text(hotfix.manifest_path(VENV), "{}")
    store.run(["git", "clone", "https://example.invalid/acme/api.git", CHECKOUT])

    assert runner.timeouts == [
        DEFAULT_CALL_TIMEOUT,
        DEFAULT_CALL_TIMEOUT,
        DEFAULT_CALL_TIMEOUT,
        hotfix.POD_WORK_TIMEOUT,
    ]


# -- --venv is read off the pod (#205 item 1) -------------------------------


def a_target(mount: str = "") -> hotfix.HotfixTarget:
    return hotfix.HotfixTarget(
        pod=hotfix.PodRef("demo", "api-7f9-abc"),
        container="app",
        image="ghcr.io/acme/api:1.4.0",
        image_digest=BASE_DIGEST,
        claim_mount=mount,
    )


def test_the_claim_mount_is_resolved_by_volume_name_not_by_path() -> None:
    """The mountPath is the answer being looked for, so it cannot also be the
    key. `podbench-app` is podbench's own volume name, which is what keeps this
    chart-agnostic where scanning a chart's keys would not be."""
    pod = pod_json(owner=None, claim=True)
    pod["spec"]["containers"][0]["volumeMounts"] = [
        {"name": model.HOTFIX_CLAIM_VOLUME, "mountPath": "/elsewhere"}
    ]
    runner = FakeRunner({"get pod api-7f9-abc -o json": json.dumps(pod)})

    target = hotfix.resolve_target(kube(runner), "pod/api-7f9-abc")

    assert target.claim_mount == "/elsewhere"


def test_venv_defaults_to_the_mount_the_pod_declares() -> None:
    assert hotfix.resolve_venv(a_target(model.HOTFIX_APP_PATH), None) == (
        model.HOTFIX_APP_PATH,
        None,
    )


def test_venv_defaults_to_the_convention_before_the_claim_exists() -> None:
    """`init` is run against a pod that has just been given the claim, but the
    same verbs are run against one that has not - and a default that guessed
    nothing would make the flag required again for the case it exists to drop."""
    assert hotfix.resolve_venv(a_target(), None) == (model.HOTFIX_APP_PATH, None)


def test_a_venv_that_disagrees_with_the_pod_is_refused() -> None:
    """The silent failure #205 item 1 names: any value other than the mountPath
    writes a manifest at a path `status` never scans, and a hotfixed pod
    invisible to `status` is the precise thing this mode exists to prevent."""
    with pytest.raises(hotfix.HotfixError) as refusal:
        hotfix.resolve_venv(a_target(model.HOTFIX_APP_PATH), "/opt/venv")

    assert "/opt/venv" in str(refusal.value)
    assert model.HOTFIX_APP_PATH in str(refusal.value)


def test_a_claim_mounted_elsewhere_is_honoured_and_said_out_loud() -> None:
    """The escape hatch stays open; what must not happen is that it is silent."""
    venv, note = hotfix.resolve_venv(a_target("/opt/venv"), "/opt/venv")

    assert venv == "/opt/venv"
    assert note is not None
    assert "hotfix status" in note


def test_a_trailing_slash_is_not_a_disagreement() -> None:
    assert hotfix.resolve_venv(a_target("/opt/venv"), "/opt/venv/")[0] == "/opt/venv"


# -- the base commit's provenance (#205 item 2) -----------------------------


REVISION = "3333333333333333333333333333333333333333"


def a_clone(**outputs: str) -> FakeStore:
    store = seeded_store()
    store.outputs.update(outputs)
    return store


def test_a_stated_base_commit_is_measured() -> None:
    sha, assumed, why = hotfix.base_commit_from(a_clone(), CHECKOUT, BASE_SHA, None)

    assert (sha, assumed) == (BASE_SHA, False)
    assert "as given" in why


def test_the_image_label_supplies_the_base_commit() -> None:
    """The number every drift figure is a difference against, taken from the
    thing that actually knows it rather than from a fresh clone's HEAD."""
    sha, assumed, why = hotfix.base_commit_from(
        a_clone(), CHECKOUT, None, {oci.REVISION_LABEL: REVISION}
    )

    assert (sha, assumed) == (REVISION, False)
    assert oci.REVISION_LABEL in why


def test_a_revision_the_clone_does_not_have_is_not_believed() -> None:
    """A label is a claim about *a* repository, and --repo may be a fork or a
    mirror. Believing it anyway makes `git log base..HEAD` fail later, which is
    a worse place to find out."""
    store = a_clone()
    store.failures[f"{GIT} cat-file -e {REVISION}"] = "not a commit"

    sha, assumed, why = hotfix.base_commit_from(
        store, CHECKOUT, None, {oci.REVISION_LABEL: REVISION}
    )

    assert (sha, assumed) == (HEAD_SHA, True)
    assert "not in this checkout" in why


def test_no_label_means_an_assumed_base_and_says_so() -> None:
    """The honest-uncertainty path, which is the point of the item rather than
    a fallback from it: without --ref the clone's HEAD is the default branch's
    tip, which is almost never what the image was built from."""
    sha, assumed, why = hotfix.base_commit_from(a_clone(), CHECKOUT, None, {})

    assert (sha, assumed) == (HEAD_SHA, True)
    assert "ASSUMED" in why


# -- annotation ------------------------------------------------------------


def deployment_target() -> hotfix.HotfixTarget:
    return hotfix.HotfixTarget(
        pod=hotfix.PodRef("demo", "api-7f9-abc"),
        container="app",
        image="ghcr.io/acme/api:1.4.0",
        image_digest=BASE_DIGEST,
        workload_kind="deployment",
        workload_name="api",
        replicas=1,
    )


def test_init_refuses_when_the_claim_is_not_mounted() -> None:
    """Nothing to seed onto is a deployment mistake, not a seeding one."""
    store = FakeStore()
    with pytest.raises(hotfix.HotfixError, match="the claim is not mounted"):
        hotfix.init(
            kube(FakeRunner()),
            store,
            deployment_target(),
            venv=VENV,
            repo="https://example.invalid/acme/api.git",
        )
    assert not store.ran("cp -a")


def test_init_refuses_an_unreachable_target_root_naming_the_ptrace_rung() -> None:
    """The seat cannot see the target's filesystem, and must not paper over it.

    Falling back to the seat's own /app would seed the claim with podbench's
    dependencies and none of the application's - a claim that looks seeded and
    runs the wrong code.
    """
    store = FakeStore(files={CHECKOUT: ""})
    # The rung is measured, not inferred: `ls` on the root is what fails when
    # the seat has no CAP_SYS_PTRACE.
    store.failures["ls /proc/1/root/"] = "ls: cannot open directory: Permission denied"

    with pytest.raises(hotfix.HotfixError) as caught:
        hotfix.init(
            kube(FakeRunner()),
            store,
            deployment_target(),
            venv=VENV,
            repo="https://example.invalid/acme/api.git",
        )

    message = str(caught.value)
    assert "ptrace rung" in message
    assert "podbench doctor" in message
    assert not store.ran("cp -a")


def test_a_missing_project_is_not_reported_as_a_ptrace_denial() -> None:
    """Issue #178, measured on bl47p-mo-ioc-01 on 2026-08-22: from that seat
    `/proc/1/root` listed cleanly and `/app` did not exist. The old message
    blamed ptrace and sent the user to `doctor`, which then reported the rung
    healthy - a contradiction with no next step."""
    store = FakeStore(files={CHECKOUT: ""})

    with pytest.raises(hotfix.HotfixError) as caught:
        hotfix.init(
            kube(FakeRunner()),
            store,
            deployment_target(),
            venv=VENV,
            repo="https://example.invalid/acme/api.git",
        )

    message = str(caught.value)
    assert "no project at /app" in message
    # The whole of the fix: neither of the two words that sent the user in a
    # circle appears, because neither is the problem.
    assert "ptrace" not in message
    assert "doctor" not in message
    # And it names the flags that express a different layout.
    assert "--image-project" in message
    assert "--image-interpreter" in message
    assert not store.ran("cp -a")


def test_the_root_is_listed_rather_than_tested_for_existence() -> None:
    """`test -e` follows the /proc/1/root symlink and answers for its target, so
    it says yes on a seat that cannot traverse it - the exact case this has to
    catch. Listing is what measures the rung."""
    store = FakeStore(files={CHECKOUT: ""})

    with pytest.raises(hotfix.HotfixError):
        hotfix.init(
            kube(FakeRunner()),
            store,
            deployment_target(),
            venv=VENV,
            repo="https://example.invalid/acme/api.git",
        )

    assert store.ran("ls /proc/1/root/")


def test_an_image_with_a_different_layout_is_expressible_without_a_code_change() -> (
    None
):
    """An epics-containers image has no /app: its venv is at /venv with a
    separate /python. That is a layout difference, and #178 is what happened
    when podbench could only express one layout."""
    store = FakeStore(files={CHECKOUT: "", "/proc/1/root/venv": ""})

    with pytest.raises(hotfix.HotfixError) as caught:
        hotfix.init(
            kube(FakeRunner()),
            store,
            deployment_target(),
            venv=VENV,
            repo="https://example.invalid/acme/api.git",
            image_project="/app",
        )
    assert "no project at /app" in str(caught.value)

    # The same claim, pointed at the layout the image actually has, gets past
    # the guard and copies from /proc/1/root/venv.
    store = FakeStore(files={CHECKOUT: "", "/proc/1/root/venv": ""})
    hotfix.init(
        kube(FakeRunner()),
        store,
        deployment_target(),
        venv=VENV,
        repo="https://example.invalid/acme/api.git",
        image_project="/venv",
        image_interpreter="/python",
        install=False,
    )

    assert store.ran("cp -a")
    assert any("/proc/1/root/venv" in " ".join(argv) for argv in store.calls)


def test_init_seeds_the_project_and_the_interpreter() -> None:
    """Both halves. The interpreter is the one that looks redundant.

    A rebuilt venv's console scripts carry an absolute shebang, so an
    interpreter left in the image is one the next restart takes away - silently,
    as the fix reverting.
    """
    store = FakeStore(
        files={CHECKOUT: "", PROJECT_SRC: "", "/proc/1/root/python": ""},
        outputs={f"{GIT} rev-parse HEAD": BASE_SHA},
    )
    _, actions = hotfix.init(
        kube(FakeRunner()),
        store,
        deployment_target(),
        venv=VENV,
        repo="https://example.invalid/acme/api.git",
        install=False,
    )
    assert store.ran(f"bash -c shopt -s dotglob; cp -a {PROJECT_SRC}/* {CHECKOUT}/")
    assert store.ran(f"cp -a /proc/1/root/python {model.HOTFIX_INTERPRETER_PATH}")
    assert any("seeded" in action for action in actions)


def test_init_clones_installs_and_records_the_base_commit() -> None:
    runner = FakeRunner()
    store = FakeStore(
        files={
            CHECKOUT: "",
            PROJECT_SRC: "",
            "/proc/1/root/python": "",
            f"{CHECKOUT}/.venv/pyvenv.cfg": PYVENV_CFG,
        },
        outputs={f"{GIT} rev-parse HEAD": BASE_SHA},
    )
    manifest, actions = hotfix.init(
        kube(runner),
        store,
        deployment_target(),
        venv=VENV,
        repo="https://example.invalid/acme/api.git",
        ref="v1.4.0",
    )

    assert store.ran(
        f"git clone --branch v1.4.0 https://example.invalid/acme/api.git {CHECKOUT}"
    )
    assert manifest.base_commit == BASE_SHA == manifest.commit
    assert manifest.ahead == 0
    assert manifest.interpreter == "3.12.7"
    assert manifest.base_image_digest == BASE_DIGEST
    assert store.read_text(hotfix.manifest_path(VENV)) is not None
    # The rebuild runs in the application container, not in the seat.
    assert runner.matching("exec -c app api-7f9-abc -- env UV_CACHE_DIR")
    # No annotation: provenance lives on the claim, where Argo self-heal cannot
    # strip it. The pod template is not touched at all any more.
    assert not runner.matching("patch deployment api")
    assert any("rebuilt the venv" in action for action in actions)


def test_init_takes_the_repo_and_the_base_from_the_image() -> None:
    """#205 item 2. Both are things the image states about itself, so neither
    is a thing to type into a six-flag command in an emergency - and the
    checkout the image ships is what says the labels are its own."""
    runner = FakeRunner()
    store = seeded_store()
    store.outputs[f"{GIT} remote get-url origin"] = "git@github.com:acme/api.git\n"

    manifest, _ = hotfix.init(
        kube(runner),
        store,
        deployment_target(),
        venv=model.HOTFIX_APP_PATH,
        image_labels={
            oci.SOURCE_LABEL: "https://github.com/acme/api",
            oci.REVISION_LABEL: BASE_SHA,
        },
        install=False,
    )

    assert manifest.repo == "git@github.com:acme/api.git"
    # The image's revision, not the clone's HEAD - which is what HEAD_SHA is
    # scripted to be, and is what this used to record.
    assert manifest.base_commit == BASE_SHA
    assert not manifest.base_commit_assumed


def test_an_inherited_label_never_becomes_a_measured_base() -> None:
    """OCI labels are inherited: measured 2026-08-23,
    ghcr.io/diamondlightsource/fastcs-example-debug:2025.10.1 advertises the
    *base image's* source and revision. Believing the pair would have measured
    the IOC's drift against another project's history and called it measured,
    because a clone made from the label's own repository contains the label's
    own revision."""
    runner = FakeRunner()
    store = seeded_store()
    store.outputs[f"{GIT} remote get-url origin"] = (
        "https://github.com/DiamondLightSource/fastcs-example\n"
    )

    manifest, actions = hotfix.init(
        kube(runner),
        store,
        deployment_target(),
        venv=model.HOTFIX_APP_PATH,
        image_labels={
            oci.SOURCE_LABEL: (
                "https://github.com/DiamondLightSource/ubuntu-devcontainer"
            ),
            oci.REVISION_LABEL: BASE_SHA,
        },
        install=False,
    )

    # The checkout's own origin wins: it is a measurement of the project on the
    # claim, and manifest.repo is where `consolidate` will push.
    assert manifest.repo == "https://github.com/DiamondLightSource/fastcs-example"
    assert manifest.base_commit == HEAD_SHA
    assert manifest.base_commit_assumed
    assert any("ubuntu-devcontainer" in action for action in actions)
    assert not store.ran(f"{GIT} cat-file -e {BASE_SHA}"), (
        "the guard cannot see this - the labels are not checked against a "
        "clone made from the same labels"
    )


def test_a_clone_made_from_the_label_corroborates_nothing() -> None:
    """With no checkout in the image there is no origin to ask, so the only
    thing naming the repository is the label itself and `_has_commit` runs
    against a clone of it. Honest uncertainty, not a measurement."""
    runner = FakeRunner()
    store = FakeStore(
        files={
            CHECKOUT: "",
            PROJECT_SRC: "",
            "/proc/1/root/python": "",
            f"{CHECKOUT}/.venv/pyvenv.cfg": PYVENV_CFG,
        },
        outputs={f"{GIT} rev-parse HEAD": HEAD_SHA},
    )

    manifest, _ = hotfix.init(
        kube(runner),
        store,
        deployment_target(),
        venv=model.HOTFIX_APP_PATH,
        image_labels={
            oci.SOURCE_LABEL: "https://github.com/acme/api",
            oci.REVISION_LABEL: BASE_SHA,
        },
        install=False,
    )

    assert store.ran(f"git clone https://github.com/acme/api {CHECKOUT}")
    assert manifest.repo == "https://github.com/acme/api"
    assert manifest.base_commit == HEAD_SHA
    assert manifest.base_commit_assumed


def test_init_refuses_a_missing_repo_before_it_copies_anything() -> None:
    """`main`'s except clause prints the error alone, so a refusal on the far
    side of a ~50s `cp -a` costs the wait and then discards the only record
    that the claim was written to at all."""
    store = FakeStore(files={CHECKOUT: "", PROJECT_SRC: "", "/proc/1/root/python": ""})

    with pytest.raises(hotfix.HotfixError, match="--repo"):
        hotfix.init(
            kube(FakeRunner()),
            store,
            deployment_target(),
            venv=model.HOTFIX_APP_PATH,
            install=False,
        )

    assert not store.ran("bash -c shopt -s dotglob"), "the seed must not have run"
    assert not store.ran("cp -a /proc/1/root/python")


def test_init_refuses_when_neither_the_flag_nor_the_label_names_a_repo() -> None:
    store = FakeStore(files={CHECKOUT: "", f"{CHECKOUT}/pyproject.toml": ""})

    with pytest.raises(hotfix.HotfixError, match="--repo"):
        hotfix.init(
            kube(FakeRunner()),
            store,
            deployment_target(),
            venv=model.HOTFIX_APP_PATH,
            install=False,
        )


def test_init_records_the_claim_venv_so_that_apply_need_not_be_told() -> None:
    """#209's first half: the flag was a cross-command contract that nothing
    recorded, so nothing downstream could honour it."""
    store = seeded_store()

    manifest, _ = hotfix.init(
        kube(FakeRunner()),
        store,
        deployment_target(),
        venv=model.HOTFIX_APP_PATH,
        repo="https://example.invalid/acme/api.git",
        claim_venv="env",
        install=False,
    )

    assert manifest.claim_venv == "env"


def test_init_is_idempotent_about_an_existing_checkout() -> None:
    runner = FakeRunner()
    store = seeded_store()
    _, actions = hotfix.init(
        kube(runner),
        store,
        deployment_target(),
        venv=VENV,
        repo="https://example.invalid/acme/api.git",
        install=False,
    )
    assert not store.ran("cp -a")
    assert any("already seeded" in action for action in actions)


def test_init_explains_a_failed_install() -> None:
    runner = FakeRunner()
    # only the rebuild fails; the supervisor probe must still succeed, or
    # init refuses earlier and this asserts the wrong refusal.
    runner.failures["exec -c app api-7f9-abc -- env UV_CACHE_DIR"] = (
        "no index reachable"
    )
    store = FakeStore(
        files={
            CHECKOUT: "",
            PROJECT_SRC: "",
            "/proc/1/root/python": "",
            f"{CHECKOUT}/.venv/pyvenv.cfg": PYVENV_CFG,
        },
        outputs={f"{GIT} rev-parse HEAD": BASE_SHA},
    )
    with pytest.raises(hotfix.HotfixError, match="pre-build the venv"):
        hotfix.init(
            kube(runner),
            store,
            deployment_target(),
            venv=VENV,
            repo="https://example.invalid/acme/api.git",
        )


# -- apply -----------------------------------------------------------------


def applied_store(dirty: bool = True, changed: str = "src/api/beam.py") -> FakeStore:
    store = seeded_store()
    store.files[hotfix.manifest_path(VENV)] = a_manifest().to_json()
    store.outputs[f"{GIT} status --porcelain"] = " M src/api/beam.py\n" if dirty else ""
    # The reinstall question is asked of the manifest's recorded commit..HEAD,
    # so that is the range the fake git answers for.
    store.outputs[f"{GIT} diff --name-only {BASE_SHA}..{HEAD_SHA}"] = f"{changed}\n"
    return store


def test_apply_commits_measures_drift_and_relaunches() -> None:
    runner = FakeRunner()
    store = applied_store()
    manifest, actions = hotfix.apply_hotfix(
        kube(runner),
        store,
        deployment_target(),
        venv=VENV,
        message="stop the beam tripping",
        author="Ada <ada@example.invalid>",
    )

    assert store.ran(f"{GIT} add -A")
    assert any("commit" in " ".join(call) for call in store.calls)
    assert manifest.commit == HEAD_SHA
    assert manifest.ahead == 1
    assert manifest.commits[0].subject == "make the beam behave"
    assert manifest.author == "Ada <ada@example.invalid>"

    # The pod template is never touched: provenance lives on the claim, where
    # Argo self-heal cannot strip it, and the relaunch happens inside the
    # container rather than by rolling the workload.
    assert not runner.matching("patch deployment api")
    assert not runner.matching("delete pod")
    assert (
        hotfix.HotfixManifest.from_json(store.files[hotfix.manifest_path(VENV)]).ahead
        == 1
    )
    assert any("without a restart" in action for action in actions)


def test_apply_skips_the_reinstall_when_only_code_changed() -> None:
    runner = FakeRunner()
    hotfix.apply_hotfix(
        kube(runner),
        applied_store(),
        deployment_target(),
        venv=VENV,
        message="fix",
    )
    assert not runner.matching("exec -c app api-7f9-abc -- env UV_CACHE_DIR")


def test_apply_reinstalls_when_packaging_metadata_changed() -> None:
    runner = FakeRunner()
    _, actions = hotfix.apply_hotfix(
        kube(runner),
        applied_store(changed="pyproject.toml"),
        deployment_target(),
        venv=VENV,
        message="new entry point",
    )
    assert runner.matching("exec -c app api-7f9-abc -- env UV_CACHE_DIR")
    assert any("rebuilt the venv" in action for action in actions)


def test_apply_reinstalls_after_a_hand_commit_that_touched_packaging() -> None:
    """A clean working tree is not evidence that nothing changed.

    Committing in the seat before running ``apply`` leaves the tree clean with
    HEAD already moved. Guarding the packaging check on dirtiness skipped it
    here, and the workload rolled with a ``.dist-info`` older than the commit —
    so a hotfix that adds an entry point looked applied and was not.
    """
    runner = FakeRunner()
    store = applied_store(dirty=False, changed="pyproject.toml")
    _, actions = hotfix.apply_hotfix(
        kube(runner),
        store,
        deployment_target(),
        venv=VENV,
        message="new entry point",
    )
    assert not store.ran(f"{GIT} add")
    assert runner.matching("exec -c app api-7f9-abc -- env UV_CACHE_DIR")
    assert any("rebuilt the venv" in action for action in actions)


def test_apply_rebuilds_into_the_venv_the_manifest_records() -> None:
    """#209's other half. `apply` has no --claim-venv and never should have, so
    before the manifest carried the name the reinstall defaulted to `.venv`
    while the supervisor's runtime switch looked in the directory `init` was
    told about - a packaging-change rebuild landing where nothing would ever run
    it, and no error anywhere."""
    runner = FakeRunner()
    store = applied_store(changed="pyproject.toml")
    store.files[hotfix.manifest_path(VENV)] = a_manifest(claim_venv="env").to_json()

    hotfix.apply_hotfix(
        kube(runner),
        store,
        deployment_target(),
        venv=VENV,
        message="new entry point",
    )

    rebuilt = runner.matching("exec -c app api-7f9-abc -- env UV_CACHE_DIR")
    assert rebuilt
    assert f"UV_PROJECT_ENVIRONMENT={CHECKOUT}/env" in " ".join(rebuilt[0])


def test_apply_rebuilds_into_uvs_own_default_without_saying_so() -> None:
    """The other side of the same switch: `install_argv` sets the variable only
    when the name is not uv's own, so a default claim must not carry it."""
    runner = FakeRunner()

    hotfix.apply_hotfix(
        kube(runner),
        applied_store(changed="pyproject.toml"),
        deployment_target(),
        venv=VENV,
        message="new entry point",
    )

    rebuilt = runner.matching("exec -c app api-7f9-abc -- env UV_CACHE_DIR")
    assert "UV_PROJECT_ENVIRONMENT" not in " ".join(rebuilt[0])


def test_apply_never_upgrades_an_unknown_base_into_a_measured_one() -> None:
    """`apply` rewrites the manifest at the current schema version. A v1
    manifest cannot say whether its base was measured, and rewriting it as v2
    with the field's default would turn "unknown" into "measured" - the one
    direction this field must never move in."""
    payload = json.loads(a_manifest().to_json())
    payload["version"] = 1
    del payload["baseCommitAssumed"]
    store = applied_store()
    store.files[hotfix.manifest_path(VENV)] = json.dumps(payload)

    manifest, _ = hotfix.apply_hotfix(
        kube(FakeRunner()), store, deployment_target(), venv=VENV, message="fix"
    )

    assert manifest.schema_version == hotfix.MANIFEST_VERSION
    assert manifest.base_commit_assumed


def test_apply_after_a_code_only_hand_commit_does_not_reinstall() -> None:
    """The other direction: the commit is the key, so a code-only one is cheap."""
    runner = FakeRunner()
    _, actions = hotfix.apply_hotfix(
        kube(runner),
        applied_store(dirty=False, changed="src/api/beam.py"),
        deployment_target(),
        venv=VENV,
        message="already committed by hand",
    )
    assert not runner.matching("exec -c app api-7f9-abc -- env UV_CACHE_DIR")
    assert any("still valid" in action for action in actions)


def test_apply_on_a_clean_tree_commits_nothing() -> None:
    store = applied_store(dirty=False)
    _, actions = hotfix.apply_hotfix(
        kube(FakeRunner()),
        store,
        deployment_target(),
        venv=VENV,
        message="nothing to see",
    )
    assert not store.ran(f"{GIT} add")
    assert any("nothing new to commit" in action for action in actions)


def test_apply_without_a_manifest_sends_you_to_init() -> None:
    with pytest.raises(hotfix.HotfixError, match="hotfix init"):
        hotfix.apply_hotfix(
            kube(FakeRunner()),
            seeded_store(),
            deployment_target(),
            venv=VENV,
            message="fix",
        )


def test_apply_relaunches_in_place_and_deletes_nothing() -> None:
    """A pod with no controller used to be refused. Now it needs no controller.

    The relaunch happens inside the container, so ownership stops mattering -
    which is what lets Hotfix mode work on a bare pod and, more to the point,
    stops it SIGKILLing the seat that shares the container's namespaces.
    """
    runner = FakeRunner()
    target = hotfix.HotfixTarget(
        pod=hotfix.PodRef("demo", "solo"),
        container="app",
        image="ghcr.io/acme/api:1.4.0",
        image_digest=BASE_DIGEST,
    )
    _, actions = hotfix.apply_hotfix(
        kube(runner), applied_store(), target, venv=VENV, message="fix"
    )
    assert not runner.matching("delete pod")
    assert any("without a restart" in action for action in actions)
    # hold, tree-kill, release - as one exec, so a dying seat cannot leave the
    # pod held with its probe short-circuited.
    assert runner.matching("exec -c app solo -- bash -c")


def test_the_relaunch_refuses_a_container_without_the_supervisor() -> None:
    """Nothing to relaunch means the kill would restart the container instead."""
    runner = FakeRunner()
    runner.failures["exec -c app"] = "no such file"
    with pytest.raises(hotfix.HotfixError, match="not running the podbench supervisor"):
        hotfix.init(
            kube(runner),
            seeded_store(),
            deployment_target(),
            venv=VENV,
            repo="https://example.invalid/acme/api.git",
        )


def test_apply_can_leave_the_process_alone() -> None:
    runner = FakeRunner()
    _, actions = hotfix.apply_hotfix(
        kube(runner),
        applied_store(),
        deployment_target(),
        venv=VENV,
        message="fix",
        bounce=False,
    )
    assert any("still has the old code" in action for action in actions)


# -- consolidate -----------------------------------------------------------


def test_consolidate_pushes_and_records_the_branch() -> None:
    runner = FakeRunner()
    store = applied_store()
    manifest, actions = hotfix.consolidate(
        kube(runner),
        store,
        deployment_target(),
        venv=VENV,
        branch="patch/beamtime-14",
    )
    assert store.ran(f"{GIT} push origin HEAD:refs/heads/patch/beamtime-14")
    assert manifest.consolidated_branch == "patch/beamtime-14"
    reread = hotfix.HotfixManifest.from_json(store.files[hotfix.manifest_path(VENV)])
    assert reread.consolidated_branch == "patch/beamtime-14"
    checklist = "\n".join(actions)
    assert "gh pr create" in checklist
    # Both routes named, because a site is on one or the other and the claim
    # outlives the boolean either way (#190).
    assert f"{hotfix.SUBCHART_VALUES_KEY}.enabled=false" in checklist
    assert "hotfixProject.enabled=false" in checklist
    assert "delete the claim" in checklist


def test_consolidate_dry_run_pushes_nothing() -> None:
    store = applied_store()
    _, actions = hotfix.consolidate(
        kube(FakeRunner()),
        store,
        deployment_target(),
        venv=VENV,
        branch="patch/beamtime-14",
        push=False,
    )
    assert not store.ran(f"{GIT} push")
    assert any("would push" in action for action in actions)


def test_consolidate_refuses_when_there_is_no_drift() -> None:
    store = applied_store()
    store.outputs[f"{GIT} log"] = ""
    with pytest.raises(hotfix.HotfixError, match="no hotfix to consolidate"):
        hotfix.consolidate(
            kube(FakeRunner()),
            store,
            deployment_target(),
            venv=VENV,
            branch="patch/beamtime-14",
        )


# -- status ----------------------------------------------------------------


def hotfixed_pod(
    manifest: hotfix.HotfixManifest,
    *,
    name: str = "api-7f9-abc",
    digest: str = BASE_DIGEST,
) -> dict[str, Any]:
    return pod_json(name=name, digest=digest, claim=True)


def state_exec(
    manifest: hotfix.HotfixManifest | None = None,
    *,
    hold: str = "",
    now: int = 1000,
) -> str:
    """What `read_pod_state`'s single exec prints back."""
    sep = hotfix.STATE_SEPARATOR
    body = manifest.to_json() if manifest is not None else ""
    return f"{body}\n{sep}\n{hold}\n{sep}\n{now}\n"


def test_status_says_when_the_drift_is_counted_from_an_assumed_base() -> None:
    """A count is a difference against the base, so an unmeasured base makes it
    a guess - and it is qualified in the column the eye lands on rather than
    printed as though it were measured."""
    row = hotfix.HotfixRow(
        pod=hotfix.PodRef("demo", "api-7f9-abc"),
        manifest=a_manifest(ahead=2, base_commit_assumed=True),
        health=hotfix.HotfixHealth.ACTIVE,
        detail="",
    )

    assert "+2 commit(s) from an assumed base" in hotfix.format_status([row])
    measured = replace(row, manifest=a_manifest(ahead=2))
    assert "assumed" not in hotfix.format_status([measured])


def test_status_ignores_pods_without_a_hotfix() -> None:
    runner = FakeRunner({"get pods -o json": json.dumps({"items": [pod_json()]})})
    assert hotfix.status_rows(kube(runner)) == []
    assert hotfix.format_status([]) == "no hotfixed pods in this namespace"


def test_status_reports_drift_for_a_healthy_hotfix() -> None:
    manifest = a_manifest(
        ahead=2, commits=(hotfix.HotfixCommit(HEAD_SHA, "stop the beam tripping"),)
    )
    runner = FakeRunner(
        {
            "get pods -o json": json.dumps({"items": [hotfixed_pod(manifest)]}),
            "exec -c app": state_exec(manifest),
        }
    )
    rows = hotfix.status_rows(kube(runner))
    assert len(rows) == 1
    assert rows[0].health is hotfix.HotfixHealth.ACTIVE
    report = hotfix.format_status(rows)
    assert "+2 commit(s)" in report
    assert "stop the beam tripping" in report
    assert "and 1 more" in report
    # One exec, for the state read - and only against a pod that carries a
    # claim. The interpreter probe is not run, because nothing moved.
    assert not runner.matching("exec -c app api-7f9-abc -- python")


def test_status_probes_the_interpreter_only_when_the_image_moved() -> None:
    manifest = a_manifest(ahead=1)
    runner = FakeRunner(
        {
            "get pods -o json": json.dumps(
                {"items": [hotfixed_pod(manifest, digest=NEW_DIGEST)]}
            ),
            # Two different execs now: the state read, and the probe on top.
            "exec -c app api-7f9-abc -- sh -c": state_exec(manifest),
            "exec -c app api-7f9-abc -- python": "Python 3.13.1\n",
        }
    )
    rows = hotfix.status_rows(kube(runner))
    assert runner.matching("exec -c app api-7f9-abc -- python")
    assert rows[0].health is hotfix.HotfixHealth.INTERPRETER_MISMATCH
    assert "will not import" in rows[0].detail
    assert "!" in hotfix.format_status(rows)


def test_status_does_not_probe_the_interpreter_when_told_not_to() -> None:
    manifest = a_manifest(ahead=1)
    runner = FakeRunner(
        {
            "get pods -o json": json.dumps(
                {"items": [hotfixed_pod(manifest, digest=NEW_DIGEST)]}
            ),
            "exec -c app": state_exec(manifest),
        }
    )
    rows = hotfix.status_rows(kube(runner), probe=False)
    # It still reads the claim - that is where the manifest is now - but it does
    # not run the interpreter probe on top.
    assert not runner.matching("exec -c app api-7f9-abc -- python")
    assert rows[0].health is hotfix.HotfixHealth.IMAGE_CHANGED


def test_status_notes_a_manifest_from_an_older_podbench() -> None:
    older = json.dumps({"commit": HEAD_SHA, "baseCommit": BASE_SHA, "container": "app"})
    sep = hotfix.STATE_SEPARATOR
    runner = FakeRunner(
        {
            "get pods -o json": json.dumps({"items": [pod_json(claim=True)]}),
            "exec -c app": f"{older}\n{sep}\n\n{sep}\n1000\n",
        }
    )
    rows = hotfix.status_rows(kube(runner))
    assert "older podbench" in "\n".join(rows[0].notes)


def test_status_reports_a_held_pod_that_was_never_hotfixed() -> None:
    """The case an annotation-keyed listing could not see at all.

    A hold with no fix behind it is a pod whose liveness probe is
    short-circuited and whose supervisor spins without backoff. Nothing else
    will notice it.
    """
    sep = hotfix.STATE_SEPARATOR
    runner = FakeRunner(
        {
            "get pods -o json": json.dumps({"items": [pod_json(claim=True)]}),
            "exec -c app": f"\n{sep}\n1600\n{sep}\n1000\n",
        }
    )
    rows = hotfix.status_rows(kube(runner))
    assert len(rows) == 1
    assert rows[0].manifest is None
    assert rows[0].hold == hotfix.Hold(deadline=1600, now=1000)
    assert not rows[0].ok
    assert "held 600s left" in hotfix.format_status(rows)


def test_a_hold_past_its_deadline_says_so() -> None:
    sep = hotfix.STATE_SEPARATOR
    runner = FakeRunner(
        {
            "get pods -o json": json.dumps({"items": [pod_json(claim=True)]}),
            "exec -c app": f"\n{sep}\n900\n{sep}\n1000\n",
        }
    )
    rows = hotfix.status_rows(kube(runner))
    assert rows[0].hold is not None and rows[0].hold.expired
    assert "held EXPIRED" in hotfix.format_status(rows)


def test_a_pod_with_a_claim_but_no_state_is_not_listed() -> None:
    """A deployed-but-unused claim is not a hotfix, and must not read as one."""
    sep = hotfix.STATE_SEPARATOR
    runner = FakeRunner(
        {
            "get pods -o json": json.dumps({"items": [pod_json(claim=True)]}),
            "exec -c app": f"\n{sep}\n\n{sep}\n1000\n",
        }
    )
    assert hotfix.status_rows(kube(runner)) == []


def test_status_reports_a_pod_whose_provenance_is_gone() -> None:
    sep = hotfix.STATE_SEPARATOR
    runner = FakeRunner(
        {
            "get pods -o json": json.dumps({"items": [pod_json(claim=True)]}),
            "exec -c app": f"{{not json\n{sep}\n1600\n{sep}\n1000\n",
        }
    )
    rows = hotfix.status_rows(kube(runner))
    assert rows[0].health is hotfix.HotfixHealth.UNREADABLE
    assert "provenance" in hotfix.format_status(rows)


# -- helm values -----------------------------------------------------------


ENTRY = "myapp serve --config /etc/myapp.yaml"


def parsed_snippet(**kwargs: Any) -> dict[str, Any]:
    """The snippet as data. It is pasted into a values file, so it has to parse."""
    loaded: object = yaml.safe_load(hotfix.values_snippet("api", ENTRY, **kwargs))
    assert isinstance(loaded, dict)
    return cast(dict[str, Any], loaded)


def volume_names(values: Mapping[str, Any], key: str) -> list[str]:
    return [str(entry["name"]) for entry in cast(list[Any], values.get(key, []))]


def test_values_snippet_mounts_the_claim_beside_the_project_not_over_it() -> None:
    """Beside is the whole design, so it is asserted rather than assumed.

    Mounting over the venv hid the image's own copy, which is what forced the
    initContainer at a staging path that ``ioc-instance`` cannot express.
    """
    values = parsed_snippet()
    mounts = {
        str(m["name"]): str(m["mountPath"])
        for m in cast(list[Any], values["volumeMounts"])
    }
    assert mounts[model.HOTFIX_CLAIM_VOLUME] == model.HOTFIX_APP_PATH
    assert model.HOTFIX_APP_PATH.startswith("/podbench/")


def test_values_snippet_emits_no_init_container_and_no_identity() -> None:
    """Both were casualties of moving beside the application.

    The initContainer existed only to reach a venv that was about to be hidden,
    and the identity ConfigMap serves a seat that is an ordinary container -
    which this mode's seat is not.
    """
    values = parsed_snippet()
    assert "initContainers" not in values
    assert "seatIdentity" not in values
    assert model.SEAT_IDENTITY_VOLUME not in hotfix.values_snippet("api", ENTRY)


def test_values_snippet_claim_name_no_longer_says_venv() -> None:
    values = parsed_snippet()
    claim = cast(list[Any], values["volumes"])[0]["persistentVolumeClaim"]["claimName"]
    assert claim == hotfix.hotfix_claim("api") == "api-podbench-project"
    assert not claim.endswith("-venv")


def test_values_snippet_declares_the_seats_home_but_does_not_mount_it() -> None:
    """Declaring is both necessary and sufficient.

    An ephemeral container may mount any volume the pod already has, and pod
    volumes are immutable after creation. Mounting it into the application as
    well would change its filesystem for nothing, so it reads as an omission
    and is asserted.
    """
    values = parsed_snippet()
    assert model.SEAT_HOME_VOLUME in volume_names(values, "volumes")
    assert model.SEAT_HOME_VOLUME not in volume_names(values, "volumeMounts")


def test_values_snippet_wraps_the_entrypoint_in_the_supervisor() -> None:
    values = parsed_snippet()
    assert values["command"] == ["bash", "-c"]
    args = "\n".join(cast(list[Any], values["args"]))
    assert ENTRY in args
    assert model.HOTFIX_HOLD_PATH in args
    assert model.HOTFIX_CHILD_PID_PATH in args


def test_the_supervisor_reaps_the_tree_not_the_pid() -> None:
    """S7: a target that allocates a pty escapes a signal aimed at the pid.

    Its real process lands in its own session, survives, and keeps the port -
    so the relaunch comes up deaf and the pod goes on serving the old code with
    ``restartCount`` still 0. Nothing about that is visible without this line.
    """
    args = "\n".join(cast(list[Any], parsed_snippet()["args"]))
    assert 'kill -TERM -"$child"' in args


def test_the_supervisor_is_fail_fast_without_a_hold_file() -> None:
    """The production case. A deployment carrying this restarts as it always did."""
    args = "\n".join(cast(list[Any], parsed_snippet()["args"]))
    assert f"[ -e {model.HOTFIX_HOLD_PATH} ] || exit $rc" in args


def test_the_runtime_switch_prefers_the_claim_only_when_it_is_seeded() -> None:
    args = "\n".join(cast(list[Any], parsed_snippet()["args"]))
    assert f"if [ -x {model.HOTFIX_APP_PATH}/.venv/bin/python ]; then" in args


def test_the_liveness_probe_is_wrapped_to_honour_the_hold() -> None:
    """Unwrapped, the kubelet restarts a held pod out from under the hold.

    Measured in S7 at 122s against 344s with the wrapper.
    """
    values = parsed_snippet(liveness_exec=["/bin/bash", "/epics/ioc/liveness.sh"])
    probe = cast(list[Any], values["livenessProbe"]["exec"]["command"])
    assert probe[:2] == ["bash", "-c"]
    assert probe[2].startswith(f"if [ -e {model.HOTFIX_HOLD_PATH} ]; then exit 0; fi;")
    assert "/epics/ioc/liveness.sh" in probe[2]


def test_no_probe_is_emitted_for_a_target_that_has_none() -> None:
    """The canonical hotfix target declares no probe, and 7 of 18 do."""
    assert "livenessProbe" not in parsed_snippet()


def test_a_whole_probe_carries_its_timings_through() -> None:
    """A chart renders a supplied probe wholesale, so an omitted timing is lost.

    Measured on p47-beamline 2026-08-22: ``ioc-instance``'s 120s/30s live on the
    ``livenessExecutable`` branch that supplying ``livenessProbe`` takes instead
    of, so a wrapped probe carrying only ``exec`` moved a compiled IOC to the
    Kubernetes defaults 0s/10s - probed before it had reached its hardware.
    """
    values = parsed_snippet(
        liveness_probe={
            "exec": {"command": ["/bin/bash", "/epics/ioc/liveness.sh"]},
            "initialDelaySeconds": 120,
            "periodSeconds": 30,
            "failureThreshold": 3,
        }
    )
    probe = cast(dict[str, Any], values["livenessProbe"])
    assert probe["initialDelaySeconds"] == 120
    assert probe["periodSeconds"] == 30
    assert probe["failureThreshold"] == 3
    # and the command is still the wrapped one, not the original
    command = cast(list[Any], probe["exec"]["command"])
    held = f"if [ -e {model.HOTFIX_HOLD_PATH} ]; then exit 0; fi;"
    assert command[2].startswith(held)
    assert "/epics/ioc/liveness.sh" in command[2]


def test_a_bare_liveness_command_warns_that_it_carries_no_timings() -> None:
    """--liveness cannot carry timings, so it must not look like it did."""
    snippet = hotfix.values_snippet(
        "api", ENTRY, liveness_exec=["/bin/bash", "/epics/ioc/liveness.sh"]
    )
    assert "WARNING" in snippet
    assert "periodSeconds 10" in snippet


def test_probe_timings_keeps_kubernetes_field_order() -> None:
    """Round-tripping somebody's probe should not reshuffle it."""
    lines = hotfix.probe_timings(
        {"periodSeconds": 30, "initialDelaySeconds": 120, "timeoutSeconds": 1}
    )
    assert lines == [
        "  initialDelaySeconds: 120",
        "  periodSeconds: 30",
        "  timeoutSeconds: 1",
    ]


def test_a_non_exec_probe_has_no_command_to_wrap() -> None:
    """httpGet answers from the application, which is what is down under a hold."""
    assert hotfix.probe_exec_command({"httpGet": {"path": "/healthz"}}) == []


def test_values_snippet_key_names_are_the_charts_business() -> None:
    """ioc-instance nests all five under ``global:``."""
    snippet = hotfix.values_snippet(
        "api", ENTRY, volumes_key="  volumes", mounts_key="  volumeMounts"
    )
    assert "\n  volumes:" in snippet
    assert "\n  volumeMounts:" in snippet


def test_values_snippet_sets_fsgroup_to_the_apps_gid() -> None:
    """Without it the claim is present and unwritable, which is worse than absent."""
    values = parsed_snippet(gid="65532")
    assert values["podSecurityContext"]["fsGroup"] == 65532


def test_values_snippet_gid_defaults_to_a_placeholder_not_a_plausible_number() -> None:
    """A snippet pasted unread must fail at install time, not at 3am."""
    assert not isinstance(parsed_snippet()["podSecurityContext"]["fsGroup"], int)


# -- CLI -------------------------------------------------------------------


def test_values_needs_the_two_things_it_cannot_guess(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert hotfix.main(["hotfix", "values"]) == 2
    assert "--app" in capsys.readouterr().err


# -- --from-pod: reading the target is the default (#176 follow-on) ---------

MO_IOC_PROBE: dict[str, Any] = {
    "exec": {"command": ["/bin/bash", "/epics/ioc/liveness.sh"]},
    "initialDelaySeconds": 120,
    "periodSeconds": 30,
    "failureThreshold": 3,
    "timeoutSeconds": 1,
}


def target_pod(
    *,
    name: str = "api-7f9-abc",
    command: list[str] | None = None,
    args: list[str] | None = None,
    probe: dict[str, Any] | None = None,
    gid: int | None = 37000,
    containers: list[str] | None = None,
) -> dict[str, Any]:
    """A pod as `--from-pod` meets it, with the three things it reads."""
    container: dict[str, Any] = {"name": "app", "image": "ghcr.io/acme/api:1.4.0"}
    if command is not None:
        container["command"] = command
    if args is not None:
        container["args"] = args
    if probe is not None:
        container["livenessProbe"] = probe
    spec: dict[str, Any] = {"containers": [container]}
    for extra in containers or []:
        spec["containers"].append({"name": extra, "image": "busybox"})
    if gid is not None:
        spec["securityContext"] = {"runAsUser": 1000, "runAsGroup": gid}
    return {"metadata": {"name": name}, "spec": spec, "status": {"phase": "Running"}}


def test_from_pod_needs_no_hand_editing(capsys: pytest.CaptureFixture[str]) -> None:
    """The whole point. `hotfix values` used to take the entrypoint, the probe
    and the gid by hand, and #176 is what a hand-supplied probe cost: a chart
    renders it wholesale, so an omitted timing silently became the Kubernetes
    default."""
    runner = FakeRunner(
        {
            "get pod api-7f9-abc -o json": json.dumps(
                target_pod(command=["python", "-m", "app"], probe=MO_IOC_PROBE)
            )
        }
    )
    code = hotfix.main(
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "-n",
            "demo",
            "--from-pod",
            "api-7f9-abc",
        ],
        runner=runner,
    )

    assert code == 0
    out = capsys.readouterr().out
    values = yaml.safe_load(out.split("# 2.", 1)[1].split("\n", 1)[1])
    # The entrypoint, shell-quoted back into the string the supervisor execs.
    assert "exec python -m app" in values["args"][0]
    # The gid, off the pod, with no placeholder left behind.
    assert values["podSecurityContext"]["fsGroup"] == 37000
    # And the timings, carried without being asked for. This is the end-to-end
    # assertion for #176: nobody typed 120 or 30.
    assert values["livenessProbe"]["initialDelaySeconds"] == 120
    assert values["livenessProbe"]["periodSeconds"] == 30
    assert values["livenessProbe"]["failureThreshold"] == 3
    assert values["livenessProbe"]["timeoutSeconds"] == 1
    # The handler is podbench's, wrapping the target's own command.
    assert "/epics/ioc/liveness.sh" in values["livenessProbe"]["exec"]["command"][-1]
    assert model.HOTFIX_HOLD_PATH in values["livenessProbe"]["exec"]["command"][-1]


def test_a_target_with_no_liveness_probe_is_not_an_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The canonical fastcs target declares none, and reporting that as a
    failure would refuse to emit values for the one pod this mode was built
    against."""
    runner = FakeRunner(
        {
            "get pod api-7f9-abc -o json": json.dumps(
                target_pod(command=["python", "-m", "app"])
            )
        }
    )
    code = hotfix.main(
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "-n",
            "demo",
            "--from-pod",
            "api-7f9-abc",
        ],
        runner=runner,
    )

    assert code == 0
    captured = capsys.readouterr()
    assert "livenessProbe:" not in captured.out
    assert captured.err == ""


def test_a_non_exec_probe_is_emitted_around_and_said_out_loud(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Not an error - the values are still right - and not silence either: an
    absent probe block looks identical to the fastcs case, and the difference is
    whether a held pod survives."""
    runner = FakeRunner(
        {
            "get pod api-7f9-abc -o json": json.dumps(
                target_pod(
                    command=["python", "-m", "app"],
                    probe={"httpGet": {"path": "/healthz", "port": 8080}},
                )
            )
        }
    )
    code = hotfix.main(
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "-n",
            "demo",
            "--from-pod",
            "api-7f9-abc",
        ],
        runner=runner,
    )

    assert code == 0
    captured = capsys.readouterr()
    assert "livenessProbe:" not in captured.out
    assert "httpGet" in captured.err
    assert "restart the pod out from under the seat" in captured.err


def test_volumes_the_target_already_has_are_named_as_a_merge(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Found by diffing --from-pod's output against the hand-written values for
    bl47p-mo-ioc-01: the emitted `volumes:` carried podbench's two and nothing
    else, so pasting it verbatim would have dropped that IOC's dev-shm and left
    it without /dev/shm. Podbench cannot merge them itself - from a live pod a
    chart-generated volume and one the service declared are indistinguishable -
    so it names them and says which key is at risk.

    The key at risk is the *values file's*, not the chart's. Measured against
    ioc-instance 5.6.1: bl47p-mo-ioc-01's values declare dev-shm alone and its
    pod carries six volumes, so a chart appends what the values give it."""
    pod = target_pod(command=["python", "-m", "app"])
    pod["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "dev-shm", "mountPath": "/dev/shm"},
        {"name": model.HOTFIX_CLAIM_VOLUME, "mountPath": model.HOTFIX_APP_PATH},
    ]
    runner = FakeRunner({"get pod api-7f9-abc -o json": json.dumps(pod)})

    code = hotfix.main(
        # fmt: off
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "-n",
            "demo",
            "--from-pod",
            "api-7f9-abc",
        ],
        # fmt: on
        runner=runner,
    )

    assert code == 0
    err = capsys.readouterr().err
    assert "dev-shm" in err
    # The advice is to merge into the values file, and explicitly *not* to copy
    # the chart's own volumes in - which is what "replaces the ones your chart
    # renders" would have sent a reader off to do.
    assert "merge into it rather than replacing it" in err
    assert "must not be copied in here" in err
    # podbench's own two are not reported back to the user as theirs.
    assert model.HOTFIX_CLAIM_VOLUME not in err


def test_values_answers_the_question_the_mount_warning_asks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Found on p47-beamline the first time the whole workflow was run.

    `EXISTING_MOUNTS_WARNING` exists because a pod cannot say which of its
    volumes the service asked for. Under `--values` the values file has just
    said, and the merge has acted on it - so the warning tells the user to go
    and do by hand what has already been done, and names volumes the merge
    deliberately did not copy because the chart renders them.
    """
    pod = target_pod(command=["python", "-m", "app"])
    pod["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "dev-shm", "mountPath": "/dev/shm"},
        {"name": model.HOTFIX_CLAIM_VOLUME, "mountPath": model.HOTFIX_APP_PATH},
    ]
    child = tmp_path / "values.yaml"
    child.write_text(CHILD)
    runner = FakeRunner({"get pod api-7f9-abc -o json": json.dumps(pod)})

    code = hotfix.main(
        # fmt: off
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "-n",
            "demo",
            "--from-pod",
            "api-7f9-abc",
            "--values",
            str(child),
        ],
        # fmt: on
        runner=runner,
    )

    assert code == 0
    err = capsys.readouterr().err
    assert "merge into it rather than replacing it" not in err
    # The merge's own notes still say what it did.
    assert "went under" in err


def test_a_target_carrying_only_podbenchs_volumes_is_not_warned_about_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    pod = target_pod(command=["python", "-m", "app"])
    pod["spec"]["containers"][0]["volumeMounts"] = [
        {"name": model.HOTFIX_CLAIM_VOLUME, "mountPath": model.HOTFIX_APP_PATH}
    ]
    runner = FakeRunner({"get pod api-7f9-abc -o json": json.dumps(pod)})

    code = hotfix.main(
        # fmt: off
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "-n",
            "demo",
            "--from-pod",
            "api-7f9-abc",
        ],
        # fmt: on
        runner=runner,
    )

    assert code == 0
    assert capsys.readouterr().err == ""


def test_from_pod_unwraps_a_target_that_already_carries_the_layout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Re-emitting the values for an already-hotfixed pod is a normal thing to
    do, and a supervisor nested in a supervisor would hold on a file the inner
    one never sees."""
    runner = FakeRunner(
        {
            "get pod api-7f9-abc -o json": json.dumps(
                target_pod(
                    command=["bash", "-c"],
                    args=[hotfix.hold_loop_args("python -m app")],
                )
            )
        }
    )
    code = hotfix.main(
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "-n",
            "demo",
            "--from-pod",
            "api-7f9-abc",
        ],
        runner=runner,
    )

    assert code == 0
    out = capsys.readouterr().out
    assert out.count("while :; do") == 1
    assert "exec python -m app" in out


def test_an_unknown_gid_keeps_the_placeholder_rather_than_becoming_root(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A hardened workload routinely states runAsUser and no runAsGroup. An
    fsGroup of 0 on a claim the application cannot write is worse than absent:
    everything starts, then fails."""
    runner = FakeRunner(
        {
            "get pod api-7f9-abc -o json": json.dumps(
                target_pod(command=["python", "-m", "app"], gid=None)
            )
        }
    )
    code = hotfix.main(
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "-n",
            "demo",
            "--from-pod",
            "api-7f9-abc",
        ],
        runner=runner,
    )

    assert code == 0
    assert hotfix.DEFAULT_GID_PLACEHOLDER in capsys.readouterr().out


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            "error: no configuration has been provided, try setting "
            "KUBERNETES_MASTER environment variable",
            "no configuration has been provided",
        ),
        (
            'Error from server (NotFound): pods "api-7f9-abc" not found',
            "not found",
        ),
        (
            'Error from server (Forbidden): pods is forbidden: User "sa" cannot '
            'list resource "pods"',
            "Forbidden",
        ),
    ],
    ids=["no-context", "absent-pod", "forbidden"],
)
def test_every_cluster_failure_names_the_escape_and_its_cost(
    capsys: pytest.CaptureFixture[str], failure: str, expected: str
) -> None:
    """Making the cluster read the default means each of these lands on somebody
    who did not ask for a cluster. kubectl tells the three apart only in the
    text of its own message, so podbench relays that verbatim - and every one of
    them has to name the way out and what taking it costs."""
    runner = FakeRunner()
    runner.failures["get pod api-7f9-abc -o json"] = failure

    code = hotfix.main(
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "-n",
            "demo",
            "--from-pod",
            "api-7f9-abc",
        ],
        runner=runner,
    )

    assert code == 2
    err = capsys.readouterr().err
    assert expected in err, err
    assert "--no-from-pod" in err
    assert "#176" in err


def test_a_target_that_is_not_a_pod_is_refused_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--from-pod` is a pod and only a pod - the entrypoint, the probe and the
    gid are read off a running container and a Deployment has none. LauncherError
    is not one of the three `main` folds into exit 2, so this used to come out as
    a traceback rather than an answer."""
    runner = FakeRunner()

    code = hotfix.main(
        # fmt: off
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "-n",
            "demo",
            "--from-pod",
            "deployment/api",
        ],
        # fmt: on
        runner=runner,
    )

    assert code == 2
    err = capsys.readouterr().err
    assert "works on pods" in err
    assert "--no-from-pod" in err
    assert runner.calls == []


def test_a_container_that_is_not_in_the_pod_names_the_escape_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeRunner(
        {
            "get pod api-7f9-abc -o json": json.dumps(
                target_pod(command=["python", "-m", "app"], containers=["sidecar"])
            )
        }
    )

    code = hotfix.main(
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "-n",
            "demo",
            "--from-pod",
            "api-7f9-abc",
            "--container",
            "nope",
        ],
        runner=runner,
    )

    assert code == 2
    err = capsys.readouterr().err
    assert "'nope' not in pod" in err
    assert "--no-from-pod" in err


def test_an_entrypoint_that_lives_in_the_image_says_which_flag_supplies_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A container declaring neither command nor args keeps its entrypoint in
    the image, which is not in the pod spec at all - nothing podbench reads from
    the cluster will find it. The answer is --entrypoint, not --no-from-pod, and
    the message has to offer the cheaper one first."""
    runner = FakeRunner({"get pod api-7f9-abc -o json": json.dumps(target_pod())})

    code = hotfix.main(
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "-n",
            "demo",
            "--from-pod",
            "api-7f9-abc",
        ],
        runner=runner,
    )

    assert code == 2
    err = capsys.readouterr().err
    assert "ENTRYPOINT" in err
    assert "--entrypoint" in err
    assert "--no-from-pod" in err


def test_a_flag_the_user_passed_beats_the_pod(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Which is what makes --entrypoint a usable answer to the case above
    without giving up the gid and the probe as well."""
    runner = FakeRunner(
        {"get pod api-7f9-abc -o json": json.dumps(target_pod(probe=MO_IOC_PROBE))}
    )

    code = hotfix.main(
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "-n",
            "demo",
            "--from-pod",
            "api-7f9-abc",
            "--entrypoint",
            "myapp serve",
        ],
        runner=runner,
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "exec myapp serve" in out
    # The other two still came off the pod.
    assert "fsGroup: 37000" in out
    assert "initialDelaySeconds: 120" in out


def test_reading_the_pod_is_the_default_and_says_so_when_none_is_named(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeRunner()

    code = hotfix.main(
        ["hotfix", "values", "--app", "api", "-n", "demo"], runner=runner
    )

    assert code == 2
    err = capsys.readouterr().err
    assert "--from-pod POD" in err
    assert "--no-from-pod" in err
    assert runner.calls == [], "nothing should have been asked of the cluster"


def test_no_from_pod_asks_the_cluster_nothing_at_all() -> None:
    """The escape has to work on a machine with no cluster to reach, which means
    it must not so much as construct a kubectl call."""
    runner = FakeRunner()

    code = hotfix.main(
        [
            "hotfix",
            "values",
            "--no-from-pod",
            "--app",
            "api",
            "--entrypoint",
            ENTRY,
        ],
        runner=runner,
    )

    assert code == 0
    assert runner.calls == []


# -- --values: the whole file, not fragments to merge (#192) ---------------


CHILD = """\
# yaml-language-server: $schema=../../.helm-shared/values.schema.json

ioc-instance:
  # the image is the one thing this service really owns
  image: ghcr.io/example/fastcs:2025.10.1
  livenessExecutable: ""
"""

PARENT = """\
global:
  domain: p47

# shared settings for every IOC on this beamline
ioc-instance:
  hostNetwork: true
  volumes:
    - name: beamline-data
      hostPath:
        path: /exports/mybeamline/data/
  volumeMounts:
    - name: beamline-data
      mountPath: /exports/mybeamline/data/

# shared setting for all legacy IOCs using the dev-c7 helm chart
dev-c7:
  hostNetwork: true
"""


def merged(child: str = CHILD, **kwargs: Any) -> tuple[dict[str, Any], list[str]]:
    """``merged_values`` over the fixtures, parsed, with its notes."""
    text, notes = hotfix.merged_values(
        child, hotfix.values_snippet("api", ENTRY, gid="37887"), **kwargs
    )
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)
    return cast(dict[str, Any], parsed), notes


def test_the_emitted_file_loses_nothing_the_input_had() -> None:
    """The plan's first falsification, and the point of the whole mode: this is
    pasted over the file it came from, so anything it drops is deployed gone."""
    document, _ = merged()
    before = yaml.safe_load(CHILD)
    for key, value in before["ioc-instance"].items():
        assert document["ioc-instance"][key] == value


def test_the_comments_the_user_wrote_are_still_there() -> None:
    """Which is why this reads YAML rather than re-dumping it. A values file is
    mostly the reasons for its keys, and a file handed back without them is not
    the file."""
    text, _ = hotfix.merged_values(
        CHILD, hotfix.values_snippet("api", ENTRY, gid="37887")
    )
    assert "# the image is the one thing this service really owns" in text
    assert "# yaml-language-server:" in text


def test_a_service_declaring_volumes_for_the_first_time_absorbs_the_shared_ones() -> (
    None
):
    """The whole difficulty of #192, and it cost a cluster run to find.

    A helm list *replaces* across the parent/child values merge; it does not
    merge. `bl47p-ea-fastcs-01` declared no `volumes:` and so inherited
    `beamline-data`; the moment podbench's values give it one, the inheritance
    is gone. The live proof is `bl47p-mo-ioc-01`, which declares `dev-shm` and
    whose running pod carries no `beamline-data` at all.
    """
    document, notes = merged(parent=PARENT)
    volumes = document["ioc-instance"]["volumes"]
    assert [entry["name"] for entry in volumes] == [
        "beamline-data",
        model.HOTFIX_CLAIM_VOLUME,
        model.SEAT_HOME_VOLUME,
    ]
    assert document["ioc-instance"]["volumeMounts"][0]["name"] == "beamline-data"
    # Absorbed, not silently: the emitted file now owns a key it used to inherit.
    assert any("beamline-data" in note and "copied" in note for note in notes)


def test_the_shared_files_comments_do_not_come_with_its_entries() -> None:
    """Measured on p47-services. A ruamel comment attaches to the node *after*
    it, so copying one entry out of the parent's list brought "shared setting
    for all legacy IOCs" along and landed it in the middle of volumeMounts."""
    text, _ = hotfix.merged_values(
        CHILD, hotfix.values_snippet("api", ENTRY, gid="37887"), parent=PARENT
    )
    assert "legacy IOCs" not in text


def test_no_parent_file_is_not_evidence_that_there_is_no_parent() -> None:
    """The one thing the merge cannot decide, so the one thing it says out loud.

    Absence of `--parent-values` looks exactly like a service with nothing to
    inherit, and getting it wrong unmounts a beamline directory.
    """
    _, notes = merged()
    assert any("--parent-values" in note for note in notes)


def test_a_service_that_already_declares_volumes_keeps_its_own() -> None:
    """Its own wins over the parent's - that is what declaring one means."""
    child = CHILD + "  volumes:\n    - name: dev-shm\n      emptyDir: {}\n"
    document, notes = merged(child, parent=PARENT)
    names = [entry["name"] for entry in document["ioc-instance"]["volumes"]]
    assert names[0] == "dev-shm"
    assert "beamline-data" not in names
    assert not any("volumes came from" in note for note in notes)
    # volumeMounts is a separate key and this service still declares none, so
    # that one is absorbed - the precedence is per key, not per file.
    assert any("volumeMounts came from" in note for note in notes)
    assert [e["name"] for e in document["ioc-instance"]["volumeMounts"]][0] == (
        "beamline-data"
    )


def test_running_the_merge_twice_changes_nothing() -> None:
    """Matched on `name`, which is what Kubernetes matches these on. A merge
    that duplicated its own entries would be unusable on a values file that has
    already been through it once - which is every re-run.

    Asserted on the **text** and not on the parsed values, which is the whole
    point. This test compared `yaml.safe_load` and passed while podbench's own
    comments appended a fresh copy of themselves on every run - found by running
    the command by hand on p47 the day after the first run, not by this suite.
    """
    snippet = hotfix.values_snippet("api", ENTRY, gid="37887")
    once, _ = hotfix.merged_values(CHILD, snippet, parent=PARENT)
    twice, _ = hotfix.merged_values(once, snippet, parent=PARENT)
    assert twice == once
    thrice, _ = hotfix.merged_values(twice, snippet, parent=PARENT)
    assert thrice == once


def test_podbenchs_own_comments_are_written_once_and_not_once_per_run() -> None:
    """The specific failure, named so a regression is legible.

    Re-emitting is a normal thing to do - after a chart bump, or to see what is
    deployed - and the second run reads the first run's output.
    """
    snippet = hotfix.values_snippet("api", ENTRY, gid="37887")
    once, _ = hotfix.merged_values(CHILD, snippet, parent=PARENT)
    twice, _ = hotfix.merged_values(once, snippet, parent=PARENT)
    for comment in (
        hotfix.SEAT_HOME_COMMENT,
        hotfix.CLAIM_COMMENT,
        hotfix.FSGROUP_COMMENT,
    ):
        marker = next(line for line in comment.splitlines() if line.strip())
        assert twice.count(marker) == 1, marker
    # The absorbed-list comment is written once per list key, and the shared
    # opening line is the same for both - so it is the key-naming line that has
    # to be counted, or this asserts the wrong thing.
    for key in ("volumes", "volumeMounts"):
        assert twice.count(f"declared no `{key}` of its own before") == 1, key


def test_where_the_keys_go_is_read_from_the_files_and_then_named() -> None:
    """Chart-agnostic: podbench does not know the target's chart. The shared
    file declares `volumes:` under `ioc-instance`, which answers the question
    for every service that inherits it."""
    _, notes = merged(parent=PARENT)
    assert any("ioc-instance" in note and "went under" in note for note in notes)


def test_values_under_overrides_what_was_read() -> None:
    document, notes = merged(under=["some-other-chart"])
    assert "volumes" in document["some-other-chart"]
    assert "volumes" not in document.get("ioc-instance", {})
    assert any("--values-under" in note for note in notes)


def test_the_merge_uses_the_key_names_it_was_given_and_not_the_convention() -> None:
    """The plan's third falsification. `values_snippet` lets a chart rename all
    five, so a merge that looked for `volumes` would quietly fail to absorb the
    parent's on a chart that calls it something else."""
    text, notes = hotfix.merged_values(
        CHILD,
        hotfix.values_snippet(
            "api", ENTRY, gid="37887", volumes_key="disks", mounts_key="diskMounts"
        ),
        parent=PARENT.replace("volumes:", "disks:").replace(
            "volumeMounts:", "diskMounts:"
        ),
        volumes_key="disks",
        mounts_key="diskMounts",
    )
    document = yaml.safe_load(text)
    assert [e["name"] for e in document["ioc-instance"]["disks"]][0] == "beamline-data"
    assert any("disks" in note and "copied" in note for note in notes)


def test_the_claim_key_goes_to_the_root_whatever_under_says() -> None:
    """It is a subchart's values, and helm looks for those by chart name at the
    root - not inside whatever mapping the target's pod template lives in."""
    document, _ = merged(parent=PARENT)
    assert document[hotfix.SUBCHART_VALUES_KEY] == {"enabled": True, "size": "2Gi"}
    assert hotfix.SUBCHART_VALUES_KEY not in document["ioc-instance"]


def test_the_central_route_still_reaches_the_root_too() -> None:
    """`--central-claim` is the older namespace-wide list, and a site already on
    it must still get a complete file."""
    text, _ = hotfix.merged_values(
        CHILD,
        hotfix.values_snippet("api", ENTRY, gid="37887", central_claim=True),
        parent=PARENT,
    )
    document = yaml.safe_load(text)
    assert document["hotfixProject"]["claims"][0]["name"] == "api"
    assert hotfix.SUBCHART_VALUES_KEY not in document


def test_merged_values_reads_no_file_and_no_cluster() -> None:
    """Strings in, a string out, for the same reason `values_snippet` takes no
    cluster: it is what makes every shape of the output assertable without
    either. `--values` is a wrapper, exactly as `--from-pod` is."""
    parameters = inspect.signature(hotfix.merged_values).parameters
    assert not [n for n in parameters if "pod" in n or "kube" in n]
    referenced = set(hotfix.merged_values.__code__.co_names)
    assert not {n for n in referenced if "kube" in n.lower()}
    assert not referenced & {"open", "read_text", "Path", "write_text"}


def test_values_flag_emits_the_file_and_asks_the_cluster_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end through the CLI, and `--no-from-pod` so the run is offline."""
    child = tmp_path / "values.yaml"
    child.write_text(CHILD)
    parent = tmp_path / "shared.yaml"
    parent.write_text(PARENT)
    runner = FakeRunner()

    code = hotfix.main(
        # fmt: off
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "--no-from-pod",
            "--entrypoint",
            ENTRY,
            "--gid",
            "37887",
            "--values",
            str(child),
            "--parent-values",
            str(parent),
        ],
        # fmt: on
        runner=runner,
    )

    assert code == 0
    assert runner.calls == []
    captured = capsys.readouterr()
    document = yaml.safe_load(captured.out)
    assert document["ioc-instance"]["image"] == "ghcr.io/example/fastcs:2025.10.1"
    assert [e["name"] for e in document["ioc-instance"]["volumes"]][0] == (
        "beamline-data"
    )
    assert document[hotfix.SUBCHART_VALUES_KEY]["enabled"] is True
    # The notes are stderr, so stdout stays something a shell can redirect over
    # the file it came from.
    assert "beamline-data" in captured.err
    assert "ioc-instance" in captured.err


def test_the_merge_flags_mean_nothing_without_the_file_they_merge_into(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = hotfix.main(
        # fmt: off
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "--no-from-pod",
            "--entrypoint",
            ENTRY,
            "--parent-values",
            str(tmp_path / "shared.yaml"),
        ],
        # fmt: on
        runner=FakeRunner(),
    )
    assert code == 2
    assert "without it" in capsys.readouterr().err


def test_a_values_file_that_is_not_there_is_refused_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = hotfix.main(
        # fmt: off
        [
            "hotfix",
            "values",
            "--app",
            "api",
            "--no-from-pod",
            "--entrypoint",
            ENTRY,
            "--values",
            str(tmp_path / "nope.yaml"),
        ],
        # fmt: on
        runner=FakeRunner(),
    )
    assert code == 2
    assert "nope.yaml" in capsys.readouterr().err


def test_values_snippet_has_no_file_dependency_either() -> None:
    """#192's second falsification. The emitter stays pure; the merge is a
    separate pure function and the reading is a wrapper over both."""
    parameters = inspect.signature(hotfix.values_snippet).parameters
    assert not [n for n in parameters if n in {"values", "path", "file", "parent"}]
    referenced = set(hotfix.values_snippet.__code__.co_names)
    assert not referenced & {"open", "read_text", "Path", "write_text"}


def test_values_snippet_has_no_cluster_dependency() -> None:
    """Not negotiable, per the plan: the emitter keeps its signature and stays
    testable with no cluster, which is what lets every shape of its output be
    asserted in a unit test. --from-pod is a wrapper that fills these arguments,
    never a read the emitter makes for itself."""
    parameters = inspect.signature(hotfix.values_snippet).parameters
    assert not [name for name in parameters if "pod" in name or "kube" in name]
    # Asked of the compiled function rather than its text, so that the word
    # "Kubernetes" in the docstring is not mistaken for a call to one.
    referenced = set(hotfix.values_snippet.__code__.co_names)
    assert not {name for name in referenced if "kube" in name.lower()}
    assert "get_pod" not in referenced


def test_values_prints_the_snippet(capsys: pytest.CaptureFixture[str]) -> None:
    code = hotfix.main(
        [
            "hotfix",
            "values",
            "--no-from-pod",
            "--app",
            "api",
            "--entrypoint",
            ENTRY,
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "claimName: api-podbench-project" in out
    assert ENTRY in out


def test_values_takes_the_apps_gid(capsys: pytest.CaptureFixture[str]) -> None:
    code = hotfix.main(
        # fmt: off
        [
            "hotfix",
            "values",
            "--no-from-pod",
            "--app",
            "api",
            "--entrypoint",
            ENTRY,
            "--gid",
            "65532",
        ],
        # fmt: on
    )
    assert code == 0
    assert "fsGroup: 65532" in capsys.readouterr().out


def test_values_wraps_a_named_liveness_probe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The probe is only emitted when the target has one to wrap."""
    code = hotfix.main(
        # fmt: off
        [
            "hotfix",
            "values",
            "--no-from-pod",
            "--app",
            "api",
            "--entrypoint",
            ENTRY,
            "--liveness",
            "/bin/bash /epics/ioc/liveness.sh",
        ],
        # fmt: on
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "livenessProbe:" in out
    assert model.HOTFIX_HOLD_PATH in out
    assert "/epics/ioc/liveness.sh" in out


def test_no_subcommand_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert hotfix.main(["hotfix"]) == 2
    out = capsys.readouterr().out
    assert "consolidate" in out
    assert "values" in out, "the emitter is a verb, so it belongs in the verb list"


def test_the_root_help_carries_no_option_belonging_to_one_verb(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#205 item 7: the root opened on fourteen options none of the four verbs
    could reach, which is what buried the verbs themselves. Every one of them
    now sits on `hotfix values`, so the root has only the flags typer gives it.
    """
    assert hotfix.main(["hotfix"]) == 2
    root = capsys.readouterr().out
    for flag in ("--app", "--from-pod", "--values", "--claim-venv", "--liveness"):
        assert flag not in root

    assert hotfix.main(["hotfix", "values", "--help"]) == 0
    verb = capsys.readouterr().out
    for flag in ("--app", "--from-pod", "--values", "--claim-venv", "--liveness"):
        assert flag in verb


def test_the_flag_the_verb_replaced_is_gone_rather_than_hidden(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No alias and no deprecation: hotfix mode is a surface nobody outside this
    repo drives yet, so a second spelling would only be a second thing to keep
    working. A hidden flag would still emit, which is the failure worth
    catching — the assertion is on stdout being empty, not on the exit code.
    """
    code = hotfix.main(
        ["hotfix", "--print-values", "--app", "api", "--no-from-pod", ENTRY]
    )

    assert code == 2
    assert capsys.readouterr().out == ""


def test_init_needs_neither_venv_nor_repo_when_the_pod_and_the_image_answer() -> None:
    """#205 items 1 and 2 from the outside. Two of the flags in a four-flag
    command typed in an emergency were values something already knew: the pod
    knows where it mounts the claim, and the image states what it was built
    from."""
    runner = FakeRunner(
        {
            "get pod api-7f9-abc -o json": json.dumps(pod_json(owner=None, claim=True)),
            # The checkout the image ships, which is what corroborates the
            # labels: OCI labels are inherited, so an uncorroborated one is
            # recorded as an assumed base rather than believed.
            f"exec -c podbench-1 api-7f9-abc -- {GIT} remote get-url origin": (
                "https://github.com/acme/api\n"
            ),
        }
    )
    labels = {
        oci.SOURCE_LABEL: "https://github.com/acme/api",
        oci.REVISION_LABEL: BASE_SHA,
    }

    def labels_for(image: str) -> dict[str, str]:
        assert image == BASE_DIGEST, "the digest running, not the tag it came in on"
        return labels

    with mock.patch.object(hotfix, "read_image_labels", labels_for):
        code = hotfix.main(
            ["hotfix", "init", "pod/api-7f9-abc", "--no-install", "-n", "demo"],
            runner=runner,
        )

    assert code == 0
    written = [
        argv
        for argv in runner.calls
        if f"cat > {model.HOTFIX_APP_PATH}/.podbench-hotfix.json" in " ".join(argv)
    ]
    assert written, "the manifest goes where `status` scans, with no --venv typed"
    manifest = hotfix.HotfixManifest.from_json(
        runner.stdins[runner.calls.index(written[0])] or ""
    )
    assert manifest.venv == model.HOTFIX_APP_PATH
    assert manifest.repo == "https://github.com/acme/api"
    assert manifest.base_commit == BASE_SHA
    assert not manifest.base_commit_assumed


def test_the_registry_is_not_asked_when_both_flags_are_given() -> None:
    """It is a network round trip on the way into an emergency, so it happens
    only when something is actually missing."""
    runner = FakeRunner(
        {"get pod api-7f9-abc -o json": json.dumps(pod_json(owner=None, claim=True))}
    )
    asked: list[str] = []

    def labels_for(image: str) -> dict[str, str]:
        asked.append(image)
        return {}

    with mock.patch.object(hotfix, "read_image_labels", labels_for):
        code = hotfix.main(
            # fmt: off
            [
                "hotfix",
                "init",
                "pod/api-7f9-abc",
                "--repo",
                "https://example.invalid/acme/api.git",
                "--base-commit",
                BASE_SHA,
                "--no-install",
                "-n",
                "demo",
            ],
            # fmt: on
            runner=runner,
        )

    assert code == 0
    assert asked == []


def test_a_venv_that_disagrees_with_the_pod_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeRunner(
        {"get pod api-7f9-abc -o json": json.dumps(pod_json(owner=None, claim=True))}
    )

    code = hotfix.main(
        # fmt: off
        [
            "hotfix",
            "apply",
            "pod/api-7f9-abc",
            "-m",
            "fix",
            "--venv",
            "/opt/venv",
            "-n",
            "demo",
        ],
        # fmt: on
        runner=runner,
    )

    assert code == 2
    assert "disagrees with the pod" in capsys.readouterr().err


def test_a_relocated_claim_is_warned_about_in_the_shared_warning_shape(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The one layout surface hotfix's `_warn` owns, driven through a verb.

    A warning is one line under the coloured leader, hung at
    `console.WARNING_HANG` - derived from `WARNING_LEAD`, so hotfix's warnings
    re-align with the launcher's rather than silently staying where a
    hand-counted indent put them.
    """
    relocated = pod_json(owner=None, claim=True)
    relocated["spec"]["containers"][0]["volumeMounts"] = [
        {"name": model.HOTFIX_CLAIM_VOLUME, "mountPath": "/opt/venv"}
    ]
    runner = FakeRunner({"get pod api-7f9-abc -o json": json.dumps(relocated)})
    runner.failures["exec -c podbench-1 api-7f9-abc -- test -e /podbench/app"] = ""

    def no_labels(image: str) -> dict[str, str]:
        return {}

    with mock.patch.object(hotfix, "read_image_labels", no_labels):
        code = hotfix.main(
            ["hotfix", "init", "pod/api-7f9-abc", "--no-install", "-n", "demo"],
            runner=runner,
        )

    lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    warning = lines[: next(i for i, line in enumerate(lines[1:], 1) if line[0] != " ")]
    assert warning[0].startswith(f"{console.WARNING_LEAD}  ")
    assert all(
        line.startswith(" " * console.WARNING_HANG)
        and line[console.WARNING_HANG] != " "
        for line in warning[1:]
    ), warning
    # And the warning does not promise something the next line takes away:
    # `init` seeds, copies the interpreter and writes the supervisor switch at
    # the default path, so a claim mounted elsewhere is refused.
    flowed = " ".join(" ".join(warning).split())
    assert "will refuse a claim mounted anywhere else" in flowed
    assert code == 2


def test_status_exits_non_zero_when_a_pod_needs_attention(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """So that "no unretired hotfixes" is testable at shutdown, not readable."""
    manifest = a_manifest(ahead=1)
    runner = FakeRunner(
        {
            "get pods -o json": json.dumps(
                {"items": [hotfixed_pod(manifest, digest=NEW_DIGEST)]}
            ),
            "exec -c app": state_exec(manifest),
        }
    )
    code = hotfix.main(["hotfix", "status", "-n", "demo", "--no-probe"], runner=runner)
    assert code == 1
    assert "image-changed" in capsys.readouterr().out


def test_status_exits_zero_when_everything_is_accounted_for() -> None:
    runner = FakeRunner(
        {
            "get pods -o json": json.dumps({"items": [hotfixed_pod(a_manifest())]}),
            "exec -c app": state_exec(a_manifest()),
        }
    )
    assert hotfix.main(["hotfix", "status", "-n", "demo"], runner=runner) == 0


def test_cli_reports_a_refusal_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner(
        {"get deployment api -o json": json.dumps(workload_json(replicas=3))}
    )
    code = hotfix.main(
        [
            "hotfix",
            "apply",
            "deployment/api",
            "--venv",
            VENV,
            "-m",
            "fix",
            "-n",
            "demo",
        ],
        runner=runner,
    )
    assert code == 2
    assert "3 replicas" in capsys.readouterr().err


def test_cli_apply_runs_git_through_the_seat() -> None:
    """Without --local the claim is reached by exec'ing into the podbench
    container, which is the only container guaranteed to have git."""
    seat = "exec -c podbench-1 api-7f9-abc --"
    runner = FakeRunner(
        {
            "get deployment api -o json": json.dumps(workload_json()),
            "get pods -l app=api -o json": json.dumps({"items": [pod_json()]}),
            "get pod api-7f9-abc -o json": json.dumps(pod_json()),
            f"{seat} cat /opt/venv/.podbench-hotfix.json": a_manifest().to_json(),
            f"{seat} {GIT} rev-parse": HEAD_SHA,
            f"{seat} {GIT} log": f"{HEAD_SHA}\x1ffix\n",
            f"{seat} {GIT} status": " M a.py\n",
        }
    )
    code = hotfix.main(
        [
            "hotfix",
            "apply",
            "deployment/api",
            "--venv",
            VENV,
            "-m",
            "fix",
            "-n",
            "demo",
        ],
        runner=runner,
    )
    assert code == 0
    assert runner.matching(f"exec -c podbench-1 api-7f9-abc -- {GIT} add")
    # The manifest is written back through the same seat, over stdin.
    assert any(
        "cat > /opt/venv/.podbench-hotfix.json" in " ".join(argv)
        for argv in runner.calls
    )


def test_seat_is_required_and_says_how_to_get_one() -> None:
    """The verbs *after* `init`. By then the seat existed, so a missing one
    means it died or the pod was replaced - a thing to be told about, not to
    paper over by spending another ephemeral container name."""
    runner = FakeRunner(
        {"get pod solo -o json": json.dumps(pod_json(name="solo", seat=False))}
    )
    with pytest.raises(hotfix.HotfixError, match="podbench attach"):
        hotfix.seat_container(kube(runner), "solo", None)


def test_init_lands_its_own_seat_rather_than_sending_the_user_to_attach(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #177's other half. `podbench vscode` lands a seat sized for an
    editor instead of telling somebody to go and attach one, and there is no
    reason `hotfix init` should be the verb that makes the user do it by hand -
    least of all now that the attach it would ask for is the one that mounts the
    claim."""
    runner = FakeRunner(
        {"get pod solo -o json": json.dumps(pod_json(name="solo", seat=False))}
    )
    landed: list[tuple[str, str]] = []

    def fake_attach(kube: Kubectl, pod: str, **kwargs: object) -> Session:
        landed.append((kube.namespace, pod))
        return Session(
            seat=ContainerRef(PodRef("demo", pod), "podbench-1"),
            workload="app",
            rung=Rung.DEGRADED,
            reused=False,
            warnings=("the claim was mounted at /podbench/app",),
        )

    with mock.patch.object(hotfix, "attach", fake_attach):
        name = hotfix.seat_container(kube(runner), "solo", None, land=True)

    assert name == "podbench-1"
    assert landed == [("demo", "solo")]
    out = capsys.readouterr().out
    # Said before the attach, because landing a seat is the slow part and a user
    # watching a silent minute deserves to know what podbench chose to do.
    assert "no seat is running in solo" in out
    # And the mount nobody typed is named, because that is the only thing that
    # makes taking it acceptable.
    assert "/podbench/app" in out


def test_a_seat_that_cannot_be_landed_exits_2_rather_than_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`init` lands its own seat since #177, and the landing goes through
    `launcher.attach`, which refuses in its own currency. `LauncherError` was
    not one of the exceptions `main` folds into exit 2, so a pod attach could
    not seat came out as a traceback instead of a sentence."""
    runner = FakeRunner(
        {
            "get pod solo -o json": json.dumps(
                {
                    "metadata": {"name": "solo"},
                    "spec": {"containers": []},
                    "status": {},
                }
            )
        }
    )

    code = hotfix.main(
        # fmt: off
        [
            "hotfix",
            "init",
            "solo",
            "--repo",
            "https://example/x",
            "--venv",
            VENV,
            "-n",
            "demo",
        ],
        # fmt: on
        runner=runner,
    )

    assert code == 2
    assert "pod has no containers" in capsys.readouterr().err


def test_a_named_seat_is_never_second_guessed_by_landing_another() -> None:
    """`--seat` is for a seat named something other than podbench-N, and taking
    it at its word is the whole of what it is for."""
    runner = FakeRunner({})

    assert hotfix.seat_container(kube(runner), "solo", "mine", land=True) == "mine"
    assert runner.calls == []


# -- `hotfix check` ---------------------------------------------------------

CHECK = ["hotfix", "check", "pod/api-7f9-abc", "-n", "demo"]
INIT = ["hotfix", "init", "pod/api-7f9-abc", "--no-install", "-n", "demo"]

IMAGE_LABELS = {
    oci.SOURCE_LABEL: "https://github.com/acme/api",
    oci.REVISION_LABEL: BASE_SHA,
}

SEAT_EXEC = "exec -c podbench-1 api-7f9-abc --"
APP_EXEC = "exec -c app api-7f9-abc --"


def stated_labels(image: str) -> dict[str, str]:
    """What the target image says about itself, which both verbs read."""
    return IMAGE_LABELS


SEEDED_PROBES = (
    f"{SEAT_EXEC} test -e /podbench/app/pyproject.toml",
    f"{APP_EXEC} test -e /podbench/app/pyproject.toml",
)
"""Both readings of "the claim already carries a project", which is one fact.

`init` asks it through the seat and `check` in the application container — one
volume, two mounts of it, deliberately, because `check` must answer before a
seat exists. A case that wants an unseeded claim has to deny both, or the fake
cluster is one the real one could not be and the two verbs disagree for a
reason nothing in the code shares.
"""


def hotfixable(pod: dict[str, Any]) -> FakeRunner:
    """A cluster on which `init` succeeds, so that a case can break one thing.

    The FakeRunner answers 0 to anything it was not told about, which is what
    makes "the claim is already seeded, the supervisor is up, every path is
    there" the default and each case below a single subtraction from it.
    """
    return FakeRunner(
        {
            "get pod api-7f9-abc -o json": json.dumps(pod),
            f"{SEAT_EXEC} {GIT} remote get-url origin": "https://github.com/acme/api\n",
        }
    )


def check_and_init(runner: FakeRunner) -> tuple[int, int]:
    """Both verbs over one cluster, which is the pairing this phase asserts."""
    with mock.patch.object(hotfix, "read_image_labels", stated_labels):
        return (
            hotfix.main(CHECK, runner=runner),
            hotfix.main(INIT, runner=runner),
        )


def rows(out: str) -> dict[str, str]:
    """The report's rows by name, flattened - several of them wrap at 80.

    Flattened the way `tests/test_doctor.py::flowed` does, and split on the two
    spaces that hold the name column apart from the detail: a name is several
    words for `target root`, and the run of spaces is what ends it.
    """
    found: dict[str, str] = {}
    name = ""
    for line in out.splitlines():
        if line.startswith(("-", "VERDICT", "BLOCKERS")):
            break
        head = line.strip()
        if head.startswith("["):
            status, _, rest = head.partition("]")
            name, _, detail = rest.strip().partition("  ")
            found[name] = f"{status}] {' '.join(detail.split())}"
        elif name:
            found[name] += " " + " ".join(line.split())
    return found


def test_check_passes_a_target_init_then_accepts() -> None:
    """The phase's falsification, the way round that matters most: `check` has
    one job, which is to make the second attempt unnecessary."""
    runner = hotfixable(pod_json(owner=None, claim=True))

    assert check_and_init(runner) == (0, 0)


@pytest.mark.parametrize(
    ("blocker", "claim", "denied"),
    [
        # The claim, read two different ways on purpose: `check` reads the pod
        # spec, so it answers with no seat, and `init` finds out by asking the
        # seat for the mountPath.
        ("claim", False, [f"{SEAT_EXEC} test -e /podbench/app"]),
        ("supervisor", True, [f"{APP_EXEC} test -e /tmp/podbench-child.pid"]),
        (
            "project",
            True,
            [
                *SEEDED_PROBES,
                f"{APP_EXEC} test -d /app",
                f"{SEAT_EXEC} test -e /proc/1/root/app",
            ],
        ),
        (
            "target root",
            True,
            [
                *SEEDED_PROBES,
                f"{SEAT_EXEC} test -e /proc/1/root/app",
                f"{SEAT_EXEC} ls /proc/1/root/",
            ],
        ),
        (
            "interpreter",
            True,
            [
                *SEEDED_PROBES,
                f"{APP_EXEC} test -d /python",
                f"{SEAT_EXEC} cp -a /proc/1/root/python",
            ],
        ),
    ],
)
def test_check_blocks_exactly_where_init_refuses(
    blocker: str,
    claim: bool,
    denied: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The phase's falsification: `check` must refuse nothing `init` accepts and
    accept nothing `init` refuses. Each case breaks one prerequisite and asserts
    both verbs on the same cluster - which is also what stops the two drifting
    apart later, since they measure the same state by different routes."""
    runner = hotfixable(pod_json(owner=None, claim=claim))
    for prefix in denied:
        runner.failures[prefix] = ""

    check, init = check_and_init(runner)

    assert (check, init) == (1, 2)
    out = capsys.readouterr().out
    assert rows(out)[blocker].startswith("[FAIL]")
    assert f"BLOCKERS: {blocker}" in out


def test_a_multi_replica_target_is_the_whole_report() -> None:
    """Nothing below the target can be measured without a pod, and eight "not
    measured" lines under one real refusal would bury it."""
    runner = FakeRunner(
        {"get deployment api -o json": json.dumps(workload_json(replicas=3))}
    )

    code = hotfix.main(
        ["hotfix", "check", "deployment/api", "-n", "demo"], runner=runner
    )

    assert code == 1
    assert (
        hotfix.main(
            ["hotfix", "init", "deployment/api", "--no-install", "-n", "demo"],
            runner=runner,
        )
        == 2
    )


def test_check_reports_every_blocker_in_one_pass(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#205 item 3 itself. Each of these was discovered one per attempt, and
    each attempt is a chart change and a redeploy in an emergency."""
    runner = hotfixable(pod_json(owner=None, claim=False))
    runner.failures[f"{APP_EXEC} test -e /tmp/podbench-child.pid"] = ""
    runner.failures[f"{APP_EXEC} test -d /app"] = ""

    with mock.patch.object(hotfix, "read_image_labels", stated_labels):
        code = hotfix.main(CHECK, runner=runner)

    assert code == 1
    out = capsys.readouterr().out
    assert "BLOCKERS: claim, supervisor, project" in out


def test_check_writes_nothing_and_lands_no_seat() -> None:
    """Read-only is the verb's premise: an ephemeral container cannot be taken
    back off a pod, so a verb somebody runs to ask a question must not spend
    one - and `init` is the verb that lands seats."""
    runner = hotfixable(pod_json(owner=None, claim=True, seat=False))

    with mock.patch.object(hotfix, "read_image_labels", stated_labels):
        assert hotfix.main(CHECK, runner=runner) == 0

    assert runner.stdins == [None] * len(runner.calls)
    for argv in runner.calls:
        key = runner.key(argv)
        command = key.partition(" -- ")[2].split()
        assert key.startswith("get ") or command[0] in ("test", "ls"), key


def test_an_unmeasurable_check_says_so_rather_than_fine(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The seat's view of the target root is a property of a container that
    does not exist yet. Reporting it either way would be an invention, and the
    verdict says `measured here` for exactly this reason."""
    runner = hotfixable(pod_json(owner=None, claim=True, seat=False))
    for probe in SEEDED_PROBES:
        runner.failures[probe] = ""

    with mock.patch.object(hotfix, "read_image_labels", stated_labels):
        assert hotfix.main(CHECK, runner=runner) == 0

    out = capsys.readouterr().out
    assert rows(out)["target root"].startswith("[warn]")
    assert "not measured" in rows(out)["target root"]
    assert "nothing measured here blocks" in out
    # And no `ls` was issued through a seat that is not there.
    assert not runner.matching("exec -c podbench-1")


def test_a_non_exec_probe_is_a_warning_because_init_accepts_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """It is `apply`'s hold this breaks, not `init`, and the exit code is
    reserved for what stops the next command. Saying nothing is not an option
    either: the kubelet will restart the pod out from under the seat."""
    pod = pod_json(owner=None, claim=True)
    pod["spec"]["containers"][0]["livenessProbe"] = {"httpGet": {"path": "/healthz"}}
    runner = hotfixable(pod)

    assert check_and_init(runner) == (0, 0)
    assert rows(capsys.readouterr().out)["liveness"].startswith("[warn]")


def test_an_exec_probe_and_no_probe_at_all_are_both_fine(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """7 of 18 containers on a real beamline declare a probe, and the canonical
    fastcs target is not one of them: an absent probe is never a fault."""
    pod = pod_json(owner=None, claim=True)
    pod["spec"]["containers"][0]["livenessProbe"] = {
        "exec": {"command": ["/bin/check"]},
        "periodSeconds": 30,
    }
    runner = hotfixable(pod)

    with mock.patch.object(hotfix, "read_image_labels", stated_labels):
        assert hotfix.main(CHECK, runner=runner) == 0
    assert rows(capsys.readouterr().out)["liveness"].startswith("[ok]")


def test_a_claim_mounted_somewhere_else_blocks_rather_than_warns() -> None:
    """`init` seeds /podbench/app, copies the interpreter there and emits a
    supervisor switch naming it, none of which move with the mount - so a claim
    mounted elsewhere is a refusal and reporting it as a note would be a warning
    contradicted by the next command."""
    pod = pod_json(owner=None, claim=True)
    pod["spec"]["containers"][0]["volumeMounts"] = [
        {"name": model.HOTFIX_CLAIM_VOLUME, "mountPath": "/opt/venv"}
    ]
    runner = hotfixable(pod)
    runner.failures[f"{SEAT_EXEC} test -e /podbench/app"] = ""

    assert check_and_init(runner) == (1, 2)


def test_a_test_that_could_not_run_is_not_an_answer_about_the_image(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A distroless application container may have no `test` at all, and 127 is
    not the same finding as a project that is absent."""
    runner = hotfixable(pod_json(owner=None, claim=True))
    for probe in SEEDED_PROBES:
        runner.failures[probe] = ""

    def no_test(argv: Sequence[str], **kwargs: Any) -> CommandResult:
        if " -- test -d /app" in " ".join(argv):
            return CommandResult(tuple(argv), 127, "", "exec: test: not found")
        return FakeRunner.__call__(runner, argv, **kwargs)

    with mock.patch.object(hotfix, "read_image_labels", stated_labels):
        code = hotfix.main(CHECK, runner=no_test)

    assert code == 0
    assert rows(capsys.readouterr().out)["project"].startswith("[warn]")


def test_an_image_naming_no_repository_blocks_because_init_refuses_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The falsification the other way round, and the one that was live: `init`
    raises NO_SOURCE_REPO before it seeds anything, so a `check` that passed
    this state would send the reader into exactly the second attempt this verb
    exists to remove."""
    runner = hotfixable(pod_json(owner=None, claim=True))

    def unlabelled(image: str) -> None:
        return None

    with mock.patch.object(hotfix, "read_image_labels", unlabelled):
        check = hotfix.main(CHECK, runner=runner)
        init = hotfix.main(INIT, runner=runner)

    assert (check, init) == (1, 2)
    out = capsys.readouterr().out
    assert rows(out)["source"].startswith("[FAIL]")
    assert "BLOCKERS: source" in out


def test_check_hears_repo_the_way_init_does() -> None:
    """The other half of the same row: `--repo` is what answers it, so a check
    that could not hear the flag would refuse a target `init --repo URL`
    accepts. And the registry is not read at all once the flag has answered -
    it is a round trip on the way into an emergency."""
    runner = hotfixable(pod_json(owner=None, claim=True))
    repo = ["--repo", "https://github.com/acme/api"]
    reads: list[str] = []

    def never_labelled(image: str) -> None:
        reads.append(image)
        return None

    with mock.patch.object(hotfix, "read_image_labels", never_labelled):
        check = hotfix.main([*CHECK, *repo], runner=runner)
        assert reads == []
        init = hotfix.main([*INIT, *repo], runner=runner)

    assert (check, init) == (0, 0)
    # `init` reads them anyway, for the revision that dates the base commit.
    assert reads


def test_an_already_seeded_claim_is_not_blocked_on_rows_init_never_reads(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`init` short-circuits its entire seed on a seeded claim, which makes the
    target root, the project and the interpreter moot - so measuring them anyway
    refuses a target `init` accepts. This is the state a second `check` is run
    in: after a fix, on a pod already hotfixed, whose seat may well be a
    degraded one that cannot list /proc/1/root."""
    runner = hotfixable(pod_json(owner=None, claim=True))
    runner.failures[f"{SEAT_EXEC} ls /proc/1/root/"] = ""
    runner.failures[f"{APP_EXEC} test -d /app"] = ""

    assert check_and_init(runner) == (0, 0)
    report = rows(capsys.readouterr().out)
    assert "already carries a project" in report["claim"]
    for name in ("target root", "project", "interpreter"):
        assert report[name].startswith("[ok]"), report[name]
        assert "not asked" in report[name]


def test_a_named_seat_is_corroborated_rather_than_restated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--seat` naming a container nobody landed used to be reported as `[ok]
    ghost is running`, and the row it broke was `target root` - so the report
    sent a reader chasing CAP_SYS_PTRACE and `doctor` for a typo. #178's false
    trail, by a new route."""
    runner = hotfixable(pod_json(owner=None, claim=True, seat=False))
    for probe in SEEDED_PROBES:
        runner.failures[probe] = ""
    # kubectl's own answer for an exec into a container that is not there.
    runner.failures["exec -c ghost"] = "container ghost not found in pod"

    with mock.patch.object(hotfix, "read_image_labels", stated_labels):
        check = hotfix.main([*CHECK, "--seat", "ghost"], runner=runner)
        init = hotfix.main([*INIT, "--seat", "ghost"], runner=runner)

    assert (check, init) == (1, 2)
    report = rows(capsys.readouterr().out)
    assert report["seat"].startswith("[FAIL]")
    assert "ghost" in report["seat"]
    # And no trail is laid to the ptrace rung for what is a name.
    assert report["target root"].startswith("[warn]")
    assert "not measured" in report["target root"]
    assert not runner.matching("exec -c ghost api-7f9-abc -- ls")


def test_the_pod_is_read_once_and_not_again() -> None:
    """The target walk has already read this pod, and a second `get pod` to
    re-read it is a call that answers nothing new - and one that puts the
    report's rows a reconcile apart from the target they are about."""
    runner = hotfixable(pod_json(owner=None, claim=True))

    with mock.patch.object(hotfix, "read_image_labels", stated_labels):
        assert hotfix.main(CHECK, runner=runner) == 0

    assert len(runner.matching("get pod api-7f9-abc -o json")) == 1


def test_the_report_uses_the_status_tokens_console_knows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A new verb uses one of the existing spellings rather than inventing one:
    the bracket is what lets `console` colour it without either module saying
    what a colour is. And the name column keeps the two spaces that make a row a
    row - a name padded to exactly its width leaves one, the rule stops firing,
    and half the names come out bold and half plain."""
    runner = hotfixable(pod_json(owner=None, claim=True, seat=False))

    with mock.patch.object(hotfix, "read_image_labels", stated_labels):
        hotfix.main(CHECK, runner=runner)

    printed = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("  [")
    ]
    assert printed
    for line in printed:
        token, _, rest = line.strip().partition("]")
        assert token[1:] in {status.value for status in hotfix.CheckStatus}
        name, separator, detail = rest.strip().partition("  ")
        assert name and separator and detail.strip(), line
