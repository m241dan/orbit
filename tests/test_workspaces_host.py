"""The WorkspacesHost showing the workspaces in one picker at a time."""

from textual.app import App

from orbit.protocol import AddWorkspace, ListDirectory, RemoveWorkspace, Watch
from orbit.tui.view.catalog import CatalogState
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.workspace_list import Served, WorkspaceList
from orbit.tui.workspaces import WorkspacesHost, WorkspacesScreen


class Host(App[None]):
    """An app holding a :class:`WorkspaceList` shown by a WorkspacesHost, recording the
    workspaces picked."""

    def __init__(self) -> None:
        super().__init__()
        self.picked: list[str] = []
        self.screens = WorkspacesHost(self.push_screen, self._change)
        self.host = WorkspaceList(self.screens, self._change, self._chosen, Served(CatalogState()))

    async def _change(
        self, command: AddWorkspace | RemoveWorkspace | Watch | ListDirectory, awaited: Awaited
    ) -> None:
        """Send nothing."""

    async def _chosen(self, workspace: str, startup: bool) -> None:
        self.picked.append(workspace)

    def pickers(self) -> list[WorkspacesScreen]:
        return [screen for screen in self.screen_stack if isinstance(screen, WorkspacesScreen)]


async def test_dismissing_and_reopening_before_the_callback_keeps_one_picker() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.open()
        await pilot.pause()
        app.screen.dismiss(None)
        app.host.open()
        await pilot.pause()
        app.host.open()
        await pilot.pause()
        app.host.open()
        await pilot.pause()
        [screen] = app.pickers()
        assert app.screens.shown is screen
        assert app.picked == []
