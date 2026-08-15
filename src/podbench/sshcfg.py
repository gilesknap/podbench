"""Both ends of podbench's ssh transport, generated from one place.

The transport is a complete OpenSSH connection whose only carrier is
``kubectl exec``: the client runs ``sshd -i`` inside the debug container as its
``ProxyCommand``, so there is no listening socket in the pod, no port-forward
and no pod IP to reach. Authentication to the cluster is whatever the kubeconfig
already does; authentication to the container is an ssh key.

Every constant here is load-bearing and was measured rather than chosen. The
spike (``docs/explanations/spikes/s1.md``) found that the failure modes of this
transport are *silent* — a wrong flag produces a network-looking error or, worse,
a truncated stream with ``rc=0`` — so the client config, the server config and
the ProxyCommand are generated together and never hand-written.
"""

from __future__ import annotations

import enum
import os
import re
import shlex
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .model import ContainerRef

__all__ = [
    "CONTROL_DIR",
    "CONTROL_PATH",
    "CONTROL_PATH_HASH_CHARS",
    "CONTROL_PERSIST",
    "DEFAULT_SFTP_SERVER",
    "DEFAULT_SSHD",
    "FALLBACK_HOME_PREFIX",
    "PRIVSEP_DIR",
    "QUIET_LOG_LEVELS",
    "SERVER_ALIVE_COUNT_MAX",
    "SERVER_ALIVE_INTERVAL",
    "SUN_PATH_MAX",
    "HostKeyBinding",
    "HostKeyPolicy",
    "KubectlInvocation",
    "SshdLayout",
    "client_config",
    "control_path_ok",
    "default_home",
    "ensure_control_dir",
    "expanded_control_path_length",
    "host_key_alias",
    "known_hosts_entry",
    "proxy_command",
    "proxy_command_string",
    "sshd_config",
]

DEFAULT_SSHD = "/usr/sbin/sshd"
DEFAULT_SFTP_SERVER = "/usr/lib/openssh/sftp-server"

ROOT_HOME = "/root"
ROOT_CONFIG = "/etc/podbench/sshd_config"
ROOT_HOST_KEY = "/etc/ssh/ssh_host_ed25519_key"
PRIVSEP_DIR = "/run/sshd"
"""sshd's privilege separation directory. Mandatory when sshd runs as root —
without it every connection dies with ``Missing privilege separation directory``
— and never consulted when it does not (s1 §8)."""

_SETENV_SAFE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
"""sshd parses SetEnv as whitespace-separated NAME=value pairs, so a name
with a space or an `=` in it would silently become a different directive."""

SUN_PATH_MAX = 108
"""``sizeof(struct sockaddr_un.sun_path)``. ssh refuses a longer ControlPath
with ``ControlPath too long (... >= 108 bytes)``, which s1 hit by putting the
socket next to the kubeconfig."""

CONTROL_PATH_HASH_CHARS = 40
"""Width of ssh's ``%C`` expansion (a hex digest), used to bound the socket path
before ssh does."""

CONTROL_DIR = "/tmp/podbench-cm"
CONTROL_PATH = f"{CONTROL_DIR}/%C"

QUIET_LOG_LEVELS = frozenset({"QUIET", "FATAL", "ERROR"})
"""sshd log levels that emit no bytes on a healthy connection. Anything chattier
lands on the ssh client's stderr, which a Remote-SSH client parses."""

SERVER_ALIVE_INTERVAL = 15
SERVER_ALIVE_COUNT_MAX = 3
"""A *stalled* transport — the shape an apiserver or konnectivity hiccup takes —
hangs ssh forever without keepalives, and fails in seconds with them (s1 §5)."""

CONTROL_PERSIST = "10m"

FALLBACK_HOME_PREFIX = "/tmp/podbench-"
"""Prefix of the home directory used when the container has none.

``runAsUser: <target uid>`` on a stock image is the *normal* shape of the
degraded rung, and that uid usually has no ``/etc/passwd`` entry; the podbench
image leaves ``HOME`` unset on top of that. There is then no home to derive, and
asking the platform for one raises. A deterministic ``/tmp`` path is the answer
because the non-root layout already sets ``StrictModes no`` — sshd's ownership
audit, the one thing that would object to a home under ``/tmp``, is off.
"""


