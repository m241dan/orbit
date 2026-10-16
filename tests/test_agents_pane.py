import pytest
from textual.visual import VisualType

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
    TurnEndReason,
)
from orbit.tui import OrbitApp
from orbit.tui.agents import AgentsPane
from tests.agent_fakes import Call, session_for
from tests.test_task_list import board, feed
from tests.test_tui import FakeSession, active_tab, shown, tab_strip


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


async def test_the_highlight_stays_on_its_agent_as_one_above_it_exits() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(
            session,
            pilot,
            AgentStarted(agent_id="agent-1", role="coder", task_id="7"),
            AgentStarted(agent_id="agent-2", role="reviewer", task_id="8"),
            AgentStarted(agent_id="startup-check", role="checker", task_id=STARTUP_TASK),
        )
        await pilot.press("l", "j", "enter", "j")
        pane = app.query_one(AgentsPane)
        assert pane.highlighted == 1
        await feed(session, pilot, AgentExited(agent_id="agent-1", reason="finished"))
        assert pane.highlighted == 0
        assert rows(pane)[0].startswith("agent-2 ")


async def test_c_after_an_agent_above_exits_cancels_the_highlighted_agent() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(
            session,
            pilot,
            AgentStarted(agent_id="agent-1", role="coder", task_id="7"),
            AgentStarted(agent_id="agent-2", role="reviewer", task_id="8"),
            AgentStarted(agent_id="startup-check", role="checker", task_id=STARTUP_TASK),
        )
        await pilot.press("l", "j", "enter", "j")
        await feed(
            session,
            pilot,
            AgentExited(agent_id="agent-1", reason="finished"),
            AgentStarted(agent_id="agent-3", role="coder", task_id="9"),
        )
        await pilot.press("c", "escape")
        assert session.commands == [CancelAgent(agent_id="agent-2")]


async def test_c_after_a_reviewer_above_exits_cancels_the_reviewer_not_the_coder() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(
            session,
            pilot,
            AgentStarted(agent_id="agent-1", role="reviewer", task_id="7"),
            AgentStarted(agent_id="agent-2", role="reviewer", task_id="8"),
            AgentStarted(agent_id="agent-3", role="coder", task_id="9"),
        )
        await pilot.press("l", "j", "enter", "j")
        await feed(session, pilot, AgentExited(agent_id="agent-1", reason="finished"))
        pane = app.query_one(AgentsPane)
        assert pane.highlighted == 0
        assert pane.rows[0].agent_id == "agent-2"
        await pilot.press("c", "escape")
        assert session.commands == [CancelAgent(agent_id="agent-2")]


async def test_the_highlight_moves_to_the_nearest_row_when_its_agent_exits() -> None:
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


async def test_a_real_planners_reply_shows_on_the_planner_tab() -> None:
    async def chats(call: Call) -> TurnEndReason:
        return "completed"

    session, _, launcher = session_for()
    launcher.planner_turns = [chats]
    launcher.planner_replies = ["Here's a plan."]
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("tab", "tab")  # Show another tab, so the reply marks the planner's.
        await pilot.press("i", *"plan it", "enter")
        for _ in range(50):
            await pilot.pause(0.02)
            if "planner *" in tab_strip(app):
                break
        assert tab_strip(app)[:3] == ["All", "planner *", "coder"]
        await pilot.press("escape", "ctrl+t")
        assert active_tab(app) == "planner"
        assert "agent-1: Here's a plan." in shown(app)
        assert "agent-1 planner · planning · working" in rows(app.query_one(AgentsPane))


async def test_the_pane_is_redrawn_only_when_its_agents_numbers_change() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        one, two = board("one", "two").tasks
        await feed(session, pilot, AgentStarted(agent_id="agent-1", role="coder", task_id="2"))
        await feed(session, pilot, TasksChanged(tasks=(one, two)))
        pane = app.query_one(AgentsPane)
        assert rows(pane) == ["agent-1 coder · task 2 · working"]
        before = pane.get_option_at_index(0)
        # A task added after agent-1's leaves its number alone.
        three = TaskState(id="3", task=TaskSpec(prompt="three"))
        await feed(session, pilot, TasksChanged(tasks=(one, two, three)))
        assert pane.get_option_at_index(0) is before
        # One added before it renumbers it.
        await feed(session, pilot, TasksChanged(tasks=(three, one, two)))
        assert rows(pane) == ["agent-1 coder · task 3 · working"]


