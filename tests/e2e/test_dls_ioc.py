"""#89 as a regression: report what the probe measured, not what admission granted.

Every claim in this file is about a seat that **can** ptrace its target and was
told by podbench that it cannot. The shape is Diamond's, reconstructed from
measurements rather than reasoned about (``apps/dls-ioc.yaml``): a root IOC four
processes deep behind ``stdio-expose``/``pptty``, on a node with
``kernel.yama.ptrace_scope=0``, under a policy engine that takes ``SYS_PTRACE``
away. uid 0 tracing uid 0 at scope 0 is classic ptrace and needs no capability,
so the probe attaches — and three places in podbench answered from the
capability bit instead, each wrong in a different direction:

* ``flavour.py`` withdrew the debugpy flavour on ``if not seat.cap_sys_ptrace``,
  so ``debug-config`` emitted no configuration at all;
* ``vscode.py``'s ``--provision`` said the injection "cannot be driven from
  here" two lines before driving it from here;
* ``model.py``'s ``Rung.description`` was a static per-rung string, so ``status``
  called a seat that had just live-attached "read-only inspection" — it has since
  been removed in favour of what the seat measures in itself.

The measured run this file reproduces printed, in the same minute:

    CAP_SYS_PTRACE (eff)       no
    live attach (pid 12)       OK
    VERDICT: live attach available
    BLOCKER: none

**Two policies are bound, and both are needed.** ``strip-sys-ptrace.yaml``
mutates the capability away, which is what lands the seat #89 is about;
``deny-sys-ptrace.yaml`` refuses one that still carries it. The denying policy
alone cannot reproduce the defect — measured on the bed, a root target under it
gets *no seat at all* ("no rung of the capability ladder was admitted": the full
rung refused, the degraded rung unauthorable at uid 0, the seat rung refused by
the kubelet) — and the mutating policy alone would leave a broken expression
free to admit an ordinary full-rung seat, against which every assertion here
passes vacuously. Bound together, mutation runs first and validation then sees a
spec with no capability in it, so the seat that lands is provably capless.

The tests are deliberately written against what the verbs *print*. The bit and
the probe live one attribute apart inside the process, and a test that read
either of them would keep passing while the sentence a user reads stayed wrong.

**Two cluster properties this module cannot create for itself**, each of which
skips it rather than failing it, because neither is a podbench defect:
``kernel.yama.ptrace_scope`` must be ``0`` on the node — at the distributions'
default of ``1`` a capless seat genuinely cannot attach, the probe fails
honestly and there is no #89 — and the API server must serve
``MutatingAdmissionPolicy`` at ``admissionregistration.k8s.io/v1``. CI gets both
from a dedicated job (``.github/workflows/_e2e.yml``, called with
``ptrace-scope: "0"``); the main e2e job runs at the default of 1 and sees this
module skip, which is what the ``-rs`` line in its log is for.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from .conftest import FIRST_SEAT, KubectlCli, NamespaceFactory, PodbenchCli

TARGET_POD = "dls-ioc"
TARGET_CONTAINER = "ioc"

APP_CMDLINE_MARKER = "/app/.venv/bin/fastcs-thorlabs-mff"
"""How the app is picked out of the wrapper chain.

Never by pid: the fixture reproduced Diamond's 1/9/11/12 exactly on one run and
came up 1/8/10/11 on the next, two identical pods on one node. The depth is
stable, the numbers are not.
"""

POLICY_LABELS = {
    "podbench.dev/strip-sys-ptrace": "enforce",
    "podbench.dev/deny-sys-ptrace": "enforce",
}
"""The two namespace opt-ins the policy fixtures are bound by."""

POLICY_FILES = ("strip-sys-ptrace.yaml", "deny-sys-ptrace.yaml")

POLICY_API = "admissionregistration.k8s.io/v1"
POLICY_RESOURCES = ("mutatingadmissionpolicies", "validatingadmissionpolicies")
"""What the two manifests are written against, asked for by *version*.

