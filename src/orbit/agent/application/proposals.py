"""Where the frontend decides on proposed checks: each is shown as an event, and waits for the
command that decides it."""

import asyncio
from collections.abc import Callable

from orbit.agent.application.ports import ProposalDecision, Publish
from orbit.agent.domain.checks import ChecksProposal
from orbit.protocol import ChecksProposed, DecideProposal, ProposedSetup, ProposedStep


def proposed(agent_id: str, task_id: str, proposal: ChecksProposal) -> ChecksProposed:
    """``proposal`` as the event that shows it."""
    setup = proposal.setup
    return ChecksProposed(
        agent_id=agent_id,
        task_id=task_id,
        steps=tuple(ProposedStep(name=s.name, command=s.command) for s in proposal.steps),
        setup=None if setup is None else ProposedSetup(command=setup.command, watch=setup.watch),
    )


class FrontendReview:
    """A :class:`ProposalReview` that publishes each proposal and waits for the frontend's
    :class:`DecideProposal` on its task. ``agent_of`` names the agent working on a task."""

    def __init__(self, publish: Publish, agent_of: Callable[[str], str]) -> None:
        self._publish = publish
        self._agent_of = agent_of
        self._waiting: dict[str, asyncio.Future[ProposalDecision]] = {}

    async def review(self, task_id: str, proposal: ChecksProposal) -> ProposalDecision:
        decision: asyncio.Future[ProposalDecision] = asyncio.get_running_loop().create_future()
        self._waiting[task_id] = decision
        try:
            await self._publish(proposed(self._agent_of(task_id), task_id, proposal))
            return await decision
        finally:
            del self._waiting[task_id]

    def decide(self, command: DecideProposal) -> bool:
        """Settle the proposal ``command`` decides; whether one was waiting."""
        decision = self._waiting.get(command.task_id)
        if decision is None or decision.done():
            return False
        decision.set_result(ProposalDecision(command.action, command.feedback))
        return True
