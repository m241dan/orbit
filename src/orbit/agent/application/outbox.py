"""The queue the session's events are published into, each stamped as it's published."""

import asyncio
from collections import deque
from collections.abc import Callable

from orbit.protocol import SESSIONLESS_EVENTS, Event, EventEnvelope


class Outbox(asyncio.Queue[Event]):
    """Events published here, read back in envelopes stamped when they were put in: the next
    ``seq``, counted from one, and the session ``open_session`` names then, unless the event
    belongs to none. Read with :meth:`envelope`, not ``get``, which would lose an event's
    stamp."""

    def __init__(self, open_session: Callable[[], str]) -> None:
        super().__init__()
        self._open = open_session
        self._seq = 0  # The last event's place in the stream, counted across sessions.
        self._stamps: deque[tuple[int, str | None]] = deque()

    def put_nowait(self, item: Event) -> None:
        self._seq += 1
        session = None if isinstance(item, SESSIONLESS_EVENTS) else self._open()
        self._stamps.append((self._seq, session))
        super().put_nowait(item)

    async def put(self, item: Event) -> None:
        self.put_nowait(item)  # Unbounded, so never full: stamped and queued at once.

    async def envelope(self) -> EventEnvelope:
        """The next event, in the envelope it was stamped with when published."""
        event = await self.get()
        seq, session = self._stamps.popleft()
        return EventEnvelope(seq=seq, session=session, payload=event)
