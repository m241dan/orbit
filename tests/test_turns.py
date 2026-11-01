"""One turn at a time: a second turn refused while one runs, the slot shown busy while it does,
and a turn's crash passed on rather than lost."""

import asyncio
import inspect
from collections.abc import Coroutine
from contextlib import suppress
from contextvars import ContextVar
from typing import Any

import pytest

from orbit.execution.turns import SlotBusy, TurnSlot
from tests.gated_work import GatedWork
from tests.stopping import SlowToStop, cancelled_while_stopping
from tests.waiting import eventually


class Heard:
    """A slot's callbacks, noting in order what it tells them: ``"busy"``, ``"idle"``,
    ``"freed"``, or the exception a turn crashed on."""

    def __init__(self) -> None:
        self.calls: list[str | BaseException] = []

    def busy(self, busy: bool) -> None:
        self.calls.append("busy" if busy else "idle")

    def crash(self, exc: BaseException) -> None:
        self.calls.append(exc)

    def freed(self) -> None:
        self.calls.append("freed")


def slot() -> tuple[TurnSlot, Heard]:
    heard = Heard()
    turns = TurnSlot("Busy already.", heard.busy, heard.crash)
    turns.freed = heard.freed
    return turns, heard


async def ended(turn: asyncio.Task[object] | None) -> None:
    """Wait until ``turn`` is done and its done-callbacks have run."""
    assert turn is not None
    with suppress(BaseException):
        await turn
    await asyncio.sleep(0)


async def test_begin_runs_the_work_as_the_turn_and_returns_its_task() -> None:
    turns, _ = slot()
    work = GatedWork()

    turn = turns.begin(work("first"))
    await work.started.wait()

    assert turn is turns.turn and turns.running and work.runs == ["first"]
    await turns.close()


async def test_a_turn_is_busy_until_it_ends_then_frees_the_slot() -> None:
    turns, heard = slot()
    work = GatedWork()

    await turns.start(work("first"))
    await work.started.wait()
    assert turns.running and heard.calls == ["busy"]

    work.gate.set()
    await ended(turns.turn)

    assert not turns.running and heard.calls == ["busy", "idle", "freed"]


async def test_on_busy_is_called_once_the_turn_runs_and_once_it_has_ended() -> None:
    seen: list[bool] = []
    turns = TurnSlot("Busy already.", lambda _: seen.append(turns.running), lambda _: None)
    turns.freed = lambda: seen.append(turns.running)
    work = GatedWork()

    await turns.start(work("first"))
    work.gate.set()
    await ended(turns.turn)

    assert seen == [True, False, False]


LABEL: ContextVar[str] = ContextVar("label", default="unlabelled")


def labelled(work: Coroutine[Any, Any, object]) -> asyncio.Task[object]:
    """``work`` as a task that sees ``LABEL`` set to ``"labelled"``."""
    token = LABEL.set("labelled")
    try:
        return asyncio.create_task(work)
    finally:
        LABEL.reset(token)


async def test_the_turn_runs_in_the_context_start_task_starts_it_in() -> None:
    heard = Heard()
    turns = TurnSlot("Busy already.", heard.busy, heard.crash, labelled)
    seen: list[str] = []

    async def note_label() -> None:
        seen.append(LABEL.get())

    await ended(await turns.start(note_label()))

    assert seen == ["labelled"] and LABEL.get() == "unlabelled"


async def test_a_turn_while_one_runs_is_refused_with_the_slots_refusal_and_closed() -> None:
    turns, heard = slot()
    work = GatedWork()
    await turns.start(work("first"))
    await work.started.wait()
    second = work("second")

    with pytest.raises(SlotBusy) as refused:
        await turns.start(second)

    assert (refused.value.refusal, str(refused.value)) == ("Busy already.", "Busy already.")
    assert heard.calls == ["busy"] and work.runs == ["first"]
    assert inspect.getcoroutinestate(second) == inspect.CORO_CLOSED
    await turns.close()


async def test_a_turn_with_a_refusal_of_its_own_refuses_the_next_with_it() -> None:
    turns, _ = slot()
    work = GatedWork()
    await turns.start(work("first"), "The architect is already answering.")

    with pytest.raises(SlotBusy) as answering:
        turns.begin(work("second"))
    await turns.close()
    turns.begin(work("third"))
    with pytest.raises(SlotBusy) as busy:
        turns.begin(work("fourth"))
    await turns.close()

    assert answering.value.refusal == "The architect is already answering."
    assert busy.value.refusal == "Busy already."


async def test_a_crash_goes_once_to_on_crash_before_the_slot_frees() -> None:
    turns, heard = slot()
    boom = ValueError("boom")

    async def crash() -> None:
        raise boom

    await turns.start(crash())
    await ended(turns.turn)
    await turns.close()

    assert heard.calls == ["busy", boom, "idle", "freed"]


async def test_a_stale_turns_end_stays_quiet_and_leaves_the_slot_busy() -> None:
    turns, heard = slot()
    work = GatedWork()
    work.gate.set()
    await turns.start(work("first"))
    first = turns.turn
    assert first is not None
    await eventually(first.done, poll=0)

    await turns.start(work("second"))  # Before the first's callback has run.
    await ended(turns.turn)

    assert heard.calls == ["busy", "busy", "idle", "freed"]


async def test_a_turn_cancelled_before_it_starts_still_frees_the_slot() -> None:
    turns, heard = slot()
    work = GatedWork()

    turns.begin(work("first"))
    assert turns.cancel()
    await ended(turns.turn)

    assert work.runs == [] and not turns.running
    assert heard.calls == ["busy", "idle", "freed"]


async def test_cancel_stops_the_turn_and_says_whether_it_ran() -> None:
    turns, heard = slot()
    work = GatedWork()
    assert not turns.cancel()
    await turns.start(work("first"))
    await work.started.wait()

    assert turns.cancel()
    await ended(turns.turn)

    assert not turns.cancel() and heard.calls == ["busy", "idle", "freed"]


async def test_close_cancels_the_turn_and_waits_for_it() -> None:
    turns, heard = slot()
    work = GatedWork()
    await turns.start(work("first"))
    await work.started.wait()

    await turns.close()

    assert turns.turn is not None and turns.turn.cancelled()
    await asyncio.sleep(0)
    assert heard.calls == ["busy", "idle", "freed"]


async def test_close_without_a_running_turn_does_nothing() -> None:
    turns, heard = slot()

    await turns.close()

    assert turns.turn is None and not turns.running and heard.calls == []


async def test_close_cancelled_while_the_turn_stops_is_cancelled_once_it_has() -> None:
    turns, heard = slot()
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
    assert heard.calls == ["busy", "idle", "freed"]
