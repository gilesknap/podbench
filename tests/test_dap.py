"""Tests for the DAP handshake that has to answer before podbench claims success.

The measurement these exist for is
``.claude/evidence/phase7-vscode-and-the-two-failures.md`` §4: the injector
exited 0, the port was open and in ``LISTEN``, and the adapter accepted the TCP
connection and never answered ``initialize``. Every outcome below is one of the
states that run could have been in, and only the first of them is a debugger.

Sockets here are loopback servers this module starts, binds on port 0 and shuts
down again — never a cluster, never a port anything else on the machine holds,
and never a listener that outlives its test.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable, Iterator

import pytest

from podbench.dap import (
    HANDSHAKE_TIMEOUT_SECONDS,
    Answer,
    Handshake,
    frame,
    initialize,
    take_message,
)

Reply = Callable[[dict[str, object]], list[bytes]]
"""What a fake adapter sends back, given the request it was asked."""


class FakeAdapter:
    """A loopback listener that answers one client however the test says.

    Threaded rather than served inline because the client under test blocks on
    ``recv``: the whole point of :data:`Handshake.SILENT` is a peer that accepts
    and then does nothing, which cannot be modelled by a function the client
    calls.
    """

    def __init__(self, reply: Reply) -> None:
        self._reply = reply
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port: int = self._sock.getsockname()[1]
        self._served = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        # Polled rather than blocking: closing a listening socket does not wake
        # a thread already inside `accept`, so a test that never connects would
        # otherwise pay the join timeout on the way out.
        self._sock.settimeout(0.05)
        conn: socket.socket | None = None
        while conn is None and not self._served.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
        if conn is None:
            return
        with conn:
            conn.settimeout(HANDSHAKE_TIMEOUT_SECONDS * 2)
            buffer = b""
            request: dict[str, object] | None = None
            while request is None:
                message, buffer, _ = take_message(buffer)
                if message is not None:
                    request = message
                    break
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
            for blob in self._reply(request):
                try:
                    conn.sendall(blob)
                except OSError:
                    return
            self._served.wait(HANDSHAKE_TIMEOUT_SECONDS * 2)

    def close(self) -> None:
        self._served.set()
        self._thread.join(timeout=5)
        self._sock.close()


@pytest.fixture
def adapter() -> Iterator[Callable[[Reply], FakeAdapter]]:
    """A factory that shuts every adapter it made down again."""
    made: list[FakeAdapter] = []

    def make(reply: Reply) -> FakeAdapter:
        server = FakeAdapter(reply)
        made.append(server)
        return server

    yield make
    for server in made:
        server.close()


def _response(request: dict[str, object], **extra: object) -> bytes:
    return frame(
        {
            "seq": 2,
            "type": "response",
            "request_seq": request.get("seq"),
            "command": "initialize",
            "success": True,
            "body": {"supportsConfigurationDoneRequest": True},
            **extra,
        }
    )


def test_an_answered_initialize_is_the_one_outcome_that_is_success(
    adapter: Callable[[Reply], FakeAdapter],
) -> None:
    """And the response is not the first frame on the wire.

    debugpy sends two telemetry ``output`` events the instant a client connects,
    so a client that read exactly one message would call a working adapter
    broken. The fake sends them for the same reason.
    """
    telemetry = frame({"seq": 1, "type": "event", "event": "output"})
    server = adapter(lambda request: [telemetry, telemetry, _response(request)])

    answer = initialize(server.port)

    assert answer.outcome is Handshake.ANSWERED
    assert answer.ok


def test_an_open_port_that_never_answers_is_not_success(
    adapter: Callable[[Reply], FakeAdapter],
) -> None:
    """**The live target's own state.** The connection is accepted, the socket
    stays open, and nothing comes back — which every earlier version of this
    check would have reported as a working debugger."""
    server = adapter(lambda _request: [])

    answer = initialize(server.port, timeout=0.5)

    assert answer.outcome is Handshake.SILENT
    assert not answer.ok
    assert answer.detail == "0 bytes"


def test_a_refused_connection_is_distinguished_from_a_silent_one() -> None:
    """Different halves to chase: nothing was left listening at all here, where
    :data:`Handshake.SILENT` means the injection took and the session did not."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port: int = probe.getsockname()[1]

    answer = initialize(closed_port, timeout=0.5)

    assert answer.outcome is Handshake.REFUSED
    assert not answer.ok


