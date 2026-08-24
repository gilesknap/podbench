"""Install debugpy *into the target* from the seat, rather than asking for a rebuild.

``debug-config``'s Python refusal used to end in "install debugpy into the app
image": true, durable, and unavailable to the person who is already attached to
a running pod at 3 a.m., which is the situation the seat exists for. The seat
can satisfy the prerequisite itself — it ships ``uv``, and ``/proc/<pid>/root``
is the target's own filesystem, which the seat writes into wherever it is
allowed to.

**Which is not everywhere, and the rung decides.** On ``full`` the seat is uid 0
and ``CAP_DAC_OVERRIDE`` settles it whatever the target's uid and modes say. On
``degraded`` — the seat matched to the target's own uid with ``CapEff
0000000000000000``, which is what a beamline pod admits — nothing bridges those
modes and they are the entire check: ``/opt`` is ``drwxr-xr-x root root`` in a
stock image, so the default destination below is refused by ordinary file
permissions (measured on ``bl47p-ea-fastcs-01-0``, 2026-08-24). That is why the
destination is chosen from the pod's layout by
``launcher.provision_destination`` rather than being one constant, and why
:func:`blocker_sentence` asks the seat's own credentials before explaining a
refusal.

Proved on the bench on 2026-08-16 (issue #45): k3s, seat Python 3.11, target
Python 3.12. The install resolved *for 3.12 while running on 3.11*, the
injection then took ~3 s, and the target's own ``/proc/1/maps`` showed
``pydevd_cython.cpython-312-...so`` loaded out of the provisioned directory,
with the workload still serving and zero restarts.

**``--python-version`` is why this is a uv install and not a copy.** The image
installs debugpy for the *seat's* interpreter, so its tree carries
``pydevd_cython.cpython-311-...so`` alone; copied into a 3.12 target that
accelerator is skipped and pydevd falls back to pure Python — right answers, far
slower, and nothing says so. uv resolves for an interpreter it is not running,
which is what gets the matching wheel. Copying the seat's tree stays the
fallback for a pod with no egress; it is not the path.

Three things this costs, named wherever it is offered (:data:`CAVEATS`):
egress, since uv resolves and downloads from an index; an injection no container
restart survives; and ~15 MB of storage, which lands on the budget the seat
*shares with the workload and cannot reserve* — because an ephemeral container
may not carry ``resources`` (report 3.9) — unless the destination is a volume,
which is also the only kind of destination whose install outlives a restart.

That is why the caller has to ask. ``flavour.injection_command`` is printed
rather than run because ptracing the workload is not something authoring a
launch.json may do on its own, and writing 15 MB into the workload's writable
layer is the larger of the two mutations.

Which is also why ``--provision`` does *both*. Having ordered them, asking twice
for the lesser one made no sense: the flag installed debugpy, emitted a
configuration that connects to ``127.0.0.1:5678``, and left nothing listening
there — so the first F5 was `ECONNREFUSED` and the remedy was a paste. One flag
means "make this target debuggable", and :func:`inject_debugpy` is the second
half of it. A bare ``debug-config`` still only prints, because that really is
authoring a launch.json and nothing more.

**And "debuggable" is now asserted rather than inferred.** The success line used
to be returned on the injector's exit code alone; measured on the live target
that code was 0, the port was open and in ``LISTEN``, and a session still could
not start, because the adapter accepted the connection and never answered
``initialize``. So :func:`inject_debugpy` finishes by asking it — one DAP
handshake through :mod:`podbench.dap` — and reports three genuinely different
things instead of one: the injector failed, the injector succeeded and the
adapter answered, or the injector succeeded and no session could be started
anyway. The third of those is what the live target was, and it is not a success
line.

The one genuinely new precondition is a **writable rootfs**, so it is probed
rather than assumed (brief, "Diagnose, don't mystify"): ``readOnlyRootFilesystem:
true`` lives in the *target's* mount namespace, so it surfaces here only as
``EROFS`` on the write itself. Where it is set there is usually still a writable
``emptyDir`` or tmpfs in the pod, which is why the destination is a parameter
and not a constant.
"""

