"""The working row below the conversation shows what runs right now under the shown tab."""

from collections.abc import Mapping

from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.visual import VisualType

from orbit.protocol import (
    Attach,
    Event,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from orbit.tui import OrbitApp
from orbit.tui.transcript import Transcript
from orbit.tui.view.tabs import track_roles
from orbit.tui.working import WorkingRow
from tests.session_apps import CATALOG, OLD, RUNNING, SWITCH_TO_OLD, in_process, joined
from tests.tui_fakes import FakeSession, changes, feed_events, started
from tests.tui_fakes import opened as snapshot_of

NOW = 222.0  # Shows the spinner's first frame.


def turn(agent: str) -> TurnStarted:
    return TurnStarted(turn_id="t", agent_id=agent)


def read(agent: str, call_id: str, path: str) -> ToolCallStarted:
    return ToolCallStarted(
        turn_id="t", agent_id=agent, call_id=call_id, name="Read", arguments={"file_path": path}
    )


def lines(row: WorkingRow) -> tuple[str, ...]:
    """The row's lines, read afresh each time."""
    return row.working.lines


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


async def test_activating_another_session_clears_the_working_lines() -> None:
    app, session = in_process()
    async with app.run_test(size=(80, 24)) as pilot:
        row = await opened(app, pilot)
        await fed(session, pilot, started("agent-2"), turn("agent-2"), read("agent-2", "c1", "a"))
        await fed(session, pilot, CATALOG)
        await pilot.press("j", "k", "enter")
        assert changes(session) == SWITCH_TO_OLD
        await fed(session, pilot, snapshot_of(OLD))
        assert lines(row) == ()
        assert not ticking(app)


async def test_attaching_to_another_session_clears_the_working_lines() -> None:
    app, session = joined()
    async with app.run_test(size=(80, 24)) as pilot:
        row = await opened(app, pilot)
        await fed(session, pilot, CATALOG, RUNNING)
        await pilot.press("enter", "j", "enter")
        assert changes(session) == [Attach(workspace="w", session="old")]
        await fed(session, pilot, started("agent-2"), turn("agent-2"), read("agent-2", "c1", "a"))
        await fed(session, pilot, snapshot_of(OLD))
        assert lines(row) == ()
        assert not ticking(app)


class RowApp(App[None]):
    """The working row alone, its clock stopped at ``NOW``."""

    def compose(self) -> ComposeResult:
        row = WorkingRow()
        row.clock = lambda: NOW
        yield row


async def tracked(pilot: Pilot[None], row: WorkingRow, *events: Event) -> None:
    """Have ``row`` follow ``events``, with the roles the tabs track from them, and lay it
    out."""
    roles: Mapping[str, str] = {}
    for event in events:
        roles = track_roles(roles, event)
        row.working.track(event, roles)
    await pilot.pause()


async def test_four_running_agents_take_a_line_each_and_one_for_the_rest() -> None:
    app = RowApp()
    async with app.run_test(size=(80, 24)) as pilot:
        row = app.query_one(WorkingRow)
        agents = [f"agent-{n}" for n in range(2, 6)]
        await tracked(pilot, row, *(started(a) for a in agents), *(turn(a) for a in agents))
        assert len(lines(row)) == 4 and row.size.height == 4


async def test_a_long_call_is_clipped_to_the_row_and_refit_on_resize() -> None:
    app = RowApp()
    async with app.run_test(size=(80, 24)) as pilot:
        row = app.query_one(WorkingRow)
        path = "x" * 200
        await tracked(pilot, row, started("agent-2"), turn("agent-2"), read("agent-2", "c", path))
        assert len(lines(row)[0]) == row.size.width
        await pilot.resize_terminal(100, 24)
        await pilot.pause()
        assert row.size.width == 100 and len(lines(row)[0]) == row.size.width


async def test_markup_in_a_call_shows_literally() -> None:
    app = RowApp()
    command = "echo [/] [red]x[/red]"
    call = ToolCallStarted(
        turn_id="t", agent_id="agent-2", call_id="c1", name="Bash", arguments={"command": command}
    )
    async with app.run_test(size=(80, 24)) as pilot:
        row = app.query_one(WorkingRow)
        await tracked(pilot, row, started("agent-2"), turn("agent-2"), call)
        assert str(row.render()) == f"◜ agent-2 · Bash {command} 0:00 · 0:00"


async def test_a_line_too_wide_for_the_row_does_not_wrap() -> None:
    app = RowApp()
    async with app.run_test(size=(80, 24)) as pilot:
        row = app.query_one(WorkingRow)
        call = read("agent-2", "c1", "日" * 60)
        await tracked(pilot, row, started("agent-2"), turn("agent-2"), call)
        assert row.size.height == 1


async def test_the_row_is_hidden_and_unfocusable_while_nothing_runs() -> None:
    app = RowApp()
    async with app.run_test(size=(80, 24)) as pilot:
        row = app.query_one(WorkingRow)
        await tracked(pilot, row, started("agent-2"))
        assert lines(row) == () and row.size.height == 0
        assert not row.focusable


async def test_the_row_shows_while_something_runs_and_hides_again() -> None:
    app = RowApp()
    async with app.run_test(size=(80, 24)) as pilot:
        row = app.query_one(WorkingRow)
        await tracked(pilot, row, started("agent-1", "planner"), turn("agent-1"))
        assert lines(row) == ("◜ planner · 0:00",) and row.size.height == 1
        await tracked(pilot, row, TurnFinished(turn_id="t", agent_id="agent-1", reason="completed"))
        assert row.size.height == 0


class TranscriptApp(App[None]):
    """The conversation window alone."""

    def compose(self) -> ComposeResult:
        yield Transcript()


async def test_the_row_sits_below_the_shown_log() -> None:
    app = TranscriptApp()
    async with app.run_test(size=(80, 24)) as pilot:
        transcript = app.query_one(Transcript)
        for event in (started("agent-2"), turn("agent-2")):
            transcript.track(event, {"agent-2": "coder"})
        await pilot.pause()
        row = app.query_one(WorkingRow)
        assert row.size.height == 1 and row.region.y == transcript.shown.region.bottom


class _LayoutSpy(WorkingRow):
    """A working row that records whether each update asked for a layout."""

    def __init__(self) -> None:
        super().__init__()
        self.layouts: list[bool] = []

    def update(self, content: VisualType = "", *, layout: bool = True) -> None:
        self.layouts.append(layout)
        super().update(content, layout=layout)


async def test_the_row_lays_out_only_when_its_line_count_changes() -> None:
    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield _LayoutSpy()

    async with Host().run_test() as pilot:
        row = pilot.app.query_one(_LayoutSpy)
        row.layouts.clear()
        row.show_lines(["◜ one"])
        row.show_lines(["◠ one"])
        row.show_lines(["◝ one", "two"])
        row.show_lines([])
        assert row.layouts == [True, False, True, True]
