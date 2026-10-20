"""The control plane's server: the protocol over a WebSocket, one frame per envelope, for each
frontend that shows the token, or under mutual TLS a client certificate, over TLS if the
settings say so.

Each WebSocket that's let in gets a connection of its own to the session: its text frames are
decoded into commands and sent on it, and its events are encoded into text frames. A frontend
that sends no ``Authorization: Bearer`` with the token is turned away with HTTP 401 before the
WebSocket opens, and one that arrives as the server stops, with HTTP 503. The token is read
afresh for each frontend, so a new one admits frontends at once, while those already let in
stay. Under mutual TLS the TLS handshake asks for a client certificate and refuses one the
client CA didn't sign, every route but the health checks turns away a client that showed none
with HTTP 401, the token isn't asked for, and a certificate whose fingerprint is revoked, as the
revoked fingerprints are read afresh for each frontend, is turned away with HTTP 403.

``GET /healthz`` and ``GET /readyz`` answer health checks without the token or a client
certificate, in ``application/health+json``: the first passes while the server runs, the second
says whether it's ready.
"""

import asyncio
import hmac
import socket
import ssl
from collections.abc import Awaitable, Callable, Collection
from contextlib import suppress
from typing import Protocol

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketDisconnected

from orbit.agent.application.connection import ConnectionDropped, SessionConnection
from orbit.agent.domain.health import Readiness, readiness, ready_answer
from orbit.agent.domain.server import ServerSettings, client_fingerprint, is_revoked
from orbit.agent.external import tls_messages
from orbit.agent.external.client_certs import (
    CertifiedWebSocketProtocol,
    certificate_subject,
    client_certificate,
    reporting_handshakes,
)
from orbit.agent.external.embedded_server import EmbeddedServer
from orbit.agent.external.mcp_host import bearer_token
from orbit.protocol import (
    CERTIFICATE_CHALLENGE,
    GOING_AWAY,
    INVALID,
    MESSAGE_TOO_BIG,
    NORMAL,
    POLICY_VIOLATION,
    UNKNOWN,
    CommandEnvelope,
    Hello,
    ProtocolError,
    decode_command,
    encode,
)
from orbit.protocol import UNAUTHORIZED as UNAUTHORIZED_STATUS

SHUTTING_DOWN = "orbit serve is shutting down"
"""The close reason as the server shuts down."""

UNAUTHORIZED = "Send the token in orbit's token file as Authorization: Bearer <token>."
"""The body of the HTTP 401 a frontend without the token gets."""

BINARY_FRAME = "Send each envelope as JSON in a text frame."
"""Why a binary frame was refused."""

HEALTH_JSON = "application/health+json"
"""The content type of the health checks' answers."""

HEALTH_PATHS = frozenset({"/healthz", "/readyz"})
"""The paths of the health checks, which answer without the token or a client certificate."""

GRACE_SECONDS = 5
"""How long the server waits, as it shuts down, for the WebSockets to finish closing."""

_REASON_BYTES = 123  # The most a close frame's reason may hold.

Say = Callable[[str], None]
"""Tells the user, on one line, what happened."""


class Connectable(Protocol):
    """A session frontends connect to, saying whether it's read-only, as another orbit owns the
    repository, and whether its storage is halted."""

    def connect(self) -> SessionConnection: ...

    @property
    def read_only(self) -> bool: ...

    @property
    def storage_halted(self) -> bool: ...


class ListenFailed(Exception):
    """The server couldn't listen on ``port``, for ``reason``."""

    def __init__(self, host: str, port: int, reason: str) -> None:
        super().__init__(f"Can't listen on {host}:{port}: {reason}")
        self.port = port
        self.reason = reason


class _TooBig(Exception):
    """An event too big to send."""


def close_reason(text: str) -> str:
    """``text``, cut to fit in a close frame."""
    return text.encode()[:_REASON_BYTES].decode(errors="ignore")


def ws_url(host: str, port: int, secure: bool = False) -> str:
    """The URL of the server on ``host`` and ``port``, an IPv6 address in brackets, ``wss://``
    if it's ``secure``, over TLS."""
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return f"{'wss' if secure else 'ws'}://{netloc}/"


