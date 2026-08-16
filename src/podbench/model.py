"""Types shared across podbench's laptop-side and in-pod-side code.

podbench runs in two very different places — a launcher on a developer's
machine and a set of helpers inside a debug container — and the two halves have
to agree about what a capability verdict or a target reference means. Those
agreements live here so that neither half owns them.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from . import __version__

__all__ = [
    "DEFAULT_IMAGE",
    "FLOATING_TAG",
    "IMAGE_REPOSITORY",
    "PTRACE_READ_PATHS",
    "WORLD_READ_PATHS",
    "SEAT_GROUP_KEY",
    "SEAT_HOME_PATH",
    "SEAT_HOME_VOLUME",
    "SEAT_IDENTITY_VOLUME",
    "SEAT_PASSWD_KEY",
    "IMAGE_ENV",
    "TARGET_CID_ENV",
    "Blocker",
    "CapabilityReport",
    "ContainerRef",
    "PodRef",
    "ProcInfo",
    "Rung",
    "Verdict",
    "as_dict",
    "describe_gated_fallback",
    "describe_reads",
    "image_tag_for",
    "ptrace_reads_ok",
]

IMAGE_REPOSITORY = "ghcr.io/gilesknap/podbench"
"""Where the released debug images live, without a tag."""

FLOATING_TAG = "main"
"""The tag used when this launcher's version names no published image.

That case is exactly a launcher built off a checkout — a clone, or
``uvx --from git+…`` — so the honest counterpart is the image built from the
same branch tip, which CI pushes on every default-branch commit. ``latest`` is
deliberately *not* it: CI moves ``latest`` only on a final release (see the
``enable=`` guard in ``.github/workflows/_container.yml``, which keeps unpinned
users off prereleases), and this project has only ever tagged prereleases, so
``latest`` would pair a launcher from today with an image from months ago.
"""

# An OCI reference tag: leading alphanumeric or underscore, then at most 127
# more of a restricted set (distribution's `TagRegexp`). PEP 440 can spell
# versions this cannot hold — a local segment's `+`, an epoch's `!` — and asking
# for one of those is not a failed pull but a malformed reference.
_OCI_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")


def image_tag_for(version: str) -> str:
    """The image tag matching a launcher version, or the floating tag.

    Under ``uvx`` the launcher's version floats between two attaches with no
    visible event, so a launcher can author a container spec its image does not
    understand — and that mismatch fails *in the pod*, where an ephemeral
    container cannot be restarted. Asking for the image that ships with this
    exact launcher is what keeps the two halves in step.

    A release has two spellings — the git tag, chart and image use SemVer
    (``1.0.0-beta.1``) while the wheel, and so ``__version__``, uses PEP 440
    (``1.0.0b1``) — and CI publishes both on the one digest precisely so this
    function can pass its own version through verbatim. Translating between the
    spellings here would put a version-string bug on the launch path, whose
    symptom is an ``ImagePullBackOff`` that burns the seat name for the life of
    the pod.
    """
    # setuptools_scm marks anything that is not exactly a tagged commit with a
    # dev segment and a `+g<sha>` local version. No such image was ever built,
    # let alone pushed, so a working tree or a post-release commit has nothing
    # to pin to and falls back to the branch tip.
    if ".dev" in version or "+" in version:
        return FLOATING_TAG
    # Everything else is a version CI tagged, and so published: a final release
    # or a prerelease. Refuse only spellings that cannot be a tag at all, rather
    # than guess at a repair.
    if not _OCI_TAG.fullmatch(version):
        return FLOATING_TAG
    return version


DEFAULT_IMAGE = f"{IMAGE_REPOSITORY}:{image_tag_for(__version__)}"
"""The debug image the launcher attaches when nothing else is specified.

