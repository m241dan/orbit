"""Saved sessions in one SQLite database per user, keyed by workspace.

The database is ``orbit.db`` in the state directory, in WAL mode. One connection serves it, owned
by a single thread: every database call runs there, a statement or a transaction at a time, so a
caller that's cancelled while awaiting one never leaves the connection mid-transaction. The schema
is upgraded at open by forward-only migrations, each in its own transaction.

Session locks are :class:`SessionLocks` lock files in ``locks/<workspace id>`` in the state
directory, so that a crash leaves no stale lock behind.
"""

import asyncio
import os
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, closing
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from orbit.agent.application.ports import StoredSession
from orbit.agent.domain.sessions import SessionRecord, Unreadable, parse_record, record_json
from orbit.agent.external.git_tree import workspace_fingerprint
from orbit.agent.external.session_locks import SessionLocks, check_session_id

DATABASE = "orbit.db"
LOCKS = "locks"
BUSY_TIMEOUT = 5.0
THREAD_PREFIX = "orbit-sqlite"
STATE_DIR_VARIABLE = "ORBIT_STATE_DIR"

# Each migration's statements, in order; migration ``n`` (from 1) brings the schema to version n.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE workspaces (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            fingerprint TEXT,
            name TEXT NOT NULL
        )""",
        """CREATE TABLE sessions (
            workspace_id TEXT NOT NULL REFERENCES workspaces (id),
            id TEXT NOT NULL,
            name TEXT NOT NULL,
            last_active TEXT NOT NULL,
            record TEXT NOT NULL,
            PRIMARY KEY (workspace_id, id)
        )""",
        """CREATE TABLE reviews (
            workspace_id TEXT PRIMARY KEY REFERENCES workspaces (id),
            since_super INTEGER NOT NULL
        )""",
    ),
)


def resolve_state_dir(flag: Path | None, environ: Mapping[str, str]) -> Path:
    """The state directory, expanded and absolute: ``flag``, if given, else ``ORBIT_STATE_DIR``
    in ``environ``, else ``orbit`` in its ``XDG_STATE_HOME``, else ``~/.local/state/orbit``."""
    if flag is not None:
        chosen = flag
    elif environ.get(STATE_DIR_VARIABLE):
        chosen = Path(environ[STATE_DIR_VARIABLE])
    elif environ.get("XDG_STATE_HOME"):
        chosen = Path(environ["XDG_STATE_HOME"]) / "orbit"
    else:
        chosen = Path("~/.local/state/orbit")
    return chosen.expanduser().absolute()


class StoreError(Exception):
    """The store can't be opened or backed up, for the reason in the message, which names the
    file."""


@dataclass(frozen=True)
class SessionRow:
    """A session's row: its ``id``, ``name`` and ``last_active`` time (empty when unknown), and
    its ``record``'s text."""

    id: str
    name: str
    last_active: str
    record: str


@dataclass(frozen=True)
class NewRows:
    """Rows to save unless already saved: the sessions' ``rows`` and the ``review_count``, if
    any."""

    rows: tuple[SessionRow, ...]
    review_count: int | None


def _create_files(database: Path) -> None:
    """Create ``database``'s directory (owner-only) and the empty file (owner read-write) if
    they aren't there."""
    try:
        database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(database, os.O_WRONLY | os.O_CREAT, 0o600))
    except OSError as error:
        raise StoreError(f"Couldn't create the database {database}: {error}") from error


def _schema_version(connection: sqlite3.Connection) -> int:
    """The version of ``connection``'s schema, 0 for a new database."""
    connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    return _recorded_version(connection)


def _recorded_version(connection: sqlite3.Connection) -> int:
    """The highest version in ``connection``'s ``schema_version`` table, 0 when it's empty."""
    (version,) = connection.execute("SELECT max(version) FROM schema_version").fetchone()
    return int(version or 0)


def _check_supported(version: int, database: Path) -> None:
    """Raises :class:`StoreError` naming ``database`` when its schema ``version`` is newer than
    this orbit's."""
    latest = len(MIGRATIONS)
    if version > latest:
        raise StoreError(
            f"The database {database} has schema version {version}, newer than this orbit's "
            f"{latest}; update orbit to use it."
        )


