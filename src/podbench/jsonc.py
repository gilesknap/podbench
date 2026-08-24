"""Read JSON with comments and trailing commas, and edit it without rewriting it.

VS Code's own ``.vscode/*.json`` files are JSONC — its scaffold writes ``//``
comments into ``launch.json``, and its editor tolerates a trailing comma — while
:mod:`json` accepts neither. Measured 2026-08-24 against a hotfixed
``bl47p-ea-fastcs-01-0``, whose checkout is what ``podbench vscode`` opens: the
application ships all four files committed and unmodified, and both merges
podbench attempted refused, one on a comment and one on a comma
(``.claude/evidence/phase7-vscode-and-the-two-failures.md`` §3). On a hotfixed
pod that is the common case, and no ``launch.json`` means no F5.

**Parsing is for deciding; writing is a textual edit.** The obvious
implementation — parse the dialect, then ``json.dumps`` the result — is a worse
outcome than the refusal it replaces: it is the user's committed file, and it
would come back with the comments gone, the keys reordered and the whole
document reformatted. So :func:`parse` returns the *spans* the values came from,
and :func:`insert_members`, :func:`append_items` and :func:`replace_value` return
:class:`Edit`\\ s that touch only podbench's own entries. Every byte the user
wrote is left where it was. The bridge's shim reached the same shape for the same
reason (``tools/vscode-bridge/README.md``).

New members go in directly after the opening brace, which looks arbitrary and is
not: appending at the end has to put a comma after the last member's value, and
where that value is followed by a ``//`` comment on the same line the comma lands
*inside* the comment and the file stops parsing. There is no such position after
an opening brace. Arrays are appended to rather than prepended, because order is
meaning there — but the comma still goes immediately after the last item's value
rather than at the end of its line, for the same reason.

Anything that is not JSONC either still raises, with :mod:`json`'s own wording:
the errors below are :class:`json.JSONDecodeError`, so a caller's message and its
``line … column …`` are what they were before this module existed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast

__all__ = [
    "INDENT",
    "Edit",
    "Member",
    "Node",
    "append_items",
    "apply_edits",
    "insert_members",
    "parse",
    "replace_value",
]

INDENT = 2
"""Spaces per level in rendered text, matching every other document podbench
writes — so a merge into a file podbench itself wrote is byte-identical to the
``json.dumps(..., indent=2)`` it would have produced."""

_DECODER = json.JSONDecoder()
"""Strings, numbers and the three keywords are decoded by the standard library
rather than by hand: a scalar reader of our own would be a second, subtly
different implementation of escape sequences and number grammar."""

_WHITESPACE = " \t\n\r"


@dataclass(frozen=True)
class Member:
    """One ``"key": value`` pair, and where its key began."""

    key: str
    start: int
    value: Node


@dataclass(frozen=True)
class Node:
    """A parsed value, the span of source it came from, and its children.

    ``items is None`` means "not an array" and ``members is None`` "not an
    object"; an empty array carries ``()``, which is a different question from
    the one a caller replacing a value of the wrong shape is asking.
    """

    start: int
    end: int
    value: Any
    members: tuple[Member, ...] | None = None
    items: tuple[Node, ...] | None = None

    def member(self, key: str) -> Node | None:
        """The value node for ``key``, or ``None`` if the object has no such key.

        The *last* duplicate wins, because that is the one
        :func:`json.loads` keeps and this node's :attr:`value` therefore holds.
        """
        found: Node | None = None
        for member in self.members or ():
            if member.key == key:
                found = member.value
        return found


def parse(text: str) -> Node:
    """The document in ``text``, with comments and trailing commas allowed.

    Raises :class:`json.JSONDecodeError` — a :class:`ValueError` — on anything
    that is not JSONC either.

    >>> node = parse('{\\n  // a comment\\n  "a": [1, 2,],\\n}')
    >>> node.value
    {'a': [1, 2]}
    >>> parse('{ "a": 1 }').member("a").value
    1
    >>> try:
    ...     parse("{ 'a': 1 }")
    ... except ValueError as error:
    ...     print(error)
    Expecting property name enclosed in double quotes: line 1 column 3 (char 2)
    """
    index = _skip(text, 0)
    node, index = _value(text, index)
    index = _skip(text, index)
    if index != len(text):
        raise json.JSONDecodeError("Extra data", text, index)
    return node


def _skip(text: str, index: int) -> int:
    """Past whitespace and comments, to the next thing that means something."""
    while index < len(text):
        if text[index] in _WHITESPACE:
            index += 1
        elif text.startswith("//", index):
            line_end = text.find("\n", index)
            index = len(text) if line_end < 0 else line_end + 1
        elif text.startswith("/*", index):
            comment_end = text.find("*/", index + 2)
            if comment_end < 0:
                raise json.JSONDecodeError("Unterminated comment", text, index)
            index = comment_end + 2
        else:
            break
    return index


def _value(text: str, index: int) -> tuple[Node, int]:
    if index >= len(text):
        raise json.JSONDecodeError("Expecting value", text, index)
    if text[index] == "{":
        return _object(text, index)
    if text[index] == "[":
        return _array(text, index)
    # Everything else is a scalar, and `raw_decode` never sees a `{` or a `[`
    # here — which matters, because it would parse one *strictly* and refuse the
    # comment three lines further in.
    value, end = _DECODER.raw_decode(text, index)
    return Node(index, end, value), end


def _object(text: str, index: int) -> tuple[Node, int]:
    start = index
    members: list[Member] = []
    index = _skip(text, index + 1)
    while True:
        if index < len(text) and text[index] == "}":
            value = {member.key: member.value.value for member in members}
            return Node(start, index + 1, value, members=tuple(members)), index + 1
        if index >= len(text) or text[index] != '"':
            raise json.JSONDecodeError(
                "Expecting property name enclosed in double quotes", text, index
            )
        key_start = index
        key, index = cast("tuple[str, int]", _DECODER.raw_decode(text, index))
        index = _skip(text, index)
        if index >= len(text) or text[index] != ":":
            raise json.JSONDecodeError("Expecting ':' delimiter", text, index)
        node, index = _value(text, _skip(text, index + 1))
        members.append(Member(key, key_start, node))
        index = _skip(text, index)
        # A comma here may be the separator or the trailing one VS Code writes;
        # the loop cannot tell them apart and does not need to, because the top
        # of it accepts the closing brace.
        if index < len(text) and text[index] == ",":
            index = _skip(text, index + 1)
        elif index >= len(text) or text[index] != "}":
            raise json.JSONDecodeError("Expecting ',' delimiter", text, index)


def _array(text: str, index: int) -> tuple[Node, int]:
    start = index
    items: list[Node] = []
    index = _skip(text, index + 1)
    while True:
        if index < len(text) and text[index] == "]":
            value = [item.value for item in items]
            return Node(start, index + 1, value, items=tuple(items)), index + 1
        node, index = _value(text, index)
        items.append(node)
        index = _skip(text, index)
        if index < len(text) and text[index] == ",":
            index = _skip(text, index + 1)
        elif index >= len(text) or text[index] != "]":
            raise json.JSONDecodeError("Expecting ',' delimiter", text, index)


@dataclass(frozen=True)
class Edit:
    """Replace ``text[start:end]``. An insertion is an empty span."""

    start: int
    end: int
    text: str


def apply_edits(text: str, edits: Iterable[Edit]) -> str:
    """``text`` with every edit applied.

    Back to front, so the offsets an earlier edit was measured against are still
    the offsets of the text it is applied to.

    >>> apply_edits('{"a": 1}', [Edit(1, 1, '"b": 2, ')])
    '{"b": 2, "a": 1}'
    """
    for edit in sorted(edits, key=lambda edit: edit.start, reverse=True):
        text = text[: edit.start] + edit.text + text[edit.end :]
    return text


def insert_members(text: str, node: Node, members: Mapping[str, Any]) -> Edit:
    """Add ``members`` to the object ``node``, directly after its opening brace.

    See the module docstring for why the front and not the end.

    >>> text = '{\\n  "a": 1  // mine\\n}'
    >>> print(apply_edits(text, [insert_members(text, parse(text), {"b": 2})]))
    {
      "b": 2,
      "a": 1  // mine
    }
    """
    pad = _child_indent(text, node)
    block = [
        f"{json.dumps(key)}: {_render(value, pad)}" for key, value in members.items()
    ]
    if not node.members:
        return _fill_empty(text, node, block, pad)
    after = node.start + 1
    if _inline(text, node):
        return Edit(after, after, "".join(f"{item}, " for item in block))
    return Edit(after, after, f"\n{pad}" + f",\n{pad}".join(block) + ",")


def append_items(text: str, node: Node, items: Iterable[Any]) -> Edit:
    """Add ``items`` to the end of the array ``node``.

    The comma goes immediately after the last item's value rather than at the end
    of its line, so a comment trailing that line is neither swallowed nor moved.

    >>> text = '["a"]'
    >>> apply_edits(text, [append_items(text, parse(text), ["b"])])
    '["a", "b"]'
    """
    pad = _child_indent(text, node)
    block = [_render(value, pad) for value in items]
    if not node.items:
        return _fill_empty(text, node, block, pad)
    end = node.items[-1].end
    separator = ", " if _inline(text, node) else f",\n{pad}"
    return Edit(end, end, "".join(f"{separator}{item}" for item in block))


def replace_value(text: str, node: Node, value: Any) -> Edit:
    """Put ``value`` where ``node`` is, at ``node``'s own indentation."""
    return Edit(node.start, node.end, _render(value, _line_indent(text, node.start)))


