"""Reviews: queued as work lands, run beside the work in snapshots, suggestions run after."""

import asyncio
import itertools
from collections.abc import Callable

from orbit.agent import AgentSession
from orbit.agent.application.ports import Reply
from orbit.agent.domain.agent_task import Limits
from orbit.protocol import (
    AddTask,
    AgentExited,
    CancelAgent,
    CancelTurn,
    ErrorOccurred,
    Event,
    Notice,
    RequestSuperReview,
    ReviewStatus,
    RunTasks,
    TasksChanged,
    TaskSpec,
    TaskState,
    TurnEndReason,
)
from tests.agent_fakes import (
    Call,
    FakeTree,
    Script,
    ScriptedLauncher,
    Steps,
    session_for,
    verified,
)

SETTLED = {"completed", "unverified", "gave_up", "error", "cancelled"}


async def claims_done(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("complete_task", summary="Did it")
    return "completed"


def suggests(*titles: str, replies: list[Reply] | None = None) -> Script:
    async def review(call: Call) -> TurnEndReason:
        await call("get_task")
        for title in titles:
            reply = await call("suggest_task", title=title, prompt=f"Please {title.lower()}.")
            if replies is not None:
                replies.append(reply)
        await call("complete_task", summary="Reviewed")
        return "completed"

    return review


async def settle(
    session: AgentSession,
    events: list[Event] | None = None,
    until: Callable[[list[Event]], bool] | None = None,
) -> list[Event]:
    """Events until ``until`` holds (by default, every task has finished) and nothing has
    happened for a moment."""
    events = [] if events is None else events

    async def pump() -> None:
        async for event in session.events():
            events.append(event)

    pumping = asyncio.ensure_future(pump())
    try:
        await _quiet(events, until or _finished)
        return events
    finally:
        pumping.cancel()


async def _quiet(events: list[Event], done: Callable[[list[Event]], bool]) -> None:
    seen, quiet = -1, 0
    while quiet < 3:
        await asyncio.sleep(0.05)
        quiet = quiet + 1 if len(events) == seen and done(events) else 0
        seen = len(events)


def _finished(events: list[Event]) -> bool:
    tasks = [e for e in events if isinstance(e, TasksChanged)]
    return bool(tasks) and all(t.status in SETTLED for t in tasks[-1].tasks)


def reviewers_of(launcher: ScriptedLauncher) -> list[str]:
    return [la.agent.id for la in launcher.launches if la.agent.role == "reviewer"]


def statuses_of(events: list[Event], role: str) -> list[str]:
    return [t.status for t in board(events) if t.task.role == role]


def board(events: list[Event]) -> tuple[TaskState, ...]:
    return [e for e in events if isinstance(e, TasksChanged)][-1].tasks


async def start(session: AgentSession, *prompts: str) -> None:
    for prompt in prompts or ("Make it faster.",):
        await session.send(AddTask(task=TaskSpec(prompt=prompt)))
    await session.send(RunTasks())


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
        review_rounds=1,
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
        review_rounds=1,
        review_suggestions=2,
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [r.is_error for r in replies] == [False, True, False, True]
    assert "already" in replies[1].text and "2 tasks" in replies[3].text
    assert len([t for t in board(events) if t.batch]) == 2


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
        review_rounds=1,
    )
    await start(session)
    while not any(la.agent.role == "reviewer" for la in launcher.launches):
        await asyncio.sleep(0.01)
    await session.send(CancelTurn())
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

    await session.send(RunTasks())
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
    while not session._events.empty():
        events.append(session._events.get_nowait())
    assert AgentExited(agent_id="agent-2", reason="killed") in events
    assert tree.dropped == ["/work/review-c1"]


async def test_a_review_the_user_queued_works_in_place_and_cannot_suggest() -> None:
    replies: list[Reply] = []
    session, _, _ = session_for(
        reviewers=(suggests("Add tests", replies=replies),), tree=FakeTree(), review=True
    )
    await session.send(AddTask(task=TaskSpec(prompt="Review the parser", role="reviewer")))
    await session.send(RunTasks())
    events = await asyncio.wait_for(settle(session), 10)
    assert replies[0].is_error and "suggest" in replies[0].text
    assert [t.status for t in board(events)] == ["completed"]