Both halves of the launcher — `attach` on a live pod and `dev` on an authored
one — put this into a container spec, and a release that bumped one and not the
other would leave the two modes silently running different builds. It is
derived from :data:`podbench.__version__` at import time rather than pinned, so
that a launcher run straight from the index by ``uvx`` — where the version
floats silently between invocations — attaches the image built from its own
source. See :func:`image_tag_for` for which versions name an image.
"""

IMAGE_ENV = "PODBENCH_IMAGE"
"""Environment override for :data:`DEFAULT_IMAGE`, so a site can point every
podbench command at its own mirror or a pinned digest without a flag on each.
"""

SEAT_IDENTITY_VOLUME = "podbench-identity"
"""Pod volume holding a passwd/group file for the seat, mounted read-only.

A debug seat runs as the *target's* uid, which a stock image has no account
for — and ssh cannot authenticate a user NSS cannot resolve. A pod's volumes are
immutable, so the identity has to be put in the pod spec at deploy time, by the
same chart cooperation Patch mode already needs; from there it is mounted by
convention rather than by flag.

Which seat can *use* it is decided by :data:`SEAT_PASSWD_KEY`'s ``subPath``: a
``podbench dev`` sidecar can, an ephemeral ``attach`` seat cannot and registers
its own record instead.
"""

SEAT_PASSWD_KEY = "passwd"
SEAT_GROUP_KEY = "group"
"""Keys inside :data:`SEAT_IDENTITY_VOLUME`, mounted with ``subPath`` so they
land as files rather than replacing all of ``/etc``.

That ``subPath`` is the rule both halves have to know, and it is the whole of
why this volume serves one kind of seat and not the other:

* in an **ordinary** container - a ``podbench dev`` sidecar - it is legal, so
  the two files land and NSS resolves the seat's uid with nothing written at
  runtime and no GID 0 needed;
* in an **ephemeral** container - every ``attach`` seat - it is forbidden, and
  refused for the *whole* request: ``spec.ephemeralContainers[0].
  volumeMounts[0].subPath: Forbidden: cannot be set for an Ephemeral
  Container``. So the seat does not land at all, and there is no whole-volume
  alternative either - a directory mount over ``/etc/passwd`` replaces the file,
  and over ``/etc`` it takes ``nsswitch.conf`` with it. Such a seat registers
  its own record instead, which is what ``attach --seat-gid-root`` is for.

Both statements are checked against a rendered chart in
``tests/test_chart_contract.py``; the refusal is
:func:`podbench.spec.validate_ephemeral_volume_mounts`.
"""

SEAT_HOME_VOLUME = "podbench-home"
"""Pod volume for the seat's home directory.

vscode-server wants a few hundred MB of writable space and, on a live pod,
everything the seat writes counts against the workload's ephemeral-storage
limit — exceed it and the kubelet evicts the pod, workload included. A volume
moves that weight off the pod's budget, and survives the seat.
"""

SEAT_HOME_PATH = "/home/podbench"
"""Where :data:`SEAT_HOME_VOLUME` is mounted, and the home the passwd record
names. Both halves must agree: sshd puts the user in the home NSS gives it."""

TARGET_CID_ENV = "PODBENCH_TARGET_CID"
"""Env var carrying the target's container id into the debug container.

This is the one string the two halves must spell identically: the launcher
writes it into the container spec, the in-pod side reads it to attribute
processes, and a rename on one side alone degrades attribution to the cgroup
fallback silently (report 3.15). It is a name, not a type, but it is an
agreement, so it lives with the rest of them.
"""


PTRACE_READ_PATHS = ("root", "maps", "environ")
"""The ``/proc/<pid>`` reads that are actually gated by ``PTRACE_MODE_READ``.

Report 3.11 measured the split, and it is the whole of what the degraded rung
buys: these three (with ``exe`` and ``cwd``) survive at the target's own uid
and are lost at any other, while :data:`WORLD_READ_PATHS` are readable by
anyone sharing the PID namespace.