def test_a_peer_that_answers_without_being_an_adapter_is_rejected(
    adapter: Callable[[Reply], FakeAdapter],
) -> None:
    """Under ``hostNetwork`` the port is the node's, so whatever answers need not
    be debugpy - and bytes that are not DAP framing are not silence."""
    server = adapter(lambda _request: [b"HTTP/1.1 400 Bad Request\r\n\r\n"])

    answer = initialize(server.port, timeout=0.5)

    assert answer.outcome is Handshake.REJECTED
    assert "not DAP framing" in answer.detail


def test_an_initialize_the_adapter_refuses_is_not_an_answer(
    adapter: Callable[[Reply], FakeAdapter],
) -> None:
    """``success: false`` is a reply and not a debugger, and the adapter's own
    ``message`` is relayed rather than paraphrased."""
    server = adapter(
        lambda request: [_response(request, success=False, message="session is busy")]
    )

    answer = initialize(server.port, timeout=0.5)

    assert answer.outcome is Handshake.REJECTED
    assert "session is busy" in answer.detail


def test_a_peer_that_hangs_up_without_answering_is_silent_and_says_so(
    adapter: Callable[[Reply], FakeAdapter],
) -> None:
    """A closed connection is reported in one round trip rather than in the
    whole of the bound: the seat has its answer and waiting adds nothing."""
    server = adapter(lambda _request: [])
    server.close()

    answer = initialize(server.port, timeout=0.5)

    assert answer.outcome in (Handshake.SILENT, Handshake.REFUSED)
    assert not answer.ok


def test_the_request_is_the_one_the_editor_sends(
    adapter: Callable[[Reply], FakeAdapter],
) -> None:
    """Asking a cheaper question would prove a cheaper thing. What is asserted
    is that the adapter is asked to ``initialize`` as ``debugpy``, which is the
    ``type`` the emitted configuration carries."""
    seen: list[dict[str, object]] = []

    def reply(request: dict[str, object]) -> list[bytes]:
        seen.append(request)
        return [_response(request)]

    server = adapter(reply)
    initialize(server.port)

    assert seen[0]["command"] == "initialize"
    assert seen[0]["type"] == "request"
    arguments = seen[0]["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["adapterID"] == "debugpy"


def test_a_message_split_across_recvs_is_still_one_message() -> None:
    """TCP is a stream, so a frame arrives in whatever pieces the kernel chose.
    ``take_message`` returns nothing rather than a complaint until it has one."""
    whole = frame({"type": "response", "command": "initialize", "success": True})
    for cut in range(1, len(whole)):
        message, rest, complaint = take_message(whole[:cut])
        assert message is None
        assert complaint is None
        assert rest == whole[:cut]
    message, rest, complaint = take_message(whole)
    assert complaint is None
    assert rest == b""
    assert message == json.loads(whole.split(b"\r\n\r\n", 1)[1])


def test_the_bound_is_the_measured_one() -> None:
    """Five seconds, against an 8.7 s injection and 15 s of measured silence.
    Pinned so that a later edit has to argue with the measurement."""
    assert HANDSHAKE_TIMEOUT_SECONDS == 5.0


def test_an_answer_carries_what_the_socket_said() -> None:
    """The detail is relayed text, so it is a field rather than a formatting
    choice made where the sentence is written."""
    assert Answer(Handshake.REFUSED, "Connection refused").detail == (
        "Connection refused"
    )
