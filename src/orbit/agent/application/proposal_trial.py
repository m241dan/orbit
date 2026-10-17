"""Trying the checks an agent proposed: run them in its tree, then have the user decide on them."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from orbit.agent.application.ports import ProposalDecision
from orbit.agent.application.verifier import Failure
from orbit.agent.domain.agent_task import AgentTask
from orbit.agent.domain.roles import ChecksProposal, setup_step
from orbit.agent.domain.verification import VerificationStep

RunSteps = Callable[[Sequence[VerificationStep]], Awaitable[Failure | None]]
"""Runs steps in order in the agent's tree until one fails; returns that failure."""

Ask = Callable[[ChecksProposal], Awaitable[ProposalDecision]]
"""Shows the user checks that passed; returns their decision."""


@dataclass(frozen=True)
class Verdict:
    """What to reply to the agent about its proposal, and the checks if the user accepted them."""

    reply: str
    accepted: ChecksProposal | None = None


class ProposalTrial:
    """Tries the proposals of an agent whose work is ``work``: ``run_steps`` runs their steps,
    ``ask`` has the user decide on them, and ``exited`` finishes when the agent's process ends."""

    def __init__(
        self, work: AgentTask, run_steps: RunSteps, ask: Ask, exited: "asyncio.Future[Any]"
    ) -> None:
        self._work = work
        self._run_steps = run_steps
        self._ask = ask
        self._exited = exited

    async def decide(self, checks: ChecksProposal) -> Verdict:
        """Try ``checks`` and, if they pass, have the user decide on them; returns the verdict."""
        failure = await self._try(checks)
        if failure is not None:
            return Verdict(self._work.proposal_failed(*failure))
        decision = await self._review(checks)
        if decision is None:
            return Verdict(self._work.ended())
        if decision.action == "accept":
            return Verdict(self._work.accepted(), checks)
        if decision.action == "revise":
            return Verdict(self._work.revise(decision.feedback))
        return Verdict(self._work.ended())

    async def _review(self, checks: ChecksProposal) -> ProposalDecision | None:
        """The user's decision on ``checks``; ``None`` if the agent exits before they decide."""
        decision = asyncio.ensure_future(self._ask(checks))
        try:
            await asyncio.wait({decision, self._exited}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            decision.cancel()
        return decision.result() if decision.done() else None

    async def _try(self, checks: ChecksProposal) -> Failure | None:
        """Run ``checks``' setup, then each step; returns the first failure."""
        steps = checks.steps
        if checks.setup is not None:
            steps = (setup_step(checks.setup), *steps)
        return await self._run_steps(steps)
