"""The configurator at work: orbit tries the checks it proposes, then the user decides."""

import asyncio
from typing import Any

from orbit.agent.application.ports import ProposalDecision
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.roles import ChecksProposal, Setup
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import CancelTurn, Event, TaskSpec, TurnEndReason
from tests.agent_fakes import Call, FakeTree, Steps, board, run, session_for, verified

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


def status(events: list[Event]) -> str:
    [task] = board(events)
    return task.status


def proposes(*arguments: dict[str, Any], replies: list[str]) -> Any:
    """A configurator that proposes each of ``arguments`` in turn, recording orbit's replies."""

    async def script(call: Call) -> TurnEndReason:
        await call("get_task")
        for proposal in arguments:
            reply = await call("propose_checks", **proposal)
            replies.append(reply.text)
        return "completed"

    return script


async def configure(
    *arguments: dict[str, Any], review: Review | None, steps: Steps, **options: Any
) -> tuple[list[Event], list[str]]:
    replies: list[str] = []
    session, _, _ = session_for(
        proposes(*arguments, replies=replies),
        verification=verified(steps),
        proposal_review=review,
        **options,
    )
    events = await asyncio.wait_for(run(session, CONFIGURE), 5)
    return events, replies


async def test_an_accepted_proposal_runs_setup_then_each_step_in_the_tree_and_completes() -> None:
    review = Review(ProposalDecision("accept"))
    steps = Steps(True, True, True)
    events, replies = await configure(
        {"steps": [LINT, TESTS], "setup": SETUP}, review=review, steps=steps, tree=FakeTree()
    )
    assert steps.runs == ["setup", "Lint", "Tests"]
    assert steps.roots == [FakeTree.path] * 3
    [(_, proposal)] = review.proposals
    assert proposal == ChecksProposal(
        steps=(
            VerificationStep("Lint", ("ruff", "check")),
            VerificationStep("Tests", ("pytest",)),
        ),
        setup=Setup(command=("uv", "sync"), watch=("uv.lock",)),
    )
    assert "accepted" in replies[0] and "End your turn now." in replies[0]
    assert status(events) == "completed"


async def test_a_failing_step_is_reported_for_revision_without_asking_the_user() -> None:
    review = Review(ProposalDecision("accept"))
    steps = Steps(True, False, True)
    events, replies = await configure(
        {"steps": [LINT, TESTS]}, {"steps": [LINT]}, review=review, steps=steps
    )
    assert steps.runs == ["Lint", "Tests", "Lint"]
    failure = replies[0]
    assert "'Tests'" in failure and "pytest" in failure and "1 test failed" in failure
    assert "propose_checks again" in failure and "(Revision 1 of 5.)" in failure
    assert [proposal.steps for _, proposal in review.proposals] == [
        (VerificationStep("Lint", ("ruff", "check")),)
    ]
    assert status(events) == "completed"


async def test_a_failing_setup_is_reported_by_name() -> None:
    steps = Steps(False)
    _, replies = await configure(
        {"steps": [LINT], "setup": SETUP}, review=Review(), steps=steps, limits=Limits(1)
    )
    assert steps.runs == ["setup"]
    assert "'setup'" in replies[0] and "uv sync" in replies[0]


async def test_the_user_can_ask_for_changes_which_quotes_their_feedback() -> None:
    review = Review(ProposalDecision("revise", "Add mypy."), ProposalDecision("accept"))
    events, replies = await configure(
        {"steps": [LINT]}, {"steps": [LINT, TESTS]}, review=review, steps=Steps(True, True, True)
    )
    assert "> Add mypy." in replies[0] and "propose_checks again" in replies[0]
    assert "(Revision 1 of 5.)" in replies[0]
    assert len(review.proposals) == 2
    assert status(events) == "completed"


async def test_the_user_can_end_it_which_cancels_the_task() -> None:
    events, replies = await configure(
        {"steps": [LINT]}, review=Review(ProposalDecision("end")), steps=Steps(True)
    )
    assert "ended" in replies[0] and "End your turn now." in replies[0]
    assert status(events) == "cancelled"


async def test_revisions_are_capped_and_the_reply_says_so() -> None:
    review = Review(ProposalDecision("revise", "Faster, please."))
    events, replies = await configure(
        {"steps": [LINT]},
        {"steps": [LINT]},
        review=review,
        steps=Steps(False, True),
        limits=Limits(max_attempts=1),
    )
    assert "(Revision 1 of 1.)" in replies[0]
    assert "> Faster, please." in replies[1]
    assert "all 1 revisions" in replies[1] and "End your turn now." in replies[1]
    assert status(events) == "unverified"


async def test_invalid_proposals_are_refused_with_the_reason() -> None:
    steps = Steps()
    invalid: list[dict[str, Any]] = [
        {"steps": []},
        {"steps": [{"name": "", "command": ["x"]}]},
        {"steps": [{"name": "Lint", "command": []}]},
        {"steps": [LINT], "setup": {"command": ["make"], "watch": ["/etc/passwd"]}},
        {"steps": [LINT], "colour": "blue"},
    ]

    async def script(call: Call) -> TurnEndReason:
        await call("get_task")
        for arguments in invalid:
            reply = await call("propose_checks", **arguments)
            assert reply.is_error and reply.text.startswith("Invalid proposal: ")
        await call("give_up", reason="Can't propose anything valid.")
        return "completed"

    session, _, _ = session_for(script, verification=verified(steps), proposal_review=Review())
    events = await asyncio.wait_for(run(session, CONFIGURE), 5)
    assert steps.runs == []
    assert status(events) == "gave_up"


async def test_proposals_before_get_task_or_without_a_reviewer_are_refused() -> None:
    async def early(call: Call) -> TurnEndReason:
        reply = await call("propose_checks", steps=[LINT])
        assert reply.is_error and "get_task" in reply.text
        await call("get_task")
        await call("give_up", reason="stop")
        return "completed"

    async def unreviewed(call: Call) -> TurnEndReason:
        await call("get_task")
        reply = await call("propose_checks", steps=[LINT])
        assert reply.is_error and "proposals" in reply.text
        await call("give_up", reason="stop")
        return "completed"

    session, _, _ = session_for(early, verification=verified(Steps()), proposal_review=Review())
    await asyncio.wait_for(run(session, CONFIGURE), 5)
    session, _, _ = session_for(unreviewed, verification=verified(Steps()))
    await asyncio.wait_for(run(session, CONFIGURE), 5)
    session, _, _ = session_for(unreviewed, proposal_review=Review())
    await asyncio.wait_for(run(session, CONFIGURE), 5)


async def test_a_task_cancelled_while_its_proposal_runs_answers_the_agent() -> None:
    steps = Steps(True)
    steps.gate = asyncio.Event()
    replies: list[str] = []
    session, _, _ = session_for(
        proposes({"steps": [LINT]}, replies=replies),
        verification=verified(steps),
        proposal_review=Review(),
    )
    running = asyncio.create_task(run(session, CONFIGURE))
    while not steps.runs:
        await asyncio.sleep(0.01)
    await session.send(CancelTurn())
    events = await asyncio.wait_for(running, 5)
    assert status(events) == "cancelled"
