"""Switching sessions: listing the saved ones, opening one in place of the open session, and
starting a new one; refused while anything runs."""

import asyncio
from datetime import UTC, datetime
from typing import Any

from orbit.agent import Agents, AgentSession, Verification, VerificationStep
from orbit.agent.application.session import PLANNER_ANSWERING, REVIEW_RUNNING, TASKS_RUNNING
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.reviews import LaneSnapshot, PlannerSnapshot
from orbit.agent.domain.sessions import SessionRecord
from orbit.protocol import (
    AddTask,
    AgentExited,
    AgentStarted,
    ErrorOccurred,
    Event,
    ListSessions,
    NewSession,
    Notice,
    OpenSession,
    ReviewStatus,
    RunTasks,
    SessionOpened,
    SessionsListed,
    SessionSummary,
    Settings,
    SettingsChanged,
    SubmitPrompt,
    TasksChanged,
    TaskSpec,
    TaskState,
    TaskStatus,
    TurnEndReason,
)
from tests.agent_fakes import (
    ROLES,
    Call,
    FakeHost,
    FakeStore,
    FakeTree,
    Script,
    ScriptedLauncher,
    Steps,
    Ticks,
    agent_exited,
    events_until,
    idle,
    of_type,
    session_for,
    verified,
)
from tests.test_agent_run import keeps_claiming
from tests.test_review_lane import claims_done

YESTERDAY = datetime(2026, 10, 16, 18, 2, tzinfo=UTC)
EARLIER = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
TASK = TaskSpec(prompt="Add a flag.")


def state(task_id: str, status: TaskStatus) -> TaskState:
    return TaskState(id=task_id, task=TaskSpec(prompt=f"Task {task_id}."), status=status)


def saved(session_id: str, *tasks: TaskState, **fields: Any) -> SessionRecord:
    """A record saved as ``session_id`` holding ``tasks``; ``fields`` override the rest."""
    record = SessionRecord(
        id=session_id, name=session_id, created=EARLIER, last_active=YESTERDAY, tasks=tasks
    )
    return record.model_copy(update=fields)


def store_with(*records: SessionRecord) -> FakeStore:
    store = FakeStore()
    store.saved = list(records)
    return store


def switching(
    store: FakeStore, *scripts: Script, reviews: bool = True, **session: Any
) -> tuple[AgentSession, ScriptedLauncher]:
    """A session saved in ``store``, as ``session-1`` then ``session-2`` and so on, whose tasks
    run through agents playing ``scripts``, reviewing them if ``reviews``; ``session`` goes to
    :class:`AgentSession`."""
    host = FakeHost()
    launcher = ScriptedLauncher(host, *scripts)
    tokens = iter(f"token-{n}" for n in range(1, 100))
    tree = FakeTree() if reviews else None
    agents = Agents(
        host, launcher, lambda: next(tokens), ROLES, Limits(), tree=tree, review=reviews
    )
    ids = iter(f"session-{n}" for n in range(1, 100))
    built = AgentSession(
        agents=agents, store=store, clock=Ticks(), new_id=lambda: next(ids), **session
    )
    return built, launcher


def last_of[E](kind: type[E], events: list[Event]) -> E:
    return [e for e in events if isinstance(e, kind)][-1]


def messages(kind: type[Notice] | type[ErrorOccurred], events: list[Event]) -> list[str]:
    return [e.message for e in events if isinstance(e, kind)]


async def opened(session: AgentSession, command: OpenSession | NewSession) -> list[Event]:
    """The events from sending ``command`` until its session is opened, and its notes."""
    await session.send(command)
    events = await events_until(session, of_type(SessionOpened))
    await session.send(ListSessions())  # Marks the end of the notes that follow.
    return events + await events_until(session, of_type(SessionsListed))


async def refused(session: AgentSession, command: OpenSession | NewSession) -> list[str]:
    """The errors sending ``command`` reports, which ends in a refusal."""
    await session.send(command)
    await session.send(ListSessions())
    return messages(ErrorOccurred, await events_until(session, of_type(SessionsListed)))


async def chats(call: Call) -> TurnEndReason:
    return "completed"


async def hangs(call: Call) -> TurnEndReason:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


