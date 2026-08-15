"""``podbench agent`` — the debug container's PID 1.

The agent's whole job is to make the container reconnectable. Ephemeral
containers cannot be restarted and their name is burnt for the pod's lifetime,
a user may attach a second session into a container that is already serving
one, and an OOM or a pod restart hands us a completely fresh rootfs with new
host keys. So every step here is *ensure*, never *create*: running the agent
twice against the same container is normal operation, not an error path.

Nothing it writes may be the only copy of anything. The host key, the
authorized keys and the sshd config are all rebuilt from the environment or a
mounted Secret on each start, which is what makes "the ephemeral container is
strictly disposable" true rather than aspirational.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from .model import ContainerRef, PodRef
from .sshcfg import SshdLayout, proxy_command, sshd_config

__all__ = [
    "request_stop",
    "AUTHORIZED_KEYS_FILE_ENV",
    "AUTHORIZED_KEYS_MOUNT",
    "HOST_KEY_ENV",
    "HOST_KEY_FILE_ENV",
    "HOST_KEY_MOUNT",
    "IDLE_SLICE",
    "PUBKEY_ENV",
    "CheckResult",
    "CommandRunner",
    "ReaperStatus",
    "ensure_all",
    "ensure_authorized_keys",
    "ensure_host_key",
    "ensure_privsep_dir",
    "ensure_sshd_config",
    "fd2_check",
    "idle",
    "main",
    "read_host_public_key",
    "reap_children",
    "reaper_status",
    "run_command",
    "proxy_shape_check",
    "stdio_roundtrip_check",
    "self_check",
]

PUBKEY_ENV = "PODBENCH_SSH_PUBKEY"
AUTHORIZED_KEYS_FILE_ENV = "PODBENCH_SSH_PUBKEY_FILE"
HOST_KEY_ENV = "PODBENCH_SSH_HOST_KEY"
HOST_KEY_FILE_ENV = "PODBENCH_SSH_HOST_KEY_FILE"

AUTHORIZED_KEYS_MOUNT = "/etc/podbench/ssh/authorized_keys"
HOST_KEY_MOUNT = "/etc/podbench/ssh/ssh_host_ed25519_key"
"""Default mount points for a Secret. A key delivered this way survives the pod
restarts that destroy an ephemeral container's writable layer, which is the only
way the client's ``known_hosts`` entry can outlive a single attach."""

CommandRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
"""How the agent runs a helper binary — ``ssh-keygen``, ``sshd -t``.

Named for what it runs, because :class:`podbench.kubectl.Runner` is a different
thing entirely (it runs ``kubectl``, and returns podbench's own result type),
and a launcher importing both had two ``Runner``s to keep straight.
"""

IDLE_SLICE = 0.5
"""Longest the idle loop will sleep without checking whether it was asked to
stop. Small enough that SIGTERM is acted on well inside the kubelet's grace
period, large enough that the loop costs nothing measurable."""

_stop_requested = False


def _say(message: str) -> None:
    """Progress goes to stderr so stdout stays parseable by the launcher."""
    print(f"agent: {message}", file=sys.stderr)


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a helper binary, capturing both streams. The single shell-out seam."""
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class CheckResult:
    """One startup check, and why it says what it says."""

    name: str
    ok: bool
    detail: str

    def __str__(self) -> str:
        return f"[{'ok  ' if self.ok else 'FAIL'}] {self.name}: {self.detail}"


def _write_if_changed(path: Path, content: str, mode: int) -> bool:
    """Write ``content`` only when it differs; return whether anything changed.

    Rewriting an identical file would be harmless for the file but not for the
    caller: a second attach reports what it did, and "changed nothing" is the
    answer that tells the user the first session is untouched.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else None
    if existing == content:
        if path.stat().st_mode & 0o777 != mode:
            path.chmod(mode)
            return True
        return False
    path.write_text(content)
    path.chmod(mode)
    return True


