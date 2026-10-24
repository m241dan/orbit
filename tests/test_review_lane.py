"""Reviews: queued as work lands, run beside the work in snapshots, suggestions run after."""

import asyncio
import itertools

import pytest

from orbit.agent import WorkspaceHost
from orbit.agent.application.ports import Reply, TreeFailed
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.work_turns import WorkTurns
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.endings import BUG, CANCELLED, REVIEW_STOPPED_BY_YOU, STOPPED_BY_YOU
from orbit.agent.domain.reviews import ReviewLimits
from orbit.protocol import (
    GIT_FAILED,
    INTERNAL,
    INVALID,
    NOT_CONFIGURED,
    SUPER_REVIEW,
    UNKNOWN,
    AddTask,
    AgentExited,
    CancelAgent,
    CancelTurn,
    CommandRefused,
    EditTask,
    ErrorOccurred,
    Event,
    FireTrigger,
    Notice,
    RemoveTask,
    ReviewStatus,
    RunTasks,
    TasksChanged,
    TaskSpec,
    TaskState,
    TaskStatus,
    TurnEndReason,
)
from tests.agent_fakes import (
    Call,
    FakeTree,
    ScriptedLauncher,
    Steps,
    Stop,
    board,
    board_of,
    claims_done,
    events_until,
    gives_up,
    keeps_claiming,
    lane_of,
    of_type,
    queued,
    refused,
    send,
    session_for,
    settle,
    start,
    suggests,
    verified,
)


def _errored(events: list[Event]) -> bool:
    return bool(_error_messages(events))


def _refused(events: list[Event]) -> bool:
    return bool(_refusals(events))


def _refusals(events: list[Event]) -> list[CommandRefused]:
    return [e for e in events if isinstance(e, CommandRefused)]


def _error_messages(events: list[Event]) -> list[str]:
    return [e.message for e in events if isinstance(e, ErrorOccurred)]


def _roles_seen(events: list[Event]) -> set[str]:
    """Every role a task on the board had, over all the board's changes."""
    return {t.task.role for e in events if isinstance(e, TasksChanged) for t in e.tasks}


def reviewers_of(launcher: ScriptedLauncher) -> list[str]:
    return [la.agent.id for la in launcher.launches if la.agent.role == "reviewer"]


def statuses_of(events: list[Event], role: str) -> list[str]:
    return [t.status for t in board(events) if t.task.role == role]


def task_is(task_id: str, status: TaskStatus) -> Stop:
    """Stops when task ``task_id`` is published with ``status``."""
    return Stop(
        f"task_is({task_id!r}, {status!r})",
        lambda e: (
            isinstance(e, TasksChanged)
            and any(t.id == task_id and t.status == status for t in e.tasks)
        ),
    )


def latest_board(events: list[Event]) -> tuple[TaskState, ...]:
    """The tasks last published in ``events``, or none if none were."""
    return board(events) if any(isinstance(e, TasksChanged) for e in events) else ()


async def test_landed_work_is_reviewed_and_suggestions_run_and_are_reviewed_in_turn() -> None:
    tree = FakeTree()
    session, _, launcher = session_for(
        claims_done,
        claims_done,
        claims_done,
        reviewers=(suggests("Add tests", "Rename x"), suggests()),
        tree=tree,
        review=True,
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)

    shown = [(t.task.role, t.status, t.origin, t.batch, t.round) for t in board(events)]
    assert shown == [
        ("coder", "completed", "manual", None, 0),
        ("reviewer", "completed", "review", None, 0),
        ("coder", "completed", "review", "batch-1", 1),
        ("coder", "completed", "review", "batch-1", 1),
        ("reviewer", "completed", "review", None, 0),
    ]
    _, first_review, suggestion, _, batch_review = board(events)
    assert first_review.task.prompt.startswith("Review commit c1")
    assert "diff of c1" in first_review.task.prompt
    assert suggestion.task.prompt == "Add tests\n\nPlease add tests."
    assert suggestion.task.settings.tool_access == "full"  # The coder role's default.
    assert batch_review.task.prompt.startswith("Review the 2 commits made for review batch-1")
    assert tree.snapshots == ["c1", "c3"]
    assert tree.dropped == ["/work/review-c1", "/work/review-c3"]
    review_launches = [la for la in launcher.launches if la.agent.role == "reviewer"]
    assert [la.cwd for la in review_launches] == ["/work/review-c1", "/work/review-c3"]


