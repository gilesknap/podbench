"""Pure functions that author Kubernetes specs.

Nothing here touches the cluster: every function takes JSON in and gives JSON
out, so the rules that the phase-0 spikes paid for can be asserted in unit
tests rather than discovered in a namespace.

Three of those rules are hard invariants rather than defaults:

* ``CAP_SYS_PTRACE`` on a container whose ``runAsUser`` is not 0 is a **silent
  no-op** — the capability lands in the bounding set only and ``CapEff`` stays
  zero, so the container looks privileged and behaves unprivileged (report
  3.10). Authoring that combination raises instead.
* A ``volumeMount`` on an *ephemeral* container may not carry ``subPath``: the
  API server refuses the whole request with ``Forbidden: cannot be set for an
  Ephemeral Container``. Authoring one raises here, where the reason can be
  given, rather than at a ``replace --raw`` that has already been composed.
* A dev pod does **not** carry its origin's Service-selector labels unless the
  caller asks. Silently joining a production Service is a foot-cannon (report
  4.4).
* A dev pod's sidecar is an *ordinary* container, so it may have the ``subPath``
  an ephemeral one may not — which is the only way a passwd *file* reaches a
  seat. Where the origin declares the identity volume the sidecar is authored to
  mount it, and to run as the uid that identity names: a record for uid N in a
  container running as uid 0 is a login for somebody else.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

from .model import (
    SEAT_HOME_PATH,
    SEAT_HOME_VOLUME,
    SEAT_IDENTITY_VOLUME,
    TARGET_CID_ENV,
    Rung,
    as_dict,
)

__all__ = [
    "AGENT_COMMAND",
    "CONTROLLER_LABELS",
    "DEVPOD_LABEL",
    "GITOPS_ANNOTATIONS",
    "GITOPS_LABELS",
    "ORIGIN_ANNOTATION",
    "SERVER_OWNED_METADATA",
    "WORKSPACE_MOUNT_PATH",
    "WORKSPACE_VOLUME",
    "InvalidSpecError",
    "container_id",
    "cutover_selector_patch",
    "dev_pod_spec",
    "dev_seat_identity",
    "devpod_selector",
    "DEFAULT_PULL_POLICY",
    "PULL_POLICIES",
    "ephemeral_container_spec",
    "moves",
    "runs_as_non_root",
    "seat_identity_volume_mounts",
    "service_selector_patch",
    "target_uid_gid",
    "validate_ephemeral_volume_mounts",
    "validate_security_context",
]

DEVPOD_LABEL = "podbench.dev/devpod"
"""Marks an authored dev pod. Also the selector a cutover flips a Service to."""

ORIGIN_ANNOTATION = "podbench.dev/origin"
"""Records the pod a dev pod was cloned from."""

CONTROLLER_LABELS: tuple[str, ...] = (
    "pod-template-hash",
    "controller-revision-hash",
    "controller-uid",
    "batch.kubernetes.io/controller-uid",
    "batch.kubernetes.io/job-name",
    "job-name",
    "statefulset.kubernetes.io/pod-name",
)
"""Labels that make a controller adopt the clone, and so must never be copied.

Keeping the Service-selector labels while dropping these puts the dev pod in the
endpointslice without making it a ReplicaSet member. Keeping ``pod-template-hash``
instead would give a ``replicas: 1`` ReplicaSet two matching pods, and one of
them would be reaped (report 3.5).
"""

GITOPS_LABELS: tuple[str, ...] = ("argocd.argoproj.io/instance",)
GITOPS_ANNOTATIONS: tuple[str, ...] = ("argocd.argoproj.io/tracking-id",)
"""Marks that say a GitOps controller owns an object and will reconcile it.

Only the unambiguous ones. Argo CD's tracking key is configurable
(``application.instanceLabelKey``) and its *default* is
``app.kubernetes.io/instance`` — which is also Helm's common label and appears
on pods nobody is reconciling (measured: 53 of 82 pods on a live cluster carried
it, none of them tracked by it). Treating that as a signal would refuse Iterate
mode on ordinary Helm deployments, and since the refusal is absolute there would
be no way past it. Missing a detection leaves the user where they are today;
a false one takes away a mode that works. So this errs toward missing.

