"""The SQLite store: its database, workspaces, migrations, failures and its thread."""

import asyncio
import errno
import sqlite3
import subprocess
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbit.agent.application.ports import SessionStore, StorageFailed, StoredSession
from orbit.agent.domain.sessions import SessionRecord, Unreadable
from orbit.agent.domain.storage_halt import StorageErrorKind
from orbit.agent.external import sqlite_schema, sqlite_store
from orbit.agent.external.sqlite_errors import error_kind
from orbit.agent.external.sqlite_schema import MIGRATIONS, StoreError, transaction
from orbit.agent.external.sqlite_store import THREAD_PREFIX, SqliteStore, find_workspace_id
from orbit.protocol import EventEnvelope, Notice

WHEN = datetime(2026, 10, 17, 14, 30, tzinfo=UTC)

type Opener = Callable[[Path], Awaitable[SqliteStore]]


def record(session_id: str = "abc") -> SessionRecord:
    return SessionRecord(id=session_id, name="Oct 17 14:30", created=WHEN, last_active=WHEN)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state" / "orbit"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture
async def open_store(state: Path) -> AsyncIterator[Opener]:
    """Opens a store in ``state`` for a workspace, closing it after the test."""
    opened: list[SqliteStore] = []

    async def open_at(workspace: Path) -> SqliteStore:
        opened.append(await SqliteStore.open(state, str(workspace)))
        return opened[-1]

    yield open_at
    for store in opened:
        await store.close()


@contextmanager
def database(state: Path) -> Iterator[sqlite3.Connection]:
    """Another connection to the store's database, in autocommit mode."""
    with closing(sqlite3.connect(state / "orbit.db", isolation_level=None)) as connection:
        yield connection


def store_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name.startswith(THREAD_PREFIX)]


async def test_the_sqlite_store_is_a_session_store(open_store: Opener, workspace: Path) -> None:
    store: SessionStore = await open_store(workspace)
    assert store is not None


# The database


async def test_the_state_directory_and_database_are_the_owners_alone_in_wal_mode(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    await open_store(workspace)
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "orbit.db").stat().st_mode & 0o777 == 0o600
    with database(state) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


