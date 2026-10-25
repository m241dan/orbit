"""The latest catalog's workspaces and sessions, which every picker reads, and its agents, whose
roles the task editor offers."""

from orbit.protocol import (
    AgentEntry,
    Catalog,
    SessionStateChanged,
    SessionSummary,
    WorkspaceEntry,
)
from orbit.tui.view.sessions import workspace_sessions
from orbit.tui.view.workspaces import with_states


class CatalogState:
    """Keeps the workspaces the latest ``Catalog`` lists, as ``SessionStateChanged`` changes
    their sessions: the one copy the hosts read, so every picker shows the same states; and the
    agents it lists."""

    def __init__(self) -> None:
        self.workspaces: tuple[WorkspaceEntry, ...] = ()
        self.agents: tuple[AgentEntry, ...] = ()

    def follow(self, catalog: Catalog) -> None:
        """Keep the workspaces and agents ``catalog`` lists."""
        self.workspaces = catalog.workspaces
        self.agents = catalog.agents

    def follow_state(self, changed: SessionStateChanged) -> None:
        """Note ``changed``'s session's state, in whichever workspace lists it."""
        self.workspaces = with_states(self.workspaces, changed)

    def sessions(self, workspace: str | None) -> tuple[SessionSummary, ...]:
        """The sessions of the workspace ``workspace``; none if it isn't listed."""
        return workspace_sessions(self.workspaces, workspace)
