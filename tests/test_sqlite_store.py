"""The SQLite store: its database, workspaces, failures, backups and its thread."""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractContextManager, closing, nullcontext
from pathlib import Path

import pytest

from orbit.agent.application.ports import (
    LockLeft,
    ReadFailed,
    SessionStore,
    StorageFailed,
    StoredSession,
    StoreFailed,
)
from orbit.agent.domain.sessions import SessionRecord, Unreadable
from orbit.agent.domain.storage_halt import StorageErrorKind
from orbit.agent.external import sqlite_store
from orbit.agent.external.sqlite_schema import StoreError
from orbit.agent.external.sqlite_store import NewRows, SqliteStore
from orbit.protocol import EventEnvelope, Notice
from tests.git_repo import git
from tests.store_views import (
    WHEN,
    Opener,
    OwnView,
    database,
    fill_up,
    record,
    store_threads,
)


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


# Views of one store


@pytest.fixture
async def shared(state: Path) -> AsyncIterator[SqliteStore]:
    """One store in ``state``, shared by the views of a test, closed after it."""
    store = await SqliteStore.open(state)
    yield store
    await store.close()


async def test_two_views_of_one_store_see_only_their_own_sessions_and_review_counts(
    shared: SqliteStore,
) -> None:
    mine, theirs = shared.workspace_view("mine"), shared.workspace_view("theirs")
    await register_ids(shared, "mine", "theirs")
    await mine.save(record("a"))
    await theirs.save(record("b"))
    await mine.save_review_count(3)
    assert [entry.id for entry in await mine.sessions()] == ["a"]
    assert [entry.id for entry in await theirs.sessions()] == ["b"]
    assert isinstance(await theirs.load("a"), Unreadable)
    assert (await mine.review_count(), await theirs.review_count()) == (3, 0)
    assert (mine.workspace_id, theirs.workspace_id) == ("mine", "theirs")


async def test_two_views_of_one_store_see_only_their_own_events_but_share_one_seq(
    shared: SqliteStore,
) -> None:
    mine, theirs = shared.workspace_view("mine"), shared.workspace_view("theirs")
    await register_ids(shared, "mine", "theirs")
    await mine.append("abc", [EventEnvelope(seq=1, session="abc", payload=Notice(message="a"))])
    await theirs.append("abc", [EventEnvelope(seq=2, session="abc", payload=Notice(message="b"))])
    assert [envelope.seq for envelope in await mine.recent("abc", 10)] == [1]
    assert [envelope.seq for envelope in await theirs.recent("abc", 10)] == [2]
    assert (mine.max_seq(), theirs.max_seq(), shared.max_seq()) == (2, 2, 2)
    assert await mine.latest_seq() == 2


async def test_two_views_of_one_workspace_share_its_locks(shared: SqliteStore, state: Path) -> None:
    first, second = shared.workspace_view("w"), shared.workspace_view("w")
    assert await first.acquire("abc")
    assert await second.acquire("abc")
    other = await SqliteStore.open(state)
    try:
        assert not await other.workspace_view("w").acquire("abc")
    finally:
        await other.close()


async def test_a_view_made_after_the_store_closed_takes_no_lock(state: Path) -> None:
    store = await SqliteStore.open(state)
    await store.close()
    with pytest.raises(StoreFailed, match="closed"):
        await store.workspace_view("w").acquire("abc")


async def register_ids(store: SqliteStore, *workspace_ids: str) -> None:
    """Register workspaces with ``workspace_ids`` in ``store``'s database, at made-up paths."""

    def insert(connection: sqlite3.Connection) -> None:
        for workspace_id in workspace_ids:
            connection.execute(
                "INSERT INTO workspaces (id, path, name) VALUES (?, ?, ?)",
                (workspace_id, f"/{workspace_id}", workspace_id),
            )

    await store.run("Couldn't register", insert)


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
    with pytest.raises(ReadFailed, match=r"Couldn't list the sessions in .*orbit\.db: their"):
        await store.sessions()


# Failures in use


