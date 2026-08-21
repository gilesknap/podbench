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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from rich.console import Console
from rich.text import Text

__all__ = [
    "MAX_WIDTH",
    "MIN_WIDTH",
    "WARNING_LEAD",
    "Column",
    "Row",
    "console",
    "emit",
    "paragraph",
    "rule",
    "table",
    "table_width",
    "wrap",
    "wrap_width",
]

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

_SECTION = "bold cyan"
"""What a heading is drawn in, wherever a heading is recognised."""

WARNING_LEAD = "WARNING"
"""The word a warning line opens with, and the one span on it that is coloured.

Named here rather than spelled twice: :func:`_styled` colours exactly this
prefix, and the callers that author warnings use the same constant, so the two
cannot drift into a warning that is not highlighted.
"""

_SECTION_LINE = re.compile(r"^(\S+)$")
"""A word alone on a line, flush left: ``ladder``, ``supports``, ``next:``.

Flush left is load-bearing, not decoration. Wrapped prose regularly leaves a
single word on its last line, and an indented one of those is a continuation —
so the same shape two columns in means the opposite thing and must not be read
as a heading.
"""

_LABEL = re.compile(r"^(\s*)(\S+(?: \S+)*?) {2,}(?=\S)")
"""The key of a row that has a value: ``seat        demo/api[podbench-1]``, or
the leading cell of a table (``podbench-1   running   full   …``).

Two spaces *followed by something* is the whole rule. Two spaces mean a column
was intended and one means an ordinary sentence, so colouring the word a
sentence happens to open with would say "heading" about a line that is not one.

The key may be several words — ``CAP_SYS_PTRACE (eff)``, ``scratch attach (own
child)``, ``add this to ~/.ssh/config once:`` — and the match is non-greedy, so
it still ends at the *first* run of two. Single-token was an assumption rather
than a rule, and where it was wrong the damage was to a whole block at once:
``capreport`` sets half its keys in bold and half in plain, which reads as a
report that is broken rather than as one making a distinction.

Safe because of :func:`wrap`: it splits on ``str.split``, so a paragraph it
laid out **cannot** contain an internal run of two spaces. A line that reaches
here holding one was authored as a row by a caller that owns the whole line,
which is exactly the population this is trying to recognise.
"""

_TOKEN = re.compile(r"[^\s`]*`[^`\n]*`[^\s`]*|\S+")
"""One unit :func:`wrap` may put on a line: a backticked run, or a bare word.

Backticks are what these reports already put round a command or a flag, and a
run between a matched pair is a *value*, not prose. So it is kept whole, and allowed to
overrun the margin rather than be broken: the wrap is correct as layout and
wrong as purpose, because the half of the line the reader was going to select
and paste comes back as two lines with an indent through the middle of it. A
flag parted from its argument is the same defect one token earlier —
:func:`podbench.launcher.target_row` had it against ``--target`` on a beamline's
pod names, and fixed it there by authoring the line finished, which only works
for a caller that owns the whole line.

The surrounding non-space is taken with the run so that ``, `podbench dev`.``
keeps its punctuation attached, exactly as ``str.split`` would have.

Matches are found left to right and do not overlap, so a paragraph with several
commands in it pairs each opening backtick with its own closing one. Text with
an *odd* number - GNU-style relayed stderr, which opens a quote with a backtick
and closes it with an apostrophe - has nothing to pair, and the second branch
splits it into words as before.
"""

_DIVIDER = re.compile(r"^([=\-_~])\1{2,}")
"""A line :func:`rule` drew, recognised by what it is made of.

Matched before anything else, because a divider is a single token at the end of
its own line and :data:`_LABEL` would otherwise read the whole bar as a heading
and set it in the heading colour, which is the loudest possible way to draw the
quietest thing on the page.
"""

_TITLE = re.compile(r"[^=\-_~\s](?:.*[^=\-_~\s])?")
"""What a divider carries in the middle of it, when it carries anything."""

_SHOUT = re.compile(r"^[A-Z][A-Z0-9]*(?:[ -][A-Z0-9]+)*")
"""A flush-left run of capitals, which in these reports is always a heading.

``doctor`` shouts its sections (``THIS MACHINE``, ``CHECKS``, ``RBAC in …``) and
its verdict where ``attach`` uses lower-case labels, and the two conventions were
never going to be reconciled — ``VERDICT`` mirrors ``capreport``'s deliberately.
The rule reads the shape instead: nothing in the prose starts a line in capitals,
because the prose is wrapped and its continuations are indented.
"""

