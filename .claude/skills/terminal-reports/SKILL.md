---
name: terminal-reports
description: How podbench's laptop-side verbs lay out and colour what they print — the one-line warning rule, and the four ways a well-meaning edit silently mangles a report. Read before adding a WARNING, changing anything a laptop verb prints, or touching console.py.
---

# Terminal reports

Everything the laptop-side verbs print — `doctor`, `attach`, `vscode`,
`ssh-config`, `status`, `list`, `dev`, `hotfix status` — is authored as **plain
text** and laid out by `src/podbench/console.py`. Two in-pod verbs go through it
as well: `capreport` (authored as text, printed through `emit`) and `pids`
(built with `console.table`, which is styled from its data and never sniffed).
`dbg` and `debug-config` do not — `dbg` writes a gdb command file and
`debug-config` writes JSON that the launcher parses, and both must stay byte
exact.

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

## Three beats, and the third one is the only one that acts

Every user-facing note gets at most three beats, in this order (#203):

1. **what happened** — the fact, about *this* pod, seat or target;
2. **whether it matters here** — the consequence, not the mechanism;
3. **what to do about it** — the flag, the command, or the page.

Anything that is *why podbench is confident this is fine* belongs in the
docstring, not on the terminal. Anything that is *how the thing works* belongs
in `docs/how-to/` or `docs/reference/cli.md`, said once. What is left on the
line is what a reader in front of a broken pod can act on.

Two corollaries, both of which delete rather than shorten:

- **Warn only where the outcome changed.** `ADMISSION_MUTATION_WARNING` fired on
  every DLS attach to say that admission had rewritten a spec in a way that cost
  the seat nothing — and the report already measures the seat itself. It is
  gone; `spec.admission_rewrites` still computes the difference, and nothing
  prints it.
- **Do not announce one event twice.** A resize is an intent
  (`EDITOR_RESIZE_NOTE`), an outcome (`try_resize`) and a residual hazard
  (`EDITOR_HEADROOM_WARNING`); each says only the part the other two cannot, and
  none of them repeats the `memory` row's `used of limit`.

A measurement replaces a guess wherever one is available, and where it could not
be taken the line says **unmeasured** — never "fine" by silence, and never a
caution invented to fill the gap. #204 is the worked example: the reconnect
ssh-key warning asserted that a seat could not carry the key without reading
`authorized_keys`, so it fired on the common case where ssh works. It now
`cat`s the file and says nothing when the key is there.

When the whole set is being rewritten, read it end to end as a body of text
before applying it. The property is consistency across the set — one voice, the
same three beats in the same order, the same word for the same thing in
`launcher.py`, `hotfix.py`, `editor.py` and `proc.py` — and that is invisible one
constant at a time.

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

A key may be **several words** — `CAP_SYS_PTRACE (eff)`, `scratch attach (own
child)` — and `_LABEL` matches non-greedily, so it still ends at the first run
of two spaces. That is safe for the reason the rule works at all: `wrap` splits
on `str.split`, so a paragraph it laid out cannot contain an internal double
space, and a line that has one was authored as a row. What is *not* safe is a
key padded to exactly its column width: it then leaves one space, the rule does
not fire, and the block comes out with half its keys bold and half plain —
which reads as broken rather than as a distinction. `capreport` pads to 28
because one of its keys is 26 characters long.

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

## A table is styled from its data, never sniffed

The line rules below (`_LABEL`, `_SHOUT`, `_COLUMN_WORD`) infer meaning from a
rendered line's *shape*, which is all that is available when thirty callers
author prose. **A dense table is the case they misread**: every pid matches
`_LABEL` and comes out as a section heading, every single-letter process state
matches the all-caps test in `_COLUMN_WORD` and comes out as a column heading.

So `console.table(columns, rows)` returns `Text` with its spans already applied
and `emit` prints those untouched. A `Column` carries a `verdicts` map —
per column, because `ok` under `PTRACE` is a measurement and `ok` somewhere else
is a word — and `fill=True` marks the one column that absorbs the leftover
width and is cut to it with `…`. Exactly one column should fill, and it should
be the one with no bound on its content: an unbounded cell does not cost the
tail of its own row, it costs the *alignment of every row below*, because the
terminal wraps it and puts the next row's first cell under this one's third.

`table_width()` is the window, not `wrap_width()`. The 96-column cap is a fact
about reading a sentence; a table is scanned down a column, so a wide terminal
buys it real information instead of a longer journey back to the margin.

Colour is never the only place a fact lives. `pids` dims the rows outside the
target container **and** keeps the `TARGET` column, because these listings are
read from a pasted log as often as from a terminal.

## One status vocabulary

`[x]`/`[ ]` (attach), `[ok]`/`[warn]`/`[FAIL]` (doctor and `vscode`'s `editor`
block) and `[ok]`/`[!]` (hotfix) are all read by one rule in
`console._STATUSES`. A new verb uses one of
those tokens rather than inventing a spelling; the bracket is what makes it
colourable without the verb knowing what a colour is. Tokens are bounded at six
characters so relayed `[Errno 2]`-style stderr stays prose.

`_VERDICTS` colours container phases and ladder verdicts, but **only in column
position** (preceded by two spaces): `running` and `refused` are ordinary
English and these reports are mostly prose. An unrecognised value is left
uncoloured on purpose — a token this module has not been taught must not borrow
the authority of one it has.

## What `vscode` prints is a checklist and a `next` block

`editor` is the past tense, one line per step, each opening with a status token
(`podbench.editor.OK`/`WARN`/`FAIL`). `next` is what the reader might do, and it
holds the pasteables. They were interleaved once, which is what made that block
a wall: the two lines worth pasting were in the middle of thirteen that were
not.

Three rules there, all of which fail quietly:

- **A step is one line.** The mechanism goes in
  `docs/how-to/vscode-remote-ssh.md`, said once. A step that explains itself in
  a paragraph buries the step that failed.
- **Relayed seat stderr is printed as a bare `Text`, never through `_styled`.**
  It is somebody else's output — `debug-config:` at the head of one is a label
  to `_LABEL`'s eye and is not one — and one of those lines ends in a
  continuation `\` that means nothing once anything follows it. It must not be
  wrapped, reflowed or re-indented. `launcher._editor_step` keeps the two
  shapes apart on `editor.is_step`.
- **`next` prints from a `finally`.** A run that ends at "ssh does not reach
  the seat" still landed a seat, and that reader is the one who most needs the
  alias and the stanza's path.

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