async def test_review_pauses_after_the_last_round_and_says_so() -> None:
    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(suggests("Add tests"),),
        tree=FakeTree(),
        review=True,
        review_limits=ReviewLimits(rounds=1),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [t.task.role for t in board(events)] == ["coder", "reviewer", "coder"]
    notices = [e.message for e in events if isinstance(e, Notice)]
    assert len(notices) == 1 and "paused" in notices[0]


async def test_suggestions_over_the_cap_or_already_queued_are_refused() -> None:
    replies: list[Reply] = []
    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(suggests("Add tests", "add TESTS", "Rename x", "Tidy", replies=replies),),
        tree=FakeTree(),
        review=True,
        review_limits=ReviewLimits(rounds=1, suggestions=2),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [r.is_error for r in replies] == [False, True, False, True]
    assert "already" in replies[1].text and "2 tasks" in replies[3].text
    assert len([t for t in board(events) if t.batch]) == 2


async def test_a_review_is_told_which_tasks_are_queued_already() -> None:
    session, _, _ = session_for(
        claims_done,
        claims_done,
        claims_done,
        reviewers=(suggests(), suggests(), suggests()),
        tree=FakeTree(),
        review=True,
    )
    await start(session, "Make it faster.", "Add tests\n\nFor parse.", "Rename x")
    events = await asyncio.wait_for(settle(session), 10)
    reviews = [t.task.prompt for t in board(events) if t.task.role == "reviewer"]
    assert "Tasks already queued (don't suggest these):\n- Add tests\n- Rename x\n\n" in reviews[0]
    assert "- Add tests" not in reviews[2] and "- Rename x" not in reviews[2]


