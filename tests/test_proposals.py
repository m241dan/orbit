"""The configurator at work: orbit tries the checks it proposes, then the user decides."""

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

from orbit.agent.application.ports import ProposalDecision
from orbit.agent.application.workspace_host import WorkspaceHost
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.checks import ChecksProposal, Setup
from orbit.agent.domain.endings import ENDED_AT_PROPOSAL, NO_PROPOSAL
from orbit.agent.domain.verification import VerificationStep, failure_report
from orbit.protocol import (
    UNKNOWN,
    AddTask,
    AgentExited,
    AgentReply,
    BusyChanged,
    CancelTurn,
    ChecksProposed,
    Command,
    CommandRefused,
    DecideProposal,
    ErrorOccurred,
    Event,
    ProposalAction,
    ProposalDecided,
    ProposedSetup,
    ProposedStep,
    RunTasks,
    TurnEndReason,
)
from tests.agent_fakes import (
    COMMAND_ID,
    Call,
    FakeStore,
    FakeTree,
    Steps,
    board,
    events_until,
    of_type,
    payloads,
    refused,
    run,
    send,
    session_for,
    verified,
)
from tests.proposal_fakes import CONFIGURE, LINT, SETUP, TESTS, Review, proposes
from tests.waiting import eventually


def status(events: list[Event]) -> str:
    [task] = board(events)
    return task.status


def why(events: list[Event]) -> tuple[str, str, str]:
    [task] = board(events)
    return task.status, task.reason, task.reason_detail


LINT_FAILED = (
    "unverified",
    "Proposed check Lint failed",
    failure_report(VerificationStep("Lint", ("ruff", "check")), "1 test failed"),
)


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
    [task] = board(events)
    assert (task.status, task.reason, task.reason_detail) == ("cancelled", ENDED_AT_PROPOSAL, "")


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
    assert why(events) == ("unverified", NO_PROPOSAL, "")


async def test_a_proposal_failing_when_revisions_run_out_says_so_without_a_fix_task() -> None:
    events, replies = await configure(
        {"steps": [LINT]},
        {"steps": [LINT]},
        review=Review(),
        steps=Steps(False, False),
        limits=Limits(max_attempts=1),
    )
    assert "all 1 revisions" in replies[1]
    assert [task.task.role for task in board(events)] == ["configurator"]
    assert why(events) == LINT_FAILED


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


async def test_proposals_before_get_task_or_without_verification_are_refused() -> None:
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
    await eventually(lambda: steps.runs)
    await send(session, CancelTurn())
    events = await asyncio.wait_for(running, 5)
    assert status(events) == "cancelled"


class Blocked:
    """A review that never decides, noting when it's shown a proposal and when it's dropped."""

    def __init__(self) -> None:
        self.shown = asyncio.Event()
        self.dropped = False

    async def review(self, task_id: str, proposal: ChecksProposal) -> ProposalDecision:
        self.shown.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.dropped = True
        raise AssertionError("unreachable")


async def test_an_agent_exiting_while_its_proposal_is_shown_drops_it_and_cancels_the_task() -> None:
    review = Blocked()

    async def leaves(call: Call) -> TurnEndReason:
        await call("get_task")
        propose = asyncio.ensure_future(call("propose_checks", steps=[LINT]))
        await review.shown.wait()
        propose.cancel()
        return "completed"

    session, _, _ = session_for(leaves, verification=verified(Steps(True)), proposal_review=review)
    events = await asyncio.wait_for(run(session, CONFIGURE), 5)
    assert review.dropped
    assert status(events) == "cancelled"


Decide = Callable[[ChecksProposed], Command]


async def decided(session: WorkspaceHost, decide: Decide) -> list[Event]:
    """Run a configurator task, answering each proposal it shows with ``decide``; returns the
    events until the session goes idle."""
    await send(session, AddTask(task=CONFIGURE))
    await send(session, RunTasks())
    events: list[Event] = []
    async for event in payloads(session):
        events.append(event)
        if isinstance(event, ChecksProposed):
            await send(session, decide(event))
        if event == BusyChanged(busy=False):
            return events
    raise AssertionError("unreachable")


def frontend(
    *arguments: dict[str, Any], replies: list[str], steps: Steps, store: FakeStore | None = None
) -> WorkspaceHost:
    """A session without a review of its own, so its frontend decides on proposals; it's
    saved in ``store``, if given."""
    session, _, _ = session_for(
        proposes(*arguments, replies=replies),
        verification=verified(steps),
        tree=FakeTree(),
        store=store,
    )
    return session


