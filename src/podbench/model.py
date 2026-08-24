"""Types shared across podbench's laptop-side and in-pod-side code.

podbench runs in two very different places — a launcher on a developer's
machine and a set of helpers inside a debug container — and the two halves have
to agree about what a capability verdict or a target reference means. Those
agreements live here so that neither half owns them.
"""

from __future__ import annotations

import enum
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from . import __version__

__all__ = [
    "DEAD_STATES",
    "DEFAULT_IMAGE",
    "FLOATING_TAG",
    "IMAGE_REPOSITORY",
    "NOT_MEASURED",
    "NOT_PROBED",
    "SEAT_REPORT_MARKER",
    "SEAT_REPORT_VERSION",
    "SeatReport",
    "PTRACE_READ_PATHS",
    "WORLD_READ_PATHS",
    "ATTACH_PROBE",
    "SEIZE_PROBE",
    "SEAT_GROUP_KEY",
    "SEAT_HOME_PATH",
    "SEAT_HOME_VOLUME",
    "SEAT_IDENTITY_VOLUME",
    "SEAT_PASSWD_KEY",
    "CLAIM_PATH_ENV",
    "IMAGE_ENV",
    "HOST_NETWORK_ENV",
    "OWNER_ENV",
    "POD_CONTAINERS_ENV",
    "TARGET_CID_ENV",
    "TARGET_NAME_ENV",
    "DEVPOD_LABEL",
    "HOTFIX_APP_PATH",
    "HOTFIX_CLAIM_VOLUME",
    "HOTFIX_CHILD_PID_PATH",
    "HOTFIX_HOLD_PATH",
    "HOTFIX_INTERPRETER_PATH",
    "Blocker",
    "CapabilityReport",
    "ContainerRef",
    "Lsm",
    "PodRef",
    "ProcInfo",
    "Rung",
    "SeatKind",
    "Verdict",
    "and_list",
    "as_dict",
    "describe_credentials",
    "describe_gated_fallback",
    "describe_lsm_remedy",
    "describe_pause",
    "describe_reads",
    "image_tag_for",
    "measured_rung",
    "measured_verdict",
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
same chart cooperation Hotfix mode already needs; from there it is mounted by
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
  runtime and nothing in the seat left writable for it;
* in an **ephemeral** container - every ``attach`` seat - it is forbidden, and
  refused for the *whole* request: ``spec.ephemeralContainers[0].
  volumeMounts[0].subPath: Forbidden: cannot be set for an Ephemeral
  Container``. So the seat does not land at all, and there is no whole-volume
  alternative either - a directory mount over ``/etc/passwd`` replaces the file,
  and over ``/etc`` it takes ``nsswitch.conf`` with it. Such a seat registers its
  own record instead, in :data:`podbench.agent.SEAT_NSS_PATH` - not in
  ``/etc/passwd``, which is writable only by GID 0 and so was out of reach of the
  degraded rung's own gid (#102).

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

HOST_NETWORK_ENV = "PODBENCH_HOST_NETWORK"
"""Env var carrying ``spec.hostNetwork`` into the debug container.

The seat cannot read it for itself: pod JSON is laptop-side, and nothing inside
a container distinguishes "this pod has its own network namespace" from "this
pod is using the node's". The difference is the whole of issue #87 — under
``hostNetwork: true`` a listener on ``127.0.0.1`` belongs to the *node*, shared
with every other hostNetwork pod and node daemon on it, so a port found there
is not evidence of anything in this pod and a server started there is exposed to
all of them.

Absent means **unknown**, never false: a seat landed by an older launcher
carries no such variable, and reading its absence as "no hostNetwork" would put
back the very claim this exists to stop.
"""

TARGET_NAME_ENV = "PODBENCH_TARGET"
"""Env var carrying the *name* of the container the seat was pointed at.

The id in :data:`TARGET_CID_ENV` is what attribution matches on, and it is not
a name anybody recognises: twelve hex digits identify the container to the
runtime and to nothing else. ``podbench pids`` heads its listing with this
instead, because "container X's processes" is the sentence that stops a
one-container reading of a three-container pod (finding 15).

The dev pod's sidecar has carried it since dev pods existed
(:func:`podbench.spec.dev_pod_spec`); this is the same variable, spelled once.
"""

POD_CONTAINERS_ENV = "PODBENCH_POD_CONTAINERS"
"""Env var carrying every container name in the pod, comma-separated.

Comma-separated because sshd's ``SetEnv`` ends a value at the first space
(:func:`podbench.sshcfg.unsafe_set_env`), and container names are DNS labels so
no name can contain one.

The seat cannot work this out for itself - it can see its own namespaces and
nothing of the pod object - and without it ``pids`` can name the container it is
showing but not the ones it is not, which is half of the answer a reader of a
multi-container pod needs. Absent means **unknown**, never "this pod has one
container": a seat landed by an older launcher carries no such variable.
"""

OWNER_ENV = "PODBENCH_OWNER"
"""Env var carrying the cluster identity that landed this seat.

A seat is otherwise anonymous, so ``podbench-1`` beside ``podbench-2`` gives a
second person no way to tell which one is theirs — and reconnecting into
somebody else's is a login refused by an ``authorized_keys`` written for another
key (issue #113).

It lives in the container's ``env`` because that is the only place on an
ephemeral container that survives both of the things ownership has to outlive.
A container **name** is burnt the moment the container exits, so an owner keyed
on the name goes stale on the next OOM; the ``env`` is a property of the
container spec, which the API server keeps for the pod's lifetime and hands back
verbatim to any later ``kubectl get pod -o json``. And a securityContext is
fixed for that same lifetime, so a seat whose gid the correction could only
mend by landing a *second* container (#103) has two names and one owner — which
is what an owner recorded per container, at the moment it is authored, gets
right and a mapping keyed on either id does not.

A pod annotation would do neither better and costs an RBAC grant podbench
deliberately does not ask for: it writes the ``ephemeralcontainers``
subresource, never the pod object.

Absent means **unknown**, never "mine": every seat landed before this variable
existed carries none, as does one landed where ``kubectl auth whoami`` would not
name the kubeconfig's user. Reporting it as unknown is the same rule the memory
row and the rung label are held to — never assert what was not measured.
"""

TARGET_CID_ENV = "PODBENCH_TARGET_CID"
"""Env var carrying the target's container id into the debug container.

This is the one string the two halves must spell identically: the launcher
writes it into the container spec, the in-pod side reads it to attribute
processes, and a rename on one side alone degrades attribution to the cgroup
fallback silently (report 3.15). It is a name, not a type, but it is an
agreement, so it lives with the rest of them.
"""

CLAIM_PATH_ENV = "PODBENCH_CLAIM_PATH"
"""Env var carrying *this seat's* mountPath for :data:`HOTFIX_CLAIM_VOLUME`.

The seat has to know it and cannot work it out: the mountPath is the
application's choice - podbench matches it, never picks it
(:func:`podbench.hotfix.claim_mount_path`) - and from inside the container a
directory that is on a volume looks like any other. So it arrives the way the
seat's other launcher-known facts do, in the container spec's ``env``, and
:func:`podbench.agent.ensure_seat_gitconfig` writes a ``safe.directory`` for it
at start-up.

Written from the mounts being authored rather than passed in beside them
(:func:`podbench.spec.ephemeral_container_spec`), so the two cannot disagree:
a seat told the claim is somewhere it does not mount would authorise a
directory that is not there, and :data:`HOTFIX_APP_PATH` is precisely the guess
:func:`podbench.launcher.seat_claim_path` exists to avoid.

Absent means **no claim**, which is the ordinary ``attach`` seat and every
seat an older launcher landed. Nothing is written for one.
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

SEIZE_PROBE = "PTRACE_SEIZE"
"""The primitive the live-attach probe issues, and the reason it costs no pause.

``PTRACE_SEIZE`` goes through the same ``PTRACE_MODE_ATTACH_REALCREDS`` check as
``PTRACE_ATTACH`` - so a seize that succeeds proves gdb's attach would too - but
it leaves the tracee running rather than stopping it. Measured against live
``p47-blueapi-0`` pid 1 (195 threads) from a capless seat on 2026-08-19: state
``S`` before, ``S`` while seized, ``S`` after. In the kernel since 3.4.
"""

ATTACH_PROBE = "PTRACE_ATTACH"
"""The stopping primitive: the scratch attach, and the fallback on a pre-3.4
kernel that answers ``EIO`` to a seize. It SIGSTOPs the tracee until the probe
reaps the stop and detaches, which is a pause the workload pays."""

DEAD_STATES = frozenset({"Z", "X", "x"})
"""``/proc/<pid>/status`` ``State:`` letters that mean there is nothing left.

``Z`` is a zombie awaiting a reaper, ``X``/``x`` a task already gone. All three
answer ``EPERM`` to ``PTRACE_ATTACH`` however good the tracer's credentials are
— measured against ``p47-blueapi-0`` pid 518, where pid 1 attached from the same
seat in the same second.
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
        return f"{and_list(denied)} all denied"
    if not denied:
        return f"{and_list(readable)} readable"
    gated = [name for name in readable if name in PTRACE_READ_PATHS]
    kept = "readable" if gated else "only"
    return f"{and_list(readable)} {kept}; {and_list(denied)} denied"


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
        else f"{and_list([f'`{name}`' for name in kept])} still opens"
    )
    return f"so {and_list([f'`{name}`' for name in lost])} will not open, but {tail}"


