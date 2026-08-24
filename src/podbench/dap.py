"""Ask the debug adapter the one question only a working one can answer.

``--provision`` used to report success on the injector's exit code. Measured
2026-08-24 against a hotfixed ``bl47p-ea-fastcs-01-0``: the injector exited 0,
the port was **open and in LISTEN**, the target's own log recorded no error, and
a debug session still could not be started — the adapter accepted the TCP
connection and never answered ``initialize`` (15 s, 0 bytes). So neither the exit
code nor an open socket is proof, and podbench claimed a working debugger from
evidence that did not support one
(``.claude/evidence/phase7-vscode-and-the-two-failures.md`` §4).

What *is* proof is the first thing VS Code itself does. :func:`initialize` opens
the socket the emitted configuration names, sends a DAP ``initialize`` request,
and requires a ``response`` with ``success: true`` back. The adapter answers that
one from a constant capability table without touching the debuggee at all
(debugpy 2026.6.0, ``adapter/clients.py`` ``initialize_request``), so it costs
the workload nothing and cannot be slow for an honest reason — which is what
makes silence here a finding rather than a race.

**The socket is closed and no DAP ``disconnect`` is sent, and that is not
laziness.** An adapter serves one client and cannot be reconnected to within a
session, so ``Client.disconnect_request`` calls ``servers.stop_serving()`` and
closes the client listener *for good*: a polite goodbye is exactly what would
burn the port this probe exists to prove. A plain socket close takes the other
path — ``Component.disconnect`` finalises the session, the debuggee is detached
rather than terminated (no launcher, so ``terminateDebuggee`` is false) and its
connection stays registered for the next session, while the listener keeps
serving. The probe is also only ever run on a server *this run just started*
(``vscode._inject`` withdraws when something was already listening), so there is
no colleague's session to interrupt either way.

Standard library only, and deliberately so: podbench has one runtime dependency
and it is the CLI (``tests/test_packaging.py``). Importing debugpy in the
launcher to talk to debugpy would also be the wrong shape — the seat asks the
adapter over the wire, exactly as the editor will.
"""

from __future__ import annotations

import json
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

__all__ = [
    "HANDSHAKE_TIMEOUT_SECONDS",
    "LOOPBACK",
    "Answer",
    "Handshake",
    "initialize",
]

LOOPBACK = "127.0.0.1"
"""Where a server started in the target is reachable from the seat.

The seat and the app share the pod's network namespace, so nothing is
port-forwarded or tunnelled even though they are separate containers — the same
fact :data:`podbench.flavour.DEBUGPY_PORT` records, said here because this module
is the one that opens the socket.
"""

HANDSHAKE_TIMEOUT_SECONDS = 5.0
"""How long the adapter gets to answer ``initialize`` before podbench gives up.

A bound, not a budget: the good case never meets it. ``initialize_request``
replies from a literal dict of capabilities without consulting the debuggee, over
a loopback socket in this pod's own network namespace, so a working adapter
answers in milliseconds — and the injector has already returned, which on a
healthy target means ``debugpy.listen()`` completed before this is asked at all.

Five seconds is chosen against both measurements from 2026-08-24: the injection
itself cost **8.7 s**, so a bound anywhere near it would double this verb's wall
clock for the failing case alone, and the wedged adapter answered **nothing in
15 s**, so waiting longer only reproduces the hang podbench is here to report.
It sits three orders of magnitude above a loopback round trip and a third of the
measured silence, which is the widest gap available between the two.
"""

_HEADER_END = b"\r\n\r\n"
_CONTENT_LENGTH = "content-length"
_MAX_MESSAGE_BYTES = 1 << 20
"""A cap on what one framed message may claim to be.

Nothing debugpy sends here is large — the ``initialize`` response is a few
hundred bytes — and the peer is whatever happens to hold the port, which on a
``hostNetwork`` pod need not be debugpy at all. A ``Content-Length`` this module
believes without a bound is a header away from an allocation the seat cannot
afford, against a memory limit it shares with the workload and cannot reserve.
"""


