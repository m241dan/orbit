"""The queue the session's events are published into, each stamped as it's published."""

import asyncio
from collections.abc import Callable
from typing import Protocol

from orbit.protocol import SESSIONLESS_EVENTS, Event, EventEnvelope


class EventSink(Protocol):
    """What the session's parts publish their events into."""

    async def put(self, item: Event) -> None: ...

    def put_nowait(self, item: Event) -> None: ...


class Outbox:
    """Events published here, read back in envelopes stamped when they were put in: the next
    ``seq``, counted from one, and the session ``open_session`` names then, unless the event
    belongs to none. Each waits in its envelope, so no event can be read without its stamp."""

    def __init__(self, open_session: Callable[[], str]) -> None:
        self._open = open_session
        self._seq = 0  # The last event's place in the stream, counted across sessions.
        self._queue: asyncio.Queue[EventEnvelope] = asyncio.Queue()

    def put_nowait(self, item: Event) -> None:
        """Stamp ``item`` and queue it."""
        self._seq += 1
        session = None if isinstance(item, SESSIONLESS_EVENTS) else self._open()
        self._queue.put_nowait(EventEnvelope(seq=self._seq, session=session, payload=item))

    async def put(self, item: Event) -> None:
        """Stamp ``item`` and queue it."""
        self.put_nowait(item)  # Unbounded, so never full: stamped and queued at once.

    async def envelope(self) -> EventEnvelope:
        """The next event, in the envelope it was stamped with when published."""
        return await self._queue.get()
