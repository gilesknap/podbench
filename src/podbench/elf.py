"""Just enough ELF to answer the three questions a debugger flavour turns on.

``debug-config`` has to decide *which* debugger to emit before it emits
anything, and the honest inputs are all in the target's own binary rather than
in anything the user typed:

* **which machine it was built for** — the debugpy attach helper is
  ``attach_linux_amd64.so`` and there is no arm64 equivalent (issue #20), so the
  architecture axis is decided by the binary, not by the node label. A pod can
  perfectly well run an amd64 image under emulation, and the node's arch would
  then be the wrong answer.
* **whether it carries debug information** — a stripped binary attaches fine and
  then shows addresses, which is the same silently-plausible-and-wrong outcome
  the rest of this codebase exists to prevent.
* **whether it is a Go program** — gdb can attach to one, but goroutines,
  channels and Go's own stack layout are delve's job.

Reading section headers by hand rather than shelling out to ``readelf``: the
unit suite may not shell out at all, ``eu-readelf`` and ``readelf`` disagree on
output format, and the whole parse is the sixty lines below. Nothing here
raises — an unreadable, truncated or exotic file is reported as "no
information", because a refusal is an answer everywhere else in this package
too.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

__all__ = [
    "GO_SECTIONS",
    "ElfInfo",
    "debugpy_helper_name",
    "machine_name",
    "read_elf",
]

_MAGIC = b"\x7fELF"

#: ``e_machine`` to the spelling :func:`platform.machine` would use for the same
#: CPU, so that a target's architecture and this container's are comparable
#: without a second table.
_MACHINES = {
    0x03: "i686",
    0x28: "arm",
    0x3E: "x86_64",
    0xB7: "aarch64",
    0xF3: "riscv64",
}

#: Either of these is conclusive: the Go toolchain emits the pclntab in every
#: binary it links, and the build-id note in everything since Go 1.10.
GO_SECTIONS = frozenset({".gopclntab", ".note.go.buildid"})

_DEBUG_SECTION = ".debug_info"
_BUILD_ID_SECTION = ".note.gnu.build-id"

#: A cap on the section header table, so a corrupt ``e_shnum`` cannot turn a
#: capability question into a multi-gigabyte read.
_MAX_SECTIONS = 4096


@dataclass(frozen=True)
class ElfInfo:
    """What one ELF file says about itself.

    ``sections`` is empty for a binary whose section headers were stripped
    entirely, which is *not* the same as "no debug info" — hence
    :attr:`sections_readable`.
    """

    machine: str
    """:func:`platform.machine`'s spelling, or ``unknown-0x<e_machine>``."""

    sections: frozenset[str]
    sections_readable: bool = True

    @property
    def has_debug_info(self) -> bool:
        """Whether DWARF is present in the file itself.

        ``False`` does not mean debugging is hopeless — a build-id plus
        debuginfod recovers the symbols (report §3.2) — only that the file
        alone will show addresses.
        """
        return _DEBUG_SECTION in self.sections

    @property
    def has_build_id(self) -> bool:
        """Whether debuginfod has anything to look the binary up by."""
        return _BUILD_ID_SECTION in self.sections

    @property
    def is_go(self) -> bool:
        """Whether the Go toolchain linked this binary."""
        return bool(self.sections & GO_SECTIONS)


def machine_name(e_machine: int) -> str:
    """Name an ``e_machine`` value the way :mod:`platform` would.

    >>> machine_name(0x3E)
    'x86_64'
    >>> machine_name(0xB7)
    'aarch64'
    >>> machine_name(0x99)
    'unknown-0x99'
    """
    return _MACHINES.get(e_machine, f"unknown-0x{e_machine:x}")


