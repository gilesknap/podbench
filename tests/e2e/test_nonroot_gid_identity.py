"""#102 as a regression: a seat gets its login at a non-zero *gid*, or it gets none.

Every claim here is about the one thing that stood between podbench and a Python
or C++ IOC at Diamond: a seat on the degraded rung runs as the target's uid **and
gid**, and the image's ``/etc/passwd`` is writable only by GID 0. On
``b01-1-beamline/bl01c-di-dcam-04-0`` — uid and gid 36070 — the agent therefore
could not append the record sshd resolves a login name through, ``ssh-keygen``
died with "No user exists for uid 36070" before a host key was ever written, and
the seat landed with **no ssh at all**: no editor, no Remote-SSH. The documented
way out then, ``--seat-gid-root``, pinned ``runAsGroup: 0``, and the gid is half of the
credential match ``__ptrace_may_access`` makes, so it bought the transport and
took the debugger (#98).

The fix gives the image a second NSS source — ``libnss-extrausers``, its database
world-writable — so the append needs no capability, no particular gid and no edit
to the workload's manifest. ``apps/nonroot-gid.yaml`` is the target's shape.

**The setup is the whole test, and it is easy to get wrong: a seat that lands as
root cannot show this defect at all.** ``/etc/passwd`` is then writable and the
append succeeds by a route that has nothing to do with the fix. Two things keep
the walk off that rung, and the policy is the one that does not depend on the
launcher: the target names a non-root uid, so the ladder now starts at
``degraded`` of its own accord, *and* ``deny-sys-ptrace.yaml`` is bound to refuse
the rung above it. Unbind it, change the ordering rule, and every assertion below
passes vacuously — so the refusal is checked for rather than assumed.

**Unlike ``test_dls_ioc.py``, this module needs nothing of the node's Yama.** An
NSS lookup is not a ptrace, so the identity half of a seat is decided identically
at ``ptrace_scope`` 0 and 1 — which is why the assertions are about ssh, the
databases and the report's uid line, and never about a verdict. It therefore runs
in the *main* e2e job at the distributions' default scope, where
``test_dls_ioc.py`` skips.

**The one contract no unit test can hold** is the last test here.
``agent.extrausers_serves`` hardcodes floors — ``MINUID`` 500, ``MINGID`` 500,
``USERSGID`` 100 — read out of Debian 0.6-4.1's ``s_config.h``, and every unit
test necessarily fakes the library. If a rebuild ever ships a build with
different floors, that function silently routes a seat to a database that will
not answer it: the append succeeds, the line is in the file, and nothing
resolves. Only the real shared object in the real image can say otherwise.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from podbench.agent import PASSWD_PATH, SEAT_NSS_PATH, extrausers_serves
from podbench.launcher import ssh_config_path
from podbench.model import PodRef, Rung
from podbench.sshcfg import SEAT_USER

from .conftest import FIRST_SEAT, KubectlCli, NamespaceFactory, PodbenchCli

SSH_TIMEOUT = 120.0

NO_MULTIPLEXING = ("-o", "ControlMaster=no", "-o", "ControlPath=none")
"""The generated stanza asks for a ControlMaster, and a shared connection would
make one test's failure the next one's, so every session here is its own."""

TARGET_POD = "nonroot-gid-target"
TARGET_CONTAINER = "app"

TARGET_UID = 36070
TARGET_GID = 36070
"""The field target's credentials, and both are above the extrausers floors.

Deliberately the measured numbers rather than convenient ones: the whole class of
bug is a *gid* that is neither 0 nor pinnable, and a pair that happened to sit
below a floor would exercise the fallback instead of the route under test.
"""

SECOND_SEAT = f"{FIRST_SEAT[:-1]}2"
"""The name a second ``--new`` attach takes. Ephemeral container names are burnt
for the pod's lifetime, so this is a fact about the pod and not a preference."""

POLICY_LABELS = {"podbench.dev/deny-sys-ptrace": "enforce"}
POLICY_FILE = "deny-sys-ptrace.yaml"
POLICY_API = "admissionregistration.k8s.io/v1"
POLICY_RESOURCE = "validatingadmissionpolicies"

