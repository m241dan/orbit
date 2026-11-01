"""Batching the backend's events, so a flood still lets the screen repaint."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from orbit.protocol import Event, EventEnvelope, Notice
from orbit.tui.event_stream import event_batches
from tests.stopping import SlowToStop
from tests.tui_fakes import FakeSession


async def finite(*events: Event) -> AsyncIterator[EventEnvelope]:
    for seq, event in enumerate(events, 1):
        yield EventEnvelope(seq=seq, payload=event)


async def collect_batches(events: AsyncIterator[EventEnvelope], limit: int) -> list[list[Event]]:
    return [batch async for batch in event_batches(events, limit)]


async def test_event_batches_takes_every_event_waiting_up_to_the_limit() -> None:
    events = [Notice(message=str(n)) for n in range(5)]
    batches = await collect_batches(finite(*events), limit=2)
    assert batches == [events[:2], events[2:4], events[4:]]


async def test_event_batches_ends_with_the_events() -> None:
    assert await collect_batches(finite(), limit=2) == []


async def test_event_batches_raises_what_reading_the_events_raises() -> None:
    async def failing() -> AsyncIterator[EventEnvelope]:
        yield EventEnvelope(seq=1, payload=Notice(message="before"))
        raise RuntimeError("lost")

    with pytest.raises(RuntimeError, match="lost"):
        await collect_batches(failing(), limit=10)


async def test_event_batches_cancels_its_reader_when_closed() -> None:
    session = FakeSession()
    await session.queue.put(Notice(message="1"))
    batches = event_batches(session.events(), limit=10)
    assert await anext(batches) == [Notice(message="1")]
    await batches.aclose()
    await session.queue.put(Notice(message="2"))
    await asyncio.sleep(0)
    # The reader is gone, so nothing takes the event.
    assert session.queue.qsize() == 1


async def test_closing_event_batches_cancelled_while_the_reader_stops_is_cancelled() -> None:
    work = SlowToStop()

    async def slow_to_stop() -> AsyncIterator[EventEnvelope]:
        yield EventEnvelope(seq=1, payload=Notice(message="1"))
        await work()

    batches = event_batches(slow_to_stop(), limit=10)
    assert await anext(batches) == [Notice(message="1")]
    await asyncio.wait_for(work.started.wait(), 5)
    closing = asyncio.create_task(batches.aclose())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()

    work.gate.set()

    with pytest.raises(asyncio.CancelledError):
        await closing
