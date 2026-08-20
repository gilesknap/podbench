"""Tests for language x mode x architecture, and for the sentences it produces.

Two things are pinned here, and the second matters as much as the first.

*The verdict*: which flavours apply to a Python service on amd64, to the same
service on arm64, to a distroless C binary, to a Go binary. Each of those is a
real target from the live cluster, reproduced as a synthetic ``/proc`` tree.

*The wording*: when a flavour cannot be emitted the message has to name the
**mechanism**. "debugpy unavailable" is the error #18 set out to remove; "no
attach_linux_arm64.so in debugpy" is a fact the reader can act on. So the
assertions look for the mechanism, not for a status.

Nothing here touches a cluster: ``/proc`` is synthetic, the ELF binaries are
built byte by byte, and ``shutil.which`` is injected.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from podbench.flavour import (
    SEAT_DEBUGPY_PATH,
    SEAT_PYTHON,
    Assessment,
    Debugger,
    Flavour,
    Language,
    Mode,
    PtraceEvidence,
    Seat,
    Target,
    Which,
    assess,
    detect_mode,
    injection_command,
    inspect_target,
    inventory,
    ptrace_evidence,
    survey_seat,
)
from test_elf import EM_AARCH64, EM_X86_64, build_elf

PID = 597
TARGET_CID = "cafe1234cafe1234cafe1234cafe1234"
"""The target container's id, as it appears in the target's own cgroup path."""
SITE_PACKAGES = "usr/local/lib/python3.12/site-packages"
FULL_SEAT = ("gdb", "gdb-podbench", "dlv")
AMD64_HELPER = ["attach_linux_amd64.so"]


def comm_of(cmdline: str) -> str:
    """``/proc/<pid>/comm`` for a command line: argv[0]'s basename, 15 chars."""
    return Path(cmdline.split()[0]).name[:15]


def proc_stat(pid: int, comm: str, *, state: str = "S", start_ticks: int = 4242) -> str:
    """A ``/proc/<pid>/stat`` line ``dev.read_process`` can read.

    Only two fields of the 52 are ever consulted — the state, which separates a
    live server from a zombie still holding a port, and field 22, which pins the
    pid to a start time so a recycled number cannot pass for it. The rest are
    padding, and are padding in the parser's eyes too: it splits on the *last*
    ``)`` precisely because ``comm`` may contain one.

    >>> proc_stat(7, "python").split()[:4]
    ['7', '(python)', 'S', '0']
    """
    padding = " ".join("0" for _ in range(18))
    return f"{pid} ({comm}) {state} {padding} {start_ticks}\n"


def make_proc(
    tmp_path: Path,
    *,
    exe: str | None = "/app/victim",
    cmdline: str = "/app/victim",
    sections: list[str] | None = None,
    machine: int = EM_X86_64,
    cwd: str = "/",
    site_packages: str | None = None,
    target_helpers: list[str] | None = None,
    cap_sys_ptrace: bool = False,
    ptrace_readable: bool = True,
    mapped: dict[str, list[str]] | None = None,
) -> Path:
    """A ``/proc`` with one target process and a rootfs behind it.

    ``mapped`` is the target's address space: each key is a file as the target
    spells it and each value its section names, so a fixture can put a
    ``libjvm.so`` full of symbols beside a main binary that has none. It writes
    both halves - the ``maps`` file and the ELF behind every line of it -
    because the whole point of the inventory is that the two agree.

    ``ptrace_readable=False`` models a seat the kernel refuses
    ``PTRACE_MODE_READ`` on, as a ``root`` link that resolves to nothing. It
    takes the whole rootfs with it, which is the point: exe, maps and every
    path under ``/proc/<pid>/root`` are gated on that one check, so a target
    this seat may not read has no site-packages it can find either. Modelled by
    the link rather than by a mode bit, because the suite runs as uid 0 and a
    mode bit stops nothing there.
    """
    status = tmp_path / "self"
    status.mkdir()
    # Bit 19 is CAP_SYS_PTRACE; the mask is read exactly as the kernel prints it,
    # so the seat's rung is measured here rather than passed in as a boolean.
    (status / "status").write_text(
        f"CapEff:\t{(1 << 19) if cap_sys_ptrace else 0:016x}\n"
    )
    # The seat's own root, which is the *left* half of every `same_root`
    # comparison. A tree without it makes every socket unattributable, which is
    # the safe answer and the wrong fixture: attribution is what decides whether
    # a listening port belongs to this pod at all (issue #87).
    seat_root = tmp_path / "seat-root"
    seat_root.mkdir()
    (status / "root").symlink_to(seat_root)
    entry = tmp_path / str(PID)
    entry.mkdir()
    # `stat` and `cgroup` are read by nothing that inspects the *target*, and by
    # everything that attributes a *socket* to it: whether the pid is alive
    # rather than a zombie holding a dead port (report 3.19), and which
    # container it belongs to (report 3.15).
    (entry / "stat").write_text(proc_stat(PID, comm_of(cmdline)))
    (entry / "comm").write_text(f"{comm_of(cmdline)}\n")
    (entry / "cgroup").write_text(
        f"0::/kubepods/besteffort/podfeed-face/{TARGET_CID}\n"
    )
    if not ptrace_readable:
        (entry / "root").symlink_to(tmp_path / "unreadable")
    if exe is not None:
        (entry / "exe").symlink_to(exe)
        if ptrace_readable:
            binary = entry / "root" / exe.lstrip("/")
            binary.parent.mkdir(parents=True)
            binary.write_bytes(build_elf(sections or [".text"], machine=machine))
    if mapped is not None and ptrace_readable:
        # `maps` is one of PTRACE_READ_PATHS, so it is written only for a
        # target this seat may read - a fixture with an unreadable rootfs and a
        # readable maps is a tree the kernel never produces.
        lines: list[str] = []
        for index, (path, sections) in enumerate(mapped.items()):
            base = 0x7F0000000000 + index * 0x100000
            lines.append(
                f"{base:012x}-{base + 0x1000:012x} r-xp 00000000 08:01 {index}"
                f"                  {path}\n"
            )
            binary = entry / "root" / path.lstrip("/")
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(build_elf(sections, machine=machine))
        # An anonymous mapping and the stack, so the parser is asked to skip
        # the lines a real /proc/<pid>/maps is mostly made of.
        lines.append(
            f"{0x7FFF00000000:012x}-{0x7FFF00001000:012x} rw-p "
            "00000000 00:00 0                          [stack]\n"
        )
        (entry / "maps").write_text("".join(lines))
    (entry / "cmdline").write_text(cmdline.replace(" ", "\x00"))
    (entry / "cwd").symlink_to(cwd)
    if site_packages is not None and ptrace_readable:
        # With the helpers by default, because that is what a wheel unpacks to.
        # The tree the injection loads is the target's, so a target debugpy with
        # no helper in it models a *broken* install rather than an ordinary one.
        write_debugpy(
            entry / "root" / site_packages,
            helpers=AMD64_HELPER if target_helpers is None else target_helpers,
        )
    return tmp_path


def python_proc(
    tmp_path: Path,
    *,
    exe: str | None = "/usr/local/bin/python3.12",
    machine: int = EM_X86_64,
    site_packages: str | None = None,
    target_helpers: list[str] | None = None,
    cap_sys_ptrace: bool = True,
    ptrace_readable: bool = True,
) -> Path:
    """``podbench-demo/demo-service``, as a synthetic tree."""
    return make_proc(
        tmp_path,
        exe=exe,
        cmdline="python /src/demo_service.py",
        machine=machine,
        site_packages=site_packages,
        target_helpers=target_helpers,
        cap_sys_ptrace=cap_sys_ptrace,
        ptrace_readable=ptrace_readable,
    )


