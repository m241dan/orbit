"""Checking on processes the code under test started."""

import asyncio

from orbit.agent.external.processes import process_alive


async def gone(pid: int) -> bool:
    """Whether process ``pid`` is gone within a few seconds; an orphan the code killed is only
    reaped once init gets to it."""
    for _ in range(100):
        if not process_alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False