def and_list(names: Sequence[str]) -> str:
    """``a, b and c`` — the report is read by people, not parsed.

    Public, and in this module, because both halves say it: the launcher names
    the containers an attach did not enter, and ``pids`` names them again from
    inside the seat (finding 15). Two spellings of the same list is the kind of
    difference a reader takes for a difference in meaning.

    >>> and_list(["ca", "pva"])
    'ca and pva'
    """
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


NOT_PROBED = "not probed"
"""What a report puts in a verdict column it has no measurement for.

A verdict column may hold exactly two kinds of thing: something a probe
measured, or this. Filling it from the rung the cluster admitted is issue #89 —
a seat whose ``capabilities.add`` a mutating webhook stripped reads back as
:attr:`Rung.DEGRADED` and ptraces the target perfectly well, so the rung is
evidence of what was *asked for* and of nothing else.
"""


def measured_verdict(
    attach_ok: bool | None,
    proc_reads: Mapping[str, bool],
    child_ok: bool | None,
) -> Verdict | None:
    """The best verdict these three measurements support, or ``None`` for none.

    The arguments are :attr:`CapabilityReport.target_attach_ok`,
    :attr:`~CapabilityReport.proc_reads` and
    :attr:`~CapabilityReport.child_attach_ok` — what was *attempted*, rather
    than :attr:`CapabilityReport.verdict`, which is not the same thing twice
    over. :func:`podbench.probe.derive_verdict` answers ``LIVE_ATTACH`` when
    there is no target pid at all ("live attach was not tested"), which is a
    fair session banner and a false claim in a column; and a launcher older
    than the image it is reading shows an unrecognised verdict as ``NONE``,
    which recomputing here corrects for the same reason the read-only tick is
    recomputed rather than read off the enum.

    >>> measured_verdict(True, {}, None).name
    'LIVE_ATTACH'
    >>> reads = dict.fromkeys(PTRACE_READ_PATHS, True)
    >>> measured_verdict(False, reads, True).name
    'READ_ONLY'

    Read-only is :func:`ptrace_reads_ok`'s to grant, never a partial matrix's:
    a seat that kept ``root`` alone is launch-only, and calling it read-only
    sends someone to an ``environ`` that will not open.

    >>> measured_verdict(False, {"root": True}, True).name
    'LAUNCH_ONLY'
    >>> measured_verdict(False, {}, False).name
    'NONE'

    Nothing attempted is not the same as everything refused, and the difference
    is the whole point: an unprobed seat is reported as :data:`NOT_PROBED`, not
    as a seat that can do nothing.

    >>> measured_verdict(None, {}, None) is None
    True
    """
    if attach_ok is True:
        return Verdict.LIVE_ATTACH
    if ptrace_reads_ok(proc_reads):
        return Verdict.READ_ONLY
    if child_ok is True:
        return Verdict.LAUNCH_ONLY
    # A probe that measured nothing at all cannot report the bottom rung: every
    # one of these is `None` for "not attempted", and `proc_reads` is empty
    # before the matrix is read as well as after every read failed.
    if attach_ok is None and child_ok is None and not proc_reads:
        return None
    return Verdict.NONE