_SHOUTED = {"BLOCKERS": "bold red"}
"""Headings whose colour is not the ordinary one.

``BLOCKERS`` only ever appears on a run that has some, so the heading itself
carries the verdict. Everything else is a section marker and takes the section
colour.
"""

_STATUS = re.compile(r"^\s*(\[[^\]]{1,6}\])")
"""The bracketed token a row opens with: ``attach``'s ``[x]``/``[ ]`` tick and
``doctor``'s ``[ok]``/``[warn]``/``[FAIL]``, which are the same thing said twice.

Bounded at six characters so a relayed ``[Errno 2] No such file`` from somebody
else's stderr is prose, not a status this report is claiming."""

_STATUSES = {
    "x": "green",
    "ok": "green",
    " ": "yellow",
    "!": "bold yellow",
    "warn": "yellow",
    "FAIL": "bold red",
}
"""What each status token is worth. An unrecognised one is left uncoloured, for
:data:`_VERDICTS`' reason: a token this module has not been taught must not
borrow the authority of one it has."""

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
    # `capreport`'s probe rows, which are the three lines in that report a
    # reader is actually looking for. Safe as cell values because they are only
    # ever looked up in column position: "ok" and "denied" are ordinary English
    # and appear in the prose all round them, where a colour would be a claim.
    "ok": "green",
    "denied": "yellow",
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


def wrap(text: str, width: int | None = None) -> list[str]:
    """Break ``text`` on whitespace at ``width``, the terminal's own by default.

    Asked for per call rather than captured once: :func:`wrap_width` reads the
    window, and a report built at import time would be wrapped for whatever the
    terminal was when the process started.

    Prose breaks on any space:

    >>> wrap("one two three four", width=9)
    ['one two', 'three', 'four']

    A backticked run does not, however narrow the window, because it is there
    to be pasted (:data:`_TOKEN`). It takes a line of its own and overruns:

    >>> wrap("run `podbench doctor --fix` now", width=12)
    ['run', '`podbench doctor --fix`', 'now']

    Whitespace inside one is still collapsed, so a value cannot smuggle in the
    two spaces that make a row's first cell a label:

    >>> wrap("`podbench  dbg`", width=40)
    ['`podbench dbg`']

    An unmatched backtick has nothing to pair with and wraps as words:

    >>> wrap("no rule to make target `foo'", width=16)
    ['no rule to make', "target `foo'"]
    """
    limit = wrap_width() if width is None else width
    lines: list[str] = []
    current = ""
    for word in (" ".join(token.split()) for token in _TOKEN.findall(text)):
        candidate = f"{current} {word}".strip()
        if len(candidate) > limit and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def paragraph(text: str, *, first: str = "", indent: str = "") -> list[str]:
    """Wrap ``text`` with a hanging indent, so a wrapped line is not mistaken
    for a second bullet.

    >>> paragraph("one two three", first="- ", indent="  ")
    ['- one two three']
    """
    wrapped = wrap(text, width=wrap_width(len(indent))) or [""]
    return [first + wrapped[0], *(indent + line for line in wrapped[1:])]


def rule(title: str = "", *, char: str = "=") -> str:
    """A full-width divider, with ``title`` centred in it when there is one.

    Drawn with :func:`wrap_width` rather than with rich's own ``Rule``, so the
    banner lines up with the paragraphs under it: those are wrapped short of the
    window by :data:`_GUTTER`, and a divider that ran to the true edge would be
    the one thing on screen the report does not fit inside.
    """
    width = wrap_width()
    return f" {title} ".center(width, char) if title else char * width


_CELL_GAP = "  "
"""What separates one cell from the next.

Two spaces, which is the same two spaces :data:`_LABEL` and
:data:`_COLUMN_WORD` read a column by. A table built here is styled from the
data it was built from rather than by sniffing its own rendering, but it still
travels through the same reports, gets quoted back and pasted into issues — so
it has to *look* like every other row podbench prints, or the one vocabulary
this module exists to keep becomes two.
"""

_ELLIPSIS = "…"
"""What marks a cell that did not fit. One column wide, unlike ``...``, which
matters when the budget it is spent from is the last few columns of the window."""

_FILL_FLOOR = 12
"""Columns a filling cell keeps however narrow the window.

Below this the truncation says nothing at all — ``/usr/bin/pyt…`` names no
process — so a very narrow terminal overruns instead. A row that ran off the
edge can still be read by widening the window; one truncated to nothing cannot
be read at all.
"""


