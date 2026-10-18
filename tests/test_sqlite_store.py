"""The SQLite store: its database, workspaces, migrations, failures and its thread."""

import asyncio
import sqlite3
import subprocess
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbit.agent.application.ports import SessionStore, StoredSession
from orbit.agent.domain.sessions import SessionRecord, Unreadable
from orbit.agent.external import sqlite_store
from orbit.agent.external.sqlite_store import (
    MIGRATIONS,
    THREAD_PREFIX,
    SqliteStore,
    StoreError,
    resolve_state_dir,
)

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


# The state directory


def test_the_state_directory_flag_wins(tmp_path: Path) -> None:
    environ = {"ORBIT_STATE_DIR": "/env/orbit", "XDG_STATE_HOME": "/xdg"}
    assert resolve_state_dir(tmp_path / "flag", environ) == tmp_path / "flag"


def test_without_the_flag_the_state_directory_is_orbits_variable() -> None:
    environ = {"ORBIT_STATE_DIR": "/env/orbit", "XDG_STATE_HOME": "/xdg"}
    assert resolve_state_dir(None, environ) == Path("/env/orbit")


def test_without_orbits_variable_the_state_directory_is_orbit_in_the_xdg_state_home() -> None:
    environ = {"ORBIT_STATE_DIR": "", "XDG_STATE_HOME": "/xdg"}
    assert resolve_state_dir(None, environ) == Path("/xdg/orbit")


def test_by_default_the_state_directory_is_in_the_home_directory() -> None:
    assert resolve_state_dir(None, {"XDG_STATE_HOME": ""}) == Path.home() / ".local/state/orbit"


def test_the_state_directory_is_expanded_and_made_absolute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert resolve_state_dir(Path("state"), {}) == tmp_path / "state"
    assert resolve_state_dir(None, {"ORBIT_STATE_DIR": "~/state"}) == Path.home() / "state"


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


async def test_sessions_whose_locks_cant_be_checked_arent_listed(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.save(record())
    (state / "locks").write_text("in the way")
    assert await store.sessions() == []


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
    assert await store.sessions() == []
    with pytest.raises(RuntimeError, match=r"Couldn't delete the session abc from .*orbit\.db"):
        await store.delete("abc")
    assert await store.review_count() == 0
    with pytest.raises(RuntimeError, match=r"Couldn't save the review count to .*orbit\.db"):
        await store.save_review_count(1)


# Failures at open


async def test_a_newer_database_is_refused_naming_both_versions(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    await (await open_store(workspace)).close()
    with database(state) as connection:
        connection.execute("INSERT INTO schema_version (version) VALUES (99)")
    with pytest.raises(StoreError, match=r"orbit\.db has schema version 99, newer than .* 1;"):
        await open_store(workspace)
    assert store_threads() == []


def racing(
    monkeypatch: pytest.MonkeyPatch, state: Path, meanwhile: Callable[[sqlite3.Connection], object]
) -> None:
    """Makes another connection run ``meanwhile`` once the store has read its schema version."""
    read = sqlite_store._schema_version

    def read_then_race(connection: sqlite3.Connection) -> int:
        version = read(connection)
        with database(state) as other:
            meanwhile(other)
        return version

    monkeypatch.setattr(sqlite_store, "_schema_version", read_then_race)


async def test_a_migration_applied_meanwhile_by_another_orbit_isnt_run_again(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, state: Path, workspace: Path
) -> None:
    racing(monkeypatch, state, lambda other: sqlite_store._apply(other, 1, state / "orbit.db"))
    await open_store(workspace)
    with database(state) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [(1,)]


async def test_a_newer_version_written_meanwhile_is_refused(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, state: Path, workspace: Path
) -> None:
    def newer(other: sqlite3.Connection) -> None:
        other.execute("INSERT INTO schema_version (version) VALUES (99)")

    racing(monkeypatch, state, newer)
    with pytest.raises(StoreError, match=r"orbit\.db has schema version 99, newer than .* 1;"):
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
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [(1,)]
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
    monkeypatch.setattr(sqlite_store, "MIGRATIONS", broken)
    with pytest.raises(StoreError, match=r"upgrade the database .*orbit\.db to schema version 2"):
        await open_store(workspace)
    with database(state) as connection:
        assert connection.execute("SELECT max(version) FROM schema_version").fetchone() == (1,)
        tables = connection.execute("SELECT name FROM sqlite_master WHERE name = 'extra'")
        assert tables.fetchall() == []


async def test_a_migration_that_cant_start_is_refused(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, state: Path, workspace: Path
) -> None:
    state.mkdir(parents=True)
    with database(state) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("BEGIN IMMEDIATE")  # Another writer holds the database.
        monkeypatch.setattr(sqlite_store, "BUSY_TIMEOUT", 0.05)
        with pytest.raises(StoreError, match=r"to schema version 1: database is locked"):
            await open_store(workspace)


async def test_a_store_that_fails_to_open_closes_its_connection(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, workspace: Path
) -> None:
    def fail(path: str) -> str:
        raise RuntimeError("git broke")

    monkeypatch.setattr(sqlite_store, "workspace_fingerprint", fail)
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
