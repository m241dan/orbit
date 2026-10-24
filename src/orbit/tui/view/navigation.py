"""Where this frontend is and how it moves, without the screens showing it: the catalog of
workspaces and sessions, the workspace and session it's on, the pickers of them, and how the
app opens on the first catalog."""

from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from typing import Protocol

from orbit.protocol import (
    AgentsChanged,
    Catalog,
    Command,
    DirectoryListing,
    ErrorOccurred,
    Event,
    SessionStateChanged,
    Snapshot,
    WorkspaceCloning,
    WorkspaceEntry,
)
from orbit.tui.view.catalog import CatalogState
from orbit.tui.view.opening import Hosts, Opening, OpeningHost
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.place import Place, PlaceView
from orbit.tui.view.sessions import chosen_workspace
from orbit.tui.view.workspace_list import Served, WorkspaceList, WorkspaceListView


class Send(Protocol):
    """Sends a command to the backend, awaiting its outcome as told, if at all; returns whether
    it was sent."""

    def __call__(self, command: Command, awaited: Awaited | None = None) -> Awaitable[bool]: ...


@dataclass(frozen=True)
class Views:
    """Where the navigation shows: the session picker and the offer to activate in ``place``,
    and the workspace picker in ``workspaces``."""

    place: PlaceView
    workspaces: WorkspaceListView


class Navigation:
    """Keeps the catalog, the :class:`Place` this frontend is in and the :class:`WorkspaceList`
    it can move to, shown in ``views``, sending with ``send``, and opens the app as its
    ``opening`` says once the first catalog comes: over a link, if ``linked``, the user picks
    the workspace; otherwise it's the one at the opening's repository, or the first."""

    def __init__(self, views: Views, send: Send, opening: Opening, linked: bool) -> None:
        self.catalog = CatalogState()
        repository = opening.repository

        def opens_on(workspaces: Sequence[WorkspaceEntry]) -> WorkspaceEntry | None:
            # Over a link the user picks the workspace.
            return None if linked else chosen_workspace(workspaces, repository)

        self.place = Place(views.place, send, self.catalog, opens_on)
        self.workspaces = WorkspaceList(
            views.workspaces, send, self._open_workspace, Served(self.catalog, repository)
        )
        self.opening = OpeningHost(opening, linked, send, Hosts(self.place, self.workspaces))

    async def _open_workspace(self, workspace: str, startup: bool) -> None:
        """Open the session picker of the ``workspace`` picked, as the opening says."""
        await self.opening.open_workspace(workspace, startup)

    def follow_catalog(self, catalog: Catalog) -> None:
        """List ``catalog``'s workspaces and sessions; one that no longer lists the workspace
        this frontend is on opens the workspace picker. In process, the first catalog opens the
        session picker, if the app picks one."""
        self.catalog.follow(catalog)
        self.workspaces.follow()
        was_on = self.place.workspace
        self.place.follow()
        if was_on is not None and self.place.workspace is None:
            self.workspaces.lost()
        self.opening.catalog_shown()

    def follow_state(self, changed: SessionStateChanged) -> None:
        """Note ``changed``'s session's state, wherever it's shown."""
        self.catalog.follow_state(changed)
        self.place.follow_state()
        self.workspaces.follow()

    def view(self, snapshot: Snapshot) -> None:
        """Note this frontend views ``snapshot``'s session."""
        self.place.view(snapshot)

    def follow_cloning(self, progress: WorkspaceCloning) -> None:
        """Note how far a clone has got."""
        self.workspaces.follow_cloning(progress)

    def follow_error(self, error: ErrorOccurred) -> None:
        """Drop the clone ``error`` says failed, if it names one."""
        self.workspaces.follow_error(error)

    def follow_listing(self, listing: DirectoryListing) -> None:
        """Show ``listing`` in the workspace picker's explorer, if it's shown."""
        self.workspaces.follow_listing(listing)

    def follow_agents(self, changed: AgentsChanged) -> None:
        """Show the agents running in the workspace picker, if it's shown."""
        self.workspaces.follow_agents(changed)

    def reconnected(self) -> None:
        """Drop the clones under way, now the link is back."""
        self.workspaces.reconnected()

    async def listed(self, batch: Sequence[Event]) -> None:
        """Open the app once ``batch`` has the first catalog."""
        await self.opening.listed(batch)
