"""A frontend's TLS: the control plane always verified, plaintext only on loopback, and each
handshake that would fail the same way again reported with what's wrong and the fix."""

import asyncio
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import InvalidMessage

from orbit.agent import AgentSession
from orbit.agent.domain.server import ServerSettings
from orbit.agent.external.control_plane import ControlPlaneServer
from orbit.client import (
    CaUnreadable,
    PlaintextRefused,
    WebSocketSession,
    accepts,
    client_ssl_context,
    messages,
    probe,
)
from orbit.client.handshake import handshake_failure
from orbit.client.tls import refuse_plaintext
from orbit.client.upgrade import HandshakeRefused
from orbit.protocol import (
    PROTOCOL_VERSION,
    CommandEnvelope,
    Connected,
    HandshakeFailed,
    Hello,
    LinkState,
    Welcome,
)
from tests.tls_certs import DAY, Ca, ServerFiles, make_ca, make_server

TOKEN = "the-token"
HELLO = CommandEnvelope(
    command_id="hello", payload=Hello(client="tests", protocol_version=PROTOCOL_VERSION)
)


@pytest.fixture
def ca(tmp_path: Path) -> Ca:
    return make_ca(tmp_path)


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text(f"{TOKEN}\n")
    return path


def _settings(files: ServerFiles) -> ServerSettings:
    return ServerSettings(port=0, tls="tls", cert=str(files.cert), key=str(files.key))


@asynccontextmanager
async def control_plane(settings: ServerSettings) -> AsyncIterator[int]:
    """A control plane serving as ``settings`` say, and its port."""
    served = AgentSession()
    server = ControlPlaneServer(served, lambda: TOKEN, settings, lambda line: None)
    url = await server.start()
    try:
        yield int(url.rstrip("/").rsplit(":", 1)[1])
    finally:
        await server.stop(served.close)


