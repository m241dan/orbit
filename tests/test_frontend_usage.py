"""Usage in every window, without Textual: the transcript's corner as tabs are shown, and the
breakdown by role that u opens on a conversational role's tab."""

from orbit.protocol import (
    AgentEntry,
    AgentStarted,
    Catalog,
    TasksChanged,
    TaskSpec,
    TaskState,
    Usage,
    WorkspaceEntry,
)
from orbit.tui.view.opening import Opening
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


CODER_SPENT = SNAPSHOT.model_copy(
    update={
        "role_spent": {"coder": Usage(cost_usd=0.80)},
        "caused_spent": {"coder": {"experimenter": Usage(cost_usd=0.20)}},
        "agents": (),
    }
)


def coder_catalog(conversational: bool) -> Catalog:
    """A catalog listing the coder, ``conversational`` in its own lane or not."""
    coder = AgentEntry(name="coder", label="coder", conversational=conversational, lane="coder")
    workspace = WorkspaceEntry(id="a", label="a", path="/src/a", sessions=(), agents=(coder,))
    return Catalog(workspaces=(workspace,), agents=(coder,), triggers=(), lanes=())


async def test_a_pinned_task_roles_tab_shows_its_own_total() -> None:
    shell = Shell()
    await shell.feed(SPENT)
    await shell.click_tab("coder")
    assert shell.window.corner == ""
    await shell.feed(CODER_SPENT)
    assert shell.window.corner == "$0.80"


async def test_the_corner_and_u_follow_a_catalog_changing_the_shown_tabs_lane() -> None:
    shell = Shell(Opening(repository="/src/a"))
    await shell.feed(coder_catalog(False), CODER_SPENT)
    await shell.click_tab("coder")
    assert shell.window.corner == "$0.80"
    await shell.feed(coder_catalog(True))
    assert shell.window.corner == "coder $0.80 · via experimenter $0.20 · $1.00"
    shell.frontend.show_spending()
    assert len(shell.app.spendings) == 1
    await shell.feed(coder_catalog(False))
    assert shell.window.corner == "$0.80"
    shell.frontend.show_spending()
    assert len(shell.app.spendings) == 1
