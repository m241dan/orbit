"""The SQLite database's schema, upgraded by forward-only migrations, and opening it.

A database is opened in WAL mode and its schema brought up to date, a migration at a time, each in
its own transaction.
"""

import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path

BUSY_TIMEOUT = 5.0
RETRY_DELAY = 0.01

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
    (
        # The highest seq ever appended, in one row, so that seq is never handed out again.
        "CREATE TABLE high_water (seq INTEGER NOT NULL)",
        "INSERT INTO high_water (seq) SELECT coalesce(max(seq), 0) FROM events",
        # The highest seq deleted with a session's log in each workspace.
        "ALTER TABLE workspaces ADD COLUMN forgotten_seq INTEGER NOT NULL DEFAULT 0",
    ),
    (
        # Whether a server serves the workspace.
        "ALTER TABLE workspaces ADD COLUMN served INTEGER NOT NULL DEFAULT 0",
    ),
    (
        # Whether orbit cloned the workspace's repository into its clone directory.
        "ALTER TABLE workspaces ADD COLUMN managed INTEGER NOT NULL DEFAULT 0",
    ),
)


class StoreError(Exception):
    """The store can't be opened or backed up, for the reason in the message, which names the
    file."""


@contextmanager
def transaction(
    connection: sqlite3.Connection,
    committing: Callable[[], AbstractContextManager[None]] = nullcontext,
) -> Iterator[None]:
    """A transaction on ``connection`` around the block, committed inside ``committing()`` when
    it ends and rolled back if anything fails, even a cancellation."""
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        with committing():
            connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


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
        with transaction(connection):
            current = _recorded_version(connection)
            if current < version:
                for statement in MIGRATIONS[version - 1]:
                    connection.execute(statement)
                connection.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    except sqlite3.Error as error:
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


def _set_up(connection: sqlite3.Connection, database: Path) -> None:
    """Put ``connection`` in WAL mode and bring its schema up to date."""
    _use_wal(connection)
    _migrate(connection, database)


def open_database(database: Path) -> sqlite3.Connection:
    """A connection to ``database``, in WAL mode with its schema up to date.

    Raises :class:`StoreError` naming ``database`` when it can't be opened or set up.
    """
    _create_files(database)
    try:
        connection = sqlite3.connect(database, timeout=BUSY_TIMEOUT, isolation_level=None)
    except sqlite3.Error as error:
        raise StoreError(f"Couldn't open the database {database}: {error}") from error
    try:
        _set_up(connection, database)
    except sqlite3.Error as error:
        connection.close()
        raise StoreError(f"Couldn't open the database {database}: {error}") from error
    except BaseException:
        connection.close()
        raise
    return connection


def open_existing(database: Path) -> sqlite3.Connection:
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
