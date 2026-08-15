"""Tests for Iterate mode.

Nothing here touches a cluster or a real process: ``ss`` output is captured
text, ``/proc`` is a directory tree built in ``tmp_path``, and the launcher's
subprocess seam is a fake runner that records what it was asked to do.

The captured ``ss`` fixtures are the shapes the phase-0 report warns about — a
listener owned by another container, and a port that is free to ``ss -lntp``
while a ``TIME_WAIT`` socket makes it unbindable.
"""

from __future__ import annotations

import json
import signal
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from podbench import dev, spec
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
    argv: Sequence[str], *, stdin: str | None = None, capture: bool = True
) -> CommandResult:
    """A runner that must never be reached."""
    raise AssertionError(f"nothing should have run: {list(argv)}")


def always(kube: Kubectl) -> Callable[..., Kubectl]:
    """A ``Kubectl`` factory the CLI can be pointed at."""

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
        argv: Sequence[str], *, stdin: str | None = None, capture: bool = True
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

SERVICE: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Service",
    "metadata": {"name": "demo"},
    "spec": {"selector": {"app": "demo"}, "ports": [{"port": 80, "targetPort": 8080}]},
}


class FakeKubectl(Kubectl):
    """A Kubectl whose runner answers from a table of objects."""

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
    ) -> CommandResult:
        self.commands.append(tuple(argv))
        args = list(argv)
        stdout = ""
        if args[3:5] == ["get", "pod"]:
            stdout = json.dumps(self.objects.get(f"pod/{args[5]}", {}))
            if stdout == "{}":
                return CommandResult(tuple(argv), 1, "", "pods 'x' not found")
        elif args[3:5] == ["get", "service"]:
            stdout = json.dumps(self.objects.get(f"service/{args[5]}", {}))
        elif args[3] == "create":
            stdout = stdin or "{}"
        return CommandResult(tuple(argv), 0, stdout, "")

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
    order = [argv[3] for argv in kube.commands if argv[3] in ("patch", "delete")]
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


def test_connection_summary_tells_the_user_how_to_tear_down():
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD, "service/demo": SERVICE})
    pod, _ = dev.create_dev_pod(kube, "demo", image="img:1", cutover_service="demo")
    summary = dev.connection_summary(pod)
    assert "podbench dev --delete demo-podbench -n podbench-test" in summary
    assert "podbench run --port 8080" in summary


# -- CLI -------------------------------------------------------------------


def test_cli_dry_run_prints_the_pod_it_would_create(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD})
    monkeypatch.setattr(dev, "Kubectl", always(kube))

    assert dev.main(["dev", "demo", "-n", "podbench-test", "--dry-run"]) == 0

    manifest = json.loads(capsys.readouterr().out)
    assert manifest["metadata"]["name"] == "demo-podbench"
    assert manifest["spec"]["shareProcessNamespace"] is True
    assert not any("create" in argv for argv in kube.commands)


def test_cli_accepts_pod_slash_name_exactly_as_attach_does(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    # One CLI, one pod syntax. `pod/demo` used to fail twice over here: kubectl
    # refused the argument, and dev_pod_name derived `pod/demo-podbench`, which
    # is not an RFC 1123 label.
    kube = FakeKubectl(**{"pod/demo": ORIGIN_POD})
    monkeypatch.setattr(dev, "Kubectl", always(kube))

    assert dev.main(["dev", "pod/demo", "-n", "podbench-test", "--dry-run"]) == 0

    manifest = json.loads(capsys.readouterr().out)
    assert manifest["metadata"]["name"] == "demo-podbench"
    assert manifest["metadata"]["annotations"][spec.ORIGIN_ANNOTATION] == "demo"


def test_cli_delete_accepts_pod_slash_name_too(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    dev_pod = {
        "metadata": {
            "name": "demo-podbench",
            "labels": {spec.DEVPOD_LABEL: "true"},
        },
        "spec": {"containers": [{"name": "app"}]},
    }
    kube = FakeKubectl(**{"pod/demo-podbench": dev_pod})
    monkeypatch.setattr(dev, "Kubectl", always(kube))

    assert dev.main(["dev", "pod/demo", "-n", "podbench-test", "--delete"]) == 0
    assert "deleted pod/demo-podbench" in capsys.readouterr().out


def test_cli_refuses_a_reference_that_is_not_a_pod(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    kube = FakeKubectl()
    monkeypatch.setattr(dev, "Kubectl", always(kube))

    assert dev.main(["dev", "deployment/api", "-n", "podbench-test"]) == 1
    assert "works on pods" in capsys.readouterr().err
    assert not kube.commands


def test_cli_reports_a_refusal_without_a_traceback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    # A pod occupying the dev pod's name that podbench did not author: the
    # guard fires rather than deleting somebody else's workload.
    kube = FakeKubectl(**{"pod/demo-podbench": ORIGIN_POD})
    monkeypatch.setattr(dev, "Kubectl", always(kube))

    assert dev.main(["dev", "demo-podbench", "--delete"]) == 1
    assert "not a podbench dev pod" in capsys.readouterr().err


def test_cli_run_will_not_relaunch_without_being_told_the_port():
    # The port is what the pre-flight and the ownership check are about, so
    # there is no default for it.
    with pytest.raises(SystemExit):
        dev.main(["run"])


def test_run_without_a_command_is_refused(tmp_path: Path):
    with pytest.raises(dev.DevError, match="nothing to run"):
        dev.start([], port=8080, workspace=str(tmp_path), proc=make_proc(tmp_path))
