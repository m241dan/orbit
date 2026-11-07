"""Sleeping: the event loop's sleep waits out a length of time given as a timedelta."""

import asyncio
from datetime import timedelta

from orbit.execution.sleeping import sleep_for


async def test_sleeping_for_a_duration_waits_at_least_that_long() -> None:
    loop = asyncio.get_running_loop()
    start = loop.time()
    await sleep_for(timedelta(milliseconds=20))
    assert loop.time() - start >= 0.02
