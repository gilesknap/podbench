"""``doctor`` — name the blocker before the first attach, not during it.

Under ``uvx`` there is no install step to hang first-run setup on, so the
client-side checks have nowhere else to live: the ssh ``Include`` is a hand-edit
the docs ask for and nothing verifies, and an RBAC denial is discovered
mid-attach — after a container name has been burnt for the life of the pod
(report 4.2). This is :mod:`podbench.probe`'s discipline turned round to face
the laptop: measure each thing, then say which mechanism will refuse and what to
type about it.

Everything is measured through the injected :class:`~podbench.kubectl.Runner`
and a ``which``, so the unit suite never shells out and never reads the
developer's own kubeconfig. The only thing ``doctor`` ever writes is what
``--fix`` asks of it — the ssh ``Include`` line and the config directory — and
even then it will not mint an ssh key: a key podbench generated would be a
credential the user never chose, authorised inside a container by a command they
ran to get a *diagnosis*.

Every direct ``runner`` call here passes :data:`~podbench.kubectl.UNBOUNDED`,
which is a decision rather than an oversight. Issue #118 bounds the ``kubectl``
calls podbench makes, and the ones in this module that reach a cluster go
through :class:`~podbench.kubectl.Kubectl` — the ``auth can-i`` reviews — where
that bound applies. What is left is local: a client version, a current context,
``ssh -G`` against a file, a fingerprint. This verb also has no ``except``
clause of its own, so a timeout raised here would replace a report of a dozen
rows with a traceback. The one call that could genuinely wedge is ``ssh-add -l``
against a dead agent socket, and it is left unbounded knowingly — the answer is
a WARN row about that socket, and a cut-off call would report the wrong thing
about it.
"""

from __future__ import annotations

import enum
import json
import os
import re
import shlex
import shutil
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .cli import new_app, run
from .console import emit, paragraph, rule
from .kubectl import UNBOUNDED, CommandResult, Kubectl, Runner, run_subprocess
from .launcher import (
    CONFIG_D,
    DEFAULT_CLIENT_DIR,
    DEFAULT_IDENTITY,
    client_dir,
    current_namespace,
    default_host_alias,
    identity_paths,
    ssh_include_line,
)
from .model import DEFAULT_IMAGE, IMAGE_ENV, PodRef, as_dict

__all__ = [
    "FEATURES",
    "MIN_KUBECTL",
    "SSH_CONFIG",
    "Check",
    "Grant",
    "IncludeState",
    "RbacFeature",
    "RbacVerdict",
    "Report",
    "Status",
    "agent_fingerprints",
    "client_version",
    "diagnose",
    "format_report",
    "include_state",
    "key_fingerprint",
    "main",
    "splice_include",
]

MIN_KUBECTL = (1, 25)
"""Ephemeral containers are stable from 1.25, which is the floor the docs state.

Below it ``pods/ephemeralcontainers`` is either absent or behind a feature gate,
and podbench posts to that subresource directly rather than shelling out to
``kubectl debug``.
"""

SSH_CONFIG = "~/.ssh/config"
"""The file OpenSSH reads for a user, and the only one podbench asks to edit."""

Which = Callable[[str], str | None]
"""How this module asks whether a program exists. Injected for the same reason
:class:`~podbench.kubectl.Runner` is: the unit suite must not depend on what
happens to be installed on the machine running it."""