def write_debugpy(root: Path, *, helpers: list[str]) -> Path:
    """A directory shaped like an installed debugpy, with chosen helpers.

    Idempotent, because ``--provision`` installs over its own destination and a
    real ``uv pip install --target`` is quite happy to be pointed at a directory
    that already has a tree in it.
    """
    package = root / "debugpy"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("")
    helper_dir = package / "_vendored" / "pydevd" / "pydevd_attach_to_process"
    helper_dir.mkdir(parents=True, exist_ok=True)
    for name in helpers:
        (helper_dir / name).write_bytes(b"")
    return root


def seat_debugpy(tmp_path: Path, *, helpers: list[str]) -> str:
    """The image's own copy, at the path ``--debugpy-root`` would name."""
    return str(write_debugpy(tmp_path / "seat-debugpy", helpers=helpers))


def which_of(*present: str, shimmed: bool = True) -> Which:
    """A ``shutil.which`` that knows about exactly these names.

    With both ``gdb`` and ``gdb-podbench`` present, ``gdb`` resolves to the
    wrapper — which is what the image's symlink does, and the only arrangement
    that helps a tool shelling out to ``gdb --pid``. ``shimmed=False`` models a
    seat where the wrapper merely sits beside an unwrapped gdb.
    """

    def which(name: str) -> str | None:
        if name not in present:
            return None
        if name == "gdb" and shimmed and "gdb-podbench" in present:
            return "/usr/local/bin/gdb-podbench"
        return f"/usr/local/bin/{name}"

    return which


def verdict(assessments: list[Assessment], flavour: Flavour) -> Assessment:
    return next(item for item in assessments if item.flavour is flavour)


def full_seat(**overrides: object) -> Seat:
    """A seat with gdb, the shim and dlv, on amd64, holding CAP_SYS_PTRACE."""
    defaults: dict[str, object] = {
        "machine": "x86_64",
        "cap_sys_ptrace": True,
        "debuggers": inventory(which=which_of(*FULL_SEAT)),
        "sysroot_gdb": True,
    }
    defaults.update(overrides)
    return Seat(**defaults)  # type: ignore[arg-type]


# -- detection --------------------------------------------------------------


def test_a_distroless_c_binary_is_native(tmp_path: Path) -> None:
    target = inspect_target(PID, proc=make_proc(tmp_path))
    assert target.language is Language.NATIVE
    assert target.program == "/app/victim"


def test_a_python_service_is_python_from_its_interpreter(tmp_path: Path) -> None:
    """The exe is CPython; the *script* is what the developer calls the app."""
    target = inspect_target(PID, proc=python_proc(tmp_path))
    assert target.language is Language.PYTHON
    assert target.script == "/src/demo_service.py"
    assert target.name == "demo_service.py"


def test_a_go_binary_is_go(tmp_path: Path) -> None:
    proc = make_proc(tmp_path, sections=[".gopclntab", ".text"])
    assert inspect_target(PID, proc=proc).language is Language.GO


JVM_CMDLINE = "/opt/java/bin/java -Xmx1g -jar /opt/nexus/nexus.jar"
BEAM_CMDLINE = (
    "/opt/erlang/lib/erlang/erts-15.2.7.12/bin/beam.smp -W w -- -root "
    "/opt/erlang/lib/erlang -progname erl -- -home /var/lib/rabbitmq -- "
    "-s rabbit boot"
)
"""p47-rabbitmq-0's own command line, kept verbatim. 208 threads behind it, and
gdb attaches to all of them and shows the emulator."""


def test_a_jvm_is_detected_from_its_command_line(tmp_path: Path) -> None:
    """``cmdline`` is world-readable, so this survives the degraded rung."""
    proc = make_proc(tmp_path, exe="/opt/java/bin/java", cmdline=JVM_CMDLINE)
    assert inspect_target(PID, proc=proc).language is Language.JVM


def test_a_jvm_behind_a_wrapper_is_found_in_its_address_space(
    tmp_path: Path,
) -> None:
    """``argv[0]`` names the wrapper, and ``libjvm.so`` names the runtime.

    The case that matters at DLS, where PID 1 is routinely an entrypoint script
    or an Argo shim rather than the runtime itself.
    """
    proc = make_proc(
        tmp_path,
        exe="/entrypoint.sh",
        cmdline="/entrypoint.sh --serve",
        mapped={"/opt/java/lib/server/libjvm.so": [".dynsym", ".text"]},
    )
    assert inspect_target(PID, proc=proc).language is Language.JVM


def test_the_beam_is_erlang(tmp_path: Path) -> None:
    proc = make_proc(
        tmp_path,
        exe="/opt/erlang/lib/erlang/erts-15.2.7.12/bin/beam.smp",
        cmdline=BEAM_CMDLINE,
    )
    assert inspect_target(PID, proc=proc).language is Language.ERLANG


def test_a_beamline_binary_is_not_a_beam(tmp_path: Path) -> None:
    """The runtime names are matched whole, and this is why.

    Every workload podbench was written for lives on a beamline, so a ``beam``
    *prefix* would refuse a debugger to most of the estate and name Erlang
    while doing it.
    """
    proc = make_proc(tmp_path, exe="/app/beamline-ioc", cmdline="/app/beamline-ioc")
    assert inspect_target(PID, proc=proc).language is Language.NATIVE


def test_a_rust_binary_is_rust_and_not_native(tmp_path: Path) -> None:
    """Not a refusal - it changes what gdb is told, not which gdb is chosen."""
    proc = make_proc(tmp_path, sections=[".rustc", ".text"])
    assert inspect_target(PID, proc=proc).language is Language.RUST


def test_a_known_runtime_never_falls_through_to_native(tmp_path: Path) -> None:
    """The root cause of finding 13, pinned at the one line that caused it.

    ``_language`` used to end "Go if the sections say so, native otherwise", so
    a JVM with a perfectly readable ELF became ``NATIVE`` and was handed a
    confident cppdbg entry pointing into HotSpot's interpreter loop. The ELF is
    given every reason to answer here: it is readable, it is not Go, and it is
    not Rust.
    """
    proc = make_proc(
        tmp_path,
        exe="/opt/java/bin/java",
        cmdline=JVM_CMDLINE,
        sections=[".text", ".debug_info", ".symtab"],
    )
    target = inspect_target(PID, proc=proc)
    assert target.language is Language.JVM
    assert target.elf is not None
    results = assess(target, Mode.OBSERVE, full_seat())
    assert not any(result.available for result in results)


def test_the_architecture_comes_from_the_target_not_the_seat(tmp_path: Path) -> None:
    """A pod can run an amd64 image on an arm64 node, and vice versa."""
    proc = make_proc(tmp_path, machine=EM_AARCH64)
    assert inspect_target(PID, proc=proc).machine == "aarch64"


def test_an_unreadable_exe_still_produces_a_target(tmp_path: Path) -> None:
    """PTRACE_MODE_READ is lost on the degraded rung, and that is not fatal."""
    proc = make_proc(tmp_path, exe=None, cmdline="python /src/app.py")
    target = inspect_target(PID, proc=proc)
    assert target.program is None
    assert target.language is Language.PYTHON
    assert any("PTRACE_MODE_READ" in note for note in target.notes)