``kubectl api-resources --api-group=…`` answers without one, so a cluster that
serves ``MutatingAdmissionPolicy`` only at ``v1alpha1``/``v1beta1`` — every
Kubernetes before it graduated — looks supported and then fails inside the
fixture with ``no matches for kind``. Asking the discovery endpoint for this one
version turns that into the skip it should always have been.
"""

POLICY_OBJECTS = (
    "mutatingadmissionpolicybinding/podbench-strip-sys-ptrace",
    "mutatingadmissionpolicy/podbench-strip-sys-ptrace",
    "validatingadmissionpolicybinding/podbench-deny-sys-ptrace",
    "validatingadmissionpolicy/podbench-deny-sys-ptrace",
)
"""Teardown, spelled ``type/name``. Without the slashes kubectl reads everything
after the first word as names of the *first* type, deletes the bindings and
leaves both policies on the cluster with nothing to say it did."""

CAP_REFUSAL = "CAP_SYS_PTRACE is not in this seat's effective set"
"""The sentence ``flavour.py`` withdraws debugpy with. Also asserted in
``tests/test_vscode.py``, so it is a fixed string rather than a paraphrase."""

PROVISION_REFUSAL = "cannot be driven from here"
# vscode.WITHHELD_NOTE, spelled out rather than imported: these tests read what
# a seat printed, and the seat is a different build of podbench from the one
# running them.
INJECTION_WITHHELD = "so no debugpy configuration is written"
"""The second capability-derived claim, in ``vscode.py``'s ``--provision``.

