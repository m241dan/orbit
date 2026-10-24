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
from orbit.tui.detail import DetailPane
from orbit.tui.transcript import Transcript
from orbit.tui.view.detail import DETAIL_COVER_BELOW, DETAIL_HEIGHT, NO_DETAILS, WAITING
from orbit.tui.widgets import ChatPrompt, ConfirmScreen
from orbit.tui.working import WorkingRow
from tests.tui_fakes import FakeSession, feed_events, said, started


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
        assert pane(app).opened and pane(app).border_title == "Detail · Read a.py · y copy"
        assert shown(app) == ["a.py", RULE, "one", "two"]
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
        assert pane(app).border_title == "Detail · complete_task Go · y copy"
        assert shown(app) == ["{", '  "summary": "Go"', "}", RULE, "orbit: Merged."]


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
        assert pane(app).border_title == "Detail · Read b.py · y copy"
        await pilot.press("k")
        assert pane(app).border_title == "Detail · Read a.py · y copy"
        assert shown(app) == ["a.py", RULE, "x"]
        await pilot.press("k")
        assert pane(app).opened and pane(app).border_title == "Detail"
        assert shown(app) == [NO_DETAILS]


async def test_a_running_call_waits_then_shows_its_result_in_place() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1"))
        await inspecting(pilot)
        assert shown(app) == ["a.py", RULE, WAITING]
        await feed_events(session, pilot, done("c1", "done"))
        await pilot.pause()
        assert shown(app) == ["a.py", RULE, "done"]


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


async def test_focus_leaving_the_conversation_closes_the_pane() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1"), done("c1", "x"))
        await inspecting(pilot)
        log = app.query_one(Transcript).shown
        app.query_one(ChatPrompt).enter_insert_mode()
        await pilot.pause()
        assert not pane(app).opened and not log.inspecting


async def test_escape_after_focus_left_keeps_the_pane_closed() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1"), done("c1", "x"))
        await inspecting(pilot)
        app.query_one(ChatPrompt).enter_insert_mode()
        await pilot.pause()
        app.screen.set_focus(None)
        await pilot.press("escape")
        assert not pane(app).opened


async def test_a_dialog_over_the_conversation_leaves_the_pane_open() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1"), done("c1", "x"))
        await inspecting(pilot)
        await pilot.press("ctrl+e")
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")
        assert pane(app).opened and app.focused is app.query_one(Transcript).shown


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


LONG = "\n".join(f"line {n}" for n in range(60))


async def scrolling(app: OrbitApp, session: FakeSession, pilot: Pilot[None]) -> DetailPane:
    """Open the details of a call with long output, from the conversation's list."""
    await feed_events(session, pilot, read("c1"), done("c1", LONG))
    await inspecting(pilot)
    await pilot.pause()
    return pane(app)


async def test_shift_j_and_k_scroll_the_open_pane_from_the_list() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        detail = await scrolling(app, session, pilot)
        await pilot.press("J", "J", "J")
        assert detail.scroll_y == 3
        await pilot.press("K")
        assert detail.scroll_y == 2
        assert app.focused is app.query_one(Transcript).shown


async def test_shift_j_does_nothing_while_the_pane_is_closed() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1"), done("c1", LONG))
        await pilot.press("k", "enter", "J")
        assert not pane(app).opened and pane(app).scroll_y == 0


def selected(app: OrbitApp) -> list[str]:
    return [widget.id or "" for widget in app.query(".selected")]


async def test_tab_from_the_list_activates_the_open_pane_and_j_scrolls_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        detail = await scrolling(app, session, pilot)
        await pilot.press("tab")
        assert app.focused is detail and selected(app) == ["detail"]
        await pilot.press("j", "j", "k")
        assert detail.scroll_y == 1
        assert app.query_one(Transcript).shown.highlighted == 0


async def test_tab_from_the_list_with_the_pane_closed_leaves_the_pane_alone() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1"), done("c1", "x"))
        await pilot.press("k", "enter", "tab")
        assert app.focused is app.query_one(Transcript).shown and selected(app) == ["transcript"]


async def test_escape_from_the_pane_leaves_it_selected_and_open() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await scrolling(app, session, pilot)
        await pilot.press("tab", "escape")
        assert app.focused is None and pane(app).opened and selected(app) == ["detail"]


async def test_letting_go_of_the_held_entry_closes_the_pane() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await scrolling(app, session, pilot)
        await pilot.press("tab", "escape", "k", "escape")
        log = app.query_one(Transcript).shown
        assert not pane(app).opened and selected(app) == ["transcript"]
        assert log.highlighted is None and log.inspecting is False


async def test_letting_go_of_the_held_entry_uncovers_the_conversation() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, SHORT)) as pilot:
        await scrolling(app, session, pilot)
        await pilot.press("tab", "escape", "k", "escape")
        await pilot.pause()
        transcript = app.query_one(Transcript)
        assert not transcript.has_class("-covered")
        assert transcript.shown.size.height == transcript.content_size.height