def _apply(connection: sqlite3.Connection, version: int, database: Path) -> int:
    """Bring ``connection``'s schema to ``version`` in one transaction, rolled back if it fails;
    returns the schema's version once done.

    Another orbit may have upgraded the schema since it was read, so the version is read again
    under the write lock and the migration skipped if it's already applied.

    Raises :class:`StoreError` naming ``database`` when it fails.
    """
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _recorded_version(connection)
        if current < version:
            for statement in MIGRATIONS[version - 1]:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        connection.execute("COMMIT")
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        message = f"Couldn't upgrade the database {database} to schema version {version}: {error}"
        raise StoreError(message) from error
    return max(current, version)


def _migrate(connection: sqlite3.Connection, database: Path) -> None:
    """Bring ``connection``'s schema up to date, a migration at a time.

    Raises :class:`StoreError` when the database is newer than this orbit or a migration fails.
    """
    version = _schema_version(connection)
    _check_supported(version, database)
    for pending in range(version + 1, len(MIGRATIONS) + 1):
        _check_supported(_apply(connection, pending, database), database)


def _register(connection: sqlite3.Connection, workspace: str) -> str:
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


def _set_up(connection: sqlite3.Connection, database: Path, workspace: str) -> str:
    """Put ``connection`` in WAL mode, bring its schema up to date and register ``workspace``;
    returns the workspace's id."""
    connection.execute("PRAGMA journal_mode = WAL")
    _migrate(connection, database)
    return _register(connection, workspace)


def _open(database: Path, workspace: str) -> tuple[sqlite3.Connection, str]:
    """A connection to ``database``, set up for ``workspace``, and the workspace's id.

    Raises :class:`StoreError` naming ``database`` when it can't be opened or set up.
    """
    _create_files(database)
    try:
        connection = sqlite3.connect(database, timeout=BUSY_TIMEOUT, isolation_level=None)
    except sqlite3.Error as error:
        raise StoreError(f"Couldn't open the database {database}: {error}") from error
    try:
        return connection, _set_up(connection, database, workspace)
    except sqlite3.Error as error:
        connection.close()
        raise StoreError(f"Couldn't open the database {database}: {error}") from error
    except BaseException:
        connection.close()
        raise


def _open_existing(database: Path) -> sqlite3.Connection:
    """A connection to ``database`` as it stands, creating, upgrading and registering nothing.

    Raises :class:`StoreError` naming ``database`` when it isn't there or can't be opened.
    """
    if not database.exists():
        raise StoreError(f"There's no database {database}, so nothing to back up.")
    uri = f"{database.absolute().as_uri()}?mode=rw"
    try:
        return sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT, isolation_level=None)
    except sqlite3.Error as error:
        raise StoreError(f"Couldn't open the database {database}: {error}") from error


def _create_target(target: Path) -> None:
    """Make ``target`` an empty file, owner read-write, whether or not it was there."""
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


