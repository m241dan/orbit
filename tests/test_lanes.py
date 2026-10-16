"""The background helper the lanes share: one piece of work at a time, its crash reported."""

import asyncio

from orbit.agent.application.lanes import Background
from orbit.protocol import ErrorOccurred, Event


class Work:
    """Work that records its runs and, once started, waits for ``gate``."""

    def __init__(self) -> None:
        self.runs: list[str] = []
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    async def __call__(self, name: str) -> None:
        self.runs.append(name)
        self.started.set()
        await self.gate.wait()


def background() -> tuple[Background, "asyncio.Queue[Event]"]:
    events: asyncio.Queue[Event] = asyncio.Queue()
    return Background("test", events.put), events


async def test_start_runs_the_work_in_the_background() -> None:
    lane, events = background()
    work = Work()

    lane.start(work("first"))
    await work.started.wait()

    assert lane.running and work.runs == ["first"]
    work.gate.set()
    await lane.close()
    assert not lane.running and events.empty()


async def test_start_while_running_drops_the_new_work() -> None:
    lane, _ = background()
    work = Work()
    lane.start(work("first"))
    await work.started.wait()

    lane.start(work("second"))
    work.gate.set()
    await lane.close()

    assert work.runs == ["first"]


async def test_cancel_stops_the_work() -> None:
    lane, events = background()
    work = Work()
    lane.start(work("first"))
    await work.started.wait()

    lane.cancel()
    await lane.close()

    assert not lane.running and events.empty()


async def test_work_cancelled_before_it_starts_never_runs() -> None:
    lane, _ = background()
    work = Work()

    lane.start(work("first"))
    await lane.close()

    assert work.runs == [] and not lane.running


async def test_close_when_never_started_does_nothing() -> None:
    lane, events = background()

    await lane.close()

    assert not lane.running and events.empty()


async def test_a_crash_is_reported_once_and_close_does_not_raise_it() -> None:
    lane, events = background()

    async def crash() -> None:
        raise ValueError("boom")

    lane.start(crash())
    report = await asyncio.wait_for(events.get(), 1)
    await lane.close()
    await lane.close()

    assert report == ErrorOccurred(
        message="The test lane stopped on an unexpected error: ValueError('boom')"
    )
    assert events.empty()
