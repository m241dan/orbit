"""Each turn an agent finishes with usage is recorded with its workspace, session, role and
model, in the background."""

from orbit.agent import WorkspaceHost
from orbit.agent.application.workspace_host import UNREGISTERED_WORKSPACE
from orbit.agent.domain.usage_totals import UsageRow
from orbit.protocol import (
    PLANNER,
    AddTask,
    RunTasks,
    SendMessage,
    Settings,
    TaskSpec,
    TurnFinished,
    Usage,
)
from tests.agent_fakes import (
    FakeLedger,
    chats,
    claims_done,
    desk_of,
    events_until,
    idle,
    publish_agents,
    send,
    session_for,
    settled,
    switcher_of,
)

CODER_USAGE = Usage(output_tokens=100, cache_read_input_tokens=7, cost_usd=1.0, turns=3)
PLANNER_USAGE = Usage(input_tokens=400, cache_creation_input_tokens=9, cost_usd=0.4, turns=1)


def recorded(row: UsageRow) -> tuple[str, str, str, str | None, Usage]:
    """What ``row`` says but its time."""
    return row.workspace_id, row.session_id, row.role, row.model, row.usage


async def coder_and_planner_turns(session: WorkspaceHost) -> None:
    """Run a task on ``sonnet`` and a planner turn in ``session``."""
    task = TaskSpec(prompt="Look.", settings=Settings(model="sonnet", tool_access="off"))
    await send(session, AddTask(task=task))
    await send(session, RunTasks())
    await events_until(session, idle())
    await send(session, SendMessage(agent=PLANNER, text="Plan it."))
    await events_until(session, idle("planner"))
    await settled()


async def test_a_coder_turn_and_a_planner_turn_are_recorded_as_rows() -> None:
    ledger = FakeLedger()
    session, _, launcher = session_for(claims_done, ledger=ledger, review=False)
    launcher.usages = [(CODER_USAGE,)]
    launcher.planner_turns = [chats]
    launcher.planner_usages = [PLANNER_USAGE]
    await coder_and_planner_turns(session)
    assert [recorded(row) for row in ledger.rows] == [
        (
            UNREGISTERED_WORKSPACE,
            "session-1",
            "coder",
            "sonnet",
            Usage(output_tokens=100, cache_read_input_tokens=7, cost_usd=1.0),
        ),
        (
            UNREGISTERED_WORKSPACE,
            "session-1",
            "planner",
            None,  # The backend's default, as the session and the planner's role set none.
            Usage(input_tokens=400, cache_creation_input_tokens=9, cost_usd=0.4),
        ),
    ]
    assert ledger.rows[0].at < ledger.rows[1].at
    await session.close()


async def test_a_turn_of_no_agent_launched_is_counted_but_not_recorded() -> None:
    ledger = FakeLedger()
    session, _, _ = session_for(ledger=ledger)
    for agent_id in ("agent-9", None):
        turn = TurnFinished(turn_id="t", agent_id=agent_id, reason="completed", usage=CODER_USAGE)
        await publish_agents(session, turn)
    await settled()
    assert ledger.rows == []
    assert switcher_of(session).spent == CODER_USAGE + CODER_USAGE
    assert desk_of(session) is not None
    await session.close()