class Handshake(Enum):
    """What came back when the seat asked the adapter to ``initialize``.

    Four outcomes rather than a boolean, because the reader acts differently on
    each of them and the last three were all "success" until 2026-08-24.
    """

    ANSWERED = "answered"
    """A ``response`` to ``initialize`` with ``success: true``. The one outcome
    that says a debug session can be started."""

    REFUSED = "refused"
    """Nothing accepted the connection: the injector returned 0 and left no
    server behind. Distinct from :data:`SILENT` because the halves to chase are
    different — here the injection did not take, there it did."""

    SILENT = "silent"
    """The connection was accepted and no answer arrived within the bound. This
    is the state the live target was in, and the whole reason this module
    exists: the port is genuinely open and the session still cannot start."""

    REJECTED = "rejected"
    """Something answered, and it was not a successful ``initialize`` response —
    a ``success: false``, or bytes that are not DAP framing at all, which is
    what another process holding the port looks like from here."""


@dataclass(frozen=True)
class Answer:
    """The outcome of one handshake, and what the socket actually said."""

    outcome: Handshake

    detail: str
    """The mechanism, in the peer's or the kernel's own words.

    Relayed rather than paraphrased: ``Connection refused`` and a DAP
    ``message`` are somebody else's text, and the sentence podbench wraps around
    them is already saying what podbench knows.
    """

    seconds: float = 0.0
    """Wall clock for connect plus handshake, so a slow answer is visible as
    one rather than averaging into the injection's own duration."""

    @property
    def ok(self) -> bool:
        """Whether a debug session could be started against this port.

        >>> Answer(Handshake.SILENT, "").ok
        False
        """
        return self.outcome is Handshake.ANSWERED


def frame(message: object) -> bytes:
    """``message`` as DAP puts it on the wire.

    ``Content-Length`` in bytes of the UTF-8 body, a blank line, then the body.
    The header is ASCII and the body is not necessarily, which is why the length
    is measured after encoding rather than on the string.

    >>> frame({"seq": 1})
    b'Content-Length: 10\\r\\n\\r\\n{"seq": 1}'
    """
    body = json.dumps(message).encode()
    return f"Content-Length: {len(body)}{_HEADER_END.decode()}".encode() + body