async def test_sessions_list_newest_first_marking_the_current_locked_and_unreadable() -> None:
    store = store_with(saved("old", last_active=EARLIER), saved("taken"))
    store.elsewhere.add("taken")
    store.broken["broken"] = "it isn't JSON"
    session, _ = switching(store)
    await session.send(AddTask(task=TASK))
    await session.send(ListSessions())
    listed = last_of(SessionsListed, await events_until(session, of_type(SessionsListed)))

    flags = [(s.id, s.current, s.locked, s.unreadable) for s in listed.sessions]
    assert flags == [
        ("session-1", True, False, None),
        ("taken", False, True, None),
        ("old", False, False, None),
        ("broken", False, False, "it isn't JSON"),
    ]
    await session.close()


async def test_without_a_store_no_sessions_are_listed() -> None:
    session = AgentSession()
    await session.send(ListSessions())
    listed = last_of(SessionsListed, await events_until(session, of_type(SessionsListed)))
    assert listed.sessions == ()
    await session.close()


async def test_sessions_are_listed_at_startup_for_the_user_to_pick_one() -> None:
    session, _ = switching(store_with(saved("old")), pick_session=True)
    listed = last_of(SessionsListed, await events_until(session, of_type(SessionsListed)))
    assert [s.id for s in listed.sessions] == ["old"]
    await session.close()


async def test_opening_a_session_restores_its_board_cancelling_what_was_running() -> None:
    record = saved(
        "auth",
        state("1", "completed"),
        state("2", "running"),
        state("3", "pending"),
        name="auth rework",
    )
    store = store_with(record)
    session, launcher = switching(store)
    events = await opened(session, OpenSession(session_id="auth"))

    board = last_of(TasksChanged, events).tasks
    assert [t.status for t in board] == ["completed", "cancelled", "pending"]
    assert messages(Notice, events) == [
        'Resumed "auth rework" (last active Oct 16 18:02)',
        "1 task was interrupted and is cancelled.",
    ]
    assert last_of(SessionOpened, events).session == SessionSummary(
        id="auth",
        name="auth rework",
        last_active=YESTERDAY,
        counts={"completed": 1, "cancelled": 1, "pending": 1},
        current=True,
    )
    assert launcher.launches == []  # Pending tasks wait to be run.
    assert store.locked == {"auth"}
    assert store.last.id == "auth" and store.last.tasks == board
    await session.close()


async def test_opening_a_session_without_interruptions_only_notes_its_resumption() -> None:
    session, _ = switching(store_with(saved("auth", state("1", "completed"))))
    events = await opened(session, OpenSession(session_id="auth"))
    assert messages(Notice, events) == ['Resumed "auth" (last active Oct 16 18:02)']
    await session.close()


async def test_an_opened_session_carries_on_its_planners_conversation() -> None:
    session, launcher = switching(store_with(saved("auth", planner_conversation="talk-9")))
    await opened(session, OpenSession(session_id="auth"))
    launcher.planner_turns = [chats]
    await session.send(SubmitPrompt(text="Where were we?"))
    await events_until(session, idle("planner"))
    assert launcher.resumed == ["talk-9"]
    await session.close()


async def test_an_opened_session_has_its_saved_settings() -> None:
    settings = Settings(model="opus", tool_access="full")
    session, _ = switching(store_with(saved("auth", settings=settings)))
    events = await opened(session, OpenSession(session_id="auth"))
    assert last_of(SettingsChanged, events).settings == settings
    assert session.settings == settings
    await session.close()


async def test_settings_given_on_the_command_line_override_the_saved_ones() -> None:
    record = saved("auth", settings=Settings(model="opus", tool_access="full"))
    session, _ = switching(store_with(record), settings=Settings(model="haiku"), pinned=("model",))
    await opened(session, OpenSession(session_id="auth"))
    assert session.settings == Settings(model="haiku", tool_access="full")
    await session.close()


async def test_a_session_saved_without_reviews_opens_with_none() -> None:
    paused = LaneSnapshot(planner=PlannerSnapshot(paused=True))
    store = store_with(saved("paused", reviews=paused), saved("auth"))
    session, _ = switching(store)
    await opened(session, OpenSession(session_id="paused"))
    assert store.last.reviews == paused
    events = await opened(session, OpenSession(session_id="auth"))
    assert store.last.reviews == LaneSnapshot(planner=PlannerSnapshot())
    assert last_of(ReviewStatus, events) == ReviewStatus(paused=False, open_batches=0)
    await session.close()


