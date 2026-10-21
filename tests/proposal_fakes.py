"""Fakes for proposing checks: scripted configurators, reviews of their proposals and the
steps they propose."""

from typing import Any

from orbit.agent.application.ports import ProposalDecision
from orbit.agent.domain.checks import ChecksProposal
from orbit.protocol import TaskSpec, TurnEndReason
from tests.agent_fakes import Call

LINT = {"name": "Lint", "command": ["ruff", "check"]}


TESTS = {"name": "Tests", "command": ["pytest"]}


SETUP = {"command": ["uv", "sync"], "watch": ["uv.lock"]}


CONFIGURE = TaskSpec(prompt="Work out this project's checks.", role="configurator")


class Review:
    """Decides on proposals as scripted, recording what it was shown."""

    def __init__(self, *decisions: ProposalDecision) -> None:
        self.decisions = list(decisions)
        self.proposals: list[tuple[str, ChecksProposal]] = []

    async def review(self, task_id: str, proposal: ChecksProposal) -> ProposalDecision:
        self.proposals.append((task_id, proposal))
        return self.decisions.pop(0)


def proposes(*arguments: dict[str, Any], replies: list[str]) -> Any:
    """A configurator that proposes each of ``arguments`` in turn, recording orbit's replies."""

    async def script(call: Call) -> TurnEndReason:
        await call("get_task")
        for proposal in arguments:
            reply = await call("propose_checks", **proposal)
            replies.append(reply.text)
        return "completed"

    return script
