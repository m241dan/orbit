"""The working row: what's running right now under the shown tab."""

import time
from collections.abc import Callable, Mapping

from rich.text import Text
from textual.widgets import Static

from orbit.protocol import Event
from orbit.tui.view import Working, WorkingView, track_working, working_lines


class WorkingRow(Static):
    """What's running right now under the shown tab, one line per agent, below the
    logs; hidden while nothing shown runs. The :class:`Transcript` ticks its spinners and
    clocks while something runs."""

    DEFAULT_CSS = """
    WorkingRow {
        display: none;
        height: auto;
        color: $text-muted;
    }
    WorkingRow.-running { display: block; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.clock: Callable[[], float] = time.monotonic
        self.working: Working = ()
        self.lines: tuple[str, ...] = ()
        self._roles: Mapping[str, str] = {}
        self._shown: str | None = None

    @property
    def running(self) -> bool:
        """Whether any agent runs."""
        return bool(self.working)

    def track(self, event: Event, roles: Mapping[str, str]) -> None:
        """Follow who runs after ``event``, with ``roles`` placing agents in tabs."""
        self.working = track_working(self.working, event, self.clock())
        self._roles = roles
        self.tick()

    def show_tab(self, key: str | None) -> None:
        """Show what runs under the tab keyed ``key``; None is All."""
        self._shown = key
        self.tick()

    def on_resize(self) -> None:
        self.tick()

    def tick(self) -> None:
        """Redraw the lines, if they changed."""
        view = WorkingView(self.working, self._roles, self._shown)
        lines = working_lines(view, self.clock(), self.size.width)
        if lines == self.lines:
            return
        self.lines = lines
        unwrapped = Text("\n", no_wrap=True, overflow="ellipsis")
        self.update(unwrapped.join(Text(line) for line in lines))
        self.set_class(bool(lines), "-running")
