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
LATEST = ReportSummary(id=3, title="Latest", author="planner", time=WHEN)
LISTED = ReportsListed(reports=(MEMORY, TIMINGS))
SHOWN = ReportShown(id=2, title="Memory", body="Peaked at 2 GB.", author="planner", time=WHEN)


def rows(app: OrbitApp) -> list[str]:
    """The rows of the reports list shown."""
    options = app.screen.query_one(OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


def highlighted(app: OrbitApp) -> int | None:
    """The row highlighted in the reports list shown, if any."""
    return app.screen.query_one(OptionList).highlighted


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


async def test_a_new_report_keeps_the_highlight_on_the_report_it_was_on() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+k")
        await feed_events(session, pilot, LISTED)
        await pilot.press("j")
        await feed_events(session, pilot, ReportsListed(reports=(LATEST, MEMORY, TIMINGS)))
        assert highlighted(app) == 2
        await pilot.press("enter")
        assert session.commands[-1] == ShowReport(id=1)


async def test_deleting_the_highlighted_report_highlights_the_one_after_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+k")
        await feed_events(session, pilot, ReportsListed(reports=(LATEST, MEMORY, TIMINGS)))
        await pilot.press("j", "x", "y")
        await pilot.pause()
        assert session.commands[-1] == DeleteReport(id=2)
        await accept_last(session, pilot)
        assert rows(app) == [
            "Latest · planner · 2026-10-25",
            "Timings · experimenter · 2026-10-25",
        ]
        assert highlighted(app) == 1


async def test_a_pick_of_reports_gone_since_neither_shows_another_nor_fails() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+k")
        await feed_events(session, pilot, LISTED)
        options = app.screen.query_one(OptionList)
        memory, timings = options.get_option_at_index(0), options.get_option_at_index(1)
        await feed_events(session, pilot, ReportsListed(reports=(LATEST,)))
        options.post_message(OptionList.OptionSelected(options, memory, 0))
        options.post_message(OptionList.OptionSelected(options, timings, 1))
        await pilot.pause()
        assert session.commands == [ListReports()]
        assert isinstance(app.screen, ReportsScreen)


async def test_a_report_arriving_twice_shows_in_one_screen() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+k")
        await feed_events(session, pilot, LISTED)
        await pilot.press("enter", "enter")
        assert session.commands[-2:] == [ShowReport(id=2), ShowReport(id=2)]
        await feed_events(session, pilot, SHOWN, SHOWN)
        assert [type(screen) for screen in app.screen_stack].count(ReportScreen) == 1
        await pilot.press("escape")
        assert isinstance(app.screen, ReportsScreen)


async def test_a_report_closed_shows_again_once_chosen_again() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+k")
        await feed_events(session, pilot, LISTED)
        await pilot.press("enter")
        await feed_events(session, pilot, SHOWN)
        await pilot.press("escape", "enter")
        await feed_events(session, pilot, SHOWN)
        assert isinstance(app.screen, ReportScreen)


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
