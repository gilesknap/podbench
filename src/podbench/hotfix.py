"""Hotfix mode: an emergency fix that outlives the session, and its provenance.

Hotfix mode is the one place podbench asks for deploy-time cooperation, and the
one place it deliberately *inverts* an earlier rule. Everywhere else the design
refuses to fight the kubelet: Iterate mode runs on a sacrificial clone precisely
because killing PID 1 gets you pristine image code back. Here the venv lives on
a PVC that outlives the container, so pristine image code is no longer what a
restart yields — **the restart is the relaunch mechanism**, probes and all.

The mount also dissolves the constraint that shapes :mod:`podbench.dev`. The
seat mounts the same claim at the same ``mountPath`` as the application, so the
venv and the checkout resolve identically on both sides and an editable install
is finally not straddling two mount namespaces (report §3.17 is about the case
where it does).

Three things the PVC does *not* fix, and which this module exists to make
visible rather than leave to be discovered:

1. **The PVC venv shadows the image's.** An image upgrade under a live hotfix
   mount keeps running the old venv, and an interpreter bump breaks it outright
   — the ``bin/python`` symlink on the volume points at an interpreter path in
   the *image*, which is not on the volume. So the manifest records the
   interpreter exactly (from ``pyvenv.cfg``, which is on the volume) and
   ``status`` measures the live one instead of assuming.
2. **Single replica only.** The claim is ReadWriteOnce, so a second replica
   either cannot schedule or, with RWX, races on the same checkout. ``init``
   and ``apply`` refuse a multi-replica target rather than partially hotfix a
   Deployment.
3. **A stale PVC silently reverts provenance.** Once a hotfix is consolidated
   and the new image rolls out, a PVC left mounted keeps the *old* code running
   under a version string that claims to contain the fix. ``status`` calls that
   out by name.

Provenance is therefore not decoration: every hotfix is a git commit, a manifest
on the volume records what it was made against, and the pod carries an
annotation saying it is hotfixed. ``status`` is the point of the whole mode —
silently-diverged pods are the risk it must never create.

Where the provenance is written is itself a design decision. Pod annotations do
not survive the reschedule that Hotfix mode *relies on*, so the annotations go on
the workload's pod template when there is one: they then propagate to every pod
the controller makes, and the template edit is also what rolls the workload —
the same mechanism ``kubectl rollout restart`` uses.
"""

from __future__ import annotations

import json
import shlex
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Protocol, cast

import typer

from .cli import new_app, require_subcommand, run
from .console import emit, paragraph
from .kubectl import (
    DEFAULT_CALL_TIMEOUT,
    CommandResult,
    Kubectl,
    KubectlError,
    Runner,
    run_subprocess,
)
from .launcher import CONTAINER_BASE, kubectl_for, running_seat
from .model import (
    HOTFIXED_ANNOTATION,
    SEAT_HOME_PATH,
    SEAT_HOME_VOLUME,
    SEAT_IDENTITY_VOLUME,
    PodRef,
    as_dict,
)
from .sshcfg import SEAT_USER

__all__ = [
    "APPLIED_ANNOTATION",
    "MANIFEST_ANNOTATION",
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "MAX_RECORDED_COMMITS",
    "METADATA_FILES",
    "HOTFIXED_ANNOTATION",
    "SEAT_HOME_SIZE",
    "InterpreterProbe",
    "LocalStore",
    "ManifestVersionError",
    "MultiReplicaError",
    "HotfixCommit",
    "HotfixError",
    "HotfixHealth",
    "HotfixManifest",
    "HotfixRow",
    "HotfixStore",
    "HotfixTarget",
    "PodStore",
    "annotate",
    "apply_hotfix",
    "assess",
    "changed_paths",
    "checkout_path",
    "consolidate",
    "drift_commits",
    "format_status",
    "identity_configmap",
    "init",
    "install_argv",
    "interpreter_warning",
    "main",
    "manifest_annotations",
    "manifest_from_pod",
    "manifest_path",
    "metadata_changed",
    "normalise_interpreter",
    "parse_log",
    "parse_pyvenv_cfg",
    "probe_interpreter",
    "replica_count",
    "resolve_target",
    "seat_container",
    "status_rows",
    "values_snippet",
]

MANIFEST_FILENAME = ".podbench-hotfix.json"
"""The manifest, at the root of the claim — which is to say inside the venv.

The claim is mounted *over* the application's venv path, so the venv directory
and the volume root are the same directory. There is nowhere else on the volume
to put it.
"""

MANIFEST_VERSION = 1
"""Schema version written by this podbench.

Read leniently and written strictly: a manifest with a lower version (or none at
all, which is version 0) loads with defaults for what it lacks, because a future
podbench will meet volumes written by this one and by whatever came before it. A
*higher* version is refused outright — guessing at fields we have never seen is
how a fix silently loses its provenance.
"""

MANIFEST_ANNOTATION = "podbench.dev/hotfix-manifest"
"""The manifest itself, so ``status`` needs one ``get pods`` and no exec."""

APPLIED_ANNOTATION = "podbench.dev/hotfix-applied-at"
"""A timestamp that changes on every apply.

Annotating a pod template with an unchanged body is a no-op and no rollout
happens, which would leave the fix on the volume and the old process running.
This is the same trick ``kubectl rollout restart`` uses.
"""

MAX_RECORDED_COMMITS = 20
"""How many commits the manifest carries verbatim.

Annotations share a 256 KB budget with everything else on the object, and a long
beamtime can accumulate a lot of hotfixes. The count of commits ahead is recorded
separately so the drift figure stays exact even when the list is truncated.
"""

METADATA_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "MANIFEST.in",
)
"""Files whose change invalidates an editable install.

An editable install is a path redirection: new *code* is picked up for free, but
a new entry point, a renamed package or a changed ``[project]`` table is baked
into the ``.dist-info`` at install time and needs the installer run again.
"""

DEFAULT_PYTHON = "python3"
"""What ``status`` runs in the application container to measure its interpreter.

With a venv-based image this usually resolves to the venv's own ``bin/python3``
— which is on the PVC, and whose symlink into the image is exactly the thing an
interpreter bump breaks. Either outcome is informative: a version, or a failure
to execute at all.
"""

SEAT_HOME_SIZE = "2Gi"
"""Default ``sizeLimit`` for the seat's home emptyDir.

Measured rather than guessed: vscode-server unpacks to roughly 700 MiB and a
realistic session — extensions, a language server's index, build output — sits
at 1.1-1.3 GB. An emptyDir with no limit draws silently on the node's ephemeral
storage, and blowing the pod's budget evicts the *pod*, application included.
"""

POD_WORK_TIMEOUT = 900.0
"""How long a single ``exec`` into the pod may take before it is killed.

Well past :data:`podbench.kubectl.DEFAULT_CALL_TIMEOUT`, because the commands
this verb sends are not questions: a ``git clone`` of somebody's application
repo and an editable install both run over one ``exec``, against whatever
network and index the cluster has, and a bound sized for a pod read would abort
the work rather than a wedge. Fifteen minutes is still a bound — issue #118's
point is that *no* number is the one wrong answer — and a clone that has not
finished by then has failed at something the user needs told about.
"""

LOG_SEPARATOR = "\x1f"
_LOG_FORMAT = f"--pretty=format:%H{LOG_SEPARATOR}%s"

_WORKLOAD_KINDS: dict[str, str] = {
    "deployment": "deployment",
    "deployments": "deployment",
    "deploy": "deployment",
    "statefulset": "statefulset",
    "statefulsets": "statefulset",
    "sts": "statefulset",
}


class HotfixError(RuntimeError):
    """Hotfix mode refused to do something, and says why."""


class MultiReplicaError(HotfixError):
    """The target runs more than one replica, which v1 does not support."""


class ManifestVersionError(HotfixError):
    """A manifest written by a newer podbench than this one."""


# -- the manifest ----------------------------------------------------------


@dataclass(frozen=True)
class HotfixCommit:
    """One commit on the PVC checkout that the released image does not have."""

    sha: str
    subject: str

    @property
    def short(self) -> str:
        """The abbreviated sha, for tables.

        >>> HotfixCommit("0123456789abcdef", "fix the thing").short
        '0123456'
        """
        return self.sha[:7]


