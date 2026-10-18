"""Batches of the backend's events, so a flood still lets the screen repaint."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import suppress

from orbit.protocol import Event, EventEnvelope

# Most events rendered in one go, so a flood still lets the screen repaint now and then.
BATCH_LIMIT = 500


async def event_batches(
    events: AsyncIterator[EventEnvelope], limit: int = BATCH_LIMIT
) -> AsyncGenerator[list[Event]]:
    """Batches of the events in ``events``' envelopes: each the next event and those already
    waiting behind it, up to ``limit``. Ends when ``events`` do, raising what reading them
    raised."""
    queue: asyncio.Queue[Event | None] = asyncio.Queue()

    async def read() -> None:
        try:
            async for envelope in events:
                queue.put_nowait(envelope.payload)
        finally:
            queue.put_nowait(None)  # The end.

    reader = asyncio.create_task(read())
    try:
        while batch := await _next_batch(queue, limit):
            yield batch
    finally:
        reader.cancel()
        with suppress(asyncio.CancelledError):
            await reader


async def _next_batch(queue: asyncio.Queue[Event | None], limit: int) -> list[Event]:
    """The next event in ``queue`` and those waiting behind it, up to ``limit``; none at the
    end, which stays queued for the next call."""
    batch: list[Event] = []
    event = await queue.get()
    while event is not None:
        batch.append(event)
        if len(batch) == limit or queue.empty():
            return batch
        event = queue.get_nowait()
    queue.put_nowait(None)
    return batch
