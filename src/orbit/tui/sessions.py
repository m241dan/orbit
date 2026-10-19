"""The modal where the user picks a saved session to view or activate, or starts a new one."""

from collections.abc import Awaitable, Callable, Sequence
from typing import ClassVar, Literal

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList

from orbit.protocol import (
    ActivateSession,
    Attach,
    Catalog,
    DeleteSession,
    NewSession,
    RenameSession,
    SessionStateChanged,
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
    picked_commands,
    picker_sessions,
    session_label,
    sessions_hint,
    with_state,
)
from orbit.tui.widgets import ConfirmScreen

SessionChoice = Attach | ActivateSession | NewSession
"""What the user chose: a saved session to view, and perhaps activate, or a new one to start."""

StartupPick = Literal["resume", "pick"]
"""How the picker opens at startup: offering to resume the session viewed, or only to pick one."""

SessionChange = RenameSession | DeleteSession
"""A change to a saved session, made while the list stays open."""

Manage = Callable[[SessionChange], Awaitable[object]]
"""Sends a change to a saved session to the backend, which lists the sessions again after;
whatever it returns is ignored."""


class SessionsScreen(ModalScreen[tuple[SessionChoice, ...]]):
    """Lists ``sessions`` before ``New session``: the one ``attached`` first (as ``Resume`` if
    ``startup`` is ``resume``, and not at all if it's ``pick``), then the others; dismisses with
    the commands the user's choice sends.

    ``j``/``k`` (and the arrows) move and enter picks the highlighted session, unless it can't
    be opened; the attached session dismisses with none, keeping it. Enter on ``New session``
    asks for a name, which enter confirms and escape leaves. Escape on the list dismisses with
    none, or quits orbit at ``startup``, where there's no session to go back to.
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
        self,
        sessions: Sequence[SessionSummary],
        attached: str | None,
        manage: Manage,
        startup: StartupPick | None = None,
    ) -> None:
        super().__init__()
        self.sessions = tuple(sessions)
        self.attached = attached
        self.startup = startup is not None
        self.resume = startup == "resume"
        self._manage = manage
        self._renaming: SessionSummary | None = None  # The session being renamed, if any.

    @property
    def rows(self) -> tuple[SessionSummary, ...]:
        """The sessions listed, in order, before ``New session``."""
        with_attached = self.resume or not self.startup
        return picker_sessions(self.sessions, self.attached, with_attached=with_attached)

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = SESSIONS_TITLE
            box.border_subtitle = self._hint(naming=False)
            yield OptionList(*self._labels())
            # Typing goes on from the end of a name being changed, rather than replacing it.
            name = Input(placeholder=NAME_PLACEHOLDER, id="session-name", select_on_focus=False)
            name.display = False
            yield name

    def on_mount(self) -> None:
        self.query_one(OptionList).highlighted = 0

    def show(self, sessions: Sequence[SessionSummary], attached: str | None) -> None:
        """List ``sessions``, with the one ``attached`` first, in place of those listed, keeping
        the highlight where it was."""
        self.sessions, self.attached = tuple(sessions), attached
        if not self.is_mounted:  # Listed again before it's on screen: compose lists them.
            return
        options = self.query_one(OptionList)
        highlighted = options.highlighted or 0
        options.clear_options()
        options.add_options(self._labels())
        options.highlighted = min(highlighted, options.option_count - 1)

    def _labels(self) -> list[Text | str]:
        """The rows' labels, then ``New session``'s."""
        rows = [
            session_label(
                summary,
                attached=summary.id == self.attached,
                resume=self.startup and summary.id == self.attached,
            )
            for summary in self.rows
        ]
        return [*rows, NEW_SESSION_LABEL]

    @property
    def _highlighted(self) -> SessionSummary | None:
        """The session highlighted; none on ``New session``."""
        rows = self.rows
        index = self.query_one(OptionList).highlighted or 0
        return rows[index] if index < len(rows) else None

    @on(OptionList.OptionSelected)
    def _choose(self, event: OptionList.OptionSelected) -> None:
        rows = self.rows
        if event.option_index == len(rows):
            self._naming(True)
            return
        summary = rows[event.option_index]
        if can_open(summary):
            self.dismiss(picked_commands(summary, self.sessions, self.attached))

    @on(Input.Submitted, "#session-name")
    async def _named(self, event: Input.Submitted) -> None:
        event.stop()
        name = event.value.strip()
        if self._renaming is None:
            self.dismiss((NewSession(name=name or None),))
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
            self.dismiss(())

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


Chosen = Callable[[tuple[SessionChoice, ...] | None], Awaitable[None]]
Push = Callable[[SessionsScreen, Chosen], object]
Send = Callable[[SessionChoice | SessionChange], Awaitable[bool]]
"""Sends a session command to the backend, returning whether it was sent."""


class SessionsHost:
    """Keeps the sessions the latest ``Catalog`` lists, as ``SessionStateChanged`` changes them,
    and the one this frontend views; shows them with ``push`` when asked, refreshing the picker
    while it's shown. ``send`` takes the user's choice and their changes to saved sessions."""

    def __init__(self, push: Push, send: Send) -> None:
        self._push = push
        self._send = send
        self.sessions: tuple[SessionSummary, ...] = ()
        self.attached: str | None = None  # The session this frontend views, once known.
        self.shown: SessionsScreen | None = None  # The picker shown, if any.

    @property
    def attached_session(self) -> SessionSummary | None:
        """The session this frontend views, among those listed; none if it isn't."""
        return next((summary for summary in self.sessions if summary.id == self.attached), None)

    def follow(self, catalog: Catalog) -> None:
        """List the sessions in ``catalog``; until a snapshot says which one this frontend views,
        it's the active one, which a frontend views as it joins."""
        self.sessions = tuple(s for workspace in catalog.workspaces for s in workspace.sessions)
        if self.attached is None:
            self.attached = next((s.id for s in self.sessions if s.active), None)
        self._refresh()

    def follow_state(self, changed: SessionStateChanged) -> None:
        """Note ``changed``'s session's state."""
        self.sessions = with_state(self.sessions, changed)
        self._refresh()

    def attach(self, session: str) -> None:
        """Note this frontend views ``session``, which a snapshot of it says."""
        self.attached = session
        self._refresh()

    def open(self, startup: StartupPick | None = None) -> None:
        """Let the user pick a session or start a new one, unless the picker is shown already; at
        ``startup``, opened as it says, escape quits."""
        if self.shown is not None:
            return

        async def chosen(choice: tuple[SessionChoice, ...] | None) -> None:
            self.shown = None
            # Refusals come back as errors, shown like any other.
            for command in choice or ():
                if not await self._send(command):
                    return

        self.shown = SessionsScreen(self.sessions, self.attached, self._send, startup)
        self._push(self.shown, chosen)

    def _refresh(self) -> None:
        """Show the sessions in the picker, if it's shown."""
        if self.shown is not None:
            self.shown.show(self.sessions, self.attached)
