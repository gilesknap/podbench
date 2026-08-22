"""Hotfix mode: an emergency fix that outlives the session, and its provenance.

Hotfix mode is the one place podbench asks for deploy-time cooperation. What it
asks for is five ordinary values passthroughs - ``volumes``, ``volumeMounts``,
``command``/``args``, ``livenessProbe`` and ``podSecurityContext`` - which every
chart tried against it already has, and which
:func:`values_snippet` emits ready to paste.

The claim mounts **beside** the application's project, at
:data:`~podbench.model.HOTFIX_APP_PATH`, never over it. Every awkward thing about
the earlier design followed from mounting over the venv: the image's own copy sat
behind the mount, so seeding needed an initContainer at a staging path that
``ioc-instance`` cannot express; and a seat that is itself a
python-copier-template image lost its own ``/app/.venv``. Beside dissolves both,
and lets the seed be a plain copy out of the container that is already running.

**The relaunch does not restart the container, and that is the point.** An
ephemeral container shares the target container's namespaces, so a restart does
not orphan the developer's seat - it SIGKILLs it, ``exitCode: 137``
(`#161 <https://github.com/gilesknap/podbench/issues/161>`_). So the values
carry a supervisor, and ``apply`` holds it, kills the child's *tree*, and
releases the hold: PID 1 never dies, the kubelet never sees a restart, and
service is back in ~6.8s against a CrashLoopBackOff ladder of 15s, 23s, 45s
(spike S7).

Two things the claim does *not* fix, and which this module exists to make
visible rather than leave to be discovered:

1. **The claim's project shadows the image's.** An image upgrade under a live
   hotfix keeps running the claim's code, so the upgrade does not reach what is
   executing. The manifest records the digest it was made against and ``status``
   compares it, which is what turns a silent shadow into ``image-changed``.
2. **Single replica only.** The claim is ReadWriteOnce, so a second replica
   either cannot schedule or, with RWX, races on the same checkout. ``init`` and
   ``apply`` refuse a multi-replica target rather than partially hotfix a
   Deployment.

What used to be a third - an interpreter bump breaking the venv, because
``bin/python`` pointed into the image - is fixed rather than reported. The
interpreter now lives on the claim at
:data:`~podbench.model.HOTFIX_INTERPRETER_PATH` and the venv is *rebuilt* there
rather than copied, so its console scripts name a path that survives a restart.

Provenance is not decoration: every hotfix is a git commit, and a manifest on
the claim records what it was made against. It lives on the **claim**, not on
the workload's pod template, because a GitOps controller reconciles a volume
towards its spec but strips an annotation as drift - which is how a hotfixed pod
under Argo used to go quiet within one sync interval. ``status`` is the point of
the whole mode: silently-diverged pods are the risk it must never create.
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
from typing import Annotated, Any, NoReturn, Protocol, cast

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
from .launcher import (
    CONTAINER_BASE,
    LauncherError,
    attach,
    kubectl_for,
    resolve_pod_name,
    running_seat,
    runs_hotfix_supervisor,
)
from .model import (
    HOTFIX_APP_PATH,
    HOTFIX_CHILD_PID_PATH,
    HOTFIX_CLAIM_VOLUME,
    HOTFIX_HOLD_PATH,
    HOTFIX_INTERPRETER_PATH,
    SEAT_HOME_VOLUME,
    PodRef,
    and_list,
    as_dict,
)
from .spec import target_uid_gid

__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "MAX_RECORDED_COMMITS",
    "METADATA_FILES",
    "SEAT_HOME_SIZE",
    "InterpreterProbe",
    "LocalStore",
    "ManifestVersionError",
    "MultiReplicaError",
    "HotfixCommit",
    "HotfixError",
    "HotfixHealth",
    "HotfixManifest",
    "Hold",
    "HotfixRow",
    "HotfixStore",
    "HotfixTarget",
    "PodStore",
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
    "seed_source",
    "seeded_python",
    "interpreter_warning",
    "main",
    "claim_container",
    "STATE_SEPARATOR",
    "read_pod_state",
    "require_supervisor",
    "manifest_path",
    "container_entrypoint",
    "pod_gid",
    "FROM_POD_ESCAPE",
    "EXISTING_MOUNTS_WARNING",
    "DEFAULT_GID_PLACEHOLDER",
    "metadata_changed",
    "normalise_interpreter",
    "parse_log",
    "parse_pyvenv_cfg",
    "probe_interpreter",
    "replica_count",
    "resolve_target",
    "seat_container",
    "status_rows",
    "git_argv",
    "hold_loop_args",
    "hotfix_claim",
    "values_snippet",
    "wrapped_liveness_probe",
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


def checkout_path(project: str = HOTFIX_APP_PATH) -> str:
    """Where the source checkout lives on the claim.

    The claim *is* the checkout now. It mounts beside the application, so the
    project sits at the root of the volume exactly as it sits in the image, and
    a rebuilt venv underneath it resolves the same relative paths it would have
    resolved there.

    It used to be ``<venv>/src``, which looked odd and was: the claim was
    mounted *over* the venv path, so the venv directory was the only directory
    on the volume and the checkout had to be buried inside it.

    >>> checkout_path()
    '/podbench/app'
    >>> checkout_path("/podbench/app/")
    '/podbench/app'
    """
    return project.rstrip("/")


def seed_source(container_root: str = "/proc/1/root") -> tuple[str, str]:
    """The two paths a seed copies out of the *running* application container.

    The project and the interpreter, and the interpreter is the half that looks
    redundant. A rebuilt venv's console scripts carry an absolute shebang, so an
    interpreter left behind in the image is one that a restart takes away -
    ``head -1 <venv>/bin/<script>`` would point back into ``/app`` and the fix
    would vanish on the next restart without a word.

    Read through PID 1's root rather than from the seat's own filesystem: the
    seat is a *different image* - a python-copier-template one with its own
    ``/app/.venv`` - so copying its copy would seed the claim with podbench's
    dependencies and none of the application's.

    >>> seed_source()
    ('/proc/1/root/app', '/proc/1/root/python')
    """
    root = container_root.rstrip("/")
    return f"{root}/app", f"{root}/python"


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
        git_argv(checkout, "log", _LOG_FORMAT, f"{base_commit}..HEAD"),
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
            git_argv(checkout, "diff", "--name-only", f"{previous}..{head}"),
            check=False,
        )
        if result.returncode == 0:
            return _paths(result.stdout)
    return _paths(
        store.run(
            git_argv(checkout, "show", "--name-only", "--pretty=format:", head),
            check=False,
        ).stdout
    )


def _paths(text: str) -> tuple[str, ...]:
    """One path per line — ``--name-only`` output, minus git's blank lines.

    Split on lines rather than on whitespace: a path containing a space is one
    path, and ``git show --pretty=format:`` leads with an empty line.
    """
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def install_argv(interpreter: str, checkout: str) -> list[str]:
    """The venv rebuild, run in the *application* container.

    ``uv sync`` rather than a pip editable install, because the claim now holds
    the whole project and ``uv`` is what built it in the image. Building it in
    place is what makes the console scripts' absolute shebangs name the
    interpreter on the *claim*, which is the one that survives a restart.

    ``--python`` pins that interpreter explicitly, and *interpreter* is the
    binary itself rather than the directory the copy landed in: a uv-managed
    install nests one level down, at
    ``<root>/cpython-<version>-<triple>/bin/python3``, so the obvious
    ``<root>/bin/python3`` does not exist. Left to its own devices uv would find
    the image's, and the venv would come out naming a path the next restart
    takes away - a failure that shows up as the fix silently reverting rather
    than as an error.

    ``UV_CACHE_DIR`` is set explicitly, on the claim. Left to itself uv wants
    ``$HOME/.cache/uv`` and falls back to ``/.cache/uv`` when HOME is unset or
    unwritable, which is a permission error partway through the rebuild:

        error: failed to create directory `/.cache/uv`: Permission denied

    Measured on the bench, 2026-08-22. Whether it works is a property of the
    *target's* environment - the p47 pod sets ``HOME=/tmp`` and the bench pod
    does not - so it cannot be left to chance. On the claim, it also survives to
    make the next rebuild cheap.

    >>> install_argv("/x/bin/python3", "/podbench/app")[:4]
    ['env', 'UV_CACHE_DIR=/podbench/app/.uv-cache', 'uv', 'sync']
    """
    return [
        "env",
        f"UV_CACHE_DIR={checkout.rstrip('/')}/.uv-cache",
        "uv",
        "sync",
        "--project",
        checkout,
        "--python",
        interpreter,
        "--frozen",
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


def seat_container(
    kube: Kubectl, pod: str, requested: str | None, *, land: bool = False
) -> str:
    """The podbench container that mounts the same claim.

    Hotfix mode's premise is that this container exists: it is the one with git
    and a shell, and — because it mounts the claim at the identical mountPath —
    the one place where the checkout and the venv resolve to the same paths the
    application sees.

    ``land`` makes that premise this function's job rather than the user's, the
    way ``podbench vscode`` lands a seat sized for an editor instead of telling
    somebody to go and attach one. ``init`` passes it: needing to type ``attach``
    before ``hotfix init`` is ceremony for a step podbench can take itself, and
    it is the same ``attach`` either way - since #177 that attach mounts the
    claim on its own, so the seat it lands is one this mode can use.

    The verbs that follow ``init`` deliberately do **not** pass it. By then the
    seat exists, and a missing one means it died or the pod was replaced -
    which is a thing to be told about, not to paper over by silently spending
    another ephemeral container name.
    """
    if requested is not None:
        return requested
    seat = running_seat(kube.get_pod(pod))
    if seat is not None:
        return seat.name
    if not land:
        raise HotfixError(
            f"no running podbench container in pod {pod}. Hotfix mode reaches the "
            "claim through the seat, which must mount it at the same mountPath "
            f"as the application: run `podbench attach {pod}` first, or "
            f"pass --seat if it is named something other than {CONTAINER_BASE}-N."
        )
    # Said before the attach rather than after it: landing a seat is the slow
    # part of `hotfix init` and a user watching a silent minute deserves to know
    # what podbench decided to do on their behalf.
    emit(
        f"podbench: no seat is running in {pod}, so one is being landed - "
        "hotfix mode reaches the claim through a seat, and `attach` mounts the "
        "claim itself on a pod carrying the layout."
    )
    session = attach(kube, pod)
    # `attach`'s own report is not printed here, but the lines that describe
    # *this* seat's mounts are: the claim is mounted without being asked for,
    # and a mount nobody typed is only acceptable if the output names it.
    for warning in session.warnings:
        emit(f"podbench: {warning}")
    return session.seat.container


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
    """There is a manifest on the claim and it cannot be parsed."""

    NOT_HOTFIXED = "not-hotfixed"
    """No manifest on the claim at all.

    Not a fault, and the reason this is a value rather than an absence: a pod
    reaches this listing by being *held*, and a hold with no fix behind it is a
    real and reportable state - the supervisor is relaunching without backoff
    and the liveness probe is short-circuited. Calling it "unreadable" would
    blame a manifest that was never written.
    """

    @property
    def summary(self) -> str:
        """One line for the status table."""
        return {
            HotfixHealth.ACTIVE: "hotfixed, base image unchanged",
            HotfixHealth.INTERPRETER_MISMATCH: "venv interpreter no longer matches",
            HotfixHealth.SUPERSEDED: "consolidated; claim is probably stale",
            HotfixHealth.IMAGE_CHANGED: "image upgraded under the hotfix mount",
            HotfixHealth.UNREADABLE: "manifest on the claim is unreadable",
            HotfixHealth.NOT_HOTFIXED: "held, but nothing hotfixed here",
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
    unreadable: bool = False,
) -> tuple[HotfixHealth, str]:
    """Judge one hotfixed pod. Pure, because this is the whole feature.

    The order is deliberate: a broken interpreter beats everything, because the
    pod is not running what anyone thinks it is; a consolidated hotfix on a
    changed image beats a merely-changed image, because that one has a known
    remedy (retire the claim).
    """
    if manifest is None:
        if unreadable:
            return (
                HotfixHealth.UNREADABLE,
                "there is a manifest on the claim and it cannot be parsed; "
                "provenance for whatever is running has been lost",
            )
        return (
            HotfixHealth.NOT_HOTFIXED,
            "this pod is held but carries no hotfix. Its supervisor is "
            "relaunching without backoff and its liveness probe is "
            "short-circuited, so release the hold or let its deadline pass",
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
            "the claim and turn hotfixProject off.",
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
class Hold:
    """A pod told to relaunch its child rather than exit with its status.

    Orthogonal to :class:`HotfixHealth` on purpose (decision 6). A hold is a
    fact about the *pod's supervisor*, not about the fix: a perfectly healthy
    hotfix can sit in a pod nobody released, and a pod that was never hotfixed
    at all can be held by an `apply` that died mid-flight. Folding it into
    health would make one of those two invisible.
    """

    deadline: int | None
    """The epoch second the hold stops being honoured, or ``None`` if unreadable.

    ``None`` is *unmeasured*, never "no deadline": a hold file podbench cannot
    parse is one written by something else, or truncated, and either way the
    honest report is that the expiry is unknown.
    """

    now: int
    """When the hold was read, so :attr:`expired` does not consult a clock."""

    @property
    def expired(self) -> bool:
        """Whether the supervisor should already have stopped honouring it.

        >>> Hold(deadline=100, now=101).expired
        True
        >>> Hold(deadline=100, now=99).expired
        False
        >>> Hold(deadline=None, now=99).expired
        False
        """
        return self.deadline is not None and self.now > self.deadline

    def summary(self) -> str:
        """One column's worth, for the row line.

        >>> Hold(deadline=160, now=100).summary()
        'held 60s left'
        >>> Hold(deadline=100, now=160).summary()
        'held EXPIRED'
        >>> Hold(deadline=None, now=100).summary()
        'held expiry unmeasured'
        """
        if self.deadline is None:
            return "held expiry unmeasured"
        if self.expired:
            return "held EXPIRED"
        return f"held {self.deadline - self.now}s left"


def claim_container(pod_json: Mapping[str, Any]) -> str | None:
    """The container mounting a hotfix claim, or ``None`` if no container does.

    This is the listing's whole filter. A volume is something a GitOps
    controller reconciles *towards*, not something it strips, which is exactly
    what an annotation on the pod template was not.

    >>> claim_container({"spec": {"containers": [
    ...     {"name": "app", "volumeMounts": [{"mountPath": "/podbench/app"}]}]}})
    'app'
    >>> claim_container({"spec": {"containers": [{"name": "app"}]}}) is None
    True
    """
    spec = as_dict(pod_json.get("spec"))
    for entry in _as_list(spec.get("containers")):
        container = as_dict(entry)
        for mount in _as_list(container.get("volumeMounts")):
            if _as_str(as_dict(mount).get("mountPath")) == HOTFIX_APP_PATH:
                return _as_str(container.get("name"))
    return None


def read_pod_state(
    kube: Kubectl, pod: str, container: str
) -> tuple[HotfixManifest | None, Hold | None, bool]:
    """The manifest on the claim and the hold in the container, in one exec.

    One exec and not two, because ``status`` is meant to be run habitually and
    two round trips per pod is how a command becomes one nobody runs. The two
    files are printed with a separator rather than parsed apart in the shell,
    so a missing one is an empty section and not an error.
    """
    script = (
        f"cat {manifest_path(HOTFIX_APP_PATH)} 2>/dev/null; "
        f'echo "{STATE_SEPARATOR}"; '
        f"cat {HOTFIX_HOLD_PATH} 2>/dev/null; "
        f'echo "{STATE_SEPARATOR}"; date -u +%s'
    )
    result = kube.exec_(pod, ["sh", "-c", script], container=container, check=False)
    if result.returncode != 0:
        return None, None, False
    parts = result.stdout.split(STATE_SEPARATOR)
    if len(parts) < 3:
        return None, None, False
    manifest: HotfixManifest | None = None
    unreadable = False
    if parts[0].strip():
        try:
            manifest = HotfixManifest.from_json(parts[0].strip())
        except (HotfixError, ValueError):
            # A manifest that is present and will not parse is a different
            # answer from no manifest: one has lost its provenance, the other
            # never had any.
            unreadable = True
    hold: Hold | None = None
    if parts[1].strip():
        now = int(parts[2].strip() or 0)
        try:
            deadline = int(parts[1].strip())
        except ValueError:
            deadline = None
        hold = Hold(deadline=deadline, now=now)
    return manifest, hold, unreadable


STATE_SEPARATOR = "--podbench--"
"""Separates the manifest, the hold and the clock in one exec's output.