class Status(enum.Enum):
    """How one check came out.

    ``WARN`` and ``FAIL`` differ in exactly one way that matters, and it is the
    exit code: a ``FAIL`` stands between this machine and a working ``podbench
    attach``, a ``WARN`` does not. Iterate mode's RBAC being absent is a real
    thing to know and not a reason for a script to stop.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "FAIL"


@dataclass(frozen=True)
class Check:
    """One question asked of this machine, and what was measured for it."""

    name: str
    status: Status
    detail: str
    hint: str | None = None
    """What to do about it. Named rather than done, unless ``--fix`` says
    otherwise and the fix is one podbench can make without inventing a
    credential."""

    fixed: str | None = None
    """What ``--fix`` changed, when it changed something. The check is
    re-measured after the change, so a fixed check reports its *new* status."""


@dataclass(frozen=True)
class Grant:
    """One RBAC permission, spelled the way an RBAC rule writes it."""

    verb: str
    resource: str

    def __str__(self) -> str:
        return f"{self.verb} {self.resource}"

    @property
    def argv(self) -> tuple[str, ...]:
        """``auth can-i`` arguments that ask about exactly this grant.

        A subresource goes in ``--subresource`` and never in the positional.
        kubectl splits a positional ``pods/exec`` on the slash into a resource
        and a resource *name*, so the review asks whether the caller may create
        a pod **called** ``exec`` — which a Role granting the exec subresource
        correctly answers no to. Reported from a Diamond beamline namespace,
        where ``auth can-i --list`` showed ``pods/exec [create]`` for a
        ServiceAccount that ``can-i create pods/exec`` said no for.

        It survives because it is invisible from an entitled kubeconfig: a
        cluster-admin is allowed both readings and answers yes to either, so the
        wrong question only misreports for the narrowly-scoped credential this
        verb exists to check. ``--subresource`` needs kubectl 1.24, below
        :data:`MIN_KUBECTL`.

        >>> Grant("create", "pods/exec").argv
        ('create', 'pods', '--subresource=exec')
        >>> Grant("get", "deployments.apps").argv
        ('get', 'deployments.apps')
        """
        resource, _, subresource = self.resource.partition("/")
        if not subresource:
            return (self.verb, self.resource)
        return (self.verb, resource, f"--subresource={subresource}")


@dataclass(frozen=True)
class RbacFeature:
    """A thing podbench can do, and the permissions it cannot do it without."""

    name: str
    """What the user calls it — the verb or flag they will reach for."""

    chart_flag: str
    """The ``rbac.<flag>`` value in ``Charts/podbench`` that grants these.

    Named so the two lists can be checked against each other rather than merely
    claimed to agree: ``tests/test_chart_contract.py`` renders the chart with
    each flag alone and asserts every grant below is one that flag really gives.
    The chart is allowed to be the more generous of the two — it also grants
    ``pods/log`` and the ``list``/``watch`` verbs podbench does not use — but a
    grant checked here that the chart does not give would send a reader to a
    chart flag that cannot fix their report.
    """

    grants: tuple[Grant, ...]

    blocking: bool = False
    """Whether losing this stops ``podbench attach`` outright, and so decides
    the exit code."""


FEATURES: tuple[RbacFeature, ...] = (
    RbacFeature(
        "attach",
        "observe",
        (
            Grant("get", "pods"),
            Grant("list", "pods"),
            # `update`, not `patch`: the seat is added by PUTting the
            # subresource, because `kubectl debug` merges its own profile over
            # the spec afterwards and can hand back an invalid rung.
            Grant("get", "pods/ephemeralcontainers"),
            Grant("update", "pods/ephemeralcontainers"),
            # The whole network story. No Service, no port-forward, no pod IP.
            Grant("create", "pods/exec"),
        ),
        blocking=True,
    ),
    RbacFeature(
        "iterate",
        "iterate",
        (
            Grant("create", "pods"),
            Grant("delete", "pods"),
            # --take-traffic/--cutover only, both opt-in.
            Grant("get", "services"),
            Grant("patch", "services"),
        ),
    ),
    RbacFeature(
        "resize",
        "resize",
        (
            # `get` as well as `patch`, for the same reason
            # `pods/ephemeralcontainers` above needs both: kubectl issues a GET
            # on the subresource before it sends the write. Measured on argus
            # (hgv27681, 2026-08-20) with `kubectl --v=8` against a service
            # account holding `pods/resize: [patch]` and no `get` - the GET is
            # Forbidden and the PATCH is never sent, while `doctor` said the
            # tier was fine. `kubectl auth can-i get pods/resize` answers yes on
            # that same cluster, so the grant list is what has to be right here.
            Grant("get", "pods/resize"),
            Grant("patch", "pods/resize"),
        ),
    ),
    RbacFeature(
        "hotfix",
        "hotfix",
        (
            Grant("get", "deployments.apps"),
            Grant("get", "replicasets.apps"),
            Grant("patch", "deployments.apps"),
            Grant("patch", "pods"),
            Grant("delete", "pods"),
        ),
    ),
)
"""Feature -> the permissions it needs, mirroring ``Charts/podbench``.

Ordered as the report prints them, headline feature first.
"""


@dataclass(frozen=True)
class RbacVerdict:
    """What ``kubectl auth can-i`` said about one feature's permissions."""

    feature: RbacFeature
    missing: tuple[Grant, ...] = ()
    unknown: tuple[Grant, ...] = ()
    """Grants kubectl could not answer for — no reachable cluster, most
    likely. Deliberately not folded into ``missing``: "you are not allowed" and
    "nobody was asked" send a reader to different places."""

    @property
    def status(self) -> Status:
        if self.unknown:
            return Status.WARN
        if not self.missing:
            return Status.OK
        return Status.FAIL if self.feature.blocking else Status.WARN

    @property
    def detail(self) -> str:
        if self.unknown:
            return "not measured: kubectl could not answer"
        if not self.missing:
            return f"all {len(self.feature.grants)} verbs allowed"
        return "missing: " + ", ".join(str(grant) for grant in self.missing)


@dataclass(frozen=True)
class Report:
    """Everything ``doctor`` measured, in the order it prints it."""

    version: str
    image: str
    context: str | None
    namespace: str | None
    checks: tuple[Check, ...]
    verdicts: tuple[RbacVerdict, ...]

    @property
    def blockers(self) -> tuple[str, ...]:
        """The names of everything standing between here and an attach."""
        return tuple(
            item.name if isinstance(item, Check) else item.feature.name
            for item in (*self.checks, *self.verdicts)
            if item.status is Status.FAIL
        )

    @property
    def exit_code(self) -> int:
        """0 when nothing blocks the headline attach path, 1 when something does.

        A warning is not a blocker: a cluster that will not grant Iterate mode
        is a fact about that cluster, and reporting it as a failure would make
        the honest answer look like a broken tool — the same call
        :func:`podbench.launcher.main` makes about a degraded seat.
        """
        return 1 if self.blockers else 0


# -- the ssh Include --------------------------------------------------------


