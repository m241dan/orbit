"""Each session's state as connections see it: whether it's the workspace's open one, whether
it's running, and how many connections view it."""

import asyncio

from orbit.agent import WorkspaceHost
from orbit.protocol import (
    PLANNER,
    SUPER_REVIEW,
    ActivateSession,
    Activity,
    AddTask,
    Catalog,
    Event,
    FireTrigger,
    RunTasks,
    SendMessage,
    SessionState,
    SessionStateChanged,
    SessionSummary,
    Settings,
    Snapshot,
    TaskSpec,
    TurnEndReason,
    Usage,
)
from tests.agent_fakes import (
    Call,
    FakeStore,
    FakeTree,
    Steps,
    Ticks,
    chats,
    checked,
    claims_done,
    frontend,
    gated_planner,
    greet,
    send,
    session_for,
)
from tests.connection_helpers import joined, received, say, until
from tests.session_builders import saved

OPEN = "session-1"
"""The id of the session :func:`session_for` opens."""

TASK = TaskSpec(prompt="Add a flag.", settings=Settings(model="opus"))


def state(
    state: SessionState = "idle",
    viewers: int = 1,
    session: str = OPEN,
    active: bool = True,
    activity: tuple[Activity, ...] = (),
) -> SessionStateChanged:
    """The state change of ``session`` to ``state`` with ``viewers``, doing ``activity``, with
    an empty spend by role if ``active`` and an unknown one otherwise."""
    return SessionStateChanged(
        session=session,
        active=active,
        state=state,
        viewers=viewers,
        activity=activity,
        role_spent={} if active else None,
        caused_spent={} if active else None,
    )


def running(*activity: Activity) -> SessionStateChanged:
    """The open session's state change to running ``activity``, with one viewer."""
    return state("running", activity=activity)


def changes(events: list[Event]) -> list[SessionStateChanged]:
    """The state changes among ``events``."""
    return [e for e in events if isinstance(e, SessionStateChanged)]


def idle_again(event: Event) -> bool:
    """Whether ``event`` says the open session is idle."""
    return isinstance(event, SessionStateChanged) and event.state == "idle"


def running_now(event: Event) -> bool:
    """Whether ``event`` says the open session is running."""
    return isinstance(event, SessionStateChanged) and event.state == "running"


def changed(event: Event) -> bool:
    return isinstance(event, SessionStateChanged)


async def greeted_and_counted(session: WorkspaceHost) -> list[Event]:
    """Greet ``session`` and return its events up to the change counting its frontend."""
    await greet(session)
    return await asyncio.wait_for(until(session, changed), 5)


def open_entry(events: list[Event]) -> tuple[SessionSummary, ...]:
    """The sessions in the one workspace of the Catalog among ``events``."""
    [catalog] = [e for e in events if isinstance(e, Catalog)]
    [workspace] = catalog.workspaces
    return workspace.sessions


async def test_the_first_hello_publishes_the_open_session_with_one_viewer() -> None:
    session, _, _ = session_for()
    events = await greeted_and_counted(session)
    await session.close()

    assert any(isinstance(e, Snapshot) for e in events)
    assert changes(events) == [state(viewers=1)]


async def test_a_task_run_makes_the_open_session_running_its_task_until_it_ends() -> None:
    session, _, _ = session_for(claims_done)
    await greeted_and_counted(session)
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    events = await asyncio.wait_for(until(session, idle_again), 5)
    await session.close()

    assert changes(events) == [running(Activity(kind="task_run", task_id="1")), state("idle")]


async def test_a_run_moving_to_its_next_task_republishes_the_open_sessions_activity() -> None:
    session, _, _ = session_for(claims_done, claims_done)
    await greeted_and_counted(session)
    await send(session, AddTask(task=TASK))
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    events = await asyncio.wait_for(until(session, idle_again), 5)
    await session.close()

    assert changes(events) == [
        running(Activity(kind="task_run", task_id="1")),
        running(Activity(kind="task_run", task_id="2")),
        state("idle"),
    ]


async def test_a_review_keeps_the_open_session_running_its_review_until_it_ends() -> None:
    reviewing, gate = asyncio.Event(), asyncio.Event()

    async def gated_review(call: Call) -> TurnEndReason:
        await call("get_task")
        reviewing.set()
        await gate.wait()
        await call("complete_task", summary="Reviewed")
        return "completed"

    session, _, _ = session_for(
        claims_done, reviewers=(gated_review,), tree=FakeTree(), review=True
    )
    await greeted_and_counted(session)
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    started = await asyncio.wait_for(until(session, running_now), 5)
    await asyncio.wait_for(reviewing.wait(), 5)
    during = await received(frontend(session))
    gate.set()
    ended = await asyncio.wait_for(until(session, idle_again), 5)
    await session.close()

    work = Activity(kind="task_run", task_id="1")
    review = Activity(kind="review", task_id="1")
    assert (changes(started), changes(during), changes(ended)) == (
        [running(work)],
        [running(review)],
        [state("idle")],
    )


