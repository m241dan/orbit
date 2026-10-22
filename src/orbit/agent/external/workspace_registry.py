"""The workspaces registered in the state directory's database: each found by its path, taken
over from where its repository was before it moved, or registered with a new id, and whether a
server serves it."""

import asyncio
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from orbit.agent.domain.fingerprints import fingerprints_match, lookalikes_note, reattached_note
from orbit.agent.external.repository import workspace_fingerprint
from orbit.agent.external.sqlite_schema import transaction
from orbit.agent.external.sqlite_store import SqliteStore


@dataclass(frozen=True)
class Registered:
    """The id of a workspace found or registered, and the ``note`` telling the user it was
    re-attached to its moved repository, or which gone workspaces it looks like, if either."""

    workspace_id: str
    note: str | None = None


def register(connection: sqlite3.Connection, path: Path, fingerprint: str | None) -> Registered:
    """The workspace at the resolved ``path`` in ``connection``: found by its path; else the one
    workspace whose repository, matching ``fingerprint``, has gone from its path, moved to
    ``path``; else registered with a new id, noting the gone workspaces it looks like if there
    are several. Its fingerprint is brought up to date to ``fingerprint``."""
    with transaction(connection):
        moved = [] if _known(connection, path) else _moved(connection, fingerprint)
        if len(moved) == 1:
            return _reattached(connection, path, fingerprint, moved[0])
        note = lookalikes_note(str(path), [old for _, old in moved]) if moved else None
        return Registered(_upserted(connection, path, fingerprint), note)


def _known(connection: sqlite3.Connection, path: Path) -> bool:
    """Whether a workspace is registered at ``path`` in ``connection``."""
    found = connection.execute("SELECT 1 FROM workspaces WHERE path = ?", (str(path),))
    return found.fetchone() is not None


def _moved(connection: sqlite3.Connection, fingerprint: str | None) -> list[tuple[str, str]]:
    """The id and path of each workspace in ``connection``, by path, whose repository matches
    ``fingerprint`` and whose path has gone; none without a ``fingerprint``."""
    if fingerprint is None:
        return []
    rows = connection.execute(
        "SELECT id, path, fingerprint FROM workspaces WHERE fingerprint IS NOT NULL ORDER BY path"
    )
    return [
        (str(workspace_id), str(path))
        for workspace_id, path, theirs in rows.fetchall()
        if fingerprints_match(fingerprint, str(theirs)) and not Path(path).exists()
    ]


def _reattached(
    connection: sqlite3.Connection, path: Path, fingerprint: str | None, moved: tuple[str, str]
) -> Registered:
    """The workspace ``moved``, by its id and old path, re-attached to its repository at
    ``path``, with ``fingerprint``."""
    workspace_id, old = moved
    connection.execute(
        "UPDATE workspaces SET path = ?, fingerprint = ?, name = ? WHERE id = ?",
        (str(path), fingerprint, path.name, workspace_id),
    )
    return Registered(workspace_id, reattached_note(old, str(path)))


def _upserted(connection: sqlite3.Connection, path: Path, fingerprint: str | None) -> str:
    """The id of the workspace at ``path`` in ``connection``, found by its path or registered
    with a new one, its fingerprint brought up to date to ``fingerprint``."""
    (workspace_id,) = connection.execute(
        """INSERT INTO workspaces (id, path, fingerprint, name) VALUES (?, ?, ?, ?)
        ON CONFLICT (path) DO UPDATE SET fingerprint = excluded.fingerprint
        RETURNING id""",
        (uuid.uuid4().hex, str(path), fingerprint, path.name),
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

    async def find_or_register(self, workspace: str) -> Registered:
        """The workspace at ``workspace``, found by its path, taken over from where its
        repository was before it moved or registered with a new id, as :func:`register` says.

        The path and fingerprint are worked out off the database's thread, so git doesn't hold
        up other workspaces' calls."""
        path = await asyncio.to_thread(Path(workspace).resolve)
        fingerprint = await asyncio.to_thread(workspace_fingerprint, workspace)
        message = f"Couldn't register the workspace {workspace} in"
        return await self._store.run(
            message, lambda connection: register(connection, path, fingerprint)
        )

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
