"""The side panels: verification steps and usage."""

import time
from collections.abc import Callable, Sequence
from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from orbit.protocol import StepState, UsageChanged
from orbit.tui.view.panels import NO_STEPS_TEXT, VerificationSteps, usage_lines
from orbit.tui.widgets import SidePanel, Ticker


class VerificationPanel(SidePanel):
    """Shows the :class:`VerificationSteps` it holds as ``verification``, timed by ``clock``:
    the verification steps and how each fared after the current task, as reported, a running
    one spinning."""

    def __init__(self, *, id: str) -> None:
        super().__init__("Verification steps", id=id)
        self.clock: Callable[[], float] = time.monotonic
        # Read through, so a clock set later still times the steps.
        self.verification = VerificationSteps(self, lambda: self.clock())
        self._ticker = Ticker(self, self.tick)

    def compose(self) -> ComposeResult:
        yield Static(NO_STEPS_TEXT)

    @property
    def ticking(self) -> bool:
        """Whether the running step's spinner and clock are turning."""
        return self._ticker.ticking

    @property
    def steps(self) -> tuple[StepState, ...]:
        """The steps shown."""
        return self.verification.steps

    @property
    def title(self) -> str:
        """What the steps shown check, when it isn't the current task."""
        return self.verification.title

    def show(self, steps: Sequence[StepState], title: str = "") -> None:
        """Show ``steps`` under ``title``, as the verification takes them."""
        self.verification.show(steps, title)

    def tick(self) -> None:
        """Redraw the steps, as the verification does."""
        self.verification.tick()

    def wrapped_height(self, text: str) -> int | None:
        width = self.query_one(Static).content_region.width
        if not width:
            return None
        return len(Text(text).wrap(self.app.console, width))

    def show_text(self, text: str, layout: bool) -> None:
        self.query_one(Static).update(Text(text), layout=layout)

    def keep_ticking(self, running: bool) -> None:
        self._ticker.keep(running)


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