def default_home(uid: int) -> str:
    """Where a non-root agent keeps its files when nothing has told it.

    ``$HOME`` first, then the platform's own answer (an ``/etc/passwd`` entry),
    then :data:`FALLBACK_HOME_PREFIX` plus the uid. The last step is not
    defensive padding: ``Path.home()`` *raises* when ``HOME`` is unset and the
    uid has no passwd entry, which is precisely the container the degraded rung
    creates, and the agent would die before running a single check with a
    message naming nothing.

    ``uid`` names the last resort only: the middle step asks the platform about
    the *running* process, which is the same thing whenever the agent is asking
    about itself.
    """
    env_home = os.environ.get("HOME", "").strip()
    if env_home:
        return env_home
    try:
        return str(Path.home())
    except RuntimeError:
        return f"{FALLBACK_HOME_PREFIX}{uid}"


@dataclass(frozen=True)
class KubectlInvocation:
    """How to invoke kubectl, so the transport inherits the user's own auth.

    podbench shells out rather than using a Kubernetes client library
    deliberately: exec credential plugins, contexts and proxies then work with
    no code of ours in the path.

    This is argv, not a client: it names no namespace and makes no calls,
    because the only thing it builds is the string ssh hands to ``/bin/sh -c``.
    :class:`podbench.kubectl.Kubectl` is the client, and it takes the same
    ``binary``/``context``/``kubeconfig`` so that one session's API calls and
    its transport cannot end up pointed at different clusters.
    """

    binary: str = "kubectl"
    context: str | None = None
    kubeconfig: str | None = None

    def base(self) -> list[str]:
        """The argv prefix common to every kubectl invocation."""
        argv = [self.binary]
        if self.kubeconfig is not None:
            argv += ["--kubeconfig", self.kubeconfig]
        if self.context is not None:
            argv += ["--context", self.context]
        return argv


@dataclass(frozen=True)
class SshdLayout:
    """Where sshd's files live inside the debug container.

    Two layouts exist because the transport does not need root: sshd skips
    privilege separation when it is not uid 0, and then needs neither
    ``/run/sshd`` nor anything under ``/etc`` (s1 §8). Keeping the non-root
    layout user-relative is what lets one image serve both a root debug
    container and a PSA-``restricted`` one, with only the ptrace features
    gated on root.
    """

    run_as_root: bool
    home: str
    config_path: str
    host_key_path: str
    authorized_keys_path: str
    privsep_dir: str | None
    sftp_server: str = DEFAULT_SFTP_SERVER
    sshd: str = DEFAULT_SSHD

    @classmethod
    def for_uid(cls, uid: int, home: str | None = None) -> SshdLayout:
        """The layout an agent running as ``uid`` should use."""
        if uid == 0:
            root_home = home if home is not None else ROOT_HOME
            return cls(
                run_as_root=True,
                home=root_home,
                config_path=ROOT_CONFIG,
                host_key_path=ROOT_HOST_KEY,
                authorized_keys_path=f"{root_home}/.ssh/authorized_keys",
                privsep_dir=PRIVSEP_DIR,
            )
        user_home = home if home is not None else default_home(uid)
        base = f"{user_home}/.podbench"
        return cls(
            run_as_root=False,
            home=user_home,
            config_path=f"{base}/sshd_config",
            host_key_path=f"{base}/ssh_host_ed25519_key",
            authorized_keys_path=f"{user_home}/.ssh/authorized_keys",
            privsep_dir=None,
        )

    @property
    def host_public_key_path(self) -> str:
        """Where ``ssh-keygen`` leaves the public half of the host key."""
        return f"{self.host_key_path}.pub"


class HostKeyPolicy(enum.Enum):
    """How the pod's ssh host key identity is established.

    Neither option is ``StrictHostKeyChecking no``. That is what the spike used
    and it is the one outcome worth refusing to ship: a debugging tool that
    teaches users to disable host verification has taught them something they
    will apply elsewhere.
    """

    SECRET = "secret"
    """The host key is delivered to the container from a Secret or an env var,
    so the pod's identity is stable across re-attaches and across the pod
    restarts that give an ephemeral container a fresh rootfs. Real verification:
    one ``known_hosts`` entry outlives every session."""

    PER_ATTACH = "per-attach"
    """No key was supplied, so the agent minted one. The key is meaningless
    beyond this attach, so podbench records it under a ``HostKeyAlias`` keyed on
    the pod UID and manages the ``known_hosts`` file itself. Verification is
    still on — it just protects a one-session identity, and a re-attach after a
    pod restart legitimately produces a different key under a different alias."""


