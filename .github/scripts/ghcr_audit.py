"""Prove the published registry still serves every image a launcher can ask for.

CI publishes on every push, so the package accumulates: 334 tags over 258 distinct
manifests six days after the first commit. Storage is not the reason this exists —
GitHub Packages is free for public packages, and podbench's is public, so the
measured cost of that accumulation is £0 and a cluttered web UI.

The reason is the *other* direction. A tag that stops resolving fails at pull
time, inside an ephemeral container that cannot be restarted, and the launcher
gives that pod's seat name away for the life of the pod. GHCR keeps a deleted
version restorable for 30 days, so a weekly check is the difference between a
mistake that is recoverable and one that is not. Two things can cause it: a
retention sweep that did not understand multi-arch (see below), and a release
whose image half never published.

Both invariants are asserted here, and neither needs delete rights:

* **Every version on PyPI resolves to a pullable image.** `image_tag_for` passes
  a launcher's own version through verbatim, so a published wheel *names* an
  image tag by construction. `ci.yml` gates the PyPI upload on the container job
  to keep the two halves in step; this is the same invariant checked after the
  fact, against what actually shipped, rather than against what the pipeline
  believed it was doing.

* **Every protected tag's whole manifest closure resolves.** A multi-arch tag is
  an index whose children are untagged, so `docker pull` needs four more
  manifests than the tag names. Anything that deletes "untagged versions" without
  walking indexes takes those children while leaving the tag in place, and the
  result is a tag that resolves and an image that 404s halfway through a pull.
  Fetching the tag is therefore not a check; fetching its descendants is.

Run it against any repository::

    uv run python .github/scripts/ghcr_audit.py

Read-only by construction: nothing here holds a credential that could delete a
version, and the registry half authenticates with an anonymous pull token.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from podbench.model import FLOATING_TAG, IMAGE_REPOSITORY
from podbench.spec import moves

# `latest` and `main` move, so `moves()` correctly calls them mutable — but they
# are the two tags an unpinned user and a source-built launcher respectively
# pull, so losing either is a user-visible outage. Protection and immutability
# are different questions and this is the only place they diverge.
FLOATING_TAGS = frozenset({FLOATING_TAG, "latest"})

# A manifest request must name every media type it is willing to accept, or the
# registry answers a multi-arch reference with a per-arch manifest chosen for the
# *client's* platform - which on an x86 runner silently hides the arm64 half of
# every index this script exists to walk.
ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)

Opener = Callable[[urllib.request.Request], Any]
"""Injected so the tests can exercise the walk without reaching the network."""


def _open(request: urllib.request.Request) -> Any:
    return urllib.request.urlopen(request, timeout=30)  # noqa: S310


@dataclass(frozen=True)
class Inventory:
    """Every published tag, split by what publishes it and what protects it."""

    releases: tuple[str, ...]
    floating: tuple[str, ...]
    shas: tuple[str, ...]
    branches: tuple[str, ...]

    @property
    def protected(self) -> tuple[str, ...]:
        return self.releases + self.floating

    @property
    def total(self) -> int:
        return len(self.releases + self.floating + self.shas + self.branches)


def is_protected(tag: str) -> bool:
    """Whether losing this tag would break somebody who is not us.

    Asked of `podbench.spec.moves` rather than of a regex of this script's own,
    because the launcher already answers exactly this question when it decides
    whether an image reference can be cached - and a second spelling of the rule
    is a second thing to keep in step.

    >>> is_protected("0.4.0"), is_protected("0.4.0b3"), is_protected("main")
    (True, True, True)
    >>> is_protected("0.4.1-beta.1-claude-my-branch")
    False
    """
    return tag in FLOATING_TAGS or not moves(f"{IMAGE_REPOSITORY}:{tag}")


def classify(tags: Iterable[str]) -> Inventory:
    """Sort published tags into the four kinds `_container.yml` emits.

    >>> inv = classify(["0.4.0", "main", "sha-abc", "0.4.1-beta.1-my-branch"])
    >>> inv.releases, inv.floating, inv.shas, inv.branches
    (('0.4.0',), ('main',), ('sha-abc',), ('0.4.1-beta.1-my-branch',))
    """
    releases: list[str] = []
    floating: list[str] = []
    shas: list[str] = []
    branches: list[str] = []
    for tag in sorted(tags):
        if tag in FLOATING_TAGS:
            floating.append(tag)
        elif is_protected(tag):
            releases.append(tag)
        elif tag.startswith("sha-"):
            shas.append(tag)
        else:
            branches.append(tag)
    return Inventory(tuple(releases), tuple(floating), tuple(shas), tuple(branches))


class Registry:
    """The subset of the OCI distribution API this audit needs, read-only."""

    def __init__(self, repository: str, opener: Opener = _open) -> None:
        self._repository = repository
        self._opener = opener
        self._token: str | None = None

    def _get(self, url: str, accept: str) -> Any:
        request = urllib.request.Request(url)  # noqa: S310
        request.add_header("Accept", accept)
        if self._token and url.startswith("https://ghcr.io/v2/"):
            request.add_header("Authorization", f"Bearer {self._token}")
        return json.load(self._opener(request))

    def authenticate(self) -> None:
        """Take an anonymous pull token; it grants read on a public package only."""
        url = (
            f"https://ghcr.io/token?scope=repository:{self._repository}:pull"
            "&service=ghcr.io"
        )
        self._token = str(self._get(url, "application/json")["token"])

    def tags(self) -> list[str]:
        url = f"https://ghcr.io/v2/{self._repository}/tags/list?n=1000"
        return [str(t) for t in self._get(url, "application/json")["tags"]]

    def manifest(self, reference: str) -> Mapping[str, Any]:
        url = f"https://ghcr.io/v2/{self._repository}/manifests/{reference}"
        return self._get(url, ACCEPT)


def children(manifest: Mapping[str, Any]) -> list[str]:
    """The digests an index references, or nothing for a leaf manifest.

    GHCR does not serve the OCI referrers API and podbench publishes no cosign
    fallback tags, so an index's `manifests` array is the whole child set - which
    for a podbench image is two per-arch manifests and their two SLSA provenance
    attestations, the latter carrying `platform.architecture: unknown`.

    >>> children({"manifests": [{"digest": "sha256:aa"}, {"digest": "sha256:bb"}]})
    ['sha256:aa', 'sha256:bb']
    >>> children({"config": {}, "layers": []})
    []
    """
    return [str(entry["digest"]) for entry in manifest.get("manifests", [])]


def closure(registry: Registry, seeds: Iterable[str]) -> dict[str, list[str]]:
    """Every manifest reachable from `seeds`, mapped to the digests it references.

    A fetch that fails propagates rather than being skipped: an unreadable
    descendant is precisely the damage this walk exists to find, so swallowing
    the error would turn the check into the thing it is guarding against.
    """
    reached: dict[str, list[str]] = {}
    pending = list(seeds)
    while pending:
        reference = pending.pop()
        if reference in reached:
            continue
        kids = children(registry.manifest(reference))
        reached[reference] = kids
        pending.extend(kids)
    return reached


def pypi_versions(project: str, opener: Opener = _open) -> list[str]:
    """Every version of `project` that has ever been uploaded to PyPI."""
    request = urllib.request.Request(f"https://pypi.org/pypi/{project}/json")  # noqa: S310
    request.add_header("Accept", "application/json")
    return sorted(json.load(opener(request))["releases"])


def audit(registry: Registry, project: str, opener: Opener = _open) -> list[str]:
    """Run both invariants and return the violations, empty when all is well."""
    registry.authenticate()
    inventory = classify(registry.tags())
    published = set(inventory.releases) | set(inventory.floating)
    violations: list[str] = []

    # Invariant one, and the only one whose failure is already in users' hands:
    # a wheel on PyPI names an image tag verbatim, so a missing image here is an
    # ImagePullBackOff for anyone who installed that version.
    wheels = pypi_versions(project, opener)
    for version in wheels:
        if version not in published:
            violations.append(
                f"PyPI has {project} {version} but the registry has no such "
                f"image tag: `image_tag_for` passes a launcher's version through "
                f"verbatim, so that release cannot attach at all."
            )

    # Invariant two. Seeded from the tags rather than from a digest list, because
    # the failure being hunted is one where the tag survives and its children do
    # not - so the walk has to start where a `docker pull` starts.
    reached = closure(registry, sorted(published))

    # Invariant three, which guards the other two rather than the registry. Every
    # image this project publishes is assembled by `_container.yml`'s merge job
    # into a manifest list, so a protected tag that resolves to a *leaf* is not a
    # single-arch release - it means this script asked the registry a question it
    # did not mean to ask, and the walk below it checked nothing. The way that
    # happens is the `Accept` header: offer the wrong media types and ghcr.io
    # answers an index request with the per-arch manifest for the *runner's*
    # platform, which has no children, so the closure collapses to the seeds and
    # the audit passes vacuously while reporting that all is well. Measured
    # 2026-08-21: all 30 protected tags are indexes, the six earliest alphas
    # carrying two children and everything since 0.1.0-alpha.6 carrying four.
    for tag in sorted(published):
        if not reached[tag]:
            violations.append(
                f"protected tag {tag} resolved to a single manifest with no "
                f"children. Every podbench image is a manifest list, so this is "
                f"an audit that checked nothing rather than a registry that lost "
                f"something - suspect the Accept header or a change at ghcr.io."
            )

    # Counted apart rather than summed: `reached` is keyed by *reference*, and a
    # tag reference is not a manifest - `latest` and `0.5.0` name one digest, as
    # does every SemVer/PEP 440 pair. Adding the two would report more manifests
    # than the registry holds, which is the wrong direction for a number whose
    # whole job is to make a shrinking closure visible.
    descendants = [
        reference for reference in reached if reference.startswith("sha256:")
    ]
    print(
        f"tags {inventory.total}: {len(inventory.releases)} release, "
        f"{len(inventory.floating)} floating, {len(inventory.shas)} sha, "
        f"{len(inventory.branches)} branch"
    )
    print(
        f"protected closure: {len(published)} tags resolved, "
        f"{len(descendants)} distinct descendant manifests fetched"
    )
    print(f"PyPI releases checked: {len(wheels)}")
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=IMAGE_REPOSITORY.split("/", 1)[1])
    parser.add_argument("--project", default="podbench")
    args = parser.parse_args(argv)

    try:
        violations = audit(Registry(args.repository), args.project)
    except (urllib.error.URLError, KeyError, ValueError) as error:
        # Loud, because the alternative reading of a silent audit is "nothing is
        # wrong" and this one cannot tell the difference.
        print(f"::error::the registry audit could not complete: {error!r}")
        return 2

    for violation in violations:
        print(f"::error::{violation}")
    if violations:
        print(f"::error::{len(violations)} registry invariant(s) violated")
        return 1
    print("registry audit clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
