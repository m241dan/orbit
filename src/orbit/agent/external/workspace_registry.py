"""The workspaces registered in the state directory's database: each found by its path, taken
over from where its repository was before it moved, or registered with a new id, whether a
server serves it, and when it was last flagged served."""

import asyncio
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from orbit.agent.domain.fingerprints import fingerprints_match, lookalikes_note, reattached_note
from orbit.agent.domain.workspaces import RegisteredWorkspace
from orbit.agent.external.repository import git_top_level, workspace_fingerprint
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
    ``path``, with ``fingerprint``, and no longer managed: orbit didn't clone into ``path``,
    unless the cloner marks it managed again."""
    workspace_id, old = moved
    connection.execute(
        "UPDATE workspaces SET path = ?, fingerprint = ?, name = ?, managed = 0 WHERE id = ?",
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


def _latest_added(connection: sqlite3.Connection) -> str | None:
    """The path of the workspace served in ``connection`` that was flagged served last, if
    any."""
    found = connection.execute(
        """SELECT path FROM workspaces WHERE served AND added_at IS NOT NULL
        ORDER BY added_at DESC, rowid DESC LIMIT 1"""
    ).fetchone()
    return None if found is None else str(found[0])


def _set_served(connection: sqlite3.Connection, workspace_id: str, served: bool) -> None:
    """Flag the workspace ``workspace_id`` in ``connection`` as ``served``, or not, stamping
    when it was flagged served; unflagging keeps the stamp."""
    if not served:
        connection.execute("UPDATE workspaces SET served = 0 WHERE id = ?", (workspace_id,))
        return
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    connection.execute(
        "UPDATE workspaces SET served = 1, added_at = ? WHERE id = ?", (now, workspace_id)
    )


def _restore_served(connection: sqlite3.Connection, workspace_id: str) -> None:
    connection.execute("UPDATE workspaces SET served = 1 WHERE id = ?", (workspace_id,))


def _set_managed(connection: sqlite3.Connection, workspace_id: str) -> None:
    connection.execute("UPDATE workspaces SET managed = 1 WHERE id = ?", (workspace_id,))


_BY_ID = "SELECT id, path, served, managed FROM workspaces WHERE id = ?"
_BY_PATH = "SELECT id, path, served, managed FROM workspaces WHERE path = ?"


def _found(connection: sqlite3.Connection, query: str, key: str) -> RegisteredWorkspace | None:
    """The workspace in ``connection`` that ``query``, either of :data:`_BY_ID` and
    :data:`_BY_PATH`, finds for ``key``, if there is one."""
    found = connection.execute(query, (key,)).fetchone()
    if found is None:
        return None
    workspace_id, path, served, managed = found
    return RegisteredWorkspace(str(workspace_id), str(path), bool(served), bool(managed))


_PURGES = (
    "DELETE FROM events WHERE workspace_id = ?",
    "DELETE FROM logs WHERE workspace_id = ?",
    "DELETE FROM sessions WHERE workspace_id = ?",
    "DELETE FROM reviews WHERE workspace_id = ?",
    "DELETE FROM record_moves WHERE workspace_id = ?",
    "DELETE FROM reports WHERE workspace_id = ?",
    "DELETE FROM report_counters WHERE workspace_id = ?",
    "DELETE FROM usage WHERE workspace_id = ?",
    "DELETE FROM workspaces WHERE id = ?",
)
"""What deletes a workspace's records, its row, with its ``forgotten_seq``, last."""


def _purge(connection: sqlite3.Connection, workspace_id: str) -> int:
    """Delete the records of the workspace ``workspace_id`` in ``connection``, in one
    transaction; returns how many sessions it had."""
    with transaction(connection):
        (sessions,) = connection.execute(
            "SELECT count(*) FROM sessions WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        for statement in _PURGES:
            connection.execute(statement, (workspace_id,))
    return int(sessions)


def _path(connection: sqlite3.Connection, workspace_id: str) -> str | None:
    row = connection.execute("SELECT path FROM workspaces WHERE id = ?", (workspace_id,))
    found = row.fetchone()
    return None if found is None else str(found[0])


def _workspace_path(key: str) -> str:
    """The path of the workspace the path ``key`` stands for: its git work tree's top level, if
    it's in one, else itself, resolved."""
    path = str(Path(key).resolve())
    return git_top_level(path) or path


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

    async def latest_added(self) -> str | None:
        """The path of the workspace served that was flagged served last; ``None`` if none
        is."""
        return await self._store.run("Couldn't find the latest added workspace in", _latest_added)

    async def set_served(self, workspace_id: str, served: bool) -> None:
        """Have a server serve the workspace ``workspace_id`` if ``served``, stamping when, else
        not."""
        message = f"Couldn't mark whether the workspace {workspace_id} is served in"
        await self._store.run(
            message, lambda connection: _set_served(connection, workspace_id, served)
        )

    async def restore_served(self, workspace_id: str) -> None:
        """Have a server serve the workspace ``workspace_id`` again, keeping when it was
        added."""
        message = f"Couldn't mark the workspace {workspace_id} served again in"
        await self._store.run(message, lambda connection: _restore_served(connection, workspace_id))

    async def set_managed(self, workspace_id: str) -> None:
        """Mark the workspace ``workspace_id`` as managed: orbit cloned its repository into its
        clone directory."""
        message = f"Couldn't mark the workspace {workspace_id} managed in"
        await self._store.run(message, lambda connection: _set_managed(connection, workspace_id))

    async def path(self, workspace_id: str) -> str | None:
        """The path of the workspace ``workspace_id``; ``None`` if it isn't registered."""
        message = f"Couldn't look up the workspace {workspace_id} in"
        return await self._store.run(message, lambda connection: _path(connection, workspace_id))

    async def workspace(self, workspace_id: str) -> RegisteredWorkspace | None:
        """The workspace ``workspace_id``; ``None`` if it isn't registered."""
        message = f"Couldn't look up the workspace {workspace_id} in"
        return await self._store.run(
            message, lambda connection: _found(connection, _BY_ID, workspace_id)
        )

    async def find(self, key: str) -> RegisteredWorkspace | None:
        """The workspace whose id is ``key``, else the one at the path ``key`` stands for: its
        git work tree's top level, if it's in one, else itself, resolved; ``None`` if there's
        neither."""
        found = await self.workspace(key)
        if found is not None:
            return found
        path = await asyncio.to_thread(_workspace_path, key)
        message = f"Couldn't look up the workspace {key} in"
        return await self._store.run(message, lambda connection: _found(connection, _BY_PATH, path))

    async def purge(self, workspace_id: str) -> int:
        """Delete the records of the workspace ``workspace_id``, its sessions, their events and
        logs, its review count and its registration, in one transaction; returns how many
        sessions it had."""
        message = f"Couldn't delete the workspace {workspace_id} from"
        return await self._store.run(message, lambda connection: _purge(connection, workspace_id))
