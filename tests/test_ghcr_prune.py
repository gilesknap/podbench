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
