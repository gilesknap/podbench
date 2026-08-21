"""Tests for Iterate mode.

Nothing here touches a cluster or a real process: ``ss`` output is captured
text, ``/proc`` is a directory tree built in ``tmp_path``, and the launcher's
subprocess seam is a fake runner that records what it was asked to do.

The captured ``ss`` fixtures are the shapes the phase-0 report warns about — a
listener owned by another container, and a port that is free to ``ss -lntp``
while a ``TIME_WAIT`` socket makes it unbindable.
"""

from __future__ import annotations

import copy
import json
import signal
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from podbench import dev, launcher, model, spec
from podbench.kubectl import CommandResult, Kubectl

# ``ss -lntpe`` on the sidecar: three listeners, one of them the app we are
# about to replace. Note the extended fields appearing after users:(...), whose
# order is not contractual, and the [::] form of an IPv6 wildcard.
SS_LISTENERS = (
    "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
    "LISTEN 0      4096         0.0.0.0:8080       0.0.0.0:*    "
    'users:(("python3",pid=612,fd=3)) ino:35876 sk:1001 '
    "cgroup:/../cri-containerd-87d2.scope v6only:0 <->\n"
    "LISTEN 0      128          0.0.0.0:22         0.0.0.0:*    "
    'users:(("sshd",pid=44,fd=3)) ino:12001 sk:2 <->\n'
    "LISTEN 0      4096            [::]:9100          [::]:*    "
    'users:(("node_exporter",pid=910,fd=7)) ino:9001 sk:3 <->\n'
)

# ``ss -tan state time-wait``: filtering by state makes ss drop the State column
# entirely, which is why the parser has to be told what it is reading.
SS_TIME_WAIT = """\
Recv-Q Send-Q Local Address:Port Peer Address:Port
0      0            10.42.0.9:8080     10.42.0.1:51422
0      0            10.42.0.9:8080     10.42.0.1:51424
0      0            10.42.0.9:9100     10.42.0.1:51999
"""

SS_EMPTY = """\
State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process
"""


class FakeRunner:
    """Answers ``ss`` invocations from a script, and records every call."""

    def __init__(self, **canned: str) -> None:
        self.canned = canned
        self.calls: list[tuple[str, ...]] = []
        self.returncode = 0

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(argv))
        key = "listeners" if "-lntpe" in argv else "time_wait"
        return CommandResult(
            argv=tuple(argv),
            returncode=self.returncode,
            stdout=self.canned.get(key, ""),
            stderr="",
        )


def fixed_spawner(pid: int) -> dev.Spawner:
    """A :class:`dev.Spawner` that pretends to have started ``pid``."""

    def spawn(argv: Sequence[str], *, cwd: str, log: str) -> int:
        return pid

    return spawn


def null_runner(
    argv: Sequence[str],
    *,
    stdin: str | None = None,
    capture: bool = True,
    timeout: float | None = None,
) -> CommandResult:
    """A runner that must never be reached."""
    raise AssertionError(f"nothing should have run: {list(argv)}")


def always(kube: Kubectl) -> Callable[..., Kubectl]:
    """A stand-in for :func:`podbench.launcher.kubectl_for`.

    It is the namespace resolution that has to be stubbed, not just the client:
    with ``-n`` unset the real helper asks ``kubectl config view`` for the
    context's namespace, and a unit test may not shell out to a cluster.
    """

    def make(*args: Any, **kwargs: Any) -> Kubectl:
        return kube

    return make


def make_proc(
    root: Path,
    *,
    pids: dict[int, dict[str, Any]] | None = None,
    self_root: Path | None = None,
) -> Path:
    """Build a synthetic ``/proc``.

    Each pid entry may carry ``state``, ``start_ticks``, ``comm``, ``cmdline``,
    ``cgroup``, ``fds`` (inode list) and ``root`` (a directory standing in for
    that process's mount namespace root).
    """
    proc = root / "proc"
    proc.mkdir(exist_ok=True)
    own_root = self_root if self_root is not None else root / "rootfs-debug"
    own_root.mkdir(parents=True, exist_ok=True)
    (proc / "self").mkdir(exist_ok=True)
    (proc / "self" / "root").symlink_to(own_root)

    for pid, attrs in (pids or {}).items():
        entry = proc / str(pid)
        entry.mkdir(exist_ok=True)
        state = attrs.get("state", "S")
        ticks = attrs.get("start_ticks", 4242)
        # Fields 3.. of /proc/<pid>/stat; comm deliberately contains a space and
        # a bracket, which is what breaks naive positional parsing.
        rest = [str(state)] + ["0"] * 18 + [str(ticks)]
        comm = attrs.get("comm", "python3")
        (entry / "stat").write_text(f"{pid} (my app) {' '.join(rest)}\n")
        (entry / "comm").write_text(f"{comm}\n")
        (entry / "cmdline").write_text(attrs.get("cmdline", "python3\x00-m\x00app\x00"))
        (entry / "cgroup").write_text(attrs.get("cgroup", "0::/\n"))
        fd_dir = entry / "fd"
        fd_dir.mkdir(exist_ok=True)
        for index, inode in enumerate(attrs.get("fds", [])):
            (fd_dir / str(index)).symlink_to(f"socket:[{inode}]")
        process_root = attrs.get("root")
        (entry / "root").symlink_to(process_root if process_root else own_root)
    return proc


# -- ss parsing ------------------------------------------------------------


def test_parse_ss_reads_state_pid_and_inode():
    entries = dev.parse_ss(SS_LISTENERS)
    assert len(entries) == 3
    first = entries[0]
    assert first.state == "LISTEN"
    assert first.port == 8080
    assert first.pids == (612,)
    assert first.processes == ("python3",)
    assert first.inode == 35876


def test_parse_ss_handles_ipv6_bracket_form():
    entries = dev.parse_ss(SS_LISTENERS)
    assert [entry.port for entry in entries] == [8080, 22, 9100]


def test_parse_ss_accepts_a_default_state_when_ss_omits_the_column():
    entries = dev.parse_ss(SS_TIME_WAIT, default_state="TIME-WAIT")
    assert len(entries) == 3
    assert {entry.state for entry in entries} == {"TIME-WAIT"}
    assert dev.time_wait_count(entries, 8080) == 2
    assert dev.time_wait_count(entries, 9100) == 1
    assert dev.time_wait_count(entries, 8081) == 0


def test_parse_ss_ignores_headers_and_junk():
    assert dev.parse_ss(SS_EMPTY) == []
    assert dev.parse_ss("total garbage\n") == []


def test_listeners_on_filters_by_port_and_state():
    entries = dev.parse_ss(SS_TIME_WAIT, default_state="TIME-WAIT")
    assert dev.listeners_on(entries, 8080) == []
    assert len(dev.listeners_on(dev.parse_ss(SS_LISTENERS), 8080)) == 1


# -- the port pre-flight ---------------------------------------------------


def test_preflight_reports_the_owning_container(tmp_path: Path):
    other = tmp_path / "rootfs-app"
    other.mkdir()
    proc = make_proc(
        tmp_path,
        pids={
            612: {
                "cgroup": "0::/../cri-containerd-87d2.scope\n",
                "cmdline": "python3\x00-m\x00app\x00",
                "root": other,
            }
        },
    )
    runner = FakeRunner(listeners=SS_LISTENERS, time_wait=SS_TIME_WAIT)

    result = dev.preflight_port(
        8080, runner=runner, proc=proc, target_cid="cri-containerd-87d2"
    )

    assert not result.clear
    assert result.time_wait == 2
    assert result.owners[0].is_target
    assert result.owners[0].same_container is False
    assert "the target container" in result.message()
    assert "SO_REUSEPORT" in result.message()