The corollary, which belongs in the docs rather than in a comment: a cluster
that leaves ``instanceLabelKey`` at its default is not detected.
"""


def gitops_owner(obj: Mapping[str, Any]) -> str | None:
    """The GitOps mark on an object, as ``key=value``, or ``None``.

    Takes any object, not just a pod, because the mark is on the *workload*:
    Argo stamps what it applied from git, and the pod template is not that.
    A Deployment carries it; the pods it makes do not (measured — 0 of 82).

    >>> gitops_owner({"metadata": {"labels": {"argocd.argoproj.io/instance": "i22"}}})
    'argocd.argoproj.io/instance=i22'
    >>> gitops_owner({"metadata": {"labels": {"app": "demo"}}}) is None
    True
    """
    metadata = as_dict(obj.get("metadata"))
    labels = as_dict(metadata.get("labels"))
    annotations = as_dict(metadata.get("annotations"))
    for key in GITOPS_LABELS:
        if (value := labels.get(key)) is not None:
            return f"{key}={value}"
    for key in GITOPS_ANNOTATIONS:
        if (value := annotations.get(key)) is not None:
            return f"{key}={value}"
    return None


SERVER_OWNED_METADATA: tuple[str, ...] = (
    "uid",
    "resourceVersion",
    "creationTimestamp",
    "generation",
    "managedFields",
    "ownerReferences",
    "selfLink",
    "generateName",
    "deletionTimestamp",
    "deletionGracePeriodSeconds",
    "finalizers",
)
"""Metadata the API server owns; a manifest carrying it back is rejected."""

WORKSPACE_VOLUME = "podbench-workspace"
WORKSPACE_MOUNT_PATH = "/workspace"

AGENT_COMMAND: tuple[str, ...] = ("podbench", "agent")
"""What a podbench container is given to run.

Not ``sleep infinity``: the sshd config, the authorized keys and the host key
are written by the agent at start-up, and the ProxyCommand's
``sshd -i -f <config>`` fails without them. It is long-running, so it still
satisfies the rule that a debug container is never given a short-lived command
— one that exits reaches ``Completed`` and burns its name for the pod's
lifetime (report 4.2).
"""

_IDLE_COMMAND: tuple[str, ...] = ("sleep", "infinity")
"""What the *target* container is given in a dev pod, so it stops serving."""
_TARGET_PROBES: tuple[str, ...] = (
    "readinessProbe",
    "livenessProbe",
    "startupProbe",
    "lifecycle",
)

_DEFAULT_SIDECAR_RESOURCES: dict[str, dict[str, str]] = {
    "requests": {"cpu": "200m", "memory": "512Mi"},
    "limits": {"cpu": "2", "memory": "3Gi"},
}


class InvalidSpecError(ValueError):
    """A spec podbench refuses to author, because it would not do what it says."""


def validate_security_context(security_context: Mapping[str, Any]) -> None:
    """Reject a securityContext whose ``SYS_PTRACE`` would be a silent no-op.

    The kernel grants capabilities to a non-root uid only through the ambient
    set, which no CRI populates, so ``capabilities.add: [SYS_PTRACE]`` with a
    non-zero ``runAsUser`` produces ``CapEff: 0000000000000000`` — measured, on
    a container the API server happily admitted (report 3.10). Shipping that
    would tell the user they have live attach when every ptrace call will return
    a bare ``EPERM``.

    An absent ``runAsUser`` is refused too: the effective uid then comes from
    the image or the pod-level securityContext, and neither is knowable here.
    """
    capabilities = as_dict(security_context.get("capabilities"))
    added = capabilities.get("add")
    if not isinstance(added, list) or "SYS_PTRACE" not in cast(list[Any], added):
        return
    run_as_user = security_context.get("runAsUser")
    if run_as_user != 0:
        raise InvalidSpecError(
            "SYS_PTRACE requires runAsUser: 0 — a capability added to a "
            f"container running as {run_as_user!r} reaches the bounding set "
            "only, leaving CapEff: 0 and every ptrace call failing with EPERM. "
            "Use the degraded rung (the target's own uid, no capabilities) "
            "instead."
        )


_SUBPATH_FIELDS: tuple[str, ...] = ("subPath", "subPathExpr")


def validate_ephemeral_volume_mounts(
    volume_mounts: Sequence[Mapping[str, Any]],
) -> None:
    """Reject a ``volumeMount`` an ephemeral container is not allowed to have.

    ``subPath`` — and ``subPathExpr`` with it — is refused by the API server for
    an ephemeral container, and refused for the whole request rather than for
    the mount:

    .. code-block:: text

       The Pod "demo" is invalid:
       * spec.ephemeralContainers[0].volumeMounts[0].subPath: Forbidden: cannot
         be set for an Ephemeral Container

    So a single mount authored this way costs the entire attach, and the error
    the user sees names a field they never typed. It is refused here for the
    same reason the ``SYS_PTRACE`` combination is: the spec would not do what it
    says, and this is the only layer that can say why.

    The consequence is the one worth stating out loud: a *file* cannot be
    projected into an ephemeral container at all. Mounting the volume without
    the ``subPath`` mounts a directory over the mountPath, which for
    ``/etc/passwd`` means replacing the file with a directory, and for ``/etc``
    means losing ``nsswitch.conf`` and with it the lookup the identity exists to
    satisfy. A live-pod seat gets its NSS identity from ``--seat-gid-root``
    instead: with ``runAsGroup: 0`` the agent appends its own record to the
    image's group-writable ``/etc/passwd``.
    """
    for index, entry in enumerate(volume_mounts):
        for field in _SUBPATH_FIELDS:
            if entry.get(field) in (None, ""):
                continue
            mount = entry.get("name")
            path = entry.get("mountPath")
            raise InvalidSpecError(
                f"volumeMounts[{index}] ({mount!r} at {path!r}) sets {field}, "
                "which an ephemeral container may not have: the API server "
                f"refuses the request with `volumeMounts[{index}].{field}: "
                "Forbidden: cannot be set for an Ephemeral Container`, so the "
                "seat never lands. Mount the whole volume at a path of its own, "
                "or - if this was an identity file for the seat's uid - land "
                "the seat with --seat-gid-root, which lets the agent register "
                "its own /etc/passwd record instead."
            )


_IMMUTABLE_TAG = re.compile(r"^\d+\.\d+\.\d+([.-]?(a|b|rc|alpha|beta)\.?\d+)?$")
"""A tag CI publishes once and never moves: a release, or a prerelease of one.

