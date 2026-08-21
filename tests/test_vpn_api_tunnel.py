"""What ``k8s/vpn-api-tunnel.sh`` does to a kubeconfig, and what it leaves alone.

The script's job is one substitution — the API server's address becomes a local
port — and everything else in the file has to survive it. That is easy to assert
and easy to break: ``kubectl config set-cluster`` merges most fields but writes
its *defaults* over boolean flags, so the copy can quietly stop being the
credential it was copied from.

The tunnel itself is not exercised here. Starting one needs an ssh server that
can reach a real API server, which is an e2e concern; ``--config-only`` stops
the script exactly where the network would begin.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "k8s" / "vpn-api-tunnel.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("kubectl") is None or not SCRIPT.is_file(),
    reason="kubectl (or the script) is not present, so nothing can be rewritten",
)

SOURCE = {
    "apiVersion": "v1",
    "kind": "Config",
    "clusters": [
        {
            "name": "target",
            "cluster": {
                "server": "https://k8s-api.example.ac.uk:6443",
                "certificate-authority-data": "Zm9vCg==",
            },
        },
        # A second cluster the current context does not name. --minify must
        # drop it: `clusters[0]` is read for the address, and on a kubeconfig
        # holding two clusters the first one is not reliably the right one.
        {"name": "elsewhere", "cluster": {"server": "https://other.example:443"}},
    ],
    "users": [{"name": "sa", "user": {"token": "a-live-bearer-token"}}],
    "contexts": [
        {
            "name": "sa",
            "context": {"cluster": "target", "user": "sa", "namespace": "beamline"},
        },
        {"name": "unused", "context": {"cluster": "elsewhere", "user": "sa"}},
    ],
    "current-context": "sa",
}


def write_config(path: Path, config: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(config))
    return path


def run(
    src: Path | str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """The script in ``--config-only`` mode, which never touches the network."""
    return subprocess.run(
        [str(SCRIPT), str(src), "--config-only", *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )


def fake_ssh(tmp_path: Path, serves: Path) -> dict[str, str]:
    """An environment whose ``ssh`` reads a local file and records its argv.

    The script calls ``ssh`` by name, so a directory ahead of it on ``PATH`` is
    the whole seam - no hook in the script itself, and the argv under test is
    the argv a real ssh would have received.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "ssh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> "{tmp_path}/ssh-argv"\n'
        # -O check: no tunnel is running, which is what a fresh run must see.
        'for a in "$@"; do [ "$a" = check ] && exit 1; done\n'
        'case "${*: -1}" in\n'
        f'  cat*) cat "{serves}" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def test_a_remote_kubeconfig_is_read_over_ssh_and_not_kept(tmp_path: Path) -> None:
    """The reason for naming one remotely: it is input, not something to store.

    A kubeconfig carries a live bearer token, so a copy fetched here to be read
    once is a credential lying around afterwards. It goes to a temporary file
    that is removed on any exit.
    """
    serves = write_config(tmp_path / "source.yaml", SOURCE)
    env = fake_ssh(tmp_path, serves)
    out = tmp_path / "fetched-tunnel.kubeconfig"

    result = run("ws001:/home/you/beamline.kubeconfig", "--out", str(out), env=env)

    assert "not kept here" in result.stdout
    assert cluster_of(yaml.safe_load(out.read_text()))["server"].startswith(
        "https://127.0.0.1:"
    )
    argv = (tmp_path / "ssh-argv").read_text()
    assert "ws001" in argv
    assert "cat --" in argv
    assert not list(Path(tempfile.gettempdir()).glob("podbench-kubeconfig.*"))


def test_the_host_it_came_from_is_the_tunnels_default_exit(tmp_path: Path) -> None:
    """A machine holding the kubeconfig can usually reach the API it names.

    Said out loud rather than assumed silently, because the exit host is the
    address the API server sees, and access is allow-listed by source IP on the
    clusters this exists for.
    """
    serves = write_config(tmp_path / "source.yaml", SOURCE)
    env = fake_ssh(tmp_path, serves)

    result = run(
        "you@ws001.example.ac.uk:beamline.kubeconfig",
        "--out",
        str(tmp_path / "o.kubeconfig"),
        env=env,
    )

    assert "--ssh-host defaults to you@ws001.example.ac.uk" in result.stdout
    assert "-L 127.0.0.1:6443:k8s-api.example.ac.uk:6443 you@ws001.example.ac.uk" in (
        result.stdout
    )


def test_a_colon_after_a_slash_is_a_local_path(tmp_path: Path) -> None:
    """scp's own rule, and the naming that follows from it.

    Stripping to the first colon unconditionally would read this as a host and
    would name the output after the remainder, turning ``od:d.kubeconfig`` into
    ``d-tunnel.kubeconfig``.
    """
    src = write_config(tmp_path / "od:d.kubeconfig", SOURCE)
    run(src, "--ssh-host", "bastion")

    assert (tmp_path / "od:d-tunnel.kubeconfig").is_file()


def tunnelled(src: Path) -> dict[str, Any]:
    """The kubeconfig the script wrote beside ``src``."""
    out = src.with_name(f"{src.stem}-tunnel.kubeconfig")
    return yaml.safe_load(out.read_text())


