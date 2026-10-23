"""The modal where the user picks a workspace the control plane serves, adds one or removes
one."""

from collections.abc import Awaitable, Callable, Sequence
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static

from orbit.protocol import (
    CONTROL_PLANE_STREAM,
    AddWorkspace,
    AgentRef,
    AgentsChanged,
    Catalog,
    DirectoryListing,
    ErrorOccurred,
    ListDirectory,
    RemoveWorkspace,
    SessionStateChanged,
    Watch,
    WorkspaceCloning,
    WorkspaceEntry,
)
from orbit.tui.explorer import ExplorerScreen
from orbit.tui.modal import ModalSlot, Push
from orbit.tui.view.common import kept_highlight
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.workspaces import (
    ADD_PATH,
    CANCEL_INDEX,
    PATH_HINT,
    PATH_PLACEHOLDER,
    REMOVE_MESSAGE,
    WORKSPACES_HINTS,
    WORKSPACES_TITLE,
    WorkspaceRow,
    add_command,
    added_workspace,
    clone_failed,
    clones_left,
    preselected,
    removal,
    remove_choices,
    remove_title,
    row_key,
    row_label,
    with_clone,
    with_states,
    workspace_rows,
)
from orbit.tui.widgets import ChoiceScreen, TypingListScreen

Change = Callable[
    [AddWorkspace | RemoveWorkspace | Watch | ListDirectory, Awaited], Awaitable[object]
]
"""Sends adding or removing a workspace, the streams to watch, or listing a directory, to the
backend, awaiting its outcome as told; whatever it returns is ignored."""
Chosen = Callable[[str, bool], Awaitable[None]]
"""Takes the id of the workspace picked, and whether it was picked at startup."""


