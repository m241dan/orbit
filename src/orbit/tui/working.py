"""The working row: what's running right now under the shown tab."""

import time
from collections.abc import Callable, Sequence

from rich.text import Text
from textual.widgets import Static

from orbit.tui.view.working import WorkingLines


class WorkingRow(Static):
    """Shows the :class:`WorkingLines` of what's running right now under the shown tab, below
    the logs; hidden while nothing shown runs. The :class:`Transcript` ticks its spinners and
    clocks while something runs."""

    DEFAULT_CSS = """
    WorkingRow {
        display: none;
        height: auto;
        color: $text-muted;
    }
    WorkingRow.-running { display: block; }
    """

    _line_count = 0

    def __init__(self) -> None:
        super().__init__()
        self.clock: Callable[[], float] = time.monotonic
        # Read through, so a clock set later still times the lines.
        self.working = WorkingLines(self, lambda: self.clock())

    @property
    def width(self) -> int:
        return self.size.width

    def on_resize(self) -> None:
        self.working.tick()

    def show_lines(self, lines: Sequence[str]) -> None:
        unwrapped = Text("\n", no_wrap=True, overflow="ellipsis")
        # Only a new line count changes the row's height; a spinner turning needs no layout.
        layout, self._line_count = len(lines) != self._line_count, len(lines)
        self.update(unwrapped.join(Text(line) for line in lines), layout=layout)
        self.set_class(bool(lines), "-running")
