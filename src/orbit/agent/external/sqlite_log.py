"""The events' log in the SQLite database: each session's events in a workspace, appended, read,
trimmed and forgotten, as functions over a connection, run by :mod:`sqlite_store` on its thread.

Each session's log has a row in ``logs`` with the size of its events, kept as they're appended
and trimmed, and the highest ``seq`` trimmed from it. ``high_water`` holds the highest ``seq`` ever
appended or reserved, so that none is handed out again once its event is deleted, or when it was
never logged, and each workspace's ``forgotten_seq`` the highest deleted with a session's log,
which counts as another session's.
"""

import sqlite3
from collections.abc import Sequence

from orbit.agent.domain.replay import LogBounds
from orbit.agent.external.sqlite_schema import transaction
from orbit.protocol import EventEnvelope, ProtocolError, decode_event, encode

type EventRow = tuple[str, str, int, str, str, int]
type JsonRow = tuple[int, str]


class UndecodableEvent(Exception):
    """A logged event whose JSON isn't a valid envelope, as a corrupt row or one a later
    protocol changed isn't: the message names its ``seq`` and says why."""

    def __init__(self, seq: int, error: ProtocolError) -> None:
        super().__init__(f"event {seq} isn't a valid envelope: {error}")
        self.seq = seq


def max_seq(connection: sqlite3.Connection) -> int:
    """The highest ``seq`` ever appended to ``connection``'s log or reserved there, 0 when
    there's none: the higher of its mark and its ``events`` table's highest."""
    (seq,) = connection.execute(
        """SELECT max(coalesce((SELECT max(seq) FROM high_water), 0),
            coalesce((SELECT max(seq) FROM events), 0))"""
    ).fetchone()
    return int(seq)


def _event_row(workspace: str, session_id: str, envelope: EventEnvelope) -> EventRow:
    """``envelope``'s row in the ``events`` table, as an event of the session ``session_id`` in
    the workspace ``workspace``: its JSON as sent, and that JSON's size in bytes."""
    text = encode(envelope)
    return (workspace, session_id, envelope.seq, envelope.payload.type, text, len(text.encode()))


def _json_rows(cursor: sqlite3.Cursor) -> list[JsonRow]:
    """The ``seq`` and JSON of each row ``cursor`` selected, in its order."""
    return [(int(seq), str(text)) for seq, text in cursor]


def _decoded(rows: Sequence[JsonRow]) -> list[EventEnvelope]:
    """The events whose ``seq`` and JSON ``rows`` hold, latest first, decoded oldest first, but
    for those of types this orbit doesn't know; raises :class:`UndecodableEvent` for a row
    whose JSON isn't a valid envelope."""
    decoded = [_decode(seq, text) for seq, text in reversed(rows)]
    return [envelope for envelope in decoded if envelope is not None]


def _decode(seq: int, text: str) -> EventEnvelope | None:
    """The envelope the event ``seq``'s JSON ``text`` holds, or ``None`` for one of a type this
    orbit doesn't know; raises :class:`UndecodableEvent` if it holds neither."""
    try:
        return decode_event(text)
    except ProtocolError as error:
        raise UndecodableEvent(seq, error) from error


def append(
    connection: sqlite3.Connection,
    workspace: str,
    session_id: str,
    envelopes: Sequence[EventEnvelope],
) -> tuple[int, int]:
    """Add ``envelopes`` to the session ``session_id``'s log in the workspace ``workspace``, but
    for those whose ``seq`` it holds already, as when parked events are imported again, in one
    transaction; returns how many it added and the highest ``seq`` among ``envelopes``, 0 when
    there's none."""
    rows = [_event_row(workspace, session_id, envelope) for envelope in envelopes]
    highest = max([0, *(envelope.seq for envelope in envelopes)])
    with transaction(connection):
        inserted = [row for row in rows if _insert_event(connection, row)]
        added = sum(row[5] for row in inserted)
        connection.execute(
            """INSERT INTO logs (workspace_id, session_id, size) VALUES (?, ?, ?)
            ON CONFLICT (workspace_id, session_id) DO UPDATE SET size = size + excluded.size""",
            (workspace, session_id, added),
        )
        reserve(connection, highest)
    return len(inserted), highest


def reserve(connection: sqlite3.Connection, seq: int) -> None:
    """Raise the ``high_water`` mark to ``seq``."""
    connection.execute("UPDATE high_water SET seq = max(seq, ?)", (seq,))


