"""Awaiting work that cancelling the awaiter doesn't cancel."""

import asyncio

import pytest

from orbit.agent.external.shielding import shielded


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
