"""Snapshots of the review lane: restored as they were when a saved session is opened, with
the reviews its crash interrupted ended."""

import asyncio

from orbit.agent import AgentSession
from orbit.agent.domain.reviews import BatchSnapshot, LaneSnapshot, PlannerSnapshot, ReviewJob
from orbit.agent.domain.sessions import SessionRecord
from orbit.protocol import (
    ActivateSession,
    CommandAccepted,
    Event,
    Notice,
    ReviewStatus,
    RunTasks,
    SessionsListed,
    Snapshot,
    TaskOrigin,
    TasksChanged,
    TaskSpec,
    TaskState,
    TaskStatus,
    TurnEndReason,
)
from tests.agent_fakes import (
    Call,
    FakeStore,
    FakeTree,
    ScriptedLauncher,
    events_until,
    of_type,
    send,
    session_for,
)
from tests.test_review_lane import claims_done, start
from tests.test_session_switch import last_of, messages, opened, saved, store_with, switching


async def suggesting() -> tuple[AgentSession, FakeStore]:
    """A session saved in its store whose review of its one task suggested a task and is
    still running."""
    suggested = asyncio.Event()

    async def review(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("suggest_task", title="Add tests", prompt="Please add tests.")
        suggested.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    store = FakeStore()
    session, _, _ = session_for(
        claims_done, reviewers=(review,), tree=FakeTree(), review=True, store=store
    )
    await start(session)
    await asyncio.wait_for(suggested.wait(), 10)
    return session, store


def from_disk(record: SessionRecord) -> SessionRecord:
    """``record`` as it's read back after being written."""
    return SessionRecord.model_validate_json(record.model_dump_json())


def reviews_of(store: FakeStore) -> LaneSnapshot:
    """The review state last saved in ``store``."""
    reviews = store.last.reviews
    assert reviews is not None
    return reviews


async def test_an_opened_session_has_the_reviews_it_was_saved_with() -> None:
    crashing, kept = await suggesting()
    record = from_disk(kept.last.model_copy(update={"id": "auth"}))
    snapshot = reviews_of(kept)
    assert snapshot.batches == {"2": "batch-1"}
    store = store_with(record)
    session, launcher = switching(store)
    events = await opened(session, ActivateSession(session="auth"))

    # The running review was interrupted, so its batch no longer fills.
    batch = snapshot.planner.batches["batch-1"].model_copy(update={"filling": False})
    planner = snapshot.planner.model_copy(update={"batches": {"batch-1": batch}})
    assert reviews_of(store) == snapshot.model_copy(update={"planner": planner})
    assert last_of(Snapshot, events).review == ReviewStatus(paused=False, open_batches=1)
    assert launcher.launches == []
    await crashing.close()
    await session.close()


def task(task_id: str, role: str, status: TaskStatus, origin: TaskOrigin = "manual") -> TaskState:
    return TaskState(
        id=task_id, task=TaskSpec(prompt=task_id, role=role), status=status, origin=origin
    )


def crashed(*suggestions: TaskStatus) -> tuple[TaskState, ...]:
    """A board saved as a crash came during the review of task 1, which suggested
    ``suggestions``."""
    tasks = [task("1", "coder", "completed"), task("2", "reviewer", "running", "review")]
    tasks += [
        task(str(n), "coder", status, "review").model_copy(update={"batch": "batch-1", "round": 1})
        for n, status in enumerate(suggestions, start=3)
    ]
    return tuple(tasks)


def reviewing(*suggestions: str, finished: frozenset[str] = frozenset()) -> LaneSnapshot:
    """The lane of :func:`crashed`, with ``finished`` of the suggestions landed."""
    batch = BatchSnapshot(
        round=1, cap=5, tasks=suggestions, finished=finished, commits=tuple(sorted(finished))
    )
    return LaneSnapshot(
        planner=PlannerSnapshot(next_batch=2, batches={"batch-1": batch}),
        jobs={"2": ReviewJob(commits=("c1",), round=0, batch=None)},
        batches={"2": "batch-1"},
    )


async def reopened(
    tasks: tuple[TaskState, ...], snapshot: LaneSnapshot
) -> tuple[AgentSession, ScriptedLauncher, FakeStore, list[Event]]:
    """A session that opened the session saved with ``tasks`` and ``snapshot``, its store and
    the events from opening it."""
    store = store_with(saved("auth", *tasks, reviews=snapshot))
    session, launcher = switching(store)
    events = await opened(session, ActivateSession(session="auth"))
    return session, launcher, store, events


async def switched(
    tasks: tuple[TaskState, ...], snapshot: LaneSnapshot
) -> tuple[AgentSession, list[Event]]:
    """A session that started, then opened the session saved with ``tasks`` and ``snapshot``,
    and the events from opening it alone."""
    session, _ = switching(store_with(saved("auth", *tasks, reviews=snapshot)))
    await events_until(session, of_type(Snapshot))  # The one after the Welcome.
    return session, await opened(session, ActivateSession(session="auth"))


def statuses(events: list[Event]) -> list[TaskStatus]:
    return [t.status for t in last_of(Snapshot, events).tasks]


async def test_an_interrupted_review_that_suggested_nothing_closes_its_batch() -> None:
    session, _, store, events = await reopened(crashed(), reviewing())
    assert reviews_of(store).planner.batches == {}
    assert last_of(Snapshot, events).review == ReviewStatus(paused=False, open_batches=0)
    assert statuses(events) == ["completed", "cancelled"]
    await session.close()


async def test_an_interrupted_review_whose_batch_closed_is_not_run_again() -> None:
    session, launcher, store, _ = await reopened(crashed(), reviewing())
    await send(session, RunTasks())
    await asyncio.sleep(0.05)
    assert launcher.launches == []
    assert [t.status for t in store.last.tasks] == ["completed", "cancelled"]
    await session.close()


async def test_an_interrupted_reviews_batch_waits_for_its_work_without_running_it() -> None:
    session, launcher, store, events = await reopened(crashed("pending"), reviewing("3"))
    assert reviews_of(store).planner.batches["batch-1"].filling is False
    assert last_of(Snapshot, events).review == ReviewStatus(paused=False, open_batches=1)
    await asyncio.sleep(0.05)
    assert launcher.launches == []
    assert statuses(events) == ["completed", "cancelled", "pending"]
    await session.close()


async def test_an_interrupted_reviews_finished_batch_is_queued_for_review_but_not_run() -> None:
    snapshot = reviewing("3", finished=frozenset({"3"}))
    session, launcher, store, events = await reopened(crashed("completed"), snapshot)
    await asyncio.sleep(0.05)
    assert launcher.launches == []
    added = last_of(Snapshot, events).tasks[-1]
    assert (added.id, added.task.role, added.status) == ("4", "reviewer", "pending")
    assert reviews_of(store).batches == {"2": "batch-1", "4": "batch-2"}
    await session.close()


async def test_an_interrupted_reviews_queued_review_is_published_only_in_the_snapshot() -> None:
    snapshot = reviewing("3", finished=frozenset({"3"}))
    session, events = await switched(crashed("completed"), snapshot)
    before = events[: events.index(last_of(Snapshot, events))]
    assert not [e for e in before if isinstance(e, TasksChanged | ReviewStatus)]
    assert last_of(Snapshot, events).review == ReviewStatus(paused=False, open_batches=1)
    await session.close()


async def test_interrupted_tasks_that_are_not_open_reviews_change_nothing() -> None:
    snapshot = reviewing().model_copy(update={"planner": PlannerSnapshot(next_batch=2)})
    tasks = (task("1", "coder", "running"), *crashed()[1:])
    session, _, store, events = await reopened(tasks, snapshot)
    assert statuses(events) == ["cancelled", "cancelled"]
    assert reviews_of(store) == snapshot
    await session.close()


async def test_opening_snapshots_the_saved_review_status_without_announcing_it() -> None:
    paused = reviewing().model_copy(update={"planner": PlannerSnapshot(paused=True)})
    session, _, store, events = await reopened(crashed()[:1], paused)
    assert last_of(Snapshot, events).review == ReviewStatus(paused=True, open_batches=0)
    assert not [e for e in events if isinstance(e, ReviewStatus)]
    assert reviews_of(store) == paused
    await session.close()


def pause_notes(events: list[Event]) -> list[str]:
    return [m for m in messages(Notice, events) if m.startswith("Review paused")]


async def test_each_paused_session_opened_notes_its_pause_once() -> None:
    paused = reviewing().model_copy(update={"planner": PlannerSnapshot(paused=True)})
    first = saved("auth", *crashed()[:1], reviews=paused)
    store = store_with(first, first.model_copy(update={"id": "docs", "name": "docs"}))
    session, _ = switching(store)
    assert len(pause_notes(await opened(session, ActivateSession(session="auth")))) == 1
    assert len(pause_notes(await opened(session, ActivateSession(session="docs")))) == 1
    await session.close()


async def test_opening_a_paused_session_notes_its_pause_after_the_snapshot() -> None:
    paused = reviewing().model_copy(update={"planner": PlannerSnapshot(paused=True)})
    session, events = await switched(crashed(), paused)
    assert [type(e) for e in events] == [
        Snapshot,
        Notice,  # The pause.
        Notice,  # The resumption.
        Notice,  # The interruption.
        CommandAccepted,
        SessionsListed,
    ]
    assert pause_notes(events) == [messages(Notice, events)[0]]
    await session.close()


async def test_opening_a_session_whose_review_is_not_paused_notes_no_pause() -> None:
    session, _, _, events = await reopened(crashed(), reviewing())
    assert pause_notes(events) == []
    await session.close()
