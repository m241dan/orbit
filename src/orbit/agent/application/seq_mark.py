"""Reserving the ``seq`` handed out in the store's mark itself, once for every workspace that
shares the store, and whether any is served or not."""

import asyncio

from orbit.agent.application.failures import outcome
from orbit.agent.application.ports import SeqMark, StoreFailed


class MarkReservations:
    """The ``seq`` reserved in ``mark``, in the background, the highest asked for at a time; one
    that fails is reserved again with the next asked for, or as this closes."""

    def __init__(self, mark: SeqMark) -> None:
        self._mark = mark
        self._asked = 0
        self._reserved = 0
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def reserve(self, seq: int) -> None:
        """Have every ``seq`` up to ``seq`` reserved in the mark, in the background; once
        closed, nothing more is."""
        if self._closed:
            return
        self._asked = max(self._asked, seq)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        """Reserve the highest ``seq`` asked for, until all are or one fails."""
        while self._asked > self._reserved:
            seq = self._asked
            # The workspaces' logs, over the same store, report its failures.
            if isinstance(await outcome(self._mark.reserve(seq)), StoreFailed):
                return
            self._reserved = seq

    async def close(self) -> None:
        """Reserve those still to be, trying again any that failed, and stop reserving."""
        self._closed = True
        if self._task is not None:
            await self._task
        await self._run()
