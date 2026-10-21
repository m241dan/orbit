"""The modal where the user picks a saved session to view or activate, or starts a new one."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, ClassVar, Literal

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static

from orbit.protocol import (
    ActivateSession,
    Attach,
    Catalog,
    DeleteSession,
    NewSession,
    RenameSession,
    SessionRef,
    SessionStateChanged,
    SessionSummary,
    Snapshot,
    WorkspaceEntry,
    with_state,
)
from orbit.tui.view.common import kept_highlight
from orbit.tui.view.sessions import (
    ACTIVATE_CHOICE,
    ACTIVATION_TITLE,
    DELETE_TITLE,
    DELETE_YES,
    NAME_PLACEHOLDER,
    NEW_SESSION_LABEL,
    SESSIONS_TITLE,
    Viewing,
    activation_choices,
    activation_message,
    active_id,
    active_name,
    can_delete,
    can_open,
    delete_message,
    idle_now_message,
    opening_attach,
    picked_commands,
    picker_sessions,
    running_words,
    session_label,
    sessions_hint,
    starting_commands,
    viewed_only,
    viewing_mark,
    workspace_sessions,
)
from orbit.tui.view.workspaces import with_states
from orbit.tui.widgets import ConfirmScreen, ListScreen

SessionChoice = Attach | ActivateSession | NewSession
"""What the user chose: a saved session to view, and perhaps activate, or a new one to start."""

StartupPick = Literal["resume", "pick"]
"""How the picker opens at startup: offering to resume the session viewed, or only to pick one."""

SessionChange = RenameSession | DeleteSession
"""A change to a saved session, made while the list stays open."""

Manage = Callable[[SessionChange], Awaitable[object]]
"""Sends a change to a saved session to the backend, which lists the sessions again after;
whatever it returns is ignored."""


class SessionsScreen(ListScreen[tuple[SessionChoice, ...]]):
    """Lists ``sessions`` of the workspace ``viewing`` names before ``New session``: the one it
    has attached first (as ``Resume`` if ``startup`` is ``resume`` or it hasn't joined the
    workspace, and not at all if ``startup`` is ``pick``), then the others; dismisses with the
    commands the user's choice sends, attaching to the workspace if it hasn't joined it.

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
        Binding("r", "rename", show=False),
        Binding("x", "delete", show=False),
    ]

    def __init__(
        self,
        sessions: Sequence[SessionSummary],
        viewing: Viewing,
        manage: Manage,
        startup: StartupPick | None = None,
    ) -> None:
        super().__init__()
        self.workspace = viewing.workspace
        self.joined = viewing.joined
        self.sessions = tuple(sessions)
        self.attached = viewing.attached
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
        the highlight on the session it was on, or where it was if that one's gone."""
        if not self.is_mounted:  # Listed again before it's on screen: compose lists them.
            self.sessions, self.attached = tuple(sessions), attached
            return
        options = self.query_one(OptionList)
        shown, highlighted = self._keys, options.highlighted
        self.sessions, self.attached = tuple(sessions), attached
        options.clear_options()
        options.add_options(self._labels())
        options.highlighted = kept_highlight(shown, highlighted, self._keys)

    @property
    def _keys(self) -> tuple[str | None, ...]:
        """The rows' session ids, then None for ``New session``."""
        return (*(row.id for row in self.rows), None)

    def _labels(self) -> list[Text | str]:
        """The rows' labels, then ``New session``'s."""
        rows = [
            session_label(
                summary,
                attached=self.joined and summary.id == self.attached,
                resume=(self.startup or not self.joined) and summary.id == self.attached,
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
            attached = self.attached if self.joined else None
            self.dismiss(picked_commands(summary, self.sessions, attached, self.workspace))

    @on(Input.Submitted, "#session-name")
    async def _named(self, event: Input.Submitted) -> None:
        event.stop()
        name = event.value.strip()
        if self._renaming is None:
            viewing = Viewing(self.workspace, self.attached, self.joined)
            self.dismiss(starting_commands(name or None, viewing))
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


class ActivationScreen(ListScreen[bool]):
    """Says ``message`` about the session viewed read-only and offers ``choices``, the first
    preselected; dismisses with True on making it the active session, False on just looking
    or escape."""

    AUTO_FOCUS = "OptionList"
    DEFAULT_CSS = """
    ActivationScreen { align: center middle; }
    ActivationScreen > Vertical {
        width: 70;
        max-width: 90%;
        height: auto;
        border: round $accent;
        padding: 0 1;
    }
    ActivationScreen OptionList { height: auto; border: none; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "just_look", show=False),
    ]

    def __init__(self, message: str, choices: Sequence[str]) -> None:
        super().__init__()
        self.message = message
        self.choices = tuple(choices)

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = ACTIVATION_TITLE
            yield Static(Text(self.message), id="activation-message")
            yield OptionList(*self.choices)

    def on_mount(self) -> None:
        # One dismissed before it mounts, replaced by a newer offer, has no list left.
        for options in self.query(OptionList):
            options.highlighted = 0

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.choices[event.option_index] == ACTIVATE_CHOICE)

    def action_just_look(self) -> None:
        self.dismiss(False)


Push = Callable[[ModalScreen[Any], Callable[[Any], Awaitable[None]]], object]
"""Shows a modal screen, calling back with what it dismisses with."""
Send = Callable[[SessionChoice | SessionChange], Awaitable[bool]]
"""Sends a session command to the backend, returning whether it was sent."""
Numbers = Callable[[], Mapping[str, str]]
"""The task list's numbers, by task id."""
OpensOn = Callable[[Sequence[WorkspaceEntry]], WorkspaceEntry | None]
"""The workspace a frontend opens on by itself, among those a catalog lists; none while the
user is to pick one."""


def _first(workspaces: Sequence[WorkspaceEntry]) -> WorkspaceEntry | None:
    """The first of ``workspaces``, if there's one."""
    return next(iter(workspaces), None)


class SessionsHost:
    """Keeps the sessions the latest ``Catalog`` lists of the workspace this frontend is on,
    which it ``opens_on`` by itself or the user picks, as ``SessionStateChanged`` changes them,
    and the one this frontend views, live or read-only; shows them with ``push`` when asked,
    refreshing the picker while it's shown, and offers to make a session viewed read-only the
    active one, naming what the active one runs by the task ``numbers``. ``send`` takes the
    user's choice and their changes to saved sessions."""

    def __init__(
        self,
        push: Push,
        send: Send,
        numbers: Numbers = lambda: {},
        opens_on: OpensOn = _first,
    ) -> None:
        self._push = push
        self._send = send
        self._numbers = numbers
        self._opens_on = opens_on
        self._workspaces: tuple[WorkspaceEntry, ...] = ()  # As the latest catalog lists them.
        self.workspace: str | None = None  # The id of the workspace it's on, once there.
        self.sessions: tuple[SessionSummary, ...] = ()
        self.attached: str | None = None  # The session this frontend views, once known.
        self.live = True  # Whether the session viewed is the active one.
        self.open_session: SessionRef | None = None  # The active one, while it's another.
        self._viewed_name = ""  # The session viewed's name, as its snapshot names it.
        self._offering: str | None = None  # The session picked to view only, until its view.
        # The session picked and viewed only as the active one ran, until that one turns idle.
        self._held: str | None = None
        self.shown: SessionsScreen | None = None  # The picker shown, if any.
        self.offered: ActivationScreen | None = None  # The offer to activate shown, if any.

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
        return running_words(self.sessions, self._numbers())

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

    def follow(self, catalog: Catalog) -> None:
        """List the sessions in ``catalog`` of the workspace this frontend is on, which, until
        it's on one, it may open on by itself, and which it leaves once ``catalog`` doesn't list
        it; until a snapshot says which one this frontend views, it's the active one, which a
        frontend attaches to as it opens."""
        self._workspaces = catalog.workspaces
        if self.workspace is None:
            chosen = self._opens_on(catalog.workspaces)
            self.workspace = chosen.id if chosen else None
        elif not any(entry.id == self.workspace for entry in catalog.workspaces):
            self._leave()
        self.sessions = workspace_sessions(catalog.workspaces, self.workspace)
        if self.attached is None:
            self.attached = active_id(self.sessions)
        self._refresh()

    def _leave(self) -> None:
        """Be on no workspace, viewing no session."""
        self.workspace = self.attached = self.open_session = None
        self.live = True

    def join(self, workspace: str) -> Attach | None:
        """Be on the workspace ``workspace``, viewing its active session once attached, and
        return what attaches to that one; none if it has none."""
        self.workspace, self.live, self.open_session = workspace, True, None
        self.sessions = workspace_sessions(self._workspaces, workspace)
        self.attached = active_id(self.sessions)
        self._refresh()
        return self.opening

    def follow_state(self, changed: SessionStateChanged) -> None:
        """Note ``changed``'s session's state; once the active session that held back the
        session picked turns idle, offer again to make that one the active session."""
        self.sessions = tuple(with_state(summary, changed) for summary in self.sessions)
        self._workspaces = with_states(self._workspaces, changed)
        self._refresh()
        held = self._held
        active = None if held is None else self._turned_idle(held)
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
        self._held = self._held_back(session.id) if offering else None
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

    def _held_back(self, viewed: str) -> str | None:
        """The session ``viewed``, if a running active session keeps it from being the active
        one; none otherwise."""
        return viewed if self.running is not None and self.active_name is not None else None

    def _turned_idle(self, held: str) -> str | None:
        """The name of the active session, if it's idle and isn't the session ``held``."""
        active = next((summary for summary in self.sessions if summary.active), None)
        if active is None or active.id == held or active.state != "idle":
            return None
        return active.name

    def _show_offer(self, viewed: str, message: str, choices: Sequence[str]) -> None:
        """Show the offer to make the session ``viewed`` the active one, saying ``message`` and
        offering ``choices``."""

        async def answered(activate: bool | None) -> None:
            if self.offered is screen:
                self.offered = None
            if activate:
                await self._send(ActivateSession(session=viewed))

        screen = ActivationScreen(message, choices)
        self.offered = screen
        self._push(screen, answered)

    def _withdraw(self) -> None:
        """Close the offer shown, if any."""
        if self.offered is not None:
            self.offered.dismiss(False)
            self.offered = None

    def open(self, startup: StartupPick | None = None, workspace: str | None = None) -> None:
        """Let the user pick a session of the ``workspace``, by default the one this frontend is
        on, or start a new one, unless the picker is shown already; at ``startup``, opened as it
        says, escape quits. Picking in another workspace moves this frontend there."""
        if self.shown is not None:
            return
        viewing, sessions = self._viewing(workspace)

        async def chosen(choice: tuple[SessionChoice, ...] | None) -> None:
            self.shown = None
            if choice and not viewing.joined:
                self.join(viewing.workspace)
            await self._send_choice(choice or (), sessions)

        self.shown = SessionsScreen(sessions, viewing, self._send, startup)
        self._push(self.shown, chosen)

    async def _send_choice(
        self, choice: Sequence[SessionChoice], sessions: Sequence[SessionSummary]
    ) -> None:
        """Send the commands of the user's ``choice`` among ``sessions``, until one fails to
        send."""
        # Before sending, as the view may come back before sending returns.
        self._offering = viewed_only(choice, sessions)
        # Refusals come back as errors, shown like any other.
        for command in choice:
            if not await self._send(command):
                self._offering = None
                return

    def _viewing(self, workspace: str | None) -> tuple[Viewing, tuple[SessionSummary, ...]]:
        """What this frontend views of the ``workspace``, the one it's on by default, and its
        sessions."""
        if workspace is None or workspace == self.workspace:
            # No session is listed, so none is picked, until a catalog names the workspace.
            return Viewing(workspace=self.workspace or "", attached=self.attached), self.sessions
        sessions = workspace_sessions(self._workspaces, workspace)
        return Viewing(workspace, active_id(sessions), joined=False), sessions

    def _refresh(self) -> None:
        """Show the sessions in the picker, if it's shown: of the workspace it lists, with its
        active one to resume if this frontend isn't on it."""
        shown = self.shown
        if shown is None:
            return
        if shown.joined:
            shown.show(self.sessions, self.attached)
            return
        sessions = workspace_sessions(self._workspaces, shown.workspace)
        shown.show(sessions, active_id(sessions))
