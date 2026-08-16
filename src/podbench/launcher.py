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
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from .agent import GROUP_PATH, PASSWD_PATH, PUBKEY_ENV
from .budget import (
    PROBE_FAILURE_REASON,
    RESERVATION_NOTE,
    ProbeBudget,
    format_probe_spend,
    memory_budget,
    memory_warning,
    probe_budgets,
    probe_explanation,
    probe_qualifier,
    probe_spend,
    probe_warning,
    storage_warning,
    target_status,
)
from .cli import new_app, require_subcommand, run
from .kubectl import (
    EphemeralContainerError,
    Kubectl,
    KubectlError,
    Runner,
    next_container_name,
    run_subprocess,
)
from .model import (
    DEFAULT_IMAGE,
    IMAGE_ENV,
    SEAT_GROUP_KEY,
    SEAT_HOME_PATH,
    SEAT_HOME_VOLUME,
    SEAT_IDENTITY_VOLUME,
    SEAT_PASSWD_KEY,
    Blocker,
    CapabilityReport,
    ContainerRef,
    Lsm,
    LsmStatus,
    PodRef,
    Rung,
    Verdict,
    as_dict,
)
from .spec import (
    AGENT_COMMAND,
    InvalidSpecError,
    container_id,
    ephemeral_container_spec,
    runs_as_non_root,
    target_uid_gid,
)
from .sshcfg import (
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
    "CAPREPORT_ARGV",
    "CONFIG_D",
    "CONTAINER_BASE",
    "DEFAULT_IMAGE",
    "HOST_KEY_ARGV",
    "LOGIN_USER_ARGV",
    "NON_ROOT_HOME",
    "OOM_WARNING",
    "RESIZE_WARNING",
    "SEAT_IDENTITY_MOUNTS",
    "Feature",
    "LadderStep",
    "LauncherError",
    "PodChoice",
    "SeatIdentity",
    "SeatInfo",
    "Session",
    "SshSeat",
    "attach",
    "capability_report_from_json",
    "choose_pod",
    "client_dir",
    "current_namespace",
    "declared_volumes",
    "default_host_alias",
    "emit_ssh_config",
    "explain_hint",
    "explain_note",
    "features",
    "forget_known_hosts",
    "forget_ssh_config",
    "format_age",
    "format_pod_choices",
    "format_seats",
    "format_session",
    "host_alias_in",
    "identity_paths",
    "kubectl_for",
    "latest_seat",
    "list_seats",
    "main",
    "match_pod_choices",
    "match_pod_names",
    "parse_mount",
    "plan_ladder",
    "pod_choices",
    "probe_spend_note",
    "probe_ssh_identity",
    "read_public_key",
    "resolve_among",
    "resolve_mounts",
    "resolve_pod",
    "resolve_pod_name",
    "run_capreport",
    "ssh_unavailable_note",
    "running_seat",
    "seat_identity_mounts",
    "seat_layout",
    "seats",
    "spec_env",
    "ssh_config_path",
    "ssh_connect_line",
    "ssh_include_line",
    "ssh_stanza",
    "target_container_name",
    "try_resize",
    "write_known_hosts",
    "write_ssh_config",
]

CONTAINER_BASE = "podbench"
"""Base name for the ephemeral container; suffixed ``-1``, ``-2``, … (report 4.2)."""

CAPREPORT_ARGV: tuple[str, ...] = ("podbench", "capreport", "--json")
"""The probe, spelled as the verb rather than as a per-subcommand alias.

``podbench`` unqualified is safe here and nowhere else: ``kubectl exec``
inherits the image's own ``ENV PATH``, which has the venv on it, whereas an ssh
session sources nothing and reaches the same program through
``/usr/local/bin/podbench``."""

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

OOM_WARNING = (
    "podbench shares this pod's memory and ephemeral-storage limits and cannot "
    "reserve its own: an ephemeral container may not carry `resources` at all, "
    "and the field is rejected outright (report 3.9). A vscode-server plus "
    "extensions is a 1.1-1.3 GB working set, so attaching to a tightly limited "
    "pod can OOM-kill the workload or get the whole pod evicted on "
    "ephemeral-storage - and an OOM inside an ephemeral container is "
    "unrecoverable, because it cannot be restarted."
)

RESIZE_WARNING = (
    "--resize raises the target container's memory limit in place (kubectl "
    "patch pod --subresource resize). It is opt-in for two reasons (report "
    "R13). It is only partly proven: three pods, two of them "
    "Deployment-managed - a ReplicaSet reconciles pod existence, not pod "
    "spec, so it does not fight the resize - but one Kubernetes version, and "
    "never against a LimitRange or a ResourceQuota. And the raised limit "
    "lives on the pod, not on its controller: the template still asks for "
    "the old limit and nothing "
    "reconciles the difference, so a rollout, a scale, an image bump or an "
    "eviction regenerates the pod from it and silently reverts the resize."
)

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


def target_container_name(
    pod_json: Mapping[str, Any], requested: str | None = None
) -> str:
    """The workload container podbench points at.

    With no ``--target`` the first container wins, matching ``kubectl exec``,
    so the common single-container pod needs no flag.
    """
    names = [
        name
        for name in (
            _entry_name(entry)
            for entry in _as_list(as_dict(pod_json.get("spec")).get("containers"))
        )
        if name is not None
    ]
    if not names:
        raise LauncherError("pod has no containers")
    if requested is None:
        return names[0]
    if requested not in names:
        raise LauncherError(f"container {requested!r} not in pod (has {names})")
    return requested


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
    the whole reason Patch mode asks for deploy-time cooperation, and it is why
    an unknown name is refused here rather than turned into a mount the API
    server would reject with a message about a volume podbench invented.

    The mountPath is not a free choice either. Patch mode's premise is that the
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
    user has in hand (``hotfix --print-values`` names the claim), while a mount
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

        application = _application_mount(pod_json, workload, volume_name)
        app_path = _as_str(application.get("mountPath"))
        if requested_path is None and app_path is None:
            raise LauncherError(
                f"container {workload!r} does not mount volume {volume_name!r}, "
                "so there is no mountPath to copy: give one explicitly as "
                f"--mount {name}:/path. In Patch mode it must be the path the "
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
                f"container {workload!r} mounts it at {app_path}. Patch mode "
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
    ephemeral-storage budget. A live-pod seat gets its NSS identity from
    ``--seat-gid-root`` instead, and :func:`features` says so where the user is
    looking.

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
        "exists, so podbench cannot add one now. This is exactly why Patch mode "
        "needs the chart's cooperation at deploy time - redeploy the workload "
        f"with a volume bound to claim {name!r}, mounted over the application's "
        "venv path (`podbench hotfix --print-values` emits the volume, the "
        "volumeMount and the seeding initContainer). The pod currently declares: "
        + (", ".join(str(entry) for entry in declared) or "no volumes")
    )


def _application_mount(
    pod_json: Mapping[str, Any], workload: str, volume_name: str
) -> Mapping[str, Any]:
    """The workload container's own mount of ``volume_name``, or ``{}``."""
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

    started_at: str | None = None
    """When the node started this container, ``None`` until it has. The window
    a probe-failure count is asked over: what the *pod* has ever spent is not
    what this session spent, and the seat's own start is the only boundary
    between them that the cluster records."""

    @property
    def running(self) -> bool:
        """Whether this container can still take an ssh session."""
        return self.phase == "running"


