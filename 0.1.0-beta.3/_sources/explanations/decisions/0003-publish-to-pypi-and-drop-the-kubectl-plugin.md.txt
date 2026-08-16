# 3. Publish the launcher to PyPI and drop the kubectl plugin

Date: 2026-08-15

## Status

Accepted

## Context

The launcher shipped two console scripts: `podbench`, and `kubectl-podbench` so
that kubectl would route `kubectl podbench <verb>` to it. kubectl discovers a
plugin by scanning `PATH` for a `kubectl-*` executable, so the plugin spelling
only works once something has been installed permanently — which is exactly what
a developer holding a kubeconfig and a broken pod does not want to do. The
plugin therefore could not deliver the ergonomics it existed for.

It also gave the cluster-side verbs two spellings that were not equivalent.
`kubectl podbench` routed `attach`, `ssh-config`, `status` and `list` only,
because the plugin entry point handed argv straight to the launcher's parser,
which has no `dev` or `patch` subparser. Every document that named a verb had to
say which of the two spellings it took, and a reader who guessed wrong got an
argparse `invalid choice` and exit 2.

Meanwhile the cluster-side half already needed no checkout: the chart is
published to `oci://ghcr.io/gilesknap/charts/podbench` on tag. The client half
was the only thing still asking for a clone.

## Decision

Publish the wheel to PyPI on tag, using trusted publishing, and remove the
`kubectl-podbench` entry point. `podbench <verb>` is the only spelling there is;
the canonical invocation is `uvx podbench <verb>`, which resolves the launcher
for one run and leaves nothing installed.

Derive the default image tag from the launcher's own version rather than fixing
it at `latest`, and fall back to `main` — the branch-tip image CI pushes on every
default-branch commit — when the launcher is a dev build that names no published
image. `latest` is not the fallback: CI moves it only on a final release, and a
project that has only ever tagged prereleases would pair a launcher built today
with an image from months ago, or with no image at all.

## Consequences

- Given uv, helm, kubectl and VS Code, a developer can land a debug seat without
  cloning this repository and without installing anything that outlives the
  command. `uvx podbench@<version>` pins it; `uv tool install podbench` is there
  for people who want it on `PATH` anyway.
- A seat outlives the launcher that created it. The generated ssh stanza's
  `ProxyCommand` names `kubectl`, not podbench, so Remote-SSH keeps working after
  the `uvx` process is gone.
- The launcher's version can now change between two invocations with no visible
  event, which is why the image tag follows it: a launcher must not author a
  container spec its image does not understand, and that mismatch fails inside
  the pod, where an ephemeral container cannot be restarted.
- One release has two spellings — SemVer for the git tag, chart and image
  (`1.0.0-beta.1`), PEP 440 for the wheel (`1.0.0b1`). CI pushes both as tags on
  the same image digest so the launcher can pass its own version through
  verbatim rather than translating between them on the launch path.
- A bare `uvx podbench` will not resolve a prerelease, and CI moves `latest` only
  on a final release. Beta testers must ask for the PEP 440 spelling explicitly,
  as `uvx podbench@1.0.0b1`.
- The PyPI upload is gated on the image job, because the wheel's version *is* the
  image tag it will ask for and a PyPI release cannot be withdrawn and re-cut. A
  wheel published while its image was missing would name an image that does not
  exist, for good.
- Images published before this decision carry the SemVer spelling only
  (`0.1.0-alpha.6`), so installing from one of those git tags yields a launcher
  asking for `0.1.0a6`. Either backfill the PEP 440 tags onto those digests with
  `docker buildx imagetools create`, or pass `--image` when running an old tag.
