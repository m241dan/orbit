"""Reserving the ``seq`` handed out in the store's mark itself, once for every workspace that
shares the store, and whether any is served or not."""

import asyncio
from contextlib import suppress

from orbit.agent.application.failures import outcome
from orbit.agent.application.ports import SeqMark, StoreFailed


class MarkReservations:
    """The ``seq`` reserved in ``mark``, in the background, the highest asked for at a time; one
    that fails is reserved again with the next asked for, or as this closes. An unexpected error
    reserving is raised as this closes."""

    def __init__(self, mark: SeqMark) -> None:
        self._mark = mark
        self._asked = 0
        self._reserved = 0
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._failure: BaseException | None = None  # The first unexpected error reserving.

    def reserve(self, seq: int) -> None:
        """Have every ``seq`` up to ``seq`` reserved in the mark, in the background; once
        closed, nothing more is."""
        if self._closed:
            return
        self._asked = max(self._asked, seq)
        if self._task is None or self._task.done():
            self._collect()
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        """Reserve the highest ``seq`` asked for, until all are or one fails."""
        while self._asked > self._reserved:
            seq = self._asked
            # The workspaces' logs, over the same store, report its failures.
            if isinstance(await outcome(self._mark.reserve(seq)), StoreFailed):
                return
            self._reserved = seq

    def _collect(self) -> None:
        """Keep the unexpected error the finished reservation raised, if it's the first."""
        if self._task is not None:
            failure = self._task.exception()
            self._failure = self._failure or failure

    async def close(self) -> None:
        """Reserve those still to be, trying again any that failed, and stop reserving; raises
        the first unexpected error reserving, if there was one."""
        self._closed = True
        if self._task is not None:
            with suppress(Exception):  # Collected, and raised once the rest are reserved.
                await self._task
            self._collect()
        await self._run()
        if self._failure is not None:
            raise self._failure
