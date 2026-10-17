"""The modal where the user picks a saved session to open, or starts a new one."""

from collections.abc import Sequence
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList

from orbit.protocol import NewSession, OpenSession, SessionSummary
from orbit.tui.view import (
    NAME_PLACEHOLDER,
    NEW_SESSION_LABEL,
    SESSIONS_TITLE,
    can_open,
    session_label,
)

LIST_HINT = "j/k move · enter open · esc close"
STARTUP_HINT = "j/k move · enter open · esc quit"
NAME_HINT = "enter start · esc back"

SessionChoice = OpenSession | NewSession
"""What the user chose: a saved session to open, or a new one to start."""


class SessionsScreen(ModalScreen[SessionChoice | None]):
    """Lists ``sessions`` after ``+ new session``, and dismisses with the user's choice.

    ``j``/``k`` (and the arrows) move and enter opens the highlighted session, unless it can't
    be opened; the current session dismisses with None, keeping it. Enter on ``+ new session``
    asks for a name, which enter confirms and escape leaves. Escape on the list dismisses with
    None, or quits orbit at ``startup``, where there's no session to go back to.
    """

    AUTO_FOCUS = "OptionList"
    DEFAULT_CSS = """
    SessionsScreen { align: center middle; }
    SessionsScreen > Vertical {
        width: 90;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: round $accent;
        padding: 0 1;
    }
    SessionsScreen OptionList { height: auto; max-height: 30; border: none; }
    SessionsScreen Input { border: round $secondary; }
    SessionsScreen Input:focus { border: heavy $accent; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", show=False),
        # Vim-style movement alongside the list's own arrow keys; the name box takes them as
        # typing, since a focused input handles keys first.
        Binding("j", "move('down')", show=False),
        Binding("k", "move('up')", show=False),
    ]

    def __init__(self, sessions: Sequence[SessionSummary], *, startup: bool = False) -> None:
        super().__init__()
        self.sessions = tuple(sessions)
        self.startup = startup

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = SESSIONS_TITLE
            box.border_subtitle = self._list_hint
            yield OptionList(NEW_SESSION_LABEL, *map(session_label, self.sessions))
            name = Input(placeholder=NAME_PLACEHOLDER, id="session-name")
            name.display = False
            yield name

    @property
    def _list_hint(self) -> str:
        return STARTUP_HINT if self.startup else LIST_HINT

    def on_mount(self) -> None:
        self.query_one(OptionList).highlighted = 0

    @on(OptionList.OptionSelected)
    def _choose(self, event: OptionList.OptionSelected) -> None:
        if event.option_index == 0:
            self._naming(True)
            return
        summary = self.sessions[event.option_index - 1]
        if summary.current:
            self.dismiss(None)
        elif can_open(summary):
            self.dismiss(OpenSession(session_id=summary.id))

    @on(Input.Submitted, "#session-name")
    def _start(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(NewSession(name=event.value.strip() or None))

    def action_move(self, direction: str) -> None:
        options = self.query_one(OptionList)
        if direction == "down":
            options.action_cursor_down()
        else:
            options.action_cursor_up()

    def action_back(self) -> None:
        if self.query_one(Input).display:
            self._naming(False)
        elif self.startup:
            self.app.exit()
        else:
            self.dismiss(None)

    def _naming(self, on: bool) -> None:
        """Show the box for the new session's name and type in it, or go back to the list."""
        name = self.query_one(Input)
        name.display = on
        self.query_one(Vertical).border_subtitle = NAME_HINT if on else self._list_hint
        if on:
            name.focus()
        else:
            name.clear()
            self.query_one(OptionList).focus()
