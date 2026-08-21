"""Delete the images a branch generated, once that branch is gone.

Every push publishes, so a merged branch leaves its images behind for ever: at the
time of writing 66 branch-slug tags and 238 `sha-` tags, over 258 distinct
manifests, and nothing has ever removed one. This reaps them, and nothing else.

**What it will delete**, and the three conditions are all required:

1. Every tag on the version is *generated* — a branch slug or `sha-<sha>`. A
   version carrying any release tag, `latest` or `main` is never a candidate, and
   neither is an untagged manifest inside a protected image (see below).
2. No live branch still claims it. A branch-slug tag whose branch exists is the
   image somebody is testing right now.
3. It is more than `--min-age-hours` old, default 24. That is the window for
   testing an image whose branch was deleted moments after the merge - and it is
   also what keeps the reaper away from a *publish in flight*, where
   `_container.yml`'s per-arch digests are pushed minutes before the merge job
   creates the index that references them. In that gap the children are untagged
   and unreferenced, which is indistinguishable from being garbage.

**What protects an image** is not its tag but its whole manifest closure. A
podbench image is an index over four untagged children - two per-arch manifests
and their two SLSA attestations - so deleting "untagged versions" without walking
indexes destroys live releases while leaving their tags in place. The keep set is
therefore computed by resolving every protected tag to its descendants, exactly as
`ghcr_audit` does, and that module is imported rather than reimplemented so the
two can never disagree about what "protected" means.

Two independent brakes, because the failure is unrecoverable in the place it
lands - an `ImagePullBackOff` inside an ephemeral container that cannot be
restarted:

* **Dry run is the default.** `--apply` is required to delete anything.
* **The audit runs afterwards** and fails the job if any protected tag or any of
  its descendants stopped resolving. A sweep that broke something says so in the
  same run, while GHCR's 30-day restore window is open.

Needs a token that can delete package versions, which the workflow's own
`GITHUB_TOKEN` may not be for a user-owned package - see the workflow for how
that is supplied and what it does when the token is refused.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import ghcr_audit

API = "https://api.github.com"


@dataclass(frozen=True)
class Version:
    """One GHCR package version: a manifest, its id, its tags and its age."""

    identifier: int
    digest: str
    tags: tuple[str, ...]
    updated: dt.datetime

    @property
    def generated(self) -> bool:
        """True when every tag on it was minted by a branch build.

        A version with no tags at all is *not* generated - it is an untagged
        manifest, which is either a live image's child or an orphan, and telling
        those apart is the closure's job rather than this predicate's.

        >>> at = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        >>> Version(1, "sha256:a", ("0.5.0-beta.1-mine", "sha-abc"), at).generated
        True
        >>> Version(2, "sha256:b", ("0.5.0", "0.5.0b1"), at).generated
        False
        >>> Version(3, "sha256:c", (), at).generated
        False
        """
        return bool(self.tags) and not any(
            ghcr_audit.is_protected(tag) for tag in self.tags
        )


def api(url: str, token: str, method: str = "GET") -> Any:
    request = urllib.request.Request(url, method=method)  # noqa: S310
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        if response.status == 204 or method == "DELETE":
            return None
        return json.load(response)


def versions(owner: str, package: str, token: str) -> list[Version]:
    """Every version of the package, following the paginated listing to the end.

    A truncated listing would silently under-delete, which is the harmless
    direction - but it would also under-*protect* if it were ever used to build a
    keep set, so the keep set comes from the registry instead.
    """
    found: list[Version] = []
    page = 1
    while True:
        batch = api(
            f"{API}/users/{owner}/packages/container/{package}/versions"
            f"?per_page=100&page={page}",
            token,
        )
        if not batch:
            return found
        for entry in batch:
            container = entry.get("metadata", {}).get("container", {})
            found.append(
                Version(
                    identifier=int(entry["id"]),
                    digest=str(entry["name"]),
                    tags=tuple(str(t) for t in container.get("tags", [])),
                    updated=dt.datetime.fromisoformat(
                        str(entry["updated_at"]).replace("Z", "+00:00")
                    ),
                )
            )
        page += 1


def live_branches(owner: str, repository: str, token: str) -> set[str]:
    """Branch names as `_container.yml` would slugify them into a tag."""
    names: set[str] = set()
    page = 1
    while True:
        batch = api(
            f"{API}/repos/{owner}/{repository}/branches?per_page=100&page={page}",
            token,
        )
        if not batch:
            return names
        names.update(slugify(str(entry["name"])) for entry in batch)
        page += 1


def slugify(branch: str) -> str:
    """Fold a branch name the way `_container.yml` does before tagging with it.

    Kept identical to the shell there deliberately: this is the only thing tying
    a published tag back to the branch that made it, so a divergence would let a
    live branch's image be read as abandoned.

    >>> slugify("feat/debug-config")
    'feat-debug-config'
    >>> slugify("Claude/Reload_After")
    'claude-reload_after'
    """
    # An explicit ASCII set rather than `str.isalnum`, which is Unicode-aware
    # where `sed`'s `[a-z0-9]` is not: a branch named `caf\xe9` keeps its accent
    # under `isalnum` and loses it under the shell, and the two would then
    # disagree about which tag that branch owns.
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789._-"
    folded = "".join(
        character if character in allowed else "-" for character in branch.lower()
    )
    while "--" in folded:
        folded = folded.replace("--", "-")
    return folded.strip("-._")[:60]


def claimed_by_a_live_branch(version: Version, branches: Iterable[str]) -> bool:
    """Whether a branch that still exists would publish to one of these tags.

    Matched by suffix because the tag is `<version>-<slug>` and only the slug half
    names the branch - `0.4.1-beta.1-claude-my-branch` for `claude/my-branch`.

    >>> at = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    >>> live = ["claude-mine"]
    >>> mine = Version(1, "d", ("0.4.1-beta.1-claude-mine",), at)
    >>> theirs = Version(2, "d", ("0.4.1-beta.1-claude-theirs",), at)
    >>> claimed_by_a_live_branch(mine, live), claimed_by_a_live_branch(theirs, live)
    (True, False)
    """
    return any(tag.endswith(f"-{slug}") for tag in version.tags for slug in branches)


def candidates(
    all_versions: Sequence[Version],
    branches: Iterable[str],
    protected: frozenset[str],
    cutoff: dt.datetime,
) -> list[Version]:
    """The versions that satisfy every condition in this module's docstring."""
    branches = list(branches)
    return [
        version
        for version in all_versions
        if version.generated
        and version.digest not in protected
        and version.updated < cutoff
        and not claimed_by_a_live_branch(version, branches)
    ]