@pytest.mark.parametrize("shared", [True, False])
def test_mode_follows_the_mount_namespace(tmp_path: Path, shared: bool) -> None:
    """A ``podbench dev`` pod relaunches the app *from* the seat.

    Its process is therefore on this side of the mount-namespace line, and that
    single fact flips every path mapping in the emitted configuration. Two
    processes share a root inode exactly when they share a namespace, so the
    tree is built with the roots pointing at the same directory or at two.
    """
    ours = tmp_path / "rootfs-seat"
    theirs = ours if shared else tmp_path / "rootfs-app"
    ours.mkdir()
    if not shared:
        theirs.mkdir()
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "root").symlink_to(ours)
    (tmp_path / str(PID)).mkdir()
    (tmp_path / str(PID) / "root").symlink_to(theirs)
    expected = Mode.DEV if shared else Mode.OBSERVE
    assert detect_mode(PID, proc=tmp_path) is expected


# -- the flavour matrix -----------------------------------------------------


def test_a_c_target_gets_gdb_and_lldb_but_not_delve(tmp_path: Path) -> None:
    target = inspect_target(PID, proc=make_proc(tmp_path))
    results = assess(target, Mode.OBSERVE, full_seat())
    assert verdict(results, Flavour.GDB).available
    assert verdict(results, Flavour.LLDB).available
    assert not verdict(results, Flavour.DELVE).available


def test_delve_applies_to_a_go_target(tmp_path: Path) -> None:
    target = inspect_target(PID, proc=make_proc(tmp_path, sections=[".gopclntab"]))
    results = assess(target, Mode.OBSERVE, full_seat())
    assert verdict(results, Flavour.DELVE).available


def test_delve_without_dlv_names_the_missing_binary(tmp_path: Path) -> None:
    """The Go extension runs dlv on the remote, so the image decides this."""
    target = inspect_target(PID, proc=make_proc(tmp_path, sections=[".gopclntab"]))
    seat = full_seat(debuggers=inventory(which=which_of("gdb")))
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DELVE)
    assert not result.available
    assert "no dlv on PATH" in result.reason


def test_delve_without_dlv_names_the_image_and_the_issue(tmp_path: Path) -> None:
    """ "no dlv on PATH" reads as something a seat could fix, and it is not.

    The image ships no delve at all, so the sentence has to say so and name the
    issue that would change it - otherwise the remedy the reader reaches for is
    a PATH entry that does not exist anywhere in the pod (issue #115).
    """
    target = inspect_target(PID, proc=make_proc(tmp_path, sections=[".gopclntab"]))
    seat = full_seat(debuggers=inventory(which=which_of("gdb")))
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DELVE)
    assert not result.available
    assert "ships no" in result.reason
    assert "#115" in result.reason


def test_the_delve_refusal_only_points_at_a_gdb_entry_that_exists(
    tmp_path: Path,
) -> None:
    """`--flavour delve` emits nothing else, so there is no entry beside it.

    The sentence said "the gdb entry beside this one is the fallback"
    unconditionally, and the one run whose whole output is this sentence - a Go
    target, delve asked for by name, stdout empty - is the run where that entry
    was never emitted. A reader who goes looking for it is looking for a
    launch.json bug that is not there.
    """
    target = inspect_target(PID, proc=make_proc(tmp_path, sections=[".gopclntab"]))
    seat = full_seat(debuggers=inventory(which=which_of("gdb")))

    alone = verdict(
        assess(target, Mode.OBSERVE, seat, wanted={Flavour.DELVE}), Flavour.DELVE
    )
    assert "beside this one" not in alone.reason
    assert "--flavour gdb" in alone.reason

    beside = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DELVE)
    assert "the gdb entry beside this one is the fallback" in beside.reason


def test_the_delve_refusal_promises_no_gdb_entry_this_seat_refuses(
    tmp_path: Path,
) -> None:
    """And the same sentence where nothing at all can be emitted: a seat with
    neither dlv nor gdb has no fallback to name, and `--flavour gdb` would be
    the second wall rather than the way past the first."""
    target = inspect_target(PID, proc=make_proc(tmp_path, sections=[".gopclntab"]))
    seat = full_seat(debuggers=inventory(which=which_of()))

    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DELVE)
    assert "beside this one" not in result.reason
    assert "--flavour gdb" not in result.reason
    assert "this seat refuses that too" in result.reason


def test_a_jvm_is_refused_and_jdwp_is_named(tmp_path: Path) -> None:
    """A named frame inside HotSpot looks like progress and is not.

    The refusal is deliberately *not* a language mismatch: gdb is the default
    flavour and would attach happily, so this sentence is the only thing
    between the reader and a plausible backtrace through the interpreter.
    """
    proc = make_proc(tmp_path, exe="/opt/java/bin/java", cmdline=JVM_CMDLINE)
    target = inspect_target(PID, proc=proc)
    result = verdict(assess(target, Mode.OBSERVE, full_seat()), Flavour.GDB)
    assert not result.available
    assert not result.language_mismatch
    assert "HotSpot" in result.reason
    assert result.remedy is not None
    assert "jdwp" in result.remedy
    assert "#114" in result.remedy


def test_a_beam_is_refused_and_the_beam_tools_are_named(tmp_path: Path) -> None:
    """gdb sees the emulator; nothing it shows is an Erlang process."""
    proc = make_proc(tmp_path, exe="/erts-15.2/bin/beam.smp", cmdline=BEAM_CMDLINE)
    target = inspect_target(PID, proc=proc)
    result = verdict(assess(target, Mode.OBSERVE, full_seat()), Flavour.GDB)
    assert not result.available
    assert not result.language_mismatch
    assert "BEAM" in result.reason
    assert result.remedy is not None
    assert "remsh" in result.remedy
    assert "observer" in result.remedy


@pytest.mark.parametrize(
    ("exe", "cmdline"),
    [
        ("/opt/java/bin/java", JVM_CMDLINE),
        ("/erts-15.2/bin/beam.smp", BEAM_CMDLINE),
    ],
)
def test_a_refused_runtime_gets_no_configuration_at_all(
    tmp_path: Path, exe: str, cmdline: str
) -> None:
    """Refusing gdb and then emitting lldb would be the same wrong backtrace."""
    target = inspect_target(PID, proc=make_proc(tmp_path, exe=exe, cmdline=cmdline))
    assert not any(
        result.available for result in assess(target, Mode.OBSERVE, full_seat())
    )


def test_rust_is_served_by_the_native_path(tmp_path: Path) -> None:
    """Rust is supported, not refused - it is next in priority after C++."""
    target = inspect_target(PID, proc=make_proc(tmp_path, sections=[".rustc", ".text"]))
    results = assess(target, Mode.OBSERVE, full_seat())
    assert verdict(results, Flavour.GDB).available
    lldb = verdict(results, Flavour.LLDB)
    assert lldb.available
    assert "rust target" in lldb.reason


def test_gdb_on_a_python_target_is_refused_with_the_reason(tmp_path: Path) -> None:
    """cppdbg attaches happily to CPython and shows interpreter frames."""
    target = inspect_target(PID, proc=python_proc(tmp_path))
    result = verdict(assess(target, Mode.OBSERVE, full_seat()), Flavour.GDB)
    assert not result.available
    assert "interpreter frames" in result.reason


# -- what the gdb entry says about itself -----------------------------------
#
# The reason string had no test at all, which is how it came to announce a
# "native target" one line under `debug-config`'s own "go target" (#115), and
# "expect addresses rather than names" about a process whose libraries were
# full of them (finding 13). It is specified here.


