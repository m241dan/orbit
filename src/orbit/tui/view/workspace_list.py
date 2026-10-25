"""The workspaces a control plane serves, without the screens showing them: the picker of them,
the clones under way and the agents running, as the control-plane stream reports them."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from orbit.protocol import (
    AgentsChanged,
    DirectoryListing,
    ErrorOccurred,
    Watch,
    WorkspaceCloning,
    WorkspaceEntry,
)
from orbit.tui.view.actions import action
from orbit.tui.view.catalog import CatalogState
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.workspace_picker import Change, WorkspacePicker
from orbit.tui.view.workspaces import with_clone, without_clone

Chosen = Callable[[str, bool], Awaitable[None]]
"""Takes the id of the workspace picked, and whether it was picked at startup."""
Picked = Callable[[str | None], Awaitable[None]]
"""Takes what the workspace picker closed with: the id of the workspace picked, or none."""


@dataclass(frozen=True)
class Served:
    """The workspaces the control plane serves, as the ``catalog`` lists them, to the frontend
    running in the ``repository``, if any."""

    catalog: CatalogState
    repository: str | None = None


class WorkspaceListView(Protocol):
    """Where the workspace picker shows, one at a time, with its explorer."""

    @property
    def picker(self) -> WorkspacePicker | None:
        """The workspace picker shown, if any."""
        ...

    def open_picker(
        self,
        workspaces: tuple[WorkspaceEntry, ...],
        repository: str | None,
        startup: bool,
        picked: Picked,
    ) -> WorkspacePicker:
        """Show the picker of ``workspaces`` to the frontend in ``repository``, opened as at
        ``startup``, calling ``picked`` with what it closes with by itself; returns it."""
        ...

    def show_listing(self, listing: DirectoryListing) -> None:
        """Show ``listing`` in the picker's explorer, if the picker and its explorer are
        open."""
        ...


class WorkspaceList:
    """Shows the workspaces ``served`` in ``view`` when asked, refreshing the picker while it's
    shown, and the clones under way, as ``WorkspaceCloning`` reports them, until they're served
    or fail or the link is back, and the agents running, as ``AgentsChanged`` reports them while
    the picker watches for them, and the directories ``DirectoryListing`` lists, in its
    explorer. ``change`` sends the streams to watch, and ``chosen`` takes the workspace
    picked."""

    def __init__(
        self, view: WorkspaceListView, change: Change, chosen: Chosen, served: Served
    ) -> None:
        self._view = view
        self._change = change
        self._chosen = chosen
        self._catalog = served.catalog
        self._repository = served.repository
        self.cloning: tuple[WorkspaceCloning, ...] = ()

    @property
    def workspaces(self) -> tuple[WorkspaceEntry, ...]:
        """The workspaces the catalog lists."""
        return self._catalog.workspaces

    def follow(self) -> None:
        """Follow the catalog's latest listing, or a session's state changing in it."""
        self._refresh()

    def follow_cloning(self, progress: WorkspaceCloning) -> None:
        """Note how far the clone ``progress`` is about has got, dropping it once it's served."""
        self.cloning = with_clone(self.cloning, progress)
        self._refresh()

    def follow_error(self, error: ErrorOccurred) -> None:
        """Drop the clone ``error`` says failed, if it names one by its URL, whatever its code,
        telling the picker shown."""
        if error.url is None:
            return
        self.cloning = without_clone(self.cloning, error.url)
        self._refresh()
        shown = self._view.picker
        if shown is not None:
            shown.clone_failed(error.url, error.message)

    def reconnected(self) -> None:
        """Drop every clone, now the link to the control plane is back, as clones under way
        don't survive the control plane stopping."""
        self.cloning = ()
        self._refresh()

    def follow_listing(self, listing: DirectoryListing) -> None:
        """Show ``listing`` in the picker's explorer, if it's shown."""
        self._view.show_listing(listing)

    def follow_agents(self, changed: AgentsChanged) -> None:
        """Show the agents ``changed`` lists in the picker, if it's shown."""
        shown = self._view.picker
        if shown is not None:
            shown.show_agents(changed.agents)

    def open(self, startup: bool = False) -> None:
        """Let the user pick a workspace, add one or remove one, unless the picker is shown
        already; at ``startup``, or once the app has lost its workspace since, escape quits and
        the workspace picked is taken as picked at startup. The picker watches the control-plane
        stream while it's shown, and stops once it's closed."""
        if self._view.picker is not None:
            return
        picker: WorkspacePicker | None = None

        async def picked(workspace: str | None) -> None:
            assert picker is not None  # Opened before it can close.
            await self._change(Watch(streams=()), Awaited())
            if workspace is not None:
                with action("pick_workspace"):
                    await self._chosen(workspace, picker.startup)

        picker = self._view.open_picker(self.workspaces, self._repository, startup, picked)
        picker.cloning = self.cloning

    def lost(self) -> None:
        """Let the user pick a workspace now the app is on none: open the picker as at startup,
        or have escape quit the one shown, as closing it would leave the app on none."""
        shown = self._view.picker
        if shown is not None:
            shown.must_pick()
            return
        self.open(startup=True)

    def _refresh(self) -> None:
        """Show the workspaces in the picker, if it's shown."""
        shown = self._view.picker
        if shown is not None:
            shown.show(self.workspaces, self.cloning)
