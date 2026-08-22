"""Unit tests for the generated-tag reaper.

Everything that decides whether a version dies is a pure function, and that is
deliberate: the safety of this script is entirely in `candidates`, so it is
testable without a token, a network, or a package to destroy.
"""

from __future__ import annotations

import datetime as dt
import doctest
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).parent.parent / ".github" / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# `ghcr_prune` imports `ghcr_audit` as a sibling, which works because running the
# script puts `.github/scripts` on `sys.path[0]`. Load it first so that import
# resolves here too.
ghcr_audit = _load("ghcr_audit")
ghcr_prune = _load("ghcr_prune")

NOW = dt.datetime(2026, 8, 21, 12, 0, tzinfo=dt.UTC)
CUTOFF = NOW - dt.timedelta(hours=24)
OLD = NOW - dt.timedelta(days=30)
FRESH = NOW - dt.timedelta(hours=1)


def version(
    identifier: int,
    tags: tuple[str, ...],
    updated: dt.datetime = OLD,
    digest: str | None = None,
) -> Any:
    return ghcr_prune.Version(
        identifier=identifier,
        digest=digest or f"sha256:{identifier:064x}",
        tags=tags,
        updated=updated,
    )


def test_doctests_execute() -> None:
    results = doctest.testmod(ghcr_prune)
    assert results.failed == 0, f"{results.failed} of {results.attempted} failed"


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("feat/debug-config", "feat-debug-config"),
        (
            "claude/reload-after-a-seat-side-install",
            "claude-reload-after-a-seat-side-install",
        ),
        ("UPPER/Case", "upper-case"),
        ("weird//slashes", "weird-slashes"),
        ("-leading-and-trailing-", "leading-and-trailing"),
    ],
)
def test_slugify_matches_the_container_workflow(branch: str, expected: str) -> None:
    assert ghcr_prune.slugify(branch) == expected


def test_slugify_truncates_at_sixty_characters() -> None:
    """`_container.yml` cuts at 60; a longer branch must still match its own tag."""
    assert len(ghcr_prune.slugify("a" * 200)) == 60


def test_a_release_version_is_never_generated() -> None:
    assert not version(1, ("0.5.0", "0.5.0b1")).generated
    assert not version(2, ("latest", "0.5.0")).generated
    assert not version(3, ("main", "sha-abc")).generated


def test_an_untagged_version_is_not_generated() -> None:
    """Untagged is the closure's business, not the tag predicate's."""
    assert not version(4, ()).generated


def test_a_branch_version_is_generated() -> None:
    assert version(5, ("0.4.1-beta.1-claude-my-branch", "sha-abc")).generated


def test_candidates_spares_a_live_branch() -> None:
    live = version(1, ("0.4.1-beta.1-claude-alive", "sha-aaa"))
    dead = version(2, ("0.4.1-beta.1-claude-merged", "sha-bbb"))
    picked = ghcr_prune.candidates([live, dead], ["claude-alive"], frozenset(), CUTOFF)
    assert picked == [dead]


def test_candidates_spares_anything_inside_the_protected_closure() -> None:
    """The branch tip that became a release shares its digest with the release."""
    shared = version(1, ("0.4.1-beta.1-claude-tip", "sha-aaa"), digest="sha256:beef")
    picked = ghcr_prune.candidates([shared], [], frozenset({"sha256:beef"}), CUTOFF)
    assert picked == []


def test_candidates_spares_anything_inside_the_age_window() -> None:
    young = version(1, ("0.4.1-beta.1-claude-merged",), updated=FRESH)
    assert ghcr_prune.candidates([young], [], frozenset(), CUTOFF) == []


def test_candidates_spares_untagged_manifests_entirely() -> None:
    """A per-arch child of a live index is untagged; never a candidate here."""
    child = version(1, ())
    assert ghcr_prune.candidates([child], [], frozenset(), CUTOFF) == []


def test_candidates_takes_a_superseded_build_of_a_live_branch() -> None:
    """A branch tag moves on every push, orphaning the previous build's sha- tag.

    174 of the 238 `sha-` tags measured on 2026-08-21 were in exactly this state,
    so this is the bulk of what the reaper is for.
    """
    superseded = version(1, ("sha-old",))
    current = version(2, ("0.4.1-beta.1-claude-alive", "sha-new"))
    picked = ghcr_prune.candidates(
        [superseded, current], ["claude-alive"], frozenset(), CUTOFF
    )
    assert picked == [superseded]


