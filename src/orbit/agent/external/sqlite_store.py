"""Saved sessions and their events' log in one SQLite database per user, keyed by workspace, and
a view of it for each workspace.

The database is ``orbit.db`` in the state directory, in WAL mode. One connection serves it, owned
by a single thread: every database call runs there, a statement or a transaction at a time, so a
caller that's cancelled while awaiting one never leaves the connection mid-transaction. The schema
and opening it are :mod:`sqlite_schema`'s, the events' log's statements :mod:`sqlite_log`'s, and
copying it to a backup :mod:`sqlite_backup`'s; this module holds the sessions' and review counts'
statements, and runs them all on the database's thread.

Session locks are :class:`SessionLocks` lock files in ``locks/<workspace id>`` in the state
directory, so that a crash leaves no stale lock behind.
"""

import asyncio
import logging
import shutil
import sqlite3
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from orbit.agent.application.ports import ReadFailed, StorageFailed, StoredSession
from orbit.agent.domain.replay import LogBounds
from orbit.agent.domain.sessions import SessionRecord, Unreadable, parse_record, record_json
from orbit.agent.external import sqlite_log
from orbit.agent.external.session_locks import SessionLocks, check_session_id
from orbit.agent.external.sqlite_backup import copy_to, is_database_file
from orbit.agent.external.sqlite_errors import error_kind
from orbit.agent.external.sqlite_schema import (
    StoreError,
    open_database,
    open_existing,
    transaction,
)
from orbit.protocol import EventEnvelope

DATABASE = "orbit.db"
LOCKS = "locks"
THREAD_PREFIX = "orbit-sqlite"

logger = logging.getLogger(__name__)


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


def _remove_tree(directory: Path) -> None:
    """Remove ``directory`` and all it holds, if it's there; raises ``OSError`` if it can't."""
    if directory.exists():
        shutil.rmtree(directory)


