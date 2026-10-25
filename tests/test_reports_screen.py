"""Browsing the workspace's reports in the app: ctrl+k lists them, enter shows one, y copies it
and x deletes one once confirmed."""

from datetime import UTC, datetime

import pytest
from textual.widgets import OptionList, Static

from orbit.protocol import (
    Command,
    DeleteReport,
    ListReports,
    ReportShown,
    ReportsListed,
    ReportSummary,
    ShowReport,
)
from orbit.tui import OrbitApp
from orbit.tui.reports import ReportScreen, ReportsHost, ReportsScreen
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.reports import NO_REPORTS_ROW
from orbit.tui.widgets import ConfirmScreen
from tests.tui_fakes import FakeSession, accept_last, feed_events

WHEN = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TIMINGS = ReportSummary(id=1, title="Timings", author="experimenter", time=WHEN)
MEMORY = ReportSummary(id=2, title="Memory", author="planner", time=WHEN)
LISTED = ReportsListed(reports=(MEMORY, TIMINGS))
SHOWN = ReportShown(id=2, title="Memory", body="Peaked at 2 GB.", author="planner", time=WHEN)


def rows(app: OrbitApp) -> list[str]:
    """The rows of the reports list shown."""
    options = app.screen.query_one(OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


async def test_ctrl_k_lists_the_reports_newest_first() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+k")
        assert isinstance(app.screen, ReportsScreen)
        assert session.commands == [ListReports()] and rows(app) == [NO_REPORTS_ROW]
        await feed_events(session, pilot, LISTED)
        assert rows(app) == [
            "Memory · planner · 2026-10-25",
            "Timings · experimenter · 2026-10-25",
        ]
        await pilot.press("escape")
        assert not isinstance(app.screen, ReportsScreen)


async def test_enter_shows_the_report_and_y_copies_it(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    copied: list[str] = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+k")
        await feed_events(session, pilot, LISTED)
        await pilot.press("enter")
        assert session.commands[-1] == ShowReport(id=2)
        await feed_events(session, pilot, SHOWN)
        assert isinstance(app.screen, ReportScreen)
        body = str(app.screen.query_one("#text-body", Static).render())
        assert body == "# Memory\n\nPeaked at 2 GB."
        await pilot.press("y")
        assert copied == ["# Memory\n\nPeaked at 2 GB."]
        await pilot.press("escape")
        assert isinstance(app.screen, ReportsScreen)


async def test_x_deletes_the_highlighted_report_once_confirmed() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+k")
        await feed_events(session, pilot, LISTED)
        await pilot.press("j", "x")
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await pilot.pause()
        assert session.commands[-1] == DeleteReport(id=1)
        await accept_last(session, pilot)
        assert rows(app) == ["Memory · planner · 2026-10-25"]


async def test_x_declined_deletes_nothing() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+k")
        await feed_events(session, pilot, LISTED)
        await pilot.press("x", "n")
        assert session.commands == [ListReports()]
        assert isinstance(app.screen, ReportsScreen)


async def test_enter_and_x_on_an_empty_list_do_nothing() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+k")
        await feed_events(session, pilot, ReportsListed(reports=()))
        await pilot.press("enter", "x")
        assert session.commands == [ListReports()]
        assert isinstance(app.screen, ReportsScreen) and rows(app) == [NO_REPORTS_ROW]


async def test_ctrl_k_with_the_list_open_asks_nothing_more() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+k")
        await app.run_action("list_reports")
        assert session.commands == [ListReports()]


async def test_reports_arriving_before_the_list_mounts_are_listed_as_it_composes() -> None:
    pushed: list[object] = []
    sent: list[Command] = []

    async def send(command: Command, awaited: Awaited) -> None:
        sent.append(command)

    host = ReportsHost(lambda screen, closed: pushed.append(screen), send)
    await host.shelf.open()
    host.shelf.follow(LISTED)
    [screen] = pushed
    assert isinstance(screen, ReportsScreen) and not screen.is_mounted
    assert host.shelf.reports == (MEMORY, TIMINGS) and sent == [ListReports()]