Matched on the clause both wordings share rather than on either whole sentence:
the note said "this seat has no CAP_SYS_PTRACE, so the injection cannot be
driven from here" before #89 and "this seat cannot ptrace the target, so …"
after it, and a regression that restored the first must not pass because the
test spelled the second.
"""

LIVE_ATTACH_VERDICT = "VERDICT: live attach available"
NO_BLOCKER = "BLOCKER: none"

YAMA_SYSCTL = "/proc/sys/kernel/yama/ptrace_scope"
"""Read from inside the target: Yama's scope is not namespaced, so a container
sees the node's value, and the node is what decides whether this reproduction is
possible at all."""


# -- the cluster this reproduction needs ------------------------------------


def _served_resources(cli: KubectlCli) -> set[str]:
    """The resource names :data:`POLICY_API` serves, or nothing at all.

    An unserved group version is a 404 from discovery, which is an answer and
    not an error, so the call is unchecked and a failure reads as "serves
    none" — the caller turns that into a skip naming what it wanted.
    """
    listing = cli.run(
        "get", "--raw", f"/apis/{POLICY_API}", namespaced=False, check=False
    )
    if listing.returncode != 0:
        return set()
    payload: Any = json.loads(listing.stdout)
    resources = cast(list[Any], cast(dict[str, Any], payload).get("resources") or [])
    return {str(cast(dict[str, Any], entry).get("name")) for entry in resources}


@pytest.fixture(scope="session")
def admission_policies(
    kubectl_bin: str, kube_context: str | None, apps_dir: Path
) -> Iterator[None]:
    """Bind both policies for the session, and delete them once.

    Session-scoped for the teardown rather than for the setup: the objects are
    cluster-scoped and fixed-named, so applying them twice is idempotent while
    deleting them under a concurrently running module is not.
    """
    cli = KubectlCli("default", binary=kubectl_bin, context=kube_context)
    missing = sorted(set(POLICY_RESOURCES) - _served_resources(cli))
    if missing:
        pytest.skip(
            f"this cluster does not serve {', '.join(missing)} at {POLICY_API}, "
            "and #89 needs a policy engine that strips SYS_PTRACE rather than "
            "refusing it — refusing it lands no seat at all on a root target "
            "(tests/e2e/apps/strip-sys-ptrace.yaml)"
        )
    try:
        for name in POLICY_FILES:
            cli.run(
                "apply",
                "-f",
                "-",
                stdin=(apps_dir / name).read_text(),
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
def dls_namespace(namespace_factory: NamespaceFactory) -> str:
    """A scratch namespace that opts into both policies."""
    return namespace_factory(labels=POLICY_LABELS, suffix="dls-")


@pytest.fixture(scope="module")
def dls_kubectl(dls_namespace: str) -> KubectlCli:
    return KubectlCli(dls_namespace)


@pytest.fixture(scope="module")
def dls_target(
    dls_kubectl: KubectlCli, apps_dir: Path, admission_policies: None
) -> str:
    """The DLS-alike IOC, running.

    One copy per node, and no more: the pod is ``hostNetwork`` and a second copy
    binds the same Channel Access ports without failing (see the manifest).
    """
    dls_kubectl.apply(apps_dir / "dls-ioc.yaml")
    dls_kubectl.wait(f"pod/{TARGET_POD}", "condition=Ready", timeout=300)
    return TARGET_POD


@pytest.fixture(scope="module")
def classic_ptrace(dls_kubectl: KubectlCli, dls_target: str) -> None:
    """Skip the module unless this node's Yama allows a capless same-uid attach.

    Checked before the attach rather than left to the probe, because the two
    outcomes look identical downstream and only one of them is a defect: at
    ``ptrace_scope=1`` a seat with no CAP_SYS_PTRACE *correctly* cannot attach,
    every assertion below would fail, and the reader of the log would be looking
    at podbench for a fault in the runner's sysctl. Ubuntu and Debian ship 1, so
    this is the common case for a contributor and for the main e2e job.

    A node with no Yama at all — the file is absent unless CONFIG_SECURITY_YAMA
    is built in — is the permissive case, not an unknown one, and runs.
    """
    read = dls_kubectl.exec(
        TARGET_POD,
        ["sh", "-c", f"cat {YAMA_SYSCTL} 2>/dev/null || echo absent"],
        container=TARGET_CONTAINER,
        check=False,
    )
    scope = read.stdout.strip()
    if scope not in ("0", "absent"):
        pytest.skip(
            f"{YAMA_SYSCTL} is {scope or 'unreadable'} on this node, so a seat "
            "with no CAP_SYS_PTRACE genuinely cannot attach and #89 — a seat "
            "that can attach being told it cannot — does not arise. Run with "
            "`sudo sysctl -w kernel.yama.ptrace_scope=0`, or read this module's "
            "row in tests/e2e/README.md"
        )


@pytest.fixture(scope="module")
def dls_seat(
    dls_namespace: str,
    dls_target: str,
    classic_ptrace: None,
    podbench_image: str,
    ssh_identity: tuple[Path, str],
    tmp_path_factory: pytest.TempPathFactory,
) -> str:
    """One ``podbench attach``, shared: its report, as it was printed.

    Shared because an ephemeral container's name is burnt for the pod's
    lifetime, so a second attach would be a second seat rather than a repeat.
    """
    identity, _ = ssh_identity
    cli = PodbenchCli(
        dls_namespace, config_dir=tmp_path_factory.mktemp("podbench-dls-client")
    )
    result = cli.run(
        "attach",
        TARGET_POD,
        "--target",
        TARGET_CONTAINER,
        "--image",
        podbench_image,
        "--identity",
        str(identity),
        "--print-config",
        *cli.common(),
        timeout=420,
    )
    return result.stdout


@pytest.fixture(scope="module")
def app_pid(dls_kubectl: KubectlCli, dls_seat: str) -> int:
    """The IOC's pid, discovered from the seat rather than assumed to be 1."""
    listing = dls_kubectl.exec(
        TARGET_POD,
        ["podbench", "pids", "--targets", "--json"],
        container=FIRST_SEAT,
    )
    payload: Any = json.loads(listing.stdout)
    processes = cast(list[Any], cast(dict[str, Any], payload)["processes"])
    matched = [
        cast(dict[str, Any], entry)
        for entry in processes
        if APP_CMDLINE_MARKER in str(cast(dict[str, Any], entry).get("cmdline") or "")
    ]
    assert len(matched) == 1, (
        f"expected exactly one process whose cmdline names {APP_CMDLINE_MARKER}, "
        f"got {[e.get('cmdline') for e in matched]} out of {listing.stdout}"
    )
    return int(matched[0]["pid"])


