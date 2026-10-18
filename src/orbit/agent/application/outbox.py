"""The queue the session's events are published into, each stamped as it's published."""

import asyncio
from collections.abc import Callable
from typing import Protocol

from orbit.agent.application.log_writer import LogWriter
from orbit.agent.application.ports import EventLog
from orbit.protocol import CONNECTION_EVENTS, SESSIONLESS_EVENTS, Event, EventEnvelope


class EventSink(Protocol):
    """What the session's parts publish their events into."""

    async def put(self, item: Event) -> None: ...

    def put_nowait(self, item: Event) -> None: ...


class Outbox:
    """Events published here, read back in envelopes stamped when they were put in: the next
    ``seq``, counted on from the highest in ``log`` (if there is one) else from one, and the
    session ``open_session`` names then, unless the event belongs to none. Each waits in its
    envelope, so no event can be read without its stamp.

    With a ``log``, each envelope of a session is also written to it in the background, but for
    the events answering one connection; :meth:`close` writes those still queued."""

    def __init__(self, open_session: Callable[[], str], log: EventLog | None = None) -> None:
        self._open = open_session
        # The last event's place in the stream, counted across sessions.
        self._seq = log.max_seq() if log else 0
        self._queue: asyncio.Queue[EventEnvelope] = asyncio.Queue()
        self._writer = LogWriter(log, self.put) if log else None

    def put_nowait(self, item: Event) -> None:
        """Stamp ``item`` and queue it."""
        self._seq += 1
        session = None if isinstance(item, SESSIONLESS_EVENTS) else self._open()
        envelope = EventEnvelope(seq=self._seq, session=session, payload=item)
        self._queue.put_nowait(envelope)
        if session is not None:
            self._log(session, envelope)

    async def put(self, item: Event) -> None:
        """Stamp ``item`` and queue it."""
        self.put_nowait(item)  # Unbounded, so never full: stamped and queued at once.

    async def envelope(self) -> EventEnvelope:
        """The next event, in the envelope it was stamped with when published."""
        return await self._queue.get()

    async def close(self) -> None:
        """Write the envelopes still queued for the log, if there is one, and stop writing."""
        if self._writer is not None:
            await self._writer.close()

    def _log(self, session_id: str, envelope: EventEnvelope) -> None:
        """Have ``envelope``, of the session ``session_id``, written to the log, if there is one
        and it's kept there."""
        if self._writer is not None and not isinstance(envelope.payload, CONNECTION_EVENTS):
            self._writer.add(session_id, envelope)
