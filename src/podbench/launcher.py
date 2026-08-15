"""``kubectl podbench`` — the launcher that lands a seat and says what it got.

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

from .agent import PUBKEY_ENV
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
    "NON_ROOT_HOME",
    "OOM_WARNING",
    "RESIZE_WARNING",
    "Feature",
    "LadderStep",
    "LauncherError",
    "SeatInfo",
    "Session",
    "attach",
    "capability_report_from_json",
    "current_namespace",
    "features",
    "format_session",
    "list_seats",
    "main",
    "plan_ladder",
    "resolve_pod_name",
    "run_capreport",
    "running_seat",
    "seat_layout",
    "seats",
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

DEFAULT_SEAT_USER = "podbench"
"""ssh login name for a non-root seat. sshd resolves it through NSS, so the
debug image must carry an account for the uid the seat runs as; ``--ssh-user``
overrides it when the image names that account something else."""

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
class Session:
    """A landed seat: where it is, what it is allowed to do, and what it cost."""

    seat: ContainerRef
    workload: str
    rung: Rung
    reused: bool
    uid: int | None = None
    """``runAsUser`` as actually pinned in the container's securityContext —
    ``None`` when the seat rung pinned nothing and the image's own user wins."""

    steps: tuple[LadderStep, ...] = ()
    report: CapabilityReport | None = None
    warnings: tuple[str, ...] = ()

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
    force_new: bool = False,
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
    """
    pod = resolve_pod_name(pod_reference)
    pod_json = kubectl.get_pod(pod)
    workload = target_container_name(pod_json, target)
    warnings: list[str] = []

    existing = running_seat(pod_json)
    if existing is not None and not force_new:
        session = Session(
            seat=ContainerRef(PodRef(kubectl.namespace, pod), existing.name),
            workload=existing.target or workload,
            rung=existing.rung,
            reused=True,
            uid=existing.uid,
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
    else:
        session = _walk_ladder(
            kubectl,
            pod=pod,
            pod_json=pod_json,
            workload=workload,
            image=image,
            public_key=public_key,
            target_uid=target_uid,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    warnings.append(OOM_WARNING)
    if session.rung is not Rung.FULL:
        warnings.append(
            f"the seat runs as uid {session.uid if session.uid is not None else '?'}; "
            "sshd resolves the login name through NSS, so the debug image needs "
            "an account for it (override the name with --ssh-user)"
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
                env=_container_env(pod_json, public_key, rung),
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
            steps=tuple(steps),
        )

    raise LauncherError(
        "no rung of the capability ladder was admitted:\n"
        + "\n".join(f"  {step.rung.value}: {step.detail}" for step in steps)
    )


def _container_env(
    pod_json: Mapping[str, Any], public_key: str | None, rung: Rung
) -> dict[str, str]:
    env: dict[str, str] = {}
    if rung is not Rung.FULL:
        # Root already has a writable home the agent's layout knows about; a
        # non-root seat has none, so both halves are pointed at the same one.
        env["HOME"] = NON_ROOT_HOME
    if public_key is not None:
        env[PUBKEY_ENV] = public_key.strip()
    node = _as_str(as_dict(pod_json.get("spec")).get("nodeName"))
    if node is not None:
        # Yama differs per node by kernel flavour, so a report that cannot name
        # the node cannot explain "attach worked yesterday" (report 3.13/4.5).
        env["PODBENCH_NODE_NAME"] = node
    return env


# -- probing the landed seat ------------------------------------------------


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
            Feature("seat (editor, shell, git)", True),
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
        Feature("seat (editor, shell, git)", True),
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
    non-root layout's paths depend only on ``$HOME``, which the launcher pins to
    :data:`NON_ROOT_HOME` when it authors the container — so an unknown uid is
    still safe here, since every non-full rung carries ``runAsNonRoot: true``
    and therefore cannot be uid 0.
    """
    if session.rung is Rung.FULL:
        return SshdLayout.for_uid(0)
    # Any non-zero uid selects the same non-root layout, so an unpinned seat can
    # be given a placeholder: the paths that follow come from `home` alone.
    return SshdLayout.for_uid(session.uid or _UNPINNED_UID, home=NON_ROOT_HOME)


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
    kept = [
        line
        for line in (path.read_text().splitlines() if path.is_file() else [])
        if line.strip() and not line.startswith(f"{binding.alias} ")
    ]
    path.write_text("\n".join([*kept, entry.strip()]) + "\n")
    path.chmod(0o600)
    return path


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


