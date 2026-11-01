"""Work that is slow to stop, and cancelling what waits for it to stop, for the tests of stopping
a task."""

import asyncio
from typing import Any


class SlowToStop:
    """Work that, once started, runs until cancelled and then waits for ``gate`` to end."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def __call__(self) -> None:
        try:
            self.started.set()
            await asyncio.Event().wait()
        finally:
            await self.gate.wait()


async def cancelled_while_stopping(
    stopping: asyncio.Task[None], child: asyncio.Task[Any], gate: asyncio.Event
) -> None:
    """Cancel ``stopping`` while ``child`` stops, then let the child end through ``gate``."""
    await asyncio.sleep(0)
    stopping.cancel()
    await asyncio.sleep(0)
    assert not stopping.done() and not child.done()
    gate.set()