@dataclass(frozen=True)
class HostKeyBinding:
    """The client-side half of the host key policy."""

    policy: HostKeyPolicy
    alias: str
    known_hosts: str
    public_key: str | None = None
    """The host's public key, present when podbench minted or supplied it and so
    can write the ``known_hosts`` entry itself."""

    @classmethod
    def from_secret(
        cls, alias: str, known_hosts: str, public_key: str
    ) -> HostKeyBinding:
        """A stable identity supplied out of band."""
        return cls(HostKeyPolicy.SECRET, alias, known_hosts, public_key)

    @classmethod
    def per_attach(
        cls, pod_uid: str, known_hosts: str, public_key: str
    ) -> HostKeyBinding:
        """A throwaway identity, namespaced by the pod's UID."""
        return cls(
            HostKeyPolicy.PER_ATTACH, host_key_alias(pod_uid), known_hosts, public_key
        )


def host_key_alias(pod_uid: str) -> str:
    """The ``HostKeyAlias`` for a pod.

    Pod UIDs are unique for all time, so a recreated pod cannot inherit the
    trust of its predecessor — which is the correct behaviour, since its
    ephemeral container really is a different machine.
    """
    return f"podbench-{pod_uid}"


def known_hosts_entry(binding: HostKeyBinding) -> str:
    """One ``known_hosts`` line pinning ``binding``'s key to its alias."""
    if binding.public_key is None:
        raise ValueError("cannot pin a host key that was never observed")
    fields = binding.public_key.split()
    if len(fields) < 2:
        raise ValueError(f"not an ssh public key: {binding.public_key!r}")
    return f"{binding.alias} {fields[0]} {fields[1]}\n"


def sshd_config(layout: SshdLayout, set_env: Mapping[str, str] | None = None) -> str:
    """The server config the ProxyCommand's sshd is pointed at.

    Deliberately not the distro's ``/etc/ssh/sshd_config``: pointing ``-f`` at
    our own file leaves the image's normal sshd usable and makes the working
    configuration a reviewable artifact. There is no ``PidFile`` directive —
    ``sshd -i`` never writes one.

    ``set_env`` is how the launcher's container environment reaches a session.
    sshd deliberately does not leak its own environment to the commands it runs,
    so the ``PODBENCH_TARGET_CID`` the launcher carefully injected into the
    container spec is invisible to ``pids`` and ``capreport`` over ssh, and both
    silently fall back to guessing which processes belong to the target. Naming
    the variables in the config is the only route that survives a
    non-interactive ``ssh host 'pids'``, which sources no profile at all.
    """
    lines = [
        "# Generated by podbench. Regenerated at container start; do not edit.",
        f"HostKey {layout.host_key_path}",
    ]
    for name, value in sorted((set_env or {}).items()):
        if _SETENV_SAFE.fullmatch(name) and "\n" not in value:
            lines.append(f"SetEnv {name}={value}")
    if layout.run_as_root:
        lines.append("PermitRootLogin prohibit-password")
    lines.append(f"AuthorizedKeysFile {layout.authorized_keys_path}")
    lines.append("UsePAM no")
    # Beyond the five lines s1 validated: no account in the image has a
    # password, so refusing password auth outright costs nothing and closes the
    # hole if one ever gains one.
    lines.append("PasswordAuthentication no")
    if not layout.run_as_root:
        # The container's home directory is whatever the image made it, and
        # sshd's ownership/permission audit refuses to run under anything it
        # considers loose.
        lines.append("StrictModes no")
    lines.append(f"Subsystem sftp {layout.sftp_server}")
    return "\n".join(lines) + "\n"


def proxy_command(
    target: ContainerRef,
    *,
    layout: SshdLayout,
    kubectl: KubectlInvocation | None = None,
    log_level: str = "ERROR",
) -> list[str]:
    """The exact ProxyCommand argv. Three flags here are not negotiable.

    ``-i`` puts sshd in inetd mode, which is what makes the exec channel itself
    the transport.

    ``-e`` is about **keeping fd 2 open**, not about where logs go. Without it
    sshd's inetd path points fd 2 at ``/dev/null``; the CRI stderr pipe then
    reaches EOF and containerd tears down the *whole* exec session, killing
    stdin and stdout mid-KEX. The user sees ``ssh_dispatch_run_fatal:
    Connection to UNKNOWN port 65535: Broken pipe`` and goes looking for a
    network fault. The same teardown truncates any exec stream silently with
    ``rc=0``, so this is not an ssh quirk to work around elsewhere.

    ``-o LogLevel=ERROR`` answers the objection ``-e`` would otherwise raise:
    it emits zero stderr bytes on a healthy connection while leaving fd 2 owned
    and open.

    Consequently: never ``-t`` (with a real TTY the client hangs forever), never
    ``2>&1`` (log lines land mid-protocol and the client dies), never a wrapper
    that redirects or closes stderr. A wrapper shell that does *not* ``exec``
    also works — by holding fd 2 on sshd's behalf — which is exactly how this
    requirement stays hidden until someone adds ``exec`` for tidiness.
    """
    if log_level not in QUIET_LOG_LEVELS:
        raise ValueError(
            f"LogLevel={log_level} would write to the ssh client's stderr; "
            f"use one of {sorted(QUIET_LOG_LEVELS)}"
        )
    kube = kubectl if kubectl is not None else KubectlInvocation()
    return [
        *kube.base(),
        "-n",
        target.pod.namespace,
        "exec",
        "-i",
        target.pod.name,
        "-c",
        target.container,
        "--",
        layout.sshd,
        "-i",
        "-e",
        "-f",
        layout.config_path,
        "-o",
        f"LogLevel={log_level}",
    ]