class WorkspacesScreen(TypingListScreen[str | None]):
    """Lists ``workspaces``, offering first to add the ``repository`` the frontend runs in while
    none is at it, then the clones :attr:`cloning`, then to add a path or URL; dismisses with the id
    of the workspace picked.

    ``j``/``k`` (and the arrows) move, preselected on the workspace at ``repository``, and
    enter picks the highlighted workspace. Enter on adding the repository sends
    ``AddWorkspace`` with ``change``, and on ``Add a path or URL…`` opens the explorer over the
    server's directories, which may open a workspace, picking it, or add one; either way the
    workspace added by its path is picked once accepted and listed. A refusal, or the clone of
    a URL typed in the path box failing, shows why. ``x`` asks whether to remove the highlighted
    workspace keeping its sessions or deleting them too, then sends ``RemoveWorkspace``,
    showing why if it's refused.
    Escape leaves the path box, or dismisses with none, or quits orbit at ``startup``, where
    there's nothing to go back to. Once shown it watches the control-plane stream, so each
    workspace's row can show the :attr:`agents` running in it.
    """

    DEFAULT_CSS = """
    WorkspacesScreen #workspace-note { color: $error; }
    """
    HINTS = WORKSPACES_HINTS
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("x", "remove", show=False),
    ]

    def __init__(
        self,
        workspaces: Sequence[WorkspaceEntry],
        repository: str | None,
        change: Change,
        startup: bool = False,
    ) -> None:
        super().__init__(None, startup)
        self.workspaces = tuple(workspaces)
        self.cloning: tuple[WorkspaceCloning, ...] = ()  # The clones under way, as shown.
        self.agents: tuple[AgentRef, ...] = ()  # The agents running, as shown.
        self.repository = repository
        self._change = change
        self._adding: str | None = None  # The path added, until its workspace is listed.
        self._cloning: str | None = None  # The URL added, until its clone fails or another is.
        self._explorer: ExplorerScreen | None = None  # The explorer opened from here, if open.

    @property
    def rows(self) -> tuple[WorkspaceRow, ...]:
        """The rows listed, in order."""
        return workspace_rows(self.workspaces, self.repository, self.cloning)

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = WORKSPACES_TITLE
            box.border_subtitle = self.hint
            yield OptionList(*self._labels)
            note = Static("", id="workspace-note")
            note.display = False
            yield note
            path = Input(placeholder=PATH_PLACEHOLDER, id="workspace-path")
            path.display = False
            yield path

    async def on_mount(self) -> None:
        self.query_one(OptionList).highlighted = preselected(self.rows, self.repository)
        await self._change(Watch(streams=(CONTROL_PLANE_STREAM,)), Awaited())

    @property
    def _labels(self) -> tuple[str, ...]:
        """What the rows show, in order."""
        return tuple(row_label(row, self.agents) for row in self.rows)

    @property
    def note(self) -> str | None:
        """Why removing or adding a workspace was refused, while that's shown."""
        note = self.query_one("#workspace-note", Static)
        return str(note.render()) if note.display else None

    def show(
        self, workspaces: Sequence[WorkspaceEntry], cloning: Sequence[WorkspaceCloning] = ()
    ) -> None:
        """List ``workspaces`` and the clones ``cloning`` in place of those listed, keeping the
        highlight on the row it was on, or where it was if that one's gone; picks the workspace
        being added once listed."""
        shown = self._keys
        self.workspaces = tuple(workspaces)
        self.cloning = tuple(cloning)
        if not self._pick_added() and self.is_mounted:
            self._relist(shown)

    def show_agents(self, agents: Sequence[AgentRef]) -> None:
        """Show ``agents`` running in place of those shown, keeping the highlight."""
        self.agents = tuple(agents)
        if self.is_mounted:
            self._relist(self._keys)

    def _relist(self, shown: Sequence[str]) -> None:
        """List the rows again in place of rows with keys ``shown``, keeping the highlight on
        the row it was on, or where it was if that one's gone."""
        options = self.query_one(OptionList)
        highlighted = options.highlighted
        options.clear_options()
        options.add_options(self._labels)
        options.highlighted = kept_highlight(shown, highlighted, self._keys)

    @property
    def _keys(self) -> tuple[str, ...]:
        """The rows' keys."""
        return tuple(map(row_key, self.rows))

    def _pick_added(self) -> bool:
        """Whether the workspace being added is listed, and picked, unless another screen is
        over this one."""
        adding = self._adding
        if adding is None or not self.is_active:
            return False
        entry = added_workspace(self.workspaces, adding)
        if entry is None:
            return False
        self._adding = None
        self.dismiss(entry.id)
        return True

    @on(OptionList.OptionSelected)
    async def _choose(self, event: OptionList.OptionSelected) -> None:
        row = self.rows[event.option_index]
        if isinstance(row, WorkspaceEntry):
            self.dismiss(row.id)
        elif isinstance(row, AddWorkspace):
            await self._add(row)
        elif row == ADD_PATH:
            self._explore()

    def _explore(self) -> None:
        """Open the explorer, picking the workspace it opens or adds."""
        explorer = ExplorerScreen(self._change)
        self._explorer = explorer

        async def explored(pick: str | AddWorkspace | None) -> None:
            self._explorer = None
            if isinstance(pick, AddWorkspace):
                self._added(pick)
            elif pick is not None:
                self.dismiss(pick)

        self.app.push_screen(explorer, explored)

    def show_listing(self, listing: DirectoryListing) -> None:
        """Show ``listing`` in the explorer, if it's open."""
        if self._explorer is not None:
            self._explorer.show_listing(listing)

    @on(Input.Submitted, "#workspace-path")
    async def _typed(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value.strip()
        if text:
            await self._add(add_command(text))

    async def _add(self, adding: AddWorkspace) -> None:
        """Send ``adding``, picking the workspace added by its path once it's accepted and
        listed."""
        self._show_note(None)
        awaited = Awaited(accepted=lambda: self._added(adding), why=self._refused)
        await self._change(adding, awaited)

    def _added(self, adding: AddWorkspace) -> None:
        """Go back to the list now ``adding`` is accepted, unless this screen is gone: pick the
        workspace added by its path once it's listed, or await the clone of its URL, which its
        row shows."""
        if not self.is_attached:
            return
        self._typing(None)
        if adding.url is not None:
            self._cloning = adding.url
            return
        self._adding = adding.path
        self._pick_added()

    def clone_failed(self, message: str) -> None:
        """Show ``message``, why a clone failed, in the path box's hint, with the URL
        back in the box to change, if it's the clone this picker asked for."""
        url = self._cloning
        if url is None or not clone_failed(message, url):
            return
        self._cloning = None
        self._typing(PATH_HINT, url)
        self.query_one(Input).border_subtitle = message

    def _refused(self, why: str) -> None:
        """Show ``why`` adding or removing a workspace was refused: in the path box's hint while
        it's shown, or else under the list; nothing once this screen is gone."""
        if not self.is_attached:
            return
        path = self.query_one(Input)
        if path.display:
            path.border_subtitle = why
        else:
            self._show_note(why)

    def action_remove(self) -> None:
        """Remove the highlighted workspace, keeping its sessions or deleting them too as the
        user chooses, unless they cancel; other rows remove nothing."""
        row = self.rows[self.query_one(OptionList).highlighted or 0]
        if not isinstance(row, WorkspaceEntry):
            return

        async def chosen(index: int | None) -> None:
            removing = None if index is None else removal(row, index)
            if removing is not None:
                self._show_note(None)
                await self._change(removing, Awaited(why=self._refused))

        choices = remove_choices(row)
        screen = ChoiceScreen(remove_title(row), REMOVE_MESSAGE, choices, escape_index=CANCEL_INDEX)
        self.app.push_screen(screen, chosen)

    def must_pick(self) -> None:
        """Have escape quit orbit, as at startup, now there's nothing to go back to."""
        self.startup = True
        self._show_hint()

    def _show_note(self, note: str | None) -> None:
        """Show ``note`` under the list, or hide it while it's none."""
        shown = self.query_one("#workspace-note", Static)
        shown.update(note or "")
        shown.display = note is not None


class WorkspacesHost:
    """Keeps the workspaces the latest ``Catalog`` lists, as ``SessionStateChanged`` changes
    their sessions, and shows them with ``push`` when asked, for the ``repository`` the
    frontend runs in, refreshing the picker while it's shown, and the clones under way, as
    ``WorkspaceCloning`` reports them, until they're listed or fail or the link is back, and
    the agents running, as ``AgentsChanged`` reports them while the picker watches for them,
    and the directories ``DirectoryListing`` lists, in its explorer. ``change`` sends adding
    and removing workspaces, the streams to watch and listing directories, and ``chosen``
    takes the workspace picked."""

    def __init__(
        self,
        push: Push[WorkspacesScreen, str],
        change: Change,
        chosen: Chosen,
        repository: str | None = None,
    ) -> None:
        self._change = change
        self._chosen = chosen
        self._repository = repository
        self.workspaces: tuple[WorkspaceEntry, ...] = ()
        self.cloning: tuple[WorkspaceCloning, ...] = ()
        self._picker = ModalSlot[WorkspacesScreen, str](push)

    @property
    def shown(self) -> WorkspacesScreen | None:
        """The picker shown, if any."""
        return self._picker.showing

    def follow(self, catalog: Catalog) -> None:
        """List the workspaces in ``catalog``, in place of the clones it lists."""
        self.cloning = clones_left(self.cloning, self.workspaces, catalog.workspaces)
        self.workspaces = catalog.workspaces
        self._refresh()

    def follow_cloning(self, progress: WorkspaceCloning) -> None:
        """Note how far the clone ``progress`` is about has got."""
        self.cloning = with_clone(self.cloning, progress)
        self._refresh()

    def follow_error(self, error: ErrorOccurred) -> None:
        """Drop the clone ``error`` says failed, if it names one, whatever its code, telling the
        picker shown."""
        self.cloning = tuple(c for c in self.cloning if not clone_failed(error.message, c.url))
        self._refresh()
        if self.shown is not None:
            self.shown.clone_failed(error.message)

    def reconnected(self) -> None:
        """Drop every clone, now the link to the control plane is back, as clones under way
        don't survive the control plane stopping."""
        self.cloning = ()
        self._refresh()

    def follow_state(self, changed: SessionStateChanged) -> None:
        """Note ``changed``'s session's state."""
        self.workspaces = with_states(self.workspaces, changed)
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
        screen.cloning = self.cloning

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
