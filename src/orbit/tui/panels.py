"""The side panels: verification steps and usage."""

import time
from collections.abc import Callable, Sequence
from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from orbit.protocol import StepState, UsageChanged
from orbit.tui.view import NO_STEPS_TEXT, spinner_frame, step_lines, usage_lines
from orbit.tui.widgets import SidePanel, Ticker


class VerificationPanel(SidePanel):
    """The verification steps and how each fared after the current task, as reported.

    A running step spins and shows how long it has been running, so a slow step
    doesn't look stalled.
    """

    def __init__(self, *, id: str) -> None:
        super().__init__("Verification steps", id=id)
        self.steps: tuple[StepState, ...] = ()
        self.title = ""  # What the steps check, when it isn't the current task.
        self.clock: Callable[[], float] = time.monotonic
        # The running step and when it started, by ``clock``.
        self._running_step: int | None = None
        self._since = 0.0
        self._text = NO_STEPS_TEXT  # What the panel shows now.
        self._height: int | None = None  # Its rows when last drawn, if its width was known.
        self._ticker = Ticker(self, self.tick)

    def compose(self) -> ComposeResult:
        yield Static(NO_STEPS_TEXT)

    @property
    def ticking(self) -> bool:
        """Whether the running step's spinner and clock are turning."""
        return self._ticker.ticking

    def show(self, steps: Sequence[StepState], title: str = "") -> None:
        self.steps, self.title = tuple(steps), title
        running = next((i for i, s in enumerate(self.steps) if s.status == "running"), None)
        if running is not None and running != self._running_step:
            self._since = self.clock()
        self._running_step = running
        self.tick()
        self._ticker.keep(running is not None)

    def tick(self) -> None:
        """Redraw the steps, if they or the spinner's frame changed since the last time."""
        now = self.clock()
        lines = step_lines(self.steps, spinner_frame(now), int(now - self._since), self.title)
        text = "\n".join(lines) or NO_STEPS_TEXT
        if text != self._text:
            # Spinner frames keep every line's length, so only a change in the wrapped
            # height needs a layout.
            height = self._wrapped_height(text)
            layout = height is None or height != self._height
            self._text, self._height = text, height
            self.query_one(Static).update(Text(text), layout=layout)

    def _wrapped_height(self, text: str) -> int | None:
        """The rows ``text`` takes wrapped to the panel's width, or None before it has one."""
        width = self.query_one(Static).content_region.width
        if not width:
            return None
        return len(Text(text).wrap(self.app.console, width))


def _local_now() -> datetime:
    return datetime.now().astimezone()


class UsagePanel(SidePanel):
    """How much of each subscription usage limit is used, as reported; hidden until a report
    arrives, so the layout is unchanged when nothing reports usage."""

    DEFAULT_CSS = """
    UsagePanel { height: auto; }
    """

    def __init__(self, *, id: str) -> None:
        super().__init__("Usage", id=id)
        self.display = False
        self.event: UsageChanged | None = None
        self.now: Callable[[], datetime] = _local_now

    def compose(self) -> ComposeResult:
        yield Static()

    def on_mount(self) -> None:
        # Keeps the reset times right as the day changes.
        self.set_interval(60, self.tick)

    def on_resize(self) -> None:
        self.tick()

    def show(self, event: UsageChanged) -> None:
        self.event = event
        self.display = True
        self.tick()

    def tick(self) -> None:
        """Redraw the usage reported, if any, to fit the panel's width."""
        if self.event is None:
            return
        lines = usage_lines(self.event, self.content_region.width, self.now())
        self.query_one(Static).update(Text("\n").join(lines))
