"""The usage monitor: fetches the usage limits on a schedule and publishes them."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest

from orbit.agent import WorkspaceHost
from orbit.agent.application import usage_monitor
from orbit.agent.application.usage_monitor import UsageMonitor
from orbit.agent.domain.usage import UsageFetch, UsageRateLimited, UsageReport, UsageUnavailable
from orbit.protocol import Event, TurnFinished, UsageChanged, UsageLimit
from tests.agent_fakes import host_of, payloads, publish, served

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
LIMITS = (UsageLimit(label="Session", percent=52.0),)
LATER = (UsageLimit(label="Session", percent=60.0),)


class Source:
    """Returns (or raises) ``outcomes`` in turn, waiting for ``gate`` first if set; once
    they run out, a fetch never ends."""

    def __init__(self, *outcomes: UsageFetch | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.gate: asyncio.Event | None = None

    async def __call__(self) -> UsageFetch:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if not self.outcomes:
            await asyncio.Event().wait()
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Clock:
    def __init__(self) -> None:
        self.time = 0.0

    def __call__(self) -> float:
        return self.time


class Monitored:
    """A monitor over ``source``, with its clock and the events it publishes."""

    def __init__(self, source: Source) -> None:
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self.clock = Clock()
        self.monitor = UsageMonitor(source, self.events.put, self.clock, lambda: NOW)

    async def next(self) -> Event:
        return await asyncio.wait_for(self.events.get(), 1)

    async def none(self) -> bool:
        """Whether nothing is published for a moment."""
        try:
            await asyncio.wait_for(self.events.get(), 0.05)
        except TimeoutError:
            return True
        return False


@pytest.fixture
def no_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage_monitor, "next_delay", lambda outcome: 0)


async def test_the_first_fetch_is_published_right_away() -> None:
    watched = Monitored(Source(UsageReport(LIMITS)))
    watched.monitor.start()
    assert await watched.next() == UsageChanged(limits=LIMITS)
    await watched.monitor.close()


@pytest.mark.usefixtures("no_delay")
async def test_the_last_limits_are_kept_when_a_fetch_brings_none() -> None:
    source = Source(UsageReport(LIMITS), UsageRateLimited(120), UsageUnavailable("offline"))
    watched = Monitored(source)
    watched.monitor.start()
    events = [await watched.next() for _ in range(3)]
    retry_at = NOW + timedelta(seconds=120)
    assert events == [
        UsageChanged(limits=LIMITS),
        UsageChanged(limits=LIMITS, status="rate_limited", retry_at=retry_at),
        UsageChanged(limits=LIMITS, status="unavailable", detail="offline"),
    ]
    await watched.monitor.close()


@pytest.mark.usefixtures("no_delay")
async def test_a_source_that_raises_is_unavailable_and_the_loop_carries_on() -> None:
    watched = Monitored(Source(RuntimeError("boom"), RuntimeError(), UsageReport(LIMITS)))
    watched.monitor.start()
    events = [await watched.next() for _ in range(3)]
    assert events == [
        UsageChanged(status="unavailable", detail="boom"),
        UsageChanged(status="unavailable", detail="error"),
        UsageChanged(limits=LIMITS),
    ]
    await watched.monitor.close()


async def test_a_nudge_fetches_again_once_a_refresh_is_due() -> None:
    watched = Monitored(Source(UsageReport(LIMITS), UsageReport(LATER)))
    watched.monitor.start()
    await watched.next()
    watched.clock.time = 60
    watched.monitor.nudge()
    assert await watched.next() == UsageChanged(limits=LATER)
    await watched.monitor.close()


async def test_a_nudge_under_a_minute_after_the_last_fetch_does_nothing() -> None:
    source = Source(UsageReport(LIMITS), UsageReport(LATER))
    watched = Monitored(source)
    watched.monitor.start()
    await watched.next()
    watched.clock.time = 59
    watched.monitor.nudge()
    assert await watched.none()
    assert source.calls == 1
    await watched.monitor.close()


async def test_a_nudge_before_a_rate_limits_retry_time_does_nothing() -> None:
    source = Source(UsageRateLimited(600), UsageReport(LATER))
    watched = Monitored(source)
    watched.monitor.start()
    await watched.next()
    watched.clock.time = 60
    watched.monitor.nudge()
    assert await watched.none()
    assert source.calls == 1
    watched.clock.time = 600
    watched.monitor.nudge()
    assert await watched.next() == UsageChanged(limits=LATER)
    await watched.monitor.close()


async def test_a_nudge_before_an_unspecified_rate_limits_retry_time_does_nothing() -> None:
    source = Source(UsageRateLimited(None), UsageReport(LATER))
    watched = Monitored(source)
    watched.monitor.start()
    await watched.next()
    watched.clock.time = 60
    watched.monitor.nudge()
    assert await watched.none()
    assert source.calls == 1
    await watched.monitor.close()


async def test_a_nudge_a_minute_after_unavailable_usage_fetches_again() -> None:
    watched = Monitored(Source(UsageUnavailable("offline"), UsageReport(LATER)))
    watched.monitor.start()
    await watched.next()
    watched.clock.time = 60
    watched.monitor.nudge()
    assert await watched.next() == UsageChanged(limits=LATER)
    await watched.monitor.close()


async def test_a_nudge_during_a_fetch_does_nothing() -> None:
    source = Source(UsageReport(LIMITS), UsageReport(LATER))
    source.gate = asyncio.Event()
    watched = Monitored(source)
    watched.monitor.start()
    while source.calls == 0:
        await asyncio.sleep(0)
    watched.clock.time = 100
    watched.monitor.nudge()
    source.gate.set()
    assert await watched.next() == UsageChanged(limits=LIMITS)
    assert await watched.none()
    assert source.calls == 1
    await watched.monitor.close()


async def test_a_nudge_before_the_first_fetch_does_nothing() -> None:
    source = Source(UsageReport(LIMITS))
    watched = Monitored(source)
    watched.clock.time = 100
    watched.monitor.nudge()
    assert source.calls == 0
    await watched.monitor.close()


async def test_close_cancels_the_loop_and_can_be_repeated() -> None:
    source = Source()
    watched = Monitored(source)
    watched.monitor.start()
    while source.calls == 0:
        await asyncio.sleep(0)
    await watched.monitor.close()
    await watched.monitor.close()
    assert await watched.none()


class Swallowing(Source):
    """A fetch that never ends until cancelled, then swallows the cancel and returns a report,
    as the usage API's was seen to."""

    async def __call__(self) -> UsageFetch:
        self.calls += 1
        with suppress(asyncio.CancelledError):
            await asyncio.Event().wait()
        return UsageReport(LIMITS)


