"""A schedule's clock a test moves by hand: its sleeps move the time on, up to where the test
lets it go, and wait there until the test lets it go further."""

import asyncio
from datetime import UTC, datetime, timedelta, tzinfo

from orbit.agent.application.schedules import ScheduleClock

WAIT = 5.0
"""How long a test waits for the schedules to reach the time it let them go to, in seconds."""


class FakeScheduleClock:
    """A clock standing at ``now`` in ``zone``, UTC unless given, whose sleeps move it on, each
    as far as it asks, noted in ``sleeps``, but never past the time a test lets it go to, at
    first ``now``: there a sleep waits until :meth:`go_to` lets it go further."""

    def __init__(self, now: datetime, zone: tzinfo = UTC) -> None:
        self.now = now
        self.zone = zone
        self.sleeps: list[timedelta] = []
        self._limit = now
        self._moved = asyncio.Event()  # The test let the time go further.
        self._parked = asyncio.Event()  # A sleep waits at the time the test let it go to.

    @property
    def schedule_clock(self) -> ScheduleClock:
        """The clock as schedules take it."""
        return ScheduleClock(now=lambda: self.now, sleep=self.sleep, zone=self.zone)

    async def sleep(self, duration: timedelta) -> None:
        """Move the time on by ``duration``, waiting wherever the test hasn't let it go past; a
        test moving :attr:`now` meanwhile, as a machine waking from sleep finds it moved, moves
        the end of the sleep with it."""
        self.sleeps.append(duration)
        remaining = duration
        while self.now + remaining > self._limit:
            passed = max(self._limit - self.now, timedelta())
            remaining -= passed
            self.now += passed
            self._parked.set()
            await self._moved.wait()
            self._moved.clear()
        self.now += remaining
        await asyncio.sleep(0)

    async def go_to(self, limit: datetime) -> None:
        """Let the time go on to ``limit``, then wait until a sleep waits there, the schedules
        having fired whatever came due on the way; fails if none does within :data:`WAIT`
        seconds."""
        self._limit = limit
        self._parked.clear()
        self._moved.set()
        await self.parked()

    async def parked(self) -> None:
        """Wait until a sleep waits at the time the test let it go to; fails if none does
        within :data:`WAIT` seconds."""
        await asyncio.wait_for(self._parked.wait(), WAIT)