from __future__ import annotations

import errno
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .dap import HANDSHAKE_TIMEOUT_SECONDS, LOOPBACK, Answer, Handshake
from .dap import initialize as dap_initialize
from .kubectl import CommandResult, Runner, run_subprocess
from .proc import DEFAULT_PROC, Credentials, read_credentials, sysroot_path

__all__ = [
    "CAVEATS",
    "CLAIM_DEST_NAME",
    "PROVISION_DEST",
    "Injected",
    "Prover",
    "Provisioned",
    "blocker_sentence",
    "claim_destination",
    "inject_debugpy",
    "provision_command",
    "provision_debugpy",
    "provision_paste",
    "target_destination",
    "writable_blocker",
]

PROVISION_DEST = "/opt/podbench-debugpy"
"""Where a provisioned debugpy goes on a pod with no layout to read.

Outside every directory a package manager owns, so nothing in the app image can
be shadowed by it, and one fixed path rather than a guess at the target's
site-packages — which is also what lets ``flavour._target_debugpy`` find it
again with one extra ``stat`` instead of a walk.

The *default* and no longer the only answer: it is a root-owned directory in a
stock image, so a seat that is not root cannot write it, and on a hotfixed pod
:func:`claim_destination` is chosen instead. ``--provision-dest`` still wins
over both.
"""

CLAIM_DEST_NAME = ".podbench-debugpy"
"""What a provisioned debugpy is called *inside* a hotfix claim.

:data:`PROVISION_DEST` gets to be outside every directory a package manager
owns; the claim is the opposite of that, being the application's own checkout,
usually a git one. So what keeps this from colliding with the user's tree is the
name rather than the place: podbench's own prefix, which nothing else would
choose, dotted so it stays out of a listing. It is the path the 2026-08-24 run
proved the whole install and injection against.
"""

CAVEATS = (
    "needs egress: uv downloads the wheel from an index",
    "no container restart survives the injection",
    "~15 MB of ephemeral storage shared with the workload, unless "
    "--provision-dest named a volume - which is also what decides whether the "
    "install outlives a restart",
)
"""What provisioning costs, printed with it rather than left to be discovered.

All three were measured or are structural, and none of them announces itself:
no egress looks like a resolver error, a restart looks like the debugger simply
stopping, and ephemeral-storage eviction takes the *workload* with it.

The restart caveat used to read "neither the install nor the injection", which
was true while every destination was the container's own writable layer and is
false now that a hotfixed pod's default is the claim: a PVC outlives the
container, so the install is still there after a restart and the injection is
not (2026-08-24). Both halves of that split hang off the same fact - whether the
destination is a volume - which is why it is said once, on the clause about
storage, rather than hedged twice. ``docs/reference/cli.md`` carries the long
form.

A clause each, because they are printed inline in a sentence the user is about
to act on rather than read as a page. Why each one is true is the module
docstring's job and ``docs/how-to/vscode-remote-ssh.md``'s; here the reader
needs to recognise the failure when they hit it, which the noun alone does.
"""

_UNKNOWN_VERSION = "'<X.Y>'"
"""Stands in when the target's Python version could not be read.

Left visible on purpose: guessing the seat's own version would produce a command
that runs, installs the wrong wheel, and degrades pydevd to pure Python without
a word. Quoted because the whole line is printed as a paste: bare ``<X.Y>`` is
an input redirection, and pasting it leaves an interactive shell sitting at a
continuation prompt instead of showing uv rejecting the version.
"""

_PROBE_NAME = ".podbench-provision-probe"


def target_destination(pid: int, dest: str = PROVISION_DEST) -> str:
    """``dest`` inside ``pid``'s rootfs, spelled so both namespaces agree.

    The same string is the driver's ``PYTHONPATH`` and the path debugpy injects
    into the target, which is the whole reason it is ``/proc/<pid>/root/...``
    (issue #20's live proof, step 3).

    >>> target_destination(1)
    '/proc/1/root/opt/podbench-debugpy'
    """
    return f"{sysroot_path(pid)}/{dest.lstrip('/')}"


