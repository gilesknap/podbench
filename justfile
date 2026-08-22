# podbench recipes.
#
# This devcontainer exports UV_PROJECT_ENVIRONMENT pointing at a shared cache
# venv keyed to a different project, which silently strips ruff/pyright/pytest
# out from under a `uv run`. Every recipe pins the repo-local venv instead.

export UV_PROJECT_ENVIRONMENT := justfile_directory() + "/.venv"

# List the recipes.
default:
    @just --list

# Create or update the local venv from the lockfile.
sync:
    uv sync --frozen

# Everything CI checks, in one go.
check: lint types test docs

# ruff, and the rest of the pre-commit hooks.
lint:
    uv run --no-sync pre-commit run --all-files

# pyright, over the same paths CI checks.
types:
    uv run --no-sync pyright --pythonpath {{ justfile_directory() }}/.venv/bin/python src tests .github/scripts

# The unit suite. Cluster-free and fast by design.
test:
    uv run --no-sync pytest -q

# Docs, with warnings as errors exactly as CI builds them.
docs:
    rm -rf docs/_build
    uv run --no-sync sphinx-build -EW --keep-going docs docs/_build

# Serve the docs, rebuilding on change.
docs-serve:
    uv run --no-sync sphinx-autobuild docs docs/_build

# The charts, as the helm workflow checks them.
helm:
    helm lint Charts/podbench
    helm lint Charts/podbench-hotfix-claim
    helm template podbench Charts/podbench >/dev/null
    helm template podbench Charts/podbench --set scratchPvc.enabled=true --set rbac.create=true >/dev/null
    # Renders nothing until asked is the subchart's whole contract, so check both ways.
    ! helm template svc Charts/podbench-hotfix-claim | grep -q '^kind:'
    helm template svc Charts/podbench-hotfix-claim --set enabled=true >/dev/null

# The e2e suite. Needs a cluster and a published image, so it is opt-in.
e2e image="ghcr.io/gilesknap/podbench:latest":
    PODBENCH_E2E=1 PODBENCH_IMAGE={{ image }} uv run --no-sync pytest tests/e2e -q
