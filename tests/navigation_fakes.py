"""Where a frontend is and how it moves, driven without Textual: a :class:`Navigation` over fake
modals holding the real pickers' and explorer's models, sending through a real CommandDesk."""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from orbit.protocol import (
    AddWorkspace,
    AgentsChanged,
    Catalog,
    Command,
    CommandAccepted,
    CommandRefused,
    DirectoryListing,
    ErrorOccurred,
    Event,
    ListDirectory,
    RemoveWorkspace,
    SessionStateChanged,
    SessionSummary,
    Snapshot,
    Watch,
    WorkspaceCloning,
    WorkspaceEntry,
)
from orbit.tui.view.confirm import Question
from orbit.tui.view.explorer import Explorer
from orbit.tui.view.navigation import Navigation, Views
from orbit.tui.view.opening import Opening
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.place import Answered, Chosen
from orbit.tui.view.session_picker import SessionChange, SessionChoice, SessionPicker, StartupPick
from orbit.tui.view.sessions import Viewing
from orbit.tui.view.typing_list import Label
from orbit.tui.view.workspace_list import Picked
from orbit.tui.view.workspace_picker import Explored, WorkspacePicker
from tests.desk_fakes import Backend

Pending = Callable[[], Awaitable[object]]


class ListView:
    """What a picker shows, as it shows it: the highlight is set as a test moves it. Closing it
    hands what it closes with to ``close``."""

    def __init__(self, modals: Modals) -> None:
        self._modals = modals
        self.is_attached = True
        self.is_mounted = False
        self.is_active = True
        self.highlighted: int | None = None
        self.keys: list[str | None] = []
        self.rows: list[str] = []
        self.hint = ""
        self.note: str | None = None
        self.box: str | None = None  # The text box's text, while it's shown.
        self.box_note = ""
        self.quit_count = 0
        self.closed: list[object] = []

    def show_rows(
        self, keys: Sequence[str | None], labels: Sequence[Label], highlight: int | None
    ) -> None:
        self.keys, self.rows = list(keys), [str(label) for label in labels]
        self.highlighted = highlight

    def show_hint(self, hint: str) -> None:
        self.hint = hint

    def show_note(self, note: str | None) -> None:
        self.note = note

    def mount(self, keys: Sequence[str | None], labels: Sequence[Label], highlight: int) -> None:
        """Compose the list, as its screen does once on screen."""
        self.is_mounted = True
        self.show_rows(keys, labels, highlight)

    def show_box(self, shown: bool, text: str) -> None:
        self.box, self.box_note = (text if shown else None), ""

    def show_box_note(self, note: str) -> None:
        self.box_note = note

    def quit(self) -> None:
        self.quit_count += 1


class SessionPickerView(ListView):
    """A session picker's fake screen, whose changes go to the backend; a note shown before
    it's on screen shows once it is, as the screen's does."""

    def __init__(self, modals: Modals) -> None:
        super().__init__(modals)
        self._pending_note: str | None = None

    def show_note(self, note: str | None) -> None:
        if self.is_mounted:
            self.note = note
        else:
            self._pending_note = note

    def mount(self, keys: Sequence[str | None], labels: Sequence[Label], highlight: int) -> None:
        super().mount(keys, labels, highlight)
        self.note = self._pending_note

    def dismiss(self, result: tuple[SessionChoice, ...]) -> None:
        self.is_attached = False
        self.closed.append(result)
        self._modals.close_sessions(result)

    def confirm(self, title: str, message: str, yes: str, then: Any) -> None:
        self._modals.confirms.append((title, message, yes, then))

    async def manage(self, change: SessionChange, awaited: Awaited) -> object:
        return await self._modals.backend.desk.send(change, awaited)


class WorkspacePickerView(ListView):
    """A workspace picker's fake screen, whose explorer holds the real explorer's model."""

    def dismiss(self, result: str | None) -> None:
        self.is_attached = False
        self.closed.append(result)
        self._modals.close_workspaces(result)

    def explore(self, explored: Explored) -> None:
        self._modals.open_explorer(explored)

    def ask(self, question: Question, answered: Answered) -> None:
        self._modals.questions.append((question, answered))

    async def change(
        self, command: AddWorkspace | RemoveWorkspace | Watch | ListDirectory, awaited: Awaited
    ) -> object:
        return await self._modals.backend.desk.send(command, awaited)


