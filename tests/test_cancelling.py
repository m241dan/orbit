"""Cancelling and shielding: telling a task being cancelled, stopping a task or several,
cancelling a task to read its outcome, and awaiting work that cancelling the awaiter doesn't
cancel."""

import asyncio
from contextlib import suppress

import pytest

from orbit.execution.cancelling import (
    cancel_and_wait,
    cancelling,
    shielded,
    stop,
    stop_all,
    waited_out,
)
from tests.stopping import SlowToStop, cancelled_while_stopping


async def cancelling_once_cancelled(waiting: asyncio.Event) -> bool:
    """Whether :func:`cancelling` says so in a task cancelled as it waits, once ``waiting``."""
    waiting.set()
    with suppress(asyncio.CancelledError):
        await asyncio.Event().wait()
    return cancelling()


async def test_a_task_being_cancelled_is_cancelling() -> None:
    waiting = asyncio.Event()
    task = asyncio.create_task(cancelling_once_cancelled(waiting))
    await waiting.wait()
    task.cancel()
    assert await task


async def test_a_task_not_being_cancelled_is_not_cancelling() -> None:
    assert not cancelling()


async def test_a_callback_outside_any_task_is_not_cancelling() -> None:
    loop = asyncio.get_running_loop()
    seen: asyncio.Future[bool] = loop.create_future()
    loop.call_soon(lambda: seen.set_result(cancelling()))
    assert not await seen


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


async def failing_once_cancelled() -> None:
    """Wait until cancelled, then fail."""
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise OSError("boom") from None


async def giving_3_once_cancelled() -> int:
    """Wait until cancelled, then give 3."""
    with suppress(asyncio.CancelledError):
        await asyncio.Event().wait()
    return 3


async def test_stop_all_waits_for_every_task_to_end_raising_none_of_their_outcomes() -> None:
    tasks = [
        asyncio.create_task(asyncio.Event().wait()),
        asyncio.create_task(failing_once_cancelled()),
        asyncio.create_task(giving_3_once_cancelled()),
    ]
    await asyncio.sleep(0)

    await asyncio.wait_for(stop_all(tasks), 5)

    assert tasks[0].cancelled()
    assert isinstance(tasks[1].exception(), OSError)
    assert tasks[2].result() == 3


async def test_stop_all_of_no_tasks_returns_at_once() -> None:
    await asyncio.wait_for(stop_all([]), 5)


async def test_stop_all_cancelled_meanwhile_is_cancelled_once_the_tasks_have_ended() -> None:
    work = SlowToStop()
    child = asyncio.create_task(work())
    await work.started.wait()
    stopping = asyncio.create_task(stop_all([child]))

    await cancelled_while_stopping(stopping, child, work.gate)

    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert child.done()


async def test_cancel_and_wait_leaves_the_tasks_error_on_it() -> None:
    child = asyncio.create_task(failing_once_cancelled())
    await asyncio.sleep(0)

    await asyncio.wait_for(cancel_and_wait(child), 5)

    assert isinstance(child.exception(), OSError)


async def test_cancel_and_wait_of_a_finished_task_returns_at_once() -> None:
    child = asyncio.create_task(asyncio.sleep(0, 3))
    await child

    await asyncio.wait_for(cancel_and_wait(child), 5)

    assert child.result() == 3


async def test_cancel_and_wait_cancelled_meanwhile_is_cancelled_without_waiting() -> None:
    work = SlowToStop()
    child = asyncio.create_task(work())
    await work.started.wait()
    waiting = asyncio.create_task(cancel_and_wait(child))
    await asyncio.sleep(0)

    waiting.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not child.done()  # The caller's cancellation goes on as the task still ends.
    work.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await child


async def test_shielded_work_gives_its_result() -> None:
    work: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    work.set_result(3)
    assert await shielded(work) == 3


async def test_cancelled_twice_it_waits_for_the_work_then_raises_cancelled() -> None:
    work: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(shielded(work))
    await asyncio.sleep(0)
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done()  # Still waiting for the work, through both cancellations.
    work.set_result(3)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not work.cancelled() and work.result() == 3


async def test_cancelled_as_the_work_finishes_it_raises_cancelled() -> None:
    work: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(shielded(work))
    await asyncio.sleep(0)
    task.cancel()
    work.set_result(3)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert work.result() == 3


async def test_cancelled_work_that_then_fails_leaves_its_error_and_raises_cancelled() -> None:
    work: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(shielded(work))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    work.set_exception(OSError("boom"))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert isinstance(work.exception(), OSError)


class Gated:
    """Work that, once started, waits for ``gate`` and then gives 3, noting that it finished."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        self.finished = False

    async def __call__(self) -> int:
        self.started.set()
        await self.gate.wait()
        self.finished = True
        return 3


async def test_waited_out_work_gives_its_result() -> None:
    work = Gated()
    work.gate.set()
    assert await waited_out(work()) == 3


async def test_waited_out_cancelled_twice_waits_for_the_work_then_raises_cancelled() -> None:
    work = Gated()
    task = asyncio.create_task(waited_out(work()))
    await work.started.wait()
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done()  # Still waiting for the work, through both cancellations.
    work.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert work.finished
