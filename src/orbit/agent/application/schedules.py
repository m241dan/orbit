"""Firing an open workspace's ``schedule`` triggers: once as it opens for the times they came due
while it was closed, then at each time one comes due, with the time each last fired kept per
repository beside the review count.

Each open workspace runs its own schedules, in the background, from the triggers it opened
with, or had as a read-only orbit took the repository over, so a changed ``cron`` takes effect
as the workspace next opens; closing it stops them.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

from orbit.agent.application.failures import ReportedOnce, outcome
from orbit.agent.application.lanes import Background
from orbit.agent.application.ports import Publish, SessionStore, StoreFailed
from orbit.agent.application.storage_halt import Halting, written
from orbit.agent.domain.schedules import (
    Firing,
    ScheduleTimes,
    TimesRead,
    decide_opening_firing,
    decide_wake_firing,
    measure_wait,
)
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.execution.sleeping import Sleep, sleep_for
from orbit.protocol import INTERNAL

FireScheduled = Callable[[tuple[str, ...]], Awaitable[None]]
"""What fires the ``schedule`` triggers come due, by name, in order."""


def _utc_now() -> datetime:
    """The time now."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class ScheduleClock:
    """How schedules tell the time: ``now`` tells it, ``sleep`` waits a length of time, and
    ``zone`` is the time zone the triggers' cron expressions are read in, the machine's own as
    orbit runs."""

    now: Callable[[], datetime] = _utc_now
    sleep: Sleep = sleep_for
    zone: tzinfo = UTC


class FiredTimes:
    """The time each ``schedule`` trigger last fired, by name, kept in ``store`` (if there is
    one); a failing read is reported with ``publish``, and a failing save halts the session
    through ``halt``, the times last asked to be saved saved when the halt is retried. Nothing
    is saved while it's halted, nor before a read of the times kept has worked, so a failed read
    never loses them."""

    write = FailedWrite.FIRED_TIMES

    def __init__(self, store: SessionStore | None, halt: Halting, publish: Publish) -> None:
        self._store = store
        self._halt = halt
        self._reads = ReportedOnce(publish, INTERNAL)
        # The store's, as last read or saved; none until a read works.
        self._saved: Mapping[str, datetime] | None = None
        self._wanted: Mapping[str, datetime] | None = None  # The times last asked to be saved.
        self._saving = asyncio.Lock()  # Saves one at a time, so the newest is written last.

    async def load(self) -> Mapping[str, datetime] | None:
        """The times kept, none of them without a store; ``None`` when they can't be read, the
        first such failure since a read worked reported."""
        if self._store is None:
            return {}
        read = await self._reads.value(self._store.fired_times())
        if read is not None:
            self._saved = read
        return read

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
        """Keep the times last asked to be saved in ``store``, if any, a read of those kept has
        worked, and they changed; returns the error when it fails."""
        wanted = self._wanted
        if wanted is None or self._saved is None or wanted == self._saved:
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
    fires once. When the times kept can't be read, each trigger is first seen and nothing is
    kept until a later wake reads them, which catches up as opening does."""

    def __init__(self, parts: ScheduleParts) -> None:
        self._parts = parts
        self._background = Background("schedules", parts.publish)
        self._times: ScheduleTimes = TimesRead({})  # The time each trigger last fired.

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
        keep the time each fired or was first seen, if the times kept could be read."""
        kept = await self._parts.times.load()
        clock = self._parts.clock
        await self._fire(decide_opening_firing(crons, kept, clock.now(), clock.zone))

    async def _wake(self, crons: Mapping[str, str]) -> None:
        """Wait until the first of ``crons`` comes due, or for as long as a wait lasts, then fire
        those come due since they last fired, reading the times kept first if they're unread."""
        clock = self._parts.clock
        wait = measure_wait(crons, self._times.fired, clock.now(), clock.zone)
        await clock.sleep(wait)
        kept = await self._read_unread()
        now = clock.now()  # The time after the wait, which may have ended late, and the read.
        await self._fire(decide_wake_firing(crons, self._times, kept, now, clock.zone))

    async def _read_unread(self) -> Mapping[str, datetime] | None:
        """The times kept, read afresh while they're unread; none once they're read, or when
        they can't be."""
        if isinstance(self._times, TimesRead):
            return None
        return await self._parts.times.load()

    async def _fire(self, firing: Firing) -> None:
        """Fire the triggers the ``firing`` finds due, if any, then keep its times, once the
        times kept have been read, after the firing, so one cut short fires again."""
        self._times = firing.times
        if firing.due:
            await self._parts.fire(firing.due)
        if isinstance(firing.times, TimesRead):
            await self._parts.times.save(firing.times.fired)