class IncludeState(enum.Enum):
    """Whether ``~/.ssh/config`` pulls podbench's stanzas in, and in time."""

    ACTIVE = "active"
    """Included before anything conditional, so podbench's keywords are the
    first values ssh obtains for the alias."""

    SHADOWED = "shadowed"
    """Included, but below a ``Host``/``Match`` block. The stanza is still read,
    so the alias resolves — but ssh takes the *first* value it sees for each
    keyword and ``Host *`` matches everything, so any keyword that block also
    sets has already won. A ``ControlPath`` from someone's dotfiles then
    silently replaces the short one podbench chose to stay under the 108-byte
    AF_UNIX limit."""

    MISSING = "missing"
    """Not included at all: ``ssh podbench-<pod>`` cannot resolve the alias."""


FIX_BANNER = "# Added by podbench doctor --fix."


def include_state(text: str, line: str) -> IncludeState:
    r"""Where ``line`` sits in an ssh config, if it is there at all.

    Paths are compared after ``~`` expansion, because the line podbench prints
    is absolute and the one a user typed usually is not.

    >>> line = "Include /home/dev/.podbench/config.d/*.conf"
    >>> include_state(line + "\n", line).name
    'ACTIVE'
    >>> include_state("Host *\n  User me\n" + line + "\n", line).name
    'SHADOWED'
    >>> include_state("Host build\n  User me\n", line).name
    'MISSING'
    """
    wanted = _expand(_directive(line)[1])
    conditional = False
    shadowed = False
    for raw in text.splitlines():
        keyword, arguments = _directive(raw)
        if keyword in ("host", "match"):
            conditional = True
        elif keyword == "include" and wanted & _expand(arguments):
            if not conditional:
                return IncludeState.ACTIVE
            shadowed = True
    return IncludeState.SHADOWED if shadowed else IncludeState.MISSING


def splice_include(text: str, line: str) -> str:
    r"""``text`` with ``line`` at the very top, and not one other byte changed.

    At the top because that is the only position that is right whatever the file
    already contains, and prepending is the only edit that cannot damage it: a
    fix that *moved* an existing line, or rewrote the user's ``Host *`` block to
    make room, would be podbench taking ownership of a file it has always
    refused to own.

    >>> print(splice_include("Host *\n    User me\n", "Include /x/*.conf"))
    # Added by podbench doctor --fix.
    Include /x/*.conf
    <BLANKLINE>
    Host *
        User me
    <BLANKLINE>
    """
    return f"{FIX_BANNER}\n{line}\n\n{text}"


def _directive(raw: str) -> tuple[str | None, list[str]]:
    """One ssh config line as ``(lowercased keyword, arguments)``.

    ``Include=path`` is as legal as ``Include path``, and an unbalanced quote is
    left alone rather than guessed at — a line podbench cannot parse is a line
    it must not claim to have understood.
    """
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        return None, []
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return None, []
    if not tokens:
        return None, []
    keyword, _, first = tokens[0].partition("=")
    return keyword.lower(), ([first] if first else []) + tokens[1:]


def _expand(arguments: Sequence[str]) -> set[str]:
    return {os.path.expanduser(argument) for argument in arguments}


def write_include(config: Path, line: str) -> str:
    """Splice ``line`` into ``config``; say what was done.

    Written through a temporary file and :func:`os.replace` rather than in
    place: a half-written ``~/.ssh/config`` locks the user out of every host
    they have, not only podbench's. The original file's mode is carried over for
    the same reason ssh cares about it at all.

    A symlink is followed rather than replaced. ``~/.ssh/config`` is very often a
    link into a dotfiles repository, and ``os.replace`` on the link itself would
    swap it for a regular file — leaving the user's own edits still going to a
    file ssh has stopped reading, which is the kind of ownership of their config
    this whole verb refuses to take.
    """
    target = config.resolve() if config.is_symlink() else config
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    existed = target.is_file()
    text = target.read_text() if existed else ""
    mode = stat.S_IMODE(target.stat().st_mode) if existed else 0o600
    temporary = target.with_name(target.name + ".podbench-new")
    temporary.write_text(splice_include(text, line))
    temporary.chmod(mode)
    os.replace(temporary, target)
    return f"inserted `{line}` at the top of {config}"


# -- the checks -------------------------------------------------------------


def client_version(text: str) -> tuple[int, int] | None:
    """``(major, minor)`` out of ``kubectl version --client -o json``.

    ``gitVersion`` rather than the ``major``/``minor`` fields beside it: those
    are strings a vendor build may spell ``"25+"``, and a distribution that
    ships them empty would otherwise read as version 0.

    >>> client_version('{"clientVersion": {"gitVersion": "v1.31.2"}}')
    (1, 31)
    >>> client_version("Client Version: v1.31.2") is None
    True
    """
    try:
        parsed: object = json.loads(text)
    except ValueError:
        return None
    git = as_dict(as_dict(parsed).get("clientVersion")).get("gitVersion")
    if not isinstance(git, str):
        return None
    found = re.match(r"v?(\d+)\.(\d+)", git)
    return (int(found.group(1)), int(found.group(2))) if found else None


