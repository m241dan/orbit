"""The workspace's reports: saved, listed and read by agents through orbit's tools, and listed,
shown and deleted by the user through their frontend."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from orbit.agent.application.desk import Tool
from orbit.agent.application.ports import Publish, Reply, SessionStore
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.reports import (
    NO_ID,
    NewReport,
    report_id,
    report_list,
    report_refusal,
    report_text,
    saved_note,
    unknown_report,
)
from orbit.agent.domain.roles import LIST_REPORTS, READ_REPORT, SAVE_REPORT
from orbit.protocol import (
    UNKNOWN,
    DeleteReport,
    ListReports,
    ReportShown,
    ReportsListed,
    ShowReport,
)

ReportCommand = ListReports | ShowReport | DeleteReport
"""The commands a frontend lists, shows and deletes reports with."""


class Reports:
    """The reports kept in ``store``, each saved in the session ``session`` says is open, at the
    time ``clock`` tells."""

    def __init__(
        self, store: SessionStore, session: Callable[[], str], clock: Callable[[], datetime]
    ) -> None:
        self._store = store
        self._session = session
        self._clock = clock

    def tools(self, author: str) -> dict[str, Tool]:
        """The report tools as an agent in the role ``author`` calls them."""

        async def save(arguments: dict[str, Any]) -> Reply:
            return await self._save(author, arguments)

        return {SAVE_REPORT: save, LIST_REPORTS: self._list, READ_REPORT: self._read}

    async def _save(self, author: str, arguments: dict[str, Any]) -> Reply:
        title, body = str(arguments.get("title", "")).strip(), str(arguments.get("body", ""))
        refusal = report_refusal(title, body)
        if refusal is not None:
            return Reply(refusal, is_error=True)
        report = NewReport(title, body, author, self._session(), self._clock())
        return Reply(saved_note(await self._store.save_report(report)))

    async def _list(self, arguments: dict[str, Any]) -> Reply:
        return Reply(report_list(await self._store.reports()))

    async def _read(self, arguments: dict[str, Any]) -> Reply:
        wanted = report_id(arguments)
        if wanted is None:
            return Reply(NO_ID, is_error=True)
        report = await self._store.report(wanted)
        if report is None:
            return Reply(unknown_report(wanted), is_error=True)
        return Reply(report_text(report))

    async def answer(self, command: ReportCommand, publish: Publish) -> None:
        """Carry out ``command``, publishing with ``publish`` the list or the report it asks
        for; refused as ``unknown`` for a report there's none of."""
        if isinstance(command, ListReports):
            await publish(await self.listed())
        elif isinstance(command, ShowReport):
            await publish(await self.shown(command.id))
        else:
            await self.delete(command.id)

    async def listed(self) -> ReportsListed:
        """The reports kept, newest first, as a frontend is sent them."""
        return ReportsListed(reports=tuple(await self._store.reports()))

    async def shown(self, wanted: int) -> ReportShown:
        """The report ``wanted``, as a frontend is sent it; refused as ``unknown`` when there's
        none."""
        report = await self._store.report(wanted)
        if report is None:
            raise Refused(UNKNOWN, unknown_report(wanted))
        return report

    async def delete(self, wanted: int) -> None:
        """Delete the report ``wanted``; refused as ``unknown`` when there's none."""
        if not await self._store.delete_report(wanted):
            raise Refused(UNKNOWN, unknown_report(wanted))
