"""Snapshots of the review lane: restored as they were, with crash-interrupted reviews ended."""

import asyncio

from orbit.agent import AgentSession
from orbit.agent.application.ports import Reply
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.domain.board import TaskBoard, interrupted
from orbit.agent.domain.reviews import BatchSnapshot, LaneSnapshot, PlannerSnapshot, ReviewJob
from orbit.protocol import ReviewStatus, TaskOrigin, TaskSpec, TaskState, TaskStatus, TurnEndReason
from tests.agent_fakes import Call, FakeTree, ScriptedLauncher, session_for
from tests.test_review_lane import claims_done, start


def lane_of(session: AgentSession) -> ReviewLane:
    lane = session._lane
    assert lane is not None
    return lane


def board_of(session: AgentSession) -> TaskBoard:
    return session._board


def idle_session() -> tuple[AgentSession, ScriptedLauncher]:
    """A session with reviews on that has run nothing."""
    session, _, launcher = session_for(tree=FakeTree(), review=True)
    return session, launcher


async def suggesting() -> tuple[AgentSession, ScriptedLauncher]:
    """A session whose review of its one task suggested a task and is still running."""
    suggested = asyncio.Event()

    async def review(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("suggest_task", title="Add tests", prompt="Please add tests.")
        suggested.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, launcher = session_for(
        claims_done, reviewers=(review,), tree=FakeTree(), review=True
    )
    await start(session)
    await asyncio.wait_for(suggested.wait(), 10)
    return session, launcher


async def test_a_restored_lane_has_the_state_it_was_snapshotted_with() -> None:
    session, _ = await suggesting()
    snapshot = lane_of(session).snapshot()
    again, launcher = idle_session()
    board_of(again).restore(board_of(session).tasks)
    await lane_of(again).restore(LaneSnapshot.model_validate_json(snapshot.model_dump_json()))
    assert lane_of(again).snapshot() == snapshot
    assert snapshot.batches == {"2": "batch-1"}
    assert lane_of(again).queued_by_orbit("2")
    assert launcher.launches == []
    await session.close()


async def test_a_restored_lane_answers_suggestions_as_before() -> None:
    session, _ = await suggesting()
    snapshot = lane_of(session).snapshot()
    again, _ = idle_session()
    board_of(again).restore(board_of(session).tasks)
    await lane_of(again).restore(snapshot)
    for each in (session, again):
        repeat = await lane_of(each).suggest("2", "Add tests", "Again.")
        assert repeat == Reply("Task 3 already covers that.", is_error=True)
        assert await lane_of(each).suggest("2", "Rename x", "Please.") == Reply("Queued as task 4.")
    assert board_of(again).tasks == board_of(session).tasks
    await session.close()


def task(task_id: str, role: str, status: TaskStatus, origin: TaskOrigin = "manual") -> TaskState:
    return TaskState(
        id=task_id, task=TaskSpec(prompt=task_id, role=role), status=status, origin=origin
    )


def crashed(*suggestions: TaskStatus) -> tuple[TaskState, ...]:
    """A board left by a crash during the review of task 1, which suggested ``suggestions``."""
    tasks = [task("1", "coder", "completed"), task("2", "reviewer", "running", "review")]
    tasks += [
        task(str(n), "coder", status, "review").model_copy(update={"batch": "batch-1", "round": 1})
        for n, status in enumerate(suggestions, start=3)
    ]
    return interrupted(tasks)[0]


def reviewing(*suggestions: str, finished: frozenset[str] = frozenset()) -> LaneSnapshot:
    """The lane of :func:`crashed`, with ``finished`` of the suggestions landed."""
    batch = BatchSnapshot(
        round=1, cap=5, tasks=suggestions, finished=finished, commits=tuple(sorted(finished))
    )
    return LaneSnapshot(
        planner=PlannerSnapshot(next_batch=2, batches={"batch-1": batch}, rounds={"batch-1": 1}),
        jobs={"2": ReviewJob(commits=("c1",), round=0, batch=None)},
        batches={"2": "batch-1"},
    )


async def restored(
    tasks: tuple[TaskState, ...], snapshot: LaneSnapshot, *stopped: str
) -> tuple[AgentSession, ScriptedLauncher]:
    session, launcher = idle_session()
    board_of(session).restore(tasks)
    await lane_of(session).restore(snapshot, interrupted=stopped)
    return session, launcher


async def test_an_interrupted_review_that_suggested_nothing_closes_its_batch() -> None:
    session, _ = await restored(crashed(), reviewing(), "2")
    assert lane_of(session).planner.open_batches == ()
    refusal = await lane_of(session).suggest("2", "Add tests", "Please.")
    assert refusal == Reply(
        "This review's suggestions are closed: its batch is over.", is_error=True
    )


async def test_an_interrupted_review_whose_batch_closed_is_not_run_again() -> None:
    session, launcher = await restored(crashed(), reviewing(), "2")
    lane_of(session).kick()
    await asyncio.sleep(0.05)
    assert launcher.launches == []
    assert board_of(session).get("2").status == "cancelled"
    await session.close()


async def test_an_interrupted_reviews_batch_waits_for_its_work_without_running_it() -> None:
    session, launcher = await restored(crashed("pending"), reviewing("3"), "2")
    assert lane_of(session).snapshot().planner.batches["batch-1"].filling is False
    await asyncio.sleep(0.05)
    assert launcher.launches == []
    assert [t.status for t in board_of(session).tasks] == ["completed", "cancelled", "pending"]


async def test_an_interrupted_reviews_finished_batch_is_queued_for_review_but_not_run() -> None:
    tasks = crashed("completed")
    session, launcher = await restored(tasks, reviewing("3", finished=frozenset({"3"})), "2")
    await asyncio.sleep(0.05)
    assert launcher.launches == []
    added = board_of(session).tasks[-1]
    assert (added.id, added.task.role, added.status) == ("4", "reviewer", "pending")
    assert lane_of(session).snapshot().batches == {"2": "batch-1", "4": "batch-2"}


async def test_interrupted_tasks_that_are_not_open_reviews_change_nothing() -> None:
    snapshot = reviewing().model_copy(update={"planner": PlannerSnapshot(next_batch=2)})
    session, _ = await restored(crashed(), snapshot, "1", "2")
    assert lane_of(session).snapshot() == snapshot


async def test_restoring_publishes_the_restored_review_status() -> None:
    paused = reviewing().model_copy(update={"planner": PlannerSnapshot(paused=True)})
    session, _ = await restored(crashed(), paused)
    events = []
    while not session._events.empty():
        events.append(session._events.get_nowait())
    assert ReviewStatus(paused=True, open_batches=0) in events
