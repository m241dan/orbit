from orbit.protocol import (
    STARTUP_TASK,
    AgentExited,
    AgentReply,
    AgentRequest,
    AgentStarted,
    CancelAgent,
    ReviewStatus,
    TasksChanged,
    TaskSpec,
    TaskState,
)
from orbit.tui import OrbitApp
from orbit.tui.agents import AgentsPane
from tests.test_task_list import feed
from tests.test_tui import FakeSession


def rows(pane: AgentsPane) -> list[str]:
    return [str(pane.get_option_at_index(i).prompt) for i in range(pane.option_count)]


async def test_the_pane_lists_running_agents_and_the_review_status() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        pane = app.query_one(AgentsPane)
        assert rows(pane) == ["No agents running"]
        board = TasksChanged(
            tasks=(
                TaskState(id="7", task=TaskSpec(prompt="code")),
                TaskState(id="8", task=TaskSpec(prompt="review", role="reviewer")),
            )
        )
        await feed(
            session,
            pilot,
            board,
            AgentStarted(agent_id="agent-1", role="coder", task_id="7"),
            AgentStarted(agent_id="agent-2", role="reviewer", task_id="8"),
            AgentRequest(agent_id="agent-2", request_id="r", tool="complete_task", arguments={}),
            ReviewStatus(paused=False, open_batches=1),
        )
        assert rows(pane) == [
            "agent-1 coder · task 1 · working",
            "agent-2 reviewer · task 2 · waiting on complete_task",
        ]
        assert pane.border_subtitle == "review: active · 1 open"
        await feed(
            session,
            pilot,
            AgentReply(agent_id="agent-2", request_id="r", text="ok"),
            AgentExited(agent_id="agent-1", reason="finished"),
        )
        assert rows(pane) == ["agent-2 reviewer · task 2 · working"]


async def test_c_on_the_pane_cancels_the_highlighted_agent() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("c")  # Not on the pane: nothing happens.
        await feed(
            session,
            pilot,
            AgentStarted(agent_id="agent-1", role="coder", task_id="7"),
            AgentStarted(agent_id="agent-2", role="reviewer", task_id="8"),
        )
        await pilot.press("l", "j", "enter", "j", "c", "escape")
        assert session.commands == [CancelAgent(agent_id="agent-2")]


async def test_c_with_no_agents_cancels_nothing() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("l", "j", "enter", "c")
        assert session.commands == []


async def test_the_highlight_stays_in_range_as_agents_exit() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(
            session,
            pilot,
            AgentStarted(agent_id="agent-1", role="coder", task_id="7"),
            AgentStarted(agent_id="agent-2", role="reviewer", task_id="8"),
        )
        await pilot.press("l", "j", "enter", "j")
        pane = app.query_one(AgentsPane)
        assert pane.highlighted == 1
        await feed(session, pilot, AgentExited(agent_id="agent-2", reason="finished"))
        assert pane.highlighted == 0


async def test_the_startup_check_is_listed_against_the_base_commit() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(
            session,
            pilot,
            AgentStarted(agent_id="startup-check", role="checker", task_id=STARTUP_TASK),
        )
        assert rows(app.query_one(AgentsPane)) == ["startup-check checker · base commit · working"]
        await pilot.press("l", "j", "enter", "c", "escape")
        assert session.commands == [CancelAgent(agent_id="startup-check")]