async def test_a_suggestion_an_earlier_review_made_is_refused_after_its_task_is_removed() -> None:
    replies: list[Reply] = []
    sessions: list[WorkspaceHost] = []

    async def suggests_then_it_is_removed(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("suggest_task", title="Add tests", prompt="Please.")
        await send(sessions[0], RemoveTask(task_id="4"))
        await call("complete_task", summary="Reviewed")
        return "completed"

    session, _, _ = session_for(
        claims_done,
        claims_done,
        claims_done,
        reviewers=(suggests_then_it_is_removed, suggests("add TESTS", "Rename x", replies=replies)),
        tree=FakeTree(),
        review=True,
    )
    sessions.append(session)
    await start(session, "one", "two")
    events = await asyncio.wait_for(settle(session), 10)
    assert [r.is_error for r in replies] == [True, False]
    assert replies[0].text == "An earlier review already suggested that."
    assert [t.task.prompt.split("\n")[0] for t in board(events) if t.batch] == ["Rename x"]


async def test_a_suggestion_without_a_title_is_refused_and_not_counted() -> None:
    replies: list[Reply] = []
    session, _, _ = session_for(
        claims_done,
        claims_done,
        claims_done,
        reviewers=(suggests(" ", "Add tests", "Rename x", replies=replies), suggests()),
        tree=FakeTree(),
        review=True,
        review_limits=ReviewLimits(rounds=1, suggestions=2),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [r.is_error for r in replies] == [True, False, False]
    assert replies[0].text == "A task needs a title."
    assert [t.task.prompt.split("\n")[0] for t in board(events) if t.batch] == [
        "Add tests",
        "Rename x",
    ]


async def test_without_review_nothing_is_reviewed() -> None:
    session, _, _ = session_for(claims_done, tree=FakeTree())
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [t.task.role for t in board(events)] == ["coder"]


async def test_a_crashing_reviewer_still_has_its_snapshot_dropped() -> None:
    async def crashes(call: Call) -> TurnEndReason:
        raise RuntimeError("reviewer fell over")

    tree = FakeTree()
    session, _, _ = session_for(claims_done, reviewers=(crashes,), tree=tree, review=True)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [t.status for t in board(events)] == ["completed", "error"]
    assert tree.dropped == ["/work/review-c1"]


async def test_a_snapshot_that_cannot_be_dropped_leaves_the_review_completed() -> None:
    tree = FakeTree()
    tree.failures["drop"] = TreeFailed("git worktree remove failed")
    session, _, _ = session_for(claims_done, reviewers=(suggests(),), tree=tree, review=True)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [t.status for t in board(events)] == ["completed", "completed"]
    message = (
        "The snapshot at /work/review-c1 couldn't be removed and is left over:"
        " git worktree remove failed"
    )
    assert ErrorOccurred(code=GIT_FAILED, message=message) in events


async def test_after_cancelling_suggestions_wait_to_be_run() -> None:
    gate = asyncio.Event()

    async def slow_review(call: Call) -> TurnEndReason:
        await call("get_task")
        await gate.wait()
        await call("suggest_task", title="Add tests", prompt="Please.")
        await call("complete_task", summary="Reviewed")
        return "completed"

    session, _, launcher = session_for(
        claims_done,
        claims_done,
        reviewers=(slow_review,),
        tree=FakeTree(),
        review=True,
        review_limits=ReviewLimits(rounds=1),
    )
    await start(session)
    while not any(la.agent.role == "reviewer" for la in launcher.launches):
        await asyncio.sleep(0.01)
    await send(session, CancelTurn())
    gate.set()

    def reviewed(events: list[Event]) -> bool:
        boards = [e.tasks for e in events if isinstance(e, TasksChanged)]
        return bool(boards) and [t.status for t in boards[-1]] == [
            "completed",
            "completed",
            "pending",
        ]

    events = await asyncio.wait_for(settle(session, until=reviewed), 10)
    assert [t.status for t in board(events)] == ["completed", "completed", "pending"]

    await send(session, RunTasks())
    events = await asyncio.wait_for(settle(session, events), 10)
    assert [t.status for t in board(events)][-1] == "completed"


async def test_closing_stops_a_running_review() -> None:
    reviewing = asyncio.Event()

    async def endless(call: Call) -> TurnEndReason:
        await call("get_task")
        reviewing.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    tree = FakeTree()
    session, _, _ = session_for(claims_done, reviewers=(endless,), tree=tree, review=True)
    await start(session)
    events: list[Event] = []
    collecting = asyncio.ensure_future(settle(session, events))
    await asyncio.wait_for(reviewing.wait(), 10)
    await asyncio.wait_for(session.close(), 5)
    collecting.cancel()
    events += queued(session)
    assert AgentExited(agent_id="agent-2", reason="killed") in events
    assert tree.dropped == ["/work/review-c1"]


async def test_a_review_the_user_queued_works_in_place_and_cannot_suggest() -> None:
    replies: list[Reply] = []
    session, _, _ = session_for(
        reviewers=(suggests("Add tests", replies=replies),), tree=FakeTree(), review=True
    )
    await send(session, AddTask(task=TaskSpec(prompt="Review the parser", role="reviewer")))
    await send(session, RunTasks())
    events = await asyncio.wait_for(settle(session), 10)
    assert replies[0].is_error and "suggest" in replies[0].text
    assert [t.status for t in board(events)] == ["completed"]


def test_launcher_scripts_for_reviews_are_kept_apart() -> None:
    launcher = ScriptedLauncher(object(), claims_done, reviewers=(claims_done,))  # type: ignore[arg-type]
    assert (len(launcher.scripts), len(launcher.reviewers)) == (1, 1)


async def test_tasks_of_a_batch_are_fixed_within_it() -> None:
    session, _, _ = session_for(
        claims_done,
        keeps_claiming,
        claims_done,
        verification=verified(Steps(True, False, False, True, True)),
        reviewers=(suggests("Add tests"), suggests()),
        tree=FakeTree(),
        review=True,
        limits=Limits(max_attempts=1),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    shown = [(t.task.role, t.status, t.batch) for t in board(events)]
    assert shown == [
        ("coder", "completed", None),
        ("reviewer", "completed", None),
        ("coder", "completed", "batch-1"),
        ("coder", "completed", "batch-1"),
        ("reviewer", "completed", None),
    ]


async def test_work_that_committed_nothing_is_not_reviewed() -> None:
    session, _, _ = session_for(claims_done, tree=FakeTree(empty=True), review=True)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [t.task.role for t in board(events)] == ["coder"]


async def test_a_fix_of_the_users_task_joins_no_batch() -> None:
    session, _, _ = session_for(
        keeps_claiming,
        claims_done,
        verification=verified(Steps(False, False, True)),
        reviewers=(suggests(),),
        tree=FakeTree(),
        review=True,
        limits=Limits(max_attempts=1),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [(t.task.role, t.batch) for t in board(events)] == [
        ("coder", None),
        ("coder", None),
        ("reviewer", None),
    ]


async def test_closing_before_any_review_ran_is_harmless() -> None:
    session, _, _ = session_for(tree=FakeTree(), review=True)
    await session.close()


async def test_reviews_queued_while_one_runs_wait_their_turn() -> None:
    gate = asyncio.Event()

    async def slow(call: Call) -> TurnEndReason:
        await call("get_task")
        await gate.wait()
        await call("complete_task", summary="Reviewed")
        return "completed"

    async def opens_gate(call: Call) -> TurnEndReason:
        gate.set()
        return await claims_done(call)

    session, _, launcher = session_for(
        claims_done, opens_gate, reviewers=(slow, suggests()), tree=FakeTree(), review=True
    )
    await start(session, "one", "two")
    events = await asyncio.wait_for(settle(session), 10)
    roles = [t.task.role for t in board(events)]
    assert roles.count("reviewer") == 2
    assert [la.agent.role for la in launcher.launches].count("reviewer") == 2


async def _landed_two_while_the_first_review_waited(
    session: WorkspaceHost, gate: asyncio.Event
) -> list[Event]:
    """Run tasks "one" and "two", the second landing while the first's review waits on
    ``gate``, then let the reviews finish; returns the events."""
    await start(session, "one", "two")
    events: list[Event] = []
    collecting = asyncio.ensure_future(settle(session, events))
    while [t.task.role for t in latest_board(events)].count("reviewer") < 2:
        await asyncio.sleep(0.01)
    gate.set()
    await asyncio.wait_for(collecting, 10)
    return events


async def test_a_review_queued_while_another_waits_is_not_told_of_that_review() -> None:
    gate = asyncio.Event()

    async def waits_then_reviews(call: Call) -> TurnEndReason:
        await gate.wait()
        return await suggests()(call)

    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(waits_then_reviews, suggests()),
        tree=FakeTree(),
        review=True,
    )
    events = await _landed_two_while_the_first_review_waited(session, gate)
    reviews = [t.task.prompt for t in board(events) if t.task.role == "reviewer"]
    assert reviews[0].startswith("Review commit c1")
    assert "Review commit" not in reviews[1].split("\n", 1)[1]


async def test_every_so_many_of_the_users_commits_the_whole_codebase_is_super_reviewed() -> None:
    gate = asyncio.Event()

    async def waits_then_reviews(call: Call) -> TurnEndReason:
        await gate.wait()
        return await suggests()(call)

    tree = FakeTree()
    session, _, _ = session_for(
        *([claims_done] * 4),
        reviewers=(waits_then_reviews, suggests(), suggests(), suggests("Split the parser")),
        tree=tree,
        review=True,
        review_limits=ReviewLimits(super_every=2),
    )
    events = await _landed_two_while_the_first_review_waited(session, gate)
    assert "super-reviewer" not in _roles_seen(events)  # Due at c2, but a review was pending.

    await send(session, AddTask(task=TaskSpec(prompt="three")))
    await send(session, RunTasks())
    events = await asyncio.wait_for(settle(session, events), 10)
    tasks = board(events)
    supers = [t for t in tasks if t.task.role == "super-reviewer"]
    assert len(supers) == 1
    assert supers[0].task.prompt.startswith("Super review of the codebase at c3")
    assert supers[0].task.settings.model == "opus"  # The super reviewer's default.
    [suggestion] = [t for t in tasks if t.batch]
    assert (suggestion.origin, suggestion.round) == ("super_review", 1)
    assert "c3" in tree.snapshots


async def test_a_super_review_can_be_asked_for_before_any_task_ran() -> None:
    # The coder's tree doesn't exist yet, so its head can't be read; the base's commit can.
    tree = FakeTree()
    session, _, _ = session_for(reviewers=(suggests(),), tree=tree, review=True)
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session), 10)
    assert [t.task.role for t in board(events)] == ["super-reviewer"]
    assert tree.snapshots == [tree.base] and tree.prepared == []
    assert not any(isinstance(e, ErrorOccurred) for e in events)


async def test_a_super_review_is_skipped_when_the_base_commit_cannot_be_read() -> None:
    tree = FakeTree()
    tree.failures["base_commit"] = TreeFailed("git rev-parse failed: unknown revision")
    session, _, _ = session_for(reviewers=(suggests(),), tree=tree, review=True)
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session, until=_errored), 10)
    message = "Super review skipped: git rev-parse failed: unknown revision"
    assert ErrorOccurred(code=GIT_FAILED, message=message) in events
    assert "super-reviewer" not in _roles_seen(events)
    assert tree.snapshots == []


