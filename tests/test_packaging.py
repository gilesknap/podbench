"""The packaging half of the "no runtime dependencies" rule.

The rule exists so the launcher inherits kubeconfig auth by shelling out to
``kubectl`` rather than reimplementing it, and the pyproject line that encodes it
(``dependencies = []``) is easy to relax without any test noticing. What relaxing
it breaks is ``uvx podbench``: a dependency is a resolve and a download on every
cold start, on a machine whose only promised tools are uv, helm, kubectl and VS
Code.

CI proves this end-to-end by running the built wheel under ``uvx`` with an empty
cache (``_dist.yml``). This asserts the same property against the *installed
metadata*, so it fails in the unit suite, before a wheel is built.
"""

from __future__ import annotations

from importlib.metadata import requires


def test_podbench_declares_no_runtime_dependencies() -> None:
    # requires() returns None when the metadata carries no Requires-Dist at all,
    # and a list of specifiers otherwise; both spellings mean "nothing to install".
    assert requires("podbench") in (None, [])
