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

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from podbench import hotfix, model
from podbench.kubectl import DEFAULT_CALL_TIMEOUT, CommandResult, Kubectl

VENV = "/opt/venv"
CHECKOUT = "/opt/venv/src"
BASE_SHA = "1111111111111111111111111111111111111111"
HEAD_SHA = "2222222222222222222222222222222222222222"
BASE_DIGEST = "ghcr.io/acme/api@sha256:aaaa"
NEW_DIGEST = "ghcr.io/acme/api@sha256:bbbb"

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
) -> dict[str, Any]:
    """One application pod, optionally carrying a podbench seat and a manifest."""
    metadata: dict[str, Any] = {"name": name, "annotations": annotations or {}}
    if owner is not None:
        metadata["ownerReferences"] = [
            {"kind": owner.title(), "name": "api-7f9", "controller": True}
        ]
    spec: dict[str, Any] = {
        "containers": [{"name": "app", "image": "ghcr.io/acme/api:1.4.0"}]
    }
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
    files = {f"{VENV}/pyvenv.cfg": PYVENV_CFG, f"{CHECKOUT}/.git": ""}
    files.update(extra)
    return FakeStore(
        files=files,
        outputs={
            f"git -C {CHECKOUT} rev-parse HEAD": HEAD_SHA,
            f"git -C {CHECKOUT} log": f"{HEAD_SHA}\x1fmake the beam behave\n",
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


def test_manifest_annotations_carry_the_marker_and_the_document() -> None:
    manifest = a_manifest()
    annotations = hotfix.manifest_annotations(manifest)
    assert annotations[hotfix.HOTFIXED_ANNOTATION] == "true"
    assert annotations[hotfix.APPLIED_ANNOTATION] == manifest.timestamp
    assert (
        hotfix.HotfixManifest.from_json(annotations[hotfix.MANIFEST_ANNOTATION])
        == manifest
    )


def test_manifest_from_pod_is_none_without_the_annotation() -> None:
    assert hotfix.manifest_from_pod(pod_json()) is None


# -- paths and small pure helpers ------------------------------------------


def test_checkout_and_manifest_live_on_the_claim() -> None:
    assert hotfix.checkout_path(VENV) == CHECKOUT
    assert hotfix.manifest_path(VENV) == f"{VENV}/{hotfix.MANIFEST_FILENAME}"


def test_install_runs_the_venvs_own_interpreter() -> None:
    argv = hotfix.install_argv(VENV, CHECKOUT)
    assert argv[0] == f"{VENV}/bin/python"
    assert "--no-deps" in argv
    assert argv[-1] == CHECKOUT


def test_metadata_changed_only_for_packaging_files() -> None:
    assert not hotfix.metadata_changed(["src/api/beam.py", "README.md"])
    assert hotfix.metadata_changed(["src/api/beam.py", "pyproject.toml"])


def test_changed_paths_covers_the_whole_range_since_the_last_apply() -> None:
    """Several hand commits can accumulate between two applies, so the metadata
    question is asked of the range and not of HEAD alone."""
    store = FakeStore(
        outputs={
            f"git -C {CHECKOUT} diff --name-only {BASE_SHA}..{HEAD_SHA}": (
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
    store = FakeStore(outputs={f"git -C {CHECKOUT} show": "\npyproject.toml\n"})
    assert hotfix.changed_paths(store, CHECKOUT, "", HEAD_SHA) == ("pyproject.toml",)


def test_changed_paths_falls_back_when_the_recorded_commit_is_gone() -> None:
    """A rewritten history over-installs rather than under-installing: a
    redundant editable install costs seconds, a skipped one costs the hotfix."""
    store = FakeStore(outputs={f"git -C {CHECKOUT} show": "pyproject.toml\n"})
    store.failures[f"git -C {CHECKOUT} diff"] = "bad revision"
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
            f"git -C {CHECKOUT} log": (
                f"{HEAD_SHA}\x1fsecond fix\n{BASE_SHA[:-1]}3\x1ffirst fix\n"
            )
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
    store.failures[f"git -C {CHECKOUT} log"] = "unknown revision"
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
    health, _ = hotfix.assess(None, current_digest=BASE_DIGEST)
    assert health is hotfix.HotfixHealth.UNREADABLE
    assert not health.ok


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


def test_annotation_goes_on_the_pod_template() -> None:
    """A pod annotation would not survive the reschedule this mode relies on."""
    runner = FakeRunner()
    manifest = a_manifest(ahead=1)
    detail = hotfix.annotate(kube(runner), deployment_target(), manifest)

    calls = runner.matching("patch deployment api")
    assert len(calls) == 1
    body = json.loads(calls[0][calls[0].index("-p") + 1])
    annotations = body["spec"]["template"]["metadata"]["annotations"]
    assert annotations[hotfix.HOTFIXED_ANNOTATION] == "true"
    assert (
        hotfix.HotfixManifest.from_json(annotations[hotfix.MANIFEST_ANNOTATION])
        == manifest
    )
    assert "--type=merge" in calls[0]
    assert "rolls the workload" in detail


def test_annotating_an_unowned_pod_says_what_it_costs() -> None:
    runner = FakeRunner()
    target = hotfix.HotfixTarget(
        pod=hotfix.PodRef("demo", "solo"),
        container="app",
        image="ghcr.io/acme/api:1.4.0",
        image_digest=BASE_DIGEST,
    )
    detail = hotfix.annotate(kube(runner), target, a_manifest())
    calls = runner.matching("patch pod solo")
    assert len(calls) == 1
    body = json.loads(calls[0][calls[0].index("-p") + 1])
    assert hotfix.HOTFIXED_ANNOTATION in body["metadata"]["annotations"]
    assert "lost if the pod is ever replaced" in detail


# -- init ------------------------------------------------------------------


def test_init_refuses_an_unseeded_claim() -> None:
    """Seeding cannot be done after the fact: the mount hides what to copy."""
    store = FakeStore()
    with pytest.raises(hotfix.HotfixError) as caught:
        hotfix.init(
            kube(FakeRunner()),
            store,
            deployment_target(),
            venv=VENV,
            repo="https://example.invalid/acme/api.git",
        )
    assert "initContainer" in str(caught.value)
    assert not store.ran("git clone")


def test_init_clones_installs_and_records_the_base_commit() -> None:
    runner = FakeRunner()
    store = FakeStore(
        files={f"{VENV}/pyvenv.cfg": PYVENV_CFG},
        outputs={f"git -C {CHECKOUT} rev-parse HEAD": BASE_SHA},
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
    # The install runs in the application container, not in the seat.
    assert runner.matching(f"exec -c app api-7f9-abc -- {VENV}/bin/python")
    assert runner.matching("patch deployment api")
    assert any("editable install" in action for action in actions)


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
    assert not store.ran("git clone")
    assert any("already present" in action for action in actions)


def test_init_explains_a_failed_install() -> None:
    runner = FakeRunner()
    runner.failures["exec -c app"] = "no module named pip"
    store = FakeStore(
        files={f"{VENV}/pyvenv.cfg": PYVENV_CFG},
        outputs={f"git -C {CHECKOUT} rev-parse HEAD": BASE_SHA},
    )
    with pytest.raises(hotfix.HotfixError, match="symlink into the application image"):
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
    store.outputs[f"git -C {CHECKOUT} status --porcelain"] = (
        " M src/api/beam.py\n" if dirty else ""
    )
    # The reinstall question is asked of the manifest's recorded commit..HEAD,
    # so that is the range the fake git answers for.
    store.outputs[f"git -C {CHECKOUT} diff --name-only {BASE_SHA}..{HEAD_SHA}"] = (
        f"{changed}\n"
    )
    return store


def test_apply_commits_measures_drift_and_annotates() -> None:
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

    assert store.ran(f"git -C {CHECKOUT} add -A")
    assert any("commit" in " ".join(call) for call in store.calls)
    assert manifest.commit == HEAD_SHA
    assert manifest.ahead == 1
    assert manifest.commits[0].subject == "make the beam behave"
    assert manifest.author == "Ada <ada@example.invalid>"

    calls = runner.matching("patch deployment api")
    body = json.loads(calls[0][calls[0].index("-p") + 1])
    written = body["spec"]["template"]["metadata"]["annotations"]
    assert (
        hotfix.HotfixManifest.from_json(written[hotfix.MANIFEST_ANNOTATION]).ahead == 1
    )
    assert any("rolls the workload" in action for action in actions)
    # The template edit is the bounce; deleting the pod as well would race it.
    assert not runner.matching("delete pod")


def test_apply_skips_the_reinstall_when_only_code_changed() -> None:
    runner = FakeRunner()
    hotfix.apply_hotfix(
        kube(runner),
        applied_store(),
        deployment_target(),
        venv=VENV,
        message="fix",
    )
    assert not runner.matching(f"exec -c app api-7f9-abc -- {VENV}/bin/python")


def test_apply_reinstalls_when_packaging_metadata_changed() -> None:
    runner = FakeRunner()
    _, actions = hotfix.apply_hotfix(
        kube(runner),
        applied_store(changed="pyproject.toml"),
        deployment_target(),
        venv=VENV,
        message="new entry point",
    )
    assert runner.matching(f"exec -c app api-7f9-abc -- {VENV}/bin/python")
    assert any("editable install" in action for action in actions)


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
    assert not store.ran(f"git -C {CHECKOUT} add")
    assert runner.matching(f"exec -c app api-7f9-abc -- {VENV}/bin/python")
    assert any("editable install" in action for action in actions)


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
    assert not runner.matching(f"exec -c app api-7f9-abc -- {VENV}/bin/python")
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
    assert not store.ran(f"git -C {CHECKOUT} add")
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


def test_apply_will_not_delete_a_pod_nobody_owns() -> None:
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
    assert any("no controller" in action for action in actions)


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
    assert store.ran(f"git -C {CHECKOUT} push origin HEAD:refs/heads/patch/beamtime-14")
    assert manifest.consolidated_branch == "patch/beamtime-14"
    reread = hotfix.HotfixManifest.from_json(store.files[hotfix.manifest_path(VENV)])
    assert reread.consolidated_branch == "patch/beamtime-14"
    checklist = "\n".join(actions)
    assert "gh pr create" in checklist
    assert "hotfixProject.enabled=false" in checklist


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
    assert not store.ran(f"git -C {CHECKOUT} push")
    assert any("would push" in action for action in actions)


def test_consolidate_refuses_when_there_is_no_drift() -> None:
    store = applied_store()
    store.outputs[f"git -C {CHECKOUT} log"] = ""
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
    return pod_json(
        name=name, digest=digest, annotations=hotfix.manifest_annotations(manifest)
    )


def test_status_ignores_pods_without_a_hotfix() -> None:
    runner = FakeRunner({"get pods -o json": json.dumps({"items": [pod_json()]})})
    assert hotfix.status_rows(kube(runner)) == []
    assert hotfix.format_status([]) == "no hotfixed pods in this namespace"


def test_status_reports_drift_for_a_healthy_hotfix() -> None:
    manifest = a_manifest(
        ahead=2, commits=(hotfix.HotfixCommit(HEAD_SHA, "stop the beam tripping"),)
    )
    runner = FakeRunner(
        {"get pods -o json": json.dumps({"items": [hotfixed_pod(manifest)]})}
    )
    rows = hotfix.status_rows(kube(runner))
    assert len(rows) == 1
    assert rows[0].health is hotfix.HotfixHealth.ACTIVE
    report = hotfix.format_status(rows)
    assert "+2 commit(s)" in report
    assert "stop the beam tripping" in report
    assert "and 1 more" in report
    # Nothing changed, so nothing was exec'd: status has to stay cheap enough
    # to run habitually.
    assert not runner.matching("exec")


def test_status_probes_the_interpreter_only_when_the_image_moved() -> None:
    manifest = a_manifest(ahead=1)
    runner = FakeRunner(
        {
            "get pods -o json": json.dumps(
                {"items": [hotfixed_pod(manifest, digest=NEW_DIGEST)]}
            ),
            "exec -c app": "Python 3.13.1\n",
        }
    )
    rows = hotfix.status_rows(kube(runner))
    assert runner.matching("exec -c app")
    assert rows[0].health is hotfix.HotfixHealth.INTERPRETER_MISMATCH
    assert "will not import" in rows[0].detail
    assert "!" in hotfix.format_status(rows)


def test_status_can_be_told_not_to_exec() -> None:
    manifest = a_manifest(ahead=1)
    runner = FakeRunner(
        {
            "get pods -o json": json.dumps(
                {"items": [hotfixed_pod(manifest, digest=NEW_DIGEST)]}
            )
        }
    )
    rows = hotfix.status_rows(kube(runner), probe=False)
    assert not runner.matching("exec")
    assert rows[0].health is hotfix.HotfixHealth.IMAGE_CHANGED


def test_status_notes_a_manifest_from_an_older_podbench() -> None:
    older = json.dumps({"commit": HEAD_SHA, "baseCommit": BASE_SHA, "container": "app"})
    pod = pod_json(
        annotations={
            hotfix.HOTFIXED_ANNOTATION: "true",
            hotfix.MANIFEST_ANNOTATION: older,
        }
    )
    runner = FakeRunner({"get pods -o json": json.dumps({"items": [pod]})})
    rows = hotfix.status_rows(kube(runner))
    assert "older podbench" in "\n".join(rows[0].notes)


def test_status_reports_a_pod_whose_provenance_is_gone() -> None:
    pod = pod_json(
        annotations={
            hotfix.HOTFIXED_ANNOTATION: "true",
            hotfix.MANIFEST_ANNOTATION: "{not json",
        }
    )
    runner = FakeRunner({"get pods -o json": json.dumps({"items": [pod]})})
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


def test_print_values_needs_the_two_things_it_cannot_guess(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert hotfix.main(["hotfix", "--print-values"]) == 2
    assert "--app" in capsys.readouterr().err


def test_print_values_prints_the_snippet(capsys: pytest.CaptureFixture[str]) -> None:
    code = hotfix.main(
        ["hotfix", "--print-values", "--app", "api", "--entrypoint", ENTRY]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "claimName: api-podbench-project" in out
    assert ENTRY in out


def test_print_values_takes_the_apps_gid(capsys: pytest.CaptureFixture[str]) -> None:
    code = hotfix.main(
        # fmt: off
        [
            "hotfix",
            "--print-values",
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


def test_print_values_wraps_a_named_liveness_probe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The probe is only emitted when the target has one to wrap."""
    code = hotfix.main(
        # fmt: off
        [
            "hotfix",
            "--print-values",
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
    assert "consolidate" in capsys.readouterr().out


def test_status_exits_non_zero_when_a_pod_needs_attention(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """So that "no unretired hotfixes" is testable at shutdown, not readable."""
    manifest = a_manifest(ahead=1)
    runner = FakeRunner(
        {
            "get pods -o json": json.dumps(
                {"items": [hotfixed_pod(manifest, digest=NEW_DIGEST)]}
            )
        }
    )
    code = hotfix.main(["hotfix", "status", "-n", "demo", "--no-probe"], runner=runner)
    assert code == 1
    assert "image-changed" in capsys.readouterr().out


def test_status_exits_zero_when_everything_is_accounted_for() -> None:
    runner = FakeRunner(
        {"get pods -o json": json.dumps({"items": [hotfixed_pod(a_manifest())]})}
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
            f"{seat} git -C /opt/venv/src rev-parse": HEAD_SHA,
            f"{seat} git -C /opt/venv/src log": f"{HEAD_SHA}\x1ffix\n",
            f"{seat} git -C /opt/venv/src status": " M a.py\n",
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
    assert runner.matching("exec -c podbench-1 api-7f9-abc -- git -C /opt/venv/src add")
    # The manifest is written back through the same seat, over stdin.
    assert any(
        "cat > /opt/venv/.podbench-hotfix.json" in " ".join(argv)
        for argv in runner.calls
    )


def test_seat_is_required_and_says_how_to_get_one() -> None:
    runner = FakeRunner(
        {"get pod solo -o json": json.dumps(pod_json(name="solo", seat=False))}
    )
    with pytest.raises(hotfix.HotfixError, match="podbench attach"):
        hotfix.seat_container(kube(runner), "solo", None)
