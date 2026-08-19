"""Rust pretty-printers for a seat that ships no Rust toolchain.

A Rust binary names ``gdb_load_rust_pretty_printers.py`` in its
``.debug_gdb_scripts`` section, and that file lives in a rustup toolchain's
``lib/rustlib/etc``. A production container has no rustup toolchain and this
image ships no Rust at all, so the reference resolves to nothing and ``Vec``,
``String`` and ``Option`` come out as the ``RawVecInner``/``Unique``/``NonNull``
nest they are made of: legible, entirely wrong-looking, and no use for deciding
anything. These four printers cover exactly the three types that costs you.

**Nothing here may print a wrong value.** Every decision that could be wrong is
taken in :func:`_lookup`, *before* a printer object exists — if any of it fails
or looks unfamiliar the lookup returns ``None`` and gdb renders the value its
own way, which is the behaviour this file replaces. That matters more than
coverage: a ``Vec`` shown as a struct is an inconvenience, and a ``Vec`` shown
with the wrong three elements is a wasted afternoon.

The layouts are read structurally rather than by version, because std has moved
the data pointer twice (``RawVec`` grew a ``RawVecInner`` in 1.76) and a seat
cannot know which std a workload was built against.

Sourced by :data:`podbench.gdbcmd.RUST_PRETTY_PRINTERS`, and only for a target
:mod:`podbench.elf` identified as Rust.
"""

from __future__ import annotations

import gdb

_TAG = "podbench-rust"

_MAX_STRING = 1 << 16
"""Longest ``String`` read out of the inferior. A cap on a *claimed* length,
which is what a garbage or half-initialised header gives you."""

_MAX_ELEMENTS = 4096
"""Ceiling on the elements offered to gdb. gdb stops at ``print elements``
long before this; the cap is here for a length field that is not a length."""

_POINTER_FIELDS = ("ptr", "pointer", "inner", "buf", "__0", "_0")
"""The field names std nests its data pointer behind, outermost first.

Followed by name rather than by searching for any pointer in the struct: an
allocator field can hold one too, and picking it would produce a plausible
``Vec`` of the wrong memory.
"""


def _field_names(value_type):
    """The named fields of a type, or ``()`` for anything without any."""
    try:
        return tuple(field.name for field in value_type.fields() if field.name)
    except (TypeError, AttributeError, gdb.error):
        return ()


def _pointer_of(value, depth=6):
    """The data pointer buried in a ``RawVec``, whatever this std calls it."""
    for _ in range(depth):
        value_type = value.type.strip_typedefs()
        if value_type.code == gdb.TYPE_CODE_PTR:
            return value
        names = _field_names(value_type)
        step = next((name for name in _POINTER_FIELDS if name in names), None)
        if step is None:
            return None
        value = value[step]
    return None


def _buffer_of(value):
    """``(address, length, element type)`` for a ``Vec``-shaped value.

    ``None`` for anything that does not look exactly like one, which is what
    keeps an unfamiliar layout rendering as gdb's own struct dump.
    """
    try:
        length = int(value["len"])
        pointer = _pointer_of(value["buf"])
        element = value.type.strip_typedefs().template_argument(0)
    except (gdb.error, RuntimeError, IndexError, KeyError, ValueError):
        return None
    if pointer is None or length < 0:
        return None
    return pointer.cast(element.pointer()), length, element


def _type_name(value):
    """The type's Rust name, following one reference if there is one."""
    try:
        value_type = value.type.strip_typedefs()
        if value_type.code == gdb.TYPE_CODE_REF:
            value_type = value_type.target().strip_typedefs()
        return value_type.tag or str(value_type)
    except (gdb.error, AttributeError):
        return None


def _unqualify(text, prefix):
    """``Some(5)`` from ``core::option::Option<i32>::Some(5)``.

    The closing angle bracket is found by counting rather than by searching,
    because ``Option<Vec<i32>>`` and ``Option<Option<i32>>`` both contain a
    ``>::`` that is not the one being looked for.
    """
    if not text.startswith(prefix):
        return None
    depth = 0
    for index, character in enumerate(text):
        if character == "<":
            depth += 1
        elif character == ">":
            depth -= 1
            if depth == 0:
                rest = text[index + 1 :]
                return rest[2:] if rest.startswith("::") else None
    return None


class _Literal:
    """A printer for text already decided on. Holds no gdb state."""

    def __init__(self, text, hint=None):
        self._text = text
        self._hint = hint

    def to_string(self):
        return self._text

    def display_hint(self):
        return self._hint


class _VecPrinter:
    """``Vec(size=3) = {1, 2, 3}``, the spelling rustc's own printers use."""

    def __init__(self, pointer, length, element):
        self._pointer = pointer
        self._length = length
        self._element = element

    def to_string(self):
        return f"Vec(size={self._length})"

    def display_hint(self):
        return "array"

    def children(self):
        for index in range(min(self._length, _MAX_ELEMENTS)):
            yield str(index), (self._pointer + index).dereference()


def _string_printer(value):
    """``String`` as its text, read out of the inferior in one go."""
    try:
        buffer = _buffer_of(value["vec"])
    except (gdb.error, KeyError):
        return None
    if buffer is None:
        return None
    pointer, length, _ = buffer
    if length == 0:
        return _Literal("", "string")
    if length > _MAX_STRING:
        return None
    try:
        raw = gdb.selected_inferior().read_memory(pointer, length).tobytes()
    except (gdb.error, OverflowError, MemoryError):
        return None
    # `replace`, never `strict`: a String read mid-mutation is not valid UTF-8
    # and a decoder that raised would take the whole backtrace with it.
    return _Literal(raw.decode("utf-8", "replace"), "string")


def _vec_printer(value):
    buffer = _buffer_of(value)
    return None if buffer is None else _VecPrinter(*buffer)


def _option_printer(value):
    """``Some(5)`` rather than ``core::option::Option<i32>::Some(5)``.

    gdb's own Rust support already reads the variant correctly, so this only
    takes the path off the front — which is the whole difference between a
    readable ``Vec<Option<T>>`` and a column of qualified names.
    """
    try:
        text = value.format_string(raw=True)
    except (gdb.error, TypeError, AttributeError):
        return None
    short = _unqualify(text, "core::option::Option<")
    return None if short is None else _Literal(short)


def _lookup(value):
    """Every guess this file makes is made here, and every failure is a None."""
    name = _type_name(value)
    if name is None:
        return None
    if name.startswith("alloc::string::String"):
        return _string_printer(value)
    if name.startswith("alloc::vec::Vec<"):
        return _vec_printer(value)
    if name.startswith("core::option::Option<"):
        return _option_printer(value)
    return None


def register():
    """Install the lookup, replacing an earlier copy rather than stacking one.

    ``source`` runs again on every re-attach in the same gdb, and a lookup
    registered twice is a lookup called twice.
    """
    _lookup.podbench = _TAG
    kept = [
        printer
        for printer in gdb.pretty_printers
        if getattr(printer, "podbench", None) != _TAG
    ]
    gdb.pretty_printers[:] = [*kept, _lookup]


register()