def check_kubectl(binary: str, path: str | None, *, runner: Runner) -> Check:
    """``kubectl``, which is podbench's entire cluster interface and its auth."""
    if path is None:
        return Check(
            "kubectl",
            Status.FAIL,
            f"{binary} is not on PATH",
            "podbench shells out to kubectl for every cluster call, including "
            "the ssh transport, so that your kubeconfig, context and exec "
            "credential plugin are the only credential in play. Install "
            f"kubectl {_spelled(MIN_KUBECTL)} or later",
        )
    result = runner([binary, "version", "--client", "-o", "json"], timeout=UNBOUNDED)
    version = client_version(result.stdout)
    if version is None:
        return Check(
            "kubectl",
            Status.WARN,
            f"{path}, but `{binary} version --client -o json` named no version",
            f"podbench needs {_spelled(MIN_KUBECTL)} or later; check it by hand",
        )
    if version < MIN_KUBECTL:
        return Check(
            "kubectl",
            Status.FAIL,
            f"{path} is v{_spelled(version)}",
            f"ephemeral containers are stable from {_spelled(MIN_KUBECTL)}, and "
            "podbench posts to the pods/ephemeralcontainers subresource itself",
        )
    return Check("kubectl", Status.OK, f"v{_spelled(version)} at {path}")


def _spelled(version: tuple[int, int]) -> str:
    return f"{version[0]}.{version[1]}"


def kubeconfig_context(
    binary: str, context: str | None, *, runner: Runner
) -> str | None:
    """The context this run will use, or ``None`` when the kubeconfig names none."""
    if context is not None:
        return context
    result = runner([binary, "config", "current-context"], timeout=UNBOUNDED)
    name = result.stdout.strip()
    return name if result.returncode == 0 and name else None


def check_context(context: str | None) -> Check:
    """A kubeconfig with no current context authenticates nothing."""
    if context is None:
        return Check(
            "kubeconfig",
            Status.FAIL,
            "no current context",
            "kubectl config use-context NAME, or pass --context. The "
            "kubeconfig is podbench's only credential",
        )
    return Check("kubeconfig", Status.OK, f"context {context}")


def check_ssh_client(*, which: Which) -> Check:
    """The client half of the transport."""
    path = which("ssh")
    if path is None:
        return Check(
            "ssh client",
            Status.FAIL,
            "ssh is not on PATH",
            "the seat is reached by a real OpenSSH connection whose carrier is "
            "kubectl exec; install an ssh client",
        )
    return Check("ssh client", Status.OK, path)


def check_identity(identity: str) -> Check:
    """Both halves of the ssh key, named and never created.

    podbench authorises the public half inside the container and names the
    private one in the generated stanza, so a missing half is a login refused
    for reasons neither file explains. ``--fix`` deliberately does not run
    ``ssh-keygen``: a key minted by a diagnostic is a credential the user never
    chose, and podbench would then be authorising it in their cluster.
    """
    private, public = identity_paths(identity)
    absent = [str(path) for path in (private, public) if not path.is_file()]
    if absent:
        return Check(
            "ssh identity",
            Status.FAIL,
            "missing " + ", ".join(absent),
            "generate one yourself — ssh-keygen -t ed25519 — or point "
            "--identity at an existing key. doctor will not create one for you",
        )
    return Check("ssh identity", Status.OK, f"{private} and {public}")


AGENT_SOCKET = "SSH_AUTH_SOCK"
"""The variable that decides *what* signs with the identity, not merely whether.

When it is set, ssh offers the agent's keys before it reads any file, so an
identity the agent also holds is signed for by the agent and the private file is
never opened. ``IdentitiesOnly yes`` in the generated stanza does not change
that: it limits which keys are *offered*, not who signs for them.
"""

GNOME_KEYRING_SOCKET = re.compile(r"^/run/user/\d+/keyring/")
"""gnome-keyring standing in as the agent, as against ssh-agent's ``/tmp/ssh-*``.

It is the default on most desktop logins and has a long history of refusing to
sign with ED25519 keys, which is the failure that made this check exist. podbench
cannot prove it will refuse — only that it is the thing that would be asked.
"""

SSH_ADD_NO_AGENT = 2
"""``ssh-add``'s exit code for "could not open a connection to the agent".

Distinguished from 1, "the agent has no identities": a dead socket means ssh
falls back to the key file, an empty agent means it never had anything to offer.
Reporting either as the other would send a reader somewhere useless.
"""

SSH_ADD_LISTING_FAILED = 1
"""``ssh-add``'s exit code for the empty agent *and* for a listing that failed.

``list_identities()`` returns the same 1 whether the agent answered "nothing" or
never answered at all — an agent killed between the connect and the reply, a
forwarded agent's communication error, a non-OpenSSH agent's protocol error. The
streams are what tell them apart: the empty agent is printed to stdout, every
error to stderr. Read as an empty agent, a failed listing becomes "the agent does
not hold your key" — the reassuring answer, from a measurement that never
happened, about exactly the thing this check exists to name.
"""

_FINGERPRINT = re.compile(r"\b((?:SHA256|MD5):\S+)")
_ALGORITHM = re.compile(r"\(([A-Za-z0-9-]+)\)\s*$")


