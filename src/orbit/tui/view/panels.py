"""The verification and usage panels."""

from collections.abc import Sequence
from datetime import datetime

from rich.text import Text

from orbit.protocol import StepState, UsageChanged, UsageLimit
from orbit.tui.view.common import STEP_ICONS

# The verification panel

NO_STEPS_TEXT = "No verification steps"


def step_lines(steps: Sequence[StepState], frame: str, elapsed: int, title: str = "") -> list[str]:
    """One line per step, under the ``title`` heading if there is one. The running one shows
    the spinner ``frame`` and the ``elapsed`` seconds it has been running."""
    return [title] * bool(title) + [
        f"{frame} {s.name} · {elapsed}s"
        if s.status == "running"
        else f"{STEP_ICONS[s.status]} {s.name}"
        for s in steps
    ]


# The usage panel

# Bar colours by the least percent they start at, most urgent first.
USAGE_COLOURS = ((90.0, "red"), (75.0, "yellow"), (0.0, "green"))
STALE_STYLE = "dim"


def usage_lines(event: UsageChanged, width: int, now: datetime) -> list[Text]:
    """The usage panel's rows: a header and a bar ``width`` cells wide for each limit, dimmed
    unless the numbers are current, then a line on the status if there's one to say."""
    stale = event.status != "ok"
    lines = [row for limit in event.limits for row in _limit_rows(limit, width, now, stale)]
    status = _usage_status(event, now)
    return lines + [Text(status, style=STALE_STYLE)] * bool(status)


def _limit_rows(limit: UsageLimit, width: int, now: datetime, stale: bool) -> list[Text]:
    header = f"{limit.label}  {limit.percent:.0f}%"
    if limit.resets_at is not None:
        header += f"  resets {reset_label(limit.resets_at, now)}"
    width = max(width, 1)
    filled = round(limit.percent / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    style = STALE_STYLE if stale else _usage_colour(limit.percent)
    return [Text(header, style=STALE_STYLE if stale else ""), Text(bar, style=style)]


def _usage_colour(percent: float) -> str:
    return next(colour for least, colour in USAGE_COLOURS if percent >= least)


def _usage_status(event: UsageChanged, now: datetime) -> str:
    match event.status:
        case "rate_limited":
            return _rate_limited(event.retry_at, now)
        case "unavailable":
            return ": ".join(filter(None, ("usage unavailable", event.detail)))
        case _:
            return "" if event.limits else "no usage limits reported"


def _rate_limited(retry_at: datetime | None, now: datetime) -> str:
    if retry_at is None:
        return "usage: rate limited"
    return f"usage: rate limited, retrying {reset_label(retry_at, now)}"


def reset_label(when: datetime, now: datetime) -> str:
    """``when`` in ``now``'s timezone: the time, like ``3:40pm``, on the same day as ``now``,
    or on another day with the weekday, like ``Mon 9:00am``."""
    local = when.astimezone(now.tzinfo)
    hour = local.hour % 12 or 12
    time = f"{hour}:{local.minute:02d}{'am' if local.hour < 12 else 'pm'}"
    return time if local.date() == now.date() else f"{local:%a} {time}"
