"""The latest catalog's workspaces and sessions, which every picker reads."""

from orbit.protocol import Catalog, SessionStateChanged, SessionSummary, WorkspaceEntry
from orbit.tui.view.sessions import workspace_sessions
from orbit.tui.view.workspaces import with_states


class CatalogState:
    """Keeps the workspaces the latest ``Catalog`` lists, as ``SessionStateChanged`` changes
    their sessions: the one copy the hosts read, so every picker shows the same states."""

    def __init__(self) -> None:
        self.workspaces: tuple[WorkspaceEntry, ...] = ()

    def follow(self, catalog: Catalog) -> None:
        """Keep the workspaces ``catalog`` lists."""
        self.workspaces = catalog.workspaces

    def follow_state(self, changed: SessionStateChanged) -> None:
        """Note ``changed``'s session's state, in whichever workspace lists it."""
        self.workspaces = with_states(self.workspaces, changed)

    def sessions(self, workspace: str | None) -> tuple[SessionSummary, ...]:
        """The sessions of the workspace ``workspace``; none if it isn't listed."""
        return workspace_sessions(self.workspaces, workspace)