def test_candidates_spares_every_release_even_with_no_branches_and_no_closure() -> None:
    """Belt and braces: the tag predicate alone must already refuse a release."""
    releases = [
        version(1, ("0.1.0-alpha.1",)),
        version(2, ("0.1.0-beta.1", "0.1.0b1")),
        version(3, ("0.5.0", "latest")),
        version(4, ("main",)),
    ]
    assert ghcr_prune.candidates(releases, [], frozenset(), CUTOFF) == []


class FakeRegistry:
    """Serves manifests by digest, so the orphan walk can be driven offline."""

    def __init__(self, manifests: dict[str, Any]) -> None:
        self.manifests = manifests
        self.fetched: list[str] = []

    def manifest(self, reference: str) -> Any:
        self.fetched.append(reference)
        return self.manifests.get(reference, {"config": {}, "layers": []})


def index(*children: str) -> dict[str, Any]:
    return {"manifests": [{"digest": child} for child in children]}


def test_orphans_takes_an_untagged_leaf_nothing_points_at() -> None:
    junk = version(1, (), digest="sha256:junk")
    doomed, held = ghcr_prune.orphans(FakeRegistry({}), [junk], frozenset(), CUTOFF)
    assert doomed == [junk]
    assert held == []


def test_orphans_spares_a_child_of_a_surviving_image() -> None:
    """The whole hazard: a release's arch layer is untagged but load-bearing."""
    layer = version(1, (), digest="sha256:layer")
    doomed, held = ghcr_prune.orphans(
        FakeRegistry({}), [layer], frozenset({"sha256:layer"}), CUTOFF
    )
    assert doomed == []
    assert held == []


def test_orphans_spares_a_child_of_a_spared_live_branch_image() -> None:
    """A live branch's image carries no release tag, so its layers survive only
    because the keep set is seeded from survivors rather than from releases."""
    layer = version(1, (), digest="sha256:branchlayer")
    keep = frozenset({"sha256:branchindex", "sha256:branchlayer"})
    doomed, _ = ghcr_prune.orphans(FakeRegistry({}), [layer], keep, CUTOFF)
    assert doomed == []


def test_orphans_holds_back_a_wrapper_index_whose_children_are_live() -> None:
    """The per-arch wrapper is garbage, but a cascade would take a live layer."""
    wrapper = version(1, (), digest="sha256:wrapper")
    registry = FakeRegistry({"sha256:wrapper": index("sha256:live", "sha256:att")})
    doomed, held = ghcr_prune.orphans(
        registry, [wrapper], frozenset({"sha256:live"}), CUTOFF
    )
    assert doomed == []
    assert held == [wrapper]


def test_orphans_takes_a_wrapper_index_whose_children_are_all_dead() -> None:
    wrapper = version(1, (), digest="sha256:wrapper")
    registry = FakeRegistry({"sha256:wrapper": index("sha256:dead1", "sha256:dead2")})
    doomed, held = ghcr_prune.orphans(registry, [wrapper], frozenset(), CUTOFF)
    assert doomed == [wrapper]
    assert held == []


def test_orphans_never_touches_a_tagged_version() -> None:
    """Tagged versions are `candidates`' business; a double claim would be a bug."""
    tagged = version(1, ("0.5.0",), digest="sha256:release")
    doomed, held = ghcr_prune.orphans(FakeRegistry({}), [tagged], frozenset(), CUTOFF)
    assert doomed == []
    assert held == []


def test_orphans_spares_anything_inside_the_age_window() -> None:
    """A publish in flight has pushed its layers but not yet the index."""
    inflight = version(1, (), updated=FRESH, digest="sha256:inflight")
    doomed, _ = ghcr_prune.orphans(FakeRegistry({}), [inflight], frozenset(), CUTOFF)
    assert doomed == []


def test_orphans_does_not_fetch_a_manifest_it_has_already_ruled_out() -> None:
    """Kept, young and tagged versions must cost no registry round trip."""
    registry = FakeRegistry({})
    versions = [
        version(1, (), digest="sha256:kept"),
        version(2, (), updated=FRESH, digest="sha256:young"),
        version(3, ("0.5.0",), digest="sha256:tagged"),
    ]
    ghcr_prune.orphans(registry, versions, frozenset({"sha256:kept"}), CUTOFF)
    assert registry.fetched == []
