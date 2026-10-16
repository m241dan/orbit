"""The helpers the lanes share: one piece of work at a time, its crash reported, and a busy
lane's turns."""

import asyncio
import inspect
from contextlib import suppress

from orbit.agent.application.lanes import Background, TurnSlot
from orbit.protocol import BusyChanged, ErrorOccurred, Event


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


def slot() -> tuple[TurnSlot, list[Event]]:
    posted: list[Event] = []
    return TurnSlot("planner", "Busy already.", posted.append), posted


BUSY = BusyChanged(busy=True, lane="planner")
IDLE = BusyChanged(busy=False, lane="planner")


async def ended(turn: "asyncio.Task[object] | None") -> None:
    """Wait until ``turn`` is done and its done-callbacks have run."""
    assert turn is not None
    with suppress(BaseException):
        await turn
    await asyncio.sleep(0)


async def test_a_turn_shows_the_lane_busy_until_it_ends() -> None:
    turns, posted = slot()
    work = Work()

    assert await turns.start(work("first"))
    await work.started.wait()
    assert turns.running and posted == [BUSY]

    work.gate.set()
    await ended(turns.turn)

    assert not turns.running and posted == [BUSY, IDLE]


async def test_a_turn_while_one_runs_is_refused_and_closed() -> None:
    turns, posted = slot()
    work = Work()
    await turns.start(work("first"))
    await work.started.wait()
    second = work("second")

    assert not await turns.start(second)

    assert posted == [BUSY, ErrorOccurred(message="Busy already.")]
    assert inspect.getcoroutinestate(second) == "CORO_CLOSED"
    await turns.close()


async def test_a_crash_is_reported_once_before_the_lane_frees() -> None:
    turns, posted = slot()

    async def crash() -> None:
        raise ValueError("boom")

    await turns.start(crash())
    await ended(turns.turn)
    await turns.close()

    report = ErrorOccurred(
        message="The planner lane stopped on an unexpected error: ValueError('boom')"
    )
    assert posted == [BUSY, report, IDLE]


async def test_a_stale_turns_end_leaves_the_lane_busy() -> None:
    turns, posted = slot()
    work = Work()
    work.gate.set()
    await turns.start(work("first"))
    first = turns.turn
    assert first is not None
    while not first.done():
        await asyncio.sleep(0)

    await turns.start(work("second"))  # Before the first's callback has run.
    await ended(turns.turn)

    assert posted == [BUSY, BUSY, IDLE]


async def test_cancel_stops_the_turn_and_says_whether_it_ran() -> None:
    turns, posted = slot()
    work = Work()
    assert not turns.cancel()
    await turns.start(work("first"))
    await work.started.wait()

    assert turns.cancel()
    await ended(turns.turn)

    assert not turns.cancel() and posted == [BUSY, IDLE]


async def test_close_cancels_the_turn_and_waits_for_it() -> None:
    turns, posted = slot()
    work = Work()
    await turns.start(work("first"))
    await work.started.wait()

    await turns.close()

    assert turns.turn is not None and turns.turn.cancelled()
    await asyncio.sleep(0)
    assert posted == [BUSY, IDLE]


async def test_close_without_a_running_turn_does_nothing() -> None:
    turns, posted = slot()

    await turns.close()

    assert turns.turn is None and posted == []