@pytest.fixture(scope="module")
def capreport(dls_kubectl: KubectlCli, app_pid: int) -> str:
    """``podbench capreport <pid>``, run in the seat. The measurement of record."""
    return dls_kubectl.exec(
        TARGET_POD,
        ["podbench", "capreport", str(app_pid)],
        container=FIRST_SEAT,
        check=False,
        timeout=300,
    ).stdout


@pytest.fixture(scope="module")
def provisioned(
    dls_kubectl: KubectlCli, app_pid: int, capreport: str
) -> subprocess.CompletedProcess[str]:
    """One ``debug-config --provision``, shared by the two tests that read it.

    Shared because it mutates the workload — ~15 MB of debugpy into the
    target's rootfs — and running it per test would install it twice and make
    the second run's answer depend on the first.
    """
    return dls_kubectl.exec(
        TARGET_POD,
        ["podbench", "debug-config", str(app_pid), "--provision", "--print-config"],
        container=FIRST_SEAT,
        check=False,
        timeout=420,
    )


def _measured(capreport: str) -> str:
    """The lines of a capreport that decide every assertion here.

    Quoted into every failure message, because the whole complaint is that two
    numbers disagree and the reader of a CI log a year from now has only what
    the message carried. The two section headings are kept: ``uid`` appears
    under both ``TRACER`` and ``TARGET``, and without them the extract reads as
    the same fact twice.
    """
    wanted = (
        "TRACER",
        "TARGET (",
        "uid",
        "CAP_SYS_PTRACE",
        "Yama",
        "live attach",
        "VERDICT:",
        "BLOCKER:",
    )
    return "\n".join(
        line for line in capreport.splitlines() if any(w in line for w in wanted)
    )


def _live_attach(capreport: str, app_pid: int) -> bool:
    """Whether the probe attached to ``app_pid``, whichever primitive it used."""
    value = _probe_line(capreport, f"live attach (pid {app_pid})")
    return value is not None and value.startswith("OK")


def _probe_line(capreport: str, key: str) -> str | None:
    """A capreport row's value, or ``None`` when the row is not there.

    Matched on the key rather than on the whole rendered line: the value is
    padded to a fixed column, so a three-digit pid moves it and a test that
    spelled the spacing out would fail on a busier pod for a reason that has
    nothing to do with #89.
    """
    for line in capreport.splitlines():
        stripped = line.strip()
        if stripped.startswith(key):
            return stripped[len(key) :].strip()
    return None


# -- the state under test, which must hold before the rest means anything ----


def test_the_seat_lands_capless_and_attaches_anyway(
    dls_kubectl: KubectlCli, dls_seat: str, app_pid: int, capreport: str
) -> None:
    """The field state of #89, reproduced: no capability, and live attach works.

    The control, and the only test here that does not read a verb's output.
    Every other test asserts that podbench describes *this* seat correctly, and
    each would pass for the wrong reason against a seat that quietly kept its
    capability — a policy that stopped matching, an expression that errored open
    — so the state itself is asserted first, from the pod spec and from the
    probe rather than from anything that could be reasoned about.
    """
    assert app_pid != 1, (
        "the point of this fixture is that the app is not PID 1 — it sits "
        f"behind stdio-expose and pptty. Got pid {app_pid}, so the chain did "
        f"not come up:\n{dls_seat}"
    )

    seat_spec = dls_kubectl.json("get", "pod", TARGET_POD)
    containers = cast(
        list[Any], cast(dict[str, Any], seat_spec["spec"])["ephemeralContainers"]
    )
    added = [
        cast(dict[str, Any], c)
        .get("securityContext", {})
        .get("capabilities", {})
        .get("add")
        for c in containers
    ]
    assert not any(added), (
        "the mutating policy did not strip SYS_PTRACE, so this seat is an "
        f"ordinary full-rung seat and proves nothing about #89: {added}"
    )

    capability = _probe_line(capreport, "CAP_SYS_PTRACE (eff)")
    assert capability is not None and capability.startswith("no"), (
        "the seat was expected to have no CAP_SYS_PTRACE in its effective set, "
        f"so that everything below measures a capless seat:\n{_measured(capreport)}"
    )
    # `startswith`, because the row now names the primitive it used - "OK via
    # PTRACE_SEIZE" - and which one that is belongs to the pause line, not here.
    assert _live_attach(capreport, app_pid), (
        "the probe could not attach, so this node cannot reproduce #89 — it "
        "needs kernel.yama.ptrace_scope=0 and a seat sharing the target's "
        f"uid:\n{_measured(capreport)}"
    )
    assert LIVE_ATTACH_VERDICT in capreport and NO_BLOCKER in capreport, (
        f"the verdict disagrees with the probe beside it:\n{_measured(capreport)}"
    )