class ExplorerView:
    """The explorer's fake screen: what it shows, and what the user has done in it."""

    def __init__(self, modals: Modals) -> None:
        self._modals = modals
        self.is_attached = True
        self.is_active = True
        self.highlighted: int | None = None
        self.field = ""
        self.title = ""
        self.rows: list[str] = []
        self.note: str | None = None

    def show_heading(self, title: str, more: str | None) -> None:
        self.title = title

    def show_rows(self, keys: Sequence[str], labels: Sequence[str], highlight: int | None) -> None:
        self.rows, self.highlighted = list(labels), highlight

    def show_note(self, note: str | None) -> None:
        self.note = note

    def set_field(self, text: str) -> None:
        self.field = text

    def show_field_note(self, note: str) -> None:
        """Show nothing: no flow here reads the field's hint."""

    def show_candidates(self, candidates: Sequence[str]) -> None:
        """Show nothing: no flow here reads the candidates."""

    def dismiss(self, result: str | AddWorkspace | None) -> None:
        self.is_attached = False
        self._modals.close_explorer(result)


class Modals:
    """The modals a navigation opens, one of each kind at a time, holding the real models in
    fake screens; what a modal closes with is handed on, and what a screen does once mounted
    is done, only once :meth:`settle` runs, as Textual does both later. ``moves`` counts the
    frontend's moves."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.sessions: SessionPicker | None = None
        self.sessions_view: SessionPickerView | None = None
        self.workspaces: WorkspacePicker | None = None
        self.workspaces_view: WorkspacePickerView | None = None
        self.explorer: Explorer | None = None
        self.explorer_view: ExplorerView | None = None
        self.offers: list[tuple[Question, Answered]] = []
        self.confirms: list[tuple[str, str, str, Any]] = []
        self.questions: list[tuple[Question, Answered]] = []
        self.moves = 0
        self._chosen: Chosen | None = None
        self._picked: Picked | None = None
        self._explored: Explored | None = None
        self._pending: list[Pending] = []

    async def settle(self) -> None:
        """Hand on what the modals closed with, and do what their screens do once mounted."""
        while self._pending:
            await self._pending.pop(0)()

    # The session picker and the offer to activate, as a Place opens them.

    @property
    def picker(self) -> SessionPicker | None:
        return self.sessions

    def open_picker(
        self,
        sessions: Sequence[SessionSummary],
        viewing: Viewing,
        startup: StartupPick | None,
        chosen: Chosen,
    ) -> SessionPicker:
        view = SessionPickerView(self)
        picker = SessionPicker(view, sessions, viewing, startup)
        self.sessions, self.sessions_view, self._chosen = picker, view, chosen

        async def mounted() -> None:
            view.mount(picker.keys, picker.labels, 0)

        self._pending.append(mounted)
        return picker

    def close_sessions(self, result: tuple[SessionChoice, ...] | None) -> None:
        chosen = self._chosen
        assert chosen is not None  # Only a picker shown closes.
        self.sessions = self.sessions_view = self._chosen = None
        self._pending.append(lambda: chosen(result))

    @property
    def offer_open(self) -> bool:
        return bool(self.offers)

    def open_offer(self, question: Question, answered: Answered) -> None:
        self.offers.append((question, answered))

    def close_offer(self) -> None:
        self.offers.clear()

    async def answer_offer(self, index: int | None) -> None:
        """Answer the offer shown with the choice at ``index``, or none, as escape doesn't."""
        _, answered = self.offers.pop()
        await answered(index)

    def moved(self) -> None:
        self.moves += 1

    # The workspace picker and its explorer, as a WorkspaceList opens them.

    def workspace_picker_open(self) -> bool:
        return self.workspaces is not None

    def open_workspaces(
        self,
        workspaces: tuple[WorkspaceEntry, ...],
        repository: str | None,
        startup: bool,
        picked: Picked,
    ) -> WorkspacePicker:
        view = WorkspacePickerView(self)
        picker = WorkspacePicker(view, workspaces, repository, startup)
        self.workspaces, self.workspaces_view, self._picked = picker, view, picked

        async def mounted() -> None:
            view.mount(picker.keys, picker.labels, picker.preselected)
            await picker.start()

        self._pending.append(mounted)
        return picker

    def close_workspaces(self, result: str | None) -> None:
        picked = self._picked
        assert picked is not None  # Only a picker shown closes.
        self.workspaces = self.workspaces_view = self._picked = None
        self.explorer = self.explorer_view = None
        self._pending.append(lambda: picked(result))

    def open_explorer(self, explored: Explored) -> None:
        view = ExplorerView(self)
        self.explorer = Explorer(view, self.backend.desk.send)
        self.explorer_view, self._explored = view, explored
        self._pending.append(self.explorer.start)

    def close_explorer(self, result: str | AddWorkspace | None) -> None:
        explored = self._explored
        assert explored is not None  # Only an explorer shown closes.
        self.explorer = self.explorer_view = self._explored = None
        self._pending.append(lambda: explored(result))

    def show_listing(self, listing: DirectoryListing) -> None:
        if self.explorer is not None:
            self.explorer.show_listing(listing)


