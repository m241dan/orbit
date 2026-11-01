"""The helpers the lanes share: background work and a busy lane's turns, whose crashes are
reported as the lane's and which start with no span current, a turn showing its lane busy and
a second refused as busy, and an agent's run the user's stop cancels, even before it begins."""

import asyncio
from contextlib import suppress

import pytest

from orbit.agent.application.lanes import Background, StoppableRun, TurnSlot
from orbit.agent.domain.refusals import Refused
from orbit.protocol import BUSY as BUSY_CODE
from orbit.protocol import INTERNAL, BusyChanged, ErrorOccurred, Event
from orbit.tracing import NoSpan, Span, current_span, use_span
from tests.agent_fakes import logged_with_traceback
from tests.gated_work import GatedWork


def background() -> tuple[Background, asyncio.Queue[Event]]:
    events: asyncio.Queue[Event] = asyncio.Queue()
    return Background("test", events.put), events


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


async def test_a_crash_is_reported_once_as_an_error_naming_the_lane(
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
    work = GatedWork()

    await turns.start(work("first"))
    await work.started.wait()
    assert posted == [BUSY]

    work.gate.set()
    await ended(turns.turn)

    assert posted == [BUSY, IDLE]


async def test_a_turn_while_one_runs_is_refused_as_busy_with_the_running_turns_refusal() -> None:
    turns, posted = slot()
    work = GatedWork()
    await turns.start(work("first"), "The architect is already answering.")

    with pytest.raises(Refused) as refused:
        turns.begin(work("second"))
    await turns.close()

    assert refused.value.code == BUSY_CODE
    assert refused.value.message == "The architect is already answering."
    await asyncio.sleep(0)
    assert posted == [BUSY, IDLE]


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


async def test_a_run_stopped_before_it_begins_is_cancelled_before_it_runs() -> None:
    agent, work = StoppableRun[None](), GatedWork()
    agent.stop()

    with pytest.raises(asyncio.CancelledError):
        await agent.run_alone(work("first"))

    assert work.runs == []


async def test_a_stop_cancels_the_run_going() -> None:
    agent, work = StoppableRun[None](), GatedWork()
    run = asyncio.create_task(agent.run_alone(work("first")))
    await work.started.wait()

    agent.stop()

    with pytest.raises(asyncio.CancelledError):
        await run


async def test_a_stop_holds_for_the_runs_after_it_until_reset() -> None:
    agent, work = StoppableRun[None](), GatedWork()
    agent.stop()
    with pytest.raises(asyncio.CancelledError):
        await agent.run_alone(work("first"))
    assert agent.stopped

    agent.reset()
    work.gate.set()
    await agent.run_alone(work("second"))

    assert (agent.stopped, work.runs) == (False, ["second"])


async def test_a_run_alone_returns_what_the_run_returned() -> None:
    async def answer() -> int:
        return 42

    assert await StoppableRun[int]().run_alone(answer()) == 42
