"""The ``usage`` table in the SQLite database: each finished turn's row, recorded and added up,
as functions over a connection, run by :mod:`sqlite_store` on its thread.

A row's ``at`` is an ISO time in UTC to the microsecond, so rows sort by time as text.
"""

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from orbit.agent.domain.sessions import Unreadable, parse_record
from orbit.agent.domain.usage_totals import SessionKey, UsageRow, UsageTotalsData, usage_totals
from orbit.protocol import Usage

_SUMS = """sum(input_tokens), sum(output_tokens), sum(cache_creation_input_tokens),
    sum(cache_read_input_tokens), sum(cost_usd), sum(credits)"""


def stored_time(when: datetime) -> str:
    """``when`` as a row's ``at``: in UTC, to the microsecond."""
    return when.astimezone(UTC).isoformat(timespec="microseconds")


def record(connection: sqlite3.Connection, row: UsageRow) -> None:
    """Add ``row`` to ``connection``'s ``usage`` table."""
    usage = row.usage
    connection.execute(
        """INSERT INTO usage (workspace_id, session_id, role, model, input_tokens, output_tokens,
            cache_creation_input_tokens, cache_read_input_tokens, cost_usd, credits, at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row.workspace_id,
            row.session_id,
            row.role,
            row.model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_creation_input_tokens,
            usage.cache_read_input_tokens,
            usage.cost_usd,
            usage.credits,
            stored_time(row.at),
        ),
    )


def _usage(sums: Sequence[Any]) -> Usage:
    """The usage whose tokens of each kind, dollars and credits are ``sums``, in that order."""
    inputs, outputs, cache_creation, cache_read, cost, credits = sums
    return Usage(
        input_tokens=int(inputs),
        output_tokens=int(outputs),
        cache_creation_input_tokens=int(cache_creation),
        cache_read_input_tokens=int(cache_read),
        cost_usd=float(cost),
        credits=float(credits),
    )


def _today(connection: sqlite3.Connection, today_start: datetime) -> dict[str, Usage]:
    """Each workspace's rows at or after ``today_start``, added up."""
    rows = connection.execute(
        f"SELECT workspace_id, {_SUMS} FROM usage WHERE at >= ? GROUP BY workspace_id",
        (stored_time(today_start),),
    )
    return {str(workspace): _usage(sums) for workspace, *sums in rows}


def _recorded(connection: sqlite3.Connection) -> dict[SessionKey, Usage]:
    """Each session's rows, added up."""
    rows = connection.execute(
        f"SELECT workspace_id, session_id, {_SUMS} FROM usage GROUP BY workspace_id, session_id"
    )
    return {(str(workspace), str(session)): _usage(sums) for workspace, session, *sums in rows}


def _spent(connection: sqlite3.Connection) -> dict[SessionKey, Usage]:
    """What each saved session's record says it spent, but for records that can't be read."""
    rows = connection.execute("SELECT workspace_id, id, record FROM sessions")
    spent: dict[SessionKey, Usage] = {}
    for workspace, session, text in rows:
        parsed = parse_record(str(text))
        if not isinstance(parsed, Unreadable):
            spent[(str(workspace), str(session))] = parsed.spent
    return spent


def totals(connection: sqlite3.Connection, today_start: datetime) -> UsageTotalsData:
    """The totals per workspace and in all in ``connection``, today's counting the rows at or
    after ``today_start``, as :func:`usage_totals` adds them up."""
    return usage_totals(_today(connection, today_start), _recorded(connection), _spent(connection))
