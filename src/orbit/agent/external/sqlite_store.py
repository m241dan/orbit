"""Saved sessions and their events' log in one SQLite database per user, keyed by workspace.

The database is ``orbit.db`` in the state directory, in WAL mode. One connection serves it, owned
by a single thread: every database call runs there, a statement or a transaction at a time, so a
caller that's cancelled while awaiting one never leaves the connection mid-transaction. The schema
and opening it are :mod:`sqlite_schema`'s, and copying it to a backup :mod:`sqlite_backup`'s.

Each session's log has a row in ``logs`` with the size of its events, kept as they're appended
and trimmed, and the highest ``seq`` trimmed from it.

Session locks are :class:`SessionLocks` lock files in ``locks/<workspace id>`` in the state
directory, so that a crash leaves no stale lock behind.
"""

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, closing
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from orbit.agent.application.ports import StorageFailed, StoredSession
from orbit.agent.domain.replay import LogBounds
from orbit.agent.domain.sessions import SessionRecord, Unreadable, parse_record, record_json
from orbit.agent.external.session_locks import SessionLocks, check_session_id
from orbit.agent.external.sqlite_backup import copy_to, is_database_file
from orbit.agent.external.sqlite_errors import error_kind
from orbit.agent.external.sqlite_schema import (
    BUSY_TIMEOUT,
    StoreError,
    open_database,
    open_existing,
    transaction,
)
from orbit.protocol import EventEnvelope, decode_event, encode

DATABASE = "orbit.db"
LOCKS = "locks"
THREAD_PREFIX = "orbit-sqlite"


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


