"""The ``reports`` table in the SQLite database: each workspace's reports, saved, listed, read
and deleted, as functions over a connection, run by :mod:`sqlite_store` on its thread.

A report's ``id`` counts from 1 in each workspace, and its ``at`` is an ISO time in UTC.
"""

import sqlite3
from datetime import datetime

from orbit.agent.domain.reports import NewReport
from orbit.agent.external.sqlite_schema import transaction
from orbit.agent.external.sqlite_usage import stored_time
from orbit.protocol import ReportShown, ReportSummary


def save(connection: sqlite3.Connection, workspace: str, report: NewReport) -> int:
    """Add ``report`` to the workspace ``workspace``'s reports in ``connection``, numbered after
    the highest there; returns its id."""
    with transaction(connection):
        (highest,) = connection.execute(
            "SELECT max(id) FROM reports WHERE workspace_id = ?", (workspace,)
        ).fetchone()
        report_id = int(highest or 0) + 1
        connection.execute(
            """INSERT INTO reports (workspace_id, id, title, body, author, session_id, at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                workspace,
                report_id,
                report.title,
                report.body,
                report.author,
                report.session_id,
                stored_time(report.at),
            ),
        )
    return report_id


def listed(connection: sqlite3.Connection, workspace: str) -> list[ReportSummary]:
    """The workspace ``workspace``'s reports in ``connection``, newest first."""
    rows = connection.execute(
        "SELECT id, title, author, at FROM reports WHERE workspace_id = ? ORDER BY id DESC",
        (workspace,),
    ).fetchall()
    return [
        ReportSummary(id=report_id, title=title, author=author, time=datetime.fromisoformat(at))
        for report_id, title, author, at in rows
    ]


def shown(connection: sqlite3.Connection, workspace: str, report_id: int) -> ReportShown | None:
    """The workspace ``workspace``'s report ``report_id`` in ``connection``, if there is one."""
    row = connection.execute(
        "SELECT title, body, author, at FROM reports WHERE workspace_id = ? AND id = ?",
        (workspace, report_id),
    ).fetchone()
    if row is None:
        return None
    title, body, author, at = row
    when = datetime.fromisoformat(at)
    return ReportShown(id=report_id, title=title, body=body, author=author, time=when)


def delete(connection: sqlite3.Connection, workspace: str, report_id: int) -> bool:
    """Delete the workspace ``workspace``'s report ``report_id`` from ``connection``; returns
    whether there was one."""
    cursor = connection.execute(
        "DELETE FROM reports WHERE workspace_id = ? AND id = ?", (workspace, report_id)
    )
    return cursor.rowcount > 0
