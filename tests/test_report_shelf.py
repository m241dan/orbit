"""Following the workspace's reports, without the screens showing them: what the list and a
report say, when they show, and what the user's choices send."""

from datetime import UTC, datetime

from orbit.protocol import (
    DeleteReport,
    ListReports,
    ReportShown,
    ReportsListed,
    ReportSummary,
    ShowReport,
)
from orbit.tui.view.reports import (
    ReportShelf,
    delete_message,
    report_markdown,
    report_row,
    report_title,
)
from tests.desk_fakes import Backend
from tests.view_fakes import ReportsView

WHEN = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TIMINGS = ReportSummary(id=1, title="Timings", author="experimenter", time=WHEN)
MEMORY = ReportSummary(id=2, title="Memory", author="planner", time=WHEN)
SHOWN = ReportShown(id=1, title="Timings", body="It took 3s.", author="experimenter", time=WHEN)


def shelf() -> tuple[ReportShelf, ReportsView, Backend]:
    view, backend = ReportsView(), Backend()
    return ReportShelf(view, backend.desk.send), view, backend


def test_a_report_row_has_its_title_author_and_date() -> None:
    assert report_row(TIMINGS) == "Timings · experimenter · 2026-10-25"


def test_a_report_shown_is_titled_by_its_id_author_and_date_and_reads_as_markdown() -> None:
    assert report_title(SHOWN) == "Report 1 · experimenter · 2026-10-25"
    assert report_markdown(SHOWN) == "# Timings\n\nIt took 3s."


def test_deleting_asks_naming_the_report() -> None:
    assert delete_message(TIMINGS) == "Delete the report 'Timings'? This can't be undone."


async def test_opening_opens_the_list_and_asks_for_the_reports_which_fill_it() -> None:
    reports, view, backend = shelf()
    await reports.open()
    assert view.list_open and backend.commands == [ListReports()]
    reports.follow(ReportsListed(reports=(MEMORY, TIMINGS)))
    assert view.lists == [(MEMORY, TIMINGS)] and reports.reports == (MEMORY, TIMINGS)


async def test_opening_the_list_open_asks_nothing_more() -> None:
    reports, _, backend = shelf()
    await reports.open()
    await reports.open()
    assert backend.commands == [ListReports()]


def test_reports_arriving_while_the_list_is_closed_show_nowhere() -> None:
    reports, view, _ = shelf()
    reports.follow(ReportsListed(reports=(TIMINGS,)))
    reports.follow(SHOWN)
    assert (view.lists, view.reports) == ([], [])


async def test_showing_asks_for_the_report_which_shows_over_the_list() -> None:
    reports, view, backend = shelf()
    await reports.open()
    await reports.show(1)
    assert backend.commands[-1] == ShowReport(id=1)
    reports.follow(SHOWN)
    assert view.reports == [SHOWN]


async def test_a_report_deleted_leaves_the_list_once_accepted() -> None:
    reports, view, backend = shelf()
    await reports.open()
    reports.follow(ReportsListed(reports=(MEMORY, TIMINGS)))
    await reports.delete(1)
    assert backend.commands[-1] == DeleteReport(id=1) and len(view.lists) == 1
    backend.accept()
    assert view.lists[-1] == (MEMORY,)


async def test_a_report_deleted_after_the_list_closed_shows_nowhere() -> None:
    reports, view, backend = shelf()
    await reports.delete(1)
    backend.accept()
    assert view.lists == []
