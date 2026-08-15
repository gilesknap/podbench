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

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from .agent import GROUP_PATH, PASSWD_PATH, PUBKEY_ENV
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
    "SeatIdentity",
    "SeatInfo",
    "Session",
    "SshSeat",
    "attach",
    "capability_report_from_json",
    "client_dir",
    "current_namespace",
    "declared_volumes",
    "default_host_alias",
    "emit_ssh_config",
    "features",
    "forget_known_hosts",
    "forget_ssh_config",
    "format_session",
    "list_seats",
    "main",
    "parse_mount",
    "plan_ladder",
    "probe_ssh_identity",
    "read_public_key",
    "resolve_mounts",
    "resolve_pod_name",
    "run_capreport",
    "ssh_unavailable_note",
    "running_seat",
    "seat_identity_mounts",
    "seat_layout",
    "seats",
    "spec_env",
    "ssh_config_path",
    "ssh_stanza",
    "target_container_name",
    "try_resize",
    "write_known_hosts",
    "write_ssh_config",
]

CONTAINER_BASE = "podbench"
"""Base name for the ephemeral container; suffixed ``-1``, ``-2``, … (report 4.2)."""

CAPREPORT_ARGV: tuple[str, ...] = ("capreport", "--json")
"""The probe, as the image puts it on PATH. Unqualified names are safe here and
nowhere else in podbench: ``kubectl exec`` inherits the image's own ``ENV
PATH``, whereas an ssh session sources nothing and needs absolute paths."""

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
    "patch pod --subresource resize). It is opt-in because it is only lightly "
    "proven (report R13): one k3s version, one pod, never against a "
    "LimitRange, a ResourceQuota, or a controller that would fight the change."
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
    user has in hand (``patch --print-values`` names the claim), while a mount
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
        "venv path (`podbench patch --print-values` emits the volume, the "
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
        phase, detail = _phase_of(statuses.get(name, {}))
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
            )
        )
    return found


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

    warnings.append(OOM_WARNING)
    # Not also a warning: `features` reports it under "supports", which is where
    # "what this seat can do and which mechanism decided" belongs, and
    # `ssh_unavailable_note` prints the way out in place of the stanza. A third
    # copy in the warning block would be the only thing said three times. The
    # same goes for the declared-but-unusable identity volume, which the ssh
    # seat's line carries for exactly this reason.
    session = replace(
        session,
        identity_declared=identity_declared,
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
    live attach, 10 read-only, 20 nothing — so a non-zero exit is the normal
    case on a restricted namespace.
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
        apparmor_profile=_as_str(payload.get("apparmor_profile")),
        self_uid=_as_int(payload.get("self_uid")) or 0,
        target_uid=_as_int(payload.get("target_uid")),
        target_pid=_as_int(payload.get("target_pid")),
        node_name=_as_str(payload.get("node_name")),
        child_attach_ok=_as_bool(payload.get("child_attach_ok")),
        target_attach_ok=_as_bool(payload.get("target_attach_ok")),
        proc_reads=reads,
        notes=notes,
    )


def _verdict_from(value: object) -> Verdict:
    name = str(value).upper()
    for verdict in Verdict:
        if verdict.name == name:
            return verdict
    return Verdict.NONE


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


