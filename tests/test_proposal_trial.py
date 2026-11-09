"""Trying a proposal of checks: have the user decide while the agent waits, then run its steps."""

import asyncio
from collections.abc import Sequence
from functools import partial

import pytest

from orbit.agent.application.ports import ProposalDecision
from orbit.agent.application.proposal_trial import ProposalTrial, Verdict
from orbit.agent.application.proposals import FrontendReview
from orbit.agent.application.verifier import Failure
from orbit.agent.domain.agent_task import AgentTask, Limits
from orbit.agent.domain.checks import ChecksProposal, Setup
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import Event, TaskSpec
from tests.waiting import eventually

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


async def shown(event: Event) -> None:
    """Publishes nothing anywhere."""


def proposing() -> AgentTask:
    work = AgentTask(TaskSpec(prompt="Work out the checks.", role="configurator"), Limits())
    work.get_task()
    assert work.propose() is None
    return work


def alive() -> asyncio.Future[None]:
    """An agent's process that hasn't ended."""
    return asyncio.get_running_loop().create_future()


async def decide(
    steps: Steps, user: User, exited: asyncio.Future[None] | None = None
) -> tuple[Verdict, AgentTask]:
    work = proposing()
    return await ProposalTrial(work, steps, user, exited or alive()).decide(CHECKS), work


async def test_accepted_checks_that_pass_end_the_task_and_come_back() -> None:
    verdict, work = await decide(Steps(), User(ProposalDecision("accept")))
    assert verdict.accepted == CHECKS
    assert verdict.reply.startswith("The user accepted your proposal.")
    assert verdict.failure is None
    assert work.phase == "done"


async def test_asks_the_user_before_running_any_step() -> None:
    steps = Steps()
    ran_when_asked: list[list[Sequence[VerificationStep]]] = []

    async def ask(checks: ChecksProposal) -> ProposalDecision:
        ran_when_asked.append(list(steps.ran))
        return ProposalDecision("accept")

    await ProposalTrial(proposing(), steps, ask, alive()).decide(CHECKS)
    assert ran_when_asked == [[]]
    assert steps.ran == [(LINT, TESTS)]


async def test_accepting_runs_the_setup_then_each_step_sets_included() -> None:
    steps = Steps()
    mutation = VerificationStep(name="Mutation", command=("mutmut", "run"), set="mutation")
    checks = ChecksProposal(steps=(LINT, TESTS, mutation), setup=Setup(command=("uv", "sync")))
    trial = ProposalTrial(proposing(), steps, User(ProposalDecision("accept")), alive())
    verdict = await trial.decide(checks)
    assert [step.name for step in steps.ran[0]] == ["setup", "Lint", "Tests", "Mutation"]
    assert len(steps.ran) == 1
    assert verdict.accepted == checks


async def test_an_accepted_proposal_with_a_failing_step_asks_for_a_revision() -> None:
    user = User(ProposalDecision("accept"))
    verdict, work = await decide(Steps(failing="Tests"), user)
    assert verdict.accepted is None
    assert "Your proposed step 'Tests' failed." in verdict.reply
    assert verdict.failure == (TESTS, "1 error")
    assert user.shown == [CHECKS]
    assert work.phase == "working"


async def test_a_revision_passes_on_the_users_feedback_without_running_anything() -> None:
    steps = Steps()
    verdict, work = await decide(steps, User(ProposalDecision("revise", "Add mypy.")))
    assert verdict.accepted is None
    assert "> Add mypy." in verdict.reply
    assert verdict.failure is None
    assert work.phase == "working"
    assert steps.ran == []


async def test_the_user_can_end_the_task_without_running_anything() -> None:
    steps = Steps()
    verdict, work = await decide(steps, User(ProposalDecision("end")))
    assert verdict.accepted is None
    assert verdict.reply.startswith("The user ended this task.")
    assert verdict.failure is None
    assert work.phase == "ended"
    assert steps.ran == []


async def test_the_agent_exiting_while_the_user_decides_ends_the_task() -> None:
    user, steps = User(), Steps()
    exited = alive()
    exited.set_result(None)
    verdict, work = await decide(steps, user, exited)
    assert verdict.accepted is None
    assert verdict.reply.startswith("The user ended this task.")
    assert work.phase == "ended"
    assert steps.ran == []
    await asyncio.sleep(0)
    assert user.cancelled


async def test_cancelling_the_trial_stops_asking_the_user() -> None:
    user = User()
    trial = asyncio.ensure_future(decide(Steps(), user))
    await eventually(lambda: user.shown, poll=0)
    trial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await trial
    await asyncio.sleep(0)
    assert user.cancelled


async def test_the_agent_exiting_withdraws_the_proposal_before_the_trial_returns() -> None:
    review = FrontendReview(publish=shown, agent_of=lambda task_id: "agent-1")
    exited = alive()
    exited.set_result(None)
    trial = ProposalTrial(proposing(), Steps(), partial(review.review, "t-1"), exited)

    await trial.decide(CHECKS)

    assert review.waiting == ()
