"""How a run that a runtime's usage limit or API outage stopped waits to start again."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from orbit.agent.domain.usage_limits import LimitWaits
from orbit.execution.sleeping import Sleep, sleep_for


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class LimitPause:
    """How a run a usage limit or an outage stopped waits: ``now`` tells the time, ``sleep``
    waits out a length of time, and ``waits`` says how long a usage limit's wait lasts."""

    now: Callable[[], datetime] = _utc_now
    sleep: Sleep = sleep_for
    waits: LimitWaits = field(default_factory=LimitWaits)
