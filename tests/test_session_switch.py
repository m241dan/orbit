"""Switching sessions: listing the saved ones, opening one in place of the open session, and
starting a new one; refused while anything runs."""

import asyncio
from typing import Any

from orbit.agent import Agents, WorkspaceHost
from orbit.agent.domain.activity import PLANNER_ANSWERING, REVIEW_RUNNING, TASKS_RUNNING
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.reviews import LaneSnapshot, PlannerSnapshot
from orbit.agent.domain.sessions import SessionRecord
from orbit.protocol import (
    BUSY,
    INVALID,
    LOCKED,
    PLANNER,
    SAVE_FAILED,
    UNKNOWN,
    ActivateSession,
    AddTask,
    AgentExited,
    AgentStarted,
    CancelTurn,
    CommandAccepted,
    CommandRefused,
    ErrorOccurred,
    Event,
    HistoryPage,
    NewSession,
    Notice,
    RemoveTask,
    ReviewStatus,
    RunTasks,
    SendMessage,
    SessionSummary,
    Settings,
    Snapshot,
    TasksChanged,
    TaskSpec,
    TaskState,
    TaskStatus,
    TurnFinished,
    Usage,
    VerificationChanged,
)
from tests.agent_fakes import (
    ROLES,
    FakeHost,
    FakeStore,
    FakeTree,
    Script,
    ScriptedLauncher,
    Steps,
    Stop,
    Ticks,
    agent_exited,
    all_of,
    board,
    chats,
    claims_done,
    events_until,
    hangs,
    idle,
    keeps_claiming,
    listed,
    of_type,
    send,
    session_for,
    until_marked,
    verified,
)
from tests.server_fakes import TASK
from tests.session_builders import (
    EARLIER,
    YESTERDAY,
    failing_startup,
    last_of,
    messages,
    opened,
    saved,
    store_with,
    switching,
)


def state(task_id: str, status: TaskStatus) -> TaskState:
    return TaskState(id=task_id, task=TaskSpec(prompt=f"Task {task_id}."), status=status)


def errors(events: list[Event]) -> list[tuple[str, str]]:
    return [(e.code, e.message) for e in events if isinstance(e, ErrorOccurred)]


async def refused(
    session: WorkspaceHost, command: ActivateSession | NewSession
) -> list[tuple[str, str]]:
    """The codes and messages ``command`` is refused with, reporting no error."""
    await send(session, command)
    events = await until_marked(session)
    assert messages(ErrorOccurred, events) == []
    return refusals(events)


def refusals(events: list[Event]) -> list[tuple[str, str]]:
    """The codes and messages of the refusals among ``events``."""
    return [(e.code, e.message) for e in events if isinstance(e, CommandRefused)]


async def hangs_after() -> None:
    await asyncio.Event().wait()


async def test_sessions_list_newest_first_marking_the_current_locked_and_unreadable() -> None:
    store = store_with(saved("old", last_active=EARLIER), saved("taken"))
    store.elsewhere.add("taken")
    store.broken["broken"] = "it isn't JSON"
    session, _ = switching(store)
    await send(session, AddTask(task=TASK))
    await until_marked(session)

    flags = [(s.id, s.current, s.locked, s.unreadable) for s in await listed(session)]
    assert flags == [
        ("session-1", True, False, None),
        ("taken", False, True, None),
        ("old", False, False, None),
        ("broken", False, False, "it isn't JSON"),
    ]
    await session.close()


async def test_without_a_store_no_sessions_are_listed() -> None:
    session = WorkspaceHost()
    assert await listed(session) == ()
    await session.close()


LEFTOVER = "2 commits on orbit/coder not yet merged into main."


def starting(**session: Any) -> WorkspaceHost:
    """A session started without the list of sessions, whose coder's tree has a leftover;
    ``session`` goes to :class:`WorkspaceHost`."""
    host = FakeHost()
    tree = FakeTree(leftovers=(LEFTOVER,))
    agents = Agents(host, ScriptedLauncher(host), lambda: "token", ROLES, Limits(), tree=tree)
    return WorkspaceHost(agents=agents, store=FakeStore(), clock=Ticks(), **session)


