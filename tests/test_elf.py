"""Tests for the minimal ELF reader.

The binaries are synthesised here rather than taken from the machine the suite
runs on: the point of the reader is that it answers the same way for an arm64
target read from an amd64 seat, and a test that reads ``/usr/bin/gdb`` proves
only that today's runner is x86.

Everything the reader is asked is a *debugger* question — which machine, is
there DWARF, is it Go — so each test names the wrong answer it prevents.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from podbench.elf import debugpy_helper_name, machine_name, read_elf

EM_X86_64 = 0x3E
EM_AARCH64 = 0xB7


def build_elf(
    sections: list[str],
    *,
    machine: int = EM_X86_64,
    is_64: bool = True,
    contents: dict[str, bytes] | None = None,
) -> bytes:
    """A valid-enough ELF carrying exactly these section names.

    ``contents`` gives a named section real bytes at a real offset. Two of the
    reader's questions are answered by what is *in* a section rather than by
    whether it exists — ``.comment`` names the compiler and ``.strtab`` carries
    the mangling — and a fixture of empty sections cannot ask either of them.
    """
    payloads = dict(contents or {})
    names = ["", *sections, ".shstrtab"]
    strtab = b""
    offsets: list[int] = []
    for name in names:
        offsets.append(len(strtab))
        strtab += name.encode() + b"\0"

    header_size = 64 if is_64 else 52
    entry_size = 64 if is_64 else 40
    count = len(names)
    # Always 64: the reader takes a single 64-byte header read, so a 32-bit
    # header is padded to that length and the table follows the padding.
    shoff = 64
    strtab_offset = shoff + entry_size * count
    shstrndx = count - 1

    header = bytearray(header_size)
    header[0:4] = b"\x7fELF"
    header[4] = 2 if is_64 else 1
    header[5] = 1  # little-endian
    header[6] = 1
    struct.pack_into("<H", header, 16, 2)  # ET_EXEC
    struct.pack_into("<H", header, 18, machine)
    if is_64:
        struct.pack_into("<Q", header, 0x28, shoff)
        struct.pack_into("<3H", header, 0x3A, entry_size, count, shstrndx)
    else:
        struct.pack_into("<I", header, 0x20, shoff)
        struct.pack_into("<3H", header, 0x2E, entry_size, count, shstrndx)

    # Section bodies follow the string table, so their offsets are known before
    # the headers that name them are packed.
    bodies = b""
    placed: dict[str, tuple[int, int]] = {}
    for name, payload in payloads.items():
        placed[name] = (strtab_offset + len(strtab) + len(bodies), len(payload))
        bodies += payload

    table = b""
    for index in range(count):
        entry = bytearray(entry_size)
        struct.pack_into("<I", entry, 0, offsets[index])
        offset, size = placed.get(names[index], (0, 0))
        if index == shstrndx:
            offset, size = strtab_offset, len(strtab)
        if is_64:
            struct.pack_into("<2Q", entry, 0x18, offset, size)
        else:
            struct.pack_into("<2I", entry, 0x10, offset, size)
        table += bytes(entry)

    # The header must be 64 bytes for the reader's single read, so a 32-bit
    # header is padded out to the same length rather than special-cased.
    return bytes(header).ljust(64, b"\0") + table + strtab + bodies


def write(tmp_path: Path, data: bytes, name: str = "prog") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# -- the architecture axis --------------------------------------------------


def test_machine_comes_from_the_binary_not_the_reader(tmp_path: Path) -> None:
    """An arm64 target is arm64 however it is read.

    This is the axis debugpy's attach helper turns on, and asking the *node* or
    ``platform.machine()`` instead would answer for the seat.
    """
    info = read_elf(write(tmp_path, build_elf([], machine=EM_AARCH64)))
    assert info is not None
    assert info.machine == "aarch64"


def test_unknown_machines_are_named_rather_than_guessed() -> None:
    assert machine_name(0x99) == "unknown-0x99"


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("x86_64", "attach_linux_amd64.so"),
        ("aarch64", "attach_linux_arm64.so"),
    ],
)
def test_debugpy_helper_names(machine: str, expected: str) -> None:
    """The filename is the message: only the amd64 one is ever published."""
    assert debugpy_helper_name(machine) == expected


# -- what the sections say --------------------------------------------------


def test_debug_info_and_build_id_are_reported_separately(tmp_path: Path) -> None:
    """They are different answers: no DWARF but a build-id is still debuggable.

    debuginfod serves symbols against the build-id (report §3.2), so collapsing
    the two would make podbench refuse a target S3 debugged successfully.
    """
    info = read_elf(write(tmp_path, build_elf([".note.gnu.build-id"])))
    assert info is not None
    assert info.has_build_id
    assert not info.has_debug_info


def test_go_is_recognised_by_the_pclntab(tmp_path: Path) -> None:
    info = read_elf(write(tmp_path, build_elf([".gopclntab", ".text"])))
    assert info is not None
    assert info.is_go


def test_a_c_binary_is_not_go(tmp_path: Path) -> None:
    info = read_elf(write(tmp_path, build_elf([".text", ".debug_info"])))
    assert info is not None
    assert info.is_go is False
    assert info.has_debug_info


MANGLED = b"\x00_ZN4core3fmt5write17h5f7c9a1b2d3e4f60E\x00"
"""One rustc-mangled symbol as it sits in a ``.strtab``: NUL-delimited, and
carrying the ``17h`` + sixteen hex digits that nothing else emits."""


def test_rust_is_recognised_from_its_own_section(tmp_path: Path) -> None:
    """The cheapest of the three signals, and the one that is usually absent."""
    info = read_elf(write(tmp_path, build_elf([".rustc", ".text"])))
    assert info is not None
    assert info.is_rust


def test_rust_is_recognised_from_the_producer_string(tmp_path: Path) -> None:
    """What survives when the linker drops ``.rustc`` from an executable."""
    elf = build_elf(
        [".comment", ".text"],
        contents={
            ".comment": b"GCC: (Debian 12.2.0) 12.2.0\x00rustc version 1.83.0\x00"
        },
    )
    info = read_elf(write(tmp_path, elf))
    assert info is not None
    assert info.is_rust
    assert info.producer is not None
    assert "rustc" in info.producer


def test_rust_is_recognised_from_a_mangled_symbol(tmp_path: Path) -> None:
    """The last signal: no ``.rustc``, no ``.comment``, and a symbol table."""
    elf = build_elf([".strtab", ".text"], contents={".strtab": MANGLED})
    info = read_elf(write(tmp_path, elf))
    assert info is not None
    assert info.rust_mangled
    assert info.is_rust


def test_a_cpp_binary_is_not_mistaken_for_rust(tmp_path: Path) -> None:
    """Itanium mangling is the same ``_ZN`` prefix, and is not the same thing.

    Rust is *served* rather than refused, so a false positive here does not
    withdraw a configuration - it sources the wrong printers into a C++ session
    and makes gdb look broken.
    """
    elf = build_elf(
        [".strtab", ".comment", ".text"],
        contents={
            ".strtab": b"\x00_ZN6thingy4makeEii\x00_ZNSt6vectorIiEC1Ev\x00",
            ".comment": b"GCC: (Debian 12.2.0) 12.2.0\x00",
        },
    )
    info = read_elf(write(tmp_path, elf))
    assert info is not None
    assert info.is_rust is False


def test_a_shallow_read_skips_the_rust_signals(tmp_path: Path) -> None:
    """What the mapped-object inventory asks with, and why it is cheaper.

    ``deep=False`` still answers every symbol question, which is all the
    inventory wants of several hundred shared objects - and it does it without
    reading a string table out of another container per file.
    """
    elf = build_elf([".strtab", ".symtab"], contents={".strtab": MANGLED})
    info = read_elf(write(tmp_path, elf), deep=False)
    assert info is not None
    assert info.has_symbols
    assert info.rust_mangled is False


def test_symbols_are_a_weaker_question_than_debug_info(tmp_path: Path) -> None:
    """A stripped-of-DWARF library with a ``.dynsym`` still names its frames.

    The difference is what stops a JVM being told "expect addresses rather than
    names" while ``libjvm.so`` sits in its address space with 65,843 symbols.
    """
    info = read_elf(write(tmp_path, build_elf([".dynsym", ".text"])))
    assert info is not None
    assert info.has_symbols
    assert not info.has_debug_info


def test_32_bit_binaries_parse(tmp_path: Path) -> None:
    info = read_elf(write(tmp_path, build_elf([".debug_info"], is_64=False)))
    assert info is not None
    assert info.has_debug_info


# -- refusals are answers ---------------------------------------------------


def test_a_shell_script_is_not_elf(tmp_path: Path) -> None:
    assert read_elf(write(tmp_path, b"#!/bin/sh\necho hi\n")) is None


def test_a_missing_file_is_none(tmp_path: Path) -> None:
    """An unreadable ``/proc/<pid>/root`` must not raise inside a diagnostic."""
    assert read_elf(tmp_path / "absent") is None


def test_a_truncated_binary_is_none(tmp_path: Path) -> None:
    assert read_elf(write(tmp_path, build_elf([".text"])[:40])) is None


def test_a_binary_whose_table_is_cut_short_reports_no_sections(
    tmp_path: Path,
) -> None:
    """A stripped-to-the-bone binary is "no information", not "no debug info"."""
    data = build_elf([".text", ".debug_info"])
    info = read_elf(write(tmp_path, data[:80]))
    assert info is not None
    assert info.sections_readable is False
    assert info.has_debug_info is False
