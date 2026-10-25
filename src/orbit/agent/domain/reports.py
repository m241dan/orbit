"""Reports: findings agents keep in the workspace, with the rules for saving, listing and reading
them through orbit's tools."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from orbit.protocol import ReportShown, ReportSummary

REPORT_LIMIT = 200 * 1024
"""The most bytes a report's body may hold, in UTF-8."""

TOO_LARGE = "That report is over 200 KB; summarise it and keep only the essential output."
NO_TITLE = "A report needs a title."
NO_REPORTS = "There are no reports yet."
NO_ID = "Name the report by the id list_reports gives it."


@dataclass(frozen=True)
class NewReport:
    """A report to save: its ``title`` and Markdown ``body``, the role it's by (``author``), the
    session it was saved in (``session_id``) and when (``at``)."""

    title: str
    body: str
    author: str
    session_id: str
    at: datetime


def report_refusal(title: str, body: str) -> str | None:
    """Why a report titled ``title`` with ``body`` can't be saved: it has no title, or its body
    is over :data:`REPORT_LIMIT`; none if it can."""
    if not title.strip():
        return NO_TITLE
    if len(body.encode()) > REPORT_LIMIT:
        return TOO_LARGE
    return None


def saved_note(report_id: int) -> str:
    """What the agent that saved the report ``report_id`` is told."""
    return f"Saved report {report_id}."


def unknown_report(report_id: int) -> str:
    """Why the report ``report_id`` can't be read or deleted: there's none."""
    return f"There's no report {report_id}."


def report_id(arguments: dict[str, Any]) -> int | None:
    """The report id ``arguments`` name as ``id``: a whole number, or its digits; none
    otherwise."""
    value = arguments.get("id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def report_list(reports: Sequence[ReportSummary]) -> str:
    """``reports`` as ``list_reports`` answers: a line each, with its id, title, author and
    date, in the order given; a note when there are none."""
    if not reports:
        return NO_REPORTS
    return "\n".join(
        f"{report.id}. {report.title} (by the {report.author}, {report.time:%Y-%m-%d})"
        for report in reports
    )


def report_text(report: ReportShown) -> str:
    """``report`` as ``read_report`` answers: its title as a heading, then its body."""
    return f"# {report.title}\n\n{report.body}"
