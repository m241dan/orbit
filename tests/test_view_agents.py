from orbit.protocol import (
    AgentExited,
    AgentReply,
    AgentRequest,
    AgentStarted,
    Notice,
    OpenRequest,
    ReviewStatus,
    RunningAgent,
    SessionSummary,
    Settings,
    Snapshot,
)
from orbit.tui.view import (
    NO_AGENTS_LABEL,
    AgentRow,
    agent_line,
    review_line,
    track_agents,
)


def test_agents_are_tracked_from_their_events() -> None:
    rows: tuple[AgentRow, ...] = ()
    for event in (
        AgentStarted(agent_id="agent-1", role="coder", task_id="1"),
        AgentStarted(agent_id="agent-2", role="reviewer", task_id="2"),
        AgentRequest(agent_id="agent-1", request_id="r", tool="complete_task", arguments={}),
    ):
        rows = track_agents(rows, event)
    assert rows == (
        AgentRow("agent-1", "coder", "1", open_requests=(("r", "complete_task"),)),
        AgentRow("agent-2", "reviewer", "2"),
    )
    rows = track_agents(rows, AgentReply(agent_id="agent-1", request_id="r", text="ok"))
    assert rows[0].open_requests == ()
    rows = track_agents(rows, AgentExited(agent_id="agent-2", reason="finished"))
    assert [r.agent_id for r in rows] == ["agent-1"]
    assert track_agents(rows, Notice(message="x")) == rows


def test_a_snapshot_replaces_the_rows_with_its_agents_and_their_open_requests() -> None:
    rows = (AgentRow("agent-1", "coder", "1"),)
    request = OpenRequest(request_id="r", tool="get_task")
    snapshot = Snapshot(
        session=SessionSummary(id="s1", name="auth"),
        settings=Settings(),
        model_choices=(),
        busy={},
        tasks=(),
        steps=(),
        review=None,
        proposals=(),
        agents=(
            RunningAgent(
                agent_id="agent-2", role="reviewer", task_id="2", open_requests=(request,)
            ),
        ),
    )
    assert track_agents(rows, snapshot) == (
        AgentRow("agent-2", "reviewer", "2", open_requests=(("r", "get_task"),)),
    )


def test_an_agent_waits_until_every_open_request_is_answered() -> None:
    rows: tuple[AgentRow, ...] = ()
    for event in (
        AgentStarted(agent_id="agent-1", role="coder", task_id="1"),
        AgentRequest(agent_id="agent-1", request_id="a", tool="complete_task", arguments={}),
        AgentRequest(agent_id="agent-1", request_id="b", tool="report_progress", arguments={}),
        AgentReply(agent_id="agent-1", request_id="b", text="ok"),
    ):
        rows = track_agents(rows, event)
    assert agent_line(rows[0], "1") == "agent-1 coder · task 1 · waiting on complete_task"
    rows = track_agents(rows, AgentReply(agent_id="agent-1", request_id="a", text="ok"))
    assert agent_line(rows[0], "1") == "agent-1 coder · task 1 · working"


def test_an_agent_line_names_its_earliest_open_request() -> None:
    row = AgentRow("agent-1", "coder", "1", open_requests=(("a", "complete_task"), ("b", "x")))
    assert agent_line(row, "1") == "agent-1 coder · task 1 · waiting on complete_task"


def test_agent_lines_say_what_each_is_doing() -> None:
    assert agent_line(AgentRow("agent-1", "coder", "4"), "2") == "agent-1 coder · task 2 · working"
    waiting = AgentRow("agent-3", "reviewer", "5", open_requests=(("r", "complete_task"),))
    assert agent_line(waiting, "3") == "agent-3 reviewer · task 3 · waiting on complete_task"
    planner = AgentRow("agent-4", "planner", "plan")
    assert agent_line(planner, None) == "agent-4 planner · planning · working"
    # A task that has left the list is named by its id, not taken for planning.
    gone = AgentRow("agent-5", "coder", "7")
    assert agent_line(gone, None) == "agent-5 coder · task 7 · working"


def test_the_review_line_says_whether_review_is_paused() -> None:
    assert review_line(None) == ""
    assert review_line(ReviewStatus(paused=False, open_batches=0)) == "review: active"
    assert review_line(ReviewStatus(paused=False, open_batches=2)) == "review: active · 2 open"
    assert review_line(ReviewStatus(paused=True, open_batches=0)) == "review: paused"


def test_an_empty_agents_pane_says_no_agents_run() -> None:
    assert NO_AGENTS_LABEL == "No agents running"


def test_a_repeated_start_leaves_the_rows_unchanged() -> None:
    started = AgentStarted(agent_id="agent-1", role="coder", task_id="1")
    rows = track_agents((), started)
    assert track_agents(rows, started) == rows


def test_a_repeated_request_leaves_the_rows_unchanged() -> None:
    request = AgentRequest(agent_id="agent-1", request_id="r", tool="get_task", arguments={})
    rows = track_agents((AgentRow("agent-1", "coder", "1"),), request)
    assert track_agents(rows, request) == rows
