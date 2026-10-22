"""The session open in orbit is saved as it goes, once it's used, so a crash loses nothing."""

import asyncio

from orbit.agent import Verification, VerificationStep, WorkspaceHost
from orbit.agent.application.ports import StoreFailed
from orbit.agent.domain.reviews import ReviewLimits
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.protocol import (
    PLANNER,
    STORAGE_FAILED,
    AddTask,
    CommandEnvelope,
    EditTask,
    ErrorOccurred,
    Event,
    Notice,
    ResumeStorage,
    SendMessage,
    Settings,
    Snapshot,
    TasksChanged,
    TaskSpec,
    TurnEndReason,
    UpdateSettings,
)
from tests.agent_fakes import (
    ACCEPTED,
    HELLO,
    START,
    Call,
    FakeStore,
    FakeTree,
    Steps,
    agent_exited,
    attach_to,
    board,
    chats,
    claims_done,
    connect,
    events_until,
    idle,
    lane_of,
    of_type,
    outcome_of,
    send,
    session_for,
    settle,
    start,
    suggests,
)

TASK = TaskSpec(prompt="Add a flag.")


async def proposes(call: Call) -> TurnEndReason:
    await call("propose_task", title="Add the flag", prompt="Add --fast to the CLI.")
    return "completed"


def storage_failed(event: Event) -> bool:
    return isinstance(event, ErrorOccurred) and event.code == STORAGE_FAILED


def errors(events: list[Event]) -> list[str]:
    return [e.message for e in events if isinstance(e, ErrorOccurred)]


async def test_a_session_nobody_used_leaves_nothing_saved() -> None:
    store = FakeStore()
    session, _, _ = session_for(store=store)
    await events_until(session, of_type(Snapshot))
    await send(session, UpdateSettings(settings=Settings(model="opus")))
    await session.close()
    assert store.saved == [] and store.locked == set() and store.released == []


async def test_adding_a_task_saves_the_session_locked_until_it_closes() -> None:
    store = FakeStore()
    session, _, _ = session_for(store=store)
    await send(session, AddTask(task=TASK))

    record = store.last
    assert (record.id, record.name, record.created) == ("session-1", "Oct 17 14:30", START)
    assert [t.task for t in record.tasks] == [TASK]
    assert record.last_active > record.created
    assert store.locked == {"session-1"}
    await session.close()
    assert store.locked == set() and store.released == ["session-1"]


async def test_each_board_change_is_saved_with_a_later_last_active() -> None:
    store = FakeStore()
    session, _, _ = session_for(store=store)
    await send(session, AddTask(task=TASK))
    await send(session, EditTask(task_id="1", task=TaskSpec(prompt="Add two flags.")))

    first, second = store.saved
    assert [t.task.prompt for t in second.tasks] == ["Add two flags."]
    assert second.last_active > first.last_active
    await session.close()


async def test_a_prompt_saves_the_session_then_its_planners_conversation() -> None:
    store = FakeStore()
    session, _, launcher = session_for(store=store)
    launcher.planner_turns = [chats]
    await send(session, SendMessage(agent=PLANNER, text="hello"))
    assert [(r.tasks, r.planner_conversation) for r in store.saved] == [((), None)]

    await events_until(session, idle("planner"))
    assert store.last.planner_conversation == "conversation-agent-1"
    await session.close()


async def test_a_planners_draft_saves_the_session() -> None:
    store = FakeStore()
    session, _, launcher = session_for(store=store)
    launcher.planner_turns = [proposes]
    await send(session, SendMessage(agent=PLANNER, text="Plan a flag."))
    await events_until(session, idle("planner"))
    assert any(t.status == "draft" for t in store.last.tasks)
    await session.close()


async def test_the_startup_checks_fix_draft_saves_the_session() -> None:
    store = FakeStore()
    lint = VerificationStep(name="Lint", command=("ruff", "check"))
    session, _, _ = session_for(
        verification=Verification((lint,), Steps(False)),
        tree=FakeTree(),
        check_at_startup=True,
        store=store,
    )
    await events_until(session, agent_exited("startup-check"))
    [draft] = store.last.tasks
    assert draft.status == "draft"
    await session.close()


async def test_a_settings_change_is_saved_once_the_session_is_used() -> None:
    store = FakeStore()
    session, _, _ = session_for(store=store)
    await send(session, AddTask(task=TASK))
    await send(session, UpdateSettings(settings=Settings(model="opus", tool_access="full")))
    assert store.last.settings == Settings(model="opus", tool_access="full")
    await session.close()


async def test_a_save_that_fails_halts_the_session_and_nothing_more_is_saved() -> None:
    store = FakeStore()
    store.failure = "Couldn't save the session: disk full"
    session, _, _ = session_for(store=store)
    await send(session, AddTask(task=TASK))
    events = await events_until(session, of_type(ErrorOccurred))
    assert [e.code for e in events if isinstance(e, ErrorOccurred)] == [STORAGE_FAILED]
    assert "Couldn't save the session: disk full" in errors(events)[0]
    store.failure = None
    await send(session, AddTask(task=TaskSpec(prompt="Another.")))
    assert store.saved == []
    await session.close()


