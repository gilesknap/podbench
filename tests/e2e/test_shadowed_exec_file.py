"""#90 as a regression: gdb must read the target's binary, not the seat's own.

``set sysroot /proc/<pid>/root`` does not survive gdb's canonicalisation of the
exec file's name. The kernel resolves ``/proc/<pid>/root`` to ``/``, so
``/proc/12/root/python/…/python3.11`` collapses to ``/python/…/python3.11`` and
BFD reopens *that* — in the seat's mount namespace, where podbench's own image
keeps a different build of the same CPython at the same absolute path. What the
field saw was::

    BFD: /python/…/python3.11: .gnu.version_r invalid entry
    warning: Can't read symbols from /python/…/python3.11: bad value
    Error reading attached process's symbol file.

and, in the runs where the two builds are close enough not to error, no message
at all and the wrong symbols.

The fixture is the Diamond IOC reconstruction (``apps/dls-ioc.yaml``) because
the *target* has to be real: a uv-managed interpreter under ``/python``, reached
through ``/proc/<pid>/root``, which is the shape #90 was reported on.

The **collision** is planted rather than inherited — see :func:`shadowed_exe`.
It used to come for free, because the seat image and that workload both set
``UV_PYTHON_INSTALL_DIR=/python`` and happened to resolve the same CPython. That
was a coincidence of two release cadences, one pinned and one floating, and it
ran out on 2026-08-21: the path carries the patch version, so the day they
disagreed the premise failed and #90 silently had no regression test.
:func:`test_this_seat_shadows_the_targets_exe` still asserts the premise before
anything else runs, because a planted collision that failed to plant is the same
vacuum as one that never happened.

Deliberately in its own module rather than beside ``test_dls_ioc.py``. That file
needs a cluster serving ``MutatingAdmissionPolicy`` and skips without one; #90
has nothing to do with admission, reproduces on any rung, and must keep its
coverage where the policy fixtures cannot run. The cost is a second copy of the
IOC pod on the node when both modules run, which the manifest documents as
silent but harmless.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from podbench.kubectl import Kubectl
from podbench.launcher import Session, attach
from podbench.model import Verdict

from .conftest import KubectlCli

TARGET_POD = "dls-ioc"
TARGET_CONTAINER = "ioc"

READY_TIMEOUT = 420.0
"""One image pull of a real EPICS IOC before the chain of four starts."""

APP_CMDLINE_MARKER = "/app/.venv/bin/fastcs-thorlabs-mff"
"""How the app is recognised. Never by pid: two identical pods on one node came
up 1/9/11/12 and 1/8/10/11 — the depth is stable, the numbers are not."""

SYMBOL = "PyGILState_Ensure"
"""The symbol asked for, and not an arbitrary one.

It is what debugpy's injector needs before it can ``call (void*)dlopen(…)``, so
"can gdb resolve this" is the same question as "can this seat attach a Python
debugger at all".
"""

BFD_COMPLAINTS = (
    "Can't read symbols",
    "invalid entry",
    "invalid string offset",
    "Error reading attached process's symbol file",
)
"""Every phrasing #90 was reported with. Matched as text because ``gdb -batch``
exits 0 whether the symbol file loaded or not."""

STAGING_FAILED = "copying the target's aside failed"
"""podbench's own note when :func:`podbench.execfile.stage_exec_file` raises.

