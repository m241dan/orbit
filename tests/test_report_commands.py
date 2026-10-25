"""Frontends browsing the workspace's reports: listing them, showing one and deleting one."""

from orbit.agent import WorkspaceHost
from orbit.agent.application.workspace_host import NO_REPORTS_KEPT
from orbit.agent.domain.reports import NewReport
from orbit.protocol import (
    INVALID,
    UNKNOWN,
    CommandAccepted,
    CommandRefused,
    DeleteReport,
    ListReports,
    ReportShown,
    ReportsListed,
    ReportSummary,
    ShowReport,
)
from tests.agent_fakes import START, FakeStore, Ticks
from tests.attach_fakes import outcome, stored, viewing, workspace
from tests.connection_helpers import joined, received, say

TIMINGS = NewReport("Timings", "It took 3s.", "experimenter", "s0", START)
MEMORY = NewReport("Memory", "Peaked at 2 GB.", "planner", "s0", START)


def with_reports(store: FakeStore) -> FakeStore:
    """``store``, keeping the reports Timings (1) and Memory (2)."""
    store.reports_kept = {1: TIMINGS, 2: MEMORY}
    return store


def host(store: FakeStore | None) -> WorkspaceHost:
    return WorkspaceHost(store=store, clock=Ticks(), new_id=lambda: "today", name="today")


async def test_listing_answers_the_asking_connection_alone_newest_first() -> None:
    session = host(with_reports(FakeStore()))
    asking, other = await joined(session, "asking", read=True), await joined(session, "other")
    await received(other)
    await say(asking, "list", ListReports())
    events, others = await received(asking), await received(other)
    await session.close()

    assert [e for e in events if isinstance(e, ReportsListed)] == [
        ReportsListed(
            reports=(
                ReportSummary(id=2, title="Memory", author="planner", time=START),
                ReportSummary(id=1, title="Timings", author="experimenter", time=START),
            )
        )
    ]
    assert outcome(events) == CommandAccepted(command_id="list")
    assert not any(isinstance(e, ReportsListed) for e in others)


async def test_showing_a_report_sends_it_whole() -> None:
    session = host(with_reports(FakeStore()))
    asking = await joined(session, "asking", read=True)
    await say(asking, "show", ShowReport(id=1))
    events = await received(asking)
    await session.close()

    shown = ReportShown(
        id=1, title="Timings", body="It took 3s.", author="experimenter", time=START
    )
    assert [e for e in events if isinstance(e, ReportShown)] == [shown]


async def test_deleting_a_report_removes_it() -> None:
    store = with_reports(FakeStore())
    session = host(store)
    asking = await joined(session, "asking", read=True)
    await say(asking, "delete", DeleteReport(id=1))
    events = await received(asking)
    await session.close()

    assert outcome(events) == CommandAccepted(command_id="delete")
    assert list(store.reports_kept) == [2]


async def test_showing_or_deleting_an_unknown_report_is_refused() -> None:
    session = host(FakeStore())
    asking = await joined(session, "asking", read=True)
    await say(asking, "show", ShowReport(id=9))
    shown = outcome(await received(asking))
    await say(asking, "delete", DeleteReport(id=9))
    deleted = outcome(await received(asking))
    await session.close()

    message = "There's no report 9."
    assert shown == CommandRefused(command_id="show", code=UNKNOWN, message=message)
    assert deleted == CommandRefused(command_id="delete", code=UNKNOWN, message=message)


async def test_without_a_store_reports_are_refused() -> None:
    session = host(None)
    asking = await joined(session, "asking", read=True)
    await say(asking, "list", ListReports())
    events = await received(asking)
    await session.close()

    assert outcome(events) == CommandRefused(
        command_id="list", code=INVALID, message=NO_REPORTS_KEPT
    )


async def test_a_read_only_view_lists_the_workspaces_reports() -> None:
    session = workspace(with_reports(stored()))
    viewer = await viewing(session, "viewer")
    await say(viewer, "list", ListReports())
    events = await received(viewer)
    await session.close()

    [listed] = [e for e in events if isinstance(e, ReportsListed)]
    assert [report.id for report in listed.reports] == [2, 1]
