"""Running jobs side by side: up to a number at once, each a task of its own under the key it was
picked by, picking again as one ends or on being woken, a failure that escapes one raised once the
others have stopped, and every one stopped and waited for when the run is."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from orbit.execution.cancelling import stop_all
from orbit.execution.turns import crash_of

type Pick[K] = Callable[[], K | None]
"""The key of the next job to run; none when there's none to run now."""

type Job[K] = Callable[[K], Coroutine[Any, Any, object]]
"""Runs the job picked by a key."""


class SideBySide[K]:
    """Runs the jobs ``pick`` picks, up to ``size`` at once, each a task of its own that ``job``
    runs under its key. A task copies the context it's created in: the run's. ``pick`` must not
    pick a key whose job still runs."""

    def __init__(self, size: int, pick: Pick[K], job: Job[K]) -> None:
        self._size = size
        self._pick = pick
        self._job = job
        self._jobs: dict[K, asyncio.Task[object]] = {}  # The jobs running, oldest first.
        self._changed = asyncio.Event()  # A job ended, or the run was woken.

    @property
    def running(self) -> tuple[K, ...]:
        """The keys of the jobs running, oldest first."""
        return tuple(self._jobs)

    def wake(self) -> None:
        """Have the run pick again, to fill the slots free."""
        self._changed.set()

    async def run(self) -> None:
        """Run the jobs picked, filling the slots free as each job ends or the run is woken,
        until none is picked and none runs. A failure that escapes a job is raised once the
        others have stopped; cancelled, this stops every job, waits for them, then is
        cancelled."""
        try:
            while self._fill():
                await self._changed.wait()
                self._changed.clear()
                self._forget_ended()
        finally:
            await stop_all(self._jobs.values())
            self._jobs.clear()

    def _fill(self) -> bool:
        """Start the jobs picked until the slots are full or none is; returns whether any job
        runs."""
        for _ in range(self._size - len(self._jobs)):
            key = self._pick()
            if key is None:
                break
            job = self._jobs[key] = asyncio.create_task(self._job(key))
            job.add_done_callback(lambda _: self._changed.set())
        return bool(self._jobs)

    def _forget_ended(self) -> None:
        """Forget the jobs that ended, raising the first failure that escaped one."""
        crash = next(filter(None, map(crash_of, self._pop_ended())), None)
        if crash is not None:
            raise crash

    def _pop_ended(self) -> list[asyncio.Task[object]]:
        """Forget the jobs that ended; returns them."""
        ended = [key for key, job in self._jobs.items() if job.done()]
        return [self._jobs.pop(key) for key in ended]