def ensure_privsep_dir(layout: SshdLayout) -> bool:
    """Create ``/run/sshd`` when we are root.

    ``/run`` is frequently a tmpfs, so the directory the image built is not
    necessarily the directory the container boots with; without it every
    connection dies with ``Missing privilege separation directory``. sshd skips
    privilege separation entirely when it is not uid 0, so for the non-root
    layout there is nothing to do.
    """
    if layout.privsep_dir is None:
        return False
    directory = Path(layout.privsep_dir)
    if directory.is_dir():
        return False
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    return True


def ensure_sshd_config(layout: SshdLayout) -> bool:
    """Write the sshd config the ProxyCommand names with ``-f``."""
    return _write_if_changed(Path(layout.config_path), sshd_config(layout), 0o644)


def _authorized_keys_from(env: Mapping[str, str]) -> list[str]:
    """Collect authorized keys from the env var and the mounted Secret."""
    keys: list[str] = []
    inline = env.get(PUBKEY_ENV)
    if inline:
        keys += inline.splitlines()
    mounted = env.get(AUTHORIZED_KEYS_FILE_ENV, AUTHORIZED_KEYS_MOUNT)
    path = Path(mounted)
    if path.is_file():
        keys += path.read_text().splitlines()
    return [key.strip() for key in keys if key.strip()]


def ensure_authorized_keys(
    layout: SshdLayout, *, env: Mapping[str, str] | None = None
) -> bool:
    """Merge our keys into ``authorized_keys`` without evicting anyone.

    Merge rather than overwrite because a second ``podbench attach`` into a
    container that is already serving a session is a supported thing to do, and
    truncating the file would drop the running session's key on the floor at the
    next reconnect.
    """
    environ = env if env is not None else os.environ
    wanted = _authorized_keys_from(environ)
    path = Path(layout.authorized_keys_path)
    existing = path.read_text().splitlines() if path.is_file() else []
    merged: list[str] = []
    for key in [*[line.strip() for line in existing if line.strip()], *wanted]:
        if key not in merged:
            merged.append(key)
    if not merged:
        return False
    # The root layout leaves StrictModes at its default, so sshd audits this
    # directory and silently ignores the keys if the image left it group-writable.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    return _write_if_changed(path, "\n".join(merged) + "\n", 0o600)


def ensure_host_key(
    layout: SshdLayout,
    *,
    env: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
) -> bool:
    """Install a supplied host key, or mint one if none was supplied.

    A supplied key always wins, and always overwrites: it is the pod's stable
    identity and the only reason a client's ``known_hosts`` entry can survive
    the fresh rootfs a pod restart produces. A minted key is generated once and
    then left alone, so a second session sees the same identity as the first.

    ``ssh-keygen -A`` is deliberately not used: it mints three keys of which the
    config names exactly one, and it only ever writes to ``/etc/ssh``, which the
    non-root layout cannot touch.
    """
    environ = env if env is not None else os.environ
    run = runner if runner is not None else run_command
    path = Path(layout.host_key_path)
    supplied = environ.get(HOST_KEY_ENV)
    supplied_file = environ.get(HOST_KEY_FILE_ENV, HOST_KEY_MOUNT)
    if supplied is None and Path(supplied_file).is_file():
        supplied = Path(supplied_file).read_text()

    if supplied is not None:
        if not supplied.endswith("\n"):
            supplied += "\n"
        changed = _write_if_changed(path, supplied, 0o600)
        if changed or not Path(layout.host_public_key_path).is_file():
            _derive_public_key(layout, runner=run)
        return changed

    if path.is_file():
        return False
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "podbench",
            "-f",
            str(path),
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"ssh-keygen failed: {result.stderr.strip()}")
    if path.is_file():
        path.chmod(0o600)
    return True