# -- #89 itself --------------------------------------------------------------


def _require_live_attach(capreport: str, app_pid: int) -> None:
    """Restate the precondition inside each test that depends on it.

    Duplicated from the control test on purpose: these three fail on their own
    in a CI log, and "podbench claimed X" is only a defect once the reader can
    see, in the same message, that the probe measured otherwise.
    """
    # `startswith`, because the row now names the primitive it used - "OK via
    # PTRACE_SEIZE" - and which one that is belongs to the pause line, not here.
    assert _live_attach(capreport, app_pid), (
        "this run measured no live attach, so nothing below is a defect. The "
        f"node was checked for {YAMA_SYSCTL} = 0 before the seat was attached, "
        "so what changed under the fixture is the seat's uid, the target's, or "
        f"a seccomp/LSM policy on the node:\n{_measured(capreport)}"
    )


def test_provision_is_not_refused_for_a_capability_the_probe_did_not_need(
    provisioned: subprocess.CompletedProcess[str], capreport: str, app_pid: int
) -> None:
    """``debug-config --provision`` must answer from the probe, not from CapEff.

    The injection is ``gdb --pid``, which is the same ptrace the probe just
    made from the same seat to the same pid. Refusing it because a capability
    bit is clear tells the user to "relaunch on the full rung" — which, in a
    namespace whose policy engine strips SYS_PTRACE, is a relaunch that lands
    this same seat again.

    Both sentences are checked, because they are two sites: the flavour is
    withdrawn in ``flavour.py`` and the ``--provision`` note is printed from
    ``vscode.py``, and fixing one alone leaves the other lying in the same run.
    """
    _require_live_attach(capreport, app_pid)
    output = provisioned.stdout + provisioned.stderr
    assert CAP_REFUSAL not in output, (
        "debug-config withdrew debugpy for want of CAP_SYS_PTRACE, from a seat "
        f"that had just ptraced pid {app_pid} successfully.\n"
        f"measured, in this seat, seconds earlier:\n{_measured(capreport)}\n"
        f"claimed, by debug-config:\n{output.strip()}"
    )
    assert PROVISION_REFUSAL not in output, (
        "--provision said the injection cannot be driven from this seat, in the "
        f"same run in which it ptraced pid {app_pid}.\n"
        f"measured:\n{_measured(capreport)}\nclaimed:\n{output.strip()}"
    )


