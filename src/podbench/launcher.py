"""``podbench`` — the launcher that lands a seat and says what it got.

The launcher is the half of podbench that runs on the developer's machine. Its
whole job is to turn "I have a kubeconfig and a pod name" into "VS Code is
connected", and — when the cluster will not grant everything — to say *which
mechanism* refused rather than leaving the user with an ``EPERM``.

Three shapes here are forced by the phase-0 spikes rather than chosen:

* **The ladder has exactly two capability rungs plus a seat** (report 4.5).
  ``SYS_PTRACE`` on a non-root container is a silent no-op, so there is no
  middle ground to invent; :mod:`podbench.spec` refuses to author one.
* **Refusal arrives through two unrelated channels** (report 3.18). Pod
  Security Admission says no synchronously, in ``kubectl``'s stderr; the
  kubelet says no *seconds later*, in the container's status. Only the first
  can be caught by wrapping the API call, and only the second burns a container
  name, so each needs its own arm of the walk.
* **Ephemeral containers are permanent** (report 4.2). Attaching twice must
  therefore *reconnect*, and a rung that failed must take a fresh name from
  :func:`podbench.kubectl.next_container_name` rather than retry its own.

Everything the launcher prints is measured: the capability report comes from
``capreport`` running inside the container it just landed, on that node, not
from the spec it asked for.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from rich.text import Text

from . import __version__
from .agent import GROUP_PATH, PASSWD_PATH, PUBKEY_ENV, SEAT_NSS_PATH
from .budget import ProbeBudget, probe_budgets, probe_qualifier
from .cli import new_app, require_subcommand, run
from .console import WARNING_HANG, WARNING_LEAD, emit, paragraph
from .editor import (
    CONNECTION_HINT,
    OK,
    WARN,
    EditorError,
    Provision,
    is_step,
    open_seat,
    resolve_editor,
)
from .kubectl import (
    CREATE_CONTAINER_CONFIG_ERROR,
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    EphemeralContainerError,
    Kubectl,
    KubectlError,
    Runner,
    next_container_name,
    run_subprocess,
)
from .model import (
    DEFAULT_IMAGE,
    DEVPOD_LABEL,
    HOST_NETWORK_ENV,
    HOTFIX_APP_PATH,
    HOTFIX_CHILD_PID_PATH,
    HOTFIX_CLAIM_VOLUME,
    HOTFIX_HOLD_PATH,
    IMAGE_ENV,
    NOT_MEASURED,
    NOT_PROBED,
    OWNER_ENV,
    POD_CONTAINERS_ENV,
    SEAT_GROUP_KEY,
    SEAT_HOME_PATH,
    SEAT_HOME_VOLUME,
    SEAT_IDENTITY_VOLUME,
    SEAT_PASSWD_KEY,
    TARGET_NAME_ENV,
    Blocker,
    CapabilityReport,
    ContainerRef,
    Lsm,
    PodRef,
    Rung,
    SeatKind,
    SeatReport,
    Verdict,
    and_list,
    as_dict,
    describe_credentials,
    describe_pause,
    measured_rung,
)
from .proc import Credentials
from .resize import (
    CPU,
    EDITOR_HEADROOM,
    MEMORY,
    QOS_REFUSAL,
    SEAT_FOOTPRINT,
    SEAT_HEADROOM,
    Headroom,
    ResizeError,
    ResizePlan,
    Want,
    editor_limit,
    explain_claim_refusal,
    format_memory,
    namespace_limits,
    parse_quantity,
    parse_top_memory,
    parse_want,
    plan_resize,
    pod_memory_limit,
    qos_preserving_flags,
)
from .spec import (
    AGENT_COMMAND,
    DEFAULT_PULL_POLICY,
    PULL_POLICIES,
    InvalidSpecError,
    admission_wedges_the_kubelet,
    capabilities_removed,
    container_id,
    ephemeral_container,
    ephemeral_container_spec,
    moves,
    reported_target_uid,
    requested_seat_spec,
    runs_as_non_root,
    shares_pid_namespace_across_uids,
    target_seccomp_profile,
    target_uid_gid,
)
from .sshcfg import (
    ROOT_HOME,
    SEAT_USER,
    HostKeyBinding,
    HostKeyPolicy,
    KubectlInvocation,
    SshdLayout,
    client_config,
    ensure_control_dir,
    host_key_alias,
    known_hosts_entry,
)

__all__ = [
    "CAPABILITY_STRIPPED_WARNING",
    "CAPREPORT_ARGV",
    "CONFIG_D",
    "CONTAINER_BASE",
    "DEFAULT_IMAGE",
    "EDITOR_ORPHAN_HOME_WARNING",
    "EDITOR_PROBE_REMINDER",
    "EDITOR_RESIZE_NOTE",
    "EDITOR_STORAGE_WARNING",
    "EDITOR_UNMEASURED_WARNING",
    "HOST_KEY_ARGV",
    "ID_CORRECTION_WARNING",
    "LADDER",
    "LOGIN_USER_ARGV",
    "NON_ROOT_HOME",
    "NO_TARGET_CONTAINER",
    "EDITOR_HEADROOM_WARNING",
    "MEMORY_HEADROOM_WARNING",
    "SEAT_IDENTITY_MOUNTS",
    "SEAT_STATUS_ARGV",
    "UNKNOWN_SEAT_VERSION",
    "OTHER_OWNER_WARNING",
    "RECONNECT_KEY_ABSENT_WARNING",
    "RECONNECT_KEY_UNMEASURED_WARNING",
    "UNKNOWN_OWNER",
    "VERSION_ARGV",
    "VERSION_SKEW_WARNING",
    "Feature",
    "LadderStep",
    "LauncherError",
    "PodChoice",
    "SeatIdentity",
    "SeatInfo",
    "Session",
    "SshSeat",
    "above_ceiling",
    "application_mount",
    "attach",
    "capability_report_from_json",
    "choose_pod",
    "client_dir",
    "container_names",
    "current_namespace",
    "declared_volumes",
    "default_host_alias",
    "seat_label",
    "seat_suffix",
    "emit_ssh_config",
    "features",
    "forget_known_hosts",
    "forget_ssh_config",
    "format_age",
    "format_pod_choices",
    "format_seats",
    "format_session",
    "headroom_note",
    "measure_headroom",
    "host_alias_in",
    "id_correction",
    "identity_paths",
    "kubectl_for",
    "list_seats",
    "main",
    "match_pod_choices",
    "match_pod_names",
    "other_containers",
    "other_owners_seat",
    "parse_mount",
    "plan_ladder",
    "pod_choices",
    "probe_seat_credentials",
    "probe_seat_version",
    "probe_seat_versions",
    "probe_seats",
    "probe_ssh_identity",
    "read_public_key",
    "reconnect_key_note",
    "resolve_among",
    "resolve_mounts",
    "resolve_pod",
    "resolve_pod_name",
    "run_capreport",
    "same_build",
    "superseded_seats",
    "ssh_unavailable_note",
    "running_seat",
    "seat_authorises",
    "seat_identity_mounts",
    "seat_layout",
    "seat_version_fact",
    "seats",
    "all_seats",
    "dev_seat",
    "seat_kind",
    "is_dev_pod",
    "is_hotfixed",
    "runs_hotfix_supervisor",
    "shares_workload_volume",
    "DEV_SIDECAR_PROVISION_NOTE",
    "DEV_SIDECAR_REUSED_NOTE",
    "OTHER_MODES_NOTE",
    "UNMOUNTED_HOTFIX_NOTE",
    "HOTFIX_CLAIM_MOUNTED_NOTE",
    "HOTFIX_CLAIM_UNMOUNTABLE_NOTE",
    "hotfix_claim_mounts",
    "wait_for_seats",
    "spec_env",
    "ssh_config_path",
    "ssh_connect_line",
    "ssh_include_line",
    "ssh_stanza",
    "target_container_name",
    "target_row",
    "try_resize",
    "write_known_hosts",
    "write_ssh_config",
]

CONTAINER_BASE = "podbench"
"""Base name for the ephemeral container; suffixed ``-1``, ``-2``, … (report 4.2)."""

CAPREPORT_ARGV: tuple[str, ...] = ("podbench", "capreport", "--json")
"""The probe, spelled as the verb rather than as a per-subcommand alias.

``podbench`` unqualified is safe here and needs no help: ``kubectl exec``
inherits the image's own ``ENV PATH``, which has the venv on it. An ssh session
inherits none of it and gets whatever the agent's ``SetEnv`` line carried, or
sshd's compiled-in default — which is why ``/usr/local/bin/podbench`` names the
venv in full and reaches the same program either way."""

HOST_KEY_ARGV: tuple[str, ...] = (
    *AGENT_COMMAND,
    "--print-host-key",
    "--no-self-check",
)
"""``--no-self-check`` because this runs against a container that is already up:
the startup checks cost a subprocess each and have nothing left to prove."""

LOGIN_USER_ARGV: tuple[str, ...] = (*AGENT_COMMAND, "--print-login-user")
"""Asks the seat which login name sshd will resolve for the uid it runs as.

Measured in the container rather than predicted from the spec, like everything
else the launcher prints: whether an account exists for the target's uid is a
fact about the *image*, and podbench can be pointed at any image."""

VERSION_ARGV: tuple[str, ...] = ("podbench", "--version")
"""Asks the seat which build of podbench it is, rather than inferring it.

The two halves share a protocol - the agent's argv and ``capreport``'s JSON -
so a skew is a hazard and not untidiness. It is also unreadable from outside:
an image tag can move under a name that has not, ``IfNotPresent`` will not
re-check, and a fix pushed to a branch tag is served from the node's cache with
no event anywhere. Spelled with a bare ``podbench`` for the reason
:data:`CAPREPORT_ARGV` is."""

SEAT_STATUS_ARGV: tuple[str, ...] = ("cat", "/proc/self/status")
"""Asks the seat what the kernel gave it: its uid, its gid and ``CapEff``.

``cat`` rather than a podbench verb, alone among the seat probes. Three
reasons, all of them about who can be asked: it works on a seat landed by an
*older* launcher, since it adds nothing to the two halves' protocol; it needs
no image change to arrive; and it is a world-readable read of the process
running it, so it costs a round trip and touches nothing. That is what lets the
rung be measured on every attach, ``--no-probe`` included — the expensive
question, whether this seat can ptrace *that* target, is
:data:`CAPREPORT_ARGV`'s and stays behind the flag.

An exec runs with the container's own uid and capability set, so the ``cat``'s
own status is the seat's."""

NON_ROOT_HOME = "/tmp/podbench-home"
"""``$HOME`` for a non-root seat.

An ephemeral container gets the image's filesystem, and this image has no user
account for the target's uid — so ``/root`` is unwritable and ``Path.home()``
inside the container would resolve to something neither side agrees on. Pinning
``HOME`` to a path under ``/tmp`` makes the agent's ``SshdLayout.for_uid``
produce exactly the paths the launcher puts in the ProxyCommand."""

DEFAULT_CLIENT_DIR = "~/.podbench"
CLIENT_DIR_ENV = "PODBENCH_CONFIG_DIR"
DEFAULT_IDENTITY = "~/.ssh/id_ed25519"

CONFIG_D = "config.d"
"""Subdirectory of the client dir holding one generated stanza per pod.

Its name is in the ``Include`` line the user adds to ``~/.ssh/config`` as well
as in every path podbench writes, so the two are derived from this rather than
spelled twice — see :func:`ssh_include_line`."""

DEFAULT_SEAT_USER = SEAT_USER
"""ssh login name for a non-root seat. sshd resolves it through NSS, so the seat
must have an account for the uid it runs as - which for the degraded rung is the
target's uid, so the agent registers one at start-up when it can (see
:func:`podbench.agent.ensure_passwd_entry`). ``--ssh-user`` overrides it when
the image names that account something else."""

MEMORY_HEADROOM_WARNING = (
    "only {free} of memory headroom here, and an ephemeral container may not "
    "reserve its own: a seat measured {footprint}. `--resize MEMORY` raises "
    "the target's limit in place, `podbench dev` gives the seat its own."
)
"""Printed only where *this* pod's measured headroom is genuinely thin.

It replaces a warning printed on every attach, and the measurement is why: ten
live seats on DLS p47 (2026-08-19) cost 13-23 MiB against 170-3858 MiB of pod
headroom, three seats to a pod and no OOM anywhere. Warning about that is a
fear the data does not support, and it was half of what this report printed.

Headroom rather than the container's limit, because the limit answers the wrong
question - the beamline's smallest limits (three 100Mi containers) are in the
pod with the most room per byte used. And measured rather than assumed in both
directions: where the headroom could not be read the report says so on the
`memory` row and this line stays silent, since a caution fired on every cluster
without a metrics-server is the thing being removed.

Every warning the report prints is one line by rule. The mechanism behind each
- which is what the paragraphs these replaced were spending their length on -
is in ``docs/how-to/attach-to-a-pod.md``, said once, where somebody reading
about the mode will meet it; a terminal is where you find out *that* it applies
to the pod in front of you.

The numerator and denominator behind ``free`` are not repeated here: the report
prints ``used of limit`` on its own ``memory`` row a few lines up, and a warning
that recites the row above it is the same measurement twice. The footprint's
provenance is ten live seats on DLS p47, 2026-08-19.
"""

EDITOR_HEADROOM_WARNING = (
    "vscode-server measured {needed} live and this pod has {free}: the "
    "overflow OOM-kills something in the pod cgroup, and an ephemeral "
    "container does not come back. `--resize MEMORY`, or `podbench dev`."
)
"""The memory cost that survived the measurement, on the verb that spends it.

A bare seat is 13-23 MiB and no longer earns a word; an editor is three orders
of magnitude more (1215 MiB with one extension, 2026-08-16, report R2) and does
not fit in most of the pods measured. So this is keyed on ``podbench vscode``,
which is podbench asking for the editor itself, and on the same reading of this
pod that the seat's own line uses. Somebody who connects VS Code by hand
afterwards is told by ``docs/how-to/vscode-remote-ssh.md`` instead - a warning
cannot be printed for a decision that has not been made yet.

Since the verb sizes the pod itself, reaching this line means the raise did not
take: a container holding a ``resources.claims`` entry refuses every resize
(#107), and `--no-resize` declines one. Both leave the hazard exactly as it was,
which is why the warning still names the flag.

It states the two numbers that decide, and no more. ``used of limit`` is on the
report's ``memory`` row, and where a raise was attempted
:data:`EDITOR_RESIZE_NOTE` and :func:`try_resize` have already said what was
asked for and what came of it - three lines for one resize was #203's
"do not announce the same event twice".
"""

EDITOR_RESIZE_NOTE = (
    "sized for the editor: {container}'s memory limit was raised to {limit}, "
    "for the {needed} vscode-server measured. `--no-resize` declines it; "
    "`--resize MEMORY` chooses the number."
)
"""Why ``vscode`` raised a limit nobody typed, printed before it tries.

Before, so that a refusal - which :func:`try_resize` reports on the next line -
is read against the intent rather than as an act with no motive. The two are one
mutation and they are two lines because the second is the one that can fail.

It states the intent and the new limit, not the shortfall that produced them:
this line, :func:`try_resize`'s outcome and :data:`EDITOR_HEADROOM_WARNING` are
three reports of one resize, so each says only the part the others cannot.

The number is arithmetic, not a default: :func:`podbench.resize.editor_limit`
raises the target by the shortfall against
:data:`podbench.resize.EDITOR_HEADROOM` and rounds up to the next whole GiB.
Naming the container is what makes it checkable - the raise goes on the
*target's* limit, because an ephemeral container may not declare ``resources``
at all and the pod's ceiling is the sum of its containers'.
"""

EDITOR_STORAGE_WARNING = (
    "this pod declares no {volume!r} volume, so vscode-server's 1.1-1.3 GB "
    "unpacks onto the workload's ephemeral-storage budget and an overrun "
    "evicts the pod, application included. Declaring the volume is a chart "
    "change: `spec.volumes` cannot be added to a running pod."
)
"""The editor cost that no flag on this verb can pay.

Memory can be raised in place, and this cannot: ``spec.volumes`` is immutable,
so the mitigation has to have been deployed. It is worth a line anyway, because
the failure mode is the worst one podbench can cause - eviction takes the
workload, not just the seat - and nothing else in the run mentions it.

Not keyed on the target's declared ``ephemeral-storage`` limit, deliberately. A
pod with no limit is not safe here, it is unbounded against the *node's* disk,
and the eviction then arrives from the kubelet's imagefs pressure instead.

Where the 1.1-1.3 GB physically goes - the seat's own writable layer, which is
on the workload's budget because a seat may not reserve one of its own - is in
``docs/how-to/vscode-remote-ssh.md``. The line says which pod it applies to.
"""

EDITOR_ORPHAN_HOME_WARNING = (
    "this pod declares {volume!r} and a root seat cannot use it - sshd gives "
    "uid 0 the image's own {home!r} instead. So vscode-server's 1.1-1.3 GB "
    "lands on the workload's ephemeral-storage budget after all, and an "
    "overrun evicts the pod (#42). `--max-rung degraded` lands a seat that "
    "uses the volume, at the cost of the live attach."
)
"""The volume is there, was deployed on purpose, and this rung ignores it.

Worth saying rather than leaving silent precisely because the pod *looks*
prepared: somebody added the volume for this, :func:`seat_identity_mounts`
mounted it, and every layer is individually correct. What fails is the join -
the launcher pins ``HOME`` in the container's environment and sshd obeys the
passwd record instead, which is the rule :func:`podbench.agent.session_home`
exists to state.

It names ``--max-rung degraded`` rather than the volume, because the volume is
already correct. That is a real trade and not a fix: the degraded rung has no
``SYS_PTRACE``, so it buys the storage with the live attach. #42 is where the
right answer - a passwd record for root that names the mount - is tracked.

That sshd takes ``$HOME`` from the passwd record rather than from the
environment, and that the degraded rung gets a written record, are both in
``docs/how-to/vscode-remote-ssh.md`` under the same #42 caveat. The line keeps
the consequence and the flag.
"""

EDITOR_UNMEASURED_WARNING = (
    "this pod's memory is unmeasured (no metrics API here), so nothing sized "
    "it for vscode-server's {needed}: overflowing the pod cgroup OOM-kills a "
    "seat that cannot be restarted. `--resize MEMORY` sets the limit yourself."
)
"""The one unmeasured case that earns a line, because this verb promised a size.

Everywhere else in podbench an unreadable headroom is stated on the report's
``memory`` row and left there: the metrics API is an add-on, its absence is
ordinary, and a caution keyed on it would fire on every cluster without a
metrics-server - which is the always-on memory warning the p47 measurement
retired.

``vscode`` is the exception because it undertook to do something. Silently not
doing it leaves the user believing the pod was sized, which is worse than the
warning is noisy, and the remedy is one flag away.
"""

EDITOR_PROBE_REMINDER = (
    "before the first breakpoint: this pod is probed, and the deadline under "
    "`supports` above is what a pause spends - debuginfod fetches included. "
    "`podbench dbg --no-debuginfod` in the seat spends none."
)
"""The last thing ``podbench vscode`` says, and the only thing it repeats.

The numbers are :func:`podbench.budget.probe_qualifier`'s and stay there — a
second copy is a second thing to keep true. What this adds is the timing: the
report is several blocks up by the time the window opens, and the reader's
attention is about to leave the terminal for a GUI.

"debuginfod fetches included" is the whole of what used to be a clause about
gdb resolving a library's debuginfo *after* the stop, which is the mechanism and
is in ``docs/how-to/attach-to-a-pod.md``. What the reader needs on the terminal
is that those fetches are inside the deadline, not outside it.

It names debuginfod because that is the one item of the spend the reader can
still decide, and because it is not obviously part of a "pause" at all: the
seat bounds each fetch and withdraws the server when it cannot be reached
(:func:`podbench.agent.check_debuginfod`), but a reachable, slow one is charged
to this budget. The mechanism is in ``docs/how-to/attach-to-a-pod.md``.
"""

VERSION_SKEW_WARNING = (
    "this seat and this launcher are different builds of podbench (the "
    "`version` row above carries both), and the two halves share a protocol, "
    "so a fix in one is missing from the other. `--pull always --new` re-lands "
    "from the tag as it stands now, at the cost of a container name; `--image` "
    "pins a version."
)
"""Printed when the seat answered ``--version`` with something other than ours.

Only ever *beside* the measurement: the numbers are on the ``version`` row and
stay there, so this line is the consequence and the way out and nothing else.
It replaces :func:`moving_tag_note` where a reading exists - a guess from the
tag string next to a measured mismatch is two answers to one question, and the
guess is the one that can be wrong in both directions.

Which protocol the two halves share - the agent's argv and ``capreport``'s JSON
- is :data:`VERSION_ARGV`'s to state, and it is the reason this is a hazard
rather than untidiness. The line says there is one and what to do about it.
"""

UNKNOWN_SEAT_VERSION = "unknown - this seat did not answer `podbench --version`"
"""Stands where a seat's build should be when it would not say.

Distinct from :data:`podbench.model.NOT_PROBED`, which is a seat that was never
asked: an image old enough to have no root ``--version``, or an ``exec`` the
namespace refuses, is a fact about this seat and not an omission of ours.
"""

_UNPINNED_UID = 65534
"""Stands in for "not root, but the image chooses which" when picking an sshd
layout. It is never written into a spec — only :func:`seat_layout` sees it."""

_KUBELET_NON_ROOT_MESSAGE = "runAsUser breaks non-root policy"
"""Substring of the kubelet's asynchronous refusal of a root container under
``runAsNonRoot: true``. Matched only to explain a failure we already pre-empt by
reading the pod's securityContext (report 3.18)."""


class LauncherError(RuntimeError):
    """Something the user has to fix before podbench can land a seat."""


# -- pod / container introspection -----------------------------------------


def resolve_pod_name(reference: str) -> str:
    """Accept ``pod/foo`` as well as bare ``foo``, as kubectl itself does.

    >>> resolve_pod_name("pod/api-7f9")
    'api-7f9'
    >>> resolve_pod_name("api-7f9")
    'api-7f9'
    """
    kind, separator, name = reference.partition("/")
    if not separator:
        return reference
    if kind not in ("pod", "pods", "po"):
        raise LauncherError(f"podbench works on pods, not {kind!r}")
    if not name:
        raise LauncherError(f"no pod name in {reference!r}")
    return name


def container_names(pod_json: Mapping[str, Any]) -> list[str]:
    """The pod's workload containers, in spec order.

    Spec order because the first of them is the default target: that is
    ``kubectl exec``'s rule and so :func:`target_container_name`'s.

    >>> container_names({"spec": {"containers": [{"name": "ca"}, {"name": "pva"}]}})
    ['ca', 'pva']
    """
    return [
        name
        for name in (
            _entry_name(entry)
            for entry in _as_list(as_dict(pod_json.get("spec")).get("containers"))
        )
        if name is not None
    ]


def target_container_name(
    pod_json: Mapping[str, Any], requested: str | None = None
) -> str:
    """The workload container podbench points at.

    With no ``--target`` the first container wins, matching ``kubectl exec``,
    so the common single-container pod needs no flag. Which container that was
    is then reported rather than left implied — see :func:`other_containers`.

    >>> target_container_name({"spec": {"containers": [{"name": "ca"}]}})
    'ca'
    """
    names = container_names(pod_json)
    if not names:
        raise LauncherError("pod has no containers")
    if requested is None:
        return names[0]
    if requested not in names:
        raise LauncherError(f"container {requested!r} not in pod (has {names})")
    return requested


def other_containers(pod_json: Mapping[str, Any], workload: str) -> tuple[str, ...]:
    """The pod's containers ``workload`` is not, in spec order.

    Defaulting to the first container is right; saying nothing about it is not.
    Three of the fifteen pods on the p47 beamline carry two or three containers,
    and on each of them a silent default leaves the report describing a pod that
    does not exist — one third of ``p47-proxy``'s processes read as all of them
    (finding 15).

    >>> pod = {"spec": {"containers": [{"name": "ca"}, {"name": "pva"}]}}
    >>> other_containers(pod, "ca")
    ('pva',)
    """
    return tuple(name for name in container_names(pod_json) if name != workload)


def parse_mount(text: str) -> tuple[str, str | None]:
    """Split one ``--mount CLAIM:MOUNTPATH`` into its two halves.

    The mountPath is optional because it is usually not the user's to choose:
    where the application container already mounts that volume, podbench copies
    *its* mountPath rather than making the user repeat it (see
    :func:`resolve_mounts`). A volume name is an RFC 1123 label and a mountPath
    is absolute, so the first colon is the separator and nothing else can be.

    >>> parse_mount("myapp-venv:/opt/venv")
    ('myapp-venv', '/opt/venv')
    >>> parse_mount("myapp-venv")
    ('myapp-venv', None)
    """
    name, separator, path = text.partition(":")
    if not name:
        raise LauncherError(f"--mount {text!r} names no claim or volume")
    if not separator:
        return name, None
    if not path.startswith("/"):
        raise LauncherError(
            f"--mount {text!r} needs an absolute mountPath, as in {name}:/opt/venv"
        )
    return name, path


