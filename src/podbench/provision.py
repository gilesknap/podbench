"""Install debugpy *into the target* from the seat, rather than asking for a rebuild.

``debug-config``'s Python refusal used to end in "install debugpy into the app
image": true, durable, and unavailable to the person who is already attached to
a running pod at 3 a.m., which is the situation the seat exists for. The seat
can satisfy the prerequisite itself — it ships ``uv``, live attach already
requires ``runAsUser: 0`` (``spec.validate_security_context``, because no CRI
populates the ambient set), and ``/proc/<pid>/root`` is the target's own
filesystem, which uid 0 writes through ``CAP_DAC_OVERRIDE`` whatever the
target's uid and modes are.

Proved on the bench on 2026-08-16 (issue #45): k3s, seat Python 3.11, target
Python 3.12. The install resolved *for 3.12 while running on 3.11*, the
injection then took ~3 s, and the target's own ``/proc/1/maps`` showed
``pydevd_cython.cpython-312-...so`` loaded out of the provisioned directory,
with the workload still serving and zero restarts.

**``--python-version`` is why this is a uv install and not a copy.** The image
installs debugpy for the *seat's* interpreter, so its tree carries
``pydevd_cython.cpython-311-...so`` alone; copied into a 3.12 target that
accelerator is skipped and pydevd falls back to pure Python — right answers, far
slower, and nothing says so. uv resolves for an interpreter it is not running,
which is what gets the matching wheel. Copying the seat's tree stays the
fallback for a pod with no egress; it is not the path.

Three things this costs, named wherever it is offered (:data:`CAVEATS`):
egress, since uv resolves and downloads from an index; nothing that survives a
container restart, neither the install nor the injection; and ~15 MB of
ephemeral storage, on a budget the seat *shares with the workload and cannot
reserve*, because an ephemeral container may not carry ``resources`` (report
3.9).

That is why the caller has to ask. ``flavour.injection_command`` is printed
rather than run because ptracing the workload is not something authoring a
launch.json may do on its own, and writing 15 MB into the workload's writable
layer is the larger of the two mutations.

The one genuinely new precondition is a **writable rootfs**, so it is probed
rather than assumed (brief, "Diagnose, don't mystify"): ``readOnlyRootFilesystem:
true`` lives in the *target's* mount namespace, so it surfaces here only as
``EROFS`` on the write itself. Where it is set there is usually still a writable
``emptyDir`` or tmpfs in the pod, which is why the destination is a parameter
and not a constant.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass
from pathlib import Path

from .kubectl import Runner, run_subprocess
from .proc import DEFAULT_PROC, sysroot_path

__all__ = [
    "CAVEATS",
    "PROVISION_DEST",
    "Provisioned",
    "blocker_sentence",
    "provision_command",
    "provision_debugpy",
    "provision_paste",
    "target_destination",
    "writable_blocker",
]

PROVISION_DEST = "/opt/podbench-debugpy"
"""Where a provisioned debugpy goes, as the *target* spells it.

Outside every directory a package manager owns, so nothing in the app image can
be shadowed by it, and one fixed path rather than a guess at the target's
site-packages — which is also what lets ``flavour._target_debugpy`` find it
again with one extra ``stat`` instead of a walk.
"""

CAVEATS = (
    "network egress from the pod is required: uv resolves and downloads the "
    "wheel from an index, so a locked-down namespace refuses this",
    "neither the install nor the injection survives a container restart - a "
    "restart brings back the app image exactly as it was built",
    "~15 MB of ephemeral storage, on a budget this seat shares with the "
    "workload and cannot reserve (an ephemeral container may not carry "
    "`resources`)",
)
"""What provisioning costs, printed with it rather than left to be discovered.

All three were measured or are structural, and none of them announces itself:
no egress looks like a resolver error, a restart looks like the debugger simply
stopping, and ephemeral-storage eviction takes the *workload* with it.
"""

_UNKNOWN_VERSION = "'<X.Y>'"
"""Stands in when the target's Python version could not be read.