def _decoded(message: Message) -> CommandEnvelope:
    """The command in a received ``message``; raises ``ProtocolError`` if it holds none."""
    text = message.get("text")
    if text is None:
        raise ProtocolError(BINARY_FRAME)
    return decode_command(text)


class _CertificateGate:
    """``app``, but for a request to any path besides the health checks that showed no client
    certificate, which is answered with what ``refusal`` returns then."""

    def __init__(self, app: ASGIApp, refusal: Callable[[], Response]) -> None:
        self._app = app
        self._refusal = refusal

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if _let_through(scope):
            await self._app(scope, receive, send)
        elif scope["type"] == "websocket":
            await WebSocket(scope, receive, send).send_denial_response(self._refusal())
        else:
            await self._refusal()(scope, receive, send)


def _let_through(scope: Scope) -> bool:
    """Whether the certificate gate lets ``scope`` through: the app's lifespan, a health
    check, or a request that showed a client certificate."""
    if scope["type"] == "lifespan" or scope["path"] in HEALTH_PATHS:
        return True
    return client_certificate(scope) is not None


class _Conversation:
    """One frontend's WebSocket, ``websocket``, over its ``connection`` to the session; events
    over ``limit`` bytes aren't sent, and a stream that ends closes the WebSocket with the code
    and reason ``farewell`` gives then."""

    def __init__(
        self,
        websocket: WebSocket,
        connection: SessionConnection,
        limit: int,
        farewell: Callable[[], tuple[int, str]],
    ) -> None:
        self._websocket = websocket
        self._connection = connection
        self._limit = limit
        self._farewell = farewell
        self._closed = False
        self.client: str | None = None  # As its Hello names it.
        self.greeted: Callable[[str], None] = lambda client: None

    async def run(self) -> None:
        """Pass the frontend's commands to the session and the session's events back, until
        either side closes."""
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._read())
                group.create_task(self._write())
        finally:
            await self._connection.close()

    async def close(self, code: int, reason: str) -> None:
        """Close the WebSocket with ``code`` and ``reason``, and the connection, unless they're
        closed."""
        if self._closed:
            return
        self._closed = True
        await self._connection.close()
        with suppress(WebSocketDisconnect, WebSocketDisconnected):
            await self._websocket.close(code, close_reason(reason))

    async def _gone(self) -> None:
        """Note that the frontend has gone, closing the connection."""
        self._closed = True
        await self._connection.close()

    async def _read(self) -> None:
        """Pass each command the frontend sends to the session, until it closes."""
        while await self._received(await self._websocket.receive()):
            pass

    async def _received(self, message: Message) -> bool:
        """Handle ``message`` from the frontend; returns whether more may follow."""
        if message["type"] == "websocket.disconnect":
            await self._gone()
            return False
        try:
            envelope = _decoded(message)
        except ProtocolError as error:
            return await self._unreadable(error)
        self._note_hello(envelope)
        await self._connection.send(envelope)
        return True

    async def _unreadable(self, error: ProtocolError) -> bool:
        """Refuse the command the frontend sent that ``error`` says couldn't be read, or close
        the WebSocket if it has no ``command_id``; returns whether more may follow."""
        if error.command_id is None:
            await self.close(POLICY_VIOLATION, error.message)
            return False
        code = UNKNOWN if error.unknown else INVALID
        await self._connection.refuse(error.command_id, code, error.message)
        return True

    def _note_hello(self, envelope: CommandEnvelope) -> None:
        """Take the frontend's name from its first ``Hello``, if ``envelope`` is that."""
        if isinstance(envelope.payload, Hello) and self.client is None:
            self.client = envelope.payload.client
            self.greeted(self.client)

    async def _write(self) -> None:
        """Send the frontend the session's events until the stream ends, then close the
        WebSocket as the reason it ended says."""
        try:
            async for envelope in self._connection.events():
                await self._send(encode(envelope))
        except (ConnectionDropped, _TooBig) as error:
            code = POLICY_VIOLATION if isinstance(error, ConnectionDropped) else MESSAGE_TOO_BIG
            await self.close(code, str(error))
            return
        except (WebSocketDisconnect, WebSocketDisconnected):
            await self._gone()
            return
        await self.close(*self._farewell())

    async def _send(self, text: str) -> None:
        """Send ``text`` in a frame; raises :class:`_TooBig` if it's over the limit."""
        size = len(text.encode())
        if size > self._limit:
            raise _TooBig(f"An event of {size} bytes is over the {self._limit}-byte limit")
        await self._websocket.send_text(text)


