"""The events' log's statements, run directly on an opened database."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from orbit.agent.domain.replay import LogBounds
from orbit.agent.external import sqlite_log
from orbit.agent.external.sqlite_schema import open_database, transaction
from orbit.protocol import EventEnvelope, encode
from tests.agent_fakes import notice

WORKSPACE = "w1"


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A new database with the workspace ``WORKSPACE`` registered."""
    with closing(open_database(tmp_path / "orbit.db")) as opened:
        opened.execute(
            "INSERT INTO workspaces (id, path, name) VALUES (?, ?, ?)",
            (WORKSPACE, str(tmp_path), "project"),
        )
        yield opened


def size_of(connection: sqlite3.Connection, session_id: str = "a") -> int | None:
    """The size the ``logs`` table holds for the session ``session_id``, if it has a row."""
    row = connection.execute(
        "SELECT size FROM logs WHERE workspace_id = ? AND session_id = ?", (WORKSPACE, session_id)
    ).fetchone()
    return None if row is None else int(row[0])


def length(envelope: EventEnvelope) -> int:
    """The bytes of ``envelope``'s JSON."""
    return len(encode(envelope).encode())


def test_append_skips_seqs_the_log_holds_and_counts_only_those_added_in_its_size(
    connection: sqlite3.Connection,
) -> None:
    first = [notice(1), notice(2)]
    assert sqlite_log.append(connection, WORKSPACE, "a", first) == (2, 2)
    again = [notice(2, message="again"), notice(3)]
    assert sqlite_log.append(connection, WORKSPACE, "a", again) == (1, 3)
    assert size_of(connection) == sum(map(length, [notice(1), notice(2), notice(3)]))
    assert sqlite_log.recent(connection, WORKSPACE, "a", 10) == [notice(1), notice(2), notice(3)]


def test_append_of_nothing_adds_none_and_raises_no_seq(connection: sqlite3.Connection) -> None:
    assert sqlite_log.append(connection, WORKSPACE, "a", []) == (0, 0)
    assert size_of(connection) == 0
    assert sqlite_log.max_seq(connection) == 0


def test_max_seq_counts_seqs_reserved_and_appended(connection: sqlite3.Connection) -> None:
    sqlite_log.append(connection, WORKSPACE, "a", [notice(4)])
    sqlite_log.reserve(connection, 9)
    sqlite_log.reserve(connection, 7)
    assert sqlite_log.max_seq(connection) == 9


def test_before_and_after_read_around_a_seq(connection: sqlite3.Connection) -> None:
    sqlite_log.append(connection, WORKSPACE, "a", [notice(seq) for seq in range(1, 6)])
    assert sqlite_log.before(connection, WORKSPACE, "a", 4, 2) == ([notice(2), notice(3)], True)
    assert sqlite_log.before(connection, WORKSPACE, "a", 3, 2) == ([notice(1), notice(2)], False)
    assert sqlite_log.after(connection, WORKSPACE, "a", 3) == [notice(4), notice(5)]


INVALID_NOTICE = '{"seq": 2, "session": "a", "payload": {"type": "notice"}}'


def test_a_row_that_isnt_json_fails_each_read_naming_its_seq(
    connection: sqlite3.Connection,
) -> None:
    sqlite_log.append(connection, WORKSPACE, "a", [notice(1), notice(2), notice(3)])
    connection.execute("UPDATE events SET json = '{oops' WHERE seq = 2")
    match = r"^event 2 isn't a valid envelope: Not JSON: "
    with pytest.raises(sqlite_log.UndecodableEvent, match=match):
        sqlite_log.recent(connection, WORKSPACE, "a", 10)
    with pytest.raises(sqlite_log.UndecodableEvent, match=match):
        sqlite_log.before(connection, WORKSPACE, "a", 3, 10)
    with pytest.raises(sqlite_log.UndecodableEvent, match=match):
        sqlite_log.after(connection, WORKSPACE, "a", 1)


def test_a_known_event_with_an_invalid_payload_fails_the_read_but_an_unknown_type_is_skipped(
    connection: sqlite3.Connection,
) -> None:
    sqlite_log.append(connection, WORKSPACE, "a", [notice(1), notice(2), notice(3)])
    connection.execute(
        "UPDATE events SET json = replace(json, '\"notice\"', '\"later\"') WHERE seq = 1"
    )
    assert sqlite_log.recent(connection, WORKSPACE, "a", 10) == [notice(2), notice(3)]
    connection.execute("UPDATE events SET json = ? WHERE seq = 2", (INVALID_NOTICE,))
    with pytest.raises(sqlite_log.UndecodableEvent, match=r"^event 2 isn't a valid envelope: "):
        sqlite_log.recent(connection, WORKSPACE, "a", 10)
    assert sqlite_log.after(connection, WORKSPACE, "a", 2) == [notice(3)]


def test_trim_keeps_the_newest_event_alone_when_it_is_over_the_target(
    connection: sqlite3.Connection,
) -> None:
    newest = notice(3, message="x" * 1_000)
    sqlite_log.append(connection, WORKSPACE, "a", [notice(1), notice(2), newest])
    sqlite_log.trim(connection, WORKSPACE, "a", 500, 100)
    assert sqlite_log.recent(connection, WORKSPACE, "a", 10) == [newest]
    assert size_of(connection) == length(newest)
    assert sqlite_log.trimmed(connection, WORKSPACE, "a")


def test_trim_leaves_a_log_within_its_limit(connection: sqlite3.Connection) -> None:
    sqlite_log.append(connection, WORKSPACE, "a", [notice(1), notice(2)])
    sqlite_log.trim(connection, WORKSPACE, "a", 10_000, 1)
    assert sqlite_log.recent(connection, WORKSPACE, "a", 10) == [notice(1), notice(2)]
    assert not sqlite_log.trimmed(connection, WORKSPACE, "a")


def test_bounds_count_forgotten_seqs_as_others_and_trimmed_seqs_as_its_own(
    connection: sqlite3.Connection,
) -> None:
    sqlite_log.append(connection, WORKSPACE, "b", [notice(5, "b")])
    sqlite_log.append(connection, WORKSPACE, "a", [notice(6), notice(7, message="x" * 1_000)])
    sqlite_log.append(connection, WORKSPACE, "c", [notice(3, "c")])
    with transaction(connection):
        sqlite_log.forget_log(connection, WORKSPACE, "b")
    sqlite_log.trim(connection, WORKSPACE, "a", 100, 100)
    bounds = sqlite_log.bounds(connection, WORKSPACE, "a", 6)
    assert bounds == LogBounds(first_seq=7, others_seq=5, own_seq=6)


def test_forget_log_deletes_the_sessions_events_and_its_size(
    connection: sqlite3.Connection,
) -> None:
    sqlite_log.append(connection, WORKSPACE, "a", [notice(1)])
    with transaction(connection):
        sqlite_log.forget_log(connection, WORKSPACE, "a")
    assert sqlite_log.recent(connection, WORKSPACE, "a", 10) == []
    assert size_of(connection) is None
    assert sqlite_log.bounds(connection, WORKSPACE, "a", 1).others_seq == 1
