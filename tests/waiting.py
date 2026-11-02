"""Waiting in a test for something to come true, for a while at most, so that a regression fails
the test rather than hanging it."""

import asyncio
import time
from collections.abc import Awaitable, Callable


async def eventually(
    condition: Callable[[], object], *, timeout: float = 5, poll: float = 0.01
) -> None:
    """Return once ``condition`` holds, looking again every ``poll`` seconds, or on every turn of
    the event loop for 0; raises :class:`TimeoutError` if it doesn't within ``timeout`` seconds.
    Looking needs no task of its own, so it returns without yielding if it already holds."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(poll)


async def eventually_awaited(
    condition: Callable[[], Awaitable[object]], *, timeout: float = 5, poll: float = 0.01
) -> None:
    """Return once what ``condition`` gives, awaited, holds, looking again every ``poll``
    seconds; raises :class:`TimeoutError` if it doesn't within ``timeout`` seconds."""
    async with asyncio.timeout(timeout):
        while not await condition():
            await asyncio.sleep(poll)


def eventually_sync(
    condition: Callable[[], object], *, timeout: float = 5, poll: float = 0.01
) -> None:
    """Return once ``condition`` holds, blocking the thread and looking again every ``poll``
    seconds, for a test with no event loop; raises :class:`TimeoutError` if it doesn't within
    ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Still not so after {timeout} s")
        time.sleep(poll)