async def test_the_review_state_and_the_repositorys_super_count_are_saved() -> None:
    store = FakeStore(count=1)
    session, _, _ = session_for(
        claims_done,
        reviewers=(suggests(), suggests()),
        tree=FakeTree(),
        review=True,
        review_limits=ReviewLimits(super_every=2),
        store=store,
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)

    # Counting on from the saved 1, the first landing made a super review due.
    assert "super-reviewer" in [t.task.role for t in board(events)]
    assert store.counts == [0]
    assert store.last.reviews == lane_of(session).snapshot()
    assert store.last.reviews is not None and store.last.reviews.jobs
    await session.close()


async def test_a_super_count_that_cannot_be_read_is_not_overwritten_by_a_landing() -> None:
    store = FakeStore(count=7)
    store.count_load_failure = "Couldn't read the review count from orbit.db: busy"
    session, _, _ = session_for(
        claims_done,
        reviewers=(suggests(), suggests()),
        tree=FakeTree(),
        review=True,
        review_limits=ReviewLimits(super_every=2),
        store=store,
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)

    assert "super-reviewer" not in [t.task.role for t in board(events)]
    assert store.counts == [] and store.count == 7
    assert store.last.reviews is not None and store.last.reviews.jobs
    await session.close()


async def test_without_reviews_the_saved_super_count_is_left_alone() -> None:
    store = FakeStore(count=3)
    session, _, _ = session_for(store=store)
    await send(session, AddTask(task=TASK))
    await events_until(session, of_type(TasksChanged))
    await session.close()
    assert store.counts == [] and store.last.reviews is None


LEFTOVER = "Leftover work."


def reviewed(store: FakeStore) -> WorkspaceHost:
    """A session with reviews, saved in ``store``, whose tree has :data:`LEFTOVER` to report
    last as a connection joins."""
    session, _, _ = session_for(tree=FakeTree(leftovers=(LEFTOVER,)), review=True, store=store)
    return session


async def joined(session: WorkspaceHost) -> None:
    """Have a new connection join ``session``, until it's been greeted to the end."""
    connection = connect(session)
    await connection.send(CommandEnvelope(command_id="join", payload=HELLO))
    await connection.send(CommandEnvelope(command_id="attach", payload=attach_to(session)))
    async for envelope in connection.events():
        if envelope.payload == Notice(message=LEFTOVER):
            return


async def test_the_super_count_is_loaded_from_the_store_on_the_first_join() -> None:
    session = reviewed(FakeStore(count=7))
    await asyncio.wait_for(joined(session), 5)

    assert lane_of(session).planner.since_super == 7
    await session.close()


async def test_a_join_while_halted_keeps_the_super_count_the_store_has_not_saved() -> None:
    store = FakeStore(count=7)
    session = reviewed(store)
    await asyncio.wait_for(joined(session), 5)
    session._halt.fail(FailedWrite.EVENT_LOG, StoreFailed("disk full"))
    lane_of(session).planner.since_super = 8
    await session._review_count.save(8)
    await asyncio.wait_for(joined(session), 5)

    assert lane_of(session).planner.since_super == 8 and store.count == 7
    await session.close()


async def test_a_join_while_the_super_count_is_saved_keeps_the_count_being_saved() -> None:
    store = FakeStore(count=7)
    store.count_yields = [50]
    session = reviewed(store)
    await asyncio.wait_for(joined(session), 5)
    lane_of(session).planner.since_super = 8
    saving = asyncio.create_task(session._review_count.save(8))
    await asyncio.sleep(0)
    await asyncio.wait_for(joined(session), 5)

    assert lane_of(session).planner.since_super == 8
    await saving
    assert store.count == 8
    await session.close()


async def test_a_join_while_the_super_count_cannot_be_saved_keeps_it_to_save_on_resuming() -> None:
    store = FakeStore(count=7)
    session = reviewed(store)
    await asyncio.wait_for(joined(session), 5)
    store.count_failure = "disk full"
    lane_of(session).planner.since_super = 8
    await session._review_count.save(8)
    await events_until(session, storage_failed)
    await asyncio.wait_for(joined(session), 5)
    assert lane_of(session).planner.since_super == 8 and store.count == 7

    store.count_failure = None
    assert (await outcome_of(session, ResumeStorage()))[-1].payload == ACCEPTED
    lane_of(session).planner.since_super += 1
    await session._review_count.save(lane_of(session).planner.since_super)
    assert store.counts == [8, 9]
    await session.close()


class MovingStore(FakeStore):
    """A store whose count, as it's read, sees the planner of ``session`` count on to 9."""

    session: WorkspaceHost | None = None

    async def review_count(self) -> int:
        assert self.session is not None
        lane_of(self.session).planner.since_super = 9
        return await super().review_count()


async def test_a_super_count_moved_while_it_was_read_is_kept() -> None:
    store = MovingStore(count=7)
    store.session = reviewed(store)
    await asyncio.wait_for(joined(store.session), 5)

    assert lane_of(store.session).planner.since_super == 9
    await store.session.close()


async def test_a_super_count_that_could_not_be_read_is_read_on_the_next_join() -> None:
    store = FakeStore(count=7)
    store.count_load_failure = "busy"
    session = reviewed(store)
    await asyncio.wait_for(joined(session), 5)
    store.count_load_failure = None
    await asyncio.wait_for(joined(session), 5)

    assert lane_of(session).planner.since_super == 7
    await session.close()