def test_launcher_scripts_for_reviews_are_kept_apart() -> None:
    launcher = ScriptedLauncher(object(), claims_done, reviewers=(claims_done,))  # type: ignore[arg-type]
    assert (len(launcher.scripts), len(launcher.reviewers)) == (1, 1)


async def test_tasks_of_a_batch_are_fixed_within_it() -> None:
    async def keeps_claiming(call: Call) -> TurnEndReason:
        await call("get_task")
        while "another agent" not in (await call("complete_task", summary="try")).text:
            pass
        return "completed"

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
    async def keeps_claiming(call: Call) -> TurnEndReason:
        await call("get_task")
        while "another agent" not in (await call("complete_task", summary="try")).text:
            pass
        return "completed"

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


async def test_every_so_many_commits_the_whole_codebase_is_super_reviewed() -> None:
    tree = FakeTree()
    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(suggests(), suggests(), suggests("Split the parser")),
        tree=tree,
        review=True,
        super_review_every=2,
    )
    await start(session, "one", "two")
    events = await asyncio.wait_for(settle(session), 10)
    tasks = board(events)
    supers = [t for t in tasks if t.task.role == "super-reviewer"]
    assert len(supers) == 1
    assert supers[0].task.prompt.startswith("Super review of the codebase at c2")
    assert supers[0].task.settings.model == "opus"  # The super reviewer's default.
    [suggestion] = [t for t in tasks if t.batch]
    assert (suggestion.origin, suggestion.round) == ("super_review", 1)
    assert "c2" in tree.snapshots


async def test_a_super_review_can_be_asked_for() -> None:
    tree = FakeTree()
    session, _, _ = session_for(reviewers=(suggests(),), tree=tree, review=True)
    await session.send(RequestSuperReview())
    events = await asyncio.wait_for(settle(session), 10)
    assert [t.task.role for t in board(events)] == ["super-reviewer"]
    assert tree.snapshots == ["c0"]


async def test_without_reviews_a_super_review_is_refused() -> None:
    session, _, _ = session_for(tree=FakeTree())
    await session.send(RequestSuperReview())
    stream = session.events()
    events = [await anext(stream), await anext(stream)]
    assert any(isinstance(e, ErrorOccurred) and "Super reviews" in e.message for e in events)


async def test_review_status_follows_batches_and_the_pause() -> None:
    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(suggests("Add tests"),),
        tree=FakeTree(),
        review=True,
        review_rounds=1,
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
    await session.send(CancelAgent(agent_id=reviewer))
    await asyncio.wait_for(collecting, 10)
    assert statuses_of(events, "reviewer") == ["cancelled", "completed"]
    assert AgentExited(agent_id=reviewer, reason="killed") in events
    assert statuses_of(events, "coder") == ["completed", "completed"]


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
    await session.send(CancelAgent(agent_id="agent-1"))
    events = await asyncio.wait_for(
        settle(session, until=lambda ev: [t.status for t in board(ev)] == ["cancelled", "pending"]),
        10,
    )
    assert [t.status for t in board(events)] == ["cancelled", "pending"]


async def test_cancelling_an_unknown_agent_is_reported() -> None:
    session, _, _ = session_for()
    await session.send(CancelAgent(agent_id="agent-7"))
    stream = session.events()
    events = [await anext(stream), await anext(stream)]
    assert ErrorOccurred(message="No running agent 'agent-7'.") in events


async def test_cancelling_a_review_that_is_not_running_does_nothing() -> None:
    session, _, _ = session_for(tree=FakeTree(), review=True)
    lane = session._lane
    assert lane is not None
    lane.cancel("42")


async def test_a_review_can_suggest_after_its_first_suggestion_landed() -> None:
    suggested = asyncio.Event()
    replies: list[Reply] = []
    sessions: list[AgentSession] = []

    async def waits_for_suggestion(call: Call) -> TurnEndReason:
        await suggested.wait()
        return await claims_done(call)

    async def suggests_after_landing(call: Call) -> TurnEndReason:
        await call("get_task")
        replies.append(await call("suggest_task", title="Add tests", prompt="Please."))
        suggested.set()
        first = sessions[0]._board.get("4")
        while first.status != "completed":
            await asyncio.sleep(0.01)
            first = sessions[0]._board.get("4")
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
