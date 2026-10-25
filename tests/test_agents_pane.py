"""The agents pane: the app listing its agents and cancelling one, and the pane alone keeping
its highlight and redrawing only the rows that change."""

import pytest
from textual import on
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.visual import VisualType

from orbit.protocol import (
    STARTUP_TASK,
    AgentExited,
    AgentReply,
    AgentRequest,
    AgentStarted,
    CancelAgent,
    Event,
    ReviewStatus,
    TasksChanged,
    TaskSpec,
    TaskState,
)
from orbit.tui import OrbitApp
from orbit.tui.agents import AgentsPane
from tests.agent_fakes import chats, frontend, session_for
from tests.tui_fakes import FakeSession, active_tab, feed_events, shown, tab_strip


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
        await feed_events(
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
        await feed_events(
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
        await feed_events(
            session,
            pilot,
            AgentStarted(agent_id="agent-1", role="coder", task_id="7"),
            AgentStarted(agent_id="agent-2", role="reviewer", task_id="8"),
        )
        await pilot.press("l", "j", "enter", "j", "c", "escape")
        assert session.commands == [CancelAgent(agent_id="agent-2")]


async def test_a_real_planners_reply_shows_on_the_planner_tab() -> None:
    session, _, launcher = session_for()
    launcher.planner_turns = [chats]
    launcher.planner_replies = ["Here's a plan."]
    app = OrbitApp(frontend(session))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press(
            "tab", "tab", "tab"
        )  # Show another tab, so the reply marks the planner's.
        await pilot.press("i", *"plan it", "enter")
        for _ in range(50):
            await pilot.pause(0.02)
            if "planner *" in tab_strip(app):
                break
        assert tab_strip(app)[:3] == ["All", "planner *", "experimenter"]
        await pilot.press("escape", "ctrl+t")
        assert active_tab(app) == "planner"
        assert "Here's a plan." in shown(app)
        assert "agent-1 planner · planning · working" in rows(app.query_one(AgentsPane))


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


class PaneApp(App[None]):
    """A bare app around an agents pane, recording the agents it asks to cancel."""

    def __init__(self) -> None:
        super().__init__()
        self.cancels: list[str] = []

    def compose(self) -> ComposeResult:
        yield AgentsPane(id="agents")

    @property
    def pane(self) -> AgentsPane:
        return self.query_one(AgentsPane)

    @on(AgentsPane.Cancel)
    def _cancel(self, message: AgentsPane.Cancel) -> None:
        self.cancels.append(message.agent_id)


async def follow(pilot: Pilot[None], pane: AgentsPane, *events: Event) -> None:
    """Have ``pane`` follow ``events``, and let it redraw."""
    for event in events:
        pane.track(event)
    await pilot.pause()


async def number(pilot: Pilot[None], pane: AgentsPane, numbers: dict[str, str]) -> None:
    """Have ``pane`` show its agents' tasks by ``numbers``, and let it redraw."""
    pane.number_tasks(numbers)
    await pilot.pause()


async def highlight_second(pilot: Pilot[None], pane: AgentsPane) -> None:
    """Activate ``pane`` and move its highlight to the second row."""
    pane.enter_list_mode()
    await pilot.press("j")


async def test_the_highlight_stays_on_its_agent_as_one_above_it_exits() -> None:
    app = PaneApp()
    async with app.run_test() as pilot:
        pane = app.pane
        await follow(
            pilot,
            pane,
            AgentStarted(agent_id="agent-1", role="coder", task_id="7"),
            AgentStarted(agent_id="agent-2", role="reviewer", task_id="8"),
            AgentStarted(agent_id="startup-check", role="checker", task_id=STARTUP_TASK),
        )
        await highlight_second(pilot, pane)
        assert pane.highlighted == 1
        await follow(pilot, pane, AgentExited(agent_id="agent-1", reason="finished"))
        assert pane.highlighted == 0
        assert rows(pane)[0].startswith("agent-2 ")


async def test_c_after_an_agent_above_exits_cancels_the_highlighted_agent() -> None:
    app = PaneApp()
    async with app.run_test() as pilot:
        pane = app.pane
        await follow(
            pilot,
            pane,
            AgentStarted(agent_id="agent-1", role="coder", task_id="7"),
            AgentStarted(agent_id="agent-2", role="reviewer", task_id="8"),
            AgentStarted(agent_id="startup-check", role="checker", task_id=STARTUP_TASK),
        )
        await highlight_second(pilot, pane)
        await follow(
            pilot,
            pane,
            AgentExited(agent_id="agent-1", reason="finished"),
            AgentStarted(agent_id="agent-3", role="coder", task_id="9"),
        )
        await pilot.press("c")
        assert app.cancels == ["agent-2"]


async def test_c_after_a_reviewer_above_exits_cancels_the_reviewer_not_the_coder() -> None:
    app = PaneApp()
    async with app.run_test() as pilot:
        pane = app.pane
        await follow(
            pilot,
            pane,
            AgentStarted(agent_id="agent-1", role="reviewer", task_id="7"),
            AgentStarted(agent_id="agent-2", role="reviewer", task_id="8"),
            AgentStarted(agent_id="agent-3", role="coder", task_id="9"),
        )
        await highlight_second(pilot, pane)
        await follow(pilot, pane, AgentExited(agent_id="agent-1", reason="finished"))
        assert pane.highlighted == 0
        assert pane.rows[0].agent_id == "agent-2"
        await pilot.press("c")
        assert app.cancels == ["agent-2"]


async def test_the_highlight_moves_to_the_nearest_row_when_its_agent_exits() -> None:
    app = PaneApp()
    async with app.run_test() as pilot:
        pane = app.pane
        await follow(
            pilot,
            pane,
            AgentStarted(agent_id="agent-1", role="coder", task_id="7"),
            AgentStarted(agent_id="agent-2", role="reviewer", task_id="8"),
        )
        await highlight_second(pilot, pane)
        assert pane.highlighted == 1
        await follow(pilot, pane, AgentExited(agent_id="agent-2", reason="finished"))
        assert pane.highlighted == 0


async def test_c_with_no_agents_cancels_nothing() -> None:
    app = PaneApp()
    async with app.run_test() as pilot:
        assert rows(app.pane) == ["No agents running"]
        app.pane.enter_list_mode()
        await pilot.press("c")
        assert app.cancels == []


async def test_the_pane_is_redrawn_only_when_its_agents_numbers_change() -> None:
    app = PaneApp()
    async with app.run_test() as pilot:
        pane = app.pane
        await follow(pilot, pane, AgentStarted(agent_id="agent-1", role="coder", task_id="2"))
        await number(pilot, pane, {"1": "1", "2": "2"})
        assert rows(pane) == ["agent-1 coder · task 2 · working"]
        before = pane.get_option_at_index(0)
        # A task added after agent-1's leaves its number alone.
        await number(pilot, pane, {"1": "1", "2": "2", "3": "3"})
        assert pane.get_option_at_index(0) is before
        # One added before it renumbers it.
        await number(pilot, pane, {"3": "1", "1": "2", "2": "3"})
        assert rows(pane) == ["agent-1 coder · task 3 · working"]


async def test_an_agent_starting_to_wait_relabels_only_its_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PaneApp()
    async with app.run_test() as pilot:
        pane = app.pane
        await follow(
            pilot,
            pane,
            AgentStarted(agent_id="agent-1", role="coder", task_id="7"),
            AgentStarted(agent_id="agent-2", role="reviewer", task_id="8"),
        )
        clears, relabelled = spy_rebuilds(pane, monkeypatch), spy_relabels(pane, monkeypatch)
        await follow(
            pilot,
            pane,
            AgentRequest(agent_id="agent-2", request_id="r", tool="complete_task", arguments={}),
        )
        assert rows(pane) == [
            "agent-1 coder · task 7 · working",
            "agent-2 reviewer · task 8 · waiting on complete_task",
        ]
        await follow(pilot, pane, AgentReply(agent_id="agent-2", request_id="r", text="ok"))
        assert rows(pane) == [
            "agent-1 coder · task 7 · working",
            "agent-2 reviewer · task 8 · working",
        ]
        assert (clears, relabelled) == ([], [1, 1])


async def test_renumbering_an_agents_task_relabels_its_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PaneApp()
    async with app.run_test() as pilot:
        pane = app.pane
        await follow(
            pilot,
            pane,
            AgentStarted(agent_id="agent-1", role="coder", task_id="1"),
            AgentStarted(agent_id="agent-2", role="coder", task_id="2"),
        )
        await number(pilot, pane, {"1": "1", "2": "2"})
        clears, relabelled = spy_rebuilds(pane, monkeypatch), spy_relabels(pane, monkeypatch)
        await number(pilot, pane, {"1": "1", "3": "2", "2": "3"})
        assert rows(pane) == [
            "agent-1 coder · task 1 · working",
            "agent-2 coder · task 3 · working",
        ]
        assert (clears, relabelled) == ([], [1])


async def test_agents_coming_and_going_rebuild_the_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PaneApp()
    async with app.run_test() as pilot:
        pane = app.pane
        clears, relabelled = spy_rebuilds(pane, monkeypatch), spy_relabels(pane, monkeypatch)
        await follow(pilot, pane, AgentStarted(agent_id="agent-1", role="coder", task_id="7"))
        await follow(pilot, pane, AgentStarted(agent_id="agent-2", role="coder", task_id="8"))
        await follow(pilot, pane, AgentExited(agent_id="agent-1", reason="finished"))
        assert rows(pane) == ["agent-2 coder · task 8 · working"]
        await follow(pilot, pane, AgentExited(agent_id="agent-2", reason="finished"))
        assert rows(pane) == ["No agents running"]
        assert (len(clears), relabelled) == (4, [])


async def test_the_highlight_stays_on_its_row_as_labels_change() -> None:
    app = PaneApp()
    async with app.run_test() as pilot:
        pane = app.pane
        await follow(
            pilot,
            pane,
            AgentStarted(agent_id="agent-1", role="coder", task_id="7"),
            AgentStarted(agent_id="agent-2", role="reviewer", task_id="8"),
        )
        await highlight_second(pilot, pane)
        await follow(
            pilot,
            pane,
            AgentRequest(agent_id="agent-1", request_id="r", tool="complete_task", arguments={}),
        )
        assert pane.highlighted == 1


async def test_a_new_agent_isnt_highlighted_from_the_empty_row() -> None:
    app = PaneApp()
    async with app.run_test() as pilot:
        pane = app.pane
        await follow(
            pilot,
            pane,
            AgentStarted(agent_id="agent-1", role="coder", task_id="7"),
            AgentStarted(agent_id="agent-2", role="reviewer", task_id="8"),
        )
        await highlight_second(pilot, pane)
        await follow(
            pilot,
            pane,
            AgentExited(agent_id="agent-1", reason="finished"),
            AgentExited(agent_id="agent-2", reason="finished"),
            AgentStarted(agent_id="agent-3", role="coder", task_id="9"),
        )
        assert pane.highlighted is None
        await pilot.press("c")
        assert app.cancels == []