def key_fingerprint(line: str) -> tuple[str, str] | None:
    r"""``(fingerprint, algorithm)`` from one ``ssh-keygen -l``/``ssh-add -l`` line.

    The two commands print the same shape, which is the whole reason a file on
    disk can be compared against the contents of an agent without asking either
    of them to sign anything.

    Only the first line is read, and that is load-bearing rather than tidy: a
    ``.pub`` may legally hold more than one key, ``ssh-keygen -lf`` then prints
    one line per key, and the algorithm is anchored to the end of its line. Given
    the lot, the fingerprint would come from the first key and the algorithm from
    the last — and an ``sk-*`` key reported as ``RSA`` is told to set
    ``IdentityAgent none``, which stops it signing at all.

    >>> key_fingerprint("256 SHA256:Ql+7 dev@laptop (ED25519)")
    ('SHA256:Ql+7', 'ED25519')
    >>> key_fingerprint("256 SHA256:a d (ED25519-SK)\n3072 SHA256:b d (RSA)")
    ('SHA256:a', 'ED25519-SK')
    >>> key_fingerprint("The agent has no identities.") is None
    True
    """
    first = line.partition("\n")[0]
    found = _FINGERPRINT.search(first)
    if found is None:
        return None
    algorithm = _ALGORITHM.search(first.strip())
    return found.group(1), algorithm.group(1) if algorithm else ""


def agent_fingerprints(text: str) -> frozenset[str]:
    r"""Every fingerprint ``ssh-add -l`` listed.

    >>> sorted(agent_fingerprints("256 SHA256:a x (ED25519)\n256 SHA256:b y (RSA)"))
    ['SHA256:a', 'SHA256:b']
    """
    found = (key_fingerprint(line) for line in text.splitlines())
    return frozenset(entry[0] for entry in found if entry is not None)


_IDENTITY_AGENT = re.compile(r"^identityagent\s+(\S.*)$", re.IGNORECASE | re.MULTILINE)


def effective_identity_agent(alias: str, *, runner: Runner, which: Which) -> str | None:
    """What ssh itself says will hold the identity for ``alias``.

    ``ssh -G`` resolves the whole config the way a connection would — includes,
    ``Host`` patterns, first-match-wins — which is the only way this module can
    observe the one keyword it recommends. Without it the warning would survive
    being acted on, and a diagnostic that cannot see its own advice taken teaches
    people to stop reading it.

    ``None`` when ssh could not be asked or said nothing: ``-G`` prints
    ``identityagent`` only when the keyword is set, so its absence means the
    default, which is the agent.
    """
    if which("ssh") is None:
        return None
    # -o on the command line outranks the file, and CanonicalizeHostname would
    # otherwise send -G to DNS: doctor must not block on the network to answer a
    # question about a local file.
    result = runner(
        ["ssh", "-G", "-o", "CanonicalizeHostname=no", alias], timeout=UNBOUNDED
    )
    if result.returncode != 0:
        return None
    found = _IDENTITY_AGENT.search(result.stdout)
    return None if found is None else found.group(1).strip()


def check_ssh_agent(
    identity: str, *, namespace: str | None, runner: Runner, which: Which
) -> Check:
    """Which thing will sign with the identity — the agent, or the file.

    The failure this precedes is entirely client-side and reads as the opposite:
    ssh matches the identity to a key the agent holds, asks the agent to sign,
    the agent refuses, and what reaches the user is ``Permission denied
    (publickey,keyboard-interactive)`` — which sends them to the seat's passwd
    record or the authorised key, neither of which is in play.
    Membership is inferred rather than a signature demanded, so this is a
    ``WARN``: it names what would be asked, not what it would answer.

    Every path a measurement can fail on reports *not measured* rather than an
    answer. "The agent does not hold your key" is the reassuring half of this
    check, and it is the one that must never be guessed.
    """
    socket = os.environ.get(AGENT_SOCKET, "")
    private, public = identity_paths(identity)
    if not socket:
        if not private.is_file():
            return Check(
                "ssh agent",
                Status.WARN,
                f"{AGENT_SOCKET} unset and {private} is missing: there is "
                "nothing left that could sign with this identity",
            )
        return Check(
            "ssh agent",
            Status.OK,
            f"{AGENT_SOCKET} unset: ssh signs with {private} itself, so expect "
            "its passphrase prompt, or a touch if it is a FIDO key",
        )
    if not public.is_file():
        return Check(
            "ssh agent",
            Status.WARN,
            f"not measured: {public} is missing, so the agent's keys have "
            "nothing to be compared against",
        )
    if which("ssh-add") is None or which("ssh-keygen") is None:
        return Check(
            "ssh agent",
            Status.WARN,
            "not measured: ssh-add or ssh-keygen is not on PATH",
        )
    printed = runner(["ssh-keygen", "-lf", str(public)], timeout=UNBOUNDED)
    fingerprinted = key_fingerprint(printed.stdout)
    if fingerprinted is None:
        return Check(
            "ssh agent",
            Status.WARN,
            f"not measured: ssh-keygen printed no fingerprint for "
            f"{public}{_said(printed)}",
        )
    fingerprint, algorithm = fingerprinted
    listed = runner(["ssh-add", "-l"], timeout=UNBOUNDED)
    if listed.returncode == SSH_ADD_NO_AGENT:
        return Check(
            "ssh agent",
            Status.WARN,
            f"{AGENT_SOCKET}={socket}, but no agent answered on it",
            "ssh falls back to the key file when the socket is dead, so this "
            "does not block an attach — but a stale SSH_AUTH_SOCK is worth "
            "clearing before it becomes the explanation for something else",
        )
    if listed.returncode != 0 and (
        listed.returncode != SSH_ADD_LISTING_FAILED or listed.stderr.strip()
    ):
        return Check(
            "ssh agent",
            Status.WARN,
            f"not measured: ssh-add -l exited {listed.returncode} against "
            f"{socket}{_said(listed)}",
            "an agent that cannot be listed cannot be ruled out — ssh will "
            "still offer it the identity, so this is unknown rather than absent",
        )
    if fingerprint not in agent_fingerprints(listed.stdout):
        return Check(
            "ssh agent",
            Status.OK,
            f"agent on {socket} does not hold {fingerprint}: ssh signs with "
            f"{private} itself",
        )
    alias = default_host_alias(PodRef(namespace, "pod")) if namespace else None
    if alias is not None and _agent_is_off(alias, runner=runner, which=which):
        return Check(
            "ssh agent",
            Status.OK,
            f"agent on {socket} holds {fingerprint}, but your ssh config sets "
            f"IdentityAgent none for {alias}: ssh signs with {private} itself",
        )
    return Check(
        "ssh agent",
        Status.WARN,
        f"agent on {socket} holds {fingerprint}: ssh will sign with the AGENT, "
        f"not with {private}",
        _agent_hint(socket, algorithm, namespace),
    )


