"""Tests for the in-pod agent.

Idempotency is the property under test almost everywhere: an ephemeral
container cannot be restarted, so re-running the agent against a container that
is already serving a session is the normal reconnection path.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from podbench import agent
from podbench.model import (
    CLAIM_PATH_ENV,
    HOTFIX_APP_PATH,
    HOTFIX_INTERPRETER_PATH,
    SEAT_IDENTITY_VOLUME,
    SEAT_REPORT_MARKER,
    TARGET_CID_ENV,
    Rung,
    SeatReport,
)
from podbench.provision import CLAIM_DEST_NAME
from podbench.sshcfg import SEAT_USER, SshdLayout, sshd_config, unsafe_set_env
from podbench.vscode import MACHINE_SETTINGS_PATH

TARGET_CID = "abc123def456"
"""The container id the synthetic tree attributes the target's processes to."""

PUBKEY = "ssh-ed25519 AAAAC3NzaC1FIRST dev@laptop"
SECOND_PUBKEY = "ssh-ed25519 AAAAC3NzaC1SECOND colleague@laptop"

UNKNOWN_UID = 4242
"""A uid the fake NSS database has no record of — the shape of the degraded
rung, where the seat runs as a target uid no image could have an account for."""

IMAGE_GITIGNORE = "/etc/podbench/gitignore"
"""The podbench-owned ignore file ``core.excludesFile`` names. Nothing in the
package reads it — it is the image's own, and the whole point is that podbench
writes no ignore file inside the user's repository — so this literal exists only
to hold the Dockerfile's two halves against each other."""

REAL_SEAT_GITCONFIG_INCLUDE = agent.SEAT_GITCONFIG_INCLUDE
"""The path the *image*'s baked ``/etc/gitconfig`` includes, captured before
:func:`seat_gitconfig_include` repoints the constant at a temporary file. The
two halves are useless apart, so the Dockerfile is checked against this literal
rather than against the patched value."""

REAL_SEAT_NSS_PATH = agent.SEAT_NSS_PATH
"""The database the *image* ships, captured before :class:`FakePasswd` repoints
the constant at a temporary file. :data:`podbench.agent.NSS_WAY_OUT` is built at
import time and quotes this literal, so a test about that text has to compare
against the real value rather than the patched one."""

DIAMOND_UID = 36070
"""The uid *and gid* of ``bl01c-di-dcam-04-0``, the workload issue #102 was
found on. Its gid is what mattered: it is not 0, so the seat that carries it
cannot write ``/etc/passwd``, and it is the target's, so it cannot be swapped
for 0 without breaking the credential match ptrace makes."""


