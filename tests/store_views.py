"""A workspace's view of a SQLite store of its own, as one orbit serving one workspace has."""

from pathlib import Path

from orbit.agent.external.sqlite_store import SqliteStore, WorkspaceStore
from orbit.agent.external.workspace_registry import WorkspaceRegistry


class OwnView(WorkspaceStore):
    """The view of the workspace ``workspace_id`` in ``store``, which is its own: closing the
    view closes the store."""

    def __init__(self, store: SqliteStore, workspace_id: str) -> None:
        super().__init__(store, workspace_id)
        self.store = store

    async def close(self) -> None:
        await self.store.close()


async def open_view(state: Path, workspace: Path | str) -> OwnView:
    """The view of the workspace at ``workspace``, registered if it isn't yet, in a store of its
    own in ``state``."""
    store = await SqliteStore.open(state)
    try:
        workspace_id = await WorkspaceRegistry(store).find_or_register(str(workspace))
    except BaseException:
        await store.close()
        raise
    return OwnView(store, workspace_id)
