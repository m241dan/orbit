"""The control plane under mutual TLS: a client certificate the client CA signed is the identity,
without a token, and one whose fingerprint is revoked is turned away."""

import asyncio
import hashlib
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from starlette.responses import Response
from starlette.websockets import WebSocket
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidMessage, InvalidStatus

from orbit.agent import AgentSession
from orbit.agent.domain.server import ServerSettings, client_fingerprint, is_revoked
from orbit.agent.external import tls_messages
from orbit.agent.external.client_certs import client_certificate, refused_client
from orbit.agent.external.control_plane import ControlPlaneServer
from orbit.agent.external.tls import TlsError, tls_warnings
from orbit.protocol import PROTOCOL_VERSION, CommandEnvelope, Hello, Welcome, decode_event, encode
from tests.tls_certs import Ca, ServerFiles, make_ca, make_client, make_server

HELLO = CommandEnvelope(
    command_id="hello", payload=Hello(client="tests", protocol_version=PROTOCOL_VERSION)
)


class Lines:
    """The lines the server said."""

    def __init__(self) -> None:
        self.said: list[str] = []

    def __call__(self, line: str) -> None:
        self.said.append(line)


@pytest.fixture
def ca(tmp_path: Path) -> Ca:
    return make_ca(tmp_path)


@pytest.fixture
def settings(ca: Ca, tmp_path: Path) -> ServerSettings:
    files = make_server(ca, tmp_path)
    return ServerSettings(
        port=0, tls="mtls", cert=str(files.cert), key=str(files.key), client_ca=str(ca.path)
    )


def _der(files: ServerFiles) -> bytes:
    return ssl.PEM_cert_to_DER_cert(files.cert.read_text())


def _context(ca: Ca, client: ServerFiles | None) -> ssl.SSLContext:
    """A context trusting ``ca``, showing the certificate in ``client`` if given."""
    context = ssl.create_default_context(cafile=ca.path)
    if client is not None:
        context.load_cert_chain(client.cert, client.key)
    return context


@asynccontextmanager
async def serving(
    settings: ServerSettings, lines: Lines, revoked: list[str] | None = None
) -> AsyncIterator[tuple[str, ControlPlaneServer]]:
    """The URL of a server serving as ``settings`` say, telling ``lines``, and the server."""
    served = AgentSession()
    server = ControlPlaneServer(served, lambda: "", settings, lines)
    if revoked is not None:
        server.revoked = lambda: revoked
    url = await server.start()
    try:
        yield url.replace("127.0.0.1", "localhost"), server
    finally:
        await server.stop(served.close)


async def _welcomed(url: str, context: ssl.SSLContext) -> bool:
    """Whether a frontend showing no token over ``context`` is welcomed at ``url``."""
    async with connect(url, ssl=context, proxy=None) as websocket:
        await websocket.send(encode(HELLO))
        envelope = decode_event(str(await asyncio.wait_for(websocket.recv(), 5)))
        return envelope is not None and type(envelope.payload) is Welcome


async def test_a_certificate_the_client_ca_signed_is_welcomed_without_a_token_and_named(
    ca: Ca, settings: ServerSettings, tmp_path: Path
) -> None:
    client = make_client(ca, tmp_path)
    lines = Lines()
    async with serving(settings, lines) as (url, _):
        assert url.startswith("wss://")
        assert await _welcomed(url, _context(ca, client))
    fingerprint = client_fingerprint(_der(client))
    assert any(f" as CN=laptop ({fingerprint})." in line for line in lines.said), lines.said


async def test_a_client_without_a_certificate_is_refused_at_the_handshake(
    ca: Ca, settings: ServerSettings
) -> None:
    lines = Lines()
    async with serving(settings, lines) as (url, _):
        with pytest.raises(InvalidMessage):
            await _welcomed(url, _context(ca, None))
    assert lines.said == [tls_messages.CLIENT_CERT_REQUIRED.format(client_ca=ca.path)]


async def test_a_certificate_another_ca_signed_is_rejected(
    ca: Ca, settings: ServerSettings, tmp_path: Path
) -> None:
    (tmp_path / "other").mkdir()
    stranger = make_client(make_ca(tmp_path / "other"), tmp_path / "other")
    lines = Lines()
    async with serving(settings, lines) as (url, _):
        with pytest.raises(InvalidMessage):
            await _welcomed(url, _context(ca, stranger))
    assert lines.said == [tls_messages.CLIENT_CERT_UNTRUSTED.format(client_ca=ca.path)]


