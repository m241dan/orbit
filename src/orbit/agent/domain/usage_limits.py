"""Claude's usage limits as they stop a run: telling that an agent hit one, and when the run
starts again."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from orbit.protocol import UsageChanged

_LIMIT_HIT = re.compile(r"\bhit your\b.*\blimit\b|\busage limit reached\b", re.IGNORECASE)

RESUMING_NOTE = "Starting the run again after the usage limit."
"""Said as a run a usage limit stopped starts again by itself."""

WAIT_CANCELLED_NOTE = "The run won't start again by itself."
"""Said when the user stops a run that was waiting to start again."""

FIX_ERROR_NOTE = (
    "A fix task ended in error, so the run stopped: the work it fixes stays in the coder's tree "
    "until the fix runs again (ctrl+r)."
)
"""Said when a fix task's error stops the run."""


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
    when = at.astimezone(UTC).strftime("%a %d %b %H:%M UTC")
    return (
        f"Claude's usage limit is reached, so the run stopped. It starts again by itself at "
        f"{when}; ctrl+r starts it now and ctrl+s cancels that."
    )