def _fill_empty(text: str, node: Node, block: list[str], pad: str) -> Edit:
    """The first entries of an empty container, laid out as ``json.dumps`` would.

    Written as an insertion after the bracket rather than a replacement of the
    whole span, so ``{ /* nothing yet */ }`` keeps its comment.
    """
    closing = _line_indent(text, node.start)
    return Edit(
        node.start + 1,
        node.start + 1,
        f"\n{pad}" + f",\n{pad}".join(block) + f"\n{closing}",
    )


def _render(value: Any, pad: str) -> str:
    """``value`` as JSON, every line after the first indented to sit under ``pad``.

    A literal newline in :func:`json.dumps` output is always layout — one inside
    a string is escaped — so re-indenting cannot reach the user's data.
    """
    return json.dumps(value, indent=INDENT).replace("\n", "\n" + pad)


def _inline(text: str, node: Node) -> bool:
    """Whether ``node`` is written on one line, which its additions then are."""
    return "\n" not in text[node.start : node.end]


def _child_indent(text: str, node: Node) -> str:
    """What a child of ``node`` is indented by: the existing children's own
    indentation where they start their lines, and one level in from ``node``
    otherwise."""
    first = node.members[0].start if node.members else None
    if first is None and node.items:
        first = node.items[0].start
    if first is not None and not text[_line_start(text, first) : first].strip():
        return _line_indent(text, first)
    return _line_indent(text, node.start) + " " * INDENT


def _line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


def _line_indent(text: str, index: int) -> str:
    start = end = _line_start(text, index)
    while end < len(text) and text[end] in " \t":
        end += 1
    return text[start:end]