class _WorkspaceViews:
    """The workspace list's view: the workspace picker of :class:`Modals`."""

    def __init__(self, modals: Modals) -> None:
        self._modals = modals

    @property
    def picker(self) -> WorkspacePicker | None:
        return self._modals.workspaces

    def open_picker(
        self,
        workspaces: tuple[WorkspaceEntry, ...],
        repository: str | None,
        startup: bool,
        picked: Picked,
    ) -> WorkspacePicker:
        return self._modals.open_workspaces(workspaces, repository, startup, picked)

    def show_listing(self, listing: DirectoryListing) -> None:
        self._modals.show_listing(listing)


def navigating(
    opening: Opening | None = None, linked: bool = False
) -> tuple[Navigation, Modals, Backend]:
    """A navigation opening as ``opening`` says, over a link if ``linked``, with its modals and
    the backend it sends to."""
    backend = Backend()
    modals = Modals(backend)
    views = Views(modals, _WorkspaceViews(modals))
    navigation = Navigation(views, backend.desk.send, opening or Opening(), linked)
    return navigation, modals, backend


async def feed(navigation: Navigation, backend: Backend, *events: Event) -> None:
    """Have ``navigation`` follow ``events`` as one batch, as the app's router does, with the
    answers to commands going to the desk, then hear the batch was listed."""
    for event in events:
        _follow(navigation, backend, event)
    await navigation.listed(events)


_FOLLOWERS: dict[type[Event], Callable[[Navigation, Any], None]] = {
    Catalog: Navigation.follow_catalog,
    SessionStateChanged: Navigation.follow_state,
    Snapshot: Navigation.view,
    WorkspaceCloning: Navigation.follow_cloning,
    ErrorOccurred: Navigation.follow_error,
    DirectoryListing: Navigation.follow_listing,
    AgentsChanged: Navigation.follow_agents,
}
"""How ``navigation`` follows each kind of event the router hands it."""


def _follow(navigation: Navigation, backend: Backend, event: Event) -> None:
    """Have ``navigation`` follow ``event`` as the router hands it on, and the desk the answers
    to commands."""
    if isinstance(event, CommandAccepted | CommandRefused):
        backend.desk.answer(event)
        return
    follow = _FOLLOWERS.get(type(event))
    assert follow is not None, f"Not a navigation event: {event}"
    follow(navigation, event)


def changes(backend: Backend) -> list[Command]:
    """The commands sent, but for the streams the workspace picker watches."""
    return [command for command in backend.commands if not isinstance(command, Watch)]


def accept_change(backend: Backend) -> None:
    """Accept the last command sent but for watching streams, which the workspace picker sends
    as it opens and closes."""
    index = max(i for i, sent in enumerate(backend.sent) if not isinstance(sent.payload, Watch))
    backend.accept(index)


async def fed(navigation: Navigation, modals: Modals, backend: Backend, *events: Event) -> None:
    """Feed ``events`` as one batch, then let the modals settle, as the app does before a test
    looks again."""
    await feed(navigation, backend, *events)
    await modals.settle()