@dataclass(frozen=True)
class Column:
    """One column of a :func:`table`, and how its cells are coloured."""

    heading: str
    """What is printed above the column. Capitals by convention, which is what
    :data:`_COLUMN_WORD` already draws in bold."""

    verdicts: Mapping[str, str] = field(default_factory=dict[str, str])
    """Cell value → style, for the values in this column that are verdicts.

    Per column rather than by the global :data:`_VERDICTS` lookup, because the
    same word is a verdict in one column and data in another: ``ok`` under
    ``PTRACE`` is a measurement that succeeded, and under a hypothetical
    ``COMM`` it would be the name of somebody's binary. A value not listed is
    left uncoloured, for :data:`_VERDICTS`' reason — a token this column has
    not been taught must not borrow the authority of one it has.
    """

    fill: bool = False
    """Whether this column absorbs the width the others left, and is cut to it.

    At most one column should set it. That column is the one whose content has
    no bound — a cmdline is as long as somebody's argv — and letting it overrun
    is what breaks the *rows below it*, because the terminal's own wrap puts
    their first cell under this one's third. Truncating one cell costs the tail
    of one line; not truncating it costs the alignment of the whole table.
    """


@dataclass(frozen=True)
class Row:
    """One row of a :func:`table`."""

    cells: Sequence[str]
    """As many as there are columns. Rendered in order, padded to the column."""

    style: str | None = None
    """Applied to the whole row before any cell verdict is, so the two layer
    rather than compete: ``dim`` on a row still lets a red ``DENIED`` in it
    read as red. Used to separate rows that answer the reader's question from
    rows that are merely also true — which is a distinction a column cannot
    make, since it is about the row as a whole."""


def table_width() -> int:
    """Columns a table may fill — the window's, not :func:`wrap_width`'s.

    Deliberately not the prose width. :data:`MAX_WIDTH` caps a paragraph at
    ninety-six because past that the eye loses the start of the next line, and
    that is a fact about *reading a sentence*. A table is not read that way: it
    is scanned down a column and across a row that is already broken into
    labelled cells, so a wide window buys it real information — the tail of a
    cmdline — rather than a longer journey back to the left margin.

    One column is left rather than :data:`_GUTTER`: what ends at this margin is
    a cell that was cut on purpose and says so with :data:`_ELLIPSIS`, so the
    "text that ends at the edge reads as text that was cut off" argument is not
    an argument against it here. It is the intended reading.
    """
    return max(MIN_WIDTH, console().width - 1)


def table(
    columns: Sequence[Column],
    rows: Sequence[Row],
    *,
    indent: str = "",
    width: int | None = None,
) -> list[Text]:
    """Lay ``rows`` out under ``columns``, styled from the data they came from.

    Returned as :class:`~rich.text.Text` rather than as strings for :func:`emit`
    to sniff, and that is the whole reason this function exists. The line rules
    in this module (:data:`_LABEL`, :data:`_SHOUT`, :data:`_COLUMN_WORD`) infer
    meaning from a rendered line's *shape*, which is the only thing available
    when a report is authored as prose by thirty different callers. A dense
    table is the case they misread — every pid reads as a section heading, and
    every single-letter process state reads as a column heading — and widening
    them to cope would put every ``doctor`` and ``attach`` row at risk for one
    caller's benefit. A table has its data in hand, so it styles from that and
    is never sniffed.

    >>> rendered = table(
    ...     [Column("PID"), Column("ST", {"Z": "yellow"}), Column("CMD", fill=True)],
    ...     [
    ...         Row(["1", "S", "/sbin/init"]),
    ...         Row(["7", "Z", "python3 -u app.py"], style="dim"),
    ...     ],
    ...     width=34,
    ... )
    >>> for line in rendered:
    ...     print(line.plain)
    PID  ST  CMD
    1    S   /sbin/init
    7    Z   python3 -u app.py

    The filling column is cut to what is left, so one long argv cannot unalign
    the rows under it:

    >>> for line in table(
    ...     [Column("PID"), Column("CMD", fill=True)],
    ...     [Row(["1", "/usr/bin/python3 -u /app/server.py --port 8080"])],
    ...     width=24,
    ... ):
    ...     print(line.plain)
    PID  CMD
    1    /usr/bin/python3 -…
    """
    limit = table_width() if width is None else width
    grid = [[*(column.heading for column in columns)], *(list(r.cells) for r in rows)]
    widths = [max(len(row[index]) for row in grid) for index in range(len(columns))]
    # A column nothing filled in is not a narrow column, it is not a column:
    # a marker column on a listing that marked nothing would otherwise indent
    # the whole table by a gap that leads to an empty cell.
    live = [index for index, width_ in enumerate(widths) if width_]
    fills = [index for index in live if columns[index].fill]
    if fills:
        index = fills[0]
        spent = sum(widths[i] for i in live if i != index)
        gaps = len(_CELL_GAP) * (len(live) - 1) + len(indent)
        widths[index] = max(_FILL_FLOOR, limit - spent - gaps)
        for row in grid:
            if len(row[index]) > widths[index]:
                row[index] = row[index][: widths[index] - 1] + _ELLIPSIS
    columns = [columns[index] for index in live]
    widths = [widths[index] for index in live]
    grid = [[row[index] for index in live] for row in grid]
    styles = [None, *(row.style for row in rows)]
    return [
        _styled_row(cells, columns, widths, indent=indent, row_style=style, head=first)
        for first, (cells, style) in enumerate(zip(grid, styles, strict=True))
    ]


