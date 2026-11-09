"""A sleep a test hands code in place of the event loop's: it ends at once, noting how long it
was asked to wait."""

import asyncio
from datetime import timedelta


class Naps:
    """Sleeps that each end on the event loop's next turn, every length of time asked for noted
    in ``slept``, in order."""

    def __init__(self) -> None:
        self.slept: list[timedelta] = []

    async def sleep(self, duration: timedelta) -> None:
        """Note ``duration``, then let the loop turn once."""
        self.slept.append(duration)
        await asyncio.sleep(0)