async def test_a_super_review_runs_as_one_review_of_no_particular_task() -> None:
    gate = asyncio.Event()

    async def gated_review(call: Call) -> TurnEndReason:
        await call("get_task")
        await gate.wait()
        await call("complete_task", summary="Reviewed")
        return "completed"

    session, _, _ = session_for(reviewers=(gated_review,), tree=FakeTree(), review=True)
    await greeted_and_counted(session)
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    started = await asyncio.wait_for(until(session, running_now), 5)
    gate.set()
    ended = await asyncio.wait_for(until(session, idle_again), 5)
    await session.close()

    assert (changes(started), changes(ended)) == (
        [running(Activity(kind="review"))],
        [state("idle")],
    )


async def test_a_planner_turn_makes_the_open_session_running_the_planner_until_it_ends() -> None:
    gate = asyncio.Event()
    session, _, launcher = session_for()
    launcher.planner_turns = [gated_planner(gate)]
    await greeted_and_counted(session)
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    started = await asyncio.wait_for(until(session, running_now), 5)
    gate.set()
    ended = await asyncio.wait_for(until(session, idle_again), 5)
    await session.close()

    assert (changes(started), changes(ended)) == (
        [running(Activity(kind="planner"))],
        [state("idle")],
    )


async def test_the_startup_check_is_reported_but_leaves_the_open_session_idle() -> None:
    steps = Steps(True, True)
    steps.gate = asyncio.Event()
    session, _, _ = session_for(verification=checked(steps), check_at_startup=True)
    await greet(session)
    during = await received(frontend(session))
    steps.gate.set()
    ended = await asyncio.wait_for(until(session, changed), 5)
    await session.close()

    checking = state(activity=(Activity(kind="startup_check"),))
    assert (changes(during)[-1], changes(ended)) == (checking, [state()])


async def test_a_second_hello_and_its_closing_change_the_viewers_for_every_connection() -> None:
    session = WorkspaceHost(new_id=lambda: OPEN)
    first = await joined(session, "first")
    await received(first)

    second = await joined(session, "second")
    joining = await received(first)
    await second.close()
    leaving = await received(first)
    await session.close()

    assert (changes(joining), changes(leaving)) == ([state(viewers=2)], [state(viewers=1)])


async def test_a_joining_connections_catalog_has_the_open_sessions_state_but_not_it() -> None:
    gate = asyncio.Event()
    session, _, launcher = session_for()
    launcher.planner_turns = [gated_planner(gate)]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await asyncio.wait_for(until(session, running_now), 5)

    second = await joined(session, "second")
    [summary] = open_entry(await received(second))
    gate.set()
    await session.close()

    assert (summary.id, summary.active, summary.state, summary.viewers, summary.activity) == (
        OPEN,
        True,
        "running",
        1,  # The first frontend's: the second's Hello attached it to nothing yet.
        (Activity(kind="planner"),),
    )


async def test_the_catalog_shows_other_sessions_inactive_idle_and_unviewed() -> None:
    store = FakeStore()
    store.saved.append(saved("earlier"))
    session = WorkspaceHost(store=store, clock=Ticks(), new_id=lambda: OPEN)
    connection = await joined(session, "first")
    sessions = open_entry(await received(connection))
    await session.close()

    states = {s.id: (s.active, s.state, s.viewers) for s in sessions}
    assert states == {OPEN: (True, "idle", 0), "earlier": (False, "idle", 0)}


async def test_opening_another_session_publishes_both_sessions_states() -> None:
    store = FakeStore()
    store.saved.append(saved("earlier"))
    session = WorkspaceHost(store=store, clock=Ticks(), new_id=lambda: OPEN)
    connection = await joined(session, "first")
    await received(connection)

    await say(connection, "open", ActivateSession(session="earlier"))
    events = await received(connection)
    await session.close()

    assert changes(events) == [
        state(viewers=0, active=False),
        state(viewers=1, session="earlier"),
    ]


async def test_state_changes_belong_to_no_session_and_are_never_logged() -> None:
    store = FakeStore()
    session = WorkspaceHost(store=store, clock=Ticks(), new_id=lambda: OPEN)
    connection = await joined(session, "first")
    await say(connection, "add", AddTask(task=TASK))
    envelopes = []
    async for envelope in connection.events():
        envelopes.append(envelope)
        if isinstance(envelope.payload, SessionStateChanged):
            break
    await session.close()

    assert envelopes[-1].session is None
    assert store.logged
    assert not any(isinstance(e.payload, SessionStateChanged) for e in store.logged)


async def test_a_turn_ending_with_usage_publishes_what_the_session_spent_by_role() -> None:
    session, _, launcher = session_for()
    launcher.planner_turns = [chats]
    launcher.planner_usages = [Usage(cost_usd=0.4)]
    connection = await joined(session, "first")
    await received(connection)
    await say(connection, "plan", SendMessage(agent=PLANNER, text="Plan."))
    events = await until(connection, spent)
    await session.close()

    [change] = [e for e in changes(events) if e.role_spent]
    assert (change.role_spent, change.caused_spent) == ({PLANNER: Usage(cost_usd=0.4)}, {})


def spent(event: Event) -> bool:
    """Whether ``event`` says the open session spent anything by role."""
    return isinstance(event, SessionStateChanged) and bool(event.role_spent)