def seats(pod_json: Mapping[str, Any], *, base: str = CONTAINER_BASE) -> list[SeatInfo]:
    """Every podbench container this pod carries, live or burnt.

    Dead ones are listed too, deliberately: their names are gone for the pod's
    lifetime and a user looking at ``podbench-4`` deserves to see why 1-3 are
    not reusable.
    """
    statuses = {
        name: as_dict(entry)
        for entry in _as_list(
            as_dict(pod_json.get("status")).get("ephemeralContainerStatuses")
        )
        if (name := _entry_name(entry)) is not None
    }
    found: list[SeatInfo] = []
    for entry in _as_list(as_dict(pod_json.get("spec")).get("ephemeralContainers")):
        container = as_dict(entry)
        name = _entry_name(container)
        if name is None or not (name == base or name.startswith(f"{base}-")):
            continue
        status = statuses.get(name, {})
        phase, detail = _phase_of(status)
        found.append(
            SeatInfo(
                name=name,
                rung=rung_of_spec(container),
                phase=phase,
                detail=detail,
                uid=_as_int(as_dict(container.get("securityContext")).get("runAsUser")),
                image=_as_str(container.get("image")),
                target=_as_str(container.get("targetContainerName")),
                home=spec_env(container).get("HOME"),
                identity_mounted=_mounts_volume(container, SEAT_IDENTITY_VOLUME),
                started_at=_landed_at(status),
            )
        )
    return found


def _landed_at(status: Mapping[str, Any]) -> str | None:
    """When the node started this seat's container, running or since dead.

    A terminated seat keeps its ``startedAt``, and that case is the one worth
    reading: if a liveness streak completed, the restart it caused is what
    killed the seat, and the events either side of this stamp are the only
    place that shows.
    """
    state = as_dict(status.get("state"))
    for key in ("running", "terminated"):
        started = as_dict(state.get(key)).get("startedAt")
        if isinstance(started, str) and started:
            return started
    return None


def running_seat(
    pod_json: Mapping[str, Any], *, base: str = CONTAINER_BASE
) -> SeatInfo | None:
    """The podbench container a reconnect should reuse, if there is one."""
    for seat in seats(pod_json, base=base):
        if seat.running:
            return seat
    return None


def rung_of_spec(container: Mapping[str, Any]) -> Rung:
    """Which rung an existing container was authored at.

    Read back from the spec rather than remembered, so a reconnect from another
    machine — or from another user — describes the seat correctly.
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


def _phase_of(status: Mapping[str, Any]) -> tuple[str, str]:
    state = as_dict(status.get("state"))
    running = as_dict(state.get("running"))
    if running:
        return "running", f"since {running.get('startedAt', 'unknown')}"
    waiting = as_dict(state.get("waiting"))
    if waiting:
        return "waiting", f"{waiting.get('reason', '?')}: {waiting.get('message', '')}"
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


def plan_ladder(
    pod_json: Mapping[str, Any],
    container: str,
    *,
    target_uid: int | None = None,
) -> tuple[tuple[Rung, str | None], ...]:
    """Order the rungs, pre-skipping the ones that cannot possibly land.

    The second element of each pair is ``None`` to mean "attempt it", or the
    reason it was skipped.

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
    value of the degraded rung (report 4.5).
    """
    uid, _ = target_uid_gid(pod_json, container)
    if target_uid is not None:
        uid = target_uid

    full: str | None = None
    if runs_as_non_root(pod_json, container):
        full = (
            "the pod sets runAsNonRoot: true, so the kubelet would refuse a root "
            "container with CreateContainerConfigError after the API server had "
            "accepted it - and burn the container name doing so"
        )

    degraded: str | None = None
    if uid is None:
        degraded = (
            "the target's uid is not in the pod spec, so this rung cannot be "
            "authored without guessing; re-run with --target-uid once the "
            "capability report below has read it from /proc"
        )
    elif uid == 0:
        degraded = "the target runs as root, which runAsNonRoot: true cannot express"

    return ((Rung.FULL, full), (Rung.DEGRADED, degraded), (Rung.SEAT, None))


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
    reused: bool
    uid: int | None = None
    """``runAsUser`` as actually pinned in the container's securityContext —
    ``None`` when the seat rung pinned nothing and the image's own user wins."""

    home: str | None = None
    """``$HOME`` as pinned in the container's env, ``None`` for the image's own.
    :func:`seat_layout` derives every path the ProxyCommand names from it."""

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

    @property
    def pod(self) -> PodRef:
        return self.seat.pod


