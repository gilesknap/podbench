"""Tests for the in-pod agent.

Idempotency is the property under test almost everywhere: an ephemeral
container cannot be restarted, so re-running the agent against a container that
is already serving a session is the normal reconnection path.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from podbench import agent
from podbench.model import SEAT_IDENTITY_VOLUME
from podbench.sshcfg import SEAT_USER, SshdLayout, sshd_config
from podbench.vscode import MACHINE_SETTINGS_PATH

PUBKEY = "ssh-ed25519 AAAAC3NzaC1FIRST dev@laptop"
SECOND_PUBKEY = "ssh-ed25519 AAAAC3NzaC1SECOND colleague@laptop"

UNKNOWN_UID = 4242
"""A uid the fake NSS database has no record of — the shape of the degraded
rung, where the seat runs as a target uid no image could have an account for."""


class FakePasswd:
    """A stand-in for the NSS ``files`` database: the file *and* ``getpwuid``.

    Both halves are faked because a test that asked the real platform would
    pass or fail on whichever uid the suite happens to run as, and the bug under
    test is exactly "this uid has no entry". Seeded with a record for the
    running uid, so the default state of every test is "identity already
    resolves" and a test that wants the broken case asks for an unknown uid
    explicitly.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        # The home field is not decoration: the agent writes VS Code's machine
        # settings into the home the *passwd record* names, so a real one here
        # would put a unit test's writes in the developer's own ~.
        self.home = path.parent / "seat-home"
        path.write_text(
            f"someone:x:{os.geteuid()}:{os.getegid()}::{self.home}:/bin/sh\n"
        )

    def _records(self) -> list[list[str]]:
        return [
            line.split(":")
            for line in self.path.read_text().splitlines()
            if line.strip()
        ]

    def name_for(self, uid: int | None = None) -> str | None:
        wanted = os.geteuid() if uid is None else uid
        for fields in self._records():
            if len(fields) > 2 and fields[2] == str(wanted):
                return fields[0]
        return None

    def uid_for(self, user: str) -> int | None:
        for fields in self._records():
            if len(fields) > 2 and fields[0] == user:
                return int(fields[2])
        return None

    def home_for(self, uid: int | None = None) -> str | None:
        wanted = os.geteuid() if uid is None else uid
        for fields in self._records():
            if len(fields) > 5 and fields[2] == str(wanted):
                return fields[5] or None
        return None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(agent, "PASSWD_PATH", str(self.path))
        monkeypatch.setattr(agent, "login_name", self.name_for)
        monkeypatch.setattr(agent, "_uid_named", self.uid_for)
        monkeypatch.setattr(agent, "_home_for_uid", self.home_for)