async def test_opening_another_session_hands_the_lock_over() -> None:
    store = store_with(saved("first"), saved("second"))
    session, _ = switching(store)
    await session.send(AddTask(task=TASK))
    await opened(session, OpenSession(session_id="first"))
    assert store.locked == {"first"} and store.released == ["session-1"]
    await opened(session, OpenSession(session_id="second"))
    assert store.locked == {"second"} and store.released == ["session-1", "first"]
    await session.close()
    assert store.locked == set()


async def test_opening_the_current_session_does_nothing() -> None:
    store = FakeStore()
    session, _ = switching(store)
    await session.send(AddTask(task=TASK))
    assert await refused(session, OpenSession(session_id="session-1")) == []
    assert store.released == [] and store.locked == {"session-1"}
    await session.close()


async def test_a_session_open_in_another_orbit_is_refused() -> None:
    store = store_with(saved("auth", name="auth rework"))
    store.elsewhere.add("auth")
    session, _ = switching(store)
    errors = await refused(session, OpenSession(session_id="auth"))
    assert errors == ["Can't open \"auth rework\": it's open in another orbit."]
    await session.close()


async def test_a_session_locked_as_it_is_opened_is_refused() -> None:
    store = store_with(saved("auth"))
    store.free = False
    session, _ = switching(store)
    errors = await refused(session, OpenSession(session_id="auth"))
    assert errors == ["Can't open \"auth\": it's open in another orbit."]
    await session.close()


async def test_an_unreadable_session_is_refused() -> None:
    store = FakeStore()
    store.broken["broken"] = "it isn't JSON"
    session, _ = switching(store)
    errors = await refused(session, OpenSession(session_id="broken"))
    assert errors == ["Can't open the session broken: it isn't JSON."]
    await session.close()


async def test_a_missing_session_is_refused() -> None:
    session, _ = switching(FakeStore())
    errors = await refused(session, OpenSession(session_id="gone"))
    assert errors == ["There's no saved session 'gone'."]
    await session.close()


async def test_a_store_that_cant_lock_the_session_leaves_the_open_one_as_it_was() -> None:
    store = store_with(saved("auth", state("1", "pending")))
    session, _ = switching(store)
    await session.send(AddTask(task=TASK))
    store.lock_failure = "Couldn't lock the session: disk full"
    assert await refused(session, OpenSession(session_id="auth")) == [
        "Couldn't lock the session: disk full"
    ]
    assert session.saved_session.id == "session-1"
    assert [t.task for t in session._board.tasks] == [TASK]
    await session.close()


async def test_switching_is_refused_while_tasks_run() -> None:
    store = store_with(saved("auth"))
    session, _ = switching(store, hangs)
    await session.send(AddTask(task=TASK))
    await session.send(RunTasks())
    await events_until(session, of_type(AgentStarted))
    assert await refused(session, OpenSession(session_id="auth")) == [TASKS_RUNNING]
    assert await refused(session, NewSession()) == [TASKS_RUNNING]
    assert store.locked == {"session-1"} and store.released == []
    assert session.saved_session.id == "session-1"
    await session.close()


async def test_switching_is_refused_while_a_review_runs() -> None:
    store = store_with(saved("auth"))
    session, _, _ = session_for(
        claims_done, reviewers=(hangs,), tree=FakeTree(), review=True, store=store
    )
    await session.send(AddTask(task=TASK))
    await session.send(RunTasks())
    await events_until(
        session, lambda e: isinstance(e, AgentStarted) and e.role == "reviewer", timeout=10
    )
    while session._work_turns.running:
        await asyncio.sleep(0.01)
    assert await refused(session, OpenSession(session_id="auth")) == [REVIEW_RUNNING]
    await session.close()


async def test_switching_is_refused_while_the_planner_answers() -> None:
    session, launcher = switching(store_with(saved("auth")))
    launcher.planner_turns = [hangs]
    await session.send(SubmitPrompt(text="Plan it."))
    await events_until(session, of_type(AgentStarted))
    assert await refused(session, OpenSession(session_id="auth")) == [PLANNER_ANSWERING]
    assert await refused(session, NewSession(name="other")) == [PLANNER_ANSWERING]
    assert session.saved_session.name != "other"
    await session.close()