POLICY_OBJECTS = (
    "validatingadmissionpolicybinding/podbench-deny-sys-ptrace",
    "validatingadmissionpolicy/podbench-deny-sys-ptrace",
)
"""Teardown, spelled ``type/name``. Given bare words kubectl reads everything
after the first as names of the *first* type: it deletes the binding, reports
nothing wrong under ``--ignore-not-found``, and leaves the policy on the cluster
to change what every later run is admitted to do."""

SSH_SEAT_TICKED = "[x] ssh seat"
"""The report line that *is* this issue's verdict. Before the fix the same run
printed ``[ ] ssh seat`` and ``no ssh config was written``."""

FLOOR_PROBE_USER = "podbenchfloorprobe"
FLOOR_PROBE_UID = 101
FLOOR_PROBE_GID = 101
"""A record below both floors — nginx-unprivileged's and envoy's credentials.

A distinct login name so the probe cannot be confused with the seat's own record,
and cannot shadow it if the cleanup below ever fails to run — and so that the
``getpwnam`` half stays a clean refusal, since ``files`` carries no such name.

The ``getpwuid`` half cannot be a clean refusal any more and the assertion below
says so instead of pretending: since #103 the image pre-seeds ``/etc/passwd``
with a static record for every free uid under 500, which is this whole range, so
``getent passwd 101`` is *answered* — by ``podbench-101``, out of ``files``. What
the floor still decides is whether it is answered by the record just appended to
the extrausers database, and that is what is checked.
"""


# -- the cluster this reproduction needs ------------------------------------


def _serves_validating_policies(cli: KubectlCli) -> bool:
    """Whether the API server serves :data:`POLICY_RESOURCE` at :data:`POLICY_API`.

    Asked of discovery by *version*. ``ValidatingAdmissionPolicy`` has been core
    API since 1.30, so this is expected to be true everywhere the suite runs; it
    is checked so that a cluster too old to refuse the full rung skips with the
    reason rather than failing every assertion for want of a ladder walk.
    """
    listing = cli.run(
        "get", "--raw", f"/apis/{POLICY_API}", namespaced=False, check=False
    )
    if listing.returncode != 0:
        return False
    payload: Any = json.loads(listing.stdout)
    resources = cast(list[Any], cast(dict[str, Any], payload).get("resources") or [])
    return POLICY_RESOURCE in {
        str(cast(dict[str, Any], entry).get("name")) for entry in resources
    }


@pytest.fixture(scope="session")
def deny_sys_ptrace(
    kubectl_bin: str, kube_context: str | None, apps_dir: Path
) -> Iterator[None]:
    """Bind the refusing policy for the session, and delete it once.

    Session-scoped for the *teardown*, not the setup: both objects are
    cluster-scoped with fixed names, so a concurrent apply is idempotent while a
    concurrent delete is not. Deleting the namespace only un-scopes the binding —
    the label goes with it — and removes neither object.
    """
    cli = KubectlCli("default", binary=kubectl_bin, context=kube_context)
    if not _serves_validating_policies(cli):
        pytest.skip(
            f"this cluster does not serve {POLICY_RESOURCE} at {POLICY_API}, so "
            "the full rung cannot be refused; without a ladder walk the seat "
            "lands root, /etc/passwd is writable anyway and #102 cannot appear "
            f"(tests/e2e/apps/{POLICY_FILE})"
        )
    try:
        cli.run(
            "apply",
            "-f",
            "-",
            stdin=(apps_dir / POLICY_FILE).read_text(),
            namespaced=False,
        )
        yield
    finally:
        cli.run(
            "delete",
            *POLICY_OBJECTS,
            "--ignore-not-found",
            namespaced=False,
            check=False,
        )


@pytest.fixture(scope="module")
def gid_namespace(namespace_factory: NamespaceFactory) -> str:
    """A scratch namespace that opts into the refusing policy."""
    return namespace_factory(labels=POLICY_LABELS, suffix="gid-")


@pytest.fixture(scope="module")
def gid_kubectl(gid_namespace: str) -> KubectlCli:
    return KubectlCli(gid_namespace)


