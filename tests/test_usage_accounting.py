"""Accounting for what the agents' turns cost: each turn that ends with usage adds it to what the
open session spent, in all, by role and under the role whose consultation it answered, and is
recorded, before the session's state is published."""

from dataclasses import dataclass, field

from orbit.agent.application.ports import AgentLaunch
from orbit.agent.application.usage_accounting import AccountParts, UsageAccounting
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.role_spend import RoleSpend
from orbit.protocol import (
    CODER,
    EXPERIMENTER,
    PLANNER,
    Event,
    Notice,
    Settings,
    TurnFinished,
    Usage,
)

WORKSPACE = "workspace-1"
OPEN = "session-1"
COST = Usage(cost_usd=0.5)


def launch_of(agent_id: str, role: str) -> AgentLaunch:
    """How agent ``agent_id`` was launched in ``role``."""
    agent = Agent(id=agent_id, role=role, task_id="1", token="token")
    return AgentLaunch(agent, "", "", Settings(), ".", "", (), WORKSPACE)


def turn(agent_id: str | None, usage: Usage | None = COST) -> TurnFinished:
    """A turn of agent ``agent_id`` that ended costing ``usage``."""
    return TurnFinished(turn_id="turn-1", agent_id=agent_id, reason="completed", usage=usage)


@dataclass
class Books:
    """What the open session spent, the agents launched, the role each consulted role's turn
    answers, and what accounting recorded, published and said changed, in order."""

    spent: Usage = field(default_factory=Usage)
    spend: RoleSpend = field(default_factory=RoleSpend)
    launches: dict[str, AgentLaunch] = field(default_factory=dict)
    askers: dict[str, str] = field(default_factory=dict)
    log: list[object] = field(default_factory=list)

    def launched(self, agent_id: str | None) -> AgentLaunch | None:
        return None if agent_id is None else self.launches.get(agent_id)

    def asker(self, role: str) -> str | None:
        return self.askers.get(role)

    def record(self, workspace_id: str, session_id: str, launch: AgentLaunch, usage: Usage) -> None:
        self.log.append(("recorded", workspace_id, session_id, launch.agent.id, usage))

    async def publish(self, event: Event) -> None:
        self.log.append(event)

    def changed(self) -> None:
        self.log.append("changed")

    async def account(self, *events: Event) -> None:
        """Publish ``events`` from the agents, in order, through accounting."""
        parts = AccountParts(
            self, self, self.asker, self, WORKSPACE, lambda: OPEN, self.publish, self.changed
        )
        accounting = UsageAccounting(parts)
        for event in events:
            await accounting.publish(event)


async def test_a_turns_cost_is_charged_to_its_role_and_recorded_before_the_state_changes() -> None:
    books = Books(launches={"agent-1": launch_of("agent-1", CODER)})
    ended = turn("agent-1")
    await books.account(ended)
    assert (books.spent, books.spend) == (COST, RoleSpend({CODER: COST}))
    assert books.log == [("recorded", WORKSPACE, OPEN, "agent-1", COST), ended, "changed"]


async def test_turns_add_up() -> None:
    books = Books(launches={"agent-1": launch_of("agent-1", CODER)})
    await books.account(turn("agent-1"), turn("agent-1"))
    assert (books.spent, books.spend) == (COST + COST, RoleSpend({CODER: COST + COST}))


async def test_a_turn_answering_a_consultation_is_charged_to_its_asker_too() -> None:
    books = Books(
        launches={"agent-2": launch_of("agent-2", EXPERIMENTER)}, askers={EXPERIMENTER: PLANNER}
    )
    await books.account(turn("agent-2"))
    assert books.spend == RoleSpend({EXPERIMENTER: COST}, {PLANNER: {EXPERIMENTER: COST}})


async def test_an_agent_not_launched_counts_only_in_what_the_session_spent_in_all() -> None:
    books = Books()
    ended = turn("agent-9")
    await books.account(ended)
    assert (books.spent, books.spend, books.log) == (COST, RoleSpend(), [ended, "changed"])


async def test_other_events_and_turns_without_usage_are_only_published() -> None:
    books = Books(launches={"agent-1": launch_of("agent-1", CODER)})
    notice, unknown = Notice(message="Hello."), turn("agent-1", usage=None)
    await books.account(notice, unknown)
    assert (books.spent, books.spend, books.log) == (Usage(), RoleSpend(), [notice, unknown])
