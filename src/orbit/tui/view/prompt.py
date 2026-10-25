"""The prompt's marks, without the widget showing them: in its title, whom it talks to and
whether that's busy or the orbit read-only, in it, whom it talks to while it's empty, and under
it, the settings."""

from typing import Protocol

from orbit.protocol import PLANNER, Settings
from orbit.tui.view.settings import settings_summary
from orbit.tui.view.transcript import prompt_placeholder, prompt_title


class PromptMarksView(Protocol):
    """Where a :class:`PromptMarks` shows."""

    def show_title(self, title: str) -> None:
        """Show ``title`` above the prompt."""

    def show_subtitle(self, subtitle: str) -> None:
        """Show ``subtitle`` under the prompt."""

    def show_placeholder(self, placeholder: str) -> None:
        """Show ``placeholder`` in the prompt while it's empty."""


class PromptMarks:
    """The prompt's marks, shown in ``view``: the conversational role it talks to, the planner
    to start with, whether that's busy and the orbit read-only, which start as neither, and the
    settings."""

    def __init__(self, view: PromptMarksView) -> None:
        self._view = view
        self.role = PLANNER
        self.busy = False
        self.read_only = False

    def show_target(self, role: str, label: str) -> None:
        """Mark the prompt as talking to the conversational ``role``, labelled ``label``."""
        self.role = role
        self._view.show_placeholder(prompt_placeholder(label))
        self._show_title()

    def show_busy(self, busy: bool) -> None:
        """Mark the prompt while the role it talks to is ``busy``."""
        self.busy = busy
        self._show_title()

    def show_read_only(self, read_only: bool) -> None:
        """Mark the prompt while the orbit is ``read_only``."""
        self.read_only = read_only
        self._show_title()

    def show_settings(self, settings: Settings) -> None:
        """Show ``settings``, those of the role it talks to, under the prompt."""
        self._view.show_subtitle(settings_summary(settings, self.role))

    def _show_title(self) -> None:
        self._view.show_title(prompt_title(self.busy, self.read_only, self.role))
