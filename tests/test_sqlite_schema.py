"""The SQLite schema: creating, migrating and opening the database, and its transactions."""

import asyncio
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbit.agent.external import sqlite_schema
from orbit.agent.external.sqlite_schema import MIGRATIONS, StoreError, transaction
from orbit.agent.external.sqlite_store import SqliteStore
from orbit.agent.external.workspace_registry import register
from tests.store_views import Opener, database, store_threads

# Failures at open

NEWER = rf"orbit\.db has schema version 99, newer than .* {len(MIGRATIONS)};"


async def test_a_newer_database_is_refused_naming_both_versions(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    await (await open_store(workspace)).close()
    with database(state) as connection:
        connection.execute("INSERT INTO schema_version (version) VALUES (99)")
    with pytest.raises(StoreError, match=NEWER):
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
            (version,) for version in range(1, len(MIGRATIONS) + 1)
        ]


async def test_a_newer_version_written_meanwhile_is_refused(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, state: Path, workspace: Path
) -> None:
    def newer(other: sqlite3.Connection) -> None:
        other.execute("INSERT INTO schema_version (version) VALUES (99)")

    racing(monkeypatch, state, newer)
    with pytest.raises(StoreError, match=NEWER):
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
            (version,) for version in range(1, len(MIGRATIONS) + 1)
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
    upgrade = rf"upgrade the database .*orbit\.db to schema version {len(MIGRATIONS) + 1}"
    with pytest.raises(StoreError, match=upgrade):
        await open_store(workspace)
    with database(state) as connection:
        assert connection.execute("SELECT max(version) FROM schema_version").fetchone() == (
            len(MIGRATIONS),
        )
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
    monkeypatch: pytest.MonkeyPatch, state: Path
) -> None:
    def fail(connection: sqlite3.Connection, database: Path) -> None:
        raise RuntimeError("migration broke")

    monkeypatch.setattr(sqlite_schema, "_migrate", fail)
    with pytest.raises(RuntimeError, match="migration broke"):
        await SqliteStore.open(state)
    assert store_threads() == []


async def test_a_version_4_database_upgrades_with_no_workspace_served(
    monkeypatch: pytest.MonkeyPatch, state: Path, workspace: Path
) -> None:
    with monkeypatch.context() as patched:
        patched.setattr(sqlite_schema, "MIGRATIONS", MIGRATIONS[:4])
        connection = sqlite_schema.open_database(state / "orbit.db")
        register(connection, workspace.resolve(), None)
        connection.close()
    store = await SqliteStore.open(state)
    await store.close()
    with database(state) as connection:
        assert connection.execute("SELECT max(version) FROM schema_version").fetchone() == (
            len(MIGRATIONS),
        )
        assert connection.execute("SELECT path, served FROM workspaces").fetchall() == [
            (str(workspace), 0)
        ]


async def test_a_version_5_database_upgrades_to_6_with_no_workspace_managed(
    monkeypatch: pytest.MonkeyPatch, state: Path, workspace: Path
) -> None:
    with monkeypatch.context() as patched:
        patched.setattr(sqlite_schema, "MIGRATIONS", MIGRATIONS[:5])
        connection = sqlite_schema.open_database(state / "orbit.db")
        register(connection, workspace.resolve(), None)
        connection.close()
    store = await SqliteStore.open(state)
    await store.close()
    with database(state) as connection:
        assert connection.execute("SELECT max(version) FROM schema_version").fetchone() == (
            len(MIGRATIONS),
        )
        assert connection.execute("SELECT path, managed FROM workspaces").fetchall() == [
            (str(workspace), 0)
        ]


async def test_a_version_6_database_upgrades_to_7_with_no_usage_recorded(
    monkeypatch: pytest.MonkeyPatch, state: Path, workspace: Path
) -> None:
    with monkeypatch.context() as patched:
        patched.setattr(sqlite_schema, "MIGRATIONS", MIGRATIONS[:6])
        connection = sqlite_schema.open_database(state / "orbit.db")
        register(connection, workspace.resolve(), None)
        connection.close()
    store = await SqliteStore.open(state)
    await store.close()
    with database(state) as connection:
        assert connection.execute("SELECT max(version) FROM schema_version").fetchone() == (
            len(MIGRATIONS),
        )
        assert connection.execute("SELECT count(*) FROM usage").fetchone() == (0,)
        assert connection.execute("SELECT path FROM workspaces").fetchall() == [(str(workspace),)]