@pytest.fixture(scope="module")
def gid_target(gid_kubectl: KubectlCli, apps_dir: Path, deny_sys_ptrace: None) -> str:
    """The non-zero-uid-and-gid target, running.

    Depends on the policy so the pod cannot exist in a window where an attach
    would be admitted the full rung: the policy evaluates *every* ephemeral
    container on the pod, so a full-rung seat that landed first would make every
    later attach on this pod fail outright rather than walk.
    """
    gid_kubectl.apply(apps_dir / "nonroot-gid.yaml")
    gid_kubectl.wait(f"pod/{TARGET_POD}", "condition=Ready", timeout=300)
    return TARGET_POD


@pytest.fixture(scope="module")
def target_credentials(gid_kubectl: KubectlCli, gid_target: str) -> tuple[int, int]:
    """The target's real uid and gid, read from the running container.

    Read rather than assumed. The manifest asks for 36070/36070, but a mutating
    admission policy or a namespace default could have rewritten it, and every
    assertion below would then be checking podbench against the wrong numbers —
    the failure would name podbench for somebody else's mutation.
    """
    read = gid_kubectl.exec(TARGET_POD, ["id", "-u"], container=TARGET_CONTAINER)
    uid = int(read.stdout.strip())
    read = gid_kubectl.exec(TARGET_POD, ["id", "-g"], container=TARGET_CONTAINER)
    gid = int(read.stdout.strip())
    if (uid, gid) != (TARGET_UID, TARGET_GID):
        pytest.skip(
            f"the target came up as {uid}:{gid}, not {TARGET_UID}:{TARGET_GID} — "
            "something on this cluster rewrote the fixture's securityContext"
        )
    return uid, gid


@pytest.fixture(scope="module")
def gid_client(
    gid_namespace: str, tmp_path_factory: pytest.TempPathFactory
) -> PodbenchCli:
    """One launcher client for the module, so both attaches share a config dir."""
    return PodbenchCli(
        gid_namespace, config_dir=tmp_path_factory.mktemp("podbench-gid-client")
    )


@pytest.fixture(scope="module")
def gid_seat(
    gid_client: PodbenchCli,
    gid_target: str,
    target_credentials: tuple[int, int],
    podbench_image: str,
    ssh_identity: tuple[Path, str],
) -> str:
    """One ``podbench attach``, shared: its report, as it was printed.

    Shared because an ephemeral container's name is burnt for the pod's lifetime,
    so a second attach is a second seat rather than a repeat of this one. The
    stanza is written rather than printed, because one test below opens a real ssh
    session through it.
    """
    identity, _ = ssh_identity
    result = gid_client.run(
        "attach",
        TARGET_POD,
        "--target",
        TARGET_CONTAINER,
        "--image",
        podbench_image,
        "--identity",
        str(identity),
        *gid_client.common(),
        timeout=420,
    )
    return result.stdout


def _host_alias(config: Path) -> str:
    for line in config.read_text().splitlines():
        if line.startswith("Host "):
            return line.split(None, 1)[1].strip()
    raise AssertionError(f"no Host stanza in {config}:\n{config.read_text()}")


@pytest.fixture(scope="module")
def gid_ssh(
    gid_client: PodbenchCli, gid_namespace: str, gid_seat: str
) -> tuple[Path, str]:
    """The stanza ``attach`` wrote for this seat, and its Host alias.

    Derived from :func:`podbench.launcher.ssh_config_path` rather than spelled:
    the stanza is named per *seat* since #93, and a hand-built path here would go
    stale the next time that changes.
    """
    if shutil.which("ssh") is None:
        pytest.skip("no ssh client installed")
    config_dir = gid_client.config_dir
    assert config_dir is not None
    path = ssh_config_path(config_dir, PodRef(gid_namespace, TARGET_POD), FIRST_SEAT)
    assert path.is_file(), f"attach wrote no ssh config:\n{gid_seat}"
    return path, _host_alias(path)


def _ssh(
    config: Path, alias: str, command: Sequence[str]
) -> subprocess.CompletedProcess[str]:
    argv = [
        "ssh",
        "-F",
        str(config),
        "-o",
        "BatchMode=yes",
        *NO_MULTIPLEXING,
        alias,
        *command,
    ]
    completed = subprocess.run(
        argv, capture_output=True, text=True, timeout=SSH_TIMEOUT, check=False
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"{shlex.join(argv)} exited {completed.returncode}\n{completed.stderr}"
        )
    return completed