async def test_a_super_review_given_input_is_refused_and_none_starts() -> None:
    tree = FakeTree()
    session, _, _ = session_for(reviewers=(suggests(),), tree=tree, review=True)
    await send(session, FireTrigger(trigger=SUPER_REVIEW, input={"x": "1"}))
    events = await asyncio.wait_for(settle(session, until=_refused), 10)
    assert _refusals(events) == [refused(INVALID, "The super-review trigger takes no input.")]
    assert not _error_messages(events)
    assert "super-reviewer" not in _roles_seen(events)
    assert tree.snapshots == []


async def test_without_reviews_a_super_review_is_refused() -> None:
    session, _, _ = session_for(tree=FakeTree())
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await events_until(session, of_type(CommandRefused))
    message = "Super reviews need reviews turned on, inside a git repository."
    assert events[-1] == refused(NOT_CONFIGURED, message)


async def test_review_status_follows_batches_and_the_pause() -> None:
    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(suggests("Add tests"),),
        tree=FakeTree(),
        review=True,
        review_limits=ReviewLimits(rounds=1),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    statuses = [e for e in events if isinstance(e, ReviewStatus)]
    assert statuses[0] == ReviewStatus(paused=False, open_batches=1)
    assert statuses[-1] == ReviewStatus(paused=True, open_batches=0)
    assert all(a != b for a, b in itertools.pairwise(statuses))  # Only changes.


