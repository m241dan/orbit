"""The modal where the user picks a saved session to view or activate, or starts a new one."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList

from orbit.protocol import (
    ActivateSession,
    Attach,
    SessionRef,
    SessionSummary,
    Snapshot,
    WorkspaceEntry,
)
from orbit.tui.catalog import CatalogState
from orbit.tui.modal import ModalSlot, Push
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.session_picker import (
    Manage,
    SessionChange,
    SessionChoice,
    SessionPicker,
    StartupPick,
)
from orbit.tui.view.sessions import (
    ACTIVATE_CHOICE,
    ACTIVATION_TITLE,
    JUST_LOOK_CHOICE,
    NAME_PLACEHOLDER,
    SESSIONS_TITLE,
    Viewing,
    activation_choices,
    activation_message,
    active_id,
    active_name,
    held_back,
    idle_now_message,
    opening_attach,
    picker_viewing,
    running_words,
    session_turned_idle,
    unjoined_view,
    viewed_only,
    viewing_mark,
)
from orbit.tui.view.tasks import coder_wait_row
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
    def joined(self) -> bool:
        """Whether the frontend is on the workspace listed."""
        return self.picker.joined

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

    def refused(self, why: str) -> None:
        """Show ``why`` a pick was refused under the list, as soon as it's composed."""
        self.picker.refused(why)

    def show(self, sessions: Sequence[SessionSummary], attached: str | None) -> None:
        """List ``sessions``, with the one ``attached`` first, in place of those listed."""
        self.picker.show(sessions, attached)

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


Send = Callable[[SessionChoice | SessionChange, Awaited], Awaitable[bool]]
"""Sends a session command to the backend, awaiting its outcome as told, returning whether it
was sent."""
OpensOn = Callable[[Sequence[WorkspaceEntry]], WorkspaceEntry | None]
"""The workspace a frontend opens on by itself, among those a catalog lists; none while the
user is to pick one."""


def _first(workspaces: Sequence[WorkspaceEntry]) -> WorkspaceEntry | None:
    """The first of ``workspaces``, if there's one."""
    return next(iter(workspaces), None)


@dataclass(frozen=True)
class Placing:
    """Where a frontend is placed: on the workspace it ``opens_on`` by itself, calling
    ``moved`` once it moves to another workspace, or back as attaching there fails to send."""

    opens_on: OpensOn = _first
    moved: Callable[[], None] = lambda: None


_FIRST = Placing()
"""Placed on the first workspace listed, with nothing to call as it moves."""


@dataclass(frozen=True)
class _Place:
    """Where a frontend is: on the workspace ``workspace``, viewing the session ``attached``,
    ``live`` if it's the active one, else read-only while ``open_session`` is."""

    workspace: str | None
    attached: str | None
    live: bool = True
    open_session: SessionRef | None = None