def resolve_mounts(
    pod_json: Mapping[str, Any],
    workload: str,
    requests: Sequence[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Author the seat's ``volumeMounts`` from ``--mount``, or refuse and say why.

    An ephemeral container may mount the volumes its pod **already declares**
    and may not introduce one: ``spec.volumes`` is immutable after the pod is
    created, so nothing an attach does can add a claim to a running pod. That is
    the whole reason Hotfix mode asks for deploy-time cooperation, and it is why
    an unknown name is refused here rather than turned into a mount the API
    server would reject with a message about a volume podbench invented.

    The mountPath is not a free choice either. Hotfix mode's premise is that the
    claim resolves at the *same* path in the seat as in the application — the
    venv's ``bin/python`` and the checkout's editable install are absolute paths
    recorded on the volume — so the application container's own mountPath is the
    default, and an explicit ``--mount`` that disagrees with it is honoured but
    warned about.

    An application mount that uses ``subPath`` is refused for the same reason
    from the other direction: the seat cannot copy it (an ephemeral container's
    volumeMounts may not carry one) and must not silently drop it (the seat
    would then resolve a different tree at the same path).

    A claim name is accepted as well as a volume name because that is what the
    user has in hand (``hotfix values`` names the claim), while a mount
    refers to the pod's volume *entry*.
    """
    volumes = [
        as_dict(entry)
        for entry in _as_list(as_dict(pod_json.get("spec")).get("volumes"))
    ]
    mounts: list[dict[str, Any]] = []
    warnings: list[str] = []
    for request in requests:
        name, requested_path = parse_mount(request)
        volume = _find_volume(volumes, name)
        if volume is None:
            raise LauncherError(_no_such_volume(name, volumes))
        volume_name = _entry_name(volume) or name

        application = application_mount(pod_json, workload, volume_name)
        app_path = _as_str(application.get("mountPath"))
        if requested_path is None and app_path is None:
            raise LauncherError(
                f"container {workload!r} does not mount volume {volume_name!r}, "
                "so there is no mountPath to copy: give one explicitly as "
                f"--mount {name}:/path. In Hotfix mode it must be the path the "
                "application's venv lives at, because that is what the manifest "
                "on the claim records."
            )
        if (
            requested_path is not None
            and app_path is not None
            and requested_path.rstrip("/") != app_path.rstrip("/")
        ):
            warnings.append(
                f"--mount asks for {volume_name!r} at {requested_path}, but "
                f"container {workload!r} mounts it at {app_path}. Hotfix mode "
                "needs the two identical: the venv's bin/python and the "
                "checkout's editable install are absolute paths on the volume, "
                "so a seat that mounts the claim elsewhere resolves a different "
                "venv and a different checkout."
            )
        mount: dict[str, Any] = {
            "name": volume_name,
            "mountPath": requested_path if requested_path is not None else app_path,
        }
        # A subPath the application uses is part of "the same path": without it
        # the seat would see the volume root where the application sees one
        # directory inside it, and every path in the manifest would be wrong.
        # Copying it is not open to podbench either — an ephemeral container may
        # not carry one — so this is refused rather than quietly resolved to a
        # different tree.
        sub_path = _as_str(application.get("subPath"))
        if sub_path is not None:
            raise LauncherError(
                f"container {workload!r} mounts volume {volume_name!r} at "
                f"{app_path} with subPath {sub_path!r}, and a seat cannot "
                "reproduce that: an ephemeral container's volumeMounts may not "
                "set subPath, which the API server refuses outright "
                "(`Forbidden: cannot be set for an Ephemeral Container`). "
                "Mounting the volume whole at the same path instead would give "
                "the seat a different tree from the application's, and Patch "
                "mode is only true while the two resolve identically. Redeploy "
                "the workload with the claim mounted whole over that path, or "
                "debug it in a dev pod (`podbench dev`), whose seat is an "
                "ordinary container and may subPath."
            )
        mounts.append(mount)
    return mounts, warnings


SEAT_IDENTITY_MOUNTS: tuple[tuple[str, str], ...] = (
    (PASSWD_PATH, SEAT_PASSWD_KEY),
    (GROUP_PATH, SEAT_GROUP_KEY),
)
"""Where each key of :data:`podbench.model.SEAT_IDENTITY_VOLUME` has to land.

One mount per file, each with its own ``subPath``, and never the volume at
``/etc``: a directory mount *replaces* the directory, so the seat would lose
``nsswitch.conf``, ``hosts``, the CA bundle and everything else the image put
there — and NSS would then have no ``files`` line telling it to read the passwd
file we just supplied.

Which is why this layout is only ever authored onto an **ordinary** container.
``subPath`` is forbidden on an ephemeral container's volumeMounts
(:func:`podbench.spec.validate_ephemeral_volume_mounts`), and the two ways of
putting a passwd *file* into a seat are the two described above, so a live-pod
attach cannot use this volume at all. It keeps its shape here because it is the
contract the chart's ConfigMap keys are checked against.
"""


def seat_identity_mounts(
    pod_json: Mapping[str, Any], workload: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """The seat's home mount, when the pod declares the volume for it.

    A convention rather than a flag. The volume can only be there because
    someone deployed the workload with it — an ephemeral container may mount
    only volumes the pod already has, and ``spec.volumes`` is immutable — so its
    presence *is* the request, and asking the user to also remember
    ``--mount podbench-home:/home/podbench`` on every attach would make the
    chart's cooperation useless twice over.

    :data:`podbench.model.SEAT_IDENTITY_VOLUME` is deliberately **not** mounted,
    even where the pod declares it. The identity has to land as two files, which
    takes a ``subPath`` per mount, and the API server refuses ``subPath`` on an
    ephemeral container outright — it fails the whole ``replace --raw``, so
    authoring it here does not degrade the attach, it destroys it. The home
    volume is kept because it is subject to no such rule and is worth having on
    its own: it moves everything the seat writes off the workload's
    ephemeral-storage budget. A live-pod seat gets its NSS identity by
    registering its own record in :data:`podbench.agent.SEAT_NSS_PATH`, which
    asks nothing of the pod at all — the file is in the image, and world-writable
    there precisely so that a seat running as the target's uid *and gid* can
    append to it (#102). :func:`features` says so where the user is looking.

    Absence is not an error: a bare ``attach`` against a pod that knows nothing
    about podbench must still land a seat and degrade honestly, which is the
    whole reason :func:`resolve_mounts` refuses an undeclared volume while this
    quietly authors nothing.
    """
    declared = declared_volumes(pod_json)
    requests: list[str] = []
    if SEAT_HOME_VOLUME in declared:
        requests.append(f"{SEAT_HOME_VOLUME}:{SEAT_HOME_PATH}")

    # Through resolve_mounts rather than beside it: the volume lookup, the
    # refusal text and the application's own subPath are one behaviour, and a
    # second path through them is a second thing to keep true.
    return resolve_mounts(pod_json, workload, requests)


HOTFIX_CLAIM_MOUNTED_NOTE = (
    "this pod carries the hotfix layout, so the claim {volume!r} was mounted "
    "into the seat at {path} without being asked for: hotfix mode needs it at "
    "the same path in both"
)
"""Said whenever :func:`hotfix_claim_mounts` authors a mount nobody typed.

A mount the user did not ask for is only acceptable if the output names it, and
this is the line that does. It is a note and not a warning: nothing went wrong,
the seat simply has one more mount than the command line accounts for.

Naming it is what is licensed, not arguing for it. That an ephemeral container's
``volumeMounts`` are fixed once it is created - so the mount has to be authored
now or not at all - is in ``docs/how-to/attach-to-a-pod.md``, and it is podbench
justifying itself rather than anything the reader has to act on."""

EDITOR_FOLDER_CLAIM_NOTE = (
    "opening {folder}, not the seat's home {home}: on a hotfixed pod the claim "
    "is the only tree here where an edit reaches the running process"
)
"""Said when ``vscode`` opens the claim rather than the home.

A folder the user did not name is only acceptable if the output names it, which
is :data:`HOTFIX_CLAIM_MOUNTED_NOTE`'s rule applied to the other unasked-for
answer in the same command."""

EDITOR_FOLDER_HOME_NOTE = (
    "opening the seat's home {home}, not the claim: this pod carries the "
    "hotfix layout but the claim is not mounted into this seat, so there is no "
    "{path} here. An editor here reads the image's code, not the code running "
    "- the block above says why"
)
"""Said when the pod is hotfixed and the seat did not get the claim anyway.

Which is not a corner case: :func:`hotfix_claim_mounts` degrades a ``subPath``
refusal to a note rather than a dead attach, a reconnect into a seat older than
the layout has no claim mount, and ``--no-seat-identity`` against a target that
does not mount it lands the same way. ``session.hotfixed`` is true in all three
while there is nothing on the claim's path to open, and opening it anyway would
put an editor on an empty directory in the seat's own rootfs."""

HOTFIX_CLAIM_UNMOUNTABLE_NOTE = (
    "this pod carries the hotfix layout, but the claim could not be mounted "
    "into the seat: {reason}. An editor here reads the image's code, not the "
    "code running"
)
"""Said when the convention mount is refused rather than authored.

:func:`resolve_mounts` refuses an application mount carrying a ``subPath``,
because an ephemeral container may not have one and dropping it silently would
resolve a different tree at the same path. That refusal is right for a mount
somebody typed and wrong for one podbench added on its own initiative, so the
convention degrades to a note - the same way :func:`seat_identity_mounts`
authors nothing on a pod that declares no home volume."""


def hotfix_claim_mounts(
    pod_json: Mapping[str, Any], workload: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """The claim's mount, when the pod was deployed to carry a hotfix.

    A convention for the same reason the home volume is one
    (:func:`seat_identity_mounts`): the layout can only be on the pod because
    somebody deployed it there, so its presence *is* the request. Issue #177 is
    what this closes from the other end - ``attach`` mounted
    :data:`~podbench.model.SEAT_HOME_VOLUME` and nothing else, so ``hotfix init``
    met a seat with no ``/podbench/app`` and blamed the values that had in fact
    been deployed correctly.

    The path is the **application's own**, copied by :func:`resolve_mounts`,
    falling back to :data:`~podbench.model.HOTFIX_APP_PATH` only where the
    target container does not mount the claim at all. Hotfix mode's premise is
    that the two resolve identically - the venv's ``bin/python`` and the
    checkout's editable install are absolute paths recorded on the claim - so
    copying beats asserting, and the constant is the answer for a seat pointed
    at a sibling container that was never given the layout.

    Absence is not an error and neither is refusal: a bare ``attach`` on a pod
    that knows nothing about hotfix mode authors nothing, and one whose
    application mounts the claim with a ``subPath`` gets a note instead of a
    dead attach.
    """
    if not is_hotfixed(pod_json):
        return [], []
    application = application_mount(pod_json, workload, HOTFIX_CLAIM_VOLUME)
    path = _as_str(application.get("mountPath"))
    request = (
        HOTFIX_CLAIM_VOLUME
        if path is not None
        else f"{HOTFIX_CLAIM_VOLUME}:{HOTFIX_APP_PATH}"
    )
    try:
        mounts, warnings = resolve_mounts(pod_json, workload, [request])
    except LauncherError as exc:
        return [], [HOTFIX_CLAIM_UNMOUNTABLE_NOTE.format(reason=exc)]
    for mount in mounts:
        warnings.append(
            HOTFIX_CLAIM_MOUNTED_NOTE.format(
                volume=HOTFIX_CLAIM_VOLUME, path=_mount_path(mount)
            )
        )
    return mounts, warnings


def declared_volumes(pod_json: Mapping[str, Any]) -> set[str]:
    """The names of the volumes the pod carries in its spec."""
    return {
        name
        for entry in _as_list(as_dict(pod_json.get("spec")).get("volumes"))
        if (name := _entry_name(entry)) is not None
    }


def _merge_mounts(
    explicit: Sequence[Mapping[str, Any]], convention: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """``explicit`` wins over ``convention`` for a mountPath they share.

    Two mounts of one path is not a tie the kubelet breaks sensibly, and the
    ``--mount`` is the one somebody typed on purpose.
    """
    taken = {_mount_path(mount) for mount in explicit}
    return [
        *(dict(mount) for mount in explicit),
        *(dict(mount) for mount in convention if _mount_path(mount) not in taken),
    ]


def _mount_path(mount: Mapping[str, Any]) -> str:
    return (_as_str(mount.get("mountPath")) or "").rstrip("/")


def _mounts_volume(container: Mapping[str, Any], volume: str) -> bool:
    """Whether a container spec carries a mount of ``volume``."""
    return any(
        _entry_name(entry) == volume
        for entry in _as_list(container.get("volumeMounts"))
    )


def _find_volume(
    volumes: Sequence[Mapping[str, Any]], name: str
) -> Mapping[str, Any] | None:
    """The pod's volume entry called ``name``, or the one backed by that claim."""
    for volume in volumes:
        if _entry_name(volume) == name:
            return volume
    for volume in volumes:
        claim = as_dict(volume.get("persistentVolumeClaim")).get("claimName")
        if _as_str(claim) == name:
            return volume
    return None


def _no_such_volume(name: str, volumes: Sequence[Mapping[str, Any]]) -> str:
    declared = [
        _entry_name(volume) for volume in volumes if _entry_name(volume) is not None
    ]
    return (
        f"the pod declares no volume named {name!r} and no volume backed by a "
        f"claim called {name!r}, and an ephemeral container may only mount "
        "volumes the pod already has: spec.volumes is immutable once the pod "
        "exists, so podbench cannot add one now. This is exactly why Hotfix mode "
        "needs the chart's cooperation at deploy time - redeploy the workload "
        f"with a volume bound to claim {name!r}, mounted beside the "
        "application's own project and never over it "
        "(`podbench hotfix values` emits the volume, the volumeMount, "
        "the supervisor entrypoint and the fsGroup). The pod currently "
        "declares: " + (", ".join(str(entry) for entry in declared) or "no volumes")
    )


def application_mount(
    pod_json: Mapping[str, Any], workload: str, volume_name: str
) -> Mapping[str, Any]:
    """The workload container's own mount of ``volume_name``, or ``{}``.

    Public because ``hotfix`` asks the same question of the same volume - where
    does this container mount the claim - and two copies of that loop is how
    the seat-vs-application mount note and the ``--venv`` default come to
    disagree about the answer.
    """
    for entry in _as_list(as_dict(pod_json.get("spec")).get("containers")):
        container = as_dict(entry)
        if _entry_name(container) != workload:
            continue
        for candidate in _as_list(container.get("volumeMounts")):
            mount = as_dict(candidate)
            if _entry_name(mount) == volume_name:
                return mount
    return {}


@dataclass(frozen=True)
class SeatInfo:
    """One podbench ephemeral container as the cluster currently reports it."""

    name: str
    rung: Rung
    phase: str
    """``running``, ``waiting``, ``terminated`` or ``pending`` — the last meaning
    the spec is in the pod but the node has not reported on it yet."""

    detail: str
    uid: int | None = None
    """The uid pinned in its securityContext, ``None`` for an unpinned seat.
    Read back from the spec rather than remembered, because it decides which
    sshd layout the ProxyCommand must name."""

    gid: int | None = None
    """The gid pinned in its securityContext, ``None`` for an unpinned seat.

    Read back for a reason the uid does not have: an ephemeral container's
    securityContext is fixed for its lifetime, so a seat landed in the wrong
    group can never be corrected in place, and :func:`running_seat` uses this to
    decline reusing one - which is what keeps the id correction to a single
    extra container name per pod rather than one per attach."""

    image: str | None = None
    target: str | None = None
    home: str | None = None
    """``$HOME`` as pinned in its env, ``None`` when the image's own wins. Read
    back for the same reason ``uid`` is: the ProxyCommand names sshd's config
    file by absolute path, and that path is derived from this."""

    identity_mounted: bool = False
    """Whether it mounts :data:`podbench.model.SEAT_IDENTITY_VOLUME`. An
    ephemeral container's mounts are fixed at creation, so a reconnect has to
    read this rather than re-derive it from what the pod declares *now*."""

    kind: SeatKind = SeatKind.ATTACH
    """Which of the three modes this seat is serving, from :func:`seat_kind`.

    Derived from the pod on every read rather than stamped on the container,
    because two of the three markers are not on the container at all and the
    third is a mount. Defaulted so that a ``SeatInfo`` built in a test or a
    doctest states only what it is about."""

    in_hotfixed_pod: bool = False
    """Whether the pod carries the hotfix layout (:func:`is_hotfixed`) while
    this seat is *not* :attr:`~podbench.model.SeatKind.HOTFIX`.

    The one combination that is neither kind and has to be said out loud: the
    application runs the venv on the claim and the seat resolves the image's, so
    an editor opened here reads code that is not running and a debugger sets
    breakpoints that never bind. Kept apart from :attr:`kind` because it is a
    fact about the *pod* the seat is in, not about the seat."""

    owner: str | None = None
    """The cluster identity that landed it, from :data:`OWNER_ENV`, or ``None``.

    ``None`` is **unknown** and never "yours": every seat landed before the
    variable existed carries none, and so does one landed where ``kubectl auth
    whoami`` would not answer. Read back from the spec for the same reason the
    ids are - a listing routinely runs on a machine that never saw the attach,
    and on this question it is routinely a *different* machine (issue #113)."""

    @property
    def running(self) -> bool:
        """Whether this container can still take an ssh session."""
        return self.phase == "running"


def editor_folder(
    session: Session, seat_spec: Mapping[str, Any] | None
) -> tuple[str, str | None]:
    """The folder ``vscode`` should open, and the line that says why.

    The seat's home is the answer for every pod without the layout, and issue
    #189 is what it costs on a pod with one: the project is on the claim, that
    is the only tree where an edit reaches the running process, and a user who
    does not notice edits the image's copy through ``/proc/1/root`` where
    nothing they write ever runs.

    The question is asked of **the seat's own** ``volumeMounts`` and not of
    :func:`is_hotfixed`, because those are different questions. ``is_hotfixed``
    reads the pod, and a pod can carry the layout while this seat does not carry
    the mount - :func:`hotfix_claim_mounts` degrades a ``subPath`` refusal to a
    note, and a reconnect into a seat older than the layout has none either. The
    home is the honest answer there, and the note says so rather than leaving it
    to be discovered.

    The path is read off the mount rather than taken from
    :data:`~podbench.model.HOTFIX_APP_PATH`, for the same reason
    :func:`hotfix_claim_mounts` copies it: the application chose it, podbench
    only matched it, and a seat pointed at a container that mounts it somewhere
    else would otherwise be sent to a path that is not there.

    The second element is ``None`` for the ordinary case. ``open_seat`` already
    reports the folder it opened, and a home that nothing competed for needs no
    justification on top of that.
    """
    home = seat_layout(session).home
    if not session.hotfixed:
        return home, None
    mounts = _as_list(as_dict(seat_spec).get("volumeMounts"))
    mount = next(
        (
            entry
            for entry in (as_dict(entry) for entry in mounts)
            if _entry_name(entry) == HOTFIX_CLAIM_VOLUME
        ),
        None,
    )
    folder = _mount_path(mount) if mount is not None else ""
    if not folder:
        return home, EDITOR_FOLDER_HOME_NOTE.format(home=home, path=HOTFIX_APP_PATH)
    return folder, EDITOR_FOLDER_CLAIM_NOTE.format(folder=folder, home=home)


def is_dev_pod(pod_json: Mapping[str, Any]) -> bool:
    """Whether podbench authored this pod as Iterate mode's clone.

    The same read :func:`podbench.dev.is_dev_pod` makes, and duplicated rather
    than imported: :mod:`podbench.dev` imports this module, so the listing
    cannot ask it. Both are one dict lookup against the label podbench itself
    wrote, so there is nothing here to drift.

    >>> is_dev_pod({"metadata": {"labels": {DEVPOD_LABEL: "true"}}})
    True
    """
    return (
        as_dict(as_dict(pod_json.get("metadata")).get("labels")).get(DEVPOD_LABEL)
        == "true"
    )


_SUPERVISOR_MARKERS = (HOTFIX_CHILD_PID_PATH, HOTFIX_HOLD_PATH)
"""What makes a container's ``args`` the supervisor rather than a shell script.

Both, never either. The pid file alone is a container that records its child;
the hold file alone is a container that reads a flag. Only the pair is the loop
:func:`podbench.hotfix.hold_loop_args` emits, and neither depends on the
entrypoint it wraps - which is what lets this be read off any application's
values without knowing what the application is."""


def runs_hotfix_supervisor(container: Mapping[str, Any]) -> bool:
    """Whether this container's ``args`` are podbench's supervisor loop.

    >>> runs_hotfix_supervisor({"args": ["--serve"]})
    False
    """
    text = "\n".join(
        arg for arg in _as_list(container.get("args")) if isinstance(arg, str)
    )
    return all(marker in text for marker in _SUPERVISOR_MARKERS)


def is_hotfixed(pod_json: Mapping[str, Any]) -> bool:
    """Whether this pod was deployed to carry a hotfix on a claim.

    Read from the **pod spec**, and from both halves of it: the pod declares
    :data:`~podbench.model.HOTFIX_CLAIM_VOLUME` *and* a container runs the
    supervisor. Either alone is something else - a pod can carry a volume of
    that name for its own reasons, and a container can wrap its entrypoint in a
    loop that has nothing to do with podbench - so a predicate three features
    hang off has to see the pair.

    It was an annotation until 2026-08-22, and the annotation was never written:
    Argo strips pod-template annotations on self-heal (#32), so the code that
    wrote it was deleted and this was left reading a key nothing set. It
    returned ``False`` on every pod podbench ever hotfixed, which silently
    disabled :data:`UNMOUNTED_HOTFIX_NOTE`, made
    :attr:`~podbench.model.SeatKind.HOTFIX` unreachable and emptied the listing
    keyed on it. Both halves read here are emitted by
    :func:`podbench.hotfix.values_snippet` and both survive Argo, which is the
    whole reason to read them instead.

    Still never from the manifest on the claim: loading that *raises* on a
    manifest a newer podbench wrote, deliberately
    (:meth:`podbench.hotfix.HotfixManifest.from_json`), and a listing must not
    be the thing that fails on it. That reasoning is unchanged - it is the
    manifest this avoids, not the annotation.

    >>> is_hotfixed({"spec": {"volumes": [{"name": HOTFIX_CLAIM_VOLUME}]}})
    False
    """
    if HOTFIX_CLAIM_VOLUME not in declared_volumes(pod_json):
        return False
    return any(
        runs_hotfix_supervisor(as_dict(entry))
        for entry in _as_list(as_dict(pod_json.get("spec")).get("containers"))
    )


_CONVENTION_VOLUMES = frozenset({SEAT_IDENTITY_VOLUME, SEAT_HOME_VOLUME})
"""The volumes a seat mounts because podbench asked it to, not because the user
did. Both are routinely mounted by the workload as well - that is what makes the
identity a *convention* - so a kind derived from "shares a mount with the target"
has to discount them or every seat in a cooperating pod reads as ``hotfix``.

:data:`~podbench.model.HOTFIX_CLAIM_VOLUME` is podbench's too, and is
deliberately **not** here even though :func:`hotfix_claim_mounts` now authors it
unasked. Sharing the claim with the target is not incidental to the kind, it
*is* the kind - discounting it would make :attr:`~podbench.model.SeatKind.HOTFIX`
unreachable a second time, by a different route than the one #177 fixed."""


def shares_workload_volume(
    pod_json: Mapping[str, Any], container: Mapping[str, Any], target: str | None
) -> bool:
    """Whether this seat mounts one of its target's own volumes.

    Which is the whole of what makes a seat Hotfix mode's rather than Observe
    mode's: :func:`resolve_mounts` authors the mount at the *same path* the
    application uses, because the venv's ``bin/python`` and the checkout's
    editable install are absolute paths recorded on the claim.
    """
    if target is None:
        return False
    mine = {
        _entry_name(as_dict(entry)) for entry in _as_list(container.get("volumeMounts"))
    } - _CONVENTION_VOLUMES
    if not mine:
        return False
    for entry in _as_list(as_dict(pod_json.get("spec")).get("containers")):
        workload = as_dict(entry)
        if _entry_name(workload) != target:
            continue
        return any(
            _entry_name(as_dict(mount)) in mine
            for mount in _as_list(workload.get("volumeMounts"))
        )
    return False


def seat_kind(
    pod_json: Mapping[str, Any],
    container: Mapping[str, Any],
    *,
    sidecar: bool,
    target: str | None,
) -> SeatKind:
    """Which mode this seat serves, from the pod and the container together.

    ``sidecar`` is not the same question as "is this a dev pod", and conflating
    them is the mistake this signature exists to prevent: nothing stops an
    ``attach`` landing an ephemeral seat *in* a dev pod, and that seat is an
    Observe-mode seat whatever the pod is labelled - its target's process is not
    its own child, so ``flavour.detect_mode`` measures ``OBSERVE`` and every path
    mapping is the Observe-mode one. Only the sidecar podbench authored is
    :attr:`~podbench.model.SeatKind.DEV`.
    """
    if sidecar and is_dev_pod(pod_json):
        return SeatKind.DEV
    if is_hotfixed(pod_json) and shares_workload_volume(pod_json, container, target):
        return SeatKind.HOTFIX
    return SeatKind.ATTACH


def seats(pod_json: Mapping[str, Any], *, base: str = CONTAINER_BASE) -> list[SeatInfo]:
    """Every podbench **ephemeral** container this pod carries, live or burnt.

    Dead ones are listed too, deliberately: their names are gone for the pod's
    lifetime and a user looking at ``podbench-4`` deserves to see why 1-3 are
    not reusable.

    Iterate mode's sidecar is *not* here, and that is load-bearing rather than an
    oversight: this is what :func:`running_seat` decides a reconnect from, and an
    ordinary container is not a seat an ``attach`` may reuse or supersede. A
    listing wants both and asks :func:`all_seats`.
    """
    statuses = {
        name: as_dict(entry)
        for entry in _as_list(
            as_dict(pod_json.get("status")).get("ephemeralContainerStatuses")
        )
        if (name := _entry_name(entry)) is not None
    }
    hotfixed = is_hotfixed(pod_json)
    found: list[SeatInfo] = []
    for entry in _as_list(as_dict(pod_json.get("spec")).get("ephemeralContainers")):
        container = as_dict(entry)
        name = _entry_name(container)
        if name is None or not (name == base or name.startswith(f"{base}-")):
            continue
        phase, detail = _phase_of(statuses.get(name, {}))
        target = _as_str(container.get("targetContainerName"))
        kind = seat_kind(pod_json, container, sidecar=False, target=target)
        found.append(
            SeatInfo(
                name=name,
                rung=rung_of_spec(container),
                phase=phase,
                detail=detail,
                uid=_as_int(as_dict(container.get("securityContext")).get("runAsUser")),
                gid=_as_int(
                    as_dict(container.get("securityContext")).get("runAsGroup")
                ),
                image=_as_str(container.get("image")),
                target=target,
                home=spec_env(container).get("HOME"),
                identity_mounted=_mounts_volume(container, SEAT_IDENTITY_VOLUME),
                kind=kind,
                in_hotfixed_pod=hotfixed and kind is not SeatKind.HOTFIX,
                owner=spec_env(container).get(OWNER_ENV),
            )
        )
    return found


def dev_seat(
    pod_json: Mapping[str, Any], *, base: str = CONTAINER_BASE
) -> SeatInfo | None:
    """Iterate mode's sidecar as a seat, or ``None`` if this is not a dev pod.

    A separate read because it is a separate *place*: the sidecar is an ordinary
    container in ``spec.containers``, so nothing walking
    ``spec.ephemeralContainers`` has ever seen it. ``podbench list`` therefore
    reported a dev pod as carrying no podbench container at all - which is to
    say it did not report the pod at all, since a pod with no seats is dropped
    (issue #141's premise, and the reason the three modes could not be told
    apart by looking).

    Its ``target`` comes from :data:`TARGET_NAME_ENV` rather than from a
    ``targetContainerName`` it cannot have, so the column reads the same for
    both container kinds and :func:`superseded_seats` compares like with like.

    >>> pod = {"metadata": {"labels": {DEVPOD_LABEL: "true"}},
    ...        "spec": {"containers": [{"name": "app"}, {"name": "podbench"}]}}
    >>> dev_seat(pod).kind
    <SeatKind.DEV: 'dev'>
    >>> dev_seat({"spec": {"containers": [{"name": "podbench"}]}}) is None
    True
    """
    if not is_dev_pod(pod_json):
        return None
    statuses = {
        name: as_dict(entry)
        for entry in _as_list(as_dict(pod_json.get("status")).get("containerStatuses"))
        if (name := _entry_name(entry)) is not None
    }
    for entry in _as_list(as_dict(pod_json.get("spec")).get("containers")):
        container = as_dict(entry)
        if _entry_name(container) != base:
            continue
        phase, detail = _phase_of(statuses.get(base, {}))
        return SeatInfo(
            name=base,
            rung=rung_of_spec(container),
            phase=phase,
            detail=detail,
            uid=_as_int(as_dict(container.get("securityContext")).get("runAsUser")),
            gid=_as_int(as_dict(container.get("securityContext")).get("runAsGroup")),
            image=_as_str(container.get("image")),
            target=spec_env(container).get(TARGET_NAME_ENV),
            home=spec_env(container).get("HOME"),
            identity_mounted=_mounts_volume(container, SEAT_IDENTITY_VOLUME),
            kind=SeatKind.DEV,
            owner=spec_env(container).get(OWNER_ENV),
        )
    return None


def _is_sidecar(session: Session, sidecar: SeatInfo | None) -> bool:
    """Whether this session ended up in Iterate mode's sidecar.

    Asked of the pair rather than of the container's name: an ephemeral seat may
    legitimately be called ``podbench`` - :func:`seats` accepts the bare base,
    which is what an older podbench landed - so the name alone does not say
    which container kind is in hand.
    """
    return sidecar is not None and session.seat.container == sidecar.name


def all_seats(
    pod_json: Mapping[str, Any], *, base: str = CONTAINER_BASE
) -> list[SeatInfo]:
    """Every seat in this pod, of either container kind, for a **listing**.

    Not for :func:`running_seat`, and the split is the point: a reconnect may
    only reuse an ephemeral container it could have landed itself, while a
    report is asked what is *there*. The sidecar comes first because it is the
    pod's reason to exist and any ephemeral seat beside it was added later.
    """
    sidecar = dev_seat(pod_json, base=base)
    return [*([] if sidecar is None else [sidecar]), *seats(pod_json, base=base)]


def running_seat(
    pod_json: Mapping[str, Any],
    *,
    base: str = CONTAINER_BASE,
    ids: tuple[int, int] | None = None,
    owner: str | None = None,
) -> SeatInfo | None:
    """The podbench container a reconnect should reuse, if there is one.

    ``ids`` is the ``(uid, gid)`` pair a seat must already be pinned to for a
    reconnect to be honest about it. An ephemeral container's securityContext is
    fixed for the pod's lifetime, so a seat in the wrong group cannot be
    corrected in place and reusing it would silently ignore the ids the caller
    asked for - the same reasoning as :func:`above_ceiling`, and the same
    currency, a permanent name.

    It is also what **bounds the automatic correction to one extra name per
    pod**. The first attach on a target whose manifest states no ``runAsGroup``
    lands a seat at the image's gid, measures the target's real gid and lands a
    corrected one; every later attach reaches the same first seat, wants the same
    corrected ids, and *finds the corrected seat already running* here instead of
    landing a third.

    A seat a later one **superseded** is never the answer, whether or not any
    ids were named. The gid correction leaves two live containers with the same
    target and the same uid, the earlier of which cannot trace and whose agent
    wrote its sshd config somewhere the corrected seat's did not; the earlier
    one is also the one reached first, because ephemeral containers are listed
    in the order they were appended. ``ssh-config`` asked with no ids at all and
    got it, and emitted a stanza whose ``ProxyCommand`` named the wrong config
    path (issue #117). :func:`superseded_seats` decides, from the seats alone,
    so this holds on a machine that never saw the attach.

    ``owner`` is the caller's own cluster identity, and a seat recording a
    **different** one is never the answer either: its ``authorized_keys`` was
    written for another key when it started and cannot be added to from here,
    so reconnecting would hand a stanza that gets ``Permission denied
    (publickey)`` and nothing to read it against (issue #113). A seat recording
    *no* owner is still reused - that is every seat landed before the stamp
    existed, and refusing them would spend a permanent container name on each
    one - but the report says ``unknown owner`` rather than claiming it.

    >>> spec = {"spec": {"ephemeralContainers": [
    ...     {"name": "podbench-1", "securityContext": {"runAsUser": 1000}},
    ...     {"name": "podbench-2",
    ...      "securityContext": {"runAsUser": 1000, "runAsGroup": 1000}}]},
    ...  "status": {"ephemeralContainerStatuses": [
    ...     {"name": "podbench-1", "state": {"running": {"startedAt": "t"}}},
    ...     {"name": "podbench-2", "state": {"running": {"startedAt": "t"}}}]}}
    >>> running_seat(spec).name
    'podbench-2'
    >>> running_seat(spec, ids=(1000, 1000)).name
    'podbench-2'

    Last, and only as a tie-break between seats already found reusable: a seat
    whose uid **cannot** match its target's is not offered ahead of one whose
    can. :func:`superseded_seats` reaches only a pair pinned to the same uid, so
    it does not reach the pairs an older podbench left behind - a full-rung seat
    at uid 0 corrected to the target's ids, which is six p47 pods
    (``bl47p-mo-ioc-01-0``, ``p47-epics-pvcs-...``). There ``ssh-config`` was
    emitting a stanza for the seat whose verdict reads "launch-only ... no
    read-only inspection" in preference to the one that reads "live attach
    available", purely because ephemeral containers are listed in the order they
    were appended.

    >>> legacy = {"spec": {
    ...     "containers": [{"name": "app",
    ...                     "securityContext": {"runAsUser": 37887}}],
    ...     "ephemeralContainers": [
    ...         {"name": "podbench-1", "targetContainerName": "app",
    ...          "securityContext": {"runAsUser": 0}},
    ...         {"name": "podbench-2", "targetContainerName": "app",
    ...          "securityContext": {"runAsUser": 37887,
    ...                              "runAsGroup": 37887}}]},
    ...  "status": {"ephemeralContainerStatuses": [
    ...     {"name": "podbench-1", "state": {"running": {"startedAt": "t"}}},
    ...     {"name": "podbench-2", "state": {"running": {"startedAt": "t"}}}]}}
    >>> running_seat(legacy).name
    'podbench-2'

    **Not "prefer the later seat", which would be wrong on the same beamline.**
    ``p47-epics-opis`` carries a root seat beside a later one at uid 101 against
    a target the manifest says nothing about and the node reports as root, and
    there the *earlier* seat is the one that can inspect. The rule is the
    credential match and never the order:

    >>> opis = {"spec": {
    ...     "containers": [{"name": "server"}],
    ...     "ephemeralContainers": [
    ...         {"name": "podbench-1", "targetContainerName": "server",
    ...          "securityContext": {"runAsUser": 101}},
    ...         {"name": "podbench-3", "targetContainerName": "server",
    ...          "securityContext": {"runAsUser": 0}}]},
    ...  "status": {
    ...     "containerStatuses": [{"name": "server",
    ...                            "user": {"linux": {"uid": 0}}}],
    ...     "ephemeralContainerStatuses": [
    ...        {"name": "podbench-1", "state": {"running": {"startedAt": "t"}}},
    ...        {"name": "podbench-3",
    ...         "state": {"running": {"startedAt": "t"}}}]}}
    >>> running_seat(opis).name
    'podbench-3'
    """
    present = seats(pod_json, base=base)
    replaced = superseded_seats(present)
    candidates = [
        seat
        for seat in present
        if seat.running
        and seat.name not in replaced
        and (ids is None or (seat.uid, seat.gid) == ids)
        and not _owned_by_another(seat.owner, owner)
    ]
    if not candidates:
        return None
    # A preference and never a filter: the demoted seat is still returned when
    # it is the only one, because it is a working editor and shell and this
    # function's job is to find a seat rather than to grade one.
    return next(
        (seat for seat in candidates if not _cannot_reach_its_target(seat, pod_json)),
        candidates[0],
    )


def _cannot_reach_its_target(seat: SeatInfo, pod_json: Mapping[str, Any]) -> bool:
    """Whether this seat's own spec rules out the credential check.

    Deliberately **not** a claim that some other seat replaced it. Relaxing
    :func:`superseded_seats` to pair seats pinned to different uids would reach
    the legacy pairs, and would also pair two *people's* seats - the correction
    signature and "a colleague attached after me" are the same shape once the
    uid is allowed to move, and legacy seats carry no :data:`OWNER_ENV` for
    ``_owned_by_another`` to tell them apart with. So nothing here is paired,
    nothing is skipped, and no seat is described as anybody's: this only breaks
    a tie between candidates every other rule already found reusable, and
    breaking it towards the seat that can inspect is right whoever landed them.

    ``True`` needs three things, and the absence of any one of them is
    *unknown* rather than a verdict:

    * a seat below the **full** rung. A stored ``SYS_PTRACE`` may have been
      stripped, but it may equally be effective, and a capability is exempt from
      the credential check entirely - so a seat carrying one is never demoted for
      a uid it does not need. The same reasoning as :func:`above_ceiling`'s.
    * a **pinned** uid. A seat that pins none runs as the image's user, which is
      not in the spec (report §3.10).
    * a **known** target uid. The manifest's, or - where it states none, which
      is every pod on argus - the uid the kubelet resolved and reports on the
      container status.

    The gid is not asked for. A differing uid is already the whole answer, and
    the gid half of the same comparison is what
    :func:`superseded_seats` covers.
    """
    if seat.rung is Rung.FULL or seat.uid is None or seat.target is None:
        return False
    stated, _ = target_uid_gid(pod_json, seat.target)
    target = (
        stated if stated is not None else reported_target_uid(pod_json, seat.target)
    )
    return target is not None and seat.uid != target


def _owned_by_another(recorded: str | None, asking: str | None) -> bool:
    """Whether a seat is provably somebody else's.

    Both halves have to be known for the answer to be yes. Either one missing
    is *unknown*, and unknown is not a verdict: a seat landed before the stamp
    existed records nothing, and a cluster that would not answer ``auth
    whoami`` leaves the asker nameless. Deciding either way on one name is how
    an anonymous seat would come to be reported as somebody's.

    >>> _owned_by_another("alice", "bob")
    True
    >>> _owned_by_another(None, "bob"), _owned_by_another("alice", None)
    (False, False)
    """
    return recorded is not None and asking is not None and recorded != asking


def other_owners_seat(
    present: Sequence[SeatInfo], owner: str | None
) -> SeatInfo | None:
    """The first live seat that provably belongs to somebody else, if any.

    Asked only to explain why a fresh seat is being landed beside a running
    one, which is a surprise worth a line: the pod already carries a working
    seat and the user is spending a permanent container name anyway.

    >>> mine = SeatInfo("podbench-2", Rung.DEGRADED, "running", "", owner="bob")
    >>> theirs = SeatInfo("podbench-1", Rung.DEGRADED, "running", "",
    ...                   owner="alice")
    >>> other_owners_seat([theirs, mine], "bob").name
    'podbench-1'
    >>> other_owners_seat([mine], "bob") is None
    True
    """
    for seat in present:
        if seat.running and _owned_by_another(seat.owner, owner):
            return seat
    return None


def superseded_seats(present: Sequence[SeatInfo]) -> dict[str, str]:
    """Which live seats a later one has replaced, keyed by the earlier name.

    A pod carrying two seats is normal and not a fault: a multi-container pod
    wants one per container, and ``--new`` asks for a second on purpose. Exactly
    one mechanism makes a seat *replace* another, and it is the one the user did
    not ask for - the gid correction (#103). It has a signature nothing else
    has: same target container, same pinned uid, and a gid the later seat pins
    and the earlier one does not, because an ephemeral container's
    securityContext is fixed for its lifetime and the correction could only be
    made by landing a second container.

    Derived rather than remembered, because ``list`` and ``status`` routinely
    run on a machine that never saw the attach - and inferred only from that
    signature, so a deliberate ``--new`` seat, which pins the same ids as the
    seat beside it, is not labelled as superseding anything.

    >>> first = SeatInfo("podbench-1", Rung.DEGRADED, "running", "",
    ...                  uid=1000, gid=None, target="app")
    >>> second = SeatInfo("podbench-2", Rung.DEGRADED, "running", "",
    ...                   uid=1000, gid=1000, target="app")
    >>> superseded_seats([first, second])
    {'podbench-1': 'podbench-2'}
    >>> deliberate = SeatInfo("podbench-2", Rung.DEGRADED, "running", "",
    ...                       uid=1000, gid=None, target="app")
    >>> superseded_seats([first, deliberate])
    {}

    Two *people* on one pod produce the same signature and mean nothing by it:
    a colleague attaching after you lands a corrected seat beside your
    uncorrected one, and calling that a supersession would tell you your own
    seat had been replaced and send the next attach off to land a third
    (issue #113). So a pairing needs the owners not to disagree.

    >>> theirs = SeatInfo("podbench-2", Rung.DEGRADED, "running", "",
    ...                   uid=1000, gid=1000, target="app", owner="alice")
    >>> superseded_seats([replace(first, owner="bob"), theirs])
    {}
    """
    replaced: dict[str, str] = {}
    live = [seat for seat in present if seat.running]
    for index, earlier in enumerate(live):
        if earlier.uid is None:
            continue
        for later in live[index + 1 :]:
            if (later.target, later.uid) != (earlier.target, earlier.uid):
                continue
            # Since `all_seats` folds Iterate mode's sidecar in beside any
            # ephemeral seat somebody attached into the same dev pod, the two
            # can now reach this pairing while being different container kinds
            # entirely - and the correction this signature detects can only ever
            # happen between two ephemeral containers, because it exists to work
            # around a securityContext an ephemeral container cannot change.
            if later.kind is not earlier.kind:
                continue
            if _owned_by_another(later.owner, earlier.owner):
                continue
            if later.gid is not None and later.gid != earlier.gid:
                replaced[earlier.name] = later.name
    return replaced


DEV_SIDECAR_REUSED_NOTE = (
    "reconnected to `{seat}`, this dev pod's own sidecar, so this is an "
    "Iterate-mode seat: the application runs as its child and is relaunched "
    "from here with `podbench run`, and the workload container is idled. "
    "`--new` lands an Observe-mode seat beside it instead"
)
"""Said whenever an attach hands back Iterate mode's sidecar.

The mode is the whole of it, which is why ``--new`` is named and not argued
for: when an Observe-mode seat is worth a permanent container name - a non-root
sidecar on a cluster that will admit ``SYS_PTRACE`` - is a question for
``docs/how-to/vscode-remote-ssh.md``, and the reader here is being told which
seat they got.

Everything else `attach` prints is true of both kinds of seat, and the one fact
that is not - which process the debugger will be looking at - is the one that
decides whether a breakpoint binds. Reporting it is
issue #141's second decision, arrived at from the other side: the mode is
measured here rather than declared, so there is nothing to disagree with.
"""

OTHER_MODES_NOTE = (
    "other modes are their own verbs: `podbench hotfix init` for a venv on a "
    "claim that survives restarts, `podbench dev` for a clone the application "
    "relaunches from. Both change the workload, so neither is offered here"
)
"""Named once, on the run that landed a seat where there was none.

Said rather than asked, and the difference is what the question would have been
worth: with no seat in the pod there is nothing ambiguous to resolve, ``attach``
is the only one of the three this verb could carry out, and the other two answers
would both have been "go and run a different command". A prompt whose default is
the only actionable answer is a keystroke on the commonest path in the product.

"Both change the workload" is the whole of the reason now; that this verb was
given no arguments for either of those changes is podbench explaining its own
restraint, which is a docstring's job and not a line in a report.

The explicitness #141 asked for is delivered by the *other* half of that issue:
the mode is measured and reported - by the ``KIND`` column, and by
:data:`DEV_SIDECAR_REUSED_NOTE` on a reconnect - rather than declared by a word
the user typed.
"""

DEV_SIDECAR_PROVISION_NOTE = (
    "not provisioning: this is Iterate mode's seat, where the application is "
    "launched from the seat rather than found running in another container. "
    "There is no live target to install debugpy into, and the launch "
    "configuration needs none"
)
"""Why ``vscode`` spends nothing on provisioning a dev pod.

Not a decline that could have gone the other way: provisioning targets a running
process in the *workload* container, and :func:`podbench.spec.dev_pod_spec` idles
that container to ``sleep`` precisely so the seat can own the port. Injecting
into it would succeed against ``sleep`` and report a debugger nobody can reach.

Both halves of provisioning - the debugpy install and the server injection - are
"there is no live target", so the line says it once. That Iterate mode's launch
configuration starts the interpreter under the debugger instead is
:mod:`podbench.vscode`'s to author and the how-to's to explain.
"""

OTHER_OWNER_WARNING = (
    "{seat} is running but {owner} landed it, and its authorized_keys cannot "
    "be added to from here: a new container is being landed, and its name is "
    "spent whether or not it works out"
)
"""Why a running seat was passed over for a fresh one (issue #113).

A warning and not a note, unlike :data:`SUPERSEDED_NOTE`: it is said on the
attach that spends the name, to the person spending it, and the fact it carries
- somebody else is working in this pod - is one they did not have.

*When* an ephemeral container's ``authorized_keys`` is written, and why that
makes a colleague's seat unreachable rather than merely impolite, is in
``docs/how-to/attach-to-a-pod.md`` under "Reconnecting only reaches your seat".
"""

UNKNOWN_OWNER = "unknown - this seat records none"
"""What a listing says about a seat with no :data:`OWNER_ENV`.

Every seat that existed before the stamp did, which on the beamline is six pods'
worth. Reported as unknown rather than left blank, and never attributed to the
reader: the same rule the memory row is held to, in the one place where guessing
would tell somebody a colleague's seat was theirs.
"""

SUPERSEDED_NOTE = (
    "superseded by {later}, which corrected this seat's group id. The "
    "correction cost a second name and this one cannot be removed; "
    "`--target-gid <gid>` pins the group up front and costs one name"
)
"""What a listing says about a seat a later one replaced.

Said here and not as a warning: by the time anyone runs ``list`` the correction
is history, and :data:`ID_CORRECTION_WARNING` already spent its one line on the
attach that made it. This is the fact a reader of two seats needs to have, which
is why it is a row under the seat rather than a block under the pod.

Why the correction cost a name - an ephemeral container's ``securityContext`` is
fixed for the pod's lifetime - is in ``docs/how-to/attach-to-a-pod.md``, and the
row carries the consequence, which is that this seat is not going away."""


def rung_of_spec(container: Mapping[str, Any]) -> Rung:
    """Which rung an existing container was *authored* at.

    Read back from the spec rather than remembered, so a listing from another
    machine — or from another user — still has an answer. It is the weaker of
    the two answers, and deliberately not the one ``attach`` reports: a
    securityContext is what admission agreed to store, which is neither what
    podbench asked for nor what the kernel gave the container.
    :func:`probe_seat_credentials` reads the seat itself, and
    :func:`podbench.model.measured_rung` labels what it finds.
    """
    security = as_dict(container.get("securityContext"))
    added = [
        str(cap) for cap in _as_list(as_dict(security.get("capabilities")).get("add"))
    ]
    if "SYS_PTRACE" in added:
        return Rung.FULL
    if isinstance(security.get("runAsUser"), int):
        return Rung.DEGRADED
    return Rung.SEAT


def above_ceiling(
    seat: SeatInfo, ceiling: Rung, *, target_uid: int | None
) -> str | None:
    """Why ``seat`` is not a seat ``--max-rung ceiling`` would have landed.

    ``None`` means it is one, and a reconnect may reuse it.

    Deliberately not ``LADDER.index(seat.rung) < LADDER.index(ceiling)``:
    :func:`rung_of_spec` cannot tell a stripped full rung from a genuine
    degraded one — which is the very reason this flag exists — and the stripped
    one reads back as ``degraded`` while running as root (issue #94). So the
    *uid* decides, because the uid is what a rung below full is for: those rungs
    pin the target's own, and it is the pin rather than the label that buys the
    ptrace credential match.

    Root is judged on its own, before the pin is compared, because a root
    target has no uid to compare against: ``uid 0`` is the one pin no rung
    below full ever writes.

    >>> seat = SeatInfo("podbench-1", Rung.DEGRADED, "running", "", uid=0)
    >>> above_ceiling(seat, Rung.FULL, target_uid=37887) is None
    True
    >>> above_ceiling(seat, Rung.DEGRADED, target_uid=37887)
    'it runs as uid 0, which no rung below full authors'
    >>> above_ceiling(seat, Rung.DEGRADED, target_uid=0)
    'it runs as uid 0, which no rung below full authors'
    >>> other = SeatInfo("podbench-2", Rung.DEGRADED, "running", "", uid=1000)
    >>> above_ceiling(other, Rung.DEGRADED, target_uid=37887)
    "it runs as uid 1000, not the target's uid 37887"
    >>> seat_rung = SeatInfo("podbench-3", Rung.SEAT, "running", "", uid=None)
    >>> above_ceiling(seat_rung, Rung.DEGRADED, target_uid=0) is None
    True
    """
    if ceiling is Rung.FULL:
        return None
    if seat.rung is Rung.FULL:
        return f"it carries SYS_PTRACE, which is above the {ceiling.value} rung"
    if seat.uid == 0:
        # The full rung is the only one that writes `runAsUser: 0`: the degraded
        # rung refuses a root target outright and the seat rung drops uid 0
        # rather than pin it, because `runAsNonRoot: true` beside it is admitted
        # and then refused by the kubelet (report 3.18, `_rung_security_context`).
        # So a root seat below the full label is a *stripped* full rung, which is
        # exactly the seat this flag exists to get away from (issue #94) - and
        # against a root target it is the one the pin comparison below cannot
        # catch, there being no non-zero uid for it to disagree with.
        return "it runs as uid 0, which no rung below full authors"
    # A target at uid 0 has no rung that pins one (`spec._rung_security_context`
    # refuses both), so an unpinned seat has nothing to disagree with.
    if target_uid and seat.uid != target_uid:
        ran_as = "the image's own user" if seat.uid is None else f"uid {seat.uid}"
        return f"it runs as {ran_as}, not the target's uid {target_uid}"
    return None


_KUBELET_POD_REF = re.compile(r'\s*\(pod: "[^"]*"(?:, container: [^)]*)?\)')
"""The kubelet's own ``(pod: "name_ns(uid)", container: x)`` tail, dropped.

``format.Pod`` is appended to every container error the kubelet raises, and
every field in it is already on screen: the listing is headed
``namespace/pod`` and the row this sits under names the seat. The one fact that
is not is the pod's UID, which nobody reading a pod they named can use.

Elided rather than wrapped because it cannot be wrapped. ``"<pod>_<ns>(<uid>)",``
carries no space, so :func:`~podbench.console.wrap` breaks nowhere in it and a
``CreateContainerConfigError`` on a 40-character pod name ran the ``state`` row
four columns past a 100-column terminal - and there is no width at which a
90-character token fits under a 14-column indent. Shortening what is printed is
the only fix that is not a broken token, and this is the half of it a reader
already has twice.
"""


def _phase_of(status: Mapping[str, Any]) -> tuple[str, str]:
    state = as_dict(status.get("state"))
    running = as_dict(state.get("running"))
    if running:
        return "running", f"since {running.get('startedAt', 'unknown')}"
    waiting = as_dict(state.get("waiting"))
    if waiting:
        # A waiting reason without a message is ordinary, not a gap: the kubelet
        # sends `PodInitializing` and `ContainerCreating` bare, and only the
        # error reasons carry prose. Joined unconditionally, that printed
        # `state     PodInitializing:` - a colon introducing nothing, which
        # reads as a message this code dropped.
        reason = _as_str(waiting.get("reason")) or "?"
        message = _KUBELET_POD_REF.sub(
            "", _as_str(waiting.get("message")) or ""
        ).strip()
        return "waiting", f"{reason}: {message}" if message else reason
    terminated = as_dict(state.get("terminated"))
    if terminated:
        return (
            "terminated",
            f"{terminated.get('reason', '?')}: name burnt for this pod's lifetime",
        )
    return "pending", "the node has not reported on it yet"


# -- the capability ladder --------------------------------------------------


@dataclass(frozen=True)
class LadderStep:
    """One rung's outcome, kept so the report can say *why* it fell through."""

    rung: Rung
    admitted: bool
    detail: str
    container: str | None = None


LADDER: tuple[Rung, ...] = (Rung.FULL, Rung.DEGRADED, Rung.SEAT)
"""The rungs by capability, most first.

Stated once so that ``--max-rung`` can express "at or below" as an index
comparison. :class:`~podbench.model.Rung` deliberately does not order itself:
its members describe securityContexts, and only the walk knows which carries
more.

It is no longer always the order they are *tried* in — see
:func:`_attempt_order`, which lets a target whose uid is known and not root
have its own rung tried first. Anything ranking rungs by capability
(``--max-rung``, :func:`above_ceiling`) indexes this; only the walk reads the
attempt order.
"""


def plan_ladder(
    pod_json: Mapping[str, Any],
    container: str,
    *,
    target_uid: int | None = None,
    max_rung: Rung | None = None,
) -> tuple[tuple[Rung, str | None], ...]:
    """Order the rungs, pre-skipping the ones that cannot possibly land.

    The second element of each pair is ``None`` to mean "attempt it", or the
    reason it was skipped.

    ``max_rung`` is a ceiling, not a choice: everything above it is skipped and
    the walk still falls through the rungs below. It exists because a rung is
    only dropped when something *refuses* it, and a **mutating** admission
    policy does not refuse — it rewrites. A cluster that strips
    ``capabilities.add`` admits the full rung and leaves a root seat with no
    capability (issue #94; measured at DLS 2026-08-18). :func:`_dry_run_rung`
    now sees that coming and drops the rung itself, so this is no longer the
    only defence — but it is the one that needs no round trip, and a cluster
    that will not serve a dry run leaves the walk with no signal at all.
    ``--max-rung degraded`` is how somebody who knows their cluster strips
    capabilities skips the rung that cannot work there.

    Two skips are pre-emptive rather than reactive. A pod with
    ``runAsNonRoot: true`` accepts a root ephemeral container at the API server
    and has the *kubelet* refuse it seconds later, burning the container name in
    the process (report 3.18) — so that refusal is read out of the target's
    securityContext instead of provoked. And a target that itself runs as root
    has no degraded rung at all: that rung *is* "the target's own uid, plus
    ``runAsNonRoot: true``", which uid 0 cannot satisfy.

    ``target_uid`` overrides what the manifest says, for the case the manifest
    says nothing: the effective uid then comes from the image and only the
    running process knows it. Guessing is refused — defaulting the fallback to
    root would cost the sysroot, maps, environ and exe reads that are the whole
    value of the degraded rung (report 4.5). The *reason* the rung was skipped
    is a different question from the uid it would have been authored with, and
    it consults the node as well (:func:`_unpinned_target`): a manifest stating
    nothing over a root image is not a target waiting for ``--target-uid``, it
    is a root target, and the flag cannot reach a rung that does not exist.

    The **order** is the target's to imply, and only the ceiling overrides it.
    Where the target's uid is known and not root, the degraded rung at that uid
    already satisfies ``PTRACE_MODE_ATTACH`` by credential match, and it reads
    all six probe paths where a root seat whose capability was stripped reads
    three (report 3.11) — so it is tried first and the capability rung is not
    spent proving it. The full rung stays in the walk below it, because it is
    the only rung Yama exempts and ``ptrace_scope`` is a per-node sysctl that no
    laptop-side read can answer (report 3.13): it is measured from inside a
    seat, which is a seat later than this decision. Root targets, targets whose
    uid is unknown, and pods sharing one PID namespace between containers of
    different uids keep the classic order, because for them the degraded rung
    cannot be authored or cannot reach what the user came for. Passing
    ``--max-rung`` states the starting rung explicitly and restores it too.

    >>> pod = {"spec": {"containers": [
    ...     {"name": "app", "securityContext": {"runAsUser": 37887}}]}}
    >>> [rung.value for rung, _ in plan_ladder(pod, "app")]
    ['degraded', 'full', 'seat']
    >>> [rung.value for rung, _ in plan_ladder(pod, "app", max_rung=Rung.FULL)]
    ['full', 'degraded', 'seat']
    >>> root = {"spec": {"containers": [
    ...     {"name": "app", "securityContext": {"runAsUser": 0}}]}}
    >>> [rung.value for rung, _ in plan_ladder(root, "app")]
    ['full', 'degraded', 'seat']
    """
    uid, _ = target_uid_gid(pod_json, container)
    if target_uid is not None:
        uid = target_uid

    full: str | None = None
    if max_rung is not None and LADDER.index(max_rung) > 0:
        full = f"--max-rung {max_rung.value} was given, so it was not attempted"
    elif runs_as_non_root(pod_json, container):
        full = (
            "the pod sets runAsNonRoot: true, so the kubelet would refuse a root "
            "container with CreateContainerConfigError after the API server had "
            "accepted it - and burn the container name doing so"
        )

    degraded: str | None = None
    if max_rung is not None and LADDER.index(max_rung) > 1:
        degraded = f"--max-rung {max_rung.value} was given, so it was not attempted"
    elif uid is None:
        degraded = _unpinned_target(reported_target_uid(pod_json, container))
    elif uid == 0:
        degraded = "the target runs as root, which runAsNonRoot: true cannot express"

    skipped = {Rung.FULL: full, Rung.DEGRADED: degraded, Rung.SEAT: None}
    return tuple(
        (rung, skipped[rung])
        for rung in _attempt_order(
            uid,
            cross_uid=shares_pid_namespace_across_uids(pod_json, container, uid),
            max_rung=max_rung,
        )
    )


def _unpinned_target(reported: int | None) -> str:
    """Why the degraded rung is out where the manifest pins no uid — and whether
    ``--target-uid`` would change that.

    A spec that omits ``runAsUser`` is not a target whose uid is unknown: the
    image's own ``USER`` decides it, and the node publishes the result in
    ``status.containerStatuses[].user.linux.uid``. Offering the flag without
    reading that was wrong wherever the answer is 0 — on argus (hgv27681,
    2026-08-20) no pod in the namespace states a uid and every container is
    root, so this line advised ``--target-uid`` on all eight of them while the
    capability report four lines below read the target's uid as 0, and the
    sentence's own second half says uid 0 admits no lower rung. Confirmed by
    running it: ``--target-uid 0 --new`` prints the refusal and spends a
    container name.

    >>> "--target-uid" in _unpinned_target(0)
    False
    >>> "runAsNonRoot: true cannot express" in _unpinned_target(0)
    True
    >>> "--target-uid 37887" in _unpinned_target(37887)
    True
    >>> "neither the pod spec nor the node" in _unpinned_target(None)
    True
    """
    if reported == 0:
        return (
            "the target runs as root, which runAsNonRoot: true cannot express - "
            "the pod spec states no uid and the node reports the container "
            "running as 0"
        )
    if reported is not None:
        return (
            f"the pod spec pins no uid, so this rung cannot be authored without "
            f"guessing; the node reports the target running as uid {reported}, "
            f"and `--target-uid {reported}` starts the walk at this rung"
        )
    return (
        "the target's uid is in neither the pod spec nor the node's container "
        "status (which reports one from Kubernetes 1.33), so this rung cannot "
        "be authored without guessing; re-run with --target-uid once the "
        "capability report below has read it from /proc"
    )


def _attempt_order(
    target_uid: int | None, *, cross_uid: bool, max_rung: Rung | None
) -> tuple[Rung, ...]:
    """The order to try the rungs in, highest-capability first by default.

    :data:`LADDER` stays the *capability* order — it is what ``--max-rung``
    indexes to express "at or below" — and this is the *attempt* order, which
    is the target's to imply. They differ only where the target hands the walk
    a uid it can match.
    """
    if max_rung is None and target_uid and not cross_uid:
        return (Rung.DEGRADED, Rung.FULL, Rung.SEAT)
    return LADDER


@dataclass(frozen=True)
class SeatIdentity:
    """Whether sshd inside the seat can resolve a login name for its own uid.

    The degraded rung runs as the target's uid, discovered at attach time, so no
    debug image can carry an account for it in advance. Without one ``getpwuid``
    fails, which kills ``ssh-keygen`` before a host key exists and would make
    sshd refuse the login even if one did. Everything ``kubectl exec`` reaches
    is unaffected, so this is a property of the *transport*, not of the seat.
    """

    login: str | None
    detail: str
    measured: bool = True
    """False when the seat could not answer the question at all — an image
    older than this launcher, say. "Unknown" and "no" have to stay apart: the
    first must not withhold a stanza that would have worked."""

    @property
    def usable(self) -> bool:
        """Whether an ssh stanza for this seat could work at all."""
        return self.login is not None

    @property
    def refused(self) -> bool:
        """The seat itself said it has no login identity."""
        return self.measured and self.login is None


@dataclass(frozen=True)
class Session:
    """A landed seat: where it is, what it is allowed to do, and what it cost."""

    seat: ContainerRef
    workload: str
    rung: Rung
    """What the seat *is*, measured in it by :func:`probe_seat_credentials`.

    Not what the walk asked admission for, which is on the :class:`LadderStep`
    that landed and belongs there: a rung is a claim about a running container
    and the two come apart in both directions (issue #94). It falls back to the
    authored rung only where the measurement could not be made - the seat would
    not say what it is running as, or the target's own ids were never read -
    and :attr:`rung_measured` is what :func:`format_session` says that with,
    on the line rather than quietly.
    """

    reused: bool
    rung_measured: bool = False
    """Whether :attr:`rung` is the seat's own measurement or the walk's request.

    Its own field because the two are indistinguishable once reduced to a
    label, and the report has to say which it is printing. False covers both
    ways the measurement is unavailable: a seat that would not report its
    ``/proc/self/status``, and a target whose ids were never read - the second
    of which is the ordinary case under ``--no-probe`` against a workload
    pinning ``runAsUser`` and no ``runAsGroup``.
    """

    siblings: tuple[str, ...] = ()
    """The pod's other workload containers, which this seat did not enter.

    Carried on the session rather than looked up when the report is laid out,
    because the two containers are chosen at different times: an ``attach`` that
    reconnects takes the workload from the seat's own ``targetContainerName``,
    and the siblings have to be the siblings of *that* one rather than of
    whatever this run would have picked.

    Empty is "none", and it is also what a dev pod's session says: its sidecar
    is a container of that pod too, but ``--target`` is ``attach``'s flag and
    naming it under ``podbench dev`` would offer a remedy that does not exist.
    """

    uid: int | None = None
    """``runAsUser`` as actually pinned in the container's securityContext —
    ``None`` when the seat rung pinned nothing and the image's own user wins."""

    gid: int | None = None
    """``runAsGroup`` as actually pinned, ``None`` when the image's group wins.

    Its own field rather than a detail of :attr:`uid`, because the two are
    pinned separately and the kernel checks them separately: a manifest stating
    ``runAsUser`` and no ``runAsGroup`` lands a seat with this ``None``, running
    at the debug image's gid 0 against a target in another group, and
    ``__ptrace_may_access()`` denies on the group half alone (#103)."""

    home: str | None = None
    """``$HOME`` as pinned in the container's env, ``None`` for the image's own.
    :func:`seat_layout` derives every path the ProxyCommand names from it."""

    owner: str | None = None
    """The cluster identity recorded on the seat, ``None`` when it records none.

    The seat's, not the caller's: on a reconnect this is what the container was
    stamped with, which is the fact the report has to state rather than the
    name of whoever is reading it."""

    identity_mounted: bool = False
    """Whether the seat mounts :data:`podbench.model.SEAT_IDENTITY_VOLUME`.

    Only ever true of a seat some *other* container spec put it there — a
    ``podbench dev`` sidecar, or a seat landed by a podbench old enough to have
    authored the mount onto an ephemeral container, which the API server has
    refused since always. Read back from the spec rather than remembered."""

    identity_declared: bool = False
    """Whether the *pod* declares :data:`podbench.model.SEAT_IDENTITY_VOLUME`.

    Which is not the same question, and after the ``subPath`` refusal it is the
    interesting one: the volume is there, the user deployed it on purpose, and
    ``attach`` still cannot project it. :func:`features` says so rather than
    leaving them to wonder why ssh is unavailable on a pod they prepared."""

    hotfixed: bool = False
    """Whether the pod this seat is in carries the hotfix layout.

    Read once in :func:`attach` and carried, because :func:`features` is handed
    a ``Session`` and never the pod json — and the two questions it changes are
    both asked there. Defaulted so a ``Session`` built in a test states only
    what it is about.
    """

    probes: tuple[ProbeBudget, ...] = ()
    """The target's probes, and the deadline each puts on a pause.

    Empty means "no probes", not "not looked at", and both callers make that
    true: ``attach`` reads them from the pod it already fetched, and a dev pod
    has none by construction, since :func:`podbench.spec.dev_pod_spec` strips
    all three. So :func:`features` can say "no deadline" from an empty tuple
    without qualifying it."""

    steps: tuple[LadderStep, ...] = ()
    report: CapabilityReport | None = None
    warnings: tuple[str, ...] = ()
    ssh: SeatIdentity | None = None
    """What the seat answered about its own login identity; ``None`` when it was
    never asked."""

    seat_version: str | None = None
    """The build of podbench running in the seat, ``None`` when unreadable.

    Measured, never derived from the image tag: the tag is what was *asked*
    for, and on ``IfNotPresent`` over a tag that moves the node may have served
    something else entirely. :func:`seat_version_fact` renders it.
    """

    credentials: Credentials | None = None
    """The seat's own ``/proc/self/status``, ``None`` when it would not say.

    The evidence behind :attr:`rung`, kept rather than reduced to it, because
    the report cites it and two decisions turn on the uid alone."""

    headroom: Headroom | None = None
    """How much of the pod's memory ceiling was free when the seat landed.

    ``None`` where nothing looked - a ``podbench dev`` pod, whose sidecar has
    limits of its own and no ceiling to share - which is not the same as a
    :class:`podbench.resize.Headroom` whose halves are ``None``. The report
    prints the row only for the first kind of caller, so a mode the question
    does not apply to is not answered "unmeasured"."""

    @property
    def pod(self) -> PodRef:
        return self.seat.pod

    @property
    def root_seat(self) -> bool:
        """Whether this seat runs as uid 0, which decides sshd's layout and login.

        Measured where the seat could be read, and taken from the pin
        otherwise. Never from the rung, which was only ever a proxy for it: the
        full rung is the one that writes ``runAsUser: 0``
        (:func:`podbench.spec._rung_security_context`), so the pin already says
        everything the label did - and says it about a seat whose label
        admission rewrote, which is the seat that made this a measurement.
        """
        if self.credentials is not None and self.credentials.uid is not None:
            return self.credentials.uid == 0
        # Compared against 0 rather than tested for truth: `self.uid or ...`
        # reads a seat pinned to uid 0 - the root seat - as one that pinned
        # nothing.
        return self.uid == 0


def attach(
    kubectl: Kubectl,
    pod_reference: str,
    *,
    target: str | None = None,
    image: str = DEFAULT_IMAGE,
    public_key: str | None = None,
    target_uid: int | None = None,
    target_gid: int | None = None,
    max_rung: Rung | None = None,
    mounts: Sequence[str] = (),
    force_new: bool = False,
    correct_ids: bool = True,
    seat_identity: bool = True,
    pull_policy: str = DEFAULT_PULL_POLICY,
    probe: bool = True,
    timeout: float = 120.0,
    poll_interval: float = 0.5,
) -> Session:
    """Ensure a podbench container is running in ``pod_reference`` and probe it.

    "Ensure", never "create": a still-running podbench container is reused,
    because ephemeral containers cannot be removed and a launcher that appended
    one per attach would grow the pod spec until the API server refused it
    (report 4.2). ``force_new`` is the deliberate override, and it takes the
    next free name rather than the idle-looking one.

    ``mounts`` are ``CLAIM:MOUNTPATH`` requests for volumes the pod already
    carries; they are resolved before anything is submitted, so a claim the pod
    does not have refuses without burning a container name. See
    :func:`resolve_mounts` for why podbench cannot simply add the volume.

    ``max_rung`` caps the walk: the rungs above it are skipped and the ones
    below still tried. It is for the cluster whose policy *mutates* rather than
    refuses, where the full rung is admitted and silently stripped of the
    capability it exists for (issue #94) — a dry run reads that back before a
    name is spent, and this is how somebody who already knows saves the round
    trip. A running seat above the ceiling is not reused, because an ephemeral
    container's securityContext is fixed for the pod's lifetime.

    The rung on the returned session is *measured*, not planned: every attach
    reads the seat's own ``/proc/self/status`` (:func:`probe_seat_credentials`),
    ``probe`` or not. What the walk asked for is on the ladder steps, which is
    where a question about what podbench did belongs.

    ``target_uid`` and ``target_gid`` override what the pod spec says, for the
    manifest that states neither or only one of them. They are *pins*: an id
    given here is not corrected against the measurement, because a flag the
    launcher quietly overrode would be worse than a wrong seat.

    ``correct_ids`` is what makes the gid right without a flag at all. The
    manifest usually states ``runAsUser`` and no ``runAsGroup``, so the seat
    lands in the debug image's group 0 against a target in its image's own
    group, and ``__ptrace_may_access()`` compares the group ids as peers of the
    user ids - the seat can log in and cannot trace (#103, measured on
    p47-blueapi-0: seat 1000:0, target 1000:1000). Only the seat can read the
    truth, from the target's world-readable ``/proc/<pid>/status``, and only a
    *new* container can act on it, since an ephemeral container's
    securityContext is fixed for its lifetime. So podbench lands the corrected
    seat itself, **once**: :func:`id_correction` decides, the second attach
    carries ``correct_ids=False``, and :func:`running_seat` finds the corrected
    container on every later attach instead of landing another. A manifest that
    states both ids costs one name as it always did; one that states only the
    uid costs two, and only ever two. ``probe=False`` measures nothing, so it
    corrects nothing either.

    ``seat_identity`` mounts the pod's home volume when it has one
    (:func:`seat_identity_mounts`). It is on by default and has no effect on a
    pod that declares none, which is the point: the volume can only be there
    deliberately, so its presence is the request and there is nothing for the
    user to remember. ``--no-seat-identity`` turns it off for the case where the
    seat is wanted with nothing of the pod's mounted into it.

    The pod's identity volume is *never* mounted here, however deliberately it
    was declared: it takes a ``subPath`` per file and an ephemeral container may
    not have one. Where the pod declares it, the capability report says so and
    names the seat's own registration as the live-pod route to the same identity,
    so that a pod prepared for podbench does not read as one whose preparation
    failed.

    The seat is always asked whether it has that identity, ``probe`` or not:
    ``probe`` governs the capability report, while this decides whether an ssh
    stanza is worth printing at all, and a stanza that cannot work is the one
    output worse than none.
    """
    pod = resolve_pod_name(pod_reference)
    pod_json = kubectl.get_pod(pod)
    workload = target_container_name(pod_json, target)
    warnings: list[str] = []
    # Read before the ladder is planned, because it decides whether the ladder
    # runs at all: Iterate mode's seat is an ordinary sidecar, so nothing
    # walking `spec.ephemeralContainers` finds it, and an attach on a dev pod
    # used to land a second seat beside the pod's own - spending a permanent
    # name to get a strictly worse view, since the sidecar is the container the
    # application was relaunched *from*.
    sidecar = dev_seat(pod_json)
    volume_mounts, mount_warnings = resolve_mounts(pod_json, workload, mounts)
    warnings.extend(mount_warnings)
    if seat_identity:
        convention, convention_warnings = seat_identity_mounts(pod_json, workload)
        warnings.extend(convention_warnings)
        volume_mounts = _merge_mounts(volume_mounts, convention)
    # Not gated on `seat_identity`, which is about the seat's *home*. This is
    # about the seat resolving the same code as the application, and the two
    # have no reason to be turned off together. An explicit `--mount` of the
    # same path still wins, through `_merge_mounts`.
    claim, claim_warnings = hotfix_claim_mounts(pod_json, workload)
    warnings.extend(claim_warnings)
    volume_mounts = _merge_mounts(volume_mounts, claim)
    identity_declared = SEAT_IDENTITY_VOLUME in declared_volumes(pod_json)

    manifest_uid, manifest_gid = target_uid_gid(pod_json, workload)
    wanted_uid = target_uid if target_uid is not None else manifest_uid
    # The gid the rung is measured against, which is a different question from
    # the gid to *pin*: `wanted_ids` below deliberately ignores the manifest,
    # because pinning off it would reject a running full-rung seat, while a
    # comparison has to use every number there is.
    wanted_gid = target_gid if target_gid is not None else manifest_gid
    # Only when a *gid was asked for* - by flag, or by the correction below -
    # and never off the manifest alone. A pod stating both ids would otherwise
    # make this reject a running full-rung seat, which pins uid 0 and no group
    # by construction and is the best seat there is. The rule enforced here is
    # "honour the ids the caller named", which is the rule `--max-rung` gets,
    # and a manifest names nothing.
    #
    # `wanted_uid` truthy rather than merely not None: uid 0 is the one pin no
    # rung below full ever writes, so filtering on (0, gid) would reject every
    # seat that could exist and land a fresh one on every attach.
    wanted_ids = (
        (wanted_uid, target_gid) if target_gid is not None and wanted_uid else None
    )
    # One call per run, cached on the Kubectl: it decides which seat is offered
    # and it is stamped on whatever gets landed, so both halves have to be the
    # same answer.
    owner = kubectl.whoami()
    # The pod's *own* seat comes first where there is one. Not a preference
    # between two equivalent seats: in a dev pod the workload container is idled
    # and the application runs as a child of the sidecar, so an ephemeral seat
    # beside it would attach to a container with nothing in it. The ids and the
    # ceiling below still apply, and `--new` still lands one - which is the case
    # this is a preference rather than a refusal for, since the sidecar gives up
    # SYS_PTRACE along with the root it does not have.
    existing = sidecar if sidecar is not None and sidecar.running else None
    if existing is None:
        existing = running_seat(pod_json, ids=wanted_ids, owner=owner)
    # Held apart from `existing`, which the ceiling check below clears: the ssh
    # key is measured after the seat's credentials are read, and by then the
    # only thing that matters is whether a reconnect happened.
    reused_seat: SeatInfo | None = None
    declined: str | None = None
    if existing is None and wanted_ids is not None and correct_ids:
        # Said only on the path a human asked for: the correction below runs
        # with `correct_ids=False` and carries its own one-line account of the
        # name it is spending, and two warnings for one event is how a report
        # becomes mostly warnings.
        stale = running_seat(pod_json, owner=owner)
        if stale is not None:
            declined = (
                f"{stale.name} is running but was not reused because it is "
                f"pinned to {_ids(stale.uid, stale.gid)}, not the "
                f"{_ids(wanted_uid, target_gid)} asked for: an ephemeral "
                "container's securityContext is fixed for the pod's lifetime, "
                "so a new container is being landed - and its name is spent "
                "whether or not it works out"
            )
    if existing is None and declined is None:
        # The other reason a running seat is passed over. One line and one
        # only, which is why it shares `declined` with the ids case: a user who
        # is landing a second seat needs the reason, not both candidate reasons.
        theirs = other_owners_seat(seats(pod_json), owner)
        if theirs is not None:
            declined = OTHER_OWNER_WARNING.format(seat=theirs.name, owner=theirs.owner)
    if declined is not None:
        warnings.append(declined)
    if existing is not None and max_rung is not None:
        above = above_ceiling(existing, max_rung, target_uid=wanted_uid)
        if above is not None:
            # A ceiling is about the securityContext, which is fixed for an
            # ephemeral container's lifetime: there is no reconnecting into a
            # different one. Reusing the seat here would silently ignore the
            # flag - and the seat it would reuse is, on the cluster this flag
            # exists for, exactly the root seat the user is trying to get away
            # from.
            warnings.append(
                f"{existing.name} is running but was not reused because "
                f"{above}. --max-rung {max_rung.value} cannot be honoured by "
                "reconnecting, since an ephemeral container's securityContext "
                "is fixed for the pod's lifetime, so a new container is being "
                "landed - and its name is spent whether or not it works out"
            )
            existing = None
    if existing is not None and not force_new:
        # The reused seat's own target, not this run's: an ephemeral container's
        # targetContainerName is fixed when it is created, so a reconnect that
        # was given a different `--target` is still in the first one - and the
        # siblings named beside it have to be that container's.
        reconnected = existing.target or workload
        session = Session(
            seat=ContainerRef(PodRef(kubectl.namespace, pod), existing.name),
            workload=reconnected,
            siblings=other_containers(pod_json, reconnected),
            rung=existing.rung,
            reused=True,
            uid=existing.uid,
            gid=existing.gid,
            home=existing.home,
            identity_mounted=existing.identity_mounted,
            owner=existing.owner,
            steps=(
                LadderStep(
                    existing.rung,
                    True,
                    f"reconnected to the running container ({existing.detail})",
                    existing.name,
                ),
            ),
        )
        # Said first, because it is the one that changes what the other lines
        # mean: this seat is Iterate mode's, which is a different contract from
        # the one `attach` usually hands back.
        if existing.kind is SeatKind.DEV:
            warnings.append(DEV_SIDECAR_REUSED_NOTE.format(seat=existing.name))
        # The key is measured below, once the seat's own uid has been read:
        # `seat_layout` needs it to know which authorized_keys file to cat.
        reused_seat = existing
        if mounts:
            warnings.append(
                "reconnected to an existing container: an ephemeral container's "
                "volumeMounts are fixed when it is created and cannot be added "
                "to, so --mount only takes effect on a seat landed with --new"
            )
        if existing.in_hotfixed_pod:
            warnings.append(UNMOUNTED_HOTFIX_NOTE)
        # The same admission facts a first attach reports. They were produced
        # only inside the walk, and a reconnect is the common case - somebody
        # who attaches on Monday and reconnects all week was never told the
        # seat had lost a capability. No second dry run: what landed is better
        # evidence than what would land, and it is already in `pod_json`.
        landed = ephemeral_container(pod_json, existing.name)
        if landed is not None:
            warnings.extend(_admission_warnings(requested_seat_spec(landed), landed))
    else:
        # Asked of the mounts about to be authored rather than of the seat about
        # to land, because the seat does not exist yet and this is the last
        # moment the answer can be acted on: an ephemeral container's
        # volumeMounts are fixed once it is created.
        if is_hotfixed(pod_json) and not shares_workload_volume(
            pod_json, {"volumeMounts": volume_mounts}, workload
        ):
            warnings.append(UNMOUNTED_HOTFIX_NOTE)
        session = _walk_ladder(
            kubectl,
            pod=pod,
            pod_json=pod_json,
            workload=workload,
            image=image,
            public_key=public_key,
            owner=owner,
            target_uid=target_uid,
            target_gid=target_gid,
            max_rung=max_rung,
            volume_mounts=volume_mounts,
            pull_policy=pull_policy,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    # Both branches above set `rung` to what was *asked* for - the walk's plan,
    # or the label `rung_of_spec` reads off a container somebody else authored.
    # The seat is right there, so ask it. One read settles both directions of
    # issue #94: a stripped full rung stops being reported as the degraded one,
    # and a reconnect stops inheriting a spec's answer to a question only the
    # kernel can settle.
    credentials = probe_seat_credentials(kubectl, session.seat)
    measured = _measured_rung_of(
        credentials, target_uid=wanted_uid, target_gid=wanted_gid
    )
    session = replace(
        session,
        credentials=credentials,
        rung=measured or session.rung,
        rung_measured=measured is not None,
    )
    session = replace(session, steps=_relabel_reconnect(session))

    # Here and not in the reconnect branch above: the file's path depends on the
    # seat's uid, which the read two lines up is the only measurement of.
    if reused_seat is not None and public_key is not None:
        key_note = reconnect_key_note(
            kubectl, session, public_key, dev=reused_seat.kind is SeatKind.DEV
        )
        if key_note is not None:
            warnings.append(key_note)

    # Before the OOM warning, because it is about which *code* is running and
    # every other line in the report is only true of the version that is.
    seat_version = probe_seat_version(kubectl, session.seat)
    if seat_version is None:
        # The guess, and only where there is nothing better: `moving_tag_note`
        # reasons from the tag string, so it can neither confirm a skew nor
        # rule one out, and printing it next to a measurement would be two
        # answers to one question.
        stale = moving_tag_note(image, pull_policy)
        if stale is not None:
            warnings.append(stale)
    elif not same_build(seat_version, __version__):
        warnings.append(VERSION_SKEW_WARNING)
    # Measured, not feared. The unconditional memory warning this replaces was
    # written against an assumption that 2026-08-19 falsified on DLS p47: ten
    # live seats cost 13-23 MiB against 170-3858 MiB of pod headroom, three
    # seats to a pod, no OOM in any of them. What decides is the headroom and
    # not the container's limit - the beamline's three smallest limits are in
    # its roomiest pod - and where the headroom cannot be read the report says
    # unmeasured on its own row rather than warning anyway.
    headroom = measure_headroom(kubectl, pod, pod_json)
    memory_note = headroom_note(
        headroom, needed=SEAT_HEADROOM, template=MEMORY_HEADROOM_WARNING
    )
    if memory_note is not None:
        warnings.append(memory_note)
    # Read from the pod spec rather than warned about in general terms: every
    # number is already in hand, so this is the deadline on *this* pod and not
    # a caution about probed pods. It is not a warning of its own, though: the
    # deadline qualifies the "live attach" tick, and `probe_qualifier` states it
    # on that line - a WARNING block repeating it was the longest thing this
    # verb printed and the one most reliably skipped.
    budgets = probe_budgets(pod_json, session.workload)
    # Not also a warning: `features` reports it under "supports", which is where
    # "what this seat can do and which mechanism decided" belongs, and
    # `ssh_unavailable_note` prints the way out in place of the stanza. A third
    # copy in the warning block would be the only thing said three times. The
    # same goes for the declared-but-unusable identity volume, which the ssh
    # seat's line carries for exactly this reason.
    session = replace(
        session,
        headroom=headroom,
        # Carried rather than re-read: `features` is handed a Session and never
        # the pod, and this is the last place both are in hand. It changes two
        # rows of the report, and #179 is what they said without it.
        hotfixed=is_hotfixed(pod_json),
        identity_declared=identity_declared,
        probes=budgets,
        seat_version=seat_version,
        ssh=probe_ssh_identity(kubectl, session.seat),
    )

    if probe:
        report, probe_warnings = run_capreport(kubectl, session.seat)
        session = replace(session, report=report)
        warnings.extend(probe_warnings)
        # The target's ids as the seat *measured* them, in place of the
        # manifest's. The rung below full turns on whether the seat's uid and
        # gid match the target's, and a manifest that pins no `runAsUser` -
        # which is every pod on argus - leaves `wanted_uid` None, so a root seat
        # beside a root target was named `seat` here while the seat's own
        # start-up report, which reads the target's `/proc/<pid>/status`, makes
        # it `degraded`. The gid half is the same reading of the same file, and
        # it is the only place the group lives when the manifest states
        # `runAsUser` and no `runAsGroup` (p47-blueapi-0). One seat, one label
        # (issue #94).
        if report is not None and report.target_uid is not None:
            # Only where it measured something. A capreport too old to report
            # the target's gid answers `None` here, and taking that over the
            # manifest's answer would replace a measurement with an absence.
            remeasured = _measured_rung_of(
                credentials,
                target_uid=report.target_uid,
                target_gid=report.target_gid,
            )
            if remeasured is not None:
                session = replace(session, rung=remeasured, rung_measured=True)
                session = replace(session, steps=_relabel_reconnect(session))
        # Never off a sidecar. The correction exists because an ephemeral
        # container's securityContext is fixed for its lifetime, so the only way
        # to change a seat's ids is to land another one - and landing an
        # ephemeral seat in a dev pod to correct the *sidecar's* ids fixes
        # nothing, since it is the sidecar the application runs under. A dev
        # pod's ids are corrected by authoring it again.
        correction = (
            id_correction(report, pinned_uid=target_uid, pinned_gid=target_gid)
            if correct_ids and not _is_sidecar(session, sidecar)
            else None
        )
        if report is not None and correction is not None:
            corrected_uid, corrected_gid = correction
            corrected = attach(
                kubectl,
                pod_reference,
                target=target,
                image=image,
                public_key=public_key,
                target_uid=corrected_uid,
                target_gid=corrected_gid,
                max_rung=max_rung,
                mounts=mounts,
                seat_identity=seat_identity,
                pull_policy=pull_policy,
                probe=probe,
                timeout=timeout,
                poll_interval=poll_interval,
                # Once, and the recursion is the whole guard: this call cannot
                # ask for a third seat however wrong the second one turns out to
                # be. A seat that still disagrees is reported instead - the
                # GID_MISMATCH arm names it and `--target-gid` pins it by hand.
                correct_ids=False,
            )
            return replace(
                corrected,
                warnings=(
                    ID_CORRECTION_WARNING.format(
                        first=session.seat.container,
                        was=_ids(report.self_uid, report.self_gid),
                        now=_ids(corrected_uid, corrected_gid),
                    ),
                    *corrected.warnings,
                ),
            )

    # Last, because it is the one read that wants the seat to have finished
    # starting: the agent writes its report after `ensure_all`, and everything
    # above has just spent several round trips in the container. A report that
    # is not there yet costs the same nothing it cost before it existed.
    warnings.extend(startup_warnings(kubectl, session.seat))
    return replace(session, warnings=(*session.warnings, *warnings))


ID_CORRECTION_WARNING = (
    "{first} ran as {was} against a target measured at {now}, and "
    "__ptrace_may_access compares group ids as well as user ids - so a "
    "corrected seat was landed at {now} and {first}'s name is spent. "
    "--no-correct-ids keeps the first seat; `--target-gid <gid>` pins the "
    "group up front and costs one name instead of two"
)
"""One line, and the only one this event gets.

The reader has just had a permanent container name spent on their behalf, so it
says which name, what was wrong with it, and the two flags that make the choice
theirs next time. The mechanism - why six ids and not three - belongs to
:attr:`podbench.model.Blocker.GID_MISMATCH`, which the same report prints
whenever the correction could not be made.

Why a correction costs a second name rather than editing the first - an
ephemeral container's ``securityContext`` is fixed for the pod's lifetime - is
in ``docs/how-to/attach-to-a-pod.md``. The line states that it was spent."""


def id_correction(
    report: CapabilityReport | None,
    *,
    pinned_uid: int | None = None,
    pinned_gid: int | None = None,
) -> tuple[int, int] | None:
    """The ``(uid, gid)`` a corrected seat should pin, or ``None`` for none needed.

    The measurement is the seat's own: ``self_uid``/``self_gid`` are what the
    kubelet actually gave it, ``target_uid``/``target_gid`` come from the
    target's world-readable ``/proc/<pid>/status``. Comparing those two rather
    than the authored spec is deliberate - it also catches a mutating admission
    policy that rewrote what podbench asked for (#94), which reading the spec
    back cannot.

    Four ``None`` results, each for its own reason:

    * **an id the caller pinned.** ``--target-uid``/``--target-gid`` are
      instructions, and a launcher that quietly overrode one would be the
      ``--max-rung`` mistake again. A pin is carried into the comparison, so
      pinning the *right* value stops the correction too.
    * **a seat that reports no gid of its own.** An image older than this
      question sends ``self_gid: null``, and "unknown" must not be read as gid 0
      and spent a container name on.
    * **a root target.** No rung below ``full`` pins uid 0, so there is no
      corrected seat to land; the ladder already says so.
    * **a root seat.** The ``full`` rung is root by construction and holds
      CAP_SYS_PTRACE, which is exempt from the credential check entirely -
      "correcting" it would trade a capability for a group.

    >>> from podbench.model import Blocker, CapabilityReport, Lsm, Verdict
    >>> def report(**kw):
    ...     return CapabilityReport(
    ...         verdict=Verdict.NONE, blocker=Blocker.GID_MISMATCH,
    ...         cap_sys_ptrace=False, cap_bounding_sys_ptrace=False,
    ...         yama_scope=0, seccomp_mode=0, no_new_privs=False,
    ...         lsm=Lsm.NONE, lsm_label=None, target_pid=1, **kw)
    >>> id_correction(report(self_uid=1000, self_gid=0,
    ...                      target_uid=1000, target_gid=1000))
    (1000, 1000)
    >>> id_correction(report(self_uid=1000, self_gid=1000,
    ...                      target_uid=1000, target_gid=1000)) is None
    True
    >>> id_correction(report(self_uid=1000, self_gid=0,
    ...                      target_uid=1000, target_gid=1000),
    ...               pinned_gid=0) is None
    True
    >>> id_correction(report(self_uid=1000, self_gid=None,
    ...                      target_uid=1000, target_gid=None)) is None
    True
    """
    if report is None or report.self_gid is None:
        return None
    if report.self_uid == 0:
        return None
    uid = pinned_uid if pinned_uid is not None else report.target_uid
    gid = pinned_gid if pinned_gid is not None else report.target_gid
    if not uid or gid is None:
        return None
    if (report.self_uid, report.self_gid) == (uid, gid):
        return None
    return uid, gid


def _ids(uid: int | None, gid: int | None) -> str:
    """``uid:gid`` for a report line, with ``?`` for an id nobody measured.

    One helper because the pair is printed in three places and a bare number
    that silently meant "unknown" is what the gid half of this was for.
    """
    return f"{uid if uid is not None else '?'}:{gid if gid is not None else '?'}"


def _walk_ladder(
    kubectl: Kubectl,
    *,
    pod: str,
    pod_json: Mapping[str, Any],
    workload: str,
    image: str,
    public_key: str | None,
    owner: str | None,
    target_uid: int | None,
    target_gid: int | None,
    max_rung: Rung | None,
    volume_mounts: Sequence[Mapping[str, Any]],
    pull_policy: str,
    timeout: float,
    poll_interval: float,
) -> Session:
    uid, gid = target_uid_gid(pod_json, workload)
    if target_uid is not None:
        uid = target_uid
    if target_gid is not None:
        gid = target_gid
    cid = container_id(pod_json, workload)
    name = next_container_name(pod_json, CONTAINER_BASE)
    steps: list[LadderStep] = []
    warnings: list[str] = []

    plan = plan_ladder(pod_json, workload, target_uid=target_uid, max_rung=max_rung)
    # Every pre-skip up front, and in ladder order rather than attempt order: a
    # rung the plan ruled out is a fact about the target whichever rung lands,
    # and the walk returns as soon as one does. Recorded where it is decided,
    # the reason survives; recorded where it is reached, it is lost the moment a
    # lower rung is tried first.
    steps.extend(
        LadderStep(rung, False, skip)
        for rung, skip in sorted(plan, key=lambda item: LADDER.index(item[0]))
        if skip is not None
    )

    for rung, skip in plan:
        if skip is not None:
            continue
        try:
            spec = ephemeral_container_spec(
                name=name,
                image=image,
                rung=rung,
                target_container=workload,
                target_container_id=cid,
                # The full rung is root by construction; handing it the target's
                # uid as well is the combination spec.py refuses to author.
                target_uid=None if rung is Rung.FULL else uid,
                target_gid=None if rung is Rung.FULL else gid,
                env=_container_env(
                    pod_json,
                    public_key,
                    rung,
                    workload=workload,
                    home=_seat_home(volume_mounts, rung),
                    owner=owner,
                ),
                volume_mounts=volume_mounts,
                # The target's own, never RuntimeDefault by default: a filter
                # the workload does not have is one podbench inflicted, and a
                # node whose RuntimeDefault denies ptrace turns it into the
                # blocker for a rung that has no capability to fall back on.
                seccomp_profile=target_seccomp_profile(pod_json, workload),
                pull_policy=pull_policy,
            )
        except InvalidSpecError as error:
            # Refused before the cluster saw it, so no container name is burnt.
            steps.append(LadderStep(rung, False, str(error)))
            continue

        # Before the create, because the currency of being wrong here is a
        # container name burnt for the pod's lifetime (report 4.2) and a dry
        # run costs a round trip.
        # A stripped capability is only worth refusing a rung over if there is a
        # rung below to fall back to. Report 3.11 compares a stripped root seat
        # against one at the *target's own uid*; against a root target that
        # comparison has no second term, because runAsNonRoot cannot express uid
        # 0 and every lower rung is already ruled out. Refusing there leaves the
        # walk with nothing (measured: the dls-ioc e2e fixture, whose target is
        # root, lost its seat entirely).
        fallback_uid = uid if uid else None
        refusal, rewrites = _dry_run_rung(
            kubectl, pod, spec, rung, fallback_uid=fallback_uid
        )
        if refusal is not None:
            steps.append(refusal)
            continue
        warnings.extend(rewrites)

        try:
            kubectl.add_ephemeral_container(pod, spec)
        except KubectlError as error:
            # Any policy engine, not only Pod Security Admission (issue #77). A
            # denial is a verdict on *this rung*, which is the signal the ladder
            # was built to act on; treating a Kyverno refusal as fatal ended the
            # walk in a namespace whose next rung would have been admitted.
            # Nothing is burnt by a refusal - the API server never stored the
            # container - so the next rung reuses the same name.
            if not error.is_admission_denial:
                raise
            steps.append(LadderStep(rung, False, _refusal_detail(error)))
            continue

        try:
            started = kubectl.wait_for_ephemeral_container(
                pod, name, timeout=timeout, poll_interval=poll_interval
            )
        except EphemeralContainerError as error:
            hint = ""
            if _KUBELET_NON_ROOT_MESSAGE in error.message:
                hint = (
                    " The cause is a runAsNonRoot policy over an image whose own "
                    "USER is root; `--target-uid <uid>` pins a non-root uid the "
                    "node will take, once the target's real uid is known."
                )
            steps.append(
                LadderStep(
                    rung,
                    False,
                    f"the kubelet refused it asynchronously ({error.reason}): "
                    f"{error.message}. That container is wedged in waiting for "
                    f"the pod's lifetime and only a pod restart clears it; its "
                    f"name is spent and the next rung takes a fresh one. No dry "
                    f"run could have caught this - dryRun=All runs the admission "
                    f"chain, and the kubelet is not on it (report 3.18).{hint}",
                    name,
                )
            )
            # The name died with the container, so the next rung needs a new one.
            name = next_container_name(kubectl.get_pod(pod), CONTAINER_BASE)
            continue

        steps.append(LadderStep(rung, True, f"running since {started}", name))
        # A rung the walk never reached because a *lower* one was tried first
        # and worked. Said rather than left out: the ladder line is where a
        # reader finds out that the target's own uid, not the cluster, is what
        # chose this seat.
        recorded = {step.rung for step in steps}
        steps.extend(
            LadderStep(
                other,
                False,
                f"not attempted: the {rung.value} rung is the one this target "
                f"implies and the cluster admitted it. `--max-rung "
                f"{other.value}` starts the walk there instead",
            )
            for other in LADDER[: LADDER.index(rung)]
            if other not in recorded
        )
        steps.sort(key=lambda step: LADDER.index(step.rung))
        return Session(
            seat=ContainerRef(PodRef(kubectl.namespace, pod), name),
            workload=workload,
            siblings=other_containers(pod_json, workload),
            rung=rung,
            reused=False,
            # What was *pinned*, not what was asked for: the full rung is root
            # whatever the target is, and the seat rung drops a root target's
            # uid rather than pair it with runAsNonRoot.
            uid=_as_int(as_dict(spec.get("securityContext")).get("runAsUser")),
            gid=_as_int(as_dict(spec.get("securityContext")).get("runAsGroup")),
            home=spec_env(spec).get("HOME"),
            identity_mounted=_mounts_volume(spec, SEAT_IDENTITY_VOLUME),
            owner=spec_env(spec).get(OWNER_ENV),
            steps=tuple(steps),
            warnings=tuple(warnings),
        )

    raise LauncherError(
        "no rung of the capability ladder was admitted:\n"
        + "\n".join(f"  {step.rung.value}: {step.detail}" for step in steps)
    )


CAPABILITY_STRIPPED_WARNING = (
    "admission removed {stripped} from this seat and it was taken anyway: "
    "every rung below pins a non-root uid and this target offers none. "
    "{stripped} would have exempted the seat from Yama and from "
    "PTRACE_MODE_ATTACH, and where the target is root too the uids match "
    "anyway. The `supports` block above is the measurement."
)
"""One line for a strip the walk cannot improve on.

Against a non-root target the same strip *drops* the rung, because the rung
below reads more (report 3.11). Against a root target there is no rung below:
every lower one needs a non-zero uid to pin. Refusing here would end the walk
with nothing admitted, which is worse than a seat that says what it lost —
measured on the ``dls-ioc`` e2e fixture, whose target is root.

It no longer points at the ``ladder`` block for why the target offers no lower
rung, because a reconnect prints this warning too and its ladder block is one
line saying it reconnected. The claim is the same on both paths; only the walk
has somewhere to send the reader for the working.

That the read-only ``/proc`` paths are unaffected by the strip is not stated any
more: the ``supports`` block the last sentence points at ticks them one by one,
and a warning that summarises the block below it is the same measurement twice.

It used to cite §3.11's "three of the six probe paths", which is the wrong half
of that table. §3.11 measured a root seat against a **non-root** target — a uid
mismatch, so ``__ptrace_may_access`` denied three of them — and this line only
ever prints where the ladder found no non-root uid to pin, which is the case
where the uids agree. On argus (hgv27681, 2026-08-20), where no pod sets
``runAsUser`` and every target is root, ``podbench capreport`` measured 6/6 and
the ``supports`` block four lines above this warning said so: the warning
contradicted the report it was printed in, which is the fastest way to teach a
reader to skip both.
"""


def _dry_run_rung(
    kubectl: Kubectl,
    pod: str,
    spec: Mapping[str, Any],
    rung: Rung,
    *,
    fallback_uid: int | None = None,
) -> tuple[LadderStep | None, tuple[str, ...]]:
    """Submit this rung to admission without letting it store anything.

    Returns the step that ends the rung, or ``None`` to go on and create it,
    plus any warnings about what admission would rewrite on the way through.

    The reactive channels the walk already had only fire on a *refusal*. A
    mutating policy issues none: the update succeeds, the ladder drops no rung,
    and the seat that lands is not the seat that was asked for (issue #94). The
    dry run is the only way to see that coming, and the two rewrites it acts on
    rather than merely reports are the two that cost something real - a stripped
    capability, which lands a root seat that reads *fewer* ``/proc`` paths than
    the rung below it (report 3.11), and an added ``runAsNonRoot`` beside no
    uid, which the kubelet turns into a container wedged for the pod's lifetime.

    A dry run that fails for any reason that is *not* a policy verdict is
    ignored, and the real create goes ahead to fail on its own terms: this is a
    pre-flight, and a cluster that will not serve one must not thereby lose the
    ability to take a seat.
    """
    name = _as_str(spec.get("name")) or ""
    try:
        preview = kubectl.add_ephemeral_container(pod, spec, dry_run=True)
    except KubectlError as error:
        if not error.is_admission_denial:
            return None, ()
        return LadderStep(rung, False, _refusal_detail(error, dry_run=True)), ()

    admitted = ephemeral_container(preview, name)
    if admitted is None:
        # Nothing came back to compare against - an API server that ignored the
        # query, or a response this code does not recognise. Not an answer, so
        # not treated as one.
        return None, ()

    wedge = admission_wedges_the_kubelet(spec, admitted)
    if wedge is not None:
        return (
            LadderStep(
                rung,
                False,
                f"{wedge}, which the API server takes and the kubelet then "
                f"refuses seconds later with CreateContainerConfigError - "
                f"burning the container name (report 3.18). A dry run read it "
                f"back before one was spent",
            ),
            (),
        )

    stripped = capabilities_removed(spec, admitted)
    if stripped and fallback_uid is not None:
        return (
            LadderStep(
                rung,
                False,
                f"admission would take it and remove "
                f"{', '.join(stripped)} from it, landing a root seat with no "
                f"capability: that reads three of the six probe paths where the "
                f"rung below, at uid {fallback_uid}, reads all six (report "
                f"3.11). A dry run read that back before a name was spent; "
                f"`--max-rung degraded` says it up front",
            ),
            (),
        )
    # No rung below to prefer, so the stripped seat is the best there is rather
    # than the worst: against a root target `runAsNonRoot: true` cannot express
    # uid 0, so every lower rung is already ruled out, and refusing here would
    # end the walk with nothing admitted at all.
    return None, _admission_warnings(spec, admitted)


def _admission_warnings(
    requested: Mapping[str, Any],
    admitted: Mapping[str, Any],
) -> tuple[str, ...]:
    """What admission did to a seat that was taken anyway, where it cost it.

    One line at most, and only for a strip. The other half of what a mutating
    policy does - the thirteen capabilities both DLS clusters add to a seat that
    asked for none, and every scalar they rewrite alongside - is reported by
    nothing, because by the time this is reached it has cost the seat nothing:
    :func:`admission_wedges_the_kubelet` has already dropped the rung for the
    rewrite that wedges the container, the strip below has its own line, and
    what is left is a stored spec that differs from the request while the seat
    it produced is exactly the seat asked for.

    That was #203's first rule applied to the loudest case in the report: a
    warning fires where the outcome changed, and "here is a difference that cost
    you nothing" is *why podbench is confident this is fine*, which belongs in a
    docstring. The measurement itself is not lost -
    :func:`podbench.spec.admission_rewrites` still computes it, and the rung the
    report prints is read from the seat's own ``/proc/self/status`` rather than
    from the spec admission rewrote, which is the answer the warning was
    standing in for.

    ``requested`` is the spec podbench submitted on the walk, and
    :func:`~podbench.spec.requested_seat_spec`'s reconstruction of it on a
    reconnect, where the request is gone and the seat that landed is not.
    """
    stripped = capabilities_removed(requested, admitted)
    if not stripped:
        return ()
    return (CAPABILITY_STRIPPED_WARNING.format(stripped=", ".join(stripped)),)


def _refusal_detail(error: KubectlError, *, dry_run: bool = False) -> str:
    """One ladder step's account of a synchronous refusal, naming the engine.

    Three engines and three wordings, because the one thing they agree on is
    that they disagree: Pod Security Admission is built in, a webhook is
    announced by the API server's wrapper, and a native
    ``ValidatingAdmissionPolicy`` names itself and its binding. The walk already
    *acts* on all three (``ADMISSION_DENIAL_MARKERS``); until this it reported
    the third as a webhook, which sends the reader looking for a webhook that
    does not exist (issue #93).

    A refusal that allow-lists ``runAsUser`` is the one with an answer, so it
    gets one rather than a relayed paragraph: DLS restricts the uid a
    host-mounting pod may run as, and ``--target-uid`` is how a seat picks one
    it will take.

    >>> from podbench.kubectl import CommandResult
    >>> vap = "ValidatingAdmissionPolicy 'p' with binding 'b' denied request"
    >>> _refusal_detail(KubectlError(CommandResult((), 1, "", vap))).split(":")[0]
    'a ValidatingAdmissionPolicy refused it synchronously'
    >>> uids = 'Allowed runAsUser values are: "36096|37887"'
    >>> refusal = _refusal_detail(KubectlError(CommandResult((), 1, "", uids)))
    >>> "--target-uid 36096" in refusal
    True
    """
    text = f"{error.stderr}\n{error.stdout}"
    if error.is_psa_ptrace_denial or "violates PodSecurity" in text:
        refuser = "Pod Security Admission"
    elif "ValidatingAdmissionPolicy '" in text:
        refuser = "a ValidatingAdmissionPolicy"
    else:
        refuser = "an admission webhook"
    when = "refused the dry run" if dry_run else "refused it synchronously"
    detail = f"{refuser} {when}: {error.stderr.strip() or error.stdout.strip()}"
    allowed = error.allowed_run_as_user
    if allowed:
        uids = ", ".join(str(uid) for uid in allowed)
        detail += (
            f". This cluster allow-lists runAsUser and takes only {uids}, so no "
            f"seat may invent one: re-run with `--target-uid {allowed[0]}` to "
            f"pin a uid it admits"
        )
    return detail


def _seat_home(volume_mounts: Sequence[Mapping[str, Any]], rung: Rung) -> str | None:
    """``$HOME`` for the seat, or ``None`` to leave the image's own alone.

    A mounted home volume wins for every rung, including root's. Everything the
    seat writes otherwise lands in the pod's ephemeral-storage budget, which it
    shares with the workload and cannot reserve (report 3.9) — a vscode-server
    plus extensions is enough to get the whole pod evicted. Where there is no
    volume, a non-root seat still needs *some* writable home the launcher can
    name, because the ProxyCommand spells sshd's config path out in full.
    """
    for mount in volume_mounts:
        if mount.get("name") == SEAT_HOME_VOLUME:
            return _as_str(mount.get("mountPath")) or SEAT_HOME_PATH
    return None if rung is Rung.FULL else NON_ROOT_HOME


def _container_env(
    pod_json: Mapping[str, Any],
    public_key: str | None,
    rung: Rung,
    *,
    workload: str,
    home: str | None,
    owner: str | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {}
    if owner is not None:
        # Who landed this seat, so that the next person on this pod is offered
        # their own rather than handed one whose authorized_keys was written
        # for somebody else's key (issue #113). Written only when the cluster
        # named the user: absence has to keep meaning "unknown", and a locally
        # invented name would be a claim nothing measured.
        env[OWNER_ENV] = owner
    # Which container this seat is in, and what else is in the pod. Both are
    # laptop-side facts - the seat sees namespaces, never the pod object - and
    # `pids` needs them to head its listing with the container whose processes
    # those are, and to name the ones whose processes are not there (finding 15).
    env[TARGET_NAME_ENV] = workload
    env[POD_CONTAINERS_ENV] = ",".join(container_names(pod_json))
    if home is not None:
        # Root already has a writable home the agent's layout knows about; a
        # non-root seat has none, so both halves are pointed at the same one.
        env["HOME"] = home
    if public_key is not None:
        env[PUBKEY_ENV] = public_key.strip()
    spec_block = as_dict(pod_json.get("spec"))
    node = _as_str(spec_block.get("nodeName"))
    if node is not None:
        # Yama differs per node by kernel flavour, so a report that cannot name
        # the node cannot explain "attach worked yesterday" (report 3.13/4.5).
        env["PODBENCH_NODE_NAME"] = node
    # Always written, both ways round, because in the seat *absence* has to keep
    # meaning "an older launcher landed me and I do not know" rather than "no".
    # `hostNetwork` is omitempty in the pod object, so a missing key is false.
    env[HOST_NETWORK_ENV] = "true" if spec_block.get("hostNetwork") is True else "false"
    return env


# -- probing the landed seat ------------------------------------------------


def probe_ssh_identity(kubectl: Kubectl, seat: ContainerRef) -> SeatIdentity:
    """Ask the seat which login name sshd will resolve for the uid it runs as.

    ``check=False`` because "there is no account for this uid" is a normal
    answer on the degraded rung, not a transport fault: the agent exits 1 and
    puts the mechanism and the way out on stderr, which is what the capability
    report goes on to print.

    Exit 1 is read strictly, because an image older than this launcher does not
    know the flag and argparse exits 2. That is "unknown", not "no", and the two
    must not be merged: withholding a stanza from a root seat that would have
    worked, on the grounds that the image could not answer a question about it,
    would be a regression dressed as honesty.
    """
    result = kubectl.exec_(
        seat.pod.name, LOGIN_USER_ARGV, container=seat.container, check=False
    )
    login = result.stdout.strip()
    if result.returncode == 0 and login:
        return SeatIdentity(login, f"sshd resolves this seat's uid as {login!r}")
    reason = result.stderr.strip() or result.stdout.strip()
    if result.returncode == 1:
        return SeatIdentity(
            None, reason or "the seat reported no login name and gave no reason"
        )
    return SeatIdentity(
        None,
        "the seat did not answer whether sshd can resolve a login name for the "
        f"uid it runs as, so this was not measured: {reason or 'no output'}",
        measured=False,
    )


def probe_seat_version(kubectl: Kubectl, seat: ContainerRef) -> str | None:
    """Which build of podbench ``seat`` is running, or ``None`` if it would not say.

    Asked over the same ``exec`` channel as everything else the report prints,
    because nothing outside the seat can answer it: an image tag moves under a
    name that does not, ``IfNotPresent`` will not re-check, and a node serving
    a cached layer produces a seat that is simply older with no event anywhere.

    ``check=False`` and every failure folds to ``None``, like
    :attr:`SeatIdentity.measured`: an image predating this flag, or an ``exec``
    the namespace refuses, is a diagnostic that did not arrive. Report 4.2 is
    what makes that non-negotiable - the seat is an ephemeral container and
    cannot be relanded, so nothing here may fail an attach that has landed.
    """
    result = kubectl.exec_(
        seat.pod.name, VERSION_ARGV, container=seat.container, check=False
    )
    version = result.stdout.strip()
    # One token or nothing. A usage message is what an image too old for the
    # flag prints, and click writes it to stdout as readily as to stderr, so a
    # zero exit and non-empty output are not on their own an answer.
    if result.returncode != 0 or len(version.split()) != 1:
        return None
    return version


def probe_seat_credentials(kubectl: Kubectl, seat: ContainerRef) -> Credentials | None:
    """What ``seat`` is really running as, or ``None`` if it would not say.

    The rung the walk authored is what podbench *asked* for, and the spec that
    landed is what admission agreed to store - neither is the container. Both
    were measured to be wrong, in opposite directions and on the same cluster
    (DLS, 2026-08-19): admission adds thirteen capabilities to a seat that
    asked for none, and the seat carrying them runs at uid 37887 with ``CapEff:
    0000000000000000``, because capabilities beside a non-zero ``runAsUser``
    land in ``CapBnd`` and never reach the effective set (report §3.10). This
    reads the two numbers the kernel will check.

    ``check=False`` and every failure folds to ``None``, for
    :func:`probe_seat_version`'s reason: the seat is an ephemeral container and
    cannot be relanded, so no diagnostic may fail an attach that has landed.
    The report then names the rung that was asked for and says that is what it
    is doing.
    """
    result = kubectl.exec_(
        seat.pod.name, SEAT_STATUS_ARGV, container=seat.container, check=False
    )
    if result.returncode != 0:
        return None
    return Credentials.from_status(result.stdout)


def _measured_rung_of(
    credentials: Credentials | None,
    *,
    target_uid: int | None,
    target_gid: int | None,
) -> Rung | None:
    """The rung these credentials put the seat on, or ``None`` for unmeasured.

    ``None`` is every way the comparison could not be made - the seat would not
    say what it is running as, or the target's own ids are not in hand - and it
    is the caller's cue to name the rung that was *asked* for and say so. It is
    never a rung: a label that outran its evidence is issue #94, and one that
    outran half of its evidence is the same defect with a smaller margin.

    The target ids are the pod spec's, or the flag's - what the walk pinned the
    seat to - because they have to be available on an attach that probed
    nothing. ``target_gid`` is routinely ``None`` there: ``runAsUser`` with no
    ``runAsGroup`` is what a hardened workload declares, and the group then
    comes from the image, so it lives only in the target's own ``/proc``
    (:func:`podbench.spec.target_uid_gid`). Where a capreport ran, its measured
    pair replaces both.

    An unknown target *uid* is unmeasured here, which is not what
    :func:`podbench.model.measured_rung` answers on its own.  That function
    names the floor for it "because its caller has the manifest to fall back on
    and says which it used" - and this caller *is* the manifest: ``None`` means
    the pod states no ``runAsUser``, which is every pod on argus, so there is
    nothing behind it to fall back to. Passing the floor off as measured made
    ``ssh-config`` and a ``--no-probe`` attach call a root seat beside a root
    target ``seat``, while a capreport on the same two containers reads
    ``degraded`` - one seat, two labels, which is issue #94 itself.
    :attr:`podbench.model.SeatReport.rung` guards its own reading in these
    words and for this reason, and the capability is exempt in both: it is the
    whole of the full rung and is compared against no target at all.
    """
    if credentials is None:
        return None
    if target_uid is None and not credentials.capabilities.sys_ptrace_effective:
        return None
    return measured_rung(
        credentials.uid,
        sys_ptrace=credentials.capabilities.sys_ptrace_effective,
        target_uid=target_uid,
        gid=credentials.gid,
        target_gid=target_gid,
    )


def startup_warnings(kubectl: Kubectl, seat: ContainerRef) -> list[str]:
    """What the seat's start-up could not do, said to the person attaching.

    Issue #99. ``ensure_all`` records a refused step rather than raising - the
    caller is PID 1 of a container that cannot be restarted - and the record
    went to the container log, which is the place the issue was filed about.
    A refused ``SetEnv`` costs the ssh transport a variable and nothing said
    so: ``c354c90`` made that failure loud *in the log* and no louder anywhere
    else.

    One warning per failure, each already a line, because that is what a
    warning is here. A log that cannot be read is silence, exactly as it was
    before this existed: there is nothing to report and nothing was measured.
    """
    text = kubectl.logs(seat.pod.name, seat.container)
    if text is None:
        return []
    report = SeatReport.from_log(text)
    if report is None:
        return []
    return [f"{seat.container} start-up: {failure}" for failure in report.failures]


def _relabel_reconnect(session: Session) -> tuple[LadderStep, ...]:
    """A reconnect's one ladder line, carrying the rung that was measured.

    The ladder says what the walk *asked* admission for, and on a reconnect the
    walk did not run: the single step is synthesised from the container that
    was found, so its rung was :func:`rung_of_spec`'s reading of a
    securityContext. That is the third of the three labels one seat wore in
    issue #94 - the header said one thing, this line said another, and neither
    was wrong about the question it was answering. A reconnect has only one
    question, so it gets one answer.

    A first attach is left alone: its steps are refusals and a landing, and the
    rung each names is the rung that was submitted.
    """
    if not session.reused or len(session.steps) != 1:
        return session.steps
    return (replace(session.steps[0], rung=session.rung),)


def run_capreport(
    kubectl: Kubectl, seat: ContainerRef
) -> tuple[CapabilityReport | None, list[str]]:
    """Run ``capreport --json`` inside the seat and parse it.

    ``check=False`` is not laziness: capreport's exit code *is* its verdict — 0
    live attach, 10 read-only, 15 launch-only, 20 nothing — so a non-zero exit
    is the normal case on a restricted namespace.
    """
    result = kubectl.exec_(
        seat.pod.name, CAPREPORT_ARGV, container=seat.container, check=False
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, [
            # Worded for both callers: `attach` prints this above a report with
            # no `measured` block, `status` beside a seat whose verdict column
            # therefore says `not probed`, and neither may claim the other's
            # layout.
            "capreport produced no parsable JSON, so nothing was measured: "
            + (result.stderr.strip() or result.stdout.strip() or "no output")
        ]
    if not isinstance(payload, dict):
        return None, ["capreport did not return a JSON object"]
    return capability_report_from_json(cast(dict[str, Any], payload)), []


def capability_report_from_json(payload: Mapping[str, Any]) -> CapabilityReport:
    """Rebuild a :class:`CapabilityReport` from ``capreport --json``.

    That JSON is a declared public interface of :mod:`podbench.probe`, which
    lets the launcher stay a version behind the image without lying: an unknown
    blocker degrades to :attr:`Blocker.UNKNOWN` and says so, rather than
    raising in the middle of a successful attach.
    """
    notes = [str(note) for note in _as_list(payload.get("notes"))]
    try:
        blocker = Blocker(str(payload.get("blocker")))
    except ValueError:
        notes.append(
            f"this launcher does not know the blocker {payload.get('blocker')!r} "
            f"the image reported: {payload.get('explanation')}"
        )
        blocker = Blocker.UNKNOWN
    verdict = _verdict_from(payload.get("verdict"))
    if verdict is None:
        # The sibling of the unknown-blocker note above, and newly load-bearing:
        # this PR added a rung, so a launcher one release behind an image is now
        # a real shape rather than a hypothetical one. The read-only and
        # gdb-launch ticks self-correct because they are recomputed from
        # `proc_reads` and `child_attach_ok`, but the verdict line cannot be
        # recomputed, so it says what it does not know instead of reading
        # `no access to the target process` off a rung it has never heard of.
        notes.append(
            f"this launcher does not know the verdict {payload.get('verdict')!r} "
            f"the image reported ({payload.get('summary')}); shown as no access, "
            "which may understate what the seat can do"
        )
        verdict = Verdict.NONE
    reads = {
        str(key): bool(value)
        for key, value in as_dict(payload.get("proc_reads")).items()
    }
    return CapabilityReport(
        verdict=verdict,
        blocker=blocker,
        cap_sys_ptrace=bool(payload.get("cap_sys_ptrace")),
        cap_bounding_sys_ptrace=bool(payload.get("cap_bounding_sys_ptrace")),
        yama_scope=_as_int(payload.get("yama_scope")),
        seccomp_mode=_as_int(payload.get("seccomp_mode")) or 0,
        no_new_privs=bool(payload.get("no_new_privs")),
        # `lsm` and `lsm_label` are the names since #104; `apparmor_profile`
        # is the name the same string was published under before podbench knew
        # which LSM it had read, and is still what an older seat sends. Falling
        # back to it keeps a current launcher honest about an old image, and
        # `Lsm.NONE` for a seat that never said is the same "older than the
        # question" silence `self_gid` and `attach_method` use.
        lsm=_lsm_from(payload.get("lsm")),
        lsm_label=_as_str(payload.get("lsm_label"))
        or _as_str(payload.get("apparmor_profile")),
        lsm_label_target=_as_str(payload.get("lsm_label_target")),
        self_uid=_as_int(payload.get("self_uid")) or 0,
        target_uid=_as_int(payload.get("target_uid")),
        # Absent from an image older than #103, and `None` has to survive that:
        # read as 0 it would be a mismatch against every non-zero-gid target,
        # and the correction would spend a permanent container name on a seat
        # nobody had measured. `id_correction` declines on `None` for this
        # reason and not out of caution.
        self_gid=_as_int(payload.get("self_gid")),
        target_gid=_as_int(payload.get("target_gid")),
        target_pid=_as_int(payload.get("target_pid")),
        node_name=_as_str(payload.get("node_name")),
        child_attach_ok=_as_bool(payload.get("child_attach_ok")),
        target_attach_ok=_as_bool(payload.get("target_attach_ok")),
        # Absent from an image older than the seize, and `None` has to survive
        # that: `describe_pause` reads it as "not measured" rather than as an
        # assurance that the seat stopped nothing.
        attach_method=_as_str(payload.get("attach_method")),
        proc_reads=reads,
        notes=notes,
    )


def _lsm_from(value: object) -> Lsm:
    """The LSM the seat named, or :attr:`Lsm.NONE` for a seat that named none.

    A seat older than #104 sends no ``lsm`` key at all, and "this image never
    said" is indistinguishable from "no LSM here" as far as the launcher can
    act on it: neither is grounds for reporting a label mismatch, which is the
    only thing the field decides.
    """
    try:
        return Lsm(str(value))
    except ValueError:
        return Lsm.NONE


def _verdict_from(value: object) -> Verdict | None:
    """The named verdict, or ``None`` for one this launcher has never heard of.

    ``None`` rather than a silent :attr:`Verdict.NONE`, so the caller has to
    decide what to say about the gap.
    """
    name = str(value).upper()
    for verdict in Verdict:
        if verdict.name == name:
            return verdict
    return None


# -- the capability report the user reads -----------------------------------


@dataclass(frozen=True)
class Feature:
    """One thing this seat can or cannot do, and the mechanism that decided."""

    name: str
    available: bool
    reason: str = ""
    note: str = ""
    """Printed whether or not the feature is available, unlike ``reason``.

    "This works, and here is what made it work" is worth exactly one line: a
    user who sees ssh land on one pod and not the next has to be able to tell
    the two apart from the report, and the mechanism is not visible in the
    seat itself."""


READS_FEATURE = "read-only inspect (/proc/<pid>/root, maps, environ)"
"""Named once, because the label is a promise about three specific paths and
the tick beside it is now decided by reading exactly those three."""

LAUNCH_FEATURE = "debug launched processes (podbench dbg --launch ./prog)"
"""The rung a denied pod actually keeps, given a line of its own.

It used to be a clause inside the exec-seat line, which claimed it
unconditionally — and it is not unconditional: it needs ptrace(2) to work at
all, which is what the scratch attach measures (report 3.12)."""


def features(session: Session) -> tuple[Feature, ...]:
    """What the landed seat supports, naming the blocker for what it does not.

    Never inferred from the rung alone. The rung is what the *cluster* admitted;
    whether ptrace actually works also depends on Yama, seccomp and AppArmor,
    which are node-local and were measured inside the container (report 4.5).
    """
    report = session.report
    # Qualifies the tick rather than repeating the warning: a bare [x] says the
    # same thing on a pod that will restart under you in twenty seconds as on
    # one that will wait all afternoon, and those are different products. The
    # arithmetic and the way out stay in the WARNING block.
    deadline = probe_qualifier(session.workload, session.probes)
    if report is None:
        unknown = "capreport did not run, so this was not measured"
        return (
            Feature("live attach (gdb -p <pid>)", False, unknown, note=deadline),
            Feature(READS_FEATURE, False, unknown),
            Feature(LAUNCH_FEATURE, False, unknown),
            _iterate_feature(session.hotfixed),
            *_seat_features(session),
        )
    return (
        Feature(
            "live attach (gdb -p <pid>)",
            report.verdict is Verdict.LIVE_ATTACH,
            report.blocker.explanation,
            note=deadline,
        ),
        Feature(
            READS_FEATURE,
            # From the reads themselves, never from the verdict: this label
            # names three specific paths, and deciding it from a predicate that
            # consults none of them is how a seat that could read none of them
            # came to tick it (issue #51).
            report.reads_ok,
            _reads_reason(report),
            # Printed whether or not the box is ticked, so the label and the
            # evidence cannot drift apart again.
            note=report.reads_summary,
        ),
        Feature(
            LAUNCH_FEATURE,
            report.can_debug_launched,
            "the seat could not ptrace even a child it forked itself, so "
            "ptrace(2) is unusable here and gdb cannot trace an inferior it "
            "starts either"
            if report.child_attach_ok is False
            else "the scratch attach was not measured, so this is not claimed",
        ),
        _iterate_feature(session.hotfixed),
        *_seat_features(session),
    )


def _reads_reason(report: CapabilityReport) -> str:
    """Why the read-only box is empty — which is not always a refusal.

    The reads are no longer taken from the verdict, so an empty box no longer
    implies a denied one, and a single unconditional reason made the report
    contradict itself: ``capreport`` with no target pid measures no matrix at
    all and still verdicts ``LIVE_ATTACH`` (no ``PODBENCH_TARGET_CID`` in the
    spec, or no process in a cgroup matching the target's container id), which
    printed an unticked box blaming "the mechanism that refused attach" three
    lines above ``blocker  none``.
    """
    if not report.proc_reads:
        return (
            "podbench capreport resolved no target pid, so these three were "
            "never read - this is not a refusal. `podbench pids` in the seat "
            "names the target, and `podbench capreport <pid>` measures it."
        )
    # Not the blocker's explanation: that paragraph is already printed against
    # live attach, and a report that repeats itself is one people stop reading.
    if report.blocker is Blocker.NONE:
        return (
            "the three paths this line names take PTRACE_MODE_READ, and at "
            "least one of them was refused - the matrix is above"
        )
    return (
        "the three paths this line names take PTRACE_MODE_READ, which the "
        "mechanism that refused attach gates too - see the blocker below"
    )


def _seat_features(session: Session) -> tuple[Feature, Feature]:
    """The seat itself, split by transport.

    One line, "seat (editor, shell, git)", used to be unconditionally true. It
    is not: the ssh half needs sshd to resolve a login name for the seat's uid,
    which on the degraded rung is the target's and may exist nowhere. The exec
    half needs nothing of the sort, and spike S5 found it to be most of that
    rung's value — so the two are reported separately rather than one of them
    quietly speaking for both.
    """
    identity = session.ssh
    usable = identity is not None and identity.usable
    ssh_seat = Feature(
        "ssh seat (Remote-SSH: editor, shell, git, sftp)",
        usable,
        identity.detail
        if identity is not None
        else "the seat was not asked whether sshd can resolve a login name for "
        "the uid it runs as",
        note=_identity_note(session, usable=usable),
    )
    return (
        ssh_seat,
        # `dbg --launch` used to be listed here, under a tick that is always
        # true. Whether it works is measured, and it has its own line now.
        Feature("exec seat (kubectl exec -- podbench capreport, pids, dbg)", True),
    )


def _identity_note(session: Session, *, usable: bool) -> str:
    """Which mechanism gave this seat an NSS identity, or why none could.

    A pod that declares the identity volume is a pod somebody prepared for
    podbench, so a seat with no login on it looks like the preparation failed.
    It did not: ``attach`` cannot project a file into an ephemeral container at
    all, and this line is where that is said - beside the feature it decides,
    with the mechanism that does work on a live pod.

    ``usable`` decides how much of that to say. A seat that already has its
    login needs the fact and not the remedy: printing a re-attach command under
    a ticked box would read as an instruction to fix something that is working.

    That branch says the seat *has* a login and not that it registered one, which
    is the only thing the launcher knows: all it asked for was a name
    (``--print-login-user``), and a seat whose uid the image already has an
    account for - root on the full rung, ``nobody`` on a hardened workload -
    resolves it and appends nothing. Claiming a record in
    :data:`podbench.agent.SEAT_NSS_PATH` sent readers to ``cat`` an empty file.

    Neither branch names a flag. The route on a live pod is the seat's own
    record and it needs none, and where that route failed the reason is
    :data:`podbench.agent.NSS_WAY_OUT`, printed immediately under this line as
    the feature's reason. Saying it twice is the paragraph-length warning the
    report was cut back from.
    """
    if session.identity_mounted:
        return (
            f"identity from the pod's {SEAT_IDENTITY_VOLUME!r} volume, mounted "
            f"read-only over {PASSWD_PATH} - which is why sshd can resolve a "
            "login name for this uid here, and cannot on a pod without it"
        )
    if not session.identity_declared:
        return ""
    cannot = (
        f"this pod declares the {SEAT_IDENTITY_VOLUME!r} volume and attach "
        "cannot use it: projecting it takes a subPath per file, which an "
        "ephemeral container may not have - the API server refuses the whole "
        f"request. Over {PASSWD_PATH} there is no whole-volume alternative "
        "either, because a directory mount replaces the path"
    )
    if usable:
        return (
            f"{cannot}. Nothing here needs fixing: this seat has a login "
            f"already - a live-pod seat that needs one registers its own record "
            f"in {SEAT_NSS_PATH}, and needs neither the volume nor a flag to. "
            "The volume is for a seat that is an ordinary container, which is "
            "what `podbench dev` authors"
        )
    return (
        f"{cannot}. On a live pod the seat registers its own record instead, in "
        f"{SEAT_NSS_PATH}, which asks nothing of the pod - why this seat has no "
        "login even so is the line below. The volume is for a seat that is an "
        "ordinary container, which is what `podbench dev` authors"
    )


def _iterate_feature(hotfixed: bool = False) -> Feature:
    """The relaunch loop, which a pod carrying the hotfix layout *does* support.

    The reason below is right about an ordinary pod and was reported unchanged
    on both p47 pods, where it was false in both of its halves: the supervisor
    is PID 1, so killing the child does not restart the container, and the probe
    is the hold-aware wrapper, which returns 0 while the hold exists. That is
    #179 — a true sentence about a pod other than the one in front of the user.

    Decided from the same spec-derived predicate the mount is
    (:func:`is_hotfixed`), so the tick and the claim in the seat cannot disagree.
    """
    if hotfixed:
        return Feature(
            "iterate (edit, relaunch, verify through the Service)",
            True,
            note="`podbench hotfix apply` relaunches the application's own "
            "child in place: this pod carries the supervisor, so the loop runs "
            "on the live workload without a second pod and without restarting "
            "the container.",
        )
    return Feature(
        "iterate (edit, relaunch, verify through the Service)",
        False,
        "attach shares a live pod, where killing PID 1 restarts the container "
        "and a liveness probe would kill a stopped one. The relaunch loop needs "
        "a sacrificial dev pod (`podbench dev`), never the live workload.",
    )


_TICK = " " * 6
"""Where the note under a ``supports`` tick starts, and stays on a wrap."""

_BUILD_HASH = re.compile(r"(?:^|\.)g([0-9a-f]{7,40})(?:\.|$)")
"""The commit setuptools_scm put in a PEP 440 local segment.

``0.4.0b2.dev24+g87be67bc8.d20260819`` carries two components after the ``+``:
the commit that was packaged, and — only when the tree was dirty — the date
setuptools_scm ran. Only the first says anything about *what* was built.
"""


def _build_key(version: str) -> tuple[str, str] | None:
    """The public version and the commit, or ``None`` where there is no commit."""
    public, _, local = version.partition("+")
    match = _BUILD_HASH.search(local)
    return None if match is None else (public, match.group(1))


def same_build(one: str, other: str) -> bool:
    """Whether two podbench versions were built from the same commit.

    Exact equality was the test, and it made :data:`VERSION_SKEW_WARNING` a
    permanent false positive: on all eight attaches of the 2026-08-19 field
    round the seat answered ``0.4.0b2.dev24+g87be67bc8.d20260819`` against a
    launcher's ``0.4.0b2.dev24+g87be67bc8`` — the same commit, differing by the
    ``.d<date>`` setuptools_scm adds for a dirty tree, which records *when* the
    build happened and not *what* was in it. A warning whose own remedy
    (``--pull always --new``) cannot clear it is one the reader learns to skip,
    which costs the case it exists for.

    >>> same_build("0.4.0b2.dev24+g87be67bc8.d20260819", "0.4.0b2.dev24+g87be67bc8")
    True

    A different commit is still a different build, which is that case:

    >>> same_build("0.4.0b2.dev24+g87be67bc8", "0.4.0b2.dev25+gdeadbeef1")
    False

    Where neither string carries a hash — a release wheel has no local segment
    at all — the whole string is the only thing left to compare, so it is:

    >>> same_build("0.4.0", "0.4.0"), same_build("0.4.0", "0.4.1")
    (True, False)
    """
    if one == other:
        return True
    key = _build_key(one)
    return key is not None and key == _build_key(other)


def seat_version_fact(seat_version: str | None) -> str:
    """The ``version`` row's value: which build the seat is, against this one.

    Both numbers on the one line, because the pair is the fact - a bare
    ``0.4.0b1`` reads as agreement to a reader who does not know what this
    launcher is, and the whole reason to ask is that the two can differ. What a
    difference *costs*, and the way out of it, is
    :data:`VERSION_SKEW_WARNING`'s job; saying it here as well would put one
    thing in two places.

    >>> print(seat_version_fact(None))
    unknown - this seat did not answer `podbench --version`
    """
    if seat_version is None:
        return UNKNOWN_SEAT_VERSION
    if seat_version == __version__:
        return f"{seat_version}, the same build as this launcher"
    # Both numbers still, because they differ and the reader can see that they
    # do; what is asserted is only what `same_build` measured, which is that the
    # difference is setuptools_scm's build date and not the code.
    if same_build(seat_version, __version__):
        return f"{seat_version}, the same commit as this launcher ({__version__})"
    return f"{seat_version} in the seat, {__version__} in this launcher"


def target_row(workload: str, siblings: Sequence[str] = ()) -> list[str]:
    """The ``target`` row: which container was entered, and which not.

    A single-container pod says only the name, because there was nothing else to
    have chosen and a sentence about the default would be a line of report per
    attach saying so. Where there *are* siblings the default stops being
    invisible: each of them is named, with the whole invocation that reaches it
    rather than a description of one, so that the reader is not left to find out
    from ``kubectl get pod -o json`` that podbench picked one of three
    (finding 15).

    The offer is a line of its own and is **never wrapped**, which is why this
    authors finished lines instead of one value for ``paragraph`` to flow. On
    ``p47-epics-gateways-0`` at 80 columns the flow left ``--target`` at the end
    of one line and ``p47-epics-gateways-pva-gateway`` at the start of the next;
    at 100 it broke ``panda8080`` off the same way. A flag parted from its
    argument cannot be pasted, which is the only thing printing it was for.
    :func:`podbench.gdbcmd.listing_heading` prints the same sentence above
    ``pids`` and has never had the defect, because it emits its lines finished.

    The prose half ends in a full stop, which the single-line version did not
    need: once it wraps, its continuation and the offer below it sit at the same
    indent, and without a terminator there is nothing to say which of the two
    the second line belongs to. ``listing_heading`` is short enough never to
    wrap, so the same two sentences there need no stop between them.

    >>> for line in target_row("api"):
    ...     print(line)
    target      api
    >>> for line in target_row("gateway-ca", ["gateway-pva"]):
    ...     print(line)
    target      gateway-ca; this pod also has gateway-pva.
                reach it with `--target gateway-pva`
    """
    indent = " " * 12
    if not siblings:
        return [f"target      {workload}"]
    flags = " or ".join(f"`--target {name}`" for name in siblings)
    reach = "reach it with" if len(siblings) == 1 else "reach one with"
    return [
        *paragraph(
            f"{workload}; this pod also has {and_list(siblings)}.",
            first="target      ",
            indent=indent,
        ),
        f"{indent}{reach} {flags}",
    ]


def format_session(session: Session) -> str:
    """The capability report, which is the product of an attach."""
    lines = [
        f"seat        {session.seat}"
        + ("  (reconnected)" if session.reused else "  (new)"),
        # Authored as finished lines: the prose half wraps under its label, and
        # the flag half must not - see `target_row`.
        *target_row(session.workload, session.siblings),
        # Above the rung, not in `measured` below it: `--no-probe` skips that
        # block entirely, and which build is answering is exactly the thing a
        # user needs when a fix appears not to have worked.
        *paragraph(
            seat_version_fact(session.seat_version),
            first="version     ",
            indent=" " * 12,
        ),
        *paragraph(_owner_fact(session), first="owner       ", indent=" " * 12),
        # Wrapped like the rows above it: the evidence is a sentence's worth of
        # numbers and it now has a second clause, so a hanging indent is what
        # keeps it a row rather than an overrun.
        *paragraph(
            f"{session.rung.value} - {_rung_evidence(session)}",
            first="rung        ",
            indent=" " * 12,
        ),
        "ladder",
    ]
    for step in session.steps:
        mark = "landed " if step.admitted else "refused"
        lines.extend(
            paragraph(
                step.detail,
                first=f"  {step.rung.value:<9} {mark}  ",
                indent=" " * 21,
            )
        )

    lines.append("supports")
    for feature in features(session):
        lines.append(f"  [{'x' if feature.available else ' '}] {feature.name}")
        if feature.note:
            lines.extend(paragraph(feature.note, first=_TICK, indent=_TICK))
        if not feature.available and feature.reason:
            lines.extend(paragraph(feature.reason, first=_TICK, indent=_TICK))

    report = session.report
    if report is not None:
        # The flag rides on the heading because this block is the whole of what
        # it skips, and because a row is where a reader looks for a value: a
        # flush-left label with a value is drawn exactly as the bare heading was
        # (`console._styled`), so naming it costs no layout.
        lines.append("measured    --no-probe skips this block")
        lines.append(f"  verdict     {report.verdict.summary}")
        lines.append(f"  blocker     {report.blocker.value}")
        lines.append(f"  node        {report.node_name or 'unknown'}")
        lines.append(f"  yama        {_yama(report.yama_scope)}")
        # uid *and* gid, because the credential check wants both and a report
        # that showed only the uid is how a seat at 1000:0 against a target at
        # 1000:1000 read as a match for a year (#103).
        # The target's pid rides on this line rather than being left to the
        # notes: every number in this block is a measurement of one process,
        # and a reader who disagrees with the choice cannot say so until the
        # report says which process it measured.
        target_pid = "" if report.target_pid is None else f" (pid {report.target_pid})"
        lines.append(
            f"  ids         seat {_ids(report.self_uid, report.self_gid)}, "
            f"target {_ids(report.target_uid, report.target_gid)}{target_pid}"
        )
        # What the probe cost the workload, said on every attach rather than
        # only when it cost something. The complaint this answers was not the
        # stop but its silence, on pods where a stop is forbidden.
        lines.append(
            f"  pause       "
            f"{describe_pause(report.attach_method, report.target_attach_ok)}"
        )
        # A row, not a warning. It is a measurement of this pod and it belongs
        # with the measurements: an ample margin - which is every pod on the
        # beamline this was calibrated against - is worth a number and not a
        # caution, and a margin that could not be read has to read as
        # *unmeasured* here rather than be passed off as fine by silence.
        if session.headroom is not None:
            lines.append(f"  memory      {session.headroom.summary}")
        if report.notes:
            lines.append("notes")
            for note in report.notes:
                lines.extend(paragraph(note, first="  - ", indent="    "))

    # One line each, hung under the word that is coloured, so the block reads as
    # a list of things to know rather than as an essay to skip. What a warning
    # may no longer do is explain itself: it names the fact and the flag, and
    # the how-to page carries the mechanism.
    for warning in session.warnings:
        lines.extend(
            paragraph(warning, first=f"{WARNING_LEAD}  ", indent=" " * WARNING_HANG)
        )
    return "\n".join(lines)


def _owner_fact(session: Session) -> str:
    """Whose seat this is, or why the report cannot say.

    The two ways an owner comes back unknown have different causes and one of
    them is about *this* run, so they are not merged: a reconnect into an
    unstamped container is history, while a seat landed without a stamp means
    the cluster would not name the kubeconfig's user and the seat this run just
    created will be anonymous to the next person too.
    """
    if session.owner is not None:
        return session.owner
    if session.reused:
        return "unknown - this container was landed before seats recorded one"
    return (
        "unknown - `kubectl auth whoami` did not name this kubeconfig's user, "
        "so this seat records none either"
    )


def _rung_evidence(session: Session) -> str:
    """What the rung on this line is a reading of.

    The numbers rather than a restatement of the label: the line used to end in
    a fixed phrase per rung, so it could not be wrong-aware, and "a pinned UID,
    all capabilities dropped" was printed for a container running as root with
    thirteen capabilities in its stored spec (issue #94, DLS 2026-08-19). It is
    also the only place a ``--no-probe`` attach reports any measurement at all.
    """
    credentials = session.credentials
    if credentials is None or credentials.uid is None:
        # Named as unmeasured rather than passed off as measured. The seat is
        # running - the attach returned - so this is an exec that was refused
        # or an answer this launcher could not parse, and the rung reverts to
        # being the walk's own claim about what it asked for.
        return "as asked for; the seat's own /proc/self/status could not be read"
    described = describe_credentials(
        credentials.uid, credentials.gid, credentials.capabilities.effective_hex
    )
    if session.rung_measured:
        return described
    # The seat was read and the target was not, so the comparison the rung
    # below full *is* was never made. Said rather than left to look measured:
    # `__ptrace_may_access()` wants both pairs, and a target that pins
    # `runAsUser` and no `runAsGroup` keeps its group only in its own /proc -
    # which is the shape `--no-probe` meets on every hardened workload.
    return f"as asked for; the seat is {described}, the target unread"


def _yama(scope: int | None) -> str:
    if scope is None:
        return "absent (no Yama LSM on this node - not the same as scope 0)"
    return str(scope)


# -- ssh client wiring ------------------------------------------------------


def seat_layout(session: Session) -> SshdLayout:
    """The sshd layout the agent will have chosen inside ``session``'s container.

    It has to be derived, not guessed: the ProxyCommand names sshd's config file
    by absolute path, and the two layouts put it in different places. The agent
    settles it inside the container with
    ``SshdLayout.for_uid(os.geteuid())``, so the *uid* is what decides — and
    :attr:`Session.root_seat` answers from the seat's own
    ``/proc/self/status`` where it could be read. Deriving it from the rung
    instead cost a whole transport once: a mutating admission webhook that
    strips ``capabilities.add`` leaves a root seat looking exactly like the
    degraded rung, and reconnecting to it named
    ``/tmp/podbench-home/.podbench/sshd_config`` in the ProxyCommand of a
    container whose agent had written ``/etc/podbench/sshd_config``. The whole
    symptom was ``No such file or directory`` in an editor's ssh log (DLS,
    2026-08-16).

    The home is read from the seat rather than assumed to be
    :data:`NON_ROOT_HOME`, because a pod that declares
    :data:`podbench.model.SEAT_HOME_VOLUME` moves it, and a ProxyCommand naming
    the config file in the wrong home is a transport that fails at the first
    connection with nothing to say why. It decides nothing for a root seat,
    whose layout ignores ``$HOME`` at both ends.
    """
    if session.root_seat:
        return SshdLayout.for_uid(0, home=session.home)
    # Any non-zero uid selects the same non-root layout, so an unpinned seat can
    # be given a placeholder: the paths that follow come from `home` alone.
    return SshdLayout.for_uid(
        _UNPINNED_UID if session.uid is None else session.uid,
        home=session.home or NON_ROOT_HOME,
    )


_KEY_BLOB = re.compile(r"AAAA[A-Za-z0-9+/]{16,}={0,3}")
"""The base64 body of an ssh public key, wherever it sits on the line.

Every ssh key blob opens ``AAAA`` — the four-byte length prefix of its own
algorithm name — so this finds the key in a line an `authorized_keys` file may
have wrapped in ``command=`` options or ended with somebody else's comment.
Matching the whole line instead would report a key as absent because the seat
stored it with a different comment, which is the guess this replaced.
"""

RECONNECT_KEY_ABSENT_WARNING = (
    "this seat does not authorise the key being offered, and its "
    "authorized_keys cannot be added to from here, so ssh will be refused. "
    "{remedy}"
)
"""Said on a reconnect whose ``authorized_keys`` was read and lacks this key.

Measured, not assumed, which is the whole of #204: the line used to be printed
on **every** reconnect that carried a key, including the overwhelmingly common
one where the seat was landed with the same identity and ssh works. A caution
that fires when nothing is wrong is the one a reader learns to skip, and this
report had four of them.
"""

RECONNECT_KEY_UNMEASURED_WARNING = (
    "this seat's authorized_keys could not be read, so whether it authorises "
    "the key being offered is unmeasured. If ssh is refused, {remedy}"
)
"""Said where the ``cat`` failed rather than answered.

Unmeasured is stated, never passed off as fine by silence - the rule the
``memory`` row is held to. It is still one line and it still ends on the same
action, because the reader's next move is the same one either way.
"""

_KEY_REMEDY_DEV = "`podbench dev --identity` recreates the pod with it."
_KEY_REMEDY_SEAT = "`--new` lands a seat that takes it."
"""What installs a key an ephemeral container cannot be given.

Two spellings because a dev pod's sidecar is not an ephemeral container: it is
recreated with the pod, so the flag that changes its key is ``dev``'s.
"""


def seat_authorises(kubectl: Kubectl, session: Session, public_key: str) -> bool | None:
    """Whether the seat's ``authorized_keys`` already carries ``public_key``.

    ``None`` where the file could not be read — an exec the namespace refused, a
    seat that died, or a layout whose path is not where this launcher thinks it
    is. Reported as unmeasured rather than folded into either answer: silence
    would claim ssh works, and warning would be the guess #204 removed.

    One ``kubectl exec`` per reconnect that carries a key, and only then. The
    path comes from :func:`seat_layout`, so it is read *after* the seat's own
    credentials are — the root and non-root layouts keep the file in different
    places and the uid is what chooses.
    """
    layout = seat_layout(session)
    result = kubectl.exec_(
        session.seat.pod.name,
        ("cat", layout.authorized_keys_path),
        container=session.seat.container,
        check=False,
    )
    if result.returncode != 0:
        return None
    wanted = _KEY_BLOB.search(public_key)
    if wanted is None:
        return None
    return any(
        match.group(0) == wanted.group(0) for match in _KEY_BLOB.finditer(result.stdout)
    )


def reconnect_key_note(
    kubectl: Kubectl, session: Session, public_key: str, *, dev: bool
) -> str | None:
    """What to say about a reconnect's ssh key, or ``None`` when it is there."""
    remedy = _KEY_REMEDY_DEV if dev else _KEY_REMEDY_SEAT
    authorised = seat_authorises(kubectl, session, public_key)
    if authorised:
        return None
    template = (
        RECONNECT_KEY_UNMEASURED_WARNING
        if authorised is None
        else RECONNECT_KEY_ABSENT_WARNING
    )
    return template.format(remedy=remedy)


def read_host_public_key(kubectl: Kubectl, seat: ContainerRef) -> str | None:
    """Ask the agent for the pod's ssh host public key, or ``None``."""
    result = kubectl.exec_(
        seat.pod.name, HOST_KEY_ARGV, container=seat.container, check=False
    )
    key = result.stdout.strip()
    return key or None


def ssh_stanza(
    session: Session,
    *,
    identity_file: str,
    host_key: HostKeyBinding,
    host_alias: str,
    user: str,
    invocation: KubectlInvocation | None = None,
) -> str:
    """The ``Include``-able ssh config stanza for this seat."""
    return client_config(
        session.seat,
        host_alias=host_alias,
        identity_file=identity_file,
        host_key=host_key,
        layout=seat_layout(session),
        user=user,
        kubectl=invocation,
    )


def ssh_unavailable_note(session: Session) -> str:
    """What to print instead of an ssh stanza that could not work.

    Same shape as :attr:`podbench.model.Blocker.explanation`: name the mechanism,
    then the way out. A stanza would be worse than this text — the user would
    spend the failure on ``Permission denied (publickey)``, which points at
    their key rather than at an image with no account for the uid the cluster
    made this container run as.
    """
    identity = session.ssh
    detail = (
        identity.detail
        if identity is not None
        else "the seat was never asked for a login name"
    )
    seat = session.seat
    target = f"-n {seat.pod.namespace} {seat.pod.name} -c {seat.container}"
    return "\n".join(
        [
            "no ssh config was written: this seat has no login identity.",
            *paragraph(detail, first="  ", indent="  "),
            "  ways out:",
            "    - a seat normally registers its own record in",
            f"      {SEAT_NSS_PATH}; the line above is why this",
            "      one did not, and only one of the two reasons it can give is",
            "      fixable from here. A uid or gid under that database's floors",
            f"      is not: this image answers those from {PASSWD_PATH}, which",
            "      it pre-seeds for every free uid below 500, so a seat saying",
            "      otherwise is older than those records. An image that has no",
            "      such database is - the launcher names the image, so upgrade it",
            "      `uvx podbench@latest`, or point --image at a tag that has one.",
            "      Note that --pull always changes nothing unless your tag moves",
            "      (main, a branch image), and --new burns another container name",
            "      on this pod for good - so re-running this attach unchanged",
            "      costs a name and fixes nothing",
            "    - or run the target in a group of 500 or more (or gid 100),",
            "      which is what the database asks of it. Pinning the seat to",
            "      gid 0 to win the write is the one thing not offered here:",
            "      ptrace compares the group ids as well as the user ids, so it",
            "      buys ssh and takes the debugger, or",
            "    - run the target as a uid the debug image has an account for, or",
            "    - debug in a dev pod (`podbench dev`), whose seat is an ordinary",
            "      container and can be given files.",
            *paragraph(
                f"a {SEAT_IDENTITY_VOLUME!r} volume does not help here, "
                "declared or not: projecting a passwd file takes a subPath per "
                "file, and the API server forbids subPath on an ephemeral "
                "container.",
                first="  ",
                indent="  ",
            ),
            "  the rest of the seat needs no ssh and works now:",
            f"    kubectl exec {target} -- podbench capreport",
            f"    kubectl exec {target} -- podbench pids",
            f"    kubectl exec -it {target} -- bash",
        ]
    )


def _pull_policy(value: str) -> str:
    """``--pull`` in the kubelet's own casing, or a refusal naming the three.

    Case-insensitive because the flag is typed by hand and ``always`` is what
    anybody types; the API server takes only ``Always``, and would reject the
    whole request over the capital.

    >>> _pull_policy("always")
    'Always'
    """
    for policy in PULL_POLICIES:
        if value.lower() == policy.lower():
            return policy
    raise typer.BadParameter(
        f"--pull {value!r} is not one of {', '.join(PULL_POLICIES)}"
    )


def moving_tag_note(image: str, policy: str) -> str | None:
    """Warn when the seat's image can have changed under a name that has not.

    Only where the policy would *not* re-check, since that is the case with no
    symptom: the seat comes up, the launcher carries today's code and the seat
    carries whatever the node cached, and nothing anywhere says the two are
    different versions. Measured while testing a branch image — the target
    selection was fixed in the launcher and absent from the seat, which read as
    the fix not working.

    This is the fallback and not the answer. :func:`probe_seat_version` asks the
    seat outright, and where it gets a reply ``attach`` prints that instead: a
    tag string can only say a skew is *possible*, and it is wrong in both
    directions — a moving tag whose node happens to be current, a pinned tag
    whose image was built dirty. The note survives for the seat that will not
    say.

    >>> print(moving_tag_note("ghcr.io/x/podbench:main", "IfNotPresent"))
    `main` is a tag that moves, and this node may already have a copy: a seat
      started from it can be older than this launcher, with no sign of it.
      `--pull always` re-checks (it needs a reachable registry, so not on a
      side-loaded image).
    >>> moving_tag_note("ghcr.io/x/podbench:0.2.0", "IfNotPresent") is None
    True
    >>> moving_tag_note("ghcr.io/x/podbench:main", "Always") is None
    True
    """
    if policy == "Always" or not moves(image):
        return None
    tag = image.rsplit("/", 1)[-1].partition(":")[2] or "latest"
    return (
        f"`{tag}` is a tag that moves, and this node may already have a copy: a "
        "seat\n  started from it can be older than this launcher, with no sign "
        "of it.\n  `--pull always` re-checks (it needs a reachable registry, so "
        "not on a\n  side-loaded image)."
    )


def seat_suffix(seat: str | None) -> str:
    """``podbench-2`` as the ``-2`` that goes on the end of a name.

    The container base is stripped because it is already the prefix of every
    alias and every stanza filename podbench writes; carrying it twice gives
    ``podbench-demo-api-podbench-2``, which nobody would type.

    A seat named exactly ``podbench`` gets no suffix at all. That is a ``dev``
    pod's sidecar, and a dev pod has exactly one — there is nothing to
    disambiguate, and giving it one would rename every existing dev alias and
    orphan its stanza.

    >>> seat_suffix("podbench-2")
    '-2'
    >>> seat_suffix("podbench")
    ''
    >>> seat_suffix("devseat")
    '-devseat'
    >>> seat_suffix(None)
    ''
    """
    if not seat or seat == CONTAINER_BASE:
        return ""
    prefix = f"{CONTAINER_BASE}-"
    return f"-{seat[len(prefix) :] if seat.startswith(prefix) else seat}"


def seat_label(seat: str | None) -> str | None:
    """``podbench-2`` as the bare ``2`` a name is qualified by, or ``None``.

    The same rule as :func:`seat_suffix` without the separator, for the one
    caller that needs the word rather than the fragment.

    >>> seat_label("podbench-2"), seat_label("podbench"), seat_label(None)
    ('2', None, None)
    """
    return seat_suffix(seat).lstrip("-") or None


def default_host_alias(pod: PodRef, seat: str | None = None) -> str:
    """The ssh ``Host`` name for one *seat*, not for a pod.

    A pod can carry several seats at once - an ephemeral container is never
    removed, so every ``attach --new`` adds one and the old ones keep running -
    and before issue #93 they all answered to the same name. The consequences
    were both silent:

    * the stanza is one file per pod, so landing a second seat **overwrote** the
      first one's ProxyCommand rather than adding to it;
    * the stanza sets ``ControlMaster auto`` with ``ControlPersist``, and the
      multiplexing key includes the host *as typed*. So even with the file
      rewritten, every ``ssh`` - VS Code's included - kept riding the connection
      already open to the **old** seat. Measured at DLS: the editor run wrote
      ``launch.json`` into ``podbench-2`` over ``kubectl exec`` while the editor
      read ``podbench-1``'s copy, and the debugger silently used the previous
      image's answer.

    Naming the seat fixes both, and is honest besides: a new seat is a different
    container with an empty ``~/.vscode-server``, so an editor treating it as
    the machine it already knows is wrong about the one thing it caches most.

    ``seat`` is optional because ``ssh-config`` and ``doctor`` describe the
    spelling before any seat is known.

    >>> default_host_alias(PodRef("demo", "api-7f9"), "podbench-2")
    'podbench-demo-api-7f9-2'
    >>> default_host_alias(PodRef("demo", "api-7f9"))
    'podbench-demo-api-7f9'
    """
    return f"podbench-{pod.namespace}-{pod.name}{seat_suffix(seat)}"


def write_known_hosts(binding: HostKeyBinding, path: Path) -> Path:
    """Pin ``binding``'s key under its alias, replacing any earlier entry.

    podbench manages this file itself rather than shipping
    ``StrictHostKeyChecking no``: a debugging tool that teaches users to skip
    host verification has taught them something they will apply elsewhere. A
    re-attach after a pod restart legitimately mints a new key, and the alias is
    keyed on the pod UID so that shows up as a new host rather than as a
    man-in-the-middle warning.
    """
    entry = known_hosts_entry(binding)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(
        "\n".join([*_known_hosts_without(path, binding.alias), entry.strip()]) + "\n"
    )
    path.chmod(0o600)
    return path


def _known_hosts_without(path: Path, alias: str) -> list[str]:
    """``path``'s lines, minus the entry pinning ``alias`` and minus blanks."""
    return [
        line
        for line in (path.read_text().splitlines() if path.is_file() else [])
        if line.strip() and not line.startswith(f"{alias} ")
    ]


def forget_known_hosts(alias: str, path: Path) -> bool:
    """Drop ``alias``'s pinned key, and say whether there was one.

    The counterpart of :func:`write_known_hosts`, for a seat that is going away
    for good. An alias is keyed on the pod UID, so leaving it behind is not a
    correctness problem — nothing will ever present that alias again — but a
    ``known_hosts`` that only grows is a file the user eventually has to reason
    about, and podbench said it manages this one.
    """
    if not path.is_file():
        return False
    kept = _known_hosts_without(path, alias)
    if len(kept) == len(path.read_text().splitlines()):
        return False
    if kept:
        path.write_text("\n".join(kept) + "\n")
        path.chmod(0o600)
    else:
        path.unlink()
    return True


def write_ssh_config(stanza: str, path: Path) -> Path:
    """Write the stanza to its own file, overwriting it wholesale.

    An include rather than an edit of ``~/.ssh/config``, so podbench can
    regenerate on every attach without ever owning a file the user also edits.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(stanza)
    path.chmod(0o600)
    # The stanza names a ControlPath, and ssh does not create its parent: it
    # fails the connection with `unix_listener: cannot bind to path ...: No such
    # file or directory`, which reads like a transport fault rather than a
    # missing directory. Writing the config is the moment we know the path, so
    # it is the moment to make it usable.
    ensure_control_dir()
    return path


def ssh_config_path(directory: Path, pod: PodRef, seat: str | None = None) -> Path:
    """The file :func:`write_ssh_config` writes for one seat.

    One place, because three commands now have to agree on it: ``attach`` and
    ``dev`` write it, ``dev --delete`` removes it, and ``list``/``status`` read
    the alias back out of it. A teardown that guessed the name would silently
    leave a stanza behind pointing at a pod that no longer exists.

    Per *seat* rather than per pod, for :func:`default_host_alias`'s reason: two
    seats on one pod both being written here meant the second silently replaced
    the first, and the first was still running.

    >>> ssh_config_path(Path("/h/.podbench"), PodRef("demo", "api"), "podbench-2").name
    'demo-api-2.conf'
    """
    return directory / CONFIG_D / f"{pod.namespace}-{pod.name}{seat_suffix(seat)}.conf"


def ssh_include_line(directory: Path) -> str:
    """The one line ``~/.ssh/config`` needs for those files to be read.

    Derived from the same :data:`CONFIG_D` the stanzas are written under, so the
    advice cannot come to name a directory nothing writes to. ``podbench
    doctor`` checks for this line and ``--fix`` splices it in; ``attach`` prints
    it, because the user may be the only one allowed to edit that file.

    >>> ssh_include_line(Path("/home/dev/.podbench"))
    'Include /home/dev/.podbench/config.d/*.conf'
    >>> ssh_include_line(Path("/Users/Jo Smith/.podbench"))
    'Include "/Users/Jo Smith/.podbench/config.d/*.conf"'
    """
    glob = str(directory / CONFIG_D / "*.conf")
    # ssh_config splits a directive's arguments on whitespace, so an unquoted
    # `/Users/Jo Smith/...` reaches OpenSSH as two paths and neither exists -
    # and `doctor` reads it back with the same split, so the check would report
    # MISSING for ever and `--fix` would prepend the line again on every run.
    # Double quotes are the quoting ssh_config understands; shlex.quote's single
    # quotes are not. Only quoted when needed, so the line a reader is asked to
    # paste stays the plain one everywhere it can.
    if any(character.isspace() for character in glob):
        return f'Include "{glob}"'
    return f"Include {glob}"


def forget_ssh_config(
    pod: PodRef,
    *,
    directory: Path,
    alias: str | None = None,
    seat: str | None = None,
) -> list[str]:
    """Remove the client wiring podbench wrote for ``seat``; say what went.

    Only ever called for a seat that cannot come back — a deleted dev pod. An
    ``attach`` seat is reconnectable for the pod's lifetime, so its stanza is
    regenerated rather than removed.
    """
    removed: list[str] = []
    path = ssh_config_path(directory, pod, seat)
    if path.is_file():
        path.unlink()
        removed.append(f"removed {path}")
    if alias is not None and forget_known_hosts(alias, directory / "known_hosts"):
        removed.append(f"dropped {alias} from {directory / 'known_hosts'}")
    return removed


def identity_paths(identity: str) -> tuple[Path, Path]:
    """The private and public halves of an ssh key named by either.

    Public because ``doctor`` reports on the same pair this module refuses
    without, and a second spelling of ".pub" would be a second thing to keep
    true.

    >>> [path.name for path in identity_paths("/keys/id_ed25519")]
    ['id_ed25519', 'id_ed25519.pub']
    """
    private = Path(identity).expanduser()
    return private, private.with_name(private.name + ".pub")


def read_public_key(identity: str) -> tuple[str, str]:
    """The private key's path and the public key's text, or a refusal naming both.

    Both halves are wanted at once: podbench authorises the public key inside
    the container and names the private one in the stanza's ``IdentityFile``,
    and a mismatch between them is a login refused for reasons neither file
    explains.
    """
    private, public = identity_paths(identity)
    if not public.is_file():
        raise LauncherError(
            f"no ssh public key at {public}. podbench authorises this key inside "
            "the container, so generate one (ssh-keygen -t ed25519) or point "
            "--identity at an existing key."
        )
    return str(private), public.read_text().strip()


def client_dir(explicit: str | None) -> Path:
    """Where the generated stanza and ``known_hosts`` live."""
    chosen = explicit or os.environ.get(CLIENT_DIR_ENV) or DEFAULT_CLIENT_DIR
    return Path(chosen).expanduser()


@dataclass(frozen=True)
class SshSeat:
    """The ssh wiring a landed seat got, and what the user is told about it."""

    note: str
    """The block printed under the report: where the stanza went and what to
    type, or — when the seat has no login identity — why there is no stanza."""

    alias: str | None = None
    """The ssh ``Host`` name, ``None`` when no stanza could be written."""

    path: Path | None = None
    """Where the stanza was written, ``None`` for ``--print-config`` and for a
    seat that got none."""


def emit_ssh_config(
    kubectl: Kubectl,
    session: Session,
    *,
    identity: str,
    config_dir: str | None = None,
    host_alias: str | None = None,
    user: str | None = None,
    print_config: bool = False,
    opening: bool = False,
) -> SshSeat:
    """Generate the client stanza for a landed seat, and write it.

    Every seat podbench lands goes through here, whatever authored it: an
    ephemeral container from ``attach`` and the ordinary sidecar of a
    ``podbench dev`` pod differ in how they were created and in nothing this
    function does. Which is the point — the host-key probe, the ``known_hosts``
    entry, the ProxyCommand and the ``Include`` advice were written once and a
    second copy of them would be a second thing to keep true.

    ``user`` overrides the login name, which is otherwise whatever the seat
    itself answered to ``--print-login-user`` — a projected passwd record on a
    dev pod, ``root`` or ``podbench`` on an ephemeral one. The rung decides only
    when the seat was never asked, or answered with an image too old to know the
    flag.

    ``opening`` drops the closing "run ``podbench debug-config`` in the seat"
    line, because ``vscode`` is about to run it and say what it got. Only that
    one line: the alias, the ``Include`` and the ssh command are what the reader
    needs whether or not a window opens, and that verb's own exit code is not
    evidence the window connected.
    """
    if session.ssh is not None and session.ssh.refused:
        # Nothing below can help: sshd resolves the login name before it looks
        # at a key, so a stanza, a known_hosts entry and a minted identity file
        # would all be spent on a login that is refused at the first step.
        return SshSeat(ssh_unavailable_note(session))

    directory = client_dir(config_dir)
    pod_json = kubectl.get_pod(session.pod.name)
    pod_uid = _as_str(as_dict(pod_json.get("metadata")).get("uid")) or session.pod.name
    known_hosts = directory / "known_hosts"
    public_key = read_host_public_key(kubectl, session.seat)
    # Always PER_ATTACH: the launcher cannot tell a minted host key from a
    # mounted one, and the weaker assumption is the safe one — an alias keyed on
    # the pod UID means a re-created pod shows up as a new host rather than as a
    # man-in-the-middle warning (report R9/R10).
    binding = HostKeyBinding(
        policy=HostKeyPolicy.PER_ATTACH,
        alias=host_key_alias(pod_uid, seat_label(session.seat.container)),
        known_hosts=str(known_hosts),
        public_key=public_key,
    )
    notes: list[str] = []
    if public_key is None:
        notes.append(
            "# the agent did not return a host public key, so known_hosts was "
            "not written; the first connection will be refused until it does"
        )
    else:
        write_known_hosts(binding, known_hosts)

    alias = host_alias or default_host_alias(session.pod, session.seat.container)
    # The seat's own answer beats any prediction of it, and the fallback below
    # is a prediction from the seat's *uid* rather than from its rung: a root
    # seat whose `capabilities.add` a mutating webhook stripped reads back as
    # the degraded rung, whose login name is `podbench` — a name that resolves
    # to nothing here, because the agent registers it only for a uid NSS cannot
    # already resolve, and uid 0 is always `root`. sshd refuses an unresolvable
    # login before it looks at a key, so the prediction costs the whole seat.
    measured = session.ssh.login if session.ssh is not None else None
    login = user or measured or ("root" if session.root_seat else DEFAULT_SEAT_USER)
    stanza = ssh_stanza(
        session,
        identity_file=identity,
        host_key=binding,
        host_alias=alias,
        user=login,
        invocation=KubectlInvocation(binary=kubectl.binary, context=kubectl.context),
    )
    if print_config:
        return SshSeat("\n".join([*notes, stanza]), alias=alias)
    path = write_ssh_config(
        stanza, ssh_config_path(directory, session.pod, session.seat.container)
    )
    return SshSeat(
        "\n".join(
            [
                *notes,
                f"ssh config written to {path}",
                # The hint names the verb that *checks* the edit as well as the
                # line itself: this one is the only setup step podbench has ever
                # asked a user to make by hand, and until `doctor` existed
                # nothing verified it had been made, or made correctly - above
                # any Host * block, where ssh will read it first.
                f"add this to ~/.ssh/config once:  {ssh_include_line(directory)}",
                "or let podbench check and add it:  podbench doctor --fix",
                # `then:` for an attach, which is what happens next; but
                # `vscode` prints this block *after* opening the window, so
                # there the alias is what a later reconnect needs rather than
                # what to do now, and a "then:" pointing backwards is a step
                # the reader has already had done for them.
                (
                    f"reconnect later with:  ssh {alias}"
                    if opening
                    else f"then:  ssh {alias}   "
                    f"(or Remote-SSH: Connect to Host -> {alias})"
                ),
                # The VS Code debugger needs a launch.json whose pid,
                # sysroot-prefixed program and setup ordering are all things
                # this launcher already knows and a human cannot guess; every
                # wrong answer fails silently rather than erroring. Dropped
                # under `vscode`, which runs that verb itself a few lines further
                # down and reports what it actually got: a step the reader has
                # already had done for them reads as a step that did not happen.
                *(
                    []
                    if opening
                    else [
                        "to debug in VS Code, run `podbench debug-config` in the "
                        "seat (writes .vscode/launch.json)"
                    ]
                ),
            ]
        ),
        alias=alias,
        path=path,
    )


# -- memory headroom --------------------------------------------------------


def measure_headroom(
    kubectl: Kubectl, pod: str, pod_json: Mapping[str, Any]
) -> Headroom:
    """What is left of this pod's memory ceiling, as far as it can be read.

    Two reads, and either may come back empty. The ceiling is arithmetic on the
    pod spec already in hand; what the pod is *using* needs the metrics API,
    which is an add-on and a separate RBAC resource, so it is asked for with
    ``check=False`` and its absence recorded as unmeasured.

    The usage read is skipped entirely where there is no ceiling: a pod cgroup
    the kubelet left unbounded has no headroom question to answer, and spending
    an API call to find out how much of nothing is free is not worth an attach's
    latency.

    Taken after the seat has landed, so the sum includes it - which is the
    number the reader wants, since what they are about to do is grow it. A
    metrics sample older than the seat simply reports the pod without it, which
    errs towards more room rather than less; the warning this feeds is about the
    order of magnitude, not the megabyte.
    """
    limit = pod_memory_limit(pod_json)
    if limit is None:
        return Headroom(limit=None, used=None)
    top = kubectl.top_pod(pod)
    return Headroom(limit=limit, used=None if top is None else parse_top_memory(top))


def headroom_note(
    headroom: Headroom | None, *, needed: Fraction, template: str
) -> str | None:
    """The warning ``headroom`` earns against ``needed``, or ``None``.

    Silent on an ample margin *and* silent on one that could not be read, which
    are different answers and only one of them is good news. The unmeasured case
    is not dropped: :attr:`podbench.resize.Headroom.summary` states it as
    unmeasured on the report's ``memory`` row, which is where a fact that is not
    a hazard belongs. Turning it into a caution instead would fire on every
    cluster without a metrics-server, and an always-on memory warning is exactly
    what the p47 measurement retired.
    """
    if headroom is None or not headroom.below(needed):
        return None
    limit, used, free = headroom.limit, headroom.used, headroom.free
    assert limit is not None and used is not None and free is not None  # noqa: S101
    return template.format(
        free=format_memory(free),
        used=format_memory(used),
        limit=format_memory(limit),
        needed=format_memory(needed),
        footprint=SEAT_FOOTPRINT,
    )


# -- in-place resize --------------------------------------------------------


def try_resize(
    kubectl: Kubectl,
    pod: str,
    container: str,
    memory: str | None = None,
    *,
    cpu: str | None = None,
    pod_json: Mapping[str, Any] | None = None,
) -> str:
    """Raise the target container's limits in place; never fatal.

    A strategic-merge patch, not the JSON patch the rest of podbench prefers:
    resize addresses containers by name, and a JSON patch would need the
    container's *index*, which is positional and would silently resize the wrong
    container if the pod spec changed under us.

    **Requests move with limits.** A ``LimitRange`` carrying
    ``maxLimitRequestRatio`` bounds limit ÷ request, so raising a limit alone
    only ever widens that ratio: at Diamond a 6Gi limit over the workload's
    64Mi request was refused for a ratio of 96 against a cap of 10, which made
    ``--resize`` unusable on every pod in the namespace. The request podbench
    submits alongside is derived from the namespace's own numbers — see
    :func:`podbench.resize.plan_resize` for the two invariants that bound it.

    Failure is reported rather than raised — the mitigation is only partly
    proven (report R13) and a seat that lands with a loud warning beats one that
    does not land at all.

    Success is reported just as loudly, because it leaves the pod diverged from
    the controller that owns it (report R13): the raised limit is on the pod
    object alone, so the next thing to regenerate the pod from an unchanged
    template takes it away again with no other symptom than a seat that OOMs.
    """
    wants: dict[str, Want] = {}
    try:
        if memory is not None:
            wants[MEMORY] = parse_want(memory, resource=MEMORY)
        if cpu is not None:
            wants[CPU] = parse_want(cpu, resource=CPU)
        document = pod_json if pod_json is not None else kubectl.get_pod(pod)
        current = _container_resources(document, container)
        plan = plan_resize(
            container,
            current=current,
            wants=wants,
            limits=namespace_limits(kubectl.list_limit_ranges()),
        )
    except ResizeError as error:
        return f"no resize was attempted: {error}"

    # The one refusal that is knowable before it happens. A Guaranteed pod has
    # request == limit everywhere by definition, so raising a limit on its own
    # must change its QoS class, and the API server refuses every resize that
    # does - at any number, so there is nothing to retry (#124). podbench does
    # not pin the request itself: a request is a *reservation* on the node, not
    # a cap, and choosing to take one is a bigger decision than this flag's, so
    # what the user gets is the command that works rather than a mutation that
    # cannot.
    remedy = qos_preserving_flags(current, plan)
    if remedy is not None and _qos_class(document) == "Guaranteed":
        return (
            "no resize was attempted: this pod is Guaranteed, so every request "
            "equals its limit, and a resize may not change a pod's QoS class - "
            "raising the limit alone is refused whatever the number. Ask for "
            f"both halves instead: `{remedy}`."
        )

    asked = ", ".join(
        f"{resource} {value}" for resource, value in sorted(_asked(plan).items())
    )
    try:
        kubectl.patch(
            "pod", pod, plan.body, patch_type="strategic", subresource="resize"
        )
    except KubectlError as error:
        refusal = error.stderr.strip() or str(error)
        # The API server answers a claim-bearing container with a sentence about
        # cpu and memory, which sends the reader after the numbers they just
        # asked for. See resize.explain_claim_refusal.
        explanation = explain_claim_refusal(current, refusal)
        if not explanation and QOS_REFUSAL in refusal and remedy is not None:
            # The backstop for a pod whose `status` did not say Guaranteed -
            # a caller-supplied document without one, or a class the API server
            # recomputed under us. The remedy is the same either way.
            explanation = (
                "a resize may not change a pod's QoS class, and this one would."
                f" Ask for both halves instead: `{remedy}`."
            )
        return (
            f"in-place resize ({asked}) was refused, so podbench is sharing "
            "the pod's existing limits"
            + (f" - {explanation}" if explanation else ".")
            # Said once, here, so nobody goes hunting for RBAC they do not need:
            # the refusal is usually the end of it. A seat costs 13-23 MiB, and
            # the tightest pod on the beamline this was measured against had
            # 170 MiB free. The `memory` row above says what *this* pod has.
            + f" A seat itself measured {SEAT_FOOTPRINT} across ten live seats "
            "(DLS p47, 2026-08-19), so sharing the existing limits is usually "
            "the end of it - unless this session runs vscode-server, which "
            "measured 1215 MiB with one extension."
            # The cluster's own words go last, and nothing follows them. Relayed
            # text is not ours to reflow and may or may not end in a stop, so any
            # sentence after it collides: a real one read `...namespace
            # "hgv27681" A seat itself measured...`, where the seam is invisible
            # and the reader takes our sentence for more of the API server's.
             + f" The API server said: {refusal}"
        )
    return "\n".join(
        [
            f"resized {container} to {asked}; restore it on detach.",
            *plan.notes,
            "The raised limits live on this pod, not on the controller that "
            "owns it, so a rollout, a scale, an image bump or an eviction "
            "regenerates it from an unchanged template and silently reverts "
            "the resize. A GitOps controller does not itself revert this - Argo "
            "CD reconciles the workload object, and the pod is not one of its "
            "manifests - but the sync that rolls the workload does. Only partly "
            "proven (report R13): three pods, two of them Deployment-managed - "
            "a ReplicaSet reconciles pod existence, not pod spec, so it does "
            "not fight this - but one Kubernetes version, one LimitRange, and "
            "never against a ResourceQuota.",
        ]
    )


def _asked(plan: ResizePlan) -> dict[str, str]:
    """What the patch says, flattened for one line of report."""
    resources = as_dict(as_dict(plan.body["spec"]["containers"][0]).get("resources"))
    stated: dict[str, str] = {}
    for resource, value in as_dict(resources.get("limits")).items():
        request = as_dict(resources.get("requests")).get(resource)
        stated[resource] = f"{request}/{value}" if request else str(value)
    return stated


def _qos_class(pod_json: Mapping[str, Any]) -> str:
    """The class the kubelet computed for this pod, or the empty string.

    Read rather than derived: the rule is "every container's every request
    equals its limit", but which containers count and what an unset request
    means are the kubelet's business, and a pod object always carries its own
    answer. An absent one is a document that was not fetched whole, and is
    never treated as evidence either way.
    """
    return str(as_dict(pod_json.get("status")).get("qosClass") or "")


def _container_resources(
    pod_json: Mapping[str, Any], container: str
) -> Mapping[str, Any]:
    """One container's ``resources`` as the pod carries them now.

    Read from ``spec`` rather than from ``status.containerStatuses``: an earlier
    resize that the kubelet has accepted but not yet actuated shows the new
    numbers in the spec and the old ones in the status, and what the next patch
    has to be consistent with is the spec.
    """
    for entry in _as_list(as_dict(pod_json.get("spec")).get("containers")):
        candidate = as_dict(entry)
        if candidate.get("name") == container:
            return as_dict(candidate.get("resources"))
    return {}


# -- namespace / listing ----------------------------------------------------


def current_namespace(
    *,
    binary: str = "kubectl",
    context: str | None = None,
    runner: Runner | None = None,
) -> str:
    """The kubeconfig's current namespace, defaulting to ``default``.

    Read from kubectl rather than reimplemented: the launcher's whole premise is
    that auth, contexts and namespace come from the kubeconfig unmodified.
    """
    run = runner if runner is not None else run_subprocess
    argv = [binary]
    if context is not None:
        argv += ["--context", context]
    argv += ["config", "view", "--minify", "-o", "jsonpath={..namespace}"]
    # Bounded like every other kubectl call, though this one reads a file and
    # contacts nothing: it is the first call every verb makes, and a kubeconfig
    # on a mount that has stopped answering would hang before anything is
    # printed at all.
    result = run(argv, timeout=DEFAULT_CALL_TIMEOUT)
    namespace = result.stdout.strip()
    if result.returncode != 0 or not namespace:
        return "default"
    return namespace


def kubectl_for(
    namespace: str | None,
    *,
    context: str | None = None,
    binary: str = "kubectl",
    runner: Runner | None = None,
) -> Kubectl:
    """The one ``Kubectl`` every cluster-side verb talks through.

    Shared rather than reimplemented per module because the namespace default is
    a promise about the whole CLI: ``-n`` unset means the kubeconfig context's
    own namespace, and a verb that quietly means ``default`` instead sends
    someone's ``dev`` at a pod they cannot see. That is issue #44, and it
    happened because the two copies of these three lines were easier to add to
    than to reach for.

    The kubeconfig is consulted only when the flag gave nothing, because the
    lookup is itself a ``kubectl`` call and every verb would otherwise pay for it
    on top of the work it came to do.

    >>> kubectl_for("demo").namespace
    'demo'
    """
    if namespace is None:
        namespace = current_namespace(binary=binary, context=context, runner=runner)
    return Kubectl(namespace, context=context, binary=binary, runner=runner)


def list_seats(kubectl: Kubectl) -> list[tuple[PodRef, list[SeatInfo]]]:
    """Every pod in the namespace that carries a podbench container.

    Which is *not* what :func:`resolve_pod` lists: this is the fleet of seats
    podbench already landed, and that is every pod a seat could be landed in.
    Both read the same ``kubectl get pods`` through
    :meth:`podbench.kubectl.Kubectl.list_pods`.
    """
    found: list[tuple[PodRef, list[SeatInfo]]] = []
    for pod_json in kubectl.list_pods():
        name = _entry_name(as_dict(pod_json.get("metadata")))
        if name is None:
            continue
        present = all_seats(pod_json)
        if present:
            found.append((PodRef(kubectl.namespace, name), present))
    return found


STARTING_PHASES = frozenset({"pending", "waiting"})
"""The two phases a seat can still leave on its own.

``pending`` is the node not having reported yet and ``waiting`` is the pull,
the mount or the sandbox; ``running`` and ``terminated`` are both settled.
"""


def _still_starting(seat: SeatInfo) -> bool:
    """Whether waiting on this seat could change what it is.

    One exception, and it is the one that would otherwise cost the whole
    timeout: a seat the kubelet refused sits in ``waiting`` with
    ``CreateContainerConfigError`` for the pod's lifetime and never leaves it
    (report 3.18). Polling that is a wait for something that has already
    happened.
    """
    if seat.phase not in STARTING_PHASES:
        return False
    return seat.detail.split(":", 1)[0] != CREATE_CONTAINER_CONFIG_ERROR


def wait_for_seats(
    kubectl: Kubectl,
    pod: str,
    *,
    timeout: float,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[SeatInfo]:
    """This pod's seats, having waited up to ``timeout`` for one still starting.

    ``attach`` needs ``--timeout`` on a cluster whose image pull is slow, and
    ``status`` is the verb reached for next — which had no such flag, so every
    one of 15 agents in the sweep hit it (finding 17.1). Waiting is what the
    flag has to mean here: ``status`` measures a seat by ``exec``ing into it,
    and a seat that is still ``waiting`` can answer nothing.

    Never raises, unlike
    :meth:`podbench.kubectl.Kubectl.wait_for_ephemeral_container`. A deadline
    that passes with the seat still starting is *reported*: this verb exists to
    say what is there, and ``waiting`` is a true answer that the caller asked
    for a while longer to improve on.

    ``timeout <= 0`` — the default — is a single read and no clock at all, so a
    plain ``podbench status`` costs exactly what it always did.
    """
    present = all_seats(kubectl.get_pod(pod))
    if timeout <= 0:
        return present
    deadline = clock() + timeout
    while any(_still_starting(seat) for seat in present):
        if clock() >= deadline:
            break
        sleep(poll_interval)
        present = all_seats(kubectl.get_pod(pod))
    return present


def host_alias_in(stanza: str) -> str | None:
    """The ``Host`` name a written stanza declares, or ``None`` if it has none.

    Only ever pointed at a file :func:`write_ssh_config` produced, which is why
    the parse is this thin: one ``Host`` line, one pattern, no ``Match`` blocks
    and no ``=`` separator. Case-insensitive because ssh_config keywords are,
    and a stanza someone reformatted by hand is still theirs to connect with.

    >>> host_alias_in("Host podbench-demo-api\\n    User root\\n")
    'podbench-demo-api'
    >>> host_alias_in("    hostname api-7f9\\n") is None
    True
    """
    for line in stanza.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].lower() == "host":
            return fields[1]
    return None


_SEAT_FACT = " " * 4
"""Where everything said about one seat is indented to, under its row."""

NO_TARGET_CONTAINER = "none - it runs in the pod's own namespaces"
"""What a seat's ``target`` row says when it names no container.

Not "unknown", and not the pod's first container either: an ephemeral container
with no ``targetContainerName`` is not pointed at one at all, and runs in the
namespaces the *pod* is configured with. Nothing podbench lands is authored that
way (:func:`podbench.spec.ephemeral_container_spec` is always given the
workload), so this is here for a seat somebody else's ``kubectl debug`` put in
the pod.
"""

_FACT_INDENT = " " * 14
"""Where a fact's value starts, and stays on a wrap.

:data:`_SEAT_FACT`, then a label padded to the width of the longest of them,
then the two spaces that make it a label at all rather than a word a sentence
opens with (see ``console._LABEL``).
"""


def ssh_connect_line(directory: Path, pod: PodRef, seat: str | None = None) -> str:
    """What to type to sit in ``pod``'s seat — read from disk, never derived.

    :func:`default_host_alias` is a pure function of the :class:`PodRef` and so
    is tempting to recompute here, but ``attach --host-alias NAME`` overrides it
    and the cluster records that choice nowhere: a derived alias would be
    silently wrong for exactly the users who picked their own. So the answer is
    read back out of the stanza, which holds the ``Host`` line ssh will actually
    match. A listing that names an alias which does not work is worse than one
    that names none, so every case that cannot produce a real alias says which
    file it looked in instead.

    Indented to :data:`_SEAT_FACT`, with the seat's other facts, because it is
    one seat's alias and a pod can list several. Returned whole and never
    wrapped: the right-hand half is there to be pasted, and
    :func:`~podbench.console.wrap` would break it on a space.
    """
    path = ssh_config_path(directory, pod, seat)
    try:
        stanza = path.read_text()
    except FileNotFoundError:
        # The stanza is client-side state: nothing in the pod carries it. A seat
        # landed from another machine, or by a colleague, is normal and reaches
        # here, and `ssh-config` exists to mint the missing half.
        return (
            f"{_SEAT_FACT}no ssh config here: "
            f"podbench ssh-config -n {pod.namespace} {pod.name}"
        )
    except OSError as error:
        return f"{_SEAT_FACT}cannot read {path}: {error.strerror or error}"
    alias = host_alias_in(stanza)
    if alias is None:
        return (
            f"{_SEAT_FACT}no Host line in {path}: "
            f"podbench ssh-config -n {pod.namespace} {pod.name}"
        )
    return f"{_SEAT_FACT}ssh {alias}"


def probe_seats(
    kubectl: Kubectl, pod: PodRef, present: Sequence[SeatInfo]
) -> dict[str, CapabilityReport | str]:
    """What ``capreport`` says in each of a pod's seats, keyed by container.

    The measurement :func:`format_seats` needs, and the only honest source for
    a verdict column: what a seat can do is decided in the kernel, on that
    node, by four subsystems that the securityContext it was admitted with does
    not determine (issue #89). So it is asked, per seat, exactly as ``attach``
    asks it.

    Only a *running* seat can be asked, and a seat that is not one is left out
    rather than given a reason: the row above it already says ``waiting`` or
    ``terminated``, and a second sentence saying so is the report growing a
    line per seat to repeat itself.

    A failed probe is kept, as the text of why. An image too old to answer, an
    ``exec`` RBAC refusal and a seat that is running but wedged are all real,
    and dropping them would report a running seat as merely unprobed with no
    hint that anything was tried.
    """
    measured: dict[str, CapabilityReport | str] = {}
    for seat in present:
        if not seat.running:
            continue
        report, refusals = run_capreport(kubectl, ContainerRef(pod, seat.name))
        measured[seat.name] = (
            report
            if report is not None
            else "; ".join(refusals) or "capreport gave no answer and no reason"
        )
    return measured


def probe_seat_versions(
    kubectl: Kubectl, pod: PodRef, present: Sequence[SeatInfo]
) -> dict[str, str | None]:
    """Which build of podbench each running seat is, keyed by container.

    The sibling of :func:`probe_seats`, and separate from it because the answer
    is not the capability report's: ``capreport``'s JSON is versioned so an old
    seat can still be read (see :func:`capability_report_from_json`), which is
    precisely what makes "how old" a question worth asking on its own.

    A seat that would not say is kept, as a ``None`` value, rather than dropped:
    :func:`format_seats` reads *absence* from this mapping as never asked, and
    an image too old to answer is a fact about that seat and not an omission of
    ours.
    """
    return {
        seat.name: probe_seat_version(kubectl, ContainerRef(pod, seat.name))
        for seat in present
        if seat.running
    }


def _fact(label: str, value: str) -> list[str]:
    """One ``label   value`` row under a seat, wrapped at the value only.

    The aligned lead goes in ``first`` because :func:`~podbench.console.wrap`
    splits on ``text.split()`` and so collapses the two spaces that make the
    label a label — a whole row through :func:`paragraph` comes back a sentence
    with the value no longer under anything.
    """
    return paragraph(value, first=f"{_SEAT_FACT}{label:<7}   ", indent=_FACT_INDENT)


def _seat_verdict(measured: CapabilityReport | str | None) -> str:
    """The one line a listing may put in a verdict column.

    ``measured`` is the seat's :class:`~podbench.model.CapabilityReport`, or
    the reason there is none, or nothing. Only the first of those can carry a
    claim, and even then only the claim its own attempts support: see
    :func:`podbench.model.measured_verdict`.

    >>> _seat_verdict(None)
    'not probed'
    >>> _seat_verdict("kubectl exec into this seat was refused")
    'not probed - kubectl exec into this seat was refused'
    """
    if isinstance(measured, CapabilityReport):
        verdict = measured.measured
        if verdict is not None:
            return verdict.summary
        # A probe that ran and measured nothing — no target pid, no matrix, no
        # scratch attach — is still an unprobed seat as far as a verdict goes.
        measured = "capreport measured nothing about the target"
    return f"{NOT_PROBED} - {measured}" if measured else NOT_PROBED


UNMOUNTED_HOTFIX_NOTE = (
    "this seat mounts none of this hotfixed pod's volumes, so the application "
    "runs the code on the claim while an editor or debugger here resolves the "
    "image's - breakpoints set on it never bind. This seat cannot be repaired: "
    "`--new` lands a fresh one, which mounts the claim itself"
)
"""Said on a seat whose kind is ``attach`` in a pod carrying the hotfix layout.

The one disagreement between the two that is silent in both directions: nothing
refuses the attach, ``--mount`` on a *reconnect* is already warned about
elsewhere, and the seat that results works perfectly - against the wrong tree.

It no longer spells out that an ephemeral container's ``volumeMounts`` are fixed
when it is created; that is in ``docs/how-to/attach-to-a-pod.md``, and what the
reader needs here is that this seat cannot be repaired and which flag lands one
that can.

Since #177 this is a **reconnect's** note rather than every hotfixed pod's: a
seat landed fresh gets the claim from :func:`hotfix_claim_mounts` without being
asked, so the only way to be here is to have reconnected into one that predates
the layout, predates that change, or was landed with ``--no-seat-identity``
against a target that does not mount the claim. Which is why it names ``--new``
and no longer asks for a ``--mount`` the user would now be typing redundantly.
"""

MEASURED_RUNG_HEADING = "RUNG (measured)"
"""What the third column of a listing is a reading of.

The measurement is the seat's own, taken at start-up from ``/proc`` and read
back out of the container log - so the word means here exactly what it means on
``attach``'s ``rung`` row. A seat that could not be read holds
:data:`~podbench.model.NOT_MEASURED` instead, and its authored rung goes in a
row of its own where no measured label can be mistaken for it."""

REQUESTED_RUNG_FOOTNOTE = (
    "request: read from the seat's securityContext, which is what admission "
    "stored and not what the kernel gave the container. It is what a listing "
    "has left when the seat's own start-up report could not be read - a seat "
    "no longer running, one older than that report, a rotated log, or RBAC "
    "without `pods/log`"
)
"""Said once per pod block, and only where a seat in it went unmeasured.

The heading has room for the word and not for the reason, and the reason is the
whole of why the word is there. Backticked runs so `wrap` cannot break a flag or
a path across the margin (issue #120).

That the stored spec and the kernel's answer differ *in both directions* is
:func:`probe_seat_credentials`'s measurement and :data:`MEASURED_RUNG_HEADING`'s
distinction; this footnote's job is to say which of the two this row is."""


def recover_seat_reports(
    kubectl: Kubectl, pod: PodRef, present: Sequence[SeatInfo]
) -> dict[str, SeatReport]:
    """Each running seat's own start-up report, read out of the container log.

    One ``kubectl logs`` per live seat, and deliberately not one ``kubectl
    exec``. The rung a seat *is* can only be measured inside it, and ``list``
    and ``status`` read pod JSON and run nothing - so the choice was between a
    column that restates a securityContext and a verb that spawns a process per
    seat across a whole namespace. The agent writes the measurement once, at
    start-up, and this reads it (issues #94b and #99).

    A seat missing from the result is a seat that went **unmeasured**, and
    every way that happens is ordinary: an image older than the report, a
    rotated log, or RBAC that grants ``get pods`` and not ``pods/log``. None of
    them is an error and none is reported as one.

    A seat that is no longer running is not asked either, though its log
    usually survives it. A burnt name is listed so that a reader of
    ``podbench-4`` can see why 1-3 are not reusable, and a pod accumulates them
    for its whole lifetime; spending a call per dead seat to recover what one
    of them measured before it exited would make the listing cost grow with
    the pod's history rather than with what is running in it.
    """
    found: dict[str, SeatReport] = {}
    for seat in present:
        if not seat.running:
            continue
        text = kubectl.logs(pod.name, seat.name)
        if text is None:
            continue
        report = SeatReport.from_log(text)
        if report is not None:
            found[seat.name] = report
    return found


def format_seats(
    pod: PodRef,
    present: Sequence[SeatInfo],
    *,
    directory: Path,
    measured: Mapping[str, CapabilityReport | str] | None = None,
    versions: Mapping[str, str | None] | None = None,
    reports: Mapping[str, SeatReport] | None = None,
) -> str:
    """One pod's podbench containers, for ``status`` and ``list``.

    ``directory`` is the client config dir, and is read — never written — for
    the one fact a listing is asked for and the cluster cannot answer: which
    alias to ssh to. Required rather than defaulted so a caller cannot drop the
    connect line by omission.

    ``measured`` carries what ``capreport`` said in each seat, keyed by
    container name, or the reason it said nothing. A seat missing from it is
    reported as :data:`~podbench.model.NOT_PROBED` — never as its rung, which
    is what the cluster admitted and not what the seat can do (issue #89). The
    two are routinely different in the same direction: a webhook that strips
    ``capabilities.add`` leaves a root seat reading back as ``degraded`` while
    it attaches perfectly well, and this column used to call that seat
    read-only.

    A seat a later one replaced is said to be superseded, which no other row
    conveys: the pod carries two live seats with nothing between them to read,
    and the one this listing was probably opened to ask about is the *second*.
    :func:`superseded_seats` decides, from the seats alone.

    ``versions`` carries what each seat said its own build of podbench is, from
    :func:`probe_seat_versions`. A seat missing from it is
    :data:`~podbench.model.NOT_PROBED` for the same reason its verdict is: this
    listing may not report an image tag as a version, since the tag is what the
    spec asked for and a node serving a cached layer of a tag that moves gives
    a seat built from something else.

    ``reports`` carries each seat's own start-up measurement, from
    :func:`recover_seat_reports`. It decides the RUNG column, and a seat missing
    from it holds :data:`~podbench.model.NOT_MEASURED` with its *requested*
    rung on a row of its own - never the authored rung wearing a measured
    label, which is the whole of issue #94 in a column. The same report carries
    what start-up could not do, which until now explained itself only to the
    container log (issue #99).

    The alias is offered only where a seat is *running*. A stanza outlives the
    container it was written for — nothing deletes it, and an ephemeral
    container's name is burnt for the pod's lifetime once it exits — so a pod
    whose only seat has terminated would otherwise be listed with an ssh line
    that cannot work, one line under the row saying why.
    """
    probes = measured or {}
    builds = versions or {}
    startup = reports or {}
    replaced = superseded_seats(present)
    # Headed, as `format_pod_choices` is, because the third cell now stops at
    # the rung's name: `degraded` under nothing at all is the very reading this
    # verb has to stop making, and a header is cheaper than a word per row.
    lines = [
        str(pod),
        f"  {'SEAT':<11}  {'KIND':<6}  {'PHASE':<10}  {MEASURED_RUNG_HEADING}",
    ]
    unmeasured = False
    for seat in present:
        report = startup.get(seat.name)
        rung = None if report is None else report.rung
        lines.append(
            # The gaps are literal, and the padding one column narrower to
            # keep every column start exactly where it was. Relying on the pad
            # to make the gap means a value that fills its field leaves *one*
            # space - and one space is `console`'s rule for "this is a
            # sentence": the cell stops being coloured as a verdict, and the
            # cell before it is read as part of the label. A twelve-character
            # seat name (`podbench-100`) is all it takes.
            f"  {seat.name:<11}  {seat.kind.value:<6}  {seat.phase:<10}  "
            f"{NOT_MEASURED if rung is None else rung.value}"
        )
        lines.extend(_fact("state", seat.detail))
        if rung is None:
            unmeasured = True
            lines.extend(_fact("request", seat.rung.value))
        # The seat's own account of a start-up step that gave up. It was
        # already written down - `ensure_all` records rather than raises,
        # because the caller is PID 1 of an unrestartable container - and until
        # this row the only copy was in a log nobody opens (issue #99).
        for failure in () if report is None else report.failures:
            lines.extend(_fact("startup", failure))
        if seat.name in replaced:
            lines.extend(
                _fact("note", SUPERSEDED_NOTE.format(later=replaced[seat.name]))
            )
        # A note beside the other notes rather than a WARNING: `list` and
        # `status` report a fleet, and a warning lead on a row inside one reads
        # as being about the listing. `attach` says it as a warning, where it is
        # about the single seat that just landed.
        if seat.in_hotfixed_pod:
            lines.extend(_fact("note", UNMOUNTED_HOTFIX_NOTE))
        # The question a second person on a shared pod actually has, and the
        # one `podbench-1` beside `podbench-2` could not answer at all before
        # seats carried a stamp (issue #113).
        lines.extend(_fact("owner", seat.owner or UNKNOWN_OWNER))
        # Per seat and not per pod: two seats on one pod may target different
        # containers, and a listing that says only which pod they are in leaves
        # the reader of a multi-container pod to guess which of them either seat
        # can actually see (finding 15).
        lines.extend(_fact("target", seat.target or NO_TARGET_CONTAINER))
        lines.extend(
            _fact(
                "version",
                seat_version_fact(builds[seat.name])
                if seat.name in builds
                else NOT_PROBED,
            )
        )
        lines.extend(_fact("verdict", _seat_verdict(probes.get(seat.name))))
        # Per seat, because the alias now names one: a pod carrying two live
        # seats has two of them, and a single line would have to pick.
        if seat.running:
            lines.append(ssh_connect_line(directory, pod, seat.name))
    if unmeasured:
        lines.extend(paragraph(REQUESTED_RUNG_FOOTNOTE, first="  ", indent="  "))
    if not any(seat.running for seat in present):
        # The command ends the line, as it does on `ssh_connect_line`'s two
        # missing-stanza answers: this line is never wrapped - the right-hand
        # half is there to be pasted and `wrap` would break it on a space - so
        # the prose that used to trail the pod name was 20 columns nothing could
        # shorten. On a beamline's pod names that alone ran the listing past a
        # 100-column terminal, and the reader had to select around it to paste.
        lines.append(
            f"  nothing to ssh to here: podbench attach -n {pod.namespace} {pod.name}"
        )
    return "\n".join(lines)


# -- choosing which pod the user meant --------------------------------------


@dataclass(frozen=True)
class PodChoice:
    """One pod in the namespace, with enough beside its name to choose by.

    A name on its own is not a choice: two replicas of one deployment differ by
    a hash, and which of them is worth a seat is decided by whether it is
    running, whether its containers came up, and how long it has been there. So
    the listing carries what ``kubectl get pods`` carries, plus the one column
    kubectl cannot know — the podbench container already in there, which turns
    "add a seat" into "reconnect to mine".
    """

    name: str
    ready: str
    """``ready/total`` containers, as the pod's own status reports them."""

    status: str
    age: str
    seat: str | None = None
    """The running podbench container's name, or ``None``."""


def pod_choices(
    kubectl: Kubectl,
    *,
    now: datetime | None = None,
    where: Callable[[Mapping[str, Any]], bool] | None = None,
) -> list[PodChoice]:
    """Every pod in the namespace, in the order the API server returned them.

    ``where`` narrows the listing to the pods the caller can actually act on,
    and is read from each pod's full JSON rather than from a
    :class:`PodChoice`, because what narrows it is usually a label. Offering a
    row that cannot be chosen is worse than not offering it: ``dev --delete``
    lists only the dev pods podbench authored, since every other pod in the
    namespace is one it would refuse to delete anyway.

    ``now`` is injected so a test can assert an age without owning the clock.
    """
    reference = now if now is not None else datetime.now(UTC)
    choices: list[PodChoice] = []
    for pod_json in kubectl.list_pods():
        if where is not None and not where(pod_json):
            continue
        metadata = as_dict(pod_json.get("metadata"))
        name = _entry_name(metadata)
        if name is None:
            continue
        running = running_seat(pod_json)
        choices.append(
            PodChoice(
                name=name,
                ready=_ready_containers(pod_json),
                status=_pod_status(pod_json),
                age=_age_of(_as_str(metadata.get("creationTimestamp")), reference),
                seat=running.name if running is not None else None,
            )
        )
    return choices


def match_pod_names(names: Sequence[str], query: str) -> list[str]:
    """The names ``query`` selects: the exact one if there is one, else every
    name containing it.

    Exact wins *outright* rather than merely ranking first, because a pod whose
    full name someone typed is never a question — and a deployment's pods are
    named by extending one prefix, so ``web`` is routinely a substring of the
    replicas of ``web`` as well as the name of something else entirely.

    Case-folded because an RFC 1123 label is lower-case already, so the only
    thing case sensitivity can do here is refuse a name the user shouted.

    >>> match_pod_names(["api", "api-canary"], "api")
    ['api']
    >>> match_pod_names(["api-7f9", "api-canary"], "API")
    ['api-7f9', 'api-canary']
    >>> match_pod_names(["api-7f9"], "web")
    []
    """
    exact = [name for name in names if name == query]
    if exact:
        return exact
    lowered = query.lower()
    return [name for name in names if lowered in name.lower()]


def match_pod_choices(choices: Sequence[PodChoice], query: str) -> list[PodChoice]:
    """:func:`match_pod_names`, keeping the rows rather than the names."""
    selected = set(match_pod_names([choice.name for choice in choices], query))
    return [choice for choice in choices if choice.name in selected]


def format_pod_choices(choices: Sequence[PodChoice]) -> str:
    """The numbered listing the prompt offers, and the refusal quotes.

    Numbered even when nothing will read a number back: a non-interactive
    refusal prints the same table, and a user who then re-runs with a name is
    reading the column their eye already found.
    """
    rows = [
        (
            f"{index}.",
            choice.name,
            choice.ready,
            choice.status,
            choice.age,
            choice.seat or "-",
        )
        for index, choice in enumerate(choices, start=1)
    ]
    header = ("", "NAME", "READY", "STATUS", "AGE", "PODBENCH")
    widths = [max(len(row[column]) for row in (header, *rows)) for column in range(6)]
    lines: list[str] = []
    for row in (header, *rows):
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=True)]
        lines.append(("  " + "  ".join(cells)).rstrip())
    return "\n".join(lines)


def choose_pod(choices: Sequence[PodChoice], ask: Callable[[], str]) -> PodChoice:
    """Prompt until ``ask`` yields one of ``choices``, and return it.

    A number or a name, because both are in front of the user and neither is
    obviously the one they will reach for. A name is put through
    :func:`match_pod_names` too, so narrowing a five-pod list by typing more of
    the prefix works the way the argument did.

    An empty line, or the EOF a closed stdin gives, is a cancellation and not a
    default: the verbs this feeds all mutate a pod, and "just pick the first
    one" is the wrong guess to make on someone's behalf.
    """
    while True:
        try:
            answer = ask().strip()
        except EOFError:
            answer = ""
        if not answer:
            raise LauncherError("no pod chosen")
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        narrowed = match_pod_choices(choices, answer)
        if len(narrowed) == 1:
            return narrowed[0]
        _say(
            f"{answer!r} is not one of the choices"
            if not narrowed
            else f"{answer!r} still matches {len(narrowed)} of them"
        )


def resolve_pod(
    kubectl: Kubectl,
    reference: str | None,
    *,
    prompt: bool = True,
    ask: Callable[[], str] | None = None,
    interactive: bool | None = None,
    now: datetime | None = None,
) -> str:
    """The pod every verb that takes one goes through: what did the user mean?

    ``reference`` is ``pod/NAME``, a bare ``NAME``, a substring of one, or
    ``None`` for "show me what there is". The order is the whole design:

    * an exact name is answered by a single ``get pod`` and never listed, so the
      long form stays as cheap and as privilege-light as it always was;
    * one substring hit resolves silently but is echoed, because a tool that
      picks a pod for you and does not say which has picked it for the next
      person too;
    * anything else is a question, and a question needs the listing to answer
      from.

    ``interactive`` defaults to whether stdin is a tty, and that default is the
    load-bearing one. podbench is run from scripts and over ssh, where a prompt
    is not a prompt but a hang — so a namespace-wide listing and a non-zero exit
    is the honest answer there, and ``--no-prompt`` says the same thing on a
    terminal. ``ask`` is the seam the tests drive; it is never the real
    :func:`input` unless the caller has decided prompting is possible.
    """
    query = resolve_pod_name(reference) if reference is not None else None
    if query is not None and kubectl.pod_exists(query):
        return query

    choices = pod_choices(kubectl, now=now)
    if not choices:
        raise LauncherError(f"namespace {kubectl.namespace} has no pods")
    matches = choices if query is None else match_pod_choices(choices, query)
    if not matches:
        raise LauncherError(
            f"no pod in namespace {kubectl.namespace} is named {query!r} or has "
            f"it in its name. What is there:\n{format_pod_choices(choices)}"
        )
    return resolve_among(
        kubectl.namespace,
        matches,
        query,
        prompt=prompt,
        ask=ask,
        interactive=interactive,
    )


def resolve_among(
    namespace: str,
    matches: Sequence[PodChoice],
    query: str | None,
    *,
    noun: str = "pod",
    prompt: bool = True,
    ask: Callable[[], str] | None = None,
    interactive: bool | None = None,
) -> str:
    """One name out of the rows a reference already narrowed to.

    Split out of :func:`resolve_pod` rather than copied into the one caller
    that cannot use it whole: ``dev --delete`` has its own answer for "nothing
    matched" — a teardown that has already happened is exit 0, not a refusal —
    but the echo, the prompt and the non-interactive refusal have to be the
    ones every other verb gives, or two halves of one CLI ask the same question
    differently.

    ``matches`` must be non-empty; ``noun`` is what those rows are, so a
    listing restricted to dev pods does not describe itself as the namespace.
    """
    if len(matches) == 1:
        # Echoed, not assumed: the name is about to appear in a ProxyCommand, in
        # an ssh alias and in the pod's permanent spec, and the user typed four
        # characters of it — or, with no POD at all, none of it, which is the
        # case that most needs saying out loud.
        _say(
            f"the only {noun} in namespace {namespace} is {matches[0].name}"
            if query is None
            else f"{query!r} matched {noun} {matches[0].name}"
        )
        return matches[0].name

    if not prompt or not (
        interactive if interactive is not None else sys.stdin.isatty()
    ):
        raise LauncherError(_ambiguous(namespace, query, matches, prompt, noun))
    _say(
        f"{len(matches)} {noun}s in namespace {namespace}"
        if query is None
        else f"{query!r} matches {len(matches)} {noun}s in namespace {namespace}"
    )
    _say(format_pod_choices(matches))
    _say("which one? [number or name, empty to cancel]")
    return choose_pod(matches, ask if ask is not None else _read_line).name


def _ambiguous(
    namespace: str,
    query: str | None,
    matches: Sequence[PodChoice],
    prompt: bool,
    noun: str = "pod",
) -> str:
    """The refusal that stands in for the prompt when nobody can answer it."""
    why = (
        "--no-prompt was given"
        if not prompt
        else "stdin is not a tty, so podbench will not prompt"
    )
    asked = (
        f"namespace {namespace} has {len(matches)} {noun}s and none was named"
        if query is None
        else f"{query!r} matches {len(matches)} {noun}s in namespace {namespace}"
    )
    return (
        f"{asked}, and {why}:\n{format_pod_choices(matches)}\n"
        "  name one exactly, or give a substring that matches only one."
    )


def _read_line() -> str:
    """One line from the terminal, with the prompt already on stderr.

    :func:`input` writes its own prompt to *stdout*, which is where the
    capability report goes; a caller redirecting stdout to a file would then be
    asked a question they cannot see.
    """
    return input()


def _say(message: str) -> None:
    """Resolution chatter goes to stderr, so stdout stays the report."""
    emit(message, stderr=True)


def _ready_containers(pod_json: Mapping[str, Any]) -> str:
    statuses = [
        as_dict(entry)
        for entry in _as_list(as_dict(pod_json.get("status")).get("containerStatuses"))
    ]
    declared = _as_list(as_dict(pod_json.get("spec")).get("containers"))
    total = len(declared) or len(statuses)
    ready = sum(1 for status in statuses if status.get("ready") is True)
    return f"{ready}/{total}"


def _pod_status(pod_json: Mapping[str, Any]) -> str:
    """What the pod is doing, in one word, as ``kubectl get pods`` says it.

    The phase alone is not it: a pod stuck in ``ImagePullBackOff`` or
    ``CrashLoopBackOff`` is ``Pending``/``Running`` by phase, and the reason is
    the thing that decides whether attaching to it is worth the attempt. Only
    the first waiting container is quoted — this is a column, not the diagnosis,
    and ``podbench status`` is where the diagnosis lives.
    """
    if as_dict(pod_json.get("metadata")).get("deletionTimestamp") is not None:
        return "Terminating"
    status = as_dict(pod_json.get("status"))
    for entry in _as_list(status.get("containerStatuses")):
        waiting = as_dict(as_dict(as_dict(entry).get("state")).get("waiting"))
        reason = _as_str(waiting.get("reason"))
        if reason is not None:
            return reason
    return _as_str(status.get("phase")) or "Unknown"


def _age_of(timestamp: str | None, now: datetime) -> str:
    """``creationTimestamp`` as an age, or ``?`` when it cannot be read.

    Unreadable is not an error: the pod is still a pod the user may want, and a
    listing that refuses to print because one row has an odd stamp is worse than
    one column of it saying so.
    """
    if timestamp is None:
        return "?"
    try:
        created = datetime.fromisoformat(timestamp)
    except ValueError:
        return "?"
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return format_age((now - created).total_seconds())


def format_age(seconds: float) -> str:
    """A duration in the largest unit that fits.

    Coarser than kubectl's two-unit form on purpose: this column exists to tell
    a pod that restarted a minute ago from one that has been up a week, and a
    second unit widens every row for a digit nobody chooses by.

    >>> format_age(45)
    '45s'
    >>> format_age(3 * 3600 + 900)
    '3h'
    >>> format_age(9 * 86400)
    '9d'
    """
    seconds = max(seconds, 0.0)
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


# -- JSON helpers -----------------------------------------------------------


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return cast(list[Any], value)
    return []


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _entry_name(entry: Any) -> str | None:
    return _as_str(as_dict(entry).get("name"))


def spec_env(container: Mapping[str, Any]) -> dict[str, str]:
    """A container spec's ``env`` list as a mapping, literal values only.

    A ``valueFrom`` entry is skipped rather than guessed at: podbench only ever
    reads back variables it wrote itself, and one sourced from a field ref has
    no value here to read.
    """
    return {
        name: value
        for entry in _as_list(container.get("env"))
        if (name := _entry_name(entry)) is not None
        and (value := _as_str(as_dict(entry).get("value"))) is not None
    }


# -- CLI --------------------------------------------------------------------


_Pod = Annotated[
    str | None,
    typer.Argument(
        metavar="POD",
        help="pod/NAME, a bare NAME, or any substring of one. Anything that "
        "does not settle on a single pod lists the namespace and asks",
    ),
]
_NoPrompt = Annotated[
    bool,
    typer.Option(
        "--no-prompt",
        help="never ask which pod: an ambiguous or missing POD is refused with "
        "the candidates instead. Already implied when stdin is not a tty",
    ),
]
_Namespace = Annotated[
    str | None,
    typer.Option(
        "-n",
        "--namespace",
        metavar="NAMESPACE",
        help="namespace (default: the kubeconfig context's own)",
    ),
]
_Context = Annotated[
    str | None, typer.Option("--context", metavar="NAME", help="kubeconfig context")
]
_KubectlBinary = Annotated[
    str, typer.Option("--kubectl", metavar="BIN", help="kubectl binary to use")
]
_ConfigDir = Annotated[
    str | None,
    typer.Option(
        "--config-dir",
        metavar="DIR",
        help="where the generated ssh config and known_hosts live "
        f"(default {DEFAULT_CLIENT_DIR})",
    ),
]
_Identity = Annotated[
    str,
    typer.Option(
        "--identity",
        metavar="KEY",
        help="ssh key to authorise in the seat and name in the generated stanza",
    ),
]
_SshUser = Annotated[
    str | None,
    typer.Option("--ssh-user", metavar="NAME", help="login name to put in the stanza"),
]
_HostAlias = Annotated[
    str | None,
    typer.Option("--host-alias", metavar="NAME", help="ssh Host name for the seat"),
]
_PrintConfig = Annotated[
    bool,
    typer.Option(
        "--print-config",
        help="print the ssh stanza instead of writing it to the config dir",
    ),
]


_Target = Annotated[
    str | None,
    typer.Option("--target", metavar="NAME", help="workload container name"),
]

_Image = Annotated[
    str | None,
    typer.Option(
        "--image",
        metavar="REF",
        # Not the resolved value: DEFAULT_IMAGE is derived from this
        # launcher's own version, so printing it here would advertise
        # `:main` from a checkout as though it were the release tag.
        help=f"debug image (default: ${IMAGE_ENV}, else the image "
        "built from this launcher's version)",
    ),
]

_TargetUid = Annotated[
    int | None,
    typer.Option(
        "--target-uid",
        metavar="UID",
        help="the target's uid, when its pod spec does not say",
    ),
]

_TargetGid = Annotated[
    int | None,
    typer.Option(
        "--target-gid",
        metavar="GID",
        help="the target's gid, when its pod spec does not say. The "
        "seat must share it: __ptrace_may_access compares the group "
        "ids as peers of the user ids, so a seat at the target's uid "
        "in another group can log in and cannot trace. Rarely needed - "
        "podbench measures the target's real gid from /proc and lands "
        "a corrected seat itself - but it costs one container name "
        "instead of two, and it is not overridden by the measurement",
    ),
]

_MaxRung = Annotated[
    Rung | None,
    typer.Option(
        "--max-rung",
        metavar="RUNG",
        help="highest rung of the capability ladder to try: full, "
        "degraded or seat. It is where the walk starts, and the ladder "
        "still falls through the rungs below. Without it a target "
        "whose uid is known and not root has its own rung tried first. "
        "Use `full` to insist on the capability rung - a node with Yama "
        "ptrace_scope >= 1 exempts nothing else - or `degraded` where a "
        "mutating admission policy strips SYS_PTRACE instead of "
        "refusing it. A running seat above the ceiling is not reused",
    ),
]

_Mount = Annotated[
    list[str] | None,
    typer.Option(
        "--mount",
        metavar="CLAIM:MOUNTPATH",
        help="mount a volume the pod already declares into the seat, "
        "named by claim or by volume name. MOUNTPATH defaults to the "
        "application container's own, which Hotfix mode requires it to "
        "equal. Repeatable",
    ),
]

_ForceNew = Annotated[
    bool,
    typer.Option(
        "--new",
        help="add a container even if one is running (its name is permanent)",
    ),
]

_NoCorrectIds = Annotated[
    bool,
    typer.Option(
        "--no-correct-ids",
        help="keep the first seat even when it landed in the wrong "
        "group. Without this, a seat whose measured uid:gid disagrees "
        "with the target's is replaced once by a corrected one, which "
        "spends a second container name for the pod's lifetime - an "
        "ephemeral container's securityContext cannot be changed in "
        "place. Use --target-gid to get it right on the first name",
    ),
]

_NoSeatIdentity = Annotated[
    bool,
    typer.Option(
        "--no-seat-identity",
        help="do not mount the pod's podbench-home volume, which is "
        "otherwise mounted by convention when the pod declares it and "
        "keeps everything the seat writes off the workload's "
        "ephemeral-storage budget. The podbench-identity volume is "
        "never mounted by attach: it needs a subPath per file, which an "
        "ephemeral container may not have - a live-pod seat registers "
        "its own NSS record instead, and needs no volume for it",
    ),
]

_NoProbe = Annotated[
    bool,
    typer.Option(
        "--no-probe",
        help="skip capreport; the report then says nothing was measured",
    ),
]

_Pull = Annotated[
    str,
    typer.Option(
        "--pull",
        metavar="POLICY",
        help="imagePullPolicy for the seat: IfNotPresent (default), "
        "Always or Never. Use Always when iterating on a tag that "
        "moves - `main`, or a branch image - since a node that already "
        "has a copy will otherwise serve it. It cannot be the default: "
        "Always is the one policy that needs a registry, so it breaks "
        "an image side-loaded with `kind load` or `ctr import`",
    ),
]

_Resize = Annotated[
    str | None,
    typer.Option(
        "--resize",
        metavar="MEMORY",
        help="raise the target's memory in place first, as LIMIT or "
        "REQUEST:LIMIT, e.g. 6Gi or 1Gi:6Gi. The request is raised too "
        "where a LimitRange bounds limit/request",
    ),
]

_ResizeCpu = Annotated[
    str | None,
    typer.Option(
        "--resize-cpu",
        metavar="CPU",
        help="raise the target's cpu in place first, as LIMIT or "
        "REQUEST:LIMIT, e.g. 4 or 500m:4",
    ),
]

_Timeout = Annotated[
    float,
    typer.Option(
        "--timeout",
        metavar="SECONDS",
        help="seconds to wait for the seat to start. It bounds that wait and "
        "nothing else: one kubectl call is bounded separately, at "
        f"{DEFAULT_CALL_TIMEOUT:g}s",
    ),
]


def _land(
    kubectl: Kubectl,
    pod: str,
    *,
    public_key: str,
    target: str | None,
    image: str | None,
    target_uid: int | None,
    target_gid: int | None,
    max_rung: Rung | None,
    mounts: Sequence[str],
    force_new: bool,
    correct_ids: bool,
    seat_identity: bool,
    pull: str,
    probe: bool,
    timeout: float,
    resize: str | None,
    resize_cpu: str | None,
    editor: bool = False,
    auto_resize: bool = False,
) -> Session:
    """Resize if asked, land the seat, and fold both outcomes into the report.

    Shared by ``attach`` and ``vscode`` because the seat is the same seat: the
    two verbs differ in what they spend on it and what they do afterwards, not
    in how it lands.

    The resize goes first, and not merely early — the headroom has to exist
    before vscode-server starts allocating into a limit podbench cannot reserve,
    and a raise afterwards is a raise the editor has already overrun.

    ``auto_resize`` is the whole of the difference. ``attach`` never passes it:
    nothing there decides to spend the pod's memory, so a resize happens only
    where a flag asked for one, which is #54's decision. ``vscode`` does,
    because the editor it is about to open is the one cost measured not to fit
    (:data:`podbench.resize.EDITOR_HEADROOM`), and the number that fixes it is
    arithmetic on a headroom this code already reads.

    ``editor`` is the separate question of whether that cost is about to be
    *spent*, and the two come apart at ``--no-resize``: declining the raise is
    not declining to be told the pod cannot hold what is being opened into it.
    """
    warnings: list[str] = []
    if resize is None and auto_resize:
        resize, note = _editor_memory(kubectl, pod, target)
        if note is not None:
            warnings.append(note)
    if resize is not None or resize_cpu is not None:
        # Re-read rather than reuse `_editor_memory`'s copy: `try_resize` needs
        # the pod as it stands at the moment it patches, and an explicit
        # `--resize` reaches here having read nothing at all.
        pod_json = kubectl.get_pod(pod)
        warnings.append(
            try_resize(
                kubectl,
                pod,
                target_container_name(pod_json, target),
                resize,
                cpu=resize_cpu,
                pod_json=pod_json,
            )
        )

    session = attach(
        kubectl,
        pod,
        target=target,
        image=image or os.environ.get(IMAGE_ENV, DEFAULT_IMAGE),
        public_key=public_key,
        target_uid=target_uid,
        max_rung=max_rung,
        mounts=mounts,
        target_gid=target_gid,
        force_new=force_new,
        correct_ids=correct_ids,
        seat_identity=seat_identity,
        pull_policy=_pull_policy(pull),
        probe=probe,
        timeout=timeout,
    )
    if editor:
        # Checked after the seat has landed rather than before, because this is
        # the headroom the editor will actually meet: it is read again inside
        # `attach`, with the seat in the sum. It is also the only thing that
        # reports a raise which did not take - `--no-resize` declined one and a
        # container holding a `resources.claims` entry refuses every one (#107)
        # - so it is keyed on opening an editor and never on having resized.
        editor_note = headroom_note(
            session.headroom, needed=EDITOR_HEADROOM, template=EDITOR_HEADROOM_WARNING
        )
        if editor_note is not None:
            warnings.append(editor_note)
    return replace(session, warnings=(*session.warnings, *warnings))


def _storage_note(pod_json: Mapping[str, Any], session: Session) -> str | None:
    """Where vscode-server's 1.1-1.3 GB is about to land, when that is a hazard.

    Two ways it lands on the workload's ephemeral-storage budget, and they read
    identically from the terminal, so they are told apart here: the pod declares
    no home volume at all, or it declares one that this seat's rung cannot use.
    Silent otherwise - a seat writing into the volume is the case the volume was
    deployed for, and saying so would be a warning about something working.

    Asked of ``session`` rather than of the spec because the rung is measured:
    :attr:`Session.root_seat` reads the seat's own ``/proc/self/status`` where it
    could, which is the same reason :func:`seat_layout` does not trust the rung
    label a mutating webhook may have rewritten.
    """
    if SEAT_HOME_VOLUME not in declared_volumes(pod_json):
        return EDITOR_STORAGE_WARNING.format(volume=SEAT_HOME_VOLUME)
    if session.root_seat:
        return EDITOR_ORPHAN_HOME_WARNING.format(
            volume=SEAT_HOME_VOLUME, home=ROOT_HOME
        )
    return None


def _editor_memory(
    kubectl: Kubectl, pod: str, target: str | None
) -> tuple[str | None, str | None]:
    """The memory limit ``vscode`` will ask the target for, and what to say.

    Both halves are optional and they are independent: a pod with room needs
    neither, and a pod nobody can measure needs the second alone.

    The measurement is the same one ``attach`` prints on its ``memory`` row, and
    reading it here costs one extra ``kubectl top pod`` before the seat lands.
    That the seat is not yet in the sum errs by 13-23 MiB
    (:data:`podbench.resize.SEAT_FOOTPRINT`) against a 1215 MiB threshold, which
    is under a rounding step of the answer.

    An unmeasured pod is the one case that earns a line without an action. It is
    not a hazard - :attr:`podbench.resize.Headroom.summary` states it as
    unmeasured on the report's own row, and a caution there would fire on every
    cluster with no metrics-server - but this verb undertook to size the pod,
    and silently not doing so is a promise quietly dropped.
    """
    pod_json = kubectl.get_pod(pod)
    headroom = measure_headroom(kubectl, pod, pod_json)
    if headroom.limit is None:
        # No ceiling for the seat to share, so nothing to run out of and no
        # limit worth raising. The report's memory row already says so.
        return None, None
    if headroom.used is None:
        return None, EDITOR_UNMEASURED_WARNING.format(
            needed=format_memory(EDITOR_HEADROOM)
        )
    workload = target_container_name(pod_json, target)
    current = as_dict(_container_resources(pod_json, workload).get("limits")).get(
        MEMORY
    )
    wanted = editor_limit(
        headroom, None if current is None else parse_quantity(str(current))
    )
    if wanted is None:
        return None, None
    free = headroom.free
    assert free is not None  # noqa: S101 - `editor_limit` answered on it
    return format_memory(wanted), EDITOR_RESIZE_NOTE.format(
        free=format_memory(free),
        needed=format_memory(EDITOR_HEADROOM),
        container=workload,
        limit=format_memory(wanted),
    )


def _editor_step(note: str) -> None:
    """One line of :func:`podbench.editor.open_seat`'s progress, laid out.

    Two shapes, and they must not be laid out alike.

    A **step** opens with ``[ok]``/``[warn]``/``[FAIL]``
    (:func:`podbench.editor.is_step`) and wraps under its own tick, so a
    continuation cannot be read as the next step. Hung under the tick rather
    than bulleted because several of these notes carry a ` - ` of their own,
    and a wrapped line beginning with one under a bulleted list reads as a new
    item; the indent cannot be forged that way.

    Anything else is the **seat's own stderr**, relayed by
    :func:`podbench.editor._relay`, and it is printed exactly as it arrived.
    It used to go through the same wrap, which collapses whitespace and breaks
    on spaces - and one of those relayed lines is the two-line injection
    command whose first line ends in a continuation ``\\`` that means nothing
    once something follows it.
    """
    if not is_step(note):
        # As a `Text`, so none of the line rules run over it. This is somebody
        # else's output: `debug-config:` followed by an indent is a label by
        # `console._LABEL`'s reckoning, and drawing it as one would have this
        # report claiming a heading in the middle of a relayed sentence.
        emit([Text(f"      {note}")])
        return
    tick, _, rest = note.partition(" ")
    emit("\n".join(paragraph(rest, first=f"  {tick} ", indent=" " * (3 + len(tick)))))


def _open_editor(
    kubectl: Kubectl,
    session: Session,
    wiring: SshSeat,
    *,
    editor: str,
    provision: Provision,
    runner: Runner | None,
    seat_spec: Mapping[str, Any] | None = None,
) -> None:
    """Hand :func:`podbench.editor.open_seat` what only the launcher knows.

    The folder is :func:`editor_folder`'s, which is the seat's **home** for
    every pod without the hotfix layout and the claim for a seat that carries
    it. The home is taken from the same layout the ProxyCommand is derived from
    rather than hardcoded — a pod that declares
    :data:`podbench.model.SEAT_HOME_VOLUME` moves it — and the guarantee that
    matters either way is that this is never ``/``: the walk from there has no
    bottom and ends the seat.

    ``seat_spec`` is the landed seat's own container spec, and it is a
    parameter rather than another ``get_pod`` because the caller already has
    it. Without it every pod looks unhotfixed, which is the pre-#189 behaviour
    and the safe direction to fail in.

    Each line is printed as it arrives rather than collected: the extension
    install bootstraps vscode-server in the seat, which is a download, and a
    progress report that appears only once it has finished is not one.
    """
    if wiring.alias is None:
        raise EditorError(
            "this seat has no ssh alias, so there is nothing for Remote-SSH "
            "to connect to. The block above names the mechanism and the ways "
            "out; the kubectl exec helpers work now regardless."
        )
    emit("editor")
    home = seat_layout(session).home
    folder, why = editor_folder(session, seat_spec)
    if why is not None:
        # Before the steps rather than after: this is the decision every one of
        # them is carried out against, and `open_seat` reports the folder it
        # opened without ever having been told why it was the folder.
        #
        # A warning when the home won on a hotfixed pod, because that is an
        # editor on code that is not running; an ordinary step when the claim
        # won, because nothing went wrong - the answer was simply not the one
        # the command line asked for.
        _editor_step(f"{WARN if folder == home else OK} {why}")
    open_seat(
        kubectl,
        session.seat,
        alias=wiring.alias,
        folder=folder,
        report=_editor_step,
        editor=editor,
        provision=provision,
        runner=runner,
    )


def _build_app(
    runner: Runner | None, which: Callable[[str], str | None]
) -> typer.Typer:
    app = new_app()

    @app.callback(invoke_without_command=True)
    def root(ctx: typer.Context) -> None:
        """Land a development seat inside a pod and say what it can do."""
        require_subcommand(ctx)

    # `attach_command`, not `attach`: the module-level :func:`attach` is what
    # it calls, and a same-named closure would shadow it into a recursion.
    @app.command(
        name="attach",
        help="add or reconnect a podbench container and print the report",
    )
    def attach_command(
        pod: _Pod = None,
        target: _Target = None,
        image: _Image = None,
        target_uid: _TargetUid = None,
        target_gid: _TargetGid = None,
        max_rung: _MaxRung = None,
        mount: _Mount = None,
        force_new: _ForceNew = False,
        no_correct_ids: _NoCorrectIds = False,
        no_seat_identity: _NoSeatIdentity = False,
        no_probe: _NoProbe = False,
        pull: _Pull = DEFAULT_PULL_POLICY,
        resize: _Resize = None,
        resize_cpu: _ResizeCpu = None,
        identity: _Identity = DEFAULT_IDENTITY,
        ssh_user: _SshUser = None,
        host_alias: _HostAlias = None,
        print_config: _PrintConfig = False,
        timeout: _Timeout = 120.0,
        no_prompt: _NoPrompt = False,
        namespace: _Namespace = None,
        context: _Context = None,
        kubectl: _KubectlBinary = "kubectl",
        config_dir: _ConfigDir = None,
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        # Before the namespace is listed: a missing key refuses this attach
        # whichever pod is chosen, and asking someone to pick one first would
        # spend their answer on it.
        key_path, public_key = read_public_key(identity)
        name = resolve_pod(kube, pod, prompt=not no_prompt)
        session = _land(
            kube,
            name,
            public_key=public_key,
            target=target,
            image=image,
            target_uid=target_uid,
            target_gid=target_gid,
            max_rung=max_rung,
            mounts=mount or (),
            force_new=force_new,
            correct_ids=not no_correct_ids,
            seat_identity=not no_seat_identity,
            pull=pull,
            probe=not no_probe,
            timeout=timeout,
            resize=resize,
            resize_cpu=resize_cpu,
        )
        emit(format_session(session))
        print()
        emit(
            _wire(
                kube,
                session,
                identity=key_path,
                config_dir=config_dir,
                host_alias=host_alias,
                ssh_user=ssh_user,
                print_config=print_config,
            ).note
        )
        raise typer.Exit(0)

    @app.command(
        name="vscode",
        help="land a seat sized and provisioned for an editor, and open it",
    )
    def vscode_command(
        pod: _Pod = None,
        target: _Target = None,
        image: _Image = None,
        target_uid: _TargetUid = None,
        target_gid: _TargetGid = None,
        max_rung: _MaxRung = None,
        mount: _Mount = None,
        force_new: _ForceNew = False,
        no_correct_ids: _NoCorrectIds = False,
        no_seat_identity: _NoSeatIdentity = False,
        no_probe: _NoProbe = False,
        pull: _Pull = DEFAULT_PULL_POLICY,
        resize: _Resize = None,
        resize_cpu: _ResizeCpu = None,
        no_resize: Annotated[
            bool,
            typer.Option(
                "--no-resize",
                help="do not raise the target's memory for the editor. Without "
                "it, a pod with less headroom than vscode-server was measured "
                "to need has the target's memory limit raised to cover it - the "
                "one mutation this verb makes that `--resize MEMORY` would "
                "otherwise have to be typed with a number. A pod that already "
                "has the room is left alone either way",
            ),
        ] = False,
        no_provision: Annotated[
            bool,
            typer.Option(
                "--no-provision",
                help="author whatever fits the target as it stands. Without it, "
                "a Python workload gets debugpy installed where it cannot "
                "import one, and its server started where nothing is listening "
                "on the port the configuration connects to - the two ways a "
                "target reaches F5 with no debugger behind it. Mutates the "
                "workload: ~15 MB of shared ephemeral storage, needs egress "
                "from the pod, ptraces the app for a few seconds, and no "
                "restart survives it",
            ),
        ] = False,
        identity: _Identity = DEFAULT_IDENTITY,
        ssh_user: _SshUser = None,
        host_alias: _HostAlias = None,
        timeout: _Timeout = 120.0,
        no_prompt: _NoPrompt = False,
        namespace: _Namespace = None,
        context: _Context = None,
        kubectl: _KubectlBinary = "kubectl",
        config_dir: _ConfigDir = None,
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        key_path, public_key = read_public_key(identity)
        # Both before the namespace is listed, and `code` for a harsher reason
        # than the key: an ephemeral container's name is permanent, so a run
        # that was always going to end at "no `code`" must not burn one on the
        # way.
        editor = resolve_editor(which)
        name = resolve_pod(kube, pod, prompt=not no_prompt)
        session = _land(
            kube,
            name,
            public_key=public_key,
            target=target,
            image=image,
            target_uid=target_uid,
            target_gid=target_gid,
            max_rung=max_rung,
            mounts=mount or (),
            force_new=force_new,
            correct_ids=not no_correct_ids,
            seat_identity=not no_seat_identity,
            pull=pull,
            probe=not no_probe,
            timeout=timeout,
            resize=resize,
            resize_cpu=resize_cpu,
            editor=True,
            auto_resize=not no_resize,
        )
        # After the seat, because which rung landed is half the question, and
        # before the report, because it is a warning about this pod like every
        # other one on it.
        landed_pod = kube.get_pod(name)
        storage = _storage_note(landed_pod, session)
        if storage is not None:
            session = replace(session, warnings=(*session.warnings, storage))
        emit(format_session(session))
        # Only where a seat was actually landed. On a reconnect the mode is
        # already settled and already reported, so naming the other two would be
        # offering a choice that was made on some earlier day.
        if not session.reused:
            print()
            emit("\n".join(paragraph(OTHER_MODES_NOTE, first="  ", indent="  ")))
        print()
        wiring = _wire(
            kube,
            session,
            identity=key_path,
            config_dir=config_dir,
            host_alias=host_alias,
            ssh_user=ssh_user,
            print_config=False,
            opening=True,
        )
        # Iterate mode is provisioned by construction, not by exec: the seat
        # launches the application itself, so debugpy is installed where the
        # launch configuration needs it and there is no live process in the
        # workload container to inject a server into - `dev_pod_spec` idles that
        # container to `sleep` for exactly this reason. Injecting anyway would
        # succeed against `sleep` and report a debugger nobody can reach.
        in_dev_pod = is_dev_pod(landed_pod)
        if in_dev_pod and not no_provision:
            emit(
                "\n".join(
                    paragraph(DEV_SIDECAR_PROVISION_NOTE, first="  ", indent="  ")
                )
            )
            print()
        opened = False
        try:
            _open_editor(
                kube,
                session,
                wiring,
                seat_spec=ephemeral_container(landed_pod, session.seat.container),
                editor=editor,
                provision=(
                    Provision.NEVER
                    if no_provision or in_dev_pod
                    else Provision.IF_NEEDED
                ),
                runner=runner,
            )
            opened = True
        finally:
            # After the steps, not before them, and this is the whole of the
            # fix for what that block had become. Everything above is the past
            # tense - what was written, installed, opened - and everything here
            # is a thing the reader might do. They used to be interleaved, so
            # the two lines worth pasting sat in the middle of thirteen that
            # were not.
            #
            # In a `finally` because the seat is real whether or not an editor
            # could be pointed at it: a run that ends at "ssh does not reach
            # the seat" still has a stanza, an alias and the exec helpers, and
            # printing how to use them only on success would withhold them from
            # exactly the reader who has to fall back to them.
            print()
            emit("next")
            emit("\n".join(f"  {line}" for line in wiring.note.split("\n")))
            # Only where a window was actually asked for. On a failed run there
            # is nothing to say "if it says ..." about, and offering a remedy
            # for a symptom that cannot arise is how a report loses its
            # authority for the ones that can.
            if opened:
                emit("\n".join(paragraph(CONNECTION_HINT, first="  ", indent="  ")))
        if session.probes:
            # Last, because it is the thing they need at the instant their
            # attention moves to the GUI, and the report that carries the
            # numbers is now several blocks up. A pointer rather than a
            # second copy: two sets of deadlines would be two things to keep
            # true, and the readiness half is the one with no trace
            # afterwards.
            print()
            emit(
                "\n".join(
                    paragraph(
                        EDITOR_PROBE_REMINDER,
                        first=f"{WARNING_LEAD}  ",
                        indent=" " * WARNING_HANG,
                    )
                )
            )
        raise typer.Exit(0)

    @app.command(
        name="ssh-config", help="regenerate the ssh stanza for an existing session"
    )
    def ssh_config(
        pod: _Pod = None,
        identity: _Identity = DEFAULT_IDENTITY,
        ssh_user: _SshUser = None,
        host_alias: _HostAlias = None,
        print_config: _PrintConfig = False,
        no_prompt: _NoPrompt = False,
        namespace: _Namespace = None,
        context: _Context = None,
        kubectl: _KubectlBinary = "kubectl",
        config_dir: _ConfigDir = None,
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        key_path, _ = read_public_key(identity)
        name = resolve_pod(kube, pod, prompt=not no_prompt)
        pod_json = kube.get_pod(name)
        # The owner is the id this verb has to select on. It writes a stanza
        # naming *this* kubeconfig's IdentityFile, so a colleague's seat is the
        # one seat that cannot work - its authorized_keys holds another key -
        # and picking it would emit a stanza whose only failure is `Permission
        # denied (publickey)` (issue #113, on top of #117's superseded seat).
        seat = running_seat(pod_json, owner=kube.whoami())
        if seat is None:
            raise LauncherError(
                f"no running podbench container in {kube.namespace}/{name}; "
                "run `podbench attach` first"
            )
        reference = ContainerRef(PodRef(kube.namespace, name), seat.name)
        workload = seat.target or target_container_name(pod_json)
        # Measured here for the reason `seat_layout` exists: this command
        # regenerates the ProxyCommand, which names sshd's config file by
        # absolute path, and which of the two paths is right is decided by the
        # uid the seat runs as - not by the rung its securityContext reads back
        # as, which is what got that path wrong at DLS (2026-08-16).
        credentials = probe_seat_credentials(kube, reference)
        stated_uid, stated_gid = target_uid_gid(pod_json, workload)
        measured = _measured_rung_of(
            credentials, target_uid=stated_uid, target_gid=stated_gid
        )
        session = Session(
            seat=reference,
            workload=workload,
            siblings=other_containers(pod_json, workload),
            rung=measured or seat.rung,
            rung_measured=measured is not None,
            reused=True,
            uid=seat.uid,
            home=seat.home,
            identity_mounted=seat.identity_mounted,
            owner=seat.owner,
            credentials=credentials,
            # Asked again rather than assumed: this command exists to regenerate
            # a stanza from another machine, where nothing of the original
            # attach is in hand.
            ssh=probe_ssh_identity(kube, reference),
        )
        emit(
            _wire(
                kube,
                session,
                identity=key_path,
                config_dir=config_dir,
                host_alias=host_alias,
                ssh_user=ssh_user,
                print_config=print_config,
            ).note
        )
        raise typer.Exit(0)

    @app.command(help="the podbench containers in one pod and what each supports")
    def status(
        pod: _Pod = None,
        no_prompt: _NoPrompt = False,
        namespace: _Namespace = None,
        context: _Context = None,
        kubectl: _KubectlBinary = "kubectl",
        no_probe: Annotated[
            bool,
            typer.Option(
                "--no-probe",
                help="do not run capreport in the seats; every verdict then "
                "reads `not probed`, which is what this listing has to say "
                "when it has measured nothing",
            ),
        ] = False,
        timeout: Annotated[
            float,
            typer.Option(
                "--timeout",
                metavar="SECONDS",
                help="wait this long for a seat that is still starting before "
                "reporting. The default reports what is there now; pass the "
                "same number `attach --timeout` needed on a cluster whose "
                "image pull is slow. It bounds that wait and nothing else: one "
                f"kubectl call is bounded separately, at {DEFAULT_CALL_TIMEOUT:g}s",
            ),
        ] = 0.0,
        config_dir: _ConfigDir = None,
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        name = resolve_pod(kube, pod, prompt=not no_prompt)
        reference = PodRef(kube.namespace, name)
        present = wait_for_seats(kube, name, timeout=timeout)
        if not present:
            print(f"no podbench containers in {kube.namespace}/{name}")
            raise typer.Exit(0)
        # An `exec` per running seat, which is what makes this verb's verdict a
        # measurement rather than a restatement of the spec: the rung a seat
        # reads back as is what admission left behind, and a stripped
        # `capabilities.add` is indistinguishable from the degraded rung
        # (issue #89). `--no-probe` is for a listing that must touch nothing.
        measured = {} if no_probe else probe_seats(kube, reference, present)
        # Same `--no-probe` gate, because it is one more `exec` per seat and
        # that flag exists for a listing that must touch nothing. Asked at all
        # because a seat's build is not its image tag: `IfNotPresent` over a tag
        # that moves serves whatever the node cached, so the spec cannot answer
        # this and neither can the launcher's own version.
        versions = {} if no_probe else probe_seat_versions(kube, reference, present)
        # Not behind `--no-probe`: that flag is for a listing that must *touch*
        # nothing, and `kubectl logs` runs nothing in the seat. It is the only
        # measurement this verb can still report with the flag on, and the
        # column would otherwise fall back to a securityContext.
        reports = recover_seat_reports(kube, reference, present)
        # Read-only: the config dir is where the ssh alias for these seats is
        # recorded, and reporting one podbench cannot back up would be worse
        # than reporting none. Nothing here writes a stanza.
        emit(
            format_seats(
                reference,
                present,
                directory=client_dir(config_dir),
                measured=measured,
                versions=versions,
                reports=reports,
            )
        )
        raise typer.Exit(0)

    @app.command(
        name="list", help="every pod in the namespace carrying a podbench container"
    )
    def list_pods(
        namespace: _Namespace = None,
        context: _Context = None,
        kubectl: _KubectlBinary = "kubectl",
        config_dir: _ConfigDir = None,
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        found = list_seats(kube)
        if not found:
            print(f"no podbench containers in namespace {kube.namespace}")
            raise typer.Exit(0)
        # `list` used to discard this flag deliberately, on the grounds that it
        # writes no ssh config — and it still writes none. It reads it, because
        # the alias to connect with lives only in the stanza on disk, so a list
        # that ignored the config dir could only guess at the one fact it is
        # asked for.
        directory = client_dir(config_dir)
        # A blank line between pods, because each block is three or four lines
        # of its own and back-to-back they read as one pod with too many seats.
        emit(
            "\n\n".join(
                format_seats(
                    pod,
                    present,
                    directory=directory,
                    reports=recover_seat_reports(kube, pod, present),
                )
                for pod, present in found
            )
        )
        raise typer.Exit(0)

    return app


def _wire(
    kubectl: Kubectl,
    session: Session,
    *,
    identity: str,
    config_dir: str | None,
    host_alias: str | None,
    ssh_user: str | None,
    print_config: bool,
    opening: bool = False,
) -> SshSeat:
    """:func:`emit_ssh_config`, with the flags this CLI spells it with."""
    return emit_ssh_config(
        kubectl,
        session,
        identity=identity,
        config_dir=config_dir,
        host_alias=host_alias,
        user=ssh_user,
        print_config=print_config,
        opening=opening,
    )


def main(
    args: Sequence[str] | None = None,
    *,
    runner: Runner | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> int:
    """Entry point for the cluster-side ``podbench`` verbs.

    ``runner`` is the seam the tests use; the CLI passes none and the calls go
    to the real ``kubectl``, which is what makes auth the kubeconfig's problem
    and not podbench's. It is also why the app is built here rather than at
    import time: every command closes over it. ``which`` is the same seam for
    ``vscode``: whether this laptop has ``code`` on PATH decides what that verb
    does, and a unit test must not answer it from the machine it runs on.

    A degraded seat is a success. Returning non-zero for "the cluster would not
    grant SYS_PTRACE" would make the honest capability report look like a
    failure, which is exactly the outcome the brief asks for instead.
    """
    try:
        return run(_build_app(runner, which), args, prog="podbench")
    except (
        LauncherError,
        KubectlError,
        EphemeralContainerError,
        EditorError,
        TimeoutError,
    ) as error:
        print(f"podbench: {error}", file=sys.stderr)
        return 2
