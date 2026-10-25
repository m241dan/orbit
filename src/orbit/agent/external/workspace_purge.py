"""Purging a workspace registered in the store, as :func:`purge` orders it: finding it, holding
its workspace lock, checking what deleting its clone would lose and deleting its fallback files,
session locks and, for a clone orbit made, its directory, then its records and registration last,
so a purge that fails partway can be run again; and forgetting one, as ``orbit db forget`` does,
refusing one a running server serves or another orbit holds."""

import asyncio
import shutil
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from orbit.agent.application.purge import erase, purge
from orbit.agent.domain.purging import Purged, PurgeRefused, locked_refusal, served_refusal
from orbit.agent.domain.workspaces import RegisteredWorkspace
from orbit.agent.external.clone_loss import clone_loss
from orbit.agent.external.fallback_files import remove_fallback_files
from orbit.agent.external.repository import orbit_dir
from orbit.agent.external.run_file import running_server
from orbit.agent.external.sqlite_store import SqliteStore
from orbit.agent.external.workspace_lock import WorkspaceLock
from orbit.agent.external.workspace_registry import WorkspaceRegistry


async def purge_losses(workspace: RegisteredWorkspace) -> list[str]:
    """What purging ``workspace`` would lose, a short line each: what its directory holds that
    its remote doesn't, if it's a clone orbit made, which would be deleted; none otherwise."""
    return await clone_loss(Path(workspace.path)) if workspace.managed else []


class StorePurging:
    """Purging the workspaces registered in ``store`` that no running ``orbit serve`` serves, as
    ``orbit db forget`` does, holding their workspace locks; each part raises
    :class:`StoreError` if the store fails, and ``OSError`` if a file can't be deleted or a
    lock taken."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    async def find(self, key: str) -> RegisteredWorkspace | None:
        """The workspace whose id is ``key``, else the one at the path ``key`` stands for."""
        return await WorkspaceRegistry(self._store).find(key)

    @asynccontextmanager
    async def held(self, workspace: RegisteredWorkspace) -> AsyncIterator[str | None]:
        """Run the block holding the workspace lock of ``workspace``, if its directory is still
        there, giving why it can't be forgotten if a running ``orbit serve`` serves it or
        another orbit holds its lock."""
        server = running_server(self._store.state_dir) if workspace.served else None
        if server is not None:
            yield served_refusal(workspace.path, server.url)
        else:
            with _locked(Path(workspace.path)) as refusal:
                yield refusal

    async def losses(self, workspace: RegisteredWorkspace) -> list[str]:
        """What purging ``workspace`` would lose, as :func:`purge_losses` says."""
        return await purge_losses(workspace)

    async def stop(self, workspace: RegisteredWorkspace) -> None:
        """Nothing: no running server serves ``workspace``, as :meth:`held` makes sure."""

    async def delete(self, workspace: RegisteredWorkspace) -> Purged:
        """Delete what orbit keeps of ``workspace``, as :func:`erase` orders it."""
        return await erase(self, workspace)

    async def remove_fallback_files(self, workspace: RegisteredWorkspace) -> int:
        """Delete the fallback files of ``workspace`` in the state directory and its
        repository's orbit directory; returns how many it deleted."""
        repository = await asyncio.to_thread(orbit_dir, workspace.path)
        places = [self._store.state_dir, repository]
        return await asyncio.to_thread(remove_fallback_files, places, workspace.id)

    async def remove_session_locks(self, workspace_id: str) -> None:
        """Let go of the session locks of the workspace ``workspace_id`` and remove them."""
        await self._store.remove_locks(workspace_id)

    async def remove_clone(self, path: str) -> None:
        """Delete the directory of the clone at ``path`` and all it holds, if it's there."""
        await asyncio.to_thread(_remove_clone, Path(path))

    async def remove_records(self, workspace_id: str) -> int:
        """Delete the records of the workspace ``workspace_id``; returns how many sessions it
        had."""
        return await WorkspaceRegistry(self._store).purge(workspace_id)


async def forget_workspace(store: SqliteStore, key: str) -> Purged:
    """Delete what orbit keeps of the workspace whose path or id is ``key`` in ``store``, as
    :func:`purge` orders it through :class:`StorePurging`, holding its workspace lock while its
    directory is there. Raises :class:`PurgeRefused`, deleting nothing, if none is registered
    so, a running ``orbit serve`` serves it, another orbit holds its lock or deleting its clone
    would lose work; :class:`StoreError` if the store fails, and ``OSError`` if a file can't be
    deleted or the lock taken."""
    return await purge(StorePurging(store), key)


async def purge_workspace(store: SqliteStore, workspace_id: str) -> Purged:
    """Delete what orbit keeps of the workspace ``workspace_id`` in ``store``, as :func:`erase`
    orders it, its registration last, for a :func:`purge` that has held it and checked what
    that would lose. Raises :class:`PurgeRefused`, deleting nothing, if it isn't registered;
    :class:`StoreError` if the store fails, and ``OSError`` if a file can't be deleted, leaving
    it registered to be purged again."""
    workspace = await WorkspaceRegistry(store).workspace(workspace_id)
    if workspace is None:
        raise PurgeRefused(f"No workspace {workspace_id} is registered.")
    return await erase(StorePurging(store), workspace)


@contextmanager
def _locked(root: Path) -> Iterator[str | None]:
    """Run the block holding the workspace lock of the workspace at ``root``, if it's still
    there, giving why it can't be forgotten if another orbit holds it. Raises ``OSError`` if it
    can't be taken."""
    if not root.is_dir():
        yield None
        return
    lock = WorkspaceLock(orbit_dir(str(root)))
    owner = lock.take()
    try:
        yield None if owner is None else locked_refusal(str(root), owner)
    finally:
        lock.close()


def _remove_clone(clone: Path) -> None:
    """Delete the directory ``clone`` and all it holds, if it's there; raises ``OSError`` if it
    can't."""
    if clone.exists():
        shutil.rmtree(clone)
