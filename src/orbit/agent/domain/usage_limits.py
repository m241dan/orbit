"""Claude's usage limits and API outages as they stop a run: telling that an agent hit one, and
when the run starts again."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from orbit.agent.domain.endings import first_line
from orbit.protocol import PauseReason, UsageChanged

_LIMIT_HIT = re.compile(r"\bhit your\b.*\blimit\b|\busage limit reached\b", re.IGNORECASE)

_API_UNAVAILABLE = re.compile(
    r"API Error: (?:500|502|503|504|529)\b|overloaded_error|API Error \(Connection error\.\)"
    r"|Request timed out",
    re.IGNORECASE,
)

_OUTAGE_DELAYS = tuple(timedelta(minutes=minutes) for minutes in (2, 5, 10, 20, 30))
"""How long a run an outage stopped waits, by the backoff's level; the last for every level
past them."""

RESUMING_NOTE = "Starting the run again after the usage limit."
"""Said as a run a usage limit stopped starts again by itself."""

OUTAGE_RESUMING_NOTE = "Starting the run again now Claude's API may be answering."
"""Said as a run an outage stopped starts again by itself."""

PLANNER_OUTAGE_NOTE = (
    "Claude's API isn't answering, so the planner couldn't reply; send your message again later."
)
"""Said in place of the error when Claude's API not answering fails a planner turn."""

WAIT_CANCELLED_NOTE = "The run won't start again by itself."
"""Said when the user stops a run that was waiting to start again."""

FIX_ERROR_NOTE = (
    "A fix task ended in error, so the run stopped: the work it fixes stays in the coder's tree "
    "until the fix runs again (ctrl+r)."
)
"""Said when a fix task's error stops the run."""


@dataclass(frozen=True)
class Paused:
    """A run a usage limit or an outage stopped, waiting to start again by itself ``until``
    then, stopped for ``reason``: ``usage_limit`` or ``api_unavailable``."""

    until: datetime
    reason: PauseReason


@dataclass(frozen=True)
class LimitWaits:
    """How long a run a usage limit stopped waits: ``grace`` past the reset time, when there's
    one, else ``retry``."""

    retry: timedelta = timedelta(minutes=15)
    grace: timedelta = timedelta(minutes=1)


def limit_hit(message: str) -> bool:
    """Whether an agent's error ``message`` says it hit a usage limit, as Claude Code says:
    ``You've hit your session limit``, or ``Claude AI usage limit reached``."""
    return _LIMIT_HIT.search(message) is not None


def resume_time(usage: UsageChanged | None, now: datetime, waits: LimitWaits) -> datetime:
    """When a run a usage limit stopped ``now`` starts again: the ``grace`` after the last of
    the limits ``usage`` shows used up resets, if that's still ahead, else after the
    ``retry``."""
    last = _last_reset(usage)
    if last is None or last <= now:
        return now + waits.retry
    return last + waits.grace


def _last_reset(usage: UsageChanged | None) -> datetime | None:
    """When the last of the limits ``usage`` shows used up resets; none if none says."""
    resets = [
        limit.resets_at
        for limit in (usage.limits if usage else ())
        if limit.percent >= 100 and limit.resets_at is not None
    ]
    return max(resets, default=None)


def paused_note(at: datetime) -> str:
    """What's said when a usage limit stops the run, which starts again by itself ``at``."""
    return (
        f"Claude's usage limit is reached, so the run stopped. It starts again by itself at "
        f"{_when(at)}; ctrl+r starts it now and ctrl+s cancels that."
    )


@dataclass(frozen=True)
class Outage:
    """Claude's API not answering as it stopped a run, at ``error``, with the ``level`` of the
    backoff: how many runs in a row it stopped before this one since a task's agent last
    finished its run without it."""

    error: str
    level: int = 0


def api_unavailable(message: str) -> bool:
    """Whether an agent's error ``message`` says Claude's API isn't answering, as Claude Code
    says: a server error (``API Error: 500``, ``502``, ``503``, ``504`` or ``529``), an
    ``overloaded_error``, ``API Error (Connection error.)`` or ``Request timed out``."""
    return _API_UNAVAILABLE.search(message) is not None


def outage_after(last: Outage | None, error: str) -> Outage:
    """Claude's API not answering stopping a run at ``error``: a level up from the ``last``
    run it stopped in a row, if there's one."""
    return Outage(error, 0 if last is None else last.level + 1)


def outage_delay(level: int) -> timedelta:
    """How long a run an outage stopped waits to start again at the backoff's ``level``: 2
    minutes at first, then 5, 10, 20, and 30 from then on."""
    return _OUTAGE_DELAYS[min(level, len(_OUTAGE_DELAYS) - 1)]


def outage_paused_note(at: datetime, error: str) -> str:
    """What's said when Claude's API not answering, with ``error``, stops the run, which starts
    again by itself ``at``."""
    return (
        f"Claude's API isn't answering ({first_line(error)}), so the run stopped. It starts again "
        f"by itself at {_when(at)}; ctrl+r starts it now and ctrl+s cancels that."
    )


def _when(at: datetime) -> str:
    """The time ``at`` as the pause notes give it, in UTC."""
    return at.astimezone(UTC).strftime("%a %d %b %H:%M UTC")
