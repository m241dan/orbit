"""The modal where the user picks a saved session to view or activate, or starts a new one."""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList

from orbit.protocol import (
    SessionSummary,
)
from orbit.tui.modal import ModalSlot, Push
from orbit.tui.view.confirm import Question
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.place import Answered, Chosen, Send
from orbit.tui.view.session_picker import (
    Manage,
    SessionChange,
    SessionChoice,
    SessionPicker,
    StartupPick,
)
from orbit.tui.view.sessions import (
    NAME_PLACEHOLDER,
    SESSIONS_TITLE,
    Viewing,
)
from orbit.tui.widgets import ChoiceScreen, TypingListScreen, confirm


class SessionsScreen(TypingListScreen[tuple[SessionChoice, ...]]):
    """Shows the :class:`SessionPicker` of ``sessions`` of the workspace ``viewing`` names,
    dismissing with the commands the user's choice sends.

    ``j``/``k`` (and the arrows) move and enter picks the highlighted row; enter in the name box
    names the new session, or renames; ``r`` renames the highlighted session and ``x`` deletes
    it, asking first. Escape on the list dismisses with none, or quits orbit at startup.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "rename", show=False),
        Binding("x", "delete", show=False),
    ]
    picker: SessionPicker

    def __init__(
        self,
        sessions: Sequence[SessionSummary],
        viewing: Viewing,
        manage: Manage,
        startup: StartupPick | None = None,
    ) -> None:
        super().__init__()
        self.picker = SessionPicker(self, sessions, viewing, startup)
        self._manage = manage
        self._why: str | None = None  # Why a pick was refused, to show once mounted.

    @property
    def workspace(self) -> str:
        """The id of the workspace whose sessions are listed."""
        return self.picker.workspace

    @property
    def sessions(self) -> tuple[SessionSummary, ...]:
        """The sessions listed, as last shown."""
        return self.picker.sessions

    @property
    def resume(self) -> bool:
        """Whether the session attached is offered to resume."""
        return self.picker.resume

    @property
    def rows(self) -> tuple[SessionSummary, ...]:
        """The sessions listed, in order, before ``New session``."""
        return self.picker.rows

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = SESSIONS_TITLE
            box.border_subtitle = self.hint
            yield OptionList(*self._options(self.picker.keys, self.picker.labels))
            yield self._note_line()
            # Typing goes on from the end of a name being changed, rather than replacing it.
            name = Input(placeholder=NAME_PLACEHOLDER, id="session-name", select_on_focus=False)
            name.display = False
            yield name

    def on_mount(self) -> None:
        self.query_one(OptionList).highlighted = 0
        self._show_note(self._why)

    def show_note(self, note: str | None) -> None:
        if self.is_mounted:
            self._show_note(note)
        else:
            self._why = note

    def confirm(
        self, title: str, message: str, yes: str, then: Callable[[], Awaitable[object]]
    ) -> None:
        confirm(self.app.push_screen, title, message, yes, then)

    async def manage(self, change: SessionChange, awaited: Awaited) -> object:
        return await self._manage(change, awaited)

    @on(OptionList.OptionSelected)
    def _choose(self, event: OptionList.OptionSelected) -> None:
        index = self._picked_row(event, self.picker.keys)
        if index is not None:
            self.picker.choose(index)

    @on(Input.Submitted, "#session-name")
    async def _named(self, event: Input.Submitted) -> None:
        event.stop()
        await self.picker.named(event.value)

    def action_rename(self) -> None:
        self.picker.rename()

    async def action_delete(self) -> None:
        await self.picker.delete()


class SessionsHost:
    """Shows the session picker and the offer to make the session viewed the active one, one of
    each at a time, with ``push``: the screens a :class:`Place` asks for, whose picker sends the
    user's changes to saved sessions with ``send``; ``moved`` hears that this frontend moved."""

    def __init__(
        self,
        push: Push[ModalScreen[Any], Any],
        send: Send,
        moved: Callable[[], None] = lambda: None,
    ) -> None:
        self._send = send
        self._moved = moved
        self._picker = ModalSlot[SessionsScreen, tuple[SessionChoice, ...]](push)
        self._offer = ModalSlot[ChoiceScreen, int](push)  # The offer to activate.

    @property
    def shown(self) -> SessionsScreen | None:
        """The picker shown, if any."""
        return self._picker.showing

    @property
    def offered(self) -> ChoiceScreen | None:
        """The offer to make the session viewed the active one, if shown."""
        return self._offer.showing

    @property
    def picker(self) -> SessionPicker | None:
        shown = self._picker.showing
        return None if shown is None else shown.picker

    def open_picker(
        self,
        sessions: Sequence[SessionSummary],
        viewing: Viewing,
        startup: StartupPick | None,
        chosen: Chosen,
    ) -> SessionPicker:
        screen = SessionsScreen(sessions, viewing, self._send, startup)
        self._picker.open(screen, chosen)
        return screen.picker

    @property
    def offer_open(self) -> bool:
        return self._offer.showing is not None

    def open_offer(self, question: Question, answered: Answered) -> None:
        screen = ChoiceScreen(
            question.title, question.message, question.choices, escape_index=question.escape
        )
        self._offer.open(screen, answered)

    def close_offer(self) -> None:
        self._offer.close(None)

    def moved(self) -> None:
        self._moved()