async def test_a_broken_database_fails_each_call_as_the_port_says(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    with database(state) as connection:
        connection.execute("DROP TABLE sessions")
        connection.execute("DROP TABLE reviews")
    with pytest.raises(StorageFailed, match=r"Couldn't save the session abc to .*orbit\.db"):
        await store.save(record())
    loaded = await store.load("abc")
    assert isinstance(loaded, Unreadable)
    assert loaded.reason.startswith("it can't be read from ")
    with pytest.raises(ReadFailed, match=r"Couldn't list the sessions in .*orbit\.db"):
        await store.sessions()
    with pytest.raises(StorageFailed, match=r"Couldn't delete the session abc from .*orbit\.db"):
        await store.delete("abc")
    with pytest.raises(ReadFailed, match=r"Couldn't read the review count from .*orbit\.db"):
        await store.review_count()
    with pytest.raises(StorageFailed, match=r"Couldn't save the review count to .*orbit\.db"):
        await store.save_review_count(1)


async def test_a_delete_whose_lock_file_cannot_be_removed_deletes_and_says_so(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.save(record())
    await store.acquire("other")
    [locks] = (state / "locks").iterdir()
    (locks / "abc.lock").mkdir()
    with pytest.raises(LockLeft, match=r"Couldn't delete the session .*abc\.lock"):
        await store.delete("abc")
    assert await store.sessions() == []


async def test_a_closed_store_fails_each_call_as_the_port_says(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.save(record())
    await store.close()
    with pytest.raises(StorageFailed, match=r"Couldn't save the session abc to .*orbit\.db"):
        await store.save(record())
    assert await store.load("abc") == Unreadable(
        f"it can't be read from {state / 'orbit.db'}: the store is closed"
    )
    with pytest.raises(ReadFailed, match=r"Couldn't list the sessions in .*orbit\.db"):
        await store.sessions()
    with pytest.raises(StorageFailed, match=r"Couldn't delete the session abc from .*orbit\.db"):
        await store.delete("abc")
    with pytest.raises(ReadFailed, match=r"Couldn't read the review count from .*orbit\.db"):
        await store.review_count()
    with pytest.raises(StorageFailed, match=r"Couldn't save the review count to .*orbit\.db"):
        await store.save_review_count(1)


type StoreCall = Callable[[OwnView], Awaitable[object]]
type Outcome = type[StoreFailed] | Callable[[Path], object]


@pytest.mark.parametrize(
    ("call", "outcome"),
    [
        (lambda store: store.save(record()), StorageFailed),
        (
            lambda store: store.load("abc"),
            lambda database: Unreadable(f"it can't be read from {database}: the store is closed"),
        ),
        (lambda store: store.sessions(), ReadFailed),
        (lambda store: store.delete("abc"), StorageFailed),
        (lambda store: store.latest_seq(), ReadFailed),
        (lambda store: store.append("abc", [big_notice()]), StorageFailed),
        (lambda store: store.recent("abc", 10), ReadFailed),
        (lambda store: store.before("abc", 10, 10), ReadFailed),
        (lambda store: store.after("abc", 0), lambda _: None),
        (lambda store: store.bounds("abc", 0), lambda _: None),
        (lambda store: store.trim("abc", 10, 5), StorageFailed),
        (lambda store: store.trimmed("abc"), ReadFailed),
        (lambda store: store.forget("abc"), StorageFailed),
        (lambda store: store.review_count(), ReadFailed),
        (lambda store: store.save_review_count(1), StorageFailed),
    ],
)
async def test_each_call_on_a_closed_store_has_its_own_failure(
    open_store: Opener, state: Path, workspace: Path, call: StoreCall, outcome: Outcome
) -> None:
    store = await open_store(workspace)
    await store.close()
    if not isinstance(outcome, type):
        assert await call(store) == outcome(state / "orbit.db")
        return
    with pytest.raises(outcome) as raised:
        await call(store)
    assert type(raised.value) is outcome
    assert str(raised.value).endswith(f"{state / 'orbit.db'}: the store is closed")
    if isinstance(raised.value, StorageFailed):
        assert (raised.value.database, raised.value.error, raised.value.kind) == (
            str(state / "orbit.db"),
            "the store is closed",
            StorageErrorKind.UNKNOWN,
        )


def no_rows() -> AbstractContextManager[NewRows]:
    return nullcontext(NewRows((), None))


async def test_an_import_into_a_closed_store_fails_as_a_write(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.close()
    with pytest.raises(StorageFailed) as raised:
        await store.insert_new(no_rows, nullcontext)
    assert str(raised.value).endswith(f"{state / 'orbit.db'}: the store is closed")
    assert raised.value.error == "the store is closed"
    assert isinstance(raised.value.__cause__, sqlite3.ProgrammingError)


async def test_a_read_that_falls_back_logs_why(
    open_store: Opener, state: Path, workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = await open_store(workspace)
    await store.close()
    with caplog.at_level(logging.WARNING):
        assert await store.after("abc", 0) is None
    assert f"Couldn't read from {state / 'orbit.db'}: the store is closed" in caplog.text


async def test_acquiring_after_closing_raises_and_takes_no_lock(
    open_store: Opener, workspace: Path
) -> None:
    store, other = await open_store(workspace), await open_store(workspace)
    await store.close()
    with pytest.raises(StoreFailed, match="closed"):
        await store.acquire("abc")
    assert await other.acquire("abc")


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