class Lsm(enum.Enum):
    """Which label-based Linux Security Module owns ``/proc/<pid>/attr/current``.

    That attribute is one slot shared by every such LSM, so the string in it
    does not say whose it is. podbench printed it under an "AppArmor" heading
    and treated any non-``unconfined`` value as confinement, which on the RHEL9
    nodes it is deployed to reported an SELinux context as an AppArmor profile
    and made every seat read as confined (#104).
    """

    SELINUX = "selinux"
    APPARMOR = "apparmor"
    NONE = "none"
    """No label-based LSM is active on this node - which is a real answer, and
    the one the k3s test bed gives (``apparmor=0`` on its kernel command line,
    no selinuxfs). A report that cannot say this ends up advising the reader to
    go and check an LSM that is not there."""

    @property
    def title(self) -> str:
        """The LSM's name as it is spelled in its own documentation.

        >>> Lsm.SELINUX.title, Lsm.APPARMOR.title, Lsm.NONE.title
        ('SELinux', 'AppArmor', 'LSM')
        """
        return {Lsm.SELINUX: "SELinux", Lsm.APPARMOR: "AppArmor", Lsm.NONE: "LSM"}[self]


def describe_lsm_remedy(lsm: Lsm, node_name: str | None) -> str:
    """Where to go and look for an LSM denial, or why there is nowhere to look.

    An LSM refusal is logged on the **node**, never in the pod: the seat sees
    the same bare ``EPERM`` as every other mechanism. So the only actionable
    thing podbench can say is the command and the machine to run it on - and it
    already knows the machine, because :attr:`CapabilityReport.node_name` is on
    the report (``PODBENCH_NODE_NAME``).

    >>> describe_lsm_remedy(Lsm.SELINUX, "cn01")
    'SELinux denials are logged on node cn01: `ausearch -m avc -ts recent`'
    >>> describe_lsm_remedy(Lsm.APPARMOR, None)
    'AppArmor denials are logged on the node: `dmesg | grep -i apparmor`'
    >>> describe_lsm_remedy(Lsm.NONE, "cn01")
    'this node runs neither SELinux nor AppArmor'
    """
    where = f"node {node_name}" if node_name else "the node"
    if lsm is Lsm.SELINUX:
        return f"SELinux denials are logged on {where}: `ausearch -m avc -ts recent`"
    if lsm is Lsm.APPARMOR:
        return f"AppArmor denials are logged on {where}: `dmesg | grep -i apparmor`"
    return "this node runs neither SELinux nor AppArmor"


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

    LSM_MISMATCH = "lsm-mismatch"
    """The seat and the target carry *different* LSM labels.

    A label on its own says nothing - under SELinux every container on the node
    carries one, and containers in a pod normally share it. What denies ptrace
    is a *difference*: differing MCS categories under SELinux, differing
    profiles under AppArmor. So this is a comparison, never a verdict read off
    one side (#104)."""

    APPARMOR = "apparmor"
    """Superseded by :attr:`LSM_MISMATCH`, and kept so that a seat older than
    #104 still parses. Nothing emits it; the launcher may still read it."""

    UID_MISMATCH = "uid-mismatch"
    """The debug container runs as a different UID than the target."""

    GID_MISMATCH = "gid-mismatch"
    """The UIDs match and the GIDs do not.

    A peer of :attr:`UID_MISMATCH` rather than a footnote to it, because
    ``__ptrace_may_access()`` compares six ids and not three: ``gid``, ``egid``
    and ``sgid`` are checked exactly as ``uid``, ``euid`` and ``sgid`` are, and
    any one of the six differing denies every ptrace-gated operation. Without
    this arm the commonest shape at Diamond — a seat at the target's uid and the
    image's gid 0 — fell through to :attr:`UNKNOWN`, which sent the reader off
    to check AppArmor and user namespaces (#103)."""

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
                "a seccomp filter is rejecting the ptrace syscall, so neither "
                "attaching nor launching an inferior works. The filter is this "
                "container's own seccompProfile - podbench mirrors the "
                "target's rather than imposing one, so where it denies ptrace "
                "it is the profile the pod runs under that has to change."
            ),
            Blocker.LSM_MISMATCH: (
                "the seat and the target carry different security labels, and "
                "the LSM permits ptrace only within one. Both labels are on "
                "the report above; the denial itself is logged on the node, "
                "not in the pod."
            ),
            Blocker.APPARMOR: (
                "an image older than #104 named AppArmor here without knowing "
                "which LSM was active. Read the two labels on the report; a "
                "current image compares them and names the real one."
            ),
            Blocker.UID_MISMATCH: (
                "this container's UID differs from the target's and it has no "
                "CAP_SYS_PTRACE. Relaunch with runAsUser matching the target."
            ),
            Blocker.GID_MISMATCH: (
                "this container's GID differs from the target's and it has no "
                "CAP_SYS_PTRACE. The uid matching is not enough: "
                "__ptrace_may_access() compares gid, egid and sgid as peers of "
                "uid, euid and suid, and one differing pair denies the whole "
                "check - live attach and the PTRACE_MODE_READ paths with it. "
                "podbench lands a corrected seat itself unless "
                "--no-correct-ids was given; `podbench attach <pod> --new "
                "--target-gid <gid>` pins it by hand."
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


DEVPOD_LABEL = "podbench.dev/devpod"
"""Marks a pod as Iterate mode's sacrificial clone.

Here rather than in :mod:`podbench.spec` because it is half of how a *listing*
tells podbench's three modes apart, and a listing reads that vocabulary
together or not at all. Hotfix mode's half used to be an annotation beside
this; it is now :data:`HOTFIX_CLAIM_VOLUME` and the supervisor in the pod spec,
because Argo strips pod-template annotations on self-heal and a label podbench
never gets to write is a signal that reads ``False`` forever (#32, #177).
"""

HOTFIX_CLAIM_VOLUME = "podbench-app"
"""The volume carrying a hotfixed project, mounted *beside* the application.

Named for what it holds rather than for a venv, because it no longer is one:
the claim carries the project - source, `.venv` and interpreter - and the
application's own `/app` is never occluded. Mounting *over* the venv was the
original design and every awkward thing about it followed from that one choice:
the image's venv sat behind the mount, so seeding needed an initContainer at a
staging path that `ioc-instance` cannot express, and a seat that is itself a
python-copier-template image lost its own `/app/.venv`.
"""

HOTFIX_APP_PATH = "/podbench/app"
"""Where :data:`HOTFIX_CLAIM_VOLUME` is mounted.

Beside the application's project, never over it. Nothing the image ships is
hidden by this mount, which is what lets the seed be a plain copy from the
running container rather than an initContainer racing the application.
"""

HOTFIX_INTERPRETER_PATH = "/podbench/app/.python"
"""The interpreter, on the claim.

A rebuilt venv's console scripts carry an absolute shebang, so the interpreter
they name has to outlive a restart too. Copying it here is what makes
`head -1 <venv>/bin/<script>` point at the claim rather than back into the
image.
"""

HOTFIX_HOLD_PATH = "/tmp/podbench-hold"
"""Presence of this file makes the supervisor relaunch rather than exit.

It is the runtime switch the whole mode turns on: absent - the production case
- the supervisor exits with the child's status and the kubelet restarts exactly
as it does today.
"""

HOTFIX_CHILD_PID_PATH = "/tmp/podbench-child.pid"
"""Where the supervisor records the pid of the process it started.

The pid is the *root of a tree*, not a thing to signal on its own: a target
that allocates a pty puts its real process in another session, so killing this
pid reaps the wrapper and leaves the application running - silently, with the
old code still serving. See spike S7.
"""
"""Marks a pod (or pod template) as carrying a hotfix on a claim.

Its natural home is :mod:`podbench.hotfix`, and it cannot live there: that
module imports :mod:`podbench.launcher`, which is where the listing is built.
"""


class SeatKind(enum.Enum):
    """Which of podbench's three modes a seat is serving.

    A property of the **pod**, not of the container: the docs introduce three
    modes and the cluster records each of them somewhere else - a label, an
    annotation, and a volumeMount that neither of them mentions. A seat carries
    no stamp saying which it is, so this is derived on every read
    (``podbench.launcher.seat_kind``) and never stored. That also makes it true
    on a machine that never saw the attach, which is where a listing usually
    runs.

    Two of these are the same *container* kind and the third is not, which is
    the asymmetry that makes the question worth asking at all: :attr:`ATTACH`
    and :attr:`HOTFIX` are both ephemeral containers differing by one mount,
    while :attr:`DEV` is an ordinary sidecar in a pod of its own.
    """

    ATTACH = "attach"
    """Observe mode: an ephemeral container that touches the workload not at
    all. The default, and the one with no marker of its own - it is what a seat
    is when neither of the others applies."""

    DEV = "dev"
    """Iterate mode: the ``podbench`` sidecar of a pod labelled
    :data:`DEVPOD_LABEL`. Not an ephemeral container, which is why it is
    invisible to anything reading ``spec.ephemeralContainers``."""

    HOTFIX = "hotfix"
    """An ephemeral container in a pod carrying the hotfix layout - it declares
    :data:`HOTFIX_CLAIM_VOLUME` and runs the supervisor
    (``podbench.launcher.is_hotfixed``) - that mounts one of the workload's own
    volumes, the claim the project lives on. The mount is what makes it this
    kind and not :attr:`ATTACH`: a seat without it resolves the image's venv
    while the application runs the claim's."""


class Rung(enum.Enum):
    """A step on the capability ladder the launcher walks.

    The launcher tries these in order and falls to the next one when admission
    refuses the previous, so that a restricted namespace still gets a working
    seat instead of an error.

    Each member says what a seat *is*, so it may only be applied to one that
    was measured: :func:`measured_rung` reads the seat's own
    ``/proc/self/status``. ``podbench.launcher.rung_of_spec`` names the rung a
    container was *authored* at, which is a weaker claim about a different
    thing — it reads a securityContext, and a securityContext is not a
    container.
    """

    FULL = "full"
    """root plus CAP_SYS_PTRACE. Live attach to the workload."""

    DEGRADED = "degraded"
    """The seat's uid *and gid* match the target's, with no added capabilities.

    What the seat *can do*, not what admitted it. The credential match is the
    whole of the rung — it is what ``__ptrace_may_access()`` checks and what
    buys all six ``PTRACE_MODE_READ`` paths — and the kernel applies it at uid
    0 exactly as it does at uid 1000. That check compares the three group ids
    as peers of the three user ids, so the match is both numbers: a seat at
    1000:0 beside a target at 1000:1000 is denied on the group half alone
    (#103) and belongs on :attr:`SEAT`. Naming the uid and forgetting the gid
    is what let one p47 seat be labelled ``degraded`` beside its own verdict of
    "launch-only ... no read-only inspection".

    The sentence this used to end with, "admitted under the restricted Pod
    Security Standard", is a fact about the *authored* non-root context and now
    lives with the authoring: see ``podbench.spec._rung_security_context``. A
    root seat against a root target is this rung and could never have been
    admitted under restricted, which is how one seat came to be labelled three
    different ways (issue #94)."""

    SEAT = "seat"
    """Whatever the cluster will admit. Editor, shell and git only."""


def measured_rung(
    uid: int | None,
    *,
    sys_ptrace: bool,
    target_uid: int | None,
    gid: int | None,
    target_gid: int | None,
) -> Rung | None:
    """Which rung a seat's own ``/proc/self/status`` puts it on, or ``None``.

    The rung podbench *asked* for is not evidence of anything (issue #94). The
    spec that lands is the spec admission left behind, and even that is not the
    container: the image's own ``USER`` never appears in it, and a capability
    stored in ``securityContext`` reaches ``CapEff`` only at uid 0 (report
    §3.10). Both directions were measured at DLS on 2026-08-19 — podbench asks
    for ``capabilities: {drop: [ALL]}`` and admission stores thirteen added
    capabilities, while the seat carrying them reports ``CapEff:
    0000000000000000`` at uid 37887. So the label comes from the four numbers
    the kernel will actually check.

    The capability decides first, because it is the whole of the full rung and
    it is exempt from the credential check — so neither group id is asked for:

    >>> measured_rung(0, sys_ptrace=True, target_uid=1000,
    ...               gid=0, target_gid=1000).name
    'FULL'

    Below it, the rung is the credential *match*, not the pin: matching the
    target is what buys the ``PTRACE_MODE_READ`` paths, and root without the
    capability reads three of the six where the target's own ids read all six
    (report §3.11). So a stripped full rung — root, no capability — is named
    for what it can do rather than for the securityContext it now resembles:

    >>> measured_rung(1000, sys_ptrace=False, target_uid=1000,
    ...               gid=1000, target_gid=1000).name
    'DEGRADED'
    >>> measured_rung(0, sys_ptrace=False, target_uid=1000,
    ...               gid=0, target_gid=1000).name
    'SEAT'
    >>> measured_rung(65532, sys_ptrace=False, target_uid=1000,
    ...               gid=65532, target_gid=1000).name
    'SEAT'

    **The match is four numbers and not two.** ``__ptrace_may_access()`` compares
    ``gid``, ``egid`` and ``sgid`` as peers of ``uid``, ``euid`` and ``suid``,
    and returns before Yama is consulted if any one pair differs - which is why
    :attr:`Blocker.GID_MISMATCH` sits between the uid arm and Yama, and why
    podbench lands a gid-corrected seat at all (#103). A seat whose uid matches
    and whose gid does not can no more inspect the target than one that matches
    neither, so it is the bottom rung. Measured on p47-blueapi-0 (2026-08-21): a
    1000:0 seat against a 1000:1000 target was labelled ``degraded`` beside a
    verdict of its own reading "launch-only ... no read-only inspection".

    >>> measured_rung(1000, sys_ptrace=False, target_uid=1000,
    ...               gid=0, target_gid=1000).name
    'SEAT'

    The uid match is tested as a match and not as a truthy uid. Root matching
    root is the commonest shape there is on a cluster that pins no
    ``runAsUser``, and reading uid 0 as "nothing pinned" put every argus seat
    one rung below what it measurably was, while the same seat read all six
    ``/proc`` paths and ran gdb to a symbolised backtrace (issue #94,
    2026-08-19):

    >>> measured_rung(0, sys_ptrace=False, target_uid=0,
    ...               gid=0, target_gid=0).name
    'DEGRADED'

    An unknown target uid is not a match, however: nothing was compared.

    >>> measured_rung(0, sys_ptrace=False, target_uid=None,
    ...               gid=0, target_gid=None).name
    'SEAT'

    ``None`` means the seat could not be read at all, which is not a rung and
    must not be reported as one — the caller falls back to naming what was
    asked for, and says so.

    >>> measured_rung(None, sys_ptrace=False, target_uid=1000,
    ...               gid=None, target_gid=1000) is None
    True

    A gid that could not be read is the same answer for the same reason. The
    uids matching is half of a comparison, and half of a comparison is not a
    rung: reporting ``degraded`` there would be the overclaim this function
    exists to stop, one pair of numbers further along.

    >>> measured_rung(1000, sys_ptrace=False, target_uid=1000,
    ...               gid=1000, target_gid=None) is None
    True
    >>> measured_rung(1000, sys_ptrace=False, target_uid=1000,
    ...               gid=None, target_gid=1000) is None
    True

    A uid that provably differs needs no gid, though: one differing pair is
    already the whole answer, so nothing is left unmeasured.

    >>> measured_rung(0, sys_ptrace=False, target_uid=1000,
    ...               gid=None, target_gid=None).name
    'SEAT'
    """
    if uid is None:
        return None
    if sys_ptrace:
        return Rung.FULL
    if target_uid is None or uid != target_uid:
        return Rung.SEAT
    if gid is None or target_gid is None:
        return None
    return Rung.DEGRADED if gid == target_gid else Rung.SEAT


NOT_MEASURED = "not measured"
"""What a rung column holds when the seat's own measurement could not be read.

The other half of :data:`NOT_PROBED`'s rule, applied to the rung rather than to
the verdict: a listing that cannot reach a seat's start-up report says so, and
never dresses :func:`podbench.launcher.rung_of_spec`'s reading of a
securityContext in a measured label."""

SEAT_REPORT_MARKER = "podbench-report:"
"""How a seat's start-up report is found again in the container log.

The channel is the log because ``list`` and ``status`` read pod JSON and exec
nothing, and a verb that ran one ``kubectl exec`` per seat to name a rung would
be paying an exec per seat against a whole namespace. ``kubectl logs`` is a
*read*. The marker is matched anywhere in a line, so a prefix - the agent's own
``agent: ``, or ``kubectl logs --timestamps`` - does not hide it."""

SEAT_REPORT_VERSION = 1
"""The schema of the line under :data:`SEAT_REPORT_MARKER`.

Carried so a reader can tell a shape it has been taught from one it has not.
Nothing is *rejected* on it: unknown keys are ignored and absent ones default,
because the seat and the launcher are separately versioned by construction - a
node serving a cached layer of a tag that moves runs whichever build it has -
and the failure that matters is a newer launcher against an older seat, which
must degrade to "not measured" rather than raise."""


@dataclass(frozen=True)
class SeatReport:
    """What a seat measured about itself at start-up, and what it could not do.

    Emitted once, by the agent, into the container's log. Two questions are
    answered by one line because they have the same problem: the rung a seat
    *is* can only be read from inside it (:func:`measured_rung` reads
    ``/proc/self/status``), and a start-up step that gave up explains itself to
    a log nobody opens (issue #99). Both are now recoverable from a laptop
    without an exec.

    The numbers are the ones the kernel checks and nothing derived from them,
    so an older report stays readable by a launcher that has learned to label
    them differently - which is exactly what happened to the degraded rung in
    issue #94.

    >>> report = SeatReport(uid=0, gid=0, effective_hex="0000000000000000",
    ...                     sys_ptrace=False, target_uid=0, target_gid=0)
    >>> report.rung.name
    'DEGRADED'
    >>> SeatReport.from_log("agent: " + report.to_line()) == report
    True

    A log with no report in it is not a report, and neither is one this build
    cannot parse:

    >>> SeatReport.from_log("agent: prepared /root") is None
    True
    >>> SeatReport.from_log("podbench-report: {not json") is None
    True

    An older seat's line, missing everything this build has since learned to
    ask for, still yields what it does say:

    >>> older = SeatReport.from_log('podbench-report: {"uid": 1000}')
    >>> older.uid, older.target_uid, older.rung is None
    (1000, None, True)
    """

    uid: int | None = None
    gid: int | None = None
    effective_hex: str = "unreadable"
    sys_ptrace: bool = False
    target_uid: int | None = None
    target_gid: int | None = None
    target_pid: int | None = None
    failures: tuple[str, ...] = ()
    """Start-up steps that gave up, each already formatted as the seat said it.

    ``ensure_all`` records rather than raises - the caller is PID 1 of an
    unrestartable container - so this is the whole of what a refused step ever
    produced. #99 is that it produced it into the container log."""

    version: int = SEAT_REPORT_VERSION

    @property
    def rung(self) -> Rung | None:
        """The rung these numbers put the seat on, or ``None`` for unreadable.

        Derived on the *reading* side rather than stored, deliberately: a seat
        already running when this launcher was installed emitted its numbers
        under the previous definition of :attr:`Rung.DEGRADED`, and a stored
        label would make that seat argue with the one the launcher would give
        it. The numbers do not go out of date.

        A target uid the seat could not discover is ``None`` and not a rung.
        :func:`measured_rung` answers ``SEAT`` for an unknown target, because
        its caller has the manifest to fall back on and says which it used;
        this one has nothing else, and "cannot match the target" is a claim
        about a comparison that was never made. An unread *gid* on either side
        is the same claim about the same comparison, and :func:`measured_rung`
        answers ``None`` for it here and everywhere.

        >>> SeatReport(uid=1000, gid=0, sys_ptrace=False,
        ...            target_uid=1000, target_gid=1000).rung.name
        'SEAT'
        >>> SeatReport(uid=1000, gid=1000, sys_ptrace=False,
        ...            target_uid=1000).rung is None
        True
        """
        if self.uid is None:
            return None
        if not self.sys_ptrace and self.target_uid is None:
            return None
        return measured_rung(
            self.uid,
            sys_ptrace=self.sys_ptrace,
            target_uid=self.target_uid,
            gid=self.gid,
            target_gid=self.target_gid,
        )

    def to_line(self) -> str:
        """The single log line a seat writes at start-up."""
        payload = {
            "version": self.version,
            "uid": self.uid,
            "gid": self.gid,
            "effective_hex": self.effective_hex,
            "sys_ptrace": self.sys_ptrace,
            "target_uid": self.target_uid,
            "target_gid": self.target_gid,
            "target_pid": self.target_pid,
            "failures": list(self.failures),
        }
        return f"{SEAT_REPORT_MARKER} {json.dumps(payload, sort_keys=True)}"

    @classmethod
    def from_log(cls, text: str) -> SeatReport | None:
        """The last report in a container log, or ``None`` if there is none.

        The *last*, because a container restarted by nothing can still have run
        the agent twice - ``--ensure-only`` is a supported way in - and the
        newest reading is the one that describes the seat now.
        """
        for line in reversed(text.splitlines()):
            marker = line.find(SEAT_REPORT_MARKER)
            if marker < 0:
                continue
            try:
                payload = json.loads(line[marker + len(SEAT_REPORT_MARKER) :])
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            fields = cast(Mapping[str, Any], payload)
            return cls(
                uid=_optional_int(fields.get("uid")),
                gid=_optional_int(fields.get("gid")),
                effective_hex=str(fields.get("effective_hex", "unreadable")),
                sys_ptrace=bool(fields.get("sys_ptrace", False)),
                target_uid=_optional_int(fields.get("target_uid")),
                target_gid=_optional_int(fields.get("target_gid")),
                target_pid=_optional_int(fields.get("target_pid")),
                failures=tuple(
                    str(entry) for entry in _as_sequence(fields.get("failures"))
                ),
                version=_optional_int(fields.get("version")) or 0,
            )
        return None


def _optional_int(value: Any) -> int | None:
    """An int from a JSON field, and ``None`` for anything that is not one.

    ``bool`` is excluded because it is an ``int`` in Python and a uid of
    ``True`` is a defect wearing a plausible type.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_sequence(value: Any) -> Sequence[Any]:
    return cast(Sequence[Any], value) if isinstance(value, list) else ()


def describe_pause(method: str | None, attach_ok: bool | None) -> str:
    """What the live-attach probe cost the workload, in one line.

    Said out loud on every report rather than only when it is bad news, because
    the complaint the seize answers was never the stop itself: it was that a
    pause happened *silently*, on pods where a pause is forbidden (finding 14).
    A line that only appears when something went wrong cannot be checked before
    the fact.

    >>> describe_pause(SEIZE_PROBE, True)
    'none - PTRACE_SEIZE does not stop the tracee'
    >>> describe_pause(ATTACH_PROBE, True)
    'brief - PTRACE_ATTACH stopped it until the probe detached'
    >>> describe_pause(ATTACH_PROBE, False)
    'none - the attach was refused, so nothing stopped'
    >>> describe_pause(None, None)
    'not measured'
    >>> describe_pause("PTRACE_LATER", True)
    'unknown - the seat probed with PTRACE_LATER'
    """
    if not method or attach_ok is None:
        # An empty method is a probe that issued nothing, which is the same
        # silence as a report from before this line existed.
        return "not measured"
    if method == SEIZE_PROBE:
        return f"none - {SEIZE_PROBE} does not stop the tracee"
    if not attach_ok:
        # Nothing was traced, so nothing was stopped, whichever primitive asked.
        return "none - the attach was refused, so nothing stopped"
    if method == ATTACH_PROBE:
        return f"brief - {ATTACH_PROBE} stopped it until the probe detached"
    # A newer image probing with something this launcher has not been taught.
    # Naming it and declining to characterise it beats claiming either answer:
    # "none" would be an assurance nobody measured.
    return f"unknown - the seat probed with {method}"


def describe_credentials(uid: int | None, gid: int | None, effective_hex: str) -> str:
    """The measurement a rung column cites, in one line.

    Was a :class:`Rung`-to-prose lookup, which could only ever restate the
    label. These are the numbers behind it, so a reader can see the rung is
    reported rather than assumed - and the one shape that used to be
    unsayable, root with nothing effective, says itself.

    >>> describe_credentials(0, 0, "0000000000000000")
    'uid 0, gid 0, CapEff 0000000000000000'
    >>> describe_credentials(None, None, "unreadable")
    'uid unknown, gid unknown, CapEff unreadable'
    """
    return (
        f"uid {uid if uid is not None else 'unknown'}, "
        f"gid {gid if gid is not None else 'unknown'}, "
        f"CapEff {effective_hex}"
    )


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

    ``ppid`` is read because an image whose entrypoint is a script has the shell
    as its lowest pid, and the process worth debugging is somewhere below it.
    Depth in the tree separates those two — but it is *not* the only measurable
    signal, and ranking on it alone picked a zombie on blueapi, a DNS helper on
    rabbitmq and an unattachable worker on opis. :attr:`state`, :attr:`threads`
    and :attr:`ptrace_readable` are the other three, each a measured correction
    to one of those pods; see :func:`podbench.proc.debug_candidates`.
    """

    pid: int
    uid: int | None
    comm: str
    cmdline: str
    container_id: str | None = None
    is_target: bool = False
    ppid: int | None = None
    """The parent, or ``None`` when ``/proc/<pid>/status`` could not be read."""

    state: str | None = None
    """The kernel's one-letter ``State:``, or ``None`` when status was unreadable.

    Carried so that a *dead* process can be told from a live one. A zombie has
    no ``mm_struct``, so ``/proc/<pid>/maps`` opens with no ptrace check at all
    and the read matrix scores it as partly working — which is how blueapi's
    unreaped ``multiprocessing`` child read as "3/6 ok" and was reported as an
    LSM denial for months (issue #52). Nothing can attach to it.
    """

    threads: int | None = None
    """``Threads:`` from status, or ``None`` when it was unreadable.

    A comparator, never a threshold. It is what separates rabbitmq's
    ``beam.smp`` (208) from the ``inet_gethost`` helper it forks (1), and
    blueapi's real server (19 warming up, 195 warm) from its zombie (1) — the
    two ends of that range are why no absolute number here would mean anything.
    """

    ptrace_readable: bool | None = None
    """Whether this seat may take ``PTRACE_MODE_READ`` on the process.

    ``None`` is "not measured", which must not be read as a denial. See
    :func:`podbench.proc.ptrace_readable` for what is measured and why a
    *denial* here is conclusive while a success is only encouraging.
    """

    @property
    def alive(self) -> bool:
        """Whether there is still a process here to attach to.

        An unknown state counts as alive: ``None`` means ``status`` could not be
        read, and refusing to debug a process because we could not read a file
        is a worse answer than trying and being told no.

        >>> ProcInfo(518, 1000, "python", "", state="Z").alive
        False
        >>> ProcInfo(1, 1000, "python", "python -m blueapi", state="S").alive
        True
        >>> ProcInfo(1, 1000, "python", "python -m blueapi").alive
        True
        """
        return self.state not in DEAD_STATES


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
    lsm: Lsm
    """Which LSM owns the labels below, read from sysfs rather than guessed at."""

    lsm_label: str | None
    """The seat's own label, verbatim. ``None`` is "no label", not "unconfined":
    the attribute is absent, unreadable or empty, and on a node with no
    label-based LSM reading it fails with ``EINVAL``."""

    self_uid: int
    target_uid: int | None
    target_pid: int | None
    self_gid: int | None = None
    """This container's real GID, ``None`` only from a seat too old to report it.

    Measured beside :attr:`self_uid` because the credential check needs both:
    ``__ptrace_may_access()`` compares the three group ids as well as the three
    user ids, so a seat that mirrors the uid alone is denied exactly as one that
    mirrors neither. ``None`` is "an older image did not say", which is not
    "gid 0" and must not be reported as a mismatch."""

    target_gid: int | None = None
    """The target's real GID, ``None`` when unreadable or unasked.

    Read from ``/proc/<pid>/status``, which is world-readable, so a seat landed
    at the wrong ids still learns the truth on arrival - and that is the only
    place the truth lives when the manifest sets ``runAsUser`` and no
    ``runAsGroup`` and the image's own user supplies the group (measured on
    p47-blueapi-0: manifest uid 1000, real gid 1000)."""

    lsm_label_target: str | None = None
    """The target's label, for the only comparison that has content.

    Held beside :attr:`lsm_label` rather than folded into a verdict, because a
    lone label is not evidence: on p47-blueapi-0 both sides read
    ``system_u:system_r:spc_t:s0`` and ptrace was permitted, so "confined"
    would have been a false accusation on a working seat (#104)."""

    node_name: str | None = None
    child_attach_ok: bool | None = None
    """Whether attaching to the probe's own forked child worked. Yama always
    permits descendants, so a failure here is structural rather than a policy
    decision about the target."""

    target_attach_ok: bool | None = None
    attach_method: str | None = None
    """Which ptrace primitive the live attach used, or ``None`` when there was
    no live attach - and from an image older than the seize, which is why the
    pause line reads "not measured" rather than "none" when it is missing.

    Reported because the two primitives cost the workload different things:
    :data:`SEIZE_PROBE` leaves it running, :data:`ATTACH_PROBE` stops it. See
    :func:`describe_pause`."""

    proc_reads: dict[str, bool] = field(default_factory=dict[str, bool])
    notes: list[str] = field(default_factory=list[str])

    @property
    def can_attach(self) -> bool:
        """Whether gdb can attach to the workload's own processes."""
        return self.verdict is Verdict.LIVE_ATTACH

    @property
    def measured(self) -> Verdict | None:
        """The verdict this report's own attempts support, or ``None``.

        What a listing states about a seat it probed, since a column has room
        for one claim and it had better be the measured one. See
        :func:`measured_verdict` for why it is not simply :attr:`verdict`.
        """
        return measured_verdict(
            self.target_attach_ok, self.proc_reads, self.child_attach_ok
        )

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