async def test_a_handshake_failing_otherwise_is_left_unsaid(settings: ServerSettings) -> None:
    lines = Lines()
    async with serving(settings, lines) as (url, _):
        with pytest.raises(InvalidMessage):  # Plaintext, which the TLS handshake can't read.
            async with connect(url.replace("wss://", "ws://"), proxy=None):
                pass
    assert lines.said == []


async def test_a_revoked_certificate_is_refused_with_403_from_its_next_connection(
    ca: Ca, settings: ServerSettings, tmp_path: Path
) -> None:
    client = make_client(ca, tmp_path)
    fingerprint = client_fingerprint(_der(client))
    revoked: list[str] = []
    async with serving(settings, Lines(), revoked) as (url, _):
        assert await _welcomed(url, _context(ca, client))
        revoked.append(fingerprint.lower())
        with pytest.raises(InvalidStatus) as refused:
            await _welcomed(url, _context(ca, client))
    assert refused.value.response.status_code == 403
    body = refused.value.response.body or b""
    assert body.decode() == tls_messages.REVOKED.format(fingerprint=fingerprint)


async def test_the_settings_revoked_fingerprints_are_used_by_default(
    ca: Ca, settings: ServerSettings, tmp_path: Path
) -> None:
    client = make_client(ca, tmp_path)
    revoked = settings.model_copy(update={"revoked": (client_fingerprint(_der(client)),)})
    async with serving(revoked, Lines()) as (url, _):
        with pytest.raises(InvalidStatus):
            await _welcomed(url, _context(ca, client))


class _Uncertified:
    """A WebSocket that showed no certificate, as the TLS handshake lets none through."""

    def __init__(self) -> None:
        self.scope: dict[str, object] = {"type": "websocket", "extensions": {}}
        self.denied: list[Response] = []

    async def send_denial_response(self, response: Response) -> None:
        self.denied.append(response)


async def test_a_websocket_without_a_certificate_is_turned_away(
    settings: ServerSettings, ca: Ca
) -> None:
    server = ControlPlaneServer(AgentSession(), lambda: "", settings, Lines())
    websocket = _Uncertified()
    await server._websocket(cast(WebSocket, websocket))
    [denied] = websocket.denied
    assert denied.status_code == 401
    assert bytes(denied.body).decode() == tls_messages.CLIENT_CERT_REQUIRED.format(
        client_ca=ca.path
    )


def test_the_fingerprint_is_the_sha256_of_the_certificate_in_colon_separated_hex() -> None:
    fingerprint = client_fingerprint(b"certificate")
    assert fingerprint.startswith("03:D6:6D:") and fingerprint.endswith(":2D:72")
    assert fingerprint.replace(":", "") == hashlib.sha256(b"certificate").hexdigest().upper()


@pytest.mark.parametrize("entry", ["AB:CD:EF", "ab:cd:ef", "ABCDEF", " ab cd ef "])
def test_a_revoked_fingerprint_matches_in_either_case_with_or_without_colons(entry: str) -> None:
    assert is_revoked("AB:CD:EF", [entry])


def test_a_fingerprint_not_listed_is_not_revoked() -> None:
    assert not is_revoked("AB:CD:EF", ["AB:CD:EE"])


def test_a_scope_without_a_certificate_has_none() -> None:
    assert client_certificate({"type": "websocket"}) is None


def test_other_handshake_failures_are_left_unsaid() -> None:
    error = ssl.SSLError(1, "unknown CA")
    error.reason = "TLSV1_ALERT_UNKNOWN_CA"  # The client didn't trust the server's.
    assert refused_client(error, "ca.pem") is None


def test_mutual_tls_with_its_files_is_served(settings: ServerSettings) -> None:
    assert tls_warnings(settings.model_copy(update={"host": "0.0.0.0"})) == ()


def test_a_client_ca_that_cant_be_read_is_refused_naming_it(
    settings: ServerSettings, tmp_path: Path
) -> None:
    missing = tmp_path / "missing.pem"
    with pytest.raises(TlsError, match=r"^A TLS file can't be read\. .*missing\.pem: "):
        tls_warnings(settings.model_copy(update={"client_ca": str(missing)}))