def test_preflight_warns_about_time_wait_on_an_otherwise_free_port(tmp_path: Path):
    proc = make_proc(tmp_path)
    runner = FakeRunner(listeners=SS_EMPTY, time_wait=SS_TIME_WAIT)

    result = dev.preflight_port(8080, runner=runner, proc=proc)

    assert result.clear
    assert result.time_wait == 2
    # The user is told to wait rather than left staring at EADDRINUSE.
    assert "TIME_WAIT" in result.message()
    assert "Waiting is the fix" in result.message()


def test_preflight_runs_a_second_command_because_ss_l_cannot_see_time_wait(
    tmp_path: Path,
):
    runner = FakeRunner(listeners=SS_EMPTY, time_wait=SS_TIME_WAIT)
    dev.preflight_port(8080, runner=runner, proc=make_proc(tmp_path))
    assert runner.calls == [
        tuple(dev.LISTENERS_COMMAND),
        tuple(dev.TIME_WAIT_COMMAND),
    ]


def test_preflight_fails_closed_when_ss_cannot_be_read(tmp_path: Path):
    runner = FakeRunner(listeners="")
    runner.returncode = 127

    result = dev.preflight_port(8080, runner=runner, proc=make_proc(tmp_path))

    assert not result.clear
    assert "pre-flight failed" in result.message()


# -- socket ownership ------------------------------------------------------


def test_socket_inodes_reads_the_fd_symlinks(tmp_path: Path):
    proc = make_proc(tmp_path, pids={612: {"fds": [35876, 41]}})
    assert dev.socket_inodes(612, proc=proc) == {35876, 41}


def test_verify_listener_passes_when_our_pid_holds_the_socket_inode(tmp_path: Path):
    proc = make_proc(tmp_path, pids={612: {"fds": [35876]}})
    runner = FakeRunner(listeners=SS_LISTENERS)

    check = dev.verify_listener(612, 8080, runner=runner, proc=proc)

    assert check.ok
    assert "owns the listening socket" in check.detail


def test_verify_listener_fails_when_the_inode_is_not_open_by_that_pid(tmp_path: Path):
    # ss says pid 612, but 612 holds no such socket: the attribution is stale
    # and something else is answering. A port poll alone would have passed here.
    proc = make_proc(tmp_path, pids={612: {"fds": [999]}})
    runner = FakeRunner(listeners=SS_LISTENERS)

    check = dev.verify_listener(612, 8080, runner=runner, proc=proc)

    assert not check.ok
    assert "not open in" in check.detail


def test_verify_listener_names_the_other_process_when_the_port_was_stolen(
    tmp_path: Path,
):
    proc = make_proc(tmp_path, pids={612: {"cmdline": "python3\x00-m\x00app\x00"}})
    runner = FakeRunner(listeners=SS_LISTENERS)

    check = dev.verify_listener(700, 8080, runner=runner, proc=proc)

    assert not check.ok
    assert "pid 612" in check.detail
    assert "SO_REUSEPORT" in check.detail


def test_verify_listener_fails_when_nothing_is_listening(tmp_path: Path):
    check = dev.verify_listener(
        612, 8080, runner=FakeRunner(listeners=SS_EMPTY), proc=make_proc(tmp_path)
    )
    assert not check.ok
    assert "nothing is listening" in check.detail


# -- process identity ------------------------------------------------------


def test_read_process_survives_a_comm_containing_spaces_and_brackets(tmp_path: Path):
    proc = make_proc(tmp_path, pids={612: {"state": "S", "start_ticks": 90210}})
    snapshot = dev.read_process(612, proc=proc)
    assert snapshot is not None
    assert snapshot.state == "S"
    assert snapshot.start_ticks == 90210
    assert snapshot.alive


def test_a_zombie_is_not_alive(tmp_path: Path):
    # Under shareProcessNamespace pid 1 is the app and reaps nothing, so an
    # orphaned child stays a zombie forever — and answers kill -0.
    proc = make_proc(tmp_path, pids={612: {"state": "Z"}})
    snapshot = dev.read_process(612, proc=proc)
    assert snapshot is not None
    assert not snapshot.alive


def test_state_is_not_live_when_the_pid_was_recycled(tmp_path: Path):
    proc = make_proc(tmp_path, pids={612: {"start_ticks": 5}})
    state = dev.RunState(
        pid=612,
        port=8080,
        command=("python3", "-m", "app"),
        cwd="/workspace/src",
        log="/workspace/.podbench/run.log",
        started_at=0.0,
        start_ticks=4,
    )
    assert not state.is_live(proc=proc)


def test_run_state_round_trips_and_tolerates_corruption():
    state = dev.RunState(
        pid=1,
        port=8080,
        command=("python3", "-m", "app"),
        cwd="/workspace/src",
        log="/workspace/.podbench/run.log",
        started_at=1.5,
        start_ticks=7,
    )
    assert dev.RunState.from_json(state.to_json()) == state
    assert dev.RunState.from_json("{not json") is None
    assert dev.RunState.from_json('{"pid": "nope"}') is None


# -- the relaunch loop -----------------------------------------------------


def test_start_verifies_its_own_child_owns_the_socket(tmp_path: Path):
    proc = make_proc(tmp_path, pids={612: {"fds": [35876]}})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner(listeners=SS_EMPTY, time_wait=SS_EMPTY)
    spawned: list[tuple[tuple[str, ...], str]] = []

    def spawner(argv: Sequence[str], *, cwd: str, log: str) -> int:
        spawned.append((tuple(argv), cwd))
        # Once the child is up, ss reports it.
        runner.canned["listeners"] = SS_LISTENERS
        return 612

    result = dev.start(
        ["python3", "-m", "app"],
        port=8080,
        workspace=str(workspace),
        runner=runner,
        spawner=spawner,
        proc=proc,
        sleep=lambda _: None,
    )

    assert result.ok, result.detail
    assert spawned == [(("python3", "-m", "app"), str(workspace))]
    assert result.state is not None
    assert result.state.pid == 612
    # Recorded on disk before verification, so a child that never binds is
    # still killable by `podbench stop`.
    assert dev.load_state(str(workspace)) == result.state


def test_start_refuses_when_the_port_is_already_bound(tmp_path: Path):
    proc = make_proc(tmp_path, pids={612: {}})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner(listeners=SS_LISTENERS, time_wait=SS_EMPTY)

    def spawner(argv: Sequence[str], *, cwd: str, log: str) -> int:
        raise AssertionError("must not launch into an occupied port")

    result = dev.start(
        ["python3", "-m", "app"],
        port=8080,
        workspace=str(workspace),
        runner=runner,
        spawner=spawner,
        proc=proc,
        sleep=lambda _: None,
    )

    assert not result.ok
    assert "already bound" in result.detail