def _styled_row(
    cells: Sequence[str],
    columns: Sequence[Column],
    widths: Sequence[int],
    *,
    indent: str,
    row_style: str | None,
    head: int,
) -> Text:
    """One row of a :func:`table`, with every span it earns already applied."""
    line = indent
    spans: list[tuple[int, int, str]] = []
    for index, (cell, column, width) in enumerate(
        zip(cells, columns, widths, strict=True)
    ):
        if index:
            line += _CELL_GAP
        start = len(line)
        line += cell.ljust(width)
        if not cell:
            continue
        # The heading row shouts, which is what every other report in podbench
        # does with a column heading; a body cell is coloured only where this
        # column has been taught what the value means.
        style = "bold" if head == 0 else column.verdicts.get(cell)
        if style is not None:
            spans.append((start, start + len(cell), style))
    # Trailing padding is not content: a row whose last cells are empty would
    # otherwise carry the alignment of a table nobody can see to the clipboard.
    styled = Text(line.rstrip())
    if row_style is not None:
        styled.stylize(row_style)
    for start, end, style in spans:
        if start < len(styled):
            styled.stylize(style, start, min(end, len(styled)))
    return styled


def emit(text: str | Iterable[Text], *, stderr: bool = False) -> None:
    """Print an already-wrapped report, colouring the leaders in it.

    Line by line, because that is the unit a style applies to and because
    ``soft_wrap`` then guarantees rich re-wraps none of it: the wrapping was
    done by the caller against :func:`wrap_width`, with hanging indents rich
    knows nothing about.

    An iterable of :class:`~rich.text.Text` is printed as it arrives, with none
    of the line rules applied — that is what :func:`table` returns, and a line
    that already knows what its spans mean must not have them guessed at again.
    """
    out = console(stderr=stderr)
    lines = text.split("\n") if isinstance(text, str) else text
    for line in lines:
        out.print(_styled(line) if isinstance(line, str) else line)


def _styled(line: str) -> Text:
    """One line of report as rich should draw it."""
    styled = Text(line)
    if line.startswith(WARNING_LEAD):
        # Alone among the rules, this one returns: the leader is also a label by
        # `_LABEL`'s reckoning, and two styles over one span is the brightest
        # thing on the screen for no added meaning.
        styled.stylize("bold yellow", 0, len(WARNING_LEAD))
        return styled
    if _DIVIDER.match(line) is not None:
        styled.stylize("dim")
        title = _TITLE.search(line)
        if title is not None:
            # `not dim` because styles layer rather than replace, and a banner
            # whose title is dimmed with its own bar is a banner with no title.
            styled.stylize(f"not dim {_SECTION}", *title.span())
        return styled
    shout = _SHOUT.match(line)
    if shout is not None:
        styled.stylize(_SHOUTED.get(shout.group(), _SECTION), *shout.span())
    box = _STATUS.match(line)
    if box is not None:
        status = _STATUSES.get(box.group(1)[1:-1])
        if status is not None:
            styled.stylize(status, *box.span(1))
    # Both arms only where no status token claimed the column: a row opening
    # with one is a row whose first cell is a verdict, and reading it as a
    # heading too would layer two styles over the same span for no added
    # meaning.
    elif (heading := _SECTION_LINE.match(line)) is not None:
        styled.stylize(_SECTION, *heading.span(1))
    elif (label := _LABEL.match(line)) is not None:
        # Flush left is a section of the report; indented is a row inside one,
        # and it is the difference between them that has to survive, not the
        # colour of either.
        flush = not label.group(1)
        styled.stylize(_SECTION if flush else "bold", *label.span(2))
    for cell in _COLUMN_WORD.finditer(line):
        value = cell.group(1)
        # An all-caps cell is a column heading - the only place these reports
        # shout - and `_LABEL` has already claimed the first of them.
        style = "bold" if value.isalpha() and value.isupper() else None
        style = _VERDICTS.get(value.lower(), style)
        if style is not None:
            styled.stylize(style, *cell.span(1))
    return styled
