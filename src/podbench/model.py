"""Types shared across podbench's laptop-side and in-pod-side code.

podbench runs in two very different places — a launcher on a developer's
machine and a set of helpers inside a debug container — and the two halves have
to agree about what a capability verdict or a target reference means. Those
agreements live here so that neither half owns them.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, cast

__all__ = [
    "DEFAULT_IMAGE",
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
]

DEFAULT_IMAGE = "ghcr.io/gilesknap/podbench:latest"
"""The debug image the launcher attaches when nothing else is specified.

Both halves of the launcher — `attach` on a live pod and `dev` on an authored
one — put this into a container spec, and a release that bumped one and not the
other would leave the two modes silently running different builds.
"""

IMAGE_ENV = "PODBENCH_IMAGE"
"""Environment override for :data:`DEFAULT_IMAGE`, so a site can point every
podbench command at its own mirror or a pinned digest without a flag on each.
"""

TARGET_CID_ENV = "PODBENCH_TARGET_CID"
"""Env var carrying the target's container id into the debug container.

This is the one string the two halves must spell identically: the launcher
writes it into the container spec, the in-pod side reads it to attribute
processes, and a rename on one side alone degrades attribution to the cgroup
fallback silently (report 3.15). It is a name, not a type, but it is an
agreement, so it lives with the rest of them.
"""


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
    environment can be read, and processes launched from the debug container
    can be debugged normally."""

    NONE = 20
    """Neither attach nor inspection of the target is available. The seat
    itself (editor, shell, git) still works."""

    @property
    def summary(self) -> str:
        """A one-line description suitable for a session banner."""
        return {
            Verdict.LIVE_ATTACH: "live attach available",
            Verdict.READ_ONLY: "read-only inspection; debug launched processes instead",
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
