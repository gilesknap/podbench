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
idles anyway: ``podbench capreport``, ``podbench pids`` and ``podbench dbg
--launch`` are reached by
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
from .sshcfg import (
    SEAT_USER,
    SshdLayout,
    proxy_command,
    sshd_config,
    unsafe_set_env,
)
from .vscode import MACHINE_SETTINGS_PATH, merge_machine_settings

__all__ = [
    "request_stop",
    "AUTHORIZED_KEYS_FILE_ENV",
    "AUTHORIZED_KEYS_MOUNT",
    "EXEC_HALF_COMMANDS",
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
    "SEAT_NSS_PATH",
    "SESSION_ENV_NAMES",
    "SESSION_ENV_PREFIX",
    "VSCODE_SETTINGS_WAY_OUT",
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
    "ensure_vscode_settings",
    "extrausers_serves",
    "fd2_check",
    "idle",
    "login_name",
    "main",
    "nss_identity_check",
    "passwd_line",
    "read_host_public_key",
    "reap_children",
    "reaper_status",
    "restrict_seat_nss_database",
    "run_command",
    "proxy_shape_check",
    "session_home",
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
"""The NSS ``files`` database sshd resolves a login name in.

Also the file a seat writes its own record to when :data:`SEAT_NSS_PATH` is not
an option. It has no uid or gid floor, and it is group-writable by GID 0, which
is what a seat mirroring a target that really runs in group 0 carries anyway.

Since #103 the image also **pre-seeds it with a static record for every unused
uid below 500**, which is the range :func:`extrausers_serves` refuses. Those
seats need no write at all: the record is already there, ``getpwuid`` answers,
and :func:`ensure_passwd_entry` returns having done nothing. A pre-baked line is
the one identity mechanism that costs no writable surface, and it is available
here precisely because the range is small and bounded - which is not true of the
uids above it.

This constant is also the *mount point* the launcher projects a ``podbench dev``
sidecar's identity onto (``launcher.SEAT_IDENTITY_MOUNTS``), so it names
``/etc/passwd`` and nothing else, whatever the append target becomes.
"""

SEAT_NSS_PATH = "/var/lib/extrausers/passwd"
"""Where a seat registers its own passwd record: libnss-extrausers' database.

The image installs ``libnss-extrausers`` and points ``nsswitch.conf``'s
``passwd`` line at ``files extrausers``, so a record appended here is resolved
by ``getpwuid`` exactly as one in :data:`PASSWD_PATH` would be - and the file is
mode 0666, so the append needs no capability, no particular gid and no edit to
the workload's manifest. That is the whole point: the degraded rung runs as the
target's uid **and gid**, and ``/etc/passwd`` is writable only by GID 0, which a
seat mirroring a non-zero-gid target does not have - and must not be given, since
``__ptrace_may_access()`` compares the group ids as peers of the user ids and
pinning group 0 to win the write forfeits the debugger (issue #102, measured;
#98, #103).

Not every seat, though: libnss-extrausers ignores a record whose uid or gid is
below 500 (gid 100 excepted), so which of the two files a seat appends to is
:func:`extrausers_serves`' decision and not the mode's.

A *second* path rather than a new value for :data:`PASSWD_PATH`, because that
constant is load-bearing elsewhere: it is the mount point for the ``podbench
dev`` sidecar's projected passwd file, a different and already working
mechanism, and repointing it would move that mount to a path nothing consults
for a sidecar.

World-writable is not an escalation. On the rungs that append here sshd is
:meth:`podbench.sshcfg.SshdLayout.for_uid` with ``run_as_root=False``: it skips
privilege separation and never ``setuid``s out of a passwd record, so a forged
record buys the forger the uid it already has, and ``NoNewPrivs`` has already
made the seat's setuid binaries inert. The full rung's sshd *is* root, and there
the mode is closed rather than argued about -
:func:`restrict_seat_nss_database`. What must not ship is the combination in
between, a root sshd that ``setuid``s into a *non-root* session (#98): there an
unprivileged writer and a privileged reader would exist in the one container.
"""

_EXTRAUSERS_MIN_UID = 500
_EXTRAUSERS_MIN_GID = 500
_EXTRAUSERS_USERS_GID = 100
"""libnss-extrausers' compiled-in floors, from Debian 0.6-4.1 ``s_config.h``.

``MINUID``, ``MINGID`` and ``USERSGID``, and there is no configuration file that
moves them - the numbers are in the shared object the image installs. Named here
because :func:`extrausers_serves` is the only thing that reads them and the
values are not guessable from the outside.
"""

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

EXEC_HALF_COMMANDS = "podbench capreport, podbench pids, podbench dbg --launch"
"""The exec-reachable half of a seat, spelled the only way it resolves.

One constant because two diagnostics name it, and both are read by someone who
is about to paste what they read. Since #47 the image ships no per-subcommand
aliases, so a bare ``capreport`` here is not a shorter spelling of anything - it
is `executable file not found`, in the message that was meant to be the way out.
"""

NSS_WAY_OUT = (
    "sshd resolves the login name through NSS before it will look at a key, so "
    "ssh into this seat cannot work. Everything reached by kubectl exec - "
    f"{EXEC_HALF_COMMANDS}, a shell - is unaffected. A seat normally registers "
    f"its own record in {SEAT_NSS_PATH}, the world-writable libnss-extrausers "
    "database the current image installs and lists in nsswitch.conf beside "
    "files: that route needs no capability, no particular gid and no edit to "
    "the workload, but libnss-extrausers ignores a record whose uid or gid is "
    "below 500 (gid 100 excepted), so it does not serve every seat. Under that "
    f"floor the current image answers from {PASSWD_PATH} instead, which it "
    "pre-seeds with a static record for every free uid below 500 - so a seat "
    "reading this at such a uid is standing in an image that predates those "
    "records, or in one whose account for this uid is taken. Above the floor, "
    "the shape left is a uid of 500 or more in a group below it: run the target "
    "in a group of 500 or more, or in gid 100, and the database serves the seat "
    f"as it stands. A {SEAT_IDENTITY_VOLUME!r} volume does not help here, "
    "however plainly the pod declares one: projecting a passwd file takes a "
    "subPath per mount and an ephemeral container may not have one. That volume "
    "is the identity a `podbench dev` sidecar gets, which is an ordinary "
    "container."
)
"""Named mechanism, then the way out - the shape :class:`podbench.model.Blocker`
uses, because "No user exists for uid 36070" names neither.

Written for the container it is *read* in. Every path that quotes it - the
registration failure, ``nss-identity``, and the launcher, which prints it
verbatim under the missing ssh stanza - is reached from a seat that got here by
running as a uid it has no account for, and on a live pod that seat is an
ephemeral container. So the routes are ordered by what they cost the reader:
extrausers first, because it costs nothing and is what the current image does;
then the static ``/etc/passwd`` records, which cost nothing either and are the
answer for every seat under the floors.

What is *not* offered any more is gid 0. ``--seat-gid-root`` pinned
``runAsGroup: 0`` to win the write, and ``__ptrace_may_access()`` compares the
group ids as peers of the user ids, so it bought ssh and took the debugger -
which is how issue #102 sent the same seat round the loop twice. The flag is
gone (#103) and the text must not grow it back: a reader who cannot get a login
is exactly the reader who will pay any price for one.

Neither route is asserted of the reader's own image, and the extrausers sentence
is careful about that: one way to be reading this text is to be standing in an
image built before the database existed, or behind a site mirror pinned to one,
and being told "this image consults it" would send that reader off to conclude
their image is broken rather than old."""

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


def _home_for_uid(uid: int | None = None) -> str | None:
    """The home directory NSS records for ``uid``, or ``None`` for no record."""
    try:
        return pwd.getpwuid(os.geteuid() if uid is None else uid).pw_dir or None
    except KeyError:
        return None


def passwd_line(*, uid: int, gid: int, home: str, user: str = SEAT_USER) -> str:
    """One passwd record for the seat's own uid.

    The format is the same whichever database it is appended to: libnss-extrausers
    parses :data:`SEAT_NSS_PATH` with the same seven colon-separated fields as
    NSS's ``files`` source parses :data:`PASSWD_PATH`.
    """
    return f"{user}:x:{uid}:{gid}:{user}:{home}:{LOGIN_SHELL}\n"


def extrausers_serves(uid: int, gid: int) -> bool:
    """Whether libnss-extrausers would answer a lookup for this uid and gid.

    It refuses records below compiled-in floors, and that is not documented
    anywhere a reader of :data:`SEAT_NSS_PATH` would look: Debian's 0.6-4.1
    ``passwd.c`` applies them twice, returning ``NSS_STATUS_NOTFOUND`` before it
    opens the file when the queried uid is under ``MINUID``, and then skipping
    any record whose own uid is under ``MINUID`` or whose gid is under ``MINGID``
    and is not ``USERSGID``. ``getpwnam`` goes through the same search, so a
    rejected record is invisible to *every* lookup - the append succeeds, the
    file has the line in it, and nothing resolves.

    Which is why this decides the append target and the file's mode does not. The
    seats below the floors are ordinary rather than exotic: a target genuinely
    running in group 0, and every low-numbered system uid there is.

    The shape that used to dominate this list has gone. A target setting
    ``runAsUser`` and no ``runAsGroup`` gave
    :func:`podbench.spec.target_uid_gid` a gid of ``None``, so the seat pinned no
    group and ran with the image's gid 0 - which reached this function as
    ``(1000, 0)`` and was routed, correctly, to :data:`PASSWD_PATH`. It was
    correct about the database and wrong about the seat: that gid 0 was never the
    target's, and the seat could log in and not trace. Since #103 the launcher
    measures the target's real gid from ``/proc`` and lands a seat carrying it,
    so such a seat now arrives here as ``(1000, 1000)`` and this database serves
    it.

    A target that runs as a low-numbered uid *and* a low-numbered non-zero gid
    (grafana's 472:472) is below both floors and cannot write ``/etc/passwd``
    either. It needs no write: the image pre-seeds :data:`PASSWD_PATH` with a
    static record for every free uid under 500, so ``getpwuid`` answers before
    :func:`ensure_passwd_entry` looks at a file at all. Returning
    :data:`PASSWD_PATH` for it is still the honest answer for the case where that
    record was taken by a real account - a refusal naming a file whose mode is the
    actual obstacle, rather than a silent append to a database that will never
    answer.

    >>> extrausers_serves(36070, 36070)   # bl01c-di-dcam-04-0 at Diamond
    True
    >>> extrausers_serves(1000, 1000)     # p47-blueapi-0, once its gid is mirrored
    True
    >>> extrausers_serves(1000, 0)        # a target whose own group really is 0
    False
    >>> extrausers_serves(472, 472)       # grafana; nginx-unprivileged is 101
    False
    >>> extrausers_serves(1000, 100)      # gid 100 is `users`, exempted
    True

    The boundary, spelled from both sides because a floor that *moved* is the
    failure this function cannot detect for itself. ``_container.yml`` asserts
    these same six points against the library in a built image, so if the two
    ever disagree it is this function that is wrong.

    >>> extrausers_serves(500, 500)       # uid == MINUID, gid == MINGID
    True
    >>> extrausers_serves(499, 500)       # one below MINUID
    False
    >>> extrausers_serves(501, 499)       # one below MINGID, uid clear
    False
    """
    if uid < _EXTRAUSERS_MIN_UID:
        return False
    return gid >= _EXTRAUSERS_MIN_GID or gid == _EXTRAUSERS_USERS_GID


def _nss_append_target(uid: int, gid: int, passwd_path: str | None = None) -> Path:
    """Which passwd database this container should append its own record to.

    :data:`SEAT_NSS_PATH` when the image has one *and* libnss-extrausers would
    serve this seat's uid and gid from it, and :data:`PASSWD_PATH` otherwise. The
    fallback is deliberate: this route was *added* by issue #102 and the GID 0 one
    it replaces as the default still works, so an older or hand-built image - or
    one whose nsswitch was rewritten by a mount - should take the old route rather
    than be told "there is no /var/lib/extrausers/passwd to add one to", which
    names a file the reader has never heard of and offers nothing to do about it.
    Every message downstream reports the path this returned, so the diagnostic
    always names the file that was actually tried.

    Writability, not just existence, decides the file half: an extrausers file
    that exists but cannot be written (an image that created it 0644, say) would
    otherwise mask a perfectly good gid 0 seat behind a refusal.
    :func:`extrausers_serves` decides the credentials half, and it has to be
    asked *here* rather than after the append, because a record the floors reject
    is written and silently unresolvable - there is no error to react to.

    There is no third attempt if the chosen file turns out not to resolve. On the
    image podbench ships there could not be one: ``/etc/passwd`` is 0664
    ``root:root``, so the only seat that can write it is a gid 0 seat, and a gid 0
    seat is already sent here. A retry would be a branch that cannot succeed.

    Nothing routed here is reached at all when the image's static sub-500 records
    already answer for the seat's uid - :func:`ensure_passwd_entry` returns on the
    ``login_name`` check above this call.

    ``passwd_path`` overrides both, and is how the unit tests point registration
    at a temporary file.
    """
    if passwd_path is not None:
        return Path(passwd_path)
    seat_nss = Path(SEAT_NSS_PATH)
    if (
        extrausers_serves(uid, gid)
        and seat_nss.is_file()
        and os.access(seat_nss, os.W_OK)
    ):
        return seat_nss
    return Path(PASSWD_PATH)


def _registration_blocker(uid: int, gid: int, path: Path) -> str | None:
    """Why this container cannot add its own passwd entry, or ``None``."""
    if not path.is_file():
        return f"there is no {path} to add one to"
    taken = _uid_named(SEAT_USER)
    if taken is not None:
        # sshd resolves the *name* the client offered, so a second record for
        # the same name would never be reached: the first one wins and the login
        # would be attempted as the wrong uid.
        #
        # The name is resolved through NSS and not read out of `path`, and since
        # #102 those are different files - the realistic collision is a `dev`
        # sidecar whose projected /etc/passwd names `podbench` at a uid the
        # sidecar does not actually run as. Naming `path` here sent that reader
        # to cat an empty extrausers database.
        return (
            f"the login name {SEAT_USER!r} already resolves to uid {taken} "
            f"through this container's NSS (`getent passwd {SEAT_USER}` shows "
            "it), and sshd resolves the name before the uid, so a record for "
            f"uid {uid} in {path} would never be reached"
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
      here is its *only* route, and it usually goes to :data:`SEAT_NSS_PATH`,
      which the image leaves world-writable precisely so that a seat running as
      the target's uid *and gid* can take it (issue #102);
    * a **``podbench dev``** sidecar is an ordinary container, so
      :func:`podbench.spec.dev_pod_spec` mounts
      :data:`podbench.model.SEAT_IDENTITY_VOLUME` read-only over ``/etc/passwd``
      and NSS resolves the uid before this step ever runs.

    That second shape is why the ``login_name`` check comes first and returns
    without looking at the file's mode: a read-only ``/etc/passwd`` that already
    carries the identity is the *success* case, and treating it as a refusal
    would report the one shape that works as the one that does not.

    Where the record lands is :func:`_nss_append_target`'s decision, and for a
    seat libnss-extrausers will not serve - or on an image that has no such
    database - it is still :data:`PASSWD_PATH`, group-writable by GID 0 in the
    OpenShift convention. So this step is *allowed to fail* - it raises,
    :func:`ensure_all` records the reason, and the container lands without ssh
    rather than not landing at all.

    Idempotent like every other ensure step: a uid NSS already resolves is left
    alone, which is also the whole of the root case.
    """
    own_uid = os.geteuid() if uid is None else uid
    own_gid = os.getegid() if gid is None else gid
    if login_name(own_uid) is not None:
        return False

    path = _nss_append_target(own_uid, own_gid, passwd_path)
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
        #
        # Which of the two reasons it is stays open, and the way out below names
        # both: nsswitch.conf may not list the source, or the source may be
        # refusing the record. `nsswitch does not consult it` alone sent one
        # reader to inspect a line that was correct.
        raise RuntimeError(
            f"{path} already carries {entry.strip()!r} and NSS still does not "
            f"resolve uid {own_uid}; this image does not appear to answer passwd "
            f"lookups from {path}. {NSS_WAY_OUT}"
        )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(("" if existing.endswith("\n") or not existing else "\n") + entry)
    if login_name(own_uid) is None:
        raise RuntimeError(
            f"appended {entry.strip()!r} to {path} and NSS still does not "
            f"resolve uid {own_uid}. {NSS_WAY_OUT}"
        )
    return True


def restrict_seat_nss_database(layout: SshdLayout) -> bool:
    """Close the seat database's world-writable bit wherever sshd runs as root.

    Mode 0666 on :data:`SEAT_NSS_PATH` is what makes the degraded rung work at
    all - the seat runs as the target's uid *and* gid, both discovered at attach
    time, so no owner and no group the image could bake in would be writable by
    it - and it is safe there because sshd on that rung holds no privilege to
    hand to a forged record.

    On the ``full`` rung sshd *is* root: ``SshdLayout.for_uid(0)`` turns
    privilege separation on and sshd ``setuid``s into the session from whatever
    NSS answers with. The argument that 0666 is still not an escalation there is
    that a root seat has no unprivileged principal to forge with - every process
    in it, including the ``kubectl exec`` that carries the ssh transport, is
    already uid 0. That happens to be true, which is a weaker thing than being
    enforced, so this enforces it: a root seat resolves its own uid from the
    image's ``/etc/passwd`` and appends nothing, so it has no use for the write
    bit and can afford to drop it.

    Narrowing one seat narrows no other. An ephemeral container gets a fresh copy
    of the image's layers, so a root seat and a degraded seat in the same pod each
    have their own database, and the degraded one keeps its 0666.

    A missing file, an already-narrow mode and a refused ``chmod`` are all
    success. The refusal worth naming is a read-only rootfs, which denies the
    forger the same write it denies this call - the property holds by another
    route, and recording a failure would put an alarming line under a seat that is
    in fact tighter than the one this step was written for.
    """
    if not layout.run_as_root:
        return False
    database = Path(SEAT_NSS_PATH)
    if not database.is_file():
        return False
    mode = database.stat().st_mode & 0o777
    narrowed = mode & ~0o022
    if narrowed == mode:
        return False
    try:
        database.chmod(narrowed)
    except OSError:
        return False
    return True


def nss_identity_check(
    *, uid: int | None = None, gid: int | None = None, passwd_path: str | None = None
) -> CheckResult:
    """Whether sshd can resolve a login name for the uid this container runs as.

    Re-derived from the platform rather than remembered, so it is the same
    answer whether it is reached through the start-up path or through
    ``podbench agent --self-check`` over ``kubectl exec`` seconds later.

    A failure names the database :func:`ensure_passwd_entry` would have written -
    :data:`SEAT_NSS_PATH` for a seat extrausers will serve, :data:`PASSWD_PATH`
    for one it will not - because a reader asked to check a file's mode has to be
    given the file that was actually tried.

    The "writable and still no entry" branch is the one the launcher relays most
    often now that the usual append target is 0666, and it is the one branch that
    cannot say why: the reason was raised inside :func:`ensure_passwd_entry`
    minutes earlier and recorded by :func:`ensure_all` into the container's
    start-up output, which the launcher does not relay. So it names the command
    that shows that output. It deliberately does not repeat
    :data:`NSS_WAY_OUT` - the routes there are for a seat that could not write,
    and this seat could.
    """
    own_uid = os.geteuid() if uid is None else uid
    own_gid = os.getegid() if gid is None else gid
    name = login_name(own_uid)
    if name is not None:
        return CheckResult(
            "nss-identity", True, f"uid {own_uid} resolves to the login name {name!r}"
        )
    path = _nss_append_target(own_uid, own_gid, passwd_path)
    blocker = _registration_blocker(own_uid, own_gid, path)
    if blocker is None:
        return CheckResult(
            "nss-identity",
            False,
            f"uid {own_uid} has no NSS entry even though {path} is writable - "
            "the agent's registration step failed and said why in this "
            "container's start-up log: `kubectl logs <pod> -c <this container> "
            "-n <namespace>`",
        )
    return CheckResult(
        "nss-identity",
        False,
        f"uid {own_uid} has no NSS entry and this container cannot add one: "
        f"{blocker}. {NSS_WAY_OUT}",
    )


SESSION_ENV_PREFIX = "PODBENCH_"
"""podbench's own variables, forwarded from the container into ssh sessions.

The launcher injects the target's container id and the node name into the
container spec, and sshd does not pass its own environment to the commands it
runs — so without this the helpers fall back to guessing which processes belong
to the target, and say the node is unknown. The ssh public key is excluded: it
is already installed in authorized_keys and has no business in a session
environment.
"""

SESSION_ENV_NAMES = frozenset({"PATH", "DEBUGINFOD_URLS", "DEBUGINFOD_TIMEOUT"})
"""The image's own variables the transport carries as well, by exact name.

The prefix above was the whole rule once, and the same missing mechanism showed
up as three unrelated-looking defects, because a session started by sshd
inherits none of the image's ``ENV``:

* ``PATH`` — ``ssh <seat> '<cmd>'`` got sshd's compiled-in default, so the
  seat's interpreter was not on it and the injection recipe died with
  ``sh: 1: python: not found``.
* ``DEBUGINFOD_URLS`` — gdb's ``set debuginfod enabled on`` was inert over the
  transport podbench generates, while working under ``kubectl exec``, which
  does inherit the image's environment. Two routes into the same container
  disagreeing about symbols is the confusing half.
* ``DEBUGINFOD_TIMEOUT`` — a bound on that fetch. Nothing sets it yet; a
  variable that is unset is simply absent from the session, so listing it here
  costs nothing and means the value arrives on the day something does.

An allow-list rather than the whole environment: the sshd config is a
world-readable file, and a seat's environment is where the launcher's secrets
(a host key, a public key) live.
"""

_SESSION_ENV_EXCLUDE = frozenset({PUBKEY_ENV, HOST_KEY_ENV})


def session_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The variables worth forwarding into an ssh session.

    >>> sorted(session_env({"PATH": "/bin", "HOME": "/root", "TERM": "xterm"}))
    ['PATH']
    """
    source = os.environ if env is None else env
    return {
        name: value
        for name, value in source.items()
        if (name.startswith(SESSION_ENV_PREFIX) or name in SESSION_ENV_NAMES)
        and name not in _SESSION_ENV_EXCLUDE
    }


def ensure_sshd_config(
    layout: SshdLayout, *, env: Mapping[str, str] | None = None
) -> bool:
    """Write the sshd config the ProxyCommand names with ``-f``.

    Raises after writing, never instead of writing, when a variable this seat
    meant to forward is one sshd's ``SetEnv`` parser cannot carry: the config
    holding everything that did survive beats no config at all, and
    :func:`ensure_all` turns the exception into a recorded failure without
    stopping the remaining steps. Silence is the one thing that is not on
    offer — a ``PATH`` dropped without a word is the defect ``SetEnv`` was
    widened to fix.
    """
    wanted = session_env(env)
    changed = _write_if_changed(
        Path(layout.config_path), sshd_config(layout, wanted), 0o644
    )
    refused = unsafe_set_env(wanted)
    if refused:
        raise RuntimeError(
            f"{layout.config_path} cannot carry {', '.join(refused)} into an ssh "
            "session: sshd reads SetEnv as whitespace-separated NAME=value "
            "pairs, so a name or a value containing whitespace or '=' would "
            "silently become a different directive. Everything else was "
            "written. `kubectl exec` sessions are unaffected - they inherit "
            "the container's environment directly."
        )
    return changed


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


def session_home(layout: SshdLayout) -> str:
    """Where an ssh session's ``$HOME`` lands, which is not always ``layout.home``.

    sshd sets a session's ``HOME`` from the *passwd record*, never from its own
    environment — the same rule that makes ``SetEnv`` the only way podbench's own
    variables reach a session — so this, not the container's ``$HOME``, is where
    ``~/.vscode-server`` is unpacked and where the machine settings have to be.

    For an ``attach`` seat the two agree, because the record is the one
    :func:`ensure_passwd_entry` wrote and it names ``layout.home``. A
    ``podbench dev`` sidecar is the shape that separates them: its ``$HOME`` is
    pinned to the workspace volume so uv's caches and venvs land there, while
    the projected ``podbench-identity`` record names
    :data:`podbench.model.SEAT_HOME_PATH` — which is where
    :data:`podbench.model.SEAT_HOME_VOLUME` is mounted, and so where these
    settings survive a re-attach. Writing them under ``layout.home`` would put
    them somewhere nothing ever looks.
    """
    return _home_for_uid() or layout.home


VSCODE_SETTINGS_WAY_OUT = (
    "Machine-level settings are the only ones that apply to every folder a "
    "Remote-SSH client opens, and without them File -> Open Folder -> / points "
    "the watcher and the search indexer at /proc, where every /proc/<pid>/root "
    "is a symlink into another container's rootfs and the walk has no bottom. "
    "A seat cannot reserve memory of its own (report 3.9) and an OOM-killed "
    "ephemeral container cannot be restarted, so that walk ends the seat and "
    "burns its name for the pod's lifetime. Until the file parses again, open "
    "/root rather than /, or repair it: podbench adds only missing keys and "
    "never overwrites one you set."
)
"""Named mechanism, then the way out — the shape :data:`NSS_WAY_OUT` uses.

The only reader is somebody whose own ``settings.json`` podbench refused to
touch, and the consequence of leaving it that way is not "an editor setting is
missing" but "the next folder you open may take the seat with it".
"""


def ensure_vscode_settings(layout: SshdLayout, *, home: str | None = None) -> bool:
    """Put VS Code's machine settings in place before a client ever connects.

    This is the agent's job rather than the image's because ``~/.vscode-server``
    is created by the *client* on first connect: a file baked in at build time
    would be in the image's ``/root`` and not in the home the passwd record
    names, and would be gone entirely from the fresh rootfs a pod restart hands
    an ephemeral container. "Prepare the container for a seat" is what every
    other step here does, and this belongs beside them — pre-creating the
    directory is harmless, since the client's installer only ever creates what
    is missing.

    ``home`` is a test seam; production passes none.
    """
    path = Path(home if home is not None else session_home(layout)) / (
        MACHINE_SETTINGS_PATH
    )
    existing = path.read_text() if path.is_file() else None
    try:
        merged = merge_machine_settings(existing)
    except ValueError as error:
        raise RuntimeError(
            f"{path} was left exactly as it is: {error}. {VSCODE_SETTINGS_WAY_OUT}"
        ) from error
    if merged is None:
        return False
    return _write_if_changed(path, merged, 0o644)


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
    # Before the registration, not after: this one only ever *removes* a write
    # bit, and on the rung it applies to nothing is going to be registered.
    step(
        "nss-db-mode",
        f"took group and other write off {SEAT_NSS_PATH}",
        lambda: restrict_seat_nss_database(layout),
    )
    # Before the host key, not after: ssh-keygen calls getpwuid() whatever it is
    # asked to do, so on a uid NSS cannot resolve it fails before writing a byte.
    step(
        "nss-identity",
        f"registered uid {os.geteuid()} as {SEAT_USER!r} in "
        f"{_nss_append_target(os.geteuid(), os.getegid())}",
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
    # `env=env` like every other step: the session environment the config
    # carries is the container's, and a caller that supplied one meant it.
    step(
        "sshd-config",
        f"wrote {layout.config_path}",
        lambda: ensure_sshd_config(layout, env=env),
    )
    # After the passwd entry, which is what decides the home this lands in: an
    # `attach` seat's record is written by the step above and a session's $HOME
    # follows it, not the container's.
    step(
        "vscode-settings",
        f"wrote {session_home(layout)}/{MACHINE_SETTINGS_PATH}",
        lambda: ensure_vscode_settings(layout),
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
            f"{failures} start-up check(s) failed; each one's reason is on a "
            "line above, and ssh into this container may be among the "
            f"casualties. Staying up: {EXEC_HALF_COMMANDS} and a shell are "
            "reached with kubectl exec and need none of sshd."
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
