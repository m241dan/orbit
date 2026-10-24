"""The port the control plane adds and removes the workspaces it serves through, and its error.
It sits beside :mod:`orbit.agent.application.ports` because it hands out workspace hosts, which
use those ports themselves."""

from typing import Protocol

from orbit.agent.application.ports import Cloner
from orbit.agent.application.workspace_host import WorkspaceHost
from orbit.agent.domain.purging import Purged
from orbit.agent.domain.workspaces import PathKind
from orbit.protocol import INVALID


class WorkspaceUnavailable(Exception):
    """A workspace can't be served, for the reason ``message`` gives, refused with ``code``:
    ``INVALID`` when it's the workspace as given, such as an invalid configuration, or
    ``INTERNAL`` when orbit failed, such as the store, perhaps partway through opening it."""

    def __init__(self, message: str, code: str = INVALID) -> None:
        super().__init__(message)
        self.code = code


class WorkspaceSource(Cloner, Protocol):
    """Where the workspaces a control plane serves come from as frontends add them, by their
    paths or as clones of repositories it makes, and where they go as they're removed."""

    async def kind(self, path: str) -> PathKind:
        """What there is at ``path``."""
        ...

    async def top_level(self, path: str) -> str:
        """The path of the workspace the directory at ``path`` stands for: its repository's top
        level, if it's in one, else itself, resolved."""
        ...

    async def open(self, root: str, *, managed: bool = False) -> WorkspaceHost:
        """The host of the workspace at ``root``, registered or found by its path, flagged
        served and, if ``managed``, as a clone orbit made, with its lock taken if no other orbit
        holds it, making it read-only otherwise. Raises :class:`WorkspaceUnavailable` if it
        can't be served."""
        ...

    async def set_served(self, workspace_id: str, served: bool) -> None:
        """Flag the workspace ``workspace_id`` as ``served``, or not, so a server starting later
        serves it, or doesn't. Raises :class:`WorkspaceUnavailable` if that fails."""
        ...

    async def restore_served(self, workspace_id: str) -> None:
        """Flag the workspace ``workspace_id`` served again, as it was before it was unflagged,
        keeping when it was added. Raises :class:`WorkspaceUnavailable` if that fails."""
        ...

    async def latest_added(self) -> str | None:
        """The path of the workspace served that was flagged served last, if any. Raises
        :class:`WorkspaceUnavailable` if that can't be found out."""
        ...

    async def close(self, workspace_id: str) -> None:
        """Let go of the lock on the workspace ``workspace_id``, whose host is closed and which
        is no longer flagged served, keeping its records."""
        ...

    async def losses(self, workspace_id: str) -> list[str]:
        """What purging the workspace ``workspace_id`` would lose, a short line each: what the
        directory of a clone orbit made holds that its remote doesn't; none for another
        workspace. Raises :class:`WorkspaceUnavailable` if that can't be found out."""
        ...

    async def purge(self, workspace_id: str) -> Purged:
        """Delete the records of the workspace ``workspace_id``, whose host is closed and which
        is no longer flagged served but still locked, and, if it's a clone orbit made, its
        directory, having checked nothing would be lost, as :func:`erase` orders it; returns
        what it deleted. Raises :class:`WorkspaceUnavailable` if they can't be, as ``invalid``
        if it isn't registered."""
        ...
