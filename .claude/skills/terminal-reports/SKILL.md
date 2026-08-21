---
name: terminal-reports
description: How podbench's laptop-side verbs lay out and colour what they print — the one-line warning rule, and the four ways a well-meaning edit silently mangles a report. Read before adding a WARNING, changing anything a laptop verb prints, or touching console.py.
---

# Terminal reports

Everything the laptop-side verbs print — `doctor`, `attach`, `ssh-config`,
`status`, `list`, `dev`, `hotfix status` — is authored as **plain text** and laid
out by `src/podbench/console.py`. The in-pod verbs (`capreport`, `pids`, `dbg`,
`debug-config`) do not go through it; they print for a log as much as for a
person.

Nothing here is enforced by a type. Every rule below has a failure mode that
looks fine in the source and is wrong on the terminal.

## A warning is one line

`format_session` renders each `session.warnings` entry as
`WARNING  <one paragraph>`, hung under the coloured leader. That is a rule, not
a coincidence of the current texts.

- Name the fact and the flag. The **mechanism** goes in `docs/how-to/`, said
  once, where somebody reading about the mode will meet it.
- The report was 69 lines, 42 of them warning prose, and the reliably-skipped
  part was all of it. A new paragraph-length warning puts it back.
- A caveat about a *mutation* belongs on the path that made it. `RESIZE_WARNING`
  is the line printed when neither resize flag was given, so it is an offer;
  R13's unproven halves (`LimitRange`, `ResourceQuota`, the ReplicaSet
  reasoning) are printed by `try_resize` on success, to the person who just
  changed a live pod.
- A deadline that is *about this pod* belongs on the line it qualifies, not in a
  block below it. `probe_qualifier` carries the probe arithmetic on the
  `supports` tick; there is no probe WARNING any more, and re-adding one would
  be the same numbers in two places.

## Styles are applied by span, never by markup

`console._styled` builds a `rich.text.Text` and calls `stylize(style, start,
end)`. It must never build a markup string.

This output is full of `[x]` ticks, `demo/api[podbench-1]` container refs and
relayed `[Errno 2]` stderr. rich's console markup parser eats or rejects every
one of them, and the damage is to *other people's* text — a pod name, a captured
error — which is the text least likely to be in a test fixture.

`tests/test_console.py::test_brackets_reach_the_terminal_as_written` is the
guard. Keep it.

## Two spaces are load-bearing

`console.wrap` splits on `text.split()`, so **it collapses every run of
whitespace**. Two independent conventions depend on runs of two spaces
surviving, and both break silently:

- **Columns.** `console` recognises a row's label by `<token>` followed by two
  or more spaces *and something after them*. A row put through `paragraph()`
  comes back a sentence, with the values no longer under their headings.
  Author the row as an f-string; pass only the prose *beneath* it to
  `paragraph()`. `doctor._row`, `hotfix.format_status` and `dev._fact` all do
  this by putting the aligned lead in `first=` and wrapping only the value.
- **Offers.** `do this:  <command>` marks its right-hand half as pasteable.
  `doctor._note` prints such a line verbatim however long it is, because
  wrapping it collapsed the gap *and* broke on a space — which turned the one
  line in that report that exists to be pasted, the ssh `Include`, into two that
  could not be.

## Backticks mark a value that may not be broken

Inside a wrapped paragraph, backticks are not decoration. `console._TOKEN` reads
a run between a matched pair — ``` `--resize MEMORY` ```, ``` `podbench doctor
--fix` ``` — as **one token**, so `wrap` keeps it on one line and lets it overrun
the margin rather than break it. That is the whole of issue #120: a wrap through
a remedy is correct as layout and wrong as purpose, because the half of the line
the reader was going to select and paste comes back as two lines with the
hanging indent through the middle.

So a command or a flag-and-its-argument in prose **gets backticks**, and a bare
one is a latent defect that shows itself only at the width where it happens to
land on a break. A single unbroken token (`--no-correct-ids`) never needs them.
Prose is untouched: it still wraps, and whitespace inside a backticked run is
still collapsed, so a value cannot smuggle in the two spaces that make a cell a
label. Text with an *odd* number of backticks — GNU-style relayed stderr, which
opens a quote with one and closes it with an apostrophe — pairs with nothing and
wraps as words, exactly as it did before.

The doctests on `console.wrap` pin all four of those cases, and
`tests/test_console.py::test_a_backticked_remedy_stays_on_one_line_at_every_width`
checks it at 80 columns and at 60.

A caller that owns the whole line has the older answer available and should
prefer it where the line is a row: author it finished, as `launcher.target_row`
and `launcher.ssh_connect_line` do.

A label is also only a label when a value follows it. A wrapped sentence
regularly ends on a single word, and an indented lone word is a continuation,
not a heading — `SYS_PTRACE` on the last line of a wrapped note was being drawn
as one until `_SECTION_LINE` was split out from `_LABEL`.

## Width comes from `wrap_width()`, always

Never hardcode a column count. `wrap_width(indent)` clamps the terminal to
48–96 with an 8-column gutter, and is asked *per call* — a constant computed at
import time is wrapped for whatever the terminal was when the process started.
`rule()` uses the same width, so banners line up with the paragraphs under them
instead of running to an edge the report never reaches.

An 80-column terminal yields 72, which is what the old hardcoded default was, so
a test that pins `COLUMNS=80` sees the pre-`console` wrap.

## One status vocabulary

`[x]`/`[ ]` (attach), `[ok]`/`[warn]`/`[FAIL]` (doctor) and `[ok]`/`[!]`
(hotfix) are all read by one rule in `console._STATUSES`. A new verb uses one of
those tokens rather than inventing a spelling; the bracket is what makes it
colourable without the verb knowing what a colour is. Tokens are bounded at six
characters so relayed `[Errno 2]`-style stderr stays prose.

`_VERDICTS` colours container phases and ladder verdicts, but **only in column
position** (preceded by two spaces): `running` and `refused` are ordinary
English and these reports are mostly prose. An unrecognised value is left
uncoloured on purpose — a token this module has not been taught must not borrow
the authority of one it has.

## Testing a report

- `tests/conftest.py` forces `NO_COLOR` and `COLUMNS=80` for the whole suite, so
  assertions see plain text at a fixed width. `tests/test_console.py` opts back
  into colour with its own `coloured` fixture; nothing else should.
- Assert on **words, not phrases**, for anything that now wraps. Where a test
  genuinely needs the sentence, flatten first — `tests/test_doctor.py::flowed`
  is `" ".join(format_report(report).split())` and exists for exactly this.
- `--print-config` output must stay pasteable byte for byte. rich drops colour
  when nothing is watching, so this holds, but check it after touching `emit`.

## rich is free, a second dependency is not

`rich` is a hard requirement of `typer`, so importing it costs nothing and
`tests/test_packaging.py` is unchanged. That test asserts typer is the **only**
direct runtime dependency; do not add a second to make output prettier.