Left visible on purpose: guessing the seat's own version would produce a command
that runs, installs the wrong wheel, and degrades pydevd to pure Python without
a word. Quoted because the whole line is printed as a paste: bare ``<X.Y>`` is
an input redirection, and pasting it leaves an interactive shell sitting at a
continuation prompt instead of showing uv rejecting the version.
"""

_PROBE_NAME = ".podbench-provision-probe"


def target_destination(pid: int, dest: str = PROVISION_DEST) -> str:
    """``dest`` inside ``pid``'s rootfs, spelled so both namespaces agree.

    The same string is the driver's ``PYTHONPATH`` and the path debugpy injects
    into the target, which is the whole reason it is ``/proc/<pid>/root/...``
    (issue #20's live proof, step 3).

    >>> target_destination(1)
    '/proc/1/root/opt/podbench-debugpy'
    """
    return f"{sysroot_path(pid)}/{dest.lstrip('/')}"


def provision_command(destination: str, python_version: str) -> tuple[str, ...]:
    """The uv install, as argv.

    ``--python-version`` rather than an interpreter: uv resolves for a version it
    is not running, which is the only way a 3.11 seat lands the accelerators a
    3.12 target will actually load.

    >>> provision_command("/proc/1/root/opt/dbg", "3.12")[:5]
    ('uv', 'pip', 'install', '--python-version', '3.12')
    """
    return (
        "uv",
        "pip",
        "install",
        "--python-version",
        python_version,
        "--target",
        destination,
        "debugpy",
    )


def provision_paste(
    pid: int, *, dest: str = PROVISION_DEST, python_version: str | None = None
) -> str:
    """The same command as a human pastes it, every field already filled in.

    This is what replaced "install debugpy into the app image" in the refusal:
    the remedy for a prerequisite the seat can meet has to be runnable where it
    is printed, not a rebuild.

    >>> print(provision_paste(1, dest="/opt/dbg", python_version="3.12"))
    uv pip install --python-version 3.12 --target /proc/1/root/opt/dbg debugpy
    """
    destination = target_destination(pid, dest)
    return " ".join(provision_command(destination, python_version or _UNKNOWN_VERSION))


def blocker_sentence(error: OSError, destination: Path) -> str:
    """Name the mechanism behind a failed write, in ``capreport``'s house style.

    ``EROFS`` is the one worth spelling out: the seat is uid 0 and holds
    ``CAP_DAC_OVERRIDE``, so the target's own uid and file modes decide nothing,
    and the only thing that can refuse is a mount flag living in the *target's*
    mount namespace.

    >>> import errno
    >>> sentence = blocker_sentence(OSError(errno.EROFS, "ro"), Path("/opt/x"))
    >>> sentence.startswith("/opt/x is read-only: the target has")
    True
    """
    if error.errno == errno.EROFS:
        return (
            f"{destination} is read-only: the target has readOnlyRootFilesystem: "
            "true, and that mount flag lives in the target's own mount namespace, "
            "so a write through /proc/<pid>/root gets EROFS however privileged "
            "this seat is. Point --provision-dest at a writable mount - an "
            "emptyDir or a tmpfs the pod already declares is the usual one"
        )
    if error.errno == errno.ENOSPC:
        # Deliberately *not* the pod's ephemeral-storage limit: that one is
        # enforced by the kubelet's periodic disk poll and arrives as an
        # eviction with no errno at all, so an errno here is the filesystem
        # itself, and naming the wrong of the two sends the reader to the wrong
        # `describe`.
        return (
            f"no space left at {destination}: this is the filesystem backing it "
            "- the node's disk, or the `sizeLimit` on the emptyDir or tmpfs "
            "--provision-dest points at. The pod's own ephemeral-storage limit "
            "is a different mechanism, polled by the kubelet and reported as an "
            "eviction rather than as an errno"
        )
    if error.errno in (errno.EACCES, errno.EPERM):
        # Three distinct causes, and CAP_DAC_OVERRIDE covers only the first.
        # Report 3.11 measured the second: at uid 0 with an empty effective set
        # even `ls /proc/<pid>/root` is denied, because the traversal takes
        # PTRACE_MODE_READ. The third is R8's unvalidated case, and the one seen
        # on the Diamond cluster - and an LSM is not a capability check.
        return (
            f"permission denied at {destination}: uid 0 in this seat carries "
            "CAP_DAC_OVERRIDE, so the target's own file modes are not it. What "
            "is left is the /proc/<pid>/root traversal, which takes "
            "PTRACE_MODE_READ and so is refused to a root seat with no "
            "CAP_SYS_PTRACE (report 3.11), or an LSM denying the cross-container "
            "write - SELinux or a custom AppArmor profile, neither of which "
            "CAP_DAC_OVERRIDE touches. `capreport` names the rung and prints "
            "both profiles"
        )
    return f"cannot write {destination}: {error.strerror or error}"


def writable_blocker(destination: Path) -> str | None:
    """Why the seat cannot write ``destination``, or ``None`` when it can.

    Probed by writing, because nothing else answers the question: the flag that
    refuses is in the target's mount namespace and is not readable from here,
    and ``os.access`` reports on uid and modes, which ``CAP_DAC_OVERRIDE``
    already makes irrelevant.
    """
    try:
        destination.mkdir(parents=True, exist_ok=True)
        probe = destination / _PROBE_NAME
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as error:
        return blocker_sentence(error, destination)
    return None


@dataclass(frozen=True)
class Provisioned:
    """What the install did, and what the user has to be told either way."""

    path: str | None
    """Where the target can now import debugpy from, or ``None`` if it cannot."""

    messages: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.path is not None


def provision_debugpy(
    pid: int,
    *,
    python_version: str,
    dest: str = PROVISION_DEST,
    proc: Path = DEFAULT_PROC,
    runner: Runner | None = None,
) -> Provisioned:
    """Install debugpy into ``pid``'s rootfs, having first proved it can be.

    The probe comes first so that a read-only rootfs is named rather than
    surfacing as whatever uv says about a directory it could not create, and the
    caveats are printed on success rather than on the next restart.
    """
    run = runner if runner is not None else run_subprocess
    destination = Path(proc) / str(pid) / "root" / dest.lstrip("/")
    blocker = writable_blocker(destination)
    if blocker is not None:
        return Provisioned(None, (blocker,))
    argv = provision_command(str(destination), python_version)
    result = run(list(argv))
    if result.returncode != 0:
        return Provisioned(
            None,
            (
                f"`{' '.join(argv)}` exited {result.returncode}: "
                f"{result.stderr.strip() or result.stdout.strip()}",
                "a resolver or network failure here is the no-egress case; the "
                "fallback is to copy this seat's own debugpy tree (`capreport` "
                "prints where it is), which works but ships this seat's "
                "interpreter's accelerators alone, so pydevd degrades to pure "
                "Python in a target on another version",
            ),
        )
    return Provisioned(
        target_destination(pid, dest),
        (
            f"installed debugpy for Python {python_version} into "
            f"{target_destination(pid, dest)}",
            *CAVEATS,
        ),
    )
