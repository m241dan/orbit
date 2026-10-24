"""The picker of the workspaces a control plane serves, without the screen showing it: which rows
it lists, picking, adding and removing workspaces, and the clones under way."""

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from orbit.protocol import (
    CONTROL_PLANE_STREAM,
    AddWorkspace,
    AgentRef,
    ListDirectory,
    RemoveWorkspace,
    Watch,
    WorkspaceCloning,
    WorkspaceEntry,
)
from orbit.tui.view.confirm import Question
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.typing_list import TypingList, TypingListView
from orbit.tui.view.workspaces import (
    ADD_PATH,
    CANCEL_INDEX,
    PATH_HINT,
    REMOVE_MESSAGE,
    WORKSPACES_HINTS,
    WorkspaceRow,
    add_command,
    added_workspace,
    preselected,
    removal,
    remove_choices,
    remove_title,
    row_key,
    row_label,
    workspace_rows,
)

Change = Callable[
    [AddWorkspace | RemoveWorkspace | Watch | ListDirectory, Awaited], Awaitable[object]
]
"""Sends adding or removing a workspace, the streams to watch, or listing a directory, to the
backend, awaiting its outcome as told; whatever it returns is ignored."""
Explored = Callable[[str | AddWorkspace | None], Awaitable[None]]
"""Takes what the explorer closed with: the id of a workspace to open, the ``AddWorkspace``
accepted, or none."""
Answered = Callable[[int | None], Awaitable[None]]
"""Takes the index of the choice picked, or none."""


class WorkspacePickerView(TypingListView[str | None], Protocol):
    """What the workspace picker shows, the screens it opens over itself, and where it sends
    its commands."""

    async def change(
        self, command: AddWorkspace | RemoveWorkspace | Watch | ListDirectory, awaited: Awaited
    ) -> object:
        """Send ``command`` to the backend, awaiting its outcome as told."""
        ...

    @property
    def is_active(self) -> bool:
        """Whether no other screen is over the picker."""
        ...

    def explore(self, explored: Explored) -> None:
        """Open the explorer over the server's directories, calling ``explored`` with what it
        closes with."""
        ...

    def ask(self, question: Question, answered: Answered) -> None:
        """Ask ``question``, calling ``answered`` with the index of the choice picked."""
        ...


