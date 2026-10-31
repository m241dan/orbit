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
from orbit.tui.view.common import kept_highlight
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


def report_keys(reports: Sequence[ReportSummary]) -> tuple[str, ...]:
    """The keys of the rows listing ``reports``, in order: their ids."""
    return tuple(str(report.id) for report in reports)


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
    """Where the reports show: their list, one at a time, and a report the user opens over it,
    one at a time."""

    @property
    def list_open(self) -> bool:
        """Whether the list is open."""
        ...

    @property
    def highlighted(self) -> int | None:
        """The row highlighted in the list open, if any."""
        ...

    @property
    def report_open(self) -> bool:
        """Whether a report is open over the list."""
        ...

    def open_list(self) -> None:
        """Open the list, empty until :meth:`show_list` fills it."""
        ...

    def show_list(self, reports: Sequence[ReportSummary], highlight: int | None) -> None:
        """List ``reports``, in order, in the list open, highlighting row ``highlight``, if any."""
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

    @property
    def keys(self) -> tuple[str, ...]:
        """The keys of the rows listing the reports, in order: their ids."""
        return report_keys(self.reports)

    @property
    def highlighted(self) -> ReportSummary | None:
        """The report highlighted in the list, if any."""
        index = self._view.highlighted
        return None if index is None else self.reports[index]

    async def open(self) -> None:
        """Open the list, unless it's open, and ask for the reports to fill it."""
        if self._view.list_open:
            return
        self.reports = ()
        self._view.open_list()
        await self._send(ListReports(), Awaited())

    def follow(self, event: ReportsListed | ReportShown) -> None:
        """Show the reports, or the report, ``event`` brings, while the list is open; a report
        only while none is open over it."""
        if not self._view.list_open:
            return
        if isinstance(event, ReportsListed):
            self._relist(event.reports)
        elif not self._view.report_open:
            self._view.show_report(event)

    async def choose(self, row: int) -> None:
        """Ask for the report on row ``row`` of the list, to show it."""
        await self._send(ShowReport(id=self.reports[row].id), Awaited())

    async def delete(self, report_id: int) -> None:
        """Delete the report ``report_id``, taking it off the list once that's done."""
        await self._send(
            DeleteReport(id=report_id), Awaited(accepted=lambda: self._gone(report_id))
        )

    def _gone(self, report_id: int) -> None:
        """Take the report ``report_id``, now deleted, off the list, if it's open."""
        if self._view.list_open:
            self._relist(tuple(report for report in self.reports if report.id != report_id))

    def _relist(self, reports: tuple[ReportSummary, ...]) -> None:
        """List ``reports`` in place of those listed, the highlight going where
        ``kept_highlight`` says, or onto the first report when none was highlighted."""
        shown = self.keys
        self.reports = reports
        highlight = kept_highlight(shown, self._view.highlighted, self.keys)
        self._view.show_list(reports, 0 if highlight is None and reports else highlight)
