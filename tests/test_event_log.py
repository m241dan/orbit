"""The SQLite store's event log: its rows, its highest ``seq``, and what deleting and failing do
to it."""

import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from orbit.agent.application.ports import EventLog
from orbit.agent.external import sqlite_store
from orbit.agent.external.sqlite_store import SqliteStore
from orbit.protocol import EventEnvelope, Notice, TasksChanged, decode_event
from tests.test_sqlite_store import database, record

type Opener = Callable[[Path], Awaitable[SqliteStore]]


def notice(seq: int, session: str = "abc", message: str = "hi") -> EventEnvelope:
    return EventEnvelope(seq=seq, session=session, payload=Notice(message=message))


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


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


def logged(state: Path) -> list[tuple[str, int, str, str, int]]:
    """The ``events`` table's rows, but for their workspace, by session and ``seq``."""
    with database(state) as connection:
        rows = connection.execute(
            "SELECT session_id, seq, type, json, size FROM events ORDER BY session_id, seq"
        )
        return list(rows)


async def test_the_sqlite_store_is_an_event_log(open_store: Opener, workspace: Path) -> None:
    log: EventLog = await open_store(workspace)
    assert log.max_seq() == 0


async def test_appended_events_are_kept_with_their_type_json_and_size_in_bytes(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    tasks = EventEnvelope(seq=2, session="abc", payload=TasksChanged(tasks=()))
    await store.append("abc", [notice(1, message="café"), tasks])

    rows = logged(state)
    assert [(session, seq, kind) for session, seq, kind, _, _ in rows] == [
        ("abc", 1, "notice"),
        ("abc", 2, "tasks_changed"),
    ]
    assert [decode_event(text) for _, _, _, text, _ in rows] == [notice(1, message="café"), tasks]
    assert [size for *_, size in rows] == [len(text.encode()) for _, _, _, text, _ in rows]
    assert rows[0][4] == len(rows[0][3]) + 1  # The é takes two bytes.


async def test_the_highest_seq_is_read_at_open_from_every_workspace_and_counts_appends(
    open_store: Opener, tmp_path: Path, workspace: Path
) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    other = await open_store(other_workspace)
    await other.append("abc", [notice(40), notice(41)])
    store = await open_store(workspace)
    assert store.max_seq() == 41
    await store.append("abc", [notice(45)])
    assert store.max_seq() == 45


async def test_deleting_a_session_deletes_its_events_and_no_others(
    open_store: Opener, state: Path, tmp_path: Path, workspace: Path
) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    store, other = await open_store(workspace), await open_store(other_workspace)
    await store.append("abc", [notice(1)])
    await store.append("def", [notice(2, "def")])
    await other.append("abc", [notice(3)])
    await store.delete("abc")

    assert [(session, seq) for session, seq, *_ in logged(state)] == [("abc", 3), ("def", 2)]


async def test_an_append_that_fails_part_way_is_rolled_back_whole(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [notice(2)])
    with pytest.raises(RuntimeError, match=r"save the events of the session abc to .*orbit\.db"):
        await store.append("abc", [notice(1), notice(2), notice(3)])
    assert [seq for _, seq, *_ in logged(state)] == [2]
    assert store.max_seq() == 2


async def test_a_delete_that_fails_keeps_the_sessions_events(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.save(record())
    await store.append("abc", [notice(1)])
    with database(state) as connection:
        connection.execute(
            "CREATE TRIGGER refuse BEFORE DELETE ON sessions BEGIN SELECT RAISE(FAIL, 'no'); END"
        )
    with pytest.raises(RuntimeError, match="Couldn't delete the session abc"):
        await store.delete("abc")
    assert [seq for _, seq, *_ in logged(state)] == [1]
    assert await store.load("abc") == record()


async def test_a_closed_store_refuses_to_append(open_store: Opener, workspace: Path) -> None:
    store = await open_store(workspace)
    await store.close()
    with pytest.raises(RuntimeError, match="the store is closed"):
        await store.append("abc", [notice(1)])


async def test_appending_under_an_id_that_isnt_a_plain_file_name_is_refused(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    with pytest.raises(ValueError, match="Not a session id"):
        await store.append("../abc", [notice(1)])


async def test_a_new_database_gets_the_events_table(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    await open_store(workspace)
    with database(state) as connection:
        columns = connection.execute("SELECT name FROM pragma_table_info('events')").fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            for _ in range(2):
                connection.execute(
                    "INSERT INTO events VALUES ((SELECT id FROM workspaces), 'a', 1, 't', '{}', 2)"
                )
    assert [name for (name,) in columns] == [
        "workspace_id",
        "session_id",
        "seq",
        "type",
        "json",
        "size",
    ]


async def test_an_append_that_cant_start_is_refused(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, state: Path, workspace: Path
) -> None:
    monkeypatch.setattr(sqlite_store, "BUSY_TIMEOUT", 0.05)
    store = await open_store(workspace)
    with database(state) as connection:
        connection.execute("BEGIN IMMEDIATE")  # Another writer holds the database.
        with pytest.raises(RuntimeError, match="database is locked"):
            await store.append("abc", [notice(1)])
        connection.execute("ROLLBACK")
    assert logged(state) == []


# Reading the latest back


async def test_recent_events_are_a_sessions_last_ones_oldest_first(
    open_store: Opener, tmp_path: Path, workspace: Path
) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    store, other = await open_store(workspace), await open_store(other_workspace)
    await store.append("abc", [notice(seq) for seq in (1, 2, 4, 5)])
    await store.append("def", [notice(3, "def")])
    await other.append("abc", [notice(6)])

    assert await store.recent("abc", 3) == [notice(2), notice(4), notice(5)]
    assert await store.recent("abc", 10) == [notice(seq) for seq in (1, 2, 4, 5)]
    assert await store.recent("ghi", 10) == []


async def test_recent_events_of_types_this_orbit_doesnt_know_are_left_out(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [notice(1), notice(2)])
    with database(state) as connection:
        connection.execute(
            "UPDATE events SET json = replace(json, '\"notice\"', '\"later\"') WHERE seq = 1"
        )

    assert await store.recent("abc", 10) == [notice(2)]


async def test_recent_events_that_cant_be_read_are_none(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [notice(1)])
    await store.close()

    assert await store.recent("abc", 10) == []


async def test_recent_events_under_an_id_that_isnt_a_plain_file_name_are_refused(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    with pytest.raises(ValueError, match="Not a session id"):
        await store.recent("../abc", 10)
