"""Sleeping: waiting out a length of time, through a port tests replace with a clock of their
own."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

type Sleep = Callable[[timedelta], Awaitable[None]]
"""What waits out a length of time."""


async def sleep_for(duration: timedelta) -> None:
    """Wait out ``duration``, on the event loop's clock."""
    await asyncio.sleep(duration.total_seconds())
