"""The SQLite store's event log: its rows, its highest ``seq``, and what deleting and failing do
to it."""

import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from orbit.agent.application.ports import EventLog, ReadFailed, StorageFailed
from orbit.agent.domain.replay import LogBounds, can_replay
from orbit.agent.external import sqlite_schema
from orbit.agent.external.workspace_registry import register
from orbit.protocol import EventEnvelope, TasksChanged, decode_event, encode
from tests.agent_fakes import notice
from tests.store_views import Opener, OwnView, big, database, record


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


async def test_the_latest_seq_is_read_again_counting_what_another_orbit_logged_since(
    open_store: Opener, workspace: Path
) -> None:
    store, other = await open_store(workspace), await open_store(workspace)
    await other.append("abc", [notice(50)])
    assert store.max_seq() == 0

    assert await store.latest_seq() == 50
    assert store.max_seq() == 50


async def test_the_latest_seq_never_counts_down_from_the_highest_appended(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [notice(7)])
    with database(state) as connection:
        connection.execute("DELETE FROM events")

    assert await store.latest_seq() == 7


async def test_a_latest_seq_that_cant_be_read_raises_naming_the_database(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    with database(state) as connection:
        connection.execute("DROP TABLE high_water")

    match = r"Couldn't read the latest seq from .*orbit\.db: no such table: high_water"
    with pytest.raises(ReadFailed, match=match):
        await store.latest_seq()


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
    with database(state) as connection:
        connection.execute(
            """CREATE TRIGGER no_fourth BEFORE INSERT ON events WHEN NEW.seq = 4
            BEGIN SELECT RAISE(ABORT, 'no'); END"""
        )
    with pytest.raises(StorageFailed, match=r"save the events of the session abc to .*orbit\.db"):
        await store.append("abc", [notice(1), notice(3), notice(4)])
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
    with pytest.raises(StorageFailed, match="Couldn't delete the session abc"):
        await store.delete("abc")
    assert [seq for _, seq, *_ in logged(state)] == [1]
    assert await store.load("abc") == record()


async def test_a_closed_store_refuses_to_append(open_store: Opener, workspace: Path) -> None:
    store = await open_store(workspace)
    await store.close()
    with pytest.raises(StorageFailed, match="the store is closed"):
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
    monkeypatch.setattr(sqlite_schema, "BUSY_TIMEOUT", 0.05)
    store = await open_store(workspace)
    with database(state) as connection:
        connection.execute("BEGIN IMMEDIATE")  # Another writer holds the database.
        with pytest.raises(StorageFailed, match="database is locked"):
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


async def test_recent_events_that_cant_be_read_raise_naming_the_database(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [notice(1)])
    await store.close()

    match = r"Couldn't read the events of the session abc from .*orbit\.db: the store is closed"
    with pytest.raises(ReadFailed, match=match):
        await store.recent("abc", 10)


async def test_recent_events_under_an_id_that_isnt_a_plain_file_name_are_refused(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    with pytest.raises(ValueError, match="Not a session id"):
        await store.recent("../abc", 10)


# Reading earlier ones


async def test_events_before_a_seq_are_the_latest_before_it_oldest_first_and_whether_more_remain(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [notice(seq) for seq in range(1, 1201)])
    await store.append("def", [notice(1201, "def")])

    assert await store.before("abc", 1201, 500) == ([notice(s) for s in range(701, 1201)], True)
    assert await store.before("abc", 701, 500) == ([notice(s) for s in range(201, 701)], True)
    assert await store.before("abc", 201, 500) == ([notice(s) for s in range(1, 201)], False)
    assert await store.before("abc", 201, 200) == ([notice(s) for s in range(1, 201)], False)
    assert await store.before("abc", 1, 500) == ([], False)
    assert await store.before("ghi", 10, 500) == ([], False)


async def test_events_before_a_seq_of_types_this_orbit_doesnt_know_are_left_out(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [notice(1), notice(2), notice(3)])
    with database(state) as connection:
        connection.execute(
            "UPDATE events SET json = replace(json, '\"notice\"', '\"later\"') WHERE seq = 2"
        )

    assert await store.before("abc", 4, 2) == ([notice(3)], True)


async def test_events_before_a_seq_that_cant_be_read_raise_naming_the_database(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [notice(1)])
    await store.close()

    match = r"Couldn't read the events of the session abc from .*orbit\.db: "
    with pytest.raises(ReadFailed, match=match):
        await store.before("abc", 10, 10)


async def test_events_before_a_seq_under_an_id_that_isnt_a_plain_file_name_are_refused(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    with pytest.raises(ValueError, match="Not a session id"):
        await store.before("../abc", 10, 10)


# Reading those after a seq, and where a session's log stands


async def test_events_after_a_seq_are_the_sessions_later_ones_oldest_first(
    open_store: Opener, tmp_path: Path, workspace: Path
) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    store, other = await open_store(workspace), await open_store(other_workspace)
    await store.append("abc", [notice(seq) for seq in (1, 2, 4, 5)])
    await store.append("def", [notice(3, "def")])
    await other.append("abc", [notice(6)])

    assert await store.after("abc", 1) == [notice(2), notice(4), notice(5)]
    assert await store.after("abc", 5) == []
    assert await store.after("ghi", 0) == []


async def test_events_after_a_seq_of_types_this_orbit_doesnt_know_are_left_out(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [notice(1), notice(2)])
    with database(state) as connection:
        connection.execute(
            "UPDATE events SET json = replace(json, '\"notice\"', '\"later\"') WHERE seq = 1"
        )

    assert await store.after("abc", 0) == [notice(2)]


async def test_events_after_a_seq_that_cant_be_read_are_none(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.close()

    assert await store.after("abc", 0) is None


async def test_bounds_are_the_sessions_oldest_seq_the_highest_of_its_workspaces_others_and_its_own(
    open_store: Opener, tmp_path: Path, workspace: Path
) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    store, other = await open_store(workspace), await open_store(other_workspace)
    assert await store.bounds("abc", 9) == LogBounds(first_seq=None, others_seq=0, own_seq=None)
    await store.append("abc", [notice(seq) for seq in (3, 4, 7)])
    await store.append("def", [notice(5, "def")])
    await other.append("ghi", [notice(6, "ghi"), notice(9, "ghi")])

    assert await store.bounds("abc", 6) == LogBounds(first_seq=3, others_seq=5, own_seq=4)
    assert await store.bounds("abc", 2) == LogBounds(first_seq=3, others_seq=5, own_seq=None)
    assert await store.bounds("def", 9) == LogBounds(first_seq=5, others_seq=7, own_seq=5)


async def test_bounds_that_cant_be_read_are_none(open_store: Opener, workspace: Path) -> None:
    store = await open_store(workspace)
    await store.close()

    assert await store.bounds("abc", 0) is None


@pytest.mark.parametrize("read", ["after", "bounds"])
async def test_reading_after_a_seq_under_an_id_that_isnt_a_plain_file_name_is_refused(
    open_store: Opener, workspace: Path, read: str
) -> None:
    store = await open_store(workspace)
    with pytest.raises(ValueError, match="Not a session id"):
        await (store.after("../abc", 0) if read == "after" else store.bounds("../abc", 0))


# Trimming

MB = 1024 * 1024


def old_database(
    monkeypatch: pytest.MonkeyPatch, state: Path, workspace: Path, version: int
) -> None:
    """Create the database in ``state`` with the schema of ``version``, registering
    ``workspace``, leaving ``MIGRATIONS`` cut to that version until ``monkeypatch`` is undone."""
    monkeypatch.setattr(sqlite_schema, "MIGRATIONS", sqlite_schema.MIGRATIONS[:version])
    connection = sqlite_schema.open_database(state / "orbit.db")
    register(connection, workspace.resolve(), None)
    connection.close()


def insert_events(connection: sqlite3.Connection, rows: list[tuple[str, int, int]]) -> None:
    """Add events, as their session, ``seq`` and size, to the only workspace's log."""
    for session, seq, size in rows:
        connection.execute(
            "INSERT INTO events VALUES ((SELECT id FROM workspaces), ?, ?, 'notice', '{}', ?)",
            (session, seq, size),
        )


def marks(state: Path) -> tuple[int, list[int]]:
    """The highest ``seq`` ever appended, and each workspace's highest ``seq`` forgotten."""
    with database(state) as connection:
        (mark,) = connection.execute("SELECT seq FROM high_water").fetchone()
        forgotten = connection.execute("SELECT forgotten_seq FROM workspaces ORDER BY path")
        return int(mark), [int(seq) for (seq,) in forgotten]


def stored_sizes(state: Path) -> dict[str, tuple[int, int]]:
    """Each session's log in ``logs``, as its size and the highest seq trimmed, by its id."""
    with database(state) as connection:
        rows = connection.execute("SELECT session_id, size, trimmed_seq FROM logs")
        return {session: (size, trimmed) for session, size, trimmed in rows}


def sizes_of_events(state: Path) -> dict[str, int]:
    """The sum of each session's events' sizes in ``events``, by its id."""
    with database(state) as connection:
        rows = connection.execute("SELECT session_id, sum(size) FROM events GROUP BY session_id")
        return {session: size for session, size in rows}


async def test_appends_keep_each_sessions_log_size(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [notice(1), notice(2)])
    await store.append("abc", [notice(3)])
    await store.append("def", [notice(4, "def")])

    assert stored_sizes(state) == {
        session: (size, 0) for session, size in sizes_of_events(state).items()
    }


async def test_events_already_stored_are_skipped_and_counted_once(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [notice(1), notice(2)])
    await store.append("abc", [notice(2, message="again"), notice(3)])

    assert [(seq, decode_event(text)) for _, seq, _, text, _ in logged(state)] == [
        (1, notice(1)),
        (2, notice(2)),
        (3, notice(3)),
    ]
    assert stored_sizes(state) == {"abc": (sizes_of_events(state)["abc"], 0)}


async def test_appending_returns_how_many_events_it_stored_skipping_those_stored_already(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    assert await store.append("abc", [notice(1), notice(2)]) == 2
    assert await store.append("abc", [notice(2), notice(3), notice(4)]) == 2
    assert await store.append("abc", [notice(1), notice(4)]) == 0


async def test_a_log_over_the_limit_keeps_its_newest_events_under_the_target(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [big(seq) for seq in range(1, 211)])  # About 2 MB.
    await store.trim("abc", MB, MB * 9 // 10)

    kept = [seq for _, seq, *_ in logged(state)]
    size, trimmed = stored_sizes(state)["abc"]
    assert size == sizes_of_events(state)["abc"]
    assert size < MB * 9 // 10
    assert size + len(encode(big(trimmed)).encode()) >= MB * 9 // 10  # Not one more than needed.
    assert kept == list(range(trimmed + 1, 211))


async def test_trimming_a_log_leaves_other_sessions_as_they_are(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [big(seq) for seq in range(1, 211)])
    await store.append("def", [big(211, "def")])
    others = logged(state)[-1:]
    await store.trim("abc", MB, MB * 9 // 10)

    assert logged(state)[-1:] == others
    assert await store.trimmed("abc")
    assert not await store.trimmed("def")


async def test_a_log_within_the_limit_or_never_written_isnt_trimmed(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [big(seq) for seq in range(1, 11)])
    before = logged(state)
    await store.trim("abc", sizes_of_events(state)["abc"], 1)
    await store.trim("def", 1, 1)

    assert logged(state) == before and "def" not in stored_sizes(state)
    assert not await store.trimmed("abc")


async def test_the_oldest_page_after_trimming_has_nothing_more(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [big(seq) for seq in range(1, 211)])
    await store.trim("abc", MB, MB * 9 // 10)
    first = (await store.recent("abc", 1000))[0].seq

    assert await store.before("abc", first + 1, 500) == ([big(first)], False)
    assert await store.bounds("abc", first) == LogBounds(
        first_seq=first, others_seq=0, own_seq=first
    )
    assert await store.bounds("abc", first - 1) == LogBounds(
        first_seq=first, others_seq=0, own_seq=first - 1
    )


async def test_a_log_whose_newest_event_alone_reaches_the_target_keeps_that_event(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [big(1), big(2)])
    await store.trim("abc", 1, 1)

    assert await store.recent("abc", 10) == [big(2)]
    assert await store.trimmed("abc")
    assert stored_sizes(state)["abc"] == (len(encode(big(2)).encode()), 1)
    assert await store.before("abc", 2, 10) == ([], False)
    assert await store.bounds("abc", 2) == LogBounds(first_seq=2, others_seq=0, own_seq=2)
    assert await store.bounds("abc", 1) == LogBounds(first_seq=2, others_seq=0, own_seq=1)


async def test_a_log_of_one_event_over_the_limit_is_kept_untrimmed(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [big(1)])
    await store.trim("abc", 1, 1)

    assert await store.recent("abc", 10) == [big(1)]
    assert not await store.trimmed("abc")
    assert stored_sizes(state)["abc"] == (len(encode(big(1)).encode()), 0)


async def test_deleting_a_session_forgets_its_trimmed_log(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [big(1), big(2)])
    await store.trim("abc", 1, 1)
    await store.delete("abc")

    assert stored_sizes(state) == {} and not await store.trimmed("abc")


async def test_upgrading_counts_the_size_of_the_events_logged_before(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, state: Path, workspace: Path
) -> None:
    old_database(monkeypatch, state, workspace, 2)
    with database(state) as connection:
        insert_events(connection, [("abc", 1, 10), ("abc", 2, 20), ("def", 3, 5)])
    monkeypatch.undo()
    await open_store(workspace)

    assert stored_sizes(state) == {"abc": (30, 0), "def": (5, 0)}
    assert marks(state) == (3, [0])


async def test_upgrading_from_version_3_starts_the_mark_at_the_highest_seq_and_nothing_forgotten(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, state: Path, workspace: Path
) -> None:
    old_database(monkeypatch, state, workspace, 3)
    with database(state) as connection:
        insert_events(connection, [("abc", 4, 10), ("abc", 9, 10)])
    monkeypatch.undo()
    store = await open_store(workspace)

    assert marks(state) == (9, [0])
    assert store.max_seq() == 9


async def test_upgrading_an_empty_log_starts_the_mark_at_0(
    monkeypatch: pytest.MonkeyPatch, open_store: Opener, state: Path, workspace: Path
) -> None:
    old_database(monkeypatch, state, workspace, 3)
    monkeypatch.undo()
    await open_store(workspace)

    assert marks(state) == (0, [0])


async def test_a_trim_that_fails_is_refused_and_so_is_reading_whether_it_was_trimmed(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [big(1), big(2)])
    await store.trim("abc", 1, 1)
    await store.close()

    with pytest.raises(StorageFailed, match=r"Couldn't trim the events of the session abc in "):
        await store.trim("abc", 1, 1)
    match = r"Couldn't read whether the session abc's log was trimmed in .*orbit\.db: "
    with pytest.raises(ReadFailed, match=match):
        await store.trimmed("abc")


@pytest.mark.parametrize("call", ["trim", "trimmed"])
async def test_trimming_under_an_id_that_isnt_a_plain_file_name_is_refused(
    open_store: Opener, workspace: Path, call: str
) -> None:
    store = await open_store(workspace)
    with pytest.raises(ValueError, match="Not a session id"):
        await (store.trim("../abc", 1, 1) if call == "trim" else store.trimmed("../abc"))


async def test_forgetting_a_session_deletes_its_events_and_log_alone(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.save(record())
    await store.append("abc", [big(1), big(2)])
    await store.append("def", [notice(3, "def")])
    await store.trim("abc", 1, 1)
    await store.forget("abc")

    assert [session for session, *_ in logged(state)] == ["def"]
    assert list(stored_sizes(state)) == ["def"] and not await store.trimmed("abc")
    assert [s.id for s in await store.sessions()] == ["abc"]


async def test_a_forget_that_fails_is_refused(open_store: Opener, workspace: Path) -> None:
    store = await open_store(workspace)
    await store.close()

    with pytest.raises(StorageFailed, match=r"Couldn't delete the events of the session abc from "):
        await store.forget("abc")


async def test_forgetting_under_an_id_that_isnt_a_plain_file_name_is_refused(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    with pytest.raises(ValueError, match="Not a session id"):
        await store.forget("../abc")


# Forgotten events

Drop = Callable[[OwnView, str], Awaitable[None]]
DROPS: list[Drop] = [OwnView.forget, OwnView.delete]


@pytest.mark.parametrize("drop", DROPS, ids=["forget", "delete"])
async def test_seq_resumes_past_events_forgotten_or_deleted(
    open_store: Opener, workspace: Path, drop: Drop
) -> None:
    store = await open_store(workspace)
    await store.append("auth", [notice(seq, "auth") for seq in range(1, 6)])
    await store.append("x", [notice(seq, "x") for seq in range(6, 21)])
    await drop(store, "x")
    await store.close()
    reopened = await open_store(workspace)

    assert reopened.max_seq() == 20
    assert await reopened.latest_seq() == 20


async def test_the_mark_is_the_highest_seq_appended_in_any_workspace(
    open_store: Opener, state: Path, tmp_path: Path, workspace: Path
) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    store, other = await open_store(workspace), await open_store(other_workspace)
    await other.append("abc", [notice(8)])
    await store.append("abc", [notice(3)])
    await store.append("abc", [])

    assert marks(state)[0] == 8


async def test_the_mark_never_counts_down_from_the_highest_seq_stored(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [notice(4)])
    with database(state) as connection:
        connection.execute("UPDATE high_water SET seq = 0")
    await store.close()

    assert (await open_store(workspace)).max_seq() == 4


async def test_a_reserved_seq_raises_the_mark_counted_from_then_on_and_once_reopened(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [notice(5)])
    await store.reserve(100)
    await store.reserve(50)
    assert store.max_seq() == 100
    await store.close()

    assert marks(state)[0] == 100
    assert (await open_store(workspace)).max_seq() == 100


async def test_a_reservation_that_fails_is_a_storage_failure_naming_the_database(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.close()

    with pytest.raises(StorageFailed, match=r"Couldn't reserve the seq numbers up to 100 in "):
        await store.reserve(100)


@pytest.mark.parametrize("drop", DROPS, ids=["forget", "delete"])
async def test_a_frontend_that_last_saw_a_forgotten_sessions_events_isnt_replayed(
    open_store: Opener, workspace: Path, drop: Drop
) -> None:
    store = await open_store(workspace)
    await store.append("auth", [notice(seq, "auth") for seq in range(1, 6)])
    await store.append("x", [notice(seq, "x") for seq in range(6, 21)])
    await drop(store, "x")
    await store.append("auth", [notice(21, "auth"), notice(22, "auth")])

    bounds = await store.bounds("auth", 20)
    assert bounds == LogBounds(first_seq=1, others_seq=20, own_seq=5)
    assert not can_replay(20, 22, bounds)


async def test_a_frontend_that_saw_the_open_sessions_events_after_those_forgotten_is_replayed(
    open_store: Opener, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("auth", [notice(seq, "auth") for seq in range(1, 6)])
    await store.append("x", [notice(seq, "x") for seq in range(6, 11)])
    await store.forget("x")
    await store.append("auth", [notice(seq, "auth") for seq in range(11, 16)])

    bounds = await store.bounds("auth", 12)
    assert bounds == LogBounds(first_seq=1, others_seq=10, own_seq=12)
    assert can_replay(12, 15, bounds)


async def test_forgetting_keeps_the_highest_seq_forgotten_in_its_workspace_alone(
    open_store: Opener, state: Path, tmp_path: Path, workspace: Path
) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    store, other = await open_store(workspace), await open_store(other_workspace)
    await store.append("x", [notice(3, "x"), notice(7, "x")])
    await store.append("y", [notice(4, "y")])
    await other.append("z", [notice(9, "z")])
    await store.forget("x")
    await store.forget("y")
    await store.forget("never-logged")
    await other.forget("never-logged")

    forgotten = dict(
        zip(sorted([str(workspace), str(other_workspace)]), marks(state)[1], strict=True)
    )
    assert forgotten == {str(workspace): 7, str(other_workspace): 0}
    assert await other.bounds("z", 9) == LogBounds(first_seq=9, others_seq=0, own_seq=9)


async def test_trimming_leaves_the_highest_seq_forgotten_and_the_next_seq_alone(
    open_store: Opener, state: Path, workspace: Path
) -> None:
    store = await open_store(workspace)
    await store.append("abc", [big(1), big(2), big(3)])
    await store.trim("abc", 1, 1)
    await store.close()
    reopened = await open_store(workspace)

    assert await reopened.trimmed("abc")
    assert marks(state) == (3, [0])
    assert reopened.max_seq() == 3
