"""Client wiring for SSH carried entirely by ``kubectl exec``."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path

from .kubectl import Kubectl, KubectlError
from .model import PodRef, as_dict
from .ssh_agent import PYTHON, ROOT_SSH_CAPABILITIES, ServerInfo

DEFAULT_IDENTITY = "~/.ssh/id_ed25519"
DEFAULT_CONFIG_DIR = "~/.podbench"
CONFIG_DIR_ENV = "PODBENCH_CONFIG_DIR"
CONTROL_DIR = Path("/tmp/podbench-cm")
CAPABILITY_PROBE = (
    "import os; print(os.geteuid(), next(line.split()[1] for line in "
    "open('/proc/self/status') if line.startswith('CapEff:')))"
)


@dataclass(frozen=True)
class SSHWiring:
    alias: str
    config: Path
    include: str

    @property
    def command(self) -> str:
        return shlex.join(["ssh", "-F", str(self.config), self.alias])


def read_public_key(identity: str) -> tuple[Path, str]:
    private = Path(identity).expanduser().resolve()
    public = private.with_name(f"{private.name}.pub")
    if not private.is_file():
        raise KubectlError(f"no SSH private key at {private}")
    if not public.is_file():
        raise KubectlError(f"no SSH public key at {public}")
    return private, public.read_text().strip()


def _server_info(kubectl: Kubectl, pod: str, seat: str, public_key: str) -> ServerInfo:
    result = kubectl.exec_(
        pod,
        [PYTHON, "-m", "podbench.ssh_agent", "--ensure"],
        container=seat,
        stdin=public_key,
    )
    try:
        value = json.loads(result.stdout)
        return ServerInfo(
            login=str(value["login"]),
            host_public_key=str(value["host_public_key"]),
            config_path=str(value["config_path"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise KubectlError("the seat returned invalid SSH setup information") from error


def _quote_config(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _host_key_entry(alias: str, public_key: str) -> str:
    fields = public_key.split()
    if len(fields) < 2:
        raise KubectlError("the seat returned an invalid SSH host public key")
    return f"{alias} {fields[0]} {fields[1]}"


def _write_known_hosts(path: Path, alias: str, public_key: str) -> None:
    previous = path.read_text().splitlines() if path.is_file() else []
    kept = [line for line in previous if line and not line.startswith(f"{alias} ")]
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text("\n".join([*kept, _host_key_entry(alias, public_key)]) + "\n")
    path.chmod(0o600)


def _control_path(host_key_alias: str) -> str:
    digest = hashlib.sha256(host_key_alias.encode()).hexdigest()[:16]
    return f"{CONTROL_DIR}/%C-{digest}"


def _ensure_control_dir() -> None:
    CONTROL_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = CONTROL_DIR.stat()
    if info.st_uid != os.getuid():
        raise KubectlError(f"{CONTROL_DIR} is not owned by the current user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        CONTROL_DIR.chmod(0o700)


def client_directory(config_dir: str | None = None) -> Path:
    return (
        Path(config_dir or os.environ.get(CONFIG_DIR_ENV, DEFAULT_CONFIG_DIR))
        .expanduser()
        .resolve()
    )


def include_line(directory: Path) -> str:
    glob = str(directory / "config.d" / "*.conf")
    return f"Include {_quote_config(glob)}"


def missing_ssh_capabilities(
    kubectl: Kubectl, pod: PodRef, seat: str
) -> tuple[str, ...] | None:
    result = kubectl.exec_(
        pod.name,
        [PYTHON, "-c", CAPABILITY_PROBE],
        container=seat,
        check=False,
    )
    try:
        uid, mask = result.stdout.split()
        runtime_uid = int(uid)
        effective = int(mask, 16)
    except ValueError:
        return None
    if result.returncode or runtime_uid != 0:
        return () if not result.returncode else None
    return tuple(
        name
        for name, bit in ROOT_SSH_CAPABILITIES.items()
        if not effective & (1 << bit)
    )


def _seat_suffix(seat: str) -> str:
    return seat.removeprefix("podbench-")


def wire_ssh(
    kubectl: Kubectl,
    pod: PodRef,
    seat: str,
    *,
    identity: str = DEFAULT_IDENTITY,
    config_dir: str | None = None,
) -> SSHWiring:
    private_key, public_key = read_public_key(identity)
    server = _server_info(kubectl, pod.name, seat, public_key)
    pod_json = kubectl.get_pod(pod.name)
    pod_uid = str(as_dict(pod_json.get("metadata")).get("uid") or pod.name)
    label = _seat_suffix(seat)
    alias = f"podbench.{pod.namespace}.{pod.name}.{label}"
    host_key_alias = f"podbench-{pod_uid}-{seat}"

    directory = client_directory(config_dir)
    config_directory = directory / "config.d"
    config = config_directory / f"{pod.namespace}-{pod.name}-{label}.conf"
    known_hosts = directory / "known_hosts"
    _write_known_hosts(known_hosts, host_key_alias, server.host_public_key)
    _ensure_control_dir()

    proxy = [kubectl.binary]
    if kubectl.context:
        proxy += ["--context", kubectl.context]
    proxy += [
        "-n",
        pod.namespace,
        "exec",
        "-i",
        pod.name,
        "-c",
        seat,
        "--",
        "/usr/sbin/sshd",
        "-i",
        "-e",
        "-f",
        server.config_path,
        "-o",
        "LogLevel=ERROR",
    ]
    stanza = "\n".join(
        [
            "# Generated by podbench; regenerated on every attach.",
            f"Host {alias}",
            f"    HostName {pod.name}",
            f"    User {server.login}",
            f"    IdentityFile {_quote_config(str(private_key))}",
            "    IdentitiesOnly yes",
            f"    ProxyCommand {shlex.join(proxy)}",
            "    ServerAliveInterval 15",
            "    ServerAliveCountMax 3",
            "    ControlMaster auto",
            f"    ControlPath {_control_path(host_key_alias)}",
            "    ControlPersist 10m",
            f"    HostKeyAlias {host_key_alias}",
            f"    UserKnownHostsFile {_quote_config(str(known_hosts))}",
            "    StrictHostKeyChecking yes",
            "",
        ]
    )
    config_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.write_text(stanza)
    config.chmod(0o600)
    return SSHWiring(alias, config, include_line(directory))