async def test_close_stops_the_loop_when_the_source_swallows_the_cancel() -> None:
    source = Swallowing()
    watched = Monitored(source)
    watched.monitor.start()
    while source.calls == 0:
        await asyncio.sleep(0)
    closing = asyncio.create_task(watched.monitor.close())
    await asyncio.wait([closing], timeout=1)
    assert closing.done()
    assert await watched.none()
    assert source.calls == 1


async def test_close_before_start_does_nothing() -> None:
    watched = Monitored(Source())
    await watched.monitor.close()
    assert await watched.none()


async def usage_events(stream: AsyncIterator[Event], count: int) -> list[UsageChanged]:
    """The next ``count`` usage events in ``stream``, skipping the others."""
    found: list[UsageChanged] = []
    while len(found) < count:
        event = await asyncio.wait_for(anext(stream), 1)
        if isinstance(event, UsageChanged):
            found.append(event)
    return found


async def quiet_of_usage(stream: AsyncIterator[Event]) -> bool:
    """Whether ``stream`` publishes no usage event for a moment."""
    try:
        while True:
            event = await asyncio.wait_for(anext(stream), 0.05)
            assert not isinstance(event, UsageChanged)
    except TimeoutError:
        return True


@pytest.fixture
def always_due(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage_monitor, "refresh_due", lambda seconds, outcome: True)


async def test_a_session_without_a_usage_source_sends_no_usage() -> None:
    session = WorkspaceHost()
    await publish(session, TurnFinished(turn_id="t", agent_id="agent-1", reason="completed"))
    assert await quiet_of_usage(payloads(session))
    await session.close()


async def test_a_session_with_a_usage_source_publishes_its_usage() -> None:
    session = served(WorkspaceHost(), Source(UsageReport(LIMITS)))
    assert await usage_events(payloads(session), 1) == [UsageChanged(limits=LIMITS)]
    await session.close()


@pytest.mark.usefixtures("always_due")
async def test_an_agents_finished_turn_nudges_the_usage_monitor() -> None:
    session = served(WorkspaceHost(), Source(UsageReport(LIMITS), UsageReport(LATER)))
    stream = payloads(session)
    await usage_events(stream, 1)
    await publish(
        host_of(session), TurnFinished(turn_id="t", agent_id="agent-1", reason="completed")
    )
    assert await usage_events(stream, 1) == [UsageChanged(limits=LATER)]
    await session.close()


@pytest.mark.usefixtures("always_due")
async def test_a_turn_finished_without_an_agent_does_not_nudge() -> None:
    session = served(WorkspaceHost(), Source(UsageReport(LIMITS), UsageReport(LATER)))
    stream = payloads(session)
    await usage_events(stream, 1)
    await publish(host_of(session), TurnFinished(turn_id="t", reason="completed"))
    assert await quiet_of_usage(stream)
    await session.close()
