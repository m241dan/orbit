"""Accounting for what the agents' turns cost: each turn that ends with usage adds it to what the
open session spent, in all, by role and, for a turn answering a consultation, under the role
that asked, and has it recorded."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from orbit.agent.application.ports import AgentLaunch, Publish
from orbit.agent.domain.role_spend import RoleSpend
from orbit.protocol import Event, TurnFinished, Usage


class Spending(Protocol):
    """What the open session spent, in all (``spent``) and by role (``spend``), as the switcher
    holds it."""

    spent: Usage
    spend: RoleSpend


class Launches(Protocol):
    """The agent desk, as accounting asks how an agent was launched."""

    def launched(self, agent_id: str | None) -> AgentLaunch | None: ...


class Recording(Protocol):
    """What records each turn's usage, as a ``UsageRecorder`` does."""

    def record(
        self, workspace_id: str, session_id: str, launch: AgentLaunch, usage: Usage
    ) -> None: ...


@dataclass(frozen=True)
class AccountParts:
    """What accounting reads and changes: what the session spent, held by ``spending``; the
    agent ``desk``, if there is one, saying how each agent was launched; ``asker``, the role
    whose consultation a role's turn running answers, if any; the ``recorder`` of each turn's
    usage in the session ``session`` says is open in the workspace ``workspace_id``; ``publish``,
    which publishes the agents' events; and ``changed``, which publishes the session's state."""

    spending: Spending
    desk: Launches | None
    asker: Callable[[str], str | None]
    recorder: Recording
    workspace_id: str
    session: Callable[[], str]
    publish: Publish
    changed: Callable[[], None]


class UsageAccounting:
    """Publishes the agents' events with the parts in ``parts``, charging what each turn that
    ends with usage cost to the open session."""

    def __init__(self, parts: AccountParts) -> None:
        self._parts = parts

    async def publish(self, event: Event) -> None:
        """Publish ``event``, from the agents; one that ends a turn adds what the turn cost to
        what the session spent, in all and by role, and records it in the background, then
        publishes the session's state, its spend by role changed. Publishing never suspends,
        so a turn is not cancelled between its TurnFinished and its own bookkeeping, which counts
        it towards its task after it."""
        if isinstance(event, TurnFinished) and event.usage is not None:
            self._charge(event.agent_id, event.usage)
        await self._parts.publish(event)
        if isinstance(event, TurnFinished) and event.usage is not None:
            self._parts.changed()

    def _charge(self, agent_id: str | None, usage: Usage) -> None:
        """Add ``usage``, what a turn of agent ``agent_id`` cost, to what the session spent and
        record it under its role and, for a turn answering a consultation, under what its asker
        caused, unless that agent wasn't launched."""
        parts = self._parts
        spending = parts.spending
        spending.spent += usage
        assert parts.desk is not None, "Only agents publish turns."
        launch = parts.desk.launched(agent_id)
        if launch is None:
            return
        parts.recorder.record(parts.workspace_id, parts.session(), launch, usage)
        role = launch.agent.role
        spending.spend = spending.spend.charged(role, usage, parts.asker(role))
