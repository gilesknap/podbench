"""Tests for the JSONC reader and the textual editor built on it.

Two properties are the whole point of the module and both fail *silently* when
they are wrong. A comment stripper that does not track string literals eats a
``//`` inside a URL, a ``,`` inside a value, or the rest of a line after an
escaped quote — and the file it hands back still parses, so nothing complains
until the user notices their setting has changed. And an editor that writes by
round-tripping loses the comments it just successfully read, which is a worse
outcome than the refusal this module replaces.

Nothing here touches a cluster: it is text in and text out.
"""

from __future__ import annotations

import json

import pytest

from podbench.jsonc import (
    Edit,
    append_items,
    apply_edits,
    insert_members,
    parse,
    replace_value,
)

# -- reading VS Code's dialect ----------------------------------------------


def test_line_comments_and_trailing_commas_are_read() -> None:
    """Both of the 2026-08-24 refusals, in one document: ``launch.json`` failed
    on a comment VS Code's own scaffold writes, ``settings.json`` on a trailing
    comma before ``}``."""
    text = """{
      // what this file is for
      "a": [1, 2,],
      "b": {"c": true,},
    }"""
    assert parse(text).value == {"a": [1, 2], "b": {"c": True}}


def test_block_comments_are_read_wherever_whitespace_is_allowed() -> None:
    text = '{/* head */ "a" /* key */ : /* value */ [1 /* one */, 2] /* tail */}'
    assert parse(text).value == {"a": [1, 2]}


def test_a_comment_marker_inside_a_string_is_data() -> None:
    """The classic way a naive stripper corrupts a file: the URL VS Code writes
    into its own scaffold carries a ``//``."""
    text = '{"url": "https://example.invalid/x", "path": "a//b"}'
    assert parse(text).value == {
        "url": "https://example.invalid/x",
        "path": "a//b",
    }


def test_a_comma_inside_a_string_is_data() -> None:
    assert parse('{"args": "run,--verbose"}').value == {"args": "run,--verbose"}


def test_an_escaped_quote_does_not_end_a_string() -> None:
    """``"he said \\" // not a comment"`` is one string, and a scanner that
    stopped at the second quote would read the rest of it as syntax."""
    text = '{"said": "he said \\" // not a comment", "n": 1}'
    assert parse(text).value == {"said": 'he said " // not a comment', "n": 1}


def test_the_span_of_every_value_is_the_text_it_came_from() -> None:
    text = '{\n  "a": {"b": 1}\n}'
    node = parse(text).member("a")
    assert node is not None
    assert text[node.start : node.end] == '{"b": 1}'


def test_a_duplicate_key_answers_with_the_last_one() -> None:
    """:func:`json.loads` keeps the last, so a reader whose decisions disagreed
    with the document's own value would edit the wrong span."""
    document = parse('{"a": 1, "a": 2}')
    assert document.value == {"a": 2}
    node = document.member("a")
    assert node is not None and node.value == 2


# -- refusing what is not JSONC either ---------------------------------------


def test_something_that_is_not_jsonc_is_refused_in_jsons_own_words() -> None:
    with pytest.raises(json.JSONDecodeError, match="property name enclosed"):
        parse("{ 'a': 1 }")


def test_an_unterminated_block_comment_is_refused() -> None:
    """Taken as running to the end of the file, it would silently delete
    whatever followed it."""
    with pytest.raises(json.JSONDecodeError, match="Unterminated comment"):
        parse('{"a": 1 /* and then')


def test_a_missing_comma_is_still_a_missing_comma() -> None:
    with pytest.raises(json.JSONDecodeError, match="delimiter"):
        parse('{"a": 1 "b": 2}')


def test_trailing_content_is_refused() -> None:
    with pytest.raises(json.JSONDecodeError, match="Extra data"):
        parse('{"a": 1} {"b": 2}')


# -- editing without rewriting -----------------------------------------------


def test_a_new_member_goes_in_after_the_opening_brace() -> None:
    """Appending at the end would have to put a comma after the last value, and
    where a ``//`` comment trails that line the comma lands inside it."""
    text = '{\n  "a": 1  // mine\n}\n'
    merged = apply_edits(text, [insert_members(text, parse(text), {"b": 2})])
    assert merged == '{\n  "b": 2,\n  "a": 1  // mine\n}\n'
    assert parse(merged).value == {"b": 2, "a": 1}


def test_an_appended_item_takes_its_comma_before_a_trailing_comment() -> None:
    text = '[\n  "a"  // mine\n]\n'
    merged = apply_edits(text, [append_items(text, parse(text), ["b"])])
    assert "// mine" in merged
    assert parse(merged).value == ["a", "b"]


def test_an_empty_container_is_filled_as_json_dumps_would_lay_it_out() -> None:
    text = '{"x": []}'
    node = parse(text).member("x")
    assert node is not None
    assert apply_edits(text, [append_items(text, node, [1])]) == '{"x": [\n  1\n]}'


def test_an_inline_container_stays_inline() -> None:
    text = '{"x": ["a"]}'
    node = parse(text).member("x")
    assert node is not None
    assert apply_edits(text, [append_items(text, node, ["b"])]) == '{"x": ["a", "b"]}'


def test_a_replaced_value_is_written_at_its_own_indentation() -> None:
    text = '{\n  "a": {\n    "b": 1\n  }\n}\n'
    node = parse(text).member("a")
    assert node is not None
    merged = apply_edits(text, [replace_value(text, node, {"b": 2, "c": [3]})])
    assert merged == '{\n  "a": {\n    "b": 2,\n    "c": [\n      3\n    ]\n  }\n}\n'


def test_edits_are_applied_back_to_front() -> None:
    """Each is measured against the original text, so an earlier one must not
    move the offsets a later one was measured at."""
    text = '{"a": 1, "b": 2}'
    document = parse(text)
    first, second = document.member("a"), document.member("b")
    assert first is not None and second is not None
    edits = [replace_value(text, first, "one"), replace_value(text, second, "two")]
    assert apply_edits(text, edits) == '{"a": "one", "b": "two"}'


def test_a_newline_in_a_string_is_never_treated_as_layout() -> None:
    """Re-indenting a rendered value walks its newlines; one inside a string is
    escaped by :func:`json.dumps`, so it cannot be reached."""
    text = '{\n  "a": 1\n}\n'
    merged = apply_edits(text, [insert_members(text, parse(text), {"b": "x\ny"})])
    assert parse(merged).value == {"a": 1, "b": "x\ny"}


def test_an_insertion_is_an_empty_span() -> None:
    assert apply_edits("ac", [Edit(1, 1, "b")]) == "abc"
