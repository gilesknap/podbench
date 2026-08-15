"""``podbench agent`` — the debug container's PID 1.

The agent's whole job is to make the container reconnectable. Ephemeral
containers cannot be restarted and their name is burnt for the pod's lifetime,
a user may attach a second session into a container that is already serving
one, and an OOM or a pod restart hands us a completely fresh rootfs with new
host keys. So every step here is *ensure*, never *create*: running the agent
twice against the same container is normal operation, not an error path.

Nothing it writes may be the only copy of anything. The host key, the
authorized keys and the sshd config are all rebuilt from the environment or a
mounted Secret on each start, which is what makes "the ephemeral container is
strictly disposable" true rather than aspirational.

And no ensure step may be fatal. The agent is PID 1 of a container that cannot
be restarted and whose name is burnt for the pod's lifetime the moment it exits
(report 4.2), so a step that cannot do its job records the reason and the agent
idles anyway: ``capreport``, ``pids`` and ``dbg --launch`` are reached by
``kubectl exec`` and need nothing sshd needs. Spike S5's degraded rung is most
of a seat without ssh and none of one with a dead container.
"""

from __future__ import annotations

import os
import pwd
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer

from .cli import new_app, run
from .model import SEAT_HOME_VOLUME, SEAT_IDENTITY_VOLUME, ContainerRef, PodRef
from .sshcfg import SEAT_USER, SshdLayout, proxy_command, sshd_config

__all__ = [
    "request_stop",
    "AUTHORIZED_KEYS_FILE_ENV",
    "AUTHORIZED_KEYS_MOUNT",
    "GROUP_PATH",
    "HOME_WAY_OUT",
    "HOST_KEY_ENV",
    "HOST_KEY_FILE_ENV",
    "HOST_KEY_MOUNT",
    "IDLE_SLICE",
    "LOGIN_SHELL",
    "NSS_WAY_OUT",
    "PASSWD_PATH",
    "PUBKEY_ENV",
    "CheckResult",
    "CommandRunner",
    "EnsureReport",
    "ReaperStatus",
    "ensure_all",
    "ensure_authorized_keys",
    "ensure_home_dir",
    "ensure_host_key",
    "ensure_passwd_entry",
    "ensure_privsep_dir",
    "ensure_sshd_config",
    "fd2_check",
    "idle",
    "login_name",
    "main",
    "nss_identity_check",
    "passwd_line",
    "read_host_public_key",
    "reap_children",
    "reaper_status",
    "run_command",
    "proxy_shape_check",
    "stdio_roundtrip_check",
    "self_check",
]

PUBKEY_ENV = "PODBENCH_SSH_PUBKEY"
AUTHORIZED_KEYS_FILE_ENV = "PODBENCH_SSH_PUBKEY_FILE"
HOST_KEY_ENV = "PODBENCH_SSH_HOST_KEY"
HOST_KEY_FILE_ENV = "PODBENCH_SSH_HOST_KEY_FILE"

AUTHORIZED_KEYS_MOUNT = "/etc/podbench/ssh/authorized_keys"
HOST_KEY_MOUNT = "/etc/podbench/ssh/ssh_host_ed25519_key"
"""Default mount points for a Secret. A key delivered this way survives the pod
restarts that destroy an ephemeral container's writable layer, which is the only
way the client's ``known_hosts`` entry can outlive a single attach."""

CommandRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
"""How the agent runs a helper binary — ``ssh-keygen``, ``sshd -t``.

Named for what it runs, because :class:`podbench.kubectl.Runner` is a different
thing entirely (it runs ``kubectl``, and returns podbench's own result type),
and a launcher importing both had two ``Runner``s to keep straight.
"""

IDLE_SLICE = 0.5
"""Longest the idle loop will sleep without checking whether it was asked to
stop. Small enough that SIGTERM is acted on well inside the kubelet's grace
period, large enough that the loop costs nothing measurable."""

_stop_requested = False


def _say(message: str) -> None:
    """Progress goes to stderr so stdout stays parseable by the launcher."""
    print(f"agent: {message}", file=sys.stderr)


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a helper binary, capturing both streams. The single shell-out seam."""
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class CheckResult:
    """One startup check, and why it says what it says."""

    name: str
    ok: bool
    detail: str

    def __str__(self) -> str:
        return f"[{'ok  ' if self.ok else 'FAIL'}] {self.name}: {self.detail}"


def _write_if_changed(path: Path, content: str, mode: int) -> bool:
    """Write ``content`` only when it differs; return whether anything changed.

    Rewriting an identical file would be harmless for the file but not for the
    caller: a second attach reports what it did, and "changed nothing" is the
    answer that tells the user the first session is untouched.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else None
    if existing == content:
        if path.stat().st_mode & 0o777 != mode:
            path.chmod(mode)
            return True
        return False
    path.write_text(content)
    path.chmod(mode)
    return True