def test_start_reports_a_child_that_died_instead_of_a_false_pass(tmp_path: Path):
    # The failure S4 caught: the relaunch died with EADDRINUSE while a socket
    # poll printed LISTENING and exited 0.
    proc = make_proc(tmp_path)  # no pid 612 at all: the child is gone
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log = workspace / "run.log"
    log.write_text("OSError: [Errno 98] Address already in use\n")
    runner = FakeRunner(listeners=SS_EMPTY, time_wait=SS_EMPTY)

    result = dev.start(
        ["python3", "-m", "app"],
        port=8080,
        workspace=str(workspace),
        log=str(log),
        runner=runner,
        spawner=fixed_spawner(612),
        proc=proc,
        sleep=lambda _: None,
    )

    assert not result.ok
    assert "exited before it served port 8080" in result.detail
    assert "Address already in use" in result.detail


def test_start_stops_the_previous_child_first(tmp_path: Path):
    proc = make_proc(tmp_path, pids={500: {"start_ticks": 11}, 612: {"fds": [35876]}})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    previous = dev.RunState(
        pid=500,
        port=8080,
        command=("python3", "-m", "app"),
        cwd=str(workspace),
        log=str(workspace / "run.log"),
        started_at=0.0,
        start_ticks=11,
    )
    dev.save_state(previous, str(workspace))
    runner = FakeRunner(listeners=SS_EMPTY, time_wait=SS_EMPTY)
    signals: list[tuple[int, int]] = []

    def killer(pid: int, sig: int) -> None:
        signals.append((pid, sig))
        # SIGTERM lands: the process leaves the synthetic /proc.
        (proc / str(pid) / "stat").unlink()

    def spawner(argv: Sequence[str], *, cwd: str, log: str) -> int:
        runner.canned["listeners"] = SS_LISTENERS
        return 612

    result = dev.start(
        ["python3", "-m", "app"],
        port=8080,
        workspace=str(workspace),
        runner=runner,
        spawner=spawner,
        proc=proc,
        killer=killer,
        sleep=lambda _: None,
    )

    assert result.ok, result.detail
    assert signals == [(500, signal.SIGTERM)]


def test_stop_kills_the_recorded_pid_and_escalates(tmp_path: Path):
    proc = make_proc(tmp_path, pids={500: {"start_ticks": 11}})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dev.save_state(
        dev.RunState(
            pid=500,
            port=8080,
            command=("python3",),
            cwd=str(workspace),
            log="",
            started_at=0.0,
            start_ticks=11,
        ),
        str(workspace),
    )
    signals: list[tuple[int, int]] = []
    clock = iter([0.0, 0.1, 0.2, 9.0, 9.1])

    result = dev.stop(
        workspace=str(workspace),
        proc=proc,
        killer=lambda pid, sig: signals.append((pid, sig)),
        grace=1.0,
        sleep=lambda _: None,
        now=lambda: next(clock),
    )

    assert result.ok
    assert signals == [(500, signal.SIGTERM), (500, signal.SIGKILL)]
    assert dev.load_state(str(workspace)) is None


def test_stop_is_a_no_op_without_a_recorded_pid(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = dev.stop(
        workspace=str(workspace),
        proc=make_proc(tmp_path),
        killer=lambda pid, sig: pytest.fail("nothing should be signalled"),
    )
    assert result.ok
    assert "nothing recorded" in result.detail


# -- never pkill -f --------------------------------------------------------


def _generated_commands() -> list[list[str]]:
    plan = dev.BootstrapPlan(
        repo="https://example.invalid/app.git",
        checkout="/workspace/src",
        ref="main",
        python="3.12",
    )
    return [
        list(dev.LISTENERS_COMMAND),
        list(dev.TIME_WAIT_COMMAND),
        *dev.bootstrap_commands(plan, checkout_exists=False),
        *dev.bootstrap_commands(plan, checkout_exists=True),
    ]


def test_no_generated_command_ever_uses_pkill():
    # Under shareProcessNamespace: true, `pkill -f` matches the invoking shell
    # and every other container's processes. Killing is by recorded pid only.
    flat = " ".join(" ".join(argv) for argv in _generated_commands())
    assert "pkill" not in flat
    assert "killall" not in flat


def test_the_module_contains_no_pkill_anywhere():
    source = Path(dev.__file__).read_text()
    assert "pkill -f" not in source.replace("``pkill -f``", "")


def test_stop_signals_by_pid_not_by_pattern(tmp_path: Path):
    proc = make_proc(tmp_path, pids={500: {"start_ticks": 11}})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dev.save_state(
        dev.RunState(500, 8080, ("python3",), str(workspace), "", 0.0, 11),
        str(workspace),
    )
    seen: list[int] = []

    def killer(pid: int, sig: int) -> None:
        seen.append(pid)
        (proc / str(pid) / "stat").unlink()

    dev.stop(
        workspace=str(workspace),
        proc=proc,
        killer=killer,
        sleep=lambda _: None,
    )
    assert seen == [500]


# -- bootstrap -------------------------------------------------------------


REPO = "https://example.invalid/app.git"


def test_bootstrap_clones_syncs_frozen_and_installs_editable():
    plan = dev.BootstrapPlan(repo=REPO, checkout="/workspace/src", ref="v1")
    commands = dev.bootstrap_commands(plan, checkout_exists=False)
    assert commands[0] == ["git", "clone", REPO, "/workspace/src"]
    assert ["uv", "--directory", "/workspace/src", "sync", "--frozen"] in commands
    assert commands[-1] == [
        "uv",
        "--directory",
        "/workspace/src",
        "pip",
        "install",
        "-e",
        ".",
    ]


def test_bootstrap_updates_an_existing_checkout_instead_of_recloning():
    plan = dev.BootstrapPlan(repo=REPO, checkout="/workspace/src")
    commands = dev.bootstrap_commands(plan, checkout_exists=True)
    assert commands[0][:3] == ["git", "-C", "/workspace/src"]
    assert not any("clone" in argv for argv in commands)


def test_bootstrap_never_installs_a_toolchain_at_runtime():
    # apt-get was 10.6 s of a 19 s loop (55 %), which is why the image bakes it.
    plan = dev.BootstrapPlan(repo=REPO, checkout="/workspace/src", python="3.12")
    for argv in dev.bootstrap_commands(plan, checkout_exists=False):
        assert argv[0] in ("git", "uv")


def test_bootstrap_runs_every_command_and_stops_on_failure():
    calls: list[tuple[str, ...]] = []

    def runner(
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        calls.append(tuple(argv))
        code = 1 if "sync" in argv else 0
        return CommandResult(tuple(argv), code, "", "no uv.lock")

    plan = dev.BootstrapPlan(repo=REPO, checkout="/workspace/src")
    with pytest.raises(dev.DevError, match="no uv.lock"):
        dev.bootstrap(plan, runner=runner, exists=lambda _: False, echo=lambda _: None)
    assert calls[-1][:2] == ("uv", "--directory")


# -- the mount-namespace rule ---------------------------------------------


def test_layout_in_the_debug_container_is_accepted():
    dev.validate_workspace_layout(
        interpreter="/workspace/src/.venv/bin/python",
        venv="/workspace/src/.venv",
        checkout="/workspace/src",
    )


def test_layout_straddling_the_two_namespaces_is_refused():
    with pytest.raises(dev.MountNamespaceError) as excinfo:
        dev.validate_workspace_layout(
            interpreter="/proc/1/root/usr/local/bin/python3",
            venv="/proc/1/root/usr/local/lib/python3.12/site-packages",
            checkout="/workspace/src",
        )
    message = str(excinfo.value)
    assert "interpreter" in message and "venv" in message
    assert "dangles silently" in message


def test_layout_wholly_in_the_target_is_refused_with_the_bridge_reason():
    with pytest.raises(dev.MountNamespaceError, match="one-directional"):
        dev.validate_workspace_layout(
            checkout="/proc/1/root/app", venv="/proc/1/root/app/.venv"
        )


def test_proc_self_root_is_our_own_side():
    assert dev.path_side("/proc/self/root/workspace") is dev.Side.DEBUG
    assert dev.path_side("/proc/thread-self/root/workspace") is dev.Side.DEBUG
    assert dev.path_side("/proc/17/root/app") is dev.Side.TARGET


def test_bootstrap_refuses_a_checkout_in_the_target():
    plan = dev.BootstrapPlan(repo=REPO, checkout="/proc/1/root/app")
    with pytest.raises(dev.MountNamespaceError):
        dev.bootstrap(plan, runner=null_runner)


def test_start_refuses_an_interpreter_in_the_target(tmp_path: Path):
    with pytest.raises(dev.MountNamespaceError):
        dev.start(
            ["/proc/1/root/usr/local/bin/python3", "-m", "app"],
            port=8080,
            workspace=str(tmp_path),
            proc=make_proc(tmp_path),
        )


# -- laptop side -----------------------------------------------------------

ORIGIN_POD: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {
        "name": "demo",
        "namespace": "podbench-test",
        "uid": "abc-123",
        "resourceVersion": "9",
        "labels": {"app": "demo", "pod-template-hash": "77f"},
    },
    "spec": {
        "containers": [
            {
                "name": "app",
                "image": "demo:1",
                "ports": [{"containerPort": 8080}],
                "readinessProbe": {"httpGet": {"path": "/", "port": 8080}},
            }
        ]
    },
    "status": {"phase": "Running"},
}


def named_pod(name: str) -> dict[str, Any]:
    """``ORIGIN_POD`` under another name, deep-copied so the fixture stays put."""
    document = copy.deepcopy(ORIGIN_POD)
    document["metadata"]["name"] = name
    return document


def labelled_dev_pod(name: str) -> dict[str, Any]:
    """A dev pod as podbench recognises one: by its label, never by its name.

    ``--name`` is why — a dev pod called ``mydev`` is a dev pod, and an
    ordinary workload called ``api-podbench`` is not.
    """
    return {
        "metadata": {"name": name, "labels": {spec.DEVPOD_LABEL: "true"}},
        "spec": {"containers": [{"name": "app"}, {"name": "podbench"}]},
    }


def origin_with_identity(**container_context: Any) -> dict[str, Any]:
    """``ORIGIN_POD`` as somebody who deployed the identity ConfigMap wrote it."""
    pod = json.loads(json.dumps(ORIGIN_POD))
    pod["spec"]["volumes"] = [
        {
            "name": model.SEAT_IDENTITY_VOLUME,
            "configMap": {"name": "demo-podbench-identity"},
        }
    ]
    pod["spec"]["containers"][0]["securityContext"] = container_context or {
        "runAsUser": 1000,
        "runAsGroup": 1000,
    }
    return cast(dict[str, Any], pod)


SERVICE: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Service",
    "metadata": {"name": "demo"},
    "spec": {"selector": {"app": "demo"}, "ports": [{"port": 80, "targetPort": 8080}]},
}


HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHostKey podbench"
CLIENT_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIClientKey me@laptop"


class FakeKubectl(Kubectl):
    """A Kubectl whose runner answers from a table of objects.

    ``exec`` is answered too, because the sidecar is now asked two questions
    after it comes up — its login name and its host public key — and both go
    through ``kubectl exec`` exactly as they do for an ``attach`` seat.
    """

    login_user: str | None = "podbench"
    """What the sidecar answers ``--print-login-user`` with; ``None`` for a
    seat that has no NSS identity, which is the case worth a stanza-free
    refusal. Set on the instance, not passed, because ``**objects`` is the pod
    table and a keyword argument here would be ambiguous with it."""

    host_key: str | None = HOST_KEY

    def __init__(self, **objects: dict[str, Any]) -> None:
        self.objects = objects
        self.commands: list[tuple[str, ...]] = []
        super().__init__("podbench-test", runner=self._run)

    def _run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        self.commands.append(tuple(argv))
        # Without the bound issue #118 added to every call, so that the fixed
        # offsets below keep meaning what they meant.
        args = [word for word in argv if not word.startswith("--request-timeout=")]
        stdout = ""
        if args[3:5] == ["get", "pods"]:
            # The namespace listing `resolve_pod` searches when the reference is
            # a substring or absent, as distinct from the `get pod NAME` below.
            stdout = json.dumps(
                {
                    "items": [
                        document
                        for key, document in self.objects.items()
                        if key.startswith("pod/")
                    ]
                }
            )
        elif args[3:5] == ["get", "pod"]:
            stdout = json.dumps(self.objects.get(f"pod/{args[5]}", {}))
            if stdout == "{}":
                return CommandResult(tuple(argv), 1, "", "pods 'x' not found")
        elif args[3:5] == ["get", "service"]:
            stdout = json.dumps(self.objects.get(f"service/{args[5]}", {}))
        elif args[3] == "get":
            # Any other kind, so the ownership walk that looks for a GitOps
            # mark can be answered: it asks for replicasets and deployments.
            stdout = json.dumps(self.objects.get(f"{args[4]}/{args[5]}", {}))
        elif args[3] == "create":
            stdout = stdin or "{}"
        elif args[3] == "exec":
            return self._exec(tuple(argv))
        return CommandResult(tuple(argv), 0, stdout, "")

    def _exec(self, argv: tuple[str, ...]) -> CommandResult:
        if "--print-login-user" in argv:
            if self.login_user is None:
                return CommandResult(argv, 1, "", "/etc/passwd is not writable")
            return CommandResult(argv, 0, self.login_user + "\n", "")
        if "--print-host-key" in argv:
            return CommandResult(argv, 0, (self.host_key or "") + "\n", "")
        return CommandResult(argv, 0, "", "")

    def subcommands(self) -> list[str]:
        """The kubectl verb of every call made, in order."""
        return [
            [word for word in argv if not word.startswith("--request-timeout=")][3]
            for argv in self.commands
        ]

    def patches(self) -> list[list[dict[str, Any]]]:
        """Every JSON patch body sent, decoded."""
        bodies: list[list[dict[str, Any]]] = []
        for argv in self.commands:
            if "patch" in argv:
                bodies.append(json.loads(argv[argv.index("-p") + 1]))
        return bodies


def test_dev_pod_name_is_derived_and_idempotent():
    assert dev.dev_pod_name("demo") == "demo-podbench"
    assert dev.dev_pod_name("demo-podbench") == "demo-podbench"
    assert len(dev.dev_pod_name("x" * 80)) <= 63


def test_sole_container_names_the_alternatives():
    with pytest.raises(dev.DevError, match="--container"):
        dev.sole_container(
            {"spec": {"containers": [{"name": "app"}, {"name": "sidecar"}]}}
        )
    assert dev.sole_container(ORIGIN_POD) == "app"


def test_default_target_port_reads_the_container_port():
    assert dev.default_target_port(ORIGIN_POD, "app") == 8080
    assert dev.default_target_port(ORIGIN_POD, "nope") is None


def test_dev_pod_takes_no_traffic_by_default():
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD})
    pod, manifest = dev.create_dev_pod(kube, "demo", image="img:1")

    assert pod.ref.name == "demo-podbench"
    assert manifest["metadata"]["labels"] == {spec.DEVPOD_LABEL: "true"}
    assert "app" not in manifest["metadata"]["labels"]
    assert not any("patch" in argv for argv in kube.commands)
    assert "receives no traffic" in dev.connection_summary(pod)