def _in_seat(cli: KubectlCli, script: str, *, seat: str = FIRST_SEAT) -> str:
    """Run a shell snippet in the seat and return its stdout.

    ``kubectl exec`` rather than ssh on purpose: the exec half of a seat works
    whether or not the identity landed, so a failure here is about the databases
    and never about the transport that this issue is otherwise all about.
    """
    return cli.exec(
        TARGET_POD, ["sh", "-c", script], container=seat, timeout=120
    ).stdout


# -- the seat lands, and it has ssh ----------------------------------------


def test_a_non_zero_gid_target_lands_a_degraded_seat_that_has_ssh(
    gid_seat: str, target_credentials: tuple[int, int]
) -> None:
    """The whole of #102, as the report prints it.

    Three claims in one assertion block because they are one claim: the ladder
    walked down (so this is the rung the field target gets), the seat carries the
    target's own uid *and* gid (so it changed no identity to get there), and ssh
    is available anyway. Before the fix the first two held and the third did not.
    """
    uid, gid = target_credentials
    assert Rung.DEGRADED.value in gid_seat, gid_seat
    assert f"ids         seat {uid}:{gid}, target {uid}:{gid}" in gid_seat, gid_seat
    assert SSH_SEAT_TICKED in gid_seat, gid_seat
    assert "no ssh config was written" not in gid_seat, gid_seat
    # The gid is the half that was broken, and since #103 the report prints it -
    # so this asserts the number the report showed is the number the pod has,
    # rather than trusting either on its own.
    assert gid == TARGET_GID


def test_the_seat_really_runs_at_the_target_gid(
    gid_kubectl: KubectlCli, gid_seat: str
) -> None:
    """``id`` in the seat, because the rung name is not the evidence.

    A rung is what podbench *asked* for. What decides whether the append can
    happen is what the kubelet gave it.
    """
    identity = _in_seat(gid_kubectl, "id -u; id -g").split()
    assert [int(value) for value in identity] == [TARGET_UID, TARGET_GID]


def test_a_real_ssh_session_resolves_the_login_name(
    gid_ssh: tuple[Path, str],
) -> None:
    """A real ssh session, not the report's tick.

    The tick is podbench's opinion; this is sshd's, and sshd is the only judge
    that matters here — it resolves the login name through NSS *before* it will
    look at a key, so a session that comes back at all is proof the record landed
    somewhere NSS answers from. ``id`` is asked for rather than assumed because
    the name resolving to the *wrong* uid would authenticate and then put the user
    in the wrong skin.
    """
    config, alias = gid_ssh
    printed = _ssh(config, alias, ["id"]).stdout
    assert f"uid={TARGET_UID}({SEAT_USER})" in printed, printed
    assert f"gid={TARGET_GID}" in printed, printed


def test_the_ssh_session_gets_a_home_from_the_record(
    gid_ssh: tuple[Path, str],
) -> None:
    """``$HOME`` comes out of the passwd record, which is the point of writing one.

    A login that resolved but carried no usable home would leave Remote-SSH with
    nowhere to install its server, and the failure would surface inside VS Code
    rather than here.
    """
    config, alias = gid_ssh
    # One argument, not three. ssh joins argv with spaces and hands the result to
    # the login shell, so `sh -c echo $HOME` arrives as a command of `echo` with
    # `$HOME` as its *$0* — it prints an empty line and the test passes or fails
    # for a reason that has nothing to do with the record.
    printed = _ssh(config, alias, ["echo $HOME"]).stdout.strip()
    assert printed, "the session reported no HOME"
    resolved = _ssh(config, alias, ["getent", "passwd", str(TARGET_UID)]).stdout
    assert resolved.strip().split(":")[5] == printed, resolved


# -- where the record went, and where it did not --------------------------


