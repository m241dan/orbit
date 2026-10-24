"""The verification and usage panels."""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

from rich.text import Text

from orbit.protocol import StepState, UsageChanged, UsageLimit
from orbit.tui.view.common import STEP_ICONS, spinner_frame
from orbit.tui.view.rules import duration_text

# The verification panel

NO_STEPS_TEXT = "No verification steps"


def step_lines(steps: Sequence[StepState], frame: str, elapsed: int, title: str = "") -> list[str]:
    """One line per step, under the ``title`` heading if there is one. The running one shows
    the spinner ``frame`` and how long, ``elapsed`` seconds, it has been running, as a turn's
    time reads."""
    return [title] * bool(title) + [
        f"{frame} {s.name} · {duration_text(elapsed)}"
        if s.status == "running"
        else f"{STEP_ICONS[s.status]} {s.name}"
        for s in steps
    ]


class StepsView(Protocol):
    """What shows the verification steps: a text wrapped to the panel's width."""

    def wrapped_height(self, text: str) -> int | None:
        """The rows ``text`` takes wrapped to the panel's width, or None before it has one."""
        ...

    def show_text(self, text: str, layout: bool) -> None:
        """Show ``text`` in place of the text shown, laying the panel out again if ``layout``."""
        ...

    def keep_ticking(self, running: bool) -> None:
        """Redraw as fast as spinners turn while ``running``, or stop."""
        ...


class VerificationSteps:
    """The verification steps and how each fared after the current task, as reported, shown in
    ``view``. A running step spins and shows how long, by ``clock``, it has been running, so a
    slow step doesn't look stalled."""

    def __init__(self, view: StepsView, clock: Callable[[], float]) -> None:
        self._view = view
        self._clock = clock
        self.steps: tuple[StepState, ...] = ()
        self.title = ""  # What the steps check, when it isn't the current task.
        # The running step and when it started, by ``clock``.
        self._running_step: int | None = None
        self._since = 0.0
        self._text = NO_STEPS_TEXT  # What the panel shows now.
        self._height: int | None = None  # Its rows when last drawn, if its width was known.

    def show(self, steps: Sequence[StepState], title: str = "") -> None:
        """Show ``steps`` under ``title``, if there is one, ticking while one runs."""
        self.steps, self.title = tuple(steps), title
        running = next((i for i, s in enumerate(self.steps) if s.status == "running"), None)
        if running is not None and running != self._running_step:
            self._since = self._clock()
        self._running_step = running
        self.tick()
        self._view.keep_ticking(running is not None)

    def tick(self) -> None:
        """Redraw the steps, if they or the spinner's frame changed since the last time."""
        now = self._clock()
        lines = step_lines(self.steps, spinner_frame(now), int(now - self._since), self.title)
        text = "\n".join(lines) or NO_STEPS_TEXT
        if text != self._text:
            # Spinner frames keep every line's length, so only a change in the wrapped
            # height needs a layout.
            height = self._view.wrapped_height(text)
            layout = height is None or height != self._height
            self._text, self._height = text, height
            self._view.show_text(text, layout)


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