Checked *before* :data:`BFD_COMPLAINTS`, and it is not decoration. ``image/bin/
gdb-podbench`` captures only the stdout of its ``podbench dbg
--print-startup-commands`` call, so that note lands on gdb's stderr, and
:func:`_in_seat` merges both streams. The note quotes ``.gnu.version_r invalid
entry`` verbatim — it is telling the user what to expect — so it matches the
``invalid entry`` phrase below and would report a failed *copy* as gdb reading
the seat's binary. Same red test, wrong cause, and the fix would be looked for
in the wrong module.
"""


@pytest.fixture(scope="module")
def target(kubectl: KubectlCli, apps_dir: Path) -> str:
    """The DLS-alike IOC, running: CPython 3.11 four processes deep."""
    kubectl.apply(apps_dir / "dls-ioc.yaml")
    kubectl.wait(f"pod/{TARGET_POD}", "condition=Ready", timeout=READY_TIMEOUT)
    return TARGET_POD


@pytest.fixture(scope="module")
def seat(kube: Kubectl, target: str, podbench_image: str) -> Session:
    """One attached podbench container, with its capability probe run."""
    return attach(kube, target, target=TARGET_CONTAINER, image=podbench_image)


@pytest.fixture(scope="module")
def dry_run(kubectl: KubectlCli, target: str, seat: Session) -> list[str]:
    """``podbench dbg --dry-run``, run in the seat, with no pid given.

    No pid on purpose: the discovery is part of what is under test here, and it
    is the discovery a user gets. It settles on the deepest non-shell process in
    the target container, which for this workload is the IOC behind
    ``stdio-expose`` and ``pptty``.
    """
    result = kubectl.exec(
        target,
        ["podbench", "dbg", "--dry-run", "--no-debuginfod"],
        container=seat.seat.container,
        timeout=300,
    )
    return result.stdout.splitlines()


@pytest.fixture(scope="module")
def app_pid(dry_run: list[str], kubectl: KubectlCli, target: str, seat: Session) -> int:
    """The pid ``dbg`` settled on, confirmed to be the IOC and not a wrapper."""
    attaches = [line for line in dry_run if line.startswith("attach ")]
    assert len(attaches) == 1, f"expected one `attach` command:\n{dry_run}"
    pid = int(attaches[0].split()[1])

    cmdline = kubectl.exec(
        target,
        ["sh", "-c", f"tr '\\0' ' ' < /proc/{pid}/cmdline"],
        container=seat.seat.container,
    ).stdout
    assert APP_CMDLINE_MARKER in cmdline, (
        f"`dbg` chose pid {pid}, which is not the IOC but {cmdline!r}. Every "
        "assertion below is about reading that process's interpreter"
    )
    assert pid != 1, "the point of this fixture is that the app is not PID 1"
    return pid


DECOY_MARKER = "podbench-e2e-shadow"
"""Appended to the planted binary so its size cannot match the target's.

