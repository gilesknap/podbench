"""Types shared across podbench's laptop-side and in-pod-side code.

podbench runs in two very different places — a launcher on a developer's
machine and a set of helpers inside a debug container — and the two halves have
to agree about what a capability verdict or a target reference means. Those
agreements live here so that neither half owns them.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

__all__ = [
    "Blocker",
    "CapabilityReport",
    "ContainerRef",
    "PodRef",
    "ProcInfo",
    "Rung",
    "Verdict",
]


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
    """

    pid: int
    uid: int
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
