"""The helpers the lanes share: one piece of work at a time, its crash reported, and a busy
lane's turns."""

import asyncio
import inspect
from contextlib import suppress
from typing import Any

import pytest

from orbit.agent.application.lanes import Background, TurnSlot, stop
from orbit.agent.domain.refusals import Refused
from orbit.protocol import BUSY as BUSY_CODE
from orbit.protocol import INTERNAL, BusyChanged, ErrorOccurred, Event
from orbit.tracing import NoSpan, Span, current_span, use_span
from tests.agent_fakes import logged_with_traceback
from tests.waiting import eventually


class SlowToStop:
    """Work that, once started, runs until cancelled and then waits for ``gate`` to end."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def __call__(self) -> None:
        try:
            self.started.set()
            await asyncio.Event().wait()
        finally:
            await self.gate.wait()


async def cancelled_while_stopping(
    stopping: asyncio.Task[None], child: asyncio.Task[Any], gate: asyncio.Event
) -> None:
    """Cancel ``stopping`` while ``child`` stops, then let the child end through ``gate``."""
    await asyncio.sleep(0)
    stopping.cancel()
    await asyncio.sleep(0)
    assert not stopping.done() and not child.done()
    gate.set()


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


async def test_stop_cancels_the_task_and_waits_for_it_to_end() -> None:
    work = SlowToStop()
    child = asyncio.create_task(work())
    await work.started.wait()
    work.gate.set()

    await stop(child)

    assert child.cancelled()


async def test_stop_cancelled_meanwhile_is_cancelled_once_the_task_has_ended() -> None:
    work = SlowToStop()
    child = asyncio.create_task(work())
    await work.started.wait()
    stopping = asyncio.create_task(stop(child))

    await cancelled_while_stopping(stopping, child, work.gate)

    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert child.done()


async def test_stop_of_a_finished_task_does_nothing() -> None:
    child = asyncio.create_task(asyncio.sleep(0))
    await child

    await stop(child)

    assert not child.cancelled()


def background() -> tuple[Background, asyncio.Queue[Event]]:
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


class NotesSpan:
    """Work that notes the span current as it runs, then is ``done``."""

    def __init__(self) -> None:
        self.seen: list[Span] = []
        self.done = asyncio.Event()

    async def __call__(self) -> None:
        self.seen.append(current_span())
        self.done.set()


async def test_work_started_inside_a_span_runs_with_no_span_current() -> None:
    lane, _ = background()
    work = NotesSpan()

    with use_span(NoSpan("task")):
        lane.start(work())
    await work.done.wait()
    await lane.close()

    assert work.seen == [current_span()]  # The no-op span current outside any block.


async def test_ended_is_called_once_the_work_has_stopped() -> None:
    lane, _ = background()
    work = Work()
    seen: list[bool] = []
    lane.ended = lambda: seen.append(lane.running)
    lane.start(work("first"))
    await work.started.wait()
    assert seen == []

    work.gate.set()
    await lane.close()
    await asyncio.sleep(0)

    assert seen == [False]


async def test_start_while_running_drops_the_new_work() -> None:
    lane, _ = background()
    work = Work()
    assert lane.start(work("first"))
    await work.started.wait()

    assert not lane.start(work("second"))
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


async def test_cancel_when_never_started_does_nothing() -> None:
    lane, events = background()

    lane.cancel()

    assert not lane.running and events.empty()


async def test_close_when_never_started_does_nothing() -> None:
    lane, events = background()

    await lane.close()

    assert not lane.running and events.empty()


async def test_start_after_close_closes_the_work_and_starts_no_task() -> None:
    lane, events = background()
    work = Work()
    await lane.close()

    coroutine = work("late")
    assert not lane.start(coroutine)
    await asyncio.sleep(0)

    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    assert work.runs == [] and not lane.running and events.empty()
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_close_cancelled_while_the_work_stops_is_cancelled() -> None:
    lane, _ = background()
    started, stop = asyncio.Event(), asyncio.Event()

    async def slow_to_stop() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await stop.wait()

    lane.start(slow_to_stop())
    await started.wait()
    closing = asyncio.create_task(lane.close())
    await asyncio.sleep(0)
    closing.cancel()
    stop.set()

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not lane.running


async def test_a_crash_is_reported_once_and_close_does_not_raise_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lane, events = background()

    async def crash() -> None:
        raise ValueError("boom")

    lane.start(crash())
    report = await asyncio.wait_for(events.get(), 1)
    await lane.close()
    await lane.close()

    message = "The test lane stopped on an unexpected error: boom"
    assert report == ErrorOccurred(code=INTERNAL, message=message)
    assert events.empty()
    assert logged_with_traceback(caplog.records, message)


def slot() -> tuple[TurnSlot, list[Event]]:
    posted: list[Event] = []
    return TurnSlot("planner", "Busy already.", posted.append), posted


async def test_a_turn_started_inside_a_span_runs_with_no_span_current() -> None:
    turns, _ = slot()
    work = NotesSpan()

    with use_span(NoSpan("task")):
        await turns.start(work())
    await work.done.wait()
    await turns.close()

    assert work.seen == [current_span()]


BUSY = BusyChanged(busy=True, lane="planner")
IDLE = BusyChanged(busy=False, lane="planner")


async def ended(turn: asyncio.Task[object] | None) -> None:
    """Wait until ``turn`` is done and its done-callbacks have run."""
    assert turn is not None
    with suppress(BaseException):
        await turn
    await asyncio.sleep(0)


async def test_a_turn_shows_the_lane_busy_until_it_ends() -> None:
    turns, posted = slot()
    work = Work()

    await turns.start(work("first"))
    await work.started.wait()
    assert turns.running and posted == [BUSY]

    work.gate.set()
    await ended(turns.turn)

    assert not turns.running and posted == [BUSY, IDLE]


async def test_the_lane_is_running_when_its_busy_is_posted() -> None:
    seen: list[bool] = []
    turns = TurnSlot("planner", "Busy already.", lambda _: seen.append(turns.running))
    work = Work()

    await turns.start(work("first"))
    work.gate.set()
    await ended(turns.turn)

    assert seen == [True, False]


async def test_a_turn_while_one_runs_is_refused_and_closed() -> None:
    turns, posted = slot()
    work = Work()
    await turns.start(work("first"))
    await work.started.wait()
    second = work("second")

    with pytest.raises(Refused) as refused:
        await turns.start(second)

    assert (refused.value.code, str(refused.value)) == (BUSY_CODE, "Busy already.")
    assert posted == [BUSY]
    assert inspect.getcoroutinestate(second) == "CORO_CLOSED"
    await turns.close()


async def test_a_turn_with_a_refusal_of_its_own_refuses_the_next_with_it() -> None:
    turns, _ = slot()
    work = Work()
    turn = await turns.start(work("first"), "The architect is already answering.")
    assert turn is turns.turn

    with pytest.raises(Refused) as answering:
        await turns.start(work("second"))
    await turns.close()
    await turns.start(work("third"))
    with pytest.raises(Refused) as busy:
        await turns.start(work("fourth"))
    await turns.close()

    assert str(answering.value) == "The architect is already answering."
    assert str(busy.value) == "Busy already."


async def test_a_crash_is_reported_once_before_the_lane_frees(
    caplog: pytest.LogCaptureFixture,
) -> None:
    turns, posted = slot()

    async def crash() -> None:
        raise ValueError("boom")

    await turns.start(crash())
    await ended(turns.turn)
    await turns.close()

    message = "The planner lane stopped on an unexpected error: boom"
    assert posted == [BUSY, ErrorOccurred(code=INTERNAL, message=message), IDLE]
    assert logged_with_traceback(caplog.records, message)


async def test_a_stale_turns_end_leaves_the_lane_busy() -> None:
    turns, posted = slot()
    work = Work()
    work.gate.set()
    await turns.start(work("first"))
    first = turns.turn
    assert first is not None
    await eventually(first.done, poll=0)

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


async def test_close_cancelled_while_the_turn_stops_is_cancelled_once_it_has() -> None:
    turns, posted = slot()
    work = SlowToStop()
    await turns.start(work())
    await work.started.wait()
    closing = asyncio.create_task(turns.close())
    assert turns.turn is not None

    await cancelled_while_stopping(closing, turns.turn, work.gate)

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert turns.turn.done()
    await asyncio.sleep(0)
    assert posted == [BUSY, IDLE]