Deliberately not something a manifest or a deadline could contain.
"""


@dataclass(frozen=True)
class HotfixRow:
    """One hotfixed pod, as ``status`` reports it."""

    pod: PodRef
    manifest: HotfixManifest | None
    health: HotfixHealth
    detail: str
    current_image: str = ""
    notes: tuple[str, ...] = ()
    hold: Hold | None = None
    """The pod's hold, if it has one. ``None`` is *not held*, and is orthogonal
    to :attr:`health` - see :class:`Hold`."""

    @property
    def ok(self) -> bool:
        """Whether this row needs no attention, hold included.

        The hold moves the exit code (decision 6). A held pod is not a healthy
        one however healthy its fix: its liveness probe is short-circuited and
        its supervisor has no backoff, so "no unretired hotfixes" must not come
        back true while one is still held.

        >>> row = HotfixRow(PodRef("d", "p"), None, HotfixHealth.ACTIVE, "")
        >>> row.ok
        True
        >>> replace(row, hold=Hold(deadline=None, now=0)).ok
        False
        """
        return self.health.ok and self.hold is None


def status_rows(
    kube: Kubectl, *, probe: bool = True, python: str = DEFAULT_PYTHON
) -> list[HotfixRow]:
    """Every pod carrying a hotfix claim, with its drift, its risks and its hold.

    Keyed on the **claim**, not on an annotation. Provenance used to ride on the
    pod template, where Argo self-heal strips it: a hotfixed pod under a GitOps
    controller went quiet within one sync interval, which is the precise failure
    this command exists to prevent. What cannot be stripped is the volume, so
    that is what the listing looks for.

    The cost is an exec per *candidate* pod rather than one ``get pods`` for the
    namespace. It is affordable because the claim is the filter: only pods that
    actually mount one are exec'd, and a namespace has a handful at most. The
    interpreter is still only probed when the image has moved, which is the one
    case where it can have broken.

    A held pod is listed **whether or not it was ever hotfixed**. A hold with no
    hotfix behind it is not a curiosity - it is a pod whose liveness probe is
    short-circuited and whose supervisor is spinning without backoff, and the
    only thing that will notice is this command.
    """
    result = kube.run("get", "pods", "-o", "json")
    rows: list[HotfixRow] = []
    for item in _as_list(_load_json(result.stdout).get("items")):
        pod_json = as_dict(item)
        metadata = as_dict(pod_json.get("metadata"))
        name = _as_str(metadata.get("name"))
        if name is None:
            continue
        container = claim_container(pod_json)
        if container is None:
            continue
        pod = PodRef(kube.namespace, name)
        notes: list[str] = []
        manifest, hold, unreadable = read_pod_state(kube, name, container)
        if manifest is None and hold is None and not unreadable:
            continue
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
        health, detail = assess(
            manifest, current_digest=digest, probe=measured, unreadable=unreadable
        )
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
                hold=hold,
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
        flag = "[ok]" if row.ok else "[!]"
        # A column, not a clause in the health sentence: a held pod and an
        # unhealthy fix are different questions and either can be true alone.
        held = f"  {row.hold.summary()}" if row.hold is not None else ""
        lines.append(
            f"  {flag}".ljust(_FLAG) + f"{row.pod}  +{ahead} commit(s)  {commit}  "
            f"{row.health.value} — {row.health.summary}{held}"
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


def seeded_python(store: HotfixStore) -> str:
    """The interpreter binary on the claim, discovered rather than assumed.

    A uv-managed install nests one level down - ``cpython-<version>-<triple>`` -
    so the path the copy created is a *directory of* interpreters, and the
    obvious ``<root>/bin/python3`` does not exist. Measured on the beamline:
    ``/podbench/app/.python/cpython-3.11.13-linux-x86_64-gnu/bin/python3.11``.

    Raises rather than guessing: a wrong ``--python`` produces a venv naming an
    interpreter that the next restart takes away, which surfaces as the fix
    reverting and not as an error.
    """
    listing = store.run(
        [
            "bash",
            "-c",
            f"ls -d {HOTFIX_INTERPRETER_PATH}/*/bin/python3 2>/dev/null | head -1",
        ],
        check=False,
    )
    found = listing.stdout.strip()
    if not found:
        raise HotfixError(
            f"no interpreter under {HOTFIX_INTERPRETER_PATH}: the seed copied "
            "the project but not a usable python. The claim's interpreter is "
            "what the rebuilt venv's shebangs name, so continuing would build a "
            "venv that stops working at the next restart."
        )
    return found


def git_argv(checkout: str, *args: str) -> list[str]:
    """A git invocation against the checkout, with the ownership check disabled.

    ``-c safe.directory=`` and not a global config, because the seat is
    ephemeral: a hotfix started in one seat is committed from whichever seat is
    alive later, and a setting written into the first one's HOME is gone by
    then.

    It is needed at all because the seed is a copy. ``cp -a`` preserves the
    image's ownership, and the claim is read back as a uid that does not own it,
    so git refuses the repository outright:

        fatal: detected dubious ownership in repository at '/podbench/app'

    Measured on the beamline, 2026-08-22. Without this every ``apply`` fails at
    the commit - after the edit has been made, which is the worst moment.

    >>> git_argv("/podbench/app", "status", "--porcelain")[:4]
    ['git', '-c', 'safe.directory=/podbench/app', '-C']
    """
    return ["git", "-c", f"safe.directory={checkout}", "-C", checkout, *args]


def require_supervisor(kube: Kubectl, target: HotfixTarget) -> None:
    """Refuse a target whose container is not running the hold loop.

    Checked at *runtime* against the live container rather than by reading the
    pod spec, because the spec can carry the supervisor while the container
    runs something else - an image whose ENTRYPOINT wins, a chart that emits
    ``command`` somewhere the values did not reach, a pod that predates the
    values being applied.

    It is a refusal and not a warning because the fallback is destructive: with
    no supervisor there is nothing to relaunch, so ``apply`` would kill the
    application and the kubelet would restart the container - taking the
    developer's seat with it (issue #161). Better to refuse at ``init``, before
    anybody has edited anything, than at ``apply``, after they have.
    """
    probe = kube.exec_(
        target.pod.name,
        ["test", "-e", HOTFIX_CHILD_PID_PATH],
        container=target.container,
        check=False,
    )
    if probe.returncode != 0:
        raise HotfixError(
            f"container {target.container} is not running the podbench "
            f"supervisor: {HOTFIX_CHILD_PID_PATH} does not exist. Hotfix mode "
            "relaunches the application in place, which needs the supervisor "
            "from `podbench hotfix --print-values` to be this container's "
            "command. Without it there is nothing to relaunch, and applying a "
            "fix would restart the container and kill your seat with it."
        )


def _seeded_interpreter(store: HotfixStore, checkout: str) -> str:
    """The interpreter version the seeded venv reports, or ``""``.

    Read from the claim rather than from the image, because after the rebuild
    the claim's is the one the console scripts name.
    """
    config = store.read_text(f"{checkout}/.venv/pyvenv.cfg")
    if config is None:
        return ""
    settings = parse_pyvenv_cfg(config)
    return normalise_interpreter(
        settings.get("version") or settings.get("version_info") or ""
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
        git_argv(checkout, "config", "--get", "user.email"), check=False
    )
    email = configured.stdout.strip()
    named = store.run(git_argv(checkout, "config", "--get", "user.name"), check=False)
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
    """Prepare a claim: seed it from the running container, then record the base.

    The seed is *performed* here, which the previous design could not do. It
    mounted the claim over the venv path, so the image's own venv sat behind the
    mount in every container and the only moment it could be copied was before
    the application started — an initContainer's job, and one ``ioc-instance``
    cannot express, because every initContainer there inherits the main
    container's ``volumeMounts`` and so cannot keep a staging path clear.

    Mounting beside the application dissolves that: nothing the image ships is
    hidden, so the copy can happen now, from the container that is already
    running, through PID 1's root.

    It is a rebuild rather than a copy of the venv. A venv's absolute paths lie
    the moment it moves, so ``uv sync`` is run afterwards in the *application*
    container — the interpreter on the claim is the one the scripts must name.
    """
    actions: list[str] = []
    require_supervisor(kube, target)
    project, interpreter_src = seed_source()
    checkout = checkout_path()

    if not store.exists(checkout):
        raise HotfixError(
            f"{checkout} is not present: the claim is not mounted. Run "
            "`podbench hotfix --print-values` and deploy the five values it "
            "emits."
        )

    if store.exists(f"{checkout}/pyproject.toml"):
        actions.append(f"claim already seeded at {checkout}")
    else:
        if not store.exists(project):
            raise HotfixError(
                f"{project} is not readable, so the seed cannot run. That path "
                "is the application container's own filesystem seen through PID "
                "1, and reaching it needs the ptrace rung — a seat that landed "
                "without CAP_SYS_PTRACE, or in a namespace whose policy denies "
                "it, cannot see the target's root at all. `podbench doctor` "
                "names the mechanism. This is not something to work around by "
                "copying the seat's own /app: the seat is a different image and "
                "its venv is podbench's, not the application's."
            )
        # The *entries*, never the mount root. `cp -a` onto the claim's own
        # directory fails with "preserving times for '.': Operation not
        # permitted": the mount root is 0777 with an owner the NFS id map does
        # not resolve, so utimes on it is refused however writable it is. The
        # contents copy fine; only the final stat on the destination fails, so
        # this reads as an inexplicable failure at the very end of a 50s copy.
        store.run(["bash", "-c", f"shopt -s dotglob; cp -a {project}/* {checkout}/"])
        actions.append(f"seeded {checkout} from {project}")
        store.run(["cp", "-a", interpreter_src, HOTFIX_INTERPRETER_PATH])
        actions.append(f"copied the interpreter to {HOTFIX_INTERPRETER_PATH}")

    interpreter = _seeded_interpreter(store, checkout)
    actions.append(f"claim seeded, venv interpreter {interpreter or 'unknown'}")

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
        base_commit or store.run(git_argv(checkout, "rev-parse", "HEAD")).stdout.strip()
    )

    if install:
        actions.append(_install(kube, target, seeded_python(store), checkout))

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
    return manifest, actions


def _install(
    kube: Kubectl, target: HotfixTarget, interpreter: str, checkout: str
) -> str:
    """Rebuild the venv on the claim, in the *application* container.

    A rebuild and not a copy. A venv records absolute paths, so one copied from
    ``/app/.venv`` to the claim names an interpreter and a site-packages that
    are no longer where it says - and the failure is a console script whose
    shebang points back into the image, which keeps working until the restart
    that was the whole point.

    It runs in the application container because that is where the interpreter
    the scripts must name lives, and where the application's own index
    configuration is.
    """
    argv = install_argv(interpreter, checkout)
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
            f"rebuilding the venv failed in container {target.container}: "
            f"{result.stderr.strip() or result.stdout.strip()}. It has to run "
            "there and not in the seat, whose interpreter is a different "
            "image's. If the pod has no egress to an index, pre-build the venv "
            "on the claim and re-run with --no-install."
        )
    return f"rebuilt the venv at {checkout}/.venv"


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

    dirty = store.run(git_argv(checkout, "status", "--porcelain")).stdout.strip()
    if not dirty:
        actions.append("working tree clean; nothing new to commit")
    else:
        store.run(git_argv(checkout, "add", "-A"))
        name, email = _identity(store, checkout, author)
        store.run(
            git_argv(
                checkout,
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={email}",
                "commit",
                "-m",
                message,
            )
        )
        actions.append(f"committed as {name} <{email}>")

    head = store.run(git_argv(checkout, "rev-parse", "HEAD")).stdout.strip()
    commits = drift_commits(store, checkout, manifest.base_commit)

    # From what the manifest last recorded to HEAD, not from what this apply
    # happened to commit: a hand commit made in the seat moves HEAD with the
    # tree clean, and the question "is the .dist-info still valid?" is about the
    # commits either way.
    changed = changed_paths(store, checkout, manifest.commit, head)
    if metadata_changed(changed):
        actions.append(_install(kube, target, seeded_python(store), checkout))
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

    if bounce:
        actions.append(_relaunch(kube, target))
    else:
        actions.append("not relaunched; the running process still has the old code")
    return manifest, actions


HOLD_SECONDS = 120.0
"""How long a hold may stand before the supervisor stops honouring it.