class FakePasswd:
    """A stand-in for the NSS passwd sources: both files *and* ``getpwuid``.

    Every half is faked because a test that asked the real platform would
    pass or fail on whichever uid the suite happens to run as, and the bug under
    test is exactly "this uid has no entry". Seeded with a record for the
    running uid, so the default state of every test is "identity already
    resolves" and a test that wants the broken case asks for an unknown uid
    explicitly.

    Two files, because the image consults two: ``nsswitch.conf``'s ``passwd``
    line is ``files extrausers``, so ``getpwuid`` answers from ``/etc/passwd``
    and then from :data:`podbench.agent.SEAT_NSS_PATH`, and only the second is
    writable by a seat whose gid is not 0 (issue #102). ``extrausers=False``
    models an image built before that database existed, where the fallback puts
    the record back in ``/etc/passwd``; both paths are patched, so nothing here
    depends on whether the machine running pytest happens to have
    ``/var/lib/extrausers/passwd``.

    The second file is **not** a second ``files``, and modelling it as one is
    what let the first version of this change ship a database that took records
    it would never serve: libnss-extrausers has compiled-in floors and drops any
    record whose uid or gid is below them, silently and for every lookup. So
    :meth:`_records` applies them to ``nss_path`` and not to ``path``, which is
    the difference the fake exists to hold.
    """

    def __init__(
        self, directory: Path, *, extrausers: bool = True, seeded: bool = True
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "etc-passwd"
        self.nss_path = directory / "extrausers-passwd"
        # The home field is not decoration: the agent writes VS Code's machine
        # settings into the home the *passwd record* names, so a real one here
        # would put a unit test's writes in the developer's own ~.
        self.home = directory / "seat-home"
        self.path.write_text(
            f"someone:x:{os.geteuid()}:{os.getegid()}::{self.home}:/bin/sh\n"
            if seeded
            else ""
        )
        if extrausers:
            self.enable_extrausers()

    def enable_extrausers(self) -> None:
        """Create the database the image ships, at the mode the image gives it.

        0666 is the point of it: the seat that appends its own record runs as the
        target's uid *and* gid, so any mode that asked for an owner or a group
        would be the ``/etc/passwd`` problem again.
        """
        self.nss_path.write_text("")
        self.nss_path.chmod(0o666)

    @staticmethod
    def _parse(path: Path) -> list[list[str]]:
        if not path.is_file():
            return []
        return [
            line.split(":") for line in path.read_text().splitlines() if line.strip()
        ]

    def _records(self) -> list[list[str]]:
        # In nsswitch order: files first, then extrausers, so a name present in
        # both resolves the way the image's getpwnam would resolve it.
        served: list[list[str]] = []
        for fields in self._parse(self.nss_path):
            if len(fields) > 3 and agent.extrausers_serves(
                int(fields[2]), int(fields[3])
            ):
                served.append(fields)
        return self._parse(self.path) + served

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
        # Both, or the suite's answer would depend on the machine: registration
        # prefers SEAT_NSS_PATH whenever that file exists and is writable, and in
        # the podbench image itself it does.
        monkeypatch.setattr(agent, "SEAT_NSS_PATH", str(self.nss_path))
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


def deny_writes_to(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Model "this one file is not writable", leaving the others alone.

    :func:`make_unwritable` denies every ``os.access`` call, which cannot express
    the shape issue #102 is about: a seat carrying the target's gid can write
    ``extrausers`` and *not* ``/etc/passwd``, and a test that denied both would
    pass whichever file the agent chose. The mode is set for the same reason
    :func:`make_unwritable` does not rely on it — the suite may run as root,
    whose writes ignore modes (CAP_DAC_OVERRIDE) — so the real ``os.access``
    still answers for every path but this one.
    """
    if path.exists():
        path.chmod(0o444)
    real_access = os.access
    denied = str(path)

    def access(path: str | os.PathLike[str], mode: int) -> bool:
        return False if str(path) == denied else real_access(path, mode)

    monkeypatch.setattr(agent.os, "access", access)


@pytest.fixture(autouse=True)
def passwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakePasswd:
    """Autouse: the agent registers itself in a passwd database, and a unit test
    must never write to one of the machine's own."""
    fake = FakePasswd(tmp_path)
    fake.install(monkeypatch)
    return fake


@pytest.fixture(autouse=True)
def seat_gitconfig_include(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Autouse for the same reason :func:`passwd` is: the agent writes this file
    at a fixed absolute path in the container, and a unit test must not write to
    the machine's own ``/tmp``."""
    path = tmp_path / "podbench-gitconfig"
    monkeypatch.setattr(agent, "SEAT_GITCONFIG_INCLUDE", str(path))
    return path


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
    # home-dir, privsep-dir, nss-db-mode, host-key, authorized-keys,
    # sshd-config, vscode-settings. nss-identity is not among them: the fake
    # database already has a record for the uid the suite runs as.
    assert len(first.changes) == 7
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
    assert Path(layout.config_path).read_text() == sshd_config(
        layout, agent.session_env(env)
    )


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
    gid = UNKNOWN_UID
    assert agent.ensure_passwd_entry(layout, uid=UNKNOWN_UID, gid=gid) is True

    assert passwd.name_for(UNKNOWN_UID) == SEAT_USER
    entry = passwd.nss_path.read_text().splitlines()[-1]
    expected = f"{SEAT_USER}:x:{UNKNOWN_UID}:{gid}:{SEAT_USER}:{layout.home}:/bin/bash"
    assert entry == expected
    # Idempotent like every other ensure step: a second attach adds nothing.
    assert agent.ensure_passwd_entry(layout, uid=UNKNOWN_UID, gid=gid) is False
    assert len(passwd.nss_path.read_text().splitlines()) == 1


def test_a_seat_the_extrausers_floors_reject_takes_etc_passwd_instead(
    tmp_path: Path, passwd: FakePasswd
) -> None:
    """The blocker a prototype of this change shipped, as a test.

    libnss-extrausers has floors compiled into it — ``MINUID 500``,
    ``MINGID 500``, gid 100 exempted — and ignores a record below them for
    ``getpwnam`` as well as ``getpwuid``. A seat picked by the file's mode alone
    would write ``podbench:x:1000:0:…`` into a database that will never answer
    for it, get "NSS still does not resolve" and land with no ssh, where before
    #102 the same seat appended to ``/etc/passwd`` and logged in.

    gid 0 is the case that matters, not a curiosity. It is what a seat carries
    when nobody supplied a group — before #103, every target that set
    ``runAsUser`` and no ``runAsGroup``; since it, a target whose own group
    really is 0. Either way ``/etc/passwd`` has no floor and gid 0 can write it,
    which is what this asserts.

    The uid is :data:`UNKNOWN_UID` and not the 1000 the prose names, because
    :class:`FakePasswd` seeds a record for the *running* euid: hard-coding the
    uid that Ubuntu gives its first user makes this test assert "already
    resolves" — the early ``return False`` — on any developer machine, while
    still passing as root in the devcontainer and in CI. Any uid over the
    ``MINUID 500`` floor carries the same case.
    """
    layout = make_layout(tmp_path, root=False)

    assert agent.ensure_passwd_entry(layout, uid=UNKNOWN_UID, gid=0) is True

    assert (
        passwd.path.read_text()
        .splitlines()[-1]
        .startswith(f"{SEAT_USER}:x:{UNKNOWN_UID}:0:")
    )
    assert passwd.nss_path.read_text() == "", "the floors leave the database alone"
    assert passwd.name_for(UNKNOWN_UID) == SEAT_USER

    # A uid under the floor is rejected too, whatever its gid, because the
    # lookup short-circuits before the file is opened.
    assert agent.extrausers_serves(472, 472) is False
    assert agent.extrausers_serves(1000, 100) is True, "gid 100 is `users`"


def test_a_seat_carrying_the_targets_gid_registers_its_own_record(
    tmp_path: Path, passwd: FakePasswd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #102, as a test, and the reason this mechanism exists.

    ``bl01c-di-dcam-04-0`` at Diamond runs as uid 36070 / gid 36070, and the
    degraded rung carries *both* — the gid deliberately, because ptrace compares
    it as well as the uid. ``/etc/passwd`` in the image is writable by GID 0 and
    nothing else, so the append this seat needs went to a file it had no write
    permission on, ``ssh-keygen`` died with "No user exists for uid 36070", and
    the seat landed with no ssh at all. The documented way out then was to pin
    ``runAsGroup: 0``, which pays for ssh with the debugger and was retired in
    #103 for saying so too quietly.

    So ``/etc/passwd`` is denied here on purpose: this test fails the moment
    registration goes back to a file only GID 0 can write.
    """
    deny_writes_to(monkeypatch, passwd.path)
    layout = make_layout(tmp_path, root=False)

    assert agent.ensure_passwd_entry(layout, uid=DIAMOND_UID, gid=DIAMOND_UID) is True

    assert passwd.name_for(DIAMOND_UID) == SEAT_USER
    entry = passwd.nss_path.read_text().splitlines()[-1]
    assert entry.split(":")[3] == str(DIAMOND_UID), "the target's gid is kept"
    # …and the check reached over `kubectl exec` seconds later agrees, because it
    # re-derives the answer from NSS rather than remembering this call.
    check = agent.nss_identity_check(uid=DIAMOND_UID, gid=DIAMOND_UID)
    assert check.ok
    assert SEAT_USER in check.detail


def test_the_record_goes_to_extrausers_and_etc_passwd_is_left_as_it_is(
    tmp_path: Path, passwd: FakePasswd
) -> None:
    """The file the seat writes is not the file sshd's ``files`` source reads.

    ``/etc/passwd`` staying byte-for-byte as the image built it is half the
    argument that this is not an escalation: the seat adds a record to a database
    of its own, and the accounts the image ships keep their modes and their
    contents. It is also what keeps a ``podbench dev`` sidecar working, which
    gets its identity *projected* over ``/etc/passwd`` and would find a
    self-registered record there shadowed by the projection.
    """
    before = passwd.path.read_bytes()
    layout = make_layout(tmp_path, root=False)

    assert agent.ensure_passwd_entry(layout, uid=UNKNOWN_UID, gid=UNKNOWN_UID) is True

    assert passwd.nss_path.read_text() == agent.passwd_line(
        uid=UNKNOWN_UID, gid=UNKNOWN_UID, home=layout.home
    )
    assert passwd.path.read_bytes() == before, "/etc/passwd was not touched"


def test_a_reconnect_leaves_the_record_a_previous_attach_wrote(
    tmp_path: Path, passwd: FakePasswd
) -> None:
    """An ephemeral container cannot be restarted, so re-running the agent is
    the normal reconnection path — and the seat's database is a file that only
    ever grows.

    The record here was written by an earlier attach rather than by this test's
    first call, which is what makes the early return the thing under test: NSS
    resolves the uid from ``extrausers``, so registration answers "nothing to
    do" without reading the file at all.
    """
    with passwd.nss_path.open("a") as handle:
        handle.write(agent.passwd_line(uid=UNKNOWN_UID, gid=UNKNOWN_UID, home="/tmp"))
    before = passwd.nss_path.read_bytes()
    layout = make_layout(tmp_path, root=False)

    assert agent.ensure_passwd_entry(layout, uid=UNKNOWN_UID, gid=UNKNOWN_UID) is False
    assert passwd.nss_path.read_bytes() == before, "a second attach appends nothing"


def test_registration_falls_back_to_etc_passwd_without_an_extrausers_database(
    tmp_path: Path, passwd: FakePasswd
) -> None:
    """An image built before #102, where the GID 0 route is the only one there is.

    The launcher will happily attach a seat from an older tag, and the honest
    thing for that seat to do is take the route its image has rather than refuse
    over the absence of a file the reader has never heard of. So the append goes
    back to ``/etc/passwd``.

    The gid here clears ``extrausers``' floors on purpose, so that the file's
    absence is the only reason the fallback was taken — with gid 0 the test would
    have passed whether the absence was noticed or not.
    """
    passwd.nss_path.unlink()
    layout = make_layout(tmp_path, root=False)

    assert agent.ensure_passwd_entry(layout, uid=UNKNOWN_UID, gid=UNKNOWN_UID) is True

    assert passwd.path.read_text().splitlines()[-1].startswith(f"{SEAT_USER}:x:")
    assert not passwd.nss_path.exists(), "the fallback creates no database"


def test_an_unwritable_extrausers_database_does_not_mask_the_etc_passwd_route(
    tmp_path: Path, passwd: FakePasswd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existence is not enough: the file has to be writable to be chosen.

    An image that created the database 0644 — or one where a read-only mount
    landed on it — would otherwise turn a seat that could still have written
    ``/etc/passwd`` into a refusal, and the refusal would name a file whose mode
    the user cannot change from inside the seat.

    The credentials here clear ``extrausers``' floors, so the mode is the only
    thing that can have sent the append elsewhere.
    """
    deny_writes_to(monkeypatch, passwd.nss_path)
    layout = make_layout(tmp_path, root=False)

    assert agent.ensure_passwd_entry(layout, uid=UNKNOWN_UID, gid=UNKNOWN_UID) is True

    assert passwd.path.read_text().splitlines()[-1].startswith(f"{SEAT_USER}:x:")
    assert passwd.nss_path.read_text() == "", "the unwritable database is left alone"


def test_registration_is_skipped_with_a_reason_when_neither_database_is_writable(
    tmp_path: Path, passwd: FakePasswd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seat that still has no way out: an old image *and* a non-zero gid.

    With ``extrausers`` unwritable the append target is ``/etc/passwd``, and with
    the target's gid the seat cannot write that either. The honest answer is to
    name the mechanism and the way out rather than to invent an identity — and to
    name the file that was *actually* tried, since a reader asked to check a
    mode has to be told which mode.
    """
    make_unwritable(monkeypatch, passwd.path)
    layout = make_layout(tmp_path, root=False)
    with pytest.raises(RuntimeError) as raised:
        agent.ensure_passwd_entry(layout, uid=UNKNOWN_UID, gid=1000)

    message = str(raised.value)
    assert "not writable" in message
    assert f"{passwd.path} is not writable" in message, "the path actually tried"
    assert str(passwd.nss_path) not in message, "not the one it fell back from"
    assert "group of 500 or more" in message, "the way out has to be in it"
    assert "--seat-gid-root" not in message, "that flag retired with #103"
    assert "kubectl exec" in message
    assert passwd.name_for(UNKNOWN_UID) is None


def test_a_database_nsswitch_does_not_consult_says_so_and_names_it(
    tmp_path: Path, passwd: FakePasswd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure a replaced ``nsswitch.conf`` produces, which looks like nothing.

    The database is there and writable, the append succeeds, and ``getpwuid``
    still answers nothing — the ``passwd`` line does not name ``extrausers``,
    because an image was hand-built or a volume landed on ``/etc``. Both attempts
    have to name the file they wrote, since the file's *presence* and the missing
    nsswitch line are the diagnosis together and either alone reads as a podbench
    bug. And the second attempt must refuse rather than append again: an
    ephemeral container is re-entered, not restarted, so a database that grows a
    copy per reconnect is how this ends up unreadable.
    """
    layout = make_layout(tmp_path, root=False)

    # NSS is blind to the file in *both* directions, which is what "nsswitch does
    # not consult it" means: neither getpwuid nor the getpwnam that guards against
    # shadowing a name can see what is in there.
    def unresolvable(uid: int | None = None) -> str | None:
        return None

    def unnamed(_user: str) -> int | None:
        return None

    monkeypatch.setattr(agent, "login_name", unresolvable)
    monkeypatch.setattr(agent, "_uid_named", unnamed)

    with pytest.raises(RuntimeError) as first:
        agent.ensure_passwd_entry(layout, uid=UNKNOWN_UID, gid=UNKNOWN_UID)
    assert f"to {passwd.nss_path} and NSS still does not resolve" in str(first.value)

    with pytest.raises(RuntimeError) as second:
        agent.ensure_passwd_entry(layout, uid=UNKNOWN_UID, gid=UNKNOWN_UID)
    message = str(second.value)
    assert f"does not appear to answer passwd lookups from {passwd.nss_path}" in message
    # And it does not pin the blame on nsswitch.conf, which is one of two
    # possible causes and was the wrong one for a seat under the extrausers
    # floors: that reader was sent to inspect a line that was correct.
    assert "nsswitch.conf does not" not in message
    assert len(passwd.nss_path.read_text().splitlines()) == 1, "no second copy"


def test_the_check_on_a_writable_database_blames_the_step_not_the_gid(
    passwd: FakePasswd,
) -> None:
    """What ``--self-check`` reports for the #102 seat once the file is writable.

    Before this, a uid with no entry and a non-zero gid meant "``/etc/passwd`` is
    not writable by uid 36070 / gid 36070" and a flag to re-attach with. Now the
    database *is* writable at that gid, so an unresolved uid can only be the
    registration step having failed for some other reason: the check says so and
    sends the reader to the start-up output, because telling them to re-attach
    into group 0 would cost them the debugger for nothing.

    Which makes this the branch the launcher relays most often, and it is the one
    branch that cannot state the reason — that was raised inside the seat minutes
    earlier. So it has to name the command that shows it. Nothing else in the
    launcher relays the agent's stderr, and ``--print-login-user`` is documented
    as giving the way out, so "look in the start-up output" on its own is a
    round trip spent working out how.
    """
    check = agent.nss_identity_check(uid=DIAMOND_UID, gid=DIAMOND_UID)

    assert not check.ok
    assert check.name == "nss-identity"
    assert f"{passwd.nss_path} is writable" in check.detail
    assert "kubectl logs" in check.detail, "the reason is in a log this names"
    assert "--seat-gid-root" not in check.detail


def test_the_start_up_step_names_the_database_the_record_went_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    """The one line the user sees about registration, on the seat's stdout.

    ``ensure_all`` reports what it changed, and this step's line has to name the
    file the record went to rather than a hardcoded one: the two routes fail in
    different places, and a start-up log naming ``/etc/passwd`` for a record that
    landed in ``extrausers`` would send the next reader to check the mode of a
    file that had nothing to do with it.

    ``ensure_all`` takes the uid and gid from the platform, so they are faked
    here rather than inherited from whoever ran pytest: the append target now
    depends on them (``extrausers`` ignores records under uid or gid 500), and a
    suite running as root would otherwise assert the fallback and a suite running
    as 1000:1000 the database, on the same code.
    """
    monkeypatch.setattr(agent.os, "geteuid", lambda: DIAMOND_UID)
    monkeypatch.setattr(agent.os, "getegid", lambda: DIAMOND_UID)
    # Unseeded, or the running uid resolves already and the step is a no-op.
    image = FakePasswd(tmp_path / "image", seeded=False)
    image.install(monkeypatch)
    layout = make_layout(tmp_path, root=False)

    report = agent.ensure_all(layout, env=env, runner=FakeRunner())

    assert report.ok
    registered = [change for change in report.changes if "registered uid" in change]
    assert registered == [
        f"registered uid {DIAMOND_UID} as {SEAT_USER!r} in {image.nss_path}"
    ]


def test_a_root_seat_takes_the_world_writable_bit_off_its_own_database(
    tmp_path: Path, passwd: FakePasswd
) -> None:
    """Mode 0666 is safe for two different reasons, and one of them is enforced.

    On the non-root rungs sshd runs as the seat's own uid, so a forged record
    buys its author the uid it already had. On the ``full`` rung sshd *is* root
    (``for_uid(0)`` returns ``run_as_root=True``) and the reason is different: a
    root seat has no unprivileged principal to forge with, since every process in
    it — the ``kubectl exec`` carrying ssh included — is already uid 0. That is a
    property of the rung rather than something enforced, and the security page
    used to describe a root sshd as unshipped work while ``Rung.FULL`` was
    shipping one, so the agent closes the bit instead of resting on the argument.

    A root seat has no use for the write bit: ``getpwuid(0)`` resolves from the
    image's own ``/etc/passwd`` and registration returns early.
    """
    root = make_layout(tmp_path / "root")
    assert agent.restrict_seat_nss_database(root) is True
    assert passwd.nss_path.stat().st_mode & 0o777 == 0o644
    # Idempotent: a reconnect re-runs every step against a container it cannot
    # restart, and a second pass has nothing left to narrow.
    assert agent.restrict_seat_nss_database(root) is False

    # And a degraded seat keeps 0666, which is the mode it needs: it appends as
    # the target's uid *and* gid, so an owner or a group would be no better than
    # the /etc/passwd this route exists to avoid.
    passwd.enable_extrausers()
    assert agent.restrict_seat_nss_database(make_layout(tmp_path, root=False)) is False
    assert passwd.nss_path.stat().st_mode & 0o777 == 0o666


def test_a_root_seat_on_an_image_without_the_database_is_not_a_failure(
    tmp_path: Path, passwd: FakePasswd
) -> None:
    """Nothing to narrow is success, not a step that could not do its job.

    An image built before #102 has no such file, and a root seat there is exactly
    as safe as one on a current image. Recording a failure would put a line under
    a seat whose ssh is working and whose database does not exist.
    """
    passwd.nss_path.unlink()
    assert agent.restrict_seat_nss_database(make_layout(tmp_path / "root")) is False


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
    # Not into the seat's own database either: a projected record is the identity
    # already, and a second one for the same name would be the record sshd never
    # reaches (files is consulted first).
    assert passwd.nss_path.read_text() == ""

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
    forbids ``subPath`` there. So the routes are ordered by what they cost the
    reader — the seat's own record first, since it costs nothing and is what the
    current image does — and the identity volume has to be named as what it is,
    the identity a ``podbench dev`` sidecar gets, rather than as something to
    deploy in the hope it fixes this.
    """
    text = agent.NSS_WAY_OUT
    assert "kubectl exec" in text, "what still works belongs in the message"
    assert "subPath" in text, "the reason the volume cannot help has to be given"
    assert "podbench dev" in text, "and where the volume *is* used"
    # The advice this replaced: it told an ephemeral seat to mount the volume
    # over /etc/passwd, which is the one thing that container may not do.
    assert "which the seat mounts read-only" not in text
    assert REAL_SEAT_NSS_PATH in text
    assert text.index(REAL_SEAT_NSS_PATH) < text.index(SEAT_IDENTITY_VOLUME)
    # Issue #102 sent one seat round this loop twice, because gid 0 was offered
    # as *the* way out with none of what it costs. #103 removed the flag
    # entirely: __ptrace_may_access() compares the group ids as peers of the
    # user ids, so the seat that took it could log in and not trace. A reader
    # who cannot get a login will pay any price for one, so this text must not
    # grow it back - not even as a footnote.
    assert "--seat-gid-root" not in text
    assert "runAsGroup: 0" not in text


def test_the_image_provides_the_database_this_module_appends_to() -> None:
    """The one half of issue #102 that no other test in this file can see.

    Every test above patches :data:`podbench.agent.SEAT_NSS_PATH` at a temporary
    file, because a unit test must not write to the machine's own — which means
    the suite stays green if the constant and the image drift apart. The failure
    that drift produces is the #102 bug exactly: the database is not there,
    :func:`podbench.agent._nss_append_target` falls back to ``/etc/passwd``, the
    seat carrying the target's gid cannot write it, and ssh is gone. No unit test
    would notice and the e2e suite needs a cluster, so the contract is checked
    against the Dockerfile as text.

    Three things have to hold, and the third is a trap a prototype fell into: the
    package has to be installed, ``nsswitch.conf``'s ``passwd`` line has to name
    ``extrausers`` (a database NSS does not consult is a file the agent writes and
    nothing reads), and ``chmod g=u /etc/passwd`` has to *stay* — a ``podbench
    dev`` sidecar's projection rests on it, and so does the one seat whose group
    genuinely is 0, so this change adds a route and removes none.
    """
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()

    assert "libnss-extrausers" in dockerfile, "the NSS source has to be installed"
    assert REAL_SEAT_NSS_PATH in dockerfile, "and the image has to create this path"
    # `files` first, and not only because NSS needs some order: a `podbench dev`
    # sidecar's projected /etc/passwd has to keep winning over anything a seat
    # appended to its own database under the same name.
    assert re.search(r"passwd:\s+files\s+extrausers", dockerfile) is not None, (
        "nsswitch's passwd line must consult extrausers, after files"
    )
    assert "chmod g=u /etc/passwd" in dockerfile, "the GID 0 fallback is not retired"


def test_the_image_pre_seeds_the_uids_the_database_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passwd: FakePasswd
) -> None:
    """The sub-500 range, which had no route at all before #103.

    ``libnss-extrausers`` ignores a record whose uid is below 500, and a seat at
    such a uid is almost always in a group below 500 too (grafana 472:472,
    nginx-unprivileged 101:101), so it cannot write ``/etc/passwd`` either. It
    landed with no ssh whatever podbench did. A record baked at build time needs
    no writable surface at all, and the range is small enough to enumerate -
    which the range above 500 is not, and is what the database is for.

    Checked against the Dockerfile as text for the same reason the extrausers
    contract above is: every unit test here points the paths at a temporary file,
    so nothing else in this suite can see the image drift away from the code.
    And checked against the *reason* as well - the seats this serves are exactly
    the ones :func:`agent.extrausers_serves` refuses.
    """
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()

    assert "seq 1 499" in dockerfile, "the range extrausers refuses"
    assert "getent passwd" in dockerfile, (
        "a uid the distribution already uses must be skipped, not shadowed"
    )
    assert ">> /etc/passwd" in dockerfile, "and the records go in the files database"
    assert agent.extrausers_serves(472, 472) is False, "which is why they are needed"

    # And once such a record exists, registration does nothing at all: no file
    # is opened, no mode is consulted, and the way-out text is never reached.
    make_unwritable(monkeypatch, passwd.path)
    layout = make_layout(tmp_path, root=False)

    def resolves(uid: int | None = None) -> str | None:
        return "podbench-472"

    monkeypatch.setattr(agent, "login_name", resolves)
    assert agent.ensure_passwd_entry(layout, uid=472, gid=472) is False


@pytest.mark.parametrize("verb", ["capreport", "pids", "dbg"])
def test_the_way_out_names_no_command_the_image_stopped_shipping(verb: str) -> None:
    """Every verb named in a diagnostic is spelled ``podbench <verb>``.

    Since #47 the image ships two files in ``/usr/local/bin``: ``podbench`` and
    ``gdb-podbench``. A bare ``capreport`` in this text is not a shorter
    spelling — it is ``exec: "capreport": executable file not found``, produced
    by pasting the way out of a seat whose ssh has already failed.
    """
    for text in (agent.EXEC_HALF_COMMANDS, agent.NSS_WAY_OUT):
        for index in _occurrences(text, verb):
            assert text[:index].endswith("podbench "), (
                f"{verb!r} at offset {index} is not spelled as a podbench verb: "
                f"{text[max(0, index - 40) : index + 20]!r}"
            )


def _occurrences(text: str, word: str) -> list[int]:
    """Offsets at which ``word`` appears as a whole word."""
    return [
        match.start() for match in re.finditer(rf"\b{re.escape(word)}\b(?!-)", text)
    ]


def test_registration_refuses_to_shadow_an_existing_login_name(
    tmp_path: Path, passwd: FakePasswd
) -> None:
    """sshd resolves the name the client offered, so the first record wins.

    The colliding record is in ``/etc/passwd`` and the append target is the
    ``extrausers`` database, which is the realistic shape: a ``podbench dev``
    sidecar whose projected ``/etc/passwd`` names ``podbench`` at a uid the
    sidecar does not run as. So the message must not claim the collision is in
    the file it was going to write — the reader cats it, finds it empty, and
    stops believing the diagnostic.
    """
    with passwd.path.open("a") as handle:
        handle.write(f"{SEAT_USER}:x:999:999::/nonexistent:/bin/sh\n")
    with pytest.raises(RuntimeError) as raised:
        agent.ensure_passwd_entry(
            make_layout(tmp_path, root=False), uid=UNKNOWN_UID, gid=UNKNOWN_UID
        )
    message = str(raised.value)
    assert "already resolves to uid 999" in message
    assert f"uid 999 in {passwd.nss_path}" not in message
    assert "getent passwd" in message, "how to find which source has it"


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
    assert "group of 500 or more" in check.detail


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
    assert "group of 500 or more" in captured.err


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


def test_print_host_key_writes_no_second_start_up_report(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--print-host-key` runs the same start-up over an exec into a live seat.

    `SeatReport.from_log` takes the *last* marked line, so a report emitted on
    this path would become every launcher verb's answer to "what is this seat"
    - timestamped later than the real one and read from the same two files.
    `--ensure-only` is the same `_run` with the flag off, and it does write one.
    """
    layout = make_layout(tmp_path)
    patch_layout(monkeypatch, layout)
    monkeypatch.setattr(agent, "run_command", FakeRunner())
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert agent.main(["--print-host-key", "--no-self-check"]) == 0
    assert SEAT_REPORT_MARKER not in capsys.readouterr().err

    assert agent.main(["--ensure-only"]) == 0
    assert SEAT_REPORT_MARKER in capsys.readouterr().err


def test_session_env_forwards_podbench_variables_and_the_named_few() -> None:
    """The transport carries the image's environment, not just podbench's.

    sshd leaks none of its own environment, and one missing mechanism read as
    three defects: no ``PATH``, so the injection recipe died with ``sh: 1:
    python: not found``; no ``DEBUGINFOD_URLS``, so gdb's ``set debuginfod
    enabled on`` was inert over podbench's own transport while working under
    ``kubectl exec``; no ``PODBENCH_TARGET_CID``, so the helpers guessed. It is
    still an allow-list — the sshd config is world-readable — so a variable
    nobody named stays behind.
    """
    forwarded = agent.session_env(
        {
            "PODBENCH_TARGET_CID": "abc",
            "PATH": "/app/.venv/bin:/usr/bin",
            "DEBUGINFOD_URLS": "https://debuginfod.debian.net",
            "HOME": "/root",
            "LANG": "C.UTF-8",
        }
    )
    assert forwarded == {
        "PODBENCH_TARGET_CID": "abc",
        "PATH": "/app/.venv/bin:/usr/bin",
        "DEBUGINFOD_URLS": "https://debuginfod.debian.net",
    }


def test_a_named_variable_that_is_unset_is_simply_absent() -> None:
    """The allow-list names variables, not requirements: a session whose
    environment carries none of them is written without them rather than with
    empty values, which sshd would parse as pairs of its own."""
    assert "DEBUGINFOD_TIMEOUT" in agent.SESSION_ENV_NAMES
    assert agent.session_env({"PATH": "/usr/bin"}) == {"PATH": "/usr/bin"}


def test_a_seat_with_a_claim_is_told_where_the_interpreters_live() -> None:
    """A VS Code shell is an ssh session, which inherits none of the image's ENV.

    So UV_PYTHON_INSTALL_DIR arrives unset and uv falls back to a store inside
    the seat. `uv sync` with the venv already present is fine - uv reuses the
    interpreter pyvenv.cfg names, and that one is on the claim - but the moment
    uv has to *choose* one it writes a seat-local path the target cannot see,
    with no error. Measured on the beamline 2026-08-22.
    """
    forwarded = agent.session_env(
        {"PATH": "/usr/bin"}, interpreter_store=HOTFIX_INTERPRETER_PATH
    )
    assert forwarded[agent.UV_PYTHON_INSTALL_DIR_ENV] == HOTFIX_INTERPRETER_PATH


def test_the_store_is_never_carried_by_name_from_the_seats_own_environment() -> None:
    """Both images set it to /python, so inheriting it names the *seat's* store.

    That is the bleed, not the fix: a session working on the target's project
    would resolve interpreters against podbench's own copy.
    """
    forwarded = agent.session_env(
        {"PATH": "/usr/bin", "UV_PYTHON_INSTALL_DIR": "/python"}
    )
    assert agent.UV_PYTHON_INSTALL_DIR_ENV not in forwarded
    assert agent.UV_PYTHON_INSTALL_DIR_ENV not in agent.SESSION_ENV_NAMES


def test_a_seat_without_a_claim_is_told_nothing() -> None:
    """An ordinary attach seat has no claim, and pointing uv at a path that does
    not exist would break it for no reason."""
    assert agent.hotfix_interpreter_store(exists=lambda _: False) is None
    assert agent.session_env({"PATH": "/usr/bin"}, interpreter_store=None) == {
        "PATH": "/usr/bin"
    }


def test_the_store_survives_sshds_setenv_parser() -> None:
    """SetEnv is whitespace-separated NAME=value, so a path with a space in it
    would silently become a different directive."""
    forwarded = agent.session_env({}, interpreter_store=HOTFIX_INTERPRETER_PATH)
    assert unsafe_set_env(forwarded) == ()


# -- the symbol server, bounded and checked ---------------------------------


def test_an_unreachable_symbol_server_is_withdrawn_from_the_session() -> None:
    """gdb fetches a library's debuginfo *after* the attach, with the workload
    stopped, and waits DEBUGINFOD_TIMEOUT for each one. A seat behind an egress
    policy would pay that repeatedly for nothing, so the URL is dropped - which
    is the off switch, since gdb's client has nothing to query without it."""
    check = agent.check_debuginfod(
        {"DEBUGINFOD_URLS": "https://debuginfod.debian.net", "PATH": "/usr/bin"},
        reachable=lambda urls: False,
    )

    assert "DEBUGINFOD_URLS" not in check.env
    assert check.env["PATH"] == "/usr/bin"
    assert check.note is not None
    # The way out for the one route no sshd config reaches.
    assert "--no-debuginfod" in check.note


def test_a_reachable_server_is_bounded_rather_than_taken_on_trust() -> None:
    """Kept on - S3 got a fully symbolised backtrace out of it - but never
    unbounded: gdb's own default is 90s per library."""
    check = agent.check_debuginfod(
        {"DEBUGINFOD_URLS": "https://debuginfod.debian.net"},
        reachable=lambda urls: True,
    )

    assert check.env["DEBUGINFOD_URLS"] == "https://debuginfod.debian.net"
    assert check.env["DEBUGINFOD_TIMEOUT"] == agent.DEBUGINFOD_TIMEOUT_SECONDS
    assert check.note is None


def test_a_timeout_the_image_already_set_is_left_alone() -> None:
    """The image's ENV is the value that also bounds `kubectl exec` and the gdb
    cpptools starts, so a seat that was given one is not second-guessed."""
    check = agent.check_debuginfod(
        {"DEBUGINFOD_URLS": "https://elsewhere", "DEBUGINFOD_TIMEOUT": "9"},
        reachable=lambda urls: True,
    )

    assert check.env["DEBUGINFOD_TIMEOUT"] == "9"


def test_nothing_is_probed_when_no_server_was_named() -> None:
    """Without a URL gdb's debuginfod is already inert, and probing a server
    nobody named would mean inventing one."""
    asked: list[str] = []

    def reachable(urls: str) -> bool:
        asked.append(urls)
        return True

    check = agent.check_debuginfod({"PATH": "/usr/bin"}, reachable=reachable)

    assert asked == []
    assert check.note is None
    assert "DEBUGINFOD_TIMEOUT" not in check.env


def test_the_probe_answers_false_rather_than_raising() -> None:
    """Every failure mode - no host, an unparseable port, a name that does not
    resolve - means the same thing to the caller, and this runs in PID 1."""
    assert not agent.debuginfod_reachable("not-a-url")
    assert not agent.debuginfod_reachable("https://host:notaport")
    assert not agent.debuginfod_reachable("")


def test_a_seat_that_cannot_reach_the_server_says_so_and_writes_no_url(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision has to reach the config, and the reason has to reach the
    container's start-up log: an unreachable symbol server is invisible
    otherwise until the first breakpoint, where it costs the pause."""

    def unreachable(urls: str, *, timeout: float = 0.0) -> bool:
        return False

    monkeypatch.setattr(agent, "debuginfod_reachable", unreachable)
    layout = make_layout(tmp_path, root=False)

    report = agent.ensure_all(
        layout,
        env={**env, "DEBUGINFOD_URLS": "https://debuginfod.debian.net"},
        runner=FakeRunner(),
    )

    written = Path(layout.config_path).read_text()
    assert "DEBUGINFOD_URLS" not in written
    assert any("debuginfod is off" in change for change in report.changes)
    assert report.ok


def test_the_image_sets_the_timeout_the_agent_would_have_supplied() -> None:
    """The ENV is what bounds the routes no sshd config reaches - a kubectl exec
    shell, and the gdb cpptools starts through gdb-podbench. Two copies of one
    number, so the drift is what this pins."""
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"

    assert (
        f"ENV DEBUGINFOD_TIMEOUT={agent.DEBUGINFOD_TIMEOUT_SECONDS}"
        in dockerfile.read_text()
    )


def test_a_path_sshd_cannot_carry_is_reported_rather_than_dropped(
    tmp_path: Path, env: dict[str, str]
) -> None:
    """A ``PATH`` with a space in it is not hypothetical, and losing it silently
    is the exact bug SetEnv was widened to fix. The config is still written —
    with everything that did survive — and the step records the reason."""
    layout = make_layout(tmp_path, root=False)

    report = agent.ensure_all(
        layout,
        env={**env, "PATH": "/opt/my tools/bin", "PODBENCH_NODE_NAME": "nuc2"},
        runner=FakeRunner(),
    )

    assert [failure.name for failure in report.failures] == ["ensure-sshd-config"]
    assert "PATH" in report.failures[0].detail
    written = Path(layout.config_path).read_text()
    assert "SetEnv PODBENCH_NODE_NAME=nuc2 " in written
    assert "my tools" not in written


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
    """Comments and trailing commas are read now, so what is left here is a file
    that is not JSONC either.

    Rewriting it would discard whatever this parser could not see, and staying
    quiet would leave the seat one File -> Open Folder away from an
    unrecoverable OOM — so the file is untouched and the reason is a recorded
    failure.
    """
    settings = passwd.home / MACHINE_SETTINGS_PATH
    settings.parent.mkdir(parents=True)
    settings.write_text("{ mine }")

    report = agent.ensure_all(make_layout(tmp_path), env=env, runner=FakeRunner())

    assert settings.read_text() == "{ mine }"
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


def test_the_seat_authorises_the_claim_it_was_told_it_mounts(
    tmp_path: Path, env: dict[str, str], seat_gitconfig_include: Path
) -> None:
    """#217: the claim is a checkout owned by a uid the seat is not, so the
    user's first ``git status`` in the folder ``vscode`` opens is fatal.

    The path is the mountPath the launcher measured and put in the container
    spec, **never** :data:`HOTFIX_APP_PATH`. That default is exactly the
    assumption ``launcher.seat_claim_path`` exists to avoid: the application
    chooses where the claim is mounted and podbench only matches it, so a seat
    that authorised the default would authorise a directory it does not have and
    leave the one it does have refused.
    """
    mounted = "/srv/beamline/checkout"
    report = agent.ensure_all(
        make_layout(tmp_path),
        env={**env, CLAIM_PATH_ENV: mounted},
        runner=FakeRunner(),
    )

    assert report.failures == ()
    written = seat_gitconfig_include.read_text()
    assert f'directory = "{mounted}"' in written
    assert HOTFIX_APP_PATH not in written
    assert f"wrote {seat_gitconfig_include}" in report.changes
    # And it is an *ensure*: a second attach into a serving seat changes nothing.
    again = agent.ensure_all(
        make_layout(tmp_path),
        env={**env, CLAIM_PATH_ENV: mounted},
        runner=FakeRunner(),
    )
    assert again.changes == ()


def test_a_seat_with_no_claim_is_given_no_gitconfig_at_all(
    tmp_path: Path, env: dict[str, str], seat_gitconfig_include: Path
) -> None:
    """An ordinary ``attach`` seat mounts no claim, and so does every seat an
    older launcher landed - both arrive with the variable unset.

    Writing a ``safe.directory`` anyway would mean guessing a path, and the only
    guess available is the default mountPath. The image's baked ``include.path``
    is what makes silence work: git ignores an include that is not there.
    """
    report = agent.ensure_all(make_layout(tmp_path), env=env, runner=FakeRunner())

    assert report.failures == ()
    assert not seat_gitconfig_include.exists()
    assert not any("gitconfig" in change for change in report.changes)


def test_the_image_bakes_the_half_of_the_gitconfig_that_can_be_baked() -> None:
    """The other half of #216 and #217, which no test above can see.

    ``core.excludesFile`` is baked because it names two fixed paths - its own,
    and a pattern for a fixed directory *name* - and a gitignore pattern matches
    at any depth, so the mountPath varying does not reach it. ``safe.directory``
    is not, so the image bakes an ``include.path`` under ``/tmp`` instead: the
    agent cannot create a file in root-owned ``/etc`` on the rungs that are not
    root.

    Checked against the Dockerfile as text for the reason the extrausers
    contract above is: the constant and the image are two halves of one
    agreement, and nothing else in this suite would notice them drifting apart.
    The pattern is pinned against :data:`CLAIM_DEST_NAME` for the same reason -
    a rename of the provision directory that missed the image would put the
    15 MB back in the SCM pane with nothing failing.
    """
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()

    assert "> /etc/gitconfig" in dockerfile, "the seat's system config is baked"
    assert REAL_SEAT_GITCONFIG_INCLUDE in dockerfile, (
        "and it includes the one file the agent can write"
    )
    # The ignore file's path is the image's alone - no constant here names it -
    # so what is checked is that the two lines agree: core.excludesFile is given
    # the path that the line below redirects the pattern into.
    assert "excludesFile = %s" in dockerfile, "core.excludesFile is baked"
    assert f"{IMAGE_GITIGNORE} {REAL_SEAT_GITCONFIG_INCLUDE}" in dockerfile, (
        "and it is the first of the two paths that printf is given"
    )
    assert f"> {IMAGE_GITIGNORE}" in dockerfile, (
        "which has to be the file the pattern is actually written to"
    )
    assert f"'{CLAIM_DEST_NAME}/'" in dockerfile, (
        "the pattern must name the directory --provision actually installs into"
    )


def _seat_proc_tree(
    root: Path, *, seat: tuple[int, int], target: tuple[int, int]
) -> Path:
    """A `/proc` with this seat and one attributed target process in it.

    Enough for `seat_report` and no more: the credentials of both sides, and a
    cgroup line carrying the target container id, which is the only attribution
    that stays correct under a shared PID namespace.
    """

    def status(pid: str, uid: int, gid: int, comm: str) -> None:
        entry = root / pid
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "comm").write_text(f"{comm}\n")
        (entry / "cmdline").write_text(f"{comm}\x00")
        (entry / "status").write_text(
            f"Name:\t{comm}\nState:\tS (sleeping)\nPPid:\t0\n"
            f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
            f"Gid:\t{gid}\t{gid}\t{gid}\t{gid}\n"
            "Threads:\t1\nCapEff:\t0000000000000000\n"
            "CapBnd:\t0000000000000000\nCapAmb:\t0000000000000000\n"
        )

    status("self", *seat, "podbench")
    (root / "self" / "cgroup").write_text("0::/\n")
    status("17", *target, "python3")
    (root / "17" / "cgroup").write_text(f"0::/../cri-containerd-{TARGET_CID}.scope\n")
    (root / "17" / "root").symlink_to(root)
    return root


def test_the_seat_measures_itself_at_startup_and_writes_it_where_a_verb_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issues #94b and #99, over one line.

    `list` and `status` read pod JSON and exec nothing, so the rung they could
    name was a securityContext. The seat can measure the real thing for free -
    two `/proc` reads and no ptrace - and the container log carries it back.
    """
    monkeypatch.setenv(TARGET_CID_ENV, TARGET_CID)
    proc = _seat_proc_tree(tmp_path / "proc", seat=(1000, 1000), target=(1000, 1000))
    refusal = agent.CheckResult("ensure-sshd-config", False, "SetEnv refused PATH")

    report = agent.seat_report(agent.EnsureReport(failures=(refusal,)), proc=proc)

    assert (report.uid, report.gid) == (1000, 1000)
    assert (report.target_uid, report.target_gid, report.target_pid) == (1000, 1000, 17)
    assert report.rung is Rung.DEGRADED
    assert report.failures == (str(refusal),)
    # The line is the contract, not the object: a launcher recovers it from a
    # log written by a build it has never met.
    assert SeatReport.from_log(f"agent: {report.to_line()}") == report


def test_a_seat_that_cannot_identify_its_target_reports_no_rung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PID 1 is not the target under `shareProcessNamespace`, so a seat with no
    container id refuses to guess - and a comparison that was never made is not
    the bottom rung, it is no measurement."""
    monkeypatch.delenv(TARGET_CID_ENV, raising=False)
    proc = _seat_proc_tree(tmp_path / "proc", seat=(1000, 1000), target=(1000, 1000))

    report = agent.seat_report(proc=proc)

    assert report.uid == 1000
    assert report.target_uid is None
    assert report.rung is None