def take_message(buffer: bytes) -> tuple[dict[str, Any] | None, bytes, str | None]:
    """One framed message off the front of ``buffer``, and what is left.

    Returns ``(message, rest, complaint)``. ``message`` is ``None`` while the
    buffer holds less than a whole frame, which is the ordinary case on a stream
    socket; ``complaint`` is set only when what arrived cannot be DAP at all, so
    a caller can tell "not yet" from "not this protocol" without inspecting the
    bytes itself.

    >>> take_message(b'Content-Length: 10\\r\\n\\r\\n{"seq": 1}')[0]
    {'seq': 1}
    >>> take_message(b'Content-Length: 10\\r\\n\\r\\n{"seq"')[0] is None
    True
    >>> take_message(b'HTTP/1.1 400 Bad Request\\r\\n\\r\\n')[2]
    'no Content-Length header, so this is not DAP framing'
    """
    end = buffer.find(_HEADER_END)
    if end < 0:
        if len(buffer) > _MAX_MESSAGE_BYTES:
            return None, buffer, "no DAP header in the first megabyte"
        return None, buffer, None
    try:
        header = buffer[:end].decode("ascii")
    except UnicodeDecodeError:
        return None, buffer, "the header is not ASCII, so this is not DAP framing"
    length: int | None = None
    for line in header.splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == _CONTENT_LENGTH:
            try:
                length = int(value.strip())
            except ValueError:
                said = value.strip()
                return None, buffer, f"Content-Length is not a number: {said!r}"
    if length is None:
        return None, buffer, "no Content-Length header, so this is not DAP framing"
    if not 0 <= length <= _MAX_MESSAGE_BYTES:
        return None, buffer, f"Content-Length is {length}, which is not a message"
    start = end + len(_HEADER_END)
    if len(buffer) < start + length:
        return None, buffer, None
    body = buffer[start : start + length]
    rest = buffer[start + length :]
    try:
        decoded = json.loads(body.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, rest, f"the body is not JSON: {error}"
    if not isinstance(decoded, dict):
        return None, rest, "the body is JSON but not a DAP message"
    return cast("dict[str, Any]", decoded), rest, None


def _request(seq: int = 1) -> dict[str, Any]:
    """The ``initialize`` request, spelled the way VS Code spells it.

    ``adapterID`` is ``debugpy`` because that is what the emitted configuration's
    ``type`` is, and the expectations are the defaults the adapter's own
    ``Expectations`` declares — this asks the question the editor will ask, not a
    cheaper one that might be answered where the editor's is not.

    >>> _request()["command"], _request()["type"]
    ('initialize', 'request')
    """
    return {
        "seq": seq,
        "type": "request",
        "command": "initialize",
        "arguments": {
            "clientID": "podbench",
            "clientName": "podbench debug-config",
            "adapterID": "debugpy",
            "locale": "en-US",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "pathFormat": "path",
        },
    }


def initialize(
    port: int,
    *,
    host: str = LOOPBACK,
    timeout: float = HANDSHAKE_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Answer:
    """Connect to ``host:port`` and require a successful DAP ``initialize``.

    The one call in this module that opens a socket, and the seam every caller
    injects around: ``clock`` is here for the same reason
    :func:`podbench.provision.inject_debugpy` takes one — so a duration can be
    asserted without spending it.

    Messages that are not the answer are read past rather than treated as one.
    debugpy sends two telemetry ``output`` events the moment a client connects
    (``adapter/clients.py``, ``Client.__init__``), so the first frame off this
    socket is never the response and a client that read exactly one would report
    a working adapter as broken.
    """
    started = clock()
    deadline = started + timeout
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as error:
        # Refused, unreachable and a connect that never completed are one
        # outcome with three mechanisms: none of them left a server behind, and
        # the mechanism is the reader's, not this function's, to act on.
        return Answer(Handshake.REFUSED, _reason(error), clock() - started)
    buffer = b""
    try:
        with sock:
            sock.sendall(frame(_request()))
            while True:
                message, buffer, complaint = take_message(buffer)
                if complaint is not None:
                    return Answer(Handshake.REJECTED, complaint, clock() - started)
                if message is not None:
                    answer = _verdict(message, clock() - started)
                    if answer is not None:
                        return answer
                    continue
                remaining = deadline - clock()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                chunk = sock.recv(4096)
                if not chunk:
                    return Answer(
                        Handshake.SILENT,
                        "the adapter closed the connection without answering",
                        clock() - started,
                    )
                buffer += chunk
    except TimeoutError:
        pass
    except OSError as error:
        return Answer(Handshake.SILENT, _reason(error), clock() - started)
    # "0 bytes" rather than a sentence about the bound: the caller's own line
    # already names the timeout, and this is the shape the live target's silence
    # was measured in (0 bytes in 15 s).
    return Answer(Handshake.SILENT, "0 bytes", clock() - started)


def _verdict(message: dict[str, Any], seconds: float) -> Answer | None:
    """The outcome ``message`` settles, or ``None`` if it settles nothing."""
    if message.get("type") != "response" or message.get("command") != "initialize":
        return None
    if message.get("success") is True:
        return Answer(Handshake.ANSWERED, "success: true", seconds)
    said = message.get("message")
    return Answer(
        Handshake.REJECTED,
        f"the adapter refused initialize: {said}"
        if isinstance(said, str) and said
        else "the adapter answered initialize with success: false",
        seconds,
    )


def _reason(error: OSError) -> str:
    """What the kernel said, without podbench's opinion on top.

    >>> import errno
    >>> _reason(OSError(errno.ECONNREFUSED, "Connection refused"))
    'Connection refused'
    """
    return error.strerror or str(error)