def gdb_reason(tmp_path: Path, **kwargs: object) -> str:
    """The sentence the gdb entry is emitted with, for one synthetic target."""
    proc = make_proc(tmp_path, **kwargs)  # type: ignore[arg-type]
    target = inspect_target(PID, proc=proc)
    result = verdict(assess(target, Mode.OBSERVE, full_seat()), Flavour.GDB)
    assert result.available, result.reason
    return result.reason


def test_the_gdb_reason_names_the_language_rather_than_assuming_native(
    tmp_path: Path,
) -> None:
    """gdb is offered for Go, and calling it a native target contradicts the
    line above it in the same report."""
    reason = gdb_reason(tmp_path, sections=[".gopclntab", ".debug_info"], mapped={})
    assert reason.startswith("go target")
    assert "native" not in reason
    # The fallback has to say what it is a fallback for, and why the better
    # tool is not here.
    assert "delve" in reason
    assert "#115" in reason
    # SIGURG is named, and named as what it is. `nostop noprint pass` is the
    # default of the gdb it was measured on - the development container's 17.1,
    # which reports `SIGURG No No Yes` before anything runs - so a sentence
    # promising the user it stops a flood is promising work the line does not
    # do. What the image's Debian gdb 13 defaults to is unread either way.
    assert "SIGURG" in reason
    assert "default" in reason


def test_symbols_in_a_mapped_library_are_not_no_symbols(tmp_path: Path) -> None:
    """The whole of finding 13, at the size it was found.

    The JVM's own executable is a launcher stub with nothing in it, and gdb was
    told to expect addresses rather than names while ``libjvm.so`` sat in the
    same address space with 65,843 symbols. The question is about the address
    space, not about ``/proc/<pid>/exe``.
    """
    reason = gdb_reason(
        tmp_path,
        sections=[".text"],
        mapped={
            "/app/victim": [".text"],
            "/usr/lib/libbig.so": [".dynsym", ".text"],
            "/usr/lib/libc.so.6": [".text"],
        },
    )
    assert "no .debug_info in the binary" in reason
    assert "libbig.so" in reason
    assert "symbol table" in reason
    assert "expect addresses" not in reason


def test_debug_info_in_a_mapped_library_is_the_better_news(tmp_path: Path) -> None:
    reason = gdb_reason(
        tmp_path,
        sections=[".text"],
        mapped={"/app/victim": [".text"], "/usr/lib/libapp.so": [".debug_info"]},
    )
    assert "libapp.so" in reason
    assert "named and lined" in reason


def test_a_measured_and_bare_address_space_says_so(tmp_path: Path) -> None:
    """The one case that really does license "expect addresses"."""
    reason = gdb_reason(
        tmp_path,
        sections=[".text"],
        mapped={"/app/victim": [".text"], "/usr/lib/libc.so.6": [".text"]},
    )
    assert "no symbols anywhere in the 2 mapped objects" in reason
    assert "expect addresses rather than names" in reason


def test_an_unreadable_maps_is_unmeasured_and_never_bare(tmp_path: Path) -> None:
    """``maps`` is PTRACE-gated, so its absence is a refusal, not an emptiness.

    Reporting "no symbols anywhere" off a read that was denied is the overclaim
    the rest of this module exists to prevent, one file along.
    """
    reason = gdb_reason(tmp_path, sections=[".text", ".note.gnu.build-id"])
    assert "unmeasured" in reason
    assert "PTRACE_MODE_READ" in reason
    assert "no symbols anywhere" not in reason
    # The build-id survives the degraded rung and is still worth saying.
    assert "debuginfod" in reason


def shadow_the_exe(tmp_path: Path, exe: str = "/app/victim") -> None:
    """Give the *seat* a file of its own at the target's exe path.

    Issue #90's precondition, and one podbench's own image meets by
    construction for any Python target: the seat and any uv-managed workload
    both install an interpreter under ``/python/cpython-<version>-<triple>/``.
    """
    ours = tmp_path / "seat-root" / exe.lstrip("/")
    ours.parent.mkdir(parents=True, exist_ok=True)
    ours.write_bytes(build_elf([".text"]))


def test_a_shadowed_exe_withdraws_lldb_and_keeps_gdb(tmp_path: Path) -> None:
    """Issue #90 in lldb, where the fix that saves the gdb entry does not work.

    gdb is handed a staged copy at a path nothing shadows and stays. lldb
    discards the path it is given and re-resolves the executable from the
    process after attaching, in *this* seat's mount namespace, so no path it
    could be handed survives - measured with a standalone lldb in a seat, with
    CodeLLDB's own bundled lldb not observed. A configuration that debugs the
    seat's binary and says so in a debug console is worse than no
    configuration, so the flavour is withdrawn.
    """
    proc = make_proc(tmp_path)
    shadow_the_exe(tmp_path)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(target, proc=proc, which=which_of(*FULL_SEAT), debugpy_root=None)
    results = assess(target, Mode.OBSERVE, seat)

    assert seat.exec_file_shadow == "/app/victim"
    result = verdict(results, Flavour.LLDB)
    assert not result.available
    assert not result.language_mismatch
    assert "/app/victim" in result.reason
    assert "#90" in result.reason
    assert verdict(results, Flavour.GDB).available


def test_the_lldb_refusal_offers_gdb_and_denies_that_a_flag_exists(
    tmp_path: Path,
) -> None:
    """The remedy has to say "there is none", not invent one.

    ``target.exec-search-paths`` and a staged copy were both measured and
    neither helps, so naming either as a fix would send the reader after an
    hour of settings that cannot work.
    """
    proc = make_proc(tmp_path)
    shadow_the_exe(tmp_path)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(target, proc=proc, which=which_of(*FULL_SEAT), debugpy_root=None)
    remedy = verdict(assess(target, Mode.OBSERVE, seat), Flavour.LLDB).remedy or ""
    assert "no lldb setting fixes it" in remedy
    assert "gdb entry" in remedy
    assert "not observed" in remedy


def test_the_lldb_refusal_only_points_at_a_gdb_entry_that_exists(
    tmp_path: Path,
) -> None:
    """The delve defect, one flavour along: `--flavour lldb` emits nothing else.

    A shadowed exe with lldb asked for by name produces this refusal and an
    otherwise empty file, so "the gdb entry beside this one" named an entry
    that was never written - and the reader goes hunting for a launch.json bug
    that is not there.
    """
    proc = make_proc(tmp_path)
    shadow_the_exe(tmp_path)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(target, proc=proc, which=which_of(*FULL_SEAT), debugpy_root=None)

    alone = (
        verdict(
            assess(target, Mode.OBSERVE, seat, wanted={Flavour.LLDB}), Flavour.LLDB
        ).remedy
        or ""
    )
    assert "beside this one" not in alone
    assert "--flavour gdb" in alone
    assert "not observed" in alone

    beside = verdict(assess(target, Mode.OBSERVE, seat), Flavour.LLDB).remedy or ""
    assert "Use the gdb entry beside this one" in beside


