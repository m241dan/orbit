"""The prompt's marks, without the widget showing them: in its title, whom it talks to and
whether that's busy or the orbit read-only, and under it, the settings."""

from typing import Protocol

from orbit.protocol import Settings
from orbit.tui.view.settings import settings_summary
from orbit.tui.view.transcript import prompt_title


class PromptMarksView(Protocol):
    """Where a :class:`PromptMarks` shows."""

    def show_title(self, title: str) -> None:
        """Show ``title`` above the prompt."""

    def show_subtitle(self, subtitle: str) -> None:
        """Show ``subtitle`` under the prompt."""


class PromptMarks:
    """The prompt's marks, shown in ``view``: whether the planner is busy and the orbit
    read-only, which start as neither, and the settings."""

    def __init__(self, view: PromptMarksView) -> None:
        self._view = view
        self.busy = False
        self.read_only = False

    def show_busy(self, busy: bool) -> None:
        """Mark the prompt while the planner is ``busy``."""
        self.busy = busy
        self._view.show_title(prompt_title(busy, self.read_only))

    def show_read_only(self, read_only: bool) -> None:
        """Mark the prompt while the orbit is ``read_only``."""
        self.read_only = read_only
        self._view.show_title(prompt_title(self.busy, read_only))

    def show_settings(self, settings: Settings) -> None:
        """Show ``settings`` under the prompt."""
        self._view.show_subtitle(settings_summary(settings))