class WorkspacePicker(TypingList[str | None]):
    """Lists ``workspaces``, offering first to add the ``repository`` the frontend runs in while
    none is at it, then the clones :attr:`cloning`, then to add a path or URL; closes with the id
    of the workspace picked.

    Picking adds the repository, or opens the explorer, which may open a
    workspace, picking it, or add one; either way the workspace added by its path is picked once
    accepted and listed. A refusal, or the clone of a URL typed in the path box failing, shows
    why. Removing asks whether to keep the highlighted workspace's sessions or delete them too,
    then sends ``RemoveWorkspace``, showing why if it's refused. Once started it watches the
    control-plane stream, so each workspace's row can show the :attr:`agents` running in it.
    """

    HINTS = WORKSPACES_HINTS

    def __init__(
        self,
        view: WorkspacePickerView,
        workspaces: Sequence[WorkspaceEntry],
        repository: str | None,
        startup: bool = False,
    ) -> None:
        super().__init__(view, None, startup)
        self._picker_view = view
        self.workspaces = tuple(workspaces)
        self.cloning: tuple[WorkspaceCloning, ...] = ()  # The clones under way, as shown.
        self.agents: tuple[AgentRef, ...] = ()  # The agents running, as shown.
        self.repository = repository
        self._adding: str | None = None  # The path added, until its workspace is listed.
        self._cloning: str | None = None  # The URL added, until its clone fails or another is.

    @property
    def rows(self) -> tuple[WorkspaceRow, ...]:
        """The rows listed, in order."""
        return workspace_rows(self.workspaces, self.repository, self.cloning)

    @property
    def keys(self) -> tuple[str | None, ...]:
        """The rows' keys, None for ``Add a path or URL…``."""
        return tuple(map(row_key, self.rows))

    @property
    def labels(self) -> tuple[str, ...]:
        """What the rows show, in order."""
        return tuple(row_label(row, self.agents) for row in self.rows)

    @property
    def preselected(self) -> int:
        """The row highlighted as the picker opens: the workspace at the repository, else the
        first."""
        return preselected(self.rows, self.repository)

    async def start(self) -> None:
        """Watch the control-plane stream, for the agents running."""
        await self._picker_view.change(Watch(streams=(CONTROL_PLANE_STREAM,)), Awaited())

    def show(
        self, workspaces: Sequence[WorkspaceEntry], cloning: Sequence[WorkspaceCloning] = ()
    ) -> None:
        """List ``workspaces`` and the clones ``cloning`` in place of those listed, keeping the
        highlight on the row it was on, or where it was if that one's gone; picks the workspace
        being added once listed."""
        shown = self.keys
        self.workspaces = tuple(workspaces)
        self.cloning = tuple(cloning)
        if not self._pick_added() and self._view.is_mounted:
            self._relist(shown, self.keys, self.labels)

    def show_agents(self, agents: Sequence[AgentRef]) -> None:
        """Show ``agents`` running in place of those shown, keeping the highlight."""
        self.agents = tuple(agents)
        if self._view.is_mounted:
            self._relist(self.keys, self.keys, self.labels)

    def _pick_added(self) -> bool:
        """Whether the workspace being added is listed, and picked, unless another screen is
        over the picker."""
        adding = self._adding
        if adding is None or not self._picker_view.is_active:
            return False
        entry = added_workspace(self.workspaces, adding)
        if entry is None:
            return False
        self._adding = None
        self._view.dismiss(entry.id)
        return True

    async def choose(self, index: int) -> None:
        """Pick row ``index``: open its workspace, add the repository, or open the explorer."""
        row = self.rows[index]
        if isinstance(row, WorkspaceEntry):
            self._view.dismiss(row.id)
        elif isinstance(row, AddWorkspace):
            await self._add(row)
        elif row == ADD_PATH:
            self._picker_view.explore(self._explored)

    async def _explored(self, pick: str | AddWorkspace | None) -> None:
        """Pick the workspace the explorer opened, or the one it added once it's listed."""
        if isinstance(pick, AddWorkspace):
            self._added(pick)
        elif pick is not None:
            self._view.dismiss(pick)

    async def typed(self, text: str) -> None:
        """Add the path or URL typed in the path box, ``text``, if any."""
        text = text.strip()
        if text:
            await self._add(add_command(text))

    async def _add(self, adding: AddWorkspace) -> None:
        """Send ``adding``, picking the workspace added by its path once it's accepted and
        listed."""
        self._view.show_note(None)
        awaited = Awaited(accepted=lambda: self._added(adding), why=self._refused)
        await self._picker_view.change(adding, awaited)

    def _added(self, adding: AddWorkspace) -> None:
        """Go back to the list now ``adding`` is accepted, unless the picker is closed: pick the
        workspace added by its path once it's listed, or await the clone of its URL, which its
        row shows."""
        if not self._view.is_attached:
            return
        self._type(None)
        if adding.url is not None:
            self._cloning = adding.url
            return
        self._adding = adding.path
        self._pick_added()

    def type_path(self, text: str = "") -> None:
        """Type a path or URL to add in the path box, starting from ``text``."""
        self._type(PATH_HINT, text)

    def clone_failed(self, url: str, message: str) -> None:
        """Show ``message``, why the clone of ``url`` failed, in the path box's hint, with the
        URL back in the box to change, if it's the clone this picker asked for."""
        if url != self._cloning:
            return
        self._cloning = None
        self.type_path(url)
        self._view.show_box_note(message)

    def remove(self) -> None:
        """Remove the highlighted workspace, keeping its sessions or deleting them too as the
        user chooses, unless they cancel; other rows remove nothing."""
        row = self.rows[self._view.highlighted or 0]
        if not isinstance(row, WorkspaceEntry):
            return

        async def chosen(index: int | None) -> None:
            removing = None if index is None else removal(row, index)
            if removing is not None:
                self._view.show_note(None)
                await self._picker_view.change(removing, Awaited(why=self._refused))

        question = Question(remove_title(row), REMOVE_MESSAGE, remove_choices(row), CANCEL_INDEX)
        self._picker_view.ask(question, chosen)

    def must_pick(self) -> None:
        """Have escape quit orbit, as at startup, now there's nothing to go back to."""
        self.startup = True
        self._view.show_hint(self.hint)