def attach(
    kubectl: Kubectl,
    pod_reference: str,
    *,
    target: str | None = None,
    image: str = DEFAULT_IMAGE,
    public_key: str | None = None,
    target_uid: int | None = None,
    mounts: Sequence[str] = (),
    force_new: bool = False,
    seat_gid_root: bool = False,
    seat_identity: bool = True,
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

    ``seat_gid_root`` lands the non-root rungs with ``runAsGroup: 0``, which is
    what allows the seat to give itself the NSS identity sshd insists on. Opt-in
    because it changes the seat's group, not because a cluster would refuse it.

    ``seat_identity`` mounts the pod's home volume when it has one
    (:func:`seat_identity_mounts`). It is on by default and has no effect on a
    pod that declares none, which is the point: the volume can only be there
    deliberately, so its presence is the request and there is nothing for the
    user to remember. ``--no-seat-identity`` turns it off for the case where the
    seat is wanted with nothing of the pod's mounted into it.

    The pod's identity volume is *never* mounted here, however deliberately it
    was declared: it takes a ``subPath`` per file and an ephemeral container may
    not have one. Where the pod declares it, the capability report says so and
    names ``--seat-gid-root`` as the live-pod route to the same identity.

    The seat is always asked whether it has that identity, ``probe`` or not:
    ``probe`` governs the capability report, while this decides whether an ssh
    stanza is worth printing at all, and a stanza that cannot work is the one
    output worse than none.
    """
    pod = resolve_pod_name(pod_reference)
    pod_json = kubectl.get_pod(pod)
    workload = target_container_name(pod_json, target)
    warnings: list[str] = []
    volume_mounts, mount_warnings = resolve_mounts(pod_json, workload, mounts)
    warnings.extend(mount_warnings)
    if seat_identity:
        convention, convention_warnings = seat_identity_mounts(pod_json, workload)
        warnings.extend(convention_warnings)
        volume_mounts = _merge_mounts(volume_mounts, convention)
    identity_declared = SEAT_IDENTITY_VOLUME in declared_volumes(pod_json)

    existing = running_seat(pod_json)
    if existing is not None and not force_new:
        session = Session(
            seat=ContainerRef(PodRef(kubectl.namespace, pod), existing.name),
            workload=existing.target or workload,
            rung=existing.rung,
            reused=True,
            uid=existing.uid,
            home=existing.home,
            identity_mounted=existing.identity_mounted,
            steps=(
                LadderStep(
                    existing.rung,
                    True,
                    f"reconnected to the running container ({existing.detail})",
                    existing.name,
                ),
            ),
        )
        if public_key is not None:
            warnings.append(
                "reconnected to an existing container: its authorized_keys was "
                "written when it started, so a new ssh key needs --new"
            )
        if mounts:
            warnings.append(
                "reconnected to an existing container: an ephemeral container's "
                "volumeMounts are fixed when it is created and cannot be added "
                "to, so --mount only takes effect on a seat landed with --new"
            )
        if seat_gid_root:
            warnings.append(
                "reconnected to an existing container: its securityContext is "
                "fixed for the pod's lifetime, so --seat-gid-root only takes "
                "effect on a seat landed with --new"
            )
    else:
        session = _walk_ladder(
            kubectl,
            pod=pod,
            pod_json=pod_json,
            workload=workload,
            image=image,
            public_key=public_key,
            target_uid=target_uid,
            volume_mounts=volume_mounts,
            seat_gid_root=seat_gid_root,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    # On the headroom this pod actually has, not on the fact that seats share
    # one. podbench holds the limit and the requests here, so a pod with room
    # is told nothing and a pod without it is told how much it is short and
    # what to type - where the unconditional version printed seven lines about
    # tightly limited pods immediately after `--resize 4Gi` had raised the
    # limit, and so said the opposite of what podbench knew (issue #54).
    memory_note = memory_warning(memory_budget(pod_json))
    if memory_note is not None:
        warnings.append(memory_note)
    # The other half of the caution these two replaced. `OOM_WARNING` named two
    # hazards, and gating both on the memory one would have dropped the disk
    # one - which report 3.8 calls the constraint of the two, and whose failure
    # is the worse: an ephemeral-storage overrun evicts the whole pod.
    storage_note = storage_warning(pod_json)
    if storage_note is not None:
        warnings.append(storage_note)
    # Read from the pod spec rather than warned about in general terms: every
    # number is already in hand, so this is the deadline on *this* pod and not
    # a caution about probed pods. It is stated before anyone sets a
    # breakpoint, because the readiness half of it is invisible afterwards -
    # and it is the windows and the stakes only, because the arithmetic they
    # came from is what `podbench status --explain` is for.
    budgets = probe_budgets(pod_json, session.workload)
    probe_note = probe_warning(budgets)
    if probe_note is not None:
        warnings.append(probe_note)
    # Not also a warning: `features` reports it under "supports", which is where
    # "what this seat can do and which mechanism decided" belongs, and
    # `ssh_unavailable_note` prints the way out in place of the stanza. A third
    # copy in the warning block would be the only thing said three times. The
    # same goes for the declared-but-unusable identity volume, which the ssh
    # seat's line carries for exactly this reason.
    session = replace(
        session,
        identity_declared=identity_declared,
        probes=budgets,
        ssh=probe_ssh_identity(kubectl, session.seat),
    )

    if probe:
        report, probe_warnings = run_capreport(kubectl, session.seat)
        session = replace(session, report=report)
        warnings.extend(probe_warnings)

    return replace(session, warnings=(*session.warnings, *warnings))


def _walk_ladder(
    kubectl: Kubectl,
    *,
    pod: str,
    pod_json: Mapping[str, Any],
    workload: str,
    image: str,
    public_key: str | None,
    target_uid: int | None,
    volume_mounts: Sequence[Mapping[str, Any]],
    seat_gid_root: bool,
    timeout: float,
    poll_interval: float,
) -> Session:
    uid, gid = target_uid_gid(pod_json, workload)
    if target_uid is not None:
        uid = target_uid
    cid = container_id(pod_json, workload)
    name = next_container_name(pod_json, CONTAINER_BASE)
    steps: list[LadderStep] = []

    for rung, skip in plan_ladder(pod_json, workload, target_uid=target_uid):
        if skip is not None:
            steps.append(LadderStep(rung, False, skip))
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
                    pod_json, public_key, rung, home=_seat_home(volume_mounts, rung)
                ),
                volume_mounts=volume_mounts,
                seat_gid_root=seat_gid_root,
            )
        except InvalidSpecError as error:
            # Refused before the cluster saw it, so no container name is burnt.
            steps.append(LadderStep(rung, False, str(error)))
            continue

        try:
            kubectl.add_ephemeral_container(pod, spec)
        except KubectlError as error:
            if not error.is_psa_ptrace_denial:
                raise
            steps.append(
                LadderStep(
                    rung,
                    False,
                    "Pod Security Admission refused it synchronously: "
                    f"{error.stderr.strip() or error.stdout.strip()}",
                )
            )
            continue

        try:
            started = kubectl.wait_for_ephemeral_container(
                pod, name, timeout=timeout, poll_interval=poll_interval
            )
        except EphemeralContainerError as error:
            hint = ""
            if _KUBELET_NON_ROOT_MESSAGE in error.message:
                hint = " (the pod's runAsNonRoot policy; read it up front next time)"
            steps.append(
                LadderStep(
                    rung,
                    False,
                    f"the kubelet refused it asynchronously ({error.reason}): "
                    f"{error.message}{hint}",
                    name,
                )
            )
            # The name died with the container, so the next rung needs a new one.
            name = next_container_name(kubectl.get_pod(pod), CONTAINER_BASE)
            continue

        steps.append(LadderStep(rung, True, f"running since {started}", name))
        return Session(
            seat=ContainerRef(PodRef(kubectl.namespace, pod), name),
            workload=workload,
            rung=rung,
            reused=False,
            # What was *pinned*, not what was asked for: the full rung is root
            # whatever the target is, and the seat rung drops a root target's
            # uid rather than pair it with runAsNonRoot.
            uid=_as_int(as_dict(spec.get("securityContext")).get("runAsUser")),
            home=spec_env(spec).get("HOME"),
            identity_mounted=_mounts_volume(spec, SEAT_IDENTITY_VOLUME),
            steps=tuple(steps),
        )

    raise LauncherError(
        "no rung of the capability ladder was admitted:\n"
        + "\n".join(f"  {step.rung.value}: {step.detail}" for step in steps)
    )


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
    home: str | None,
) -> dict[str, str]:
    env: dict[str, str] = {}
    if home is not None:
        # Root already has a writable home the agent's layout knows about; a
        # non-root seat has none, so both halves are pointed at the same one.
        env["HOME"] = home
    if public_key is not None:
        env[PUBKEY_ENV] = public_key.strip()
    node = _as_str(as_dict(pod_json.get("spec")).get("nodeName"))
    if node is not None:
        # Yama differs per node by kernel flavour, so a report that cannot name
        # the node cannot explain "attach worked yesterday" (report 3.13/4.5).
        env["PODBENCH_NODE_NAME"] = node
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
            "capreport produced no parsable JSON, so the capability report "
            "below is missing: "
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
    # Blanks dropped here rather than rendered: a note is the one part of the
    # report the launcher does not author, an image and a launcher are versioned
    # separately, and an empty string carries nothing while still printing a
    # heading with nothing under it.
    notes = [str(note) for note in _as_list(payload.get("notes")) if str(note).strip()]
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
        lsm=_lsm_from_json(payload),
        self_uid=_as_int(payload.get("self_uid")) or 0,
        target_uid=_as_int(payload.get("target_uid")),
        target_pid=_as_int(payload.get("target_pid")),
        node_name=_as_str(payload.get("node_name")),
        child_attach_ok=_as_bool(payload.get("child_attach_ok")),
        target_attach_ok=_as_bool(payload.get("target_attach_ok")),
        proc_reads=reads,
        notes=notes,
    )


def _lsm_from_json(payload: Mapping[str, Any]) -> LsmStatus:
    """Rebuild the LSM status, including from an image that predates issue #52.

    Such an image emits ``apparmor_profile``, which on any RHEL-family node
    holds an SELinux context — the mislabelling this replaced. The string is
    worth carrying over, so it is; the module it came from is left
    :attr:`Lsm.UNKNOWN`, because guessing it from the shape of the context is
    the mistake, not the fix.
    """
    raw = _as_str(payload.get("lsm"))
    try:
        kind = Lsm(raw) if raw is not None else Lsm.UNKNOWN
    except ValueError:
        # A newer image naming a module this launcher has never heard of. The
        # context still reads, so report it rather than dropping the evidence.
        kind = Lsm.UNKNOWN
    context = _as_str(payload.get("lsm_context")) or _as_str(
        payload.get("apparmor_profile")
    )
    return LsmStatus(kind, context, _as_bool(payload.get("lsm_enforcing")))


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

    short: str = ""
    """The label for the compact report's one-line ``supports``, which lists
    every feature and so has no room for the parenthesis in ``name``."""

    brief: str = ""
    """The single actionable line a *missing* feature earns in that report.

    Empty where the compact report already carries the answer - the blocker is
    on the ``measured`` line, and ``iterate`` is unavailable in attach mode by
    construction rather than by measurement - because a line that repeats what
    is two lines above it is how a report stops being read."""


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
    whether ptrace actually works also depends on Yama, seccomp and whichever
    LSM the node runs, all of which are node-local and were measured inside the
    container (report 4.5).
    """
    report = session.report
    # Qualifies the tick rather than repeating the warning: a bare [x] says the
    # same thing on a pod that will restart under you in twenty seconds as on
    # one that will wait all afternoon, and those are different products. The
    # arithmetic and the way out stay in `status --explain`.
    deadline = probe_qualifier(session.workload, session.probes)
    if report is None:
        unknown = "capreport did not run, so this was not measured"
        return (
            Feature(
                "live attach (gdb -p <pid>)",
                False,
                unknown,
                note=deadline,
                short="live attach",
                brief=unknown,
            ),
            Feature(READS_FEATURE, False, unknown, short="inspect"),
            Feature(LAUNCH_FEATURE, False, unknown, short="launch"),
            _iterate_feature(report),
            *_seat_features(session),
        )
    return (
        Feature(
            "live attach (gdb -p <pid>)",
            report.verdict is Verdict.LIVE_ATTACH,
            report.blocker.explanation,
            note=deadline,
            short="live attach",
            # No brief: the compact report's `measured` line names the blocker
            # two lines below this one, and the paragraph explaining it is what
            # `status --explain` is for.
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
            short="inspect",
            # The measurement itself is the brief: it is already one line, and
            # it is the line that tells a launch-only pod from a read-only one.
            brief=report.reads_summary,
        ),
        Feature(
            LAUNCH_FEATURE,
            report.can_debug_launched,
            "the seat could not ptrace even a child it forked itself, so "
            "ptrace(2) is unusable here and gdb cannot trace an inferior it "
            "starts either"
            if report.child_attach_ok is False
            else "the scratch attach was not measured, so this is not claimed",
            short="launch",
            brief="the seat could not ptrace a child it forked itself, so "
            "ptrace(2) is unusable here"
            if report.child_attach_ok is False
            else "the scratch attach was not measured, so this is not claimed",
        ),
        _iterate_feature(report),
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
        short="ssh seat",
        # The remedy rather than the mechanism: sshd's own words are three
        # lines of NSS, and the reader of a compact report needs the command
        # that fixes it. `status --explain` keeps the mechanism.
        #
        # Unknown is not no, here as everywhere: an image that could not answer
        # gets the fact that nothing was measured, not an instruction to fix
        # something that may not be broken.
        brief=_ssh_brief(identity),
    )
    return (
        ssh_seat,
        # `dbg --launch` used to be listed here, under a tick that is always
        # true. Whether it works is measured, and it has its own line now.
        Feature(
            "exec seat (kubectl exec -- podbench capreport, pids, dbg)",
            True,
            short="exec seat",
        ),
    )


def _ssh_brief(identity: SeatIdentity | None) -> str:
    """The compact report's line for a seat that cannot be ssh'd into."""
    if identity is None:
        return "the seat was not asked whether sshd can resolve a login name"
    if not identity.measured:
        return (
            "the seat did not answer whether sshd can resolve a login name for "
            "its uid, so this is not claimed - the stanza is written anyway"
        )
    return (
        "sshd cannot resolve a login name for this seat's uid - re-attach with "
        "--new --seat-gid-root, which lets the agent register one"
    )


def _identity_note(session: Session, *, usable: bool) -> str:
    """Which mechanism gave this seat an NSS identity, or why none could.

    A pod that declares the identity volume is a pod somebody prepared for
    podbench, so a seat with no login on it looks like the preparation failed.
    It did not: ``attach`` cannot project a file into an ephemeral container at
    all, and this line is where that is said - beside the feature it decides,
    with the flag that does work on a live pod.

    ``usable`` decides how much of that to say. A seat that already has its
    login needs the fact and not the remedy: printing "re-attach with
    --seat-gid-root" under a ticked box would read as an instruction to fix
    something that is working.
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
            f"{cannot}. This seat's login came from somewhere else, so nothing "
            "here needs fixing: a seat landed with --seat-gid-root registers "
            "its own record. The volume is for a seat that is an ordinary "
            "container, which is what `podbench dev` authors"
        )
    return (
        f"{cannot}. On a live pod the seat registers its own record instead: "
        "re-attach with --new --seat-gid-root, which runs it with runAsGroup: "
        f"0 against the image's group-writable {PASSWD_PATH}. The volume is "
        "for a seat that is an ordinary container, which is what `podbench "
        "dev` authors"
    )


def _iterate_feature(report: CapabilityReport | None) -> Feature:
    """Why Observe cannot iterate — and, on the closed rungs, that the other
    two modes are not closed with it.

    A reader who has just watched attach and every read be denied assumes the
    pod is shut to podbench altogether. It is not: `dev` and `hotfix` debug the
    sidecar's *own child*, which is the one attach no policy in play refuses and
    which this very seat has just measured as permitted (issue #52). Saying so
    beside the empty box is what turns a dead end into the next command."""
    return Feature(
        "iterate (edit, relaunch, verify through the Service)",
        False,
        "attach shares a live pod, where killing PID 1 restarts the container "
        "and a liveness probe would kill a stopped one. The relaunch loop needs "
        "a sacrificial dev pod (`podbench dev`), never the live workload.",
        note=_other_modes_note(report),
        short="iterate",
        # Nothing on the common path: `iterate` is unavailable in attach mode
        # by construction, and a line saying so at every attach is the kind
        # nobody reads by the third one. The exception is the rung where the
        # reader has just watched everything be denied and is about to
        # conclude the pod is shut to podbench altogether (issue #52).
        brief=""
        if report is None or report.verdict is not Verdict.LAUNCH_ONLY
        else "`podbench dev` and `podbench hotfix` debug the sidecar's own "
        "child - the attach this seat just measured as permitted",
    )


def _other_modes_note(report: CapabilityReport | None) -> str:
    # Only on the rung that needs it. Under a live-attach verdict this would be
    # noise, and a report people skim is one that has stopped working.
    if report is None or report.verdict is not Verdict.LAUNCH_ONLY:
        return ""
    return (
        "what closed this pod does not close the other two modes: `podbench "
        "dev` and `podbench hotfix` debug the sidecar's own child, the attach "
        "this seat just measured as permitted, so on a pod like this one they "
        "are the way in"
    )


def format_session(
    session: Session,
    *,
    explain: bool = False,
    context: str | None = None,
    binary: str = "kubectl",
) -> str:
    """The capability report, which is the product of an attach.

    Two shapes of the same facts. The default is what ``attach`` prints: every
    conclusion, one line each, and nothing that is true of pods in general. It
    ran to 66 lines on the bench and 85 on Diamond, of which 37 and 43 were
    WARNING blocks, and a report nobody reads to the end is one whose last line
    might as well not be there (issue #54).

    ``explain`` is the long form, printed by ``podbench status --explain``:
    the same features with the mechanism that decided each, the ladder, and
    every warning in full. Nothing is dropped by the compact shape - it is one
    command away, and that command mutates nothing.

    ``context`` and ``binary`` are how this attach reached its cluster, and are
    here only so that the command the compact shape ends with is the one that
    reaches the same cluster again (:func:`explain_hint`).
    """
    if not explain:
        return _compact_session(session, context=context, binary=binary)
    lines = [
        f"seat        {session.seat}"
        + ("  (reconnected)" if session.reused else "  (new)"),
        f"target      {session.workload}",
        f"rung        {session.rung.value} - {session.rung.description}",
    ]
    if session.steps:
        lines.append("ladder")
    for step in session.steps:
        mark = "landed " if step.admitted else "refused"
        lines.extend(
            _paragraph(
                step.detail,
                first=f"  {step.rung.value:<9} {mark}  ",
                indent=" " * 21,
            )
        )

    lines.append("supports")
    for feature in features(session):
        lines.append(f"  [{'x' if feature.available else ' '}] {feature.name}")
        if feature.note:
            lines.extend(f"      {line}" for line in _wrap(feature.note))
        if not feature.available and feature.reason:
            lines.extend(f"      {line}" for line in _wrap(feature.reason))

    report = session.report
    if report is not None:
        lines.append("measured")
        lines.append(f"  verdict     {report.verdict.summary}")
        lines.append(f"  blocker     {report.blocker.value}")
        lines.append(f"  node        {report.node_name or 'unknown'}")
        lines.append(f"  yama        {_yama(report.yama_scope)}")
        lines.append(
            f"  uids        seat {report.self_uid}, target "
            f"{report.target_uid if report.target_uid is not None else '?'}"
        )
        if report.notes:
            lines.append("notes")
            for note in report.notes:
                lines.extend(_paragraph(note, first="  - ", indent="    "))

    for warning in session.warnings:
        lines.append("WARNING")
        lines.extend(f"  {line}" for line in _warning_lines(warning))
    return "\n".join(lines)


def _compact_session(
    session: Session, *, context: str | None = None, binary: str = "kubectl"
) -> str:
    """The report as ``attach`` prints it: conclusions, one line each."""
    lines = [
        f"seat        {session.seat}"
        + ("  (reconnected)" if session.reused else "  (new)"),
        f"target      {session.workload:<10} rung  {session.rung.value} - "
        f"{session.rung.description}",
    ]
    # Only the rungs that were refused. A ladder that landed on its first step
    # is four lines saying nothing happened, and the rung it landed on is on
    # the line above; a refusal is the surprise, and keeps its wording.
    for step in session.steps:
        if not step.admitted:
            lines.extend(
                _paragraph(
                    step.detail,
                    first=f"refused     {step.rung.value}: ",
                    indent=" " * 12,
                )
            )

    present = features(session)
    supported = [feature.short for feature in present if feature.available]
    missing = [feature.short for feature in present if not feature.available]
    summary = ", ".join(supported) or "nothing measurable"
    if missing:
        summary += "   (not: " + ", ".join(missing) + ")"
    lines.extend(_paragraph(summary, first="supports    ", indent=" " * 12))
    for feature in present:
        if not feature.available and feature.brief:
            lines.extend(
                _paragraph(
                    feature.brief,
                    first=f"  no {feature.short}: ",
                    indent=" " * 4,
                )
            )

    report = session.report
    if report is None:
        lines.append("measured    nothing: capreport did not run")
    else:
        lines.extend(
            _paragraph(
                f"{report.verdict.summary} - blocker {report.blocker.value}; "
                f"node {report.node_name or 'unknown'}, "
                f"yama {_yama(report.yama_scope)}, "
                f"uids {report.self_uid}/"
                f"{report.target_uid if report.target_uid is not None else '?'}",
                first="measured    ",
                indent=" " * 12,
            )
        )
        # Kept in full: a note is emitted only when the image found something
        # the launcher has no line for - an unknown blocker, two identical LSM
        # contexts - so it is rare, and it is the part that is not restatable.
        for note in report.notes:
            lines.extend(_paragraph(note, first="note        ", indent=" " * 12))

    # The good news, and only the good news: the deadline itself arrives as a
    # warning below, and both readings have to be stated because "explore
    # freely" and "you have twenty seconds" are different products.
    if not any(budget.in_force for budget in session.probes):
        lines.extend(
            _paragraph(
                probe_qualifier(session.workload, session.probes),
                first="pause       ",
                indent=" " * 12,
            )
        )

    for warning in session.warnings:
        lines.extend(_paragraph(warning, first="! ", indent="  "))
    lines.append(
        f"explain     {explain_hint(session.pod, context=context, binary=binary)}"
    )
    return "\n".join(lines)


def _warning_lines(warning: str) -> list[str]:
    """Wrap a warning, keeping any indented list it carries readable.

    Most warnings are one paragraph and come out of here exactly as ``_wrap``
    left them. The probe budget is the exception: it is one line per probe and
    the numbers only compare down the column, so its newlines and its indent
    have to survive - and an indented line's continuation is hung two further
    columns so it cannot be misread as the next probe.
    """
    lines: list[str] = []
    for raw in warning.split("\n"):
        indent = len(raw) - len(raw.lstrip())
        wrapped = _wrap(raw.strip(), width=max(24, 72 - indent))
        if not wrapped:
            continue
        lines.append(" " * indent + wrapped[0])
        hang = indent + 2 if indent else 0
        lines.extend(" " * hang + line for line in wrapped[1:])
    return lines


def _yama(scope: int | None) -> str:
    if scope is None:
        return "absent (no Yama LSM on this node - not the same as scope 0)"
    return str(scope)


def _paragraph(text: str, *, first: str, indent: str) -> list[str]:
    """Wrap ``text`` with a hanging indent, so a wrapped line is not mistaken
    for a second bullet.

    Text that wraps to nothing keeps its label and loses only the body.
    ``_wrap`` returns no lines at all for an empty string, and the compact
    report puts strings through here that the long form only ever passed to
    ``_warning_lines``, which skips blanks by design - so what used to render as
    nothing became an ``IndexError`` that takes down the whole report, which is
    the only thing an attach has to show for itself. The label is kept rather
    than the line dropped because the caller supplied it and it is the half that
    still means something: a refused rung is a fact even if its detail is empty.

    >>> _paragraph("", first="refused     full: ", indent=" " * 12)
    ['refused     full:']
    """
    wrapped = _wrap(text, width=max(24, 78 - len(indent)))
    if not wrapped:
        return [first.rstrip()]
    return [first + wrapped[0], *(indent + line for line in wrapped[1:])]


def _wrap(text: str, width: int = 72) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


# -- ssh client wiring ------------------------------------------------------


def seat_layout(session: Session) -> SshdLayout:
    """The sshd layout the agent will have chosen inside ``session``'s container.

    It has to be derived, not guessed: the ProxyCommand names sshd's config file
    by absolute path, and the two layouts put it in different places. The
    non-root layout's paths depend only on ``$HOME``, which the launcher pins in
    the container spec and :class:`Session` carries back from it — so an unknown
    uid is still safe here, since every non-full rung carries
    ``runAsNonRoot: true`` and therefore cannot be uid 0.

    The home is read from the seat rather than assumed to be
    :data:`NON_ROOT_HOME`, because a pod that declares
    :data:`podbench.model.SEAT_HOME_VOLUME` moves it, and a ProxyCommand naming
    the config file in the wrong home is a transport that fails at the first
    connection with nothing to say why.
    """
    if session.rung is Rung.FULL:
        return SshdLayout.for_uid(0, home=session.home)
    # Any non-zero uid selects the same non-root layout, so an unpinned seat can
    # be given a placeholder: the paths that follow come from `home` alone.
    return SshdLayout.for_uid(
        session.uid or _UNPINNED_UID, home=session.home or NON_ROOT_HOME
    )


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
            *(f"  {line}" for line in _wrap(detail)),
            "  ways out:",
            "    - land a seat that registers one itself, with GID 0:",
            f"        podbench attach {seat.pod.name} "
            f"-n {seat.pod.namespace} --new --seat-gid-root",
            f"      the image makes {PASSWD_PATH} group-writable, so a seat with gid 0",
            "      appends its own record. This is the route on a live pod, or",
            "    - run the target as a uid the debug image has an account for, or",
            "    - debug in a dev pod (`podbench dev`), whose seat is an ordinary",
            "      container and can be given files.",
            *(
                f"  {line}"
                for line in _wrap(
                    f"a {SEAT_IDENTITY_VOLUME!r} volume does not help here, "
                    "declared or not: projecting a passwd file takes a subPath "
                    "per file, and the API server forbids subPath on an "
                    "ephemeral container."
                )
            ),
            "  the rest of the seat needs no ssh and works now:",
            f"    kubectl exec {target} -- podbench capreport",
            f"    kubectl exec {target} -- podbench pids",
            f"    kubectl exec -it {target} -- bash",
        ]
    )


def default_host_alias(pod: PodRef) -> str:
    """A stable ssh ``Host`` name for a pod."""
    return f"podbench-{pod.namespace}-{pod.name}"


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


def ssh_config_path(directory: Path, pod: PodRef) -> Path:
    """The file :func:`write_ssh_config` writes for ``pod``.

    One place, because two commands now have to agree on it: ``attach`` and
    ``dev`` write it, and ``dev --delete`` removes it. A teardown that guessed
    the name would silently leave a stanza behind pointing at a pod that no
    longer exists.
    """
    return directory / CONFIG_D / f"{pod.namespace}-{pod.name}.conf"


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
    pod: PodRef, *, directory: Path, alias: str | None = None
) -> list[str]:
    """Remove the client wiring podbench wrote for ``pod``; say what went.

    Only ever called for a seat that cannot come back — a deleted dev pod. An
    ``attach`` seat is reconnectable for the pod's lifetime, so its stanza is
    regenerated rather than removed.
    """
    removed: list[str] = []
    path = ssh_config_path(directory, pod)
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
) -> SshSeat:
    """Generate the client stanza for a landed seat, and write it.

    Every seat podbench lands goes through here, whatever authored it: an
    ephemeral container from ``attach`` and the ordinary sidecar of a
    ``podbench dev`` pod differ in how they were created and in nothing this
    function does. Which is the point — the host-key probe, the ``known_hosts``
    entry, the ProxyCommand and the ``Include`` advice were written once and a
    second copy of them would be a second thing to keep true.

    ``user`` overrides the login name. It is measured for a dev sidecar, whose
    identity comes from a projected passwd record naming whatever the chart
    chose; the default is the one ``attach`` has always used, where the seat's
    rung decides.
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
        alias=host_key_alias(pod_uid),
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

    alias = host_alias or default_host_alias(session.pod)
    login = user or ("root" if session.rung is Rung.FULL else DEFAULT_SEAT_USER)
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
    path = write_ssh_config(stanza, ssh_config_path(directory, session.pod))
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
                f"then:  ssh {alias}   (or Remote-SSH: Connect to Host -> {alias})",
                # The VS Code debugger needs a launch.json whose pid,
                # sysroot-prefixed program and setup ordering are all things
                # this launcher already knows and a human cannot guess; every
                # wrong answer fails silently rather than erroring.
                "to debug in VS Code, run `podbench debug-config` in the seat "
                "(writes .vscode/launch.json)",
            ]
        ),
        alias=alias,
        path=path,
    )