def test_the_record_lands_in_the_extrausers_database(
    gid_kubectl: KubectlCli, gid_seat: str
) -> None:
    """The seat's own record is in :data:`SEAT_NSS_PATH`, exactly once.

    Counted by login name rather than by total lines: another test in this module
    appends a probe record and removes it again, and a test that asserted on the
    file's length would fail or pass depending on collection order.
    """
    found = _in_seat(gid_kubectl, f"grep -c '^{SEAT_USER}:' {SEAT_NSS_PATH}")
    assert int(found.strip()) == 1
    record = _in_seat(gid_kubectl, f"grep '^{SEAT_USER}:' {SEAT_NSS_PATH}").strip()
    assert record.split(":")[2:4] == [str(TARGET_UID), str(TARGET_GID)], record


def test_etc_passwd_is_neither_written_nor_writable(
    gid_kubectl: KubectlCli, gid_seat: str
) -> None:
    """``/etc/passwd`` carries no record and could not have been given one.

    The second half is what makes the first half mean something: a test that only
    checked the file was unmodified would pass just as well on a seat that *could*
    have written it and happened not to. This asserts the condition that used to
    end the attach — the file is not writable by this uid and gid — while the seat
    has ssh regardless.
    """
    # `|| true` because grep exits 1 on no match, which is the expected answer
    # here and would otherwise be raised as a failed command rather than asserted.
    found = _in_seat(gid_kubectl, f"grep -c '{SEAT_USER}' {PASSWD_PATH} || true")
    assert int(found.strip() or 0) == 0
    writable = _in_seat(
        gid_kubectl, f"test -w {PASSWD_PATH} && echo writable || echo refused"
    )
    assert writable.strip() == "refused"


def test_nss_resolves_the_seat_by_uid_and_by_name(
    gid_kubectl: KubectlCli, gid_seat: str
) -> None:
    """Both lookups, because sshd makes both and they fail separately.

    ``ssh-keygen`` dies on ``getpwuid``; sshd authenticates through ``getpwnam``.
    A database consulted for one and not the other would leave the seat with a
    host key and no login, which reads as a key problem.
    """
    by_uid = _in_seat(gid_kubectl, f"getent passwd {TARGET_UID}").strip()
    by_name = _in_seat(gid_kubectl, f"getent passwd {SEAT_USER}").strip()
    assert by_uid.startswith(f"{SEAT_USER}:"), by_uid
    assert by_uid == by_name


# -- the fallback, which is the half that is easy to break ----------------


def test_a_gid_0_seat_falls_back_to_etc_passwd_and_still_has_ssh(
    gid_client: PodbenchCli,
    gid_kubectl: KubectlCli,
    gid_seat: str,
    podbench_image: str,
    ssh_identity: tuple[Path, str],
) -> None:
    """The ``/etc/passwd`` fallback survives, because extrausers refuses gid 0.

    This is the regression test for the bug *inside* #102's fix. Routing on the
    database's mode instead of on the seat's credentials would send this seat to
    :data:`SEAT_NSS_PATH`, where the record is written, ignored for being below
    ``MINGID``, and never resolved. It would have been invisible at Diamond,
    whose 36070/36070 clears every floor.

    ``--seat-gid-root`` used to pose this seat and retired with #103, so the
    shape is posed by ``--target-gid 0`` instead — which is not a workaround: a
    target whose own group really *is* 0 is the seat this fallback now exists
    for, and pinning the gid explicitly is what podbench does for it. The pin
    also suppresses the id correction, which is the other half of the contract:
    a flag the launcher quietly overrode would be worse than a wrong seat.

    Ordered after the seat above by depending on it: this is a second ephemeral
    container, and its name is burnt for the pod's lifetime either way.
    """
    identity, _ = ssh_identity
    report = gid_client.run(
        "attach",
        TARGET_POD,
        "--target",
        TARGET_CONTAINER,
        "--image",
        podbench_image,
        "--identity",
        str(identity),
        "--new",
        "--target-gid",
        "0",
        "--print-config",
        *gid_client.common(),
        timeout=420,
    ).stdout
    assert SSH_SEAT_TICKED in report, report

    assert not extrausers_serves(TARGET_UID, 0)
    assert int(_in_seat(gid_kubectl, "id -g", seat=SECOND_SEAT)) == 0
    # The record went to the file that has no floor, and the database this change
    # added stayed empty in this seat.
    in_passwd = _in_seat(
        gid_kubectl, f"grep -c '^{SEAT_USER}:' {PASSWD_PATH}", seat=SECOND_SEAT
    )
    in_extrausers = _in_seat(
        gid_kubectl,
        f"grep -c '^{SEAT_USER}:' {SEAT_NSS_PATH} || true",
        seat=SECOND_SEAT,
    )
    assert int(in_passwd.strip()) == 1
    assert int(in_extrausers.strip() or 0) == 0
    resolved = _in_seat(
        gid_kubectl, f"getent passwd {TARGET_UID}", seat=SECOND_SEAT
    ).strip()
    assert resolved.startswith(f"{SEAT_USER}:"), resolved


