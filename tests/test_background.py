"""Running work in the background: one piece of work at a time, a failure that escapes it passed
on rather than raised, and nothing started once closed."""

import asyncio
import inspect
from collections.abc import Coroutine
from contextvars import ContextVar
from typing import Any

import pytest

from orbit.execution.background import Background
from tests.gated_work import GatedWork
from tests.stopping import SlowToStop
from tests.waiting import eventually


class Crashes:
    """A crash callback that records the failures passed to it."""

    def __init__(self) -> None:
        self.seen: list[Exception] = []
        self.passed = asyncio.Event()

    async def __call__(self, exc: Exception) -> None:
        self.seen.append(exc)
        self.passed.set()


def background() -> tuple[Background, Crashes]:
    crashes = Crashes()
    return Background(crashes), crashes


async def test_start_runs_the_work_in_the_background() -> None:
    lane, crashes = background()
    work = GatedWork()

    assert lane.start(work("first"))
    await work.started.wait()

    assert lane.running and work.runs == ["first"]
    work.gate.set()
    await lane.close()
    assert not lane.running and crashes.seen == []


LABEL: ContextVar[str] = ContextVar("label", default="unlabelled")


def labelled(work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    """``work`` as a task that sees ``LABEL`` set to ``"labelled"``."""
    token = LABEL.set("labelled")
    try:
        return asyncio.create_task(work)
    finally:
        LABEL.reset(token)


async def test_the_work_runs_in_the_context_start_task_starts_it_in() -> None:
    lane = Background(Crashes(), labelled)
    seen: list[str] = []

    async def note_label() -> None:
        seen.append(LABEL.get())

    lane.start(note_label())
    await eventually(lambda: not lane.running, poll=0)
    await lane.close()

    assert seen == ["labelled"] and LABEL.get() == "unlabelled"


async def test_ended_is_called_once_the_work_has_stopped() -> None:
    lane, _ = background()
    work = GatedWork()
    seen: list[bool] = []
    lane.ended = lambda: seen.append(lane.running)
    lane.start(work("first"))
    await work.started.wait()
    assert seen == []

    work.gate.set()
    await lane.close()
    await asyncio.sleep(0)

    assert seen == [False]


async def test_ended_is_called_once_for_each_piece_of_work() -> None:
    lane, _ = background()
    work = GatedWork()
    work.gate.set()
    ends: list[str] = []
    lane.ended = lambda: ends.append("ended")

    lane.start(work("first"))
    await eventually(lambda: len(ends) == 1, poll=0)
    lane.start(work("second"))
    await eventually(lambda: len(ends) == 2, poll=0)
    await lane.close()
    await asyncio.sleep(0)

    assert work.runs == ["first", "second"] and ends == ["ended", "ended"]


async def test_start_while_running_drops_and_closes_the_new_work() -> None:
    lane, _ = background()
    work = GatedWork()
    assert lane.start(work("first"))
    await work.started.wait()

    second = work("second")
    assert not lane.start(second)
    work.gate.set()
    await lane.close()

    assert work.runs == ["first"]
    assert inspect.getcoroutinestate(second) == inspect.CORO_CLOSED


async def test_cancel_stops_the_work() -> None:
    lane, crashes = background()
    work = GatedWork()
    lane.start(work("first"))
    await work.started.wait()

    lane.cancel()
    await eventually(lambda: not lane.running, poll=0)

    assert work.runs == ["first"] and crashes.seen == []
    await lane.close()


async def test_work_cancelled_before_it_starts_is_closed_and_never_runs() -> None:
    lane, _ = background()
    work = GatedWork()
    coroutine = work("first")

    lane.start(coroutine)
    await lane.close()

    assert work.runs == [] and not lane.running
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED


async def test_cancel_when_never_started_does_nothing() -> None:
    lane, crashes = background()

    lane.cancel()

    assert not lane.running and crashes.seen == []


async def test_close_when_never_started_does_nothing() -> None:
    lane, crashes = background()

    await lane.close()

    assert not lane.running and crashes.seen == []


async def test_stop_stops_the_work_and_lets_work_start_again() -> None:
    lane, _ = background()
    work = GatedWork()
    lane.start(work("first"))
    await work.started.wait()

    await lane.stop()
    assert not lane.running
    work.gate.set()
    assert lane.start(work("second"))
    await eventually(lambda: not lane.running, poll=0)
    await lane.close()

    assert work.runs == ["first", "second"]


async def test_start_after_close_closes_the_work_and_starts_no_task() -> None:
    lane, crashes = background()
    work = GatedWork()
    await lane.close()

    coroutine = work("late")
    assert not lane.start(coroutine)
    await asyncio.sleep(0)

    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    assert work.runs == [] and not lane.running and crashes.seen == []
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_close_cancelled_while_the_work_stops_is_cancelled_once_it_has() -> None:
    lane, _ = background()
    work = SlowToStop()
    lane.start(work())
    await work.started.wait()
    closing = asyncio.create_task(lane.close())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done() and lane.running

    work.gate.set()

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not lane.running


async def test_a_crash_is_passed_once_to_on_crash_and_never_raised() -> None:
    lane, crashes = background()
    boom = ValueError("boom")

    async def crash() -> None:
        raise boom

    lane.start(crash())
    await asyncio.wait_for(crashes.passed.wait(), 1)
    await lane.close()
    await lane.close()

    assert crashes.seen == [boom] and not lane.running