def _said(result: CommandResult) -> str:
    """What a probe that answered nothing useful put on a stream.

    The single line that explains a refusal — a ``.pub`` whose mode ssh-keygen
    cannot read, an agent that stopped answering — is on stderr, and "not
    measured" with the reason dropped is a fact the reader cannot act on.
    """
    spoke = result.stderr.strip() or result.stdout.strip()
    return f": {spoke.splitlines()[0]}" if spoke else ""


def _agent_is_off(alias: str, *, runner: Runner, which: Which) -> bool:
    """Whether the user's own config has already taken the agent out of play."""
    keyword = effective_identity_agent(alias, runner=runner, which=which)
    return keyword is not None and keyword.lower() == "none"


def _agent_hint(socket: str, algorithm: str, namespace: str | None) -> str:
    """What to type when it is the agent, and not podbench, that refuses.

    Both escapes, in the order a reader needs them: one settles who is at fault,
    the other changes it. ``IdentityAgent none`` is never offered unqualified —
    a FIDO/``sk-*`` key or a smartcard has no private half on disk and can *only*
    sign through an agent, so the fix breaks exactly the keys most likely to be
    in one deliberately. Applying it is not this verb's job either: doctor
    diagnoses, and the generated stanza is rewritten on every attach, so the
    keyword is named where a user's own config can hold it.

    The keyword's placement is stated because the obvious one is wrong: pasted
    above the ``Include``, a ``Host`` block leaves doctor's own include check
    reporting ``shadowed`` on the next run, which is a foot-gun this hint would
    have handed out itself.
    """
    lines: list[str] = []
    if GNOME_KEYRING_SOCKET.match(socket):
        lines.append(
            "that socket is gnome-keyring standing in for ssh-agent, which has "
            "a long history of refusing ED25519 keys with `agent refused "
            "operation`"
        )
    spelled = default_host_alias(PodRef(namespace or "<namespace>", "<pod>"))
    lines.append(
        f"prove it is the agent and not the seat:  SSH_AUTH_SOCK= ssh {spelled}"
    )
    # "-SK" rather than a suffix test: a certificate over an sk-* key prints
    # ED25519-SK-CERT, and the key underneath is just as unable to sign without
    # its token.
    if "-SK" in algorithm.upper():
        lines.append(
            f"do not set IdentityAgent none for this key: a {algorithm} key has "
            "no private half on disk and can only sign through an agent"
        )
        return "\n".join(lines)
    lines.append(
        "if it refuses, sign with the file instead — put this in ~/.ssh/config "
        "below the Include line, where it cannot shadow the generated stanza:"
    )
    lines.append("    Host podbench-*")
    lines.append("        IdentityAgent none")
    lines.append(
        "never for a FIDO/sk-* key or a smartcard, though: those can only sign "
        "through an agent"
    )
    return "\n".join(lines)


def check_config_dir(directory: Path, *, fix: bool) -> Check:
    """The directory the generated per-pod stanzas land in.

    A warning rather than a blocker: ``attach`` creates it when it writes the
    first stanza. It is checked because the ``Include`` glob is the thing that
    has to name it, and a reader comparing the two wants both in front of them.
    """
    config_d = directory / CONFIG_D
    fixed: str | None = None
    if fix and not config_d.is_dir():
        config_d.mkdir(mode=0o700, parents=True, exist_ok=True)
        fixed = f"created {config_d}"
    if not config_d.is_dir():
        return Check(
            "config dir",
            Status.WARN,
            f"{config_d} does not exist yet",
            "attach creates it when it writes the first stanza; "
            "podbench doctor --fix creates it now",
        )
    return Check("config dir", Status.OK, str(config_d), fixed=fixed)


