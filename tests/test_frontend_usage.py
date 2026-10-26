"""Usage in every window, without Textual: the transcript's corner as tabs are shown, and the
breakdown by role that u opens on a conversational role's tab."""

from orbit.protocol import AgentStarted, TasksChanged, TaskSpec, TaskState, Usage
from orbit.tui.view.spend import spending_text
from tests.frontend_fakes import Shell
from tests.tui_fakes import SNAPSHOT

ROLE_SPENT = {"planner": Usage(cost_usd=0.42), "experimenter": Usage(cost_usd=1.10)}
CAUSED = {"planner": {"experimenter": Usage(cost_usd=1.10)}}
SPENT = SNAPSHOT.model_copy(update={"role_spent": ROLE_SPENT, "caused_spent": CAUSED, "agents": ()})


async def test_the_all_tab_shows_the_session_total_and_the_planners_its_spend() -> None:
    shell = Shell()
    await shell.feed(SPENT)
    assert shell.window.corner == "session $2.00"
    await shell.click_tab("● planner")
    assert shell.window.corner == "planner $0.42 · via experimenter $1.10 · $1.52"


async def test_a_task_agents_tab_shows_its_tasks_usage() -> None:
    shell = Shell()
    task = TaskState(id="1", task=TaskSpec(prompt="Docs."), usage=Usage(cost_usd=0.75))
    await shell.feed(
        SPENT, TasksChanged(tasks=(task,)), AgentStarted(agent_id="a9", role="docs", task_id="1")
    )
    await shell.click_tab("docs")
    assert shell.window.corner == "$0.75"


async def test_u_on_a_conversational_roles_tab_shows_the_breakdown() -> None:
    shell = Shell()
    await shell.feed(SPENT)
    shell.frontend.show_spending()
    assert shell.app.spendings == []
    await shell.click_tab("● planner")
    shell.frontend.show_spending()
    assert shell.app.spendings == [spending_text(ROLE_SPENT, CAUSED)]