def cluster_of(config: dict[str, Any]) -> dict[str, Any]:
    return config["clusters"][0]["cluster"]


def test_the_address_moves_to_localhost_and_the_certificate_still_verifies(
    tmp_path: Path,
) -> None:
    """The one substitution, and the field that keeps TLS working across it.

    Pointing a client at 127.0.0.1 would fail verification against a
    certificate issued for the server's own name. ``tls-server-name`` is what
    keeps the source's CA usable, and its absence is the failure that tempts
    people into ``insecure-skip-tls-verify`` — throwing away a CA the file
    already carries.
    """
    src = write_config(tmp_path / "beamline.kubeconfig", SOURCE)
    run(src, "--ssh-host", "bastion", "--local-port", "7443")

    cluster = cluster_of(tunnelled(src))
    assert cluster["server"] == "https://127.0.0.1:7443"
    assert cluster["tls-server-name"] == "k8s-api.example.ac.uk"


def test_everything_that_is_not_the_address_is_carried_over(tmp_path: Path) -> None:
    """A copy that lost the token or the CA is not the credential it copied."""
    src = write_config(tmp_path / "beamline.kubeconfig", SOURCE)
    run(src, "--ssh-host", "bastion")

    config = tunnelled(src)
    assert cluster_of(config)["certificate-authority-data"] == "Zm9vCg=="
    assert config["users"][0]["user"]["token"] == "a-live-bearer-token"
    assert config["contexts"][0]["context"]["namespace"] == "beamline"
    assert config["current-context"] == "sa"


def test_only_the_current_context_survives(tmp_path: Path) -> None:
    """--minify, so that reading ``clusters[0]`` is reading the right cluster."""
    src = write_config(tmp_path / "beamline.kubeconfig", SOURCE)
    run(src, "--ssh-host", "bastion")

    config = tunnelled(src)
    assert [c["name"] for c in config["clusters"]] == ["target"]
    assert [c["name"] for c in config["contexts"]] == ["sa"]


def test_a_source_that_declines_to_verify_still_declines(tmp_path: Path) -> None:
    """The regression this file exists for.

    ``insecure-skip-tls-verify`` is a boolean flag with a default, so
    ``set-cluster --server`` alone writes ``false`` over it. The copy then
    verifies against a CA it does not have, and fails with a TLS error that
    points at the tunnel rather than at the rewrite.
    """
    source = {
        **SOURCE,
        "clusters": [
            {
                "name": "target",
                "cluster": {
                    "server": "https://10.1.2.3:6443",
                    "insecure-skip-tls-verify": True,
                },
            }
        ],
        "contexts": [{"name": "sa", "context": {"cluster": "target", "user": "sa"}}],
    }
    src = write_config(tmp_path / "bed.kubeconfig", source)
    run(src, "--ssh-host", "bastion")

    cluster = cluster_of(tunnelled(src))
    assert cluster["insecure-skip-tls-verify"] is True
    # Not stated beside a check that is not happening.
    assert "tls-server-name" not in cluster


def test_a_server_with_no_port_is_forwarded_to_443(tmp_path: Path) -> None:
    """The default the URL leaves implicit has to be explicit in ``ssh -L``."""
    source = {
        **SOURCE,
        "clusters": [
            {"name": "target", "cluster": {"server": "https://api.example.ac.uk"}}
        ],
        "contexts": [{"name": "sa", "context": {"cluster": "target", "user": "sa"}}],
    }
    src = write_config(tmp_path / "implicit.kubeconfig", source)
    result = run(src, "--ssh-host", "bastion", "--local-port", "7443")

    assert "-L 127.0.0.1:7443:api.example.ac.uk:443" in result.stdout


def test_an_inherited_proxy_is_named_rather_than_silently_carried(
    tmp_path: Path,
) -> None:
    """A proxy for the real address cannot serve the loopback one.

    client-go applies ``proxy-url`` to every connection the cluster entry
    makes, so an internal proxy inherited from the source breaks the copy. It
    is reported rather than removed: a SOCKS entry somebody added on purpose is
    indistinguishable from a corporate one that came with the file.
    """
    source = {
        **SOURCE,
        "clusters": [
            {
                "name": "target",
                "cluster": {
                    "server": "https://api.example.ac.uk:6443",
                    "proxy-url": "http://proxy.internal:3128",
                },
            }
        ],
        "contexts": [{"name": "sa", "context": {"cluster": "target", "user": "sa"}}],
    }
    src = write_config(tmp_path / "proxied.kubeconfig", source)
    result = run(src, "--ssh-host", "bastion")

    assert "proxy-url" in result.stdout
    assert "--proxy-url=''" in result.stdout


def test_a_tunnel_needs_somewhere_to_exit(tmp_path: Path) -> None:
    """Without --config-only there is a forward to start, and it needs a host."""
    src = write_config(tmp_path / "beamline.kubeconfig", SOURCE)
    result = subprocess.run(
        [str(SCRIPT), str(src)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 1
    assert "--ssh-host is required" in result.stderr
