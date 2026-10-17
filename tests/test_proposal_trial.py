"""Trying a proposal of checks: run its steps, then have the user decide while the agent waits."""

import asyncio
from collections.abc import Sequence

import pytest

from orbit.agent.application.ports import ProposalDecision
from orbit.agent.application.proposal_trial import ProposalTrial, Verdict
from orbit.agent.application.verifier import Failure
from orbit.agent.domain.agent_task import AgentTask, Limits
from orbit.agent.domain.roles import ChecksProposal, Setup
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import TaskSpec

LINT = VerificationStep(name="Lint", command=("ruff", "check"))
TESTS = VerificationStep(name="Tests", command=("pytest",))
CHECKS = ChecksProposal(steps=(LINT, TESTS))


class Steps:
    """Runs steps, recording them, and fails the one named ``failing``."""

    def __init__(self, failing: str | None = None) -> None:
        self.failing = failing
        self.ran: list[Sequence[VerificationStep]] = []

    async def __call__(self, steps: Sequence[VerificationStep]) -> Failure | None:
        self.ran.append(steps)
        failed = [step for step in steps if step.name == self.failing]
        return (failed[0], "1 error") if failed else None


class User:
    """Decides as told, recording the proposals shown; with no decision, never decides."""

    def __init__(self, decision: ProposalDecision | None = None) -> None:
        self.decision = decision
        self.shown: list[ChecksProposal] = []
        self.cancelled = False

    async def __call__(self, checks: ChecksProposal) -> ProposalDecision:
        self.shown.append(checks)
        if self.decision is None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        assert self.decision is not None
        return self.decision


def proposing() -> AgentTask:
    work = AgentTask(TaskSpec(prompt="Work out the checks.", role="configurator"), Limits())
    work.get_task()
    assert work.propose() is None
    return work


def alive() -> "asyncio.Future[None]":
    """An agent's process that hasn't ended."""
    return asyncio.get_running_loop().create_future()


async def decide(
    steps: Steps, user: User, exited: "asyncio.Future[None] | None" = None
) -> tuple[Verdict, AgentTask]:
    work = proposing()
    return await ProposalTrial(work, steps, user, exited or alive()).decide(CHECKS), work


async def test_accepted_checks_end_the_task_and_come_back() -> None:
    verdict, work = await decide(Steps(), User(ProposalDecision("accept")))
    assert verdict.accepted == CHECKS
    assert verdict.reply.startswith("The user accepted your proposal.")
    assert work.phase == "done"


async def test_runs_the_proposed_steps_before_asking() -> None:
    steps, user = Steps(), User(ProposalDecision("accept"))
    await decide(steps, user)
    assert steps.ran == [(LINT, TESTS)]
    assert user.shown == [CHECKS]


async def test_runs_the_setup_first() -> None:
    steps = Steps()
    checks = CHECKS.model_copy(update={"setup": Setup(command=("uv", "sync"))})
    trial = ProposalTrial(proposing(), steps, User(ProposalDecision("accept")), alive())
    await trial.decide(checks)
    assert [step.name for step in steps.ran[0]] == ["setup", "Lint", "Tests"]


async def test_a_failing_step_asks_for_a_revision_without_asking_the_user() -> None:
    user = User(ProposalDecision("accept"))
    verdict, work = await decide(Steps(failing="Tests"), user)
    assert verdict.accepted is None
    assert "Your proposed step 'Tests' failed." in verdict.reply
    assert user.shown == []
    assert work.phase == "working"


async def test_a_revision_passes_on_the_users_feedback() -> None:
    verdict, work = await decide(Steps(), User(ProposalDecision("revise", "Add mypy.")))
    assert verdict.accepted is None
    assert "> Add mypy." in verdict.reply
    assert work.phase == "working"


async def test_the_user_can_end_the_task() -> None:
    verdict, work = await decide(Steps(), User(ProposalDecision("end")))
    assert verdict.accepted is None
    assert verdict.reply.startswith("The user ended this task.")
    assert work.phase == "ended"


async def test_the_agent_exiting_while_the_user_decides_ends_the_task() -> None:
    user = User()
    exited = alive()
    exited.set_result(None)
    verdict, work = await decide(Steps(), user, exited)
    assert verdict.accepted is None
    assert verdict.reply.startswith("The user ended this task.")
    assert work.phase == "ended"
    await asyncio.sleep(0)
    assert user.cancelled


async def test_cancelling_the_trial_stops_asking_the_user() -> None:
    user = User()
    trial = asyncio.ensure_future(decide(Steps(), user))
    while not user.shown:
        await asyncio.sleep(0)
    trial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await trial
    await asyncio.sleep(0)
    assert user.cancelled