Everything else this project publishes is *mutable* — ``main`` moves on every
default-branch push, and a branch tag (``0.2.0-beta.2-my-branch``) is overwritten
on every push to that branch.
"""

DEFAULT_PULL_POLICY = "IfNotPresent"
"""What a seat asks for unless the caller says otherwise, and it cannot change.

``Always`` is the obvious answer for a tag that moves, and it is wrong: it is
the only policy that *requires* a registry round trip, so it breaks every image
that was put on the node rather than pulled to it — ``kind load``, ``ctr
import``, an air-gapped mirror. Measured, by breaking the e2e suite with it:
kind side-loads ``docker.io/library/podbench:e2e``, and the kubelet answered
``pull access denied, repository does not exist``.

The kubelet offers no third option — "re-check if you can, carry on if you
cannot" is not a policy it has — so the choice belongs to whoever knows where
their image came from. :func:`moves` is how podbench raises the question at the
moment it matters instead of guessing at the answer.
"""

PULL_POLICIES = ("IfNotPresent", "Always", "Never")
"""What ``--pull`` accepts, in the kubelet's own spelling."""


def moves(image: str) -> bool:
    """Whether this reference can point somewhere else tomorrow.

    Not a policy — a *warning* condition. A seat started from a moving tag on a
    node that already has a copy is serving whatever was published when that
    copy was pulled, and since the launcher and the image are two halves of one
    release, "which half am I running" is the last question anyone thinks to
    ask. It cost a round of confused debugging on a branch image, where the
    launcher had the fix and the seat did not.

    >>> moves("ghcr.io/gilesknap/podbench:0.2.0")
    False
    >>> moves("ghcr.io/gilesknap/podbench:0.2.0-beta.1")
    False
    >>> moves("ghcr.io/gilesknap/podbench@sha256:" + "0" * 64)
    False
    >>> moves("ghcr.io/gilesknap/podbench:main")
    True
    >>> moves("ghcr.io/gilesknap/podbench:0.2.0-beta.2-my-branch")
    True
    """
    reference = image.rsplit("/", 1)[-1]
    # A digest names one manifest for all time — and the `@` beats the `:` in a
    # reference that carries both.
    if "@" in reference:
        return False
    _, separator, tag = reference.partition(":")
    # No tag at all means `:latest`, which moves by definition.
    if not separator:
        return True
    return not _IMMUTABLE_TAG.match(tag)


