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
    sections: list[str], *, machine: int = EM_X86_64, is_64: bool = True
) -> bytes:
    """A valid-enough ELF carrying exactly these section names."""
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

    table = b""
    for index in range(count):
        entry = bytearray(entry_size)
        struct.pack_into("<I", entry, 0, offsets[index])
        offset = strtab_offset if index == shstrndx else 0
        size = len(strtab) if index == shstrndx else 0
        if is_64:
            struct.pack_into("<2Q", entry, 0x18, offset, size)
        else:
            struct.pack_into("<2I", entry, 0x10, offset, size)
        table += bytes(entry)

    # The header must be 64 bytes for the reader's single read, so a 32-bit
    # header is padded out to the same length rather than special-cased.
    return bytes(header).ljust(64, b"\0") + table + strtab


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
