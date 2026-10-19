"""The connections frontends make to one session, each with its own handshake and its own
stream of the session's events."""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

from orbit.agent.application.handshake import Handshake, HandshakeParts
from orbit.agent.application.outbox import EventSink
from orbit.protocol import (
    CONNECTION_EVENTS,
    CommandEnvelope,
    CommandRefused,
    Event,
    EventEnvelope,
    Hello,
    SessionRef,
)

QUEUE_LIMIT = 10_000
"""How many events a connection's stream holds unread before it's closed as too far behind."""

TOO_FAR_BEHIND = "too far behind"
"""Why a connection whose stream held too many events unread was closed."""

Publish = Callable[[Event], Awaitable[None]]
"""Publishes an event."""

Answer = Callable[[CommandEnvelope, "SessionConnection", Publish], Awaitable[None]]
"""Handles a command from a connection, publishing its outcome."""


class ConnectionDropped(Exception):
    """Ends the stream of a connection the session closed, for the reason in its message."""


class Stamp(Protocol):
    """Stamps an event without publishing it, as the session it's given, if one is, and logs
    it, unless it's ``viewed`` or not ``logged``."""

    def __call__(
        self, item: Event, viewed: str | None = None, *, logged: bool = True
    ) -> EventEnvelope: ...


class Connections:
    """The connections open to one session, among which its events are routed, each viewing
    the session ``open_session`` names or another; ``viewers_changed`` is called whenever one
    may have joined or left a session's viewers."""

    def __init__(
        self, viewers_changed: Callable[[], None], open_session: Callable[[], str]
    ) -> None:
        self._open: set[SessionConnection] = set()
        self.viewers_changed = viewers_changed
        self._open_session = open_session

    def add(self, connection: "SessionConnection") -> None:
        self._open.add(connection)

    def discard(self, connection: "SessionConnection") -> None:
        self._open.discard(connection)

    def viewing(self, connection: "SessionConnection") -> SessionRef | None:
        """The session ``connection`` views, if it isn't the open one."""
        attached = connection.attached
        return None if attached is None or attached.id == self._open_session() else attached

    def viewed(self, *, joining: bool = False) -> Counter[str]:
        """How many connections view each session they view: those greeted, and, if
        ``joining``, the one whose ``Hello`` is being answered."""
        asker = _answering.get() if joining else None
        return Counter(self._viewed_by(c) for c in self._open if c.greeted or c is asker)

    def others_viewing(self, session_id: str) -> int:
        """How many greeted connections view the session ``session_id``, but for the one whose
        command is being answered."""
        asker = _answering.get()
        viewers = (c for c in self._open if c.greeted and c is not asker)
        return sum(1 for c in viewers if self._viewed_by(c) == session_id)

    def others_attached(self, session_id: str) -> list["SessionConnection"]:
        """The greeted connections attached to the session ``session_id``, but for the one whose
        command is being answered."""
        asker = _answering.get()
        others = (c for c in self._open if c.greeted and c is not asker)
        return [c for c in others if c.attached_id == session_id]

    def activate(self, left: SessionRef | None) -> None:
        """Have the connection whose command is being answered, if any, view the open session,
        whichever that is, and, given the session ``left``, the others viewing the open session
        keep viewing that one once another is open in its place."""
        asker = _answering.get()
        for connection in tuple(self._open):
            if connection is asker:
                connection.attach(None)
            elif left is not None and self.viewing(connection) is None:
                connection.attach(left)

    def _viewed_by(self, connection: "SessionConnection") -> str:
        """The id of the session ``connection`` views."""
        viewing = self.viewing(connection)
        return self._open_session() if viewing is None else viewing.id

    def deliver(self, envelope: EventEnvelope) -> None:
        """Pass ``envelope`` to the connection whose command it answers, if it's one of the
        ``CONNECTION_EVENTS``, else to every connection that's listening and views the session
        it belongs to, if it belongs to one."""
        asker = _answering.get()
        if asker is not None and isinstance(envelope.payload, CONNECTION_EVENTS):
            asker.receive(envelope)
            return
        for connection in tuple(self._open):
            if connection.listening and self._sees(connection, envelope.session):
                connection.deliver(envelope)

    def _sees(self, connection: "SessionConnection", session: str | None) -> bool:
        """Whether the events of ``session``, or those of none, reach ``connection``."""
        return session is None or self._viewed_by(connection) == session

    def end(self) -> None:
        """End every connection's stream once what it has queued is read."""
        for connection in tuple(self._open):
            connection.end()


_answering: ContextVar["SessionConnection | None"] = ContextVar("answering", default=None)
"""The connection whose command is being answered, if any."""