def test_the_lldb_refusal_promises_no_gdb_entry_this_seat_refuses(
    tmp_path: Path,
) -> None:
    """Where gdb is refused too there is no route, and none may be named.

    Both halves of the offer - the entry and `podbench dbg` - rest on the same
    staged copy, so a seat with no gdb at all has neither. Naming `--flavour
    gdb` there would be the second wall rather than the way past the first.
    """
    proc = make_proc(tmp_path)
    shadow_the_exe(tmp_path)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(target, proc=proc, which=which_of(), debugpy_root=None)

    assert not verdict(assess(target, Mode.OBSERVE, seat), Flavour.GDB).available
    remedy = verdict(assess(target, Mode.OBSERVE, seat), Flavour.LLDB).remedy or ""
    assert "beside this one" not in remedy
    assert "--flavour gdb" not in remedy
    assert "podbench dbg" not in remedy
    assert "no fallback here" in remedy
    # The provenance outlives every arm; it is what the refusal is evidence of.
    assert "not observed" in remedy


def test_lldb_stays_where_this_seat_shadows_nothing(tmp_path: Path) -> None:
    """The refusal is the collision's, not lldb's: no shadow, no withdrawal."""
    proc = make_proc(tmp_path)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(target, proc=proc, which=which_of(*FULL_SEAT), debugpy_root=None)
    assert seat.exec_file_shadow is None
    assert verdict(assess(target, Mode.OBSERVE, seat), Flavour.LLDB).available


def test_a_shadowed_exe_stops_the_gdb_refusal_offering_lldb(tmp_path: Path) -> None:
    """Both refusals can fire at once, and then the first one lies.

    "use the lldb entry beside this one" is the remedy for a binary this seat's
    binutils cannot read - and where the seat also shadows the exe path there
    is no such entry in the file, which sends the reader looking for a bug in
    VS Code.
    """
    proc = make_proc(tmp_path)
    shadow_the_exe(tmp_path)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(
        target,
        proc=proc,
        which=which_of(*FULL_SEAT),
        debugpy_root=None,
        program_load_error=".gnu.version_r invalid entry",
    )
    remedy = verdict(assess(target, Mode.OBSERVE, seat), Flavour.GDB).remedy or ""
    assert "no lldb entry" in remedy
    assert "use the lldb entry" not in remedy


def test_lldb_survives_an_unreadable_binary(tmp_path: Path) -> None:
    """Language unknown must not withdraw a configuration that works.

    Reading the target's ELF needs PTRACE_MODE_READ; on the degraded rung an
    ordinary C binary is unreadable, and refusing lldb there would be a silent
    loss rather than a stated one.
    """
    proc = make_proc(tmp_path, exe=None, cmdline="/app/victim")
    target = inspect_target(PID, proc=proc, program="/app/victim")
    assert verdict(assess(target, Mode.OBSERVE, full_seat()), Flavour.LLDB).available


def test_no_program_refuses_rather_than_guessing(tmp_path: Path) -> None:
    """A wrong ``program`` reads this image's binary and looks believable."""
    proc = make_proc(tmp_path, exe=None, cmdline="/app/victim")
    target = inspect_target(PID, proc=proc)
    result = verdict(assess(target, Mode.OBSERVE, full_seat()), Flavour.GDB)
    assert not result.available
    assert "--program" in (result.remedy or "")


def test_dev_mode_is_a_launch_and_leaves_lldb_out(tmp_path: Path) -> None:
    """Only the attach shape has been measured for CodeLLDB in a seat."""
    target = inspect_target(PID, proc=make_proc(tmp_path))
    results = assess(target, Mode.DEV, full_seat())
    assert verdict(results, Flavour.GDB).available
    assert not verdict(results, Flavour.LLDB).available


# -- the debugpy prerequisites ----------------------------------------------


def test_arm64_names_the_helper_that_does_not_exist(tmp_path: Path) -> None:
    """The headline for an arm64 Python target, and why it is promoted there.

    Every other prerequisite can be met inside the pod. This one cannot be met
    anywhere: debugpy publishes no aarch64 Linux wheel, so there is nothing to
    install.
    """
    proc = python_proc(tmp_path, machine=EM_AARCH64, site_packages=SITE_PACKAGES)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(
        target,
        proc=proc,
        which=which_of(*FULL_SEAT),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
    )
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    assert not result.available
    assert "attach_linux_arm64.so" in result.reason


def test_amd64_still_fails_on_the_dlopen_path_first(tmp_path: Path) -> None:
    """Architecture is the *third* blocker, not the first (issue #20's comment).

    On amd64, with the helper present, injection still fails because debugpy
    injects a dlopen of the path the driver sees and the target's mount
    namespace does not have it.
    """
    proc = python_proc(tmp_path)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(
        target,
        proc=proc,
        which=which_of(*FULL_SEAT),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
    )
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    assert not result.available
    assert "not importable by the target" in result.reason
    assert "mount namespace" in result.reason


def test_the_remedy_is_a_runnable_install_not_an_image_rebuild(
    tmp_path: Path,
) -> None:
    """The remedy for "not importable by the target" has to run where it prints.

    Both old remedies were image changes, and neither is available to someone
    already attached to a running pod. The bench proved the seat can do it
    itself, so what is printed is the command, with the *target's* version and
    the destination already in it (issue #45).
    """
    proc = python_proc(tmp_path)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(
        target,
        proc=proc,
        which=which_of(*FULL_SEAT),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
    )
    remedy = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY).remedy or ""
    assert (
        "uv pip install --no-cache --python-version 3.12 --target "
        f"/proc/{PID}/root/opt/podbench-debugpy debugpy" in remedy
    )
    # The image change is still named, as the thing that survives a restart -
    # but second, and no longer as the only way out.
    assert remedy.startswith("install it into the target from this seat")


def test_the_targets_python_version_is_read_from_its_own_rootfs(
    tmp_path: Path,
) -> None:
    """``/usr/local/bin/python`` names no version, and the seat's is the wrong one.

    Seat 3.11 and target 3.12 is the bench's own arrangement: uv resolves for
    whichever version it is told, and told the seat's it lands a wheel whose
    accelerator the target skips without a word.
    """
    proc = python_proc(
        tmp_path, exe="/usr/local/bin/python", site_packages=SITE_PACKAGES
    )
    assert inspect_target(PID, proc=proc).python_version == "3.12"


def test_an_unversioned_python_is_admitted_rather_than_guessed(
    tmp_path: Path,
) -> None:
    """With nothing to read it from, the paste carries a gap and says so."""
    proc = make_proc(tmp_path, exe="/usr/local/bin/python", cmdline="python app.py")
    target = inspect_target(PID, proc=proc)
    assert target.python_version is None
    seat = survey_seat(
        target,
        proc=proc,
        which=which_of(*FULL_SEAT),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
    )
    remedy = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY).remedy or ""
    # And the gap is shell-safe: the whole line is printed as a paste, and a
    # bare <X.Y> is an input redirection that hangs an interactive shell on an
    # unterminated quote instead of showing uv rejecting the version.
    assert "--python-version '<X.Y>'" in remedy


def test_two_library_directories_are_an_ambiguity_not_a_measurement(
    tmp_path: Path,
) -> None:
    """A rootfs carrying 3.9 and 3.10 gets the gap, not the later of the two.

    Sorting picks ``python3.10`` over ``python3.9``, so the guess would not even
    have been the plausible one - and either way an installed library directory
    is not evidence about the *running* interpreter once there are two of them.
    """
    proc = make_proc(tmp_path, exe="/usr/local/bin/python", cmdline="python app.py")
    for version in ("3.9", "3.10"):
        (proc / str(PID) / "root" / "usr" / "lib" / f"python{version}").mkdir(
            parents=True
        )
    assert inspect_target(PID, proc=proc).python_version is None


