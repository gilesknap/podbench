"""What is allowed to arrive on a cold ``uvx podbench`` start, and what is not.

The rule used to be ``dependencies = []``. It is now "the CLI, and nothing else":
typer earns its place because the help a developer reads at 3 a.m. is part of the
product, and it costs one resolve of four small pure-Python wheels on a machine
whose only promised tools are uv, helm, kubectl and VS Code.

What has not changed is the part the rule was really protecting. podbench talks
to Kubernetes by shelling out to ``kubectl``, which is what makes kubeconfig
contexts, exec credential plugins and cloud auth somebody else's problem. A
Kubernetes client library in this list would be that decision quietly reversed,
so this asserts the *whole* direct set rather than a blocklist — a new entry has
to be argued for here before it can ship.

CI proves the cold-start half end-to-end by running the built wheel under ``uvx``
with an empty cache (``_dist.yml``). This asserts the declared metadata, so it
fails in the unit suite, before a wheel is built.
"""

from __future__ import annotations

import re
from importlib.metadata import requires

ALLOWED = {"typer"}
"""Direct runtime requirements, by distribution name. See the module docstring."""


def distribution_name(requirement: str) -> str:
    """The name out of a ``Requires-Dist`` line.

    >>> distribution_name("typer>=0.12")
    'typer'
    >>> distribution_name('Rich [jupyter] ; extra == "pretty"')
    'rich'
    """
    return re.split(r"[<>=!~\[\s;]", requirement, maxsplit=1)[0].lower()


def test_podbench_declares_only_its_cli_at_runtime() -> None:
    # requires() returns None when the metadata carries no Requires-Dist at all,
    # and a list of specifiers otherwise.
    declared = requires("podbench") or []
    assert {distribution_name(text) for text in declared} == ALLOWED
