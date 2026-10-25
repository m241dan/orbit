"""The connections frontends make to the control plane, each with its own handshake and its own
stream of the events of the workspace it's attached to."""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

from orbit.agent.application.handshake import Handshake, HandshakeParts
from orbit.agent.application.outbox import EventSink
from orbit.protocol import (
    CONNECTION_EVENTS,
    Attach,
    Command,
    CommandEnvelope,
    CommandRefused,
    Event,
    EventEnvelope,
    Hello,
    SessionRef,
)
from orbit.tracing import NoSpan, Span

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
    may have joined or left a session's viewers. Once they're ended, a connection added ends at
    once."""

    def __init__(
        self, viewers_changed: Callable[[], None], open_session: Callable[[], str]
    ) -> None:
        self._open: set[SessionConnection] = set()
        self.viewers_changed = viewers_changed
        self._open_session = open_session
        self._ended = False

    def add(self, connection: SessionConnection) -> None:
        """Route events to ``connection``, or end its stream at once if the connections have
        ended."""
        if self._ended:
            connection.end()
            return
        self._open.add(connection)

    def discard(self, connection: SessionConnection) -> None:
        self._open.discard(connection)

    def viewing(self, connection: SessionConnection) -> SessionRef | None:
        """The session ``connection`` views, if it isn't the open one."""
        attached = connection.attached
        return None if attached is None or attached.id == self._open_session() else attached

    def viewed(self, *, joining: bool = False) -> Counter[str]:
        """How many connections view each session they view: those greeted, and, if
        ``joining``, the one whose command is being answered, if it's attached or attaching."""
        joined = _joiner() if joining else None
        return Counter(self.viewed_by(c) for c in self._open if c.greeted or c is joined)

    def others_viewing(self, session_id: str) -> int:
        """How many greeted connections view the session ``session_id``, but for the one whose
        command is being answered."""
        asker = _asker()
        viewers = (c for c in self._open if c.greeted and c is not asker)
        return sum(1 for c in viewers if self.viewed_by(c) == session_id)

    def others_attached(self, session_id: str) -> list[SessionConnection]:
        """The greeted connections attached to the session ``session_id``, but for the one whose
        command is being answered."""
        asker = _asker()
        others = (c for c in self._open if c.greeted and c is not asker)
        return [c for c in others if c.attached_id == session_id]

    def others_view(self) -> list[str]:
        """The ids of the sessions the connections here view, each once, by id, but for the one
        whose command is being answered."""
        asker = _asker()
        return sorted({self.viewed_by(c) for c in self._open if c is not asker})

    def watching(self, stream: str) -> list[SessionConnection]:
        """The connections here watching the optional stream ``stream``."""
        return [c for c in self._open if stream in c.streams]

    def attached(self, session_id: str) -> list[SessionConnection]:
        """The connections attached to the session ``session_id``."""
        return [c for c in self._open if c.attached_id == session_id]

    def renamed(self, session: SessionRef) -> None:
        """Have the connections attached to the session ``session`` names attached to it as it's
        named now."""
        for connection in self.attached(session.id):
            connection.attach(session)

    def activate(self, left: SessionRef | None) -> None:
        """Have the connection whose command is being answered, if any, view the open session,
        whichever that is, and, given the session ``left``, the others attached and viewing the
        open session keep viewing that one once another is open in its place."""
        asker = _asker()
        for connection in tuple(self._open):
            if connection is asker:
                connection.attach(None)
            elif left is not None and self.viewing(connection) is None:
                connection.attach(left)

    def viewed_by(self, connection: SessionConnection) -> str:
        """The id of the session ``connection`` views."""
        viewing = self.viewing(connection)
        return self._open_session() if viewing is None else viewing.id

    def deliver(self, envelope: EventEnvelope) -> None:
        """Pass ``envelope`` to the connection whose command it answers, if it's one of the
        ``CONNECTION_EVENTS`` not published :func:`to_everyone` and is published by the task
        answering that command, not one it started, else to every connection that's
        listening and views the session it belongs to, if it belongs to one."""
        asker = _asker()
        if asker is not None and _answers_one(envelope):
            asker.receive(envelope)
            return
        for connection in tuple(self._open):
            if connection.listening and self._sees(connection, envelope.session):
                connection.deliver(envelope)

    def broadcast(self, envelope: EventEnvelope) -> None:
        """Pass ``envelope``, a session-less event, to every connection that's listening,
        whichever session it views and whoever's command is being answered."""
        for connection in tuple(self._open):
            if connection.listening:
                connection.deliver(envelope)

    def announce(self, envelope: EventEnvelope) -> None:
        """Pass ``envelope``, a control-plane event, to every connection that's listening, as
        :meth:`broadcast` does, and to every one welcomed but attached to no workspace."""
        for connection in tuple(self._open):
            if connection.listening:
                connection.deliver(envelope)
            elif connection.handshake.welcomed and connection.workspace is None:
                connection.receive(envelope)

    def _sees(self, connection: SessionConnection, session: str | None) -> bool:
        """Whether the events of ``session``, or those of none, reach ``connection``."""
        return session is None or self.viewed_by(connection) == session

    def end(self) -> None:
        """End every connection's stream once what it has queued is read, and that of any added
        later at once."""
        self._ended = True
        for connection in tuple(self._open):
            connection.end()