def spy_rebuilds(pane: AgentsPane, monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """A list that grows by one each time ``pane`` clears its rows."""
    clears: list[None] = []
    clear = pane.clear_options

    def spy() -> AgentsPane:
        clears.append(None)
        return clear()

    monkeypatch.setattr(pane, "clear_options", spy)
    return clears


def spy_relabels(pane: AgentsPane, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The indexes of the rows ``pane`` relabels in place, as it relabels them."""
    relabelled: list[int] = []
    replace = pane.replace_prompt

    def spy(index: int, prompt: VisualType) -> None:
        relabelled.append(index)
        replace(index, prompt)

    monkeypatch.setattr(pane, "replace_prompt", spy)
    return relabelled


async def test_an_agent_starting_to_wait_relabels_only_its_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(
            session,
            pilot,
            AgentStarted(agent_id="agent-1", role="coder", task_id="7"),
            AgentStarted(agent_id="agent-2", role="reviewer", task_id="8"),
        )
        pane = app.query_one(AgentsPane)
        clears, relabelled = spy_rebuilds(pane, monkeypatch), spy_relabels(pane, monkeypatch)
        await feed(
            session,
            pilot,
            AgentRequest(agent_id="agent-2", request_id="r", tool="complete_task", arguments={}),
        )
        assert rows(pane) == [
            "agent-1 coder · task 7 · working",
            "agent-2 reviewer · task 8 · waiting on complete_task",
        ]
        await feed(session, pilot, AgentReply(agent_id="agent-2", request_id="r", text="ok"))
        assert rows(pane) == [
            "agent-1 coder · task 7 · working",
            "agent-2 reviewer · task 8 · working",
        ]
        assert (clears, relabelled) == ([], [1, 1])


async def test_renumbering_an_agents_task_relabels_its_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        one, two = board("one", "two").tasks
        await feed(
            session,
            pilot,
            AgentStarted(agent_id="agent-1", role="coder", task_id="1"),
            AgentStarted(agent_id="agent-2", role="coder", task_id="2"),
            TasksChanged(tasks=(one, two)),
        )
        pane = app.query_one(AgentsPane)
        clears, relabelled = spy_rebuilds(pane, monkeypatch), spy_relabels(pane, monkeypatch)
        three = TaskState(id="3", task=TaskSpec(prompt="three"))
        await feed(session, pilot, TasksChanged(tasks=(one, three, two)))
        assert rows(pane) == [
            "agent-1 coder · task 1 · working",
            "agent-2 coder · task 3 · working",
        ]
        assert (clears, relabelled) == ([], [1])


async def test_agents_coming_and_going_rebuild_the_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        pane = app.query_one(AgentsPane)
        clears, relabelled = spy_rebuilds(pane, monkeypatch), spy_relabels(pane, monkeypatch)
        await feed(session, pilot, AgentStarted(agent_id="agent-1", role="coder", task_id="7"))
        await feed(session, pilot, AgentStarted(agent_id="agent-2", role="coder", task_id="8"))
        await feed(session, pilot, AgentExited(agent_id="agent-1", reason="finished"))
        assert rows(pane) == ["agent-2 coder · task 8 · working"]
        await feed(session, pilot, AgentExited(agent_id="agent-2", reason="finished"))
        assert rows(pane) == ["No agents running"]
        assert (len(clears), relabelled) == (4, [])


async def test_the_highlight_stays_on_its_row_as_labels_change() -> None:
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
        await feed(
            session,
            pilot,
            AgentRequest(agent_id="agent-1", request_id="r", tool="complete_task", arguments={}),
        )
        assert pane.highlighted == 1