# -- in-place resize --------------------------------------------------------


def try_resize(kubectl: Kubectl, pod: str, container: str, memory: str) -> str:
    """Raise the target container's memory limit in place; never fatal.

    A strategic-merge patch, not the JSON patch the rest of podbench prefers:
    resize addresses containers by name, and a JSON patch would need the
    container's *index*, which is positional and would silently resize the wrong
    container if the pod spec changed under us.

    Failure is reported rather than raised — the mitigation is only partly
    proven (report R13) and a seat that lands with a loud warning beats one that
    does not land at all.

    Success is reported just as loudly, because it leaves the pod diverged from
    the controller that owns it (report R13): the raised limit is on the pod
    object alone, so the next thing to regenerate the pod from an unchanged
    template takes it away again with no other symptom than a seat that OOMs.
    One line of it, though - what was raised, that it is yours to put back, and
    that it can vanish without anyone doing anything. Why a ReplicaSet does not
    fight it and what a LimitRange might do are in ``RESIZE_WARNING``, which
    ``podbench status --explain`` prints.
    """
    body = {
        "spec": {
            "containers": [
                {"name": container, "resources": {"limits": {"memory": memory}}}
            ]
        }
    }
    try:
        kubectl.patch("pod", pod, body, patch_type="strategic", subresource="resize")
    except KubectlError as error:
        return (
            f"in-place resize to {memory} was refused, so podbench is sharing "
            f"the pod's existing limits: {error.stderr.strip() or error}"
        )
    return (
        f"resized {container} to {memory} - restore it on detach; a rollout, a "
        "scale or an eviction reverts it silently"
    )


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
    result = run(argv)
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
        present = seats(pod_json)
        if present:
            found.append((PodRef(kubectl.namespace, name), present))
    return found


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