def check_include(config: Path, directory: Path, *, fix: bool) -> Check:
    """The one hand-edit podbench's setup has ever asked for.

    ``--fix`` prepends the line; without it the exact line is printed, because
    the user may keep their ssh config under configuration management and a
    diagnostic that edits it anyway is worse than one that tells them what to
    add.
    """
    line = ssh_include_line(directory)
    text = config.read_text() if config.is_file() else ""
    state = include_state(text, line)
    fixed: str | None = None
    if fix and state is not IncludeState.ACTIVE:
        fixed = write_include(config, line)
        state = include_state(config.read_text(), line)
    if state is IncludeState.ACTIVE:
        return Check("ssh include", Status.OK, f"{config} includes {line}", fixed=fixed)
    if state is IncludeState.SHADOWED:
        return Check(
            "ssh include",
            Status.WARN,
            f"{config} includes it below a Host or Match block",
            "ssh takes the first value it sees for each keyword and Host * "
            "matches everything, so those keywords win over podbench's. "
            "podbench doctor --fix adds the line at the top",
            fixed=fixed,
        )
    return Check(
        "ssh include",
        Status.FAIL,
        f"{config} does not include the generated stanzas",
        f"add this line above any Host * block:  {line}\n"
        "or run:  podbench doctor --fix",
        fixed=fixed,
    )


def can_i(kubectl: Kubectl, grant: Grant) -> bool | None:
    """Whether this kubeconfig may do that, or ``None`` if kubectl could not say.

    One ``SelfSubjectAccessReview`` per grant. ``auth can-i --list`` would be a
    single round trip, but its answer is a table of wildcards that has to be
    re-implemented here to be read — and a resolver of ours getting that subtly
    wrong is exactly the silent wrong answer this verb exists to remove.
    """
    result = kubectl.run("auth", "can-i", *grant.argv, check=False)
    answer = result.stdout.strip().lower()
    if answer.startswith("yes"):
        return True
    if answer.startswith("no"):
        return False
    return None


def feature_verdict(kubectl: Kubectl, feature: RbacFeature) -> RbacVerdict:
    """Ask about every grant one feature needs."""
    missing: list[Grant] = []
    unknown: list[Grant] = []
    for grant in feature.grants:
        allowed = can_i(kubectl, grant)
        if allowed is None:
            unknown.append(grant)
        elif not allowed:
            missing.append(grant)
    return RbacVerdict(feature, tuple(missing), tuple(unknown))


def diagnose(
    *,
    binary: str = "kubectl",
    context: str | None = None,
    namespace: str | None = None,
    identity: str = DEFAULT_IDENTITY,
    config_dir: str | None = None,
    fix: bool = False,
    runner: Runner = run_subprocess,
    which: Which = shutil.which,
) -> Report:
    """Measure everything, in one pass, applying ``--fix`` as it goes."""
    checks: list[Check] = []
    path = which(binary)
    checks.append(check_kubectl(binary, path, runner=runner))

    resolved_context: str | None = None
    resolved_namespace: str | None = None
    verdicts: list[RbacVerdict] = []
    if path is None:
        # Nothing below this line can be measured without the binary, and
        # reporting "you are not allowed" for a question nobody asked would be
        # an invention.
        verdicts = [
            RbacVerdict(feature, unknown=feature.grants) for feature in FEATURES
        ]
    else:
        resolved_context = kubeconfig_context(binary, context, runner=runner)
        checks.append(check_context(resolved_context))
        resolved_namespace = namespace or current_namespace(
            binary=binary, context=context, runner=runner
        )
        kubectl = Kubectl(
            resolved_namespace, context=context, binary=binary, runner=runner
        )
        verdicts = [feature_verdict(kubectl, feature) for feature in FEATURES]

    checks.append(check_ssh_client(which=which))
    checks.append(check_identity(identity))
    checks.append(
        check_ssh_agent(
            identity, namespace=resolved_namespace, runner=runner, which=which
        )
    )
    directory = client_dir(config_dir)
    checks.append(check_config_dir(directory, fix=fix))
    checks.append(check_include(Path(SSH_CONFIG).expanduser(), directory, fix=fix))

    return Report(
        version=__version__,
        image=os.environ.get(IMAGE_ENV, DEFAULT_IMAGE),
        context=resolved_context,
        namespace=resolved_namespace,
        checks=tuple(checks),
        verdicts=tuple(verdicts),
    )


# -- reporting --------------------------------------------------------------