@dataclass(frozen=True)
class HotfixManifest:
    """What a hotfix was made against, recorded on the volume that carries it.

    Every field is defaulted so that a manifest written by an older schema
    version loads rather than raising: the missing fields become empty, and
    :attr:`schema_version` records what was actually read so a reader can say
    "written by an older podbench" instead of silently inventing provenance.
    """

    venv: str = ""
    checkout: str = ""
    repo: str = ""
    base_image: str = ""
    base_image_digest: str = ""
    """The ``imageID`` of the container when the hotfix was made. Compared
    against the live one to detect an image upgrade under the mount."""

    interpreter: str = ""
    """The venv's interpreter version, read from ``pyvenv.cfg`` on the volume."""

    container: str = ""
    commit: str = ""
    base_commit: str = ""
    """The commit the released image was built from; drift is measured from it."""

    author: str = ""
    timestamp: str = ""
    ahead: int = 0
    commits: tuple[HotfixCommit, ...] = ()
    consolidated_branch: str | None = None
    schema_version: int = MANIFEST_VERSION

    def to_mapping(self) -> dict[str, Any]:
        """The JSON form. Explicit rather than ``asdict``: this document is a
        wire format between podbench versions, so its keys are chosen, not
        derived from whatever the dataclass happens to be called today."""
        return {
            "version": MANIFEST_VERSION,
            "venv": self.venv,
            "checkout": self.checkout,
            "repo": self.repo,
            "baseImage": self.base_image,
            "baseImageDigest": self.base_image_digest,
            "interpreter": self.interpreter,
            "container": self.container,
            "commit": self.commit,
            "baseCommit": self.base_commit,
            "author": self.author,
            "timestamp": self.timestamp,
            "ahead": self.ahead,
            "commits": [
                {"sha": commit.sha, "subject": commit.subject}
                for commit in self.commits
            ],
            "consolidatedBranch": self.consolidated_branch,
        }

    def to_json(self) -> str:
        """Compact JSON, because this also goes into an annotation."""
        return json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> HotfixManifest:
        """Load a manifest, tolerating anything this schema once lacked.

        >>> HotfixManifest.from_mapping({"commit": "abc"}).schema_version
        0
        >>> HotfixManifest.from_mapping({"version": 1, "ahead": 2}).ahead
        2
        """
        version = _as_int(payload.get("version")) or 0
        if version > MANIFEST_VERSION:
            raise ManifestVersionError(
                f"this hotfix manifest is schema version {version}; this podbench "
                f"understands up to {MANIFEST_VERSION}. Upgrade podbench rather "
                "than overwriting it — the fields it does not know about are "
                "somebody's provenance."
            )
        commits = tuple(
            HotfixCommit(
                sha=_as_str(as_dict(entry).get("sha")) or "",
                subject=_as_str(as_dict(entry).get("subject")) or "",
            )
            for entry in _as_list(payload.get("commits"))
        )
        return cls(
            venv=_as_str(payload.get("venv")) or "",
            checkout=_as_str(payload.get("checkout")) or "",
            repo=_as_str(payload.get("repo")) or "",
            base_image=_as_str(payload.get("baseImage")) or "",
            base_image_digest=_as_str(payload.get("baseImageDigest")) or "",
            interpreter=_as_str(payload.get("interpreter")) or "",
            container=_as_str(payload.get("container")) or "",
            commit=_as_str(payload.get("commit")) or "",
            base_commit=_as_str(payload.get("baseCommit")) or "",
            author=_as_str(payload.get("author")) or "",
            timestamp=_as_str(payload.get("timestamp")) or "",
            ahead=_as_int(payload.get("ahead")) or len(commits),
            commits=commits,
            consolidated_branch=_as_str(payload.get("consolidatedBranch")),
            schema_version=version,
        )

    @classmethod
    def from_json(cls, text: str) -> HotfixManifest:
        """Load from the JSON written by :meth:`to_json`."""
        parsed: object = json.loads(text)
        if not isinstance(parsed, dict):
            raise HotfixError("hotfix manifest is not a JSON object")
        return cls.from_mapping(cast(dict[str, Any], parsed))

    @property
    def stale_schema(self) -> bool:
        """Whether this came off the volume in an older schema version."""
        return self.schema_version < MANIFEST_VERSION


def manifest_path(venv: str) -> str:
    """Where the manifest lives for a claim mounted at ``venv``.

    >>> manifest_path("/opt/venv")
    '/opt/venv/.podbench-hotfix.json'
    """
    return f"{venv.rstrip('/')}/{MANIFEST_FILENAME}"


def checkout_path(venv: str) -> str:
    """Where the source checkout lives on the claim.

    Inside the venv directory, which looks odd until you remember the claim is
    mounted *over* the venv path and so there is no other directory on the
    volume. Being on the volume is the whole requirement: the editable install's
    path redirection has to survive the restart too.

    >>> checkout_path("/opt/venv/")
    '/opt/venv/src'
    """
    return f"{venv.rstrip('/')}/src"


def manifest_annotations(manifest: HotfixManifest) -> dict[str, str]:
    """The annotations that mark a pod (or pod template) as hotfixed."""
    return {
        HOTFIXED_ANNOTATION: "true",
        MANIFEST_ANNOTATION: manifest.to_json(),
        APPLIED_ANNOTATION: manifest.timestamp,
    }


def manifest_from_pod(pod_json: Mapping[str, Any]) -> HotfixManifest | None:
    """The manifest a pod's annotations carry, or ``None`` if it has none."""
    annotations = as_dict(as_dict(pod_json.get("metadata")).get("annotations"))
    raw = _as_str(annotations.get(MANIFEST_ANNOTATION))
    if raw is None:
        return None
    return HotfixManifest.from_json(raw)


# -- reaching the volume ---------------------------------------------------


class HotfixStore(Protocol):
    """How hotfix mode reaches the claim's filesystem and a git on it.

    The claim is only ever mounted in the cluster, so the default is
    :class:`PodStore` — every read, write and git invocation goes through the
    seat container, which mounts the same claim at the same path. The local
    implementation exists for the case where podbench is already *in* that
    container, and it is what the tests use with a temp directory standing in
    for the volume.
    """

    def run(self, argv: Sequence[str], *, check: bool = True) -> CommandResult:
        """Run a command with the claim visible. Paths are absolute."""
        ...

    def read_text(self, path: str) -> str | None:
        """The file's contents, or ``None`` if it does not exist."""
        ...

    def write_text(self, path: str, text: str) -> None:
        """Create or replace a file."""
        ...

    def exists(self, path: str) -> bool:
        """Whether a path exists on the claim."""
        ...


