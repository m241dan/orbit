"""Each session's state as connections see it: whether it's the workspace's open one, whether
it's running, and how many connections view it."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from orbit.agent import AgentSession
from orbit.agent.application.connection import SessionConnection
from orbit.agent.domain.sessions import SessionRecord
from orbit.protocol import (
    PLANNER,
    ActivateSession,
    AddTask,
    Catalog,
    Command,
    CommandEnvelope,
    Event,
    RunTasks,
    SendMessage,
    Session,
    SessionState,
    SessionStateChanged,
    SessionSummary,
    Settings,
    Snapshot,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import (
    HELLO,
    Call,
    FakeStore,
    FakeTree,
    Steps,
    Ticks,
    agent_exited,
    frontend,
    greet,
    send,
    session_for,
)
from tests.test_planner_lane import gated_planner
from tests.test_startup_check import checked
from tests.test_task_runs import claims_done

OPEN = "session-1"
"""The id of the session :func:`session_for` opens."""

TASK = TaskSpec(prompt="Add a flag.", settings=Settings(model="opus"))


def state(
    state: SessionState = "idle", viewers: int = 1, session: str = OPEN, active: bool = True
) -> SessionStateChanged:
    """The open session's state change to ``state`` with ``viewers``."""
    return SessionStateChanged(session=session, active=active, state=state, viewers=viewers)


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


async def until(session: AgentSession, done: Callable[[Event], bool]) -> list[Event]:
    """The events reaching ``session``'s frontend up to and including the first that's
    ``done``."""
    stream = frontend(session).events()
    events = [(await anext(stream)).payload]
    while not done(events[-1]):
        events.append((await anext(stream)).payload)
    return events


async def greeted_and_counted(session: AgentSession) -> list[Event]:
    """Greet ``session`` and return its events up to the change counting its frontend."""
    await greet(session)
    return await asyncio.wait_for(until(session, changed), 5)


async def say(connection: SessionConnection, command_id: str, command: Command) -> None:
    await connection.send(CommandEnvelope(command_id=command_id, payload=command))


async def taken(connection: Session) -> list[Event]:
    """What reached ``connection`` until nothing more comes for a moment."""
    events: list[Event] = []
    stream = connection.events()
    try:
        while True:
            events.append((await asyncio.wait_for(anext(stream), 0.05)).payload)
    except TimeoutError:
        return events


async def joined(session: AgentSession, name: str) -> SessionConnection:
    """A new connection to ``session`` that has said Hello as ``name``."""
    connection = session.connect()
    await say(connection, name, HELLO)
    return connection


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


async def test_a_task_run_makes_the_open_session_running_until_it_ends() -> None:
    session, _, _ = session_for(claims_done)
    await greeted_and_counted(session)
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    events = await asyncio.wait_for(until(session, idle_again), 5)
    await session.close()

    assert changes(events) == [state("running"), state("idle")]


async def test_a_review_keeps_the_open_session_running_until_it_ends() -> None:
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
    running = await asyncio.wait_for(until(session, running_now), 5)
    await asyncio.wait_for(reviewing.wait(), 5)
    during = await taken(frontend(session))
    gate.set()
    ended = await asyncio.wait_for(until(session, idle_again), 5)
    await session.close()

    assert (changes(running), changes(during), changes(ended)) == (
        [state("running")],
        [],
        [state("idle")],
    )


async def test_a_planner_turn_makes_the_open_session_running_until_it_ends() -> None:
    gate = asyncio.Event()
    session, _, launcher = session_for()
    launcher.planner_turns = [gated_planner(gate)]
    await greeted_and_counted(session)
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    running = await asyncio.wait_for(until(session, running_now), 5)
    gate.set()
    ended = await asyncio.wait_for(until(session, idle_again), 5)
    await session.close()

    assert (changes(running), changes(ended)) == ([state("running")], [state("idle")])


async def test_the_startup_check_leaves_the_open_session_idle() -> None:
    session, _, _ = session_for(verification=checked(Steps(True, True)), check_at_startup=True)
    await greet(session)
    events = await asyncio.wait_for(until(session, agent_exited("startup-check")), 5)
    await session.close()

    assert changes(events) == [state("idle")]


async def test_a_second_hello_and_its_closing_change_the_viewers_for_every_connection() -> None:
    session = AgentSession(new_id=lambda: OPEN)
    first = await joined(session, "first")
    await taken(first)

    second = await joined(session, "second")
    joining = await taken(first)
    await second.close()
    leaving = await taken(first)
    await session.close()

    assert (changes(joining), changes(leaving)) == ([state(viewers=2)], [state(viewers=1)])


async def test_a_joining_connections_catalog_counts_it_and_has_the_open_sessions_state() -> None:
    gate = asyncio.Event()
    session, _, launcher = session_for()
    launcher.planner_turns = [gated_planner(gate)]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await asyncio.wait_for(until(session, running_now), 5)

    second = await joined(session, "second")
    [summary] = open_entry(await taken(second))
    gate.set()
    await session.close()

    assert (summary.id, summary.active, summary.state, summary.viewers) == (
        OPEN,
        True,
        "running",
        2,
    )


def saved(session_id: str) -> SessionRecord:
    """A saved session ``session_id``, last active a while ago."""
    when = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    return SessionRecord(id=session_id, name="Earlier", created=when, last_active=when)


async def test_the_catalog_shows_other_sessions_inactive_idle_and_unviewed() -> None:
    store = FakeStore()
    store.saved.append(saved("earlier"))
    session = AgentSession(store=store, clock=Ticks(), new_id=lambda: OPEN)
    connection = await joined(session, "first")
    sessions = open_entry(await taken(connection))
    await session.close()

    states = {s.id: (s.active, s.state, s.viewers) for s in sessions}
    assert states == {OPEN: (True, "idle", 1), "earlier": (False, "idle", 0)}


async def test_opening_another_session_publishes_both_sessions_states() -> None:
    store = FakeStore()
    store.saved.append(saved("earlier"))
    session = AgentSession(store=store, clock=Ticks(), new_id=lambda: OPEN)
    connection = await joined(session, "first")
    await taken(connection)

    await say(connection, "open", ActivateSession(session="earlier"))
    events = await taken(connection)
    await session.close()

    assert changes(events) == [
        state(viewers=0, active=False),
        state(viewers=1, session="earlier"),
    ]


async def test_state_changes_belong_to_no_session_and_are_never_logged() -> None:
    store = FakeStore()
    session = AgentSession(store=store, clock=Ticks(), new_id=lambda: OPEN)
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