class SqliteStore:
    """The state directory's database, shared by every workspace an orbit serves: each
    workspace's saved sessions, events' log and review count are reached through its
    :meth:`workspace_view`, and the workspaces themselves through a ``WorkspaceRegistry``.

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
        # The highest seq ever logged or reserved when it opened or was last read, or since.
        self._max_seq = 0
        # Each workspace's session locks, by its id, made as they're first used.
        self._locks: dict[str, SessionLocks] = {}

    @classmethod
    async def open(cls, state_dir: Path | str) -> Self:
        """The store in ``state_dir``, creating the directory and the database as needed.

        Raises :class:`StoreError`, naming the file, when the database can't be opened, is newer
        than this orbit, or can't be upgraded.
        """
        store = cls(Path(state_dir))
        try:
            await store._call(store._connect)
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
    def state_dir(self) -> Path:
        """The state directory the database is in."""
        return self._state_dir

    def workspace_view(self, workspace_id: str) -> "WorkspaceStore":
        """The saved sessions, events' log, review count and session locks of the workspace
        ``workspace_id``, in this store."""
        return WorkspaceStore(self, workspace_id)

    async def close(self) -> None:
        """Let go of this store's locks, and close the database once the calls in flight are
        done, leaving no thread running; once closed, it stays closed."""
        for locks in self._locks.values():
            locks.close()
        if not self._closed:
            self._closed = True
            self._executor.submit(self._disconnect)
        await asyncio.shield(asyncio.to_thread(self._executor.shutdown))

    async def run[T](self, message: str, function: Callable[[sqlite3.Connection], T]) -> T:
        """What ``function`` returns given the connection, run on the database's thread.

        Raises :class:`StoreError`, saying ``message`` and the database, when the database
        fails, as once the store is closed.
        """
        try:
            return await self._call(lambda: function(self._db))
        except sqlite3.Error as error:
            raise StoreError(self._message(message, error)) from error

    async def save(self, workspace: str, record: SessionRecord) -> None:
        check_session_id(record.id)
        message = f"Couldn't save the session {record.id} to"
        await self._or_raise(message, self._save, workspace, record, write=True)

    async def load(self, workspace: str, session_id: str) -> SessionRecord | Unreadable:
        check_session_id(session_id)
        return await self._read_or(self._unreadable, self._load, workspace, session_id)

    async def sessions(self, workspace: str) -> list[StoredSession]:
        rows = await self._or_raise("Couldn't list the sessions in", self._rows, workspace)
        locks = self._locks_of(workspace)
        return await asyncio.to_thread(self._listed, workspace, locks, rows)

    async def delete(self, workspace: str, session_id: str) -> None:
        check_session_id(session_id)
        message = f"Couldn't delete the session {session_id} from"
        await self._or_raise(message, self._delete, workspace, session_id, write=True)
        await self._locks_of(workspace).remove(session_id)

    def max_seq(self) -> int:
        """The highest ``seq`` ever logged or reserved, across workspaces, when the store opened
        or :meth:`latest_seq` last read it, or appended or reserved by it since; 0 when there's
        none."""
        return self._max_seq

    async def latest_seq(self) -> int:
        return await self._or_raise("Couldn't read the latest seq from", self._latest_seq)

    async def reserve(self, seq: int) -> None:
        message = f"Couldn't reserve the seq numbers up to {seq} in"
        await self._or_raise(message, self._reserve, seq, write=True)

    async def append(
        self, workspace: str, session_id: str, envelopes: Sequence[EventEnvelope]
    ) -> int:
        check_session_id(session_id)
        message = f"Couldn't save the events of the session {session_id} to"
        return await self._or_raise(
            message, self._append, workspace, session_id, envelopes, write=True
        )

    async def recent(self, workspace: str, session_id: str, limit: int) -> list[EventEnvelope]:
        """The last ``limit`` events logged of the session ``session_id`` in the workspace
        ``workspace``, oldest first, but for those of types this orbit doesn't know. Raises
        :class:`ReadFailed` when they can't be read."""
        check_session_id(session_id)
        message = f"Couldn't read the events of the session {session_id} from"
        return await self._or_raise(
            message, self._log(sqlite_log.recent), workspace, session_id, limit
        )

    async def before(
        self, workspace: str, session_id: str, before_seq: int, limit: int
    ) -> tuple[list[EventEnvelope], bool]:
        """The last ``limit`` events logged of the session ``session_id`` in the workspace
        ``workspace`` before ``before_seq``, oldest first, but for those of types this orbit
        doesn't know, and whether earlier ones remain. Raises :class:`ReadFailed` when they
        can't be read."""
        check_session_id(session_id)
        message = f"Couldn't read the events of the session {session_id} from"
        return await self._or_raise(
            message, self._log(sqlite_log.before), workspace, session_id, before_seq, limit
        )

    async def after(
        self, workspace: str, session_id: str, after_seq: int
    ) -> list[EventEnvelope] | None:
        """The events logged of the session ``session_id`` in the workspace ``workspace`` after
        ``after_seq``, oldest first, but for those of types this orbit doesn't know; ``None``
        when they can't be read."""
        check_session_id(session_id)
        return await self._read_or(
            lambda _: None, self._log(sqlite_log.after), workspace, session_id, after_seq
        )

    async def bounds(self, workspace: str, session_id: str, since: int) -> LogBounds | None:
        """Where the log of the session ``session_id`` stands in the workspace ``workspace``'s,
        for a frontend that saw the events up to ``since``; ``None`` when that can't be read."""
        check_session_id(session_id)
        return await self._read_or(
            lambda _: None, self._log(sqlite_log.bounds), workspace, session_id, since
        )

    async def trim(self, workspace: str, session_id: str, limit: int, target: int) -> None:
        check_session_id(session_id)
        message = f"Couldn't trim the events of the session {session_id} in"
        await self._or_raise(
            message, self._log(sqlite_log.trim), workspace, session_id, limit, target, write=True
        )

    async def trimmed(self, workspace: str, session_id: str) -> bool:
        check_session_id(session_id)
        message = f"Couldn't read whether the session {session_id}'s log was trimmed in"
        return await self._or_raise(message, self._log(sqlite_log.trimmed), workspace, session_id)

    async def forget(self, workspace: str, session_id: str) -> None:
        check_session_id(session_id)
        message = f"Couldn't delete the events of the session {session_id} from"
        await self._or_raise(message, self._forget, workspace, session_id, write=True)

    async def acquire(self, workspace: str, session_id: str) -> bool:
        """Lock the session ``session_id`` of the workspace ``workspace`` for this orbit;
        returns ``False`` when another running orbit has it locked.

        Raises :class:`StoreFailed` when the lock can't be taken, as once the store is closed.
        """
        return await self._locks_of(workspace).acquire(session_id)

    async def release(self, workspace: str, session_id: str) -> None:
        await self._locks_of(workspace).release(session_id)

    async def remove_locks(self, workspace: str) -> None:
        """Let go of the workspace ``workspace``'s session locks this store holds and remove
        their directory, for purging the workspace; raises ``OSError`` if it can't be
        removed."""
        locks = self._locks.pop(workspace, None)
        if locks is not None:
            locks.close()
        directory = self._lock_dir(workspace)
        await asyncio.to_thread(_remove_tree, directory)

    async def review_count(self, workspace: str) -> int:
        message = "Couldn't read the review count from"
        return await self._or_raise(message, self._review_count, workspace)

    async def save_review_count(self, workspace: str, count: int) -> None:
        message = "Couldn't save the review count to"
        await self._or_raise(message, self._save_review_count, workspace, count, write=True)

    async def insert_new(
        self,
        workspace: str,
        reading: Callable[[], AbstractContextManager[NewRows]],
        committing: Callable[[], AbstractContextManager[None]],
    ) -> int:
        """Save, in the workspace ``workspace``, the session rows ``reading()`` gives whose ids
        aren't saved yet and, unless one is, its review count (if any), in one transaction
        committed inside ``committing()``, all while ``reading()`` is held, and rolled back if
        anything fails; returns how many sessions it saved.

        Raises :class:`StorageFailed` when the database fails, and whatever ``reading`` or
        ``committing`` raises.
        """
        message = "Couldn't import the sessions into"
        return await self._or_raise(
            message, self._insert_new, workspace, reading, committing, write=True
        )

    async def backup(self, target: Path) -> None:
        """Copy the database to ``target``, owner read-write, replacing any file there only once
        the copy is whole, with SQLite's online backup, which is safe while another orbit uses
        the database.

        Raises :class:`StoreError`, naming both files, when ``target`` is the database or one of
        its files, or when the copy fails, leaving ``target`` as it was.
        """
        await self._call(self._backup, target)

    async def _read_or[*A, T](
        self, fallback: Callable[[sqlite3.Error], T], function: Callable[[*A], T], *args: *A
    ) -> T:
        """What ``function(*args)`` returns, run on the database's thread, or what ``fallback``
        makes of the error, logged, when the database fails."""
        try:
            return await self._call(function, *args)
        except sqlite3.Error as error:
            logger.warning("Couldn't read from %s: %s", self._database, error)
            return fallback(error)

    async def _or_raise[*A, T](
        self, message: str, function: Callable[[*A], T], *args: *A, write: bool = False
    ) -> T:
        """What ``function(*args)`` returns, run on the database's thread.

        Raises, saying ``message`` and the database, when the database fails: a
        :class:`StorageFailed` when it's a ``write``, or else a :class:`ReadFailed`.
        """
        try:
            return await self._call(function, *args)
        except sqlite3.Error as error:
            text = self._message(message, error)
            raise (self._failed(text, error) if write else ReadFailed(text)) from error

    def _message(self, message: str, error: sqlite3.Error) -> str:
        """``message``, followed by the database and ``error``."""
        return f"{message} {self._database}: {error}"

    def _unreadable(self, error: sqlite3.Error) -> Unreadable:
        """Why a session can't be loaded when the database raised ``error``."""
        return Unreadable(self._message("it can't be read from", error))

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

    def _log[*A, T](self, function: Callable[[sqlite3.Connection, *A], T]) -> Callable[[*A], T]:
        """``function`` of :mod:`sqlite_log`, given the open connection; only on the database's
        thread."""
        return lambda *args: function(self._db, *args)

    def _locks_of(self, workspace: str) -> SessionLocks:
        """The workspace ``workspace``'s session locks, made if they aren't yet: closed, taking
        none, once the store is."""
        if workspace not in self._locks:
            locks = SessionLocks(self._lock_dir(workspace))
            if self._closed:
                locks.close()
            self._locks[workspace] = locks
        return self._locks[workspace]

    def _lock_dir(self, workspace: str) -> Path:
        """The directory of the workspace ``workspace``'s session locks."""
        return self._state_dir / LOCKS / workspace

    def _connect(self) -> None:
        self._connection = open_database(self._database)
        self._max_seq = sqlite_log.max_seq(self._connection)

    def _connect_existing(self) -> None:
        self._connection = open_existing(self._database)

    def _backup(self, target: Path) -> None:
        if is_database_file(target, self._database):
            message = (
                f"Won't back up the database {self._database} onto itself: {target} is the"
                " database or one of its files."
            )
            raise StoreError(message)
        try:
            copy_to(self._db, target)
        except (OSError, sqlite3.Error) as error:
            message = f"Couldn't back up the database {self._database} to {target}: {error}"
            raise StoreError(message) from error

    def _disconnect(self) -> None:
        if self._connection is not None:
            self._connection.close()

    def _save(self, workspace: str, record: SessionRecord) -> None:
        self._db.execute(
            """INSERT INTO sessions (workspace_id, id, name, last_active, record)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (workspace_id, id) DO UPDATE SET
                name = excluded.name, last_active = excluded.last_active, record = excluded.record
            """,
            (
                workspace,
                record.id,
                record.name,
                record.last_active.isoformat(),
                record_json(record),
            ),
        )

    def _insert_new(
        self,
        workspace: str,
        reading: Callable[[], AbstractContextManager[NewRows]],
        committing: Callable[[], AbstractContextManager[None]],
    ) -> int:
        with reading() as new, transaction(self._db, committing):
            saved = sum(self._insert_row(workspace, row) for row in new.rows)
            if new.review_count is not None:
                self._db.execute(
                    """INSERT INTO reviews (workspace_id, since_super) VALUES (?, ?)
                    ON CONFLICT (workspace_id) DO NOTHING""",
                    (workspace, new.review_count),
                )
        return saved

    def _insert_row(self, workspace: str, row: SessionRow) -> int:
        """Save ``row`` unless its id is saved; returns how many rows it saved."""
        cursor = self._db.execute(
            """INSERT INTO sessions (workspace_id, id, name, last_active, record)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (workspace_id, id) DO NOTHING""",
            (workspace, row.id, row.name, row.last_active, row.record),
        )
        return cursor.rowcount

    def _load(self, workspace: str, session_id: str) -> SessionRecord | Unreadable:
        row = self._db.execute(
            "SELECT record FROM sessions WHERE workspace_id = ? AND id = ?",
            (workspace, session_id),
        ).fetchone()
        return Unreadable("it isn't saved") if row is None else parse_record(str(row[0]))

    def _rows(self, workspace: str) -> list[tuple[str, str]]:
        rows = self._db.execute(
            "SELECT id, record FROM sessions WHERE workspace_id = ? ORDER BY id",
            (workspace,),
        )
        return [(str(session_id), str(text)) for session_id, text in rows]

    def _append(self, workspace: str, session_id: str, envelopes: Sequence[EventEnvelope]) -> int:
        """Add ``envelopes`` to the session ``session_id``'s log, but for those whose ``seq`` it
        holds already; returns how many it added."""
        added, highest = sqlite_log.append(self._db, workspace, session_id, envelopes)
        self._max_seq = max(self._max_seq, highest)
        return added

    def _reserve(self, seq: int) -> None:
        """Raise the mark to ``seq``, counted in :meth:`max_seq` from then on."""
        sqlite_log.reserve(self._db, seq)
        self._max_seq = max(self._max_seq, seq)

    def _latest_seq(self) -> int:
        """The highest ``seq`` ever logged or reserved now, counted in :meth:`max_seq` from then
        on."""
        self._max_seq = max(self._max_seq, sqlite_log.max_seq(self._db))
        return self._max_seq

    def _forget(self, workspace: str, session_id: str) -> None:
        with transaction(self._db):
            sqlite_log.forget_log(self._db, workspace, session_id)

    def _delete(self, workspace: str, session_id: str) -> None:
        with transaction(self._db):
            sqlite_log.forget_log(self._db, workspace, session_id)
            self._db.execute(
                "DELETE FROM sessions WHERE workspace_id = ? AND id = ?",
                (workspace, session_id),
            )

    def _review_count(self, workspace: str) -> int:
        row = self._db.execute(
            "SELECT since_super FROM reviews WHERE workspace_id = ?", (workspace,)
        ).fetchone()
        return 0 if row is None else int(row[0])

    def _save_review_count(self, workspace: str, count: int) -> None:
        self._db.execute(
            """INSERT INTO reviews (workspace_id, since_super) VALUES (?, ?)
            ON CONFLICT (workspace_id) DO UPDATE SET since_super = excluded.since_super""",
            (workspace, count),
        )

    def _listed(
        self, workspace: str, locks: SessionLocks, rows: list[tuple[str, str]]
    ) -> list[StoredSession]:
        """The sessions of the workspace ``workspace`` saved as ``rows``, each marked locked when
        another orbit holds it in ``locks``.

        Raises :class:`ReadFailed` when the locks can't be checked.
        """
        try:
            self._lock_dir(workspace).mkdir(parents=True, exist_ok=True)
            with locks.checking():
                return [
                    StoredSession(session_id, parse_record(text), locks.held_elsewhere(session_id))
                    for session_id, text in rows
                ]
        except OSError as error:
            message = f"Couldn't list the sessions in {self._database}: their locks can't be"
            raise ReadFailed(f"{message} checked: {error}") from error