def features(session: Session) -> tuple[Feature, ...]:
    """What the landed seat supports, naming the blocker for what it does not.

    Never inferred from the rung alone. The rung is what the *cluster* admitted;
    whether ptrace actually works also depends on Yama, seccomp and AppArmor,
    which are node-local and were measured inside the container (report 4.5).
    """
    report = session.report
    if report is None:
        unknown = "capreport did not run, so this was not measured"
        return (
            Feature("live attach (gdb -p <pid>)", False, unknown),
            Feature(
                "read-only inspect (/proc/<pid>/root, maps, environ)", False, unknown
            ),
            _iterate_feature(),
            *_seat_features(session),
        )
    return (
        Feature(
            "live attach (gdb -p <pid>)",
            report.verdict is Verdict.LIVE_ATTACH,
            report.blocker.explanation,
        ),
        Feature(
            "read-only inspect (/proc/<pid>/root, maps, environ)",
            report.verdict in (Verdict.LIVE_ATTACH, Verdict.READ_ONLY),
            report.blocker.explanation,
        ),
        _iterate_feature(),
        *_seat_features(session),
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
        Feature("exec seat (capreport, pids, dbg --launch, kubectl exec)", True),
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


def _iterate_feature() -> Feature:
    return Feature(
        "iterate (edit, relaunch, verify through the Service)",
        False,
        "attach shares a live pod, where killing PID 1 restarts the container "
        "and a liveness probe would kill a stopped one. The relaunch loop needs "
        "a sacrificial dev pod (`podbench dev`), never the live workload.",
    )


def format_session(session: Session) -> str:
    """The capability report, which is the product of an attach."""
    lines = [
        f"seat        {session.seat}"
        + ("  (reconnected)" if session.reused else "  (new)"),
        f"target      {session.workload}",
        f"rung        {session.rung.value} - {session.rung.description}",
        "ladder",
    ]
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
        lines.extend(f"  {line}" for line in _wrap(warning))
    return "\n".join(lines)


def _yama(scope: int | None) -> str:
    if scope is None:
        return "absent (no Yama LSM on this node - not the same as scope 0)"
    return str(scope)


def _paragraph(text: str, *, first: str, indent: str) -> list[str]:
    """Wrap ``text`` with a hanging indent, so a wrapped line is not mistaken
    for a second bullet."""
    wrapped = _wrap(text, width=max(24, 78 - len(indent)))
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
            f"    kubectl exec {target} -- capreport",
            f"    kubectl exec {target} -- pids",
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
    return directory / "config.d" / f"{pod.namespace}-{pod.name}.conf"


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


def _identity_paths(identity: str) -> tuple[Path, Path]:
    private = Path(identity).expanduser()
    return private, private.with_name(private.name + ".pub")


def read_public_key(identity: str) -> tuple[str, str]:
    """The private key's path and the public key's text, or a refusal naming both.

    Both halves are wanted at once: podbench authorises the public key inside
    the container and names the private one in the stanza's ``IdentityFile``,
    and a mismatch between them is a login refused for reasons neither file
    explains.
    """
    private, public = _identity_paths(identity)
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
                f"add this to ~/.ssh/config once:  Include {directory}/config.d/*.conf",
                f"then:  ssh {alias}   (or Remote-SSH: Connect to Host -> {alias})",
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

    Failure is reported rather than raised — the mitigation is only lightly
    proven (report R13) and a seat that lands with a loud warning beats one that
    does not land at all.
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
    return f"resized {container} to a {memory} memory limit; restore it on detach"


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


def list_seats(kubectl: Kubectl) -> list[tuple[PodRef, list[SeatInfo]]]:
    """Every pod in the namespace that carries a podbench container."""
    result = kubectl.run("get", "pods", "-o", "json")
    parsed: object = json.loads(result.stdout)
    items = _as_list(as_dict(parsed if isinstance(parsed, dict) else {}).get("items"))
    found: list[tuple[PodRef, list[SeatInfo]]] = []
    for item in items:
        pod_json = as_dict(item)
        name = _entry_name(as_dict(pod_json.get("metadata")))
        if name is None:
            continue
        present = seats(pod_json)
        if present:
            found.append((PodRef(kubectl.namespace, name), present))
    return found


def format_seats(pod: PodRef, present: Sequence[SeatInfo]) -> str:
    """One pod's podbench containers, for ``status`` and ``list``."""
    lines = [str(pod)]
    for seat in present:
        lines.append(
            f"  {seat.name:<12} {seat.phase:<11} {seat.rung.value:<9} "
            f"{seat.rung.description}"
        )
        lines.append(f"  {'':<12} {seat.detail}")
    return "\n".join(lines)


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


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-n", "--namespace", default=None)
    parser.add_argument("--context", default=None)
    parser.add_argument("--kubectl", default="kubectl", help="kubectl binary to use")
    parser.add_argument(
        "--config-dir",
        default=None,
        help="where the generated ssh config and known_hosts live "
        f"(default {DEFAULT_CLIENT_DIR})",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="podbench",
        description="Land a development seat inside a pod and say what it can do.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    attach_parser = sub.add_parser(
        "attach", help="add or reconnect a podbench container and print the report"
    )
    attach_parser.add_argument("pod")
    attach_parser.add_argument("--target", default=None, help="workload container name")
    attach_parser.add_argument("--image", default=None)
    attach_parser.add_argument(
        "--target-uid",
        type=int,
        default=None,
        help="the target's uid, when its pod spec does not say",
    )
    attach_parser.add_argument(
        "--mount",
        action="append",
        default=None,
        metavar="CLAIM:MOUNTPATH",
        help="mount a volume the pod already declares into the seat, named by "
        "claim or by volume name. MOUNTPATH defaults to the application "
        "container's own, which Patch mode requires it to equal. Repeatable",
    )
    attach_parser.add_argument(
        "--new",
        dest="force_new",
        action="store_true",
        help="add a container even if one is running (its name is permanent)",
    )
    attach_parser.add_argument(
        "--seat-gid-root",
        action="store_true",
        help="land the seat with runAsGroup: 0 so it can register an "
        "/etc/passwd entry for the target's uid, which is what sshd needs to "
        "let anyone log in, and the only way to get one on a live pod. Off by "
        "default: it drops the target's own group",
    )
    attach_parser.add_argument(
        "--no-seat-identity",
        dest="seat_identity",
        action="store_false",
        help="do not mount the pod's podbench-home volume, which is otherwise "
        "mounted by convention when the pod declares it and keeps everything "
        "the seat writes off the workload's ephemeral-storage budget. The "
        "podbench-identity volume is never mounted by attach: it needs a "
        "subPath per file, which an ephemeral container may not have - use "
        "--seat-gid-root for the seat's /etc/passwd entry",
    )
    attach_parser.add_argument(
        "--no-probe",
        dest="probe",
        action="store_false",
        help="skip capreport; the report then says nothing was measured",
    )
    attach_parser.add_argument(
        "--resize",
        default=None,
        metavar="MEMORY",
        help="raise the target's memory limit in place first, e.g. 6Gi",
    )
    attach_parser.add_argument("--identity", default=DEFAULT_IDENTITY)
    attach_parser.add_argument("--ssh-user", default=None)
    attach_parser.add_argument("--host-alias", default=None)
    attach_parser.add_argument(
        "--print-config",
        action="store_true",
        help="print the ssh stanza instead of writing it to the config dir",
    )
    attach_parser.add_argument("--timeout", type=float, default=120.0)
    _add_common(attach_parser)

    ssh_parser = sub.add_parser(
        "ssh-config", help="regenerate the ssh stanza for an existing session"
    )
    ssh_parser.add_argument("pod")
    ssh_parser.add_argument("--identity", default=DEFAULT_IDENTITY)
    ssh_parser.add_argument("--ssh-user", default=None)
    ssh_parser.add_argument("--host-alias", default=None)
    ssh_parser.add_argument("--print-config", action="store_true")
    _add_common(ssh_parser)

    status_parser = sub.add_parser(
        "status", help="the podbench containers in one pod and what each supports"
    )
    status_parser.add_argument("pod")
    _add_common(status_parser)

    list_parser = sub.add_parser(
        "list", help="every pod in the namespace carrying a podbench container"
    )
    _add_common(list_parser)
    return parser


def main(args: Sequence[str] | None = None, *, runner: Runner | None = None) -> int:
    """Entry point for the cluster-side ``podbench`` verbs.

    ``runner`` is the seam the tests use; the CLI passes none and the calls go
    to the real ``kubectl``, which is what makes auth the kubeconfig's problem
    and not podbench's.

    A degraded seat is a success. Returning non-zero for "the cluster would not
    grant SYS_PTRACE" would make the honest capability report look like a
    failure, which is exactly the outcome the brief asks for instead.
    """
    parsed = _build_parser().parse_args(args)
    namespace: str | None = cast(str | None, parsed.namespace)
    context: str | None = cast(str | None, parsed.context)
    binary: str = cast(str, parsed.kubectl)
    if namespace is None:
        namespace = current_namespace(binary=binary, context=context, runner=runner)
    kubectl = Kubectl(namespace, context=context, binary=binary, runner=runner)

    try:
        command = cast(str, parsed.command)
        if command == "attach":
            return _cmd_attach(kubectl, parsed)
        if command == "ssh-config":
            return _cmd_ssh_config(kubectl, parsed)
        if command == "status":
            return _cmd_status(kubectl, parsed)
        return _cmd_list(kubectl)
    except (
        LauncherError,
        KubectlError,
        EphemeralContainerError,
        TimeoutError,
    ) as error:
        print(f"podbench: {error}", file=sys.stderr)
        return 2


def _cmd_attach(kubectl: Kubectl, parsed: argparse.Namespace) -> int:
    identity, public_key = read_public_key(cast(str, parsed.identity))
    pod = resolve_pod_name(cast(str, parsed.pod))
    image = cast(str | None, parsed.image) or os.environ.get(IMAGE_ENV, DEFAULT_IMAGE)

    # Resize before attaching, not after: the headroom has to exist before
    # vscode-server starts allocating into a limit podbench cannot reserve.
    resize = cast(str | None, parsed.resize)
    if resize is None:
        resize_note = RESIZE_WARNING
    else:
        workload = target_container_name(
            kubectl.get_pod(pod), cast(str | None, parsed.target)
        )
        resize_note = try_resize(kubectl, pod, workload, resize)

    session = attach(
        kubectl,
        pod,
        target=cast(str | None, parsed.target),
        image=image,
        public_key=public_key,
        target_uid=cast(int | None, parsed.target_uid),
        mounts=cast(list[str] | None, parsed.mount) or (),
        force_new=cast(bool, parsed.force_new),
        seat_gid_root=cast(bool, parsed.seat_gid_root),
        seat_identity=cast(bool, parsed.seat_identity),
        probe=cast(bool, parsed.probe),
        timeout=cast(float, parsed.timeout),
    )
    session = replace(session, warnings=(*session.warnings, resize_note))
    print(format_session(session))
    print()
    print(_emit(kubectl, session, parsed, identity).note)
    return 0


def _cmd_ssh_config(kubectl: Kubectl, parsed: argparse.Namespace) -> int:
    identity, _ = read_public_key(cast(str, parsed.identity))
    pod = resolve_pod_name(cast(str, parsed.pod))
    pod_json = kubectl.get_pod(pod)
    seat = running_seat(pod_json)
    if seat is None:
        raise LauncherError(
            f"no running podbench container in {kubectl.namespace}/{pod}; "
            "run `podbench attach` first"
        )
    reference = ContainerRef(PodRef(kubectl.namespace, pod), seat.name)
    session = Session(
        seat=reference,
        workload=seat.target or target_container_name(pod_json),
        rung=seat.rung,
        reused=True,
        uid=seat.uid,
        home=seat.home,
        identity_mounted=seat.identity_mounted,
        # Asked again rather than assumed: this command exists to regenerate a
        # stanza from another machine, where nothing of the original attach is
        # in hand.
        ssh=probe_ssh_identity(kubectl, reference),
    )
    print(_emit(kubectl, session, parsed, identity).note)
    return 0


def _emit(
    kubectl: Kubectl,
    session: Session,
    parsed: argparse.Namespace,
    identity: str,
) -> SshSeat:
    """:func:`emit_ssh_config`, with the flags this CLI spells it with."""
    return emit_ssh_config(
        kubectl,
        session,
        identity=identity,
        config_dir=cast(str | None, parsed.config_dir),
        host_alias=cast(str | None, parsed.host_alias),
        user=cast(str | None, parsed.ssh_user),
        print_config=cast(bool, parsed.print_config),
    )


def _cmd_status(kubectl: Kubectl, parsed: argparse.Namespace) -> int:
    pod = resolve_pod_name(cast(str, parsed.pod))
    present = seats(kubectl.get_pod(pod))
    if not present:
        print(f"no podbench containers in {kubectl.namespace}/{pod}")
        return 0
    print(format_seats(PodRef(kubectl.namespace, pod), present))
    return 0


def _cmd_list(kubectl: Kubectl) -> int:
    found = list_seats(kubectl)
    if not found:
        print(f"no podbench containers in namespace {kubectl.namespace}")
        return 0
    print("\n".join(format_seats(pod, present) for pod, present in found))
    return 0