async def test_work_started_while_a_session_is_locked_to_open_refuses_the_switch() -> None:
    store = store_with(saved("auth"))
    session, _ = switching(store, hangs)
    await session.send(AddTask(task=TASK))

    async def run() -> None:
        store.on_acquire = None
        await session.send(RunTasks())

    store.on_acquire = run
    assert await refused(session, OpenSession(session_id="auth")) == [TASKS_RUNNING]
    assert store.locked == {"session-1"} and store.released == ["auth"]
    await session.close()


async def test_a_new_session_starts_empty_with_its_name() -> None:
    store = FakeStore()
    session, _ = switching(store)
    await session.send(AddTask(task=TASK))
    events = await opened(session, NewSession(name="auth rework"))

    assert last_of(TasksChanged, events).tasks == ()
    assert last_of(SessionOpened, events).session == SessionSummary(
        id="session-2", name="auth rework", current=True
    )
    assert store.released == ["session-1"]
    assert [r.id for r in store.saved] == ["session-1"]  # Not saved until it's used.
    await session.send(AddTask(task=TASK))
    assert store.last.id == "session-2" and store.last.name == "auth rework"
    assert store.locked == {"session-2"}
    await session.close()


async def test_a_new_session_without_a_name_is_named_by_its_start() -> None:
    session, _ = switching(FakeStore())
    for name, named in ((None, "Oct 17 14:31"), ("  ", "Oct 17 14:32")):
        events = await opened(session, NewSession(name=name))
        assert last_of(SessionOpened, events).session.name == named
    await session.close()


async def test_a_new_session_has_a_fresh_planner_conversation() -> None:
    session, launcher = switching(store_with(saved("auth")))
    launcher.planner_turns = [chats, chats]
    await session.send(SubmitPrompt(text="Hello."))
    await events_until(session, idle("planner"))
    events = await opened(session, NewSession())
    await session.send(SubmitPrompt(text="Hello again."))
    await events_until(session, idle("planner"))
    assert launcher.resumed == [None, None]
    assert any(isinstance(e, AgentExited) for e in events)
    await session.close()


async def with_a_chain_out_of_fixes(store: FakeStore, *scripts: Script) -> AgentSession:
    """A session whose task 1 failed verification until its fix, task 2, ran out of fixes,
    whose later tasks run through agents playing ``scripts``."""
    always_fails = Steps(*[False] * 40)
    session, _ = switching(
        store,
        keeps_claiming,
        keeps_claiming,
        *scripts,
        reviews=False,
        verification=verified(always_fails, fallback_fixes=1),
    )
    await session.send(AddTask(task=TASK))
    await session.send(RunTasks())
    await events_until(session, idle())
    return session


async def ran(session: AgentSession, *tasks: TaskSpec) -> tuple[TaskState, ...]:
    """The board once ``tasks`` were added and ran."""
    for task in tasks:
        await session.send(AddTask(task=task))
    await session.send(RunTasks())
    return last_of(TasksChanged, await events_until(session, idle())).tasks


async def test_a_session_opened_keeps_no_fix_counts_from_the_one_before() -> None:
    store = store_with(saved("auth", state("1", "completed")))
    session = await with_a_chain_out_of_fixes(store, keeps_claiming, keeps_claiming)
    await opened(session, OpenSession(session_id="auth"))
    board = await ran(session, TASK)
    assert [(t.id, t.status, t.fixes) for t in board] == [
        ("1", "completed", None),
        ("2", "unverified", None),
        ("3", "unverified", "2"),
    ]
    await session.close()


async def test_a_new_session_keeps_no_fix_counts_from_the_one_before() -> None:
    session = await with_a_chain_out_of_fixes(
        FakeStore(), claims_done, keeps_claiming, keeps_claiming
    )
    await opened(session, NewSession())
    unchecked = TaskSpec(prompt="Look around.", settings=Settings(tool_access="off"))
    board = await ran(session, unchecked, TASK)
    assert [(t.id, t.status, t.fixes) for t in board] == [
        ("1", "completed", None),
        ("2", "unverified", None),
        ("3", "unverified", "2"),
    ]
    await session.close()


def failing_startup(store: FakeStore) -> AgentSession:
    """A session for the user to pick, whose startup check fails, adding a draft to fix it."""
    lint = VerificationStep(name="Lint", command=("ruff", "check"))
    session, _ = switching(
        store,
        reviews=False,
        verification=Verification((lint,), Steps(False)),
        check_at_startup=True,
        pick_session=True,
    )
    return session


