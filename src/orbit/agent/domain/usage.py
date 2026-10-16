"""The rules for fetching and reporting the subscription's usage limits."""

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from orbit.protocol import UsageChanged, UsageLimit

REFRESH_SECONDS = 300
MIN_REFRESH_SECONDS = 60
# The longest a rate limit can hold off the next fetch, an hour, so that a bogus Retry-After
# can't stall the limits for good or overflow the time it's added to.
MAX_RETRY_SECONDS = 3600

# The usage endpoint's windows we show, by key, and the labels they're shown with.
_WINDOWS = (("five_hour", "Session"), ("seven_day", "Week"))


@dataclass(frozen=True)
class UsageReport:
    """A fetch that returned these ``limits``."""

    limits: tuple[UsageLimit, ...]


@dataclass(frozen=True)
class UsageRateLimited:
    """A fetch refused for its rate, to be tried again after ``retry_after`` seconds if given."""

    retry_after: float | None


@dataclass(frozen=True)
class UsageUnavailable:
    """A fetch that couldn't be made, for ``reason``."""

    reason: str


UsageFetch = UsageReport | UsageRateLimited | UsageUnavailable


def parse_usage(payload: object) -> tuple[UsageLimit, ...]:
    """The Session and Week limits in the usage endpoint's ``payload``, skipping windows it
    doesn't measure. Raises ``ValueError`` when the payload is malformed."""
    if not isinstance(payload, dict):
        raise ValueError(f"usage isn't an object: {payload!r}")
    limits = (_parse_window(payload.get(key), label) for key, label in _WINDOWS)
    return tuple(limit for limit in limits if limit is not None)


def _parse_window(window: object, label: str) -> UsageLimit | None:
    if window is None:
        return None
    if not isinstance(window, dict):
        raise ValueError(f"{label} usage isn't an object: {window!r}")
    utilization = window.get("utilization")
    if utilization is None:
        return None
    percent = _parse_percent(utilization, label)
    return UsageLimit(label=label, percent=percent, resets_at=_parse_time(window.get("resets_at")))


def _parse_percent(utilization: object, label: str) -> float:
    """``utilization`` clamped into a percentage, raising ``ValueError`` unless it's a finite
    number."""
    if isinstance(utilization, bool) or not isinstance(utilization, int | float):
        raise ValueError(f"{label} utilization isn't a number: {utilization!r}")
    if not math.isfinite(utilization):
        raise ValueError(f"{label} utilization isn't finite: {utilization!r}")
    return min(max(float(utilization), 0.0), 100.0)


def _parse_time(value: object) -> datetime | None:
    """``value`` as a UTC time unless it says otherwise, or ``None`` when it's missing."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"reset time isn't a string: {value!r}")
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def next_delay(outcome: UsageFetch) -> float:
    """Seconds to wait after ``outcome`` before fetching again: as long as a rate limit asks,
    between the minimum and the maximum, and the refresh period otherwise, including when the
    rate limit asks for no time or a NaN."""
    match outcome:
        case UsageRateLimited(retry_after) if retry_after is not None and retry_after > 0:
            return min(max(retry_after, MIN_REFRESH_SECONDS), MAX_RETRY_SECONDS)
        case _:
            return REFRESH_SECONDS


def refresh_due(seconds_since_last_fetch: float) -> bool:
    """Whether enough time has passed since the last fetch to fetch again."""
    return seconds_since_last_fetch >= MIN_REFRESH_SECONDS


def usage_event(
    outcome: UsageFetch, last_limits: tuple[UsageLimit, ...], now: datetime
) -> UsageChanged:
    """The event reporting ``outcome``, keeping ``last_limits`` when it has none of its own."""
    if isinstance(outcome, UsageReport):
        return UsageChanged(limits=outcome.limits)
    if isinstance(outcome, UsageRateLimited):
        retry_at = now + timedelta(seconds=next_delay(outcome))
        return UsageChanged(limits=last_limits, status="rate_limited", retry_at=retry_at)
    return UsageChanged(limits=last_limits, status="unavailable", detail=outcome.reason)
