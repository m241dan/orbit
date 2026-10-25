"""Runtimes' usage limits and API outages as they stop a run: telling that an agent hit one of
Claude's, and when the run starts again, each pause belonging to the runtime whose agent hit it."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from orbit.agent.domain.endings import first_line
from orbit.agent.domain.runtimes import CLAUDE, Failure
from orbit.protocol import PLANNER, PauseReason, UsageChanged

_LIMIT_HIT = re.compile(r"\bhit your\b.*\blimit\b|\busage limit reached\b", re.IGNORECASE)

_API_UNAVAILABLE = re.compile(
    r"API Error: (?:500|502|503|504|529)\b|overloaded_error|API Error \(Connection error\.\)"
    r"|Request timed out",
    re.IGNORECASE,
)

_OUTAGE_DELAYS = tuple(timedelta(minutes=minutes) for minutes in (2, 5, 10, 20, 30))
"""How long a run an outage stopped waits, by the backoff's level; the last for every level
past them."""

_RUNTIME_NAMES = {CLAUDE: "Claude"}
"""How the notes name each runtime that has a name of its own; any other goes by its name."""

RESUMING_NOTE = "Starting the run again after the usage limit."
"""Said as a run a usage limit stopped starts again by itself."""


def outage_resuming_note(runtime: str) -> str:
    """Said as a run an outage of ``runtime``'s API stopped starts again by itself."""
    return f"Starting the run again now {_named(runtime)}'s API may be answering."


def outage_note(role: str) -> str:
    """Said in place of the error when Claude's API not answering fails a turn of the
    conversational role ``role``."""
    return (
        f"Claude's API isn't answering, so the {role} couldn't reply; "
        "send your message again later."
    )


PLANNER_OUTAGE_NOTE = outage_note(PLANNER)
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
    """A run a usage limit or an outage of ``runtime`` stopped, waiting to start again by itself
    ``until`` then, stopped for ``reason``: ``usage_limit`` or ``api_unavailable``."""

    until: datetime
    reason: PauseReason
    runtime: str


@dataclass(frozen=True)
class RuntimeStop:
    """A run stopped by an agent's ``failure`` on the runtime ``runtime``, as its launcher told
    it."""

    runtime: str
    failure: Failure


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


def limit_resume_time(
    stop: RuntimeStop, usage: UsageChanged | None, now: datetime, waits: LimitWaits
) -> datetime:
    """When a run the usage limit ``stop`` tells stopped ``now`` starts again: when the failure
    says the limit resets, if that's still ahead, else as :func:`resume_time` says from
    ``usage``, if it's the usage of the stop's runtime, else after the ``retry``."""
    stated = _stated_reset(stop, now)
    if stated is not None:
        return stated
    return resume_time(usage if usage and usage.runtime == stop.runtime else None, now, waits)


def _stated_reset(stop: RuntimeStop, now: datetime) -> datetime | None:
    """When ``stop``'s failure says the limit it hit resets, if it says and that's after
    ``now``; none otherwise."""
    resets = stop.failure.resets_at
    return resets if resets is not None and resets > now else None


def _last_reset(usage: UsageChanged | None) -> datetime | None:
    """When the last of the limits ``usage`` shows used up resets; none if none says."""
    resets = [
        limit.resets_at
        for limit in (usage.limits if usage else ())
        if limit.percent >= 100 and limit.resets_at is not None
    ]
    return max(resets, default=None)


def paused_note(at: datetime, runtime: str) -> str:
    """What's said when ``runtime``'s usage limit stops the run, which starts again by itself
    ``at``."""
    return (
        f"{_named(runtime)}'s usage limit is reached, so the run stopped. It starts again by "
        f"itself at {_when(at)}; ctrl+r starts it now and ctrl+s cancels that."
    )


@dataclass(frozen=True)
class Outage:
    """The API of the runtime ``runtime`` not answering as it stopped a run, at ``error``, with
    the ``level`` of the backoff: how many runs in a row it stopped before this one since an
    agent on that runtime last finished its run without it."""

    runtime: str
    error: str
    level: int = 0


def api_unavailable(message: str) -> bool:
    """Whether an agent's error ``message`` says Claude's API isn't answering, as Claude Code
    says: a server error (``API Error: 500``, ``502``, ``503``, ``504`` or ``529``), an
    ``overloaded_error``, ``API Error (Connection error.)`` or ``Request timed out``."""
    return _API_UNAVAILABLE.search(message) is not None


def claude_failure(message: str) -> Failure | None:
    """The failure an agent's error ``message`` tells, as Claude Code words it: a
    ``usage_limit`` when :func:`limit_hit` says so, an ``api_unavailable`` when
    :func:`api_unavailable` does, with no reset time; none for any other error."""
    if limit_hit(message):
        return Failure("usage_limit")
    if api_unavailable(message):
        return Failure("api_unavailable")
    return None


def outage_after(last: Outage | None, runtime: str, error: str) -> Outage:
    """``runtime``'s API not answering stopping a run at ``error``: a level up from the ``last``
    run it stopped in a row, if there's one and it was on that runtime too."""
    level = 0 if last is None or last.runtime != runtime else last.level + 1
    return Outage(runtime, error, level)


def outage_delay(level: int) -> timedelta:
    """How long a run an outage stopped waits to start again at the backoff's ``level``: 2
    minutes at first, then 5, 10, 20, and 30 from then on."""
    return _OUTAGE_DELAYS[min(level, len(_OUTAGE_DELAYS) - 1)]


def outage_resume_time(stop: RuntimeStop, outage: Outage, now: datetime) -> datetime:
    """When a run the outage ``stop`` tells stopped ``now`` starts again: when the failure says
    the API answers again, if that's still ahead, else after the backoff at ``outage``'s
    level."""
    stated = _stated_reset(stop, now)
    return stated if stated is not None else now + outage_delay(outage.level)


def outage_paused_note(at: datetime, error: str, runtime: str) -> str:
    """What's said when ``runtime``'s API not answering, with ``error``, stops the run, which
    starts again by itself ``at``."""
    return (
        f"{_named(runtime)}'s API isn't answering ({first_line(error)}), so the run stopped. It "
        f"starts again by itself at {_when(at)}; ctrl+r starts it now and ctrl+s cancels that."
    )


def _named(runtime: str) -> str:
    """How the notes name the runtime ``runtime``: ``Claude`` for Claude, else its name."""
    return _RUNTIME_NAMES.get(runtime, runtime)


def _when(at: datetime) -> str:
    """The time ``at`` as the pause notes give it, in UTC."""
    return at.astimezone(UTC).strftime("%a %d %b %H:%M UTC")