def format_report(report: Report) -> str:
    """The human report. One line per check, and the verdict last.

    Shaped like ``capreport``'s for the same reason it is: the facts first so a
    reader can see what was measured, then a verdict that names the blockers
    rather than summarising them away. Which is also why the sections shout
    where ``attach``'s are lower-case — :mod:`podbench.console` recognises both
    and colours them the same, so the two conventions cost nothing to keep.

    Every hint is wrapped. This is the first thing a new user runs and the
    hints are the half of it that tells them what to do, and until they were
    wrapped the longest of them — the one that says why podbench shells out to
    kubectl — ran off the side of the terminal it was explaining.
    """
    lines = [rule("podbench doctor")]
    lines.append("THIS MACHINE")
    lines.append(_fact("launcher", report.version))
    lines.append(_fact("image", report.image))
    lines.append(_fact("context", report.context or "none"))
    lines.append(_fact("namespace", report.namespace or "unknown"))

    lines.append("CHECKS")
    for check in report.checks:
        lines.extend(_row(check.status, check.name, check.detail))
        if check.fixed is not None:
            lines.extend(_note(f"fixed: {check.fixed}"))
        if check.status is not Status.OK and check.hint is not None:
            # Split first: a hint that carries its own newlines is a hint whose
            # lines are a list (the Include line to paste, then the verb that
            # pastes it), and re-flowing those into one paragraph would put a
            # command in the middle of a sentence.
            for part in check.hint.splitlines():
                lines.extend(_note(part))

    where = report.namespace or "this namespace"
    lines.append(f"RBAC in {where} (kubectl auth can-i, as your kubeconfig's user)")
    for verdict in report.verdicts:
        lines.extend(_row(verdict.status, verdict.feature.name, verdict.detail))
        if verdict.missing:
            lines.extend(
                _note(
                    "grant it with the chart's rbac."
                    f"{verdict.feature.chart_flag}=true, or the equivalent Role"
                )
            )

    lines.append(rule(char="-"))
    if report.blockers:
        count = len(report.blockers)
        lines.append(
            f"VERDICT: {count} blocker{'' if count == 1 else 's'} before "
            f"`podbench attach` can work (exit {report.exit_code})"
        )
        lines.extend(paragraph(", ".join(report.blockers), first="BLOCKERS: "))
    else:
        lines.append(
            "VERDICT: nothing blocks `podbench attach` from this machine "
            f"(exit {report.exit_code})"
        )
    lines.append(rule())
    return "\n".join(lines)


_INDENT = 10
"""Where a row's name starts, and so where its follow-up lines are indented to."""

_NAMES = 14
"""Width of the name column, which is also the hang of a detail that wraps."""


def _fact(key: str, value: str) -> str:
    return f"  {key:<{_NAMES}} {value}"


def _row(status: Status, name: str, detail: str) -> list[str]:
    """One check: its status, its name, and what was measured.

    The detail hangs under itself rather than under the name, so a wrapped one
    cannot be read as the next check's — the status column is the thing the eye
    is running down, and it must stay empty for exactly one row.
    """
    lead = f"  [{status.value}]".ljust(_INDENT) + f"{name:<{_NAMES}} "
    return paragraph(detail, first=lead, indent=" " * len(lead))


def _note(text: str) -> list[str]:
    """A hint line: as written where it fits, re-flowed only where it must.

    Several of these are ``do this:  <command>`` offers, and the two spaces
    before the command are what marks it as one. Such a line is printed exactly
    as its check wrote it however long it is: wrapping collapses whitespace and
    breaks on spaces, and both of those turn the one line here that exists to be
    *pasted* — the ssh ``Include`` — into two that cannot be.

    Everything else is prose and wraps, which is the half of this report that
    tells a new user what to do and, until it wrapped, the half that ran off the
    side of the terminal it was explaining.
    """
    indent = " " * _INDENT
    if "  " in text.strip():
        return [indent + text]
    return paragraph(text, first=indent, indent=" " * (_INDENT + 2))


# -- CLI --------------------------------------------------------------------


def main(
    args: Sequence[str] | None = None,
    *,
    runner: Runner | None = None,
    which: Which | None = None,
) -> int:
    """Report on this machine and return 0 only if nothing blocks an attach.

    ``runner`` and ``which`` are the seams the tests drive; the CLI passes
    neither. They are why the app is built here rather than at import time —
    the command closes over them.
    """
    app = new_app()

    @app.command()
    def doctor(
        fix: Annotated[
            bool,
            typer.Option(
                "--fix",
                help="make the two changes podbench can make safely: create the "
                "config directory, and add the ssh Include above any Host * "
                "block. Never creates an ssh key",
            ),
        ] = False,
        identity: Annotated[
            str,
            typer.Option(
                "--identity", metavar="KEY", help="the ssh key attach would use"
            ),
        ] = DEFAULT_IDENTITY,
        namespace: Annotated[
            str | None,
            typer.Option(
                "-n",
                "--namespace",
                metavar="NAMESPACE",
                help="namespace to test RBAC in (default: the context's own)",
            ),
        ] = None,
        context: Annotated[
            str | None,
            typer.Option("--context", metavar="NAME", help="kubeconfig context"),
        ] = None,
        kubectl: Annotated[
            str, typer.Option("--kubectl", metavar="BIN", help="kubectl binary to use")
        ] = "kubectl",
        config_dir: Annotated[
            str | None,
            typer.Option(
                "--config-dir",
                metavar="DIR",
                help="where the generated ssh config and known_hosts live "
                f"(default {DEFAULT_CLIENT_DIR})",
            ),
        ] = None,
    ) -> None:
        """Name what will block the first attach from this machine."""
        report = diagnose(
            binary=kubectl,
            context=context,
            namespace=namespace,
            identity=identity,
            config_dir=config_dir,
            fix=fix,
            runner=runner if runner is not None else run_subprocess,
            which=which if which is not None else shutil.which,
        )
        emit(format_report(report))
        raise typer.Exit(report.exit_code)

    return run(app, args, prog="podbench doctor")