def ensure_privsep_dir(layout: SshdLayout) -> bool:
    """Create ``/run/sshd`` when we are root.

    ``/run`` is frequently a tmpfs, so the directory the image built is not
    necessarily the directory the container boots with; without it every
    connection dies with ``Missing privilege separation directory``. sshd skips
    privilege separation entirely when it is not uid 0, so for the non-root
    layout there is nothing to do.
    """
    if layout.privsep_dir is None:
        return False
    directory = Path(layout.privsep_dir)
    if directory.is_dir():
        return False
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    return True


def _home_directories(layout: SshdLayout) -> list[Path]:
    """The directories under ``$HOME`` the layout expects to already exist.

    Only the ones *under* home: the root layout keeps its config in ``/etc``,
    which the image owns and this step has no business creating.
    """
    home = Path(layout.home)
    wanted = (
        Path(layout.authorized_keys_path).parent,
        Path(layout.config_path).parent,
        Path(layout.host_key_path).parent,
    )
    return [path for path in dict.fromkeys(wanted) if home in path.parents]


def ensure_home_dir(layout: SshdLayout) -> bool:
    """Make ``$HOME`` exist, be writable, and carry the layout's directories.

    A seat pointed at :data:`podbench.model.SEAT_HOME_VOLUME` gets an *empty*
    volume: the kubelet creates the mount point and nothing else, so the
    ``.ssh`` and ``.podbench`` directories the host key, the authorized keys and
    the sshd config live in are simply not there. sshd is not told to create
    them - it refuses the login instead - so this is the step that makes a
    mounted home usable rather than merely present.

    Writability is checked rather than assumed because the interesting failure
    is not ours: an empty volume is owned by root:root until the pod's
    ``fsGroup`` hands it to the seat's group, and a seat running as the target's
    uid cannot chown its way out. See :data:`HOME_WAY_OUT`.
    """
    home = Path(layout.home)
    created = False
    try:
        if not home.is_dir():
            home.mkdir(mode=0o755, parents=True, exist_ok=True)
            created = True
        if not os.access(home, os.W_OK):
            raise RuntimeError(
                f"{home} is not writable by uid {os.geteuid()} / gid "
                f"{os.getegid()}, so this seat has no home to keep sshd's files "
                f"in. {HOME_WAY_OUT}"
            )
        for directory in _home_directories(layout):
            if directory.is_dir():
                continue
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            created = True
    except OSError as error:
        raise RuntimeError(
            f"{home} could not be prepared: {error}. {HOME_WAY_OUT}"
        ) from error
    return created


PASSWD_PATH = "/etc/passwd"
"""The NSS ``files`` database sshd resolves a login name in."""

GROUP_PATH = "/etc/group"
"""The NSS ``files`` database ``getgrgid`` answers from.

Named here beside :data:`PASSWD_PATH` because a ``podbench dev`` sidecar mounts
both from :data:`podbench.model.SEAT_IDENTITY_VOLUME` and the two halves have to
spell the paths identically. A missing group record is not fatal the way a
missing passwd record is - it costs ``id`` and ``ls -l`` a name, not the login.
"""

LOGIN_SHELL = "/bin/bash"
"""Shell recorded in a registered entry. bash is in the image; a passwd entry
naming a shell that is not would give the user a session that exits at once."""

NSS_WAY_OUT = (
    "sshd resolves the login name through NSS before it will look at a key, so "
    "ssh into this seat cannot work. Everything reached by kubectl exec - "
    "capreport, pids, dbg --launch, a shell - is unaffected. A seat that reads "
    "this is almost always an ephemeral container, and the way out for one is "
    "GID 0: land it again with "
    "`podbench attach <pod> --new --seat-gid-root` and it registers its "
    f"own record in the image's group-writable {PASSWD_PATH}. Failing that, run "
    "the target as a uid the debug image already has an account for. A "
    f"{SEAT_IDENTITY_VOLUME!r} volume does not help here, however plainly the "
    "pod declares one: projecting a passwd file takes a subPath per mount and "
    "an ephemeral container may not have one. That volume is the identity a "
    "`podbench dev` sidecar gets, which is an ordinary container."
)
"""Named mechanism, then the way out - the shape :class:`podbench.model.Blocker`
uses, because "No user exists for uid 1000" names neither.

Written for the container it is *read* in. Every path that quotes it - the
registration failure, ``nss-identity``, and the launcher, which prints it
verbatim under the missing ssh stanza - is reached from a seat that got here by
running as a uid it has no account for, and on a live pod that seat is an
ephemeral container. So the ephemeral route leads and the volume is named only
to stop somebody deploying it in the hope it fixes this."""