@dataclass
class LocalStore:
    """A claim mounted in this very process's mount namespace.

    Used when podbench runs inside the seat container, and by the tests against
    a temp directory. ``runner`` is the seam: no test ever forks a real git.
    """

    runner: Runner = run_subprocess

    def run(self, argv: Sequence[str], *, check: bool = True) -> CommandResult:
        result = self.runner(argv)
        if check and result.returncode != 0:
            raise HotfixError(
                f"{' '.join(argv)} exited {result.returncode}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    def read_text(self, path: str) -> str | None:
        target = Path(path)
        if not target.is_file():
            return None
        return target.read_text()

    def write_text(self, path: str, text: str) -> None:
        Path(path).write_text(text)

    def exists(self, path: str) -> bool:
        return Path(path).exists()


@dataclass(frozen=True)
class PodStore:
    """A claim reached through ``kubectl exec`` into the seat container.

    The seat is podbench's own image, so a shell and a git are guaranteed there;
    the application container is assumed to have neither, and in Hotfix mode it
    is quite likely to be distroless.
    """

    kube: Kubectl
    pod: str
    container: str

    def _exec(
        self, argv: Sequence[str], *, check: bool, timeout: float
    ) -> CommandResult:
        return self.kube.exec_(
            self.pod, argv, container=self.container, check=check, timeout=timeout
        )

    def run(self, argv: Sequence[str], *, check: bool = True) -> CommandResult:
        # Every git command this verb issues comes through here, clone included,
        # so the bound is the pod-work one rather than the per-call default.
        return self._exec(argv, check=check, timeout=POD_WORK_TIMEOUT)

    def read_text(self, path: str) -> str | None:
        # A `cat` is a question, not work, so it keeps issue #118's bound rather
        # than the clone's: reaching it through `run` would let a wedged exec on
        # a manifest read sit for fifteen minutes, which is the failure that
        # bound exists to stop. `write_text` below passes no timeout for the
        # same reason.
        result = self._exec(["cat", path], check=False, timeout=DEFAULT_CALL_TIMEOUT)
        return result.stdout if result.returncode == 0 else None

    def write_text(self, path: str, text: str) -> None:
        # Through a shell redirect rather than a heredoc: the payload is JSON and
        # goes over stdin, so nothing in it can be read as shell syntax.
        self.kube.exec_(
            self.pod,
            ["sh", "-c", f"cat > {shlex.quote(path)}"],
            container=self.container,
            stdin=text,
        )

    def exists(self, path: str) -> bool:
        return (
            self._exec(
                ["test", "-e", path], check=False, timeout=DEFAULT_CALL_TIMEOUT
            ).returncode
            == 0
        )


def read_manifest(store: HotfixStore, venv: str) -> HotfixManifest | None:
    """The manifest on the claim, or ``None`` if the claim has never been used."""
    text = store.read_text(manifest_path(venv))
    if text is None:
        return None
    return HotfixManifest.from_json(text)


def write_manifest(store: HotfixStore, manifest: HotfixManifest) -> None:
    """Record the manifest on the claim it describes."""
    store.write_text(manifest_path(manifest.venv), manifest.to_json() + "\n")


# -- interpreters ----------------------------------------------------------


def parse_pyvenv_cfg(text: str) -> dict[str, str]:
    """Parse ``pyvenv.cfg``, which is key/value and not quite INI.

    >>> parse_pyvenv_cfg("home = /usr/local/bin\\nversion = 3.12.7\\n")["version"]
    '3.12.7'
    """
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            parsed[key.strip()] = value.strip()
    return parsed


def normalise_interpreter(text: str) -> str:
    """Reduce anything that names a CPython version to just the version.

    ``python -V`` says ``Python 3.12.7``, ``pyvenv.cfg`` says ``3.12.7``, and
    ``version_info`` says ``3.12.7.final.0``. They have to be comparable.

    >>> normalise_interpreter("Python 3.12.7")
    '3.12.7'
    >>> normalise_interpreter("3.12.7.final.0")
    '3.12.7'
    >>> normalise_interpreter("")
    ''
    """
    token = text.strip().split()[-1] if text.strip() else ""
    parts = token.split(".")
    numeric: list[str] = []
    for part in parts:
        if not part.isdigit():
            break
        numeric.append(part)
    return ".".join(numeric[:3])


def _feature_version(version: str) -> str:
    """``major.minor``, the granularity at which a venv breaks.

    >>> _feature_version("3.12.7")
    '3.12'
    """
    return ".".join(normalise_interpreter(version).split(".")[:2])


def interpreter_warning(recorded: str, observed: str) -> str | None:
    """What to say about a venv whose interpreter no longer matches the image.

    A venv is bound to one ``major.minor``: its ``lib/pythonX.Y/site-packages``
    is not on the new interpreter's path and its compiled extensions are built
    against the old ABI. A micro bump is survivable and merely worth noting.

    >>> interpreter_warning("3.12.7", "3.12.7") is None
    True
    >>> "will not import" in (interpreter_warning("3.11.9", "3.12.7") or "")
    True
    """
    if not recorded or not observed:
        return None
    if normalise_interpreter(recorded) == normalise_interpreter(observed):
        return None
    if _feature_version(recorded) != _feature_version(observed):
        return (
            f"the hotfixed venv was built for Python {_feature_version(recorded)} "
            f"but the container now runs {_feature_version(observed)}: the venv's "
            "site-packages will not import, and the pod is running on whatever "
            "the interpreter finds instead. Retire the claim or rebuild the venv."
        )
    return (
        f"the venv records Python {normalise_interpreter(recorded)} and the "
        f"container now runs {normalise_interpreter(observed)}; same feature "
        "release, so imports still resolve, but compiled extensions were built "
        "against the older micro version."
    )


@dataclass(frozen=True)
class InterpreterProbe:
    """What running the interpreter in the application container reported."""

    ok: bool
    version: str | None
    detail: str


def probe_interpreter(
    kube: Kubectl, pod: str, container: str, *, python: str = DEFAULT_PYTHON
) -> InterpreterProbe:
    """Measure the interpreter the application container actually runs now.

    Not inferred from the image tag: with the claim mounted over the venv, the
    ``python`` on ``PATH`` is usually the venv's own shim, whose symlink points
    at an interpreter path *in the image*. If an upgrade moved that path the
    exec fails outright, and that failure is the most direct possible evidence
    of the shadowing risk this mode carries.
    """
    result = kube.exec_(pod, [python, "-V"], container=container, check=False)
    if result.returncode != 0:
        return InterpreterProbe(
            ok=False,
            version=None,
            detail=(result.stderr.strip() or result.stdout.strip() or "exec failed"),
        )
    reported = (result.stdout or result.stderr).strip()
    return InterpreterProbe(
        ok=True, version=normalise_interpreter(reported), detail=reported
    )


# -- git on the claim ------------------------------------------------------


def parse_log(text: str) -> tuple[HotfixCommit, ...]:
    """Read ``git log`` in this module's own format.

    ``%x1f`` rather than a printable separator, because a commit subject may
    contain any printable character and a hotfix commit's subject is written by
    somebody in a hurry at 3am.

    >>> parse_log("abc\\x1ffix the thing\\ndef\\x1fand another")
    (HotfixCommit(sha='abc', subject='fix the thing'), HotfixCommit(sha='def', subject='and another'))
    """  # noqa: E501
    commits: list[HotfixCommit] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        sha, separator, subject = line.partition(LOG_SEPARATOR)
        if not separator:
            continue
        commits.append(HotfixCommit(sha=sha.strip(), subject=subject.strip()))
    return tuple(commits)


def drift_commits(
    store: HotfixStore, checkout: str, base_commit: str
) -> tuple[HotfixCommit, ...]:
    """Commits on the claim's checkout that the released image does not have.

    Newest first, which is the order ``git log`` gives and the order a reader
    wants: the last thing somebody did to the running code is the first line.
    """
    if not base_commit:
        return ()
    result = store.run(
        ["git", "-C", checkout, "log", _LOG_FORMAT, f"{base_commit}..HEAD"],
        check=False,
    )
    if result.returncode != 0:
        raise HotfixError(
            f"cannot measure drift from {base_commit[:12]}: "
            f"{result.stderr.strip() or result.stdout.strip()}. The manifest's "
            "base commit is not in this checkout — was the claim seeded from a "
            "different repository?"
        )
    return parse_log(result.stdout)


def metadata_changed(paths: Sequence[str]) -> bool:
    """Whether a commit touched anything an editable install baked in.

    >>> metadata_changed(["src/app/main.py"])
    False
    >>> metadata_changed(["pyproject.toml", "src/app/main.py"])
    True
    """
    names = {Path(path).name for path in paths}
    return any(name in names for name in METADATA_FILES)


def changed_paths(
    store: HotfixStore, checkout: str, previous: str, head: str
) -> tuple[str, ...]:
    """The paths the checkout's history moved between two commits.

    This is what decides whether the editable install has to be run again, and
    it is keyed on the *commits* rather than on whether ``apply`` found a dirty
    working tree. Committing by hand in the seat — which is the natural thing to
    do when the fix took three attempts — and then running ``apply`` leaves a
    clean tree behind a moved HEAD. Guarding the packaging check on dirtiness
    skipped it in exactly that case, and the workload then rolled with a
    ``.dist-info`` older than the commit: a hotfix adding an entry point looked
    applied and was not.

    The whole range is examined and not just ``HEAD``, because several hand
    commits can accumulate between two applies and the metadata change may be in
    any of them. When there is no range to walk — a manifest old enough to carry
    no commit, or a history that no longer contains the one it carries — the
    fallback is ``HEAD``'s own paths, which over-installs rather than
    under-installs; a redundant editable install costs seconds, a skipped one
    costs a silently-unapplied hotfix.
    """
    if previous == head:
        return ()
    if previous:
        result = store.run(
            ["git", "-C", checkout, "diff", "--name-only", f"{previous}..{head}"],
            check=False,
        )
        if result.returncode == 0:
            return _paths(result.stdout)
    return _paths(
        store.run(
            ["git", "-C", checkout, "show", "--name-only", "--pretty=format:", head],
            check=False,
        ).stdout
    )


def _paths(text: str) -> tuple[str, ...]:
    """One path per line — ``--name-only`` output, minus git's blank lines.

    Split on lines rather than on whitespace: a path containing a space is one
    path, and ``git show --pretty=format:`` leads with an empty line.
    """
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def install_argv(venv: str, checkout: str) -> list[str]:
    """The editable reinstall, run in the *application* container.

    Not in the seat: the venv's ``bin/python`` is a symlink to an interpreter
    that lives in the application image and is therefore not resolvable from
    podbench's own image, even though the venv itself is on a volume both can
    see. ``--no-deps`` because a hotfix is a code change; pulling new
    dependencies mid-run is a release, and releases go through the pipeline.
    """
    return [
        f"{venv.rstrip('/')}/bin/python",
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--no-build-isolation",
        "-e",
        checkout,
    ]


# -- targets ---------------------------------------------------------------


@dataclass(frozen=True)
class HotfixTarget:
    """The single pod a hotfix applies to, and the workload behind it."""

    pod: PodRef
    container: str
    image: str
    image_digest: str
    workload_kind: str | None = None
    workload_name: str | None = None
    replicas: int | None = None

    @property
    def workload(self) -> str | None:
        """``kind/name``, or ``None`` for a pod nobody owns."""
        if self.workload_kind is None or self.workload_name is None:
            return None
        return f"{self.workload_kind}/{self.workload_name}"


def replica_count(obj: Mapping[str, Any]) -> int | None:
    """A workload's declared replica count.

    >>> replica_count({"spec": {"replicas": 3}})
    3
    >>> replica_count({"spec": {}}) is None
    True
    """
    return _as_int(as_dict(obj.get("spec")).get("replicas"))


def _refuse_multi_replica(kind: str, name: str, replicas: int) -> None:
    raise MultiReplicaError(
        f"{kind}/{name} runs {replicas} replicas and hotfix mode v1 supports "
        "exactly one. The claim is ReadWriteOnce, so a second replica either "
        "fails to schedule or — with ReadWriteMany — two pods share one "
        "checkout and one venv, and an apply on either is a race with no "
        "winner. Scale to 1 for the duration of the hotfix, or hotfix a "
        "single-replica canary and let the release pipeline carry the fix to "
        "the rest."
    )


def resolve_target(
    kube: Kubectl, reference: str, *, container: str | None = None
) -> HotfixTarget:
    """Turn ``pod/foo``, ``deployment/foo`` or ``foo`` into one hotfixable pod.

    A workload reference is the natural one to type — hotfixing is an operation
    on an application, not on a pod that will be replaced by the next
    reschedule — so both are accepted and both are checked for the single
    replica this mode requires.
    """
    kind, separator, name = reference.partition("/")
    if not separator:
        return _target_from_pod(kube, reference, container)
    if not name:
        raise HotfixError(f"no name in {reference!r}")
    if kind in ("pod", "pods", "po"):
        return _target_from_pod(kube, name, container)
    workload_kind = _WORKLOAD_KINDS.get(kind)
    if workload_kind is None:
        raise HotfixError(
            f"hotfix mode works on pods, deployments and statefulsets, not {kind!r}"
        )
    return _target_from_workload(kube, workload_kind, name, container)


def _target_from_workload(
    kube: Kubectl, kind: str, name: str, container: str | None
) -> HotfixTarget:
    workload = _get_json(kube, kind, name)
    replicas = replica_count(workload)
    if replicas is not None and replicas != 1:
        _refuse_multi_replica(kind, name, replicas)
    selector = as_dict(as_dict(workload.get("spec")).get("selector"))
    labels = as_dict(selector.get("matchLabels"))
    if not labels:
        raise HotfixError(f"{kind}/{name} has no matchLabels to find its pod by")
    query = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
    result = kube.run("get", "pods", "-l", query, "-o", "json")
    pods = [as_dict(item) for item in _as_list(_load_json(result.stdout).get("items"))]
    live = [pod for pod in pods if _phase(pod) not in ("Succeeded", "Failed")]
    if len(live) != 1:
        raise HotfixError(
            f"{kind}/{name} matches {len(live)} live pods; hotfix mode needs "
            "exactly one. Name the pod directly if this is a rollout in flight."
        )
    # The workload is already known and already checked, so the ownership walk
    # below is skipped: repeating it would be two more API calls to rediscover
    # the object the caller named.
    target = _target_from_pod_json(kube, live[0], container, follow_owner=False)
    return replace(
        target, workload_kind=kind, workload_name=name, replicas=replicas or 1
    )


def _target_from_pod(kube: Kubectl, name: str, container: str | None) -> HotfixTarget:
    return _target_from_pod_json(kube, kube.get_pod(name), container)


def _target_from_pod_json(
    kube: Kubectl,
    pod_json: Mapping[str, Any],
    container: str | None,
    *,
    follow_owner: bool = True,
) -> HotfixTarget:
    metadata = as_dict(pod_json.get("metadata"))
    name = _as_str(metadata.get("name"))
    if name is None:
        raise HotfixError("pod JSON has no metadata.name")
    chosen = _application_container(pod_json, container)
    image, digest = _image_of(pod_json, chosen)
    target = HotfixTarget(
        pod=PodRef(kube.namespace, name),
        container=chosen,
        image=image,
        image_digest=digest,
    )
    owner = _controller_of(metadata) if follow_owner else None
    if owner is None:
        return target
    kind, owner_name = owner
    if kind == "replicaset":
        return _through_replicaset(kube, target, owner_name)
    if kind in ("statefulset", "deployment"):
        return _with_workload(kube, target, kind, owner_name)
    # Something else owns it — a Job, an operator's CRD. Not refused: there is
    # one pod and the hotfix is legitimate, but the annotation cannot go on a
    # template podbench does not understand.
    return replace(target, workload_kind=kind, workload_name=owner_name, replicas=1)


def _through_replicaset(kube: Kubectl, target: HotfixTarget, name: str) -> HotfixTarget:
    """Resolve a pod's ReplicaSet to the Deployment that owns it.

    The Deployment is what has to be annotated: annotating the ReplicaSet's
    template would be overwritten by the next rollout, and annotating the pod
    does not survive the reschedule Hotfix mode depends on.
    """
    replicaset = _get_json(kube, "replicaset", name)
    replicas = replica_count(replicaset)
    if replicas is not None and replicas != 1:
        _refuse_multi_replica("replicaset", name, replicas)
    owner = _controller_of(as_dict(replicaset.get("metadata")))
    if owner is not None and owner[0] == "deployment":
        return _with_workload(kube, target, "deployment", owner[1])
    return replace(
        target, workload_kind="replicaset", workload_name=name, replicas=replicas or 1
    )


def _with_workload(
    kube: Kubectl, target: HotfixTarget, kind: str, name: str
) -> HotfixTarget:
    workload = _get_json(kube, kind, name)
    replicas = replica_count(workload)
    if replicas is not None and replicas != 1:
        _refuse_multi_replica(kind, name, replicas)
    return replace(
        target, workload_kind=kind, workload_name=name, replicas=replicas or 1
    )


def _application_container(pod_json: Mapping[str, Any], requested: str | None) -> str:
    names = [
        name
        for entry in _as_list(as_dict(pod_json.get("spec")).get("containers"))
        if (name := _as_str(as_dict(entry).get("name"))) is not None
    ]
    if not names:
        raise HotfixError("pod has no containers")
    if requested is None:
        return names[0]
    if requested not in names:
        raise HotfixError(f"container {requested!r} not in pod (has {names})")
    return requested


def _image_of(pod_json: Mapping[str, Any], container: str) -> tuple[str, str]:
    for entry in _as_list(as_dict(pod_json.get("status")).get("containerStatuses")):
        status = as_dict(entry)
        if _as_str(status.get("name")) == container:
            return (
                _as_str(status.get("image")) or "",
                _as_str(status.get("imageID")) or "",
            )
    for entry in _as_list(as_dict(pod_json.get("spec")).get("containers")):
        spec = as_dict(entry)
        if _as_str(spec.get("name")) == container:
            return _as_str(spec.get("image")) or "", ""
    return "", ""


def _controller_of(metadata: Mapping[str, Any]) -> tuple[str, str] | None:
    for entry in _as_list(metadata.get("ownerReferences")):
        owner = as_dict(entry)
        if owner.get("controller") is not True:
            continue
        kind = _as_str(owner.get("kind"))
        name = _as_str(owner.get("name"))
        if kind is not None and name is not None:
            return kind.lower(), name
    return None


def _phase(pod_json: Mapping[str, Any]) -> str:
    return _as_str(as_dict(pod_json.get("status")).get("phase")) or ""


def seat_container(kube: Kubectl, pod: str, requested: str | None) -> str:
    """The podbench container that mounts the same claim.

    Hotfix mode's premise is that this container exists: it is the one with git
    and a shell, and — because it mounts the claim at the identical mountPath —
    the one place where the checkout and the venv resolve to the same paths the
    application sees.
    """
    if requested is not None:
        return requested
    seat = running_seat(kube.get_pod(pod))
    if seat is None:
        raise HotfixError(
            f"no running podbench container in pod {pod}. Hotfix mode reaches the "
            "claim through the seat, which must mount it at the same mountPath "
            f"as the application: run `podbench attach {pod}` first, or "
            f"pass --seat if it is named something other than {CONTAINER_BASE}-N."
        )
    return seat.name


# -- health ----------------------------------------------------------------


class HotfixHealth(Enum):
    """What ``status`` found, worst-first when it has to pick one."""

    ACTIVE = "active"
    """The hotfix is live and the image it was made against is still deployed."""

    INTERPRETER_MISMATCH = "interpreter"
    """The image's interpreter no longer matches the venv on the claim."""

    SUPERSEDED = "superseded"
    """Consolidated, and the image has since changed: the claim is probably
    stale and is now the only thing keeping the old code alive."""

    IMAGE_CHANGED = "image-changed"
    """The image was upgraded under a live hotfix mount, so the pod is running
    the claim's venv and not the new image's."""

    UNREADABLE = "unreadable"
    """The pod says it is hotfixed and its manifest cannot be read."""

    @property
    def summary(self) -> str:
        """One line for the status table."""
        return {
            HotfixHealth.ACTIVE: "hotfixed, base image unchanged",
            HotfixHealth.INTERPRETER_MISMATCH: "venv interpreter no longer matches",
            HotfixHealth.SUPERSEDED: "consolidated; claim is probably stale",
            HotfixHealth.IMAGE_CHANGED: "image upgraded under the hotfix mount",
            HotfixHealth.UNREADABLE: "marked hotfixed, manifest unreadable",
        }[self]

    @property
    def ok(self) -> bool:
        """Whether this needs somebody's attention today."""
        return self is HotfixHealth.ACTIVE


def assess(
    manifest: HotfixManifest | None,
    *,
    current_digest: str,
    probe: InterpreterProbe | None = None,
) -> tuple[HotfixHealth, str]:
    """Judge one hotfixed pod. Pure, because this is the whole feature.

    The order is deliberate: a broken interpreter beats everything, because the
    pod is not running what anyone thinks it is; a consolidated hotfix on a
    changed image beats a merely-changed image, because that one has a known
    remedy (retire the claim).
    """
    if manifest is None:
        return (
            HotfixHealth.UNREADABLE,
            "the pod carries the hotfixed annotation but no readable manifest; "
            "provenance for whatever is on the claim has been lost",
        )
    if probe is not None and not probe.ok:
        return (
            HotfixHealth.INTERPRETER_MISMATCH,
            f"the venv's interpreter would not run in this container: "
            f"{probe.detail}. The claim's bin/python points into the image, and "
            "an image change can move it.",
        )
    if probe is not None and probe.version is not None:
        warning = interpreter_warning(manifest.interpreter, probe.version)
        if warning is not None:
            return HotfixHealth.INTERPRETER_MISMATCH, warning
    changed = bool(
        manifest.base_image_digest
        and current_digest
        and manifest.base_image_digest != current_digest
    )
    if changed and manifest.consolidated_branch is not None:
        return (
            HotfixHealth.SUPERSEDED,
            f"the hotfix was consolidated onto {manifest.consolidated_branch} and "
            "the image has changed since. If the rebuild included it, the claim "
            "is now shadowing the released fix with an older copy of it: retire "
            "the claim and turn hotfixVenv off.",
        )
    if changed:
        return (
            HotfixHealth.IMAGE_CHANGED,
            f"deployed image is {current_digest.split('@')[-1][:19]}, the hotfix "
            f"was made against {manifest.base_image_digest.split('@')[-1][:19]}. "
            "The claim's venv shadows the new image's, so the upgrade has not "
            "reached the running code.",
        )
    return HotfixHealth.ACTIVE, f"{manifest.ahead} commit(s) ahead of the image"


@dataclass(frozen=True)
class HotfixRow:
    """One hotfixed pod, as ``status`` reports it."""

    pod: PodRef
    manifest: HotfixManifest | None
    health: HotfixHealth
    detail: str
    current_image: str = ""
    notes: tuple[str, ...] = ()


def status_rows(
    kube: Kubectl, *, probe: bool = True, python: str = DEFAULT_PYTHON
) -> list[HotfixRow]:
    """Every hotfixed pod in the namespace, with its drift and its risks.

    The manifest travels in an annotation so this is one ``get pods`` for the
    whole namespace. The interpreter is only probed when the image has moved,
    which is the one case where it can have broken — an exec per pod on every
    status call would make the command too slow to run habitually, and a status
    command nobody runs is how silently-diverged pods happen.
    """
    result = kube.run("get", "pods", "-o", "json")
    rows: list[HotfixRow] = []
    for item in _as_list(_load_json(result.stdout).get("items")):
        pod_json = as_dict(item)
        metadata = as_dict(pod_json.get("metadata"))
        annotations = as_dict(metadata.get("annotations"))
        if _as_str(annotations.get(HOTFIXED_ANNOTATION)) != "true":
            continue
        name = _as_str(metadata.get("name"))
        if name is None:
            continue
        pod = PodRef(kube.namespace, name)
        notes: list[str] = []
        manifest: HotfixManifest | None
        try:
            manifest = manifest_from_pod(pod_json)
        except (HotfixError, ValueError) as error:
            manifest = None
            notes.append(str(error))
        container = manifest.container if manifest is not None else ""
        container = container or _application_container(pod_json, None)
        image, digest = _image_of(pod_json, container)
        measured: InterpreterProbe | None = None
        if (
            probe
            and manifest is not None
            and manifest.base_image_digest
            and digest
            and manifest.base_image_digest != digest
        ):
            measured = probe_interpreter(kube, name, container, python=python)
        health, detail = assess(manifest, current_digest=digest, probe=measured)
        if manifest is not None and manifest.stale_schema:
            notes.append(
                f"manifest is schema version {manifest.schema_version}; it was "
                "written by an older podbench and some provenance may be absent"
            )
        rows.append(
            HotfixRow(
                pod=pod,
                manifest=manifest,
                health=health,
                detail=detail,
                current_image=image,
                notes=tuple(notes),
            )
        )
    return rows


_FLAG = 10
"""Where a row's pod name starts, which is ``doctor``'s column so the two tables
scan alike — and the status is bracketed for the same reason, so that
:mod:`podbench.console` colours it without either module saying what a colour
is."""

_ROW_INDENT = " " * 4
"""Where a row's prose sits under it."""


def format_status(rows: Sequence[HotfixRow]) -> str:
    """The status report. Empty is a real and reassuring answer, so say it.

    Only the prose is wrapped. The row line itself is a set of columns held
    apart by double spaces, and wrapping collapses whitespace — so a row put
    through it would come back as a sentence, with the commit and the health
    no longer under the headings the eye is running down.
    """
    if not rows:
        return "no hotfixed pods in this namespace"
    lines: list[str] = []
    for row in rows:
        manifest = row.manifest
        ahead = manifest.ahead if manifest is not None else 0
        commit = manifest.commit[:7] if manifest is not None else "unknown"
        flag = "[ok]" if row.health.ok else "[!]"
        lines.append(
            f"  {flag}".ljust(_FLAG) + f"{row.pod}  +{ahead} commit(s)  {commit}  "
            f"{row.health.value} — {row.health.summary}"
        )
        lines.extend(paragraph(row.detail, first=_ROW_INDENT, indent=_ROW_INDENT))
        if manifest is not None:
            lines.append(
                f"{_ROW_INDENT}base {manifest.base_commit[:7] or '?'} · "
                f"{manifest.author or 'unknown author'} · "
                f"{manifest.timestamp or 'no timestamp'}"
            )
            for entry in manifest.commits:
                # The sha goes in `first` rather than into the wrapped text, so
                # the two spaces holding the column stay two spaces.
                lead = f"{_ROW_INDENT}  {entry.short}  "
                lines.extend(
                    paragraph(entry.subject, first=lead, indent=" " * len(lead))
                )
            if ahead > len(manifest.commits):
                lines.append(
                    f"{_ROW_INDENT}  … and {ahead - len(manifest.commits)} more "
                    "(the manifest keeps the most recent)"
                )
        for note in row.notes:
            lines.extend(
                paragraph(
                    note, first=f"{_ROW_INDENT}note: ", indent=f"{_ROW_INDENT}      "
                )
            )
    return "\n".join(lines)


# -- the workflow ----------------------------------------------------------


def annotate(kube: Kubectl, target: HotfixTarget, manifest: HotfixManifest) -> str:
    """Record the provenance where the reschedule cannot lose it.

    A *merge* patch, and deliberately the opposite choice from the Service
    cutover in :mod:`podbench.dev`: there a merge unioned two selectors and
    silently kept the old pod in the endpointslice, whereas here union is
    exactly right — podbench is adding three annotations to whatever else the
    object carries, not replacing the map.
    """
    annotations = manifest_annotations(manifest)
    if target.workload_kind in ("deployment", "statefulset"):
        kube.patch(
            cast(str, target.workload_kind),
            cast(str, target.workload_name),
            {"spec": {"template": {"metadata": {"annotations": annotations}}}},
            patch_type="merge",
        )
        return (
            f"annotated {target.workload} pod template; the edit rolls the "
            "workload, which is how the hotfix is picked up"
        )
    kube.patch(
        "pod",
        target.pod.name,
        {"metadata": {"annotations": annotations}},
        patch_type="merge",
    )
    return (
        f"annotated pod {target.pod.name} directly — it has no pod template, so "
        "this provenance is lost if the pod is ever replaced"
    )


def _identity(store: HotfixStore, checkout: str, author: str | None) -> tuple[str, str]:
    """The name and email a hotfix commit is made under.

    Read from the checkout's git config when the caller does not say, so the
    provenance is whoever the seat is configured as rather than a constant. The
    fallback is deliberately obviously-not-a-person.
    """
    if author is not None:
        name, _, email = author.partition("<")
        return name.strip() or "podbench", email.strip().rstrip(">") or "podbench@local"
    configured = store.run(
        ["git", "-C", checkout, "config", "--get", "user.email"], check=False
    )
    email = configured.stdout.strip()
    named = store.run(
        ["git", "-C", checkout, "config", "--get", "user.name"], check=False
    )
    name = named.stdout.strip()
    return name or "podbench", email or "podbench@local"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def init(
    kube: Kubectl,
    store: HotfixStore,
    target: HotfixTarget,
    *,
    venv: str,
    repo: str,
    ref: str | None = None,
    base_commit: str | None = None,
    author: str | None = None,
    install: bool = True,
) -> tuple[HotfixManifest, list[str]]:
    """Prepare a claim: verify the seed, put the source on it, record the base.

    The seed is *verified*, never performed. Once the claim is mounted over the
    venv path the image's own venv is hidden behind it in every container, so
    the only moment it can be copied is before the application container starts
    — which is an initContainer's job, and ``--print-values`` emits one. A
    podbench that offered to seed after the fact would be offering to copy a
    directory it cannot see.
    """
    actions: list[str] = []
    config = store.read_text(f"{venv.rstrip('/')}/pyvenv.cfg")
    if config is None:
        raise HotfixError(
            f"{venv}/pyvenv.cfg is missing: the claim is mounted but was never "
            "seeded from the image's venv. Seeding has to happen in an "
            "initContainer, before the mount hides the image's copy — run "
            "`podbench hotfix --print-values` for the snippet, and note "
            "that the initContainer must use the application's own image."
        )
    settings = parse_pyvenv_cfg(config)
    interpreter = normalise_interpreter(
        settings.get("version") or settings.get("version_info") or ""
    )
    actions.append(f"claim seeded, venv interpreter {interpreter or 'unknown'}")

    checkout = checkout_path(venv)
    if store.exists(f"{checkout}/.git"):
        actions.append(f"checkout already present at {checkout}")
    else:
        clone = ["git", "clone"]
        if ref is not None:
            clone += ["--branch", ref]
        clone += [repo, checkout]
        store.run(clone)
        actions.append(f"cloned {repo} to {checkout}")

    resolved = (
        base_commit
        or store.run(["git", "-C", checkout, "rev-parse", "HEAD"]).stdout.strip()
    )

    if install:
        actions.append(_install(kube, target, venv, checkout))

    name, email = _identity(store, checkout, author)
    manifest = HotfixManifest(
        venv=venv,
        checkout=checkout,
        repo=repo,
        base_image=target.image,
        base_image_digest=target.image_digest,
        interpreter=interpreter,
        container=target.container,
        commit=resolved,
        base_commit=resolved,
        author=f"{name} <{email}>",
        timestamp=_now(),
        ahead=0,
        commits=(),
    )
    write_manifest(store, manifest)
    actions.append(f"wrote {manifest_path(venv)}")
    actions.append(annotate(kube, target, manifest))
    return manifest, actions


def _install(kube: Kubectl, target: HotfixTarget, venv: str, checkout: str) -> str:
    argv = install_argv(venv, checkout)
    # A resolve and a build against the cluster's index, so the pod-work bound.
    result = kube.exec_(
        target.pod.name,
        argv,
        container=target.container,
        check=False,
        timeout=POD_WORK_TIMEOUT,
    )
    if result.returncode != 0:
        raise HotfixError(
            f"editable install failed in container {target.container}: "
            f"{result.stderr.strip() or result.stdout.strip()}. It has to run "
            "there and not in the seat: the venv's bin/python is a symlink into "
            "the application image. If that image has no pip, install once at "
            "build time and re-run with --no-install."
        )
    return f"editable install of {checkout} into {venv}"


def apply_hotfix(
    kube: Kubectl,
    store: HotfixStore,
    target: HotfixTarget,
    *,
    venv: str,
    message: str,
    author: str | None = None,
    bounce: bool = True,
) -> tuple[HotfixManifest, list[str]]:
    """Commit what is in the checkout, reinstall if needed, and roll the pod.

    The commit is not optional and not a convenience: a hotfix that is only a
    working-tree edit has no sha, and without a sha the manifest cannot say what
    is running and ``consolidate`` has nothing to push.
    """
    actions: list[str] = []
    manifest = read_manifest(store, venv)
    if manifest is None:
        raise HotfixError(
            "no hotfix manifest on the claim: run `hotfix init` first so the "
            "commit this hotfix diverges from is recorded"
        )
    checkout = manifest.checkout or checkout_path(manifest.venv)

    dirty = store.run(["git", "-C", checkout, "status", "--porcelain"]).stdout.strip()
    if not dirty:
        actions.append("working tree clean; nothing new to commit")
    else:
        store.run(["git", "-C", checkout, "add", "-A"])
        name, email = _identity(store, checkout, author)
        store.run(
            [
                "git",
                "-C",
                checkout,
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={email}",
                "commit",
                "-m",
                message,
            ]
        )
        actions.append(f"committed as {name} <{email}>")

    head = store.run(["git", "-C", checkout, "rev-parse", "HEAD"]).stdout.strip()
    commits = drift_commits(store, checkout, manifest.base_commit)

    # From what the manifest last recorded to HEAD, not from what this apply
    # happened to commit: a hand commit made in the seat moves HEAD with the
    # tree clean, and the question "is the .dist-info still valid?" is about the
    # commits either way.
    changed = changed_paths(store, checkout, manifest.commit, head)
    if metadata_changed(changed):
        actions.append(_install(kube, target, manifest.venv, checkout))
    elif changed:
        actions.append("no packaging metadata changed; editable install still valid")

    updated_author = manifest.author
    if dirty:
        name, email = _identity(store, checkout, author)
        updated_author = f"{name} <{email}>"
    manifest = replace(
        manifest,
        commit=head,
        author=updated_author,
        timestamp=_now(),
        ahead=len(commits),
        commits=commits[:MAX_RECORDED_COMMITS],
        base_image=target.image or manifest.base_image,
        base_image_digest=target.image_digest or manifest.base_image_digest,
        schema_version=MANIFEST_VERSION,
    )
    write_manifest(store, manifest)
    actions.append(f"{len(commits)} commit(s) ahead of {manifest.base_commit[:7]}")
    actions.append(annotate(kube, target, manifest))

    if bounce:
        actions.append(_bounce(kube, target))
    else:
        actions.append("not bounced; the running process still has the old code")
    return manifest, actions


def _bounce(kube: Kubectl, target: HotfixTarget) -> str:
    """Make the kubelet pick the hotfix up.

    With a pod template the annotation edit has already rolled the workload, so
    there is nothing more to do — and doing more would race the rollout. A bare
    pod has to be deleted, and deleting a pod nobody owns destroys it, so that
    one is refused rather than done helpfully.
    """
    if target.workload_kind in ("deployment", "statefulset"):
        return "the annotation edit rolls the workload; watch `kubectl rollout status`"
    if target.workload_kind is None:
        return (
            "not bounced: this pod has no controller, so deleting it would not "
            "bring it back. Restart the application process yourself — with the "
            "venv on the claim, the restart is the relaunch."
        )
    kube.delete_pod(target.pod.name)
    return f"deleted pod {target.pod.name}; {target.workload} will replace it"


def consolidate(
    kube: Kubectl,
    store: HotfixStore,
    target: HotfixTarget,
    *,
    venv: str,
    branch: str,
    remote: str = "origin",
    push: bool = True,
) -> tuple[HotfixManifest, list[str]]:
    """Push the claim's checkout as a branch, ready for a real rebuild.

    The PR itself is not opened here: that needs a forge client podbench does
    not depend on, and a printed ``gh pr create`` is one paste. What matters is
    that the branch exists and that the manifest — and therefore ``status`` —
    records that this hotfix is on its way into an image, because that is what
    turns a stale claim from a mystery into a named condition.
    """
    actions: list[str] = []
    manifest = read_manifest(store, venv)
    if manifest is None:
        raise HotfixError("no hotfix manifest on the claim; nothing to consolidate")
    checkout = manifest.checkout or checkout_path(manifest.venv)
    commits = drift_commits(store, checkout, manifest.base_commit)
    if not commits:
        raise HotfixError(
            f"the checkout is not ahead of {manifest.base_commit[:7]}: there is "
            "no hotfix to consolidate. If the fix is already in the image, retire "
            "the claim instead — see `hotfix status`."
        )
    if push:
        store.run(["git", "-C", checkout, "push", remote, f"HEAD:refs/heads/{branch}"])
        actions.append(f"pushed {len(commits)} commit(s) to {remote}/{branch}")
    else:
        actions.append(f"would push {len(commits)} commit(s) to {remote}/{branch}")

    manifest = replace(
        manifest,
        consolidated_branch=branch,
        timestamp=_now(),
        ahead=len(commits),
        commits=commits[:MAX_RECORDED_COMMITS],
        schema_version=MANIFEST_VERSION,
    )
    write_manifest(store, manifest)
    actions.append(annotate(kube, target, manifest))
    actions.append(_retirement_checklist(branch, manifest, target))
    return manifest, actions


def _retirement_checklist(
    branch: str, manifest: HotfixManifest, target: HotfixTarget
) -> str:
    workload = target.workload or f"pod/{target.pod.name}"
    return "\n".join(
        [
            "",
            "next, in order — the claim is now the only copy of this fix:",
            f"  1. gh pr create --head {branch} --title 'consolidate hotfix "
            f"{manifest.commit[:7]}'",
            "  2. merge, and let CI build and publish the image",
            f"  3. roll {workload} onto the new image and confirm it is healthy",
            "  4. remove the volume/volumeMount from the application's values",
            "  5. set hotfixVenv.enabled=false and delete the claim",
            "",
            "until step 5 the claim keeps shadowing the image's venv, and "
            "`hotfix status` will report this pod as superseded.",
        ]
    )


# -- helm values -----------------------------------------------------------


def identity_configmap(app: str) -> str:
    """The ConfigMap ``seatIdentity`` emits for one application.

    Derived, not configured, so that the chart and this snippet cannot drift:
    ``Charts/podbench/templates/_helpers.tpl`` builds the same name from the
    same field. It is keyed on the application rather than on the podbench
    release because the volume that references it lives in the *application's*
    pod spec.

    >>> identity_configmap("api")
    'api-podbench-identity'
    """
    return f"{app}-podbench-identity"


def values_snippet(
    app: str,
    venv: str,
    *,
    image: str = "<the application's own image>",
    size: str = "2Gi",
    uid: str = "<the application's runAsUser>",
    gid: str = "<the application's runAsGroup>",
    home_size: str = SEAT_HOME_SIZE,
    volumes_key: str = "extraVolumes",
    mounts_key: str = "extraVolumeMounts",
    init_key: str = "initContainers",
    security_key: str = "podSecurityContext",
) -> str:
    """The values an application's chart needs, ready to paste.

    Kept as small as it can be, because this is the *only* deploy-time
    cooperation podbench ever asks for and every line of it is a line somebody
    has to defend in review. The key names are parameters because charts differ
    on what they call their passthroughs; the shapes underneath are plain
    Kubernetes and do not.

    The initContainer is the load-bearing part and the part that looks
    redundant. It mounts the claim at a *staging* path, which is the only moment
    the image's own venv is still visible: once the claim is mounted over the
    venv path, the thing that has to be copied is behind the mount.

    The seat's own two volumes ride along here for one reason: they are subject
    to the identical constraint. An ephemeral container may only mount volumes
    the pod already declares, and pod volumes are immutable after creation, so
    the writable home that keeps a seat's disk use off the workload's
    ephemeral-storage budget has to be in the spec at deploy time or not at all.

    The identity volume is emitted beside it and is **not** what gives a live-pod
    seat its login: projecting a passwd *file* takes a ``subPath`` per mount and
    an ephemeral container may not have one, so ``attach`` never mounts it and a
    live-pod seat registers its own record instead. It is emitted because it is
    the identity a seat that is an *ordinary* container can be given, which is
    what ``podbench dev`` authors — and the comments say exactly that, so nobody
    deploys it expecting it to fix an ssh they cannot get. ``uid``/``gid``
    default to placeholders rather than to plausible numbers: a wrong uid pasted
    unread is a seat that authenticates as nobody, and a snippet that fails at
    ``helm install`` beats one that fails at 3am.
    """
    claim = f"{app}-venv"
    configmap = identity_configmap(app)
    return "\n".join(
        [
            f"# 1. values for the podbench release — creates the claim {claim}",
            f"#    and the identity ConfigMap {configmap}",
            "hotfixVenv:",
            "  enabled: true",
            "  claims:",
            f"    - name: {app}",
            f"      size: {size}",
            "seatIdentity:",
            "  # An /etc/passwd + /etc/group for a seat that runs as this",
            f"  # application's uid. sshd cannot log in a {SEAT_USER!r} that NSS —",
            "  # i.e. this file — does not resolve at that uid, so uid/gid must",
            "  # be the application container's own.",
            "  #",
            "  # It serves a seat that is an *ordinary* container. `attach` does",
            "  # not use it: landing the two files takes a subPath per mount and",
            "  # the API server forbids subPath on an ephemeral container, so a",
            "  # live-pod seat registers its own record at start-up instead, in",
            "  # the image's own NSS database and with no help from this chart.",
            "  # It is enabled here because Hotfix mode's seat is an ordinary",
            "  # container; set it false if this application only ever gets",
            "  # live-pod `attach` sessions.",
            "  enabled: true",
            "  apps:",
            f"    - name: {app}",
            f"      uid: {uid}",
            f"      gid: {gid}",
            "",
            f"# 2. values for {app}'s own chart — mounts it over {venv}",
            "#    Use whatever passthrough your chart already has for these four",
            "#    keys; the names below are the common convention, not a",
            "#    requirement. The shapes underneath them are plain Kubernetes.",
            f"{volumes_key}:",
            "  - name: podbench-hotfix-venv",
            "    persistentVolumeClaim:",
            f"      claimName: {claim}",
            "  # The next two are the seat's, and they are deliberately *declared",
            "  # and not mounted* by the application container — which looks like",
            "  # a mistake and is not. Declaring them is enough: an ephemeral",
            "  # container may only mount volumes its pod already has, and pod",
            "  # volumes cannot be added later. Mounting them into the",
            "  # application as well would change its filesystem for no gain.",
            f"  # {SEAT_IDENTITY_VOLUME} is the one `attach` cannot use — it needs a",
            "  # subPath per file, which an ephemeral container may not have. It",
            "  # is here for a seat that is an ordinary container; drop it if you",
            "  # only ever attach to live pods, whose seats register a record of",
            "  # their own and need nothing declared for it.",
            f"  - name: {SEAT_IDENTITY_VOLUME}",
            "    configMap:",
            f"      name: {configmap}",
            "      # Read-only to everybody: this is the seat's /etc/passwd, and",
            "      # nothing in the pod has any business rewriting it.",
            "      defaultMode: 0444",
            f"  # {SEAT_HOME_VOLUME} *is* mounted by `attach`, by convention, and is",
            "  # worth having on its own: it keeps everything the seat writes off",
            "  # the workload's ephemeral-storage budget.",
            f"  - name: {SEAT_HOME_VOLUME}",
            "    emptyDir:",
            "      # vscode-server unpacks to ~700 MiB and a real session reaches",
            "      # 1.1-1.3 GB. Unbounded, that comes out of the node's ephemeral",
            "      # storage and an overrun evicts the pod — application included.",
            f"      sizeLimit: {home_size}",
            f"{mounts_key}:",
            "  - name: podbench-hotfix-venv",
            f"    mountPath: {venv}",
            f"{security_key}:",
            "  # Not optional if the seat is to be able to write its own home: an",
            "  # emptyDir is created root:root, and the seat runs as the",
            "  # application's uid, not as root. Without fsGroup the kubelet never",
            f"  # chgrps the volume and {SEAT_HOME_PATH} is present and unwritable,",
            "  # which is worse than absent — everything starts, then fails.",
            f"  fsGroup: {gid}",
            f"{init_key}:",
            "  # Seeds an empty claim from the image's venv. It mounts the claim",
            "  # somewhere else on purpose: at this point the image's venv is",
            f"  # still visible at {venv}, and after the main mount it is not.",
            "  # The image must be the application's own, or the venv is the",
            "  # wrong one.",
            "  - name: podbench-seed-venv",
            f"    image: {image}",
            '    command: ["sh", "-c"]',
            "    args:",
            "      - test -e /podbench-seed/pyvenv.cfg || cp -a "
            f"{venv.rstrip('/')}/. /podbench-seed/",
            "    volumeMounts:",
            "      - name: podbench-hotfix-venv",
            "        mountPath: /podbench-seed",
            "",
            "# Single replica only: the claim is ReadWriteOnce and one checkout",
            "# cannot serve two writers. Take hotfixVenv off again — and delete the",
            "# claim — once `hotfix consolidate` has been through the pipeline.",
            f"# {SEAT_HOME_VOLUME} has no such lifetime: it is deployment",
            "# furniture, it costs one volume the application does not mount, and",
            "# it is what keeps a seat's disk use off the workload's budget on",
            "# the day the namespace refuses the ptrace rung.",
        ]
    )


# -- JSON helpers ----------------------------------------------------------


def _load_json(text: str) -> dict[str, Any]:
    parsed: object = json.loads(text)
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {}


def _get_json(kube: Kubectl, kind: str, name: str) -> dict[str, Any]:
    return _load_json(kube.run("get", kind, name, "-o", "json").stdout)


def _as_list(value: Any) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


# -- CLI -------------------------------------------------------------------


_Target = Annotated[
    str,
    typer.Argument(
        metavar="TARGET", help="pod/NAME, deployment/NAME or statefulset/NAME"
    ),
]
_Venv = Annotated[
    str,
    typer.Option(
        "--venv",
        metavar="PATH",
        help="the mountPath the claim is mounted at, i.e. the application's venv path",
    ),
]
_Container = Annotated[
    str | None,
    typer.Option(
        "--container",
        metavar="NAME",
        help="the application container (default: first)",
    ),
]
_Seat = Annotated[
    str | None,
    typer.Option(
        "--seat",
        metavar="NAME",
        help="the podbench container that mounts the same claim "
        f"(default: the running {CONTAINER_BASE}-N)",
    ),
]
_Local = Annotated[
    bool,
    typer.Option(
        "--local",
        help="reach the claim through this process's own filesystem instead of "
        "through the seat — for running the command from inside the seat's own "
        "terminal, where the claim is already mounted at --venv",
    ),
]
_Author = Annotated[
    str | None,
    typer.Option("--author", metavar="AUTHOR", help='"Name <email>" for the commit'),
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


def _store_for(
    kube: Kubectl, pod: str, *, seat: str | None, local: bool
) -> HotfixStore:
    if local:
        return LocalStore()
    return PodStore(kube=kube, pod=pod, container=seat_container(kube, pod, seat))


def _report(actions: Sequence[str]) -> int:
    emit("\n".join(actions))
    return 0


def _build_app(runner: Runner | None) -> typer.Typer:
    app = new_app()

    @app.callback(invoke_without_command=True)
    def root(
        ctx: typer.Context,
        print_values: Annotated[
            bool,
            typer.Option(
                "--print-values",
                help="emit the helm values an application's chart needs, and exit",
            ),
        ] = False,
        app_name: Annotated[
            str | None,
            typer.Option(
                "--app", metavar="NAME", help="application name, for --print-values"
            ),
        ] = None,
        venv_path: Annotated[
            str | None,
            typer.Option(
                "--venv-path",
                metavar="PATH",
                help="the application's venv path, for --print-values",
            ),
        ] = None,
        size: Annotated[
            str,
            typer.Option(
                "--size", metavar="SIZE", help="claim size, for --print-values"
            ),
        ] = "2Gi",
        app_image: Annotated[
            str,
            typer.Option(
                "--app-image",
                metavar="REF",
                help="image the seeding initContainer runs, for --print-values",
            ),
        ] = "<the application's own image>",
        # Left as placeholders when unset, so that a snippet pasted without
        # reading it fails at `helm install` rather than deploying an identity
        # for the wrong uid — which fails later, in the dark, as a login that is
        # simply refused.
        uid: Annotated[
            str,
            typer.Option(
                "--uid",
                metavar="UID",
                help="the application container's uid, for --print-values",
            ),
        ] = "<the application's runAsUser>",
        gid: Annotated[
            str,
            typer.Option(
                "--gid",
                metavar="GID",
                help="the application container's gid, for --print-values",
            ),
        ] = "<the application's runAsGroup>",
    ) -> None:
        """Durable in-place fixes: a venv on a claim, every change a commit, and
        a status command that will not let a hotfixed pod go unnoticed.
        """
        if print_values:
            if app_name is None or venv_path is None:
                print(
                    "podbench: --print-values needs --app NAME and --venv-path PATH",
                    file=sys.stderr,
                )
                raise typer.Exit(2)
            print(
                values_snippet(
                    app_name,
                    venv_path,
                    image=app_image,
                    size=size,
                    uid=uid,
                    gid=gid,
                )
            )
            raise typer.Exit(0)
        # `hotfix --print-values` is a legitimate whole command line, so the
        # subcommand is only required once that flag has been ruled out.
        require_subcommand(ctx)

    # `init_command`/`consolidate_command`, not `init`/`consolidate`: the
    # module-level functions of those names are what they call, and a same-named
    # closure would shadow them into a recursion.
    @app.command(
        name="init",
        help="verify the seeded claim, clone the source, editable-install",
    )
    def init_command(
        target: _Target,
        repo: Annotated[
            str, typer.Option("--repo", metavar="URL", help="git URL to clone")
        ],
        venv: _Venv,
        ref: Annotated[
            str | None,
            typer.Option("--ref", metavar="REF", help="branch or tag to clone"),
        ] = None,
        base_commit: Annotated[
            str | None,
            typer.Option(
                "--base-commit",
                metavar="SHA",
                help="the commit the released image was built from "
                "(default: cloned HEAD)",
            ),
        ] = None,
        no_install: Annotated[
            bool,
            typer.Option(
                "--no-install",
                help="skip the editable install (the application image has no pip)",
            ),
        ] = False,
        container: _Container = None,
        seat: _Seat = None,
        local: _Local = False,
        author: _Author = None,
        namespace: _Namespace = None,
        context: _Context = None,
        kubectl: _KubectlBinary = "kubectl",
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        resolved = resolve_target(kube, target, container=container)
        store = _store_for(kube, resolved.pod.name, seat=seat, local=local)
        _, actions = init(
            kube,
            store,
            resolved,
            venv=venv,
            repo=repo,
            ref=ref,
            base_commit=base_commit,
            author=author,
            install=not no_install,
        )
        raise typer.Exit(_report(actions))

    @app.command(help="commit the change on the claim and roll the workload")
    def apply(
        target: _Target,
        message: Annotated[
            str,
            typer.Option("-m", "--message", metavar="TEXT", help="commit message"),
        ],
        venv: _Venv,
        no_bounce: Annotated[
            bool,
            typer.Option(
                "--no-bounce",
                help="leave the running process alone; the hotfix takes effect "
                "on the next restart",
            ),
        ] = False,
        container: _Container = None,
        seat: _Seat = None,
        local: _Local = False,
        author: _Author = None,
        namespace: _Namespace = None,
        context: _Context = None,
        kubectl: _KubectlBinary = "kubectl",
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        resolved = resolve_target(kube, target, container=container)
        store = _store_for(kube, resolved.pod.name, seat=seat, local=local)
        _, actions = apply_hotfix(
            kube,
            store,
            resolved,
            venv=venv,
            message=message,
            author=author,
            bounce=not no_bounce,
        )
        raise typer.Exit(_report(actions))

    @app.command(help="every hotfixed pod in the namespace, and its drift")
    def status(
        no_probe: Annotated[
            bool,
            typer.Option(
                "--no-probe",
                help="do not exec to measure the interpreter of a changed image",
            ),
        ] = False,
        python: Annotated[
            str,
            typer.Option("--python", metavar="BIN", help="interpreter to measure"),
        ] = DEFAULT_PYTHON,
        namespace: _Namespace = None,
        context: _Context = None,
        kubectl: _KubectlBinary = "kubectl",
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        rows = status_rows(kube, probe=not no_probe, python=python)
        emit(format_status(rows))
        raise typer.Exit(0 if all(row.health.ok for row in rows) else 1)

    @app.command(
        name="consolidate",
        help="push the claim's checkout as a branch for the rebuild",
    )
    def consolidate_command(
        target: _Target,
        branch: Annotated[
            str, typer.Option("--branch", metavar="NAME", help="branch to push")
        ],
        venv: _Venv,
        remote: Annotated[
            str, typer.Option("--remote", metavar="NAME", help="git remote to push to")
        ] = "origin",
        dry_run: Annotated[
            bool,
            typer.Option(
                "--dry-run", help="say what would be pushed without pushing it"
            ),
        ] = False,
        container: _Container = None,
        seat: _Seat = None,
        local: _Local = False,
        author: _Author = None,
        namespace: _Namespace = None,
        context: _Context = None,
        kubectl: _KubectlBinary = "kubectl",
    ) -> None:
        del author  # accepted for symmetry; this verb writes no commit
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        resolved = resolve_target(kube, target, container=container)
        store = _store_for(kube, resolved.pod.name, seat=seat, local=local)
        _, actions = consolidate(
            kube,
            store,
            resolved,
            venv=venv,
            branch=branch,
            remote=remote,
            push=not dry_run,
        )
        raise typer.Exit(_report(actions))

    return app


def main(args: Sequence[str] | None = None, *, runner: Runner | None = None) -> int:
    """Entry point for ``podbench hotfix <subcommand>``.

    ``status`` exits non-zero when any pod needs attention, so that it is usable
    as a shutdown-checklist assertion — "no pod is still carrying an unretired
    hotfix" is a thing a facility wants to be able to test, not read.
    """
    argv = list(sys.argv[1:] if args is None else args)
    # The central dispatcher keeps the verb in argv; this app's subcommands are
    # the *sub*-verbs, so the leading `hotfix` is consumed here.
    if argv and argv[0] == "hotfix":
        argv = argv[1:]
    try:
        return run(_build_app(runner), argv, prog="podbench hotfix")
    except (HotfixError, KubectlError, ValueError) as error:
        print(f"podbench: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