def _derive_public_key(layout: SshdLayout, *, runner: CommandRunner) -> None:
    """Recover the public half of a supplied private key.

    A Secret usually carries only the private key, and podbench needs the public
    half to write the client's ``known_hosts`` entry itself.
    """
    result = runner(["ssh-keygen", "-y", "-f", layout.host_key_path])
    if result.returncode == 0 and result.stdout.strip():
        Path(layout.host_public_key_path).write_text(result.stdout.strip() + "\n")


def read_host_public_key(layout: SshdLayout) -> str | None:
    """The host's public key, for the launcher to pin in ``known_hosts``."""
    path = Path(layout.host_public_key_path)
    return path.read_text().strip() if path.is_file() else None


def ensure_all(
    layout: SshdLayout,
    *,
    env: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
) -> list[str]:
    """Bring the container to a serving state. Returns what actually changed.

    An empty list is the expected result of the second and later runs.
    """
    changes: list[str] = []
    if ensure_privsep_dir(layout):
        changes.append(f"created {layout.privsep_dir}")
    if ensure_host_key(layout, env=env, runner=runner):
        changes.append(f"installed host key {layout.host_key_path}")
    if ensure_authorized_keys(layout, env=env):
        changes.append(f"updated {layout.authorized_keys_path}")
    if ensure_sshd_config(layout):
        changes.append(f"wrote {layout.config_path}")
    return changes


def fd2_check() -> CheckResult:
    """Whether our own stderr is a real, open file descriptor.

    fd 2 is the transport's lifeline: an exec'd process that closes or replaces
    it makes containerd tear down the whole exec session, truncating stdin and
    stdout mid-stream with ``rc=0`` and no diagnostic anywhere. If the agent
    itself was started with stderr pointed at ``/dev/null`` then whatever
    wrapper did that will do it to sshd too.
    """
    try:
        os.fstat(2)
    except OSError as exc:
        return CheckResult("fd2-open", False, f"stderr is not open: {exc}")
    try:
        dest = os.readlink("/proc/self/fd/2")
    except OSError:
        return CheckResult("fd2-open", True, "stderr is open")
    if dest == "/dev/null":
        return CheckResult(
            "fd2-open",
            False,
            "stderr is /dev/null; a wrapper doing this to sshd kills the "
            "kubectl exec stream mid-handshake",
        )
    return CheckResult("fd2-open", True, f"stderr is open on {dest}")