class SqliteStore:
    """Saved sessions and the review count of one workspace, in the state directory's database.

    Each stands for one orbit: the session locks it holds are its own, even against another
    ``SqliteStore`` in the same process. Open one with :meth:`open` and :meth:`close` it when
    done.
    """

    def __init__(self, state_dir: Path) -> None:
        self._database = state_dir / DATABASE
        self._state_dir = state_dir
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=THREAD_PREFIX)
        self._connection: sqlite3.Connection | None = None
        self._closed = False
        # Both are the workspace's once the store is open.
        self._workspace = ""
        self._lock_dir = state_dir / LOCKS
        self._locks = SessionLocks(self._lock_dir)

    @classmethod
    async def open(cls, state_dir: Path | str, workspace: str) -> Self:
        """The store in ``state_dir`` for the workspace at ``workspace``, creating the directory,
        the database and the workspace's entry as needed.

        Raises :class:`StoreError`, naming the file, when the database can't be opened, is newer
        than this orbit, or can't be upgraded.
        """
        store = cls(Path(state_dir))
        try:
            await store._call(store._connect, workspace)
        except BaseException:
            await store.close()
            raise
        return store

    @classmethod
    async def open_for_backup(cls, state_dir: Path | str) -> Self:
        """The store in ``state_dir`` with its database opened as it stands, only to
        :meth:`backup` it: nothing is created, upgraded or registered.

        Raises :class:`StoreError`, naming the file, when the database isn't there or can't be
        opened.
        """
        store = cls(Path(state_dir))
        try:
            await store._call(store._connect_existing)
        except BaseException:
            await store.close()
            raise
        return store

    async def close(self) -> None:
        """Let go of this store's locks, and close the database once the calls in flight are
        done, leaving no thread running; once closed, it stays closed."""
        self._locks.close()
        if not self._closed:
            self._closed = True
            self._executor.submit(self._disconnect)
        await asyncio.shield(asyncio.to_thread(self._executor.shutdown))

    async def save(self, record: SessionRecord) -> None:
        check_session_id(record.id)
        try:
            await self._call(self._save, record)
        except sqlite3.Error as error:
            message = f"Couldn't save the session {record.id} to {self._database}: {error}"
            raise RuntimeError(message) from error

    async def load(self, session_id: str) -> SessionRecord | Unreadable:
        check_session_id(session_id)
        try:
            text = await self._call(self._load, session_id)
        except sqlite3.Error as error:
            return Unreadable(f"it can't be read from {self._database}: {error}")
        return Unreadable("it isn't saved") if text is None else parse_record(text)

    async def sessions(self) -> list[StoredSession]:
        try:
            rows = await self._call(self._rows)
        except sqlite3.Error:
            return []
        return await asyncio.to_thread(self._listed, rows)

    async def delete(self, session_id: str) -> None:
        check_session_id(session_id)
        try:
            await self._call(self._delete, session_id)
        except sqlite3.Error as error:
            message = f"Couldn't delete the session {session_id} from {self._database}: {error}"
            raise RuntimeError(message) from error
        await self._locks.remove(session_id)

    async def acquire(self, session_id: str) -> bool:
        """Lock the session ``session_id`` for this orbit; returns ``False`` when another
        running orbit has it locked.

        Raises ``RuntimeError`` when the lock can't be taken, as once the store is closed.
        """
        return await self._locks.acquire(session_id)

    async def release(self, session_id: str) -> None:
        await self._locks.release(session_id)

    async def review_count(self) -> int:
        try:
            return await self._call(self._review_count)
        except sqlite3.Error:
            return 0

    async def save_review_count(self, count: int) -> None:
        try:
            await self._call(self._save_review_count, count)
        except sqlite3.Error as error:
            message = f"Couldn't save the review count to {self._database}: {error}"
            raise RuntimeError(message) from error

    async def insert_new(
        self,
        reading: Callable[[], AbstractContextManager[NewRows]],
        committing: Callable[[], AbstractContextManager[None]],
    ) -> int:
        """Save the session rows ``reading()`` gives whose ids aren't saved yet and, unless one
        is, its review count (if any), in one transaction committed inside ``committing()``, all
        while ``reading()`` is held, and rolled back if anything fails; returns how many sessions
        it saved.

        Raises ``sqlite3.Error`` when the database fails, and whatever ``reading`` or
        ``committing`` raises.
        """
        return await self._call(self._insert_new, reading, committing)

    async def backup(self, target: Path) -> None:
        """Copy the database to ``target``, owner read-write, replacing any file there, with
        SQLite's online backup, which is safe while another orbit uses the database.

        Raises :class:`StoreError`, naming both files, when the copy fails.
        """
        await self._call(self._backup, target)

    async def _call[*A, T](self, function: Callable[[*A], T], *args: *A) -> T:
        """What ``function(*args)`` returns, run on the database's thread.

        Raises ``sqlite3.ProgrammingError`` once the store is closed, so that callers answer as
        for any database that can't be used.
        """
        if self._closed:
            raise sqlite3.ProgrammingError("the store is closed")
        return await asyncio.wrap_future(self._executor.submit(function, *args))

    @property
    def _db(self) -> sqlite3.Connection:
        """The open connection; only on the database's thread."""
        assert self._connection is not None, "The store isn't open."
        return self._connection

    def _connect(self, workspace: str) -> None:
        self._connection, self._workspace = _open(self._database, workspace)
        self._lock_dir = self._state_dir / LOCKS / self._workspace
        self._locks = SessionLocks(self._lock_dir)

    def _connect_existing(self) -> None:
        self._connection = _open_existing(self._database)

    def _backup(self, target: Path) -> None:
        try:
            _create_target(target)
            with closing(sqlite3.connect(target)) as copy:
                self._db.backup(copy)
        except (OSError, sqlite3.Error) as error:
            message = f"Couldn't back up the database {self._database} to {target}: {error}"
            raise StoreError(message) from error

    def _disconnect(self) -> None:
        if self._connection is not None:
            self._connection.close()

    def _save(self, record: SessionRecord) -> None:
        self._db.execute(
            """INSERT INTO sessions (workspace_id, id, name, last_active, record)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (workspace_id, id) DO UPDATE SET
                name = excluded.name, last_active = excluded.last_active, record = excluded.record
            """,
            (
                self._workspace,
                record.id,
                record.name,
                record.last_active.isoformat(),
                record_json(record),
            ),
        )

    def _insert_new(
        self,
        reading: Callable[[], AbstractContextManager[NewRows]],
        committing: Callable[[], AbstractContextManager[None]],
    ) -> int:
        with reading() as new:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                saved = sum(self._insert_row(row) for row in new.rows)
                if new.review_count is not None:
                    self._db.execute(
                        """INSERT INTO reviews (workspace_id, since_super) VALUES (?, ?)
                        ON CONFLICT (workspace_id) DO NOTHING""",
                        (self._workspace, new.review_count),
                    )
                with committing():
                    self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        return saved

    def _insert_row(self, row: SessionRow) -> int:
        """Save ``row`` unless its id is saved; returns how many rows it saved."""
        cursor = self._db.execute(
            """INSERT INTO sessions (workspace_id, id, name, last_active, record)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (workspace_id, id) DO NOTHING""",
            (self._workspace, row.id, row.name, row.last_active, row.record),
        )
        return cursor.rowcount

    def _load(self, session_id: str) -> str | None:
        row = self._db.execute(
            "SELECT record FROM sessions WHERE workspace_id = ? AND id = ?",
            (self._workspace, session_id),
        ).fetchone()
        return None if row is None else str(row[0])

    def _rows(self) -> list[tuple[str, str]]:
        rows = self._db.execute(
            "SELECT id, record FROM sessions WHERE workspace_id = ? ORDER BY id",
            (self._workspace,),
        )
        return [(str(session_id), str(text)) for session_id, text in rows]

    def _delete(self, session_id: str) -> None:
        self._db.execute(
            "DELETE FROM sessions WHERE workspace_id = ? AND id = ?",
            (self._workspace, session_id),
        )

    def _review_count(self) -> int:
        row = self._db.execute(
            "SELECT since_super FROM reviews WHERE workspace_id = ?", (self._workspace,)
        ).fetchone()
        return 0 if row is None else int(row[0])

    def _save_review_count(self, count: int) -> None:
        self._db.execute(
            """INSERT INTO reviews (workspace_id, since_super) VALUES (?, ?)
            ON CONFLICT (workspace_id) DO UPDATE SET since_super = excluded.since_super""",
            (self._workspace, count),
        )

    def _listed(self, rows: list[tuple[str, str]]) -> list[StoredSession]:
        """The sessions saved as ``rows``, each marked locked when another orbit holds it; none
        when the locks can't be checked."""
        try:
            self._lock_dir.mkdir(parents=True, exist_ok=True)
            with self._locks.checking():
                return [
                    StoredSession(
                        session_id, parse_record(text), self._locks.held_elsewhere(session_id)
                    )
                    for session_id, text in rows
                ]
        except OSError:
            return []
