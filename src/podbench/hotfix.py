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

import io
import json
import shlex
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, NoReturn, Protocol, cast

import typer
from rich.text import Text
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .cli import new_app, require_subcommand, run
from .console import WARNING_HANG, WARNING_LEAD, emit, paragraph, rule
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
    application_mount,
    attach,
    declared_volumes,
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
from .oci import REVISION_LABEL, SOURCE_LABEL, parse_reference
from .oci import image_labels as read_image_labels
from .spec import target_uid_gid

__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "MAX_RECORDED_COMMITS",
    "METADATA_FILES",
    "SEAT_HOME_SIZE",
    "CheckStatus",
    "ClaimState",
    "HOTFIX_TARGET_LABEL",
    "InterpreterProbe",
    "LocalStore",
    "PreflightCheck",
    "RetirementCheck",
    "RetirementStep",
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
    "format_preflight",
    "format_status",
    "identity_configmap",
    "init",
    "install_argv",
    "seed_source",
    "seeded_python",
    "interpreter_warning",
    "main",
    "base_commit_from",
    "claim_container",
    "claim_mount_path",
    "resolve_venv",
    "VENV_DISAGREES_WITH_POD",
    "VENV_INVISIBLE_TO_STATUS",
    "NO_SOURCE_REPO",
    "SOURCE_LABEL_UNCORROBORATED",
    "image_name_agrees",
    "CLAIM_NOT_MOUNTED",
    "CLAIM_ALREADY_SEEDED",
    "SEAT_NOT_RUNNING",
    "IMAGE_HAS_NO_PROJECT",
    "IMAGE_HAS_NO_INTERPRETER",
    "NON_EXEC_PROBE_BLOCKS_THE_HOLD",
    "preflight",
    "retire",
    "retirement",
    "retirement_summary",
    "format_retirement",
    "claim_state",
    "hotfix_wiring",
    "pod_fs_group",
    "pod_claim_name",
    "WIRING_IS_THE_APPLICATIONS_OWN",
    "FSGROUP_NOT_ATTRIBUTED",
    "CLAIM_OUTLIVES_THE_FLIP",
    "CLAIM_GONE_BUT_STILL_WIRED",
    "CLAIM_STILL_MOUNTED",
    "CLAIM_MOUNTERS_UNREADABLE",
    "CLAIM_CONTENTS_UNVERIFIED",
    "CLAIM_NOT_LABELLED_FOR_THIS_TARGET",
    "CLAIM_UNMEASURED",
    "STATUS_DOES_NOT_READ_CLAIMS",
    "STATE_SEPARATOR",
    "read_pod_state",
    "require_supervisor",
    "manifest_path",
    "container_entrypoint",
    "pod_gid",
    "FROM_POD_ESCAPE",
    "EXISTING_MOUNTS_WARNING",
    "SUBCHART_VALUES_KEY",
    "merged_values",
    "locate_keys",
    "ABSORBED_FROM_PARENT_NOTE",
    "NO_PARENT_NOTE",
    "MERGED_UNDER_NOTE",
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

MANIFEST_VERSION = 2
"""Schema version written by this podbench.

Read leniently and written strictly: a manifest with a lower version (or none at
all, which is version 0) loads with defaults for what it lacks, because a future
podbench will meet volumes written by this one and by whatever came before it. A
*higher* version is refused outright — guessing at fields we have never seen is
how a fix silently loses its provenance.

Version 2 adds :attr:`HotfixManifest.claim_venv` and
:attr:`HotfixManifest.base_commit_assumed`. Both default safely, so the bump is
not needed to *load* a v1 manifest. It is needed to keep ``apply`` from
laundering one: a v1 manifest cannot say whether its base was measured, and
rewriting it at the current version with the field's default would turn "this
schema could not record it" into "it was measured" — the one direction that
field must never move in. :attr:`~HotfixManifest.stale_schema` is what carries
that, and it is also what makes ``status`` say the provenance may be absent.

The cost, which is real and is the reason the rule exists rather than a bump
being free: a podbench that predates this one refuses a v2 manifest outright.
For ``status`` that is one row, not the listing — :func:`read_pod_state`
catches :class:`ManifestVersionError` along with every other unreadable
manifest and reports the pod as such, so the exit-code contract still holds per
row.
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

CLAIM_VENV_DIR = ".venv"
"""The venv's directory name on the claim, relative to the checkout.

``uv`` creates ``.venv`` and the runtime switch looks for it, so the two have to
agree - which is why this is one constant threaded to both ends rather than a
literal at each. :func:`install_argv` sets ``UV_PROJECT_ENVIRONMENT`` when it is
anything else, because otherwise uv would build ``.venv`` beside the venv the
supervisor is looking for and the pod would quietly run the image's code.

Up here beside the other vocabulary, and not beside :data:`IMAGE_PROJECT_PATH`
where it reads more naturally, because :class:`HotfixManifest` defaults a field
to it and a dataclass default is evaluated when the class is created.
"""

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


# -- output ------------------------------------------------------------------

_FINISHED_INDENT = "  "
"""What marks a line as authored finished rather than as prose to be wrapped.