def ssh_connect_line(directory: Path, pod: PodRef) -> str:
    """What to type to sit in ``pod``'s seat — read from disk, never derived.

    :func:`default_host_alias` is a pure function of the :class:`PodRef` and so
    is tempting to recompute here, but ``attach --host-alias NAME`` overrides it
    and the cluster records that choice nowhere: a derived alias would be
    silently wrong for exactly the users who picked their own. So the answer is
    read back out of the stanza, which holds the ``Host`` line ssh will actually
    match. A listing that names an alias which does not work is worse than one
    that names none, so every case that cannot produce a real alias says which
    file it looked in instead.
    """
    path = ssh_config_path(directory, pod)
    try:
        stanza = path.read_text()
    except FileNotFoundError:
        # The stanza is client-side state: nothing in the pod carries it. A seat
        # landed from another machine, or by a colleague, is normal and reaches
        # here, and `ssh-config` exists to mint the missing half.
        return (
            f"  no ssh config here: podbench ssh-config -n {pod.namespace} {pod.name}"
        )
    except OSError as error:
        return f"  cannot read {path}: {error.strerror or error}"
    alias = host_alias_in(stanza)
    if alias is None:
        return (
            f"  no Host line in {path}: "
            f"podbench ssh-config -n {pod.namespace} {pod.name}"
        )
    return f"  ssh {alias}"


