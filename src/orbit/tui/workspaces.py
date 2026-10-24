"""The modal where the user picks a workspace the control plane serves, adds one or removes
one."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Input, OptionList

from orbit.protocol import (
    AddWorkspace,
    AgentRef,
    AgentsChanged,
    DirectoryListing,
    ErrorOccurred,
    ListDirectory,
    RemoveWorkspace,
    Watch,
    WorkspaceCloning,
    WorkspaceEntry,
)
from orbit.tui.catalog import CatalogState
from orbit.tui.explorer import ExplorerScreen
from orbit.tui.modal import ModalSlot, Push
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.workspace_picker import Answered, Change, Explored, Question, WorkspacePicker
from orbit.tui.view.workspaces import (
    PATH_PLACEHOLDER,
    WORKSPACES_TITLE,
    with_clone,
    without_clone,
)
from orbit.tui.widgets import ChoiceScreen, TypingListScreen

Chosen = Callable[[str, bool], Awaitable[None]]
"""Takes the id of the workspace picked, and whether it was picked at startup."""


@dataclass(frozen=True)
class Served:
    """The workspaces the control plane serves, as the ``catalog`` lists them, to the frontend
    running in the ``repository``, if any."""

    catalog: CatalogState
    repository: str | None = None


class WorkspacesScreen(TypingListScreen[str | None]):
    """Shows the :class:`WorkspacePicker` of ``workspaces``, dismissing with the id of the
    workspace picked.

    ``j``/``k`` (and the arrows) move, enter picks the highlighted row, and enter in the path
    box adds the path or URL typed; ``x`` removes the highlighted workspace, asking how first.
    Escape leaves the path box, or dismisses with none, or quits orbit at startup. The explorer
    it opens is shown the directories listed.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("x", "remove", show=False),
    ]
    picker: WorkspacePicker

    def __init__(
        self,
        workspaces: Sequence[WorkspaceEntry],
        repository: str | None,
        change: Change,
        startup: bool = False,
    ) -> None:
        super().__init__()
        self.picker = WorkspacePicker(self, workspaces, repository, startup)
        self._change = change
        self._explorer: ExplorerScreen | None = None  # The explorer opened from here, if open.

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = WORKSPACES_TITLE
            box.border_subtitle = self.hint
            yield OptionList(*self._options(self.picker.keys, self.picker.labels))
            yield self._note_line()
            path = Input(placeholder=PATH_PLACEHOLDER, id="workspace-path")
            path.display = False
            yield path

    async def on_mount(self) -> None:
        self.query_one(OptionList).highlighted = self.picker.preselected
        await self.picker.start()

    def show(
        self, workspaces: Sequence[WorkspaceEntry], cloning: Sequence[WorkspaceCloning] = ()
    ) -> None:
        """List ``workspaces`` and the clones ``cloning``, as the picker takes them."""
        self.picker.show(workspaces, cloning)

    def show_agents(self, agents: Sequence[AgentRef]) -> None:
        """Show ``agents`` running, as the picker takes them."""
        self.picker.show_agents(agents)

    def clone_failed(self, url: str, message: str) -> None:
        """Show why the clone of ``url`` failed, as the picker takes it."""
        self.picker.clone_failed(url, message)

    def must_pick(self) -> None:
        """Have escape quit orbit, as at startup."""
        self.picker.must_pick()

    def show_listing(self, listing: DirectoryListing) -> None:
        """Show ``listing`` in the explorer, if it's open."""
        if self._explorer is not None:
            self._explorer.show_listing(listing)

    def explore(self, explored: Explored) -> None:
        explorer = ExplorerScreen(self._change)
        self._explorer = explorer

        async def closed(pick: str | AddWorkspace | None) -> None:
            self._explorer = None
            await explored(pick)

        self.app.push_screen(explorer, closed)

    async def change(
        self, command: AddWorkspace | RemoveWorkspace | Watch | ListDirectory, awaited: Awaited
    ) -> object:
        return await self._change(command, awaited)

    def ask(self, question: Question, answered: Answered) -> None:
        screen = ChoiceScreen(
            question.title, question.message, question.choices, escape_index=question.escape
        )
        self.app.push_screen(screen, answered)

    @on(OptionList.OptionSelected)
    async def _choose(self, event: OptionList.OptionSelected) -> None:
        index = self._picked_row(event, self.picker.keys)
        if index is not None:
            await self.picker.choose(index)

    @on(Input.Submitted, "#workspace-path")
    async def _typed(self, event: Input.Submitted) -> None:
        event.stop()
        await self.picker.typed(event.value)

    def action_remove(self) -> None:
        self.picker.remove()