class WorkspaceStore:
    """The saved sessions, events' log, review count and session locks of the workspace
    ``workspace_id`` in the shared ``store``, as a :class:`SessionStore` sees them: every call
    goes to the store, scoped to the workspace, but for the highest ``seq``, which is the
    store's across workspaces. Closing the store closes it."""

    def __init__(self, store: SqliteStore, workspace_id: str) -> None:
        self._store = store
        self._id = workspace_id

    @property
    def workspace_id(self) -> str:
        """The id of the workspace this is a view of."""
        return self._id

    async def save(self, record: SessionRecord) -> None:
        await self._store.save(self._id, record)

    async def load(self, session_id: str) -> SessionRecord | Unreadable:
        return await self._store.load(self._id, session_id)

    async def sessions(self) -> list[StoredSession]:
        return await self._store.sessions(self._id)

    async def delete(self, session_id: str) -> None:
        await self._store.delete(self._id, session_id)

    def max_seq(self) -> int:
        """The highest ``seq`` ever logged or reserved, across workspaces, as the store knows
        it."""
        return self._store.max_seq()

    async def latest_seq(self) -> int:
        return await self._store.latest_seq()

    async def reserve(self, seq: int) -> None:
        await self._store.reserve(seq)

    async def append(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> int:
        return await self._store.append(self._id, session_id, envelopes)

    async def recent(self, session_id: str, limit: int) -> list[EventEnvelope]:
        return await self._store.recent(self._id, session_id, limit)

    async def before(
        self, session_id: str, before_seq: int, limit: int
    ) -> tuple[list[EventEnvelope], bool]:
        return await self._store.before(self._id, session_id, before_seq, limit)

    async def after(self, session_id: str, after_seq: int) -> list[EventEnvelope] | None:
        return await self._store.after(self._id, session_id, after_seq)

    async def bounds(self, session_id: str, since: int) -> LogBounds | None:
        return await self._store.bounds(self._id, session_id, since)

    async def trim(self, session_id: str, limit: int, target: int) -> None:
        await self._store.trim(self._id, session_id, limit, target)

    async def trimmed(self, session_id: str) -> bool:
        return await self._store.trimmed(self._id, session_id)

    async def forget(self, session_id: str) -> None:
        await self._store.forget(self._id, session_id)

    async def acquire(self, session_id: str) -> bool:
        return await self._store.acquire(self._id, session_id)

    async def release(self, session_id: str) -> None:
        await self._store.release(self._id, session_id)

    async def review_count(self) -> int:
        return await self._store.review_count(self._id)

    async def save_review_count(self, count: int) -> None:
        await self._store.save_review_count(self._id, count)

    async def insert_new(
        self,
        reading: Callable[[], AbstractContextManager[NewRows]],
        committing: Callable[[], AbstractContextManager[None]],
    ) -> int:
        """As :meth:`SqliteStore.insert_new`, in this workspace."""
        return await self._store.insert_new(self._id, reading, committing)
