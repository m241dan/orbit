"""Cancelling and shielding: telling a task being cancelled, stopping a task, and awaiting work
that cancelling the awaiter doesn't cancel."""

import asyncio
from contextlib import suppress

import pytest

from orbit.execution.cancelling import cancelling, shielded, stop, waited_out
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