def make_unwritable(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Model "this file is not writable" for the test process as well.

    A mode is not enough. The suite may run as root, and root's writes ignore
    file modes entirely (CAP_DAC_OVERRIDE), so a 0444 file would report itself
    writable here and unwritable in the container this stands in for — the
    assertion would then pass or fail on who ran pytest.
    """
    path.chmod(0o444)

    def denied(_path: object, _mode: int) -> bool:
        return False

    monkeypatch.setattr(agent.os, "access", denied)


@pytest.fixture(autouse=True)
def passwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakePasswd:
    """Autouse: the agent registers itself in ``/etc/passwd``, and a unit test
    must never write to the real one."""
    fake = FakePasswd(tmp_path / "etc-passwd")
    fake.install(monkeypatch)
    return fake


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
    assert first.failures == ()
    assert len(first.changes) == 6
    assert first.changes[0] == f"prepared {layout.home}"
    before = {
        path: Path(path).read_text()
        for path in (
            layout.config_path,
            layout.host_key_path,
            layout.authorized_keys_path,
        )
    }

    # A second attach into the same container must change nothing at all.
    assert agent.ensure_all(layout, env=env, runner=runner) == agent.EnsureReport()
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


def test_a_uid_with_no_nss_entry_registers_one(
    tmp_path: Path, passwd: FakePasswd
) -> None:
    """The degraded rung's uid comes from the target, so no image can pre-bake it."""
    layout = make_layout(tmp_path, root=False)
    assert agent.ensure_passwd_entry(layout, uid=UNKNOWN_UID, gid=0) is True

    assert passwd.name_for(UNKNOWN_UID) == SEAT_USER
    entry = passwd.path.read_text().splitlines()[-1]
    assert entry == f"{SEAT_USER}:x:{UNKNOWN_UID}:0:{SEAT_USER}:{layout.home}:/bin/bash"
    # Idempotent like every other ensure step: a second attach adds nothing.
    assert agent.ensure_passwd_entry(layout, uid=UNKNOWN_UID, gid=0) is False
    assert len(passwd.path.read_text().splitlines()) == 2


def test_registration_is_skipped_with_a_reason_when_passwd_is_read_only(
    tmp_path: Path, passwd: FakePasswd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal degraded rung: uid from the target, gid from the target too.

    ``/etc/passwd`` is then unwritable, and the honest answer is to name the
    mechanism and the way out rather than to invent an identity.
    """
    make_unwritable(monkeypatch, passwd.path)
    layout = make_layout(tmp_path, root=False)
    with pytest.raises(RuntimeError) as raised:
        agent.ensure_passwd_entry(layout, uid=UNKNOWN_UID, gid=1000)

    message = str(raised.value)
    assert "not writable" in message
    assert "--seat-gid-root" in message, "the way out has to be in the message"
    assert "kubectl exec" in message
    assert passwd.name_for(UNKNOWN_UID) is None


def test_a_mounted_identity_is_used_as_it_stands_and_is_not_a_failure(
    tmp_path: Path, passwd: FakePasswd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape the identity volume produces, which is the good outcome.

    ``/etc/passwd`` is a read-only projection of a pod volume and already
    carries a record for the seat's uid. The registration step must recognise
    that as *done* rather than as an unwritable file: the one arrangement that
    makes ssh work on a PSA-restricted namespace is also the one arrangement
    where the file cannot be appended to.
    """
    with passwd.path.open("a") as handle:
        handle.write(
            f"{SEAT_USER}:x:{UNKNOWN_UID}:{UNKNOWN_UID}::/home/podbench:/bin/bash\n"
        )
    make_unwritable(monkeypatch, passwd.path)
    layout = make_layout(tmp_path, root=False)

    assert agent.ensure_passwd_entry(layout, uid=UNKNOWN_UID, gid=UNKNOWN_UID) is False
    assert len(passwd.path.read_text().splitlines()) == 2, "nothing was appended"

    check = agent.nss_identity_check(uid=UNKNOWN_UID, gid=UNKNOWN_UID)
    assert check.ok
    assert SEAT_USER in check.detail
    # …and the launcher's question gets the same answer, so a stanza is printed.
    assert agent.login_name(UNKNOWN_UID) == SEAT_USER


def test_the_way_out_is_the_one_the_container_reading_it_can_take() -> None:
    """This text ships inside the image and the launcher prints it verbatim.

    It is read from a seat whose ssh has just failed, and on a live pod that
    seat is an *ephemeral* container: it cannot be given a passwd file at all,
    because projecting one takes a ``subPath`` per mount and the API server
    forbids ``subPath`` there. So the GID 0 route has to lead, and the identity
    volume has to be named as what it is — the identity a ``podbench dev``
    sidecar gets — rather than as something to deploy in the hope it fixes this.
    """
    text = agent.NSS_WAY_OUT
    assert "--seat-gid-root" in text
    assert "kubectl exec" in text, "what still works belongs in the message"
    assert "subPath" in text, "the reason the volume cannot help has to be given"
    assert "podbench dev" in text, "and where the volume *is* used"
    # The advice this replaced: it told an ephemeral seat to mount the volume
    # over /etc/passwd, which is the one thing that container may not do.
    assert "which the seat mounts read-only" not in text
    assert text.index("--seat-gid-root") < text.index(SEAT_IDENTITY_VOLUME)


def test_registration_refuses_to_shadow_an_existing_login_name(
    tmp_path: Path, passwd: FakePasswd
) -> None:
    """sshd resolves the name the client offered, so the first record wins."""
    with passwd.path.open("a") as handle:
        handle.write(f"{SEAT_USER}:x:999:999::/nonexistent:/bin/sh\n")
    with pytest.raises(RuntimeError) as raised:
        agent.ensure_passwd_entry(make_layout(tmp_path, root=False), uid=UNKNOWN_UID)
    assert "already belongs to uid 999" in str(raised.value)


def test_an_empty_home_volume_gets_the_directories_the_layout_needs(
    tmp_path: Path,
) -> None:
    """A mounted home arrives empty: the kubelet makes the mount point, not the
    layout. Nothing else creates ``.podbench``, and sshd will not create either."""
    home = tmp_path / "home" / "podbench"
    home.mkdir(parents=True)
    layout = SshdLayout.for_uid(UNKNOWN_UID, home=str(home))

    assert agent.ensure_home_dir(layout) is True
    ssh_dir = Path(layout.authorized_keys_path).parent
    podbench_dir = Path(layout.config_path).parent
    assert ssh_dir.is_dir()
    assert podbench_dir.is_dir()
    assert ssh_dir.stat().st_mode & 0o777 == 0o700
    # Idempotent like every other ensure step: a reconnect changes nothing.
    assert agent.ensure_home_dir(layout) is False


def test_a_missing_home_directory_is_created(tmp_path: Path) -> None:
    home = tmp_path / "absent" / "podbench"
    assert (
        agent.ensure_home_dir(SshdLayout.for_uid(UNKNOWN_UID, home=str(home))) is True
    )
    assert home.is_dir()


def test_an_unwritable_home_names_fsgroup_as_the_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The volume is there and the seat cannot write to it — say why.

    An emptyDir or a PVC is root:root until the pod's ``fsGroup`` hands it to
    the seat's group, and a seat running as the target's uid can chown nothing.
    From inside the container that is indistinguishable from a podbench bug
    unless the message names the mechanism.
    """
    home = tmp_path / "home"
    home.mkdir()
    make_unwritable(monkeypatch, home)

    with pytest.raises(RuntimeError) as raised:
        agent.ensure_home_dir(SshdLayout.for_uid(UNKNOWN_UID, home=str(home)))
    message = str(raised.value)
    assert "not writable" in message
    assert "fsGroup" in message
    assert "kubectl exec" in message, "what still works belongs in the message"


def test_an_unwritable_home_is_recorded_rather_than_raised(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """PID 1 of an unrestartable container lands anyway (report 4.2)."""
    home = tmp_path / "home"
    home.mkdir()
    layout = SshdLayout.for_uid(UNKNOWN_UID, home=str(home))
    make_unwritable(monkeypatch, home)

    report = agent.ensure_all(layout, env=env, runner=FakeRunner())
    failure = next(f for f in report.failures if f.name == "ensure-home-dir")
    assert "fsGroup" in failure.detail


def test_a_failed_ensure_step_is_recorded_rather_than_raised(
    tmp_path: Path, env: dict[str, str]
) -> None:
    """The crash this whole change exists to stop.

    ``ssh-keygen`` dying ("No user exists for uid 1000") took the agent — PID 1
    of a container that cannot be restarted — down with it, burning the
    container's name for the pod's lifetime and losing the exec-reachable half
    of the seat as well as the ssh half.
    """

    class BrokenKeygen(FakeRunner):
        def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
            args = list(argv)
            if args[0] == "ssh-keygen":
                self.calls.append(args)
                return subprocess.CompletedProcess(
                    args, 1, "", "No user exists for uid 1000"
                )
            return super().__call__(args)

    layout = make_layout(tmp_path)
    runner = BrokenKeygen()
    report = agent.ensure_all(layout, env=env, runner=runner)

    assert [failure.name for failure in report.failures] == ["ensure-host-key"]
    assert "No user exists for uid 1000" in report.failures[0].detail
    # The steps after the failed one still ran: the transport is the only thing
    # a missing host key costs.
    assert any("authorized_keys" in change for change in report.changes)
    assert Path(layout.config_path).is_file()

    # …and the failure is what self_check reports, so `--self-check` over
    # kubectl exec can say why there is no host key.
    named = {
        check.name: check
        for check in agent.self_check(layout, runner=FakeRunner(), ensure=report)
    }
    assert not named["ensure-host-key"].ok
    assert "No user exists" in named["ensure-host-key"].detail


def test_the_agent_idles_instead_of_dying_when_a_check_fails(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrestartable PID 1 stays up: exiting burns the name (report 4.2)."""

    def broken(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            list(argv), 1, "", "No user exists for uid 1000"
        )

    layout = make_layout(tmp_path)
    patch_layout(monkeypatch, layout)
    monkeypatch.setattr(agent, "run_command", broken)
    monkeypatch.setattr(agent, "time", FakeClock(signal_after=1))
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    # Reached the idle loop and left it on SIGTERM, rather than exiting 1 at
    # the check.
    assert agent.main([]) == 0


def test_self_check_names_a_missing_nss_identity_and_the_way_out(
    passwd: FakePasswd, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_unwritable(monkeypatch, passwd.path)
    check = agent.nss_identity_check(uid=UNKNOWN_UID, gid=1000)
    assert not check.ok
    assert check.name == "nss-identity"
    assert "no NSS entry" in check.detail
    assert "--seat-gid-root" in check.detail


def test_print_login_user_reports_the_name_or_the_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    passwd: FakePasswd,
) -> None:
    """The launcher's one question about the seat's ssh identity.

    stdout carries the name and nothing else, because the launcher parses it;
    the reason goes to stderr, where a mechanism-and-way-out sentence belongs.
    """
    layout = make_layout(tmp_path, root=False)
    patch_layout(monkeypatch, layout)
    monkeypatch.setattr(agent, "run_command", FakeRunner())

    assert agent.main(["--print-login-user"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == passwd.name_for()
    # Nothing was ensured on the way: this is a read of the state sshd will find.
    assert not Path(layout.config_path).exists()

    def unresolvable(uid: int | None = None) -> str | None:
        return None

    monkeypatch.setattr(agent, "login_name", unresolvable)
    make_unwritable(monkeypatch, passwd.path)
    assert agent.main(["--print-login-user"]) == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    assert "--seat-gid-root" in captured.err


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


def test_session_env_forwards_podbench_variables_only() -> None:
    forwarded = agent.session_env(
        {"PODBENCH_TARGET_CID": "abc", "PATH": "/usr/bin", "HOME": "/root"}
    )
    assert forwarded == {"PODBENCH_TARGET_CID": "abc"}


def test_the_ssh_public_key_is_never_forwarded_into_a_session() -> None:
    """It is already installed in authorized_keys; a session has no use for it."""
    forwarded = agent.session_env(
        {agent.PUBKEY_ENV: "ssh-ed25519 AAAA", "PODBENCH_NODE_NAME": "nuc2"}
    )
    assert forwarded == {"PODBENCH_NODE_NAME": "nuc2"}


# --- VS Code machine settings ----------------------------------------------
#
# The seat prepares these before any client connects, because the folder that
# kills it is the first one the user opens and an OOM-killed ephemeral container
# cannot be restarted.


def test_machine_settings_land_in_the_home_the_passwd_record_names(
    tmp_path: Path, passwd: FakePasswd, env: dict[str, str]
) -> None:
    """Not ``layout.home``: sshd puts a session in the home NSS gives it, so a
    ``podbench dev`` sidecar — whose ``$HOME`` is pinned to the workspace volume
    — would otherwise get the settings written where nothing ever looks."""
    layout = make_layout(tmp_path / "workspace", root=False)
    (tmp_path / "workspace").mkdir()

    report = agent.ensure_all(layout, env=env, runner=FakeRunner())

    assert report.failures == ()
    assert not (Path(layout.home) / MACHINE_SETTINGS_PATH).exists()
    settings = passwd.home / MACHINE_SETTINGS_PATH
    assert json.loads(settings.read_text())["search.exclude"]["**/proc/**"] is True


def test_machine_settings_survive_a_re_attach(
    tmp_path: Path, passwd: FakePasswd, env: dict[str, str]
) -> None:
    """The home is where a ``podbench-home`` volume is mounted, so "present on
    the second attach" is what persistence across re-attaches means here."""
    layout = make_layout(tmp_path)
    agent.ensure_all(layout, env=env, runner=FakeRunner())
    settings = passwd.home / MACHINE_SETTINGS_PATH
    written = settings.read_text()

    second = agent.ensure_all(layout, env=env, runner=FakeRunner())

    assert second == agent.EnsureReport()
    assert settings.read_text() == written


def test_a_users_own_settings_are_merged_into_rather_than_replaced(
    tmp_path: Path, passwd: FakePasswd, env: dict[str, str]
) -> None:
    settings = passwd.home / MACHINE_SETTINGS_PATH
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"editor.fontSize": 15, "search.exclude": {}}))

    agent.ensure_all(make_layout(tmp_path), env=env, runner=FakeRunner())

    document = json.loads(settings.read_text())
    assert document["editor.fontSize"] == 15
    assert document["search.exclude"]["**/proc/**"] is True


def test_settings_that_cannot_be_parsed_are_left_alone_and_reported(
    tmp_path: Path, passwd: FakePasswd, env: dict[str, str]
) -> None:
    """VS Code allows comments in settings.json and :mod:`json` does not.

    Rewriting would discard them, and staying quiet would leave the seat one
    File -> Open Folder away from an unrecoverable OOM — so the file is
    untouched and the reason is a recorded failure.
    """
    settings = passwd.home / MACHINE_SETTINGS_PATH
    settings.parent.mkdir(parents=True)
    settings.write_text("{ // mine\n}")

    report = agent.ensure_all(make_layout(tmp_path), env=env, runner=FakeRunner())

    assert settings.read_text() == "{ // mine\n}"
    failure = {check.name: check for check in report.failures}["ensure-vscode-settings"]
    assert "cannot parse" in failure.detail
    assert "OOM-killed" in failure.detail
    # …and self_check is where a `--self-check` over kubectl exec finds it.
    checks = agent.self_check(make_layout(tmp_path), runner=FakeRunner(), ensure=report)
    assert not {check.name: check for check in checks}["ensure-vscode-settings"].ok


def test_the_settings_home_falls_back_when_nss_resolves_nothing(
    tmp_path: Path, passwd: FakePasswd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The degraded rung has no passwd record at all. ssh cannot work there
    either, but the step must not be the one that raises about it."""

    def no_record(uid: int | None = None) -> str | None:
        return None

    monkeypatch.setattr(agent, "_home_for_uid", no_record)
    layout = make_layout(tmp_path)
    assert agent.session_home(layout) == layout.home