@asynccontextmanager
async def tls_server(files: ServerFiles) -> AsyncIterator[int]:
    """A WebSocket server showing the certificate in ``files``, and its port."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(files.cert, files.key)

    async def handle(connection: ServerConnection) -> None:
        await connection.wait_closed()

    async with serve(handle, "127.0.0.1", 0, ssl=context) as server:
        yield next(iter(server.sockets)).getsockname()[1]


async def final_state(
    url: str, token_file: Path, context: ssl.SSLContext | None = None
) -> LinkState:
    """The first state a session with ``url`` reports, once it has stopped trying."""
    session = WebSocketSession(url, token_file, ssl_context=context)
    states: asyncio.Queue[LinkState] = asyncio.Queue()
    session.watch(states.put_nowait)
    session.events()
    try:
        state = await asyncio.wait_for(states.get(), 5)
        assert session._running is not None
        await asyncio.wait_for(session._running, 5)  # Not retried.
        return state
    finally:
        await session.close()


def test_the_context_trusts_the_ca_given_and_always_verifies(ca: Ca) -> None:
    context = client_ssl_context(ca.path)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    subjects = [certificate["subject"] for certificate in context.get_ca_certs()]
    assert subjects == [((("commonName", "orbit test CA"),),)]


def test_without_a_ca_the_context_trusts_the_systems_cas() -> None:
    context = client_ssl_context(None)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.get_ca_certs() == ssl.create_default_context().get_ca_certs()


def test_a_missing_ca_file_is_named(tmp_path: Path) -> None:
    missing = tmp_path / "missing.pem"
    with pytest.raises(CaUnreadable) as raised:
        client_ssl_context(missing)
    assert str(raised.value).startswith(f"The CA file can't be read. {missing}: No such file")


def test_a_ca_file_that_isnt_pem_is_named(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("not a certificate\n")
    with pytest.raises(CaUnreadable) as raised:
        client_ssl_context(garbage)
    assert str(raised.value).startswith(f"The CA file can't be read. {garbage}: ")
    assert str(raised.value).endswith(
        "check the path given to --ca, and that the file is a PEM certificate."
    )


@pytest.mark.parametrize(
    "url",
    ["ws://127.0.0.1:7433/", "ws://LocalHost:7433/", "ws://[::1]:7433/", "wss://example.com/"],
)
def test_loopback_plaintext_and_any_tls_are_allowed(url: str) -> None:
    refuse_plaintext(url)


@pytest.mark.parametrize("url", ["ws://example.com:7433", "ws://192.168.1.20:7433/"])
def test_plaintext_beyond_loopback_is_refused_before_connecting(url: str, token_file: Path) -> None:
    with pytest.raises(PlaintextRefused) as raised:
        WebSocketSession(url, token_file)
    host = url.split("//")[1].split(":")[0]
    assert str(raised.value) == messages.PLAINTEXT_NOT_LOOPBACK.format(url=url, host=host)


async def test_it_connects_to_a_tls_control_plane_trusting_its_ca(
    ca: Ca, tmp_path: Path, token_file: Path
) -> None:
    async with control_plane(_settings(make_server(ca, tmp_path))) as port:
        url = f"wss://localhost:{port}/"
        session = WebSocketSession(url, token_file, ssl_context=client_ssl_context(ca.path))
        states: list[LinkState] = []
        session.watch(states.append)
        events = session.events()
        try:
            await session.send(HELLO)
            first = await asyncio.wait_for(anext(aiter(events)), 5)
            assert type(first.payload) is Welcome and states == [Connected(url)]
        finally:
            await session.close()


async def test_a_ca_not_trusted_is_reported_naming_ca_and_not_retried(
    ca: Ca, tmp_path: Path, token_file: Path
) -> None:
    async with control_plane(_settings(make_server(ca, tmp_path))) as port:
        url = f"wss://localhost:{port}/"
        state = await final_state(url, token_file, client_ssl_context(None))
    assert state == HandshakeFailed(url, messages.UNKNOWN_CA.format(url=url))
    assert "pass --ca" in messages.UNKNOWN_CA


async def test_a_name_the_certificate_doesnt_give_is_reported(
    ca: Ca, tmp_path: Path, token_file: Path
) -> None:
    files = make_server(ca, tmp_path, hosts=("orbit.example.lan",))
    async with tls_server(files) as port:
        url = f"wss://localhost:{port}/"
        state = await final_state(url, token_file, client_ssl_context(ca.path))
    assert state == HandshakeFailed(
        url, messages.HOSTNAME_MISMATCH.format(url=url, host="localhost")
    )


async def test_an_expired_certificate_is_reported_with_the_reason(
    ca: Ca, tmp_path: Path, token_file: Path
) -> None:
    files = make_server(ca, tmp_path, valid_from=-10 * DAY, valid_for=5 * DAY)
    async with tls_server(files) as port:
        url = f"wss://localhost:{port}/"
        state = await final_state(url, token_file, client_ssl_context(ca.path))
    reason = "certificate has expired"
    assert state == HandshakeFailed(
        url, messages.CERTIFICATE_INVALID.format(url=url, reason=reason)
    )


async def test_tls_to_a_plaintext_control_plane_is_reported(ca: Ca, token_file: Path) -> None:
    async with control_plane(ServerSettings(port=0)) as port:
        url = f"wss://localhost:{port}/"
        state = await final_state(url, token_file, client_ssl_context(ca.path))
    assert state == HandshakeFailed(url, messages.NOT_TLS.format(url=url))


async def test_plaintext_to_a_tls_control_plane_is_reported(
    ca: Ca, tmp_path: Path, token_file: Path
) -> None:
    async with control_plane(_settings(make_server(ca, tmp_path))) as port:
        url = f"ws://localhost:{port}/"
        state = await final_state(url, token_file, client_ssl_context(ca.path))
    assert state == HandshakeFailed(url, messages.SPEAKS_TLS.format(url=url))


def test_other_tls_errors_may_pass() -> None:
    error = ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
    error.reason = "UNEXPECTED_EOF_WHILE_READING"  # As OpenSSL sets it.
    assert handshake_failure("wss://localhost/", error) is None


def test_a_connection_closed_unanswered_over_tls_may_pass() -> None:
    error = InvalidMessage("did not receive a valid HTTP response")
    error.__cause__ = EOFError("connection closed while reading HTTP status line")
    assert handshake_failure("wss://localhost/", error) is None


def test_an_answer_that_isnt_http_may_pass() -> None:
    error = InvalidMessage("did not receive a valid HTTP response")
    error.__cause__ = ValueError("unsupported protocol")
    assert handshake_failure("ws://localhost/", error) is None


async def test_the_probe_takes_a_failed_handshake_as_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refusing(url: str, token: str, **options: object) -> None:
        raise HandshakeRefused(messages.UNKNOWN_CA.format(url=url))

    monkeypatch.setattr(probe, "open_connection", refusing)
    assert not await accepts("wss://127.0.0.1:1/", TOKEN)