_answering: ContextVar[tuple[SessionConnection, object] | None] = ContextVar(
    "answering", default=None
)
"""The connection whose command is being answered, if any, with the task answering it."""


def _asker() -> SessionConnection | None:
    """The connection whose command the current task is answering, if any: not one whose
    command was being answered when a task started in the background, which inherits the
    context but answers no one."""
    answering = _answering.get()
    if answering is None or answering[1] is not asyncio.current_task():
        return None
    return answering[0]


def _joiner() -> SessionConnection | None:
    """The connection whose command the current task is answering, if it's attached or
    attaching."""
    asker = _asker()
    return asker if asker is not None and asker.workspace is not None else None


_to_everyone: ContextVar[bool] = ContextVar("to_everyone", default=False)
"""Whether the events published now reach every connection, as the session's new state."""


def _answers_one(envelope: EventEnvelope) -> bool:
    """Whether ``envelope`` answers only the connection whose command is being answered."""
    return isinstance(envelope.payload, CONNECTION_EVENTS) and not _to_everyone.get()


@contextmanager
def to_everyone() -> Iterator[None]:
    """Have the events published within reach every connection that views the session they
    belong to, the ``CONNECTION_EVENTS`` too, even while a connection's command is answered:
    they're the session's new state, not an answer to that connection alone."""
    token = _to_everyone.set(True)
    try:
        yield
    finally:
        _to_everyone.reset(token)


@dataclass(frozen=True)
class Place:
    """Where a connection is: the ``events`` the workspace there publishes into, ``stamp``,
    which stamps an event without publishing it, as the session it's given if one is, and the
    ``connections`` open there."""

    events: EventSink
    stamp: Stamp
    connections: Connections


@dataclass(frozen=True)
class ConnectionParts:
    """What a connection to the control plane uses: the ``handshake`` parts, the ``place`` it
    starts in, before it's attached to a workspace, and ``answer``, which handles a command and
    publishes its outcome."""

    handshake: HandshakeParts
    place: Place
    answer: Answer


