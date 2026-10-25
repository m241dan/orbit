"""The workspace's reports: the list the user browses, a report shown, and what their choices
send."""

from collections.abc import Sequence
from typing import Protocol

from orbit.protocol import (
    DeleteReport,
    ListReports,
    ReportShown,
    ReportsListed,
    ReportSummary,
    ShowReport,
)
from orbit.tui.view.outcomes import Awaited, Send

REPORTS_TITLE = "Reports"
REPORTS_HINT = "enter show · x delete · esc close"
REPORT_HINT = "j/k scroll · y copy · esc close"
NO_REPORTS_ROW = "No reports yet."
DELETE_TITLE = "Delete report"
DELETE_YES = "delete"


def report_row(report: ReportSummary) -> str:
    """``report`` as a row of the list: its title, author and date."""
    return f"{report.title} · {report.author} · {report.time:%Y-%m-%d}"


def report_title(report: ReportShown) -> str:
    """The title of the view showing ``report``: its id, author and date."""
    return f"Report {report.id} · {report.author} · {report.time:%Y-%m-%d}"


def report_markdown(report: ReportShown) -> str:
    """``report`` as shown and copied: its title as a heading, then its body."""
    return f"# {report.title}\n\n{report.body}"


def delete_message(report: ReportSummary) -> str:
    """What deleting ``report`` asks."""
    return f"Delete the report '{report.title}'? This can't be undone."


class ReportsView(Protocol):
    """Where the reports show: their list, one at a time, and a report the user opens."""

    @property
    def list_open(self) -> bool:
        """Whether the list is open."""
        ...

    def open_list(self) -> None:
        """Open the list, empty until :meth:`show_list` fills it."""
        ...

    def show_list(self, reports: Sequence[ReportSummary]) -> None:
        """List ``reports``, in order, in the list open."""
        ...

    def show_report(self, report: ReportShown) -> None:
        """Show ``report`` in full, over the list."""
        ...


class ReportShelf:
    """Follows the workspace's reports in ``view`` while its list is open, asking the backend
    for them, a report and deletions with ``send``."""

    def __init__(self, view: ReportsView, send: Send) -> None:
        self._view = view
        self._send = send
        self.reports: tuple[ReportSummary, ...] = ()  # Those listed, newest first.

    async def open(self) -> None:
        """Open the list, unless it's open, and ask for the reports to fill it."""
        if self._view.list_open:
            return
        self.reports = ()
        self._view.open_list()
        await self._send(ListReports(), Awaited())

    def follow(self, event: ReportsListed | ReportShown) -> None:
        """Show the reports, or the report, ``event`` brings, while the list is open."""
        if not self._view.list_open:
            return
        if isinstance(event, ReportsListed):
            self.reports = event.reports
            self._view.show_list(event.reports)
        else:
            self._view.show_report(event)

    async def show(self, report_id: int) -> None:
        """Ask for the report ``report_id``, to show it."""
        await self._send(ShowReport(id=report_id), Awaited())

    async def delete(self, report_id: int) -> None:
        """Delete the report ``report_id``, taking it off the list once that's done."""
        await self._send(
            DeleteReport(id=report_id), Awaited(accepted=lambda: self._gone(report_id))
        )

    def _gone(self, report_id: int) -> None:
        """Take the report ``report_id``, now deleted, off the list, if it's open."""
        self.reports = tuple(report for report in self.reports if report.id != report_id)
        if self._view.list_open:
            self._view.show_list(self.reports)