def claim_destination(mount_path: str) -> str:
    """Where debugpy goes on a hotfixed pod, given where the claim is mounted.

    The claim is the one tree that is *the same inode in both mount namespaces*
    — ``stat`` says ``1048592:64`` for ``/podbench/app`` and for
    ``/proc/13/root/podbench/app`` alike — which is the property the
    ``gdb-across-namespaces`` skill demands of any path both halves of a debug
    session name. It is also writable by a seat that is not root, which is what
    ``/opt`` is not, and a volume, so the install survives a restart.

    ``mount_path`` is read off the seat's own ``volumeMounts`` by
    ``launcher.provision_destination`` and never assumed to be
    :data:`~podbench.model.HOTFIX_APP_PATH`: the application chose the
    mountPath and podbench only matched it.

    >>> claim_destination("/podbench/app")
    '/podbench/app/.podbench-debugpy'
    """
    return f"{mount_path.rstrip('/')}/{CLAIM_DEST_NAME}"


def provision_command(destination: str, python_version: str) -> tuple[str, ...]:
    """The uv install, as argv.

    ``--python-version`` rather than an interpreter: uv resolves for a version it
    is not running, which is the only way a 3.11 seat lands the accelerators a
    3.12 target will actually load.

    ``--no-cache`` is what makes the ~15 MB in :data:`CAVEATS` the true cost. uv
    downloads into its cache in the *seat's* writable layer and materialises from
    there into ``--target``; the two are different filesystems, so the hardlink
    is impossible and both copies exist — and both land on the one pod-level
    ephemeral-storage budget the caveat is warning about.

    >>> provision_command("/proc/1/root/opt/dbg", "3.12")[:4]
    ('uv', 'pip', 'install', '--no-cache')
    """
    return (
        "uv",
        "pip",
        "install",
        "--no-cache",
        "--python-version",
        python_version,
        "--target",
        destination,
        "debugpy",
    )


def provision_paste(
    pid: int, *, dest: str = PROVISION_DEST, python_version: str | None = None
) -> str:
    """The same command as a human pastes it, every field already filled in.

    This is what replaced "install debugpy into the app image" in the refusal:
    the remedy for a prerequisite the seat can meet has to be runnable where it
    is printed, not a rebuild.

    >>> print(provision_paste(1, dest="/dbg", python_version="3.12"))
    uv pip install --no-cache --python-version 3.12 --target /proc/1/root/dbg debugpy
    """
    destination = target_destination(pid, dest)
    return " ".join(provision_command(destination, python_version or _UNKNOWN_VERSION))