def ephemeral_container_spec(
    *,
    name: str,
    image: str,
    rung: Rung,
    target_container: str | None = None,
    target_container_id: str | None = None,
    target_uid: int | None = None,
    target_gid: int | None = None,
    command: Sequence[str] = AGENT_COMMAND,
    env: Mapping[str, str] | None = None,
    volume_mounts: Sequence[Mapping[str, Any]] | None = None,
    seat_gid_root: bool = False,
    pull_policy: str = DEFAULT_PULL_POLICY,
) -> dict[str, Any]:
    """Author one ephemeral container for a rung of the capability ladder.

    ``command`` must be long-running: an ephemeral container that exits reaches
    ``Completed`` and its name is then unusable for the pod's lifetime (report
    4.2). It defaults to :data:`AGENT_COMMAND` rather than an idle sleep because
    the transport's server-side files do not exist until the agent has written
    them.

    ``target_container_id`` is the target's ``containerID`` with the runtime
    scheme stripped. It is passed through as ``PODBENCH_TARGET_CID`` because the
    only attribution of a process to its container that stays correct under
    ``shareProcessNamespace`` — and with a second podbench session attached — is
    a substring match of that id against ``/proc/<pid>/cgroup`` (report 3.15).

    Ephemeral containers carry no ``resources``: the field is rejected, and the
    container is confined by the pod's cgroup regardless (report 3.9). For the
    same "the API server refuses the whole request" reason, a ``volume_mounts``
    entry carrying ``subPath`` is refused here — see
    :func:`validate_ephemeral_volume_mounts`.

    ``seat_gid_root`` pins ``runAsGroup: 0`` on the non-root rungs, which is
    what lets the agent register an ``/etc/passwd`` entry for the target's uid
    and so is the difference between a seat with ssh and one reachable only by
    ``kubectl exec``. It is opt-in: see :func:`_rung_security_context` for why
    the target's own gid stays the default.
    """
    spec: dict[str, Any] = {
        "name": name,
        "image": image,
        "imagePullPolicy": pull_policy,
        "command": list(command),
        "terminationMessagePolicy": "File",
        "securityContext": _rung_security_context(
            rung, target_uid, target_gid, seat_gid_root=seat_gid_root
        ),
    }
    if target_container is not None:
        spec["targetContainerName"] = target_container

    environment = dict(env or {})
    if target_container_id is not None:
        environment.setdefault(TARGET_CID_ENV, target_container_id)
    if environment:
        spec["env"] = [
            {"name": key, "value": value} for key, value in sorted(environment.items())
        ]

    if volume_mounts:
        # Ephemeral containers may mount the pod's *existing* volumes, which is
        # the only practical way to hand a toolchain to a non-root debug
        # container that cannot apt-get (report 3.19). They may not subPath any
        # of them, which is checked before the mounts are copied in so that a
        # refused attach burns no container name.
        validate_ephemeral_volume_mounts(volume_mounts)
        spec["volumeMounts"] = [dict(mount) for mount in volume_mounts]

    validate_security_context(as_dict(spec["securityContext"]))
    return spec


def _seat_gid(context: dict[str, Any], gid_root: bool) -> dict[str, Any]:
    """Apply the opt-in ``runAsGroup: 0`` override to a non-root rung.

    GID 0 is how a container running as an arbitrary uid registers itself in
    NSS: the debug image makes ``/etc/passwd`` group-writable (OpenShift's
    convention) and the agent appends a record for whatever uid it turned out to
    be, without which sshd refuses every login. Pod Security Admission does not
    constrain ``runAsGroup`` at any level, so this is admissible wherever the
    rung itself is - measured on a ``restricted`` namespace, uid 1000 / gid 0.
    """
    if gid_root:
        context["runAsGroup"] = 0
    return context


_NOT_PRIVILEGED = {
    "privileged": False,
    "allowPrivilegeEscalation": False,
}
"""Stated on every rung, including the full one, rather than left to default.

Both fields default to false, so this changes nothing the kernel does — and it
is the difference between an admitted seat and a refused one. A Kyverno
``validate.pattern`` rule fails on an **absent** field unless the pattern wraps
it in a conditional anchor, and a policy written as ``privileged: "false"``
therefore rejects a container that never mentioned privilege at all. Measured at
DLS on 2026-08-16 (issue #77):

    block user privileged access to privileged directories: … rule failed at
    path /spec/ephemeralContainers/1/securityContext/privileged/

The message says the field "must not be set to true", which is exactly what
sent the reader looking for where podbench set it. Saying what podbench means
costs two keys and removes the whole class.

``allowPrivilegeEscalation: false`` on the **full** rung is the one line here
that is not obviously free, since that rung also asks for ``SYS_PTRACE``. It is
admissible — only ``privileged: true`` and ``CAP_SYS_ADMIN`` conflict with it —
and it does not withdraw the capability: ``NoNewPrivs`` restricts privilege
*gained at* ``execve`` from setuid bits and file capabilities, not the set the
runtime grants the container at start. ``tests/e2e/test_s3_gdb.py`` asserts the
capability is still effective, because this repo measures rather than reasons.
"""