async def test_cancelling_a_reviewer_stops_its_review_and_the_lane_carries_on() -> None:
    reviewing = asyncio.Event()

    async def endless(call: Call) -> TurnEndReason:
        await call("get_task")
        reviewing.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, launcher = session_for(
        claims_done, claims_done, reviewers=(endless, suggests()), tree=FakeTree(), review=True
    )
    await start(session, "one", "two")
    events: list[Event] = []
    collecting = asyncio.ensure_future(settle(session, events))
    await asyncio.wait_for(reviewing.wait(), 10)
    [reviewer] = reviewers_of(launcher)
    await send(session, CancelAgent(agent_id=reviewer))
    await asyncio.wait_for(collecting, 10)
    assert statuses_of(events, "reviewer") == ["cancelled", "completed"]
    stopped = next(t for t in board(events) if t.task.role == "reviewer")
    assert (stopped.reason, stopped.reason_detail) == (REVIEW_STOPPED_BY_YOU, "")
    assert AgentExited(agent_id=reviewer, reason="killed") in events
    assert statuses_of(events, "coder") == ["completed", "completed"]


async def test_a_review_cancelled_as_the_session_closes_is_just_cancelled() -> None:
    reviewing = asyncio.Event()

    async def endless(call: Call) -> TurnEndReason:
        await call("get_task")
        reviewing.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, _ = session_for(claims_done, reviewers=(endless,), tree=FakeTree(), review=True)
    await start(session)
    collecting = asyncio.ensure_future(settle(session, until=lambda _: reviewing.is_set()))
    await asyncio.wait_for(reviewing.wait(), 10)
    await asyncio.wait_for(collecting, 10)
    await session.close()
    [review] = [t for t in board(queued(session)) if t.task.role == "reviewer"]
    assert (review.status, review.reason, review.reason_detail) == ("cancelled", CANCELLED, "")


async def test_a_review_cancelled_as_the_session_closes_leaves_its_batch_open() -> None:
    reviewing = asyncio.Event()

    async def endless(call: Call) -> TurnEndReason:
        await call("get_task")
        reviewing.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, _ = session_for(claims_done, reviewers=(endless,), tree=FakeTree(), review=True)
    await start(session)
    collecting = asyncio.ensure_future(settle(session, until=lambda _: reviewing.is_set()))
    await asyncio.wait_for(reviewing.wait(), 10)
    events = await asyncio.wait_for(collecting, 10)
    await session.close()
    statuses = [e for e in events + queued(session) if isinstance(e, ReviewStatus)]
    assert statuses[-1] == ReviewStatus(paused=False, open_batches=1)


async def test_a_review_ending_as_the_session_closes_starts_no_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewing, release = asyncio.Event(), asyncio.Event()

    async def waits_then_suggests(call: Call) -> TurnEndReason:
        await call("get_task")
        reviewing.set()
        await release.wait()
        return await suggests("Add tests")(call)

    close_turns = WorkTurns.close

    async def close_then_release(turns: WorkTurns) -> None:
        await close_turns(turns)
        release.set()
        await asyncio.sleep(0.05)

    monkeypatch.setattr(WorkTurns, "close", close_then_release)
    session, _, launcher = session_for(
        claims_done, claims_done, reviewers=(waits_then_suggests,), tree=FakeTree(), review=True
    )
    await start(session)
    await asyncio.wait_for(reviewing.wait(), 10)
    await session.close()
    assert [la.agent.role for la in launcher.launches] == ["coder", "reviewer"]
    assert {t for t in asyncio.all_tasks() if not t.done()} == {asyncio.current_task()}


class _HeldTree(FakeTree):
    """A coder tree whose head, once work is committed, is read only after ``release``."""

    def __init__(self) -> None:
        super().__init__()
        self.heading, self.release = asyncio.Event(), asyncio.Event()

    async def head(self) -> str:
        if self.commits:
            self.heading.set()
            await self.release.wait()
        return await super().head()


