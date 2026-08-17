"""How a report meets the terminal: how wide it wraps, and what is coloured.

A report is authored as plain text — one paragraph per fact — and handed to
:func:`emit`. Two reasons the layout is one module rather than a ``print`` per
caller:

* **Width.** The reports used to wrap at a hardcoded 72 columns, which is a
  narrow ribbon on a wide terminal and a folded mess on a narrow one. rich
  already knows the real width — and falls back to 80 when nothing is watching,
  which is what keeps piped output stable — so :func:`wrap_width` is the one
  place that asks.
* **Colour.** ``WARNING`` and the section labels are what a reader's eye lands
  on first, and they are the only things styled: a report where everything is
  coloured is a report where nothing is.

Styles are applied **by span** to a :class:`~rich.text.Text`, never through
rich's console markup. That is not a preference. These reports are full of
``[x]`` ticks, ``demo/pod[podbench-1]`` container references and
``[ERROR]``-style relayed stderr, every one of which rich's markup parser would
eat or reject. ``Text`` treats its content as data, so nothing has to be
escaped on the way in.
"""

from __future__ import annotations

import re

from rich.console import Console
from rich.text import Text

__all__ = ["MAX_WIDTH", "MIN_WIDTH", "WARNING_LEAD", "console", "emit", "wrap_width"]

MAX_WIDTH = 96
"""Widest a paragraph gets, however wide the terminal is.

Prose stops being readable somewhere past ninety columns — the eye loses the
start of the next line — and these blocks are prose. A very wide terminal buys
a shorter report, not a wider one.
"""

MIN_WIDTH = 48
"""Narrowest a paragraph gets, however narrow the terminal is. Below this the
hanging indents cost more than the wrap saves."""

_GUTTER = 8
"""Columns left unused at the right margin.

Text that ends exactly at the edge reads as text that was cut off, and a report
whose longest line is one short of the window wraps the moment anything quotes
it back with a prefix."""

_FLOOR = 24
"""Floor for the width left to a deeply indented paragraph."""

WARNING_LEAD = "WARNING"
"""The word a warning line opens with, and the one span on it that is coloured.

Named here rather than spelled twice: :func:`_styled` colours exactly this
prefix, and the callers that author warnings use the same constant, so the two
cannot drift into a warning that is not highlighted.
"""

_LABEL = re.compile(r"^(\s*)(\S+)(?= {2,}|$)")
"""The first column of a row, when the row has columns.

One rule for three shapes, because they are the same shape: a section on a line
of its own (``ladder``), the key of a ``seat        demo/api[podbench-1]`` row,
and the leading cell of a table (``podbench-1   Running   full   …``). The
lookahead is what keeps it a label rather than merely a first word — two spaces
mean a column was intended, one means an ordinary sentence, and colouring the
word a sentence happens to open with says "heading" about a line that is not
one.
"""

_CHECKBOX = re.compile(r"^\s*(\[[x ]\])")
"""The tick beside a feature under ``supports``."""

_COLUMN_WORD = re.compile(r"(?<=  )(\S+)(?= {2,}|$)")
"""A cell of a table, anywhere along the row.

Cells are what :data:`_VERDICTS` is looked up in, rather than whole words
anywhere: ``Running`` and ``refused`` are ordinary English and appear in the
prose these reports are mostly made of, where a colour on them would be noise
at best and a claim at worst.
"""

_VERDICTS = {
    "landed": "green",
    "running": "green",
    "succeeded": "green",
    "refused": "yellow",
    "pending": "yellow",
    "waiting": "yellow",
    "terminated": "yellow",
    "unknown": "yellow",
    "failed": "red",
}
"""Cell values worth a colour: a ladder step's verdict, and a container phase.

Green is "this worked", yellow "this did not, and the report says what it cost".
Anything not listed — a phase kubectl invented, a rung name — is left alone, so
an unrecognised value reads as data rather than as a silent pass.

Matched case-insensitively because both spellings are already in the output and
neither is wrong: a pod's ``status.phase`` arrives from the API server as
``Running``, while :func:`podbench.launcher._phase_of` lower-cases a container's
state to sit in a sentence.
"""


def console(*, stderr: bool = False) -> Console:
    """A console for one block of output.

    Built per call rather than cached. rich decides colour from ``NO_COLOR``,
    ``FORCE_COLOR`` and whether stdout is a tty *at construction*, and the unit
    suite sets those per test — a module-level instance would freeze whichever
    test ran first into every one after it.

    ``highlight`` is off because rich's automatic highlighter styles numbers,
    paths and anything that looks like a UUID, which on this output means most
    of the report: pod names, uids, capability sets and probe windows would all
    come out coloured, and the two things worth noticing would not stand out.

    ``stderr`` is the resolution chatter's stream: which pod a substring matched,
    and the listing behind "which one?". It goes there so a caller redirecting
    stdout to a file still sees the question, and it is styled the same either
    way — a table does not change shape because of which fd it left by.
    """
    return Console(stderr=stderr, highlight=False, soft_wrap=True)


def wrap_width(indent: int = 0) -> int:
    """Columns a paragraph may fill, once ``indent`` of them are spent."""
    width = max(MIN_WIDTH, min(MAX_WIDTH, console().width - _GUTTER))
    return max(_FLOOR, width - indent)


def emit(text: str, *, stderr: bool = False) -> None:
    """Print an already-wrapped report, colouring the leaders in it.

    Line by line, because that is the unit a style applies to and because
    ``soft_wrap`` then guarantees rich re-wraps none of it: the wrapping was
    done by the caller against :func:`wrap_width`, with hanging indents rich
    knows nothing about.
    """
    out = console(stderr=stderr)
    for line in text.split("\n"):
        out.print(_styled(line))


def _styled(line: str) -> Text:
    """One line of report as rich should draw it."""
    styled = Text(line)
    if line.startswith(WARNING_LEAD):
        # Alone among the rules, this one returns: the leader is also a label by
        # `_LABEL`'s reckoning, and two styles over one span is the brightest
        # thing on the screen for no added meaning.
        styled.stylize("bold yellow", 0, len(WARNING_LEAD))
        return styled
    label = _LABEL.match(line)
    if label is not None:
        # Flush left is a section of the report; indented is a row inside one,
        # and it is the difference between them that has to survive, not the
        # colour of either.
        flush = not label.group(1)
        styled.stylize("bold cyan" if flush else "bold", *label.span(2))
    box = _CHECKBOX.match(line)
    if box is not None:
        available = box.group(1) == "[x]"
        styled.stylize("green" if available else "yellow", *box.span(1))
    for cell in _COLUMN_WORD.finditer(line):
        value = cell.group(1)
        # An all-caps cell is a column heading - the only place these reports
        # shout - and `_LABEL` has already claimed the first of them.
        style = "bold" if value.isalpha() and value.isupper() else None
        style = _VERDICTS.get(value.lower(), style)
        if style is not None:
            styled.stylize(style, *cell.span(1))
    return styled