HOME_WAY_OUT = (
    f"The usual cause is a missing fsGroup. A {SEAT_HOME_VOLUME!r} volume is "
    "created root:root and stays that way until the pod's securityContext."
    "fsGroup makes the kubelet chgrp it to that group - and a seat running as "
    "the target's uid can chown nothing. Set fsGroup to the application's gid in "
    "workload's pod spec, the same number the seat identity's group record "
    "carries. Until then ssh has nowhere to keep a host key; everything kubectl "
    "exec reaches is unaffected."
)
"""Why an unwritable ``$HOME`` happens, and what fixes it.

The seat cannot fix this itself, and the message is the only place the cause is
named: an empty volume with no ``fsGroup`` looks exactly like a permissions bug
in podbench from inside the container.
"""


def login_name(uid: int | None = None) -> str | None:
    """The login name NSS resolves for ``uid``, or ``None`` when it resolves none.

    Asked of the platform rather than read out of ``/etc/passwd``: sshd itself
    calls ``getpwuid``/``getpwnam``, so an image whose identities arrive from
    some other NSS source is one podbench has to agree with rather than
    contradict. It is also the exact call that kills ``ssh-keygen`` - "No user
    exists for uid 1000" is ``getpwuid`` failing, and no ``-C comment`` avoids
    it, because ssh-keygen asks regardless of whether it needs an answer.
    """
    try:
        return pwd.getpwuid(os.geteuid() if uid is None else uid).pw_name
    except KeyError:
        return None


def _uid_named(user: str) -> int | None:
    """The uid NSS resolves ``user`` to, or ``None``."""
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None


def passwd_line(*, uid: int, gid: int, home: str, user: str = SEAT_USER) -> str:
    """One ``/etc/passwd`` record for the seat's own uid."""
    return f"{user}:x:{uid}:{gid}:{user}:{home}:{LOGIN_SHELL}\n"


def _registration_blocker(uid: int, gid: int, path: Path) -> str | None:
    """Why this container cannot add its own passwd entry, or ``None``."""
    if not path.is_file():
        return f"there is no {path} to add one to"
    taken = _uid_named(SEAT_USER)
    if taken is not None:
        # sshd resolves the *name* the client offered, so a second record for
        # the same name would never be reached: the first one wins and the login
        # would be attempted as the wrong uid.
        return (
            f"the login name {SEAT_USER!r} already belongs to uid {taken} in "
            f"{path}, and sshd resolves the name before the uid"
        )
    if not os.access(path, os.W_OK):
        return f"{path} is not writable by uid {uid} / gid {gid}"
    return None


def ensure_passwd_entry(
    layout: SshdLayout,
    *,
    uid: int | None = None,
    gid: int | None = None,
    passwd_path: str | None = None,
) -> bool:
    """Give the running uid an NSS identity, when the container is able to.

    The degraded rung runs as the *target's* uid, which is discovered at attach
    time from a pod podbench did not build - so no account for it can exist in
    the debug image, and none can be pre-baked that would match. Without one,
    ``ssh-keygen`` dies with "No user exists for uid <n>" before sshd is ever
    started, and sshd would refuse the login even if it had a host key.

    Which of the two mechanisms applies is decided by the kind of container this
    is, not by anything measured here:

    * an **ephemeral** seat - ``attach``, and so the common case - can be given
      no passwd file at all: projecting one takes a ``subPath`` per mount and the
      API server forbids ``subPath`` on an ephemeral container. Registration
      here is its *only* route, and it needs GID 0
      (``attach --new --seat-gid-root``);
    * a **``podbench dev``** sidecar is an ordinary container, so
      :func:`podbench.spec.dev_pod_spec` mounts
      :data:`podbench.model.SEAT_IDENTITY_VOLUME` read-only over ``/etc/passwd``
      and NSS resolves the uid before this step ever runs.

    That second shape is why the ``login_name`` check comes first and returns
    without looking at the file's mode: a read-only ``/etc/passwd`` that already
    carries the identity is the *success* case, and treating it as a refusal
    would report the one shape that works as the one that does not.

    The registration convention is the one containers running as an arbitrary
    uid already use (OpenShift's): ``/etc/passwd`` is group-writable by GID 0
    and the entrypoint appends its own record. That works only when the seat
    really does run with GID 0, so this step is *allowed to fail* - it raises,
    :func:`ensure_all` records the reason, and the container lands without ssh
    rather than not landing at all.

    Idempotent like every other ensure step: a uid NSS already resolves is left
    alone, which is also the whole of the root case.
    """
    own_uid = os.geteuid() if uid is None else uid
    own_gid = os.getegid() if gid is None else gid
    if login_name(own_uid) is not None:
        return False

    path = Path(PASSWD_PATH if passwd_path is None else passwd_path)
    blocker = _registration_blocker(own_uid, own_gid, path)
    if blocker is not None:
        raise RuntimeError(
            f"uid {own_uid} has no NSS entry and this container cannot add one: "
            f"{blocker}. {NSS_WAY_OUT}"
        )

    entry = passwd_line(uid=own_uid, gid=own_gid, home=layout.home)
    existing = path.read_text()
    if entry.strip() in existing.splitlines():
        # Written by a previous run and still not resolving: appending a second
        # copy would not help, and a second attach must not grow the file.
        raise RuntimeError(
            f"{path} already carries {entry.strip()!r} and NSS still does not "
            f"resolve uid {own_uid}; this image's nsswitch.conf does not appear "
            f"to consult {path}. {NSS_WAY_OUT}"
        )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(("" if existing.endswith("\n") or not existing else "\n") + entry)
    if login_name(own_uid) is None:
        raise RuntimeError(
            f"appended {entry.strip()!r} to {path} and NSS still does not "
            f"resolve uid {own_uid}. {NSS_WAY_OUT}"
        )
    return True