async def test_a_session_started_without_the_list_is_snapshot_before_the_leftovers() -> None:
    session = starting(name="auth rework")
    events = await events_until(session, of_type(Notice))

    assert [type(e) for e in events] == [Snapshot, Notice]
    opened = last_of(Snapshot, events).session
    assert (opened.name, opened.current, opened.counts) == ("auth rework", True, {})
    assert opened.usage == Usage() and opened.last_active is None
    await session.close()


async def test_a_session_started_without_a_name_is_named_by_its_start() -> None:
    session = starting()
    events = await events_until(session, of_type(Snapshot))
    assert last_of(Snapshot, events).session.name == "Oct 17 14:30"
    await session.close()


async def test_a_session_started_to_pick_one_is_snapshot_before_the_leftovers() -> None:
    session = starting(pick_session=True)
    events = await events_until(session, of_type(Notice))
    assert [type(e) for e in events] == [Snapshot, Notice]
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
    events = await opened(session, ActivateSession(session="auth"))

    board = last_of(Snapshot, events).tasks
    assert [t.status for t in board] == ["completed", "cancelled", "pending"]
    assert messages(Notice, events) == [
        'Resumed "auth rework" (last active Oct 16 18:02)',
        "1 task was interrupted and is cancelled.",
    ]
    assert last_of(Snapshot, events).session == SessionSummary(
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


async def test_opening_a_session_publishes_its_snapshot_then_the_resumed_notes() -> None:
    session, _ = switching(store_with(saved("auth", state("1", "running"))))
    events = await events_until(session, of_type(Snapshot))  # The one after the Welcome.
    events = await opened(session, ActivateSession(session="auth"))
    assert [type(e) for e in events] == [
        Snapshot,
        Notice,
        Notice,
        CommandAccepted,
        HistoryPage,  # The answer to History, which marks the end.
    ]
    await session.close()


async def test_opening_a_session_without_interruptions_only_notes_its_resumption() -> None:
    session, _ = switching(store_with(saved("auth", state("1", "completed"))))
    events = await opened(session, ActivateSession(session="auth"))
    assert messages(Notice, events) == ['Resumed "auth" (last active Oct 16 18:02)']
    await session.close()


async def test_an_opened_session_carries_on_its_planners_conversation() -> None:
    session, launcher = switching(store_with(saved("auth", planner_conversation="talk-9")))
    await opened(session, ActivateSession(session="auth"))
    launcher.planner_turns = [chats]
    await send(session, SendMessage(agent=PLANNER, text="Where were we?"))
    await events_until(session, idle("planner"))
    assert launcher.resumed == ["talk-9"]
    await session.close()


async def test_an_opened_session_has_its_saved_settings() -> None:
    settings = Settings(model="opus", tool_access="full")
    session, _ = switching(store_with(saved("auth", settings=settings)))
    events = await opened(session, ActivateSession(session="auth"))
    assert last_of(Snapshot, events).settings == settings
    assert session.settings == settings
    await session.close()


async def test_settings_given_on_the_command_line_override_the_saved_ones() -> None:
    record = saved("auth", settings=Settings(model="opus", tool_access="full"))
    session, _ = switching(store_with(record), settings=Settings(model="haiku"), pinned=("model",))
    await opened(session, ActivateSession(session="auth"))
    assert session.settings == Settings(model="haiku", tool_access="full")
    await session.close()


async def test_a_session_saved_without_reviews_opens_with_none() -> None:
    paused = LaneSnapshot(planner=PlannerSnapshot(paused=True))
    store = store_with(saved("paused", reviews=paused), saved("auth"))
    session, _ = switching(store)
    await opened(session, ActivateSession(session="paused"))
    assert store.last.reviews == paused
    events = await opened(session, ActivateSession(session="auth"))
    assert store.last.reviews == LaneSnapshot(planner=PlannerSnapshot())
    assert last_of(Snapshot, events).review == ReviewStatus(paused=False, open_batches=0)
    await session.close()


async def test_opening_another_session_hands_the_lock_over() -> None:
    store = store_with(saved("first"), saved("second"))
    session, _ = switching(store)
    await send(session, AddTask(task=TASK))
    await opened(session, ActivateSession(session="first"))
    assert store.locked == {"first"} and store.released == ["session-1"]
    await opened(session, ActivateSession(session="second"))
    assert store.locked == {"second"} and store.released == ["session-1", "first"]
    await session.close()
    assert store.locked == set()


async def test_opening_the_current_session_does_nothing() -> None:
    store = FakeStore()
    session, _ = switching(store)
    await send(session, AddTask(task=TASK))
    assert await refused(session, ActivateSession(session="session-1")) == []
    assert store.released == [] and store.locked == {"session-1"}
    await session.close()


async def test_a_switch_sent_as_the_host_closes_is_refused_and_takes_no_lock() -> None:
    store = store_with(saved("other"))
    session, _ = switching(store)
    await send(session, AddTask(task=TASK))
    await until_marked(session)
    releasing = asyncio.Event()
    release = asyncio.Event()

    async def held() -> None:
        releasing.set()
        await release.wait()

    store.on_release = held
    closing = asyncio.create_task(session.close())
    await releasing.wait()
    await send(session, ActivateSession(session="other"))
    events = await events_until(session, of_type(CommandRefused))
    release.set()
    await closing

    assert refusals(events) == [(INVALID, "Orbit is stopping.")]
    assert store.locked == set() and store.released == ["session-1"]


async def test_a_session_open_in_another_orbit_is_refused() -> None:
    store = store_with(saved("auth", name="auth rework"))
    store.elsewhere.add("auth")
    session, _ = switching(store)
    errors = await refused(session, ActivateSession(session="auth"))
    assert errors == [(LOCKED, "Can't open \"auth rework\": it's open in another orbit.")]
    await session.close()


async def test_a_session_locked_as_it_is_opened_is_refused() -> None:
    store = store_with(saved("auth"))
    store.free = False
    session, _ = switching(store)
    errors = await refused(session, ActivateSession(session="auth"))
    assert errors == [(LOCKED, "Can't open \"auth\": it's open in another orbit.")]
    await session.close()


async def test_an_unreadable_session_is_refused() -> None:
    store = FakeStore()
    store.broken["broken"] = "it isn't JSON"
    session, _ = switching(store)
    errors = await refused(session, ActivateSession(session="broken"))
    assert errors == [(INVALID, "Can't open the session broken: it isn't JSON.")]
    await session.close()


async def test_a_missing_session_is_refused() -> None:
    session, _ = switching(FakeStore())
    errors = await refused(session, ActivateSession(session="gone"))
    assert errors == [(UNKNOWN, "There's no saved session 'gone'.")]
    await session.close()


async def test_a_session_saved_again_before_it_is_locked_opens_as_saved_last() -> None:
    store = store_with(saved("auth", state("1", "completed")))
    newer = saved("auth", state("1", "completed"), state("2", "running"), last_active=EARLIER)

    async def save_newer() -> None:
        store.on_acquire = None
        store.saved.append(newer)

    store.on_acquire = save_newer
    session, _ = switching(store)
    events = await opened(session, ActivateSession(session="auth"))

    board = last_of(Snapshot, events).tasks
    assert [t.status for t in board] == ["completed", "cancelled"]
    assert messages(Notice, events) == [
        'Resumed "auth" (last active Oct 15 09:00)',
        "1 task was interrupted and is cancelled.",
    ]
    assert last_of(Snapshot, events).session.last_active == EARLIER
    assert store.last.tasks == board
    await session.close()


async def test_a_session_unreadable_once_locked_is_refused_and_lets_go_of_the_lock() -> None:
    store = store_with(saved("auth"))

    async def break_it() -> None:
        store.broken["auth"] = "it isn't JSON"

    store.on_acquire = break_it
    session, _ = switching(store)
    errors = await refused(session, ActivateSession(session="auth"))
    assert errors == [(INVALID, "Can't open the session auth: it isn't JSON.")]
    assert store.released == ["auth"] and store.locked == set()
    await session.close()


async def test_a_store_that_cant_lock_the_session_leaves_the_open_one_as_it_was() -> None:
    store = store_with(saved("auth", state("1", "pending")))
    session, _ = switching(store)
    await send(session, AddTask(task=TASK))
    store.lock_failure = "Couldn't lock the session: disk full"
    await send(session, ActivateSession(session="auth"))
    events = await until_marked(session)
    assert errors(events) == [(SAVE_FAILED, "Couldn't lock the session: disk full")]
    assert session.saved_session.id == "session-1"
    assert [t.task for t in board(events)] == [TASK]  # Its last board is the one it had.
    await session.close()


async def test_switching_is_refused_while_tasks_run() -> None:
    store = store_with(saved("auth"))
    session, _ = switching(store, hangs)
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    await events_until(session, of_type(AgentStarted))
    assert await refused(session, ActivateSession(session="auth")) == [(BUSY, TASKS_RUNNING)]
    assert await refused(session, NewSession()) == [(BUSY, TASKS_RUNNING)]
    assert store.locked == {"session-1"} and store.released == []
    assert session.saved_session.id == "session-1"
    await session.close()


async def test_switching_is_refused_while_a_review_runs() -> None:
    store = store_with(saved("auth"))
    session, _, _ = session_for(
        claims_done, reviewers=(hangs,), tree=FakeTree(), review=True, store=store
    )
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    reviewing = Stop("reviewer", lambda e: isinstance(e, AgentStarted) and e.role == "reviewer")
    await events_until(session, all_of(reviewing, idle("work")), timeout=10)
    assert await refused(session, ActivateSession(session="auth")) == [(BUSY, REVIEW_RUNNING)]
    await session.close()


async def test_switching_is_refused_while_the_planner_answers() -> None:
    session, launcher = switching(store_with(saved("auth")))
    launcher.planner_turns = [hangs]
    await send(session, SendMessage(agent=PLANNER, text="Plan it."))
    await events_until(session, of_type(AgentStarted))
    assert await refused(session, ActivateSession(session="auth")) == [(BUSY, PLANNER_ANSWERING)]
    assert await refused(session, NewSession(name="other")) == [(BUSY, PLANNER_ANSWERING)]
    assert session.saved_session.name != "other"
    await session.close()


async def test_work_started_while_a_session_is_locked_to_open_refuses_the_switch() -> None:
    store = store_with(saved("auth"))
    session, _ = switching(store, hangs)
    await send(session, AddTask(task=TASK))

    async def run() -> None:
        store.on_acquire = None
        await send(session, RunTasks())

    store.on_acquire = run
    assert await refused(session, ActivateSession(session="auth")) == [(BUSY, TASKS_RUNNING)]
    assert store.locked == {"session-1"} and store.released == ["auth"]
    await session.close()


async def test_a_new_session_starts_empty_with_its_name() -> None:
    store = FakeStore()
    session, _ = switching(store)
    await send(session, AddTask(task=TASK))
    events = await opened(session, NewSession(name="auth rework"))

    assert last_of(Snapshot, events).tasks == ()
    assert last_of(Snapshot, events).session == SessionSummary(
        id="session-2", name="auth rework", current=True
    )
    assert store.released == ["session-1"]
    assert [r.id for r in store.saved] == ["session-1"]  # Not saved until it's used.
    await send(session, AddTask(task=TASK))
    assert store.last.id == "session-2" and store.last.name == "auth rework"
    assert store.locked == {"session-2"}
    await session.close()


async def test_a_new_session_without_a_name_is_named_by_its_start() -> None:
    session, _ = switching(FakeStore())
    for name, named in ((None, "Oct 17 14:31"), ("  ", "Oct 17 14:32")):
        events = await opened(session, NewSession(name=name))
        assert last_of(Snapshot, events).session.name == named
    await session.close()


async def test_a_new_session_has_a_fresh_planner_conversation() -> None:
    session, launcher = switching(store_with(saved("auth")))
    launcher.planner_turns = [chats, chats]
    await send(session, SendMessage(agent=PLANNER, text="Hello."))
    await events_until(session, idle("planner"))
    events = await opened(session, NewSession())
    await send(session, SendMessage(agent=PLANNER, text="Hello again."))
    await events_until(session, idle("planner"))
    assert launcher.resumed == [None, None]
    assert any(isinstance(e, AgentExited) for e in events)
    await session.close()


async def with_a_chain_out_of_fixes(store: FakeStore, *scripts: Script) -> WorkspaceHost:
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
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    await events_until(session, idle())
    return session


async def ran(session: WorkspaceHost, *tasks: TaskSpec) -> tuple[TaskState, ...]:
    """The board once ``tasks`` were added and ran."""
    for task in tasks:
        await send(session, AddTask(task=task))
    await send(session, RunTasks())
    return last_of(TasksChanged, await events_until(session, idle())).tasks


async def test_a_session_opened_keeps_no_fix_counts_from_the_one_before() -> None:
    store = store_with(saved("auth", state("1", "completed")))
    session = await with_a_chain_out_of_fixes(store, keeps_claiming, keeps_claiming)
    await opened(session, ActivateSession(session="auth"))
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


def spent_live(events: list[Event]) -> Usage:
    """What the turns ending among ``events`` cost, as the task list adds it up live."""
    finished = (e.usage for e in events if isinstance(e, TurnFinished) and e.usage is not None)
    return sum(finished, Usage())


async def spent_on_a_removed_task(session: WorkspaceHost, launcher: ScriptedLauncher) -> Usage:
    """What ``session`` spent, as its events showed it, on a planner turn and a task that was
    removed once it ran."""
    launcher.planner_turns = [chats]
    launcher.planner_usages = [Usage(input_tokens=400, cost_usd=0.4, turns=1)]
    launcher.usages = [(Usage(output_tokens=100, cost_usd=1.0, turns=3),)]
    await send(session, SendMessage(agent=PLANNER, text="Plan it."))
    events = await events_until(session, idle("planner"))
    await send(
        session, AddTask(task=TaskSpec(prompt="Look.", settings=Settings(tool_access="off")))
    )
    await send(session, RunTasks())
    events += await events_until(session, idle())
    await send(session, RemoveTask(task_id="1"))
    events += await events_until(session, of_type(TasksChanged))
    return spent_live(events)


async def test_a_reopened_session_has_spent_what_it_showed_live() -> None:
    store = FakeStore()
    session, launcher = switching(store, claims_done, reviews=False)
    spent = await spent_on_a_removed_task(session, launcher)
    assert spent == Usage(input_tokens=400, output_tokens=100, cost_usd=1.4, turns=4)

    fresh = last_of(Snapshot, await opened(session, NewSession()))
    reopened = last_of(Snapshot, await opened(session, ActivateSession(session="session-1")))
    assert (fresh.session.usage, reopened.session.usage) == (Usage(), spent)
    assert store.last.spent == spent
    await session.close()


async def test_an_agents_turn_counts_toward_its_tasks_usage_and_the_saved_spent() -> None:
    store = FakeStore()
    session, launcher = switching(store, claims_done, reviews=False)
    usage = Usage(output_tokens=100, cost_usd=1.0, turns=3)
    launcher.usages = [(usage,)]
    await send(
        session, AddTask(task=TaskSpec(prompt="Look.", settings=Settings(tool_access="off")))
    )
    await send(session, RunTasks())
    events = await events_until(session, idle())
    assert last_of(TasksChanged, events).tasks[0].usage == usage
    assert store.last.spent == usage
    await session.close()


class HeldStore(FakeStore):
    """A store whose saves of a session that has spent something wait for ``gate``."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def save(self, record: SessionRecord) -> None:
        if record.spent != Usage():
            await self.gate.wait()
        await super().save(record)


async def test_a_planner_turn_cancelled_once_it_finished_ends_once_and_is_saved_spent() -> None:
    store = HeldStore()
    session, launcher = switching(store, reviews=False)
    usage = Usage(input_tokens=400, cost_usd=0.4, turns=1)
    launcher.planner_turns = [chats]
    launcher.planner_usages = [usage]
    launcher.planner_afters = [hangs_after]
    await send(session, SendMessage(agent=PLANNER, text="Plan it."))
    events = await events_until(session, of_type(TurnFinished))
    await send(session, CancelTurn(lane="planner"))
    store.gate.set()
    events += await events_until(session, idle("planner"))
    assert [e.usage for e in events if isinstance(e, TurnFinished)] == [usage]
    assert store.last.spent == usage
    await session.close()


async def test_the_sessions_list_shows_what_a_session_spent() -> None:
    session, launcher = switching(FakeStore(), claims_done, reviews=False)
    spent = await spent_on_a_removed_task(session, launcher)
    await opened(session, NewSession())
    assert {s.id: s.usage for s in await listed(session)} == {"session-1": spent}
    await session.close()


async def test_tasks_orbit_added_before_a_session_was_picked_carry_over_into_it() -> None:
    store = store_with(saved("auth", state("4", "completed")))
    session = failing_startup(store)
    await events_until(session, agent_exited("startup-check"))
    events = await opened(session, ActivateSession(session="auth"))

    board = last_of(Snapshot, events).tasks
    assert [(t.id, t.status) for t in board] == [("4", "completed"), ("5", "draft")]
    assert store.deleted == ["session-1"]
    assert store.released == []
    await session.close()


async def test_a_session_picked_while_the_startup_check_runs_gets_its_fix_draft() -> None:
    store = store_with(saved("auth", state("4", "completed")))
    gate = asyncio.Event()
    session = failing_startup(store, gate)
    await events_until(session, of_type(AgentStarted))
    events = await opened(session, ActivateSession(session="auth"))
    assert not [e for e in events if isinstance(e, CommandRefused)]
    gate.set()
    events = await events_until(session, agent_exited("startup-check"))

    assert [(t.id, t.status) for t in board(events)] == [("4", "completed"), ("5", "draft")]
    assert store.last.id == "auth" and [t.status for t in store.last.tasks] == [
        "completed",
        "draft",
    ]
    await session.close()


class Holding:
    """A store call held, once it's ``entered``, until ``released``."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.released = asyncio.Event()

    async def __call__(self) -> None:
        self.entered.set()
        await self.released.wait()


class HeldSaves(FakeStore):
    """A store whose saves wait for ``hold``, while it's set."""

    def __init__(self) -> None:
        super().__init__()
        self.hold: Holding | None = None

    async def save(self, record: SessionRecord) -> None:
        if self.hold is not None:
            await self.hold()
        await super().save(record)


def latest(store: FakeStore) -> dict[str, SessionRecord]:
    """The record each session in ``store`` was saved as last, by its id."""
    return {record.id: record for record in store.saved}


def drafts(store: FakeStore) -> dict[str, int]:
    """How many drafts each session in ``store`` was saved with last, by its id."""
    return {i: sum(t.status == "draft" for t in r.tasks) for i, r in latest(store).items()}


async def saved_and_checking(store: FakeStore, gate: asyncio.Event) -> WorkspaceHost:
    """A session picked and saved in ``store`` as ``session-1``, its startup check running until
    ``gate`` opens, then failing."""
    session = failing_startup(store, gate, picking=False)
    await events_until(session, of_type(AgentStarted))
    await send(session, AddTask(task=TASK))
    await until_marked(session)
    assert store.last.id == "session-1"
    return session


async def fail_check_while(session: WorkspaceHost, gate: asyncio.Event, hold: Holding) -> None:
    """Fail ``session``'s startup check while ``hold`` holds the store, then release it."""
    await hold.entered.wait()
    gate.set()
    failed = Stop(
        "the step failed",
        lambda e: (
            isinstance(e, VerificationChanged) and any(step.status == "failed" for step in e.steps)
        ),
    )
    await events_until(session, failed)
    for _ in range(20):  # Let the fix's draft go as far as it can while the store is held.
        await asyncio.sleep(0)
    hold.released.set()


async def test_a_check_failing_while_a_saved_session_opens_drafts_its_fix_there() -> None:
    store, gate = store_with(saved("auth", state("4", "completed"))), asyncio.Event()
    session = await saved_and_checking(store, gate)
    hold = Holding()
    store.on_acquire = hold
    sent = asyncio.create_task(send(session, ActivateSession(session="auth")))
    await fail_check_while(session, gate, hold)
    await sent
    events = await events_until(session, agent_exited("startup-check"))
    events += await until_marked(session)

    assert last_of(Snapshot, events).session.id == "auth"
    assert [(t.id, t.status) for t in board(events)] == [("4", "completed"), ("5", "draft")]
    assert drafts(store) == {"auth": 1, "session-1": 0}
    await session.close()


async def test_a_check_failing_while_a_new_session_waits_on_a_save_drafts_its_fix_there() -> None:
    store, gate = HeldSaves(), asyncio.Event()
    session = await saved_and_checking(store, gate)
    hold = store.hold = Holding()
    added = asyncio.create_task(send(session, AddTask(task=TASK)))
    await hold.entered.wait()
    sent = asyncio.create_task(send(session, NewSession(name="fresh")))
    await fail_check_while(session, gate, hold)
    await asyncio.gather(added, sent)
    events = await events_until(session, agent_exited("startup-check"))
    events += await until_marked(session)

    assert last_of(Snapshot, events).session.id == "session-2"
    assert [t.status for t in board(events)] == ["draft"]
    assert drafts(store) == {"session-1": 0, "session-2": 1}
    await session.close()


async def test_tasks_orbit_added_carry_over_into_a_new_session_picked() -> None:
    store = FakeStore()
    session = failing_startup(store)
    await events_until(session, agent_exited("startup-check"))
    events = await opened(session, NewSession(name="fresh"))
    assert [t.status for t in last_of(Snapshot, events).tasks] == ["draft"]
    assert store.last.id == "session-2" and store.deleted == ["session-1"]
    await session.close()


async def test_a_session_that_cant_be_deleted_as_its_tasks_carry_over_is_reported() -> None:
    store = FakeStore()
    store.delete_failure = "Couldn't delete the session: read-only"
    session = failing_startup(store)
    await events_until(session, agent_exited("startup-check"))
    events = await opened(session, NewSession())
    assert errors(events) == [(SAVE_FAILED, "Couldn't delete the session: read-only")]
    assert store.released == ["session-1"]
    await session.close()


async def test_once_the_user_works_in_the_session_its_tasks_stay_with_it() -> None:
    store = store_with(saved("auth"))
    session = failing_startup(store)
    await events_until(session, agent_exited("startup-check"))
    await send(session, AddTask(task=TASK))
    events = await opened(session, ActivateSession(session="auth"))
    assert last_of(Snapshot, events).tasks == ()
    assert store.deleted == [] and store.released == ["session-1"]
    await session.close()


async def test_a_switch_whose_old_lock_cant_be_released_still_opens_the_session() -> None:
    store = store_with(saved("auth"))
    session, _ = switching(store)
    await send(session, AddTask(task=TASK))
    await events_until(session, of_type(TasksChanged))
    store.release_failure = "Couldn't unlock the session: read-only"
    events = await opened(session, ActivateSession(session="auth"))
    assert last_of(Snapshot, events).session.id == "auth"
    assert errors(events) == [(SAVE_FAILED, "Couldn't unlock the session: read-only")]
    await session.close()


async def test_a_session_without_agents_opens_its_board_and_settings() -> None:
    settings = Settings(model="opus")
    store = store_with(saved("auth", state("1", "pending"), settings=settings))
    session = WorkspaceHost(store=store, clock=Ticks())
    events = await opened(session, ActivateSession(session="auth"))
    assert [t.id for t in last_of(Snapshot, events).tasks] == ["1"]
    assert session.settings == settings
    assert last_of(Snapshot, await opened(session, NewSession())).tasks == ()
    await session.close()


async def test_a_session_without_reviews_keeps_the_reviews_of_the_session_it_opens() -> None:
    reviews = LaneSnapshot(planner=PlannerSnapshot(paused=True))
    store = store_with(saved("auth", reviews=reviews))
    session, _ = switching(store, reviews=False)
    await opened(session, ActivateSession(session="auth"))
    await send(session, AddTask(task=TASK))
    await events_until(session, of_type(TasksChanged))
    assert store.last.id == "auth" and store.last.reviews == reviews
    await opened(session, NewSession())
    await send(session, AddTask(task=TASK))
    await events_until(session, of_type(TasksChanged))
    assert store.last.id != "auth" and store.last.reviews is None
    await session.close()


async def test_a_session_without_a_planner_keeps_the_conversation_of_the_session_it_opens() -> None:
    store = store_with(saved("auth", state("1", "pending"), planner_conversation="conv-1"))
    session = WorkspaceHost(store=store, clock=Ticks())
    await opened(session, ActivateSession(session="auth"))
    assert store.last.id == "auth" and store.last.planner_conversation == "conv-1"
    await opened(session, NewSession())
    await send(session, AddTask(task=TASK))
    await events_until(session, of_type(TasksChanged))
    assert store.last.id != "auth" and store.last.planner_conversation is None
    await session.close()