def _rung_security_context(
    rung: Rung,
    target_uid: int | None,
    target_gid: int | None,
    *,
    seat_gid_root: bool = False,
) -> dict[str, Any]:
    if rung is Rung.FULL:
        if target_uid not in (None, 0):
            raise InvalidSpecError(
                f"the full rung runs as root; it cannot also run as uid "
                f"{target_uid}. Use Rung.DEGRADED for the target's own uid."
            )
        return {
            "runAsUser": 0,
            "capabilities": {"add": ["SYS_PTRACE"]},
            **_NOT_PRIVILEGED,
        }

    restricted: dict[str, Any] = {
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
        "runAsNonRoot": True,
        **_NOT_PRIVILEGED,
    }
    if rung is Rung.SEAT:
        # Whatever the cluster will admit. The seat needs nothing from the
        # target, so it pins no uid of its own and the image's user will do; a
        # non-zero target uid is honoured when the caller offers one, because
        # sharing the target's uid costs nothing and buys the /proc reads.
        #
        # A target uid of 0 is dropped rather than honoured. This is exactly the
        # case the ladder reaches SEAT for — DEGRADED refuses a root target — and
        # runAsNonRoot: true beside runAsUser: 0 is admitted by the API server
        # and then refused by the kubelet, asynchronously, with
        # CreateContainerConfigError (report 3.18). The whole point of this rung
        # is to be admissible.
        if target_uid:
            restricted["runAsUser"] = target_uid
        # The gid is dropped alongside the uid rather than on its own merit. It
        # is admissible by itself — restricted PSS does not constrain
        # runAsGroup — but inheriting the target's group while deliberately not
        # inheriting its user describes a process that does not exist, and the
        # /proc reads a shared gid might buy are gated on the uid anyway.
        if target_uid and target_gid is not None:
            restricted["runAsGroup"] = target_gid
        return _seat_gid(restricted, seat_gid_root)

    if target_uid is None:
        raise InvalidSpecError(
            "the degraded rung must run as the target's own uid — discover it "
            "from the target container's securityContext or /proc/<pid>/status. "
            "Defaulting to root would cost the sysroot, maps, environ and exe, "
            "which is the entire value of this rung."
        )
    if target_uid == 0:
        raise InvalidSpecError(
            "the target runs as root, so the degraded rung (runAsNonRoot: true "
            "at the target's uid) cannot be authored. Use Rung.FULL if the "
            "namespace admits SYS_PTRACE, otherwise Rung.SEAT."
        )
    restricted["runAsUser"] = target_uid
    # The target's own gid stays the default even though it is exactly what
    # stops the seat registering an /etc/passwd entry for itself, and so costs
    # this rung its ssh transport. Two reasons to leave it: the target's gid is
    # what buys the group-gated reads under its sysroot that this rung exists
    # for, and quietly running a debug seat as the *root group* is a privilege
    # change that belongs to the person attaching. `seat_gid_root` makes it one
    # flag away rather than unavailable.
    if target_gid is not None:
        restricted["runAsGroup"] = target_gid
    return _seat_gid(restricted, seat_gid_root)


def seat_identity_volume_mounts() -> list[dict[str, Any]]:
    """Project :data:`podbench.model.SEAT_IDENTITY_VOLUME` as two files.

    The layout — which key lands at which path — is
    :data:`podbench.launcher.SEAT_IDENTITY_MOUNTS`, imported rather than
    restated so that the paths the chart contract is checked against and the
    paths a dev pod actually mounts cannot drift apart. The import is deferred
    to the call because :mod:`podbench.launcher` imports this module.

    Read-only, and one ``subPath`` per file: mounting the volume at ``/etc``
    would replace the directory and take ``nsswitch.conf`` with it, which is the
    lookup the identity exists to satisfy. Only an *ordinary* container may
    carry this — see :func:`validate_ephemeral_volume_mounts` for what happens
    when an ephemeral one is asked to.

    >>> [mount["mountPath"] for mount in seat_identity_volume_mounts()]
    ['/etc/passwd', '/etc/group']
    """
    from .launcher import SEAT_IDENTITY_MOUNTS

    return [
        {
            "name": SEAT_IDENTITY_VOLUME,
            "mountPath": path,
            "subPath": key,
            "readOnly": True,
        }
        for path, key in SEAT_IDENTITY_MOUNTS
    ]


def dev_seat_identity(
    origin_pod_json: Mapping[str, Any], target_container: str
) -> tuple[int, int] | None:
    """The uid/gid a dev pod's sidecar must run as to own the projected identity.

    The record the chart writes is for the *application's* uid, because that is
    the uid a seat beside a PSA-restricted workload is forced to take. A sidecar
    mounting that file while running as anything else has an ``/etc/passwd``
    describing somebody it is not: sshd resolves the login name, finds uid N,
    and refuses the keys the agent wrote as uid 0 with ``Permission denied
    (publickey)`` — a message naming nothing about identity. So the mount and
    the ``runAsUser`` are decided together, here.

    ``None`` means "author the sidecar as before":

    * the origin declares no :data:`podbench.model.SEAT_IDENTITY_VOLUME`, so
      there is nothing to project, or
    * it declares one but the manifest does not say which uid *and* gid the
      application runs as, so the number in the file is unknowable from here and
      a guess would be a login for the wrong user, or
    * the application runs as root, which every image already resolves — mounting
      a file to reach uid 0 would buy nothing and cost the sidecar SYS_PTRACE.
    """
    volumes = as_dict(origin_pod_json.get("spec")).get("volumes")
    declared = [
        cast(dict[str, Any], entry)
        for entry in (cast(list[Any], volumes) if isinstance(volumes, list) else [])
        if isinstance(entry, dict)
    ]
    if not any(entry.get("name") == SEAT_IDENTITY_VOLUME for entry in declared):
        return None
    uid, gid = target_uid_gid(origin_pod_json, target_container)
    if uid is None or uid == 0 or gid is None:
        return None
    return uid, gid