def debugpy_helper_name(machine: str) -> str:
    """The attach helper debugpy would need for a target on ``machine``.

    Named rather than inferred at the call site because this filename *is* the
    architecture axis: debugpy ships ``attach_linux_amd64.so`` and publishes no
    aarch64 Linux wheel at all, so on arm64 the file the message names is one
    that exists nowhere rather than one that is merely missing here.

    >>> debugpy_helper_name("x86_64")
    'attach_linux_amd64.so'
    >>> debugpy_helper_name("aarch64")
    'attach_linux_arm64.so'
    """
    suffix = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    return f"attach_linux_{suffix.get(machine, machine)}.so"


def read_elf(path: Path | str) -> ElfInfo | None:
    """Parse ``path`` far enough to answer the three questions, or ``None``.

    ``None`` means "not an ELF file, or not readable" — a shell script, a
    denied ``/proc/<pid>/root`` and a truncated binary are all the same answer
    to the caller, which is that this file decides nothing.
    """
    try:
        with open(path, "rb") as stream:
            header = stream.read(64)
            if not header.startswith(_MAGIC) or len(header) < 64:
                return None
            is_64 = header[4] == 2
            endian = "<" if header[5] == 1 else ">"
            (e_machine,) = struct.unpack_from(f"{endian}H", header, 18)
            table = _section_table(header, endian=endian, is_64=is_64)
            if table is None:
                return ElfInfo(machine_name(e_machine), frozenset(), False)
            names = _section_names(stream, table, endian=endian, is_64=is_64)
    except OSError:
        return None
    except struct.error:
        # A file that starts with the magic but is too short to hold what its
        # own header promises. Reported as no information rather than as a
        # crash in a diagnostic tool.
        return None
    if names is None:
        return ElfInfo(machine_name(e_machine), frozenset(), False)
    return ElfInfo(machine_name(e_machine), names)


@dataclass(frozen=True)
class _SectionTable:
    offset: int
    entry_size: int
    count: int
    name_index: int


def _section_table(header: bytes, *, endian: str, is_64: bool) -> _SectionTable | None:
    """Where the section headers are, or ``None`` when there are none."""
    if is_64:
        offset, entry_size, count, name_index = struct.unpack_from(
            f"{endian}Q", header, 0x28
        ) + struct.unpack_from(f"{endian}3H", header, 0x3A)
    else:
        offset, entry_size, count, name_index = struct.unpack_from(
            f"{endian}I", header, 0x20
        ) + struct.unpack_from(f"{endian}3H", header, 0x2E)
    # e_shnum 0 is the extended form (the real count lives in section 0's
    # sh_size) as well as the "no sections at all" form. Both are rare enough
    # that declining to parse is better than a second code path nothing tests.
    if not offset or not count or count > _MAX_SECTIONS or name_index >= count:
        return None
    return _SectionTable(offset, entry_size, count, name_index)


def _section_names(
    stream: BinaryIO, table: _SectionTable, *, endian: str, is_64: bool
) -> frozenset[str] | None:
    """The names of every section, read through the string table."""
    stream.seek(table.offset)
    entries = stream.read(table.entry_size * table.count)
    if len(entries) < table.entry_size * table.count:
        return None

    def entry(index: int) -> tuple[int, int, int]:
        base = index * table.entry_size
        (name,) = struct.unpack_from(f"{endian}I", entries, base)
        if is_64:
            offset, size = struct.unpack_from(f"{endian}2Q", entries, base + 0x18)
        else:
            offset, size = struct.unpack_from(f"{endian}2I", entries, base + 0x10)
        return name, offset, size

    _, strtab_offset, strtab_size = entry(table.name_index)
    if strtab_size > 1 << 20:  # a sane cap; real .shstrtab is a few hundred bytes
        return None
    stream.seek(strtab_offset)
    strings = stream.read(strtab_size)

    names: set[str] = set()
    for index in range(table.count):
        name_offset, _, _ = entry(index)
        end = strings.find(b"\0", name_offset)
        if name_offset >= len(strings) or end < 0:
            continue
        names.add(strings[name_offset:end].decode("ascii", "replace"))
    return frozenset(names)
