"""Purging a workspace: deleting every record orbit keeps of it, its sessions, events, logs,
review count and registration, its session locks and fallback files, and, for a clone orbit
made, its directory, unless that would lose work its remote doesn't have."""

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

from orbit.agent.domain.purging import purge_refusal
from orbit.agent.external.clone_loss import clone_loss
from orbit.agent.external.fallback_files import remove_fallback_files
from orbit.agent.external.repository import orbit_dir
from orbit.agent.external.sqlite_store import SqliteStore
from orbit.agent.external.workspace_registry import Workspace, WorkspaceRegistry


class PurgeRefused(Exception):
    """A workspace isn't purged, for the reason the message gives, having deleted nothing."""


@dataclass(frozen=True)
class Purged:
    """What purging the ``workspace`` deleted: its ``sessions``, its ``fallback_files`` and,
    for a clone orbit made, the ``clone`` directory."""

    workspace: Workspace
    sessions: int
    fallback_files: int
    clone: str | None


async def purge_losses(workspace: Workspace) -> list[str]:
    """What purging ``workspace`` would lose, a short line each: what its directory holds that
    its remote doesn't, if it's a clone orbit made, which would be deleted; none otherwise."""
    return await clone_loss(Path(workspace.path)) if workspace.managed else []


async def purge_workspace(store: SqliteStore, workspace_id: str) -> Purged:
    """Delete the records of the workspace ``workspace_id`` in ``store``: its sessions, events,
    logs, review count and registration, in one transaction, then its session locks and its
    fallback files, and, if it's a clone orbit made, its directory; never a directory orbit
    didn't clone into. Raises :class:`PurgeRefused`, deleting nothing, if it isn't registered or
    deleting its clone would lose work; :class:`StoreError` if the store fails, and ``OSError``
    if a file can't be deleted."""
    workspace = await WorkspaceRegistry(store).workspace(workspace_id)
    if workspace is None:
        raise PurgeRefused(f"No workspace {workspace_id} is registered.")
    losses = await purge_losses(workspace)
    if losses:
        raise PurgeRefused(purge_refusal(workspace.path, losses))
    repository = await asyncio.to_thread(orbit_dir, workspace.path)
    sessions = await WorkspaceRegistry(store).purge(workspace_id)
    await store.remove_locks(workspace_id)
    places = [store.state_dir, repository]
    files = await asyncio.to_thread(remove_fallback_files, places, workspace_id)
    clone = workspace.path if workspace.managed else None
    if clone is not None:
        await asyncio.to_thread(_remove_clone, Path(clone))
    return Purged(workspace, sessions, files, clone)


def _remove_clone(clone: Path) -> None:
    """Delete the directory ``clone`` and all it holds, if it's there; raises ``OSError`` if it
    can't."""
    if clone.exists():
        shutil.rmtree(clone)