def dev_pod_spec(
    origin_pod_json: Mapping[str, Any],
    *,
    name: str,
    target_container: str,
    image: str,
    target_port: int,
    take_traffic: bool = False,
    sidecar_name: str = "podbench",
    sidecar_ptrace: bool = True,
    workspace_size: str = "4Gi",
    resources: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    seat_identity: bool = True,
) -> dict[str, Any]:
    """Author Iterate mode's sacrificial clone of ``origin_pod_json``.

    The launcher authors this rather than shelling out to ``kubectl debug
    --copy-to``, which strips every label and annotation — leaving the clone
    invisible to the Service, so the headline "edit code, curl the Service, see
    your change" demo cannot work at all — and which offers no way to give the
    debug container ``resources`` or a workspace volume, and no ``--dry-run`` to
    preview any of it (report 3.5).

    ``take_traffic`` is off by default. With it off the dev pod carries only its
    podbench label and receives nothing until a cutover is made explicitly.

    ``seat_identity`` is the convention this mode's sidecar can honour and
    ``attach`` cannot: where the origin declares the identity volume, the clone
    carries it (its ``spec.volumes`` are copied wholesale) and the sidecar
    mounts it as ``/etc/passwd`` and ``/etc/group``. The sidecar then runs as the
    uid that identity names rather than as root — see :func:`dev_seat_identity`
    — and gives up ``SYS_PTRACE`` with the root it no longer has. That trade is
    the point of the volume: nothing is written at runtime, the seat needs no
    GID 0, and the pod is admissible under the restricted Pod Security Standard,
    which refuses the root sidecar outright. A declared
    :data:`podbench.model.SEAT_HOME_VOLUME` is mounted with it, at the path the
    identity's own passwd record names.

    The readiness probe on the *podbench* container is not optional. Without it
    a probe-less dev pod is Ready the instant its containers start and joins the
    endpointslice while nothing is listening, blackholing about half the
    requests; with it, Service membership tracks the inner loop in both
    directions, joining when the relaunched process binds and dropping out
    within ~6 s when it dies (report 3.6).

    The origin pod is not modified.
    """
    pod = copy.deepcopy(dict(origin_pod_json))
    metadata = as_dict(pod.get("metadata"))
    origin_name = metadata.get("name")
    spec = as_dict(pod.get("spec"))
    if not spec:
        raise InvalidSpecError("origin pod JSON has no spec")

    pod["apiVersion"] = "v1"
    pod["kind"] = "Pod"
    pod.pop("status", None)

    for key in SERVER_OWNED_METADATA:
        metadata.pop(key, None)
    metadata["name"] = name

    labels = as_dict(metadata.get("labels")) if take_traffic else {}
    for key in CONTROLLER_LABELS:
        labels.pop(key, None)
    labels[DEVPOD_LABEL] = "true"
    metadata["labels"] = labels
    # The origin's annotations are dropped rather than copied: they carry
    # last-applied-configuration and sidecar-injection directives that would be
    # untrue, or actively harmful, on an orphan copy.
    metadata["annotations"] = (
        {ORIGIN_ANNOTATION: origin_name} if isinstance(origin_name, str) else {}
    )
    pod["metadata"] = metadata

    spec.pop("nodeName", None)
    # The origin's seats are not copyable and not wanted: the API server refuses
    # the create outright with `spec.ephemeralContainers: Forbidden: cannot be
    # set on create`, so cloning any pod somebody has ever attached to fails on
    # a field the user never typed. They are also spent — an ephemeral
    # container's name is burnt for its pod's lifetime, and this is a new pod.
    spec.pop("ephemeralContainers", None)
    # A crashed dev pod must leave a corpse to inspect rather than looping.
    spec["restartPolicy"] = "Never"
    # gdb, ss -lntp and /proc/<pid>/root all depend on it.
    spec["shareProcessNamespace"] = True

    containers = spec.get("containers")
    if not isinstance(containers, list):
        raise InvalidSpecError("origin pod JSON has no containers")
    container_list = cast(list[Any], containers)
    names = {
        cast(dict[str, Any], c).get("name")
        for c in container_list
        if isinstance(c, dict)
    }
    if sidecar_name in names:
        raise InvalidSpecError(
            f"the origin pod already has a container named {sidecar_name!r}"
        )
    if target_container not in names:
        raise InvalidSpecError(
            f"container {target_container!r} not found in pod "
            f"{origin_name!r} (has {sorted(str(n) for n in names)})"
        )

    for entry in container_list:
        if not isinstance(entry, dict):
            continue
        container = cast(dict[str, Any], entry)
        if container.get("name") != target_container:
            continue
        container["command"] = list(_IDLE_COMMAND)
        container.pop("args", None)
        # Probes and lifecycle hooks against an idled container would fail
        # forever and, for a liveness probe, kill the pod.
        for probe in _TARGET_PROBES:
            container.pop(probe, None)

    volumes = spec.get("volumes")
    volume_list: list[Any] = (
        cast(list[Any], volumes) if isinstance(volumes, list) else []
    )
    if any(
        isinstance(v, dict) and cast(dict[str, Any], v).get("name") == WORKSPACE_VOLUME
        for v in volume_list
    ):
        raise InvalidSpecError(
            f"the origin pod already has a volume named {WORKSPACE_VOLUME!r}"
        )
    volume_list.append(
        {"name": WORKSPACE_VOLUME, "emptyDir": {"sizeLimit": workspace_size}}
    )
    spec["volumes"] = volume_list

    identity = (
        dev_seat_identity(origin_pod_json, target_container) if seat_identity else None
    )
    if identity is not None:
        # The pod-level securityContext came over with the deepcopy, fsGroup
        # included, so an origin that already sets one is inherited and nothing
        # is second-guessed. A pod that sets none needs one invented here: the
        # workspace emptyDir is created root:root, the sidecar is about to stop
        # being root, and a seat whose $HOME is present and unwritable fails
        # later and further away than one with no home at all. The identity's
        # own gid is the number to use — it is what the chart tells the
        # application's chart to set for exactly this reason.
        pod_security = as_dict(spec.get("securityContext"))
        pod_security.setdefault("fsGroup", identity[1])
        spec["securityContext"] = pod_security

    container_list.append(
        _sidecar(
            name=sidecar_name,
            image=image,
            target_container=target_container,
            target_port=target_port,
            ptrace=sidecar_ptrace,
            resources=resources,
            env=env,
            identity=identity,
            # The home the identity's passwd record *names*, mounted when the
            # pod declares a volume for it. sshd puts a session in the home NSS
            # gives it, and an ssh login that lands on `Could not chdir to home
            # directory /home/podbench` is one whose vscode-server has nowhere
            # to unpack. The sidecar's own $HOME stays the workspace: uv's
            # caches and venvs belong on the volume sized for them.
            seat_home=identity is not None
            and any(
                isinstance(v, dict)
                and cast(dict[str, Any], v).get("name") == SEAT_HOME_VOLUME
                for v in volume_list
            ),
        )
    )
    spec["containers"] = container_list
    pod["spec"] = spec
    return pod