def blocker_sentence(
    error: OSError, destination: Path, *, credentials: Credentials | None = None
) -> str:
    """Name the mechanism behind a failed write, in ``capreport``'s house style.

    ``EROFS`` is the one worth spelling out whatever the seat is: a mount flag
    lives in the *target's* mount namespace, and no credential this container
    holds reaches it.

    ``credentials`` is the seat's own ``/proc/self/status``, and ``EACCES`` is
    where it changes the answer. At uid 0 the file modes really are ruled out,
    because ``CAP_DAC_OVERRIDE`` overrides them; below it nothing does, and the
    sentence that ruled them out sent a beamline reader to SELinux and
    ``PTRACE_MODE_READ`` while the whole cause was a root-owned ``/opt`` and a
    seat at uid 37887 (2026-08-24). ``None`` is *unknown*, not root: it keeps
    the older text, which names more mechanisms than it excludes.

    >>> import errno
    >>> sentence = blocker_sentence(OSError(errno.EROFS, "ro"), Path("/opt/x"))
    >>> sentence.startswith("/opt/x is read-only: the target has")
    True
    >>> from .proc import Capabilities, Credentials
    >>> seat = Credentials(37887, 37887, Capabilities(0, 0, 0))
    >>> denied = blocker_sentence(
    ...     OSError(errno.EACCES, "denied"), Path("/opt/x"), credentials=seat
    ... )
    >>> "uid 37887" in denied and "CAP_DAC_OVERRIDE" not in denied
    True
    """
    if error.errno == errno.EROFS:
        return (
            f"{destination} is read-only: the target has readOnlyRootFilesystem: "
            "true, and that mount flag lives in the target's own mount namespace, "
            "so a write through /proc/<pid>/root gets EROFS however privileged "
            "this seat is. Point --provision-dest at a writable mount - an "
            "emptyDir or a tmpfs the pod already declares is the usual one"
        )
    if error.errno == errno.ENOSPC:
        # Deliberately *not* the pod's ephemeral-storage limit: that one is
        # enforced by the kubelet's periodic disk poll and arrives as an
        # eviction with no errno at all, so an errno here is the filesystem
        # itself, and naming the wrong of the two sends the reader to the wrong
        # `describe`.
        return (
            f"no space left at {destination}: this is the filesystem backing it "
            "- the node's disk, or the `sizeLimit` on the emptyDir or tmpfs "
            "--provision-dest points at. The pod's own ephemeral-storage limit "
            "is a different mechanism, polled by the kubelet and reported as an "
            "eviction rather than as an errno"
        )
    if error.errno in (errno.EACCES, errno.EPERM):
        if credentials is not None and credentials.uid not in (None, 0):
            # The degraded rung, where the sentence below is not merely
            # incomplete but excludes the answer: a seat at the target's own
            # uid holds no capability at all (`capabilities.add` on a non-root
            # uid is a silent no-op), so the target's ownership and modes are
            # the whole of the check. Measured, not reasoned: `mkdir
            # /proc/13/root/opt/podbench-debugpy` refused for uid 37887 while
            # `ls /proc/13/root` succeeded, which is what rules the traversal
            # out - so that check is offered here rather than a mechanism.
            return (
                f"permission denied at {destination}: this seat runs as uid "
                f"{credentials.uid} with CapEff "
                f"{credentials.capabilities.effective_hex}, so it holds nothing "
                "that overrides the target's own ownership and modes - they are "
                "the whole of the check, and the directory this path sits in "
                "does not grant that uid a write. `ls -ld` on it through "
                "/proc/<pid>/root names the owner; if `ls /proc/<pid>/root` is "
                "refused too then it is the traversal and not this directory. "
                "--provision-dest points the install at a tree the seat can "
                "write, which on a hotfixed pod is the claim"
            )
        # Three distinct causes, and CAP_DAC_OVERRIDE covers only the first.
        # Report 3.11 measured the second: at uid 0 with an empty effective set
        # even `ls /proc/<pid>/root` is denied, because the traversal takes
        # PTRACE_MODE_READ. The third is R8's unvalidated case, and the one seen
        # on the Diamond cluster - and an LSM is not a capability check.
        return (
            f"permission denied at {destination}: uid 0 in this seat carries "
            "CAP_DAC_OVERRIDE, so the target's own file modes are not it. What "
            "is left is the /proc/<pid>/root traversal, which takes "
            "PTRACE_MODE_READ and so is refused to a root seat with no "
            "CAP_SYS_PTRACE (report 3.11), or an LSM denying the cross-container "
            "write - SELinux or a custom AppArmor profile, neither of which "
            "CAP_DAC_OVERRIDE touches. `capreport` names the rung and prints "
            "both profiles"
        )
    return f"cannot write {destination}: {error.strerror or error}"