Any leading whitespace does; two spaces is what this module writes. The marker
is the indent and not a flag on the data because a :class:`HotfixError` carries
one string and nothing else, which is the difference from
:func:`podbench.launcher._editor_step` - that one is handed a note and an
``is_step`` predicate and can keep the two shapes apart without a convention.
"""


def _relay(text: str) -> str:
    """Somebody else's output, marked so :func:`_laid_out` leaves it alone.

    kubectl's and uv's stderr reach the user through a :class:`HotfixError` that
    wraps a sentence round them. :func:`podbench.console.wrap` collapses runs of
    whitespace and breaks on spaces, so reflowing a relayed line is how a paste
    stops matching what the person actually saw - and one of these carries a
    multi-line uv resolution report.

    A stream that said nothing is named rather than relayed as an empty line:
    an indented blank is a blank to :func:`_laid_out`, which would leave the
    sentence's trailing colon promising a quote that is not there - and output
    that looks truncated is read as podbench having lost it.

    >>> _relay("error: no solution found\\n  hint: pin it")
    '  error: no solution found\\n    hint: pin it'
    >>> _relay("")
    '  (no output)'
    """
    lines = text.splitlines() if text.strip() else ["(no output)"]
    return "\n".join(f"{_FINISHED_INDENT}{line}" for line in lines)


def _verbatim(text: str) -> list[Text]:
    """*text* printed as it arrived, with nothing of podbench's laid over it.

    The whole-message counterpart of :func:`_relay`, which marks somebody else's
    output *inside* a sentence podbench wrote. A :class:`KubectlError` is the
    other shape: ``KubectlError._message`` inlines kubectl's stderr behind an
    argv and an exit code, so wrapping the "prose" would break lines through the
    only part of it that matters.

    >>> [line.plain for line in _verbatim("error: forbidden\\n  by RBAC")]
    ['error: forbidden', '  by RBAC']
    """
    return [Text(line) for line in text.split("\n")]


def _laid_out(text: str) -> list[Text]:
    """*text* as the terminal should draw it: prose wrapped, the rest untouched.

    One rule, read off the line itself. A line the caller wrote **flush left**
    is prose and is wrapped to :func:`podbench.console.wrap_width`. A line it
    **indented** was authored finished - a numbered step, a ``do this:``
    offer, a relayed error - and is printed exactly as it arrived.

    Either way the line comes back as a :class:`~rich.text.Text`, so none of
    ``console``'s line rules run over it. That is not caution about the relay
    alone: those rules read a *rendered* line's shape, and both
    :data:`podbench.console._SHOUT` and
    :data:`podbench.console._SECTION_LINE` say in as many words that they are
    safe because prose never reaches them flush left ("the prose is wrapped and
    its continuations are indented"). Here it does, so an ordinary sentence
    opening on a capital had that capital drawn as a section heading - measured
    on :data:`FROM_POD_ESCAPE`'s last paragraph, whose "So" came out with a bold
    cyan ``S`` - and a wrap leaving one word on the last line drew the whole
    word as one. Prose earns no leader, so nothing is lost by saying so.

    Blank lines survive, which is what keeps a multi-paragraph message
    (:data:`FROM_POD_ESCAPE` is appended to most of them) reading as paragraphs.

    >>> for line in _laid_out("one\\n\\n  two  three"):
    ...     print(repr(line.plain))
    'one'
    ''
    '  two  three'
    """
    lines: list[Text] = []
    for line in text.split("\n"):
        if not line.strip():
            lines.append(Text(""))
        elif line[:1].isspace():
            lines.append(Text(line))
        else:
            lines.extend(Text(wrapped) for wrapped in paragraph(line))
    return lines


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

    base_commit_assumed: bool = False
    """Whether :attr:`base_commit` was measured or merely stood in for.

    Measured means the caller stated it, or the image's own
    :data:`~podbench.oci.REVISION_LABEL` supplied it and the clone has that
    commit. Everything else — an image with no labels, a registry that would not
    answer, a revision the repository does not contain — falls back to the
    clone's ``HEAD``, which without ``--ref`` is the default branch's tip and is
    almost never what the image was built from. ``status`` says so rather than
    print ``+N commit(s)`` as though the N were a measurement.
    """

    claim_venv: str = CLAIM_VENV_DIR
    """The venv's *directory name* on the claim — ``--claim-venv``, not
    :attr:`venv`, which is the mountPath.

    Recorded because it is a cross-command contract nothing used to check
    (#209): ``init`` took the flag, the manifest did not carry it, and ``apply``
    had no such flag at all — so a rebuild triggered by a packaging change went
    to ``.venv`` while the supervisor's runtime switch looked somewhere else,
    and the pod quietly went on running the image's code.
    """

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
            "baseCommitAssumed": self.base_commit_assumed,
            "claimVenv": self.claim_venv,
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
        >>> HotfixManifest.from_mapping({"version": 1}).claim_venv
        '.venv'
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
            base_commit_assumed=payload.get("baseCommitAssumed") is True,
            claim_venv=_as_str(payload.get("claimVenv")) or CLAIM_VENV_DIR,
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


IMAGE_PROJECT_PATH = "/app"
"""Where a python-copier-template image keeps the application's project.

The default and nothing more. It is python-copier-template's convention, not a
law: an epics-containers image has no ``/app`` at all - its venv is at ``/venv``
with a separate ``/python`` - which is how #178 came to report a layout
difference as a ptrace denial. A layout that differs is expressible with
``--image-project`` rather than a code change.
"""

IMAGE_INTERPRETER_PATH = "/python"
"""The interpreter beside :data:`IMAGE_PROJECT_PATH`, in the same image."""


def seed_source(
    container_root: str = "/proc/1/root",
    *,
    project: str = IMAGE_PROJECT_PATH,
    interpreter: str = IMAGE_INTERPRETER_PATH,
) -> tuple[str, str]:
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

    Both paths are parameters because the layout is the image's and not
    podbench's, and an image that puts them elsewhere should not need a code
    change. What hotfixing a *compiled* IOC would mean is a separate question
    and stays on #34 - this only makes the paths expressible.

    >>> seed_source()
    ('/proc/1/root/app', '/proc/1/root/python')
    >>> seed_source(project="/venv", interpreter="/python")
    ('/proc/1/root/venv', '/proc/1/root/python')
    """
    root = container_root.rstrip("/")
    return f"{root}/{project.strip('/')}", f"{root}/{interpreter.strip('/')}"


TARGET_ROOT_UNREADABLE = (
    "{root} cannot be listed, so the seed cannot run: reading the target's own "
    "filesystem through PID 1 needs the ptrace rung. `podbench doctor` measures "
    "it and names what denied it."
)
"""Said when the seat genuinely cannot see the target's root.

Which is a *different* failure from the one below, and telling them apart is the
whole of #178. This one is about the rung; the other is about the image. Until
2026-08-22 podbench inferred this from the project path being absent, so an
epics-containers image - which has no ``/app`` - was reported as a ptrace denial
and the user was sent to ``doctor``, which correctly reported the rung healthy.
A contradiction, and no next step.

What a missing rung *is* - a seat that landed without ``CAP_SYS_PTRACE``, or
into a namespace whose policy denies it - is said once in
``docs/how-to/hotfix-a-running-pod.md``, where somebody reading about the mode
will meet it, and measured by ``doctor``, which is where the reader is sent.
Naming it here as well spent three lines restating the report the next sentence
tells them to run.

It names what the listing *needs*, and stops short of asserting that this seat
lacks it. :func:`target_root_readable` is one ``ls`` exit status, so any non-zero
- an exec that failed, a seat that died mid-command - reaches here too, and a
flat "this seat does not have it" followed by "run ``doctor``" is #178's shape
again: podbench asserts the rung, ``doctor`` measures it healthy, and the reader
has a contradiction and no next step. Pointing at the measurement costs nothing
and cannot be contradicted by it.
"""

TARGET_HAS_NO_PROJECT = (
    "the target's image has no project at {image_project}, so there is nothing "
    "to seed the claim from. Its filesystem reads fine ({root} lists), so this "
    "is a layout difference: name the paths this image really uses with "
    "`--image-project PATH` and `--image-interpreter PATH`, or the container "
    "you meant with `--container NAME`."
)
"""Said when the root is fine and the project simply is not there.

Measured on ``bl47p-mo-ioc-01`` on 2026-08-22: from that seat ``/proc/1/root``
listed cleanly and ``/app`` did not exist. The old message blamed ptrace, so the
user was sent to ``doctor``, which reported the rung healthy - which is a
contradiction with no next step, and is #178.

It names neither *ptrace* nor *doctor*, deliberately and not merely because
neither is the cause. Both are the false trail the old message opened, and a
message that mentions a mechanism at all - even to rule it out - is one a reader
will go and chase. The path it prints is the one **inside the image**, not the
``/proc/1/root/...`` form podbench reads it through, because ``--image-project``
takes the former and an error should print what you would type.

Which layouts exist and why ``/app`` beside ``/python`` is only
python-copier-template's convention - an epics-containers image keeps its venv
at ``/venv``, a compiled IOC has no Python project at all - is in
``docs/how-to/hotfix-a-running-pod.md``, along with the one workaround that has
to be refused: copying the seat's own ``/app``, which is a different image whose
venv is podbench's rather than the application's. Two of the three paragraphs
this used to print were that material, read by somebody who at that moment needs
a flag rather than a taxonomy.
"""


def target_root_readable(store: HotfixStore, container_root: str) -> bool:
    """Whether the seat can traverse the target's root at all.

    Asked **explicitly**, and not inferred from the project path being missing,
    which is the mistake #178 is. ``ls`` and not ``test -e``: ``test -e`` follows
    the ``/proc/1/root`` symlink and answers for the target of the link, so it
    says yes on a seat that cannot traverse it - the exact case this has to
    catch. Listing is what :func:`podbench.proc.ptrace_readable` measures for the
    same reason, from inside the seat rather than through one.
    """
    return (
        store.run(["ls", f"{container_root.rstrip('/')}/"], check=False).returncode == 0
    )


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


def install_argv(
    interpreter: str, checkout: str, *, venv: str = CLAIM_VENV_DIR
) -> list[str]:
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

    ``UV_PROJECT_ENVIRONMENT`` is set only when *venv* is not uv's own default.
    uv builds ``.venv`` and the runtime switch the supervisor carries looks for
    whatever :data:`CLAIM_VENV_DIR` says; left to disagree, the rebuild would
    land beside the venv the pod is looking for and the pod would quietly go on
    running the image's code - the failure mode this whole mode exists to avoid.

    >>> install_argv("/x/bin/python3", "/podbench/app")[:4]
    ['env', 'UV_CACHE_DIR=/podbench/app/.uv-cache', 'uv', 'sync']
    >>> install_argv("/x/bin/python3", "/podbench/app", venv="env")[2]
    'UV_PROJECT_ENVIRONMENT=/podbench/app/env'
    """
    root = checkout.rstrip("/")
    environment = (
        [] if venv == CLAIM_VENV_DIR else [f"UV_PROJECT_ENVIRONMENT={root}/{venv}"]
    )
    return [
        "env",
        f"UV_CACHE_DIR={root}/.uv-cache",
        *environment,
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
    claim_mount: str = ""
    """Where :attr:`container` mounts the hotfix claim, ``""`` when it does not.

    Carried on the target because three verbs used to *ask* for it as
    ``--venv``; see :func:`resolve_venv`.
    """

    @property
    def workload(self) -> str | None:
        """``kind/name``, or ``None`` for a pod nobody owns."""
        if self.workload_kind is None or self.workload_name is None:
            return None
        return f"{self.workload_kind}/{self.workload_name}"


def claim_mount_path(pod_json: Mapping[str, Any], container: str) -> str:
    """Where *container* mounts the hotfix claim, or ``""`` if it does not.

    Resolved by the **volume's name** rather than by its mountPath, because the
    mountPath is the answer being looked for. :data:`HOTFIX_CLAIM_VOLUME` is
    podbench's own vocabulary — :func:`values_snippet` is what emits it — so
    this stays chart-agnostic in the way keying on a chart's own names would
    not.

    The search itself is :func:`podbench.launcher.application_mount`, which the
    launcher already runs over the same volume for the seat-vs-application mount
    note. A second copy of it here is how the two come to disagree about where
    the claim is.

    >>> pod = {"spec": {"containers": [{"name": "app", "volumeMounts": [
    ...     {"name": "podbench-app", "mountPath": "/podbench/app"}]}]}}
    >>> claim_mount_path(pod, "app")
    '/podbench/app'
    >>> claim_mount_path(pod, "sidecar")
    ''
    """
    mount = application_mount(pod_json, container, HOTFIX_CLAIM_VOLUME)
    return (_as_str(mount.get("mountPath")) or "").rstrip("/")


VENV_DISAGREES_WITH_POD = (
    "--venv {requested} disagrees with the pod: container {container} mounts "
    "the claim at {mounted}. The manifest lives at the root of the claim, so a "
    "--venv naming anywhere else writes this hotfix's provenance into the "
    "container's own filesystem, where it dies with the pod and where "
    "`hotfix status` cannot find it. Drop the flag — podbench reads the "
    "mountPath off the pod."
)
"""#205 item 1. The refusal is the point: the old flag accepted this silently."""

VENV_INVISIBLE_TO_STATUS = (
    "the claim is mounted at {venv}, not {default}, so `hotfix status` will "
    "not list this pod — it finds hotfixed pods by scanning for a mountPath of "
    "{default}. The rest of the mode is fixed at {default} as well: the seed, "
    "the copied interpreter and the supervisor's runtime switch all name it, "
    "so `hotfix init` will refuse a claim mounted anywhere else."
)
"""Said out loud on the one path that can still reach a non-default mount.

Named rather than refused *here* because ``apply`` and ``consolidate`` reach a
claim through a manifest that already records where it is, and a mode that
refused the value outright would be a mode with no escape hatch. What must not
happen is that it goes unsaid.

The second sentence is not a hedge. ``init`` seeds :func:`checkout_path`,
copies the interpreter to :data:`~podbench.model.HOTFIX_INTERPRETER_PATH` and
emits a supervisor switch naming :data:`~podbench.model.HOTFIX_APP_PATH`, none
of which move with the mount — so it refuses with "the claim is not mounted".
Saying "the fix itself is unaffected" would have been a warning contradicted by
the refusal one line later, which is worse than either message alone.
"""


def _venv_note(venv: str) -> str | None:
    return (
        None
        if venv == HOTFIX_APP_PATH
        else VENV_INVISIBLE_TO_STATUS.format(venv=venv, default=HOTFIX_APP_PATH)
    )


def resolve_venv(target: HotfixTarget, requested: str | None) -> tuple[str, str | None]:
    """The claim's mountPath, read off the pod, and a warning if it is unusual.

    ``--venv`` was required by ``init``, ``apply`` and ``consolidate`` and used
    by none of the measurement (#205 item 1): ``status`` reads the manifest at
    :data:`~podbench.model.HOTFIX_APP_PATH` and finds the mounting container by
    scanning for exactly that mountPath. So any other value wrote a manifest
    ``status`` could not see, and a hotfixed pod invisible to ``status`` is the
    precise failure the mode exists to prevent. Nothing warned.

    The pod already knows the answer, so it is asked. A value that disagrees
    with what the pod mounts is refused rather than obeyed, and a claim genuinely
    mounted elsewhere is honoured with :data:`VENV_INVISIBLE_TO_STATUS` said out
    loud.

    >>> here = HotfixTarget(PodRef("d", "p"), "app", "", "",
    ...                     claim_mount="/podbench/app")
    >>> resolve_venv(here, None)
    ('/podbench/app', None)
    >>> resolve_venv(HotfixTarget(PodRef("d", "p"), "app", "", ""), None)
    ('/podbench/app', None)
    >>> resolve_venv(here, "/opt/venv")  # doctest: +ELLIPSIS
    Traceback (most recent call last):
    podbench.hotfix.HotfixError: --venv /opt/venv disagrees with the pod: ...
    """
    mounted = target.claim_mount
    if requested is None:
        venv = mounted or HOTFIX_APP_PATH
        return venv, _venv_note(venv)
    venv = requested.rstrip("/") or requested
    if mounted and venv != mounted:
        raise HotfixError(
            VENV_DISAGREES_WITH_POD.format(
                requested=requested, container=target.container, mounted=mounted
            )
        )
    return venv, _venv_note(venv)


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
    return _resolve_target(kube, reference, container=container)[0]


def _resolve_target(
    kube: Kubectl,
    reference: str,
    *,
    container: str | None = None,
    require_single_replica: bool = True,
) -> tuple[HotfixTarget, dict[str, Any]]:
    """The target, and the pod JSON this walk resolved it out of.

    Both, for :func:`preflight`, which needs the pod's own document as well —
    the livenessProbe and the ephemeral container statuses. Asking ``get pod``
    a second time to re-read what this walk has just read is a call against an
    API server that answers nothing new, and it would put the report's rows a
    reconcile apart from the target they are about.

    ``require_single_replica`` is off for :func:`retire` alone. The refusal is
    about *writing* to a ReadWriteOnce claim, and the state a retirement report
    exists to confirm — the wiring out, the team scaled back up — is precisely
    the one it forbids, so asking "is this retired?" would answer with ``init``
    at somebody who has already finished.
    """
    kind, separator, name = reference.partition("/")
    if not separator:
        return _target_from_pod(
            kube, reference, container, require_single_replica=require_single_replica
        )
    if not name:
        raise HotfixError(f"no name in {reference!r}")
    if kind in ("pod", "pods", "po"):
        return _target_from_pod(
            kube, name, container, require_single_replica=require_single_replica
        )
    workload_kind = _WORKLOAD_KINDS.get(kind)
    if workload_kind is None:
        raise HotfixError(
            f"hotfix mode works on pods, deployments and statefulsets, not {kind!r}"
        )
    return _target_from_workload(
        kube,
        workload_kind,
        name,
        container,
        require_single_replica=require_single_replica,
    )


def _target_from_workload(
    kube: Kubectl,
    kind: str,
    name: str,
    container: str | None,
    *,
    require_single_replica: bool = True,
) -> tuple[HotfixTarget, dict[str, Any]]:
    workload = _get_json(kube, kind, name)
    replicas = replica_count(workload)
    if require_single_replica and replicas is not None and replicas != 1:
        _refuse_multi_replica(kind, name, replicas)
    selector = as_dict(as_dict(workload.get("spec")).get("selector"))
    labels = as_dict(selector.get("matchLabels"))
    if not labels:
        raise HotfixError(f"{kind}/{name} has no matchLabels to find its pod by")
    query = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
    result = kube.run("get", "pods", "-l", query, "-o", "json")
    pods = [as_dict(item) for item in _as_list(_load_json(result.stdout).get("items"))]
    live = [pod for pod in pods if _phase(pod) not in ("Succeeded", "Failed")]
    if not live or (len(live) != 1 and require_single_replica):
        raise HotfixError(
            f"{kind}/{name} matches {len(live)} live pods; hotfix mode needs "
            "exactly one. Name the pod directly if this is a rollout in flight."
        )
    chosen = _still_wired_first(live)
    # The workload is already known and already checked, so the ownership walk
    # below is skipped: repeating it would be two more API calls to rediscover
    # the object the caller named.
    target = _target_from_pod_json(kube, chosen, container, follow_owner=False)
    return (
        replace(target, workload_kind=kind, workload_name=name, replicas=replicas or 1),
        chosen,
    )


def _still_wired_first(live: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Which of a workload's live pods a report must be measured against.

    Only ever more than one under ``retire``, and the choice is the difference
    between a report and a lie: the ``wiring`` row asserts the hotfix is out of
    the application's pod template, so measuring the replica that has already
    been rolled while another still mounts the claim would tick it. Sorted by
    name so that the pick is stable between two runs that found the same thing.
    """
    ordered = sorted(
        (as_dict(pod) for pod in live),
        key=lambda pod: _as_str(as_dict(pod.get("metadata")).get("name")) or "",
    )
    return next((pod for pod in ordered if hotfix_wiring(pod)), ordered[0])


def _target_from_pod(
    kube: Kubectl,
    name: str,
    container: str | None,
    *,
    require_single_replica: bool = True,
) -> tuple[HotfixTarget, dict[str, Any]]:
    pod_json = kube.get_pod(name)
    return (
        _target_from_pod_json(
            kube,
            pod_json,
            container,
            require_single_replica=require_single_replica,
        ),
        pod_json,
    )


def _target_from_pod_json(
    kube: Kubectl,
    pod_json: Mapping[str, Any],
    container: str | None,
    *,
    follow_owner: bool = True,
    require_single_replica: bool = True,
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
        claim_mount=claim_mount_path(pod_json, chosen),
    )
    owner = _controller_of(metadata) if follow_owner else None
    if owner is None:
        return target
    kind, owner_name = owner
    if kind == "replicaset":
        return _through_replicaset(
            kube, target, owner_name, require_single_replica=require_single_replica
        )
    if kind in ("statefulset", "deployment"):
        return _with_workload(
            kube,
            target,
            kind,
            owner_name,
            require_single_replica=require_single_replica,
        )
    # Something else owns it — a Job, an operator's CRD. Not refused: there is
    # one pod and the hotfix is legitimate, but the annotation cannot go on a
    # template podbench does not understand.
    return replace(target, workload_kind=kind, workload_name=owner_name, replicas=1)


def _through_replicaset(
    kube: Kubectl,
    target: HotfixTarget,
    name: str,
    *,
    require_single_replica: bool = True,
) -> HotfixTarget:
    """Resolve a pod's ReplicaSet to the Deployment that owns it.

    The Deployment is what has to be annotated: annotating the ReplicaSet's
    template would be overwritten by the next rollout, and annotating the pod
    does not survive the reschedule Hotfix mode depends on.
    """
    replicaset = _get_json(kube, "replicaset", name)
    replicas = replica_count(replicaset)
    if require_single_replica and replicas is not None and replicas != 1:
        _refuse_multi_replica("replicaset", name, replicas)
    owner = _controller_of(as_dict(replicaset.get("metadata")))
    if owner is not None and owner[0] == "deployment":
        return _with_workload(
            kube,
            target,
            "deployment",
            owner[1],
            require_single_replica=require_single_replica,
        )
    return replace(
        target, workload_kind="replicaset", workload_name=name, replicas=replicas or 1
    )


def _with_workload(
    kube: Kubectl,
    target: HotfixTarget,
    kind: str,
    name: str,
    *,
    require_single_replica: bool = True,
) -> HotfixTarget:
    workload = _get_json(kube, kind, name)
    replicas = replica_count(workload)
    if require_single_replica and replicas is not None and replicas != 1:
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
    # No detail. The row's own columns already carry `+N commit(s)` and
    # `active - hotfixed, base image unchanged`, and a healthy row saying the
    # same number twice was the last of the three repetitions this phase set out
    # to remove. Every other branch here returns the sentence the columns cannot.
    return HotfixHealth.ACTIVE, ""


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

    retirement: tuple[RetirementCheck, ...] = ()
    """Where a consolidated hotfix is in its retirement, empty for one that has
    not started.

    Carried on the row rather than derived in :func:`format_status`, because it
    is measured from the *pod document* - which the formatter does not have, and
    must not be handed a second copy of on the way to a listing.

    Deliberately not part of :attr:`ok`. A consolidated fix whose image has not
    moved yet is a live hotfix doing its job, and a shutdown assertion that went
    red the moment ``consolidate`` ran would be one nobody could leave in CI.
    """

    @property
    def ok(self) -> bool:
        """Whether this row needs no attention, hold included.

        The hold moves the exit code (decision 6). A held pod is not a healthy
        one however healthy its fix: its liveness probe is short-circuited and
        its supervisor has no backoff, so "nothing here needs somebody today"
        must not come back true while one is still held.

        >>> row = HotfixRow(PodRef("d", "p"), None, HotfixHealth.ACTIVE, "")
        >>> row.ok
        True
        >>> replace(row, hold=Hold(deadline=None, now=0)).ok
        False
        """
        return self.health.ok and self.hold is None


def status_rows(
    kube: Kubectl,
    *,
    probe: bool = True,
    python: str = DEFAULT_PYTHON,
    all_namespaces: bool = False,
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

    ``all_namespaces`` is the same listing over the cluster, with the same exit
    contract on top of it (#205 item 5). ``main``'s docstring has always sold
    the exit code as a shutdown-checklist assertion, and a namespace-scoped
    command makes the facility-wide form a shell loop the operator has to write
    and keep correct. Each pod is still read through a client bound to *its own*
    namespace: one ``-n`` wrong on an exec is a claim read out of the wrong pod.
    """
    if all_namespaces:
        # No `-n` beside it. kubectl resolves `-n X --all-namespaces` in favour
        # of the latter, so this is not a behaviour fix - it is what podbench
        # can be quoted as having asked when the call is refused and its argv is
        # relayed verbatim (evidence §5).
        result = kube.run(
            "get", "pods", "--all-namespaces", "-o", "json", cluster_wide=True
        )
    else:
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
        namespace = _as_str(metadata.get("namespace")) or kube.namespace
        client = kube.for_namespace(namespace)
        pod = PodRef(namespace, name)
        notes: list[str] = []
        manifest, hold, unreadable = read_pod_state(client, name, container)
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
            measured = probe_interpreter(client, name, container, python=python)
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
                retirement=_retirement_of(pod_json, manifest, digest),
            )
        )
    return rows


def _retirement_of(
    pod_json: Mapping[str, Any], manifest: HotfixManifest | None, digest: str
) -> tuple[RetirementCheck, ...]:
    """The retirement steps for a row, measured only once one is under way.

    Only for a consolidated hotfix, because until then there is nothing to
    retire: every live hotfix is "still wired to its claim", and a listing that
    said so on every row would be saying nothing on the one row where it is the
    finding (#205 item 4).

    The claim is left **unmeasured** rather than inferred from the mount - see
    :data:`STATUS_DOES_NOT_READ_CLAIMS`.
    """
    if manifest is None or manifest.consolidated_branch is None:
        return ()
    return tuple(
        retirement(
            pod_json,
            manifest,
            claim=ClaimState(
                name=pod_claim_name(pod_json), detail=STATUS_DOES_NOT_READ_CLAIMS
            ),
            current_digest=digest,
        )
    )


_FLAG = 10
"""Where a row's pod name starts, which is ``doctor``'s column so the two tables
scan alike — and the status is bracketed for the same reason, so that
:mod:`podbench.console` colours it without either module saying what a colour
is."""

_ROW_INDENT = " " * 4
"""Where a row's prose sits under it."""


def format_status(rows: Sequence[HotfixRow], *, all_namespaces: bool = False) -> str:
    """The status report. Empty is a real and reassuring answer, so say it.

    Only the prose is wrapped. The row line itself is a set of columns held
    apart by double spaces, and wrapping collapses whitespace — so a row put
    through it would come back as a sentence, with the commit and the health
    no longer under the headings the eye is running down.

    ``all_namespaces`` changes one thing, and it is the empty answer: "no
    hotfixed pods in this namespace" from a cluster-wide run is the reassurance
    somebody wanted for the whole facility, given for one namespace.
    """
    if not rows:
        return "no hotfixed pods in " + (
            "any namespace" if all_namespaces else "this namespace"
        )
    lines: list[str] = []
    for row in rows:
        manifest = row.manifest
        ahead = manifest.ahead if manifest is not None else 0
        commit = manifest.commit[:7] if manifest is not None else "unknown"
        flag = "[ok]" if row.ok else "[!]"
        # A column, not a clause in the health sentence: a held pod and an
        # unhealthy fix are different questions and either can be true alone.
        held = f"  {row.hold.summary()}" if row.hold is not None else ""
        # The count is a difference against the base, so a base nobody measured
        # makes it a guess — and it is qualified here, in the column the eye
        # actually lands on, rather than only on the `base` line below.
        drift = f"+{ahead} commit(s)" + (
            " from an assumed base"
            if manifest is not None and manifest.base_commit_assumed
            else ""
        )
        lines.append(
            f"  {flag}".ljust(_FLAG) + f"{row.pod}  {drift}  {commit}  "
            f"{row.health.value} — {row.health.summary}{held}"
        )
        if row.detail:
            lines.extend(paragraph(row.detail, first=_ROW_INDENT, indent=_ROW_INDENT))
        if manifest is not None:
            # The sha is dropped where it is the one already in the row's own
            # column: at `+0` the claim is *at* its base, so repeating it says
            # nothing the column did not. What is left is the provenance, which
            # is the half of this line that appears nowhere else.
            #
            # Guarded on the sha being there at all: with neither recorded the
            # two are equal by vacuity, and "on its base commit" would assert an
            # identity nobody measured in the one place the old `base ?` said
            # so.
            base = (
                "on its base commit"
                if manifest.base_commit and manifest.commit == manifest.base_commit
                else f"base {manifest.base_commit[:7] or '?'}"
            )
            lines.append(
                f"{_ROW_INDENT}{base} · "
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
        if row.retirement:
            # One line, and only for a hotfix on its way out: `superseded` used
            # to be the whole of what this said, correctly and indefinitely,
            # which is a state nobody can act on (#205 item 4).
            lead = f"{_ROW_INDENT}retirement: "
            lines.extend(
                paragraph(
                    retirement_summary(
                        row.retirement,
                        # The namespace goes in even when it is the current
                        # one: under `--all-namespaces` the rows come from
                        # several, and an offer that is pasteable on some rows
                        # of one listing and not others is worse than a
                        # redundant flag.
                        reference=f"pod/{row.pod.name} -n {row.pod.namespace}",
                    ),
                    first=lead,
                    indent=" " * len(lead),
                )
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
            "from `podbench hotfix values` to be this container's "
            "command. Without it there is nothing to relaunch, and applying a "
            "fix would restart the container and kill your seat with it."
        )


def _seeded_interpreter(
    store: HotfixStore, checkout: str, *, venv: str = CLAIM_VENV_DIR
) -> str:
    """The interpreter version the seeded venv reports, or ``""``.

    Read from the claim rather than from the image, because after the rebuild
    the claim's is the one the console scripts name.
    """
    config = store.read_text(f"{checkout}/{venv}/pyvenv.cfg")
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


NO_SOURCE_REPO = (
    "hotfix init has no source repository to clone: the target image carries "
    "no {label} label, or its registry would not answer without credentials. "
    "Pass `--repo URL`."
)
"""#205 item 2's other half. ``--repo`` is a value the image usually states.

One constant, said by ``init`` when it refuses and by ``check`` before it, for
:data:`CLAIM_NOT_MOUNTED`'s reason: ``check``'s whole job is to say ``init``'s
refusals earlier, and a second spelling is how the two come to describe
different remedies for one state.
"""

LABELS_FROM_BASE_IMAGE = (
    "the image's labels name {labelled}, not {source}: inherited from its base "
    "image, so its revision is not this repository's"
)
"""Why an image's own ``org.opencontainers.image.*`` may describe something else.

**OCI labels are inherited.** A derived image carries its base image's labels
unless its build overrides them, and many builds do not. Measured 2026-08-23:
``ghcr.io/diamondlightsource/fastcs-example-debug:2025.10.1`` — the IOC this
mode was proved against — advertises
``source=https://github.com/DiamondLightSource/ubuntu-devcontainer``, a revision
in *that* repository and ``title=ubuntu-devcontainer``, while the IOC's own
source is ``.../fastcs-example``.

:func:`_has_commit` cannot catch this on its own: when the repository *also*
came from the label, the clone made from it contains the label's own revision,
so the check passes and the drift is recorded as measured against another
project's history. Hence :func:`corroborate_source` — the label is believed
only where something that did not come from the image agrees with it.
"""

SOURCE_LABEL_UNCORROBORATED = (
    "the image names {source}, which its own repository {image} does not "
    "correspond to. OCI labels are inherited from the base image unless the "
    "build overrides them, and `hotfix init` with no `--repo` clones whatever "
    "the label names: pass `--repo URL` if that is not this application's "
    "source."
)
"""``check``'s ``source`` row when the only thing naming the repository is a
label the image's own name contradicts.

A ``WARN`` and not a ``FAIL``, because ``init`` does not refuse this state - it
clones the label's repository and records an ``ASSUMED`` base
(:func:`base_commit_from`). The defect this replaced was the opposite error: an
``[ok]`` under "nothing measured here blocks `podbench hotfix init`", which made
``check`` *more* confident than the verb it exists to speak for. Measured
2026-08-23 on ``fastcs-example-debug:2025.10.1``, whose labels are
``ubuntu-devcontainer``'s throughout - see :data:`LABELS_FROM_BASE_IMAGE`.
"""


def same_repository(one: str, other: str) -> bool:
    """Whether two spellings of a git remote name the same repository.

    Compared on host and path, because ``origin`` and a label are written by
    different tools: one may be scp-style, carry a ``.git`` suffix, a scheme, a
    username or a trailing slash, and none of that is a difference.

    >>> same_repository("https://github.com/Acme/Api.git", "git@github.com:acme/api")
    True
    >>> same_repository("https://github.com/acme/api", "https://github.com/acme/web")
    False
    >>> same_repository("", "https://github.com/acme/api")
    False
    """
    return bool(one) and bool(other) and _repo_key(one) == _repo_key(other)


def _repo_key(url: str) -> str:
    """A git remote URL reduced to ``host/path``.

    >>> _repo_key("ssh://git@github.com/acme/api.git/")
    'github.com/acme/api'
    >>> _repo_key("git@github.com:acme/api")
    'github.com/acme/api'
    """
    text = url.strip().lower().removeprefix("git+")
    _, scheme, rest = text.partition("://")
    text = rest if scheme else text
    text = text.rpartition("@")[2]
    head, colon, tail = text.partition(":")
    if colon and "/" not in head:
        # scp-style `host:path`, which has no slash before the colon.
        text = f"{head}/{tail}"
    return text.rstrip("/").removesuffix(".git").rstrip("/")


def image_name_agrees(image: str, source: str) -> bool:
    """Whether the image's *own* name corresponds to the repository *source* names.

    The one corroborator that is free and always available: a registry path is
    not part of the config blob, so a base image cannot have set it. Every
    other independent naming of the repository - ``--repo``, the seeded
    checkout's ``origin`` - has to be supplied or read from somewhere, and on
    an unseeded claim there is no checkout to read one from.

    It is a correspondence and not an equality, because a debug or a runtime
    variant is built from the same source under a suffixed name: measured
    2026-08-23, ``fastcs-example-debug`` is built from ``fastcs-example``, and
    an equality test would cry wolf on every image built that way.

    >>> image_name_agrees("ghcr.io/dls/fastcs-example-debug:2025.10.1",
    ...                   "https://github.com/DiamondLightSource/fastcs-example")
    True
    >>> image_name_agrees("ghcr.io/dls/fastcs-example-debug:2025.10.1",
    ...                   "https://github.com/DiamondLightSource/ubuntu-devcontainer")
    False
    >>> image_name_agrees("", "https://github.com/acme/api")
    False
    """
    ref = parse_reference(image)
    if ref is None or not source.strip():
        return False
    named = ref.repository.lower().rpartition("/")[2]
    repo = _repo_key(source).rpartition("/")[2]
    if not named or not repo:
        return False
    return named == repo or named.startswith(f"{repo}-") or repo.startswith(f"{named}-")


def corroborate_source(
    given: str | None, labelled: str, origin: str
) -> tuple[str, bool, str | None]:
    """The repository to record, whether the image's labels describe it, and why.

    *given* is ``--repo``, *labelled* is the image's
    :data:`~podbench.oci.SOURCE_LABEL` and *origin* is the seeded checkout's own
    remote — ``""`` where there is none to read, which is also the case after a
    clone, because a clone made from *labelled* corroborates nothing.

    Corroboration is the whole point (see :data:`LABELS_FROM_BASE_IMAGE`): a
    label is believed only where something *independent of the image* names the
    same repository. Everything else records an assumed base, which is the
    honest-uncertainty path #205 item 2 asks for rather than a fallback from it.

    >>> corroborate_source(None, "https://github.com/acme/api",
    ...                    "git@github.com:acme/api")[1:]
    (True, None)
    >>> source, ok, note = corroborate_source(
    ...     None, "https://github.com/base/devcontainer",
    ...     "https://github.com/acme/api")
    >>> source, ok
    ('https://github.com/acme/api', False)
    >>> "inherited from its base image" in note
    True
    >>> corroborate_source(None, "https://github.com/acme/api", "")
    ('https://github.com/acme/api', False, None)
    """
    independent = (given or "").strip() or origin
    source = independent or labelled
    corroborated = bool(labelled) and same_repository(labelled, independent)
    note = (
        None
        if corroborated or not labelled or not independent
        else LABELS_FROM_BASE_IMAGE.format(labelled=labelled, source=source)
    )
    return source, corroborated, note


def _origin_url(store: HotfixStore, checkout: str) -> str:
    """The checkout's ``origin`` remote, or ``""`` where there is none."""
    probe = store.run(git_argv(checkout, "remote", "get-url", "origin"), check=False)
    return probe.stdout.strip() if probe.returncode == 0 else ""


def _has_commit(store: HotfixStore, checkout: str, sha: str) -> bool:
    """Whether the checkout actually contains *sha*.

    An image label is a claim about *a* repository, and the one cloned may not
    be it — a fork, a mirror, a history that was rewritten. A base the clone
    does not have makes ``git log base..HEAD`` fail outright, so the label is
    verified before it is believed rather than discovered to be wrong later.
    """
    probe = store.run(
        git_argv(checkout, "cat-file", "-e", f"{sha}^{{commit}}"), check=False
    )
    return probe.returncode == 0


def base_commit_from(
    store: HotfixStore,
    checkout: str,
    stated: str | None,
    labels: Mapping[str, str] | None,
    *,
    corroborated: bool = True,
) -> tuple[str, bool, str]:
    """The commit drift is measured from, whether it was measured, and why.

    Three answers in a fixed order of confidence, and the third is the one
    #205 item 2 is about: ``git rev-parse HEAD`` of a fresh clone is, without
    ``--ref``, the default branch's tip, which is almost never the commit the
    released image was built from. Everything downstream — ``status``'s
    ``+N commit(s)``, :func:`drift_commits`, the set ``consolidate`` pushes — is
    a difference against this number, so a guess here is a guess everywhere,
    and one that reads as a measurement.

    *corroborated* is :func:`corroborate_source`'s verdict on whether the labels
    describe this repository at all. It gates the revision because
    :func:`_has_commit` cannot: a clone made from the label's own repository
    contains the label's own revision whatever project that repository is.

    Returns the sha, whether it was *assumed*, and the line ``init`` prints.
    """
    if stated:
        return stated, False, f"base commit {stated[:7]}, as given"
    revision = ((labels or {}).get(REVISION_LABEL) or "").strip()
    if revision and corroborated and _has_commit(store, checkout, revision):
        return (
            revision,
            False,
            f"base commit {revision[:7]}, from the image's {REVISION_LABEL}",
        )
    head = store.run(git_argv(checkout, "rev-parse", "HEAD")).stdout.strip()
    if not revision:
        why = f"no {REVISION_LABEL} on the image"
    elif not corroborated:
        why = (
            f"the image names {revision[:7]}, but nothing outside the image "
            "confirms its labels are this repository's"
        )
    else:
        why = f"the image names {revision[:7]}, not in this checkout"
    return (
        head,
        True,
        f"base commit {head[:7]} ASSUMED ({why}); pass --base-commit SHA",
    )


def init(
    kube: Kubectl,
    store: HotfixStore,
    target: HotfixTarget,
    *,
    venv: str,
    repo: str | None = None,
    image_labels: Mapping[str, str] | None = None,
    ref: str | None = None,
    base_commit: str | None = None,
    author: str | None = None,
    install: bool = True,
    container_root: str = "/proc/1/root",
    image_project: str = IMAGE_PROJECT_PATH,
    image_interpreter: str = IMAGE_INTERPRETER_PATH,
    claim_venv: str = CLAIM_VENV_DIR,
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

    *image_labels* are the target image's OCI labels, when they could be read
    (:func:`podbench.oci.image_labels`). They default ``repo`` and the base
    commit, which is what stops the two values the image already states from
    being two more things to type in an emergency. ``None`` is not an error:
    it means the base is recorded as assumed, which :func:`base_commit_from`
    is careful to say rather than paper over.

    Labels are **corroborated before they are believed** — see
    :data:`LABELS_FROM_BASE_IMAGE`, which is the measured case of an image
    advertising its base image's repository and revision. The seeded checkout's
    ``origin`` is what corroborates them, and where it disagrees it wins: it is
    a measurement of the project actually on the claim, and ``manifest.repo`` is
    where ``consolidate`` will push.
    """
    actions: list[str] = []
    require_supervisor(kube, target)
    project, interpreter_src = seed_source(
        container_root, project=image_project, interpreter=image_interpreter
    )
    checkout = checkout_path()

    if not store.exists(checkout):
        raise HotfixError(CLAIM_NOT_MOUNTED.format(checkout=checkout))

    labelled = ((image_labels or {}).get(SOURCE_LABEL) or "").strip()
    # Resolved before the seed, not after it. The seed is a ~50s `cp -a` plus an
    # interpreter copy, and `main`'s except clause prints the error alone - so a
    # refusal on the far side of it costs the wait and then discards the only
    # record that the claim was written to at all. `--repo` was a required
    # option until #205 item 2, which is why this ordering was unreachable.
    if not (repo or "").strip() and not labelled:
        raise HotfixError(NO_SOURCE_REPO.format(label=SOURCE_LABEL))

    if store.exists(f"{checkout}/pyproject.toml"):
        actions.append(f"claim already seeded at {checkout}")
    else:
        if not store.exists(project):
            # Two failures wearing one message until 2026-08-22. Ask the root
            # question directly rather than inferring it from the leaf: they
            # have different causes and different fixes, and the one podbench
            # guessed sent the user to `doctor`, which then disagreed with it.
            if not target_root_readable(store, container_root):
                raise HotfixError(TARGET_ROOT_UNREADABLE.format(root=container_root))
            raise HotfixError(
                TARGET_HAS_NO_PROJECT.format(
                    root=container_root, image_project=f"/{image_project.strip('/')}"
                )
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

    interpreter = _seeded_interpreter(store, checkout, venv=claim_venv)
    actions.append(f"claim seeded, venv interpreter {interpreter or 'unknown'}")

    if store.exists(f"{checkout}/.git"):
        actions.append(f"checkout already present at {checkout}")
        # The one naming of the repository that did not come out of the image,
        # and so the only thing that can tell an inherited label from this
        # image's own. Read after the seed because the checkout the image ships
        # is what the seed just copied.
        origin = _origin_url(store, checkout)
    else:
        clone = ["git", "clone"]
        if ref is not None:
            clone += ["--branch", ref]
        clone += [(repo or "").strip() or labelled, checkout]
        store.run(clone)
        actions.append(f"cloned {clone[-2]} to {checkout}")
        # Deliberately not read back: a clone made from the label agrees with
        # the label by construction, which is not corroboration.
        origin = ""

    source, corroborated, mismatch = corroborate_source(repo, labelled, origin)
    if mismatch is not None:
        actions.append(mismatch)

    resolved, assumed, provenance = base_commit_from(
        store, checkout, base_commit, image_labels, corroborated=corroborated
    )
    actions.append(provenance)

    if install:
        actions.append(
            _install(kube, target, seeded_python(store), checkout, venv=claim_venv)
        )

    name, email = _identity(store, checkout, author)
    manifest = HotfixManifest(
        venv=venv,
        checkout=checkout,
        repo=source,
        base_image=target.image,
        base_image_digest=target.image_digest,
        interpreter=interpreter,
        container=target.container,
        commit=resolved,
        base_commit=resolved,
        base_commit_assumed=assumed,
        claim_venv=claim_venv,
        author=f"{name} <{email}>",
        timestamp=_now(),
        ahead=0,
        commits=(),
    )
    write_manifest(store, manifest)
    actions.append(f"wrote {manifest_path(venv)}")
    return manifest, actions


def _install(
    kube: Kubectl,
    target: HotfixTarget,
    interpreter: str,
    checkout: str,
    *,
    venv: str = CLAIM_VENV_DIR,
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
    argv = install_argv(interpreter, checkout, venv=venv)
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
            f"rebuilding the venv failed in container {target.container}:\n"
            f"{_relay(result.stderr.strip() or result.stdout.strip())}\n"
            "It has to run there and not in the seat, whose interpreter is a "
            "different image's. If the pod has no egress to an index, pre-build "
            "the venv on the claim and re-run with --no-install."
        )
    return f"rebuilt the venv at {checkout}/{venv}"


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
        # The venv the *manifest* records, never the default (#209). `apply`
        # has no --claim-venv of its own and never should have: a rebuild that
        # landed in `.venv` while the supervisor's runtime switch looked in the
        # directory `init` was told about is the silent revert this whole mode
        # exists to prevent, and it is not something to ask twice about.
        actions.append(
            _install(
                kube,
                target,
                seeded_python(store),
                checkout,
                venv=manifest.claim_venv or CLAIM_VENV_DIR,
            )
        )
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
        # Rewriting a v1 manifest as v2 would otherwise turn "this schema could
        # not record whether the base was measured" into "it was measured",
        # which is the one direction this field must never move in.
        base_commit_assumed=manifest.base_commit_assumed or manifest.stale_schema,
        schema_version=MANIFEST_VERSION,
    )
    write_manifest(store, manifest)
    actions.append(
        f"{len(commits)} commit(s) ahead of {manifest.base_commit[:7]}"
        f"{' (an assumed base)' if manifest.base_commit_assumed else ''}"
    )

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
            f"the relaunch failed in container {target.container}:\n"
            f"{_relay(result.stderr.strip() or result.stdout.strip())}\n"
            "The hold file is removed on every path, so the pod is fail-fast "
            f"again either way - check {HOTFIX_CHILD_PID_PATH} exists, which is "
            "what tells you the supervisor is the one from `hotfix values` and "
            "not the image's own entrypoint."
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
    """The steps after this one, and the verb that now tracks them.

    Steps 4 and 5 are the two nobody does, and they are stated as two because
    the first is in the *application's* values and the second in the claim's
    chart: turning the claim off leaves the pod wired, which is how a claim
    goes on shadowing a fixed image (measured on p47-beamline, 2026-08-23 -
    :data:`WIRING_IS_THE_APPLICATIONS_OWN`). Neither is prose any more:
    ``hotfix retire`` measures where the reader has got to.
    """
    workload = target.workload or f"pod/{target.pod.name}"
    # Steps 1 to 3 are authored finished, and step 1 for the reason the
    # `terminal-reports` skill gives: it is a command somebody selects and
    # pastes, and a wrap through it costs the report the one line that was for
    # doing rather than reading. 4 and 5 are prose, so they go through
    # `paragraph` and take their width from the terminal.
    lines = [
        "",
        "next, in order — the claim is now the only copy of this fix:",
        f"  1. gh pr create --head {branch} --title 'consolidate hotfix "
        f"{manifest.commit[:7]}'",
        "  2. merge, and let CI build and publish the image",
        f"  3. roll {workload} onto the new image and confirm it is healthy",
    ]
    lines.extend(
        paragraph(
            "take the volume, volumeMount, args and podSecurityContext back out "
            "of the application's own values, and redeploy",
            first="  4. ",
            indent="     ",
        )
    )
    lines.extend(
        paragraph(
            f"turn the claim off (`{SUBCHART_VALUES_KEY}.enabled=false`, or "
            "`hotfixProject.enabled=false` on the central route) and delete it "
            "— it is annotated Prune=false, so the flip alone leaves it",
            first="  5. ",
            indent="     ",
        )
    )
    lines.append("")
    lines.extend(
        paragraph(
            f"`podbench hotfix retire pod/{target.pod.name}` says which of those "
            "have landed, and deletes the claim once nothing mounts it."
        )
    )
    return "\n".join(lines)


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


def hold_loop_args(entrypoint: str, *, venv: str = CLAIM_VENV_DIR) -> str:
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
            f"    if [ -x {HOTFIX_APP_PATH}/{venv}/bin/python ]; then",
            f'      export PATH="{HOTFIX_APP_PATH}/{venv}/bin:$PATH"',
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
    "`hotfix values` reads the target itself and there is no offline emission. "
    "Supplying these values by hand is what produced #176: a probe timing left "
    "out becomes a Kubernetes default, in the restart-sooner direction.\n"
    "\n"
    "So make the read work - `--from-pod POD` names the pod, `-n NS` and "
    "`--context NAME` say where - or state one field on top of it with "
    "`--entrypoint`, `--gid` or `--liveness-probe`."
)
"""Why reading the pod is not optional, appended to every failure of it.

Making a cluster read the default means every ``kubectl`` failure now lands on a
user who did not ask for one, so a bare relay of kubectl's stderr is not enough:
the message has to say what to do next. Until #205 item 6 that was a flag -
``--no-from-pod`` emitted from the three hand-supplied values instead - and the
message's second job was to warn that the flag was the exact route that produced
#176.

The flag is gone and the #176 sentence stays, because what it names is now the
*reason* there is nothing to fall back to rather than the price of falling back.
Dropping it would leave a reader who cannot reach the cluster thinking the read
is a convenience, and their next move is to hand-write the five keys into the
chart anyway - the same footgun, off the tool and unwarned.

What stays is the *consequence*; the mechanism behind it - a chart renders a
supplied ``livenessProbe`` wholesale, measured as 120s/30s going to 0s/10s on a
compiled IOC - is in ``docs/how-to/hotfix-a-running-pod.md``, said once, where
somebody reading about the mode will meet it. A terminal is where you find out
*that* it applies to the command in front of you.

What went off it in the 6c pass is only compression - "there is no offline
emission to fall back to" and "where the read reaches the pod and the pod
cannot answer for one field" - and the three beats are unchanged: there is no
fallback, hand-supplying costs a restart-sooner probe, and here are the flags
that fix the read or override one field of it.

The consequence is stated as a *direction*, not an event, because #176 was
caught in review and the values were hand-corrected before they deployed: no
target was ever restarted for it. Saying it was would be one word's worth of
length and the whole of the message's authority - a reader who opens the issue
finds it overstated, and a warning caught exaggerating once is discounted
thereafter.
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
            "`--entrypoint CMD`; the gid and the probe are still read from the "
            f"pod.\n\n{FROM_POD_ESCAPE}"
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


SUBCHART_VALUES_KEY = "podbench-hotfix-claim"
"""The values key the ``podbench-hotfix-claim`` chart dependency reads.

A subchart's values live under its chart name, so this is the name of the chart
and not a choice - unless the target aliases the dependency, which is why every
function that uses it takes it as an argument rather than reaching for the
constant."""


def values_snippet(
    app: str,
    entrypoint: str,
    *,
    size: str = "2Gi",
    gid: str = DEFAULT_GID_PLACEHOLDER,
    home_size: str = SEAT_HOME_SIZE,
    venv: str = CLAIM_VENV_DIR,
    liveness_exec: Sequence[str] | None = None,
    liveness_probe: Mapping[str, Any] | None = None,
    indent: int = 0,
    volumes_key: str = "volumes",
    mounts_key: str = "volumeMounts",
    command_key: str = "command",
    args_key: str = "args",
    probe_key: str = "livenessProbe",
    security_key: str = "podSecurityContext",
    claim_key: str = SUBCHART_VALUES_KEY,
    central_claim: bool = False,
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
    # Timings only ever come from a whole probe, and a probe is entitled to
    # declare none - the read finds that shape on a target whose schedule lives
    # in its chart. #176 is what an emitted probe missing them costs, so the
    # empty case is warned about below rather than left to a restart ladder.
    exec_command = liveness_exec or (
        probe_exec_command(liveness_probe) if liveness_probe else []
    )
    probe = wrapped_liveness_probe(exec_command) if exec_command else None
    timings = probe_timings(liveness_probe)
    header = (
        [
            f"# 1. values for the podbench release - creates the claim {claim}",
            "hotfixProject:",
            "  enabled: true",
            "  claims:",
            f"    - name: {app}",
            f"      size: {size}",
        ]
        if central_claim
        else [
            f"# 1. the claim {claim}, from the podbench-hotfix-claim chart",
            "#    dependency. The name is the release's, so this key is all the",
            "#    service says; --central-claim emits the older namespace-wide",
            "#    hotfixProject list instead.",
            f"{claim_key}:",
            "  enabled: true",
            f"  size: {size}",
        ]
    ) + [
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
    lines += [
        f"    {line}" for line in hold_loop_args(entrypoint, venv=venv).splitlines()
    ]
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
                "  # WARNING: the probe this was built from declared no timings, so",
                "  # this one gets the Kubernetes defaults, initialDelaySeconds 0",
                "  # and periodSeconds 10. The chart may have been supplying its",
                "  # own on a branch that a supplied livenessProbe takes instead",
                "  # of: ioc-instance does, measured at 120s/30s. Check, and copy",
                "  # those numbers in here, or the target is probed sooner and more",
                "  # often than it was before.",
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


# -- merging into a target's own values file --------------------------------


ABSORBED_FROM_PARENT_NOTE = (
    "podbench: {key} came from {parent} and has been copied into this file, "
    "because a helm list replaces across the parent/child merge rather than "
    "merging into it. Copied: {names}."
)
"""Said when the merge absorbs the shared file's entries into the target's own.

The live proof is ``bl47p-mo-ioc-01``, which declares ``dev-shm`` and whose
running pod carries no ``beamline-data`` at all - the shared entry every other
IOC on that beamline inherits. This is the difference between output that is
correct and output that silently unmounts a beamline directory, so it is said
out loud rather than done quietly.

What the replace costs when it is *not* handled - the service takes the shared
list over completely and anything it declared is gone - is in
``docs/how-to/hotfix-a-running-pod.md`` under ``--parent-values``. Here it has
been handled, so the note names the fact, the reason and the entries copied, and
:data:`NO_PARENT_NOTE` carries the cost because that is the case where it is
still to be paid."""

NO_PARENT_NOTE = (
    "podbench: {path} declares no {key} of its own and no shared values file "
    "was given. If this service inherits {key} from one, pass it with "
    "`--parent-values`: the emitted file declares {key} for the first time, "
    "and a helm list replaces across that merge, so an inherited entry would "
    "be silently dropped."
)
"""Said when the target declares no list and podbench cannot see whether one
was inherited.

Absence of a parent file is not evidence that there is no parent, and this is
the one thing the merge genuinely cannot decide. It is the only case where the
output may be wrong, so it is the only case that says so."""

MERGED_UNDER_NOTE = (
    "podbench: the application's keys went under {where}, {why}. "
    "--values-under puts them somewhere else."
)
"""Where the five passthroughs were put, and why.

An answer the user did not ask for is only acceptable if the output names it -
the rule the auto-mount and the editor's folder choice already follow."""

TOP_LEVEL = "the top level"
"""How :data:`MERGED_UNDER_NOTE` spells an empty key path."""


def _round_trip() -> YAML:
    """A YAML round-tripper configured to leave somebody else's file alone.

    Comments are why this is ``ruamel.yaml`` and not a parse-and-redump: a
    values file is mostly comments explaining why each key is there, and handing
    one back without them is not "the whole file". Round-tripping keeps the
    comments and the quoting but re-emits the layout, so the indent is set to
    what helm values files are conventionally written with, and the width is set
    high enough that a long ``args:`` line or a hostPath is not ours to re-wrap.
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    return yaml


# ruamel.yaml ships no complete type information, so the four calls into it
# are annotated here and narrowed immediately. The ignores are deliberately on
# these lines and nowhere else: everything past them is ordinary dicts and
# lists, which is what keeps the rest of this section strict-checkable.
def _load_yaml(text: str) -> dict[str, Any]:
    loaded: object = _round_trip().load(text)  # pyright: ignore[reportUnknownMemberType]
    return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else CommentedMap()


def _dump_yaml(document: Any) -> str:
    stream = io.StringIO()
    _round_trip().dump(document, stream)  # pyright: ignore[reportUnknownMemberType]
    return stream.getvalue()


def _mapping_at(document: Any, path: Sequence[str]) -> dict[str, Any] | None:
    """The mapping at *path*, or ``None`` where the path is not one."""
    node: Any = document
    for step in path:
        if not isinstance(node, dict) or step not in cast(dict[str, Any], node):
            return None
        node = cast(dict[str, Any], node)[step]
    return cast(dict[str, Any], node) if isinstance(node, dict) else None


def _mappings(document: Any) -> list[tuple[str, ...]]:
    """Every mapping in *document* as a key path, shallowest first.

    Two levels and no further. A chart nests its passthroughs under its own name
    and an umbrella nests one deeper; nothing sensible puts a pod template three
    mappings down, and searching further only invents candidates.
    """
    found: list[tuple[str, ...]] = [()]
    if not isinstance(document, dict):
        return found
    for key, value in cast(dict[str, Any], document).items():
        if not isinstance(value, dict):
            continue
        found.append((str(key),))
        for inner, deeper in cast(dict[str, Any], value).items():
            if isinstance(deeper, dict):
                found.append((str(key), str(inner)))
    return found


def locate_keys(
    document: Mapping[str, Any],
    keys: Sequence[str],
    parent: Mapping[str, Any] | None = None,
) -> tuple[tuple[str, ...], str]:
    """Where the application's keys belong in this file, and why.

    Read from the files rather than assumed, because podbench does not know the
    target's chart and #192 will not let it guess: ``ioc-instance`` nests all
    five under an ``ioc-instance:`` key, and a chart with no subchart of its own
    has them at the root.

    A file that already declares one of these keys has answered the question. A
    shared file that declares one has answered it for every service that
    inherits it - which is the ``bl47p-ea-fastcs-01`` case exactly, since it
    declares none of them itself. Failing both, the top level, which is right
    often enough to be the default and wrong quietly enough that the caller has
    to say so.
    """
    wanted = set(keys)
    sources = (
        (document, "where this file already declares them"),
        (parent, "where the shared values file declares them"),
    )
    for source, why in sources:
        if source is None:
            continue
        for path in _mappings(source):
            node = _mapping_at(source, path)
            if node is not None and wanted & set(node):
                return path, why
    return (), "with nothing else to go on"


def _strip_comments(node: Any) -> Any:
    """Every comment out of a parsed tree, in place, and the tree back.

    Two sources of comments have no business in somebody else's values file.

    The **snippet's** are written for a reader deciding what to paste; pasted,
    they are twenty lines of podbench prose in a service's values file, and
    ruamel re-emits them at the column they were parsed at, which is not the
    column they end up in. The merge writes a short deliberate set instead - see
    :data:`CLAIM_COMMENT` and its neighbours.

    The **shared file's** are worse than noise: a comment sits on the node
    *after* it, so copying one entry out of a parent's list brings the sentence
    that introduced the next key with it, and it lands mid-list under a heading
    it has nothing to do with. Measured on `p47-services`, where absorbing
    `beamline-data` dragged "shared setting for all legacy IOCs" into the
    middle of `volumeMounts`.
    """
    if isinstance(node, (CommentedMap, CommentedSeq)):
        node.ca.comment = None  # pyright: ignore[reportUnknownMemberType]
        node.ca.items.clear()  # pyright: ignore[reportUnknownMemberType]
    if isinstance(node, dict):
        for value in cast(dict[str, Any], node).values():
            _strip_comments(value)
    elif isinstance(node, list):
        for item in cast(list[Any], node):
            _strip_comments(item)
    return cast(Any, node)


SEAT_HOME_COMMENT = (
    "Declared and deliberately *not mounted* by the application. An ephemeral\n"
    "container may only mount volumes its pod already declares, and pod volumes\n"
    "cannot be added after the pod is created - so the seat's home is here at\n"
    "deploy time or the seat does without one.\n"
)
"""Written above the seat's home volume, which otherwise reads as a mistake.

It is the one entry in the emitted `volumes:` that nothing in the same file
mounts, so a reader who is not told assumes it was left half-finished."""

CLAIM_COMMENT = (
    "\npodbench Hotfix mode: a claim carrying the project, mounted beside the\n"
    "application's own and never over it. The claim's name comes from the\n"
    "release, and `podbench hotfix values` wrote the matching\n"
    "volume into the application's own values.\n"
)
"""Written above the claim's key in a merged file."""

ABSORBED_COMMENT = (
    "{names} {verb} repeated from the shared values file, and must be: a helm\n"
    "list does not merge across the parent/child values merge, it replaces.\n"
    "This service declared no `{key}` of its own before, so it inherited the\n"
    "shared one wholesale; declaring one here takes that over completely.\n"
)
"""Written above a list the merge absorbed the shared file's entries into.

The same sentence a human wrote by hand on `p47-services` after working it out
the expensive way, which is the argument for podbench writing it instead."""

FSGROUP_COMMENT = (
    "Not optional. The claim is created root:root and the application does not\n"
    "run as root; without this the mount path is present and unwritable, which\n"
    "is worse than absent - everything starts, then fails.\n"
)
"""Written above ``podSecurityContext`` in a merged file."""


def _already_said(text: str, rendered: str) -> bool:
    """Whether *rendered* already carries the comment *text*.

    Asked of the **rendered result**, which is the only reliable place to ask.
    Neither end of the round trip is reliable on its own: ruamel attaches a
    comment it parsed to the node *before* it, so the key a comment sits above
    has nothing on it to find - and replacing a key's value drops a comment that
    had been parsed onto that value, so a comment present in the input can be
    missing from the output. Rendering settles both.

    Matched on the first line with anything on it, which identifies one of ours
    and survives a user reflowing the rest.

    This is what makes re-emitting safe, and re-emitting is a normal thing to do
    - after a chart bump, or to see what is deployed - so the second run reads
    the first run's output. The values were always idempotent; the comments were
    not, and appended a fresh copy of themselves on every run. Found running the
    command by hand against p47 the day after the first run, not by the suite:
    the test compared parsed values, which cannot see a comment at all.
    """
    marker = next((line for line in text.splitlines() if line.strip()), "")
    return bool(marker) and marker in rendered


def _comment_before(scope: Any, key: str, text: str, indent: int) -> None:
    """Attach *text* above *key*, or do nothing if the node cannot carry it."""
    if not isinstance(scope, CommentedMap) or key not in scope:
        return
    scope.yaml_set_comment_before_after_key(  # pyright: ignore[reportUnknownMemberType]
        key, before=text, indent=indent
    )


def _comment_before_entry(entries: Any, name: str, text: str, indent: int) -> None:
    """Attach *text* above the list entry called *name*, if it is there."""
    if not isinstance(entries, CommentedSeq):
        return
    for index, entry in enumerate(cast(list[Any], entries)):
        if isinstance(entry, dict) and cast(dict[str, Any], entry).get("name") == name:
            entries.yaml_set_comment_before_after_key(  # pyright: ignore[reportUnknownMemberType]
                index, before=text, indent=indent
            )
            return


def _entry_names(entries: Any) -> list[str]:
    return [
        str(cast(dict[str, Any], entry)["name"])
        for entry in _as_list(entries)
        if isinstance(entry, dict) and "name" in cast(dict[str, Any], entry)
    ]


@dataclass(frozen=True)
class _ListMerge:
    """One list key merged, and the two things the caller has to say about it."""

    entries: Any
    absorbed: Sequence[str]
    declared_first_time: bool


def _merge_list(existing: Any, ours: Any, inherited: Any) -> _ListMerge:
    """Three sources, strict precedence, matched on ``name``.

    What the service already declares wins, then what it would otherwise have
    inherited, then podbench's own. ``name`` is what Kubernetes matches volumes
    and mounts on, so running the merge twice is a no-op rather than a
    duplicate.
    """
    absorbed: Sequence[str] = ()
    if existing is not None:
        base = existing
    elif inherited is not None:
        base = _strip_comments(deepcopy(inherited))
        absorbed = _entry_names(inherited)
    else:
        base = CommentedSeq()
    taken = set(_entry_names(base))
    for entry in _as_list(ours):
        name = (
            cast(dict[str, Any], entry).get("name") if isinstance(entry, dict) else None
        )
        if name is not None and str(name) in taken:
            continue
        cast(list[Any], base).append(entry)
    return _ListMerge(base, absorbed, existing is None)


def merged_values(
    document: str,
    snippet: str,
    *,
    parent: str | None = None,
    under: Sequence[str] | None = None,
    claim_key: str = SUBCHART_VALUES_KEY,
    volumes_key: str = "volumes",
    mounts_key: str = "volumeMounts",
    security_key: str = "podSecurityContext",
    path: str = "the values file",
    parent_path: str = "the shared values file",
) -> tuple[str, list[str]]:
    """*document*, whole, with *snippet*'s keys merged into it.

    Pure, and deliberately: strings in, a string and its notes out. Reading the
    files is a wrapper's job for the same reason :func:`values_snippet` takes no
    cluster - it is what makes every shape of this assertable without a
    filesystem or a kubeconfig.

    This is the whole of #192. Read from a live pod, a chart-generated volume
    and one the service declared for itself are indistinguishable, so
    ``--from-pod`` can only warn and ask for a hand-merge
    (:data:`EXISTING_MOUNTS_WARNING`). Read from the values file it is
    decidable: the file says exactly what the service declares, and everything
    else came from the chart.

    The claim's key is set at the **top level** whatever *under* says, because
    it is a subchart's values and helm looks for those by chart name at the root.
    Everything else goes wherever :func:`locate_keys` finds the chart keeps them.
    """
    target = _load_yaml(document)
    inherited = _load_yaml(parent) if parent is not None else None
    ours = cast(dict[str, Any], _strip_comments(_load_yaml(snippet)))
    notes: list[str] = []

    # Every comment podbench writes is deferred until the tree has been rendered
    # once, and then written only where the render did not already carry it.
    # See :func:`_already_said` - this is what makes a second run a no-op.
    pending: list[tuple[str, Callable[[], None]]] = []

    claim, claim_key = _take_claim(ours, claim_key)
    if claim is not None:
        target[claim_key] = claim
        pending.append(
            (
                CLAIM_COMMENT,
                lambda key=claim_key: _comment_before(target, key, CLAIM_COMMENT, 0),
            )
        )

    if under is not None:
        where, why = tuple(under), "where --values-under put them"
    else:
        where, why = locate_keys(target, list(ours), inherited)
    notes.append(MERGED_UNDER_NOTE.format(where=".".join(where) or TOP_LEVEL, why=why))

    scope: dict[str, Any] = target
    for step in where:
        if not isinstance(scope.get(step), dict):
            scope[step] = CommentedMap()
        scope = cast(dict[str, Any], scope[step])
    from_parent = _mapping_at(inherited, where) if inherited is not None else None

    for key, value in ours.items():
        if key in (volumes_key, mounts_key):
            merge = _merge_list(
                scope.get(key),
                value,
                None if from_parent is None else from_parent.get(key),
            )
            scope[key] = merge.entries
            if key == volumes_key:
                pending.append(
                    (
                        SEAT_HOME_COMMENT,
                        lambda entries=merge.entries: _comment_before_entry(
                            entries,
                            SEAT_HOME_VOLUME,
                            SEAT_HOME_COMMENT,
                            2 * len(where) + 4,
                        ),
                    )
                )
            if merge.absorbed:
                notes.append(
                    ABSORBED_FROM_PARENT_NOTE.format(
                        key=key, parent=parent_path, names=", ".join(merge.absorbed)
                    )
                )
                absorbed_comment = ABSORBED_COMMENT.format(
                    names=", ".join(merge.absorbed),
                    verb="is" if len(merge.absorbed) == 1 else "are",
                    key=key,
                )
                pending.append(
                    (
                        absorbed_comment,
                        lambda k=key, t=absorbed_comment: _comment_before(
                            scope, k, t, 2 * len(where)
                        ),
                    )
                )
            elif merge.declared_first_time and inherited is None:
                notes.append(NO_PARENT_NOTE.format(path=path, key=key))
        elif isinstance(value, dict) and isinstance(scope.get(key), dict):
            # podSecurityContext, and a probe the service already has opinions
            # inside: ours adds to it rather than replacing the lot.
            for inner, deeper in cast(dict[str, Any], value).items():
                cast(dict[str, Any], scope[key])[inner] = deeper
        else:
            scope[key] = value

    pending.append(
        (
            FSGROUP_COMMENT,
            lambda: _comment_before(
                scope, security_key, FSGROUP_COMMENT, 2 * len(where)
            ),
        )
    )

    rendered = _dump_yaml(target)
    written = False
    for text, write_it in pending:
        if not _already_said(text, rendered):
            write_it()
            written = True
    return (_dump_yaml(target) if written else rendered), notes


def _take_claim(ours: dict[str, Any], claim_key: str) -> tuple[Any, str]:
    """Lift the claim's own half out of the snippet, whichever route emitted it.

    ``values_snippet`` emits either the subchart's key or the central
    ``hotfixProject``, and the merge has to put whichever it got at the root
    rather than under the chart's key. The central shape is recognised by its
    ``claims`` list rather than by name, so renaming it does not lose it.
    """
    if claim_key in ours:
        return ours.pop(claim_key), claim_key
    for key in list(ours):
        value = ours[key]
        if isinstance(value, dict) and "claims" in cast(dict[str, Any], value):
            return ours.pop(key), str(key)
    return None, claim_key


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


def _read_values_file(path: str, what: str) -> str:
    """*path*'s text, or a refusal naming what it was for.

    Read here and not in :func:`merged_values` for the reason the pod is read in
    :func:`_read_values_from_pod` and not in :func:`values_snippet`: the
    thing that decides the output has to be assertable with no filesystem.
    """
    try:
        return Path(path).read_text()
    except OSError as exc:
        _values_failure(f"could not read {what} {path}: {exc}")


def _merge_values_into_file(
    snippet: str,
    *,
    values: str,
    parent_values: str | None,
    values_under: str | None,
    claim_key: str,
) -> str:
    """``--values``: the target's own file, whole, with the snippet merged in.

    A wrapper, exactly as ``--from-pod`` is. Notes go to stderr and the file
    goes to stdout, so the output stays something a shell can redirect over the
    file it came from.
    """
    merged, notes = merged_values(
        _read_values_file(values, "the values file"),
        snippet,
        parent=(
            None
            if parent_values is None
            else _read_values_file(parent_values, "the shared values file")
        ),
        under=None if values_under is None else values_under.split("."),
        claim_key=claim_key,
        path=values,
        parent_path=parent_values or "the shared values file",
    )
    for note in notes:
        emit(_laid_out(note), stderr=True)
    return merged


# -- pre-flight -------------------------------------------------------------


class CheckStatus(Enum):
    """How one pre-flight check came out.

    Three tokens rather than ``status``'s two, because the distinction
    ``doctor`` draws is the one ``check`` needs and it is the exit code: a
    ``FAIL`` stands between this target and a working ``hotfix init``, a
    ``WARN`` does not. The spellings are :mod:`podbench.console`'s - that module
    is what colours every report podbench prints, and a token it has not been
    taught is left uncoloured - rather than a second vocabulary for a reader of
    this one to learn.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "FAIL"


@dataclass(frozen=True)
class PreflightCheck:
    """One prerequisite of hotfix mode, and what was measured for it."""

    name: str
    status: CheckStatus
    detail: str

    @property
    def blocks(self) -> bool:
        """Whether this is a state ``init`` would refuse on.

        >>> PreflightCheck("liveness", CheckStatus.WARN, "").blocks
        False
        >>> PreflightCheck("claim", CheckStatus.FAIL, "").blocks
        True
        """
        return self.status is CheckStatus.FAIL


CLAIM_NOT_MOUNTED = (
    "{checkout} is not present: the claim is not mounted. Run `podbench hotfix "
    "values` and deploy the five values it emits."
)
"""Said by ``init`` when the claim is not there, and by ``check`` before it.

One constant and not two. ``check``'s whole job is to say ``init``'s refusals
earlier, and a second copy of this sentence is how the two come to describe
different remedies for one state.
"""

IMAGE_HAS_NO_PROJECT = (
    "the target's image has no project at {image_project}, so there is nothing "
    "to seed the claim from. Name the paths this image really uses with "
    "`--image-project PATH` and `--image-interpreter PATH`, or the container "
    "you meant with `--container NAME`."
)
"""Said when the application container answers that the project is not there.

Measured in the application container itself, beside
:func:`require_supervisor`'s own probe, so it is a statement about the image's
*layout* and nothing else. :data:`TARGET_HAS_NO_PROJECT` is the same finding
reached the other way round - through the seat's ``/proc/1/root`` - and says
"{root} lists" because that is what it measured; this one has not looked at the
seat and must not borrow the claim.

Like that message it names neither ptrace nor ``doctor``. Both are the false
trail the old wording opened (#178), and a message that mentions a mechanism at
all - even to rule it out - is one a reader will go and chase.

Its remedy sentence names the same three flags in the same order and the same
words as :data:`TARGET_HAS_NO_PROJECT`'s, for the reason
:data:`CLAIM_NOT_MOUNTED` is one constant and not two: ``check``'s job is
to say ``init``'s refusals earlier, in ``init``'s terms. Two spellings of one
remedy read as two different fixes to the reader who meets them an hour apart.
Not merged into one constant, because the halves that differ - "{root} lists"
here and not there - are the measurements each one actually made.
"""

IMAGE_HAS_NO_INTERPRETER = (
    "the target's image has no interpreter at {image_interpreter}, and the seed "
    "copies one onto the claim: a rebuilt venv's console scripts carry an "
    "absolute shebang, so an interpreter left behind in the image is one the "
    "next restart takes away. `--image-interpreter PATH` names the layout this "
    "image has."
)
"""Said when the interpreter half of :func:`seed_source` is not where it looked.

``init`` refuses a missing project in its own words and a missing interpreter in
nobody's: the copy is a ``cp -a`` that simply fails, ~50s into the seed and in
kubectl's sentence rather than podbench's. That is the round trip this verb
exists to save, so the second path is asked in the same breath as the first.
"""

NON_EXEC_PROBE_BLOCKS_THE_HOLD = (
    "a {kind} livenessProbe cannot be short-circuited by the hold - only an exec "
    "probe can - so the kubelet will restart the pod out from under the seat at "
    "failureThreshold x periodSeconds. Deal with that before `hotfix apply` "
    "holds this pod."
)
"""Said when the target's probe is real, readable, and of no use to hold mode.

A ``WARN`` and not a ``FAIL`` because ``init`` accepts such a target: what this
breaks is ``apply``'s hold, later, and the exit code is reserved for what stops
the next command.

Separate from :data:`NON_EXEC_PROBE_WARNING`, which says the same finding to
somebody reading an emitted snippet and carries a second half about the block
not being emitted. There is no block here, and a reader of this report has not
asked for one.
"""

CLAIM_ALREADY_SEEDED = (
    "{container} mounts {volume} at {checkout}, and it already carries a "
    "project: `hotfix init` seeds nothing over one, so the target root, the "
    "project and the interpreter are not asked."
)
"""Why three rows are not measured on a claim that has already been seeded.

``init`` short-circuits its entire seed on ``{checkout}/pyproject.toml``, which
makes the target root, the image's project and the image's interpreter moot -
so a ``check`` that measured them anyway would refuse a target ``init``
accepts, which is the second half of #205 item 3's falsification.

Said once, here, on the row that measured it; the three rows beneath carry the
short form. The mechanism belongs to the fact and not to each of its
consequences - three copies of this sentence is fifteen lines of identical
prose in the middle of a nine-line report.
"""

SEAT_NOT_RUNNING = (
    "--seat {seat} names no running container in {pod}: every claim read hotfix "
    "mode makes goes through it, so the next command would fail on kubectl's "
    "refusal to exec into it. Drop the flag to use the seat podbench finds, or "
    "name one that is running."
)
"""Said when ``--seat`` names something the pod's statuses do not.

Corroborated rather than restated. ``--seat`` is taken at its word by
:func:`seat_container` - which is what it is for - but a report that answered
``[ok] {seat} is running`` for a name nobody landed would be restating the
request as a measurement, and the row it then breaks is ``target root``: a seat
that is not there cannot list ``/proc/1/root``, so the reader is sent to
CAP_SYS_PTRACE and ``doctor`` for a typo. #178's false trail, reached by a new
route.
"""

_TEST_SAID_NO = 1
"""``test``'s exit code for a predicate that is false.

Distinguished from 126 and 127 - ``test`` itself not running, which on a
distroless application container is a real possibility - because those are not
an answer about the image's layout and must not be reported as one.
"""


def _target_check(target: HotfixTarget) -> PreflightCheck:
    where = f"{target.pod.name}, container {target.container}"
    return PreflightCheck(
        "target",
        CheckStatus.OK,
        f"{where}, {target.workload}" if target.workload else where,
    )


def _claim_check(target: HotfixTarget, seeded: bool) -> PreflightCheck:
    if target.claim_mount == HOTFIX_APP_PATH:
        mounts = f"{target.container} mounts {HOTFIX_CLAIM_VOLUME} at {HOTFIX_APP_PATH}"
        if seeded:
            mounts = CLAIM_ALREADY_SEEDED.format(
                container=target.container,
                volume=HOTFIX_CLAIM_VOLUME,
                checkout=HOTFIX_APP_PATH,
            )
        return PreflightCheck("claim", CheckStatus.OK, mounts)
    if not target.claim_mount:
        return PreflightCheck(
            "claim",
            CheckStatus.FAIL,
            CLAIM_NOT_MOUNTED.format(checkout=checkout_path()),
        )
    # A claim mounted elsewhere is a refusal and not a note, because everything
    # the seed writes names the default path - see VENV_INVISIBLE_TO_STATUS,
    # whose last sentence is the one that applies here.
    return PreflightCheck(
        "claim",
        CheckStatus.FAIL,
        VENV_INVISIBLE_TO_STATUS.format(
            venv=target.claim_mount, default=HOTFIX_APP_PATH
        ),
    )


def _claim_seeded(kube: Kubectl, target: HotfixTarget) -> bool:
    """Whether the claim already carries a project, asked where ``init`` asks.

    Exactly :func:`init`'s own predicate — ``{checkout}/pyproject.toml`` — and
    it is the one that decides how much of the seed happens at all. See
    :data:`CLAIM_ALREADY_SEEDED` for why measuring the three rows underneath it
    anyway makes ``check`` refuse targets ``init`` accepts.

    A claim mounted anywhere else is already a blocker, and its rows are read
    against the default path, so it is not asked there.
    """
    if target.claim_mount != HOTFIX_APP_PATH:
        return False
    probe = kube.exec_(
        target.pod.name,
        ["test", "-e", f"{checkout_path()}/pyproject.toml"],
        container=target.container,
        check=False,
    )
    return probe.returncode == 0


def _supervisor_check(kube: Kubectl, target: HotfixTarget) -> PreflightCheck:
    try:
        require_supervisor(kube, target)
    except HotfixError as error:
        return PreflightCheck("supervisor", CheckStatus.FAIL, str(error))
    return PreflightCheck(
        "supervisor",
        CheckStatus.OK,
        f"{target.container} is running it: {HOTFIX_CHILD_PID_PATH} exists",
    )


def _image_path_check(
    kube: Kubectl, target: HotfixTarget, name: str, path: str, absent: str
) -> PreflightCheck:
    """Whether the image keeps something at *path*, asked in the target itself.

    ``test -d`` in the application container rather than ``ls`` through the
    seat's ``/proc/1/root``, because this is a question about the *image* and
    the two are different questions - which is the whole of #178. It also needs
    no seat, so the layout refusal that costs an attach and a round trip is the
    one this verb can answer before either.
    """
    probe = kube.exec_(
        target.pod.name, ["test", "-d", path], container=target.container, check=False
    )
    if probe.returncode == 0:
        return PreflightCheck(name, CheckStatus.OK, f"the image keeps one at {path}")
    if probe.returncode == _TEST_SAID_NO:
        return PreflightCheck(name, CheckStatus.FAIL, absent)
    return PreflightCheck(
        name,
        CheckStatus.WARN,
        f"not measured: `test -d {path}` in {target.container} exited "
        f"{probe.returncode}, which is `test` not running rather than an answer "
        "about the image.",
    )


def _running_containers(pod_json: Mapping[str, Any]) -> set[str]:
    """Every container of the pod that is running now, ephemeral ones included.

    Ephemeral statuses are where a seat appears, and the ordinary ones are here
    because ``--seat`` may legitimately name a sidecar that mounts the claim.
    """
    status = as_dict(pod_json.get("status"))
    return {
        name
        for key in (
            "containerStatuses",
            "initContainerStatuses",
            "ephemeralContainerStatuses",
        )
        for entry in _as_list(status.get(key))
        if (name := _as_str(as_dict(entry).get("name")))
        and "running" in as_dict(as_dict(entry).get("state"))
    }


def _seat_check(
    target: HotfixTarget, seat: str | None, running: Collection[str]
) -> PreflightCheck:
    if seat is None:
        return PreflightCheck(
            "seat",
            CheckStatus.WARN,
            f"no podbench container is running in {target.pod.name}. Not a "
            "blocker - `hotfix init` lands one itself - but it is why `target "
            "root` below is unmeasured.",
        )
    if seat in running:
        return PreflightCheck("seat", CheckStatus.OK, f"{seat} is running")
    return PreflightCheck(
        "seat",
        CheckStatus.FAIL,
        SEAT_NOT_RUNNING.format(seat=seat, pod=target.pod.name),
    )


def _target_root_check(
    kube: Kubectl, target: HotfixTarget, seat: str | None, container_root: str
) -> PreflightCheck:
    if seat is None:
        return PreflightCheck(
            "target root",
            CheckStatus.WARN,
            f"not measured: listing {container_root} is a property of the seat's "
            "ptrace rung, and this check has no running seat to ask. `podbench "
            f"attach {target.pod.name}` lands one, or let `hotfix init` land it "
            "and answer this on its own.",
        )
    store = PodStore(kube=kube, pod=target.pod.name, container=seat)
    if target_root_readable(store, container_root):
        return PreflightCheck(
            "target root",
            CheckStatus.OK,
            f"{seat} can list {container_root}, so the seed can read the target",
        )
    return PreflightCheck(
        "target root",
        CheckStatus.FAIL,
        TARGET_ROOT_UNREADABLE.format(root=container_root),
    )


def _liveness_check(pod_json: Mapping[str, Any], container: str) -> PreflightCheck:
    absent: dict[str, Any] = {}
    spec = next(
        (
            as_dict(entry)
            for entry in _as_list(as_dict(pod_json.get("spec")).get("containers"))
            if _as_str(as_dict(entry).get("name")) == container
        ),
        absent,
    )
    declared = as_dict(spec.get("livenessProbe"))
    if not declared:
        # Never a fault: the canonical fastcs target declares no probe, and 7 of
        # 18 containers on a real beamline do.
        return PreflightCheck(
            "liveness", CheckStatus.OK, "no livenessProbe, so nothing cuts a hold short"
        )
    if probe_exec_command(declared):
        return PreflightCheck(
            "liveness",
            CheckStatus.OK,
            "an exec probe, which `hotfix values` wraps to honour the hold",
        )
    kind = next(
        (k for k in ("httpGet", "tcpSocket", "grpc") if k in declared), "non-exec"
    )
    return PreflightCheck(
        "liveness", CheckStatus.WARN, NON_EXEC_PROBE_BLOCKS_THE_HOLD.format(kind=kind)
    )


def _source_check(target: HotfixTarget, repo: str | None) -> PreflightCheck:
    """Whether anything names the repository ``init`` is going to clone.

    A blocker, and ``--repo`` is asked here because ``init`` asks it: that verb
    refuses outright when neither the flag nor the image's label names one
    (:data:`NO_SOURCE_REPO`), and it refuses *before* the seed, so a ``check``
    that passed the state would be sending the reader into precisely the second
    attempt this verb exists to remove. Hearing the flag is the other half:
    without it this row would refuse a target ``init --repo URL`` accepts.

    The registry is not read at all when the flag answers it - a round trip on
    the way into an emergency, for a value already in hand, which is the
    ordering ``init`` uses too.

    **A bare non-empty label is not an answer** (measured 2026-08-23, evidence
    ``phase7-the-contradiction.md`` §6/§7.1). OCI labels are inherited, and the
    target this mode was proved against advertises its base image's repository,
    revision and title throughout, so an ``[ok]`` on the label alone green-lit
    cloning another project entirely. Four states, and the row says which one
    it is in:

    * ``--repo`` was given - independent of the image by construction, ``OK``;
    * no label and no flag - ``init`` refuses, ``FAIL``;
    * a label the image's own name corroborates
      (:func:`image_name_agrees`) - ``OK``, naming what corroborated it;
    * a label nothing corroborates - ``WARN``, because ``init`` proceeds.

    The two corroborators it does **not** take are named here so the row cannot
    be read as having taken them: the seeded checkout's ``origin``, which
    :func:`init` reads and ``check`` cannot without a seat, and the manifest's
    own ``repo``, which exists only once ``init`` has run. Neither is available
    in the state this verb is written for - an unseeded claim, before starting.
    """
    named = (repo or "").strip()
    if named:
        return PreflightCheck("source", CheckStatus.OK, f"--repo names {named}")
    reference = target.image_digest or target.image
    labels = read_image_labels(reference) or {}
    source = (labels.get(SOURCE_LABEL) or "").strip()
    if not source:
        return PreflightCheck(
            "source", CheckStatus.FAIL, NO_SOURCE_REPO.format(label=SOURCE_LABEL)
        )
    ref = parse_reference(reference)
    # The repository without its tag or digest: that is what the label is
    # compared against, and naming the whole reference spends two lines of the
    # row on a version nothing here read.
    named_image = f"{ref.registry}/{ref.repository}" if ref is not None else reference
    if image_name_agrees(reference, source):
        return PreflightCheck(
            "source",
            CheckStatus.OK,
            f"the image names {source}, which its own repository "
            f"{named_image} corroborates - an inherited label would not",
        )
    return PreflightCheck(
        "source",
        CheckStatus.WARN,
        SOURCE_LABEL_UNCORROBORATED.format(image=named_image, source=source),
    )


def preflight(
    kube: Kubectl,
    reference: str,
    *,
    container: str | None = None,
    seat: str | None = None,
    repo: str | None = None,
    image_project: str = IMAGE_PROJECT_PATH,
    image_interpreter: str = IMAGE_INTERPRETER_PATH,
    container_root: str = "/proc/1/root",
) -> list[PreflightCheck]:
    """Every prerequisite hotfix mode has, asked in one pass instead of six.

    Each was discovered serially and at the moment it bit: no supervisor, no
    claim, a second replica, a root the seat cannot read, no project where
    podbench looked, a probe the hold cannot short-circuit. Every one of those
    costs a chart change and a redeploy, in an emergency, one per attempt
    (#205 item 3). Nothing here is a new measurement - each row is the function
    that already enforces it, asked early and caught rather than raised.

    It asks ``init``'s own questions in ``init``'s own order of relevance, which
    is why it takes ``--repo`` and why it asks whether the claim is seeded
    before anything about the image: both are conditions ``init`` evaluates, and
    a row measured where ``init`` does not look is a row that can disagree with
    it. See :data:`CLAIM_ALREADY_SEEDED` and :func:`_source_check`.

    One thing it deliberately does not decide, because it is not a fact about
    the target: the **seat's view of the target root**, when no seat is running.
    Whether one will be able to list ``/proc/1/root`` is a property of a
    container that does not exist yet, so it is reported *unmeasured* rather
    than guessed at either way - and the verdict says "nothing measured here"
    for exactly that reason.

    What is left to ``init`` is what only performing the seed can find: an
    ``attach`` the cluster refuses, or an interpreter directory the copy lands
    and :func:`seeded_python` then finds nothing usable inside. A pre-flight
    that claimed those would be claiming to have run the seed.
    """
    try:
        target, pod_json = _resolve_target(kube, reference, container=container)
    except HotfixError as error:
        # The whole report rather than its first row: nothing below can be
        # measured without a pod, and eight "not measured" lines under one real
        # refusal would bury it.
        return [PreflightCheck("target", CheckStatus.FAIL, str(error))]
    found = running_seat(pod_json)
    running = _running_containers(pod_json)
    named = seat if seat is not None else (None if found is None else found.name)
    # A seat that is not running is no seat to measure through: exec'ing into it
    # would fail, and TARGET_ROOT_UNREADABLE is the ptrace false trail this
    # report must not lay for what is a mistyped --seat.
    usable_seat = named if named in running else None
    seeded = _claim_seeded(kube, target)
    return [
        _target_check(target),
        _claim_check(target, seeded),
        _supervisor_check(kube, target),
        _seat_check(target, named, running),
        *_seed_checks(
            kube,
            target,
            seeded=seeded,
            seat=usable_seat,
            container_root=container_root,
            image_project=image_project,
            image_interpreter=image_interpreter,
        ),
        _liveness_check(pod_json, target.container),
        _source_check(target, repo),
    ]


def _seed_checks(
    kube: Kubectl,
    target: HotfixTarget,
    *,
    seeded: bool,
    seat: str | None,
    container_root: str,
    image_project: str,
    image_interpreter: str,
) -> list[PreflightCheck]:
    """The three rows that only a seed reads, and which a seeded claim retires.

    ``init`` short-circuits the whole seed on a claim that already carries a
    project, so on an already-hotfixed pod - the state ``check`` is most likely
    to be run in a second time - these three are not measured at all rather
    than measured and reported as blockers ``init`` would never raise.
    """
    if seeded:
        # Short, because the `claim` row above says why (CLAIM_ALREADY_SEEDED)
        # and a mechanism is said once.
        moot = f"not asked: {checkout_path()} is already seeded"
        return [
            PreflightCheck(name, CheckStatus.OK, moot)
            for name in ("target root", "project", "interpreter")
        ]
    return [
        _target_root_check(kube, target, seat, container_root),
        _image_path_check(
            kube,
            target,
            "project",
            f"/{image_project.strip('/')}",
            IMAGE_HAS_NO_PROJECT.format(image_project=f"/{image_project.strip('/')}"),
        ),
        _image_path_check(
            kube,
            target,
            "interpreter",
            f"/{image_interpreter.strip('/')}",
            IMAGE_HAS_NO_INTERPRETER.format(
                image_interpreter=f"/{image_interpreter.strip('/')}"
            ),
        ),
    ]


_CHECK_NAMES = 14
"""Width of the name column, which is ``doctor._NAMES`` because the two reports
are the same shape and are read the same way. Every name is well short of it, so
the two spaces that make a row a row are never spent down to one - which is what
would leave half the names bold and half plain."""


def format_preflight(checks: Sequence[PreflightCheck]) -> str:
    """The pre-flight report: one line per check, then the verdict.

    Shaped like ``doctor``'s, down to the column: the facts first so a reader
    can see what was measured, then a verdict that names the blockers rather
    than summarising them away. Only the detail is wrapped, and it hangs under
    itself rather than under the name, so a wrapped one cannot be read as the
    next check's.
    """
    lines: list[str] = []
    for check in checks:
        lead = (
            f"  [{check.status.value}]".ljust(_FLAG) + f"{check.name:<{_CHECK_NAMES}} "
        )
        lines.extend(paragraph(check.detail, first=lead, indent=" " * len(lead)))
    lines.append(rule(char="-"))
    blockers = [check.name for check in checks if check.blocks]
    if blockers:
        count = len(blockers)
        lines.append(
            f"VERDICT: {count} blocker{'' if count == 1 else 's'} before "
            "`podbench hotfix init` can work (exit 1)"
        )
        lines.extend(paragraph(", ".join(blockers), first="BLOCKERS: "))
    else:
        lines.append(
            "VERDICT: nothing measured here blocks `podbench hotfix init` (exit 0)"
        )
    return "\n".join(lines)


# -- retirement -------------------------------------------------------------


HOTFIX_TARGET_LABEL = "podbench.dev/hotfix-target"
"""The label both claim charts put the application's name in.

``Charts/podbench-hotfix-claim/templates/pvc.yaml`` sets it from the release
name and ``Charts/podbench/templates/pvc-hotfix-project.yaml`` from the claim
entry, so it is the one thing a claim carries that says what it is *for*. It is
how :func:`retire` finds the claim once the pod has been unwired: a pod that no
longer declares the volume no longer names the claim either, and deriving
``<app>-podbench-project`` from a container name would be a guess about somebody
else's values file.
"""


class RetirementStep(Enum):
    """What retiring a hotfix consists of, in the order the steps happen.

    Four rather than :func:`_retirement_checklist`'s five, because these are
    the four a cluster can be *asked* about. Opening the PR and merging it
    leave no trace podbench can read; rolling the image, unwiring the pod and
    deleting the claim each do.
    """

    BRANCH = "branch"
    """The fix is on a branch, so the claim is no longer its only copy."""

    IMAGE = "image"
    """The deployed image has moved on from the one the hotfix was made against."""

    WIRING = "wiring"
    """The pod no longer carries the volume, the mount and the supervisor."""

    CLAIM = "claim"
    """The claim itself is gone."""


@dataclass(frozen=True)
class RetirementCheck:
    """One step of retirement, and what was measured for it."""

    step: RetirementStep
    done: bool | None
    """``True`` measured done, ``False`` measured outstanding, ``None``
    unmeasured — which is never "done" (#205 item 4's falsification: a
    retirement that lies is worse than the checklist it replaces)."""

    detail: str

    @property
    def flag(self) -> str:
        """``[x]`` only for a step that was measured done.

        ``attach``'s two tokens rather than a third spelling for unmeasured:
        :mod:`podbench.console` colours the ones it has been taught and leaves
        an invented one plain, and the distinction this report must not blur is
        *ticked or not* — an unmeasured step is not ticked, and its detail says
        why.

        >>> RetirementCheck(RetirementStep.CLAIM, None, "").flag
        '[ ]'
        >>> RetirementCheck(RetirementStep.CLAIM, True, "").flag
        '[x]'
        """
        return "[x]" if self.done else "[ ]"

    @property
    def remaining(self) -> bool:
        """Whether this step was measured and is not done.

        The exit code is this and not ``not done``: an unmeasured step is not
        an assertion in either direction, and a ``retire`` that exited 1 for
        something it never looked at would be unusable in the shutdown
        checklist ``status`` already serves.

        >>> RetirementCheck(RetirementStep.CLAIM, None, "").remaining
        False
        >>> RetirementCheck(RetirementStep.CLAIM, False, "").remaining
        True
        """
        return self.done is False


WIRING_IS_THE_APPLICATIONS_OWN = (
    "{pod} still carries {what}. Those are fields in the application's own pod "
    "template, not in the claim's chart, so turning the claim off does not "
    "remove them: take those entries back out of the application's values and "
    "redeploy - the entries, and not the whole `volumes` and `volumeMounts` "
    "keys, which a helm list replaces rather than merges."
)
"""Said when the pod is still wired for hotfix mode.

Measured, and it is the state this verb exists for (p47-beamline, 2026-08-23):
`p47-services` carried ``podbench-hotfix-claim.enabled: false``, every pod in
the namespace was deleted, and ``bl47p-ea-fastcs-01-0`` came back still mounting
the claim and still running the supervisor loop. The boolean disables the
*subchart* — the PVC — while ``volumes``, ``volumeMounts``, ``args`` and
``podSecurityContext`` live in the target's own ``ioc-instance`` values and are
untouched. Somebody who believes they turned hotfix mode off has done step 5 and
not step 4, and nothing until now said so.

The closing clause used to read "take them back out of the values `podbench
hotfix values` emitted". Re-measured 2026-08-24, that is hazardous rather than
merely loose: ``values --from-pod`` emits podbench's own ``volumes`` and
``volumeMounts`` entries only, while the deployed values carry ``beamline-data``
in both keys - repeated there on purpose, because a helm list *replaces* across
the parent/child merge. Somebody who deleted the two keys wholesale would
unmount the beamline directory. :data:`EXISTING_MOUNTS_WARNING` says exactly
this on the way in, and nothing said it on the way out.
"""

FSGROUP_NOT_ATTRIBUTED = (
    "podSecurityContext.fsGroup is {gid}, which `hotfix values` emits too; "
    "whether this pod had one before the hotfix is not measured here, so check "
    "it against the values before taking it out."
)
"""The fifth value hotfix mode wires, and the one the pod cannot attribute.

The ``wiring`` row named three things and six values were wired (evidence
§7.3). Four of them are podbench's by construction and are read off the pod:
the two volumes, the mount and the supervisor's ``command``/``args``. An
``fsGroup`` is not - an application may perfectly well have declared its own -
so it is *named* rather than counted, which is the difference between finishing
somebody's checklist and inventing an item for it.
"""

CLAIM_OUTLIVES_THE_FLIP = (
    "{claim} still exists. It is annotated Prune=false,Delete=false so that a "
    "hotfix survives somebody reverting a repoint mid-beamtime, which means "
    "turning the claim off leaves the object standing: deleting it is a "
    "separate, deliberate act (`podbench hotfix retire {reference} "
    "--delete-claim`)."
)
"""Said when the claim is still there.

The annotation is not decoration and the sentence must keep saying so: a claim
carrying Helm's ``keep`` alone was pruned about three minutes after it left the
desired state, and its ``Delete``-reclaim PV went with it (#190,
`.claude/evidence/phase1-prune-on-sync.md`). The pair is what makes retirement
an act rather than a side effect of a sync — and therefore a step somebody has
to take.
"""

CLAIM_STILL_MOUNTED = (
    "still mounted by {pods}, so it was not deleted. A claim deleted out from "
    "under a running pod stays Terminating while that pod holds it and then "
    "fails to bind on the next reschedule, so the wiring comes out of the "
    "application's values first."
)
"""Why ``--delete-claim`` declines on a claim something still mounts.

The refusal is reported on the claim's own row rather than raised, because the
report is the point of the verb: the reader needs to see which step they are
actually on, not an error about the one they asked for.
"""

CLAIM_MOUNTERS_UNREADABLE = (
    "not deleted: this namespace's pods could not be listed, so whether "
    "anything still mounts the claim is unmeasured - and that is the one thing "
    "that makes deleting it safe."
)
"""Why ``--delete-claim`` declines when it could not ask who holds the claim.

Unmeasured, and therefore no. The deletion is irreversible and its only
precondition is a *negative* one, which is the shape that a failed read turns
into a false yes if the caller treats "found no mounters" and "could not look"
as the same answer.
"""

CLAIM_GONE_BUT_STILL_WIRED = (
    "{pod} mounts {claim} and no such claim exists. The pod is running only "
    "because it was scheduled while the claim was still there; the next "
    "reschedule will not bind. Take the wiring out of the application's values, "
    "or put the claim back."
)
"""The half-retired state that is worse than either end of the checklist.

Named as its own condition because it is silent until a reschedule, and because
it is what "delete the claim" does when it is done *before* the values change
rather than after. Reported on the ``wiring`` row: the claim being gone is not
the fault, the pod still asking for it is.
"""

CLAIM_CONTENTS_UNVERIFIED = (
    "deleted. Nothing mounted it, so its manifest could not be read first and "
    "what was on it is unverified. If the chart still declares the claim - "
    "`{key}.enabled: false` is what stops that - the next sync recreates it."
)
"""Said on the path that deletes, about the deletion it just made.

Both halves are caveats about a mutation, which is why they are here rather
than in the help: once the pod is unwired nothing can read the claim, so
provenance is unmeasurable exactly when the deletion becomes safe; and a claim
deleted while its chart still renders it comes straight back, which would
otherwise read as podbench's delete having failed.
"""

STATUS_DOES_NOT_READ_CLAIMS = (
    "not measured: `hotfix status` reads pods, and asking every namespace for "
    "its claims as well is a call per row. `podbench hotfix retire` reads it."
)
"""Why ``status``'s retirement summary leaves the claim unmeasured.

Not "the claim is there". The pod in front of it names a claim, but a pod goes
on running perfectly well after its claim is deleted - see
:data:`CLAIM_GONE_BUT_STILL_WIRED` - so a listing that inferred the claim from
the mount would report the one state that most needs finding as normal.
"""

CLAIM_NOT_LABELLED_FOR_THIS_TARGET = (
    "not measured: no claim in {namespace} carries {label} for {names}, and "
    "this pod declares none. The label is set from "
    "`hotfixProject.claims[].name`, which nothing requires to match the "
    "container, the workload or the pod, so that is an answer about labels and "
    "not about the claim: `kubectl -n {namespace} get pvc` is the read podbench "
    "cannot narrow."
)
"""Why an empty label listing leaves the claim unmeasured rather than gone.

The one step of retirement that cannot be undone, so it is ticked only off a
measurement of the claim. ``Charts/podbench/templates/pvc-hotfix-project.yaml``
labels the claim with the ``hotfixProject.claims[]`` entry's own ``name``, which
nothing requires to equal the container, the workload or the pod - the subchart
route usually does match, since it labels from the release name, which is
exactly what would make this silent. An empty listing is therefore evidence
about labels, and reading it as "the claim is gone" would report a standing
claim as a completed retirement.
"""


def pod_claim_name(pod_json: Mapping[str, Any]) -> str | None:
    """The claim the pod's hotfix volume names, or ``None`` if it declares none.

    Read from the volume rather than derived from the application's name: the
    name is a value somebody can override (``claimName`` in the subchart), so
    the pod's own spec is the only place it is a measurement.

    >>> pod_claim_name({"spec": {"volumes": [{"name": "podbench-app",
    ...     "persistentVolumeClaim": {"claimName": "api-podbench-project"}}]}})
    'api-podbench-project'
    >>> pod_claim_name({"spec": {"volumes": []}}) is None
    True
    """
    spec = as_dict(pod_json.get("spec"))
    for entry in _as_list(spec.get("volumes")):
        volume = as_dict(entry)
        if _as_str(volume.get("name")) != HOTFIX_CLAIM_VOLUME:
            continue
        return _as_str(as_dict(volume.get("persistentVolumeClaim")).get("claimName"))
    return None


def hotfix_wiring(pod_json: Mapping[str, Any]) -> tuple[str, ...]:
    """Which parts of the hotfix wiring this pod still carries, named.

    Every one of them, and not :func:`podbench.launcher.is_hotfixed`'s single
    verdict: retirement is somebody editing a values file, and a report that
    said "still wired" without saying *which keys* leaves them to diff the pod
    against the snippet by eye.

    The seat's home volume is here because :func:`values_snippet` emits it and
    nothing mounts it, so it is the entry a reader working from the pod's mounts
    alone would leave behind (evidence §7.3). ``command`` is named only where
    the pod actually carries podbench's, since the loop is shell text and a
    chart that supplied its own ``bash -c`` would have it either way.

    >>> hotfix_wiring({"spec": {"volumes": [{"name": "podbench-app"}],
    ...     "containers": [{"name": "app"}]}})
    ('the podbench-app volume',)
    >>> hotfix_wiring({"spec": {"volumes": [{"name": "podbench-home"}],
    ...     "containers": []}})
    ('the podbench-home volume',)
    """
    spec = as_dict(pod_json.get("spec"))
    declared = declared_volumes(pod_json)
    carried: list[str] = []
    for volume in (HOTFIX_CLAIM_VOLUME, SEAT_HOME_VOLUME):
        if volume in declared:
            carried.append(f"the {volume} volume")
    if claim_container(pod_json) is not None:
        carried.append(f"a volumeMount at {HOTFIX_APP_PATH}")
    for entry in _as_list(spec.get("containers")):
        container = as_dict(entry)
        if not runs_hotfix_supervisor(container):
            continue
        command = [word for word in _as_list(container.get("command")) if _as_str(word)]
        carried.append(
            "the supervisor loop in command and args"
            if command == ["bash", "-c"]
            else "the supervisor loop in args"
        )
        break
    return tuple(carried)


def pod_fs_group(pod_json: Mapping[str, Any]) -> int | None:
    """The pod's ``securityContext.fsGroup``, or ``None`` where it declares none.

    >>> pod_fs_group({"spec": {"securityContext": {"fsGroup": 37887}}})
    37887
    >>> pod_fs_group({"spec": {}}) is None
    True
    """
    value = as_dict(as_dict(pod_json.get("spec")).get("securityContext")).get("fsGroup")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class ClaimState:
    """What was found out about the claim itself, ahead of the report.

    A value and not three arguments because the two callers know different
    amounts: ``retire`` reads the object, ``status`` deliberately does not
    (:data:`STATUS_DOES_NOT_READ_CLAIMS`), and a deletion this run performed is
    a third answer again. Keeping them one shape is what stops the row that
    says "gone" from ever being authored by a caller that did not look.
    """

    name: str | None = None
    """The claim's name, where anything names it."""

    present: bool | None = None
    """``None`` is unmeasured, never absent — an RBAC refusal and a 404 differ
    only in kubectl's text."""

    detail: str = ""
    """Said instead of the default for this state: why it is unmeasured, or why
    a ``--delete-claim`` that was asked for did not happen."""

    deleted: bool = False
    """Whether *this run* deleted it, which is the only path that may say so."""


CLAIM_UNMEASURED = ClaimState()
"""The claim as a caller that has not looked at one describes it.

A module-level singleton because a frozen default has to be one, and named
rather than spelled inline so that "nobody asked about the claim" reads as a
deliberate state rather than as an omission.
"""


def retirement(
    pod_json: Mapping[str, Any],
    manifest: HotfixManifest | None,
    *,
    claim: ClaimState = CLAIM_UNMEASURED,
    current_digest: str = "",
    reference: str = "",
) -> list[RetirementCheck]:
    """Where this pod is in its retirement, measured rather than remembered.

    Pure, for :func:`values_snippet`'s reason: every shape of this report has to
    be exercisable against fixture pod JSON, and the shape that matters most -
    a pod wired to a claim whose chart no longer declares it - is one no test
    cluster is going to be left in by accident.

    Nothing here infers one step from another. The branch and the image are
    read from the manifest, and the manifest can only be read while something
    mounts the claim, so both go *unmeasured* the moment the pod is unwired -
    which is the point at which the earlier steps stop being answerable and
    must stop being answered.
    """
    carried = hotfix_wiring(pod_json)
    fs_group = pod_fs_group(pod_json)
    pod = _as_str(as_dict(pod_json.get("metadata")).get("name")) or "this pod"
    if not carried and claim.present is False:
        # Said once, on the two rows it makes moot, in `_seed_checks`' words and
        # for its reason: the mechanism belongs to the fact - stated on the
        # `claim` row below - and not to each of its consequences.
        moot = "not asked: the claim is gone, and its manifest with it"
        return [
            RetirementCheck(RetirementStep.BRANCH, None, moot),
            RetirementCheck(RetirementStep.IMAGE, None, moot),
            _wiring_check(carried, pod=pod, claim=claim, fs_group=fs_group),
            _claim_check_row(claim, reference=reference),
        ]
    return [
        _branch_check(manifest, wired=bool(carried), reference=reference),
        _image_check(manifest, current_digest=current_digest),
        _wiring_check(carried, pod=pod, claim=claim, fs_group=fs_group),
        _claim_check_row(claim, reference=reference),
    ]


def _branch_check(
    manifest: HotfixManifest | None, *, wired: bool, reference: str
) -> RetirementCheck:
    step = RetirementStep.BRANCH
    if manifest is None:
        return RetirementCheck(
            step,
            None,
            "not measured: nothing mounts the claim, so its manifest cannot be read"
            if not wired
            # "was read", not "is there": the read is an exec, and a refused one
            # must not come back as a claim that carries no fix.
            else "no manifest was read from the claim, so nothing here records "
            "a hotfix to consolidate",
        )
    if manifest.consolidated_branch is not None:
        return RetirementCheck(
            step,
            True,
            f"consolidated onto {manifest.consolidated_branch}, so the claim is "
            "no longer the only copy of this fix",
        )
    if manifest.ahead == 0:
        # Measured on the live target (evidence §7.2): with no commits this row
        # counted a step with nothing in it and offered `consolidate`, which
        # refuses that exact claim - "there is no hotfix to consolidate ...
        # retire the claim instead". Two verbs pointing at each other about one
        # claim, in a report whose whole value is that its count is right.
        return RetirementCheck(
            step,
            True,
            "this claim records no commits of its own, so there is nothing to "
            "consolidate and nothing that retiring it would discard.",
        )
    return RetirementCheck(
        step,
        False,
        f"{manifest.ahead} commit(s) on this claim are on no branch: "
        f"`podbench hotfix consolidate {reference} --branch NAME` first, or "
        "retiring the claim discards them.",
    )


def _image_check(
    manifest: HotfixManifest | None, *, current_digest: str
) -> RetirementCheck:
    step = RetirementStep.IMAGE
    if manifest is None:
        return RetirementCheck(
            step,
            None,
            "not measured: nothing here records which image this hotfix was "
            "made against",
        )
    if not manifest.base_image_digest or not current_digest:
        return RetirementCheck(
            step,
            None,
            "not measured: one of the two digests could not be read, and a "
            "comparison against a digest nobody read is not one",
        )
    was = manifest.base_image_digest.split("@")[-1][:19]
    now = current_digest.split("@")[-1][:19]
    if manifest.base_image_digest == current_digest:
        return RetirementCheck(
            step,
            False,
            f"the deployed image is still {was}, the one this hotfix was made "
            "against: the fix is not in a released image yet.",
        )
    return RetirementCheck(
        step,
        True,
        f"the deployed image is {now}, and the hotfix was made against {was}. "
        "Whether the rebuild included the fix is *not* measured - podbench "
        "compares digests, not contents.",
    )


def _wiring_check(
    carried: Sequence[str],
    *,
    pod: str,
    claim: ClaimState,
    fs_group: int | None = None,
) -> RetirementCheck:
    step = RetirementStep.WIRING
    if not carried:
        return RetirementCheck(
            step,
            True,
            f"{pod} carries none of the hotfix wiring: no {HOTFIX_CLAIM_VOLUME} "
            f"or {SEAT_HOME_VOLUME} volume, no mount at {HOTFIX_APP_PATH}, no "
            "supervisor loop.",
        )
    if claim.present is False and claim.name is not None and not claim.deleted:
        return RetirementCheck(
            step,
            False,
            CLAIM_GONE_BUT_STILL_WIRED.format(pod=pod, claim=claim.name),
        )
    detail = WIRING_IS_THE_APPLICATIONS_OWN.format(
        pod=pod, what=and_list(list(carried))
    )
    if fs_group is not None:
        detail += " " + FSGROUP_NOT_ATTRIBUTED.format(gid=fs_group)
    return RetirementCheck(step, False, detail)


def _claim_check_row(claim: ClaimState, *, reference: str) -> RetirementCheck:
    """The ``claim`` row. ``_row`` because ``_claim_check`` is the pre-flight's,
    which asks a different question - whether the claim is *mounted* - of the
    same object."""
    step = RetirementStep.CLAIM
    named = claim.name or "the claim"
    if claim.deleted:
        return RetirementCheck(
            step,
            True,
            f"{named}: " + CLAIM_CONTENTS_UNVERIFIED.format(key=SUBCHART_VALUES_KEY),
        )
    if claim.present is None:
        return RetirementCheck(step, None, claim.detail or "not measured")
    if not claim.present:
        return RetirementCheck(
            step, True, claim.detail or f"there is no {named} in this namespace"
        )
    if claim.detail:
        # A `--delete-claim` that declined is the only thing that sets this, and
        # it is the answer to what the reader just asked. The offer below is
        # what they already took, so repeating it under a refusal of it would
        # be the report arguing with itself.
        return RetirementCheck(step, False, f"{named}: {claim.detail}")
    return RetirementCheck(
        step, False, CLAIM_OUTLIVES_THE_FLIP.format(claim=named, reference=reference)
    )


def retirement_summary(
    checks: Sequence[RetirementCheck], *, reference: str = ""
) -> str:
    """One line naming what is left, for a listing that has no room for four.

    ``reference`` is how to name this target on a command line - flags
    included, since ``status`` lists pods from namespaces other than the one
    its own flags selected - and an empty one leaves the offer off entirely.

    Shared with :func:`format_retirement` rather than phrased again, because
    ``status`` saying a different number from ``retire`` about the same pod is
    the failure #205 item 4 describes wearing a new hat.
    """
    remaining = [check.step.value for check in checks if check.remaining]
    unmeasured = [check.step.value for check in checks if check.done is None]
    if not remaining and _retired(checks):
        return "complete: nothing is wired and the claim is gone"
    parts: list[str] = []
    if remaining:
        parts.append(
            f"{len(remaining)} of {len(checks)} steps remain ({and_list(remaining)})"
        )
    if unmeasured:
        parts.append(f"{and_list(unmeasured)} not measured here")
    said = "; ".join(parts)
    if not reference:
        return said
    return f"{said} — `podbench hotfix retire {reference}` measures the rest"


def _retired(checks: Sequence[RetirementCheck]) -> bool:
    """Whether the two steps that actually end a retirement are both ticked.

    >>> _retired([RetirementCheck(RetirementStep.WIRING, True, "")])
    False
    """
    ticked = {check.step for check in checks if check.done is True}
    return {RetirementStep.WIRING, RetirementStep.CLAIM} <= ticked


def format_retirement(checks: Sequence[RetirementCheck]) -> str:
    """The retirement report: one line per step, then the verdict.

    ``check``'s shape, down to the column, because it is read the same way and
    by the same person on the same day. The verdict counts only what was
    *measured* outstanding, and a step nobody could measure is named rather
    than absorbed into either answer.
    """
    lines: list[str] = []
    for check in checks:
        lead = f"  {check.flag}".ljust(_FLAG) + f"{check.step.value:<{_CHECK_NAMES}} "
        lines.extend(paragraph(check.detail, first=lead, indent=" " * len(lead)))
    lines.append(rule(char="-"))
    remaining = [check.step.value for check in checks if check.remaining]
    unmeasured = [check.step.value for check in checks if check.done is None]
    if remaining:
        lines.append(
            f"VERDICT: {len(remaining)} of {len(checks)} steps of retirement "
            "remain (exit 1)"
        )
        lines.extend(paragraph(", ".join(remaining), first="REMAINING: "))
    elif _retired(checks):
        # The two steps that end it, and only those: the branch and the image
        # are moot once the claim is gone, so waiting for four ticks would be
        # waiting for two nobody can ever take.
        lines.append("VERDICT: retirement is complete (exit 0)")
    else:
        lines.append("VERDICT: nothing measured here is outstanding (exit 0)")
    if unmeasured:
        lines.extend(paragraph(", ".join(unmeasured), first="NOT MEASURED: "))
    return "\n".join(lines)


def claim_state(
    kube: Kubectl, target: HotfixTarget, pod_json: Mapping[str, Any]
) -> ClaimState:
    """Find the claim and ask whether it is still there.

    Two routes, because the pod stops naming the claim at exactly the step
    before the one that deletes it. While it is wired the pod's own volume is
    the measurement; once it is not, the only thing left pointing at the claim
    is :data:`HOTFIX_TARGET_LABEL`, which both claim charts set.

    Every failure to read is :attr:`ClaimState.present` ``None`` and never
    ``False``: kubectl distinguishes a 404 from a 403 in its text alone, and a
    report that read a refusal as "the claim is gone" would tick the one step
    of retirement that cannot be undone.
    """
    named = pod_claim_name(pod_json)
    if named is not None:
        result = kube.run("get", "pvc", named, "-o", "name", check=False)
        if result.returncode == 0:
            return ClaimState(named, True)
        if _is_not_found(result):
            return ClaimState(named, False)
        return ClaimState(named, None, _unreadable(result, f"pvc/{named}"))
    result = kube.run(
        "get", "pvc", "-l", HOTFIX_TARGET_LABEL, "-o", "json", check=False
    )
    if result.returncode != 0:
        return ClaimState(None, None, _unreadable(result, "the namespace's claims"))
    wanted = {
        name
        for name in (target.container, target.workload_name, target.pod.name)
        if name
    }
    try:
        items = _as_list(_load_json(result.stdout).get("items"))
    except json.JSONDecodeError:
        # rc=0 and unparseable is `_pods_mounting`'s case, and for its reason:
        # the answer here decides an irreversible step, so anything short of a
        # listing this code understood is unmeasured.
        return ClaimState(None, None, _unreadable(result, "the namespace's claims"))
    found = sorted(
        name
        for item in items
        if (metadata := as_dict(as_dict(item).get("metadata")))
        and (name := _as_str(metadata.get("name"))) is not None
        and _as_str(as_dict(metadata.get("labels")).get(HOTFIX_TARGET_LABEL)) in wanted
    )
    if len(found) == 1:
        return ClaimState(found[0], True)
    if not found:
        return ClaimState(
            None,
            None,
            CLAIM_NOT_LABELLED_FOR_THIS_TARGET.format(
                namespace=kube.namespace,
                label=HOTFIX_TARGET_LABEL,
                names=and_list(sorted(wanted)),
            ),
        )
    return ClaimState(
        None,
        None,
        f"not measured: {and_list(found)} all carry {HOTFIX_TARGET_LABEL} for "
        "this target, and this pod names none of them",
    )


def _is_not_found(result: CommandResult) -> bool:
    """Whether kubectl said the object is absent, as opposed to unreadable.

    >>> _is_not_found(CommandResult((), 1, "", 'pvc "api" not found'))
    True
    >>> _is_not_found(CommandResult((), 1, "", "Error: Forbidden"))
    False
    """
    return "not found" in result.stderr.lower()


def _unreadable(result: CommandResult, what: str) -> str:
    said = (result.stderr.strip() or result.stdout.strip()).splitlines()
    return f"not measured: {what} could not be read" + (
        f" - {said[-1].strip()}" if said else ""
    )


def retire(
    kube: Kubectl,
    reference: str,
    *,
    container: str | None = None,
    delete_claim: bool = False,
) -> list[RetirementCheck]:
    """Measure every step of retirement, and perform the one podbench can.

    The one is deleting the claim, and only with ``delete_claim`` and only once
    nothing mounts it. Everything above it in the checklist is a PR, a merge, a
    rebuild and a values change - all of them somebody else's system - so this
    verb's honest job is to say which of them have landed. Reading the claim's
    manifest costs no seat: it is exec'd out of the application container, the
    way ``status`` reads it.

    The single-replica refusal is lifted here alone: it guards a write to a
    ReadWriteOnce claim, and the state this verb exists to confirm is the one
    after the wiring came out and the team scaled back up.
    """
    target, pod_json = _resolve_target(
        kube, reference, container=container, require_single_replica=False
    )
    mounting = claim_container(pod_json)
    manifest = (
        read_pod_state(kube, target.pod.name, mounting)[0]
        if mounting is not None
        else None
    )
    claim = claim_state(kube, target, pod_json)
    if delete_claim:
        claim = _delete_claim(kube, claim)
    return retirement(
        pod_json,
        manifest,
        claim=claim,
        current_digest=target.image_digest,
        reference=_pasteable(kube, reference),
    )


def _pasteable(kube: Kubectl, reference: str) -> str:
    """*reference* with the flags that make the offers under it work.

    ``format_status`` reasons the same way about the same offer and for the
    same reason: a command a reader pastes out of a report has to reach the
    object the report was about, and one that silently means the kubeconfig's
    default namespace instead is worse than a redundant flag. The namespace
    goes in even when it is the current one, because nothing in the report says
    which that was.

    >>> _pasteable(Kubectl("demo", context="beam"), "pod/api")
    'pod/api -n demo --context beam'
    """
    said = f"{reference} -n {kube.namespace}"
    return said if kube.context is None else f"{said} --context {kube.context}"


def _delete_claim(kube: Kubectl, claim: ClaimState) -> ClaimState:
    """Delete the claim, or say why this run did not.

    The question asked first is about the **namespace** and not about the
    target: what makes the deletion safe is that nothing mounts the claim, and
    a target-local check would miss the second pod of a rollout still holding
    it. The delete would then block on that pod's reference and be reported as
    done, which is exactly the step-it-could-not-verify this verb must not take.
    """
    if claim.name is None or claim.present is not True:
        # Nothing measured to delete. Whatever `claim_state` found already says
        # what it found, and inventing a second sentence for it here is how the
        # unmeasured case comes to read as a refusal.
        return claim
    holders = _pods_mounting(kube, claim.name)
    if holders is None:
        return replace(claim, detail=CLAIM_MOUNTERS_UNREADABLE)
    if holders:
        return replace(claim, detail=CLAIM_STILL_MOUNTED.format(pods=and_list(holders)))
    kube.run("delete", "pvc", claim.name)
    return replace(claim, present=False, deleted=True, detail="")


def _pods_mounting(kube: Kubectl, claim: str) -> list[str] | None:
    """Which pods in the namespace mount *claim*, or ``None`` if unreadable.

    Every volume, not only :data:`~podbench.model.HOTFIX_CLAIM_VOLUME`: what
    matters here is who holds the object, and a pod mounting it under a name of
    its own holds it just as hard.
    """
    result = kube.run("get", "pods", "-o", "json", check=False)
    if result.returncode != 0:
        return None
    try:
        items = _as_list(_load_json(result.stdout).get("items"))
    except json.JSONDecodeError:
        return None
    return sorted(
        name
        for item in items
        if (pod := as_dict(item))
        and (name := _as_str(as_dict(pod.get("metadata")).get("name"))) is not None
        and any(
            _as_str(
                as_dict(as_dict(volume).get("persistentVolumeClaim")).get("claimName")
            )
            == claim
            for volume in _as_list(as_dict(pod.get("spec")).get("volumes"))
        )
    )


# -- CLI -------------------------------------------------------------------


_Target = Annotated[
    str,
    typer.Argument(
        metavar="TARGET", help="pod/NAME, deployment/NAME or statefulset/NAME"
    ),
]
_Venv = Annotated[
    str | None,
    typer.Option(
        "--venv",
        metavar="PATH",
        help="the mountPath the claim is mounted at, beside the application's "
        "own project and never over it (default: read off the pod, and "
        f"{HOTFIX_APP_PATH} where it mounts no claim yet). A value that "
        "disagrees with the pod is refused",
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
_ImageProject = Annotated[
    str,
    typer.Option(
        "--image-project",
        metavar="PATH",
        help="where the application's project lives inside its own image "
        f"(default: {IMAGE_PROJECT_PATH}, python-copier-template's convention)",
    ),
]
_ImageInterpreter = Annotated[
    str,
    typer.Option(
        "--image-interpreter",
        metavar="PATH",
        help="the interpreter beside it, in the same image "
        f"(default: {IMAGE_INTERPRETER_PATH})",
    ),
]
# Shared by `values` and `init` deliberately: the flag is a contract *between*
# those two verbs (#209), so two declarations of it are two chances for the
# sentence describing that contract to drift apart, which is what happened while
# one copy lived on the root callback.
_ClaimVenv = Annotated[
    str,
    typer.Option(
        "--claim-venv",
        metavar="NAME",
        help="the venv's directory name on the claim, which the runtime switch "
        f"looks for and `uv sync` builds (default: {CLAIM_VENV_DIR}). `values` "
        "and `init` have to be given the same name",
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
_Repo = Annotated[
    str | None,
    typer.Option(
        "--repo",
        metavar="URL",
        help=f"git URL to clone (default: the target image's {SOURCE_LABEL} label)",
    ),
]
"""``init``'s flag, and ``check``'s because it is ``check``'s question too.

``init`` refuses a target nothing names a repository for, so the pre-flight has
to ask the same thing to reach the same answer: a ``check`` that could not hear
``--repo`` would refuse a target the very next command accepts.
"""
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
    """What a verb did, one wrapped paragraph per action.

    :func:`_laid_out` and not a bare ``emit``: ``emit`` styles a line but never
    breaks one, and several of these actions are a sentence rather than a row -
    ``_install``'s and ``_relaunch``'s carry a whole remedy. The retirement
    checklist is in here too and is already laid out, which is why the rule is
    "wrap what was written flush left" rather than "wrap everything": its
    numbered steps are indented, and step 1 is a command to be pasted.
    """
    emit(_laid_out("\n".join(actions)))
    return 0


def _warn(message: str) -> None:
    """One warning, on stderr, in the vocabulary the rest of podbench uses.

    ``WARNING`` and a hung indent rather than a ``podbench:`` prefix, so it can
    be picked out of a paste and coloured by span the way every other verb's is;
    stderr because a verb's report is its stdout.
    """
    emit(
        "\n".join(
            paragraph(message, first=f"{WARNING_LEAD}  ", indent=" " * WARNING_HANG),
        ),
        stderr=True,
    )


NON_EXEC_PROBE_WARNING = (
    "{pod}'s {container!r} declares a {kind} livenessProbe, which the hold "
    "cannot short-circuit - only an exec probe can. No livenessProbe is "
    "emitted below, so the chart keeps this one and the kubelet will restart "
    "the pod out from under the seat at failureThreshold x periodSeconds. "
    "Deal with that before holding this pod."
)
"""Said when the target's probe is real, readable, and of no use to hold mode.

Not an error, because the values are still correct and still worth emitting -
and not silence either, because the emitted block being *absent* looks identical
to the canonical fastcs case, which genuinely has no probe. The difference is
the whole of whether a held pod survives.

Printed through :func:`_warn`, which supplies :data:`podbench.console.WARNING_LEAD`
- so the text opens on the fact rather than on a ``podbench:`` prefix that is
neither coloured nor findable in a pasted terminal.

*Why* only an exec probe can be short-circuited - an httpGet or tcpSocket probe
answers from the application, and the application is exactly what is down while
a pod is held - is in ``docs/how-to/hotfix-a-running-pod.md`` under the
``--liveness-probe`` bullet. The line says which pod it applies to and what the
kubelet will do about it.
"""


EXISTING_MOUNTS_WARNING = (
    "{pod}'s {container!r} already mounts {mounts}, and podbench cannot tell "
    "from here which of those the service's own values declared. The "
    "`volumes:` and `volumeMounts:` keys below are a whole key each, so merge "
    "them into that file rather than pasting over it - or let `--values` do "
    "the merge."
)
"""Said when the target carries volumes that are not podbench's.

Found by diffing `--from-pod`'s output against the hand-written values for
`bl47p-mo-ioc-01` on 2026-08-22: the emitted `volumes:` carried podbench's two
and nothing else, so pasting it verbatim would have dropped that IOC's `dev-shm`
and left it without `/dev/shm`.

The hazard is the *values file*, not the chart. Measured against `ioc-instance`
5.6.1 on the same day: `bl47p-mo-ioc-01`'s values declare `dev-shm` alone, and
its pod carries six volumes - the chart's five and that one. So a chart appends
what the values give it, and what a paste destroys is the service's own key.
Saying "replaces the ones your chart renders", as this warning first did, would
send a reader to copy five chart-generated volumes into their values file and
end up with them declared twice.

Podbench cannot do the merge itself and must not pretend to: read from a live
pod, a chart-generated volume and one the service declared are
indistinguishable. Naming them and saying which key is at risk is the whole of
what can honestly be done here - and beat three is now ``--values``, which is
the merge, rather than an instruction to do it by hand.

That what the chart renders for itself is unaffected and must not be copied into
the values file is in ``docs/how-to/hotfix-a-running-pod.md`` and in full in
``docs/reference/cli.md``: it is the second mistake a reader of this warning
makes, and it is a paragraph rather than a clause.

Printed through :func:`_warn`, for :data:`NON_EXEC_PROBE_WARNING`'s reason.
"""


def _values_failure(message: str) -> NoReturn:
    """``hotfix values`` refusing, on stderr, laid out like everything else.

    Through :func:`_laid_out` and not ``print``: most of these messages end in
    :data:`FROM_POD_ESCAPE`, which is two paragraphs carrying seven backticked
    flags, and a terminal wrapping that itself breaks mid-token - which is
    exactly the pasteable half :data:`podbench.console._TOKEN` exists to keep
    whole. The two ``--liveness-probe`` refusals carry no escape, because what
    they are refusing is the flag and not the pod read.

    stderr because stdout is the values snippet and a shell redirects it over a
    values file (the p47 run did): nothing this verb says may land in there.
    """
    emit(_laid_out(f"podbench: {message}"), stderr=True)
    raise typer.Exit(2)


def _read_values_from_pod(
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
    warn_mounts: bool = True,
) -> tuple[str, str, Mapping[str, Any] | None]:
    """Fill the three emitted values from the target, or say why not.

    This is the whole of the cluster read: a thin wrapper that fills
    :func:`values_snippet`'s existing arguments. The emitter keeps its
    signature and stays testable with no cluster, which is not negotiable - it
    is what lets every shape of the output be asserted in a unit test.

    A flag the user passed always wins over the pod. Since #205 item 6 took the
    offline route away that is the *only* thing the three flags still do, and it
    is what makes ``--entrypoint`` a usable answer to an image-ENTRYPOINT target
    without giving up the gid and the probe as well.

    Every failure here says why the read is not optional
    (:data:`FROM_POD_ESCAPE`). Making the cluster read the only way means each
    of these lands on somebody who did not ask for a cluster, and the one thing
    they must not be left to work out for themselves is that hand-writing the
    values instead is the way #176 happened.
    """
    if pod is None:
        _values_failure(
            "`hotfix values` reads the target, so it needs to know which pod: "
            "pass `--from-pod POD`.\n"
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
        _values_failure(f"{error}.\n\n{FROM_POD_ESCAPE}")
    try:
        pod_json = kube.get_pod(name)
    except KubectlError as error:
        # One branch for no kubeconfig, no current context, a pod that does not
        # exist and a forbidden `get pods` alike: kubectl tells those apart only
        # in the text of its own message, so podbench relays that verbatim
        # rather than guessing at a category and getting it wrong.
        _values_failure(
            f"could not read pod {name!r} in {kube.namespace!r}:\n"
            f"{_relay(str(error))}\n"
            f"\n{FROM_POD_ESCAPE}"
        )
    try:
        target = _application_container(pod_json, container)
    except HotfixError as error:
        _values_failure(f"{error}.\n\n{FROM_POD_ESCAPE}")
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
            _values_failure(str(error))
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
    # Not under `--values`: the warning exists because a pod cannot say which of
    # its volumes the service asked for, and the values file has just said. Left
    # in, it tells the user to go and do by hand the merge that has already been
    # done for them, and names volumes the merge deliberately did not copy
    # because the chart renders them.
    if theirs and warn_mounts:
        _warn(
            EXISTING_MOUNTS_WARNING.format(
                pod=name, container=target, mounts=and_list(theirs)
            )
        )
    if probe is None:
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
                _warn(
                    NON_EXEC_PROBE_WARNING.format(pod=name, container=target, kind=kind)
                )
    return entrypoint, gid, probe


def _build_app(runner: Runner | None) -> typer.Typer:
    app = new_app()

    @app.callback(invoke_without_command=True)
    def root(ctx: typer.Context) -> None:
        """Durable in-place fixes: the project on a claim beside the
        application, every change a commit, and a status command that will not
        let a hotfixed pod go unnoticed.
        """
        require_subcommand(ctx)

    # `values_command` for symmetry with the two verbs below, whose suffix is
    # forced. Nothing forces it here - `values` is not a module-level name - so
    # do not read a hazard into it.
    @app.command(
        name="values",
        help="emit the helm values an application's chart needs",
    )
    def values_command(
        app_name: Annotated[
            str,
            typer.Option("--app", metavar="NAME", help="application name"),
        ],
        entrypoint: Annotated[
            str | None,
            typer.Option(
                "--entrypoint",
                metavar="CMD",
                help=(
                    "the command the container runs today, which the supervisor "
                    "wraps (default: read from the target)"
                ),
            ),
        ] = None,
        liveness_probe: Annotated[
            str | None,
            typer.Option(
                "--liveness-probe",
                metavar="JSON",
                help=(
                    "the target's whole livenessProbe as json, overriding the "
                    "one read off the target. Its exec command is wrapped and "
                    "its timings are carried over; a chart renders a supplied "
                    "probe wholesale, so a timing left out becomes the k8s "
                    "default"
                ),
            ),
        ] = None,
        size: Annotated[
            str,
            typer.Option("--size", metavar="SIZE", help="claim size"),
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
                help="the application container's gid (default: read from the target)",
            ),
        ] = DEFAULT_GID_PLACEHOLDER,
        from_pod: Annotated[
            str | None,
            typer.Option(
                "--from-pod",
                metavar="POD",
                help="read the entrypoint, livenessProbe and gid off this pod, "
                "so the emitted values need no hand-editing",
            ),
        ] = None,
        values: Annotated[
            str | None,
            typer.Option(
                "--values",
                metavar="PATH",
                help="the target service's own values file: emit it back whole "
                "with podbench's keys merged in, rather than a fragment to "
                "merge by hand",
            ),
        ] = None,
        parent_values: Annotated[
            str | None,
            typer.Option(
                "--parent-values",
                metavar="PATH",
                help="a shared values file the service inherits from. A helm "
                "list replaces rather than merges, so this is what stops a "
                "service declaring `volumes:` for the first time silently "
                "dropping the shared ones",
            ),
        ] = None,
        values_under: Annotated[
            str | None,
            typer.Option(
                "--values-under",
                metavar="KEY",
                help="dotted path to the mapping the target chart keeps its pod "
                "template keys under (`ioc-instance`). Read from the files when "
                "not given, and the output says where they went",
            ),
        ] = None,
        central_claim: Annotated[
            bool,
            typer.Option(
                "--central-claim",
                help="emit the older namespace-wide `hotfixProject.claims[]` "
                "entry instead of the podbench-hotfix-claim chart dependency",
            ),
        ] = False,
        claim_venv: _ClaimVenv = CLAIM_VENV_DIR,
        container: _Container = None,
        namespace: _Namespace = None,
        context: _Context = None,
        kubectl: _KubectlBinary = "kubectl",
    ) -> None:
        parsed_probe: Mapping[str, Any] | None = None
        if liveness_probe is not None:
            try:
                parsed_probe = _load_json(liveness_probe)
            except json.JSONDecodeError as exc:
                # `_values_failure` and not a `print` of its own: this is the
                # same verb refusing the same way, and two spellings of that is
                # how one of them stops being wrapped.
                _values_failure(f"--liveness-probe is not valid json: {exc}")
            if not probe_exec_command(parsed_probe):
                _values_failure(
                    "--liveness-probe has no exec.command. Only an exec probe "
                    "can be short-circuited by the hold - an httpGet or "
                    "tcpSocket probe answers from the application, which is "
                    "exactly what is down while a pod is held."
                )
        # Unconditional since #205 item 6: `--no-from-pod` emitted from the
        # flags alone, and every field it left to be typed is one #176 says
        # costs a restart ladder to get wrong.
        entrypoint, gid, parsed_probe = _read_values_from_pod(
            from_pod,
            container=container,
            namespace=namespace,
            context=context,
            binary=kubectl,
            runner=runner,
            entrypoint=entrypoint,
            gid=gid,
            probe=parsed_probe,
            warn_mounts=values is None,
        )
        snippet = values_snippet(
            app_name,
            entrypoint,
            size=size,
            gid=gid,
            venv=claim_venv,
            liveness_probe=parsed_probe,
            central_claim=central_claim,
        )
        if values is not None:
            snippet = _merge_values_into_file(
                snippet,
                values=values,
                parent_values=parent_values,
                values_under=values_under,
                claim_key=SUBCHART_VALUES_KEY,
            )
        elif parent_values is not None or values_under is not None:
            _values_failure(
                "--parent-values and --values-under are what --values does "
                "with the file it is given, so neither means anything "
                "without it."
            )
        print(snippet, end="" if values is not None else "\n")
        raise typer.Exit(0)

    @app.command(
        name="check",
        help="say what would stop a hotfix on this target, before starting one",
    )
    def check_command(
        target: _Target,
        repo: _Repo = None,
        image_project: _ImageProject = IMAGE_PROJECT_PATH,
        image_interpreter: _ImageInterpreter = IMAGE_INTERPRETER_PATH,
        container: _Container = None,
        seat: _Seat = None,
        namespace: _Namespace = None,
        context: _Context = None,
        kubectl: _KubectlBinary = "kubectl",
    ) -> None:
        """Read-only, so it can be run on the way in and run again after a fix.

        It writes nothing and lands nothing - notably not a seat, which ``init``
        does: an ephemeral container cannot be taken back off a pod, and a verb
        somebody runs to *ask a question* must not spend one.
        """
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        checks = preflight(
            kube,
            target,
            container=container,
            seat=seat,
            repo=repo,
            image_project=image_project,
            image_interpreter=image_interpreter,
        )
        emit(format_preflight(checks))
        raise typer.Exit(1 if any(check.blocks for check in checks) else 0)

    # `init_command`/`consolidate_command`, not `init`/`consolidate`: the
    # module-level functions of those names are what they call, and a same-named
    # closure would shadow them into a recursion.
    @app.command(
        name="init",
        help="seed the claim from the running container, clone the source, "
        "rebuild the venv",
    )
    def init_command(
        target: _Target,
        repo: _Repo = None,
        venv: _Venv = None,
        ref: Annotated[
            str | None,
            typer.Option("--ref", metavar="REF", help="branch or tag to clone"),
        ] = None,
        base_commit: Annotated[
            str | None,
            typer.Option(
                "--base-commit",
                metavar="SHA",
                help="the commit the released image was built from (default: "
                f"the image's {REVISION_LABEL} label; failing that the clone's "
                "HEAD, recorded as an assumed base and reported as one)",
            ),
        ] = None,
        no_install: Annotated[
            bool,
            typer.Option(
                "--no-install",
                help="skip the editable install (the application image has no pip)",
            ),
        ] = False,
        image_project: _ImageProject = IMAGE_PROJECT_PATH,
        image_interpreter: _ImageInterpreter = IMAGE_INTERPRETER_PATH,
        claim_venv: _ClaimVenv = CLAIM_VENV_DIR,
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
        mount, note = resolve_venv(resolved, venv)
        if note is not None:
            _warn(note)
        # Only when something is still missing, because it is a registry round
        # trip on the way into an emergency. The digest is preferred over the
        # tag: it is what is actually running, and a tag can have moved since.
        labels = (
            read_image_labels(resolved.image_digest or resolved.image)
            if repo is None or base_commit is None
            else None
        )
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
            venv=mount,
            repo=repo,
            image_labels=labels,
            ref=ref,
            base_commit=base_commit,
            author=author,
            install=not no_install,
            image_project=image_project,
            image_interpreter=image_interpreter,
            claim_venv=claim_venv,
        )
        raise typer.Exit(_report(actions))

    @app.command(help="commit the change on the claim and relaunch the running child")
    def apply(
        target: _Target,
        message: Annotated[
            str,
            typer.Option("-m", "--message", metavar="TEXT", help="commit message"),
        ],
        venv: _Venv = None,
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
        mount, note = resolve_venv(resolved, venv)
        if note is not None:
            _warn(note)
        store = _store_for(kube, resolved.pod.name, seat=seat, local=local)
        _, actions = apply_hotfix(
            kube,
            store,
            resolved,
            venv=mount,
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
        all_namespaces: Annotated[
            bool,
            typer.Option(
                "-A",
                "--all-namespaces",
                help="every namespace in the cluster, with the same exit code — "
                "the facility-wide shutdown assertion, as one command",
            ),
        ] = False,
        namespace: _Namespace = None,
        context: _Context = None,
        kubectl: _KubectlBinary = "kubectl",
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        rows = status_rows(
            kube, probe=not no_probe, python=python, all_namespaces=all_namespaces
        )
        emit(format_status(rows, all_namespaces=all_namespaces))
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
        venv: _Venv = None,
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
        mount, note = resolve_venv(resolved, venv)
        if note is not None:
            _warn(note)
        store = _store_for(kube, resolved.pod.name, seat=seat, local=local)
        _, actions = consolidate(
            kube,
            store,
            resolved,
            venv=mount,
            branch=branch,
            remote=remote,
            push=not dry_run,
        )
        raise typer.Exit(_report(actions))

    # `retire_command`, for `init_command`'s reason: `retire` is the
    # module-level function this calls, and a same-named closure would shadow
    # it into a recursion.
    @app.command(
        name="retire",
        help="what is left of retiring this hotfix, and the one step podbench can take",
    )
    def retire_command(
        target: _Target,
        delete_claim: Annotated[
            bool,
            typer.Option(
                "--delete-claim",
                help="delete the claim, once nothing mounts it. Irreversible: "
                "an unmounted claim cannot be read first, so what is on it goes "
                "unverified",
            ),
        ] = False,
        container: _Container = None,
        namespace: _Namespace = None,
        context: _Context = None,
        kubectl: _KubectlBinary = "kubectl",
    ) -> None:
        """Read-only without ``--delete-claim``, and it lands no seat.

        The claim's manifest is read through the *application* container, the
        way ``status`` reads it, so asking where a retirement has got to costs
        no ephemeral container — the same rule ``check`` follows, and for the
        same reason: one cannot be taken back off a pod.
        """
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        checks = retire(kube, target, container=container, delete_claim=delete_claim)
        emit(format_retirement(checks))
        raise typer.Exit(1 if any(check.remaining for check in checks) else 0)

    return app


def main(args: Sequence[str] | None = None, *, runner: Runner | None = None) -> int:
    """Entry point for ``podbench hotfix <subcommand>``.

    ``status`` exits non-zero when any pod needs attention, so that it is usable
    as a shutdown-checklist assertion — "no hotfix in this namespace needs
    somebody today" is a thing a facility wants to be able to test, not read.
    With ``--all-namespaces`` that assertion covers the cluster, which is what
    makes it a command rather than a shell loop somebody has to keep correct.

    It is deliberately **not** the assertion "nothing here is still to be
    retired", and the wording above used to say it was. A consolidated fix whose
    image has not moved yet is a live hotfix doing its job, so retirement is not
    part of :attr:`HotfixRow.ok` (see the attribute, which states the reasoning)
    and ``status`` exits 0 on a pod ``retire`` reports three steps short. Two
    verbs, two assertions: ``retire`` is the one that goes red until a
    retirement is finished. Whether the shutdown gate should be the stricter of
    the two is an open contract question and not a wording one, so nothing here
    decides it - it is recorded so that the two docstrings stop disagreeing
    about which assertion is being made.

    ``retire`` exits **1** while any step of a retirement it measured is
    outstanding, and **0** when the pod is unwired and the claim is gone. A step
    it could not measure moves neither: an unmeasured step is not an assertion
    in either direction, and ticking one would be the lie that verb exists to
    stop.

    ``check`` exits **1** the same way, and for the same reason: it is the
    assertion "nothing on this target stands between here and ``hotfix init``".
    Exit 2 stays what it has always been — podbench refusing the command it was
    given — so a blocked target and a mistyped one are not the same answer.
    """
    argv = list(sys.argv[1:] if args is None else args)
    # The central dispatcher keeps the verb in argv; this app's subcommands are
    # the *sub*-verbs, so the leading `hotfix` is consumed here.
    if argv and argv[0] == "hotfix":
        argv = argv[1:]
    try:
        return run(_build_app(runner), argv, prog="podbench hotfix")
    except KubectlError as error:
        # Its own arm because it is not podbench's prose: `KubectlError._message`
        # inlines `result.stderr` behind an argv and an exit code, so there is no
        # flush-left half to wrap, and laying it out would put podbench's line
        # breaks through somebody else's error - the paste then stops matching
        # the terminal it came from, which is the whole of `_relay`'s reason.
        emit(_verbatim(f"podbench: {error}"), stderr=True)
        return 2
    # `LauncherError` is here because `hotfix init` lands its own seat now
    # (#177): the landing goes through `launcher.attach`, which refuses in its
    # own currency - an unknown --mount, a pod with no containers, a target
    # container that is not there. Those are all things the user has to fix, and
    # exit 2 with the sentence is the answer; a traceback is not.
    except (HotfixError, LauncherError, ValueError) as error:
        # Through `_laid_out` rather than `print`: `TARGET_HAS_NO_PROJECT` is
        # three paragraphs and 728 characters, and the terminal's own wrap puts
        # a break through the middle of `--image-project PATH`. The relayed
        # halves of the messages that carry kubectl's or uv's stderr are
        # indented by `_relay`, so they come out exactly as they arrived.
        emit(_laid_out(f"podbench: {error}"), stderr=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
