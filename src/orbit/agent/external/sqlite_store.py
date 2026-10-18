"""Saved sessions and their events' log in one SQLite database per user, keyed by workspace.

The database is ``orbit.db`` in the state directory, in WAL mode. One connection serves it, owned
by a single thread: every database call runs there, a statement or a transaction at a time, so a
caller that's cancelled while awaiting one never leaves the connection mid-transaction. The schema
is upgraded at open by forward-only migrations, each in its own transaction.

Each session's log has a row in ``logs`` with the size of its events, kept as they're appended
and trimmed, and the highest ``seq`` trimmed from it.

Session locks are :class:`SessionLocks` lock files in ``locks/<workspace id>`` in the state
directory, so that a crash leaves no stale lock behind.
"""

import asyncio
import errno
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from orbit.agent.application.ports import StorageFailed, StoredSession
from orbit.agent.domain.replay import LogBounds
from orbit.agent.domain.sessions import SessionRecord, Unreadable, parse_record, record_json
from orbit.agent.domain.storage_halt import StorageErrorKind
from orbit.agent.external.git_tree import workspace_fingerprint
from orbit.agent.external.session_locks import SessionLocks, check_session_id
from orbit.protocol import EventEnvelope, decode_event, encode

DATABASE = "orbit.db"
LOCKS = "locks"
BUSY_TIMEOUT = 5.0
RETRY_DELAY = 0.01
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
    (
        """CREATE TABLE events (
            workspace_id TEXT NOT NULL REFERENCES workspaces (id),
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            type TEXT NOT NULL,
            json TEXT NOT NULL,
            size INTEGER NOT NULL,
            PRIMARY KEY (workspace_id, session_id, seq)
        )""",
    ),
    (
        # Each session's log: the bytes of its events kept, and the highest seq trimmed, if any.
        """CREATE TABLE logs (
            workspace_id TEXT NOT NULL REFERENCES workspaces (id),
            session_id TEXT NOT NULL,
            size INTEGER NOT NULL,
            trimmed_seq INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (workspace_id, session_id)
        )""",
        """INSERT INTO logs (workspace_id, session_id, size)
        SELECT workspace_id, session_id, sum(size) FROM events GROUP BY workspace_id, session_id""",
    ),
)


_SQLITE_KINDS = {
    sqlite3.SQLITE_FULL: StorageErrorKind.DISK_FULL,
    sqlite3.SQLITE_READONLY: StorageErrorKind.READ_ONLY,
    sqlite3.SQLITE_PERM: StorageErrorKind.READ_ONLY,
    sqlite3.SQLITE_BUSY: StorageErrorKind.LOCKED,
    sqlite3.SQLITE_LOCKED: StorageErrorKind.LOCKED,
    sqlite3.SQLITE_CANTOPEN: StorageErrorKind.MISSING,
}
"""What orbit makes of SQLite's primary result codes."""

_EXTENDED_KINDS = {sqlite3.SQLITE_READONLY_DBMOVED: StorageErrorKind.MISSING}
"""What orbit makes of the extended result codes that mean more than their primary code."""

_ERRNO_KINDS = {
    errno.ENOSPC: StorageErrorKind.DISK_FULL,
    errno.EDQUOT: StorageErrorKind.DISK_FULL,
    errno.EROFS: StorageErrorKind.READ_ONLY,
    errno.EACCES: StorageErrorKind.READ_ONLY,
    errno.EPERM: StorageErrorKind.READ_ONLY,
    errno.ENOENT: StorageErrorKind.MISSING,
}
"""What orbit makes of the operating system's errors."""


def error_kind(error: sqlite3.Error | OSError) -> StorageErrorKind:
    """What orbit makes of ``error``, by its SQLite result code or its ``errno``."""
    if isinstance(error, OSError):
        return _ERRNO_KINDS.get(error.errno or 0, StorageErrorKind.UNKNOWN)
    code = getattr(error, "sqlite_errorcode", None)  # Unset on errors SQLite didn't raise.
    if not isinstance(code, int):
        return StorageErrorKind.UNKNOWN
    return _EXTENDED_KINDS.get(code) or _SQLITE_KINDS.get(code & 0xFF, StorageErrorKind.UNKNOWN)


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


def _use_wal(connection: sqlite3.Connection) -> None:
    """Put ``connection``'s database in WAL mode, trying for up to :data:`BUSY_TIMEOUT` seconds.

    While another connection holds a write lock on a database not yet in WAL mode, such as one
    switching it at the same time, SQLite refuses the switch at once rather than waiting, so it's
    tried again until it works or the time is up, when the error is raised.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT
    while True:
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
        time.sleep(RETRY_DELAY)


def _set_up(connection: sqlite3.Connection, database: Path, workspace: str) -> str:
    """Put ``connection`` in WAL mode, bring its schema up to date and register ``workspace``;
    returns the workspace's id."""
    _use_wal(connection)
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