def writable_blocker(
    destination: Path, *, credentials: Credentials | None = None
) -> str | None:
    """Why the seat cannot write ``destination``, or ``None`` when it can.

    Probed by writing, because nothing else is right on both rungs: the flag
    that answers ``EROFS`` is in the target's mount namespace and is not
    readable from here, and ``os.access`` weighs this process's uid against the
    target's modes — the whole answer for a degraded seat, and irrelevant for a
    root one, where ``CAP_DAC_OVERRIDE`` overrides them. A write is what both
    of those reduce to.

    ``credentials`` only reaches :func:`blocker_sentence`, which needs them to
    explain a refusal rather than to predict one.
    """
    try:
        destination.mkdir(parents=True, exist_ok=True)
        probe = destination / _PROBE_NAME
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as error:
        return blocker_sentence(error, destination, credentials=credentials)
    return None


@dataclass(frozen=True)
class Provisioned:
    """What the install did, and what the user has to be told either way."""

    path: str | None
    """Where the target can now import debugpy from, or ``None`` if it cannot."""

    messages: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.path is not None


def _last_line(result: CommandResult) -> str:
    """What a failed injection actually said, on one line.

    ``stderr`` first and ``stdout`` only as a fallback, because the driver here
    is gdb wrapped in debugpy's bootstrap: the useful sentence ("ptrace:
    Operation not permitted") is on stderr, while stdout is pages of MI.
    """
    for stream in (result.stderr, result.stdout):
        lines = [line.strip() for line in stream.splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return "it said nothing"


INJECTION_TIMEOUT_SECONDS = 30
"""How long the injection may hold the workload before podbench takes it back.

Issue #76: the attach ran to whatever end gdb reached, and the duration
:class:`Injected` reported was therefore the one number here podbench did not
control. It stops the app for the whole of it, against readiness budgets
:mod:`podbench.budget` computes in tens of seconds, so an injection that has
not returned is not "still working" in any sense the pod agrees with.

Thirty seconds is an order of magnitude above the 3.4 s measured on the k3s
bench and well below the point at which a stopped app has any chance of still
being in its Service. It is a bound, not a budget: the good case never meets it.
"""

_INJECTION_KILL_AFTER_SECONDS = 5
"""Grace between the TERM and the KILL, in seconds.

gdb quits on TERM and quitting detaches an inferior it attached to, so the app
resumes with its state intact. The KILL is for one too stuck to do even that,
and the app resumes there too - the kernel detaches a tracee whose tracer dies.
"""

_TIMED_OUT_CODES = frozenset({124, 128 + 9})
"""``timeout``'s own exit codes: 124 when it fired, 137 when the KILL was needed.

GNU ``timeout`` reserves 124 for itself and reports a killed command as
128+signal, so these read as "podbench stopped this" rather than as anything the
driver said - which is why the message they select describes the bound and does
not put words in gdb's mouth.
"""


Prover = Callable[[int], Answer]
"""How the injection is proved: a port in, a DAP :class:`~podbench.dap.Answer` out.

A seam rather than a direct call for the reason ``port_chooser`` is one in
:func:`podbench.vscode.main` — the default really opens a socket, and a unit
suite that may not do that still has to exercise every outcome. It is also what
lets the DAP client be tested on its own, against a loopback server the test
owns.
"""


@dataclass(frozen=True)
class Injected:
    """What the injection did, how long the target was held, and whether it took."""

    ok: bool
    """Whether the injector returned cleanly, and **not** whether the app is
    debuggable.

    Kept as the injector's own verdict on purpose. The caller uses it to decide
    where to re-probe for a listener, and a run that started a wedged adapter has
    still moved the server off the conventional port — reporting it as a failure
    would put "nothing is listening" over the top of a port that genuinely is.
    Whether a debug session can be started is :attr:`session`, and the two
    disagreeing is exactly the 2026-08-24 measurement.
    """

    seconds: float
    """Wall clock for the whole ptrace attach.

    Reported because it is the only number here that a probed pod cares about:
    the app answers no probe while it is stopped, and the readiness budget is
    tens of seconds. 3.4 s measured on the k3s bench against an 11-16 s
    readiness budget, matching issue #45's ~3 s.

    Bounded as well as measured since #76: it cannot now exceed
    :data:`INJECTION_TIMEOUT_SECONDS` by more than the grace period, whatever
    the driver was waiting for.
    """

    messages: tuple[str, ...] = ()

    session: Answer | None = None
    """What the adapter said when the seat asked it to ``initialize``.

    ``None`` means no handshake was attempted, which is every path where the
    injector itself failed: there is nothing to ask, and a refusal invented here
    would read as a second failure rather than as the absence of a question.
    """

    @property
    def proved(self) -> bool:
        """Whether a debug session was actually started against the target.

        The only claim this module is entitled to make about F5 working, and the
        only one the 2026-08-24 redo could not falsify.
        """
        return self.session is not None and self.session.ok


def inject_debugpy(
    command: str,
    *,
    runner: Runner | None = None,
    port: int,
    timeout: int = INJECTION_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    prove: Prover = dap_initialize,
) -> Injected:
    """Run the injection ``command`` this seat would otherwise have printed.

    ``command`` is taken rather than rebuilt so that what runs is character for
    character what :func:`podbench.flavour.injection_command` prints — the two
    cannot drift into a paste that works and a run that does not, and the
    continuation backslash is handled by ``sh`` for both.

    Timed rather than merely run, and now bounded as well (#76). The ptrace
    attach *stops the app*, so on a probed pod the duration is the thing that
    decides whether this was free or whether the pod quietly left its Service —
    and a duration nothing limits is a pause nothing limits. ``clock`` is a seam
    so the unit suite can assert the number without one.

    The bound is coreutils ``timeout`` and deliberately not ``subprocess``'s:
    the driver forks gdb, and a Python-side timeout kills only the ``sh`` we
    started, leaving that gdb attached to the workload with nobody waiting for
    it. ``timeout`` puts the command in a process group of its own and signals
    the group, so the thing actually holding the app is what receives the
    signal. The ``command`` inside the wrapper is still untouched, which is what
    keeps it character for character what was printed.

    **An exit code of 0 ends the injection and does not end the question.** On
    the live target it was 0, the port was open, and the adapter never answered
    ``initialize`` — so a success line returned here was a claim about F5 that
    nothing had checked. ``prove`` is what checks it: a DAP handshake against the
    port the emitted configuration names, whose four outcomes are reported as
    four different things because the reader chases a different half in each.
    """
    run = runner if runner is not None else run_subprocess
    started = clock()
    argv = [
        "timeout",
        f"--kill-after={_INJECTION_KILL_AFTER_SECONDS}",
        str(timeout),
        "sh",
        "-c",
        command,
    ]
    try:
        result = run(argv)
    except OSError as error:
        # An in-pod verb may not traceback at somebody: `timeout` is coreutils
        # and is in podbench's image, but this is the one line here that assumes
        # a binary other than `sh`, and the workload was never touched.
        return Injected(
            False,
            clock() - started,
            (
                f"the injection did not start: {argv[0]} could not be run "
                f"({error}), and podbench will not attach without the bound it "
                "provides. The workload was not stopped",
            ),
        )
    seconds = clock() - started
    if result.returncode in _TIMED_OUT_CODES:
        return Injected(
            False,
            seconds,
            (
                f"injection did not return within {timeout}s and was stopped "
                f"after {seconds:.1f}s: {_last_line(result)}",
                "the app resumes as soon as its tracer dies, so the pause ends "
                "here rather than lasting as long as gdb felt like waiting. A "
                "slow symbol server is the usual reason: this seat drops "
                "DEBUGINFOD_URLS from ssh sessions when it cannot reach the "
                "server at all, and bounds each fetch with DEBUGINFOD_TIMEOUT "
                "when it can",
            ),
        )
    if result.returncode != 0:
        return Injected(
            False,
            seconds,
            (
                f"injection exited {result.returncode} after {seconds:.1f}s: "
                f"{_last_line(result)}",
                "the app is not left stopped by a failed attach - gdb detaches "
                "on the way out - so this costs the pause and nothing else",
            ),
        )
    # The injector is done and the app is running again; everything below is a
    # question asked of the server it left behind, at the cost of one loopback
    # socket and nothing the workload pays for.
    answer = prove(port)
    return Injected(True, seconds, _proof(answer, seconds=seconds, port=port), answer)


def _proof(answer: Answer, *, seconds: float, port: int) -> tuple[str, ...]:
    """What to say about an injection that returned 0, given what the adapter said.

    Four sentences and not one, because the reader's next move differs in every
    case and three of them were a success line until 2026-08-24. What they share
    is the injection's own duration: the app was stopped for it whatever came of
    it, and on a probed pod that is the number the pod cares about.
    """
    if answer.outcome is Handshake.ANSWERED:
        return (
            f"injected in {seconds:.1f}s and the adapter answered a DAP "
            f"`initialize` on {LOOPBACK}:{port} in {answer.seconds:.2f}s, so the "
            "app is debuggable rather than merely listening - F5 on the "
            "configuration this run emitted reaches it",
        )
    if answer.outcome is Handshake.REFUSED:
        return (
            f"injected in {seconds:.1f}s, but nothing is listening on "
            f"{LOOPBACK}:{port} ({answer.detail}): the injector returned 0 and "
            "left no server behind, so no debug session can be started and a "
            "configuration pointing here connects to a closed port. The "
            "injection command printed below runs the same thing by hand",
        )
    if answer.outcome is Handshake.REJECTED:
        return (
            f"injected in {seconds:.1f}s, and something on {LOOPBACK}:{port} "
            f"answered without being a debug adapter ({answer.detail}), so no "
            "debug session can be started and what holds that port is not this "
            "run's server. Pass `--port` to put the server somewhere nothing "
            "else is",
        )
    return (
        f"injected in {seconds:.1f}s and {LOOPBACK}:{port} accepts a connection, "
        "but the adapter did not answer a DAP `initialize` within "
        f"{HANDSHAKE_TIMEOUT_SECONDS:.0f}s ({answer.detail}), so no debug "
        "session could be started. What is established is that the injection ran "
        "and the port is open; what is not is a session of any kind, and F5 will "
        "hang here the same way. DEBUGPY_LOG_DIR in the target is where the "
        "adapter and the debuggee record what they said to each other",
    )


def provision_debugpy(
    pid: int,
    *,
    python_version: str,
    dest: str = PROVISION_DEST,
    proc: Path = DEFAULT_PROC,
    runner: Runner | None = None,
) -> Provisioned:
    """Install debugpy into ``pid``'s rootfs, having first proved it can be.

    The probe comes first so that a read-only rootfs is named rather than
    surfacing as whatever uv says about a directory it could not create, and the
    caveats are printed on success rather than on the next restart.
    """
    run = runner if runner is not None else run_subprocess
    destination = Path(proc) / str(pid) / "root" / dest.lstrip("/")
    # Read here rather than in `writable_blocker`, so the probe stays a probe:
    # what this container is running as is evidence for the *explanation* of a
    # refusal, and reading it under the same `proc` root keeps the synthetic
    # trees the unit suite builds answering for the seat as well as the target.
    blocker = writable_blocker(destination, credentials=read_credentials(proc=proc))
    if blocker is not None:
        return Provisioned(None, (blocker,))
    argv = provision_command(str(destination), python_version)
    result = run(list(argv))
    if result.returncode != 0:
        return Provisioned(
            None,
            (
                f"`{' '.join(argv)}` exited {result.returncode}: "
                f"{result.stderr.strip() or result.stdout.strip()}",
                "a resolver or network failure here is the no-egress case; the "
                "fallback is to copy this seat's own debugpy tree (`capreport` "
                "prints where it is), which works but ships this seat's "
                "interpreter's accelerators alone, so pydevd degrades to pure "
                "Python in a target on another version",
            ),
        )
    return Provisioned(
        target_destination(pid, dest),
        (
            f"installed debugpy for Python {python_version} into "
            f"{target_destination(pid, dest)}",
            *CAVEATS,
        ),
    )