def format_seats(pod: PodRef, present: Sequence[SeatInfo], *, directory: Path) -> str:
    """One pod's podbench containers, for ``status`` and ``list``.

    ``directory`` is the client config dir, and is read — never written — for
    the one fact a listing is asked for and the cluster cannot answer: which
    alias to ssh to. Required rather than defaulted so a caller cannot drop the
    connect line by omission.
    """
    lines = [str(pod)]
    for seat in present:
        lines.append(
            f"  {seat.name:<12} {seat.phase:<11} {seat.rung.value:<9} "
            f"{seat.rung.description}"
        )
        lines.append(f"  {'':<12} {seat.detail}")
    lines.append(ssh_connect_line(directory, pod))
    return "\n".join(lines)


def latest_seat(
    pod_json: Mapping[str, Any], present: Sequence[SeatInfo]
) -> tuple[SeatInfo | None, str]:
    """The seat a report about this pod is about, and the container it watches.

    The running one, or the last one landed if none is running: a burnt seat
    still names the target its budget was spent against, and falling back to
    the pod's first container would quietly report a different container's
    numbers on a multi-container pod.
    """
    latest = next((seat for seat in reversed(present) if seat.running), None) or (
        present[-1] if present else None
    )
    container = (latest.target if latest is not None else None) or (
        target_container_name(pod_json)
    )
    return latest, container