def find_workspace_id(state_dir: Path, workspace: str) -> str | None:
    """The id of the workspace at ``workspace`` in the database in ``state_dir``, creating,
    upgrading and registering nothing; ``None`` if it isn't registered or the database can't be
    read."""
    database = state_dir / DATABASE
    if not database.exists():
        return None
    path = str(Path(workspace).resolve())
    uri = f"{database.absolute().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT)) as connection:
            row = connection.execute("SELECT id FROM workspaces WHERE path = ?", (path,)).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row[0])


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
        # The highest seq in the log when it opened or was last read, or appended since.
        self._max_seq = 0
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
        await self._or_raise(
            f"Couldn't save the session {record.id} to", self._save, record, failed=True
        )

    async def load(self, session_id: str) -> SessionRecord | Unreadable:
        check_session_id(session_id)
        return await self._read_or(self._unreadable, self._load, session_id)

    async def sessions(self) -> list[StoredSession]:
        rows = await self._or_raise("Couldn't list the sessions in", self._rows)
        return await asyncio.to_thread(self._listed, rows)

    async def delete(self, session_id: str) -> None:
        check_session_id(session_id)
        message = f"Couldn't delete the session {session_id} from"
        await self._or_raise(message, self._delete, session_id, failed=True)
        await self._locks.remove(session_id)

    def max_seq(self) -> int:
        """The highest ``seq`` in the log, across workspaces, when the store opened or
        :meth:`latest_seq` last read it, or appended by it since; 0 when there's none."""
        return self._max_seq

    async def latest_seq(self) -> int | None:
        return await self._read_or(lambda _: None, self._latest_seq)

    async def append(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> int:
        check_session_id(session_id)
        message = f"Couldn't save the events of the session {session_id} to"
        return await self._or_raise(message, self._append, session_id, envelopes, failed=True)

    async def recent(self, session_id: str, limit: int) -> list[EventEnvelope]:
        """The last ``limit`` events logged of the session ``session_id``, oldest first, but for
        those of types this orbit doesn't know; none when they can't be read."""
        check_session_id(session_id)
        return await self._read_or(lambda _: [], self._recent, session_id, limit)

    async def before(
        self, session_id: str, before_seq: int, limit: int
    ) -> tuple[list[EventEnvelope], bool]:
        """The last ``limit`` events logged of the session ``session_id`` before ``before_seq``,
        oldest first, but for those of types this orbit doesn't know, and whether earlier ones
        remain. Raises ``RuntimeError`` when they can't be read."""
        check_session_id(session_id)
        message = f"Couldn't read the events of the session {session_id} from"
        return await self._or_raise(message, self._before, session_id, before_seq, limit)

    async def after(self, session_id: str, after_seq: int) -> list[EventEnvelope] | None:
        """The events logged of the session ``session_id`` after ``after_seq``, oldest first,
        but for those of types this orbit doesn't know; ``None`` when they can't be read."""
        check_session_id(session_id)
        return await self._read_or(lambda _: None, self._after, session_id, after_seq)

    async def bounds(self, session_id: str, since: int) -> LogBounds | None:
        """Where the log of the session ``session_id`` stands in its workspace's, for a frontend
        that saw the events up to ``since``; ``None`` when that can't be read."""
        check_session_id(session_id)
        return await self._read_or(lambda _: None, self._bounds, session_id, since)

    async def trim(self, session_id: str, limit: int, target: int) -> None:
        check_session_id(session_id)
        message = f"Couldn't trim the events of the session {session_id} in"
        await self._or_raise(message, self._trim, session_id, limit, target)

    async def trimmed(self, session_id: str) -> bool:
        check_session_id(session_id)
        return await self._read_or(lambda _: False, self._trimmed, session_id)

    async def forget(self, session_id: str) -> None:
        check_session_id(session_id)
        message = f"Couldn't delete the events of the session {session_id} from"
        await self._or_raise(message, self._forget, session_id)

    async def acquire(self, session_id: str) -> bool:
        """Lock the session ``session_id`` for this orbit; returns ``False`` when another
        running orbit has it locked.

        Raises ``RuntimeError`` when the lock can't be taken, as once the store is closed.
        """
        return await self._locks.acquire(session_id)

    async def release(self, session_id: str) -> None:
        await self._locks.release(session_id)

    async def review_count(self) -> int:
        return await self._or_raise("Couldn't read the review count from", self._review_count)

    async def save_review_count(self, count: int) -> None:
        message = "Couldn't save the review count to"
        await self._or_raise(message, self._save_review_count, count, failed=True)

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
        makes of the error when the database fails."""
        try:
            return await self._call(function, *args)
        except sqlite3.Error as error:
            return fallback(error)

    async def _or_raise[*A, T](
        self, message: str, function: Callable[[*A], T], *args: *A, failed: bool = False
    ) -> T:
        """What ``function(*args)`` returns, run on the database's thread.

        Raises, saying ``message`` and the database, when the database fails: a
        :class:`StorageFailed` when ``failed``, as for a write that halts the session, or else a
        ``RuntimeError``.
        """
        try:
            return await self._call(function, *args)
        except sqlite3.Error as error:
            text = self._message(message, error)
            raise (self._failed(text, error) if failed else RuntimeError(text)) from error

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

    def _connect(self, workspace: str) -> None:
        self._connection, self._workspace = open_database(self._database, workspace)
        self._max_seq = _max_seq(self._connection)
        self._lock_dir = self._state_dir / LOCKS / self._workspace
        self._locks = SessionLocks(self._lock_dir)

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
        with reading() as new, transaction(self._db, committing):
            saved = sum(self._insert_row(row) for row in new.rows)
            if new.review_count is not None:
                self._db.execute(
                    """INSERT INTO reviews (workspace_id, since_super) VALUES (?, ?)
                    ON CONFLICT (workspace_id) DO NOTHING""",
                    (self._workspace, new.review_count),
                )
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

    def _load(self, session_id: str) -> SessionRecord | Unreadable:
        row = self._db.execute(
            "SELECT record FROM sessions WHERE workspace_id = ? AND id = ?",
            (self._workspace, session_id),
        ).fetchone()
        return Unreadable("it isn't saved") if row is None else parse_record(str(row[0]))

    def _rows(self) -> list[tuple[str, str]]:
        rows = self._db.execute(
            "SELECT id, record FROM sessions WHERE workspace_id = ? ORDER BY id",
            (self._workspace,),
        )
        return [(str(session_id), str(text)) for session_id, text in rows]

    def _append(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> int:
        """Add ``envelopes`` to the session ``session_id``'s log, but for those whose ``seq`` it
        holds already, as when parked events are imported again; returns how many it added."""
        rows = [_event_row(self._workspace, session_id, envelope) for envelope in envelopes]
        with transaction(self._db):
            inserted = [row for row in rows if self._insert_event(row)]
            added = sum(row[5] for row in inserted)
            self._db.execute(
                """INSERT INTO logs (workspace_id, session_id, size) VALUES (?, ?, ?)
                ON CONFLICT (workspace_id, session_id) DO UPDATE SET size = size + excluded.size""",
                (self._workspace, session_id, added),
            )
        self._max_seq = max([self._max_seq, *(envelope.seq for envelope in envelopes)])
        return len(inserted)

    def _latest_seq(self) -> int:
        """The highest ``seq`` in the log now, counted in :meth:`max_seq` from then on."""
        self._max_seq = max(self._max_seq, _max_seq(self._db))
        return self._max_seq

    def _insert_event(self, row: EventRow) -> bool:
        """Add ``row`` to the ``events`` table unless its ``seq`` is there; returns whether it
        was added."""
        cursor = self._db.execute(
            """INSERT INTO events (workspace_id, session_id, seq, type, json, size)
            VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
            row,
        )
        return cursor.rowcount > 0

    def _recent(self, session_id: str, limit: int) -> list[EventEnvelope]:
        """The session ``session_id``'s last ``limit`` events, oldest first, but for those of
        types this orbit doesn't know."""
        rows = self._db.execute(
            """SELECT json FROM events WHERE workspace_id = ? AND session_id = ?
            ORDER BY seq DESC LIMIT ?""",
            (self._workspace, session_id, limit),
        )
        return _decoded([str(text) for (text,) in rows])

    def _before(
        self, session_id: str, before_seq: int, limit: int
    ) -> tuple[list[EventEnvelope], bool]:
        """The session ``session_id``'s last ``limit`` events before ``before_seq``, oldest
        first, but for those of types this orbit doesn't know, and whether earlier ones remain."""
        rows = self._db.execute(
            """SELECT json FROM events WHERE workspace_id = ? AND session_id = ? AND seq < ?
            ORDER BY seq DESC LIMIT ?""",
            (self._workspace, session_id, before_seq, limit + 1),
        )
        texts = [str(text) for (text,) in rows]
        return _decoded(texts[:limit]), len(texts) > limit

    def _after(self, session_id: str, after_seq: int) -> list[EventEnvelope]:
        """The session ``session_id``'s events after ``after_seq``, oldest first, but for those
        of types this orbit doesn't know."""
        rows = self._db.execute(
            """SELECT json FROM events WHERE workspace_id = ? AND session_id = ? AND seq > ?
            ORDER BY seq DESC""",
            (self._workspace, session_id, after_seq),
        )
        return _decoded([str(text) for (text,) in rows])

    def _trim(self, session_id: str, limit: int, target: int) -> None:
        """Delete the session ``session_id``'s oldest events, if its log is over ``limit``
        bytes, keeping the latest that together come under ``target``, and always its newest:
        a newest event of ``target`` bytes or more is kept alone, over ``target`` or even
        ``limit`` until a later one is appended."""
        key = (self._workspace, session_id)
        with transaction(self._db):
            row = self._db.execute(
                "SELECT size FROM logs WHERE workspace_id = ? AND session_id = ?", key
            ).fetchone()
            if row is None or row[0] <= limit:
                return
            # An event goes when it and those after it come to the target or more, but the
            # newest stays so that reopening the session shows where it was.
            (cut,) = self._db.execute(
                """SELECT max(seq) FROM (
                    SELECT seq, sum(size) OVER (ORDER BY seq DESC) AS kept,
                        row_number() OVER (ORDER BY seq DESC) AS place FROM events
                    WHERE workspace_id = ? AND session_id = ?
                ) WHERE kept >= ? AND place > 1""",
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

    def _bounds(self, session_id: str, since: int) -> LogBounds:
        """The bounds of the session ``session_id``'s log in the workspace's, for a frontend that
        saw the events up to ``since``."""
        (first,) = self._db.execute(
            "SELECT min(seq) FROM events WHERE workspace_id = ? AND session_id = ?",
            (self._workspace, session_id),
        ).fetchone()
        (others,) = self._db.execute(
            "SELECT max(seq) FROM events WHERE workspace_id = ? AND session_id != ?",
            (self._workspace, session_id),
        ).fetchone()
        # The last event trimmed was the session's too.
        (own,) = self._db.execute(
            """SELECT max(seq) FROM (
                SELECT seq FROM events WHERE workspace_id = ?1 AND session_id = ?2 AND seq <= ?3
                UNION ALL SELECT trimmed_seq FROM logs
                    WHERE workspace_id = ?1 AND session_id = ?2
                    AND trimmed_seq > 0 AND trimmed_seq <= ?3
            )""",
            (self._workspace, session_id, since),
        ).fetchone()
        return LogBounds(first_seq=first, others_seq=int(others or 0), own_seq=own)

    def _forget(self, session_id: str) -> None:
        with transaction(self._db):
            self._forget_log(session_id)

    def _forget_log(self, session_id: str) -> None:
        """Delete the session ``session_id``'s events and its row in ``logs``, within a
        transaction."""
        self._db.execute(
            "DELETE FROM events WHERE workspace_id = ? AND session_id = ?",
            (self._workspace, session_id),
        )
        self._db.execute(
            "DELETE FROM logs WHERE workspace_id = ? AND session_id = ?",
            (self._workspace, session_id),
        )

    def _delete(self, session_id: str) -> None:
        with transaction(self._db):
            self._forget_log(session_id)
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
        """The sessions saved as ``rows``, each marked locked when another orbit holds it.

        Raises ``RuntimeError`` when the locks can't be checked.
        """
        try:
            self._lock_dir.mkdir(parents=True, exist_ok=True)
            with self._locks.checking():
                return [
                    StoredSession(
                        session_id, parse_record(text), self._locks.held_elsewhere(session_id)
                    )
                    for session_id, text in rows
                ]
        except OSError as error:
            message = f"Couldn't list the sessions in {self._database}: their locks can't be"
            raise RuntimeError(f"{message} checked: {error}") from error