So only these three may decide whether read-only inspection is available. A
verdict taken from all six reads is taken mostly from constants — it was true
on a Diamond pod that could read none of these (issue #51), which is the
overclaim this split exists to make impossible.
"""

WORLD_READ_PATHS = ("cmdline", "status", "fd")
"""Reads that need no ptrace permission at all, so they prove nothing.

Kept named rather than implied: they are still worth *reporting* — "cmdline and
status only" is a usable answer — and only worthless as evidence of capability.
"""


def ptrace_reads_ok(proc_reads: Mapping[str, bool]) -> bool:
    """Whether read-only inspection of a target actually works.

    Every ptrace-gated read has to have landed, because the one claim made on
    the strength of this names all three of them, and a partial tick sends
    someone to a sysroot that will not open. Reads that need no permission are
    ignored entirely: see :data:`PTRACE_READ_PATHS`.

    >>> ptrace_reads_ok(dict.fromkeys(PTRACE_READ_PATHS, True))
    True
    >>> ptrace_reads_ok({"cmdline": True, "status": True, "root": False})
    False
    >>> ptrace_reads_ok({"cmdline": True, "status": True})
    False
    >>> ptrace_reads_ok({"maps": True})
    False
    """
    # An absent key is a no, not an abstention. Skipping the ones that were
    # never measured would let a single `maps: true` from an older image's
    # matrix carry the whole claim — the same overclaim as issue #51, one path
    # smaller — and it makes `all` over an empty matrix answer True as well.
    return all(proc_reads.get(name) is True for name in PTRACE_READ_PATHS)


def describe_reads(proc_reads: Mapping[str, bool]) -> str:
    """Say which ``/proc`` reads landed, in one line, without overclaiming.

    A bare boolean cannot express the Diamond shape — the ptrace-gated reads
    gone, the world-readable ones intact — and that is exactly the case where
    the user needs to know which half they still have.

    >>> describe_reads(dict.fromkeys(PTRACE_READ_PATHS, True))
    'root, maps and environ readable'
    >>> diamond = dict.fromkeys(PTRACE_READ_PATHS, False)
    >>> diamond.update(dict.fromkeys(WORLD_READ_PATHS, True))
    >>> describe_reads(diamond)
    'cmdline, status and fd only; root, maps and environ denied'
    >>> describe_reads({})
    'no /proc reads were measured'

    A gated read that survived alone is not folded into "only": that shape is
    what :func:`describe_gated_fallback` exists to word, and calling it "only"
    would hide the one path report 3.4 makes the fix.

    >>> describe_reads({"root": True, "maps": False, "environ": False})
    'root readable; maps and environ denied'
    """
    if not proc_reads:
        return "no /proc reads were measured"
    # A fixed order, not the mapping's: capreport emits the matrix in probe
    # order and `--json` re-sorts it alphabetically on the way to the launcher,
    # so the two halves would otherwise word the same measurement differently.
    ordered = [
        name for name in (*PTRACE_READ_PATHS, *WORLD_READ_PATHS) if name in proc_reads
    ]
    ordered += [name for name in proc_reads if name not in ordered]
    readable = [name for name in ordered if proc_reads[name]]
    denied = [name for name in ordered if not proc_reads[name]]
    if not readable:
        return f"{_names(denied)} all denied"
    if not denied:
        return f"{_names(readable)} readable"
    gated = [name for name in readable if name in PTRACE_READ_PATHS]
    kept = "readable" if gated else "only"
    return f"{_names(readable)} {kept}; {_names(denied)} denied"


def describe_gated_fallback(proc_reads: Mapping[str, bool]) -> str:
    """Whether a sysroot is still worth reaching for, given the matrix.

    :func:`ptrace_reads_ok` is all-or-nothing, so a matrix that kept ``root``
    and lost ``maps`` lands on the same rung as one that kept nothing — and the
    sentence that rung used to print steered the reader off ``set sysroot
    /proc/<pid>/root``, which report 3.4 makes the *mandatory* fix for wrong
    symbols. So the wording is built from which gated paths survived rather
    than from the verdict.

    >>> describe_gated_fallback(dict.fromkeys(PTRACE_READ_PATHS, False))
    'so a sysroot, `environ` or `maps` is not the fallback here'
    >>> describe_gated_fallback({"root": True, "maps": False, "environ": False})
    'so `maps` and `environ` will not open, but a sysroot on root still will'
    >>> describe_gated_fallback(dict.fromkeys(PTRACE_READ_PATHS, True))
    'and read-only inspection of the target still works'
    """
    kept = [name for name in PTRACE_READ_PATHS if proc_reads.get(name) is True]
    lost = [name for name in PTRACE_READ_PATHS if name not in kept]
    if not lost:
        return "and read-only inspection of the target still works"
    if not kept:
        return "so a sysroot, `environ` or `maps` is not the fallback here"
    tail = (
        "a sysroot on root still will"
        if "root" in kept
        else f"{_names([f'`{name}`' for name in kept])} still opens"
    )
    return f"so {_names([f'`{name}`' for name in lost])} will not open, but {tail}"


def _names(names: Sequence[str]) -> str:
    """``a, b and c`` — the report is read by people, not parsed."""
    # An empty list is no caller's case today, but `names[-1]` on the way to a
    # diagnostic is a crash in the one code path that must never crash.
    if len(names) < 2:
        return ", ".join(names)
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def as_dict(value: Any) -> dict[str, Any]:
    """Coerce a value out of decoded API-server JSON into a mapping.

    Every consumer of a pod's JSON walks a tree that is typed ``Any`` and that
    the server may legitimately have omitted, so "a dict, or an empty one" is
    the shape each of them needs. Defined once here because both halves parse
    the same documents, and three private copies of it drift.

    >>> as_dict({"a": 1})
    {'a': 1}
    >>> as_dict(None)
    {}
    """
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    return {}


class Verdict(enum.Enum):
    """What debugging is possible in the container podbench landed in.

    The values are process exit codes: ``capreport`` exits with them so that a
    shell script can branch on the outcome without parsing anything.
    """

    LIVE_ATTACH = 0
    """gdb can attach to the workload's own running processes."""

    READ_ONLY = 10
    """The target's memory is off limits, but its filesystem, maps and
    environment can be read — every path in :data:`PTRACE_READ_PATHS`."""

    LAUNCH_ONLY = 15
    """Read-only inspection of the target is not available, but the seat can
    still debug processes it starts itself.

    The rung between the other two, and the one a real cluster lands on: a
    Diamond production pod reported ``child_attach_ok`` with ``root``, ``maps``
    and ``environ`` all denied (issue #51). Calling that read-only sends someone
    to a sysroot that will not open; calling it nothing hides the inner loop
    that does work, since tracing your own descendants needs no capability and
    no Yama exemption (report 3.12). Its value sits between them because the
    codes are ordered by how much is possible.

    It is :data:`PTRACE_READ_PATHS` that are gone, not the target entirely:
    :data:`WORLD_READ_PATHS` answer on any pod whatsoever, so `pids` still
    names the processes. Saying "the target is closed" would be the same
    overclaim as #51 pointing the other way, which is why every report of this
    rung prints the matrix rather than a word for it.
    """

    NONE = 20
    """Neither attach nor inspection of the target is available. The seat
    itself (editor, shell, git) still works."""

    @property
    def summary(self) -> str:
        """A one-line description suitable for a session banner."""
        return {
            Verdict.LIVE_ATTACH: "live attach available",
            Verdict.READ_ONLY: "read-only inspection of the target; no live attach",
            Verdict.LAUNCH_ONLY: (
                "launch-only: `podbench dbg --launch` works; no read-only inspection"
            ),
            Verdict.NONE: "no access to the target process",
        }[self]


class Blocker(enum.Enum):
    """The mechanism that denied ptrace.

    Four unrelated subsystems refuse ``PTRACE_ATTACH`` with the same ``EPERM``,
    and a previous hand-rolled attempt at this tool reached same-UID and still
    could not tell which one had said no. Naming the blocker — rather than
    reporting the errno — is the point of the whole probe.
    """

    NONE = "none"
    """Nothing is blocking attach."""

    NO_CAP_SYS_PTRACE = "no-cap-sys-ptrace"
    """CAP_SYS_PTRACE is absent from the effective set and the UIDs differ."""

    YAMA_SCOPE = "yama-scope"
    """Yama's ``ptrace_scope`` forbids attaching to a non-descendant."""

    SECCOMP = "seccomp"
    """A seccomp filter is rejecting the ``ptrace`` syscall itself."""

    APPARMOR = "apparmor"
    """An AppArmor profile denies ptrace between these two domains."""

    UID_MISMATCH = "uid-mismatch"
    """The debug container runs as a different UID than the target."""

    ALREADY_TRACED = "already-traced"
    """Another process is already tracing the target. A tracee has exactly one
    tracer, so this refusal is indistinguishable from a policy one by errno."""

    UNKNOWN = "unknown"
    """Attach failed and none of the known mechanisms explains it."""

    @property
    def explanation(self) -> str:
        """Actionable text naming the mechanism and the way out of it."""
        return {
            Blocker.NONE: "nothing is blocking ptrace",
            Blocker.NO_CAP_SYS_PTRACE: (
                "CAP_SYS_PTRACE is not in this container's effective set. "
                "Relaunch with a --custom profile adding SYS_PTRACE, or run as "
                "the target's UID for read-only inspection."
            ),
            Blocker.YAMA_SCOPE: (
                "denied by Yama: /proc/sys/kernel/yama/ptrace_scope forbids "
                "attaching to a process that is not a descendant. This is a "
                "host-global, node-local setting; CAP_SYS_PTRACE overrides it."
            ),
            Blocker.SECCOMP: (
                "a seccomp filter is rejecting the ptrace syscall. The pod's "
                "seccompProfile has to allow ptrace for live attach to work."
            ),
            Blocker.APPARMOR: (
                "AppArmor denies ptrace between this container's profile and "
                "the target's. Both must be in a profile that permits it."
            ),
            Blocker.UID_MISMATCH: (
                "this container's UID differs from the target's and it has no "
                "CAP_SYS_PTRACE. Relaunch with runAsUser matching the target."
            ),
            Blocker.ALREADY_TRACED: (
                "the target already has a tracer, and a process can have only "
                "one: PTRACE_ATTACH returns the same EPERM a policy refusal "
                "does. Nothing about this container's privileges is at fault. "
                "Detach the debugger holding it — its pid is named in the "
                "report — and attach again."
            ),
            Blocker.UNKNOWN: (
                "ptrace was denied and none of the known mechanisms accounts "
                "for it. Please report this with the full capreport output."
            ),
        }[self]


class Rung(enum.Enum):
    """A step on the capability ladder the launcher walks.

    The launcher tries these in order and falls to the next one when admission
    refuses the previous, so that a restricted namespace still gets a working
    seat instead of an error.
    """

    FULL = "full"
    """root plus CAP_SYS_PTRACE. Live attach to the workload."""

    DEGRADED = "degraded"
    """The target's own UID, no added capabilities, everything dropped. Admitted
    under the restricted Pod Security Standard."""

    SEAT = "seat"
    """Whatever the cluster will admit. Editor, shell and git only."""

    @property
    def description(self) -> str:
        """One line for the launcher's capability report."""
        return {
            Rung.FULL: "root + CAP_SYS_PTRACE (live attach)",
            Rung.DEGRADED: "target UID, no capabilities (read-only inspection)",
            Rung.SEAT: "unprivileged seat (editor only)",
        }[self]


@dataclass(frozen=True)
class PodRef:
    """A pod, and the namespace it lives in."""

    namespace: str
    name: str

    def __str__(self) -> str:
        return f"{self.namespace}/{self.name}"


@dataclass(frozen=True)
class ContainerRef:
    """A container within a pod."""

    pod: PodRef
    container: str

    def __str__(self) -> str:
        return f"{self.pod}[{self.container}]"


@dataclass(frozen=True)
class ProcInfo:
    """A process visible in the shared PID namespace.

    ``container_id`` is how a process is attributed to the container that owns
    it: the debug container sees every process in the namespace, including its
    own, and matching the target's container id against ``/proc/<pid>/cgroup``
    is the only attribution that stays correct when a second podbench session
    is attached to the same pod.

    ``uid`` is optional because an unreadable ``/proc/<pid>/status`` is a real
    outcome and must stay distinguishable from uid 0. Root is the one value the
    degraded rung may never be handed: it costs the sysroot, maps, environ and
    exe that are the whole point of that rung, and the launcher picks
    ``runAsUser`` from here (report 3.11).
    """

    pid: int
    uid: int | None
    comm: str
    cmdline: str
    container_id: str | None = None
    is_target: bool = False


@dataclass(frozen=True)
class CapabilityReport:
    """The result of probing what this container may do to the target.

    Everything here is measured, never assumed: the probe reads the kernel's
    own accounting and then attempts a real scratch attach, because the
    permission rules involve four subsystems whose interactions are not worth
    predicting.
    """

    verdict: Verdict
    blocker: Blocker
    cap_sys_ptrace: bool
    cap_bounding_sys_ptrace: bool
    yama_scope: int | None
    seccomp_mode: int
    no_new_privs: bool
    apparmor_profile: str | None
    self_uid: int
    target_uid: int | None
    target_pid: int | None
    node_name: str | None = None
    child_attach_ok: bool | None = None
    """Whether attaching to the probe's own forked child worked. Yama always
    permits descendants, so a failure here is structural rather than a policy
    decision about the target."""

    target_attach_ok: bool | None = None
    proc_reads: dict[str, bool] = field(default_factory=dict[str, bool])
    notes: list[str] = field(default_factory=list[str])

    @property
    def can_attach(self) -> bool:
        """Whether gdb can attach to the workload's own processes."""
        return self.verdict is Verdict.LIVE_ATTACH

    @property
    def reads_ok(self) -> bool:
        """Whether read-only inspection of the target actually works.

        The launcher's tick and the probe's verdict must not be able to
        disagree, so both go through :func:`ptrace_reads_ok` on the same
        measurement.
        """
        return ptrace_reads_ok(self.proc_reads)

    @property
    def reads_summary(self) -> str:
        """The read matrix in one line, for a report that must show its work."""
        return describe_reads(self.proc_reads)

    @property
    def can_debug_launched(self) -> bool:
        """Whether the seat can debug a process it starts itself.

        Measured, and by the attach that policy is least likely to refuse: the
        credential check always passes against your own child and Yama permits
        it below ``ptrace_scope=2``, so a scratch attach that failed means
        ptrace(2) is unusable here and ``gdb ./prog`` — which traces a child of
        its own — cannot work either.

        It is a proxy in the other direction, and knowingly: the probe issues
        ``PTRACE_ATTACH`` on a fork while ``gdb ./prog`` uses
        ``PTRACE_TRACEME``, which report 3.12 measured as two separate rows.
        Yama gates the pair identically (scopes 0 and 1 permit both, 2 requires
        CAP_SYS_PTRACE of the tracer for both, 3 denies both) and a seccomp
        filter rejecting ``ptrace`` takes both, so the only known divergence is
        an AppArmor profile transition on the ``exec`` of the launched binary.
        """
        return self.child_attach_ok is True