# -- CLI --------------------------------------------------------------------


def _identity_paths(identity: str) -> tuple[Path, Path]:
    private = Path(identity).expanduser()
    return private, private.with_name(private.name + ".pub")


def _read_public_key(identity: str) -> tuple[str, str]:
    private, public = _identity_paths(identity)
    if not public.is_file():
        raise LauncherError(
            f"no ssh public key at {public}. podbench authorises this key inside "
            "the container, so generate one (ssh-keygen -t ed25519) or point "
            "--identity at an existing key."
        )
    return str(private), public.read_text().strip()


def _client_dir(explicit: str | None) -> Path:
    chosen = explicit or os.environ.get(CLIENT_DIR_ENV) or DEFAULT_CLIENT_DIR
    return Path(chosen).expanduser()


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
        prog="kubectl podbench",
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
        "--new",
        dest="force_new",
        action="store_true",
        help="add a container even if one is running (its name is permanent)",
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
    """Entry point for ``kubectl podbench`` / ``podbench launcher``.

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
    identity, public_key = _read_public_key(cast(str, parsed.identity))
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
        force_new=cast(bool, parsed.force_new),
        probe=cast(bool, parsed.probe),
        timeout=cast(float, parsed.timeout),
    )
    session = replace(session, warnings=(*session.warnings, resize_note))
    print(format_session(session))
    print()
    print(_emit_ssh_config(kubectl, session, parsed, identity))
    return 0


def _cmd_ssh_config(kubectl: Kubectl, parsed: argparse.Namespace) -> int:
    identity, _ = _read_public_key(cast(str, parsed.identity))
    pod = resolve_pod_name(cast(str, parsed.pod))
    pod_json = kubectl.get_pod(pod)
    seat = running_seat(pod_json)
    if seat is None:
        raise LauncherError(
            f"no running podbench container in {kubectl.namespace}/{pod}; "
            "run `kubectl podbench attach` first"
        )
    session = Session(
        seat=ContainerRef(PodRef(kubectl.namespace, pod), seat.name),
        workload=seat.target or target_container_name(pod_json),
        rung=seat.rung,
        reused=True,
        uid=seat.uid,
    )
    print(_emit_ssh_config(kubectl, session, parsed, identity))
    return 0


def _emit_ssh_config(
    kubectl: Kubectl,
    session: Session,
    parsed: argparse.Namespace,
    identity: str,
) -> str:
    directory = _client_dir(cast(str | None, parsed.config_dir))
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

    alias = cast(str | None, parsed.host_alias) or default_host_alias(session.pod)
    user = cast(str | None, parsed.ssh_user) or (
        "root" if session.rung is Rung.FULL else DEFAULT_SEAT_USER
    )
    stanza = ssh_stanza(
        session,
        identity_file=identity,
        host_key=binding,
        host_alias=alias,
        user=user,
        invocation=KubectlInvocation(binary=kubectl.binary, context=kubectl.context),
    )
    if cast(bool, parsed.print_config):
        return "\n".join([*notes, stanza])
    path = write_ssh_config(
        stanza,
        directory / "config.d" / f"{session.pod.namespace}-{session.pod.name}.conf",
    )
    return "\n".join(
        [
            *notes,
            f"ssh config written to {path}",
            f"add this to ~/.ssh/config once:  Include {directory}/config.d/*.conf",
            f"then:  ssh {alias}   (or Remote-SSH: Connect to Host -> {alias})",
        ]
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
