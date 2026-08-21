"""Tests for the presentation layer.

Styled output is asserted with colour forced *on*, which the suite's own
``plain_console`` fixture turns off everywhere else. That asymmetry is the
point: every other test asserts on the text a report carries, and this one
asserts on what is drawn over it, so the two never have to be untangled from
one string.
"""

from __future__ import annotations

import pytest

from podbench.console import (
    MAX_WIDTH,
    MIN_WIDTH,
    WARNING_LEAD,
    emit,
    paragraph,
    rule,
    wrap_width,
)


@pytest.fixture
def coloured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo ``plain_console``, so the escape codes are there to assert on."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")


def drawn(text: str, capsys: pytest.CaptureFixture[str]) -> str:
    emit(text)
    return capsys.readouterr().out


def test_the_wrap_follows_the_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    assert wrap_width() == 72


def test_a_wide_terminal_buys_a_shorter_report_not_a_wider_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prose past ninety columns loses the reader between lines."""
    monkeypatch.setenv("COLUMNS", "400")
    assert wrap_width() == MAX_WIDTH


def test_a_narrow_terminal_stops_folding_rather_than_going_to_one_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "20")
    assert wrap_width() == MIN_WIDTH


def test_an_indent_comes_out_of_the_width_it_is_spent_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    assert wrap_width(9) == wrap_width() - 9


def test_a_backticked_remedy_stays_on_one_line_at_every_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half of a warning that exists to be pasted, at 80 and at 60.

    A remedy that wraps pastes as two lines with the hanging indent through the
    middle of it, which is the whole of issue #120. The prose around it still
    wraps — the fix tells the two apart rather than turning wrapping off — so
    the same text is asserted to be more than one line.
    """
    remedy = "`podbench attach <pod> --new --target-gid <gid>`"
    for columns in ("80", "60"):
        monkeypatch.setenv("COLUMNS", columns)
        lines = paragraph(
            f"the seat's gid differs from the target's and nothing corrects it "
            f"here: {remedy} pins it by hand.",
            first=f"{WARNING_LEAD}  ",
            indent=" " * (len(WARNING_LEAD) + 2),
        )
        assert len(lines) > 1, columns
        assert any(remedy in line for line in lines), columns


@pytest.mark.usefixtures("coloured")
def test_the_warning_leader_is_the_only_span_coloured_on_its_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = drawn(f"{WARNING_LEAD}  the pod may be evicted", capsys)
    lead, rest = out.split("evicted")[0].split("  ", 1)
    assert "\x1b[" in lead
    assert "\x1b[" not in rest, "one style per span, and the word is the span"


@pytest.mark.usefixtures("coloured")
def test_a_flush_left_label_and_an_indented_one_are_told_apart(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A section of the report against a row inside one."""
    section = drawn("seat        demo/api[podbench-1]", capsys)
    row = drawn("  verdict     live attach available", capsys)
    assert section != row.strip()
    assert "\x1b[" in section and "\x1b[" in row


@pytest.mark.usefixtures("coloured")
def test_a_sentence_is_not_a_heading_because_of_the_word_it_opens_with(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The rule is two spaces, meaning a column was intended."""
    assert "\x1b[" not in drawn("ssh config written to /home/me/.podbench", capsys)


@pytest.mark.usefixtures("coloured")
def test_a_tick_and_a_gap_are_different_colours(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert drawn("  [x] live attach", capsys) != drawn("  [ ] live attach", capsys)


@pytest.mark.usefixtures("coloured")
def test_a_phase_is_coloured_in_a_column_and_left_alone_in_prose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``running`` and ``refused`` are ordinary English as well as verdicts, and
    these reports are mostly prose."""
    assert "\x1b[32m" in drawn("  podbench-1   running     full   seat", capsys)
    assert "\x1b[" not in drawn(
        "the pod is running and the request was refused", capsys
    )


@pytest.mark.usefixtures("coloured")
def test_an_unrecognised_cell_value_is_left_as_data(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A phase this table has never seen must not read as a silent pass."""
    out = drawn("  podbench-1   CrashLoopBackOff   full   seat", capsys)
    assert "\x1b[32m" not in out and "\x1b[33m" not in out


def test_brackets_reach_the_terminal_as_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The reason styles are applied by span rather than through rich markup.

    Every one of these would be swallowed or rejected by rich's markup parser,
    and all three are in output podbench prints today.
    """
    text = "seat        demo/api[podbench-1]\n  [x] live attach\n  [ERROR] relayed"
    assert drawn(text, capsys) == f"{text}\n"


@pytest.mark.usefixtures("coloured")
def test_a_wrapped_line_holding_one_word_is_not_a_heading(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The regression: prose regularly ends on a single word, and indented that
    is a continuation, not a section. `SYS_PTRACE` alone on the last line of a
    wrapped sentence was being set as though it opened a new one."""
    assert "\x1b[" not in drawn("            SYS_PTRACE", capsys)
    assert "\x1b[" in drawn("supports", capsys), "flush left still is one"


@pytest.mark.usefixtures("coloured")
def test_a_label_needs_a_value_beside_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert "\x1b[" not in drawn("  trailing spaces are not a column   ", capsys)


@pytest.mark.usefixtures("coloured")
def test_doctors_statuses_are_told_apart_from_each_other(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``attach``'s tick and ``doctor``'s verdict are the same thing said twice,
    so one rule reads both - but ok, warn and FAIL must not all look alike."""
    drawings = {
        drawn(f"  [{token}]  kubectl        found", capsys)
        for token in ("ok", "warn", "FAIL")
    }
    assert len(drawings) == 3


@pytest.mark.usefixtures("coloured")
def test_a_bracket_too_long_to_be_a_status_is_left_as_prose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Relayed stderr is full of these, and none of them is this report's claim."""
    assert "\x1b[" not in drawn("  [Errno 2] no such file", capsys)


@pytest.mark.usefixtures("coloured")
def test_a_banner_is_quiet_and_its_title_is_not(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Styles layer rather than replace, so a title inside a dimmed bar has to
    turn the dim back off or the banner has no title."""
    out = drawn(rule("podbench doctor"), capsys)
    assert "\x1b[2m" in out
    assert "not dim" not in out and "podbench doctor" in out
    title = out.split("podbench doctor")[0].split("\x1b[")[-1]
    assert "2" not in title.split("m")[0].split(";"), "the title is not dimmed"


def test_a_rule_fits_inside_what_the_report_wraps_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    assert len(rule()) == wrap_width()
    assert len(rule("podbench doctor")) == wrap_width()
