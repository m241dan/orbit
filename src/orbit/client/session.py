"""A session with a control plane in another process, over a WebSocket: one text frame per
envelope, the token sent as ``Authorization: Bearer`` on the upgrade.

A lost connection is retried with backoff until the session is closed, and the last ``Hello``
sent again with ``since`` the last ``seq`` received, so the control plane resends what was
missed. How the link stands is reported to whoever watches it.
"""

import asyncio
import random
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from orbit.protocol import (
    CommandEnvelope,
    Connected,
    EventEnvelope,
    Hello,
    LinkState,
    NotConnected,
    ProtocolError,
    Reconnecting,
    Stopped,
    TokenRefused,
    decode_event,
    encode,
)

MAX_MESSAGE_BYTES = 64 * 1024 * 1024
"""The largest message taken from the control plane, as large as it sends by default."""

PING_SECONDS = 20
"""How often the connection is pinged, and how long a ping may go unanswered before it's
taken as lost."""

GOING_AWAY = 1001
"""The close code of a control plane shutting down."""

UNAUTHORIZED = 401
"""The status of an upgrade turned away for its token."""


_MOST_DOUBLINGS = 64


@dataclass(frozen=True)
class Backoff:
    """How long to wait before each attempt to reconnect: ``first`` seconds, doubling up to
    ``most``, each cut at random by up to ``jitter`` of itself."""

    first: float = 0.5
    most: float = 10.0
    jitter: float = 0.5

    def delay(self, failures: int, chance: float) -> float:
        """The wait after ``failures`` attempts in a row failed, ``chance`` a random number from
        0 up to 1."""
        # Capped, so hours of failures can't overflow the float.
        doubled = self.first * 2.0 ** min(failures, _MOST_DOUBLINGS)
        return min(self.most, doubled) * (1 - self.jitter * chance)


BACKOFF = Backoff()


class _TurnedAway(Exception):
    """The control plane turned the token away."""