def nss_identity_check(
    *, uid: int | None = None, gid: int | None = None, passwd_path: str | None = None
) -> CheckResult:
    """Whether sshd can resolve a login name for the uid this container runs as.

    Re-derived from the platform rather than remembered, so it is the same
    answer whether it is reached through the start-up path or through
    ``podbench agent --self-check`` over ``kubectl exec`` seconds later.
    """
    own_uid = os.geteuid() if uid is None else uid
    own_gid = os.getegid() if gid is None else gid
    name = login_name(own_uid)
    if name is not None:
        return CheckResult(
            "nss-identity", True, f"uid {own_uid} resolves to the login name {name!r}"
        )
    path = Path(PASSWD_PATH if passwd_path is None else passwd_path)
    blocker = _registration_blocker(own_uid, own_gid, path)
    if blocker is None:
        return CheckResult(
            "nss-identity",
            False,
            f"uid {own_uid} has no NSS entry even though {path} is writable - "
            "the agent's registration step failed; its reason is in the "
            "container's start-up output",
        )
    return CheckResult(
        "nss-identity",
        False,
        f"uid {own_uid} has no NSS entry and this container cannot add one: "
        f"{blocker}. {NSS_WAY_OUT}",
    )


SESSION_ENV_PREFIX = "PODBENCH_"
"""Variables forwarded from the container's environment into ssh sessions.

The launcher injects the target's container id and the node name into the
container spec, and sshd does not pass its own environment to the commands it
runs — so without this the helpers fall back to guessing which processes belong
to the target, and say the node is unknown. Only podbench's own variables are
forwarded, and the ssh public key is excluded: it is already installed in
authorized_keys and has no business in a session environment.
"""

_SESSION_ENV_EXCLUDE = frozenset({PUBKEY_ENV, HOST_KEY_ENV})