def proxy_command_string(
    target: ContainerRef,
    *,
    layout: SshdLayout,
    kubectl: KubectlInvocation | None = None,
    log_level: str = "ERROR",
) -> str:
    """:func:`proxy_command` quoted for an ssh config file.

    ssh hands the ProxyCommand to ``/bin/sh -c``, so a kubeconfig path with a
    space in it has to survive a round trip through the shell.
    """
    return shlex.join(
        proxy_command(target, layout=layout, kubectl=kubectl, log_level=log_level)
    )


def expanded_control_path_length(path: str) -> int:
    """Length of ``path`` once ssh has expanded ``%C``.

    Only ``%C`` is expanded — it is the only token podbench's own default uses.
    A caller substituting ``%h``/``%r``/``%p`` is on its own, because their
    widths are not knowable here.
    """
    return len(path.replace("%C", "C" * CONTROL_PATH_HASH_CHARS))


def control_path_ok(path: str) -> bool:
    """Whether ``path`` will fit in an AF_UNIX socket address.

    Checked here so the failure is a podbench message naming the socket
    directory, rather than ssh's ``ControlPath too long`` arriving in the
    middle of an editor's connection attempt.
    """
    return expanded_control_path_length(path) < SUN_PATH_MAX


def ensure_control_dir(path: str = CONTROL_DIR) -> Path:
    """Create the ControlMaster socket directory, 0700, and return it.

    Sockets are created by ssh with the umask applied, so the directory is the
    only thing standing between another user on the machine and a live
    multiplexed session.
    """
    directory = Path(path)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid():
        raise RuntimeError(
            f"{directory} is not owned by this user; refusing to share a "
            "ControlMaster directory"
        )
    if stat.S_IMODE(info.st_mode) != 0o700:
        directory.chmod(0o700)
    return directory


def client_config(
    target: ContainerRef,
    *,
    host_alias: str,
    identity_file: str,
    host_key: HostKeyBinding,
    layout: SshdLayout,
    user: str = "root",
    kubectl: KubectlInvocation | None = None,
    control_path: str = CONTROL_PATH,
    log_level: str = "ERROR",
) -> str:
    """An ``Include``-able stanza for the user's ssh config.

    Written as an include rather than an edit of ``~/.ssh/config`` so podbench
    can regenerate it wholesale on every attach without ever owning a file the
    user also edits.
    """
    if not control_path_ok(control_path):
        raise ValueError(
            f"ControlPath {control_path!r} expands to "
            f"{expanded_control_path_length(control_path)} bytes, over the "
            f"{SUN_PATH_MAX}-byte AF_UNIX limit; keep it under {CONTROL_DIR}"
        )
    proxy = proxy_command_string(
        target, layout=layout, kubectl=kubectl, log_level=log_level
    )
    lines = [
        "# Generated by podbench. Regenerated on every attach; do not edit.",
        f"# target: {target}",
        f"Host {host_alias}",
        f"    HostName {target.pod.name}",
        f"    User {user}",
        f"    IdentityFile {identity_file}",
        "    IdentitiesOnly yes",
        f"    ProxyCommand {proxy}",
        f"    ServerAliveInterval {SERVER_ALIVE_INTERVAL}",
        f"    ServerAliveCountMax {SERVER_ALIVE_COUNT_MAX}",
        "    ControlMaster auto",
        f"    ControlPath {control_path}",
        f"    ControlPersist {CONTROL_PERSIST}",
        f"    HostKeyAlias {host_key.alias}",
        f"    UserKnownHostsFile {host_key.known_hosts}",
        "    StrictHostKeyChecking yes",
    ]
    return "\n".join(lines) + "\n"