def test_the_provisioned_copy_is_found_by_one_more_fixed_path(
    tmp_path: Path,
) -> None:
    """A copy nobody can find is a copy that was never installed.

    ``_SEARCH_ROOTS`` covers ``usr/local/lib`` and ``usr/lib``, and a
    ``--target`` install is under neither. One more fixed path answers it; a
    glob wide enough to have found it anyway would be a walk of another
    container's rootfs, which is the unrecoverable OOM.
    """
    proc = python_proc(tmp_path)
    write_debugpy(
        proc / str(PID) / "root" / "opt" / "podbench-debugpy", helpers=AMD64_HELPER
    )
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(
        target,
        proc=proc,
        which=which_of(*FULL_SEAT),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
    )
    assert seat.debugpy_there == f"/proc/{PID}/root/opt/podbench-debugpy"
    assert verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY).available


VENV_CMDLINE = (
    "/app/.venv/bin/python3 /app/.venv/bin/fastcs-example run "
    "/epics/ioc/config/controller.yaml"
)
"""p47's ``bl47p-ea-fastcs-01``, verbatim: a uv venv at ``/app/.venv`` whose
interpreter is a symlink out to ``/python/cpython-<version>-<triple>``."""


def venv_proc(tmp_path: Path, *, pyvenv_cfg: bool = True) -> Path:
    """An epics-containers rootfs: the app in a venv, nothing under ``/usr``."""
    proc = make_proc(
        tmp_path,
        exe="/python/cpython-3.11.13-linux-x86_64-gnu/bin/python3.11",
        cmdline=VENV_CMDLINE,
        cap_sys_ptrace=True,
    )
    venv = proc / str(PID) / "root" / "app" / ".venv"
    venv.mkdir(parents=True)
    if pyvenv_cfg:
        (venv / "pyvenv.cfg").write_text("version_info = 3.11.13\n")
    write_debugpy(venv / "lib" / "python3.11" / "site-packages", helpers=AMD64_HELPER)
    return proc


def test_the_target_venv_is_searched_and_beats_a_system_copy(tmp_path: Path) -> None:
    """``_SEARCH_ROOTS`` is a system layout, and a DLS IOC is not one.

    Measured against p47's ``fastcs-example`` on 2026-08-18: the image ships
    debugpy 1.8.17 in ``/app/.venv/lib/python3.11/site-packages``, both fixed
    prefixes missed it, and ``debug-config`` refused the flavour with "debugpy
    is not importable by the target" — of the app that had it installed. Every
    epics-containers image is this shape, so the refusal was not a corner case.

    The venv wins over ``/usr/lib`` rather than merely being tried after it: a
    venv built ``include-system-site-packages = false`` cannot import from
    there at all, so a system answer would name a tree the bootstrap could not
    load.
    """
    proc = venv_proc(tmp_path)
    write_debugpy(
        proc / str(PID) / "root" / "usr" / "lib" / "python3.11" / "site-packages",
        helpers=AMD64_HELPER,
    )
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(
        target,
        proc=proc,
        which=which_of(*FULL_SEAT),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
    )
    assert seat.debugpy_there == (
        f"/proc/{PID}/root/app/.venv/lib/python3.11/site-packages"
    )
    assert verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY).available


def test_a_bin_directory_without_pyvenv_cfg_is_not_a_venv(tmp_path: Path) -> None:
    """The guard that keeps the lookup a measurement rather than a guess.

    ``argv[0]``'s grandparent is a venv root only when ``pyvenv.cfg`` says so;
    otherwise ``/usr/bin/python3`` would be read as a venv rooted at ``/usr``,
    and any ``lib/python3*/site-packages`` under it would be answered as the
    target's own tree.
    """
    proc = venv_proc(tmp_path, pyvenv_cfg=False)
    write_debugpy(
        proc / str(PID) / "root" / "usr" / "lib" / "python3.11" / "site-packages",
        helpers=AMD64_HELPER,
    )
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(
        target,
        proc=proc,
        which=which_of(*FULL_SEAT),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
    )
    assert seat.debugpy_there == (f"/proc/{PID}/root/usr/lib/python3.11/site-packages")


def test_a_parent_directory_component_disqualifies_a_candidate(
    tmp_path: Path,
) -> None:
    """The answer leaves this mount namespace, so it may not carry a ``..``.

    ``debugpy_there`` becomes the injection's ``PYTHONPATH`` and the prefix of
    the ``dlopen`` path written into the *target*, and the only reason
    ``/proc/<pid>/root/...`` is the one spelling valid on both sides is that it
    names the same file in either namespace. A ``..`` is resolved against
    whatever ``/proc/<pid>/root`` means on each side, so it also walks out of
    the rootfs the lookup is supposed to stay inside — here, into a venv the
    target does not have.
    """
    proc = make_proc(
        tmp_path,
        exe="/python/cpython-3.11.13-linux-x86_64-gnu/bin/python3.11",
        cmdline="/../../../opt/venv/bin/python3 /app/run.py",
        cap_sys_ptrace=True,
    )
    outside = tmp_path.parent / "opt" / "venv"
    (outside / "bin").mkdir(parents=True, exist_ok=True)
    (outside / "pyvenv.cfg").write_text("version_info = 3.11.13\n")
    write_debugpy(
        outside / "lib" / "python3.11" / "site-packages", helpers=AMD64_HELPER
    )
    seat = survey_seat(
        inspect_target(PID, proc=proc),
        proc=proc,
        which=which_of(*FULL_SEAT),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
    )
    assert seat.debugpy_there is None


def test_a_chosen_destination_is_the_one_searched_and_the_one_printed(
    tmp_path: Path,
) -> None:
    """Read-only rootfs is common, and then the copy goes on a writable mount.

    So the destination is a parameter everywhere it appears: the path searched,
    the path in the remedy and the path a later run has to find again are one
    value, not three.
    """
    proc = python_proc(tmp_path)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(
        target,
        proc=proc,
        which=which_of(*FULL_SEAT),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
        provision_dest="/scratch/debugpy",
    )
    remedy = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY).remedy or ""
    assert f"--target /proc/{PID}/root/scratch/debugpy debugpy" in remedy


def test_the_helper_is_looked_for_in_the_tree_the_injection_loads(
    tmp_path: Path,
) -> None:
    """The seat's tree is the wrong one to ask.

    The injection runs with ``PYTHONPATH`` pointed at the *target's* copy, so
    the helper the seat happens to have says nothing about the dlopen that copy
    will ask for. Harmless while both sides come from the same amd64 wheel, and
    a misreport the moment they differ — which provisioning makes possible.
    """
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES, target_helpers=[])
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(
        target,
        proc=proc,
        which=which_of(*FULL_SEAT),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
    )
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    assert not result.available
    assert f"no attach_linux_amd64.so in /proc/{PID}/root/{SITE_PACKAGES}" in (
        result.reason
    )
    # And not blamed on the architecture: amd64 is in every wheel, so this is an
    # incomplete install with a remedy, not the arm64 wall with none.
    assert "nothing to install" not in result.reason
    assert "uv pip install" in (result.remedy or "")


def test_a_missing_sysroot_gdb_is_named_too(tmp_path: Path) -> None:
    """debugpy shells out to a bare ``gdb --nx --pid``, which needs the shim."""
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(
        target,
        proc=proc,
        which=which_of("gdb"),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
    )
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    assert not result.available
    assert "sysroot" in result.reason
    assert "-iex" in (result.remedy or "")