async def test_window_moves_reach_the_open_pane_between_conversation_and_prompt() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        detail = await scrolling(app, session, pilot)
        await pilot.press("tab", "escape", "k")
        assert selected(app) == ["transcript"]
        await pilot.press("j")
        assert selected(app) == ["detail"]
        await pilot.press("enter", "j")
        assert app.focused is detail and detail.scroll_y == 1
        await pilot.press("escape", "j")
        assert selected(app) == ["prompt"]


async def test_the_pane_s_other_moves_go_to_the_task_list() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await scrolling(app, session, pilot)
        await pilot.press("tab", "escape", "l")
        assert selected(app) == ["task-list"]


async def test_j_from_the_conversation_reaches_the_prompt_while_the_pane_is_closed() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.press("k", "j")
        assert selected(app) == ["prompt"]


async def test_escape_on_the_selected_pane_closes_it_and_selects_the_conversation() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await scrolling(app, session, pilot)
        await pilot.press("tab", "escape", "escape")
        assert not pane(app).opened and selected(app) == ["transcript"]
        assert app.query_one(Transcript).shown.highlighted == 0


async def test_closing_the_active_pane_leaves_it_for_the_conversation() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await scrolling(app, session, pilot)
        await pilot.press("tab", "ctrl+t")  # The planner tab closes the pane.
        await pilot.pause()
        assert app.focused is None and selected(app) == ["transcript"]


SHORT = DETAIL_COVER_BELOW + 6  # Terminal rows leaving the conversation just short of it.


async def test_on_a_short_terminal_the_open_pane_covers_the_conversation() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, SHORT)) as pilot:
        transcript = app.query_one(Transcript)
        assert transcript.content_size.height < DETAIL_COVER_BELOW
        detail = await scrolling(app, session, pilot)
        assert detail.size.height + 1 == transcript.content_size.height
        assert transcript.shown.size.height == 0
        assert app.focused is transcript.shown


async def test_closing_the_covering_pane_shows_the_conversation_again() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, SHORT)) as pilot:
        await scrolling(app, session, pilot)
        await pilot.press("escape")
        await pilot.pause()
        transcript = app.query_one(Transcript)
        assert transcript.shown.size.height == transcript.content_size.height


async def test_growing_the_terminal_gives_the_conversation_back_its_room() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, SHORT)) as pilot:
        detail = await scrolling(app, session, pilot)
        await pilot.resize_terminal(80, 40)
        await pilot.pause()
        transcript = app.query_one(Transcript)
        assert detail.size.height + 1 == transcript.content_size.height * DETAIL_HEIGHT // 100
        assert transcript.shown.size.height > 0
        await pilot.resize_terminal(80, SHORT)
        await pilot.pause()
        assert transcript.shown.size.height == 0


async def test_the_pane_starts_at_the_top_for_another_entry() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c0"), done("c0", LONG))
        detail = await scrolling(app, session, pilot)
        await pilot.press("J", "J", "k")
        await pilot.pause()
        assert detail.border_title == "Detail · Read a.py · y copy" and detail.scroll_y == 0


async def test_the_pane_keeps_its_place_as_its_entry_updates() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1", LONG))
        await inspecting(pilot)
        await pilot.press("J", "J")
        await feed_events(session, pilot, done("c1", "x"))
        await pilot.pause()
        assert shown(app)[-1] == "x" and pane(app).scroll_y == 2


async def test_reopening_the_pane_starts_at_the_top() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        detail = await scrolling(app, session, pilot)
        await pilot.press("J", "J", "escape", "enter")
        await pilot.pause()
        assert detail.opened and detail.scroll_y == 0


async def test_another_call_finishing_leaves_the_open_pane_alone() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c2", "b.py"))
        detail = await scrolling(app, session, pilot)
        await pilot.press("J", "J")
        before = shown(app)
        shows: list[object] = []
        detail.show = lambda *args: shows.append(args)  # type: ignore[method-assign]
        await feed_events(session, pilot, done("c2", "x"))
        await pilot.pause()
        assert shows == [] and shown(app) == before and detail.scroll_y == 2


async def test_a_scrolled_pane_starts_at_the_top_for_another_call() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        long = "\n".join(f"line {n}" for n in range(150))
        await feed_events(session, pilot, read("c0"), done("c0", "x"), read("c1"), done("c1", long))
        await inspecting(pilot)
        await pilot.pause()
        pane(app).scroll_to(y=50, animate=False)
        await pilot.pause()
        assert pane(app).scroll_y == 50
        await pilot.press("k")
        await pilot.pause()
        assert pane(app).border_title == "Detail · Read a.py · y copy" and pane(app).scroll_y == 0


async def test_a_running_call_finishing_in_place_keeps_the_pane_s_offset() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1", LONG))
        await inspecting(pilot)
        await pilot.pause()
        pane(app).scroll_to(y=5, animate=False)
        await pilot.pause()
        await feed_events(session, pilot, done("c1", LONG))
        await pilot.pause()
        assert shown(app)[-1] == "line 59" and pane(app).scroll_y == 5
