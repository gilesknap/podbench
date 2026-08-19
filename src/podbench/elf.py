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
* **whether it is a Rust program** — the debugger is the same one, but ``Vec``,
  ``String`` and ``Option`` are unreadable without the pretty-printers, so the
  answer changes what gdb is told rather than which gdb is chosen.
* **whether it carries a symbol table at all** — asked of every object mapped
  into the target, not only of the main binary, because "no symbols" said of a
  JVM whose ``libjvm.so`` holds sixty thousand of them is simply false.

Reading section headers by hand rather than shelling out to ``readelf``: the
unit suite may not shell out at all, ``eu-readelf`` and ``readelf`` disagree on
output format, and the whole parse is the sixty lines below. Nothing here
raises — an unreadable, truncated or exotic file is reported as "no
information", because a refusal is an answer everywhere else in this package
too.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

__all__ = [
    "GO_SECTIONS",
    "RUST_SECTIONS",
    "ElfInfo",
    "debugpy_helper_name",
    "debugpy_helper_published",
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

#: rustc's crate metadata. Conclusive where it survives, and it often does not:
#: it is emitted for rlibs and dylibs and routinely absent from a linked
#: executable, which is why it is only the first of :attr:`ElfInfo.is_rust`'s
#: three signals rather than the whole test.
RUST_SECTIONS = frozenset({".rustc", ".rustc_meta"})

_DEBUG_SECTION = ".debug_info"
_BUILD_ID_SECTION = ".note.gnu.build-id"
_SYMBOL_SECTIONS = frozenset({".symtab", ".dynsym"})
_COMMENT_SECTION = ".comment"
_STRING_SECTIONS = (".strtab", ".dynstr")

#: rustc's legacy symbol mangling: a trailing ``17h`` length prefix on a
#: sixteen-hex-digit disambiguator, which nothing else emits. The v0 scheme
#: (``_R``-prefixed) is still opt-in, so this is the one that identifies a
#: stock ``cargo build``.
_RUST_MANGLED = re.compile(rb"_ZN[0-9A-Za-z_$.]*17h[0-9a-f]{16}E")

#: How much of a string table is searched for that mangling. Bounded because
#: the search runs against another container's binary from inside a seat, and
#: an unbounded read of a statically linked Rust binary's ``.strtab`` is the
#: kind of allocation that OOMs the seat (skill: vscode-in-a-seat).
_MAX_STRING_SCAN = 1 << 20

#: ``.comment`` holds one producer string per translation unit and is a few
#: hundred bytes in practice; this cap only bounds a corrupt ``sh_size``.
_MAX_COMMENT = 1 << 16

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

    producer: str | None = None
    """What ``.comment`` says compiled this, when it says anything.

    LLVM writes its module producer there, so a ``cargo build`` leaves
    ``rustc version 1.83.0 (...)`` beside the usual ``GCC: (...)``. It is the
    signal that survives when :data:`RUST_SECTIONS` has been linked away, which
    is the normal case for an executable.
    """

    rust_mangled: bool = False
    """Whether a rustc-mangled symbol was seen in this file's string table.

    The last of the three Rust signals and the only one that costs a read of
    real size, so it is asked only when the other two are silent, and only when
    :func:`read_elf` was asked for a deep read.
    """

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
    def has_symbols(self) -> bool:
        """Whether *any* symbol table survives in the file.

        Weaker than :attr:`has_debug_info` and worth a separate answer: a
        stripped-of-DWARF library with a ``.dynsym`` still gives named frames
        without source lines, which is the difference between a backtrace a
        reader can act on and a column of addresses.
        """
        return bool(self.sections & _SYMBOL_SECTIONS)

    @property
    def is_go(self) -> bool:
        """Whether the Go toolchain linked this binary."""
        return bool(self.sections & GO_SECTIONS)

    @property
    def is_rust(self) -> bool:
        """Whether rustc built this binary, on any of three signals.

        Three rather than one because each is absent from a perfectly ordinary
        Rust binary on its own: :data:`RUST_SECTIONS` is usually linked out of
        an executable, ``.comment`` is dropped by ``strip``, and the mangling
        goes with ``.symtab``. Rust is *supported* rather than refused, so a
        miss here costs the pretty-printers and not the configuration — which
        is why three cheap signals are worth more than one exact one.
        """
        if self.sections & RUST_SECTIONS:
            return True
        if self.producer is not None and "rustc" in self.producer:
            return True
        return self.rust_mangled


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


def debugpy_helper_published(machine: str) -> bool:
    """Whether debugpy ships an attach helper for ``machine`` *anywhere*.

    The difference decides whether a missing helper is a wall or a chore: amd64
    is in every wheel, so its absence from a particular tree means that tree is
    an incomplete install and re-installing fixes it, while on anything else
    there is no wheel to install and no remedy inside the pod at all. Calling
    the first case "architecture" would be the false report — "not published for
    x86_64" — that the message wording exists to avoid.

    >>> debugpy_helper_published("x86_64")
    True
    >>> debugpy_helper_published("aarch64")
    False
    """
    return debugpy_helper_name(machine) == "attach_linux_amd64.so"


def read_elf(path: Path | str, *, deep: bool = True) -> ElfInfo | None:
    """Parse ``path`` far enough to answer the questions above, or ``None``.

    ``None`` means "not an ELF file, or not readable" — a shell script, a
    denied ``/proc/<pid>/root`` and a truncated binary are all the same answer
    to the caller, which is that this file decides nothing.

    ``deep=False`` stops after the section names, which answer every question
    except "was this rustc". That is what the mapped-object inventory asks with,
    because it reads every shared object in another container's address space —
    dozens of files for an ordinary service and several hundred for a JVM — and
    only wants to know which of them carry symbols.
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
            sections = _sections(stream, table, endian=endian, is_64=is_64)
            if sections is None:
                return ElfInfo(machine_name(e_machine), frozenset(), False)
            names = frozenset(sections)
            if not deep or names & RUST_SECTIONS:
                # The section name settles it, so neither of the two reads
                # below can change the answer they exist to reach.
                return ElfInfo(machine_name(e_machine), names)
            producer = _text_at(stream, sections.get(_COMMENT_SECTION), _MAX_COMMENT)
            # The string-table scan is the one read of real size here, so it is
            # skipped the moment a cheaper signal has already answered.
            mangled = (
                False
                if producer is not None and "rustc" in producer
                else _rust_mangled(stream, sections)
            )
    except OSError:
        return None
    except struct.error:
        # A file that starts with the magic but is too short to hold what its
        # own header promises. Reported as no information rather than as a
        # crash in a diagnostic tool.
        return None
    return ElfInfo(
        machine_name(e_machine), names, producer=producer, rust_mangled=mangled
    )


def _text_at(stream: BinaryIO, extent: tuple[int, int] | None, cap: int) -> str | None:
    """A section's bytes as text, NULs included, or ``None`` if it is absent."""
    if extent is None:
        return None
    offset, size = extent
    if size <= 0 or size > cap:
        return None
    stream.seek(offset)
    return stream.read(size).decode("ascii", "replace")


def _rust_mangled(stream: BinaryIO, sections: dict[str, tuple[int, int]]) -> bool:
    """Whether a rustc-mangled symbol name is in this file's string table.

    Only the first :data:`_MAX_STRING_SCAN` bytes are searched. A truncated
    search can answer "not Rust" about a Rust binary, which costs the
    pretty-printers; an unbounded one can allocate the seat out of memory,
    which costs the session.
    """
    for name in _STRING_SECTIONS:
        extent = sections.get(name)
        if extent is None:
            continue
        offset, size = extent
        if size <= 0:
            continue
        stream.seek(offset)
        if _RUST_MANGLED.search(stream.read(min(size, _MAX_STRING_SCAN))):
            return True
    return False


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


def _sections(
    stream: BinaryIO, table: _SectionTable, *, endian: str, is_64: bool
) -> dict[str, tuple[int, int]] | None:
    """Every section's name, file offset and size, via the string table.

    The extents are carried beside the names because two of the questions above
    are answered by a section's *contents* rather than by its existence, and
    re-walking the table to find them would be the same parse twice.
    """
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

    found: dict[str, tuple[int, int]] = {}
    for index in range(table.count):
        name_offset, offset, size = entry(index)
        end = strings.find(b"\0", name_offset)
        if name_offset >= len(strings) or end < 0:
            continue
        found[strings[name_offset:end].decode("ascii", "replace")] = (offset, size)
    return found
