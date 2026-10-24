"""The working row's lines, without the row showing them: who runs under the shown tab, what
they're running and for how long."""

from collections.abc import Mapping, Sequence

from orbit.protocol import (
    PLANNER_LANE,
    AgentExited,
    Event,
    RunningAgent,
    Snapshot,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from orbit.tui.view.tabs import track_roles
from orbit.tui.view.working import WorkingLines
from tests.session_apps import OLD
from tests.tui_fakes import opened as snapshot_of
from tests.tui_fakes import started

NOW = 222.0  # Shows the spinner's first frame.


class Row:
    """The working row as the lines show in it, ``width`` columns wide: what it shows each
    time, in turn."""

    def __init__(self, width: int = 80) -> None:
        self.width = width
        self.shows: list[tuple[str, ...]] = []

    def show_lines(self, lines: Sequence[str]) -> None:
        self.shows.append(tuple(lines))


class Lines:
    """Working lines shown in a :class:`Row`, at ``now``, following events with the roles
    the conversation's tabs track from them."""

    def __init__(self, width: int = 80) -> None:
        self.row = Row(width)
        self.now = NOW
        self.roles: Mapping[str, str] = {}
        self.working = WorkingLines(self.row, lambda: self.now)

    def feed(self, *events: Event) -> None:
        for event in events:
            self.roles = track_roles(self.roles, event)
            self.working.track(event, self.roles)

    def shown(self) -> tuple[str, ...]:
        """The lines shown, read afresh each time."""
        return self.working.lines


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


def test_nothing_runs_at_first_and_nothing_is_shown() -> None:
    lines = Lines()
    lines.feed(started("agent-2"))
    assert lines.shown() == () and not lines.working.running
    assert lines.row.shows == []


def test_the_planner_shows_while_its_turn_runs() -> None:
    lines = Lines()
    lines.feed(started("agent-1", "planner"), turn("agent-1"))
    assert lines.shown() == ("◜ planner · 0:00",) and lines.working.running
    assert lines.row.shows == [("◜ planner · 0:00",)]
    lines.feed(finished("agent-1"))
    assert lines.shown() == () and not lines.working.running
    assert lines.row.shows[-1] == ()


def test_the_lines_follow_tool_calls_starting_and_finishing() -> None:
    lines = Lines()
    lines.feed(started("agent-2"), turn("agent-2"), read("agent-2", "c1", "a"))
    assert lines.shown() == ("◜ agent-2 · Read a 0:00 · 0:00",)
    lines.feed(read("agent-2", "c2", "b"))
    assert lines.shown() == ("◜ agent-2 · Read b (+1 running) 0:00 · 0:00",)
    lines.feed(done("agent-2", "c2"), done("agent-2", "c1"))
    assert lines.shown() == ("◜ agent-2 · 0:00",)


def test_the_clocks_tick_while_something_runs() -> None:
    lines = Lines()
    lines.feed(started("agent-2"), turn("agent-2"))
    lines.now = NOW + 12
    lines.working.tick()
    assert lines.shown() == ("◜ agent-2 · 0:12",)


def test_a_tick_showing_the_same_lines_shows_nothing_again() -> None:
    lines = Lines()
    lines.feed(started("agent-2"), turn("agent-2"))
    lines.working.tick()
    assert lines.row.shows == [("◜ agent-2 · 0:00",)]


def test_four_running_agents_show_three_lines_and_how_many_more() -> None:
    lines = Lines()
    agents = [f"agent-{n}" for n in range(2, 6)]
    lines.feed(*(started(a) for a in agents), *(turn(a) for a in agents))
    assert lines.shown() == (
        "◜ agent-2 · 0:00",
        "◜ agent-3 · 0:00",
        "◜ agent-4 · 0:00",
        "+1 more",
    )


def test_an_agent_exiting_leaves_the_lines() -> None:
    lines = Lines()
    lines.feed(started("agent-2"), turn("agent-2"))
    lines.feed(AgentExited(agent_id="agent-2", reason="crashed"))
    assert lines.shown() == () and not lines.working.running


def test_each_tab_shows_only_its_own_running_agents() -> None:
    lines = Lines()
    lines.feed(started("agent-1", "planner"), started("agent-2"))
    lines.feed(turn("agent-1"), turn("agent-2"))
    assert lines.shown() == ("◜ planner · 0:00", "◜ agent-2 · 0:00")
    lines.working.show_tab("coder")
    assert lines.shown() == ("◜ agent-2 · 0:00",)
    lines.working.show_tab("planner")
    assert lines.shown() == ("◜ planner · 0:00",)
    lines.working.show_tab("reviewer")
    assert lines.shown() == () and lines.row.shows[-1] == ()
    assert lines.working.running  # Others still run.


def test_a_long_call_is_clipped_to_the_row_and_refit_to_a_new_width() -> None:
    lines = Lines(76)
    lines.feed(started("agent-2"), turn("agent-2"), read("agent-2", "c", "x" * 200))
    assert len(lines.shown()[0]) == 76
    lines.row.width = 96
    lines.working.tick()
    assert len(lines.shown()[0]) == 96


def test_markup_in_a_call_is_kept_as_it_is() -> None:
    lines = Lines()
    command = "echo [/] [red]x[/red]"
    call = ToolCallStarted(
        turn_id="t", agent_id="agent-2", call_id="c1", name="Bash", arguments={"command": command}
    )
    lines.feed(started("agent-2"), turn("agent-2"), call)
    assert lines.shown() == (f"◜ agent-2 · Bash {command} 0:00 · 0:00",)


def test_a_cancelled_planner_turn_leaves_the_lines() -> None:
    lines = Lines()
    lines.feed(started("agent-1", "planner"), turn("agent-1"), read("agent-1", "c1", "a.py"))
    assert lines.shown() == ("◜ planner · Read a.py 0:00 · 0:00",)
    lines.feed(TurnFinished(turn_id="t", agent_id="agent-1", reason="cancelled"))
    assert lines.shown() == ()


def planning(agent: str, busy: bool) -> Snapshot:
    """A snapshot of the old session, its planner ``agent`` live and mid-turn if ``busy``."""
    planner = RunningAgent(agent_id=agent, role="planner", task_id="")
    return snapshot_of(OLD).model_copy(update={"agents": (planner,), "busy": {PLANNER_LANE: busy}})


def test_a_snapshot_replaces_who_runs_with_none_mid_turn() -> None:
    lines = Lines()
    lines.feed(started("agent-2"), turn("agent-2"), read("agent-2", "c1", "a"))
    lines.working.track(snapshot_of(OLD), {})
    assert lines.shown() == () and not lines.working.running


def test_a_snapshot_with_an_idle_planner_shows_no_planner_line() -> None:
    lines = Lines()
    lines.working.track(planning("agent-1", busy=False), {"agent-1": "planner"})
    assert lines.shown() == () and not lines.working.running


def test_a_snapshot_with_a_busy_planner_shows_it_as_the_planner_until_its_turn_ends() -> None:
    lines = Lines()
    roles = {"agent-7": "planner"}
    lines.working.track(planning("agent-7", busy=True), roles)
    assert lines.shown() == ("◜ planner · 0:00",)
    lines.working.show_tab("planner")
    assert lines.shown() == ("◜ planner · 0:00",) and lines.working.running
    lines.working.track(finished("agent-7"), roles)
    assert lines.shown() == () and not lines.working.running