def _sidecar(
    *,
    name: str,
    image: str,
    target_container: str,
    target_port: int,
    ptrace: bool,
    resources: Mapping[str, Any] | None,
    env: Mapping[str, str] | None,
    identity: tuple[int, int] | None = None,
    seat_home: bool = False,
    pull_policy: str = DEFAULT_PULL_POLICY,
) -> dict[str, Any]:
    if identity is not None:
        # The identity wins over the capability, and cannot be reconciled with
        # it: SYS_PTRACE needs runAsUser 0 (anywhere else it reaches the
        # bounding set only), and the projected passwd record names the
        # application's uid. Running as root with that file mounted would leave
        # sshd resolving the login name to a uid this container is not, and the
        # authorized_keys the agent wrote unreadable by it.
        seat_uid, seat_gid = identity
        security_context: dict[str, Any] = {
            "capabilities": {"drop": ["ALL"]},
            "allowPrivilegeEscalation": False,
            "seccompProfile": {"type": "RuntimeDefault"},
            "runAsNonRoot": True,
            "runAsUser": seat_uid,
            "runAsGroup": seat_gid,
        }
    elif ptrace:
        # runAsUser must be explicit even though this is a normal container: the
        # origin pod's pod-level securityContext may pin a non-root uid, which
        # would apply here too and silently void the capability. runAsNonRoot is
        # overridden at container level for the same reason — a pod-level
        # runAsNonRoot: true otherwise makes the kubelet reject this container
        # asynchronously with CreateContainerConfigError (report 3.18).
        security_context = {
            "runAsUser": 0,
            "runAsNonRoot": False,
            "capabilities": {"add": ["SYS_PTRACE"]},
        }
    else:
        security_context = {
            "capabilities": {"drop": ["ALL"]},
            "allowPrivilegeEscalation": False,
            "seccompProfile": {"type": "RuntimeDefault"},
            "runAsNonRoot": True,
        }
    validate_security_context(security_context)

    environment = {
        "HOME": WORKSPACE_MOUNT_PATH,
        "PODBENCH_TARGET": target_container,
        **dict(env or {}),
    }
    return {
        "name": name,
        "image": image,
        "imagePullPolicy": pull_policy,
        # The sidecar is this pod's ssh endpoint too, so it runs the agent for
        # the same reason the ephemeral container does: nothing writes the sshd
        # config, the authorized keys or the host key otherwise.
        "command": list(AGENT_COMMAND),
        "workingDir": WORKSPACE_MOUNT_PATH,
        # HOME lives on the workspace volume so uv's toolchains, caches and
        # venvs land there rather than in the container's writable layer.
        "env": [
            {"name": key, "value": value} for key, value in sorted(environment.items())
        ],
        "volumeMounts": [
            {"name": WORKSPACE_VOLUME, "mountPath": WORKSPACE_MOUNT_PATH},
            *(
                [{"name": SEAT_HOME_VOLUME, "mountPath": SEAT_HOME_PATH}]
                if seat_home
                else []
            ),
            # An ordinary container may subPath, so this seat gets the passwd
            # file an ephemeral one cannot be given at all.
            *(seat_identity_volume_mounts() if identity is not None else []),
        ],
        "resources": copy.deepcopy(dict(resources or _DEFAULT_SIDECAR_RESOURCES)),
        "securityContext": security_context,
        "readinessProbe": {
            "tcpSocket": {"port": target_port},
            "periodSeconds": 2,
            "failureThreshold": 1,
        },
    }


