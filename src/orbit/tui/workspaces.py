"""The modal where the user picks a workspace the control plane serves, adds one or removes
one."""

from collections.abc import Sequence
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Input, OptionList

from orbit.protocol import (
    AddWorkspace,
    DirectoryListing,
    ListDirectory,
    RemoveWorkspace,
    Watch,
    WorkspaceEntry,
)
from orbit.tui.explorer import ExplorerScreen
from orbit.tui.modal import ModalSlot, Push
from orbit.tui.view.confirm import Question
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.workspace_list import Picked
from orbit.tui.view.workspace_picker import Answered, Change, Explored, WorkspacePicker
from orbit.tui.view.workspaces import (
    PATH_PLACEHOLDER,
    WORKSPACES_TITLE,
)
from orbit.tui.widgets import ChoiceScreen, TypingListScreen


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
    """Shows the workspace picker a :class:`WorkspaceList` asks for, one at a time, with
    ``push``, its changes sent with ``change``, and the directories listed in its explorer."""

    def __init__(self, push: Push[WorkspacesScreen, str], change: Change) -> None:
        self._change = change
        self._picker = ModalSlot[WorkspacesScreen, str](push)

    @property
    def shown(self) -> WorkspacesScreen | None:
        """The picker shown, if any."""
        return self._picker.showing

    @property
    def picker(self) -> WorkspacePicker | None:
        shown = self._picker.showing
        return None if shown is None else shown.picker

    def open_picker(
        self,
        workspaces: tuple[WorkspaceEntry, ...],
        repository: str | None,
        startup: bool,
        picked: Picked,
    ) -> WorkspacePicker:
        screen = WorkspacesScreen(workspaces, repository, self._change, startup)
        self._picker.open(screen, picked)
        return screen.picker

    def show_listing(self, listing: DirectoryListing) -> None:
        shown = self._picker.showing
        if shown is not None:
            shown.show_listing(listing)
