"""Cancelling and shielding: telling a task being cancelled, stopping a task and waiting for it,
and awaiting work that cancelling the awaiter doesn't cancel, and that is always waited out."""

import asyncio
import contextlib
from collections.abc import Coroutine
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
