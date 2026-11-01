"""Running work in the background: one piece of work at a time, a failure that escapes it passed
on rather than lost with the task that ran it, and nothing started once closed."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from orbit.execution.cancelling import stop

type OnCrash = Callable[[Exception], Awaitable[None]]
"""Hears of a failure that escaped the work."""

type StartTask = Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]]
"""Makes the work's task, in the context the caller chooses."""


def _unwatched() -> None:
    """Note an end no one watches."""


class Background:
    """Runs one piece of work at a time in the background. A failure that escapes the work is
    passed once to ``on_crash``, which is awaited, as it happens, and never raised;
    ``start_task`` makes the work's task, so the caller decides the context it starts in.
    :attr:`ended` is called once the work has stopped. Once closed, it starts nothing."""

    def __init__(self, on_crash: OnCrash, start_task: StartTask = asyncio.create_task) -> None:
        self._on_crash = on_crash
        self._start_task = start_task
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.ended: Callable[[], None] = _unwatched

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, work: Coroutine[Any, Any, object]) -> bool:
        """Run ``work`` in the background, unless earlier work still runs or this is closed:
        then drop it. Returns whether ``work`` was started."""
        if self._closed or self.running:
            work.close()
            return False
        self._task = self._start_task(self._guarded(work))
        # Work cancelled before it starts is closed, not left never awaited.
        self._task.add_done_callback(lambda _: work.close())
        self._task.add_done_callback(lambda _: self.ended())
        return True

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def stop(self) -> None:
        """Cancel the work, if any, and wait for it to stop; cancelled meanwhile, this is
        cancelled once it has. Work can start again afterwards."""
        if self._task is not None:
            await stop(self._task)

    async def close(self) -> None:
        """Stop the work, as :meth:`stop` does, for good: nothing starts from now on."""
        self._closed = True
        await self.stop()

    async def _guarded(self, work: Coroutine[Any, Any, object]) -> None:
        try:
            await work
        except Exception as exc:  # A bug; nothing else awaits the work, so say so here.
            await self._on_crash(exc)
