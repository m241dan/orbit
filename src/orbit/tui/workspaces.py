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
from orbit.tui.modal import ModalSlot, Push
from orbit.tui.view.common import kept_highlight
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.workspaces import (
    PATH_HINT,
    PATH_PLACEHOLDER,
    REMOVE_TITLE,
    REMOVE_YES,
    WORKSPACES_HINTS,
    WORKSPACES_TITLE,
    WorkspaceRow,
    added_workspace,
    preselected,
    remove_message,
    row_key,
    row_label,
    with_states,
    workspace_rows,
)
from orbit.tui.widgets import TypingListScreen, confirm

Change = Callable[[AddWorkspace | RemoveWorkspace, Awaited], Awaitable[object]]
"""Sends adding or removing a workspace to the backend, awaiting its outcome as told; whatever
it returns is ignored."""
Chosen = Callable[[str, bool], Awaitable[None]]
"""Takes the id of the workspace picked, and whether it was picked at startup."""


class WorkspacesScreen(TypingListScreen[str | None]):
    """Lists ``workspaces``, offering first to add the ``repository`` the frontend runs in while
    none is at it, then to add a path; dismisses with the id of the workspace picked.

    ``j``/``k`` (and the arrows) move, preselected on the workspace at ``repository``, and
    enter picks the highlighted workspace. Enter on adding sends ``AddWorkspace`` with
    ``change``, asking for the path first on ``Add a path…``, and picks the workspace once
    accepted and listed; a refusal shows why. ``x`` asks to remove the highlighted workspace,
    then sends ``RemoveWorkspace``, showing why if it's refused. Escape leaves the path box, or
    dismisses with none, or quits orbit at ``startup``, where there's nothing to go back to.
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
        self.repository = repository
        self._change = change
        self._adding: str | None = None  # The path added, until its workspace is listed.

    @property
    def rows(self) -> tuple[WorkspaceRow, ...]:
        """The rows listed, in order."""
        return workspace_rows(self.workspaces, self.repository)

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = WORKSPACES_TITLE
            box.border_subtitle = self.hint
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
        shown = self._keys
        self.workspaces = tuple(workspaces)
        if not self._pick_added() and self.is_mounted:
            self._relist(shown)

    def _relist(self, shown: Sequence[str]) -> None:
        """List the rows again in place of rows with keys ``shown``, keeping the highlight on
        the row it was on, or where it was if that one's gone."""
        options = self.query_one(OptionList)
        highlighted = options.highlighted
        options.clear_options()
        options.add_options(map(row_label, self.rows))
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
            assert row.path is not None  # The picker offers adding the repository's path alone.
            await self._add(row.path)
        else:
            self._typing(PATH_HINT)

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
        self._typing(None)
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

    def action_remove(self) -> None:
        """Remove the highlighted workspace once the user confirms; other rows remove
        nothing."""
        row = self.rows[self.query_one(OptionList).highlighted or 0]
        if not isinstance(row, WorkspaceEntry):
            return
        removing = RemoveWorkspace(workspace=row.id)

        async def remove() -> None:
            self._show_note(None)
            await self._change(removing, Awaited(why=self._refused))

        confirm(self.app.push_screen, REMOVE_TITLE, remove_message(row), REMOVE_YES, remove)

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
    frontend runs in, refreshing the picker while it's shown. ``change`` sends adding and
    removing workspaces, and ``chosen`` takes the workspace picked."""

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
        self._picker = ModalSlot[WorkspacesScreen, str](push)

    @property
    def shown(self) -> WorkspacesScreen | None:
        """The picker shown, if any."""
        return self._picker.showing

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
        already; at ``startup``, or once the app has lost its workspace since, escape quits and
        the workspace picked is taken as picked at startup."""
        if self.shown is not None:
            return
        screen = WorkspacesScreen(self.workspaces, self._repository, self._change, startup)

        async def picked(workspace: str | None) -> None:
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
            self.shown.show(self.workspaces)
