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
    AddWorkspace,
    Catalog,
    RemoveWorkspace,
    SessionStateChanged,
    WorkspaceEntry,
)
from orbit.tui.sessions import Push
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.workspaces import (
    PATH_PLACEHOLDER,
    REMOVE_TITLE,
    REMOVE_YES,
    WORKSPACES_TITLE,
    WorkspaceRow,
    added_workspace,
    preselected,
    remove_message,
    row_key,
    row_label,
    with_states,
    workspace_rows,
    workspaces_hint,
)
from orbit.tui.widgets import ConfirmScreen, ListScreen

Change = Callable[[AddWorkspace | RemoveWorkspace, Awaited], Awaitable[object]]
"""Sends adding or removing a workspace to the backend, awaiting its outcome as told; whatever
it returns is ignored."""
Chosen = Callable[[str, bool], Awaitable[None]]
"""Takes the id of the workspace picked, and whether it was picked at startup."""


class WorkspacesScreen(ListScreen[str | None]):
    """Lists ``workspaces``, offering first to add the ``repository`` the frontend runs in while
    none is at it, then to add a path; dismisses with the id of the workspace picked.

    ``j``/``k`` (and the arrows) move, preselected on the workspace at ``repository``, and
    enter picks the highlighted workspace. Enter on adding sends ``AddWorkspace`` with
    ``change``, asking for the path first on ``Add a path…``, and picks the workspace once
    accepted and listed; a refusal shows why. ``x`` asks to remove the highlighted workspace,
    then sends ``RemoveWorkspace``, showing why if it's refused. Escape leaves the path box, or
    dismisses with none, or quits orbit at ``startup``, where there's nothing to go back to.
    """

    AUTO_FOCUS = "OptionList"
    DEFAULT_CSS = """
    WorkspacesScreen { align: center middle; }
    WorkspacesScreen > Vertical {
        width: 90;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: round $accent;
        padding: 0 1;
    }
    WorkspacesScreen OptionList { height: auto; max-height: 30; border: none; }
    WorkspacesScreen #workspace-note { color: $error; }
    WorkspacesScreen Input { border: round $secondary; }
    WorkspacesScreen Input:focus { border: heavy $accent; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", show=False),
        Binding("x", "remove", show=False),
    ]

    def __init__(
        self,
        workspaces: Sequence[WorkspaceEntry],
        repository: str | None,
        change: Change,
        startup: bool = False,
    ) -> None:
        super().__init__()
        self.workspaces = tuple(workspaces)
        self.repository = repository
        self.startup = startup
        self._change = change
        self._adding: str | None = None  # The path added, until its workspace is listed.

    @property
    def rows(self) -> tuple[WorkspaceRow, ...]:
        """The rows listed, in order."""
        return workspace_rows(self.workspaces, self.repository)

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = WORKSPACES_TITLE
            box.border_subtitle = workspaces_hint(startup=self.startup, adding=False)
            yield OptionList(*map(row_label, self.rows))
            note = Static("", id="workspace-note")
            note.display = False
            yield note
            path = Input(placeholder=PATH_PLACEHOLDER, id="workspace-path")
            path.display = False
            yield path

    def on_mount(self) -> None:
        self.query_one(OptionList).highlighted = preselected(self.rows, self.repository)

    @property
    def note(self) -> str | None:
        """Why removing or adding a workspace was refused, while that's shown."""
        note = self.query_one("#workspace-note", Static)
        return str(note.render()) if note.display else None

    def show(self, workspaces: Sequence[WorkspaceEntry]) -> None:
        """List ``workspaces`` in place of those listed, keeping the highlight on the row it was
        on, or where it was if that one's gone; picks the workspace being added once listed."""
        kept = self._highlighted if self.is_mounted else None
        self.workspaces = tuple(workspaces)
        if not self._pick_added() and kept is not None:
            self._relist(kept)

    def _relist(self, kept: str) -> None:
        """List the rows again, highlighting the one whose key is ``kept``, or the row where
        the highlight was if that one's gone."""
        options = self.query_one(OptionList)
        highlighted, rows = options.highlighted or 0, self.rows
        options.clear_options()
        options.add_options(map(row_label, rows))
        keys = [row_key(row) for row in rows]
        options.highlighted = keys.index(kept) if kept in keys else min(highlighted, len(rows) - 1)

    @property
    def _highlighted(self) -> str:
        """The key of the row highlighted."""
        return row_key(self.rows[self.query_one(OptionList).highlighted or 0])

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
            await self._add(row.path)
        else:
            self._typing(True)

    @on(Input.Submitted, "#workspace-path")
    async def _typed(self, event: Input.Submitted) -> None:
        event.stop()
        path = event.value.strip()
        if path:
            await self._add(path)

    async def _add(self, path: str) -> None:
        """Send adding the workspace at ``path``, picking it once it's accepted and listed."""
        self._show_note(None)
        awaited = Awaited(accepted=lambda: self._added(path), why=self._refused)
        await self._change(AddWorkspace(path=path), awaited)

    def _added(self, path: str) -> None:
        """Pick the workspace adding ``path`` adds, now accepted, once it's listed, unless this
        screen is gone."""
        if not self.is_attached:
            return
        self._typing(False)
        self._adding = path
        self._pick_added()

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

    async def action_remove(self) -> None:
        """Remove the highlighted workspace once the user confirms; other rows remove
        nothing."""
        row = self.rows[self.query_one(OptionList).highlighted or 0]
        if not isinstance(row, WorkspaceEntry):
            return
        removing = RemoveWorkspace(workspace=row.id)

        async def answered(confirmed: bool | None) -> None:
            if confirmed:
                self._show_note(None)
                await self._change(removing, Awaited(why=self._refused))

        confirm = ConfirmScreen(REMOVE_TITLE, remove_message(row), REMOVE_YES)
        self.app.push_screen(confirm, answered)

    def must_pick(self) -> None:
        """Have escape quit orbit, as at startup, now there's nothing to go back to."""
        self.startup = True
        adding = self.query_one(Input).display
        self.query_one(Vertical).border_subtitle = workspaces_hint(startup=True, adding=adding)

    def action_back(self) -> None:
        if self.query_one(Input).display:
            self._typing(False)
        elif self.startup:
            self.app.exit()
        else:
            self.dismiss(None)

    def _typing(self, on: bool) -> None:
        """Show the box for a path to add, and type in it; or go back to the list."""
        path = self.query_one(Input)
        path.display = on
        path.border_subtitle = ""
        self.query_one(Vertical).border_subtitle = workspaces_hint(startup=self.startup, adding=on)
        if on:
            path.focus()
        else:
            path.clear()
            self.query_one(OptionList).focus()

    def _show_note(self, note: str | None) -> None:
        """Show ``note`` under the list, or hide it while it's none."""
        shown = self.query_one("#workspace-note", Static)
        shown.update(note or "")
        shown.display = note is not None