def test_every_unmet_prerequisite_is_listed_not_just_the_first(
    tmp_path: Path,
) -> None:
    """Fixing the headline only to hit the next wall is what this replaces."""
    proc = python_proc(tmp_path, machine=EM_AARCH64)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(
        target,
        proc=proc,
        which=which_of("gdb"),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
    )
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    message = result.message()
    for mechanism in ("attach_linux_arm64.so", "importable by the target", "sysroot"):
        assert mechanism in message


def test_the_architecture_is_not_blamed_when_there_is_no_debugpy(
    tmp_path: Path,
) -> None:
    """With nothing installed, a missing helper says nothing about the arch.

    Reported live: an amd64 seat with no debugpy was told "no
    attach_linux_amd64.so ... not published for x86_64", which is simply false.
    """
    proc = python_proc(tmp_path)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(target, proc=proc, which=which_of("gdb"), debugpy_root=None)
    message = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY).message()
    assert "attach_linux_amd64.so" not in message
    assert "no debugpy in this seat" in message


def test_a_wrapper_beside_an_unwrapped_gdb_is_not_enough(tmp_path: Path) -> None:
    """debugpy runs whatever ``gdb`` resolves to, not what is installed."""
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(
        target,
        proc=proc,
        which=which_of("gdb", "gdb-podbench", shimmed=False),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
    )
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    assert not result.available
    assert "no sysroot-aware gdb on PATH" in result.reason


def test_every_prerequisite_met_emits_debugpy(tmp_path: Path) -> None:
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(
        target,
        proc=proc,
        which=which_of(*FULL_SEAT),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
    )
    assert seat.debugpy_there == f"/proc/{PID}/root/{SITE_PACKAGES}"
    assert verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY).available


# -- the injection needs ptrace, and the capability is one way to have it ----
#
# Issue #89. A Diamond seat under a policy engine that strips SYS_PTRACE landed
# capless at uid 0 beside a uid 0 target on a node at ptrace_scope=0 — classic
# ptrace, no capability involved — and `capreport` in that seat printed
# `live attach (pid 12) OK` while `debug-config --provision` refused the
# injection and emitted nothing. Both directions are pinned below, because the
# fix must not turn "no capability and Yama says no" into a yes: that overclaim
# is what spike S5 and issue #51 exist to prevent.


BFD_REFUSAL = (
    "BFD: /python/cpython-3.11.15-linux-x86_64-gnu/bin/python3.11: "
    ".gnu.version_r invalid entry / Can't read symbols from "
    "/python/cpython-3.11.15-linux-x86_64-gnu/bin/python3.11: bad value"
)
"""What gdb said in the field (issue #90), as ``program_load_error`` joins it."""


def injectable(
    tmp_path: Path,
    proc: Path,
    target: Target,
    *,
    target_attach_ok: bool | None = None,
    program_load_error: str | None = None,
    listening_port: int | None = None,
) -> Seat:
    """A seat with every pid-injection prerequisite met, before the measured ones.

    gdb, the sysroot shim, a debugpy on each side and the amd64 helper: whatever
    these tests then withhold is the only thing left to refuse on.
    """
    return survey_seat(
        target,
        proc=proc,
        which=which_of(*FULL_SEAT),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
        target_attach_ok=target_attach_ok,
        program_load_error=program_load_error,
        listening_port=listening_port,
    )


def test_a_measured_attach_offers_the_injection_without_the_capability(
    tmp_path: Path,
) -> None:
    """The bit is clear, the attach was made, and the attach is what gdb needs."""
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES, cap_sys_ptrace=False)
    target = inspect_target(PID, proc=proc)
    seat = injectable(tmp_path, proc, target, target_attach_ok=True)
    assert not seat.cap_sys_ptrace
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    assert result.available, result.message()


def test_an_unmeasured_attach_leaves_the_capability_deciding(tmp_path: Path) -> None:
    """With nothing measured at all, the capability is what is left.

    A ``Seat`` whose readability was never asked — a synthetic tree, or one a
    caller assembled by hand — is the only place the bit still decides, and it
    decides towards a refusal because nothing here has any evidence either way.
    """
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES, cap_sys_ptrace=False)
    target = inspect_target(PID, proc=proc)
    seat = replace(injectable(tmp_path, proc, target), target_ptrace_readable=None)
    assert seat.target_attach_ok is None
    assert ptrace_evidence(seat) is PtraceEvidence.UNKNOWN
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    assert not result.available
    assert "CAP_SYS_PTRACE is not in this seat's effective set" in result.reason


def test_a_readable_target_is_not_refused_to_a_capless_unmeasured_seat(
    tmp_path: Path,
) -> None:
    """#89's shape, without a probe: not measured is not the same as refused.

    ``--print-config`` measures nothing by design, and a capless seat that can
    read ``/proc/<pid>/root`` has passed the same credential comparison the
    attach takes, one mode weaker. Refusing it there is the Diamond defect
    again, arrived at from the other side.
    """
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES, cap_sys_ptrace=False)
    target = inspect_target(PID, proc=proc)
    seat = injectable(tmp_path, proc, target)
    assert not seat.cap_sys_ptrace and seat.target_attach_ok is None
    assert ptrace_evidence(seat) is PtraceEvidence.CREDENTIALS
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    assert result.available, result.message()


def test_an_unmeasured_offer_says_it_measured_nothing(tmp_path: Path) -> None:
    """The overclaim half of the same rule, which is the one that does harm.

    The configuration may be emitted on the credential check; the sentence
    beside it may not say an attach was made, because nobody made one. An F5
    that fails four subsystems down is what #18 and S5 were about.
    """
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES, cap_sys_ptrace=False)
    target = inspect_target(PID, proc=proc)
    result = verdict(
        assess(target, Mode.OBSERVE, injectable(tmp_path, proc, target)),
        Flavour.DEBUGPY,
    )
    assert f"ptrace to pid {PID} was not measured" in result.reason
    assert f"/proc/{PID}/root" in result.reason
    # The word the measured path uses for itself, and the one an unmeasured
    # offer must never borrow.
    assert "succeeded" not in result.reason


def test_a_denied_credential_check_refuses_and_names_the_credentials(
    tmp_path: Path,
) -> None:
    """A denial is conclusive, and the capability is not what said no.

    ``PTRACE_MODE_ATTACH`` is strictly stronger than the read, so nothing that
    refuses ``/proc/<pid>/root`` permits ``gdb --pid``. Naming CAP_SYS_PTRACE
    there would send the reader after a bit that changes nothing.
    """
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES, cap_sys_ptrace=False)
    target = inspect_target(PID, proc=proc)
    seat = replace(injectable(tmp_path, proc, target), target_ptrace_readable=False)
    assert ptrace_evidence(seat) is PtraceEvidence.DENIED
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    assert not result.available
    assert f"may not read /proc/{PID}/root" in result.reason
    assert "CAP_SYS_PTRACE" not in result.message()
    assert "--max-rung full" in result.message()


def test_a_measurement_outranks_the_credential_check(tmp_path: Path) -> None:
    """Both directions: an attach that was made settles it either way.

    The read is a fallback for the unasked case and nothing more — a seat that
    attached is offered the injection whatever the read said, and one that was
    refused is not offered it however readable the target is.
    """
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES, cap_sys_ptrace=False)
    target = inspect_target(PID, proc=proc)
    attached = replace(
        injectable(tmp_path, proc, target, target_attach_ok=True),
        target_ptrace_readable=False,
    )
    assert verdict(assess(target, Mode.OBSERVE, attached), Flavour.DEBUGPY).available
    refused = injectable(tmp_path, proc, target, target_attach_ok=False)
    assert refused.target_ptrace_readable
    result = verdict(assess(target, Mode.OBSERVE, refused), Flavour.DEBUGPY)
    assert not result.available
    assert "refused when this seat measured it" in result.reason


