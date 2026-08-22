---
name: ghcr-registry
description: What podbench's published container package is actually made of, and the ways reading or pruning it silently lies to you — an expired pull token that answers 404, seven manifests per push of which one is tagged, and both spellings of a release sharing one deletable version. Read before touching .github/scripts/ghcr_audit.py or ghcr_prune.py, before deleting anything from the registry, or before reasoning about which image a released launcher can pull.
---

# The published registry

`ghcr.io/gilesknap/podbench` is not a list of tags. It is a graph of manifests
that tags point into, and almost every intuition about it is wrong in a way that
destroys a release quietly rather than loudly. `.github/scripts/ghcr_audit.py`
(read-only, weekly) and `.github/scripts/ghcr_prune.py` (the reaper) encode what
follows; both are covered by pyright via `pyproject.toml` and the `types` recipe
in `justfile`, which name `.github/scripts` explicitly.

## One push publishes seven manifests and tags one

`_container.yml` builds each architecture on its own runner and pushes it
**by digest** (`push-by-digest=true`), then a separate `merge` job assembles the
tagged index. Per architecture that is an OCI *index* wrapping an image manifest
**plus a SLSA provenance attestation** — `docker/build-push-action` attaches
provenance by default — so:

| | count | tagged? |
|---|---|---|
| per-arch wrapper index | 2 | no |
| per-arch image manifest | 2 | no |
| per-arch attestation manifest | 2 | no |
| merged index, flattening the four leaves | 1 | **yes** |

An attestation child is recognisable by `platform.architecture: unknown` and a
`vnd.docker.reference.type: attestation-manifest` annotation.

**So six of every seven manifests are untagged, and four of them are load
bearing.** Every "delete untagged versions" recipe on the internet deletes a live
index's children while leaving the tag in place; the tag then resolves and the
pull 404s halfway through. Measured 2026-08-21: 30 protected tags reach 66
untagged descendants.

GHCR does **not** serve the OCI referrers API (`/v2/<name>/referrers/<digest>`
returns `MANIFEST_UNKNOWN`), and podbench publishes no cosign `.sig`/`.att` tags,
so an index's `manifests` array is the whole child set. Nothing else to walk.

## An expired pull token answers 404, not 401

This is the one that will cost you an afternoon. ghcr.io's anonymous pull token
carries **no `expires_in`** and lasts on the order of minutes. A walk of this
package is thousands of requests, so it *will* outlive one — and a registry will
not confirm that a manifest exists to a client that cannot read it, so the
expired token comes back as **404**.

At the call site that is indistinguishable from the single thing this tooling
exists to detect: a manifest that is genuinely gone. Re-authenticate and retry on
any 401/403/404, and believe only the second answer.

The same walk also dies on transient DNS failures and on ghcr.io answering
429/5xx under the request rate. Neither says anything about the registry's
contents, so neither may be allowed to look like it does — retry with backoff.

## Both spellings of a release are one deletable object

GHCR's unit of deletion is the **version** (a manifest), and one version carries
many tags. `0.1.0-beta.3` and `0.1.0b3` are one version, as are `0.5.0` and
`latest`. You cannot delete the SemVer tag without the PEP 440 one, and the PEP
440 one is what a launcher asks for: `podbench.model.image_tag_for` passes a
version through **verbatim**, so a published wheel *names* an image tag by
construction.

That coupling is why the reaper will not touch releases, and why deleting a
prerelease image is a decision about PyPI as much as about the registry.

## The keep set is seeded from survivors, not from releases

When pruning, the set of manifests that must live is the closure of **everything
that survives this sweep** — not of the protected tags. A live branch's image is
spared because its branch exists, while its four children carry no release tag at
all; a keep set seeded from releases alone spares the index and deletes the
layers underneath it.

Two further rules in `ghcr_prune.orphans`:

* **An untagged index whose children are live is held back.** The per-arch
  wrapper is unreferenced garbage whose children the merged index still uses.
  GHCR is not documented to cascade a version delete to an index's children and
  no evidence was found that it does — but it has never been tested against a
  real package, and this is the one shape where a cascade takes a release's arch
  manifest with it. Proving it needs a throwaway package: push a two-arch image,
  delete the wrapper index by id, confirm the tagged index still pulls.
* **Nothing inside the age window is a candidate.** That covers a *publish in
  flight*, where the per-arch digests exist for minutes before the merge job
  creates the index referencing them. In that gap they are indistinguishable
  from garbage.

## Tokens

* **A fine-grained PAT cannot use the Packages API at all** — it 403s on even
  *listing* versions. The devcontainer's `gh` is authenticated with one, so
  package work cannot be done from a session there.
* **`GITHUB_TOKEN` with `permissions: packages: write` can delete versions.**
  Measured 2026-08-22: it deleted 1413 versions of this package. The docs imply a
  classic PAT is required for a user-owned package; for this repo-linked package
  it is not. Do not create a PAT before testing this.
* Outside Actions, `gh auth refresh -s read:packages,delete:packages` adds the
  scopes when `gh` was logged in through a browser. It cannot upgrade a
  `github_pat_` login.
* **SSH keys are irrelevant here.** The Packages REST API takes tokens only; an
  SSH key authenticates the git transport and nothing else.
* A public package version with **more than 5,000 downloads cannot be deleted**
  through the API at all. Collect such failures and continue rather than
  aborting, or one stuck version wedges every future run.

## Storage is free, so cleanup is not about cost

GitHub Packages storage **and** transfer are free for public packages, and this
one is public. Pruning buys a navigable UI and a shorter API walk, not money.
Weigh that against the failure mode, which is an `ImagePullBackOff` inside an
ephemeral container that cannot be restarted.

GHCR keeps a deleted version restorable for **30 days**, provided the namespace
has not been reused. That window is why the audit runs weekly rather than
monthly: it turns a mistake into a restore.

## PyPI has no write API

Yank/unyank/delete exist only in the web UI — the published APIs are index, JSON,
upload, integrity, stats and RSS. Ten releases is ten clicks at
`https://pypi.org/manage/project/podbench/release/<version>/`.

And yanking is weaker than it looks. PEP 592: *"Yanked files are always ignored,
unless they are the only file that matches a version specifier that pins to an
exact version"*. So `podbench==0.1.0b1` still installs a yanked `0.1.0b1`. Yanking
stops range resolution and adds an installer warning; it does not close the
exact-pin case, which is the only case that breaks when the image is gone.

## A checker whose failure mode is a green tick

The audit asserts a third thing that guards the other two: **a protected tag
resolving to a leaf is a violation.** Every image here is a manifest list, so a
childless one means the wrong `Accept` header went out and ghcr.io answered with
the runner's own architecture — at which point the closure collapses to the seeds
and the walk reports success having checked nothing.

Offer every media type you will accept — OCI index, Docker manifest list, OCI
image, Docker image — or the registry picks for you and hides half the graph.
