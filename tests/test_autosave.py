"""The session open in orbit is saved as it goes, once it's used, so a crash loses nothing."""

import asyncio

from orbit.agent import Verification, VerificationStep
from orbit.agent.domain.reviews import ReviewLimits
from orbit.protocol import (
    PLANNER,
    SAVE_FAILED,
    AddTask,
    EditTask,
    ErrorOccurred,
    Event,
    SendMessage,
    Settings,
    Snapshot,
    TasksChanged,
    TaskSpec,
    TurnEndReason,
    UpdateSettings,
)
from tests.agent_fakes import (
    START,
    Call,
    FakeStore,
    FakeTree,
    Steps,
    agent_exited,
    board,
    events_until,
    idle,
    lane_of,
    of_type,
    send,
    session_for,
)
from tests.test_review_lane import claims_done, settle, start, suggests

TASK = TaskSpec(prompt="Add a flag.")


async def chats(call: Call) -> TurnEndReason:
    return "completed"


async def proposes(call: Call) -> TurnEndReason:
    await call("propose_task", title="Add the flag", prompt="Add --fast to the CLI.")
    return "completed"


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


async def test_a_save_that_fails_is_reported_once_and_work_carries_on() -> None:
    store = FakeStore()
    store.failure = "Couldn't save the session: disk full"
    session, _, _ = session_for(store=store)
    await send(session, AddTask(task=TASK))
    await send(session, AddTask(task=TaskSpec(prompt="Another.")))
    events = await events_until(
        session, lambda e: isinstance(e, TasksChanged) and len(e.tasks) == 2
    )
    assert errors(events) == ["Couldn't save the session: disk full"]
    assert {e.code for e in events if isinstance(e, ErrorOccurred)} == {SAVE_FAILED}
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


async def test_without_reviews_the_saved_super_count_is_left_alone() -> None:
    store = FakeStore(count=3)
    session, _, _ = session_for(store=store)
    await send(session, AddTask(task=TASK))
    await events_until(session, of_type(TasksChanged))
    await session.close()
    assert store.counts == [] and store.last.reviews is None
