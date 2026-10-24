"""A session with a control plane in another process, over a WebSocket: one text frame per
envelope, the token sent as ``Authorization: Bearer`` on the upgrade.

A lost connection is retried with backoff until the session is closed, but for one closed as
its stream ended (1000), for a message over the limit (1009) or for an event that can't be read,
which reconnecting would only repeat; on reconnecting, the last ``Hello`` is sent again with
``since`` the last ``seq`` received, then, once an ``Attach`` was sent, that one again until a
``Snapshot`` answers it or it's refused, and after that one for the workspace and session the
last ``Snapshot`` showed, so the connection views that session again and the control plane
resends what was missed of it, and then the last ``Watch``, unless it watched nothing, as a new
connection watches nothing already. How the link stands is reported to whoever watches
it.
"""

import asyncio
import random
import ssl
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from orbit.client import messages
from orbit.client.tls import refuse_plaintext
from orbit.client.token import TokenUnreadable, read_token
from orbit.client.upgrade import HandshakeRefused, TokenRejected, open_connection
from orbit.protocol import (
    GOING_AWAY,
    MESSAGE_TOO_BIG,
    NORMAL,
    Attach,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Connected,
    Ended,
    EventEnvelope,
    HandshakeFailed,
    Hello,
    LinkState,
    NotConnected,
    ProtocolError,
    Reconnecting,
    Snapshot,
    Stopped,
    TokenRefused,
    Watch,
    decode_event,
    encode,
)

PING_SECONDS = 20
"""How often the connection is pinged, and how long a ping may go unanswered before it's
taken as lost."""


_MOST_DOUBLINGS = 64

_FINAL_CLOSES = frozenset({NORMAL, MESSAGE_TOO_BIG})
"""The close codes that would recur on reconnecting: a stream that ended, as an incompatible
``Hello``'s does, and an event over the limit, which the control plane would resend."""


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
    """The control plane turned the token away, or it couldn't be read; the message says
    which."""


class _Ended(Exception):
    """The connection closed in a way reconnecting would only repeat, for the reason the
    message gives."""


def _closing(websocket: ClientConnection) -> Close | None:
    """The close frame ``websocket`` received, else the one it sent, as one it fails on its
    own, such as for a frame breaking the protocol, isn't answered; none if neither was."""
    return websocket.protocol.close_rcvd or websocket.protocol.close_sent