def main(argv: list[str] | None = None) -> int:  # noqa: C901
    parser = argparse.ArgumentParser(description="Reap generated container tags.")
    parser.add_argument("--owner", default="gilesknap")
    parser.add_argument("--repository", default="podbench")
    parser.add_argument("--package", default="podbench")
    parser.add_argument("--token", required=True)
    parser.add_argument("--min-age-hours", type=int, default=24)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete; without it nothing is removed",
    )
    args = parser.parse_args(argv)

    if args.min_age_hours < 1:
        # Below an hour the window stops covering a publish in flight, where the
        # per-arch digests exist minutes before the index that references them.
        print("::error::--min-age-hours must be at least 1")
        return 2

    registry = ghcr_audit.Registry(f"{args.owner}/{args.package}")
    try:
        registry.authenticate()
        inventory = ghcr_audit.classify(registry.tags())
        keep = ghcr_audit.closure(registry, sorted(inventory.protected))
        all_versions = versions(args.owner, args.package, args.token)
        branches = live_branches(args.owner, args.repository, args.token)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            print(
                f"::error::the token cannot read the package or the repository "
                f"({error.code}). A user-owned package needs a classic PAT with "
                f"`read:packages` and `delete:packages`; see the workflow."
            )
            return 2
        print(f"::error::could not build the keep set: {error!r}")
        return 2
    except (urllib.error.URLError, KeyError, ValueError) as error:
        print(f"::error::could not build the keep set: {error!r}")
        return 2

    protected = frozenset(digest for digest in keep if digest.startswith("sha256:"))
    # Refuse to run at all on a keep set that looks collapsed. Every protected tag
    # here is a manifest list, so the descendants must outnumber the tags; if they
    # do not, the walk failed and every untagged manifest looks like an orphan.
    if len(protected) < len(inventory.protected):
        print(
            f"::error::keep set is {len(protected)} descendants for "
            f"{len(inventory.protected)} protected tags, which is too few for "
            f"images that are all manifest lists. Refusing to delete anything."
        )
        return 2

    cutoff = dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=args.min_age_hours)
    doomed = candidates(all_versions, branches, protected, cutoff)

    print(
        f"{len(all_versions)} versions, {len(branches)} live branches, "
        f"{len(protected)} manifests protected by {len(inventory.protected)} tags"
    )
    for version in sorted(doomed, key=lambda v: v.updated):
        age = (cutoff - version.updated).days
        print(
            f"  delete {version.digest[:19]} {list(version.tags)} (+{age}d past cutoff)"
        )
    print(f"{len(doomed)} of {len(all_versions)} versions are candidates")

    if not args.apply:
        print("::notice::dry run - nothing deleted. Pass --apply to delete.")
        return 0

    deleted = 0
    failures: list[str] = []
    for version in doomed:
        url = (
            f"{API}/users/{args.owner}/packages/container/{args.package}"
            f"/versions/{version.identifier}"
        )
        try:
            api(url, args.token, method="DELETE")
            deleted += 1
        except urllib.error.HTTPError as error:
            if error.code == 404:
                # Idempotent: gone is the outcome we wanted. A retried DELETE
                # whose first response was lost lands here.
                deleted += 1
                continue
            # Collected rather than raised, so one undeletable version cannot
            # halt every future run - a public version past GHCR's download
            # ceiling returns a permanent 403 and would otherwise sort to the
            # front for ever.
            failures.append(f"{version.digest[:19]} {list(version.tags)}: {error.code}")

    print(f"deleted {deleted} versions")
    for failure in failures:
        print(f"::warning::could not delete {failure}")

    # The independent check. Anything this run broke is visible here, in this run.
    print("re-auditing the protected closure after the sweep")
    violations = ghcr_audit.audit(
        ghcr_audit.Registry(f"{args.owner}/{args.package}"), args.package
    )
    for violation in violations:
        print(f"::error::{violation}")
    if violations:
        print("::error::the sweep broke a protected image; restore it from GHCR")
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
