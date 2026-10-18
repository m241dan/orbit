"""The connections frontends make to one session, each with its own handshake and its own
stream of the session's events."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass

from orbit.agent.application.handshake import Handshake, HandshakeParts
from orbit.agent.application.outbox import EventSink
from orbit.protocol import CONNECTION_EVENTS, CommandEnvelope, Event, EventEnvelope, Hello

QUEUE_LIMIT = 10_000
"""How many events a connection's stream holds unread before it's closed as too far behind."""

TOO_FAR_BEHIND = "too far behind"
"""Why a connection whose stream held too many events unread was closed."""

Publish = Callable[[Event], Awaitable[None]]
"""Publishes an event."""

Answer = Callable[[CommandEnvelope, Handshake, Publish], Awaitable[None]]
"""Handles a command from the connection a handshake opened, publishing its outcome."""


class ConnectionDropped(Exception):
    """Ends the stream of a connection the session closed, for the reason in its message."""


class Connections:
    """The connections open to one session, among which its events are routed."""

    def __init__(self) -> None:
        self._open: set[SessionConnection] = set()

    def add(self, connection: "SessionConnection") -> None:
        self._open.add(connection)

    def discard(self, connection: "SessionConnection") -> None:
        self._open.discard(connection)

    def deliver(self, envelope: EventEnvelope) -> None:
        """Pass ``envelope`` to the connection whose command it answers, if it's one of the
        ``CONNECTION_EVENTS``, else to every connection that's listening."""
        asker = _answering.get()
        if asker is not None and isinstance(envelope.payload, CONNECTION_EVENTS):
            asker.receive(envelope)
            return
        for connection in tuple(self._open):
            if connection.listening:
                connection.receive(envelope)

    def end(self) -> None:
        """End every connection's stream once what it has queued is read."""
        for connection in tuple(self._open):
            connection.end()


_answering: ContextVar["SessionConnection | None"] = ContextVar("answering", default=None)
"""The connection whose command is being answered, if any."""


@dataclass(frozen=True)
class ConnectionParts:
    """What a connection to a session uses: the ``handshake`` parts, the ``events`` the session
    publishes into, ``stamp``, which stamps an event without publishing it, the ``connections``
    open to the session, and ``answer``, which handles a command and publishes its outcome."""

    handshake: HandshakeParts
    events: EventSink
    stamp: Callable[[Event], EventEnvelope]
    connections: Connections
    answer: Answer


class SessionConnection:
    """A frontend's connection to a session, from ``parts``: a ``Session`` with its own
    handshake and its own stream.

    Once it's welcomed, the stream gets every event the session publishes, but for the
    ``CONNECTION_EVENTS`` answering another connection's command. What answers a ``Hello``,
    and the outcome of any command before one is welcomed, reaches this connection alone. The
    stream holds at most :data:`QUEUE_LIMIT` events unread; one more closes the connection as
    :data:`TOO_FAR_BEHIND`, and the session and its other connections carry on.
    """

    def __init__(self, parts: ConnectionParts) -> None:
        self._parts = parts
        self._handshake = Handshake(parts.handshake, parts.events, self.receive)
        self._queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(QUEUE_LIMIT)
        self._dropped: ConnectionDropped | None = None
        self._ended = False
        parts.connections.add(self)

    @property
    def listening(self) -> bool:
        """Whether the session's events reach this connection: once it's being welcomed, until
        its stream ends."""
        return self._handshake.listening and not self._ended

    async def send(self, envelope: CommandEnvelope) -> None:
        """Have the session handle the command in ``envelope`` and publish its outcome, to this
        connection alone for a ``Hello`` or a command before one is welcomed. Nothing is handled
        once the stream has ended; an incompatible ``Hello``'s refusal ends it."""
        if self._ended:
            return
        private = isinstance(envelope.payload, Hello) or not self._handshake.welcomed
        publish = self._publish_here if private else self._parts.events.put
        token = _answering.set(self)
        try:
            await self._parts.answer(envelope, self._handshake, publish)
        finally:
            _answering.reset(token)
        if self._handshake.ended:
            self.end()

    async def _publish_here(self, event: Event) -> None:
        """Stamp ``event`` and queue it for this connection alone."""
        self.receive(self._parts.stamp(event))

    def receive(self, envelope: EventEnvelope) -> None:
        """Queue ``envelope`` for the stream, unless it has ended; closes the connection as too
        far behind when the stream holds too many events unread."""
        if self._ended:
            return
        try:
            self._queue.put_nowait(envelope)
        except asyncio.QueueFull:
            self._dropped = ConnectionDropped(TOO_FAR_BEHIND)
            self._finish(immediate=True)

    async def events(self) -> AsyncIterator[EventEnvelope]:
        """The events reaching this connection, ending once it's closed, or failing with
        :class:`ConnectionDropped` once it falls too far behind."""
        while True:
            try:
                envelope = await self._queue.get()
            except asyncio.QueueShutDown:
                if self._dropped is not None:
                    raise self._dropped from None
                return
            yield envelope

    def end(self) -> None:
        """End the stream once what it has queued is read."""
        self._finish(immediate=False)

    async def close(self) -> None:
        """End this connection and its stream; the session and its other connections carry
        on."""
        self._finish(immediate=True)

    def _finish(self, immediate: bool) -> None:
        """End the stream, at once if ``immediate``, and leave the session's connections."""
        self._ended = True
        self._parts.connections.discard(self)
        self._queue.shutdown(immediate)
