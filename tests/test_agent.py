"""Tests for the in-pod agent.

Idempotency is the property under test almost everywhere: an ephemeral
container cannot be restarted, so re-running the agent against a container that
is already serving a session is the normal reconnection path.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from podbench import agent
from podbench.sshcfg import SshdLayout, sshd_config

PUBKEY = "ssh-ed25519 AAAAC3NzaC1FIRST dev@laptop"
SECOND_PUBKEY = "ssh-ed25519 AAAAC3NzaC1SECOND colleague@laptop"


def make_layout(tmp_path: Path, *, root: bool = True) -> SshdLayout:
    """A layout rooted in a tmp dir, so nothing here touches a real /etc."""
    return SshdLayout(
        run_as_root=root,
        home=str(tmp_path),
        config_path=str(tmp_path / "etc" / "podbench" / "sshd_config"),
        host_key_path=str(tmp_path / "etc" / "ssh" / "ssh_host_ed25519_key"),
        authorized_keys_path=str(tmp_path / ".ssh" / "authorized_keys"),
        privsep_dir=str(tmp_path / "run" / "sshd") if root else None,
    )


class FakeRunner:
    """Stands in for ssh-keygen and sshd -t, and records what was asked."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        self.calls.append(args)
        stdout = ""
        if args[0] == "ssh-keygen" and "-t" in args:
            key = Path(args[args.index("-f") + 1])
            key.write_text("PRIVATE KEY\n")
            Path(f"{key}.pub").write_text("ssh-ed25519 AAAAHOST podbench\n")
        if args[0] == "ssh-keygen" and "-y" in args:
            stdout = "ssh-ed25519 AAAADERIVED\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    def named(self, binary: str) -> list[list[str]]:
        return [call for call in self.calls if call[0].endswith(binary)]


class FakeClock:
    """Stands in for the ``time`` module so the idle loop runs instantly.

    It replaces ``agent.time`` wholesale rather than patching ``time.sleep``
    globally: a fake sleep with a real ``monotonic`` never reaches its deadline.
    """

    def __init__(self, *, signal_after: int | None = None) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self._signal_after = signal_after

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        if self._signal_after is not None and len(self.slept) >= self._signal_after:
            agent.request_stop(15, None)  # the kubelet's SIGTERM


def patch_layout(monkeypatch: pytest.MonkeyPatch, layout: SshdLayout) -> None:
    """Pin the layout main() would otherwise read off the real uid and HOME."""

    def for_uid(uid: int, home: str | None = None) -> SshdLayout:
        return layout

    monkeypatch.setattr(agent.SshdLayout, "for_uid", for_uid)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """An environment with a pubkey and no Secret mounts in play."""
    return {
        agent.PUBKEY_ENV: PUBKEY,
        agent.AUTHORIZED_KEYS_FILE_ENV: str(tmp_path / "absent-authorized_keys"),
        agent.HOST_KEY_FILE_ENV: str(tmp_path / "absent-hostkey"),
    }


def test_ensure_all_is_idempotent(tmp_path: Path, env: dict[str, str]) -> None:
    layout = make_layout(tmp_path)
    runner = FakeRunner()

    first = agent.ensure_all(layout, env=env, runner=runner)
    assert len(first) == 4
    before = {
        path: Path(path).read_text()
        for path in (
            layout.config_path,
            layout.host_key_path,
            layout.authorized_keys_path,
        )
    }

    # A second attach into the same container must change nothing at all.
    assert agent.ensure_all(layout, env=env, runner=runner) == []
    assert {path: Path(path).read_text() for path in before} == before
    assert len(runner.named("ssh-keygen")) == 1


def test_ensure_sshd_config_matches_the_generator(
    tmp_path: Path, env: dict[str, str]
) -> None:
    layout = make_layout(tmp_path, root=False)
    agent.ensure_all(layout, env=env, runner=FakeRunner())
    assert Path(layout.config_path).read_text() == sshd_config(layout)


def test_privsep_dir_created_only_for_root(tmp_path: Path) -> None:
    root = make_layout(tmp_path / "root")
    assert agent.ensure_privsep_dir(root) is True
    assert root.privsep_dir is not None
    assert Path(root.privsep_dir).is_dir()
    assert agent.ensure_privsep_dir(root) is False
    # sshd skips privilege separation when it is not uid 0.
    assert agent.ensure_privsep_dir(make_layout(tmp_path / "user", root=False)) is False


def test_authorized_keys_merges_a_second_session(
    tmp_path: Path, env: dict[str, str]
) -> None:
    layout = make_layout(tmp_path)
    assert agent.ensure_authorized_keys(layout, env=env) is True

    second = dict(env, **{agent.PUBKEY_ENV: SECOND_PUBKEY})
    assert agent.ensure_authorized_keys(layout, env=second) is True
    keys = Path(layout.authorized_keys_path).read_text().split("\n")
    # The first session's key survives; kicking it out would break its reconnect.
    assert PUBKEY in keys
    assert SECOND_PUBKEY in keys
    assert agent.ensure_authorized_keys(layout, env=second) is False


def test_authorized_keys_read_from_a_mounted_secret(tmp_path: Path) -> None:
    mounted = tmp_path / "secret" / "authorized_keys"
    mounted.parent.mkdir()
    mounted.write_text(f"{PUBKEY}\n")
    layout = make_layout(tmp_path)
    assert agent.ensure_authorized_keys(
        layout,
        env={
            agent.AUTHORIZED_KEYS_FILE_ENV: str(mounted),
            agent.HOST_KEY_FILE_ENV: str(tmp_path / "absent"),
        },
    )
    assert Path(layout.authorized_keys_path).read_text() == f"{PUBKEY}\n"