class WebSocketSession:
    """The control plane at ``url``, shown the token in ``token_file``, read afresh for each
    attempt to connect, so a rotated one is shown on reconnecting, or without one the client
    certificate in ``ssl_context``; a lost connection is retried as ``backoff`` says. A
    ``wss://`` URL is verified with ``ssl_context``, or the system's trusted CAs without one.

    Raises :class:`PlaintextRefused` for a ``ws://`` URL to a host beyond loopback, before
    connecting. It connects once it's first used. While the connection is down, or yet to be
    greeted, :meth:`send` raises :class:`NotConnected`, but for a ``Hello`` or a ``Watch``, which
    is kept to be sent on connecting.
    """

    def __init__(
        self,
        url: str,
        token_file: Path | None,
        backoff: Backoff = BACKOFF,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        refuse_plaintext(url)
        self.url = url
        self._ssl_context = ssl_context
        self._token_file = token_file
        self._backoff = backoff
        self._events: asyncio.Queue[EventEnvelope | Exception | None] = asyncio.Queue()
        self._open: ClientConnection | None = None  # While connected, greeting included.
        self._websocket: ClientConnection | None = None  # While connected, once greeted.
        self._hello: CommandEnvelope | None = None  # The last Hello sent.
        self._attach: CommandEnvelope | None = None  # The last Attach sent.
        self._watch: CommandEnvelope | None = None  # The last Watch sent.
        self._pending = False  # Whether the last Attach sent is yet to be shown or refused.
        self._accepted = False  # Whether the last Attach sent was accepted.
        self._viewed: Attach | None = None  # The session the last Snapshot received showed.
        self._seq: int | None = None  # The last seq received.
        self._ever_linked = False  # Whether a connection was ever made.
        self._watchers: list[Callable[[LinkState], None]] = []
        self.state: LinkState | None = None  # None until the first attempt to connect ends.
        self._running: asyncio.Task[None] | None = None
        self._closed = False

    def watch(self, changed: Callable[[LinkState], None]) -> None:
        """Have ``changed`` called with each state the link moves to."""
        self._watchers.append(changed)

    async def send(self, envelope: CommandEnvelope) -> None:
        """Send ``envelope``; raises :class:`NotConnected` while the connection is down, unless
        it holds a ``Hello`` or a ``Watch``, which is sent on connecting."""
        self._start()
        if isinstance(envelope.payload, Hello):
            self._hello = envelope
            with suppress(NotConnected):  # Sent again on reconnecting.
                await self._send_now(self._greeting(envelope))
            return
        if isinstance(envelope.payload, Watch):
            self._watch = envelope
            with suppress(NotConnected):  # Sent again on reconnecting.
                await self._send_now(envelope)
            return
        if isinstance(envelope.payload, Attach):
            await self._send_attach(envelope)
            return
        await self._send_now(envelope)

    def events(self) -> AsyncIterator[EventEnvelope]:
        """The events the control plane sends, across reconnections, until the session closes;
        raises :class:`ProtocolError` if it sends one that can't be read, and any error keeping
        connected didn't expect, such as a watcher's."""
        self._start()
        return self._received()

    async def close(self) -> None:
        """Close the WebSocket and stop reconnecting; the events end."""
        self._closed = True
        if self._open is not None:
            await self._open.close()
        try:
            if self._running is not None:
                self._running.cancel()
                with suppress(asyncio.CancelledError):
                    await self._running
        finally:
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

    async def _send_attach(self, attach: CommandEnvelope) -> None:
        """Send ``attach``, noted as the last ``Attach`` first, so its answer is known as one
        however soon it comes, unless it can't be sent."""
        previous = self._attach, self._pending, self._accepted
        self._attach, self._pending, self._accepted = attach, True, False
        try:
            await self._send_now(attach)
        except NotConnected:
            self._attach, self._pending, self._accepted = previous
            raise

    def _greeting(self, hello: CommandEnvelope) -> CommandEnvelope:
        """``hello`` asking for the events since the last ``seq`` received, if any was."""
        if self._seq is None:
            return hello
        payload = hello.payload.model_copy(update={"since": self._seq})
        return hello.model_copy(update={"payload": payload})

    async def _run(self) -> None:
        """Keep connected as :meth:`_link` does, ending the events with any error it didn't
        expect, such as a watcher's, so they don't wait for ones that won't come, and the link
        with it, unless it had ended already."""
        try:
            await self._link()
        except Exception as error:
            self._events.put_nowait(error)
            if not isinstance(self.state, Ended):
                # The events already carry the error, so a watcher's failing again is dropped.
                with suppress(Exception):
                    self._report(Ended(self.url, str(error)))

    async def _link(self) -> None:
        """Keep connected until the session closes, the token is turned away or can't be read,
        the connection can't be made safely, or it closes in a way that would recur, as it would
        for an event that can't be read, which also ends the events with its error."""
        try:
            await self._keep_connected()
        except _TurnedAway as error:
            self._report(TokenRefused(str(error)))
        except HandshakeRefused as error:
            self._report(HandshakeFailed(self.url, str(error)))
        except _Ended as ended:
            self._report(Ended(self.url, str(ended)))
        except ProtocolError as error:
            self._events.put_nowait(error)
            # The control plane would resend it on reconnecting, from the last seq received.
            self._report(Ended(self.url, f"An event can't be read: {error}"))

    async def _keep_connected(self) -> None:
        """Connect, and connect again each time the connection fails or is lost, until the
        session closes, waiting the backoff before each attempt again; raises
        :class:`_TurnedAway` if the token is, or can't be read, :class:`HandshakeRefused` if
        the connection can't be made safely, and :class:`_Ended` if it closes in a way that
        would recur. Only a connection that received
        something resets the backoff, so one dropped as soon as it opens counts as a failure."""
        failures = 0
        while True:
            websocket = await self._connect()
            if websocket is not None and await self._linked(websocket):
                failures = 0
            if self._closed:
                return
            self._lost()
            await asyncio.sleep(self._backoff.delay(failures, random.random()))
            failures += 1

    async def _connect(self) -> ClientConnection | None:
        """A new connection to the control plane, shown the token in the token file as it is
        now, if there's one, or none if it can't be made; raises :class:`_TurnedAway` if the
        token is, or can't be read, and :class:`HandshakeRefused` if it can't be made
        safely, as judged knowing whether one was made before."""
        token = self._token()
        try:
            return await open_connection(
                self.url,
                token,
                ssl_context=self._ssl_context,
                linked=self._ever_linked,
                ping_interval=PING_SECONDS,
                ping_timeout=PING_SECONDS,
                # Unlimited: the control plane, shown the token, enforces its own limit.
                max_size=None,
            )
        except TokenRejected:
            raise _TurnedAway(messages.TOKEN_REFUSED.format(path=self._token_file)) from None

    def _token(self) -> str | None:
        """The token in the token file as it is now; none without a token file.

        Raises :class:`_TurnedAway` if it can't be read.
        """
        if self._token_file is None:
            return None
        try:
            return read_token(self._token_file)
        except TokenUnreadable as error:
            raise _TurnedAway(str(error)) from None

    async def _linked(self, websocket: ClientConnection) -> bool:
        """Greet the control plane on ``websocket`` with the last ``Hello``, if any, and take
        its events until the connection closes, noting if it said it was shutting down; whether
        it received anything. Commands are sent on it, and it's reported connected, only once
        it's greeted, so none goes before the ``Hello`` or the ``Attach`` sent again. Raises
        :class:`_Ended` if it closed, from either side, in a way that would recur, unless the
        session closed it."""
        async with websocket:
            self._open = websocket
            self._ever_linked = True
            try:
                with suppress(ConnectionClosed):
                    await self._greet(websocket)
                self._websocket = websocket
                self._report(Connected(self.url))
                received = await self._read(websocket)
            finally:
                self._open = self._websocket = None
        self._closed_as(_closing(websocket))
        return received

    def _closed_as(self, close: Close | None) -> None:
        """Note how the connection closed, as ``close``, the frame that closed it, if any, says:
        report that the control plane is shutting down if it said so, and raise
        :class:`_Ended` if it closed in a way that would recur, unless the session closed it."""
        if close is None:
            return
        if close.code == GOING_AWAY:
            self._report(Stopped(self.url))
        elif close.code in _FINAL_CLOSES and not self._closed:
            raise _Ended(close.reason)

    async def _greet(self, websocket: ClientConnection) -> None:
        """Send the last ``Hello`` on ``websocket``, if any, then, if an ``Attach`` was sent,
        the one attaching the connection again to what it viewed, then the last ``Watch``, unless
        none was sent or it watched nothing, as a new connection watches nothing already."""
        if self._hello is None:
            return
        await websocket.send(encode(self._greeting(self._hello)))
        if self._attach is not None:
            await websocket.send(encode(self._reattach(self._attach)))
        if self._watch is not None and self._watching(self._watch):
            await websocket.send(encode(self._watch))

    @staticmethod
    def _watching(watch: CommandEnvelope) -> bool:
        """Whether ``watch``, a ``Watch``, names any stream."""
        assert isinstance(watch.payload, Watch)  # Only a Watch is kept as one.
        return bool(watch.payload.streams)

    def _reattach(self, attach: CommandEnvelope) -> CommandEnvelope:
        """``attach``, the last ``Attach`` sent, as it was until a ``Snapshot`` answers it or it's
        refused, else for the session the last ``Snapshot`` received showed, if any was, as
        switches and deletions change what the connection views, and a refused one leaves it as
        it was."""
        if self._pending or self._viewed is None:
            return attach
        return attach.model_copy(update={"payload": self._viewed})

    async def _read(self, websocket: ClientConnection) -> bool:
        """Queue the events ``websocket`` brings, skipping those of unknown types, until it
        closes, and whether it brought any; raises :class:`ProtocolError` for one that can't be
        read."""
        received = False
        with suppress(ConnectionClosed):
            while True:
                envelope = decode_event(await websocket.recv(decode=True))
                received = True
                if envelope is not None:
                    self._seq = envelope.seq
                    self._note(envelope)
                    self._events.put_nowait(envelope)
        return received

    def _note(self, envelope: EventEnvelope) -> None:
        """Note what ``envelope`` tells of the view: whether it answers the last ``Attach``
        sent, and the session it shows, if it holds a ``Snapshot``."""
        answer = envelope.payload
        if self._attach is None or not isinstance(answer, CommandAccepted | CommandRefused):
            self._note_shown(envelope)
        elif answer.command_id == self._attach.command_id:
            self._accepted = isinstance(answer, CommandAccepted)
            self._pending = self._pending and self._accepted

    def _note_shown(self, envelope: EventEnvelope) -> None:
        """Note the session ``envelope`` shows, if it holds a ``Snapshot``, in the workspace it
        names, or else that of the last ``Attach`` sent, as answering that ``Attach`` if it's
        the one it named or it came after the ``Attach`` was accepted."""
        snapshot = envelope.payload
        if not isinstance(snapshot, Snapshot) or self._attach is None:
            return
        attached = self._attach.payload
        assert isinstance(attached, Attach)  # Only an Attach is kept as one.
        workspace = envelope.workspace or attached.workspace
        self._viewed = Attach(workspace=workspace, session=snapshot.session.id)
        self._pending = self._pending and not (self._accepted or self._viewed == attached)

    def _lost(self) -> None:
        """Report that the connection is down, from now unless it already was."""
        if not isinstance(self.state, Reconnecting):
            self._report(Reconnecting(self.url, time.monotonic()))

    def _report(self, state: LinkState) -> None:
        """Move to ``state``, telling the watchers."""
        self.state = state
        for changed in self._watchers:
            changed(state)