@dataclass(frozen=True)
class ConnectionParts:
    """What a connection to a session uses: the ``handshake`` parts, the ``events`` the session
    publishes into, ``stamp``, which stamps an event without publishing it, as the session it's
    given if one is, the ``connections`` open to the session, and ``answer``, which handles a
    command and publishes its outcome."""

    handshake: HandshakeParts
    events: EventSink
    stamp: Stamp
    connections: Connections
    answer: Answer


class SessionConnection:
    """A frontend's connection to a session, from ``parts``: a ``Session`` with its own
    handshake and its own stream.

    Once it's welcomed, the stream gets every event the session publishes of the session it
    views and of none, but for the ``CONNECTION_EVENTS`` answering another connection's command.
    It views the open session, whichever that is, until :meth:`attach` attaches it to another.
    What answers a ``Hello``, its greeting unlogged, and the outcome of any command before one
    is welcomed, or while it views a session that isn't open, reaches this connection alone. The
    stream, with the live events held while it's being caught up, holds at most
    :data:`QUEUE_LIMIT` events unread; one more closes the connection as
    :data:`TOO_FAR_BEHIND`, and the session and its other connections carry on.
    """

    def __init__(self, parts: ConnectionParts) -> None:
        self._parts = parts
        self._handshake = Handshake(parts.handshake, self._greet, self.receive)
        self._queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(QUEUE_LIMIT)
        self._dropped: ConnectionDropped | None = None
        self._ended = False
        self.attached: SessionRef | None = None  # None while it views the open session.
        parts.connections.add(self)

    @property
    def handshake(self) -> Handshake:
        """The handshake that opens this connection's stream."""
        return self._handshake

    @property
    def viewing(self) -> SessionRef | None:
        """The session this connection views, if it isn't the open one."""
        return self._parts.connections.viewing(self)

    @property
    def attached_id(self) -> str | None:
        """The id of the session this connection was attached to, if any."""
        return None if self.attached is None else self.attached.id

    def attach(self, session: SessionRef | None) -> None:
        """View ``session`` from now on, or the open session, whichever that is, if it's
        none."""
        self.attached = session

    @property
    def greeted(self) -> bool:
        """Whether this connection's ``Hello`` was welcomed and all that follows it published."""
        return self._handshake.greeted

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
        publish = self._publisher(private=isinstance(envelope.payload, Hello))
        token = _answering.set(self)
        try:
            await self._parts.answer(envelope, self, publish)
        finally:
            _answering.reset(token)
        if self._handshake.ended:
            self.end()
        elif isinstance(envelope.payload, Hello):
            self._parts.connections.viewers_changed()

    async def refuse(self, command_id: str, code: str, message: str) -> None:
        """Publish ``CommandRefused`` with ``code`` and ``message`` for a command sent as
        ``command_id`` that couldn't be read, as the outcome of one that could would be; nothing
        once the stream has ended."""
        if self._ended:
            return
        refused = CommandRefused(command_id=command_id, code=code, message=message)
        await self._publisher(private=False)(refused)

    def _publisher(self, private: bool) -> Publish:
        """What publishes a command's outcome: to this connection alone if ``private``, before
        one is welcomed or while it views a session that isn't open, else to every connection;
        which is decided as it's published, as the command may change what it views."""

        async def publish(event: Event) -> None:
            if private or not self._handshake.welcomed or self.viewing is not None:
                await self.publish_here(event)
            else:
                await self._parts.events.put(event)

        return publish

    async def publish_here(self, event: Event) -> None:
        """Stamp ``event``, as the session this connection views if it isn't the open one, and
        queue it for this connection alone."""
        viewing = self.viewing
        self.receive(self._parts.stamp(event, None if viewing is None else viewing.id))

    async def _greet(self, event: Event) -> None:
        """Stamp ``event``, a part of this connection's greeting, and queue it for this
        connection alone, unlogged, as another connection replaying the log mustn't get it."""
        self.receive(self._parts.stamp(event, logged=False))

    def deliver(self, envelope: EventEnvelope) -> None:
        """Queue ``envelope``, a live event of the session, for the stream, or have the
        handshake hold it while the frontend is being caught up; closes the connection as too far
        behind when the stream and the handshake hold too many events unread between them. Only
        a listening connection is delivered to."""
        if self._queue.qsize() + self._handshake.held >= QUEUE_LIMIT:
            self._drop()
            return
        self._handshake.deliver(envelope)

    def receive(self, envelope: EventEnvelope) -> None:
        """Queue ``envelope`` for the stream, unless it has ended; closes the connection as too
        far behind when the stream holds too many events unread."""
        if self._ended:
            return
        try:
            self._queue.put_nowait(envelope)
        except asyncio.QueueFull:
            self._drop()

    def _drop(self) -> None:
        """Close the connection as too far behind."""
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
        self._parts.connections.viewers_changed()

    def _finish(self, immediate: bool) -> None:
        """End the stream, at once if ``immediate``, and leave the session's connections."""
        self._ended = True
        self._parts.connections.discard(self)
        self._queue.shutdown(immediate)
