"""The workspaces registered in the state directory's database: each found by its path or
registered with a new id, and whether a server serves it."""

import sqlite3
import uuid
from pathlib import Path

from orbit.agent.external.repository import workspace_fingerprint
from orbit.agent.external.sqlite_store import SqliteStore


def register(connection: sqlite3.Connection, workspace: str) -> str:
    """The id of ``workspace`` in ``connection``, found by its path or registered with a new one;
    its fingerprint is brought up to date."""
    path = Path(workspace).resolve()
    (workspace_id,) = connection.execute(
        """INSERT INTO workspaces (id, path, fingerprint, name) VALUES (?, ?, ?, ?)
        ON CONFLICT (path) DO UPDATE SET fingerprint = excluded.fingerprint
        RETURNING id""",
        (uuid.uuid4().hex, str(path), workspace_fingerprint(workspace), path.name),
    ).fetchone()
    return str(workspace_id)


def _served(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """The id and path of each workspace served in ``connection``, by path."""
    rows = connection.execute("SELECT id, path FROM workspaces WHERE served ORDER BY path")
    return [(str(workspace_id), str(path)) for workspace_id, path in rows]


def _set_served(connection: sqlite3.Connection, workspace_id: str, served: bool) -> None:
    connection.execute("UPDATE workspaces SET served = ? WHERE id = ?", (served, workspace_id))


def _path(connection: sqlite3.Connection, workspace_id: str) -> str | None:
    row = connection.execute("SELECT path FROM workspaces WHERE id = ?", (workspace_id,))
    found = row.fetchone()
    return None if found is None else str(found[0])


class WorkspaceRegistry:
    """The workspaces registered in ``store``'s database.

    Each call raises :class:`StoreError`, naming the database, when the database fails.
    """

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    async def find_or_register(self, workspace: str) -> str:
        """The id of the workspace at ``workspace``, found by its path or registered with a new
        one, with its name, and its fingerprint brought up to date."""
        message = f"Couldn't register the workspace {workspace} in"
        return await self._store.run(message, lambda connection: register(connection, workspace))

    async def served(self) -> list[tuple[str, str]]:
        """The id and path of each workspace a server serves, by path."""
        return await self._store.run("Couldn't list the served workspaces in", _served)

    async def set_served(self, workspace_id: str, served: bool) -> None:
        """Have a server serve the workspace ``workspace_id`` if ``served``, else not."""
        message = f"Couldn't mark whether the workspace {workspace_id} is served in"
        await self._store.run(
            message, lambda connection: _set_served(connection, workspace_id, served)
        )

    async def path(self, workspace_id: str) -> str | None:
        """The path of the workspace ``workspace_id``; ``None`` if it isn't registered."""
        message = f"Couldn't look up the workspace {workspace_id} in"
        return await self._store.run(message, lambda connection: _path(connection, workspace_id))