def test_a_prepared_origin_gets_a_seat_with_the_identity_it_declared():
    """The volume is the request; the sidecar is the only container that can use it."""
    kube = FakeKubectl(**{"pod/demo": origin_with_identity()})
    pod, manifest = dev.create_dev_pod(kube, "demo", image="img:1")

    assert pod.seat_identity == (1000, 1000)
    sidecar = next(c for c in manifest["spec"]["containers"] if c["name"] == "podbench")
    assert sidecar["securityContext"]["runAsUser"] == 1000
    assert [
        mount["mountPath"]
        for mount in sidecar["volumeMounts"]
        if mount["name"] == model.SEAT_IDENTITY_VOLUME
    ] == ["/etc/passwd", "/etc/group"]

    summary = dev.connection_summary(pod)
    assert "seat identity" in summary
    assert "1000:1000" in summary


def test_a_declared_identity_the_sidecar_cannot_use_is_said_out_loud(
    capsys: pytest.CaptureFixture[str],
):
    """Prepared and ignored is the shape that looks like a podbench bug.

    Without a uid in the manifest podbench cannot know who the record was
    written for, so it authors the root sidecar it always did — and says so,
    because the user did put the volume there on purpose.
    """
    kube = FakeKubectl(**{"pod/demo": origin_with_identity(runAsNonRoot=True)})
    pod, manifest = dev.create_dev_pod(kube, "demo", image="img:1")

    assert pod.seat_identity is None
    note = capsys.readouterr().err
    assert model.SEAT_IDENTITY_VOLUME in note
    assert "runs as root" in note
    sidecar = next(c for c in manifest["spec"]["containers"] if c["name"] == "podbench")
    assert sidecar["securityContext"]["runAsUser"] == 0
    assert model.SEAT_IDENTITY_VOLUME not in dev.connection_summary(pod)


def test_an_ordinary_origin_says_nothing_about_identity(
    capsys: pytest.CaptureFixture[str],
):
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD})
    pod, _ = dev.create_dev_pod(kube, "demo", image="img:1")
    assert pod.seat_identity is None
    assert model.SEAT_IDENTITY_VOLUME not in capsys.readouterr().err
    assert "seat identity" not in dev.connection_summary(pod)


def test_dev_pod_waits_for_running_not_ready():
    # The sidecar's readiness probe is a tcpSocket on the app port, which only
    # passes once `podbench run` binds it. Waiting for Ready would hang.
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD})
    dev.create_dev_pod(kube, "demo", image="img:1")
    waits = [argv for argv in kube.commands if "wait" in argv]
    assert waits
    assert any("jsonpath={.status.phase}=Running" in arg for arg in waits[0])
    assert not any("condition=Ready" in arg for arg in waits[0])


def test_cutover_replaces_the_selector_and_records_the_original():
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD, "service/demo": SERVICE})
    pod, manifest = dev.create_dev_pod(
        kube, "demo", image="img:1", cutover_service="demo"
    )

    annotations = manifest["metadata"]["annotations"]
    assert annotations[dev.CUTOVER_SERVICE_ANNOTATION] == "demo"
    assert json.loads(annotations[dev.CUTOVER_SELECTOR_ANNOTATION]) == {"app": "demo"}
    assert pod.cutover == dev.Cutover("demo", {"app": "demo"})

    [patch] = kube.patches()
    # A merge patch would union the maps and silently drop the original pod out
    # of the endpointslice, so this has to be a replace.
    assert patch == [
        {
            "op": "replace",
            "path": "/spec/selector",
            "value": {spec.DEVPOD_LABEL: "true"},
        }
    ]
    assert any("--type=json" in argv for argv in kube.commands if "patch" in argv)


def test_cutover_needs_a_selector_to_borrow():
    headless = {"metadata": {"name": "demo"}, "spec": {"clusterIP": "None"}}
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD, "service/demo": headless})
    with pytest.raises(dev.DevError, match="no selector"):
        dev.create_dev_pod(kube, "demo", image="img:1", cutover_service="demo")


def test_teardown_restores_the_original_selector_exactly_then_deletes():
    dev_pod = {
        "metadata": {
            "name": "demo-podbench",
            "labels": {spec.DEVPOD_LABEL: "true"},
            "annotations": dev.cutover_annotations("demo", {"app": "demo"}),
        },
        "spec": {"containers": [{"name": "app"}]},
    }
    kube = FakeKubectl(**{"pod/demo-podbench": dev_pod})

    actions = dev.delete_dev_pod(kube, "demo-podbench")

    [patch] = kube.patches()
    assert patch == [
        {"op": "replace", "path": "/spec/selector", "value": {"app": "demo"}}
    ]
    # Restore first, delete second: otherwise the Service selects nothing for
    # the window in between.
    order = [verb for verb in kube.subcommands() if verb in ("patch", "delete")]
    assert order == ["patch", "delete"]
    assert actions[-1] == "deleted pod/demo-podbench"


def test_teardown_refuses_to_delete_the_origin_pod():
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD})
    with pytest.raises(dev.DevError, match="not a podbench dev pod"):
        dev.delete_dev_pod(kube, "demo")
    assert not any("delete" in argv for argv in kube.commands)


def test_teardown_of_a_missing_pod_is_not_an_error():
    kube = FakeKubectl()
    assert "nothing to delete" in dev.delete_dev_pod(kube, "demo-podbench")[0]


def test_recorded_cutover_is_none_without_annotations():
    assert dev.recorded_cutover({"metadata": {}}) is None


# -- the seat the dev pod's sidecar is ------------------------------------


def identity_file(tmp_path: Path) -> str:
    key = tmp_path / "id_ed25519"
    key.write_text("PRIVATE")
    key.with_suffix(".pub").write_text(CLIENT_KEY + "\n")
    return str(key)


def dev_pod_document(name: str = "demo-podbench") -> dict[str, Any]:
    """The created dev pod as the API server reports it back.

    Only the UID matters: it is what the ``HostKeyAlias`` is keyed on, so a
    re-created pod is a new host rather than a man-in-the-middle warning.
    """
    return {
        "metadata": {"name": name, "namespace": "podbench-test", "uid": "pod-uid-1"},
        "spec": {"containers": [{"name": "app"}, {"name": "podbench"}]},
        "status": {"phase": "Running"},
    }


def test_the_users_key_is_authorised_in_the_sidecar_at_authoring_time():
    """An ordinary container's env is immutable, so this is the only chance.

    Without it the sidecar's own start-up check ends `[FAIL] authorized-keys`
    and the dev pod is reachable by `kubectl exec` alone — which is Iterate mode
    with its point removed, since the editor is meant to be inside the cluster.
    """
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD})
    _, manifest = dev.create_dev_pod(kube, "demo", image="img:1", public_key=CLIENT_KEY)

    sidecar = next(c for c in manifest["spec"]["containers"] if c["name"] == "podbench")
    env = {entry["name"]: entry["value"] for entry in sidecar["env"]}
    assert env[dev.PUBKEY_ENV] == CLIENT_KEY