class WorkspacesHost:
    """Shows the workspaces ``served`` with ``push`` when asked, refreshing the picker while
    it's shown, and the clones under way, as ``WorkspaceCloning`` reports them, until they're
    served or fail or the link is back, and the agents running, as ``AgentsChanged`` reports
    them while the picker watches for them, and the directories ``DirectoryListing`` lists, in
    its explorer. ``change`` sends adding and removing workspaces, the streams to watch and
    listing directories, and ``chosen`` takes the workspace picked."""

    def __init__(
        self,
        push: Push[WorkspacesScreen, str],
        change: Change,
        chosen: Chosen,
        served: Served,
    ) -> None:
        self._change = change
        self._catalog = served.catalog
        self._chosen = chosen
        self._repository = served.repository
        self.cloning: tuple[WorkspaceCloning, ...] = ()
        self._picker = ModalSlot[WorkspacesScreen, str](push)

    @property
    def shown(self) -> WorkspacesScreen | None:
        """The picker shown, if any."""
        return self._picker.showing

    @property
    def workspaces(self) -> tuple[WorkspaceEntry, ...]:
        """The workspaces the catalog lists."""
        return self._catalog.workspaces

    def follow(self) -> None:
        """Follow the catalog's latest listing, or a session's state changing in it."""
        self._refresh()

    def follow_cloning(self, progress: WorkspaceCloning) -> None:
        """Note how far the clone ``progress`` is about has got, dropping it once it's served."""
        self.cloning = with_clone(self.cloning, progress)
        self._refresh()

    def follow_error(self, error: ErrorOccurred) -> None:
        """Drop the clone ``error`` says failed, if it names one by its URL, whatever its code,
        telling the picker shown."""
        if error.url is None:
            return
        self.cloning = without_clone(self.cloning, error.url)
        self._refresh()
        if self.shown is not None:
            self.shown.clone_failed(error.url, error.message)

    def reconnected(self) -> None:
        """Drop every clone, now the link to the control plane is back, as clones under way
        don't survive the control plane stopping."""
        self.cloning = ()
        self._refresh()

    def follow_listing(self, listing: DirectoryListing) -> None:
        """Show ``listing`` in the picker's explorer, if it's shown."""
        if self.shown is not None:
            self.shown.show_listing(listing)

    def follow_agents(self, changed: AgentsChanged) -> None:
        """Show the agents ``changed`` lists in the picker, if it's shown."""
        if self.shown is not None:
            self.shown.show_agents(changed.agents)

    def open(self, startup: bool = False) -> None:
        """Let the user pick a workspace, add one or remove one, unless the picker is shown
        already; at ``startup``, or once the app has lost its workspace since, escape quits and
        the workspace picked is taken as picked at startup. The picker watches the control-plane
        stream while it's shown, and stops once it's closed."""
        if self.shown is not None:
            return
        screen = WorkspacesScreen(self.workspaces, self._repository, self._change, startup)
        screen.picker.cloning = self.cloning

        async def picked(workspace: str | None) -> None:
            await self._change(Watch(streams=()), Awaited())
            if workspace is not None:
                await self._chosen(workspace, screen.startup)

        self._picker.open(screen, picked)

    def lost(self) -> None:
        """Let the user pick a workspace now the app is on none: open the picker as at startup,
        or have escape quit the one shown, as closing it would leave the app on none."""
        if self.shown is not None:
            self.shown.must_pick()
            return
        self.open(startup=True)

    def _refresh(self) -> None:
        """Show the workspaces in the picker, if it's shown."""
        if self.shown is not None:
            self.shown.show(self.workspaces, self.cloning)