def probe_spend_note(
    kubectl: Kubectl,
    pod: str,
    pod_json: Mapping[str, Any],
    present: Sequence[SeatInfo],
) -> str:
    """What the target's probes have already cost, for ``status`` to print.

    ``attach`` states the budget on the way in and then never mentions it
    again, which leaves the one number that decides whether the seat survives —
    how many liveness failures have gone by — to be counted by hand out of
    ``kubectl get events``. It is asked for here, where a session already
    running is being looked at, and it is the only budget podbench has that is
    spent without anything saying so.

    The events are a separate RBAC verb from the pods, so a namespace that
    grants one and not the other must still get its listing: the failure
    reports itself and costs this block only.
    """
    # Both of these are named in the block's own header rather than implied:
    # `status` lists every seat the pod has ever carried and only one of them
    # sets this window, and a seat that declares no targetContainerName gets
    # the same first-container default the rest of the launcher uses.
    latest, container = latest_seat(pod_json, present)
    if not (budgets := probe_budgets(pod_json, container)):
        return (
            f"probes on {container!r}: none declared, so there is no budget to "
            "spend - nothing removes this pod from a Service or restarts it "
            "while it is stopped in a debugger"
        )
    try:
        events = kubectl.list_events(pod, reason=PROBE_FAILURE_REASON)
    except KubectlError as error:
        # `list`, not `get`: the call names no event, so RBAC decides it on the
        # list verb - which is what the refusal itself says - and telling the
        # reader to ask for `get` on events has them refused a second time.
        return (
            f"probes on {container!r}: what they have cost cannot be read - "
            f"{error}. The failures are only in the pod's events, so this "
            "needs `list` on events in this namespace; the budget itself is in "
            "the pod spec and `podbench attach` states it"
        )
    landed = latest.started_at if latest is not None else None
    return format_probe_spend(
        container,
        budgets,
        probe_spend(events, container, since=landed),
        since=landed,
        seat=latest.name if latest is not None else None,
        target=target_status(pod_json, container),
    )


def explain_hint(
    pod: PodRef, *, context: str | None = None, binary: str = "kubectl"
) -> str:
    """The command that prints the reasoning, spelled for *this* pod.

    A command nobody has heard of is not "one command away", so the compact
    report ends with this rather than trusting the reader to find the flag -
    and it carries the namespace and the pod name, because a bare
    ``podbench status`` in a namespace of more than one pod asks a question
    instead of answering one.

    It carries ``--context`` and ``--kubectl`` for the same reason and a worse
    failure: a pod name is unique to a namespace *in a cluster*, and this
    attach chose its cluster on the command line. Handed back without them the
    line either reports another cluster's Yama scope, node and limits as if
    they were this pod's, or refuses a pod the reader is looking at. The ssh
    stanza already holds this standard through
    :class:`podbench.sshcfg.KubectlInvocation`. Nothing else of the client is
    dropped: no verb takes a ``--kubeconfig`` flag, so there is none to spell.

    >>> explain_hint(PodRef("demo", "web-6c9d7f4b8b-hq2vn"))
    'podbench status -n demo web-6c9d7f4b8b-hq2vn --explain'
    >>> explain_hint(PodRef("demo", "web"), context="prod", binary="/opt/kubectl")
    'podbench status --context prod --kubectl /opt/kubectl -n demo web --explain'
    """
    selectors = ""
    if context is not None:
        selectors += f"--context {context} "
    if binary != "kubectl":
        selectors += f"--kubectl {binary} "
    return f"podbench status {selectors}-n {pod.namespace} {pod.name} --explain"


def explain_note(
    kubectl: Kubectl,
    pod: str,
    pod_json: Mapping[str, Any],
    present: Sequence[SeatInfo],
) -> str:
    """The reasoning ``attach`` compresses out of its report.

    It lives on ``status`` because ``status`` is where someone looks *before*
    setting a breakpoint — which is the moment the arithmetic matters, and a
    different moment from connecting — and because it mutates nothing, so it is
    safe to re-run as often as the question comes back. ``attach``'s report
    states the conclusions; this states what they were computed from.

    The capability half is measured again rather than remembered: nothing of an
    attach survives on the client, the seat is still there to be asked, and a
    report kept from an earlier session would age silently on a node whose Yama
    setting or LSM had changed underneath it.
    """
    latest, container = latest_seat(pod_json, present)
    blocks: list[tuple[str, list[str]]] = []
    if latest is not None and latest.running:
        session = _seat_session(kubectl, pod, pod_json, latest, container)
        blocks.append(
            (
                f"what {latest.name} supports, and what decided it",
                # Carried through as it is: the capability report aligns its
                # own columns, and re-wrapping it here would take the columns
                # out and leave the numbers no longer comparing down them.
                format_session(session, explain=True).split("\n"),
            )
        )
    else:
        # "Burnt" is not an assumption here: `status` prints "no podbench
        # containers in ns/pod" and exits before `--explain` is consulted, so
        # this branch is only ever reached with seats that landed and are gone,
        # and `--new` is the command that replaces one.
        blocks.append(
            (
                "what these seats support",
                _warning_lines(
                    "nothing is running here to ask. capreport measures from "
                    "inside the seat, on the node the pod is on, and a burnt "
                    "container cannot answer - `podbench attach --new` lands "
                    "one that can"
                ),
            )
        )
    explanation = probe_explanation(container, probe_budgets(pod_json, container))
    if explanation is not None:
        blocks.append(
            (f"what a pause costs on {container!r}", _warning_lines(explanation))
        )
    budget = memory_budget(pod_json)
    blocks.append(
        (
            "the memory and disk a seat shares",
            # RESERVATION_NOTE beside the number, not below the block: the
            # summary is limits less requests, and a reader who takes it for
            # free memory has been told the opposite of what it measures.
            _warning_lines(f"{budget.summary}. {RESERVATION_NOTE} {OOM_WARNING}"),
        )
    )
    blocks.append(("raising that limit with --resize", _warning_lines(RESIZE_WARNING)))
    lines: list[str] = []
    for heading, body in blocks:
        lines.append(f"explain: {heading}")
        lines.extend(f"  {line}" for line in body)
    return "\n".join(lines)


def _seat_session(
    kubectl: Kubectl,
    pod: str,
    pod_json: Mapping[str, Any],
    seat: SeatInfo,
    container: str,
) -> Session:
    """A running seat as a :class:`Session`, measured now.

    Everything the seat's own spec records is read back from it rather than
    assumed, as ``ssh-config`` does and for the same reason: this may be a seat
    landed from another machine, and nothing of that attach is in hand here.
    """
    reference = ContainerRef(PodRef(kubectl.namespace, pod), seat.name)
    report, notes = run_capreport(kubectl, reference)
    return Session(
        seat=reference,
        workload=container,
        rung=seat.rung,
        reused=True,
        uid=seat.uid,
        home=seat.home,
        identity_mounted=seat.identity_mounted,
        identity_declared=SEAT_IDENTITY_VOLUME in declared_volumes(pod_json),
        probes=probe_budgets(pod_json, container),
        report=report,
        ssh=probe_ssh_identity(kubectl, reference),
        warnings=tuple(notes),
    )


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
    print(message, file=sys.stderr)


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


