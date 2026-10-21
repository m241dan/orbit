"""Checking on processes the code under test started."""

import asyncio
from pathlib import Path

from orbit.agent.external.processes import process_alive


def _zombie(pid: int) -> bool:
    """Whether process ``pid`` has exited and only awaits reaping; an unreadable state counts
    as not."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[0] == "Z"


async def gone(pid: int) -> bool:
    """Whether process ``pid`` has exited within a few seconds. An orphan the code killed is
    only reaped once its new parent gets to it, which can take longer under load, so a zombie
    counts as gone."""
    for _ in range(100):
        if not process_alive(pid) or _zombie(pid):
            return True
        await asyncio.sleep(0.05)
    return False