An ELF reader stops at the section table, so trailing bytes leave it a valid
binary that gdb will open and BFD will try to read — which is the whole point.
What they buy is that ``sizes[0] != sizes[1]`` cannot fail by coincidence, even
where the seat's interpreter and the target's are the same upstream build.
"""


@pytest.fixture(scope="module")
def shadowed_exe(kubectl: KubectlCli, target: str, seat: Session, app_pid: int) -> str:
    """Guarantee two different files at the target's interpreter path.

    #90 needs one absolute path to name a different file in each mount
    namespace. The fixture used to get that for free: the seat and the pinned
    IOC image both set ``UV_PYTHON_INSTALL_DIR=/python``, so a uv-managed
    CPython landed at ``/python/cpython-<version>-<triple>/`` in both.

    That was a coincidence of release cadences, and it ran out on 2026-08-21.
    The path carries the **patch** version, the IOC image is pinned at
    ``fastcs-thorlabs-mff:0.2.0`` with 3.11.15 frozen into it, and the seat is
    built on every CI run with ``uv sync --managed-python``, which resolves
    whatever 3.11.x exists that day. The moment those differ there is no
    collision, the premise fails, and #90 has no regression test — which is
    what happened to the 0.4.0 tag build.

    So the collision is *made* rather than hoped for. Only when the seat has no
    file at that path: where it already has one the natural collision is the
    better fixture, and overwriting would mean writing to the seat's own
    running interpreter — ``cp`` onto a mapped executable is ``ETXTBSY``, and
    succeeding would be worse than failing.
    """
    exe = kubectl.exec(
        target, ["readlink", f"/proc/{app_pid}/exe"], container=seat.seat.container
    ).stdout.strip()
    assert exe.startswith("/python/cpython-"), (
        f"the IOC's interpreter is no longer a uv-managed one at /python: {exe}. "
        "That is a change in the *target* image, not the drift this fixture "
        "plants around - the path below is what #90 collides on"
    )
    planted = _in_seat(
        kubectl,
        target,
        seat,
        # `-e` so a failed mkdir or cp is this fixture's error rather than an
        # empty listing blamed on the assertion below.
        "set -e\n"
        f'if [ ! -e "{exe}" ]; then\n'
        f'  mkdir -p "$(dirname "{exe}")"\n'
        f'  cp "$(readlink -f /app/.venv/bin/python)" "{exe}"\n'
        f'  printf %s {DECOY_MARKER} >> "{exe}"\n'
        "fi\n"
        f'stat -c %s "{exe}"',
    )
    assert planted.split() and planted.split()[0].isdigit(), (
        f"could not put a second build at {exe} in the seat, so #90 cannot "
        f"happen here and nothing below tests it:\n{planted}"
    )
    return exe


def _in_seat(
    kubectl: KubectlCli,
    target: str,
    seat: Session,
    script: str,
    *,
    timeout: float = 420,
) -> str:
    """Run a shell line in the seat and return both streams.

    Both, because gdb splits #90 across them: BFD's complaint goes to stderr and
    the symbol listing to stdout, and asserting on either alone would pass while
    the other said the opposite.
    """
    result = kubectl.exec(
        target,
        ["sh", "-c", script],
        container=seat.seat.container,
        check=False,
        timeout=timeout,
    )
    return f"{result.stdout}\n{result.stderr}"


def _assert_read_the_targets_binary(output: str, detail: str) -> None:
    """No BFD complaint in ``output``, with the two causes kept apart.

    ``detail`` describes the #90 failure for the caller's argv; the staging
    failure gets its own sentence, because it is a different module's bug.
    """
    assert STAGING_FAILED not in output, (
        "gdb was never handed a staged binary — `stage_exec_file` could not "
        "copy the target's aside, so this run says nothing about #90 and the "
        f"BFD complaints below are that copy's fault, not gdb's:\n{output}"
    )
    for complaint in BFD_COMPLAINTS:
        assert complaint not in output, f"{detail}\n{output}"


def _require_live_attach(seat: Session) -> None:
    """Yama and seccomp differ per node, so a seat that cannot attach skips."""
    report = seat.report
    if report is None or report.verdict is not Verdict.LIVE_ATTACH:
        pytest.skip(
            "this node does not grant live attach, so gdb cannot get far "
            f"enough to misread anything: {report.blocker.value if report else '?'}"
        )


# -- the premise -------------------------------------------------------------


def test_this_seat_shadows_the_targets_exe(
    kubectl: KubectlCli, target: str, seat: Session, app_pid: int, shadowed_exe: str
) -> None:
    """Two *different* files at one absolute path, which is all #90 needs.

    The control for the whole file, and the reason it is checked rather than
    assumed: without something for gdb's canonicalisation to land on there is no
    #90 to regress, and every assertion below would pass vacuously.

    :func:`shadowed_exe` establishes it. What is left here is that it *held* —
    the two files exist and differ — which is not the same statement and is the
    one the tests below depend on.
    """
    listing = _in_seat(
        kubectl,
        target,
        seat,
        f"stat -c '%s' '{shadowed_exe}' 2>&1; "
        f"stat -c '%s' '/proc/{app_pid}/root{shadowed_exe}' 2>&1",
    )

    sizes = listing.split()
    assert len(sizes) == 2 and all(size.isdigit() for size in sizes), (
        f"this seat has no file of its own at {shadowed_exe}, so gdb's "
        f"canonicalisation has nothing to land on and #90 cannot happen "
        f"here:\n{listing}"
    )
    assert sizes[0] != sizes[1], (
        f"the two builds at {shadowed_exe} are the same size, so this run "
        f"cannot tell a correct read from the collapsed one:\n{listing}"
    )


# -- #90 itself --------------------------------------------------------------


def test_dbg_hands_gdb_the_targets_own_binary(
    kubectl: KubectlCli,
    target: str,
    seat: Session,
    app_pid: int,
    shadowed_exe: str,
) -> None:
    """The file ``dbg`` names must hold the *target's* bytes.

    Checked by content rather than by spelling: a ``file`` command pointing at a
    plausible path with this seat's binary behind it is precisely the defect,
    and it is invisible in the command sequence.
    """
    printed = _in_seat(
        kubectl,
        target,
        seat,
        f"exec=$(podbench dbg {app_pid} --print-exec-file) && "
        f'sha256sum "$exec" "/proc/{app_pid}/root$(readlink /proc/{app_pid}/exe)" '
        f'"$(readlink /proc/{app_pid}/exe)"',
    )
    digests = [
        line.split()[0] for line in printed.splitlines() if len(line.split()) == 2
    ]

    assert len(digests) == 3, f"expected three checksums:\n{printed}"
    given, theirs, ours = digests
    assert given == theirs, (
        f"gdb is being handed a file that is not the target's:\n{printed}"
    )
    assert given != ours, (
        "the file gdb is handed is this seat's own binary, which is #90 with an "
        f"extra step:\n{printed}"
    )


def test_dbgs_own_sequence_resolves_the_targets_symbols(
    kubectl: KubectlCli,
    target: str,
    seat: Session,
    dry_run: list[str],
    app_pid: int,
    shadowed_exe: str,
) -> None:
    """Report 4.3's sequence, run verbatim, must not produce #90's text.

    The commands are taken from ``--dry-run`` rather than rebuilt here, so this
    fails if the printed sequence and the executed one ever diverge — a dry run
    that documents a ``file`` command podbench does not issue would be its own
    defect.

    debuginfod is off (``--no-debuginfod``): the question is which local file
    gdb read, and a network round trip would make the answer depend on egress.
    """
    _require_live_attach(seat)
    commands = " ".join(f"-ex {command!r}" for command in dry_run)
    output = _in_seat(
        kubectl,
        target,
        seat,
        f"gdb -q -batch {commands} -ex 'info functions ^{SYMBOL}$' -ex detach",
    )

    _assert_read_the_targets_binary(
        output,
        f"gdb read the wrong file for pid {app_pid} — issue #90. It "
        f"canonicalises /proc/{app_pid}/root away and opens this seat's own "
        "binary of the same name:",
    )
    assert re.search(rf"^0x[0-9a-f]+\s+{SYMBOL}$", output, re.MULTILINE), (
        f"gdb resolved no {SYMBOL}, so it has no symbols for the main "
        f"executable and every user frame comes back as ?? ():\n{output}"
    )


def test_a_third_party_gdb_pid_attach_resolves_them_too(
    kubectl: KubectlCli,
    target: str,
    seat: Session,
    app_pid: int,
    shadowed_exe: str,
) -> None:
    """debugpy's own argv, which is how #90 was reported.

    ``gdb --nw --nh --nx --pid <n> --batch`` through ``sh -c``, exactly as
    ``pydevd_attach_to_process`` shells out. podbench never sees that command
    line, so everything it can contribute has to come from ``image/bin/gdb-
    podbench`` standing in as ``gdb`` on PATH — the sysroot, and now the exec
    file. Without the second one the injector reaches ``call (void*)dlopen(…)``
    with no symbols and the attach fails.
    """
    _require_live_attach(seat)
    output = _in_seat(
        kubectl,
        target,
        seat,
        "DEBUGINFOD_URLS= gdb --nw --nh --nx --pid "
        f"{app_pid} --batch --eval-command='info functions ^{SYMBOL}$'",
    )

    _assert_read_the_targets_binary(
        output,
        "a third-party `gdb --pid` still reads this seat's binary; the "
        "wrapper is not supplying an exec file:",
    )
    assert re.search(rf"^0x[0-9a-f]+\s+{SYMBOL}$", output, re.MULTILINE), (
        f"debugpy's injector would find no {SYMBOL} to call through:\n{output}"
    )