class ControlPlaneServer:
    """Serves ``session`` to every frontend that shows the token ``token`` returns as it
    connects, or under mutual TLS a client certificate whose fingerprint isn't among those
    :attr:`revoked` returns then, on the host and port in ``settings``, until stopped; ``say``
    tells the user as frontends connect and disconnect. An empty token admits no one."""

    def __init__(
        self, session: Connectable, token: Callable[[], str], settings: ServerSettings, say: Say
    ) -> None:
        self._session = session
        self._token = token
        self._settings = settings
        self._say = say
        self._open: set[_Conversation] = set()
        self._stopping = False
        self._accepting = False
        self._server: uvicorn.Server | None = None
        self._serving: asyncio.Task[None] | None = None
        self.revoked: Callable[[], Collection[str]] = lambda: settings.revoked

    async def start(self) -> str:
        """Listen for frontends; returns the server's URL, with the port it listens on.

        Raises :class:`ListenFailed` if it can't listen there.
        """
        host, port = self._settings.host, self._settings.port
        try:
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            sock = socket.create_server((host, port), family=family)
        except OSError as exc:
            raise ListenFailed(host, port, exc.strerror or str(exc)) from None
        url = ws_url(host, sock.getsockname()[1], self._secure)
        self._server = EmbeddedServer(self._config())
        self._serving = asyncio.create_task(self._server.serve(sockets=[sock]))
        while not self._server.started:
            if self._serving.done():
                sock.close()
                raise RuntimeError("Orbit's control plane failed to start.") from (
                    self._serving.exception()
                )
            await asyncio.sleep(0.01)
        self._accepting = True
        return url

    def readiness(self) -> Readiness:
        """How ready the server is: starting until it accepts frontends, shutting down once it
        stops, and otherwise as its session's storage and ownership say."""
        return readiness(
            accepting=self._accepting,
            stopping=self._stopping,
            halted=self._session.storage_halted,
            read_only=self._session.read_only,
        )

    @property
    def _secure(self) -> bool:
        """Whether the server serves over TLS."""
        return self._settings.tls != "off"

    @property
    def _mutual(self) -> bool:
        """Whether the server serves mutual TLS, requiring client certificates."""
        return self._settings.tls == "mtls"

    def app(self) -> Starlette:
        """The app serving frontends and health checks, behind a gate turning away every request
        but a health check that showed no client certificate if it's mutual."""
        routes = [
            WebSocketRoute("/", self._websocket),
            Route("/healthz", _healthz, methods=["GET"]),
            Route("/readyz", self._readyz, methods=["GET"]),
        ]
        gate = [Middleware(_CertificateGate, refusal=self._no_certificate)]
        return Starlette(routes=routes, middleware=gate if self._mutual else [])

    def _config(self) -> uvicorn.Config:
        """How uvicorn serves the app: logging left to the program, messages kept under the
        limit, over TLS with the settings' certificate and key if it's secure, and if it's
        mutual asking for a client certificate, which must be one the client CA signed, without
        requiring one, so health checks need none."""
        return uvicorn.Config(
            self.app(),
            ssl_certfile=self._settings.cert if self._secure else None,
            ssl_keyfile=self._settings.key if self._secure else None,
            ssl_ca_certs=self._settings.client_ca if self._mutual else None,
            ssl_cert_reqs=ssl.CERT_OPTIONAL if self._mutual else ssl.CERT_NONE,
            ssl_context_factory=self._ssl_context if self._mutual else None,
            log_config=None,
            lifespan="off",
            ws=CertifiedWebSocketProtocol,
            ws_max_size=self._settings.max_message_bytes,
            timeout_graceful_shutdown=GRACE_SECONDS,
        )

    def _ssl_context(
        self, config: uvicorn.Config, default: Callable[[], ssl.SSLContext]
    ) -> ssl.SSLContext:
        """uvicorn's ``default`` context for ``config``, saying why each client the TLS
        handshake refuses for an untrusted certificate was refused."""
        context = default()
        assert self._settings.client_ca is not None  # As mutual TLS needs one.
        context.sslobject_class = reporting_handshakes(self._say, self._settings.client_ca)
        return context

    async def stop(self, close_session: Callable[[], Awaitable[None]]) -> None:
        """Stop accepting frontends, ``close_session``, then close every WebSocket as the
        server is shutting down and stop serving."""
        if self._server is None or self._serving is None:
            return
        self._stopping = True
        for listener in self._server.servers:
            listener.close()
        try:
            await close_session()
        finally:
            for conversation in tuple(self._open):
                await conversation.close(GOING_AWAY, SHUTTING_DOWN)
            self._server.should_exit = True
            await self._serving

    async def _readyz(self, request: Request) -> JSONResponse:
        """Whether the server is ready, as ``/readyz`` answers it."""
        status, body = ready_answer(self.readiness())
        return JSONResponse(body, status, media_type=HEALTH_JSON)

    def _farewell(self) -> tuple[int, str]:
        """How a WebSocket whose stream ended is closed: as the server shuts down, if it is."""
        return (GOING_AWAY, SHUTTING_DOWN) if self._stopping else (NORMAL, "")

    def _authorized(self, websocket: WebSocket) -> bool:
        """Whether ``websocket`` showed the token as it is now."""
        token = self._token().encode()
        return bool(token) and hmac.compare_digest(bearer_token(websocket.headers).encode(), token)

    def _refusal(self, websocket: WebSocket) -> PlainTextResponse | None:
        """What turns ``websocket`` away: under mutual TLS, a certificate missing or revoked,
        and otherwise a token missing or wrong; then a server that's stopping. None if it's
        let in."""
        if self._mutual:
            refusal = self._uncertified(client_certificate(websocket.scope))
        else:
            unauthorized = PlainTextResponse(UNAUTHORIZED, UNAUTHORIZED_STATUS)
            refusal = None if self._authorized(websocket) else unauthorized
        if refusal is None and self._stopping:
            return PlainTextResponse(SHUTTING_DOWN, 503)
        return refusal

    def _no_certificate(self) -> PlainTextResponse:
        """The HTTP 401 turning away a client that showed no certificate under mutual TLS,
        telling the user why."""
        message = tls_messages.CLIENT_CERT_REQUIRED.format(client_ca=self._settings.client_ca)
        self._say(message)
        headers = {"WWW-Authenticate": CERTIFICATE_CHALLENGE}
        return PlainTextResponse(message, UNAUTHORIZED_STATUS, headers=headers)

    def _uncertified(self, der: bytes | None) -> PlainTextResponse | None:
        """What turns away a frontend that showed the client certificate ``der``, in DER, if
        it showed none, which the certificate gate already turns away, or it's revoked."""
        if der is None:
            return self._no_certificate()
        fingerprint = client_fingerprint(der)
        if is_revoked(fingerprint, self.revoked()):
            return PlainTextResponse(tls_messages.REVOKED.format(fingerprint=fingerprint), 403)
        return None

    async def _websocket(self, websocket: WebSocket) -> None:
        """Serve the frontend at ``websocket``, if it's let in, until it's closed."""
        refusal = self._refusal(websocket)
        if refusal is not None:
            await websocket.send_denial_response(refusal)
            return
        await websocket.accept()
        if self._stopping:  # Began stopping as it was accepted, so stop won't close it.
            await websocket.close(GOING_AWAY, close_reason(SHUTTING_DOWN))
            return
        peer = _peer(websocket)
        conversation = _Conversation(
            websocket, self._session.connect(), self._settings.max_message_bytes, self._farewell
        )
        conversation.greeted = lambda name: self._say(f"{name} connected from {peer}.")
        self._open.add(conversation)
        try:
            await conversation.run()
        finally:
            self._open.discard(conversation)
            if conversation.client is not None:
                self._say(f"{conversation.client} disconnected from {peer}.")


async def _healthz(request: Request) -> JSONResponse:
    """That the server runs, as ``/healthz`` answers it."""
    return JSONResponse({"status": "pass"}, media_type=HEALTH_JSON)


def _peer(websocket: WebSocket) -> str:
    """Where ``websocket`` connected from, with the subject and fingerprint of its client
    certificate if it showed one."""
    client = websocket.client
    peer = f"{client.host}:{client.port}" if client else "an unknown address"
    der = client_certificate(websocket.scope)
    if der is None:
        return peer
    return f"{peer} as {certificate_subject(der)} ({client_fingerprint(der)})"