def test_a_denied_rootfs_is_not_reported_as_a_missing_debugpy(
    tmp_path: Path,
) -> None:
    """One refusal, one sentence — and not the one that offers an install.

    ``_target_debugpy`` stats through ``/proc/<pid>/root``, so on a tree the
    kernel refuses there is nothing to conclude from an empty search. Saying
    "debugpy is not importable by the target" there would be the same denial
    told a second time, with a remedy that writes through the very path that
    was just refused: ``--provision`` would run ``uv pip install --target
    /proc/<pid>/root/...`` and fail at the same wall.
    """
    proc = python_proc(tmp_path, cap_sys_ptrace=False, ptrace_readable=False)
    target = inspect_target(PID, proc=proc)
    seat = injectable(tmp_path, proc, target)
    assert seat.target_rootfs_denied
    assert seat.debugpy_there is None
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    assert not result.available
    assert f"may not read /proc/{PID}/root" in result.reason
    assert "nothing in the target's filesystem could be searched" in result.reason
    message = result.message()
    assert "not importable by the target" not in message
    assert "--target" not in message
    # Nor the architecture: `tree` falls back to this seat's own copy only when
    # the target has none, which nobody found out — and this seat's copy has
    # the helper it is about to be told it lacks.
    assert "attach_linux" not in message


def test_a_denied_rootfs_leaves_the_seats_own_gaps_reported(tmp_path: Path) -> None:
    """Only the *target-side* answers are withdrawn, not every answer.

    What this seat has is knowable however unreadable the target is, and a
    refusal that swallowed it would send the reader to fix the credentials only
    to meet a second wall — which is the experience the prerequisite list
    exists to end.
    """
    proc = python_proc(tmp_path, cap_sys_ptrace=False, ptrace_readable=False)
    target = inspect_target(PID, proc=proc)
    seat = survey_seat(target, proc=proc, which=which_of("gdb"), debugpy_root=None)
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    assert f"may not read /proc/{PID}/root" in result.reason
    assert any("no debugpy in this seat" in item for item in result.detail)
    assert any("no sysroot-aware gdb on PATH" in item for item in result.detail)


def test_a_measured_refusal_names_the_measurement_and_not_the_bit(
    tmp_path: Path,
) -> None:
    """A seat holding the capability can still be refused — by Yama, by seccomp,
    by an existing tracer — and naming the capability there would send the
    reader after the one mechanism that is not at fault."""
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES, cap_sys_ptrace=True)
    target = inspect_target(PID, proc=proc)
    seat = injectable(tmp_path, proc, target, target_attach_ok=False)
    assert seat.cap_sys_ptrace
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    assert not result.available
    assert "refused when this seat measured it" in result.reason
    assert "CAP_SYS_PTRACE" not in result.message()


def test_a_program_gdb_cannot_read_withdraws_the_injection_too(
    tmp_path: Path,
) -> None:
    """The injection is not a symbol-free ptrace (issue #90).

    debugpy drives gdb to evaluate ``call (void*)dlopen(...)`` inside the
    target, and gdb cannot call a function whose symbols it has just refused to
    load — so the same measurement that withdraws cppdbg has to withdraw this
    flavour, quoting what gdb said rather than paraphrasing it.
    """
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES)
    target = inspect_target(PID, proc=proc)
    seat = injectable(tmp_path, proc, target, program_load_error=BFD_REFUSAL)
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    assert not result.available
    message = result.message()
    assert BFD_REFUSAL in message
    assert "dlopen" in message


def test_a_listening_server_survives_a_program_gdb_cannot_read(
    tmp_path: Path,
) -> None:
    """``debugpy.listen()`` in the app starts no gdb, so gdb's opinion of the
    interpreter cannot withdraw it: the symbol check gates the *injection*."""
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES)
    target = inspect_target(PID, proc=proc)
    seat = injectable(
        tmp_path, proc, target, program_load_error=BFD_REFUSAL, listening_port=5678
    )
    assert verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY).available


def test_a_listening_server_needs_no_prerequisites_at_all(tmp_path: Path) -> None:
    """``debugpy.listen()`` baked into the app is pure Python: any arch, no cap."""
    target = inspect_target(PID, proc=python_proc(tmp_path, machine=EM_AARCH64))
    seat = Seat(machine="aarch64", cap_sys_ptrace=False, listening_port=5678)
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    assert result.available
    assert "already listening" in result.reason


def test_the_injection_command_loads_the_targets_debugpy() -> None:
    """The PYTHONPATH is the whole trick: that path resolves on both sides."""
    target = Target(pid=PID, language=Language.PYTHON, program="/usr/bin/python3")
    seat = Seat(
        machine="x86_64",
        cap_sys_ptrace=True,
        debugpy_here="/opt/podbench/debugpy",
        debugpy_there=f"/proc/{PID}/root/usr/lib/python3/dist-packages",
    )
    command = injection_command(target, seat)
    assert f"PYTHONPATH=/proc/{PID}/root/" in command
    assert "/opt/podbench/debugpy" not in command


def test_the_injection_command_names_the_seats_interpreter_in_full() -> None:
    """A bare ``python`` names whichever of the seat's two interpreters a PATH
    reaches, and only one of them is the one the image resolved debugpy for.
    The recipe is also printed to be pasted - into a ``kubectl exec``, which has
    no sshd config carrying a PATH at all - so it may not depend on one."""
    target = Target(pid=PID, language=Language.PYTHON, program="/usr/bin/python3")
    seat = Seat(machine="x86_64", cap_sys_ptrace=True, debugpy_here=SEAT_DEBUGPY_PATH)

    command = injection_command(target, seat)

    assert f" {SEAT_PYTHON} -m debugpy" in command
    assert SEAT_PYTHON.startswith("/")


# -- the inventory ----------------------------------------------------------


def test_inventory_reports_the_helpers_by_name(tmp_path: Path) -> None:
    """ "debugpy: yes" would be a lie on arm64: package present, mechanism not."""
    entries = inventory(
        which=which_of("gdb"),
        debugpy_root=seat_debugpy(tmp_path, helpers=AMD64_HELPER),
    )
    debugpy = next(entry for entry in entries if entry.name == "debugpy")
    assert debugpy.present
    assert "attach_linux_amd64.so" in debugpy.detail


def test_inventory_says_whether_gdb_on_path_is_the_shim() -> None:
    """Whatever PATH resolves is what debugpy will run."""
    entries = inventory(which=which_of("gdb"))
    shim = next(entry for entry in entries if entry.name == "gdb-podbench")
    assert not shim.present
    assert "no sysroot" in shim.detail


@pytest.mark.parametrize("name", ["gdb", "lldb", "dlv", "gdb-podbench", "debugpy"])
def test_inventory_covers_every_flavour_and_the_shim(name: str) -> None:
    assert any(entry.name == name for entry in inventory(which=which_of()))


def test_a_debugger_line_is_yes_or_no_first() -> None:
    assert Debugger("gdb", True, "gdb: /usr/bin/gdb").line().startswith("yes")
    assert Debugger("dlv", False, "dlv: absent").line().startswith("no")