Decision 4. The supervisor has no backoff, so a hold left behind over a child
that cannot start is an unbounded spin - and worse, a pod whose liveness probe
is short-circuited indefinitely. The deadline is absolute rather than a
countdown so that a seat which dies mid-apply cannot leave the pod held.
"""

HOLD_AND_RELAUNCH = """\
set -u
test -e {pid_file} || {{ echo "no {pid_file}: this container is not running the \
podbench supervisor, so there is nothing to relaunch" >&2; exit 1; }}
date -u -d "+{deadline} seconds" +%s > {hold}
trap 'rm -f {hold}' EXIT
child=$(cat {pid_file})
python - "$child" <<'PY'
import glob, os, re, signal, sys, time
root = int(sys.argv[1])
kids = {{}}
for d in glob.glob('/proc/[0-9]*'):
    try:
        pid = int(d.rsplit('/', 1)[1])
        ppid = int(re.search(r'PPid:\\s+(\\d+)', open(d + '/status').read()).group(1))
    except Exception:
        continue
    kids.setdefault(ppid, []).append(pid)
seen, stack = [], [root]
while stack:
    p = stack.pop()
    if p in seen:
        continue
    seen.append(p)
    stack.extend(kids.get(p, []))
for pid in reversed(seen):
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
deadline = time.time() + 5
while time.time() < deadline and any(os.path.exists('/proc/%d' % p) for p in seen):
    time.sleep(0.02)
