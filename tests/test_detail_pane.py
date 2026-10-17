"""Enter on a tool call or request opens a pane below the conversation with its full content,
which follows the cursor until escape closes it."""

import io

from rich.console import Console, Group, RenderableType
from rich.text import Text
from textual.pilot import Pilot
from textual.widgets import Static

from orbit.protocol import (
    AgentReply,
    AgentRequest,
    ToolCallFinished,
    ToolCallStarted,
    TurnStarted,
)
from orbit.tui import OrbitApp
from orbit.tui.transcript import DetailPane, Transcript, TranscriptLog, WorkingRow
from orbit.tui.view import DETAIL_HEIGHT, NO_DETAILS, WAITING
from tests.test_tui import FakeSession, feed_events, said, started


def read(call_id: str, path: str = "a.py") -> ToolCallStarted:
    return ToolCallStarted(
        turn_id="t", call_id=call_id, name="Read", arguments={"file_path": path}, agent_id="agent-1"
    )


def done(call_id: str, output: str) -> ToolCallFinished:
    return ToolCallFinished(turn_id="t", call_id=call_id, output=output, agent_id="agent-1")


def pane(app: OrbitApp) -> DetailPane:
    return app.query_one(DetailPane)


def plain(renderable: RenderableType) -> list[str]:
    """``renderable``'s lines as drawn 40 columns wide."""
    out = io.StringIO()
    Console(width=40, file=out, color_system=None).print(renderable)
    return out.getvalue().splitlines()


def shown(app: OrbitApp) -> list[str]:
    """What the detail pane shows, line by line."""
    content = pane(app).query_one(Static).content
    assert isinstance(content, Text | Group)
    return plain(content)


RULE = "─" * 40


async def inspecting(pilot: Pilot[None]) -> None:
    """Activate the conversation and press enter on its latest entry."""
    await pilot.press("k", "enter", "enter")


async def test_enter_on_a_tool_call_opens_its_details() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1"), done("c1", "one\ntwo"))
        assert not pane(app).opened and pane(app).size.height == 0
        await inspecting(pilot)
        assert pane(app).opened and pane(app).border_title == "Detail · Read a.py"
        assert shown(app) == ["file_path: a.py", RULE, "one", "two"]
        assert app.focused is app.query_one(Transcript).shown


async def test_the_pane_takes_its_share_of_the_conversation_below_the_working_row() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        turn = TurnStarted(turn_id="t", agent_id="agent-1")
        await feed_events(session, pilot, started("agent-1"), turn, read("c1"))
        await inspecting(pilot)
        await pilot.pause()
        transcript = app.query_one(Transcript)
        detail, working = pane(app), app.query_one(WorkingRow)
        assert detail.size.height + 1 == transcript.content_size.height * DETAIL_HEIGHT // 100
        assert working.size.height == 1 and working.region.bottom <= detail.region.y
        assert transcript.shown.region.bottom <= working.region.y


async def test_enter_on_a_request_opens_its_arguments_and_reply() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        request = AgentRequest(
            agent_id="agent-1", request_id="r", tool="complete_task", arguments={"summary": "Go"}
        )
        reply = AgentReply(agent_id="agent-1", request_id="r", text="Merged.")
        await feed_events(session, pilot, request, reply)
        await inspecting(pilot)
        assert pane(app).border_title == "Detail · complete_task Go"
        assert shown(app) == ["summary: Go", RULE, "Merged."]


async def test_enter_on_a_message_still_collapses_and_expands_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "a\nb"))
        log = app.query_one(Transcript).shown
        await inspecting(pilot)
        assert log.collapsed(1) and not pane(app).opened


async def test_the_pane_follows_the_cursor_and_notes_entries_without_details() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(
            session, pilot, said("agent-1", "hi"), read("c1"), done("c1", "x"), read("c2", "b.py")
        )
        await inspecting(pilot)
        assert pane(app).border_title == "Detail · Read b.py"
        await pilot.press("k")
        assert pane(app).border_title == "Detail · Read a.py"
        assert shown(app) == ["file_path: a.py", RULE, "x"]
        await pilot.press("k")
        assert pane(app).opened and pane(app).border_title == "Detail"
        assert shown(app) == [NO_DETAILS]


async def test_a_running_call_waits_then_shows_its_result_in_place() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1"))
        await inspecting(pilot)
        assert shown(app) == ["file_path: a.py", RULE, WAITING]
        await feed_events(session, pilot, done("c1", "done"))
        await pilot.pause()
        assert shown(app) == ["file_path: a.py", RULE, "done"]


async def test_escape_closes_the_pane_first_then_leaves_list_mode() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1"), done("c1", "x"))
        await inspecting(pilot)
        log = app.query_one(Transcript).shown
        await pilot.press("escape")
        assert not pane(app).opened and app.focused is log
        await pilot.press("escape")
        assert app.focused is None and log.highlighted == 0


async def test_a_closed_pane_stays_closed_as_the_cursor_moves() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1"), done("c1", "x"), read("c2"))
        await inspecting(pilot)
        await pilot.press("escape", "k")
        await feed_events(session, pilot, done("c2", "y"))
        assert not pane(app).opened


async def test_switching_tabs_closes_the_pane_for_good() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, read("c1"), done("c1", "x"))
        await inspecting(pilot)
        await pilot.press("ctrl+t")  # The planner tab.
        assert not pane(app).opened
        await pilot.press("ctrl+t", "enter", "j", "k")  # Back to All, and its list again.
        await pilot.pause()
        assert not pane(app).opened


def test_a_log_without_a_cursor_has_no_details() -> None:
    assert TranscriptLog().detail is None