def devpod_selector() -> dict[str, str]:
    """The label selector that matches only podbench dev pods."""
    return {DEVPOD_LABEL: "true"}


def service_selector_patch(selector: Mapping[str, str]) -> list[dict[str, Any]]:
    """A JSON patch replacing a Service's selector.

    It must be a *replace*, and therefore a JSON patch: ``kubectl patch
    --type=merge`` unions map keys instead, so restoring ``{app: foo}`` over
    ``{app: foo, podbench.dev/devpod: "true"}`` leaves both keys in place and
    silently drops the original pod out of the endpointslice (report 3.5).

    >>> service_selector_patch({"app": "demo"})
    [{'op': 'replace', 'path': '/spec/selector', 'value': {'app': 'demo'}}]
    """
    return [{"op": "replace", "path": "/spec/selector", "value": dict(selector)}]


def cutover_selector_patch() -> list[dict[str, Any]]:
    """A JSON patch pointing a Service exclusively at the dev pod.

    Record the Service's original selector first: the restore has to be exact.
    """
    return service_selector_patch(devpod_selector())


def target_uid_gid(
    pod_json: Mapping[str, Any], container: str
) -> tuple[int | None, int | None]:
    """The uid/gid the named container runs as, container settings winning.

    ``None`` means the manifest does not say, and the effective id comes from
    the image — which only the running process knows, so the caller must read
    ``/proc/<pid>/status`` instead of assuming root.
    """
    spec = as_dict(pod_json.get("spec"))
    pod_context = as_dict(spec.get("securityContext"))
    container_context = as_dict(_find_container(spec, container).get("securityContext"))

    def pick(key: str) -> int | None:
        for source in (container_context, pod_context):
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    return pick("runAsUser"), pick("runAsGroup")


def runs_as_non_root(pod_json: Mapping[str, Any], container: str) -> bool:
    """Whether ``runAsNonRoot`` is set on the container or its pod.

    A pre-flight read of this lets the launcher skip the full rung outright.
    Otherwise the API server accepts a root debug container and the kubelet
    refuses it seconds later with ``CreateContainerConfigError: container's
    runAsUser breaks non-root policy`` (report 3.18).
    """
    spec = as_dict(pod_json.get("spec"))
    container_context = as_dict(_find_container(spec, container).get("securityContext"))
    value = container_context.get("runAsNonRoot")
    if isinstance(value, bool):
        return value
    return as_dict(spec.get("securityContext")).get("runAsNonRoot") is True


def container_id(pod_json: Mapping[str, Any], container: str) -> str | None:
    """The named container's runtime id, with the ``runtime://`` scheme stripped."""
    statuses = as_dict(pod_json.get("status")).get("containerStatuses")
    if not isinstance(statuses, list):
        return None
    for entry in cast(list[Any], statuses):
        if not isinstance(entry, dict):
            continue
        status = cast(dict[str, Any], entry)
        if status.get("name") != container:
            continue
        value = status.get("containerID")
        if isinstance(value, str):
            return value.split("://", 1)[-1]
    return None


def _find_container(spec: Mapping[str, Any], name: str) -> dict[str, Any]:
    for key in ("containers", "initContainers", "ephemeralContainers"):
        entries = spec.get(key)
        if not isinstance(entries, list):
            continue
        for entry in cast(list[Any], entries):
            if (
                isinstance(entry, dict)
                and cast(dict[str, Any], entry).get("name") == name
            ):
                return cast(dict[str, Any], entry)
    return {}