async def test_a_task_landing_as_the_session_closes_starts_no_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = _HeldTree()
    close_lane = ReviewLane.close

    async def close_then_release(lane: ReviewLane) -> None:
        await close_lane(lane)
        tree.release.set()

    monkeypatch.setattr(ReviewLane, "close", close_then_release)
    session, _, launcher = session_for(claims_done, reviewers=(suggests(),), tree=tree, review=True)
    await start(session)
    await asyncio.wait_for(tree.heading.wait(), 10)
    await session.close()
    assert [la.agent.role for la in launcher.launches] == ["coder"]
    assert any(t.task.role == "reviewer" for t in board(queued(session)))
    assert not lane_of(session).running
    assert {t for t in asyncio.all_tasks() if not t.done()} == {asyncio.current_task()}


async def test_a_cancelled_reviewer_that_suggested_is_not_run_again() -> None:
    suggested, release = asyncio.Event(), asyncio.Event()

    async def suggests_then_waits(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("suggest_task", title="Add tests", prompt="Please add tests.")
        suggested.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def waits_then_done(call: Call) -> TurnEndReason:
        await release.wait()
        return await claims_done(call)

    session, _, launcher = session_for(
        claims_done,
        waits_then_done,
        reviewers=(suggests_then_waits, suggests()),
        tree=FakeTree(),
        review=True,
    )
    await start(session)
    await asyncio.wait_for(suggested.wait(), 10)
    [reviewer] = reviewers_of(launcher)
    await send(session, CancelAgent(agent_id=reviewer))
    await events_until(session, task_is("2", "cancelled"))
    await send(session, RunTasks())
    await asyncio.sleep(0.05)
    release.set()
    events = await asyncio.wait_for(settle(session), 10)
    assert reviewers_of(launcher)[0] == reviewer
    assert [la.agent.task_id for la in launcher.launches].count("2") == 1
    assert [(t.task.role, t.status) for t in board(events)][:3] == [
        ("coder", "completed"),
        ("reviewer", "cancelled"),
        ("coder", "completed"),
    ]


async def test_cancelling_the_coder_is_like_escape() -> None:
    working = asyncio.Event()

    async def endless(call: Call) -> TurnEndReason:
        await call("get_task")
        working.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, _ = session_for(endless)
    await start(session, "one", "two")
    await asyncio.wait_for(working.wait(), 5)
    await send(session, CancelAgent(agent_id="agent-1"))
    events = await asyncio.wait_for(
        settle(session, until=lambda ev: [t.status for t in board(ev)] == ["cancelled", "pending"]),
        10,
    )
    assert [(t.status, t.reason, t.reason_detail) for t in board(events)] == [
        ("cancelled", STOPPED_BY_YOU, ""),
        ("pending", "", ""),
    ]


async def test_cancelling_an_unknown_agent_is_reported() -> None:
    session, _, _ = session_for()
    await send(session, CancelAgent(agent_id="agent-7"))
    events = await events_until(session, of_type(CommandRefused))
    assert events[-1] == refused(UNKNOWN, "No running agent 'agent-7'.")


async def test_cancelling_a_review_that_is_not_running_does_nothing() -> None:
    session, _, _ = session_for(tree=FakeTree(), review=True)
    lane_of(session).cancel("42")


async def test_a_review_can_suggest_after_its_first_suggestion_landed() -> None:
    suggested = asyncio.Event()
    replies: list[Reply] = []
    sessions: list[WorkspaceHost] = []

    async def waits_for_suggestion(call: Call) -> TurnEndReason:
        await suggested.wait()
        return await claims_done(call)

    async def suggests_after_landing(call: Call) -> TurnEndReason:
        await call("get_task")
        replies.append(await call("suggest_task", title="Add tests", prompt="Please."))
        suggested.set()
        first = board_of(sessions[0]).get("4")
        while first.status != "completed":
            await asyncio.sleep(0.01)
            first = board_of(sessions[0]).get("4")
        await asyncio.sleep(0.1)  # Let the work lane tell the review lane it landed.
        replies.append(await call("suggest_task", title="Rename x", prompt="Please."))
        await call("complete_task", summary="Reviewed")
        return "completed"

    session, _, _ = session_for(
        claims_done,
        waits_for_suggestion,
        claims_done,
        claims_done,
        reviewers=(suggests_after_landing, suggests(), suggests()),
        tree=FakeTree(),
        review=True,
    )
    sessions.append(session)
    await start(session, "one", "two")
    events = await asyncio.wait_for(settle(session), 10)
    assert [r.is_error for r in replies] == [False, False]
    shown = [(t.id, t.task.role, t.status, t.batch) for t in board(events)]
    assert shown == [
        ("1", "coder", "completed", None),
        ("2", "coder", "completed", None),
        ("3", "reviewer", "completed", None),
        ("4", "coder", "completed", "batch-1"),
        ("5", "reviewer", "completed", None),  # Of task 2.
        ("6", "coder", "completed", "batch-1"),
        ("7", "reviewer", "completed", None),
    ]
    batch_review = board(events)[6]
    assert batch_review.task.prompt.startswith("Review the 2 commits made for review batch-1")


async def test_a_snapshot_that_fails_errors_the_review_and_the_next_review_still_runs() -> None:
    tree = FakeTree()
    tree.failures["snapshot"] = TreeFailed("git worktree add failed: no space left")
    session, _, _ = session_for(
        claims_done, claims_done, reviewers=(suggests(),), tree=tree, review=True
    )
    await start(session, "one", "two")
    events = await asyncio.wait_for(settle(session), 10)
    assert statuses_of(events, "reviewer") == ["error", "completed"]
    assert statuses_of(events, "coder") == ["completed", "completed"]
    message = "git worktree add failed: no space left"
    assert ErrorOccurred(code=GIT_FAILED, message=message) in events
    errored = next(t for t in board(events) if t.status == "error")
    assert (errored.reason, errored.reason_detail) == (f"Error: {message}", message)
    assert [e for e in events if isinstance(e, ReviewStatus)][-1].open_batches == 0
    assert tree.snapshots == ["c2"] and tree.dropped == ["/work/review-c2"]


async def test_a_diff_that_cannot_be_shown_skips_the_review_and_the_run_goes_on() -> None:
    tree = FakeTree()
    tree.failures["show"] = TreeFailed("git show failed: bad object c1")
    session, _, _ = session_for(
        claims_done, claims_done, reviewers=(suggests(),), tree=tree, review=True
    )
    await start(session, "one", "two")
    events = await asyncio.wait_for(settle(session), 10)
    assert [(t.task.role, t.status) for t in board(events)] == [
        ("coder", "completed"),
        ("coder", "completed"),
        ("reviewer", "completed"),
    ]
    assert (
        ErrorOccurred(code=GIT_FAILED, message="Review skipped: git show failed: bad object c1")
        in events
    )
    assert "diff of c2" in board(events)[2].task.prompt


async def test_an_unexpected_error_in_the_review_lane_is_reported() -> None:
    tree = FakeTree()
    tree.failures["snapshot"] = ValueError("the lane tripped")
    session, _, _ = session_for(claims_done, tree=tree, review=True)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    message = "The review lane stopped on an unexpected error: ValueError('the lane tripped')"
    assert ErrorOccurred(code=INTERNAL, message=message) in events
    [review] = [t for t in board(events) if t.task.role == "reviewer"]
    assert (review.status, review.reason, review.reason_detail) == (
        "error",
        BUG,
        "the lane tripped",
    )
    assert [e for e in events if isinstance(e, ReviewStatus)][-1].open_batches == 0


async def test_a_suggestion_removed_from_its_batch_leaves_the_rest_reviewed() -> None:
    sessions: list[WorkspaceHost] = []

    async def suggests_then_one_is_removed(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("suggest_task", title="Add tests", prompt="Please.")
        await call("suggest_task", title="Rename x", prompt="Please.")
        await send(sessions[0], RemoveTask(task_id="4"))
        await call("complete_task", summary="Reviewed")
        return "completed"

    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(suggests_then_one_is_removed, suggests()),
        tree=FakeTree(),
        review=True,
    )
    sessions.append(session)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [(t.id, t.task.role, t.status) for t in board(events)] == [
        ("1", "coder", "completed"),
        ("2", "reviewer", "completed"),
        ("3", "coder", "completed"),
        ("5", "reviewer", "completed"),
    ]
    assert board(events)[3].task.prompt.startswith("Review the 1 commits made for review batch-1")
    assert [e for e in events if isinstance(e, ReviewStatus)][-1].open_batches == 0


async def test_a_queued_review_that_is_removed_leaves_no_batch_open() -> None:
    gate = asyncio.Event()

    async def slow(call: Call) -> TurnEndReason:
        await call("get_task")
        await gate.wait()
        await call("complete_task", summary="Reviewed")
        return "completed"

    session, _, launcher = session_for(
        claims_done, claims_done, reviewers=(slow,), tree=FakeTree(), review=True
    )
    await start(session, "one", "two")
    events: list[Event] = []
    collecting = asyncio.ensure_future(settle(session, events))
    while [t.task.role for t in latest_board(events)].count("reviewer") < 2:
        await asyncio.sleep(0.01)
    await send(session, RemoveTask(task_id="4"))
    gate.set()
    await asyncio.wait_for(collecting, 10)
    assert [(t.id, t.task.role) for t in board(events)] == [
        ("1", "coder"),
        ("2", "coder"),
        ("3", "reviewer"),
    ]
    assert len(reviewers_of(launcher)) == 1
    assert [e for e in events if isinstance(e, ReviewStatus)][-1].open_batches == 0


async def test_a_finished_review_can_be_removed_but_not_edited() -> None:
    session, _, _ = session_for(claims_done, reviewers=(suggests(),), tree=FakeTree(), review=True)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await send(session, EditTask(task_id="2", task=TaskSpec(prompt="Again", role="reviewer")))
    await send(session, RemoveTask(task_id="2"))
    events = await asyncio.wait_for(settle(session, events), 10)
    assert refused(INVALID, "Reviews can't be edited; remove it instead.") in events
    assert [(t.id, t.status) for t in board(events)] == [("1", "completed")]
    assert [e for e in events if isinstance(e, ReviewStatus)][-1].open_batches == 0


async def edit_a_user_review(session: WorkspaceHost) -> list[Event]:
    """The events after adding a reviewer task to ``session`` and editing its prompt."""
    await send(session, AddTask(task=TaskSpec(prompt="Review the parser", role="reviewer")))
    await send(session, EditTask(task_id="1", task=TaskSpec(prompt="Again", role="reviewer")))
    events = queued(session)
    await session.close()
    return events


async def test_a_review_the_user_queued_can_be_edited_without_the_review_lane() -> None:
    session, _, _ = session_for()
    events = await edit_a_user_review(session)
    assert not _error_messages(events)
    assert [t.task.prompt for t in board(events)] == ["Again"]


async def test_a_review_the_user_queued_can_be_edited_with_the_review_lane() -> None:
    session, _, _ = session_for(tree=FakeTree(), review=True)
    events = await edit_a_user_review(session)
    assert not _error_messages(events)
    assert [t.task.prompt for t in board(events)] == ["Again"]


async def test_a_task_edited_back_to_pending_is_reviewed_with_its_batch() -> None:
    sessions: list[WorkspaceHost] = []

    async def reopens_task_3(call: Call) -> TurnEndReason:
        await call("get_task")
        await send(sessions[0], EditTask(task_id="3", task=TaskSpec(prompt="Add tests, again.")))
        await call("complete_task", summary="Did it")
        return "completed"

    session, _, _ = session_for(
        claims_done,
        gives_up,
        reopens_task_3,
        claims_done,
        reviewers=(suggests("Add tests", "Rename x"), suggests()),
        tree=FakeTree(),
        review=True,
    )
    sessions.append(session)
    await start(session)
    events = await asyncio.wait_for(settle(session, until=lambda e: len(board(e)) == 4), 10)
    assert [t.status for t in board(events)][2:] == ["pending", "completed"]
    await send(session, RunTasks())
    events = await asyncio.wait_for(settle(session, events), 10)
    assert [(t.id, t.task.role, t.status) for t in board(events)] == [
        ("1", "coder", "completed"),
        ("2", "reviewer", "completed"),
        ("3", "coder", "completed"),
        ("4", "coder", "completed"),
        ("5", "reviewer", "completed"),
    ]
    prompt = board(events)[4].task.prompt
    assert prompt.startswith("Review the 2 commits made for review batch-1")
    assert "ended without landing a commit" not in prompt
    assert [e for e in events if isinstance(e, ReviewStatus)][-1].open_batches == 0


async def test_closing_the_session_mid_suggestion_leaves_its_batch_as_it_was() -> None:
    working = asyncio.Event()

    async def works_forever(call: Call) -> TurnEndReason:
        await call("get_task")
        working.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, _ = session_for(
        claims_done, works_forever, reviewers=(suggests("Add tests"),), tree=FakeTree(), review=True
    )
    await start(session)
    await asyncio.wait_for(working.wait(), 10)
    lane = lane_of(session)
    before = lane.planner.snapshot()
    await session.close()
    assert lane.planner.snapshot() == before
    roles = [t.task.role for t in board_of(session).tasks]
    assert roles == ["coder", "reviewer", "coder"]
