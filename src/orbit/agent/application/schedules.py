"""Firing an open workspace's ``schedule`` triggers: once as it opens for the times they came due
while it was closed, then at each time one comes due, with the time each last fired kept per
repository beside the review count.

Each open workspace runs its own schedules, in the background, from the triggers it opened
with, so a changed ``cron`` takes effect as the workspace next opens; closing it stops them.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

from orbit.agent.application.failures import ReportedOnce, outcome
from orbit.agent.application.lanes import Background
from orbit.agent.application.ports import Publish, SessionStore, StoreFailed
from orbit.agent.application.storage_halt import Halting, written
from orbit.agent.domain.schedules import due_triggers, fire_due, open_schedules, wait_for_due
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.protocol import INTERNAL

FireScheduled = Callable[[tuple[str, ...]], Awaitable[None]]
"""What fires the ``schedule`` triggers come due, by name, in order."""


def _utc_now() -> datetime:
    """The time now."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class ScheduleClock:
    """How schedules tell the time: ``now`` tells it, ``sleep`` waits a number of seconds, and
    ``zone`` is the time zone the triggers' cron expressions are read in, the machine's own as
    orbit runs."""

    now: Callable[[], datetime] = _utc_now
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    zone: tzinfo = UTC


class FiredTimes:
    """The time each ``schedule`` trigger last fired, by name, kept in ``store`` (if there is
    one); a failing read is reported with ``publish``, and a failing save halts the session
    through ``halt``, the times last asked to be saved saved when the halt is retried. Nothing
    is saved while it's halted."""

    write = FailedWrite.FIRED_TIMES

    def __init__(self, store: SessionStore | None, halt: Halting, publish: Publish) -> None:
        self._store = store
        self._halt = halt
        self._reads = ReportedOnce(publish, INTERNAL)
        self._saved: Mapping[str, datetime] | None = None  # The store's, as last read or saved.
        self._wanted: Mapping[str, datetime] | None = None  # The times last asked to be saved.
        self._saving = asyncio.Lock()  # Saves one at a time, so the newest is written last.

    async def load(self) -> Mapping[str, datetime]:
        """The times kept; none without a store, or when they can't be read, the first such
        failure since a read worked reported."""
        if self._store is None:
            return {}
        self._saved = await self._reads.value(self._store.fired_times())
        return self._saved or {}

    async def save(self, fired: Mapping[str, datetime]) -> None:
        """Keep ``fired`` as the times, replacing those kept, if they changed."""
        if self._store is None:
            return
        store = self._store
        self._wanted = fired
        async with self._saving:
            await written(self._halt, self.write, lambda: self._write(store))

    async def retry(self) -> StoreFailed | None:
        """Keep the times last asked to be saved, if they changed, though the session is halted,
        as resuming does; returns the error when the save fails."""
        if self._store is None:
            return None
        async with self._saving:
            return await self._write(self._store)

    async def _write(self, store: SessionStore) -> StoreFailed | None:
        """Keep the times last asked to be saved in ``store``, if any and they changed; returns
        the error when it fails."""
        wanted = self._wanted
        if wanted is None or wanted == self._saved:
            return None
        error = await outcome(store.save_fired_times(wanted))
        if isinstance(error, StoreFailed):
            return error
        self._saved = wanted
        return None


@dataclass(frozen=True)
class ScheduleParts:
    """What schedules act through: the ``times`` each trigger last fired, kept, the ``clock``
    they tell the time by, ``fire``, which fires the triggers come due, and ``publish``, which
    reports a failure that escapes them."""

    times: FiredTimes
    clock: ScheduleClock
    fire: FireScheduled
    publish: Publish


class Schedules:
    """Fires one open workspace's ``schedule`` triggers through the ``parts``' ``fire``, in the
    background: as it opens, once each that came due since it last fired, as the times kept
    say, then each as it comes due, the time each fired kept after it does. A trigger first seen
    fires at its next time, and one come due several times unseen, as while the machine slept,
    fires once."""

    def __init__(self, parts: ScheduleParts) -> None:
        self._parts = parts
        self._background = Background("schedules", parts.publish)
        self._fired: Mapping[str, datetime] = {}  # The time each trigger last fired, by name.

    @property
    def times(self) -> FiredTimes:
        """The times each trigger last fired, kept, which resuming a halted session saves."""
        return self._parts.times

    @property
    def running(self) -> bool:
        """Whether the schedules run."""
        return self._background.running

    def start(self, crons: Mapping[str, str]) -> None:
        """Start firing the triggers of ``crons``, their cron expressions by name, as the
        workspace opens, unless there are none or they were started already."""
        if crons:
            self._background.start(self._run(dict(crons)))

    async def close(self) -> None:
        """Stop firing the triggers, for good, waiting for a firing under way to end."""
        await self._background.close()

    async def _run(self, crons: Mapping[str, str]) -> None:
        """Fire the triggers of ``crons`` come due since they last fired, then each as it comes
        due, until stopped."""
        await self._open(crons)
        while True:
            await self._wake(crons)

    async def _open(self, crons: Mapping[str, str]) -> None:
        """Fire, once each, the triggers of ``crons`` that came due since the times kept, and
        keep the time each fired or was first seen."""
        clock = self._parts.clock
        kept = await self._parts.times.load()
        opened = open_schedules(crons, kept, clock.now(), clock.zone)
        await self._fire(opened.due, opened.fired)

    async def _wake(self, crons: Mapping[str, str]) -> None:
        """Wait until the first of ``crons`` comes due, or for as long as a wait lasts, then fire
        those come due since they last fired."""
        clock = self._parts.clock
        wait = wait_for_due(crons, self._fired, clock.now(), clock.zone)
        await clock.sleep(wait.total_seconds())
        now = clock.now()  # The time after the wait, which may have ended late.
        due = due_triggers(crons, self._fired, now, clock.zone)
        if due:
            await self._fire(due, fire_due(self._fired, due, now))

    async def _fire(self, due: tuple[str, ...], fired: Mapping[str, datetime]) -> None:
        """Fire the triggers ``due``, if any, then keep ``fired`` as the time each last fired,
        after the firing, so one cut short fires again."""
        self._fired = fired
        if due:
            await self._parts.fire(due)
        await self._parts.times.save(fired)
