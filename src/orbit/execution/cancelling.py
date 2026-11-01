"""Cancelling and shielding: telling a task being cancelled, stopping a task or several and
waiting for them, cancelling a task to read its outcome after, and awaiting work that cancelling
the awaiter doesn't cancel, and that is always waited out."""

import asyncio
import contextlib
from collections.abc import Coroutine, Iterable
from typing import Any


def cancelling() -> bool:
    """Whether the task running now is being cancelled."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def stop(task: asyncio.Task[Any]) -> None:
    """Cancel ``task`` and wait for it to end; cancelled meanwhile, this is cancelled once it
    has."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        # The task's own cancel ends here; one meant for the caller goes on.
        if cancelling():
            raise


async def stop_all(tasks: Iterable[asyncio.Task[Any]]) -> None:
    """Cancel each of ``tasks`` and wait for all of them to end, raising none of their results,
    errors or cancellations; cancelled meanwhile, this cancels them again and is cancelled once
    they have ended."""
    stopping = list(tasks)
    for task in stopping:
        task.cancel()
    await asyncio.gather(*stopping, return_exceptions=True)


async def cancel_and_wait(task: asyncio.Task[Any]) -> None:
    """Cancel ``task`` and wait until it's done, raising nothing of its own: its result, error
    or cancellation stays on it for the caller to read. Cancelled meanwhile, this is cancelled
    at once, leaving ``task`` to end."""
    task.cancel()
    await asyncio.wait({task})


async def shielded[T](future: asyncio.Future[T]) -> T:
    """``future``'s result, which cancelling this doesn't cancel. When cancelled, this waits
    for ``future`` to finish, however often it's cancelled meanwhile, then raises
    ``CancelledError``, leaving ``future``'s result or exception for the caller to clean up
    after."""
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait([future])
        raise


async def waited_out[T](work: Coroutine[Any, Any, T]) -> T:
    """``work``'s result, run as a task of its own that cancelling this doesn't cancel. When
    cancelled, this waits for ``work`` to finish, however often it's cancelled meanwhile, then
    is cancelled itself."""
    return await shielded(asyncio.ensure_future(work))
