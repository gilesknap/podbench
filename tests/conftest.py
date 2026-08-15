"""Suite-wide fixtures.

Only one so far, and it exists because of an asymmetry worth stating: the CLI's
output is now rendered by rich, which decides whether to style based on what it
thinks is watching. So the bytes a test asserts on differ between a developer's
terminal and CI.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def plain_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render CLI output unstyled and at a fixed width, for every test.

    typer builds ``--help`` and its usage errors with rich, which styles the
    pieces of a line separately rather than the line as a whole: ``--launch``
    is emitted as a styled ``-`` followed by a styled ``-launch``, and
    ``Usage: podbench`` carries a reset between the two words. A test asserting
    on either substring then fails on the escape codes rather than on the text.

    The reason this needs a fixture rather than a note is *when* it fails.
    Styling is off when rich believes nothing is watching, so these tests pass
    on a developer's machine and fail only where something sets ``FORCE_COLOR``
    — which is CI. That is the worst way round for a check to break.

    ``FORCE_COLOR`` beats ``NO_COLOR`` in rich, so it has to be removed rather
    than countered. ``COLUMNS`` is pinned for the same class of reason in the
    other axis: rich wraps to the terminal width, and a narrow one would fold a
    long usage line through the middle of an asserted string.
    """
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "80")