def test_debug_config_emits_a_configuration_or_withholds_one_and_says_so(
    provisioned: subprocess.CompletedProcess[str], capreport: str, app_pid: int
) -> None:
    """A seat that can attach gets a launch.json, or an explicit refusal to write one.

    Separate from the refusal above because they fail independently: the
    capability gate is one of the prerequisites the debugpy flavour is withdrawn
    on, and a run that emitted nothing for one of the others would be a
    different defect with the same symptom.

    **The injection is allowed to fail here.** Issue #225 records that gdb's
    ``DoAttach`` refuses intermittently on this fixture, upstream of anything
    podbench decides and while ``capreport`` on the same seat and pid measures
    the attach as available. What is *not* allowed is authoring a configuration
    naming a port no server ever answered, which is issue #218: since that fix a
    run whose handshake failed withdraws the flavour, keeps whatever
    ``launch.json`` already held, and exits non-zero.

    So there are exactly two acceptable outcomes, and asserting only the happy
    one turns an upstream flake into a red job while asserting only the sad one
    would let a real withdrawal pass unnoticed. Both are pinned.
    """
    _require_live_attach(capreport, app_pid)
    output = provisioned.stdout + provisioned.stderr

    if INJECTION_WITHHELD in output:
        # The one thing #218 forbids: the port that failed must not reach the
        # emitted document. Read it back out of the seat's own sentence rather
        # than assuming stdout is empty - another candidate may legitimately
        # have emitted a configuration of its own in the same run.
        refused = re.search(r"no debug session could be started on \S+?:(\d+)", output)
        assert refused, (
            "debug-config said it withheld a configuration but did not name the "
            f"port it withheld it for, so #218's contract cannot be checked.\n"
            f"said:\n{output.strip()}"
        )
        port = refused.group(1)
        assert port not in provisioned.stdout, (
            f"debug-config withheld its configuration for port {port} and then "
            "emitted that port anyway - a launch.json naming a port nothing "
            f"answered is exactly #218.\nemitted:\n{provisioned.stdout.strip()}"
        )
        return

    assert provisioned.returncode == 0, (
        f"debug-config exited {provisioned.returncode} for a target that "
        f"capreport says is live-attachable, and did not say it was withholding "
        f"a configuration.\nmeasured:\n{_measured(capreport)}\n"
        f"printed:\n{output.strip()}"
    )
    assert provisioned.stdout.strip(), (
        "debug-config --print-config emitted no configuration at all for pid "
        f"{app_pid}, whose live attach was measured OK from this seat, and gave "
        f"no reason for withholding one.\n"
        f"measured:\n{_measured(capreport)}\n"
        f"said instead:\n{provisioned.stderr.strip()}"
    )


def test_status_does_not_call_a_seat_read_only_when_it_live_attached(
    dls_namespace: str,
    dls_target: str,
    dls_seat: str,
    capreport: str,
    app_pid: int,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """``status``'s verdict column must not contradict the probe.

    The column was ``Rung.description``, a static string per rung, and the rung
    is read back off the landed container — where a stripped ``capabilities.add``
    beside ``runAsUser: 0`` is indistinguishable from the degraded rung
    (``launcher.py::seat_layout`` says so, citing the same cluster). So the one
    line a user checks a day later said the opposite of what the seat can do.
    That prose is gone outright: a rung has no description of its own any more,
    and ``attach`` cites the seat's own ``/proc/self/status`` instead.
    """
    _require_live_attach(capreport, app_pid)
    cli = PodbenchCli(
        dls_namespace, config_dir=tmp_path_factory.mktemp("podbench-dls-status")
    )
    result = cli.run("status", TARGET_POD, *cli.common(), timeout=300)
    # Collapsed before matching: the row is wider than a piped terminal and a
    # wrap must not turn a failing assertion into a passing one.
    printed = " ".join(result.stdout.split())

    assert FIRST_SEAT in printed, (
        f"status listed no seat for {TARGET_POD}, so it was not asked about the "
        f"one under test:\n{result.stdout}"
    )
    assert "all capabilities dropped" not in printed, (
        f"status describes {FIRST_SEAT} by the securityContext it reads back "
        "with — a verdict derived from the rung, not from the probe.\n"
        f"measured, in that seat:\n{_measured(capreport)}\n"
        f"claimed, by status:\n{result.stdout.strip()}"
    )
    assert "read-only inspection" not in printed, (
        f"status calls a seat that live-attached to pid {app_pid} a read-only "
        f"one.\nmeasured:\n{_measured(capreport)}\nclaimed:\n{result.stdout.strip()}"
    )
