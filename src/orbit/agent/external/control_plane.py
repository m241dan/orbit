"""The control plane's server: the protocol over a WebSocket, one frame per envelope, for each
frontend that shows the token.

Each WebSocket that's let in gets a connection of its own to the session: its text frames are
decoded into commands and sent on it, and its events are encoded into text frames. A frontend
that sends no ``Authorization: Bearer`` with the token is turned away with HTTP 401 before the
WebSocket opens. The token is read afresh for each frontend, so a new one admits frontends at
once, while those already let in stay.
"""

import asyncio
import hmac
import socket
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Protocol

import uvicorn
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import WebSocketRoute
from starlette.types import Message
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketDisconnected

from orbit.agent.application.connection import ConnectionDropped, SessionConnection
from orbit.agent.domain.server import ServerSettings
from orbit.agent.external.embedded_server import EmbeddedServer
from orbit.agent.external.mcp_host import bearer_token
from orbit.protocol import (
    INVALID,
    UNKNOWN,
    CommandEnvelope,
    Hello,
    ProtocolError,
    decode_command,
    encode,
)

NORMAL = 1000
"""The close code of a WebSocket whose stream ended, as an incompatible ``Hello``'s does."""

GOING_AWAY = 1001
"""The close code of every WebSocket as the server shuts down."""

POLICY_VIOLATION = 1008
"""The close code of a WebSocket that sent a frame with no readable ``command_id``, or fell
too far behind."""

MESSAGE_TOO_BIG = 1009
"""The close code of a WebSocket a message over the limit was sent to, or from."""

SHUTTING_DOWN = "orbit serve is shutting down"
"""The close reason as the server shuts down."""

UNAUTHORIZED = "Send the token in orbit's token file as Authorization: Bearer <token>."
"""The body of the HTTP 401 a frontend without the token gets."""

BINARY_FRAME = "Send each envelope as JSON in a text frame."
"""Why a binary frame was refused."""

GRACE_SECONDS = 5
"""How long the server waits, as it shuts down, for the WebSockets to finish closing."""

_REASON_BYTES = 123  # The most a close frame's reason may hold.

Say = Callable[[str], None]
"""Tells the user, on one line, what happened."""


class Connectable(Protocol):
    """A session frontends connect to."""

    def connect(self) -> SessionConnection: ...


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


def ws_url(host: str, port: int) -> str:
    """The URL of the server on ``host`` and ``port``, an IPv6 address in brackets."""
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return f"ws://{netloc}/"


def _decoded(message: Message) -> CommandEnvelope:
    """The command in a received ``message``; raises ``ProtocolError`` if it holds none."""
    text = message.get("text")
    if text is None:
        raise ProtocolError(BINARY_FRAME)
    return decode_command(text)


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
    connects, on the host and port in ``settings``, until stopped; ``say`` tells the user as
    frontends connect and disconnect. An empty token admits no one."""

    def __init__(
        self, session: Connectable, token: Callable[[], str], settings: ServerSettings, say: Say
    ) -> None:
        self._session = session
        self._token = token
        self._settings = settings
        self._say = say
        self._open: set[_Conversation] = set()
        self._stopping = False
        self._server: uvicorn.Server | None = None
        self._serving: asyncio.Task[None] | None = None

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
        url = ws_url(host, sock.getsockname()[1])
        self._server = EmbeddedServer(self._config())
        self._serving = asyncio.create_task(self._server.serve(sockets=[sock]))
        while not self._server.started:
            if self._serving.done():
                sock.close()
                raise RuntimeError("Orbit's control plane failed to start.") from (
                    self._serving.exception()
                )
            await asyncio.sleep(0.01)
        return url

    def _config(self) -> uvicorn.Config:
        """How uvicorn serves the app: logging left to the program, messages kept under the
        limit."""
        app = Starlette(routes=[WebSocketRoute("/", self._websocket)])
        return uvicorn.Config(
            app,
            log_config=None,
            lifespan="off",
            ws="websockets-sansio",
            ws_max_size=self._settings.max_message_bytes,
            timeout_graceful_shutdown=GRACE_SECONDS,
        )

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

    def _farewell(self) -> tuple[int, str]:
        """How a WebSocket whose stream ended is closed: as the server shuts down, if it is."""
        return (GOING_AWAY, SHUTTING_DOWN) if self._stopping else (NORMAL, "")

    def _authorized(self, websocket: WebSocket) -> bool:
        """Whether ``websocket`` showed the token as it is now."""
        token = self._token().encode()
        return bool(token) and hmac.compare_digest(bearer_token(websocket.headers).encode(), token)

    async def _websocket(self, websocket: WebSocket) -> None:
        """Serve the frontend at ``websocket``, if it showed the token, until it's closed."""
        if not self._authorized(websocket):
            await websocket.send_denial_response(PlainTextResponse(UNAUTHORIZED, 401))
            return
        await websocket.accept()
        client = websocket.client
        peer = f"{client.host}:{client.port}" if client else "an unknown address"
        conversation = _Conversation(
            websocket, self._session.connect(), self._settings.max_message_bytes, self._farewell
        )
        conversation.greeted = lambda name: self._say(f"{name} connected from {peer}.")
        self._open.add(conversation)
        try:
            await conversation.run()
        finally:
            self._open.discard(conversation)
            self._say(f"{conversation.client or 'A frontend'} disconnected from {peer}.")