def _build_app(runner: Runner | None) -> typer.Typer:
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
        target: Annotated[
            str | None,
            typer.Option("--target", metavar="NAME", help="workload container name"),
        ] = None,
        image: Annotated[
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
        ] = None,
        target_uid: Annotated[
            int | None,
            typer.Option(
                "--target-uid",
                metavar="UID",
                help="the target's uid, when its pod spec does not say",
            ),
        ] = None,
        mount: Annotated[
            list[str] | None,
            typer.Option(
                "--mount",
                metavar="CLAIM:MOUNTPATH",
                help="mount a volume the pod already declares into the seat, "
                "named by claim or by volume name. MOUNTPATH defaults to the "
                "application container's own, which Patch mode requires it to "
                "equal. Repeatable",
            ),
        ] = None,
        force_new: Annotated[
            bool,
            typer.Option(
                "--new",
                help="add a container even if one is running (its name is permanent)",
            ),
        ] = False,
        seat_gid_root: Annotated[
            bool,
            typer.Option(
                "--seat-gid-root",
                help="land the seat with runAsGroup: 0 so it can register an "
                "/etc/passwd entry for the target's uid, which is what sshd "
                "needs to let anyone log in, and the only way to get one on a "
                "live pod. Off by default: it drops the target's own group",
            ),
        ] = False,
        no_seat_identity: Annotated[
            bool,
            typer.Option(
                "--no-seat-identity",
                help="do not mount the pod's podbench-home volume, which is "
                "otherwise mounted by convention when the pod declares it and "
                "keeps everything the seat writes off the workload's "
                "ephemeral-storage budget. The podbench-identity volume is "
                "never mounted by attach: it needs a subPath per file, which an "
                "ephemeral container may not have - use --seat-gid-root for the "
                "seat's /etc/passwd entry",
            ),
        ] = False,
        no_probe: Annotated[
            bool,
            typer.Option(
                "--no-probe",
                help="skip capreport; the report then says nothing was measured",
            ),
        ] = False,
        resize: Annotated[
            str | None,
            typer.Option(
                "--resize",
                metavar="MEMORY",
                help="raise the target's memory limit in place first, e.g. 6Gi",
            ),
        ] = None,
        identity: _Identity = DEFAULT_IDENTITY,
        ssh_user: _SshUser = None,
        host_alias: _HostAlias = None,
        print_config: _PrintConfig = False,
        timeout: Annotated[
            float,
            typer.Option(
                "--timeout", metavar="SECONDS", help="seconds to wait for the seat"
            ),
        ] = 120.0,
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
        chosen = image or os.environ.get(IMAGE_ENV, DEFAULT_IMAGE)

        # Resize before attaching, not after: the headroom has to exist before
        # vscode-server starts allocating into a limit podbench cannot reserve.
        #
        # Nothing is said when the flag is absent. The caveats in
        # RESIZE_WARNING are all about a limit that has been raised - that it
        # lives on the pod and not on its controller, that a LimitRange was
        # never tested against it - and none of them is true of a pod nobody
        # resized, which is how this warning came to be printed at every attach
        # that did not use the flag (issue #54).
        resize_note: str | None = None
        if resize is not None:
            workload = target_container_name(kube.get_pod(name), target)
            resize_note = try_resize(kube, name, workload, resize)

        session = attach(
            kube,
            name,
            target=target,
            image=chosen,
            public_key=public_key,
            target_uid=target_uid,
            mounts=mount or (),
            force_new=force_new,
            seat_gid_root=seat_gid_root,
            seat_identity=not no_seat_identity,
            probe=not no_probe,
            timeout=timeout,
        )
        if resize_note is not None:
            session = replace(session, warnings=(*session.warnings, resize_note))
        # Through the client rather than the flags: `--namespace` is resolved
        # from the kubeconfig when it was not given, and the hint has to name
        # the cluster this report was measured on however it was chosen.
        print(format_session(session, context=kube.context, binary=kube.binary))
        print()
        print(
            _emit(
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
        seat = running_seat(pod_json)
        if seat is None:
            raise LauncherError(
                f"no running podbench container in {kube.namespace}/{name}; "
                "run `podbench attach` first"
            )
        reference = ContainerRef(PodRef(kube.namespace, name), seat.name)
        session = Session(
            seat=reference,
            workload=seat.target or target_container_name(pod_json),
            rung=seat.rung,
            reused=True,
            uid=seat.uid,
            home=seat.home,
            identity_mounted=seat.identity_mounted,
            # Asked again rather than assumed: this command exists to regenerate
            # a stanza from another machine, where nothing of the original
            # attach is in hand.
            ssh=probe_ssh_identity(kube, reference),
        )
        print(
            _emit(
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
        explain: Annotated[
            bool,
            typer.Option(
                "--explain",
                help="also print the reasoning attach's report compresses out: "
                "what a pause costs, what memory the seat shares, and what this "
                "seat supports with the mechanism that decided each",
            ),
        ] = False,
        no_prompt: _NoPrompt = False,
        namespace: _Namespace = None,
        context: _Context = None,
        kubectl: _KubectlBinary = "kubectl",
        config_dir: _ConfigDir = None,
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        name = resolve_pod(kube, pod, prompt=not no_prompt)
        pod_json = kube.get_pod(name)
        present = seats(pod_json)
        if not present:
            print(f"no podbench containers in {kube.namespace}/{name}")
            raise typer.Exit(0)
        # Read-only: the config dir is where the ssh alias for these seats is
        # recorded, and reporting one podbench cannot back up would be worse
        # than reporting none. Nothing here writes a stanza.
        print(
            format_seats(
                PodRef(kube.namespace, name), present, directory=client_dir(config_dir)
            )
        )
        # A blank line first: the seats block ends with an `ssh` alias meant to
        # be copied, and a probe header butted straight against it reads as
        # part of it.
        print()
        print(
            "\n".join(_warning_lines(probe_spend_note(kube, name, pod_json, present)))
        )
        if explain:
            print(explain_note(kube, name, pod_json, present))
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
        print(
            "\n".join(
                format_seats(pod, present, directory=directory)
                for pod, present in found
            )
        )
        raise typer.Exit(0)

    return app


def _emit(
    kubectl: Kubectl,
    session: Session,
    *,
    identity: str,
    config_dir: str | None,
    host_alias: str | None,
    ssh_user: str | None,
    print_config: bool,
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
    )


def main(args: Sequence[str] | None = None, *, runner: Runner | None = None) -> int:
    """Entry point for the cluster-side ``podbench`` verbs.

    ``runner`` is the seam the tests use; the CLI passes none and the calls go
    to the real ``kubectl``, which is what makes auth the kubeconfig's problem
    and not podbench's. It is also why the app is built here rather than at
    import time: every command closes over it.

    A degraded seat is a success. Returning non-zero for "the cluster would not
    grant SYS_PTRACE" would make the honest capability report look like a
    failure, which is exactly the outcome the brief asks for instead.
    """
    try:
        return run(_build_app(runner), args, prog="podbench")
    except (
        LauncherError,
        KubectlError,
        EphemeralContainerError,
        TimeoutError,
    ) as error:
        print(f"podbench: {error}", file=sys.stderr)
        return 2