async def test_without_a_review_of_its_own_the_session_has_the_frontend_decide() -> None:
    replies: list[str] = []
    session = frontend(
        {"steps": [LINT, TESTS], "setup": SETUP}, replies=replies, steps=Steps(True, True, True)
    )
    events = await asyncio.wait_for(
        decided(session, lambda e: DecideProposal(task_id=e.task_id, action="accept")), 5
    )
    [shown] = [e for e in events if isinstance(e, ChecksProposed)]
    assert shown.agent_id == "agent-1"
    assert shown.steps == (
        ProposedStep(name="Lint", command=("ruff", "check")),
        ProposedStep(name="Tests", command=("pytest",)),
    )
    assert shown.setup == ProposedSetup(command=("uv", "sync"), watch=("uv.lock",))
    assert "accepted" in replies[0]
    assert status(events) == "completed"


def decisions_of(events: list[Event]) -> list[ProposalDecided]:
    return [e for e in events if isinstance(e, ProposalDecided)]


async def test_the_frontend_can_ask_for_changes_or_end_it() -> None:
    replies: list[str] = []
    store = FakeStore()
    session = frontend(
        {"steps": [LINT]}, {"steps": [LINT]}, replies=replies, steps=Steps(True, True), store=store
    )
    decisions: Iterator[ProposalAction] = iter(("revise", "end"))

    def decide(event: ChecksProposed) -> Command:
        assert event.setup is None
        return DecideProposal(task_id=event.task_id, action=next(decisions), feedback="Add mypy.")

    events = await asyncio.wait_for(decided(session, decide), 5)
    assert "> Add mypy." in replies[0]
    assert "ended" in replies[1]
    assert status(events) == "cancelled"
    [task] = board(events)
    recorded = [
        ProposalDecided(
            command_id=COMMAND_ID,
            agent_id="agent-1",
            task_id=task.id,
            action=action,
            feedback="Add mypy.",
        )
        for action in ("revise", "end")
    ]
    assert decisions_of(events) == recorded
    await session.close()
    logged = [envelope.payload for envelope in store.logged]
    assert [e for e in logged if isinstance(e, ProposalDecided)] == recorded


async def test_deciding_when_no_proposal_waits_is_an_error() -> None:
    session, _, _ = session_for()
    await send(session, DecideProposal(task_id="nope", action="accept"))

    events = await events_until(session, of_type(CommandRefused))
    assert events[-1] == refused(UNKNOWN, "No proposal is waiting for task 'nope'.")


async def test_a_second_decision_on_the_same_proposal_is_an_error() -> None:
    replies: list[str] = []
    session = frontend({"steps": [LINT]}, replies=replies, steps=Steps(True))

    def twice(event: ChecksProposed) -> Command:
        accept = DecideProposal(task_id=event.task_id, action="accept")
        asyncio.get_running_loop().create_task(send(session, accept))
        return accept

    events = await asyncio.wait_for(decided(session, twice), 5)
    [task] = board(events)
    refusals = [e for e in events if isinstance(e, CommandRefused)]
    assert refusals == [refused(UNKNOWN, f"No proposal is waiting for task {task.id!r}.")]
    assert [decision.action for decision in decisions_of(events)] == ["accept"]
    assert not any(isinstance(e, ErrorOccurred) for e in events)
    assert task.status == "completed"


async def test_cancelling_while_the_frontend_decides_cancels_the_task() -> None:
    replies: list[str] = []
    session = frontend({"steps": [LINT]}, replies=replies, steps=Steps(True))
    events = await asyncio.wait_for(decided(session, lambda _: CancelTurn()), 5)
    assert status(events) == "cancelled"
    assert any(isinstance(e, AgentExited) for e in events)
    assert not any(isinstance(e, AgentReply) and "accepted" in e.text for e in events)


async def test_a_configurator_that_exits_without_proposing_is_unverified() -> None:
    async def leaves(call: Call) -> TurnEndReason:
        await call("get_task")
        return "completed"

    session, _, _ = session_for(
        leaves, verification=verified(Steps()), proposal_review=Review(), tree=FakeTree()
    )
    events = await asyncio.wait_for(run(session, CONFIGURE), 5)
    assert why(events) == ("unverified", NO_PROPOSAL, "")


async def test_a_configurator_that_exits_after_a_failure_or_revise_reply_is_unverified() -> None:
    events, replies = await configure(
        {"steps": [LINT]}, review=Review(), steps=Steps(False), tree=FakeTree()
    )
    assert "propose_checks again" in replies[0]
    assert why(events) == LINT_FAILED
    events, replies = await configure(
        {"steps": [LINT]},
        review=Review(ProposalDecision("revise", "Add mypy.")),
        steps=Steps(True),
        tree=FakeTree(),
    )
    assert "> Add mypy." in replies[0]
    assert why(events) == ("unverified", NO_PROPOSAL, "")
