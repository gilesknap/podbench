"""Unit tests for the registry audit, with the network injected away.

The script lives in `.github/scripts/` rather than in `src/`, because it is CI
tooling and not part of the wheel - podbench ships one runtime dependency and a
registry janitor is not it. That puts it outside `testpaths`, so its doctests are
run explicitly below rather than by `--doctest-modules`.
"""

from __future__ import annotations

import doctest
import importlib.util
import io
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).parent.parent / ".github" / "scripts" / "ghcr_audit.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ghcr_audit", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ghcr_audit"] = module
    spec.loader.exec_module(module)
    return module


ghcr_audit = _load()


# One index with the shape `_container.yml` actually publishes: two per-arch
# manifests and their two attestations, the latter at `unknown/unknown`.
INDEX = {
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {"digest": "sha256:amd64", "platform": {"architecture": "amd64"}},
        {"digest": "sha256:amd64att", "platform": {"architecture": "unknown"}},
        {"digest": "sha256:arm64", "platform": {"architecture": "arm64"}},
        {"digest": "sha256:arm64att", "platform": {"architecture": "unknown"}},
    ],
}
LEAF: Mapping[str, Any] = {"config": {}, "layers": []}


class FakeRegistry:
    """Routes the four URL shapes the audit asks for, and counts the fetches."""

    def __init__(
        self,
        tags: list[str],
        manifests: Mapping[str, Any] | None = None,
        wheels: list[str] | None = None,
        missing: set[str] | None = None,
    ) -> None:
        self.tags = tags
        self.manifests = manifests or {}
        self.wheels = wheels or []
        self.missing = missing or set()
        self.fetched: list[str] = []

    def __call__(self, request: urllib.request.Request) -> Any:
        url = request.full_url
        self.fetched.append(url)
        if "pypi.org" in url:
            return self._json({"releases": {v: [] for v in self.wheels}})
        if "/token?" in url:
            return self._json({"token": "anonymous"})
        if "/tags/list" in url:
            return self._json({"tags": self.tags})
        reference = url.rsplit("/manifests/", 1)[1]
        if reference in self.missing:
            raise urllib.error.HTTPError(url, 404, "MANIFEST_UNKNOWN", {}, None)  # type: ignore[arg-type]
        return self._json(self.manifests.get(reference, LEAF))

    @staticmethod
    def _json(body: Any) -> io.StringIO:
        return io.StringIO(json.dumps(body))


def test_doctests_execute() -> None:
    """The repo runs every `>>>`; this module is outside testpaths, so do it here."""
    results = doctest.testmod(ghcr_audit)
    assert results.failed == 0, (
        f"{results.failed} of {results.attempted} doctests failed"
    )


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("0.4.0", True),
        ("0.4.0b3", True),
        ("0.4.0-beta.3", True),
        ("0.1.0-alpha.1", True),
        ("main", True),
        ("latest", True),
        # A branch tag opens with a prerelease version and must not be mistaken
        # for one - this is the case a naive `startswith` release regex fails.
        ("0.4.1-beta.1-claude-my-branch", False),
        ("sha-cd1c5df9d04b741d8e6a20591efc24624ee8391a", False),
    ],
)
def test_is_protected(tag: str, expected: bool) -> None:
    assert ghcr_audit.is_protected(tag) is expected


def test_classify_splits_the_four_kinds() -> None:
    inventory = ghcr_audit.classify(
        ["0.4.0", "0.4.0b3", "main", "latest", "sha-abc", "0.4.1-beta.1-branch"]
    )
    assert inventory.releases == ("0.4.0", "0.4.0b3")
    assert inventory.floating == ("latest", "main")
    assert inventory.shas == ("sha-abc",)
    assert inventory.branches == ("0.4.1-beta.1-branch",)
    assert inventory.total == 6


def test_closure_reaches_every_child_of_an_index() -> None:
    registry = ghcr_audit.Registry("o/p", FakeRegistry([], {"0.4.0": INDEX}))
    reached = ghcr_audit.closure(registry, ["0.4.0"])
    assert set(reached) == {
        "0.4.0",
        "sha256:amd64",
        "sha256:amd64att",
        "sha256:arm64",
        "sha256:arm64att",
    }


def test_closure_fetches_a_shared_child_once() -> None:
    """Two tags on one digest is the normal case: `latest` and `0.5.0` are one."""
    fake = FakeRegistry([], {"latest": INDEX, "0.5.0": INDEX})
    registry = ghcr_audit.Registry("o/p", fake)
    ghcr_audit.closure(registry, ["latest", "0.5.0"])
    assert sum(1 for url in fake.fetched if url.endswith("sha256:amd64")) == 1


def test_closure_refuses_to_skip_an_unreadable_child() -> None:
    """The whole point: a child that will not fetch must fail, never be ignored."""
    fake = FakeRegistry([], {"0.4.0": INDEX}, missing={"sha256:arm64"})
    registry = ghcr_audit.Registry("o/p", fake)
    with pytest.raises(urllib.error.HTTPError):
        ghcr_audit.closure(registry, ["0.4.0"])


def test_audit_flags_a_wheel_with_no_image() -> None:
    fake = FakeRegistry(
        tags=["0.4.0", "main"],
        manifests={"0.4.0": INDEX, "main": INDEX},
        wheels=["0.4.0", "0.4.0b2"],
    )
    registry = ghcr_audit.Registry("o/p", fake)
    violations = ghcr_audit.audit(registry, "podbench", fake)
    assert len(violations) == 1
    assert "0.4.0b2" in violations[0]


def test_audit_is_clean_when_both_invariants_hold() -> None:
    fake = FakeRegistry(
        tags=["0.4.0", "0.4.0b3", "main", "latest", "sha-abc", "0.4.1-beta.1-branch"],
        manifests=dict.fromkeys(("0.4.0", "0.4.0b3", "main", "latest"), INDEX),
        wheels=["0.4.0", "0.4.0b3"],
    )
    registry = ghcr_audit.Registry("o/p", fake)
    assert ghcr_audit.audit(registry, "podbench", fake) == []


def test_audit_refuses_to_pass_when_the_closure_collapses_to_leaves() -> None:
    """A wrong Accept header makes every index answer as a leaf; that is a failure.

    Without this the audit would report "all fetched" having walked nothing,
    which is worse than not running it at all.
    """
    fake = FakeRegistry(tags=["0.4.0", "main"], manifests={}, wheels=["0.4.0"])
    registry = ghcr_audit.Registry("o/p", fake)
    violations = ghcr_audit.audit(registry, "podbench", fake)
    assert len(violations) == 2
    assert all("checked nothing" in violation for violation in violations)


def test_audit_never_walks_an_unprotected_tag() -> None:
    """A branch tag is deletable by design, so its health is not this job's business."""
    fake = FakeRegistry(
        tags=["0.4.0", "0.4.1-beta.1-branch", "sha-abc"],
        manifests={"0.4.0": INDEX},
        wheels=["0.4.0"],
    )
    registry = ghcr_audit.Registry("o/p", fake)
    ghcr_audit.audit(registry, "podbench", fake)
    assert not any("0.4.1-beta.1-branch" in url for url in fake.fetched)
    assert not any("sha-abc" in url for url in fake.fetched)
