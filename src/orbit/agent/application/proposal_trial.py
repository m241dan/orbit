"""Trying the checks an agent proposed: have the user decide on them, then run those they accept
in its tree."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from orbit.agent.application.ports import ProposalDecision
from orbit.agent.application.verifier import Failure
from orbit.agent.domain.agent_task import AgentTask
from orbit.agent.domain.checks import ChecksProposal, setup_step
from orbit.agent.domain.verification import VerificationStep
from orbit.execution.cancelling import cancel_and_wait

RunSteps = Callable[[Sequence[VerificationStep]], Awaitable[Failure | None]]
"""Runs steps in order in the agent's tree until one fails; returns that failure."""

Ask = Callable[[ChecksProposal], Awaitable[ProposalDecision]]
"""Shows the user proposed checks, none of which has run; returns their decision."""


@dataclass(frozen=True)
class Verdict:
    """What to reply to the agent about its proposal, the checks if the user accepted them and
    they passed, and how they failed if they did when tried."""

    reply: str
    accepted: ChecksProposal | None = None
    failure: Failure | None = None


class ProposalTrial:
    """Tries the proposals of an agent whose work is ``work``: ``run_steps`` runs their steps,
    ``ask`` has the user decide on them, and ``exited`` finishes when the agent's process ends."""

    def __init__(
        self, work: AgentTask, run_steps: RunSteps, ask: Ask, exited: asyncio.Future[Any]
    ) -> None:
        self._work = work
        self._run_steps = run_steps
        self._ask = ask
        self._exited = exited

    async def decide(self, checks: ChecksProposal) -> Verdict:
        """Have the user decide on ``checks`` and, if they accept them, try them; returns the
        verdict."""
        decision = await self._review(checks)
        if decision is None:
            return Verdict(self._work.ended())
        if decision.action == "revise":
            return Verdict(self._work.revise(decision.feedback))
        if decision.action == "end":
            return Verdict(self._work.ended())
        failure = await self._try(checks)
        if failure is not None:
            return Verdict(self._work.proposal_failed(*failure), failure=failure)
        return Verdict(self._work.accepted(), checks)

    async def _review(self, checks: ChecksProposal) -> ProposalDecision | None:
        """The user's decision on ``checks``; ``None`` if the agent exits before they decide, once
        asking them has stopped."""
        decision = asyncio.ensure_future(self._ask(checks))
        try:
            await asyncio.wait({decision, self._exited}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await cancel_and_wait(decision)
        return None if decision.cancelled() else decision.result()

    async def _try(self, checks: ChecksProposal) -> Failure | None:
        """Run the accepted ``checks``' setup, then each step; returns the first failure."""
        steps = checks.steps
        if checks.setup is not None:
            steps = (setup_step(checks.setup), *steps)
        return await self._run_steps(steps)
