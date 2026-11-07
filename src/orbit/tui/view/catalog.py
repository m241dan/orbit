"""The latest catalog's workspaces and sessions, which every picker reads, and each workspace's
agents, whose roles the task editor offers, and mapped skills, which the task list shows."""

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
    their sessions, with each one's agents: the one copy the hosts read, so every picker shows
    the same states."""

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

    def agents(self, workspace: str | None) -> tuple[AgentEntry, ...]:
        """The agents of the workspace ``workspace``; none if it isn't listed."""
        entry = self._entry(workspace)
        return entry.agents if entry else ()

    def mapped_skills(self, workspace: str | None) -> tuple[str, ...]:
        """The skills the ``[[skills]]`` entries of the workspace ``workspace`` map; none if it
        isn't listed."""
        entry = self._entry(workspace)
        return entry.mapped_skills if entry else ()

    def _entry(self, workspace: str | None) -> WorkspaceEntry | None:
        """The entry of the workspace ``workspace``, if it's listed."""
        return next((entry for entry in self.workspaces if entry.id == workspace), None)