class SessionConnection:
    """A frontend's connection to the control plane, from ``parts``: a ``Session`` with its own
    handshake and its own stream. It starts in the ``place`` its parts give, and :meth:`move`\\s
    to the workspace its first ``Attach`` attaches it to.

    Once it's attached, the stream gets every event the workspace publishes of the session it
    views and of none, but for the ``CONNECTION_EVENTS`` answering another connection's command;
    those published :func:`to_everyone`, as a switch's ``Snapshot``, and the ``Catalog`` a change
    to the sessions broadcasts reach it too.
    Once its first ``Attach`` attaches it to a ``workspace``, it views the open session there,
    whichever that is, until :meth:`attach` attaches it to another. What answers a ``Hello``,
    its greeting unlogged, and the outcome of any command before it's attached, or while it
    views a session that isn't open, reaches this connection alone. The
    stream, with the live events held while it's being caught up, holds at most
    :data:`QUEUE_LIMIT` events unread; one more closes the connection as
    :data:`TOO_FAR_BEHIND`, and the session and its other connections carry on.

    Its :attr:`trace` is the span of its WebSocket, if it has one, that what the frontend says
    and does over it is recorded on.
    """

    def __init__(self, parts: ConnectionParts) -> None:
        self._answer = parts.answer
        self._place = parts.place
        self._handshake = Handshake(parts.handshake, self._greet, self.receive)
        self._queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(QUEUE_LIMIT)
        self._dropped: ConnectionDropped | None = None
        self._ended = False
        self.attached: SessionRef | None = None  # None while it views the open session.
        self.workspace: str | None = None  # The id of the workspace attached to, once it is.
        self.streams: frozenset[str] = frozenset()  # The optional streams it watches.
        self.trace: Span = NoSpan("connection")
        self._place.connections.add(self)

    def move(self, place: Place) -> None:
        """Leave the connections where this connection is for those at ``place``, viewing the
        open session there."""
        self._place.connections.discard(self)
        self._place = place
        self.attached = None
        place.connections.add(self)

    @property
    def handshake(self) -> Handshake:
        """The handshake that opens this connection's stream."""
        return self._handshake

    @property
    def viewing(self) -> SessionRef | None:
        """The session this connection views, if it isn't the open one."""
        return self._place.connections.viewing(self)

    @property
    def viewed_id(self) -> str | None:
        """The id of the session this connection views, once it's attached to a workspace."""
        return None if self.workspace is None else self._place.connections.viewed_by(self)

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
        """Whether this connection attached to a session and all that follows its first
        ``Attach`` was published."""
        return self._handshake.greeted

    @property
    def listening(self) -> bool:
        """Whether the session's events reach this connection: once it's attaching, until its
        stream ends."""
        return self._handshake.listening and not self._ended

    async def send(self, envelope: CommandEnvelope) -> None:
        """Have the session handle the command in ``envelope`` and publish its outcome, to this
        connection alone for a ``Hello`` or a command before it's attached, and, unlogged, as
        part of its greeting, for its first ``Attach``. Nothing is handled once the stream has
        ended; an incompatible ``Hello``'s refusal ends it."""
        if self._ended:
            return
        token = _answering.set((self, asyncio.current_task()))
        try:
            await self._answer(envelope, self, self._outcome_publisher(envelope.payload))
        finally:
            _answering.reset(token)
        if self._handshake.ended:
            self.end()
        elif isinstance(envelope.payload, Attach):
            self._place.connections.viewers_changed()

    def _outcome_publisher(self, command: Command) -> Publish:
        """What publishes the outcome of ``command``: the greeting for the first ``Attach``,
        else as :meth:`_publisher` says, privately for a ``Hello``."""
        if isinstance(command, Attach) and self.workspace is None:
            return self._greet
        return self._publisher(isinstance(command, Hello))

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
        it's attached or while it views a session that isn't open, else to every connection;
        which is decided as it's published, as the command may change what it views."""

        async def publish(event: Event) -> None:
            if private or self.workspace is None or self.viewing is not None:
                await self.publish_here(event)
            else:
                await self._place.events.put(event)

        return publish

    async def publish_here(self, event: Event) -> None:
        """Stamp ``event``, as the session this connection views if it isn't the open one, and
        queue it for this connection alone."""
        viewing = self.viewing
        self.receive(self._place.stamp(event, None if viewing is None else viewing.id))

    async def _greet(self, event: Event) -> None:
        """Stamp ``event``, a part of this connection's greeting, and queue it for this
        connection alone, unlogged, as another connection replaying the log mustn't get it."""
        self.receive(self._place.stamp(event, logged=False))

    def deliver(self, envelope: EventEnvelope) -> None:
        """Queue ``envelope``, a live event of the session, for the stream, or have the
        handshake hold it while the frontend is being caught up; closes the connection as too far
        behind when the stream and the handshake hold too many events unread between them. Only
        a listening connection is delivered to."""
        if self._queue.qsize() + self._handshake.held >= QUEUE_LIMIT:
            self._drop()
            return
        self._handshake.deliver(envelope)

    def tell(self, envelope: EventEnvelope) -> None:
        """Queue ``envelope``, an event of a stream this connection watches, as a live event
        once it's listening, else for the stream at once."""
        if self.listening:
            self.deliver(envelope)
        else:
            self.receive(envelope)

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
        self._place.connections.viewers_changed()

    def _finish(self, immediate: bool) -> None:
        """End the stream, at once if ``immediate``, and leave the session's connections."""
        self._ended = True
        self._place.connections.discard(self)
        self._queue.shutdown(immediate)