def _insert_event(connection: sqlite3.Connection, row: EventRow) -> bool:
    """Add ``row`` to the ``events`` table unless its ``seq`` is there; returns whether it was
    added."""
    cursor = connection.execute(
        """INSERT INTO events (workspace_id, session_id, seq, type, json, size)
        VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
        row,
    )
    return cursor.rowcount > 0


def recent(
    connection: sqlite3.Connection, workspace: str, session_id: str, limit: int
) -> list[EventEnvelope]:
    """The session ``session_id``'s last ``limit`` events in the workspace ``workspace``, oldest
    first, but for those of types this orbit doesn't know."""
    rows = connection.execute(
        """SELECT seq, json FROM events WHERE workspace_id = ? AND session_id = ?
        ORDER BY seq DESC LIMIT ?""",
        (workspace, session_id, limit),
    )
    return _decoded(_json_rows(rows))


def before(
    connection: sqlite3.Connection, workspace: str, session_id: str, before_seq: int, limit: int
) -> tuple[list[EventEnvelope], bool]:
    """The session ``session_id``'s last ``limit`` events in the workspace ``workspace`` before
    ``before_seq``, oldest first, but for those of types this orbit doesn't know, and whether
    earlier ones remain."""
    rows = connection.execute(
        """SELECT seq, json FROM events WHERE workspace_id = ? AND session_id = ? AND seq < ?
        ORDER BY seq DESC LIMIT ?""",
        (workspace, session_id, before_seq, limit + 1),
    )
    json_rows = _json_rows(rows)
    return _decoded(json_rows[:limit]), len(json_rows) > limit


def after(
    connection: sqlite3.Connection, workspace: str, session_id: str, after_seq: int
) -> list[EventEnvelope]:
    """The session ``session_id``'s events in the workspace ``workspace`` after ``after_seq``,
    oldest first, but for those of types this orbit doesn't know."""
    rows = connection.execute(
        """SELECT seq, json FROM events WHERE workspace_id = ? AND session_id = ? AND seq > ?
        ORDER BY seq DESC""",
        (workspace, session_id, after_seq),
    )
    return _decoded(_json_rows(rows))


def trim(
    connection: sqlite3.Connection, workspace: str, session_id: str, limit: int, target: int
) -> None:
    """Delete the session ``session_id``'s oldest events in the workspace ``workspace``, if its
    log is over ``limit`` bytes, keeping the latest that together come under ``target``, and
    always its newest, in one transaction: a newest event of ``target`` bytes or more is kept
    alone, over ``target`` or even ``limit`` until a later one is appended."""
    key = (workspace, session_id)
    with transaction(connection):
        row = connection.execute(
            "SELECT size FROM logs WHERE workspace_id = ? AND session_id = ?", key
        ).fetchone()
        if row is None or row[0] <= limit:
            return
        # An event goes when it and those after it come to the target or more, but the
        # newest stays so that reopening the session shows where it was.
        (cut,) = connection.execute(
            """SELECT max(seq) FROM (
                SELECT seq, sum(size) OVER (ORDER BY seq DESC) AS kept,
                    row_number() OVER (ORDER BY seq DESC) AS place FROM events
                WHERE workspace_id = ? AND session_id = ?
            ) WHERE kept >= ? AND place > 1""",
            (*key, target),
        ).fetchone()
        connection.execute(
            "DELETE FROM events WHERE workspace_id = ? AND session_id = ? AND seq <= ?",
            (*key, cut),
        )
        connection.execute(
            """UPDATE logs SET
                size = (SELECT coalesce(sum(size), 0) FROM events
                    WHERE workspace_id = ?1 AND session_id = ?2),
                trimmed_seq = coalesce(?3, trimmed_seq)
            WHERE workspace_id = ?1 AND session_id = ?2""",
            (*key, cut),
        )


def trimmed(connection: sqlite3.Connection, workspace: str, session_id: str) -> bool:
    """Whether events were ever trimmed from the session ``session_id``'s log in the workspace
    ``workspace``."""
    row = connection.execute(
        "SELECT trimmed_seq FROM logs WHERE workspace_id = ? AND session_id = ?",
        (workspace, session_id),
    ).fetchone()
    return row is not None and row[0] > 0


def bounds(
    connection: sqlite3.Connection, workspace: str, session_id: str, since: int
) -> LogBounds:
    """The bounds of the session ``session_id``'s log in the workspace ``workspace``'s, for a
    frontend that saw the events up to ``since``."""
    (first,) = connection.execute(
        "SELECT min(seq) FROM events WHERE workspace_id = ? AND session_id = ?",
        (workspace, session_id),
    ).fetchone()
    # Events forgotten count as another session's: a frontend may have seen them last.
    (others,) = connection.execute(
        """SELECT max(
            coalesce((SELECT max(seq) FROM events WHERE workspace_id = ?1 AND session_id != ?2),
                0),
            (SELECT forgotten_seq FROM workspaces WHERE id = ?1))""",
        (workspace, session_id),
    ).fetchone()
    # The last event trimmed was the session's too.
    (own,) = connection.execute(
        """SELECT max(seq) FROM (
            SELECT seq FROM events WHERE workspace_id = ?1 AND session_id = ?2 AND seq <= ?3
            UNION ALL SELECT trimmed_seq FROM logs
                WHERE workspace_id = ?1 AND session_id = ?2
                AND trimmed_seq > 0 AND trimmed_seq <= ?3
        )""",
        (workspace, session_id, since),
    ).fetchone()
    return LogBounds(first_seq=first, others_seq=int(others), own_seq=own)


def forget_log(connection: sqlite3.Connection, workspace: str, session_id: str) -> None:
    """Delete the session ``session_id``'s events and its row in ``logs`` in the workspace
    ``workspace``, within the caller's transaction, raising the workspace's ``forgotten_seq`` to
    the highest deleted."""
    connection.execute(
        """UPDATE workspaces SET forgotten_seq = max(forgotten_seq, coalesce(
            (SELECT max(seq) FROM events WHERE workspace_id = ?1 AND session_id = ?2), 0))
        WHERE id = ?1""",
        (workspace, session_id),
    )
    connection.execute(
        "DELETE FROM events WHERE workspace_id = ? AND session_id = ?",
        (workspace, session_id),
    )
    connection.execute(
        "DELETE FROM logs WHERE workspace_id = ? AND session_id = ?",
        (workspace, session_id),
    )
