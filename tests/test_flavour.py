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

from pathlib import Path

import pytest

from podbench.flavour import (
    Assessment,
    Debugger,
    Flavour,
    Language,
    Mode,
    Seat,
    Target,
    Which,
    assess,
    detect_mode,
    injection_command,
    inspect_target,
    inventory,
    survey_seat,
)
from test_elf import EM_AARCH64, EM_X86_64, build_elf

PID = 597
SITE_PACKAGES = "usr/local/lib/python3.12/site-packages"
FULL_SEAT = ("gdb", "gdb-podbench", "dlv")
AMD64_HELPER = ["attach_linux_amd64.so"]


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
) -> Path:
    """A ``/proc`` with one target process and a rootfs behind it."""
    status = tmp_path / "self"
    status.mkdir()
    # Bit 19 is CAP_SYS_PTRACE; the mask is read exactly as the kernel prints it,
    # so the seat's rung is measured here rather than passed in as a boolean.
    (status / "status").write_text(
        f"CapEff:\t{(1 << 19) if cap_sys_ptrace else 0:016x}\n"
    )
    entry = tmp_path / str(PID)
    entry.mkdir()
    if exe is not None:
        (entry / "exe").symlink_to(exe)
        binary = entry / "root" / exe.lstrip("/")
        binary.parent.mkdir(parents=True)
        binary.write_bytes(build_elf(sections or [".text"], machine=machine))
    (entry / "cmdline").write_text(cmdline.replace(" ", "\x00"))
    (entry / "cwd").symlink_to(cwd)
    if site_packages is not None:
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


def test_source_root_prefers_the_script_over_the_cwd(tmp_path: Path) -> None:
    """The debuggee reports the path it was *given*, whatever its cwd is."""
    assert inspect_target(PID, proc=python_proc(tmp_path)).source_root == "/src"


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


def test_gdb_on_a_python_target_is_refused_with_the_reason(tmp_path: Path) -> None:
    """cppdbg attaches happily to CPython and shows interpreter frames."""
    target = inspect_target(PID, proc=python_proc(tmp_path))
    result = verdict(assess(target, Mode.OBSERVE, full_seat()), Flavour.GDB)
    assert not result.available
    assert "interpreter frames" in result.reason


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
    """No probe result is not a probe result saying yes.

    The regression that would do real harm: an unmeasured attach reported as a
    working one sends someone to an F5 that fails four subsystems down, which is
    the whole failure mode #18 and S5 were about.
    """
    proc = python_proc(tmp_path, site_packages=SITE_PACKAGES, cap_sys_ptrace=False)
    target = inspect_target(PID, proc=proc)
    seat = injectable(tmp_path, proc, target)
    assert seat.target_attach_ok is None
    result = verdict(assess(target, Mode.OBSERVE, seat), Flavour.DEBUGPY)
    assert not result.available
    assert "CAP_SYS_PTRACE is not in this seat's effective set" in result.reason


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