def stdio_roundtrip_check(*, delay: float = 0.2, timeout: float = 10.0) -> CheckResult:
    """The fd-2 tripwire: a delayed second line must still arrive.

    This is the shape from the report — write one line, wait, write another,
    and confirm *both* come back — because the failure it guards against is
    silent truncation with a zero exit code, which no error check can catch.

    Run locally, as PID 1, it only proves the image's ``sh`` and pipes are sane.
    The teardown it is named for happens in the CRI, so the launcher must also
    run this over ``kubectl exec`` (``podbench agent --self-check``) where the
    exec stream is genuinely in the path.
    """
    payload_a, payload_b = "podbench-fd2-one", "podbench-fd2-two"
    try:
        with subprocess.Popen(
            ["sh", "-c", "exec cat"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as proc:
            assert proc.stdin is not None
            proc.stdin.write(payload_a + "\n")
            proc.stdin.flush()
            time.sleep(delay)
            proc.stdin.write(payload_b + "\n")
            out, _ = proc.communicate(timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("stdio-roundtrip", False, f"could not run the probe: {exc}")
    lines = out.split()
    if lines == [payload_a, payload_b]:
        return CheckResult("stdio-roundtrip", True, "both lines returned")
    return CheckResult(
        "stdio-roundtrip",
        False,
        f"expected both lines back, got {lines!r} — the stream is being truncated",
    )


def proxy_shape_check(layout: SshdLayout) -> CheckResult:
    """Assert the generated ProxyCommand still has the flags that keep it alive.

    Cheap, and it fires here rather than in a user's editor on the day someone
    tidies ``-e`` away or adds ``-t``.
    """
    argv = proxy_command(
        ContainerRef(PodRef("podbench-selfcheck", "podbench-selfcheck"), "podbench"),
        layout=layout,
    )
    if "-e" not in argv:
        return CheckResult("proxy-command-shape", False, "sshd -e is missing")
    if "-t" in argv:
        return CheckResult("proxy-command-shape", False, "kubectl -t would hang ssh")
    if any(">" in token for token in argv):
        return CheckResult("proxy-command-shape", False, "stderr is being redirected")
    return CheckResult("proxy-command-shape", True, "sshd -i -e, no tty, no redirect")


def self_check(
    layout: SshdLayout,
    *,
    runner: CommandRunner | None = None,
    roundtrip: bool = True,
) -> list[CheckResult]:
    """Everything that must hold before a client is told the pod is ready.

    Each of these has a silent failure mode: a missing privsep directory, an
    unparsed config or a torn-down exec stream all surface to the user as a
    connection error with no cause attached.
    """
    results = [proxy_shape_check(layout), fd2_check()]
    if roundtrip:
        results.append(stdio_roundtrip_check())

    if layout.privsep_dir is not None:
        present = Path(layout.privsep_dir).is_dir()
        results.append(
            CheckResult(
                "privsep-dir",
                present,
                f"{layout.privsep_dir} "
                + ("exists" if present else "is missing; sshd will refuse every login"),
            )
        )
    for name, path in (
        ("host-key", layout.host_key_path),
        ("authorized-keys", layout.authorized_keys_path),
        ("sshd-config", layout.config_path),
    ):
        ok = Path(path).is_file() and Path(path).stat().st_size > 0
        results.append(
            CheckResult(name, ok, f"{path} {'present' if ok else 'missing'}")
        )

    run = runner if runner is not None else run_command
    validation = run([layout.sshd, "-t", "-f", layout.config_path])
    results.append(
        CheckResult(
            "sshd-config-valid",
            validation.returncode == 0,
            (validation.stderr or validation.stdout).strip() or "CONFIG_OK",
        )
    )
    return results


@dataclass(frozen=True)
class ReaperStatus:
    """Whether this process is the one the kernel will hand orphans to."""

    pid: int
    is_init: bool
    namespace_init: str | None
    """``comm`` of pid 1 in our namespace when that is not us."""

    @property
    def note(self) -> str:
        """A line for the session banner."""
        if self.is_init:
            return "podbench agent is pid 1; orphaned children will be reaped"
        return (
            f"pid 1 in this namespace is {self.namespace_init!r}, not podbench — "
            "it does not reap, so orphaned helpers become permanent zombies. "
            "Track helper pids explicitly and never use `kill -0` as a liveness "
            "check here."
        )


def reaper_status(
    *, pid: int | None = None, proc: Path = Path("/proc")
) -> ReaperStatus:
    """Work out whether we inherit orphans.

    Under ``kubectl debug --target`` the pod's PID namespace is shared and pid 1
    is the *target application*, which reaps nothing. A helper orphaned in that
    namespace stays a zombie for the pod's lifetime, and a zombie still answers
    ``kill -0`` — which is how a bootstrap script once reported a dead server as
    healthy and handed a client a dead port.
    """
    own = pid if pid is not None else os.getpid()
    if own == 1:
        return ReaperStatus(own, True, None)
    comm_path = proc / "1" / "comm"
    comm = comm_path.read_text().strip() if comm_path.is_file() else None
    return ReaperStatus(own, False, comm)


def reap_children() -> list[tuple[int, int]]:
    """Reap every exited child that is ready. Returns ``(pid, status)`` pairs."""
    reaped: list[tuple[int, int]] = []
    while True:
        try:
            child, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return reaped
        if child == 0:
            return reaped
        reaped.append((child, status))


def request_stop(signum: int, frame: FrameType | None) -> None:
    """Ask :func:`idle` to return at its next slice.

    Public because graceful shutdown is a behaviour worth asserting: the agent
    is PID 1, so taking longer than the kubelet's grace period to notice a
    SIGTERM means being SIGKILLed instead of exiting cleanly.
    """
    global _stop_requested
    _stop_requested = True


def _on_sigchld(signum: int, frame: FrameType | None) -> None:
    reap_children()


def _install_handlers(*, reap: bool) -> None:
    """Install signal handlers, tolerating a non-main thread (i.e. a test)."""
    try:
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        if reap:
            signal.signal(signal.SIGCHLD, _on_sigchld)
    except ValueError:
        pass


def _sleep_interruptibly(seconds: float, *, slice_seconds: float) -> None:
    """Sleep, but never for longer than ``slice_seconds`` without looking up.

    A signal handler in Python does not cut a ``time.sleep`` short: since PEP
    475 the sleep is *resumed* for its remaining time, and the Python-level
    handler only runs when it finally returns. So a single 30 s sleep means a
    SIGTERM is acted on up to 30 s later — at or past the kubelet's default
    grace period, which turns a clean exit into a SIGKILL and a container that
    looks like it crashed.
    """
    deadline = time.monotonic() + seconds
    while not _stop_requested:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(slice_seconds, remaining))


def idle(
    status: ReaperStatus,
    *,
    interval: float = 30.0,
    iterations: int | None = None,
    slice_seconds: float = IDLE_SLICE,
) -> int:
    """Sit still as a restart-tolerant PID 1 until asked to stop.

    ``iterations`` bounds the loop for tests; production passes ``None`` and
    leaves on SIGTERM. The kubelet's ``SIGTERM`` is the container's normal end
    of life, so exiting 0 on it keeps a stopped agent out of the crash-loop
    accounting — which it only does if the signal is *noticed* promptly, hence
    the sliced sleep.
    """
    global _stop_requested
    _stop_requested = False
    _install_handlers(reap=status.is_init)
    count = 0
    while not _stop_requested:
        if iterations is not None and count >= iterations:
            break
        if status.is_init:
            reap_children()
        _sleep_interruptibly(interval, slice_seconds=slice_seconds)
        count += 1
    return 0


def main(args: Sequence[str] | None = None) -> int:
    """Entry point for ``podbench agent``. CLI wiring lives elsewhere."""
    parser = argparse.ArgumentParser(
        prog="podbench agent",
        description="Prepare the debug container for ssh and idle as its PID 1.",
    )
    parser.add_argument(
        "--ensure-only",
        action="store_true",
        help="prepare the container and exit instead of idling",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="run the startup checks and exit; non-zero if any fails",
    )
    parser.add_argument(
        "--print-host-key",
        action="store_true",
        help="print the host public key for the launcher's known_hosts",
    )
    parser.add_argument(
        "--no-self-check",
        action="store_true",
        help="skip the startup checks (they cost a subprocess and ~0.2 s)",
    )
    parser.add_argument(
        "--idle-interval",
        type=float,
        default=30.0,
        help="seconds between reap sweeps while idling",
    )
    opts = parser.parse_args(args)

    layout = SshdLayout.for_uid(os.geteuid())
    if not opts.self_check:
        for change in ensure_all(layout):
            _say(change)

    failures = 0
    if opts.self_check or not opts.no_self_check:
        for result in self_check(layout):
            _say(str(result))
            failures += 0 if result.ok else 1
        if opts.self_check:
            return 1 if failures else 0
    if failures:
        return 1

    if opts.print_host_key:
        public = read_host_public_key(layout)
        if public is None:
            _say("no host public key available")
            return 1
        print(public)
        return 0

    status = reaper_status()
    _say(status.note)
    if opts.ensure_only:
        return 0
    return idle(status, interval=opts.idle_interval)