def test_a_sidecar_with_a_projected_identity_is_reached_at_the_env_home(
    tmp_path: Path,
):
    """The two homes have to agree, and $HOME is the one that decides.

    The agent inside the container picks its layout with
    ``SshdLayout.for_uid(geteuid())``, which reads ``$HOME`` — ``/workspace`` —
    for a non-root seat. The projected passwd record names ``/home/podbench``,
    which is where an ssh *session* lands and nothing else. A ProxyCommand
    naming sshd's config in the passwd home would fail at the first connection.
    """
    kube = FakeKubectl(**{"pod/demo": origin_with_identity()})
    pod, manifest = dev.create_dev_pod(kube, "demo", image="img:1")

    session = dev.sidecar_session(pod, manifest)
    assert session.uid == 1000
    assert session.home == "/workspace"
    layout = launcher.seat_layout(session)
    assert layout.config_path == "/workspace/.podbench/sshd_config"
    assert layout.authorized_keys_path == "/workspace/.ssh/authorized_keys"


def test_a_root_sidecar_is_reached_at_the_root_layout_whatever_home_says():
    """The other half of the same decision.

    ``SshdLayout.for_uid(0)`` ignores ``$HOME`` entirely — root's files live in
    ``/root`` and ``/etc/podbench`` — so reading the sidecar's ``HOME`` env here
    would point the ProxyCommand at a config file the agent never wrote.
    """
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD})
    pod, manifest = dev.create_dev_pod(kube, "demo", image="img:1")

    session = dev.sidecar_session(pod, manifest)
    sidecar = next(c for c in manifest["spec"]["containers"] if c["name"] == "podbench")
    assert {e["name"]: e["value"] for e in sidecar["env"]}["HOME"] == "/workspace"
    assert session.home is None, "the root layout does not consult $HOME"
    assert launcher.seat_layout(session).config_path == "/etc/podbench/sshd_config"


def test_dev_writes_the_client_stanza_the_launcher_would(tmp_path: Path):
    """Same generator, same file, same advice — for an ordinary container."""
    kube = FakeKubectl(
        **{"pod/demo": origin_with_identity(), "pod/demo-podbench": dev_pod_document()}
    )
    pod, manifest = dev.create_dev_pod(kube, "demo", image="img:1")

    seat = dev.seat_ssh_config(
        kube,
        pod,
        manifest,
        identity=identity_file(tmp_path),
        config_dir=str(tmp_path / "cfg"),
    )

    assert seat.alias == "podbench-podbench-test-demo-podbench"
    assert seat.path is not None
    stanza = seat.path.read_text()
    assert "Host podbench-podbench-test-demo-podbench" in stanza
    # Measured, not derived from the rung: the login name is whatever the
    # projected passwd record calls the application's uid.
    assert "User podbench" in stanza
    assert (
        "ProxyCommand kubectl -n podbench-test exec -i demo-podbench -c podbench "
        "-- /usr/sbin/sshd -i -e -f /workspace/.podbench/sshd_config "
        "-o LogLevel=ERROR"
    ) in stanza
    assert "HostKeyAlias podbench-pod-uid-1" in stanza
    known_hosts = (tmp_path / "cfg" / "known_hosts").read_text()
    assert known_hosts.startswith("podbench-pod-uid-1 ssh-ed25519 ")

    summary = dev.connection_summary(pod, seat)
    assert f"then:  ssh {seat.alias}" in summary
    # The exec line stays: it works when ssh does not.
    assert "kubectl -n podbench-test exec -it demo-podbench -c podbench -- bash" in (
        summary
    )


def test_a_sidecar_with_no_login_gets_the_reason_not_a_stanza(tmp_path: Path):
    """A stanza that cannot work is the one output worse than none."""
    kube = FakeKubectl(
        **{"pod/demo": origin_with_identity(), "pod/demo-podbench": dev_pod_document()}
    )
    kube.login_user = None
    pod, manifest = dev.create_dev_pod(kube, "demo", image="img:1")

    seat = dev.seat_ssh_config(
        kube,
        pod,
        manifest,
        identity=identity_file(tmp_path),
        config_dir=str(tmp_path / "cfg"),
    )

    assert seat.alias is None and seat.path is None
    assert "no ssh config was written" in seat.note
    assert not (tmp_path / "cfg" / "config.d").exists()
    summary = dev.connection_summary(pod, seat)
    assert "ProxyCommand" not in summary
    # …and the route that still works is still offered.
    assert "exec -it demo-podbench -c podbench -- bash" in summary


def test_teardown_takes_the_client_config_with_it(tmp_path: Path):
    """The pod UID is gone for good, so a stanza left behind can only fail."""
    dev_pod = dev_pod_document()
    dev_pod["metadata"]["labels"] = {spec.DEVPOD_LABEL: "true"}
    kube = FakeKubectl(**{"pod/demo-podbench": dev_pod})
    config = tmp_path / "cfg"
    stanza = launcher.ssh_config_path(
        config, model.PodRef("podbench-test", "demo-podbench")
    )
    stanza.parent.mkdir(parents=True)
    stanza.write_text("Host podbench-podbench-test-demo-podbench\n")
    (config / "known_hosts").write_text(
        "podbench-pod-uid-1 ssh-ed25519 AAAA\npodbench-other ssh-ed25519 BBBB\n"
    )

    actions = dev.delete_dev_pod(kube, "demo-podbench", config_dir=str(config))

    assert not stanza.exists()
    assert (config / "known_hosts").read_text() == "podbench-other ssh-ed25519 BBBB\n"
    assert any("removed" in action for action in actions)
    # Order: the pod goes first, so a teardown that fails half way through
    # leaves a working stanza for the pod that is still there.
    assert actions.index("deleted pod/demo-podbench") < len(actions) - 1


def test_connection_summary_tells_the_user_how_to_tear_down():
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD, "service/demo": SERVICE})
    pod, _ = dev.create_dev_pod(kube, "demo", image="img:1", cutover_service="demo")
    summary = dev.connection_summary(pod)
    assert "podbench dev --delete demo-podbench -n podbench-test" in summary
    assert "podbench run --port 8080" in summary


# -- CLI -------------------------------------------------------------------


def test_cli_dry_run_prints_the_pod_it_would_create(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD})
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    assert (
        dev.main(
            [
                "dev",
                "demo",
                "-n",
                "podbench-test",
                "--identity",
                identity_file(tmp_path),
                "--dry-run",
            ]
        )
        == 0
    )

    manifest = json.loads(capsys.readouterr().out)
    assert manifest["metadata"]["name"] == "demo-podbench"
    assert manifest["spec"]["shareProcessNamespace"] is True
    assert not any("create" in argv for argv in kube.commands)


def test_cli_refuses_before_creating_anything_when_there_is_no_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """The key cannot be added later, so the refusal has to come first.

    A sidecar's ``env`` is fixed when the pod is created. A dev pod made without
    a key would have to be deleted and re-made to get one, so this refuses in
    the same words ``attach`` does, before a pod exists.
    """
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD})
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    code = dev.main(
        ["dev", "demo", "-n", "podbench-test", "--identity", str(tmp_path / "absent")]
    )

    assert code == 1
    error = capsys.readouterr().err
    assert "no ssh public key" in error and "ssh-keygen" in error
    assert not any("create" in argv for argv in kube.commands)


