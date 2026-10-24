"""Awaiting work that cancelling the awaiter doesn't cancel, and that is always waited out."""

import asyncio
import contextlib


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