def test_supplied_host_key_wins_and_its_public_half_is_derived(
    tmp_path: Path,
) -> None:
    layout = make_layout(tmp_path)
    runner = FakeRunner()
    supplied = tmp_path / "secret-hostkey"
    supplied.write_text("SUPPLIED KEY\n")
    env = {agent.HOST_KEY_FILE_ENV: str(supplied)}

    assert agent.ensure_host_key(layout, env=env, runner=runner) is True
    assert Path(layout.host_key_path).read_text() == "SUPPLIED KEY\n"
    assert Path(layout.host_key_path).stat().st_mode & 0o777 == 0o600
    # The Secret carries only the private half; known_hosts needs the public one.
    assert agent.read_host_public_key(layout) == "ssh-ed25519 AAAADERIVED"
    assert agent.ensure_host_key(layout, env=env, runner=runner) is False


def test_minted_host_key_is_generated_once(tmp_path: Path, env: dict[str, str]) -> None:
    layout = make_layout(tmp_path)
    runner = FakeRunner()
    assert agent.ensure_host_key(layout, env=env, runner=runner) is True
    assert agent.ensure_host_key(layout, env=env, runner=runner) is False
    keygen = runner.named("ssh-keygen")
    assert len(keygen) == 1
    # -A would mint three keys, only one of which the config names.
    assert "-A" not in keygen[0]
    assert agent.read_host_public_key(layout) == "ssh-ed25519 AAAAHOST podbench"


def test_self_check_passes_on_a_prepared_container(
    tmp_path: Path, env: dict[str, str]
) -> None:
    layout = make_layout(tmp_path)
    runner = FakeRunner()
    agent.ensure_all(layout, env=env, runner=runner)
    results = agent.self_check(layout, runner=runner)
    assert [check.name for check in results if not check.ok] == []
    assert any(check.name == "stdio-roundtrip" and check.ok for check in results)


def test_self_check_names_what_is_missing(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    failed = {
        check.name
        for check in agent.self_check(layout, runner=FakeRunner())
        if not check.ok
    }
    assert {"privsep-dir", "host-key", "authorized-keys", "sshd-config"} <= failed


def test_self_check_asserts_the_transport_flags(tmp_path: Path) -> None:
    # The tripwire for a future "cleanup" that drops -e or adds -t.
    shape = agent.proxy_shape_check(make_layout(tmp_path))
    assert shape.ok
    assert "sshd -i -e" in shape.detail


def test_stdio_roundtrip_returns_a_delayed_second_line() -> None:
    result = agent.stdio_roundtrip_check(delay=0.05)
    assert result.ok, result.detail


def test_reaper_status_when_we_are_init() -> None:
    status = agent.reaper_status(pid=1)
    assert status.is_init
    assert "will be reaped" in status.note


def test_reaper_status_when_the_target_app_is_init(tmp_path: Path) -> None:
    # Under `kubectl debug --target` pid 1 is the workload, which does not reap.
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "comm").write_text("gunicorn\n")
    status = agent.reaper_status(pid=42, proc=tmp_path)
    assert not status.is_init
    assert status.namespace_init == "gunicorn"
    assert "zombies" in status.note


def test_idle_stops_after_its_bounded_iterations() -> None:
    status = agent.reaper_status(pid=42, proc=Path("/nonexistent"))
    assert agent.idle(status, interval=0.0, iterations=2) == 0


def test_idle_notices_a_sigterm_without_serving_out_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PEP 475 resumes an interrupted sleep, so the interval must be sliced.

    A 30 s sleep would swallow a SIGTERM for up to 30 s — at or past the
    kubelet's grace period, so the agent gets SIGKILLed instead of exiting 0.
    """
    clock = FakeClock(signal_after=1)
    monkeypatch.setattr(agent, "time", clock)
    status = agent.reaper_status(pid=42, proc=Path("/nonexistent"))

    assert agent.idle(status, interval=1800.0) == 0
    # One slice, not 1800 seconds: the signal was seen on the first look-up.
    assert clock.slept == [agent.IDLE_SLICE]


def test_idle_still_waits_the_whole_interval_when_nothing_interrupts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(agent, "time", clock)
    status = agent.reaper_status(pid=42, proc=Path("/nonexistent"))

    assert agent.idle(status, interval=2.0, iterations=1, slice_seconds=0.5) == 0
    assert clock.slept == [0.5, 0.5, 0.5, 0.5]


def test_main_ensure_only(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = make_layout(tmp_path)
    patch_layout(monkeypatch, layout)
    monkeypatch.setattr(agent, "run_command", FakeRunner())
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert agent.main(["--ensure-only"]) == 0
    assert Path(layout.config_path).is_file()
    # Rerunning is the reconnect path, not an error.
    assert agent.main(["--ensure-only"]) == 0


def test_main_self_check_only_does_not_write(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = make_layout(tmp_path)
    patch_layout(monkeypatch, layout)
    monkeypatch.setattr(agent, "run_command", FakeRunner())
    assert agent.main(["--self-check"]) == 1
    assert not Path(layout.config_path).exists()


def test_main_print_host_key(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    layout = make_layout(tmp_path)
    patch_layout(monkeypatch, layout)
    monkeypatch.setattr(agent, "run_command", FakeRunner())
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert agent.main(["--print-host-key", "--no-self-check"]) == 0
    assert "ssh-ed25519 AAAAHOST podbench" in capsys.readouterr().out