def session_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The PODBENCH_* variables worth forwarding into an ssh session."""
    source = os.environ if env is None else env
    return {
        name: value
        for name, value in source.items()
        if name.startswith(SESSION_ENV_PREFIX) and name not in _SESSION_ENV_EXCLUDE
    }


def ensure_sshd_config(
    layout: SshdLayout, *, env: Mapping[str, str] | None = None
) -> bool:
    """Write the sshd config the ProxyCommand names with ``-f``."""
    return _write_if_changed(
        Path(layout.config_path), sshd_config(layout, session_env(env)), 0o644
    )


def _authorized_keys_from(env: Mapping[str, str]) -> list[str]:
    """Collect authorized keys from the env var and the mounted Secret."""
    keys: list[str] = []
    inline = env.get(PUBKEY_ENV)
    if inline:
        keys += inline.splitlines()
    mounted = env.get(AUTHORIZED_KEYS_FILE_ENV, AUTHORIZED_KEYS_MOUNT)
    path = Path(mounted)
    if path.is_file():
        keys += path.read_text().splitlines()
    return [key.strip() for key in keys if key.strip()]


def ensure_authorized_keys(
    layout: SshdLayout, *, env: Mapping[str, str] | None = None
) -> bool:
    """Merge our keys into ``authorized_keys`` without evicting anyone.

    Merge rather than overwrite because a second ``podbench attach`` into a
    container that is already serving a session is a supported thing to do, and
    truncating the file would drop the running session's key on the floor at the
    next reconnect.
    """
    environ = env if env is not None else os.environ
    wanted = _authorized_keys_from(environ)
    path = Path(layout.authorized_keys_path)
    existing = path.read_text().splitlines() if path.is_file() else []
    merged: list[str] = []
    for key in [*[line.strip() for line in existing if line.strip()], *wanted]:
        if key not in merged:
            merged.append(key)
    if not merged:
        return False
    # The root layout leaves StrictModes at its default, so sshd audits this
    # directory and silently ignores the keys if the image left it group-writable.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    return _write_if_changed(path, "\n".join(merged) + "\n", 0o600)


def ensure_host_key(
    layout: SshdLayout,
    *,
    env: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
) -> bool:
    """Install a supplied host key, or mint one if none was supplied.

    A supplied key always wins, and always overwrites: it is the pod's stable
    identity and the only reason a client's ``known_hosts`` entry can survive
    the fresh rootfs a pod restart produces. A minted key is generated once and
    then left alone, so a second session sees the same identity as the first.

    ``ssh-keygen -A`` is deliberately not used: it mints three keys of which the
    config names exactly one, and it only ever writes to ``/etc/ssh``, which the
    non-root layout cannot touch.
    """
    environ = env if env is not None else os.environ
    run = runner if runner is not None else run_command
    path = Path(layout.host_key_path)
    supplied = environ.get(HOST_KEY_ENV)
    supplied_file = environ.get(HOST_KEY_FILE_ENV, HOST_KEY_MOUNT)
    if supplied is None and Path(supplied_file).is_file():
        supplied = Path(supplied_file).read_text()

    if supplied is not None:
        if not supplied.endswith("\n"):
            supplied += "\n"
        changed = _write_if_changed(path, supplied, 0o600)
        if changed or not Path(layout.host_public_key_path).is_file():
            _derive_public_key(layout, runner=run)
        return changed

    if path.is_file():
        return False
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "podbench",
            "-f",
            str(path),
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"ssh-keygen failed: {result.stderr.strip()}")
    if path.is_file():
        path.chmod(0o600)
    return True


def _derive_public_key(layout: SshdLayout, *, runner: CommandRunner) -> None:
    """Recover the public half of a supplied private key.

    A Secret usually carries only the private key, and podbench needs the public
    half to write the client's ``known_hosts`` entry itself.
    """
    result = runner(["ssh-keygen", "-y", "-f", layout.host_key_path])
    if result.returncode == 0 and result.stdout.strip():
        Path(layout.host_public_key_path).write_text(result.stdout.strip() + "\n")


def read_host_public_key(layout: SshdLayout) -> str | None:
    """The host's public key, for the launcher to pin in ``known_hosts``."""
    path = Path(layout.host_public_key_path)
    return path.read_text().strip() if path.is_file() else None