def test_cli_prints_the_ssh_route_and_the_exec_route(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    kube = FakeKubectl(
        **{"pod/demo": origin_with_identity(), "pod/demo-podbench": dev_pod_document()}
    )
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    code = dev.main(
        [
            "dev",
            "demo",
            "-n",
            "podbench-test",
            "--identity",
            identity_file(tmp_path),
            "--config-dir",
            str(tmp_path / "cfg"),
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "ssh config written to" in out
    assert "then:  ssh podbench-podbench-test-demo-podbench" in out
    assert "Include" in out
    assert "exec -it demo-podbench -c podbench -- bash" in out
    assert (
        tmp_path / "cfg" / "config.d" / "podbench-test-demo-podbench.conf"
    ).is_file()


def test_cli_accepts_pod_slash_name_exactly_as_attach_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    # One CLI, one pod syntax. `pod/demo` used to fail twice over here: kubectl
    # refused the argument, and dev_pod_name derived `pod/demo-podbench`, which
    # is not an RFC 1123 label.
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD})
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    assert (
        dev.main(
            [
                "dev",
                "pod/demo",
                "-n",
                "podbench-test",
                "--identity",
                identity_file(tmp_path),
                "--dry-run",
            ]
        )
        == 0
    )

    manifest = json.loads(capsys.readouterr().out)
    assert manifest["metadata"]["name"] == "demo-podbench"
    assert manifest["metadata"]["annotations"][spec.ORIGIN_ANNOTATION] == "demo"


def test_cli_delete_accepts_pod_slash_name_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    dev_pod = {
        "metadata": {
            "name": "demo-podbench",
            "labels": {spec.DEVPOD_LABEL: "true"},
        },
        "spec": {"containers": [{"name": "app"}]},
    }
    kube = FakeKubectl(**{"pod/demo-podbench": dev_pod})
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    assert (
        dev.main(
            [
                "dev",
                "pod/demo",
                "-n",
                "podbench-test",
                "--delete",
                "--config-dir",
                str(tmp_path / "cfg"),
            ]
        )
        == 0
    )
    assert "deleted pod/demo-podbench" in capsys.readouterr().out


def test_cli_refuses_a_reference_that_is_not_a_pod(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    # `--identity` is not incidental: the key is read before the reference is
    # resolved, matching `attach` (`launcher.attach_command`), so a machine with
    # no ~/.ssh/id_ed25519 reaches the key's refusal and never the reference's.
    kube = FakeKubectl()
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    argv = [
        "dev",
        "deployment/api",
        "-n",
        "podbench-test",
        "--identity",
        identity_file(tmp_path),
    ]
    assert dev.main(argv) == 1
    assert "works on pods" in capsys.readouterr().err
    assert not kube.commands


def test_cli_reports_a_refusal_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    # A pod occupying the dev pod's name that podbench did not author: the
    # guard fires rather than deleting somebody else's workload.
    kube = FakeKubectl(**{"pod/demo-podbench": ORIGIN_POD})
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    assert (
        dev.main(
            ["dev", "demo-podbench", "--delete", "--config-dir", str(tmp_path / "cfg")]
        )
        == 1
    )
    assert "not a podbench dev pod" in capsys.readouterr().err


def test_cli_takes_the_namespace_from_the_kubeconfig(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """`-n` unset means the context's namespace here as it does everywhere else.

    `dev` used to mean the literal namespace `default` (issue #44), which is the
    one direction that costs: it is the mode that creates and deletes objects,
    so pointing it at a namespace the user is not looking at either fails to
    find their pod or clones something they never meant.
    """
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD})
    asked: list[tuple[str | None, dict[str, Any]]] = []

    def factory(namespace: str | None, **kwargs: Any) -> Kubectl:
        asked.append((namespace, kwargs))
        return kube

    monkeypatch.setattr(dev, "kubectl_for", factory)

    assert (
        dev.main(["dev", "demo", "--identity", identity_file(tmp_path), "--dry-run"])
        == 0
    )

    assert json.loads(capsys.readouterr().out)["metadata"]["name"] == "demo-podbench"
    # `None`, not `"default"`: the shared helper is what asks the kubeconfig,
    # and it is tested in test_launcher.
    assert asked == [(None, {"context": None})]


def test_cli_resolves_a_substring_the_way_attach_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD})
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    assert (
        dev.main(
            [
                "dev",
                "dem",
                "-n",
                "podbench-test",
                "--identity",
                identity_file(tmp_path),
                "--dry-run",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert json.loads(captured.out)["metadata"]["name"] == "demo-podbench"
    # Echoed, not assumed: the resolved name is about to become a pod.
    assert "'dem' matched pod demo" in captured.err


def test_cli_will_not_guess_between_two_pods_it_cannot_ask_about(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """Ambiguity is a question, and a question nobody can answer is a refusal.

    `dev` clones what it is given, so picking the first match on the user's
    behalf would author a second copy of the wrong workload.
    """
    kube = FakeKubectl(
        **{
            "pod/demo-7f9": named_pod("demo-7f9"),
            "pod/demo-canary": named_pod("demo-canary"),
        }
    )
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    code = dev.main(
        [
            "dev",
            "demo",
            "-n",
            "podbench-test",
            "--no-prompt",
            "--identity",
            identity_file(tmp_path),
        ]
    )

    assert code == 1
    error = capsys.readouterr().err
    assert "matches 2 pods" in error and "--no-prompt was given" in error
    assert not any("create" in argv for argv in kube.commands)


def test_cli_delete_of_a_pod_already_gone_is_still_a_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """Teardown scripts run `--delete` twice, and the second run must exit 0.

    Substring resolution must not turn "there is nothing here by that name"
    into the refusal it is for the create path.
    """
    kube = FakeKubectl(**{"pod/other": named_pod("other")})
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    code = dev.main(
        [
            "dev",
            "demo-podbench",
            "-n",
            "podbench-test",
            "--delete",
            "--config-dir",
            str(tmp_path / "cfg"),
        ]
    )

    assert code == 0
    assert "nothing to delete" in capsys.readouterr().out


def test_cli_delete_ignores_the_workload_pods_a_reference_also_matches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """The idempotent teardown is what `--delete` resolves *against*.

    `api` matching two ordinary pods is not an ambiguity `--delete` can have:
    neither of them is a thing it would ever delete. Resolving the reference
    against the whole namespace made the second run of a teardown script exit 1
    the moment the origin's own replicas shared its prefix.
    """
    kube = FakeKubectl(
        **{
            "pod/api-worker": named_pod("api-worker"),
            "pod/api-cache": named_pod("api-cache"),
        }
    )
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    code = dev.main(
        [
            "dev",
            "api",
            "-n",
            "podbench-test",
            "--delete",
            "--config-dir",
            str(tmp_path / "cfg"),
        ]
    )

    assert code == 0
    assert "nothing to delete" in capsys.readouterr().out
    assert not any("delete" in argv for argv in kube.commands)


def test_cli_delete_finds_a_dev_pod_that_was_given_its_own_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """`--name` breaks the derivation, and the label is what puts it back.

    A dev pod called `mydev` derives to `mydev-podbench`, which is nothing, so
    teardown used to report success while the pod — and any `--cutover` it is
    holding open — stayed exactly where it was.
    """
    kube = FakeKubectl(**{"pod/mydev": labelled_dev_pod("mydev")})
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    code = dev.main(
        [
            "dev",
            "mydev",
            "-n",
            "podbench-test",
            "--delete",
            "--config-dir",
            str(tmp_path / "cfg"),
        ]
    )

    assert code == 0
    assert "deleted pod/mydev" in capsys.readouterr().out


def test_cli_delete_with_no_pod_offers_only_what_it_could_delete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    kube = FakeKubectl(
        **{
            "pod/api": named_pod("api"),
            "pod/api-podbench": labelled_dev_pod("api-podbench"),
        }
    )
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    code = dev.main(
        [
            "dev",
            "-n",
            "podbench-test",
            "--delete",
            "--config-dir",
            str(tmp_path / "cfg"),
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "deleted pod/api-podbench" in captured.out
    # `dev pod`, not `pod`: `api` is in the namespace too, and it is not on
    # offer here.
    assert "the only dev pod in namespace podbench-test is api-podbench" in captured.err


def test_cli_delete_with_no_pod_in_a_namespace_with_none_is_a_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """ "Nothing to tear down" is the same answer whether or not POD was given."""
    kube = FakeKubectl(**{"pod/api": named_pod("api")})
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    code = dev.main(
        [
            "dev",
            "-n",
            "podbench-test",
            "--delete",
            "--config-dir",
            str(tmp_path / "cfg"),
        ]
    )

    assert code == 0
    assert "nothing to delete" in capsys.readouterr().out


def test_cli_delete_will_not_guess_between_two_dev_pods(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    kube = FakeKubectl(
        **{
            "pod/api-podbench": labelled_dev_pod("api-podbench"),
            "pod/web-podbench": labelled_dev_pod("web-podbench"),
        }
    )
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    code = dev.main(
        [
            "dev",
            "-n",
            "podbench-test",
            "--delete",
            "--no-prompt",
            "--config-dir",
            str(tmp_path / "cfg"),
        ]
    )

    assert code == 1
    assert "2 dev pods and none was named" in capsys.readouterr().err
    assert not any("delete" in argv for argv in kube.commands)


def test_cli_reads_the_key_before_it_asks_which_pod(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """`attach`'s order, for `attach`'s reason.

    A missing key refuses this run whichever pod is chosen, so the namespace is
    not listed and the question is not asked: an answer spent on a run that
    cannot proceed is an answer wasted.
    """
    kube = FakeKubectl(
        **{
            "pod/demo-7f9": named_pod("demo-7f9"),
            "pod/demo-canary": named_pod("demo-canary"),
        }
    )
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    code = dev.main(
        ["dev", "demo", "-n", "podbench-test", "--identity", str(tmp_path / "absent")]
    )

    assert code == 1
    assert "ssh-keygen" in capsys.readouterr().err
    assert not any(argv[3:5] == ("get", "pods") for argv in kube.commands)


def test_cli_will_not_clone_a_dev_pod(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """Reachable the moment `dev` took substrings: the origin is gone, its
    clone is still there, and `dev demo` matches `demo-podbench`.

    Cloning that copies the sidecar in as an ordinary container, and what the
    user then sees is a complaint about container counts or an AlreadyExists,
    neither of which names what actually happened.
    """
    kube = FakeKubectl(**{"pod/demo-podbench": labelled_dev_pod("demo-podbench")})
    monkeypatch.setattr(dev, "kubectl_for", always(kube))

    code = dev.main(
        [
            "dev",
            "demo",
            "-n",
            "podbench-test",
            "--identity",
            identity_file(tmp_path),
            "--container",
            "app",
            "--name",
            "demo2",
            "--dry-run",
        ]
    )

    assert code == 1
    assert "is itself a podbench dev pod" in capsys.readouterr().err
    assert not any("create" in argv for argv in kube.commands)


def test_cli_run_will_not_relaunch_without_being_told_the_port():
    # The port is what the pre-flight and the ownership check are about, so
    # there is no default for it.
    assert dev.main(["run"]) == 2


def test_run_without_a_command_is_refused(tmp_path: Path):
    with pytest.raises(dev.DevError, match="nothing to run"):
        dev.start([], port=8080, workspace=str(tmp_path), proc=make_proc(tmp_path))


# -- GitOps refusal ---------------------------------------------------------


def owned_pod(owner: str = "demo-77f") -> dict[str, Any]:
    """The origin, as a controller actually leaves it: owned, and unmarked.

    The absence of a mark here is the point of the fixture. Argo stamps what it
    applied from git, and the pod template is not that — measured on a live
    install, 0 of 82 pods carried the tracking label while their workloads did.
    """
    pod = copy.deepcopy(ORIGIN_POD)
    pod["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "name": owner, "controller": True}
    ]
    return pod


def workload(
    kind: str, name: str, *, marked: bool, owner: str | None = None
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name}
    if marked:
        metadata["labels"] = {"argocd.argoproj.io/instance": "beamline-i22"}
    if owner is not None:
        metadata["ownerReferences"] = [
            {"kind": "Deployment", "name": owner, "controller": True}
        ]
    return {"kind": kind, "metadata": metadata}


def gitops_kube() -> FakeKubectl:
    return FakeKubectl(
        **{
            "pod/demo": owned_pod(),
            "replicaset/demo-77f": workload(
                "ReplicaSet", "demo-77f", marked=False, owner="demo"
            ),
            "deployment/demo": workload("Deployment", "demo", marked=True),
        }
    )


def test_dev_refuses_a_workload_a_gitops_controller_reconciles() -> None:
    with pytest.raises(dev.DevError) as caught:
        dev.create_dev_pod(gitops_kube(), "demo", image="img:1")
    message = str(caught.value)
    assert "deployment/demo" in message
    assert "argocd.argoproj.io/instance=beamline-i22" in message
    # Both failure modes named, and the modes that do work offered.
    assert "self-heal" in message
    assert "podbench attach" in message


def test_the_mark_is_looked_for_on_the_workload_not_on_the_pod() -> None:
    """The pod carries nothing; refusing still has to happen.

    Checking the pod alone is the plausible-looking implementation that would
    let Iterate mode run on exactly the workloads it must not.
    """
    kube = gitops_kube()
    assert spec.gitops_owner(owned_pod()) is None
    with pytest.raises(dev.DevError):
        dev.create_dev_pod(kube, "demo", image="img:1")


def test_an_unmarked_workload_is_left_alone() -> None:
    kube = FakeKubectl(
        **{
            "pod/demo": owned_pod(),
            "replicaset/demo-77f": workload(
                "ReplicaSet", "demo-77f", marked=False, owner="demo"
            ),
            "deployment/demo": workload("Deployment", "demo", marked=False),
        }
    )
    pod, _ = dev.create_dev_pod(kube, "demo", image="img:1")
    assert pod.ref.name.startswith("demo")


def test_an_unreadable_owner_does_not_read_as_managed() -> None:
    """A missing `get deployments` grant must not be reported as GitOps.

    The user is left with the failure they already had rather than a confident
    diagnosis of a condition nobody checked.
    """
    kube = FakeKubectl(**{"pod/demo": owned_pod()})
    pod, _ = dev.create_dev_pod(kube, "demo", image="img:1")
    assert pod.ref.name.startswith("demo")


def test_the_walk_is_bounded_against_an_ownership_cycle() -> None:
    kube = FakeKubectl(
        **{
            "pod/demo": owned_pod("loop"),
            "replicaset/loop": workload(
                "ReplicaSet", "loop", marked=False, owner="loop"
            ),
            "deployment/loop": {
                "kind": "Deployment",
                "metadata": {
                    "name": "loop",
                    "ownerReferences": [
                        {"kind": "ReplicaSet", "name": "loop", "controller": True}
                    ],
                },
            },
        }
    )
    assert dev.gitops_manager(kube, owned_pod("loop")) is None
