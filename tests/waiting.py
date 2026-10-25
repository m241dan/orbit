"""Waiting in a test for something to come true, for a while at most, so that a regression fails
the test rather than hanging it."""

import asyncio
from collections.abc import Callable


async def eventually(
    condition: Callable[[], object], *, timeout: float = 5, poll: float = 0.01
) -> None:
    """Return once ``condition`` holds, looking again every ``poll`` seconds, or on every turn of
    the event loop for 0; raises :class:`TimeoutError` if it doesn't within ``timeout`` seconds.
    Looking needs no task of its own, so it returns without yielding if it already holds."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(poll)