async def test_a_version_7_database_upgrades_to_8_stamping_the_served_workspaces(
    monkeypatch: pytest.MonkeyPatch, state: Path, tmp_path: Path
) -> None:
    served, other = tmp_path.resolve() / "served", tmp_path.resolve() / "other"
    served.mkdir()
    other.mkdir()
    with monkeypatch.context() as patched:
        patched.setattr(sqlite_schema, "MIGRATIONS", MIGRATIONS[:7])
        connection = sqlite_schema.open_database(state / "orbit.db")
        register(connection, served, None)
        register(connection, other, None)
        connection.execute("UPDATE workspaces SET served = 1 WHERE path = ?", (str(served),))
        connection.close()
    before = datetime.now(UTC)
    store = await SqliteStore.open(state)
    await store.close()
    after = datetime.now(UTC)
    with database(state) as connection:
        assert connection.execute("SELECT max(version) FROM schema_version").fetchone() == (
            len(MIGRATIONS),
        )
        rows = dict(connection.execute("SELECT path, added_at FROM workspaces").fetchall())
    assert rows[str(other)] is None
    # The migration's time is to the millisecond.
    assert (
        before.replace(microsecond=before.microsecond // 1000 * 1000)
        <= datetime.fromisoformat(rows[str(served)])
        <= after
    )


async def test_a_version_8_database_upgrades_to_9_with_no_record_moved(
    monkeypatch: pytest.MonkeyPatch, state: Path, workspace: Path
) -> None:
    with monkeypatch.context() as patched:
        patched.setattr(sqlite_schema, "MIGRATIONS", MIGRATIONS[:8])
        connection = sqlite_schema.open_database(state / "orbit.db")
        register(connection, workspace.resolve(), None)
        connection.close()
    store = await SqliteStore.open(state)
    await store.close()
    with database(state) as connection:
        assert connection.execute("SELECT max(version) FROM schema_version").fetchone() == (
            len(MIGRATIONS),
        )
        assert connection.execute("SELECT count(*) FROM record_moves").fetchone() == (0,)


async def test_a_version_9_database_upgrades_to_10_with_no_credits_spent_by_its_turns(
    monkeypatch: pytest.MonkeyPatch, state: Path, workspace: Path
) -> None:
    with monkeypatch.context() as patched:
        patched.setattr(sqlite_schema, "MIGRATIONS", MIGRATIONS[:9])
        connection = sqlite_schema.open_database(state / "orbit.db")
        workspace_id = register(connection, workspace.resolve(), None).workspace_id
        connection.execute(
            "INSERT INTO usage VALUES (?, 's', 'coder', NULL, 1, 2, 3, 4, 0.5, 'then')",
            (workspace_id,),
        )
        connection.close()
    store = await SqliteStore.open(state)
    await store.close()
    with database(state) as connection:
        assert connection.execute("SELECT max(version) FROM schema_version").fetchone() == (
            len(MIGRATIONS),
        )
        assert connection.execute("SELECT cost_usd, credits FROM usage").fetchall() == [(0.5, 0)]


async def test_a_version_10_database_upgrades_with_no_reports(
    monkeypatch: pytest.MonkeyPatch, state: Path, workspace: Path
) -> None:
    with monkeypatch.context() as patched:
        patched.setattr(sqlite_schema, "MIGRATIONS", MIGRATIONS[:10])
        connection = sqlite_schema.open_database(state / "orbit.db")
        register(connection, workspace.resolve(), None)
        connection.close()
    store = await SqliteStore.open(state)
    await store.close()
    with database(state) as connection:
        assert connection.execute("SELECT max(version) FROM schema_version").fetchone() == (
            len(MIGRATIONS),
        )
        assert connection.execute("SELECT count(*) FROM reports").fetchone() == (0,)


# Transactions


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