async def test_sessions_and_the_review_count_survive_closing(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.save(record())
    await store.save_review_count(2)
    await store.close()
    reopened = await open_store(workspace)
    assert await reopened.load("abc") == record()
    assert await reopened.review_count() == 2


async def test_each_workspace_has_its_own_sessions_and_review_count(
    open_store: Opener, tmp_path: Path, workspace: Path
) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    store, other = await open_store(workspace), await open_store(other_workspace)
    await store.save(record("mine"))
    await other.save(record("theirs"))
    await store.save_review_count(3)
    assert [entry.id for entry in await store.sessions()] == ["mine"]
    assert [entry.id for entry in await other.sessions()] == ["theirs"]
    assert isinstance(await other.load("mine"), Unreadable)
    assert await other.review_count() == 0
    assert await store.acquire("same") and await other.acquire("same")


async def test_a_workspace_is_registered_once_by_path_and_its_fingerprint_kept_current(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    await (await open_store(workspace)).close()
    git(workspace, "init", "-q", "-b", "main")
    git(workspace, "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-q", "--allow-empty",
        "-m", "x")  # fmt: skip
    await open_store(workspace / ".")
    with database(state) as connection:
        rows = connection.execute("SELECT path, fingerprint, name FROM workspaces").fetchall()
    assert rows == [(str(workspace), f"{git(workspace, 'rev-parse', 'HEAD')} ", "project")]


async def test_a_registered_workspaces_id_is_found_by_its_path(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    assert find_workspace_id(state, str(workspace / ".")) == store.workspace_id


async def test_an_unregistered_workspace_is_found_nowhere_and_left_unregistered(
    open_store: Opener, state: Path, workspace: Path, tmp_path: Path
) -> None:
    await open_store(workspace)
    assert find_workspace_id(state, str(tmp_path)) is None
    with database(state) as connection:
        assert connection.execute("SELECT count(*) FROM workspaces").fetchone() == (1,)


def test_without_a_database_no_workspace_is_found_and_none_is_made(
    state: Path, workspace: Path
) -> None:
    assert find_workspace_id(state, str(workspace)) is None
    assert not state.exists()


def test_in_a_database_that_cant_be_read_no_workspace_is_found(
    state: Path, workspace: Path
) -> None:
    state.mkdir(parents=True)
    (state / "orbit.db").write_text("not a database")
    assert find_workspace_id(state, str(workspace)) is None


async def test_a_record_that_isnt_json_is_unreadable_but_still_listed(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.save(record("b"))
    with database(state) as connection:
        connection.execute("UPDATE sessions SET record = '{oops'")
    loaded = await store.load("b")
    assert isinstance(loaded, Unreadable)
    assert loaded.reason.startswith("it isn't JSON")
    assert await store.sessions() == [StoredSession("b", loaded, locked=False)]


async def test_a_missing_session_isnt_saved(open_store: Opener, workspace: Path) -> None:
    assert await (await open_store(workspace)).load("abc") == Unreadable("it isn't saved")


# Locks


async def test_locks_live_in_the_workspaces_directory_under_the_state_directory(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    await (await open_store(workspace)).acquire("abc")
    assert [path.name for path in (state / "locks").glob("*/*")] == ["abc.lock"]


async def test_closing_lets_go_of_the_stores_locks(open_store: Opener, workspace: Path) -> None:
    store, other = await open_store(workspace), await open_store(workspace)
    assert await store.acquire("abc")
    assert not await other.acquire("abc")
    await store.close()
    assert await other.acquire("abc")


async def test_sessions_whose_locks_cant_be_checked_fail_to_list(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.save(record())
    (state / "locks").write_text("in the way")
    with pytest.raises(RuntimeError, match=r"Couldn't list the sessions in .*orbit\.db: their"):
        await store.sessions()


# Failures in use


async def test_a_broken_database_fails_each_call_as_the_port_says(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    with database(state) as connection:
        connection.execute("DROP TABLE sessions")
        connection.execute("DROP TABLE reviews")
    with pytest.raises(RuntimeError, match=r"Couldn't save the session abc to .*orbit\.db"):
        await store.save(record())
    loaded = await store.load("abc")
    assert isinstance(loaded, Unreadable)
    assert loaded.reason.startswith("it can't be read from ")
    with pytest.raises(RuntimeError, match=r"Couldn't list the sessions in .*orbit\.db"):
        await store.sessions()
    with pytest.raises(RuntimeError, match=r"Couldn't delete the session abc from .*orbit\.db"):
        await store.delete("abc")
    with pytest.raises(RuntimeError, match=r"Couldn't read the review count from .*orbit\.db"):
        await store.review_count()
    with pytest.raises(RuntimeError, match=r"Couldn't save the review count to .*orbit\.db"):
        await store.save_review_count(1)


async def test_a_closed_store_fails_each_call_as_the_port_says(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.save(record())
    await store.close()
    with pytest.raises(RuntimeError, match=r"Couldn't save the session abc to .*orbit\.db"):
        await store.save(record())
    assert await store.load("abc") == Unreadable(
        f"it can't be read from {state / 'orbit.db'}: the store is closed"
    )
    with pytest.raises(RuntimeError, match=r"Couldn't list the sessions in .*orbit\.db"):
        await store.sessions()
    with pytest.raises(RuntimeError, match=r"Couldn't delete the session abc from .*orbit\.db"):
        await store.delete("abc")
    with pytest.raises(RuntimeError, match=r"Couldn't read the review count from .*orbit\.db"):
        await store.review_count()
    with pytest.raises(RuntimeError, match=r"Couldn't save the review count to .*orbit\.db"):
        await store.save_review_count(1)


async def test_acquiring_after_closing_raises_and_takes_no_lock(
    open_store: Opener, workspace: Path
) -> None:
    store, other = await open_store(workspace), await open_store(workspace)
    await store.close()
    with pytest.raises(RuntimeError, match="closed"):
        await store.acquire("abc")
    assert await other.acquire("abc")


# Failures at open


async def test_a_newer_database_is_refused_naming_both_versions(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    await (await open_store(workspace)).close()
    with database(state) as connection:
        connection.execute("INSERT INTO schema_version (version) VALUES (99)")
    with pytest.raises(StoreError, match=r"orbit\.db has schema version 99, newer than .* 3;"):
        await open_store(workspace)
    assert store_threads() == []


def racing(
    monkeypatch: pytest.MonkeyPatch, state: Path, meanwhile: Callable[[sqlite3.Connection], object]
) -> None:
    """Makes another connection run ``meanwhile`` once the store has read its schema version."""
    read = sqlite_schema._schema_version

    def read_then_race(connection: sqlite3.Connection) -> int:
        version = read(connection)
        with database(state) as other:
            meanwhile(other)
        return version

    monkeypatch.setattr(sqlite_schema, "_schema_version", read_then_race)


async def test_a_migration_applied_meanwhile_by_another_orbit_isnt_run_again(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, state: Path, workspace: Path
) -> None:
    racing(monkeypatch, state, lambda other: sqlite_schema._apply(other, 1, state / "orbit.db"))
    await open_store(workspace)
    with database(state) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [
            (1,),
            (2,),
            (3,),
        ]


async def test_a_newer_version_written_meanwhile_is_refused(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, state: Path, workspace: Path
) -> None:
    def newer(other: sqlite3.Connection) -> None:
        other.execute("INSERT INTO schema_version (version) VALUES (99)")

    racing(monkeypatch, state, newer)
    with pytest.raises(StoreError, match=r"orbit\.db has schema version 99, newer than .* 3;"):
        await open_store(workspace)
    with database(state) as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE name = 'workspaces'")
        assert tables.fetchall() == []


async def test_stores_opened_at_once_on_a_new_state_directory_both_open(
    open_store: Opener, state: Path, tmp_path: Path
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    await asyncio.gather(open_store(first), open_store(second))
    with database(state) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [
            (1,),
            (2,),
            (3,),
        ]
        assert connection.execute("SELECT count(*) FROM workspaces").fetchone() == (2,)


async def test_a_file_that_isnt_a_database_is_refused_naming_it(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    state.mkdir(parents=True)
    (state / "orbit.db").write_bytes(b"not a database at all, " * 100)
    with pytest.raises(StoreError, match=r"Couldn't open the database .*orbit\.db: "):
        await open_store(workspace)


async def test_a_database_that_cant_be_created_is_refused(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    state.parent.mkdir(parents=True)
    state.write_text("in the way")
    with pytest.raises(StoreError, match=r"Couldn't create the database .*orbit\.db: "):
        await open_store(workspace)


async def test_a_database_that_cant_be_connected_to_is_refused(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, workspace: Path
) -> None:
    def refused(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", refused)
    with pytest.raises(StoreError, match=r"Couldn't open the database .*: unable to open"):
        await open_store(workspace)


async def test_a_failed_migration_is_rolled_back_and_refused(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, state: Path, workspace: Path
) -> None:
    await (await open_store(workspace)).close()
    broken = (*MIGRATIONS, ("CREATE TABLE extra (x)", "NOT SQL"))
    monkeypatch.setattr(sqlite_schema, "MIGRATIONS", broken)
    with pytest.raises(StoreError, match=r"upgrade the database .*orbit\.db to schema version 4"):
        await open_store(workspace)
    with database(state) as connection:
        assert connection.execute("SELECT max(version) FROM schema_version").fetchone() == (3,)
        tables = connection.execute("SELECT name FROM sqlite_master WHERE name = 'extra'")
        assert tables.fetchall() == []


def locked_new_database(state: Path) -> sqlite3.Connection:
    """Another connection to a new, empty database in ``state``, not yet in WAL mode, holding
    its write lock."""
    state.mkdir(parents=True)
    connection = sqlite3.connect(state / "orbit.db", isolation_level=None)
    connection.execute("CREATE TABLE held (x)")
    connection.execute("BEGIN IMMEDIATE")
    return connection


async def test_a_switch_to_wal_mode_refused_at_once_is_tried_until_it_works(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    with closing(locked_new_database(state)) as connection:
        opening = asyncio.ensure_future(open_store(workspace))
        await asyncio.sleep(0.1)
        assert not opening.done()
        connection.execute("ROLLBACK")
        await opening
    with database(state) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


async def test_a_switch_to_wal_mode_refused_for_too_long_is_refused(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, state: Path, workspace: Path
) -> None:
    monkeypatch.setattr(sqlite_schema, "BUSY_TIMEOUT", 0.05)
    refused = pytest.raises(StoreError, match=r"Couldn't open the database .*: database is locked")
    with closing(locked_new_database(state)), refused:
        await open_store(workspace)


async def test_a_migration_that_cant_start_is_refused(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, state: Path, workspace: Path
) -> None:
    state.mkdir(parents=True)
    with database(state) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("BEGIN IMMEDIATE")  # Another writer holds the database.
        monkeypatch.setattr(sqlite_schema, "BUSY_TIMEOUT", 0.05)
        with pytest.raises(StoreError, match=r"to schema version 1: database is locked"):
            await open_store(workspace)


async def test_a_store_that_fails_to_open_closes_its_connection(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, workspace: Path
) -> None:
    def fail(path: str) -> str:
        raise RuntimeError("git broke")

    monkeypatch.setattr(sqlite_schema, "workspace_fingerprint", fail)
    with pytest.raises(RuntimeError, match="git broke"):
        await open_store(workspace)
    assert store_threads() == []


# The database's thread


async def test_closing_while_a_call_is_in_flight_finishes_it_and_leaves_no_thread(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    with database(state) as connection:
        connection.execute("BEGIN IMMEDIATE")  # Holds the save mid-flight.
        saving = asyncio.create_task(store.save(record()))
        await asyncio.sleep(0.05)
        closing_store = asyncio.create_task(store.close())
        await asyncio.sleep(0.05)
        assert not closing_store.done()
        connection.execute("ROLLBACK")
        await closing_store
        await saving
    assert store_threads() == []
    assert await (await open_store(workspace)).load("abc") == record()


async def test_a_cancelled_call_leaves_the_store_usable(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    with database(state) as connection:
        connection.execute("BEGIN IMMEDIATE")
        saving = asyncio.create_task(store.save(record("first")))
        await asyncio.sleep(0.05)
        saving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await saving
        connection.execute("ROLLBACK")
    await store.save(record("second"))
    assert [entry.id for entry in await store.sessions()] == ["first", "second"]


# Backups


async def backed_up(state: Path, target: Path) -> None:
    """Back up the database in ``state`` to ``target``, opened for a backup alone."""
    store = await SqliteStore.open_for_backup(state)
    try:
        await store.backup(target)
    finally:
        await store.close()


async def test_a_backup_holds_the_sessions_and_is_the_owners_alone(
    open_store: Opener, state: Path, workspace: Path, tmp_path: Path
) -> None:
    store = await open_store(workspace)
    await store.save(record("first"))
    await store.save(record("second"))
    await backed_up(state, tmp_path / "copy.db")
    assert (tmp_path / "copy.db").stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(tmp_path / "copy.db")) as connection:
        rows = connection.execute("SELECT id FROM sessions ORDER BY id").fetchall()
    assert rows == [("first",), ("second",)]


async def test_a_backup_includes_what_another_open_store_has_written(
    open_store: Opener, state: Path, workspace: Path, tmp_path: Path
) -> None:
    store = await open_store(workspace)
    await store.save(record())
    await backed_up(state, tmp_path / "copy.db")
    await store.save(record("later"))
    with closing(sqlite3.connect(tmp_path / "copy.db")) as connection:
        assert connection.execute("SELECT id FROM sessions").fetchall() == [("abc",)]


async def test_a_backup_replaces_an_existing_file_and_makes_it_the_owners_alone(
    open_store: Opener, state: Path, workspace: Path, tmp_path: Path
) -> None:
    await (await open_store(workspace)).save(record())
    target = tmp_path / "copy.db"
    target.write_text("old")
    target.chmod(0o644)
    await backed_up(state, target)
    assert target.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("SELECT id FROM sessions").fetchall() == [("abc",)]


async def test_opening_for_a_backup_registers_no_workspace(
    open_store: Opener, state: Path, workspace: Path, tmp_path: Path
) -> None:
    await open_store(workspace)
    with database(state) as connection:
        connection.execute("DELETE FROM workspaces")
    await backed_up(state, tmp_path / "copy.db")
    with database(state) as connection:
        assert connection.execute("SELECT count(*) FROM workspaces").fetchone() == (0,)


async def test_opening_a_missing_database_for_a_backup_is_refused_creating_nothing(
    state: Path,
) -> None:
    with pytest.raises(StoreError, match=r"no database .*orbit\.db, so nothing to back up"):
        await SqliteStore.open_for_backup(state)
    assert not state.exists()
    assert store_threads() == []


async def test_a_database_that_cant_be_opened_for_a_backup_is_refused(state: Path) -> None:
    (state / "orbit.db").mkdir(parents=True)
    with pytest.raises(StoreError, match=r"Couldn't open the database .*orbit\.db: "):
        await SqliteStore.open_for_backup(state)


async def test_a_backup_that_cant_be_written_is_refused_naming_the_target(
    open_store: Opener, state: Path, workspace: Path, tmp_path: Path
) -> None:
    await open_store(workspace)
    target = tmp_path / "missing" / "copy.db"
    with pytest.raises(StoreError, match=r"Couldn't back up the database .* to .*copy\.db: "):
        await backed_up(state, target)


def stored_ids(state: Path) -> list[str]:
    with database(state) as connection:
        rows = connection.execute("SELECT id FROM sessions ORDER BY id").fetchall()
    return [str(session_id) for (session_id,) in rows]


@pytest.mark.parametrize("name", ["orbit.db", "orbit.db-wal", "orbit.db-shm"])
async def test_a_backup_onto_the_database_or_its_files_is_refused(
    open_store: Opener, state: Path, workspace: Path, name: str
) -> None:
    await (await open_store(workspace)).save(record())
    with pytest.raises(
        StoreError, match=r"Won't back up the database .*orbit\.db onto itself: .*orbit\.db"
    ):
        await backed_up(state, state / name)
    assert stored_ids(state) == ["abc"]


async def test_a_backup_onto_a_link_to_the_database_is_refused(
    open_store: Opener, state: Path, workspace: Path, tmp_path: Path
) -> None:
    await (await open_store(workspace)).save(record())
    link = tmp_path / "link.db"
    link.symlink_to(state / "orbit.db")
    with pytest.raises(StoreError, match="Won't back up the database"):
        await backed_up(state, link)
    assert stored_ids(state) == ["abc"]


async def test_a_backup_onto_a_hard_link_to_the_database_is_refused(
    open_store: Opener, state: Path, workspace: Path, tmp_path: Path
) -> None:
    await (await open_store(workspace)).save(record())
    link = tmp_path / "link.db"
    link.hardlink_to(state / "orbit.db")
    with pytest.raises(StoreError, match="Won't back up the database"):
        await backed_up(state, link)
    assert stored_ids(state) == ["abc"]


class FailingBackup(sqlite3.Connection):
    """A connection whose backups copy everything and then fail, as an I/O error would."""

    def backup(self, target: sqlite3.Connection, **kwargs: object) -> None:
        super().backup(target)
        raise sqlite3.OperationalError("disk I/O error")


def failing_backups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Have stores opened for a backup fail their backups."""

    def open_existing(database: Path) -> sqlite3.Connection:
        return sqlite3.connect(database, factory=FailingBackup, check_same_thread=False)

    monkeypatch.setattr(sqlite_store, "open_existing", open_existing)


async def test_a_failed_backup_keeps_the_file_it_would_have_replaced(
    monkeypatch: pytest.MonkeyPatch,
    open_store: Opener,
    state: Path,
    workspace: Path,
    tmp_path: Path,
) -> None:
    await (await open_store(workspace)).save(record())
    target = tmp_path / "copies" / "copy.db"
    target.parent.mkdir()
    target.write_text("old")
    failing_backups(monkeypatch)
    with pytest.raises(StoreError, match=r"Couldn't back up the database .* to .*copy\.db: disk"):
        await backed_up(state, target)
    assert target.read_text() == "old"
    assert list(target.parent.iterdir()) == [target]


async def test_a_failed_backup_leaves_no_file_behind(
    monkeypatch: pytest.MonkeyPatch,
    open_store: Opener,
    state: Path,
    workspace: Path,
    tmp_path: Path,
) -> None:
    await (await open_store(workspace)).save(record())
    copies = tmp_path / "copies"
    copies.mkdir()
    failing_backups(monkeypatch)
    with pytest.raises(StoreError, match="Couldn't back up the database"):
        await backed_up(state, copies / "copy.db")
    assert list(copies.iterdir()) == []


# Failed writes, described for the storage halt


async def fill_up(store: SqliteStore) -> None:
    """Have ``store``'s database refuse to grow, as a full disk does."""

    def cap() -> None:
        (pages,) = store._db.execute("PRAGMA page_count").fetchone()
        store._db.execute(f"PRAGMA max_page_count = {pages}")

    await store._call(cap)


async def test_a_full_database_fails_its_writes_as_disk_full(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await fill_up(store)
    big = SessionRecord(id="abc", name="x" * 100_000, created=WHEN, last_active=WHEN)
    for write in (store.save(big), store.append("abc", [big_notice()])):
        with pytest.raises(StorageFailed) as failed:
            await write
        assert (failed.value.database, failed.value.error, failed.value.kind) == (
            str(state / "orbit.db"),
            "database or disk is full",
            StorageErrorKind.DISK_FULL,
        )


async def test_a_failed_delete_says_what_the_database_reported(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    with database(state) as connection:
        connection.execute("DROP TABLE sessions")
    with pytest.raises(StorageFailed) as failed:
        await store.delete("abc")
    assert (failed.value.database, failed.value.error, failed.value.kind) == (
        str(state / "orbit.db"),
        "no such table: sessions",
        StorageErrorKind.UNKNOWN,
    )


async def test_a_failed_review_count_save_says_what_the_database_reported(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    with database(state) as connection:
        connection.execute("DROP TABLE reviews")
    with pytest.raises(StorageFailed) as failed:
        await store.save_review_count(1)
    assert (failed.value.error, failed.value.kind) == (
        "no such table: reviews",
        StorageErrorKind.UNKNOWN,
    )


def big_notice() -> EventEnvelope:
    return EventEnvelope(seq=1, session="abc", payload=Notice(message="x" * 100_000))


async def test_the_store_knows_its_workspaces_id(open_store: Opener, workspace: Path) -> None:
    store = await open_store(workspace)
    assert len(store.workspace_id) == 32


def sqlite_error(code: int | None) -> sqlite3.Error:
    error = sqlite3.OperationalError("failed")
    if code is not None:
        error.sqlite_errorcode = code
    return error


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        (sqlite3.SQLITE_FULL, StorageErrorKind.DISK_FULL),
        (sqlite3.SQLITE_READONLY, StorageErrorKind.READ_ONLY),
        (sqlite3.SQLITE_PERM, StorageErrorKind.READ_ONLY),
        (sqlite3.SQLITE_IOERR_WRITE, StorageErrorKind.UNKNOWN),
        (sqlite3.SQLITE_BUSY, StorageErrorKind.LOCKED),
        (sqlite3.SQLITE_LOCKED, StorageErrorKind.LOCKED),
        (sqlite3.SQLITE_CANTOPEN, StorageErrorKind.MISSING),
        (sqlite3.SQLITE_READONLY_DBMOVED, StorageErrorKind.MISSING),
        (sqlite3.SQLITE_READONLY_ROLLBACK, StorageErrorKind.READ_ONLY),
        (sqlite3.SQLITE_CORRUPT, StorageErrorKind.UNKNOWN),
        (None, StorageErrorKind.UNKNOWN),
    ],
)
def test_sqlite_errors_are_told_apart_by_their_result_code(
    code: int | None, kind: StorageErrorKind
) -> None:
    assert error_kind(sqlite_error(code)) == kind


@pytest.mark.parametrize(
    ("number", "kind"),
    [
        (errno.ENOSPC, StorageErrorKind.DISK_FULL),
        (errno.EDQUOT, StorageErrorKind.DISK_FULL),
        (errno.EROFS, StorageErrorKind.READ_ONLY),
        (errno.EACCES, StorageErrorKind.READ_ONLY),
        (errno.EPERM, StorageErrorKind.READ_ONLY),
        (errno.ENOENT, StorageErrorKind.MISSING),
        (errno.EIO, StorageErrorKind.UNKNOWN),
    ],
)
def test_os_errors_are_told_apart_by_their_errno(number: int, kind: StorageErrorKind) -> None:
    assert error_kind(OSError(number, "failed")) == kind


def test_an_os_error_without_an_errno_is_not_recognised() -> None:
    assert error_kind(OSError("failed")) == StorageErrorKind.UNKNOWN


@contextmanager
def scratch() -> Iterator[sqlite3.Connection]:
    """An in-memory database in autocommit mode, with an empty table ``t``."""
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        connection.execute("CREATE TABLE t (x)")
        yield connection


def test_a_transaction_whose_block_raises_is_rolled_back() -> None:
    with scratch() as connection:
        with pytest.raises(RuntimeError, match="broke"), transaction(connection):
            connection.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("broke")
        assert not connection.in_transaction
        assert connection.execute("SELECT count(*) FROM t").fetchone() == (0,)


def test_a_transaction_commits_inside_the_committing_context() -> None:
    seen: list[str] = []

    @contextmanager
    def committing() -> Iterator[None]:
        seen.append(f"before, in a transaction: {connection.in_transaction}")
        yield
        seen.append(f"after, in a transaction: {connection.in_transaction}")

    with scratch() as connection:
        with transaction(connection, committing):
            connection.execute("INSERT INTO t VALUES (1)")
        assert connection.execute("SELECT count(*) FROM t").fetchone() == (1,)
    assert seen == ["before, in a transaction: True", "after, in a transaction: False"]


def test_a_transaction_whose_commit_is_refused_is_rolled_back() -> None:
    @contextmanager
    def refusing() -> Iterator[None]:
        raise RuntimeError("not now")
        yield

    with scratch() as connection:
        with pytest.raises(RuntimeError, match="not now"), transaction(connection, refusing):
            connection.execute("INSERT INTO t VALUES (1)")
        assert not connection.in_transaction
        assert connection.execute("SELECT count(*) FROM t").fetchone() == (0,)
