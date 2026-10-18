"""The modal where the user picks a saved session to open, or starts a new one."""

from collections.abc import Awaitable, Callable, Sequence
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList

from orbit.protocol import (
    ActivateSession,
    DeleteSession,
    NewSession,
    RenameSession,
    SessionsListed,
    SessionSummary,
)
from orbit.tui.view.sessions import (
    DELETE_TITLE,
    DELETE_YES,
    NAME_PLACEHOLDER,
    NEW_SESSION_LABEL,
    SESSIONS_TITLE,
    can_delete,
    can_open,
    delete_message,
    session_label,
    sessions_hint,
)
from orbit.tui.widgets import ConfirmScreen

SessionChoice = ActivateSession | NewSession
"""What the user chose: a saved session to activate, or a new one to start."""

SessionChange = RenameSession | DeleteSession
"""A change to a saved session, made while the list stays open."""

Manage = Callable[[SessionChange], Awaitable[object]]
"""Sends a change to a saved session to the backend, which lists the sessions again after;
whatever it returns is ignored."""


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
        Binding("r", "rename", show=False),
        Binding("x", "delete", show=False),
    ]

    def __init__(
        self, sessions: Sequence[SessionSummary], manage: Manage, *, startup: bool = False
    ) -> None:
        super().__init__()
        self.sessions = tuple(sessions)
        self.startup = startup
        self._manage = manage
        self._renaming: SessionSummary | None = None  # The session being renamed, if any.

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = SESSIONS_TITLE
            box.border_subtitle = self._hint(naming=False)
            yield OptionList(NEW_SESSION_LABEL, *map(session_label, self.sessions))
            # Typing goes on from the end of a name being changed, rather than replacing it.
            name = Input(placeholder=NAME_PLACEHOLDER, id="session-name", select_on_focus=False)
            name.display = False
            yield name

    def on_mount(self) -> None:
        self.query_one(OptionList).highlighted = 0

    def show(self, sessions: Sequence[SessionSummary]) -> None:
        """List ``sessions`` in place of those listed, keeping the highlight where it was."""
        self.sessions = tuple(sessions)
        if not self.is_mounted:  # Listed again before it's on screen: compose lists them.
            return
        options = self.query_one(OptionList)
        highlighted = options.highlighted or 0
        options.clear_options()
        options.add_options([NEW_SESSION_LABEL, *map(session_label, self.sessions)])
        options.highlighted = min(highlighted, options.option_count - 1)

    @property
    def _highlighted(self) -> SessionSummary | None:
        """The session highlighted; none on ``+ new session``."""
        index = self.query_one(OptionList).highlighted
        return self.sessions[index - 1] if index else None

    @on(OptionList.OptionSelected)
    def _choose(self, event: OptionList.OptionSelected) -> None:
        if event.option_index == 0:
            self._naming(True)
            return
        summary = self.sessions[event.option_index - 1]
        if summary.current:
            self.dismiss(None)
        elif can_open(summary):
            self.dismiss(ActivateSession(session=summary.id))

    @on(Input.Submitted, "#session-name")
    async def _named(self, event: Input.Submitted) -> None:
        event.stop()
        name = event.value.strip()
        if self._renaming is None:
            self.dismiss(NewSession(name=name or None))
            return
        renaming = self._renaming
        self._naming(False)
        if name:  # An empty name keeps the old one.
            await self._manage(RenameSession(session_id=renaming.id, name=name))

    def action_rename(self) -> None:
        """Ask for the highlighted session's new name, starting from its name."""
        summary = self._highlighted
        if summary is not None:
            self._naming(True, summary)

    async def action_delete(self) -> None:
        """Delete the highlighted session once the user confirms; one that can't be deleted is
        left to the backend to refuse."""
        summary = self._highlighted
        if summary is None:
            return
        deleting = DeleteSession(session_id=summary.id)
        if not can_delete(summary):
            await self._manage(deleting)
            return

        async def answered(confirmed: bool | None) -> None:
            if confirmed:
                await self._manage(deleting)

        confirm = ConfirmScreen(DELETE_TITLE, delete_message(summary), DELETE_YES)
        self.app.push_screen(confirm, answered)

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

    def _naming(self, on: bool, renaming: SessionSummary | None = None) -> None:
        """Show the box for the name of a new session, or the new name of the session
        ``renaming``, and type in it; or go back to the list."""
        name = self.query_one(Input)
        name.display = on
        self._renaming = renaming
        self.query_one(Vertical).border_subtitle = self._hint(naming=on, renaming=renaming)
        if on:
            name.value = renaming.name if renaming else ""
            name.focus()
        else:
            name.clear()
            self.query_one(OptionList).focus()

    def _hint(self, *, naming: bool, renaming: SessionSummary | None = None) -> str:
        """The keys shown: the name box's while ``naming``, or the list's."""
        return sessions_hint(startup=self.startup, naming=naming, renaming=renaming is not None)


Chosen = Callable[[SessionChoice | None], Awaitable[None]]
Push = Callable[[SessionsScreen, Chosen], object]
Send = Callable[[SessionChoice | SessionChange], Awaitable[object]]
"""Sends a session command to the backend; whatever it returns is ignored."""


class SessionsHost:
    """Shows the sessions listed, with ``push``, refreshing the list while it's shown;
    ``send`` takes the user's choice and their changes to saved sessions."""

    def __init__(self, push: Push, send: Send) -> None:
        self._push = push
        self._send = send
        self.shown: SessionsScreen | None = None  # The list of sessions shown, if any.
        # Whether the user pressed ctrl+p, after which no list opens as the startup list.
        self._asked = False

    def asked(self) -> None:
        """Note the user asked for the sessions, so none opens as the startup list."""
        self._asked = True

    def show(self, listed: SessionsListed) -> None:
        """Let the user pick one of the sessions ``listed`` or start a new one; at startup, before
        any ctrl+p, escape quits. Listed while the list is shown, they replace those it shows;
        refreshed after a rename or delete once the list was closed, they're ignored."""
        if self.shown is not None:
            self.shown.show(listed.sessions)
            return
        if listed.reason == "refreshed":
            return

        async def chosen(choice: SessionChoice | None) -> None:
            self.shown = None
            # Refusals come back as errors, shown like any other.
            if choice is not None:
                await self._send(choice)

        startup = listed.reason == "startup" and not self._asked
        self.shown = SessionsScreen(listed.sessions, self._send, startup=startup)
        self._push(self.shown, chosen)