# -- the floors, which only the real library can confirm ------------------


def test_the_image_libnss_extrausers_has_the_floors_the_agent_hardcodes(
    gid_kubectl: KubectlCli, gid_seat: str
) -> None:
    """The one assertion in this suite that cannot be made without the real image.

    :func:`podbench.agent.extrausers_serves` carries ``MINUID``/``MINGID`` 500 as
    literals, read out of Debian 0.6-4.1's ``s_config.h`` because there is no
    configuration file that moves them and no way to interrogate the shared
    object. Every unit test fakes the library, so a rebuild that shipped
    different floors would leave that function routing seats to a database that
    will not answer them — and the failure is silent in the worst way: the append
    succeeds, the record is in the file, and ``getpwnam`` returns nothing.

    The probe writes a record below both floors, checks that the real library
    ignores it for *both* lookups, and removes it again — so the seat this module
    shares is left exactly as it was found. "Ignores" is not "nothing answers":
    ``files`` answers ``getpwuid`` for this uid now (see
    :data:`FLOOR_PROBE_UID`), so what is asserted is that the answer is not the
    appended record.
    """
    probe = (
        f"{FLOOR_PROBE_USER}:x:{FLOOR_PROBE_UID}:{FLOOR_PROBE_GID}:"
        f"{FLOOR_PROBE_USER}:/tmp:/bin/sh"
    )
    # The cleanup rewrites the file in place through its own descriptor rather
    # than with `sed -i`. Only the *file* is 0666: `/var/lib/extrausers` is
    # root-owned 755, so sed cannot create the temporary file it renames over the
    # original and fails with "couldn't open temporary file … Permission denied",
    # leaving the probe record behind. `cat > file` truncates the existing inode,
    # which write permission on the file is enough for.
    script = (
        f"printf '%s\\n' '{probe}' >> {SEAT_NSS_PATH}; "
        f"getent passwd {FLOOR_PROBE_UID} || echo NO_UID; "
        f"getent passwd {FLOOR_PROBE_USER} || echo NO_NAME; "
        f"grep -c '^{FLOOR_PROBE_USER}:' {SEAT_NSS_PATH}; "
        f"grep -v '^{FLOOR_PROBE_USER}:' {SEAT_NSS_PATH} > /tmp/nss-kept || true; "
        f"cat /tmp/nss-kept > {SEAT_NSS_PATH}"
    )
    printed = _in_seat(gid_kubectl, script)
    # Not "nothing answered": the image's own static record for uid 101 answers,
    # which is #103's fix for the range below the floor. What must not answer is
    # the record just appended to the extrausers database.
    assert f"{FLOOR_PROBE_USER}:" not in printed.split("NO_NAME")[0], printed
    assert "NO_NAME" in printed, printed
    # The record really was in the file while the lookups were refused, so this
    # is the library's floor and not a failed write.
    assert printed.strip().splitlines()[-1].strip() == "1", printed
    # And the agent agrees about this pair, which is what ties the measurement to
    # the code that depends on it.
    assert not extrausers_serves(FLOOR_PROBE_UID, FLOOR_PROBE_GID)
    assert extrausers_serves(TARGET_UID, TARGET_GID)
    # Left as found, so collection order cannot matter.
    remaining = _in_seat(
        gid_kubectl, f"grep -c '^{FLOOR_PROBE_USER}:' {SEAT_NSS_PATH} || true"
    )
    assert int(remaining.strip() or 0) == 0
