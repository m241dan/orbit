"""The usage monitor: fetches the subscription's usage limits in the background on a schedule,
and again when an agent's turn ends if a refresh is due, publishing each outcome."""

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime

from orbit.agent.application.lanes import Background
from orbit.agent.application.ports import Publish, UsageSource
from orbit.agent.domain.usage import (
    UsageFetch,
    UsageReport,
    UsageUnavailable,
    next_delay,
    refresh_due,
    usage_event,
)
from orbit.protocol import UsageLimit

Clock = Callable[[], float]
"""Seconds on a clock that only goes forward."""

Now = Callable[[], datetime]
"""The time now."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class UsageMonitor:
    """Fetches usage from ``source`` until closed, publishing every outcome, the last limits
    kept, with ``publish``. ``clock`` times the fetches and ``now`` dates retries."""

    def __init__(
        self,
        source: UsageSource,
        publish: Publish,
        clock: Clock = time.monotonic,
        now: Now = _utc_now,
    ) -> None:
        self._source = source
        self._publish = publish
        self._clock = clock
        self._now = now
        self._limits: tuple[UsageLimit, ...] = ()
        self._fetching = False
        # When the last fetch started, and its outcome.
        self._last: tuple[float, UsageFetch] | None = None
        self._wake = asyncio.Event()
        self._background = Background("usage", publish)

    def start(self) -> None:
        self._background.start(self._run())

    def nudge(self) -> None:
        """Fetch again now, if no fetch is in progress and a refresh is due: a minute after the
        last fetch started, or once a rate limit's retry time has passed."""
        if self._fetching or self._last is None:
            return
        fetched_at, outcome = self._last
        if refresh_due(self._clock() - fetched_at, outcome):
            self._wake.set()

    async def close(self) -> None:
        await self._background.close()

    async def _run(self) -> None:
        while True:
            outcome = await self._fetch()
            await self._publish(usage_event(outcome, self._limits, self._now()))
            if isinstance(outcome, UsageReport):
                self._limits = outcome.limits
            await self._wait(next_delay(outcome))

    async def _fetch(self) -> UsageFetch:
        """The source's outcome, a failure in it being one where usage is unavailable."""
        self._fetching = True
        fetched_at = self._clock()
        self._wake.clear()  # A nudge from before this fetch is answered by it.
        try:
            outcome = await self._source()
        except Exception as exc:  # A bug in the source mustn't stop the monitor.
            outcome = UsageUnavailable(str(exc) or "error")
        finally:
            self._fetching = False
        self._last = (fetched_at, outcome)
        return outcome

    async def _wait(self, delay: float) -> None:
        """Wait ``delay`` seconds, or until nudged."""
        with suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), delay)