class WorkspacesHost:
    """Keeps the workspaces the latest ``Catalog`` lists, as ``SessionStateChanged`` changes
    their sessions, and shows them with ``push`` when asked, for the ``repository`` the
    frontend runs in, refreshing the picker while it's shown. ``change`` sends adding and
    removing workspaces, and ``chosen`` takes the workspace picked."""

    def __init__(
        self, push: Push, change: Change, chosen: Chosen, repository: str | None = None
    ) -> None:
        self._push = push
        self._change = change
        self._chosen = chosen
        self._repository = repository
        self.workspaces: tuple[WorkspaceEntry, ...] = ()
        self.shown: WorkspacesScreen | None = None  # The picker shown, if any.

    def follow(self, catalog: Catalog) -> None:
        """List the workspaces in ``catalog``."""
        self.workspaces = catalog.workspaces
        self._refresh()

    def follow_state(self, changed: SessionStateChanged) -> None:
        """Note ``changed``'s session's state."""
        self.workspaces = with_states(self.workspaces, changed)
        self._refresh()

    def open(self, startup: bool = False) -> None:
        """Let the user pick a workspace, add one or remove one, unless the picker is shown
        already; at ``startup``, escape quits."""
        if self.shown is not None:
            return

        async def picked(workspace: str | None) -> None:
            self.shown = None
            if workspace is not None:
                await self._chosen(workspace, startup)

        self.shown = WorkspacesScreen(self.workspaces, self._repository, self._change, startup)
        self._push(self.shown, picked)

    def lost(self) -> None:
        """Let the user pick a workspace now the app is on none: open the picker, or have
        escape quit the one shown, as closing it would leave the app on none."""
        if self.shown is not None:
            self.shown.must_pick()
        self.open()

    def _refresh(self) -> None:
        """Show the workspaces in the picker, if it's shown."""
        if self.shown is not None:
            self.shown.show(self.workspaces)