async def test_tasks_orbit_added_before_a_session_was_picked_carry_over_into_it() -> None:
    store = store_with(saved("auth", state("4", "completed")))
    session = failing_startup(store)
    await events_until(session, agent_exited("startup-check"))
    events = await opened(session, OpenSession(session_id="auth"))

    board = last_of(TasksChanged, events).tasks
    assert [(t.id, t.status) for t in board] == [("4", "completed"), ("5", "draft")]
    assert store.deleted == ["session-1"]
    assert store.released == []
    await session.close()


async def test_tasks_orbit_added_carry_over_into_a_new_session_picked() -> None:
    store = FakeStore()
    session = failing_startup(store)
    await events_until(session, agent_exited("startup-check"))
    events = await opened(session, NewSession(name="fresh"))
    assert [t.status for t in last_of(TasksChanged, events).tasks] == ["draft"]
    assert store.last.id == "session-2" and store.deleted == ["session-1"]
    await session.close()


async def test_a_session_that_cant_be_deleted_as_its_tasks_carry_over_is_reported() -> None:
    store = FakeStore()
    store.delete_failure = "Couldn't delete the session: read-only"
    session = failing_startup(store)
    await events_until(session, agent_exited("startup-check"))
    events = await opened(session, NewSession())
    assert messages(ErrorOccurred, events) == ["Couldn't delete the session: read-only"]
    await session.close()


async def test_once_the_user_works_in_the_session_its_tasks_stay_with_it() -> None:
    store = store_with(saved("auth"))
    session = failing_startup(store)
    await events_until(session, agent_exited("startup-check"))
    await session.send(AddTask(task=TASK))
    events = await opened(session, OpenSession(session_id="auth"))
    assert last_of(TasksChanged, events).tasks == ()
    assert store.deleted == [] and store.released == ["session-1"]
    await session.close()


async def test_a_switch_whose_old_lock_cant_be_released_still_opens_the_session() -> None:
    store = store_with(saved("auth"))
    session, _ = switching(store)
    await session.send(AddTask(task=TASK))
    await events_until(session, of_type(TasksChanged))
    store.release_failure = "Couldn't unlock the session: read-only"
    events = await opened(session, OpenSession(session_id="auth"))
    assert last_of(SessionOpened, events).session.id == "auth"
    assert messages(ErrorOccurred, events) == ["Couldn't unlock the session: read-only"]
    await session.close()


async def test_a_session_without_agents_opens_its_board_and_settings() -> None:
    settings = Settings(model="opus")
    store = store_with(saved("auth", state("1", "pending"), settings=settings))
    session = AgentSession(store=store, clock=Ticks())
    events = await opened(session, OpenSession(session_id="auth"))
    assert [t.id for t in last_of(TasksChanged, events).tasks] == ["1"]
    assert session.settings == settings
    await opened(session, NewSession())
    assert session._board.tasks == ()
    await session.close()


async def test_a_session_without_reviews_keeps_the_reviews_of_the_session_it_opens() -> None:
    reviews = LaneSnapshot(planner=PlannerSnapshot(paused=True))
    store = store_with(saved("auth", reviews=reviews))
    session, _ = switching(store, reviews=False)
    await opened(session, OpenSession(session_id="auth"))
    await session.send(AddTask(task=TASK))
    await events_until(session, of_type(TasksChanged))
    assert store.last.id == "auth" and store.last.reviews == reviews
    await opened(session, NewSession())
    await session.send(AddTask(task=TASK))
    await events_until(session, of_type(TasksChanged))
    assert store.last.id != "auth" and store.last.reviews is None
    await session.close()


async def test_a_session_without_a_planner_keeps_the_conversation_of_the_session_it_opens() -> None:
    store = store_with(saved("auth", state("1", "pending"), planner_conversation="conv-1"))
    session = AgentSession(store=store, clock=Ticks())
    await opened(session, OpenSession(session_id="auth"))
    assert store.last.id == "auth" and store.last.planner_conversation == "conv-1"
    await opened(session, NewSession())
    await session.send(AddTask(task=TASK))
    await events_until(session, of_type(TasksChanged))
    assert store.last.id != "auth" and store.last.planner_conversation is None
    await session.close()
