"""What the lanes share: a failure that escapes one is reported to the user, not lost with the
background task that ran it."""

import asyncio
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any

from orbit.agent.application.ports import Publish
from orbit.protocol import ErrorOccurred


def crash_of(task: "asyncio.Task[object]") -> BaseException | None:
    """The exception a done ``task`` ended with; none if it finished or was cancelled."""
    if task.cancelled():
        return None
    return task.exception()


def crash_report(lane: str, exc: BaseException) -> ErrorOccurred:
    """The event telling the user that ``lane`` stopped on ``exc``, which nothing expected."""
    return ErrorOccurred(message=f"The {lane} lane stopped on an unexpected error: {exc!r}")


class Background:
    """Runs one piece of work at a time in the background for lane ``name``. A failure that
    escapes the work is reported once with ``publish``, as it happens, and never raised."""

    def __init__(self, name: str, publish: Publish) -> None:
        self._name = name
        self._publish = publish
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, work: Coroutine[Any, Any, object]) -> None:
        """Run ``work`` in the background, unless earlier work still runs: then drop it."""
        if self.running:
            work.close()
            return
        self._task = asyncio.create_task(self._guarded(work))
        # Work cancelled before it starts is closed, not left never awaited.
        self._task.add_done_callback(lambda _: work.close())

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def close(self) -> None:
        """Cancel the work, if any, and wait for it to stop."""
        self.cancel()
        if self._task is not None:
            with suppress(asyncio.CancelledError):
                await self._task

    async def _guarded(self, work: Coroutine[Any, Any, object]) -> None:
        try:
            await work
        except Exception as exc:  # A bug; nothing else awaits the work, so say so here.
            await self._publish(crash_report(self._name, exc))
