"""The working row below the conversation shows what runs right now under the shown tab."""

from textual.pilot import Pilot

from orbit.protocol import (
    AgentExited,
    Event,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from orbit.tui import OrbitApp
from orbit.tui.tabs import ConversationTabs
from orbit.tui.transcript import Transcript
from orbit.tui.working import WorkingRow
from tests.test_transcript import labels
from tests.test_tui import FakeSession, feed_events, started

NOW = 222.0  # Shows the spinner's first frame.


def turn(agent: str) -> TurnStarted:
    return TurnStarted(turn_id="t", agent_id=agent)


def finished(agent: str) -> TurnFinished:
    return TurnFinished(turn_id="t", agent_id=agent, reason="completed")


def read(agent: str, call_id: str, path: str) -> ToolCallStarted:
    return ToolCallStarted(
        turn_id="t", agent_id=agent, call_id=call_id, name="Read", arguments={"file_path": path}
    )


def done(agent: str, call_id: str) -> ToolCallFinished:
    return ToolCallFinished(turn_id="t", agent_id=agent, call_id=call_id, output="")


def lines(row: WorkingRow) -> tuple[str, ...]:
    """The row's lines, read afresh each time."""
    return row.lines


def ticking(app: OrbitApp) -> bool:
    """Whether the conversation's spinners and clocks are turning."""
    return app.query_one(Transcript).ticking


async def opened(app: OrbitApp, pilot: Pilot[None]) -> WorkingRow:
    """The app's working row, its clock stopped at ``NOW``."""
    row = app.query_one(WorkingRow)
    row.clock = lambda: NOW
    await pilot.pause()
    return row


async def fed(session: FakeSession, pilot: Pilot[None], *events: Event) -> None:
    await feed_events(session, pilot, *events)
    await pilot.pause()


async def test_the_row_is_hidden_and_still_while_nothing_runs() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(80, 24)) as pilot:
        row = await opened(app, pilot)
        assert lines(row) == ()
        assert row.size.height == 0
        assert not ticking(app)
        assert not row.focusable


async def test_the_planner_shows_while_its_turn_runs() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        row = await opened(app, pilot)
        await fed(session, pilot, started("agent-1", "planner"), turn("agent-1"))
        assert lines(row) == ("◜ planner · 0:00",)
        assert row.size.height == 1
        assert ticking(app)
        await fed(session, pilot, finished("agent-1"))
        assert lines(row) == ()
        assert row.size.height == 0
        assert not ticking(app)


async def test_the_row_follows_tool_calls_starting_and_finishing() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        row = await opened(app, pilot)
        await fed(session, pilot, started("agent-2"), turn("agent-2"), read("agent-2", "c1", "a"))
        assert lines(row) == ("◜ agent-2 · Read a 0:00 · 0:00",)
        await fed(session, pilot, read("agent-2", "c2", "b"))
        assert lines(row) == ("◜ agent-2 · Read b (+1 running) 0:00 · 0:00",)
        await fed(session, pilot, done("agent-2", "c2"), done("agent-2", "c1"))
        assert lines(row) == ("◜ agent-2 · 0:00",)


async def test_the_clocks_tick_while_something_runs() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        row = await opened(app, pilot)
        await fed(session, pilot, started("agent-2"), turn("agent-2"))
        row.clock = lambda: NOW + 12
        row.tick()
        assert lines(row) == ("◜ agent-2 · 0:12",)


async def test_one_ticker_turns_a_call_and_its_row_until_the_turn_ends() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        row = await opened(app, pilot)
        await fed(session, pilot, started("agent-2"), turn("agent-2"), read("agent-2", "c1", "a"))
        assert ticking(app)
        row.clock = lambda: NOW + 12
        app.query_one(Transcript).tick()
        assert lines(row) == ("◜ agent-2 · Read a 0:12 · 0:12",)
        await fed(session, pilot, finished("agent-2"))
        assert lines(row) == ()
        assert not ticking(app)


async def test_four_running_agents_show_three_lines_and_how_many_more() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        row = await opened(app, pilot)
        agents = [f"agent-{n}" for n in range(2, 6)]
        await fed(session, pilot, *(started(a) for a in agents), *(turn(a) for a in agents))
        assert lines(row) == (
            "◜ agent-2 · 0:00",
            "◜ agent-3 · 0:00",
            "◜ agent-4 · 0:00",
            "+1 more",
        )
        assert row.size.height == 4


async def test_an_agent_exiting_leaves_the_row() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        row = await opened(app, pilot)
        await fed(session, pilot, started("agent-2"), turn("agent-2"))
        await fed(session, pilot, AgentExited(agent_id="agent-2", reason="crashed"))
        assert lines(row) == ()
        assert not ticking(app)


async def test_each_tab_shows_only_its_own_running_agents() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        row = await opened(app, pilot)
        await fed(session, pilot, started("agent-1", "planner"), started("agent-2"))
        await fed(session, pilot, turn("agent-1"), turn("agent-2"))
        assert lines(row) == ("◜ planner · 0:00", "◜ agent-2 · 0:00")
        app.query_one(ConversationTabs).active = "tab-coder"
        await pilot.pause()
        assert lines(row) == ("◜ agent-2 · 0:00",)
        await pilot.press("ctrl+t")  # The planner tab.
        assert lines(row) == ("◜ planner · 0:00",)
        app.query_one(ConversationTabs).active = "tab-reviewer"
        await pilot.pause()
        assert lines(row) == ()
        assert row.size.height == 0
        assert ticking(app)  # Others still run.


async def test_the_row_sits_below_the_shown_log() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        row = await opened(app, pilot)
        await fed(session, pilot, started("agent-2"), turn("agent-2"))
        log = app.query_one(Transcript).shown
        assert row.region.y == log.region.bottom


async def test_a_long_call_is_clipped_to_the_row_and_refit_on_resize() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        row = await opened(app, pilot)
        path = "x" * 200
        await fed(session, pilot, started("agent-2"), turn("agent-2"), read("agent-2", "c", path))
        assert len(lines(row)[0]) == row.size.width
        await pilot.resize_terminal(100, 24)
        await pilot.pause()
        assert len(lines(row)[0]) == row.size.width


async def test_markup_in_a_call_shows_literally() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    command = "echo [/] [red]x[/red]"
    call = ToolCallStarted(
        turn_id="t", agent_id="agent-2", call_id="c1", name="Bash", arguments={"command": command}
    )
    async with app.run_test(size=(80, 24)) as pilot:
        row = await opened(app, pilot)
        await fed(session, pilot, started("agent-2"), turn("agent-2"), call)
        assert lines(row) == (f"◜ agent-2 · Bash {command} 0:00 · 0:00",)
        assert str(row.render()) == f"◜ agent-2 · Bash {command} 0:00 · 0:00"


async def test_a_line_too_wide_for_the_row_does_not_wrap() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        row = await opened(app, pilot)
        await fed(
            session, pilot, started("agent-2"), turn("agent-2"), read("agent-2", "c1", "日" * 60)
        )
        assert row.size.height == 1


async def test_a_cancelled_planner_turn_leaves_the_row_and_ends_its_running_call() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        row = await opened(app, pilot)
        planner = (started("agent-1", "planner"), turn("agent-1"), read("agent-1", "c1", "a.py"))
        await fed(session, pilot, *planner)
        assert lines(row) == ("◜ planner · Read a.py 0:00 · 0:00",)
        cancelled = TurnFinished(turn_id="t", agent_id="agent-1", reason="cancelled")
        await fed(session, pilot, cancelled)
        assert lines(row) == ()
        assert "agent-1: → Read a.py ⊘ no result" in labels(app)