for pid in seen:
    if os.path.exists('/proc/%d' % pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
PY
for _ in $(seq 1 300); do
  [ "$(cat {pid_file})" != "$child" ] && break
  sleep 0.1
done
"""
"""Hold, tree-kill, release - as one script, because it must be one exec.

One exec and not three: a seat that dies between them would leave the pod held,
which is a pod whose liveness probe is short-circuited and whose supervisor is
spinning without backoff. ``trap ... EXIT`` removes the hold on every path
including the failure ones, and the deadline in the file is the belt to that
brace.

The refusal when the pid file is absent is what stops this being run against a
container that never got the supervisor: there the kill would take the
application down with no relaunch behind it, and the kubelet would restart the
container - taking the seat with it, which is precisely what the mode exists to
avoid.
"""


def _relaunch(
    kube: Kubectl, target: HotfixTarget, *, deadline: float = HOLD_SECONDS
) -> str:
    """Relaunch the application without restarting its container.

    Hold, kill, release. The hold makes the supervisor relaunch the child rather
    than exit with its status, so PID 1 never dies and the kubelet never sees a
    restart - which is the whole point: an ephemeral container shares the target
    container's namespaces, so a restart does not orphan the developer's seat,
    it SIGKILLs it (issue #161, exitCode 137).

    Measured on the beamline: ~6.8s to full service, against a kubelet
    CrashLoopBackOff ladder of 15s, 23s, 45s. See spike S7.

    The kill is a **tree** kill, not a signal to the recorded pid. A target that
    allocates a pty puts its real process in its own session, where it survives
    a signal aimed at the pid or its process group and is reparented onto PID 1
    still holding the port - so the relaunch comes up deaf and the pod goes on
    serving the old code with restartCount still 0. Nothing about that is
    visible without collecting the tree first.

    The hold carries an absolute deadline because the supervisor has no backoff
    of its own: held with a child that cannot start, it relaunches as fast as
    the child can exit. The hold replaces the kubelet's ladder with none at all,
    so it must not be left behind.
    """
    script = HOLD_AND_RELAUNCH.format(
        hold=HOTFIX_HOLD_PATH,
        pid_file=HOTFIX_CHILD_PID_PATH,
        deadline=int(deadline),
    )
    result = kube.exec_(
        target.pod.name,
        ["bash", "-c", script],
        container=target.container,
        check=False,
        timeout=POD_WORK_TIMEOUT,
    )
    if result.returncode != 0:
        raise HotfixError(
            f"the relaunch failed in container {target.container}: "
            f"{result.stderr.strip() or result.stdout.strip()}. The hold file is "
            f"removed on every path, so the pod is fail-fast again either way - "
            f"check {HOTFIX_CHILD_PID_PATH} exists, which is what tells you the "
            "supervisor is the one from `--print-values` and not the image's own "
            "entrypoint."
        )
    return f"relaunched the application in {target.container} without a restart"


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
        store.run(git_argv(checkout, "push", remote, f"HEAD:refs/heads/{branch}"))
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
            "  5. set hotfixProject.enabled=false and delete the claim",
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


def hotfix_claim(app: str) -> str:
    """The claim ``hotfixProject`` emits for one application.

    Derived rather than configured, so the chart and this snippet cannot drift -
    ``templates/pvc-hotfix-project.yaml`` builds the same name from the same
    field.

    Not ``<app>-venv``: the claim stopped being a venv when the layout moved
    beside the application. It now carries the project - source, ``.venv`` and
    interpreter - and a name that says venv is a name that sends the next reader
    looking for a mount over one.

    >>> hotfix_claim("api")
    'api-podbench-project'
    """
    return f"{app}-podbench-project"


def hold_loop_args(entrypoint: str) -> str:
    """The supervisor, as one ``args`` string, wrapping *entrypoint*.

    Three things it does, and each was forced by a measurement rather than
    chosen:

    The **runtime switch** prefers the claim only when the claim is seeded, so
    the same values deploy safely with an empty claim - Phase 0 proved the
    unseeded case runs image code and says so.

    It sits *inside* the loop, and that is not tidiness. Evaluated once at
    container start it can never see a claim seeded afterwards, so the first
    ``apply`` after an ``init`` relaunches the **image's** code and reports
    success - measured on 2026-08-22: restartCount 0, new pids, and the IOC
    still ``/app/.venv/bin/fastcs-example``. A mode whose whole promise is
    "relaunch without restarting the container" would have needed a container
    restart to take effect even once.

    The **hold** decides relaunch versus exit. With no hold file the supervisor
    exits with the child's status and the kubelet restarts as it always has, so
    a deployment carrying this is fail-fast until somebody deliberately holds
    the pod.

    The **reap** is the part that looks redundant and is not. A target that
    allocates a pty - ``stdio-socket --ptty``, i.e. every epics-containers IOC -
    puts its real process in its own session, so it survives a signal aimed at
    the recorded pid *or its process group* and is reparented onto PID 1 still
    holding the port. Measured on 2026-08-22: ten relaunches, ``restartCount``
    0, and the pod still served by the process that started with the container.
    See spike S7.

    >>> print(hold_loop_args("myapp serve"))  # doctest: +ELLIPSIS
    while :; do...
    """
    return "\n".join(
        [
            "while :; do",
            "  (",
            f"    if [ -x {HOTFIX_APP_PATH}/.venv/bin/python ]; then",
            f'      export PATH="{HOTFIX_APP_PATH}/.venv/bin:$PATH"',
            '      echo "podbench: running the hotfixed project"',
            "    fi",
            f"    exec {entrypoint}",
            "  ) &",
            "  child=$!",
            f"  echo $child > {HOTFIX_CHILD_PID_PATH}",
            "  wait $child; rc=$?",
            '  kill -TERM -"$child" 2>/dev/null || true',
            f"  [ -e {HOTFIX_HOLD_PATH} ] || exit $rc",
            "done",
        ]
    )


def wrapped_liveness_probe(original: Sequence[str]) -> str:
    """*original* exec probe, short-circuited while the pod is held.

    A held pod has a deliberately dead or restarting child, so its own health
    check fails and an unwrapped probe restarts the container out from under
    the hold. Measured on 2026-08-22: wrapped, a held pod with its service down
    survived 344s with no restart and no ``Unhealthy`` event; unwrapped, the
    same pod restarted at 122s. 7 of 18 containers in a real beamline namespace
    declare a probe, and the canonical hotfix target declares none - so this
    cannot be omitted on the grounds that the target in front of you has no
    probe.
    """
    inner = " ".join(original)
    return f"if [ -e {HOTFIX_HOLD_PATH} ]; then exit 0; fi; exec {inner}"


#: A probe's scalar fields, in the order Kubernetes documents them.
#:
#: A chart that accepts a whole ``livenessProbe`` renders it *wholesale*, so any
#: of these the target declared and the snippet omits silently reverts to the
#: Kubernetes default - and every default is in the direction that restarts
#: things sooner. ``ioc-instance`` is the measured case: its 120s/30s live on a
#: ``livenessExecutable`` branch that supplying ``livenessProbe`` takes instead
#: of, so a wrapped probe carrying only ``exec`` moved a compiled IOC from
#: 120s/30s to 0s/10s - probed from the moment it started, before it had reached
#: its hardware. Found on p47-beamline, 2026-08-22.
PROBE_SCALAR_FIELDS = (
    "initialDelaySeconds",
    "periodSeconds",
    "timeoutSeconds",
    "successThreshold",
    "failureThreshold",
    "terminationGracePeriodSeconds",
)


def probe_timings(probe: Mapping[str, Any] | None) -> list[str]:
    """*probe*'s scalar fields as yaml lines, in :data:`PROBE_SCALAR_FIELDS` order.

    Only the scalars. The handler is wrapped separately, and the handler is the
    one part of somebody else's probe podbench is entitled to rewrite.

    >>> probe_timings({"exec": {"command": ["x"]}, "periodSeconds": 30})
    ['  periodSeconds: 30']
    >>> probe_timings({"initialDelaySeconds": 120, "periodSeconds": 30})
    ['  initialDelaySeconds: 120', '  periodSeconds: 30']
    >>> probe_timings(None)
    []
    """
    if not probe:
        return []
    return [
        f"  {field}: {probe[field]}"
        for field in PROBE_SCALAR_FIELDS
        if probe.get(field) is not None
    ]


def probe_exec_command(probe: Mapping[str, Any]) -> list[str]:
    """*probe*'s ``exec.command``, or ``[]`` if it does not have one.

    >>> probe_exec_command({"exec": {"command": ["/bin/bash", "live.sh"]}})
    ['/bin/bash', 'live.sh']
    >>> probe_exec_command({"httpGet": {"path": "/healthz"}})
    []
    """
    handler = probe.get("exec")
    if not isinstance(handler, Mapping):
        return []
    command = cast(Mapping[str, Any], handler).get("command")
    return [str(part) for part in _as_list(command)]


DEFAULT_GID_PLACEHOLDER = "<the application's runAsGroup>"
"""What the fsGroup stays when nothing could say what it should be.

Deliberately not a number. A snippet pasted without reading it fails at
``helm install`` rather than deploying an fsGroup the application does not
run as - which fails later, in the dark, as a claim that is present and
unwritable. Named here because ``--from-pod`` has to recognise "the user did
not pass --gid" and the sentinel is the only signal there is."""


FROM_POD_ESCAPE = (
    "`--print-values` reads the target by default so the emitted values need no "
    "hand-editing. To emit them without a cluster, pass `--no-from-pod` and "
    "supply `--entrypoint`, `--gid` and `--liveness-probe` yourself.\n"
    "\n"
    "Supplying them by hand is how #176 happened: a chart renders a supplied "
    "`livenessProbe` wholesale, so a timing you leave out becomes the Kubernetes "
    "default and the target is probed sooner and more often than it was before."
)
"""The way out of every failure reading the pod, and what taking it costs.

Making a cluster read the default means every ``kubectl`` failure now lands on a
user who did not ask for one, so a bare relay of kubectl's stderr is not enough:
the message has to name the flag that gets the job done anyway. It also has to
say what that flag costs, because the escape hatch is the exact route that
produced #176 - the consequence *is* the point, and a generic "try
``--no-from-pod``" would hand somebody the footgun without the warning.
"""


def container_entrypoint(container: Mapping[str, Any]) -> str:
    """The command this container runs today, as the supervisor will wrap it.

    ``command`` and ``args`` are Kubernetes' override of the image's ENTRYPOINT
    and CMD, concatenated in that order and shell-quoted back into the one
    string :func:`hold_loop_args` execs. Quoting matters: an argument containing
    a space that is joined naively becomes two arguments the moment the
    supervisor's ``exec`` re-splits it.

    A container already carrying the layout is **unwrapped** rather than wrapped
    twice. ``--from-pod`` against a pod that is already hotfixed is a normal
    thing to do - re-emitting the values after a chart bump, or checking what is
    deployed - and a supervisor nested inside a supervisor would hold on a file
    the inner one never sees.

    >>> container_entrypoint({"command": ["python", "-m", "app"]})
    'python -m app'
    >>> container_entrypoint({"command": ["sh", "-c"], "args": ["a b"]})
    "sh -c 'a b'"
    >>> container_entrypoint({"args": [hold_loop_args("myapp serve")]})
    'myapp serve'
    """
    if runs_hotfix_supervisor(container):
        for line in _supervised_lines(container):
            if (stripped := line.strip()).startswith("exec "):
                return stripped[len("exec ") :].strip()
    words = [
        word
        for key in ("command", "args")
        for word in _as_list(container.get(key))
        if isinstance(word, str)
    ]
    if not words:
        raise HotfixError(
            "the target declares neither `command` nor `args`, so the command it "
            "runs lives in the image's ENTRYPOINT and is not in the pod spec at "
            "all - nothing podbench can read from the cluster will find it. Pass "
            "`--entrypoint CMD` (the rest is still read from the pod), or "
            f"`--no-from-pod` and supply all three.\n\n{FROM_POD_ESCAPE}"
        )
    return shlex.join(words)


def _supervised_lines(container: Mapping[str, Any]) -> list[str]:
    return "\n".join(
        word for word in _as_list(container.get("args")) if isinstance(word, str)
    ).splitlines()


def pod_gid(pod_json: Mapping[str, Any], container: str) -> str | None:
    """The gid the application runs as, or ``None`` where it does not say.

    ``None`` is not zero and must not become it. A hardened workload routinely
    states ``runAsUser`` and no ``runAsGroup`` (:func:`podbench.spec.target_uid_gid`
    says so at length), and an fsGroup of 0 on a claim the application cannot
    write is the failure this whole mode exists downstream of - present and
    unwritable, which starts and then fails. Unknown keeps the placeholder, so
    the snippet still refuses at ``helm install``.
    """
    _, gid = target_uid_gid(pod_json, container)
    return None if gid is None else str(gid)


def values_snippet(
    app: str,
    entrypoint: str,
    *,
    size: str = "2Gi",
    gid: str = DEFAULT_GID_PLACEHOLDER,
    home_size: str = SEAT_HOME_SIZE,
    liveness_exec: Sequence[str] | None = None,
    liveness_probe: Mapping[str, Any] | None = None,
    indent: int = 0,
    volumes_key: str = "volumes",
    mounts_key: str = "volumeMounts",
    command_key: str = "command",
    args_key: str = "args",
    probe_key: str = "livenessProbe",
    security_key: str = "podSecurityContext",
) -> str:
    """The values an application's chart needs, ready to paste.

    Five keys, and every one of them is an ordinary passthrough that charts
    already have - which is the practical payoff of mounting *beside* the
    application rather than over it. The old snippet needed an initContainer at
    a staging path, and ``ioc-instance`` cannot express one: every initContainer
    there inherits ``volumeMounts`` from the main container, so the staging path
    could not be kept clear.

    Nothing is emitted for seat identity. That was always for a seat which is an
    ordinary container, and this mode's seat attaches to the live pod.

    The key names are parameters because charts differ on what they call their
    passthroughs, and *indent* exists because some nest them: ``ioc-instance``
    wants all five under an ``ioc-instance:`` key. Indenting the block is the
    only correct way to do that - prefixing each key instead leaves a mapping's
    children at their parent's level, which is silently invalid rather than
    obviously so. The shapes underneath the keys are plain Kubernetes and do not
    vary.
    """
    claim = hotfix_claim(app)
    # A whole probe carries its timings; a bare command cannot, and the caller is
    # told so rather than left to find out from a restart ladder.
    exec_command = liveness_exec or (
        probe_exec_command(liveness_probe) if liveness_probe else []
    )
    probe = wrapped_liveness_probe(exec_command) if exec_command else None
    timings = probe_timings(liveness_probe)
    header = [
        f"# 1. values for the podbench release - creates the claim {claim}",
        "hotfixProject:",
        "  enabled: true",
        "  claims:",
        f"    - name: {app}",
        f"      size: {size}",
        "",
        f"# 2. values for {app}'s own chart. All five are ordinary passthroughs;",
        "#    the names below are the common convention, not a requirement.",
    ]
    lines = [
        f"{volumes_key}:",
        f"  - name: {HOTFIX_CLAIM_VOLUME}",
        "    persistentVolumeClaim:",
        f"      claimName: {claim}",
        f"  # {SEAT_HOME_VOLUME} is the seat's, and is deliberately *declared and",
        "  # not mounted* by the application container. An ephemeral container",
        "  # may only mount volumes its pod already declares, and pod volumes",
        "  # cannot be added later - so it is here at deploy time or not at all.",
        f"  - name: {SEAT_HOME_VOLUME}",
        "    emptyDir:",
        "      # vscode-server unpacks to ~700 MiB and a real session reaches",
        "      # 1.1-1.3 GB. Unbounded, that comes out of the node's ephemeral",
        "      # storage and an overrun evicts the pod - application included.",
        f"      sizeLimit: {home_size}",
        f"{mounts_key}:",
        "  # Beside the application's own project, never over it. Nothing the",
        "  # image ships is hidden, so the seed is a plain copy and the seat",
        "  # keeps its own venv.",
        f"  - name: {HOTFIX_CLAIM_VOLUME}",
        f"    mountPath: {HOTFIX_APP_PATH}",
        f"{command_key}:",
        "  - bash",
        "  - -c",
        f"{args_key}:",
        "  - |",
    ]
    lines += [f"    {line}" for line in hold_loop_args(entrypoint).splitlines()]
    if probe is not None:
        lines += [
            f"{probe_key}:",
            "  # The application's own check, short-circuited while the pod is",
            "  # held. Without the wrapper the kubelet restarts a held pod at",
            "  # failureThreshold x periodSeconds and takes the seat with it.",
            "  exec:",
            "    command:",
            "      - bash",
            "      - -c",
            f"      - {probe!r}",
        ]
        # A chart renders a supplied probe wholesale, so a timing that is not
        # here is not "left as it was" - it is the Kubernetes default.
        if timings:
            lines += [
                "  # Carried over from the target's own probe. A chart renders a",
                "  # supplied livenessProbe wholesale, so anything omitted here",
                "  # silently becomes the Kubernetes default.",
                *timings,
            ]
        else:
            lines += [
                "  # WARNING: no timings were supplied, so this probe will use the",
                "  # Kubernetes defaults - initialDelaySeconds 0, periodSeconds 10.",
                "  # If the target declared its own, copy them in here or it will be",
                "  # probed sooner and more often than it was before. Pass the whole",
                "  # probe (--liveness-probe) instead of --liveness to carry them.",
            ]
    lines += [
        f"{security_key}:",
        "  # Not optional. The claim and the seat's home are created root:root,",
        "  # and neither the application nor the seat runs as root. Without",
        f"  # fsGroup {HOTFIX_APP_PATH} is present and unwritable, which is worse",
        "  # than absent - everything starts, then fails.",
        f"  fsGroup: {gid}",
    ]
    pad = " " * indent
    body = "\n".join(f"{pad}{line}" if line else line for line in lines)
    return "\n".join(
        [
            *header,
            body,
            "",
            "# Single replica only: the claim is ReadWriteOnce and one checkout",
            "# cannot serve two writers.",
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
    kube: Kubectl, pod: str, *, seat: str | None, local: bool, land: bool = False
) -> HotfixStore:
    if local:
        return LocalStore()
    return PodStore(
        kube=kube, pod=pod, container=seat_container(kube, pod, seat, land=land)
    )


def _report(actions: Sequence[str]) -> int:
    emit("\n".join(actions))
    return 0


NON_EXEC_PROBE_WARNING = (
    "podbench: {pod}'s {container!r} declares a {kind} livenessProbe, which "
    "cannot be short-circuited by the hold - only an exec probe can, because an "
    "httpGet or tcpSocket probe answers from the application, and the "
    "application is exactly what is down while a pod is held. No livenessProbe "
    "is emitted below, so the chart keeps the one it has and the kubelet will "
    "restart the pod out from under the seat at failureThreshold x "
    "periodSeconds. Deal with that before holding this pod."
)
"""Said when the target's probe is real, readable, and of no use to hold mode.

Not an error, because the values are still correct and still worth emitting -
and not silence either, because the emitted block being *absent* looks identical
to the canonical fastcs case, which genuinely has no probe. The difference is
the whole of whether a held pod survives.
"""


EXISTING_MOUNTS_WARNING = (
    "podbench: {pod}'s {container!r} already mounts {mounts}. The `volumeMounts:` "
    "and `volumes:` keys below *replace* the ones your chart renders, they do not "
    "add to them - so merge these entries into what the service's values.yaml "
    "already has rather than pasting over it. Anything chart-generated will come "
    "back on its own; anything the service declares for itself will not."
)
"""Said when the target carries volumes that are not podbench's.

Found by diffing `--from-pod`'s output against the hand-written values for
`bl47p-mo-ioc-01` on 2026-08-22: the emitted `volumes:` carried podbench's two
and nothing else, so pasting it verbatim would have dropped that IOC's `dev-shm`
and left it without `/dev/shm`.

Podbench cannot merge them itself and must not pretend to: read from a live pod,
a chart-generated volume and one the service declared for itself are
indistinguishable, and emitting the chart's own back to it would duplicate what
the next render produces anyway. Naming them and saying which way the key
resolves is the whole of what can honestly be done here.
"""


def _print_values_failure(message: str) -> NoReturn:
    print(f"podbench: {message}", file=sys.stderr)
    raise typer.Exit(2)


def _read_print_values_from_pod(
    pod: str | None,
    *,
    container: str | None,
    namespace: str | None,
    context: str | None,
    binary: str,
    runner: Runner | None,
    entrypoint: str | None,
    gid: str,
    probe: Mapping[str, Any] | None,
    liveness: str | None,
) -> tuple[str | None, str, Mapping[str, Any] | None]:
    """Fill the three hand-supplied arguments from the target, or say why not.

    This is the whole of ``--from-pod``: a thin cluster-reading wrapper that
    fills :func:`values_snippet`'s existing arguments. The emitter keeps its
    signature and stays testable with no cluster, which is not negotiable - it
    is what lets every shape of the output be asserted in a unit test.

    A flag the user passed always wins over the pod. That is what makes
    ``--entrypoint`` a usable answer to an image-ENTRYPOINT target without
    giving up the gid and the probe as well.

    Every failure here names ``--no-from-pod`` and what it costs
    (:data:`FROM_POD_ESCAPE`). Making the cluster read the default means each
    of these lands on somebody who did not ask for a cluster, and the one thing
    they must not be left to work out for themselves is that the way round it
    is the way #176 happened.
    """
    if pod is None:
        _print_values_failure(
            "--print-values reads the target by default, so it needs to know "
            "which pod: pass `--from-pod POD`.\n"
            "\n" + FROM_POD_ESCAPE
        )
    kube = kubectl_for(namespace, context=context, binary=binary, runner=runner)
    try:
        # `--from-pod` is a pod and only a pod: the entrypoint, the probe and
        # the gid are read off a running container, and a Deployment has none.
        # `LauncherError` is not one of the three `main` folds into exit 2, so
        # letting it out here is a traceback rather than an answer.
        name = resolve_pod_name(pod)
    except LauncherError as error:
        _print_values_failure(f"{error}.\n\n{FROM_POD_ESCAPE}")
    try:
        pod_json = kube.get_pod(name)
    except KubectlError as error:
        # One branch for no kubeconfig, no current context, a pod that does not
        # exist and a forbidden `get pods` alike: kubectl tells those apart only
        # in the text of its own message, so podbench relays that verbatim
        # rather than guessing at a category and getting it wrong.
        _print_values_failure(
            f"could not read pod {name!r} in {kube.namespace!r}: "
            f"{error}.\n\n{FROM_POD_ESCAPE}"
        )
    try:
        target = _application_container(pod_json, container)
    except HotfixError as error:
        _print_values_failure(f"{error}.\n\n{FROM_POD_ESCAPE}")
    spec = next(
        as_dict(entry)
        for entry in _as_list(as_dict(pod_json.get("spec")).get("containers"))
        if _as_str(as_dict(entry).get("name")) == target
    )
    if entrypoint is None:
        try:
            entrypoint = container_entrypoint(spec)
        except HotfixError as error:
            # Already carries the escape: it has an extra way out of its own.
            _print_values_failure(str(error))
    if gid == DEFAULT_GID_PLACEHOLDER:
        gid = pod_gid(pod_json, target) or DEFAULT_GID_PLACEHOLDER
    # `volume` and not `name`: a walrus inside a comprehension binds in the
    # *enclosing* scope, so reusing `name` here silently overwrote the pod's.
    theirs = sorted(
        volume
        for entry in _as_list(spec.get("volumeMounts"))
        if (volume := _as_str(as_dict(entry).get("name"))) is not None
        and volume not in (HOTFIX_CLAIM_VOLUME, SEAT_HOME_VOLUME)
    )
    if theirs:
        print(
            EXISTING_MOUNTS_WARNING.format(
                pod=name, container=target, mounts=and_list(theirs)
            ),
            file=sys.stderr,
        )
    if probe is None and liveness is None:
        declared = as_dict(spec.get("livenessProbe")) or None
        # A target with no livenessProbe is not an error, and must never be
        # reported as one: the canonical fastcs target declares none.
        if declared is not None:
            if probe_exec_command(declared):
                probe = declared
            else:
                kind = next(
                    (k for k in ("httpGet", "tcpSocket", "grpc") if k in declared),
                    "non-exec",
                )
                print(
                    NON_EXEC_PROBE_WARNING.format(
                        pod=name, container=target, kind=kind
                    ),
                    file=sys.stderr,
                )
    return entrypoint, gid, probe


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
        entrypoint: Annotated[
            str | None,
            typer.Option(
                "--entrypoint",
                metavar="CMD",
                help=(
                    "the command the container runs today, which the supervisor "
                    "wraps, for --print-values"
                ),
            ),
        ] = None,
        liveness: Annotated[
            str | None,
            typer.Option(
                "--liveness",
                metavar="CMD",
                help=(
                    "the target's existing exec livenessProbe command, which is "
                    "emitted wrapped to honour the hold; omit if it has none. "
                    "Carries no timings - prefer --liveness-probe"
                ),
            ),
        ] = None,
        liveness_probe: Annotated[
            str | None,
            typer.Option(
                "--liveness-probe",
                metavar="JSON",
                help=(
                    "the target's whole livenessProbe as json, e.g. from "
                    "`kubectl get pod POD -o jsonpath='{.spec.containers[0]"
                    ".livenessProbe}'`. Its exec command is wrapped and its "
                    "timings are carried over; a chart renders a supplied probe "
                    "wholesale, so a timing left out becomes the k8s default"
                ),
            ),
        ] = None,
        size: Annotated[
            str,
            typer.Option(
                "--size", metavar="SIZE", help="claim size, for --print-values"
            ),
        ] = "2Gi",
        # Left as a placeholder when unset, so that a snippet pasted without
        # reading it fails at `helm install` rather than deploying an fsGroup
        # the application does not run as — which fails later, in the dark, as a
        # claim that is present and unwritable.
        gid: Annotated[
            str,
            typer.Option(
                "--gid",
                metavar="GID",
                help="the application container's gid, for --print-values "
                "(default: read from the target)",
            ),
        ] = DEFAULT_GID_PLACEHOLDER,
        from_pod: Annotated[
            str | None,
            typer.Option(
                "--from-pod",
                metavar="POD",
                help="read the entrypoint, livenessProbe and gid off this pod, "
                "so the emitted values need no hand-editing (the default; "
                "--no-from-pod turns it off)",
            ),
        ] = None,
        no_from_pod: Annotated[
            bool,
            typer.Option(
                "--no-from-pod",
                help="do not read any pod: emit from the flags alone, for CI, an "
                "offline machine, or a pod that does not exist yet",
            ),
        ] = False,
        container: _Container = None,
        namespace: _Namespace = None,
        context: _Context = None,
        kubectl: _KubectlBinary = "kubectl",
    ) -> None:
        """Durable in-place fixes: the project on a claim beside the
        application, every change a commit, and a status command that will not
        let a hotfixed pod go unnoticed.
        """
        if print_values:
            if app_name is None:
                print("podbench: --print-values needs --app NAME", file=sys.stderr)
                raise typer.Exit(2)
            parsed_probe: Mapping[str, Any] | None = None
            if liveness_probe is not None:
                try:
                    parsed_probe = _load_json(liveness_probe)
                except json.JSONDecodeError as exc:
                    print(
                        f"podbench: --liveness-probe is not valid json: {exc}",
                        file=sys.stderr,
                    )
                    raise typer.Exit(2) from exc
                if not probe_exec_command(parsed_probe):
                    print(
                        "podbench: --liveness-probe has no exec.command. Only an "
                        "exec probe can be short-circuited by the hold - an "
                        "httpGet or tcpSocket probe answers from the application, "
                        "which is exactly what is down while a pod is held.",
                        file=sys.stderr,
                    )
                    raise typer.Exit(2)
            if not no_from_pod:
                entrypoint, gid, parsed_probe = _read_print_values_from_pod(
                    from_pod,
                    container=container,
                    namespace=namespace,
                    context=context,
                    binary=kubectl,
                    runner=runner,
                    entrypoint=entrypoint,
                    gid=gid,
                    probe=parsed_probe,
                    liveness=liveness,
                )
            if entrypoint is None:
                print(
                    "podbench: --print-values needs --entrypoint CMD when the "
                    "target is not read. Pass --from-pod POD to read it, or "
                    "--entrypoint CMD to state it.",
                    file=sys.stderr,
                )
                raise typer.Exit(2)
            print(
                values_snippet(
                    app_name,
                    entrypoint,
                    size=size,
                    gid=gid,
                    liveness_exec=shlex.split(liveness) if liveness else None,
                    liveness_probe=parsed_probe,
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
        # The one verb that lands its own seat when none is running. See
        # `seat_container`: `init` is where the seat first has to exist, so it
        # is where podbench should be the one to arrange it.
        store = _store_for(
            kube, resolved.pod.name, seat=seat, local=local, land=not local
        )
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
        raise typer.Exit(0 if all(row.ok for row in rows) else 1)

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
    # `LauncherError` is here because `hotfix init` lands its own seat now
    # (#177): the landing goes through `launcher.attach`, which refuses in its
    # own currency - an unknown --mount, a pod with no containers, a target
    # container that is not there. Those are all things the user has to fix, and
    # exit 2 with the sentence is the answer; a traceback is not.
    except (HotfixError, KubectlError, LauncherError, ValueError) as error:
        print(f"podbench: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
