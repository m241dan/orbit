"""The working row's state and lines: who runs, which calls, and how each line reads."""

from rich.cells import cell_len

from orbit.protocol import (
    PLANNER_LANE,
    AgentExited,
    Event,
    Notice,
    RunningAgent,
    SessionSummary,
    Settings,
    Snapshot,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from orbit.tui.view.working import (
    RunningCall,
    Working,
    WorkingAgent,
    WorkingView,
    elapsed_clock,
    track_working,
    working_label,
    working_line,
    working_lines,
)

ROLES = {"agent-1": "planner", "agent-2": "coder", "agent-3": "coder"}
WIDE = 200


def tracked(*events: tuple[Event, float]) -> Working:
    working: Working = ()
    for event, now in events:
        working = track_working(working, event, now)
    return working


def turn(agent: str | None) -> TurnStarted:
    return TurnStarted(turn_id="t", agent_id=agent)


def finished(agent: str | None) -> TurnFinished:
    return TurnFinished(turn_id="t", agent_id=agent, reason="completed")


def bash(agent: str | None, call_id: str, command: str = "uv run pytest") -> ToolCallStarted:
    return ToolCallStarted(
        turn_id="t", agent_id=agent, call_id=call_id, name="Bash", arguments={"command": command}
    )


def done(agent: str | None, call_id: str) -> ToolCallFinished:
    return ToolCallFinished(turn_id="t", agent_id=agent, call_id=call_id, output="")


def snapshot(*agents: str, planner: str | None = None, planning: bool = False) -> Snapshot:
    """A snapshot of a session with coders ``agents`` running, and its ``planner`` agent live,
    mid-turn if ``planning``."""
    coders = tuple(RunningAgent(agent_id=agent, role="coder", task_id="1") for agent in agents)
    planners = (
        () if planner is None else (RunningAgent(agent_id=planner, role="planner", task_id=""),)
    )
    return Snapshot(
        session=SessionSummary(id="s", name="s"),
        settings=Settings(),
        model_choices=(),
        busy={PLANNER_LANE: planning},
        tasks=(),
        steps=(),
        review=None,
        proposals=(),
        agents=(*planners, *coders),
    )


def test_nobody_runs_before_a_turn_starts() -> None:
    assert tracked() == ()
    assert working_lines(WorkingView((), ROLES, None), 0, WIDE) == ()


def test_a_started_turn_runs_until_it_finishes() -> None:
    working = tracked((turn("agent-1"), 5))
    assert working == (WorkingAgent("agent-1", 5),)
    assert tracked((turn("agent-1"), 5), (finished("agent-1"), 9)) == ()


def test_an_agent_exiting_stops_its_turn() -> None:
    working = tracked((turn("agent-2"), 0), (bash("agent-2", "c1"), 1))
    assert track_working(working, AgentExited(agent_id="agent-2", reason="finished"), 2) == ()


def test_tool_calls_are_kept_until_they_finish() -> None:
    working = tracked((turn("agent-2"), 0), (bash("agent-2", "c1"), 1), (bash("agent-2", "c2"), 2))
    assert working[0].calls == (
        RunningCall("c1", "Bash uv run pytest", 1),
        RunningCall("c2", "Bash uv run pytest", 2),
    )
    assert track_working(working, done("agent-2", "c1"), 3)[0].calls == (
        RunningCall("c2", "Bash uv run pytest", 2),
    )


def test_calls_by_an_agent_not_running_are_ignored() -> None:
    working = tracked((turn("agent-2"), 0), (bash("agent-3", "c1"), 1))
    assert working == (WorkingAgent("agent-2", 0),)


def test_unfinished_calls_are_dropped_when_the_turn_ends() -> None:
    working = tracked((turn("agent-2"), 0), (bash("agent-2", "c1"), 1), (finished("agent-2"), 2))
    assert working == ()
    assert track_working(working, turn("agent-2"), 3) == (WorkingAgent("agent-2", 3),)


def test_a_restarted_turn_moves_to_the_end() -> None:
    working = tracked((turn("agent-2"), 0), (turn("agent-3"), 1), (turn("agent-2"), 2))
    assert [running.agent for running in working] == ["agent-3", "agent-2"]


def test_other_events_leave_the_working_state_alone() -> None:
    working = tracked((turn("agent-2"), 0))
    assert track_working(working, Notice(message="hi"), 1) is working


def test_the_planner_is_named_planner() -> None:
    assert working_label(None, ROLES) == "planner"
    assert working_label("agent-1", ROLES) == "planner"
    assert working_label("agent-2", ROLES) == "agent-2"


def test_clocks_read_minutes_and_seconds_then_hours() -> None:
    assert elapsed_clock(0) == "0:00"
    assert elapsed_clock(12.9) == "0:12"
    assert elapsed_clock(220) == "3:40"
    assert elapsed_clock(3600) == "1:00:00"
    assert elapsed_clock(3725) == "1:02:05"


def test_a_line_without_calls_shows_the_turn_time() -> None:
    assert working_line(WorkingAgent("agent-3", 2), "agent-3", 222, WIDE) == "◜ agent-3 · 3:40"


def test_a_line_shows_the_latest_call_and_its_time() -> None:
    running = WorkingAgent("agent-3", 2, (RunningCall("c1", "Bash uv run pytest", 210),))
    assert (
        working_line(running, "agent-3", 222, WIDE) == "◜ agent-3 · Bash uv run pytest 0:12 · 3:40"
    )


def test_a_line_counts_the_other_running_calls() -> None:
    calls = (
        RunningCall("c1", "Bash ls", 0),
        RunningCall("c2", "Bash pwd", 0),
        RunningCall("c3", "Read src/x.py", 212),
    )
    line = working_line(WorkingAgent("agent-3", 2, calls), "agent-3", 222, WIDE)
    assert line == "◜ agent-3 · Read src/x.py (+2 running) 0:10 · 3:40"


def test_a_long_call_is_clipped_to_fit_the_width() -> None:
    running = WorkingAgent("agent-3", 0, (RunningCall("c1", "Bash " + "x" * 100, 0),))
    line = working_line(running, "agent-3", 0, 40)
    assert len(line) == 40
    assert line == "◜ agent-3 · Bash xxxxxxxxxx… 0:00 · 0:00"


def test_a_call_keeps_at_least_its_ellipsis_in_a_narrow_row() -> None:
    running = WorkingAgent("agent-3", 0, (RunningCall("c1", "Bash ls", 0),))
    assert working_line(running, "agent-3", 0, 5) == "◜ agent-3 · … 0:00 · 0:00"


def test_lines_follow_turn_start_and_stop_at_three_then_sum_up() -> None:
    working = tracked(*[(turn(f"agent-{n}"), n) for n in (4, 5, 6, 7)])
    lines = working_lines(WorkingView(working, {}, None), 12, WIDE)
    assert lines == (
        "◜ agent-4 · 0:08",
        "◜ agent-5 · 0:07",
        "◜ agent-6 · 0:06",
        "+1 more",
    )


def test_all_shows_every_running_agent_with_the_planner() -> None:
    working = tracked((turn("agent-1"), 0), (turn("agent-2"), 0), (turn("agent-9"), 0))
    lines = working_lines(WorkingView(working, ROLES, None), 0, WIDE)
    assert lines == ("◜ planner · 0:00", "◜ agent-2 · 0:00", "◜ agent-9 · 0:00")


def test_a_role_tab_shows_only_its_agents() -> None:
    working = tracked((turn("agent-1"), 0), (turn("agent-2"), 0), (turn("agent-3"), 0))
    lines = working_lines(WorkingView(working, ROLES, "coder"), 0, WIDE)
    assert lines == ("◜ agent-2 · 0:00", "◜ agent-3 · 0:00")


def test_the_planner_tab_shows_only_the_planner() -> None:
    working = tracked((turn("agent-1"), 0), (turn("agent-2"), 0))
    assert working_lines(WorkingView(working, ROLES, "planner"), 0, WIDE) == ("◜ planner · 0:00",)


def test_a_turn_with_no_agent_shows_under_all_only() -> None:
    working = tracked((turn(None), 0))
    assert working_lines(WorkingView(working, ROLES, None), 0, WIDE) == ("◜ planner · 0:00",)
    assert working_lines(WorkingView(working, ROLES, "planner"), 0, WIDE) == ()


def test_a_wide_call_is_clipped_to_fit_the_width_in_cells() -> None:
    running = WorkingAgent("agent-3", 0, (RunningCall("c1", "Bash " + "日" * 50, 0),))
    line = working_line(running, "agent-3", 0, 40)
    assert cell_len(line) == 40
    assert line == "◜ agent-3 · Bash 日日日日日… 0:00 · 0:00"


def test_a_wide_call_split_by_the_clip_is_padded_to_fit_the_width() -> None:
    running = WorkingAgent("agent-3", 0, (RunningCall("c1", "Bash " + "日" * 50, 0),))
    line = working_line(running, "agent-3", 0, 41)
    assert cell_len(line) == 41
    assert line == "◜ agent-3 · Bash 日日日日日 … 0:00 · 0:00"


def test_a_snapshot_without_running_agents_drops_every_turn_and_call() -> None:
    working = tracked((turn("agent-1"), 0), (turn("agent-2"), 1), (bash("agent-2", "c1"), 2))
    assert track_working(working, snapshot(), 3) == ()


def test_a_snapshot_runs_only_its_other_agents_timed_from_the_snapshot() -> None:
    working = tracked((turn("agent-2"), 0), (bash("agent-2", "c1"), 1), (turn("agent-1"), 2))
    assert track_working(working, snapshot("agent-2"), 7) == (WorkingAgent("agent-2", 7),)


def test_a_snapshot_with_an_idle_planner_leaves_it_out() -> None:
    working = tracked((turn("agent-1"), 0))
    after = track_working(working, snapshot("agent-2", planner="agent-1"), 7)
    assert after == (WorkingAgent("agent-2", 7),)


def test_a_snapshot_with_the_planner_lane_busy_runs_the_planner() -> None:
    after = track_working((), snapshot("agent-2", planner="agent-1", planning=True), 7)
    assert after == (WorkingAgent("agent-1", 7), WorkingAgent("agent-2", 7))