@dataclass(frozen=True)
class EnsureReport:
    """What start-up changed, and what it could not do.

    Failures are carried rather than raised because the caller is PID 1 of an
    unrestartable container: the only thing worse than a seat without ssh is a
    seat that died explaining why it had none.
    """

    changes: tuple[str, ...] = ()
    failures: tuple[CheckResult, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


def ensure_all(
    layout: SshdLayout,
    *,
    env: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
) -> EnsureReport:
    """Bring the container as close to a serving state as it can get.

    No step is fatal. Each one that fails is recorded against its name and the
    rest still run, because they are independent: ``ssh-keygen`` failing for
    want of a passwd entry costs the ssh transport and nothing else, and the
    container has to stay up for the half of the seat that ``kubectl exec``
    reaches.

    An empty report is the expected result of the second and later runs.
    """
    changes: list[str] = []
    failures: list[CheckResult] = []

    def step(name: str, description: str, action: Callable[[], bool]) -> None:
        try:
            if action():
                changes.append(description)
        except (OSError, RuntimeError) as error:
            failures.append(CheckResult(f"ensure-{name}", False, str(error)))
        except Exception as error:
            # Deliberately broad, and only here: PID 1 of an unrestartable
            # container must not exit over a bug in a step whose failure costs
            # the ssh transport at most. The exception type is named so that a
            # defect stays distinguishable from a refusal we planned for.
            failures.append(
                CheckResult(
                    f"ensure-{name}",
                    False,
                    f"unexpected {type(error).__name__}: {error}",
                )
            )

    # First: every other step writes under it, and a home that arrived as an
    # empty volume has none of the directories they expect.
    step(
        "home-dir",
        f"prepared {layout.home}",
        lambda: ensure_home_dir(layout),
    )
    step(
        "privsep-dir",
        f"created {layout.privsep_dir}",
        lambda: ensure_privsep_dir(layout),
    )
    # Before the host key, not after: ssh-keygen calls getpwuid() whatever it is
    # asked to do, so on a uid NSS cannot resolve it fails before writing a byte.
    step(
        "nss-identity",
        f"registered uid {os.geteuid()} as {SEAT_USER!r} in {PASSWD_PATH}",
        lambda: ensure_passwd_entry(layout),
    )
    step(
        "host-key",
        f"installed host key {layout.host_key_path}",
        lambda: ensure_host_key(layout, env=env, runner=runner),
    )
    step(
        "authorized-keys",
        f"updated {layout.authorized_keys_path}",
        lambda: ensure_authorized_keys(layout, env=env),
    )
    step(
        "sshd-config", f"wrote {layout.config_path}", lambda: ensure_sshd_config(layout)
    )
    return EnsureReport(tuple(changes), tuple(failures))


def fd2_check() -> CheckResult:
    """Whether our own stderr is a real, open file descriptor.

    fd 2 is the transport's lifeline: an exec'd process that closes or replaces
    it makes containerd tear down the whole exec session, truncating stdin and
    stdout mid-stream with ``rc=0`` and no diagnostic anywhere. If the agent
    itself was started with stderr pointed at ``/dev/null`` then whatever
    wrapper did that will do it to sshd too.
    """
    try:
        os.fstat(2)
    except OSError as exc:
        return CheckResult("fd2-open", False, f"stderr is not open: {exc}")
    try:
        dest = os.readlink("/proc/self/fd/2")
    except OSError:
        return CheckResult("fd2-open", True, "stderr is open")
    if dest == "/dev/null":
        return CheckResult(
            "fd2-open",
            False,
            "stderr is /dev/null; a wrapper doing this to sshd kills the "
            "kubectl exec stream mid-handshake",
        )
    return CheckResult("fd2-open", True, f"stderr is open on {dest}")


def stdio_roundtrip_check(*, delay: float = 0.2, timeout: float = 10.0) -> CheckResult:
    """The fd-2 tripwire: a delayed second line must still arrive.

    This is the shape from the report — write one line, wait, write another,
    and confirm *both* come back — because the failure it guards against is
    silent truncation with a zero exit code, which no error check can catch.

    Run locally, as PID 1, it only proves the image's ``sh`` and pipes are sane.
    The teardown it is named for happens in the CRI, so the launcher must also
    run this over ``kubectl exec`` (``podbench agent --self-check``) where the
    exec stream is genuinely in the path.
    """
    payload_a, payload_b = "podbench-fd2-one", "podbench-fd2-two"
    try:
        with subprocess.Popen(
            ["sh", "-c", "exec cat"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as proc:
            assert proc.stdin is not None
            proc.stdin.write(payload_a + "\n")
            proc.stdin.flush()
            time.sleep(delay)
            proc.stdin.write(payload_b + "\n")
            out, _ = proc.communicate(timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("stdio-roundtrip", False, f"could not run the probe: {exc}")
    lines = out.split()
    if lines == [payload_a, payload_b]:
        return CheckResult("stdio-roundtrip", True, "both lines returned")
    return CheckResult(
        "stdio-roundtrip",
        False,
        f"expected both lines back, got {lines!r} — the stream is being truncated",
    )


def proxy_shape_check(layout: SshdLayout) -> CheckResult:
    """Assert the generated ProxyCommand still has the flags that keep it alive.

    Cheap, and it fires here rather than in a user's editor on the day someone
    tidies ``-e`` away or adds ``-t``.
    """
    argv = proxy_command(
        ContainerRef(PodRef("podbench-selfcheck", "podbench-selfcheck"), "podbench"),
        layout=layout,
    )
    if "-e" not in argv:
        return CheckResult("proxy-command-shape", False, "sshd -e is missing")
    if "-t" in argv:
        return CheckResult("proxy-command-shape", False, "kubectl -t would hang ssh")
    if any(">" in token for token in argv):
        return CheckResult("proxy-command-shape", False, "stderr is being redirected")
    return CheckResult("proxy-command-shape", True, "sshd -i -e, no tty, no redirect")


def self_check(
    layout: SshdLayout,
    *,
    runner: CommandRunner | None = None,
    roundtrip: bool = True,
    ensure: EnsureReport | None = None,
) -> list[CheckResult]:
    """Everything that must hold before a client is told the pod is ready.

    Each of these has a silent failure mode: a missing privsep directory, an
    unparsed config or a torn-down exec stream all surface to the user as a
    connection error with no cause attached. A start-up step that gave up is
    the same kind of thing, so ``ensure``'s failures are reported here too -
    this is the one place that answers "why is there no host key".
    """
    results = [proxy_shape_check(layout), fd2_check(), nss_identity_check()]
    results.extend(ensure.failures if ensure is not None else ())
    if roundtrip:
        results.append(stdio_roundtrip_check())

    if layout.privsep_dir is not None:
        present = Path(layout.privsep_dir).is_dir()
        results.append(
            CheckResult(
                "privsep-dir",
                present,
                f"{layout.privsep_dir} "
                + ("exists" if present else "is missing; sshd will refuse every login"),
            )
        )
    for name, path in (
        ("host-key", layout.host_key_path),
        ("authorized-keys", layout.authorized_keys_path),
        ("sshd-config", layout.config_path),
    ):
        ok = Path(path).is_file() and Path(path).stat().st_size > 0
        results.append(
            CheckResult(name, ok, f"{path} {'present' if ok else 'missing'}")
        )

    run = runner if runner is not None else run_command
    validation = run([layout.sshd, "-t", "-f", layout.config_path])
    results.append(
        CheckResult(
            "sshd-config-valid",
            validation.returncode == 0,
            (validation.stderr or validation.stdout).strip() or "CONFIG_OK",
        )
    )
    return results


@dataclass(frozen=True)
class ReaperStatus:
    """Whether this process is the one the kernel will hand orphans to."""

    pid: int
    is_init: bool
    namespace_init: str | None
    """``comm`` of pid 1 in our namespace when that is not us."""

    @property
    def note(self) -> str:
        """A line for the session banner."""
        if self.is_init:
            return "podbench agent is pid 1; orphaned children will be reaped"
        return (
            f"pid 1 in this namespace is {self.namespace_init!r}, not podbench — "
            "it does not reap, so orphaned helpers become permanent zombies. "
            "Track helper pids explicitly and never use `kill -0` as a liveness "
            "check here."
        )


def reaper_status(
    *, pid: int | None = None, proc: Path = Path("/proc")
) -> ReaperStatus:
    """Work out whether we inherit orphans.

    Under ``kubectl debug --target`` the pod's PID namespace is shared and pid 1
    is the *target application*, which reaps nothing. A helper orphaned in that
    namespace stays a zombie for the pod's lifetime, and a zombie still answers
    ``kill -0`` — which is how a bootstrap script once reported a dead server as
    healthy and handed a client a dead port.
    """
    own = pid if pid is not None else os.getpid()
    if own == 1:
        return ReaperStatus(own, True, None)
    comm_path = proc / "1" / "comm"
    comm = comm_path.read_text().strip() if comm_path.is_file() else None
    return ReaperStatus(own, False, comm)


def reap_children() -> list[tuple[int, int]]:
    """Reap every exited child that is ready. Returns ``(pid, status)`` pairs."""
    reaped: list[tuple[int, int]] = []
    while True:
        try:
            child, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return reaped
        if child == 0:
            return reaped
        reaped.append((child, status))


def request_stop(signum: int, frame: FrameType | None) -> None:
    """Ask :func:`idle` to return at its next slice.

    Public because graceful shutdown is a behaviour worth asserting: the agent
    is PID 1, so taking longer than the kubelet's grace period to notice a
    SIGTERM means being SIGKILLed instead of exiting cleanly.
    """
    global _stop_requested
    _stop_requested = True


def _on_sigchld(signum: int, frame: FrameType | None) -> None:
    reap_children()


def _install_handlers(*, reap: bool) -> None:
    """Install signal handlers, tolerating a non-main thread (i.e. a test)."""
    try:
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        if reap:
            signal.signal(signal.SIGCHLD, _on_sigchld)
    except ValueError:
        pass


def _sleep_interruptibly(seconds: float, *, slice_seconds: float) -> None:
    """Sleep, but never for longer than ``slice_seconds`` without looking up.

    A signal handler in Python does not cut a ``time.sleep`` short: since PEP
    475 the sleep is *resumed* for its remaining time, and the Python-level
    handler only runs when it finally returns. So a single 30 s sleep means a
    SIGTERM is acted on up to 30 s later — at or past the kubelet's default
    grace period, which turns a clean exit into a SIGKILL and a container that
    looks like it crashed.
    """
    deadline = time.monotonic() + seconds
    while not _stop_requested:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(slice_seconds, remaining))


def idle(
    status: ReaperStatus,
    *,
    interval: float = 30.0,
    iterations: int | None = None,
    slice_seconds: float = IDLE_SLICE,
) -> int:
    """Sit still as a restart-tolerant PID 1 until asked to stop.

    ``iterations`` bounds the loop for tests; production passes ``None`` and
    leaves on SIGTERM. The kubelet's ``SIGTERM`` is the container's normal end
    of life, so exiting 0 on it keeps a stopped agent out of the crash-loop
    accounting — which it only does if the signal is *noticed* promptly, hence
    the sliced sleep.
    """
    global _stop_requested
    _stop_requested = False
    _install_handlers(reap=status.is_init)
    count = 0
    while not _stop_requested:
        if iterations is not None and count >= iterations:
            break
        if status.is_init:
            reap_children()
        _sleep_interruptibly(interval, slice_seconds=slice_seconds)
        count += 1
    return 0


def _build_app() -> typer.Typer:
    app = new_app()

    @app.command()
    def agent(
        ensure_only: Annotated[
            bool,
            typer.Option(
                "--ensure-only", help="prepare the container and exit instead of idling"
            ),
        ] = False,
        self_check_only: Annotated[
            bool,
            typer.Option(
                "--self-check",
                help="run the startup checks and exit; non-zero if any fails",
            ),
        ] = False,
        print_host_key: Annotated[
            bool,
            typer.Option(
                "--print-host-key",
                help="print the host public key for the launcher's known_hosts",
            ),
        ] = False,
        print_login_user: Annotated[
            bool,
            typer.Option(
                "--print-login-user",
                help="print the login name sshd will resolve for this uid; "
                "non-zero with the reason on stderr when there is none",
            ),
        ] = False,
        no_self_check: Annotated[
            bool,
            typer.Option(
                "--no-self-check",
                help="skip the startup checks (they cost a subprocess and ~0.2 s)",
            ),
        ] = False,
        idle_interval: Annotated[
            float,
            typer.Option(
                "--idle-interval",
                metavar="SECONDS",
                help="seconds between reap sweeps while idling",
            ),
        ] = 30.0,
    ) -> None:
        """Prepare the debug container for ssh and idle as its PID 1."""
        raise typer.Exit(
            _run(
                ensure_only=ensure_only,
                self_check_only=self_check_only,
                print_host_key=print_host_key,
                print_login_user=print_login_user,
                no_self_check=no_self_check,
                idle_interval=idle_interval,
            )
        )

    return app


def main(args: Sequence[str] | None = None) -> int:
    """Entry point for ``podbench agent``. CLI wiring lives elsewhere."""
    return run(_build_app(), args, prog="podbench agent")


def _run(
    *,
    ensure_only: bool,
    self_check_only: bool,
    print_host_key: bool,
    print_login_user: bool,
    no_self_check: bool,
    idle_interval: float,
) -> int:
    layout = SshdLayout.for_uid(os.geteuid())

    if print_login_user:
        # A pure read, answered before anything is ensured: the launcher asks
        # this of a container that has already started, and the answer has to be
        # the state sshd will find rather than the state a second ensure created.
        return _print_login_user()

    ensure: EnsureReport | None = None
    if not self_check_only:
        ensure = ensure_all(layout)
        for change in ensure.changes:
            _say(change)
        for failure in ensure.failures:
            _say(str(failure))

    failures = 0
    if self_check_only or not no_self_check:
        for result in self_check(layout, ensure=ensure):
            _say(str(result))
            failures += 0 if result.ok else 1
        if self_check_only:
            return 1 if failures else 0

    if print_host_key:
        public = read_host_public_key(layout)
        if public is None:
            _say("no host public key available")
            return 1
        print(public)
        return 0

    if failures:
        # Reported, never fatal. Exiting here would leave the container in
        # Error, burn its name for the pod's lifetime (report 4.2) and take the
        # exec-reachable half of the seat down along with the ssh half - which
        # is most of what spike S5 found the degraded rung to be worth.
        _say(
            f"{failures} start-up check(s) failed, so ssh into this container "
            "may not work. Staying up: capreport, pids, dbg --launch and a "
            "shell are reached with kubectl exec and need none of sshd."
        )

    status = reaper_status()
    _say(status.note)
    if ensure_only:
        return 1 if failures else 0
    return idle(status, interval=idle_interval)


def _print_login_user() -> int:
    """``--print-login-user``: stdout is the name, stderr is why there is none."""
    result = nss_identity_check()
    if not result.ok:
        print(result.detail, file=sys.stderr)
        return 1
    name = login_name()
    if name is None:  # pragma: no cover - nss_identity_check just resolved it
        return 1
    print(name)
    return 0