def _max_seq(connection: sqlite3.Connection) -> int:
    """The highest ``seq`` in ``connection``'s ``events`` table, 0 when it's empty."""
    (seq,) = connection.execute("SELECT max(seq) FROM events").fetchone()
    return int(seq or 0)


type EventRow = tuple[str, str, int, str, str, int]


def _event_row(workspace_id: str, session_id: str, envelope: EventEnvelope) -> EventRow:
    """``envelope``'s row in the ``events`` table, as an event of the session ``session_id`` in
    the workspace ``workspace_id``: its JSON as sent, and that JSON's size in bytes."""
    text = encode(envelope)
    return (workspace_id, session_id, envelope.seq, envelope.payload.type, text, len(text.encode()))


def _decoded(texts: Sequence[str]) -> list[EventEnvelope]:
    """The events whose JSON ``texts`` holds, latest first, decoded oldest first, but for those
    of types this orbit doesn't know."""
    decoded = [decode_event(text) for text in reversed(texts)]
    return [envelope for envelope in decoded if envelope is not None]


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
    """Saved sessions, their events' log and the review count of one workspace, in the state
    directory's database.

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
        self._max_seq = 0  # The highest seq in the log when it opened, or appended since.
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

    @property
    def workspace_id(self) -> str:
        """The id of the store's workspace; empty until the store is open."""
        return self._workspace

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
            raise self._failed(message, error) from error

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

    def max_seq(self) -> int:
        """The highest ``seq`` in the log, across workspaces, when the store opened or appended
        by it since; 0 when there's none."""
        return self._max_seq

    async def append(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> None:
        check_session_id(session_id)
        try:
            await self._call(self._append, session_id, envelopes)
        except sqlite3.Error as error:
            message = f"Couldn't save the events of the session {session_id} to {self._database}"
            raise self._failed(f"{message}: {error}", error) from error

    async def recent(self, session_id: str, limit: int) -> list[EventEnvelope]:
        """The last ``limit`` events logged of the session ``session_id``, oldest first, but for
        those of types this orbit doesn't know; none when they can't be read."""
        check_session_id(session_id)
        try:
            texts = await self._call(self._recent, session_id, limit)
        except sqlite3.Error:
            return []
        return _decoded(texts)

    async def before(
        self, session_id: str, before_seq: int, limit: int
    ) -> tuple[list[EventEnvelope], bool]:
        """The last ``limit`` events logged of the session ``session_id`` before ``before_seq``,
        oldest first, but for those of types this orbit doesn't know, and whether earlier ones
        remain; none when they can't be read."""
        check_session_id(session_id)
        try:
            texts = await self._call(self._before, session_id, before_seq, limit + 1)
        except sqlite3.Error:
            return [], False
        return _decoded(texts[:limit]), len(texts) > limit

    async def after(self, session_id: str, after_seq: int) -> list[EventEnvelope] | None:
        """The events logged of the session ``session_id`` after ``after_seq``, oldest first,
        but for those of types this orbit doesn't know; ``None`` when they can't be read."""
        check_session_id(session_id)
        try:
            texts = await self._call(self._after, session_id, after_seq)
        except sqlite3.Error:
            return None
        return _decoded(texts)

    async def bounds(self, session_id: str) -> LogBounds | None:
        check_session_id(session_id)
        try:
            return await self._call(self._bounds, session_id)
        except sqlite3.Error:
            return None

    async def trim(self, session_id: str, limit: int, target: int) -> None:
        check_session_id(session_id)
        try:
            await self._call(self._trim, session_id, limit, target)
        except sqlite3.Error as error:
            message = f"Couldn't trim the events of the session {session_id} in {self._database}"
            raise RuntimeError(f"{message}: {error}") from error

    async def trimmed(self, session_id: str) -> bool:
        check_session_id(session_id)
        try:
            return await self._call(self._trimmed, session_id)
        except sqlite3.Error:
            return False

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
            raise self._failed(message, error) from error

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

    def _failed(self, message: str, error: sqlite3.Error) -> StorageFailed:
        """The failure, saying ``message``, of a write to the database that raised ``error``."""
        return StorageFailed(message, str(self._database), str(error), error_kind(error))

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

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """A transaction around the block, committed when it ends and rolled back if it fails."""
        try:
            self._db.execute("BEGIN IMMEDIATE")
            yield
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def _connect(self, workspace: str) -> None:
        self._connection, self._workspace = _open(self._database, workspace)
        self._max_seq = _max_seq(self._connection)
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

    def _append(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> None:
        rows = [_event_row(self._workspace, session_id, envelope) for envelope in envelopes]
        with self._transaction():
            self._db.executemany(
                """INSERT INTO events (workspace_id, session_id, seq, type, json, size)
                VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )
            self._db.execute(
                """INSERT INTO logs (workspace_id, session_id, size) VALUES (?, ?, ?)
                ON CONFLICT (workspace_id, session_id) DO UPDATE SET size = size + excluded.size""",
                (self._workspace, session_id, sum(row[5] for row in rows)),
            )
        self._max_seq = max([self._max_seq, *(envelope.seq for envelope in envelopes)])

    def _recent(self, session_id: str, limit: int) -> list[str]:
        """The JSON of the session ``session_id``'s last ``limit`` events, latest first."""
        rows = self._db.execute(
            """SELECT json FROM events WHERE workspace_id = ? AND session_id = ?
            ORDER BY seq DESC LIMIT ?""",
            (self._workspace, session_id, limit),
        )
        return [str(text) for (text,) in rows]

    def _before(self, session_id: str, before_seq: int, limit: int) -> list[str]:
        """The JSON of the session ``session_id``'s last ``limit`` events before ``before_seq``,
        latest first."""
        rows = self._db.execute(
            """SELECT json FROM events WHERE workspace_id = ? AND session_id = ? AND seq < ?
            ORDER BY seq DESC LIMIT ?""",
            (self._workspace, session_id, before_seq, limit),
        )
        return [str(text) for (text,) in rows]

    def _after(self, session_id: str, after_seq: int) -> list[str]:
        """The JSON of the session ``session_id``'s events after ``after_seq``, latest first."""
        rows = self._db.execute(
            """SELECT json FROM events WHERE workspace_id = ? AND session_id = ? AND seq > ?
            ORDER BY seq DESC""",
            (self._workspace, session_id, after_seq),
        )
        return [str(text) for (text,) in rows]

    def _trim(self, session_id: str, limit: int, target: int) -> None:
        """Delete the session ``session_id``'s oldest events, if its log is over ``limit``
        bytes, keeping the latest that together come under ``target``."""
        key = (self._workspace, session_id)
        with self._transaction():
            row = self._db.execute(
                "SELECT size FROM logs WHERE workspace_id = ? AND session_id = ?", key
            ).fetchone()
            if row is None or row[0] <= limit:
                return
            # An event goes when it and those after it come to the target or more.
            (cut,) = self._db.execute(
                """SELECT max(seq) FROM (
                    SELECT seq, sum(size) OVER (ORDER BY seq DESC) AS kept FROM events
                    WHERE workspace_id = ? AND session_id = ?
                ) WHERE kept >= ?""",
                (*key, target),
            ).fetchone()
            self._db.execute(
                "DELETE FROM events WHERE workspace_id = ? AND session_id = ? AND seq <= ?",
                (*key, cut),
            )
            self._db.execute(
                """UPDATE logs SET
                    size = (SELECT coalesce(sum(size), 0) FROM events
                        WHERE workspace_id = ?1 AND session_id = ?2),
                    trimmed_seq = coalesce(?3, trimmed_seq)
                WHERE workspace_id = ?1 AND session_id = ?2""",
                (*key, cut),
            )

    def _trimmed(self, session_id: str) -> bool:
        row = self._db.execute(
            "SELECT trimmed_seq FROM logs WHERE workspace_id = ? AND session_id = ?",
            (self._workspace, session_id),
        ).fetchone()
        return row is not None and row[0] > 0

    def _bounds(self, session_id: str) -> LogBounds:
        # Once every event kept is trimmed, the oldest that could be kept is after the last cut.
        (first,) = self._db.execute(
            """SELECT coalesce(
                (SELECT min(seq) FROM events WHERE workspace_id = ?1 AND session_id = ?2),
                (SELECT trimmed_seq + 1 FROM logs
                    WHERE workspace_id = ?1 AND session_id = ?2 AND trimmed_seq > 0)
            )""",
            (self._workspace, session_id),
        ).fetchone()
        (others,) = self._db.execute(
            "SELECT max(seq) FROM events WHERE workspace_id = ? AND session_id != ?",
            (self._workspace, session_id),
        ).fetchone()
        return LogBounds(first_seq=first, others_seq=int(others or 0))

    def _delete(self, session_id: str) -> None:
        with self._transaction():
            self._db.execute(
                "DELETE FROM events WHERE workspace_id = ? AND session_id = ?",
                (self._workspace, session_id),
            )
            self._db.execute(
                "DELETE FROM logs WHERE workspace_id = ? AND session_id = ?",
                (self._workspace, session_id),
            )
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