class SessionsHost:
    """Keeps the workspace this frontend is on, among those the ``catalog`` lists, which it
    opens on by itself, as its ``placing`` says, or the user picks, and the session this
    frontend views, live or read-only; shows that workspace's sessions with ``push`` when
    asked, refreshing the picker while it's shown, and offers to make a session viewed
    read-only the active one, saying what the active one runs. ``send`` takes the user's choice
    and their changes to saved sessions."""

    def __init__(
        self,
        push: Push[ModalScreen[Any], Any],
        send: Send,
        catalog: CatalogState,
        placing: Placing = _FIRST,
    ) -> None:
        self._send = send
        self._catalog = catalog
        self._opens_on = placing.opens_on
        self._moved = placing.moved
        self.workspace: str | None = None  # The id of the workspace it's on, once there.
        self.attached: str | None = None  # The session this frontend views, once known.
        self.live = True  # Whether the session viewed is the active one.
        self.open_session: SessionRef | None = None  # The active one, while it's another.
        self._viewed_name = ""  # The session viewed's name, as its snapshot names it.
        self._offering: str | None = None  # The session picked to view only, until its view.
        # The session picked and viewed only as the active one ran, until that one turns idle.
        self._held: str | None = None
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
    def sessions(self) -> tuple[SessionSummary, ...]:
        """The sessions of the workspace this frontend is on, as the catalog lists them."""
        return self._catalog.sessions(self.workspace)

    @property
    def attached_session(self) -> SessionSummary | None:
        """The session this frontend views, among those listed; none if it isn't."""
        return next((summary for summary in self.sessions if summary.id == self.attached), None)

    @property
    def mark(self) -> str | None:
        """The mark by the prompt while the session viewed isn't the active one, named as the
        catalog names it, renamed perhaps, with what the active one runs; none while it's
        live."""
        if self.live:
            return None
        return viewing_mark(self.viewed_name, self.active_name, self.running)

    @property
    def running(self) -> str | None:
        """What the active session runs, in words; none while it's idle or isn't known."""
        return running_words(self.sessions)

    @property
    def coder_wait(self) -> tuple[str, str] | None:
        """The task about to run in the session viewed while it waits for a coder, and what its
        row says; none while it doesn't wait."""
        attached = self.attached_session
        return None if attached is None else coder_wait_row(attached.activity)

    @property
    def viewed_name(self) -> str:
        """The session viewed's name, as the catalog names it, renamed perhaps, or else as its
        snapshot does."""
        attached = self.attached_session
        return self._viewed_name if attached is None else attached.name

    @property
    def active_name(self) -> str | None:
        """The active session's name, as the catalog names it, or else as the snapshot of the
        session viewed read-only does; none while the session viewed is live."""
        if self.open_session is None:
            return None
        return active_name(self.sessions) or self.open_session.name

    def follow(self) -> None:
        """Follow the catalog's latest listing: until this frontend is on a workspace, it may
        open on one by itself, and it leaves the one it's on once the catalog doesn't list it;
        until a snapshot says which session this frontend views, it's the active one, which a
        frontend attaches to as it opens."""
        workspaces = self._catalog.workspaces
        if self.workspace is None:
            chosen = self._opens_on(workspaces)
            self.workspace = chosen.id if chosen else None
        elif not any(entry.id == self.workspace for entry in workspaces):
            self._leave()
        if self.attached is None:
            self.attached = active_id(self.sessions)
        self._refresh()

    def _leave(self) -> None:
        """Be on no workspace, viewing no session."""
        self.workspace = self.attached = self.open_session = None
        self.live = True

    async def join(self, workspace: str) -> bool:
        """Whether attaching to the active session of the workspace ``workspace`` was sent, after
        which this frontend is on that workspace, viewing that session; it stays where it was if
        that fails to send or there's no active session."""
        was = self._move(workspace)
        if await self.attach_active():
            return True
        self._be(was)
        return False

    async def attach_active(self) -> bool:
        """Whether attaching to the active session of the workspace this frontend is on was
        sent; not if there's none."""
        attach = self.opening
        return attach is not None and await self._send(attach, Awaited())

    def _move(self, workspace: str) -> _Place:
        """Be on the workspace ``workspace``, viewing its active session, and return where this
        frontend was."""
        was = _Place(self.workspace, self.attached, self.live, self.open_session)
        self._be(_Place(workspace, active_id(self._catalog.sessions(workspace))))
        return was

    def _be(self, place: _Place) -> None:
        """Be at ``place``, listing the sessions of its workspace as the latest catalog does."""
        self.workspace, self.attached = place.workspace, place.attached
        self.live, self.open_session = place.live, place.open_session
        self._refresh()
        self._moved()

    def follow_state(self) -> None:
        """Follow a session's state changing in the catalog; once the active session that held
        back the session picked turns idle, offer again to make that one the active session."""
        self._refresh()
        held = self._held
        active = None if held is None else session_turned_idle(self.sessions, held)
        if held is not None and active is not None:
            self._held = None
            self._withdraw()
            self._show_offer(held, idle_now_message(active), activation_choices(running=False))

    @property
    def opening(self) -> Attach | None:
        """What this frontend sends to view the active session of the workspace it opens on;
        none until a catalog lists it."""
        return opening_attach(self.workspace, self.sessions)

    def view(self, snapshot: Snapshot) -> None:
        """Note this frontend views ``snapshot``'s session, live or read-only; a read-only view
        of the session just picked to view offers to make it the active one."""
        session = snapshot.session
        offering, self._offering = self._offering == session.id, None
        self.attached, self.live, self._viewed_name = session.id, snapshot.active, session.name
        self.open_session = None if snapshot.active else snapshot.open_session
        if snapshot.active:  # The offer is moot once it's live.
            self._withdraw()
        self._refresh()
        running, active = self.running, self.active_name
        self._held = held_back(session.id, running=running, active=active) if offering else None
        if offering:
            self.offer()

    def offer(self) -> None:
        """Offer to make the session viewed the active one, or say what the active one runs,
        unless it's live, the active one isn't known or the offer is shown already."""
        viewed, active = self.attached, self.active_name
        if viewed is None or active is None or self.offered is not None:
            return
        running = self.running
        message = activation_message(self.viewed_name, active, running=running)
        self._show_offer(viewed, message, activation_choices(running=running is not None))

    def _show_offer(self, viewed: str, message: str, choices: Sequence[str]) -> None:
        """Show the offer to make the session ``viewed`` the active one, saying ``message`` and
        offering ``choices``."""

        async def answered(index: int | None) -> None:
            if index is not None and choices[index] == ACTIVATE_CHOICE:
                await self._send(ActivateSession(session=viewed), Awaited())

        just_look = choices.index(JUST_LOOK_CHOICE)
        screen = ChoiceScreen(ACTIVATION_TITLE, message, choices, escape_index=just_look)
        self._offer.open(screen, answered)

    def _withdraw(self) -> None:
        """Close the offer shown, if any."""
        self._offer.close(None)

    def open(self, startup: StartupPick | None = None, workspace: str | None = None) -> None:
        """Let the user pick a session of the ``workspace``, by default the one this frontend is
        on, or start a new one, unless the picker is shown already; at ``startup``, opened as it
        says, escape quits. Picking in another workspace moves this frontend there."""
        if self.shown is not None:
            return
        viewing, sessions = picker_viewing(
            self._catalog.workspaces, self.workspace, self.attached, self.sessions, workspace
        )

        screen = SessionsScreen(sessions, viewing, self._send, startup)

        async def chosen(choice: tuple[SessionChoice, ...] | None) -> None:
            # Moved before sending, as the view may come back before sending returns.
            was = self._move(viewing.workspace) if choice and not viewing.joined else None
            # The sessions as last shown, as they may have changed since the picker opened.
            refused = Awaited(why=lambda why: self._pick_refused(why, startup, workspace))
            await self._send_choice(choice or (), screen.sessions, refused, was)

        self._picker.open(screen, chosen)

    def _pick_refused(self, why: str, startup: StartupPick | None, workspace: str | None) -> None:
        """Show ``why`` a pick was refused in the picker, opening it again as it was opened, at
        ``startup`` and on the ``workspace``, unless one is shown; no view of it comes."""
        self._offering = None
        self.open(startup, workspace)
        shown = self.shown
        assert shown is not None  # Opened, or shown already.
        shown.refused(why)

    async def _send_choice(
        self,
        choice: Sequence[SessionChoice],
        sessions: Sequence[SessionSummary],
        awaited: Awaited,
        was: _Place | None = None,
    ) -> None:
        """Send the commands of the user's ``choice`` among ``sessions``, each awaited as
        ``awaited`` says, until one fails to send; if the first does, go back to where this
        frontend ``was``, if it moved."""
        # Before sending, as the view may come back before sending returns.
        self._offering = viewed_only(choice, sessions)
        for command in choice:
            if not await self._send(command, awaited):
                self._offering = None
                if was is not None:
                    self._be(was)
                return
            was = None  # On the workspace moved to once attached there.

    def _refresh(self) -> None:
        """Show the sessions in the picker, if it's shown: of the workspace it lists, with its
        active one to resume if this frontend isn't on it."""
        shown = self.shown
        if shown is None:
            return
        if shown.joined:
            shown.show(self.sessions, self.attached)
            return
        viewing, sessions = unjoined_view(self._catalog.workspaces, shown.workspace)
        shown.show(sessions, viewing.attached)