class WebSocketSession:
    """The control plane at ``url``, shown ``token``, read from ``token_file``; a lost
    connection is retried as ``backoff`` says.

    It connects once it's first used. While the connection is down, :meth:`send` raises
    :class:`NotConnected`, but for a ``Hello``, which is kept to be sent on connecting.
    """

    def __init__(self, url: str, token: str, token_file: Path, backoff: Backoff = BACKOFF) -> None:
        self.url = url
        self._headers = {"Authorization": f"Bearer {token}"}
        self._token_file = token_file
        self._backoff = backoff
        self._events: asyncio.Queue[EventEnvelope | Exception | None] = asyncio.Queue()
        self._websocket: ClientConnection | None = None  # While connected.
        self._hello: CommandEnvelope | None = None  # The last Hello sent.
        self._seq: int | None = None  # The last seq received.
        self._watchers: list[Callable[[LinkState], None]] = []
        self.state: LinkState | None = None  # None until the first attempt to connect ends.
        self._running: asyncio.Task[None] | None = None
        self._closed = False

    def watch(self, changed: Callable[[LinkState], None]) -> None:
        """Have ``changed`` called with each state the link moves to."""
        self._watchers.append(changed)

    async def send(self, envelope: CommandEnvelope) -> None:
        """Send ``envelope``; raises :class:`NotConnected` while the connection is down, unless
        it holds a ``Hello``, which is sent on connecting."""
        self._start()
        if isinstance(envelope.payload, Hello):
            self._hello = envelope
            with suppress(NotConnected):  # Sent again on reconnecting.
                await self._send_now(self._greeting(envelope))
            return
        await self._send_now(envelope)

    def events(self) -> AsyncIterator[EventEnvelope]:
        """The events the control plane sends, across reconnections, until the session closes;
        raises :class:`ProtocolError` if it sends one that can't be read."""
        self._start()
        return self._received()

    async def close(self) -> None:
        """Close the WebSocket and stop reconnecting; the events end."""
        self._closed = True
        if self._websocket is not None:
            await self._websocket.close()
        if self._running is not None:
            self._running.cancel()
            with suppress(asyncio.CancelledError):
                await self._running
        self._events.put_nowait(None)

    def _start(self) -> None:
        """Start connecting, unless it's started or the session is closed."""
        if self._running is None and not self._closed:
            self._running = asyncio.create_task(self._run())

    async def _received(self) -> AsyncIterator[EventEnvelope]:
        """The events received, until the session closes."""
        while (item := await self._events.get()) is not None:
            if isinstance(item, Exception):
                raise item
            yield item

    async def _send_now(self, envelope: CommandEnvelope) -> None:
        """Send ``envelope`` on the connection; raises :class:`NotConnected` if it's down."""
        websocket = self._websocket
        if websocket is None:
            raise NotConnected()
        try:
            await websocket.send(encode(envelope))
        except ConnectionClosed:
            raise NotConnected() from None

    def _greeting(self, hello: CommandEnvelope) -> CommandEnvelope:
        """``hello`` asking for the events since the last ``seq`` received, if any was."""
        if self._seq is None:
            return hello
        payload = hello.payload.model_copy(update={"since": self._seq})
        return hello.model_copy(update={"payload": payload})

    async def _run(self) -> None:
        """Keep connected until the session closes, or the token is turned away; an event that
        can't be read ends the events with its error."""
        try:
            await self._keep_connected()
        except _TurnedAway:
            message = f"The control plane refused the token from {self._token_file}."
            self._report(TokenRefused(message))
        except ProtocolError as error:
            self._events.put_nowait(error)

    async def _keep_connected(self) -> None:
        """Connect, and connect again with backoff each time the connection fails or is lost,
        until the session closes; raises :class:`_TurnedAway` if the token is."""
        failures = 0
        while not self._closed:
            websocket = await self._connect()
            if websocket is None:
                self._lost()
                await asyncio.sleep(self._backoff.delay(failures, random.random()))
                failures += 1
                continue
            failures = 0
            await self._linked(websocket)
            if not self._closed:
                self._lost()

    async def _connect(self) -> ClientConnection | None:
        """A new connection to the control plane, or none if it can't be made; raises
        :class:`_TurnedAway` if the token is."""
        try:
            return await connect(
                self.url,
                additional_headers=self._headers,
                ping_interval=PING_SECONDS,
                ping_timeout=PING_SECONDS,
                max_size=MAX_MESSAGE_BYTES,
                proxy=None,
            )
        except InvalidStatus as error:
            if error.response.status_code == UNAUTHORIZED:
                raise _TurnedAway() from None
            return None
        except (OSError, TimeoutError, WebSocketException):
            return None

    async def _linked(self, websocket: ClientConnection) -> None:
        """Greet the control plane on ``websocket`` with the last ``Hello``, if any, and take
        its events until the connection closes, noting if it said it was shutting down."""
        async with websocket:
            self._websocket = websocket
            try:
                self._report(Connected(self.url))
                if self._hello is not None:
                    with suppress(ConnectionClosed):
                        await websocket.send(encode(self._greeting(self._hello)))
                await self._read(websocket)
            finally:
                self._websocket = None
        if websocket.close_code == GOING_AWAY:
            self._report(Stopped(self.url))

    async def _read(self, websocket: ClientConnection) -> None:
        """Queue the events ``websocket`` brings, skipping those of unknown types, until it
        closes; raises :class:`ProtocolError` for one that can't be read."""
        with suppress(ConnectionClosed):
            while True:
                envelope = decode_event(await websocket.recv(decode=True))
                if envelope is not None:
                    self._seq = envelope.seq
                    self._events.put_nowait(envelope)

    def _lost(self) -> None:
        """Report that the connection is down, from now unless it already was."""
        if not isinstance(self.state, Reconnecting):
            self._report(Reconnecting(self.url, time.monotonic()))

    def _report(self, state: LinkState) -> None:
        """Move to ``state``, telling the watchers."""
        self.state = state
        for changed in self._watchers:
            changed(state)
