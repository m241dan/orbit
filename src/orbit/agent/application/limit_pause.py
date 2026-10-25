"""How a run that Claude's usage limit or an API outage stopped waits to start again."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from orbit.agent.domain.usage_limits import LimitWaits


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class LimitPause:
    """How a run a usage limit or an outage stopped waits: ``now`` tells the time, ``sleep``
    waits a number of seconds, and ``waits`` says how long a usage limit's wait lasts."""

    now: Callable[[], datetime] = _utc_now
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    waits: LimitWaits = field(default_factory=LimitWaits)
