"""Attaching connections to sessions: the open one, live, or a saved one as a read-only view."""

import asyncio
from datetime import UTC, datetime

from orbit.agent import WorkspaceHost
from orbit.agent.application.connection import SessionConnection
from orbit.agent.domain.sessions import SessionRecord
from orbit.protocol import (
    BUSY,
    INVALID,
    NOT_ACTIVE,
    UNKNOWN,
    WRONG_SESSION,
    AddTask,
    AssistantMessage,
    Attach,
    Catalog,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    DeleteSession,
    Event,
    EventEnvelope,
    History,
    HistoryPage,
    RenameSession,
    RunTasks,
    SessionRef,
    SessionStateChanged,
    Snapshot,
    TasksChanged,
    TaskSpec,
    TaskState,
)
from tests.agent_fakes import HELLO, FakeStore, Ticks, connect

OPEN = "today-id"
"""The id of the workspace's open session."""

EARLIER = "earlier"
"""The id of the saved session that isn't open."""

WHEN = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
TASK = TaskSpec(prompt="Add a flag.")
SAVED_TASK = TaskState(id="t1", task=TASK, status="completed")
LOGGED = tuple(
    EventEnvelope(seq=seq, session=EARLIER, payload=AssistantMessage(turn_id="turn", text=text))
    for seq, text in ((1, "First."), (2, "Second."))
)
NOT_ACTIVE_MESSAGE = (
    "Session 'old work' isn't the active session; 'today' is. Activate it to work in it."
)


def stored() -> FakeStore:
    """A store holding the session ``earlier``, named "old work", with a task and two logged
    events."""
    store = FakeStore()
    record = SessionRecord(
        id=EARLIER, name="old work", created=WHEN, last_active=WHEN, tasks=(SAVED_TASK,)
    )
    store.saved.append(record)
    store.logged += LOGGED
    return store


def workspace(store: FakeStore) -> WorkspaceHost:
    """A session saved in ``store``, its open one named "today"."""
    return WorkspaceHost(store=store, clock=Ticks(), new_id=lambda: OPEN, name="today")


async def say(
    connection: SessionConnection, command_id: str, command: Command, session: str | None = None
) -> None:
    envelope = CommandEnvelope(command_id=command_id, payload=command, session=session)
    await connection.send(envelope)


async def envelopes(connection: SessionConnection) -> list[EventEnvelope]:
    """What reached ``connection`` until nothing more comes for a moment."""
    taken: list[EventEnvelope] = []
    stream = connection.events()
    try:
        while True:
            taken.append(await asyncio.wait_for(anext(stream), 0.05))
    except TimeoutError:
        return taken


async def taken(connection: SessionConnection) -> list[Event]:
    """The events that reached ``connection`` until nothing more comes for a moment."""
    return [envelope.payload for envelope in await envelopes(connection)]


async def joined(session: WorkspaceHost, name: str) -> SessionConnection:
    """A new connection to ``session`` that has said Hello as ``name``, and read what that
    brought."""
    connection = connect(session)
    await say(connection, name, HELLO)
    await taken(connection)
    return connection


async def viewing(session: WorkspaceHost, name: str) -> SessionConnection:
    """A connection to ``session`` that has attached to ``earlier``, and read what that
    brought."""
    connection = await joined(session, name)
    await say(connection, "attach", Attach(session=EARLIER))
    await taken(connection)
    return connection


def snapshot_in(events: list[Event]) -> Snapshot:
    [snapshot] = [e for e in events if isinstance(e, Snapshot)]
    return snapshot


def outcome(events: list[Event]) -> CommandAccepted | CommandRefused:
    [last] = [e for e in events if isinstance(e, CommandAccepted | CommandRefused)]
    return last


def states(events: list[Event]) -> list[tuple[str, bool, int]]:
    """Each session's state change among ``events``, as its session, whether it's active and
    its viewers."""
    changes = [e for e in events if isinstance(e, SessionStateChanged)]
    return [(e.session, e.active, e.viewers) for e in changes]


async def test_attaching_to_a_saved_session_answers_with_its_read_only_view() -> None:
    session = workspace(stored())
    viewer = await joined(session, "viewer")
    await say(viewer, "attach", Attach(session=EARLIER))
    events = await taken(viewer)
    await session.close()

    snapshot = snapshot_in(events)
    assert (snapshot.session.id, snapshot.session.name) == (EARLIER, "old work")
    assert (snapshot.active, snapshot.viewers) == (False, 1)
    assert snapshot.open_session == SessionRef(id=OPEN, name="today")
    assert snapshot.tasks == (SAVED_TASK,)
    assert snapshot.transcript == LOGGED
    assert snapshot.busy == {"work": False, "planner": False}
    assert events[-1] == CommandAccepted(command_id="attach")


async def test_a_views_answers_belong_to_the_viewed_session_and_are_never_logged() -> None:
    store = stored()
    session = workspace(store)
    viewer = await joined(session, "viewer")
    await say(viewer, "attach", Attach(session=EARLIER))
    answers = [e for e in await envelopes(viewer) if not isinstance(e.payload, SessionStateChanged)]
    await session.close()

    assert {e.session for e in answers} == {EARLIER}
    assert {e.seq for e in store.logged if e.session == EARLIER} == {1, 2}


async def test_a_viewer_no_longer_gets_the_open_sessions_events_while_others_still_do() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    other = await joined(session, "other")
    await taken(viewer)

    await say(other, "add", AddTask(task=TASK))
    seen_by_other = await taken(other)
    seen_by_viewer = await taken(viewer)
    await session.close()

    assert any(isinstance(e, TasksChanged) for e in seen_by_other)
    assert not any(isinstance(e, TasksChanged | CommandAccepted) for e in seen_by_viewer)


async def test_the_open_sessions_snapshot_is_active_and_counts_its_viewers() -> None:
    session = workspace(stored())
    await joined(session, "first")
    second = connect(session)
    await say(second, "second", HELLO)
    snapshot = snapshot_in(await taken(second))
    await session.close()

    assert (snapshot.active, snapshot.viewers) == (True, 2)
    assert snapshot.open_session == SessionRef(id=OPEN, name="today")


async def test_attaching_back_to_the_open_session_makes_it_live_again() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "back", Attach(session=OPEN))
    back = await taken(viewer)
    await say(viewer, "add", AddTask(task=TASK))
    added = await taken(viewer)
    await session.close()

    snapshot = snapshot_in(back)
    assert (snapshot.session.id, snapshot.active, snapshot.viewers) == (OPEN, True, 1)
    assert states(back) == [(EARLIER, False, 0), (OPEN, True, 1)]
    assert any(isinstance(e, TasksChanged) for e in added)
    assert outcome(added) == CommandAccepted(command_id="add")


async def test_attaching_publishes_the_viewers_of_both_sessions_to_every_connection() -> None:
    session = workspace(stored())
    viewer = await joined(session, "viewer")
    other = await joined(session, "other")
    await taken(viewer)

    await say(viewer, "attach", Attach(session=EARLIER))
    await taken(viewer)
    seen = await taken(other)
    await session.close()

    assert states(seen) == [(EARLIER, False, 1), (OPEN, True, 1)]


async def test_a_joining_connections_catalog_counts_the_viewers_of_a_view() -> None:
    session = workspace(stored())
    await viewing(session, "viewer")
    joining = connect(session)
    await say(joining, "joining", HELLO)
    events = await taken(joining)
    await session.close()

    [catalog] = [e for e in events if isinstance(e, Catalog)]
    [entry] = catalog.workspaces
    viewers = {s.id: (s.active, s.viewers) for s in entry.sessions}
    assert viewers == {OPEN: (True, 1), EARLIER: (False, 1)}


async def test_attaching_to_a_session_there_is_none_of_is_refused_as_unknown() -> None:
    session = workspace(stored())
    connection = await joined(session, "first")
    await say(connection, "attach", Attach(session="nowhere"))
    events = await taken(connection)
    await session.close()

    message = "There's no saved session 'nowhere'."
    assert events == [CommandRefused(command_id="attach", code=UNKNOWN, message=message)]


async def test_attaching_to_an_unreadable_session_is_refused_as_invalid() -> None:
    store = stored()
    store.broken["broken"] = "it isn't JSON"
    session = workspace(store)
    connection = await joined(session, "first")
    await say(connection, "attach", Attach(session="broken"))
    events = await taken(connection)
    await session.close()

    message = "Can't view the session broken: it isn't JSON."
    assert events == [CommandRefused(command_id="attach", code=INVALID, message=message)]


async def test_attaching_when_the_sessions_cant_be_listed_changes_nothing() -> None:
    store = stored()
    session = workspace(store)
    connection = await joined(session, "first")
    store.list_failure = RuntimeError("The store is gone.")
    await say(connection, "attach", Attach(session=EARLIER))
    events = await taken(connection)
    store.list_failure = None
    await say(connection, "add", AddTask(task=TASK))
    added = await taken(connection)
    await session.close()

    assert not any(isinstance(e, Snapshot) for e in events)
    assert outcome(events) == CommandAccepted(command_id="attach")
    assert outcome(added) == CommandAccepted(command_id="add")


async def test_a_view_refuses_work_as_not_active_naming_both_sessions() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "run", RunTasks())
    events = await taken(viewer)
    await session.close()

    assert events == [CommandRefused(command_id="run", code=NOT_ACTIVE, message=NOT_ACTIVE_MESSAGE)]


async def test_a_view_answers_history_with_the_viewed_sessions_events() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "history", History(session=EARLIER, before_seq=2))
    events = await taken(viewer)
    await session.close()

    [page] = [e for e in events if isinstance(e, HistoryPage)]
    assert page.events == LOGGED[:1]
    assert outcome(events) == CommandAccepted(command_id="history")


async def test_a_view_renames_the_session_it_views() -> None:
    store = stored()
    session = workspace(store)
    viewer = await viewing(session, "viewer")
    await say(viewer, "rename", RenameSession(session_id=EARLIER, name="older work"))
    events = await taken(viewer)
    await session.close()

    assert outcome(events) == CommandAccepted(command_id="rename")
    assert store.last.name == "older work"


RENAMED_MESSAGE = (
    "Session 'older work' isn't the active session; 'today' is. Activate it to work in it."
)


async def test_a_view_refuses_work_naming_the_session_as_the_viewer_renamed_it() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "rename", RenameSession(session_id=EARLIER, name="older work"))
    await taken(viewer)
    await say(viewer, "run", RunTasks())
    events = await taken(viewer)
    await session.close()

    assert events == [CommandRefused(command_id="run", code=NOT_ACTIVE, message=RENAMED_MESSAGE)]


async def test_a_view_refuses_work_naming_the_session_as_another_renamed_it() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    other = await joined(session, "other")
    await say(other, "rename", RenameSession(session_id=EARLIER, name="  older work "))
    await taken(other)
    await taken(viewer)
    await say(viewer, "run", RunTasks())
    events = await taken(viewer)
    await session.close()

    assert events == [CommandRefused(command_id="run", code=NOT_ACTIVE, message=RENAMED_MESSAGE)]


async def test_a_command_for_another_session_than_the_one_viewed_is_wrong_session() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "history", History(session=EARLIER, before_seq=2), session=OPEN)
    events = await taken(viewer)
    await session.close()

    message = f"Session {OPEN!r} isn't the one viewed; this connection views {EARLIER}."
    assert events == [CommandRefused(command_id="history", code=WRONG_SESSION, message=message)]


async def test_a_command_naming_the_viewed_session_is_taken() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "history", History(session=EARLIER, before_seq=2), session=EARLIER)
    events = await taken(viewer)
    await session.close()

    assert outcome(events) == CommandAccepted(command_id="history")


async def test_deleting_a_session_another_connection_views_is_refused_as_busy() -> None:
    store = stored()
    session = workspace(store)
    await viewing(session, "viewer")
    other = await joined(session, "other")
    await say(other, "delete", DeleteSession(session_id=EARLIER))
    events = await taken(other)
    await session.close()

    message = 'Can\'t delete "old work": 1 other frontend views it.'
    assert outcome(events) == CommandRefused(command_id="delete", code=BUSY, message=message)
    assert store.deleted == []


async def test_deleting_a_session_two_connections_view_names_them_both() -> None:
    session = workspace(stored())
    await viewing(session, "first")
    await viewing(session, "second")
    other = await joined(session, "other")
    await say(other, "delete", DeleteSession(session_id=EARLIER))
    events = await taken(other)
    await session.close()

    message = 'Can\'t delete "old work": 2 other frontends view it.'
    assert outcome(events) == CommandRefused(command_id="delete", code=BUSY, message=message)


async def test_a_session_only_the_deleting_connection_views_is_deleted() -> None:
    store = stored()
    session = workspace(store)
    viewer = await viewing(session, "viewer")
    await say(viewer, "delete", DeleteSession(session_id=EARLIER))
    events = await taken(viewer)
    await session.close()

    assert outcome(events) == CommandAccepted(command_id="delete")
    assert store.deleted == [EARLIER]


async def test_deleting_the_viewed_session_moves_the_connection_to_the_open_one() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "delete", DeleteSession(session_id=EARLIER))
    deleted = await taken(viewer)
    await say(viewer, "add", AddTask(task=TASK))
    added = await taken(viewer)
    await session.close()

    snapshot = snapshot_in(deleted)
    assert (snapshot.session.id, snapshot.active, snapshot.viewers) == (OPEN, True, 1)
    assert outcome(deleted) == CommandAccepted(command_id="delete")
    assert outcome(added) == CommandAccepted(command_id="add")


async def test_deleting_the_viewed_session_publishes_it_left_with_no_viewers() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "delete", DeleteSession(session_id=EARLIER))
    deleted = await taken(viewer)
    joining = connect(session)
    await say(joining, "joining", HELLO)
    [catalog] = [e for e in await taken(joining) if isinstance(e, Catalog)]
    await session.close()

    assert states(deleted) == [(EARLIER, False, 0), (OPEN, True, 1)]
    [entry] = catalog.workspaces
    assert {s.id: s.viewers for s in entry.sessions} == {OPEN: 2}


async def test_a_connection_attaching_while_its_session_is_deleted_views_the_open_one() -> None:
    store = stored()
    session = workspace(store)
    deleting = await joined(session, "deleting")
    attaching = await joined(session, "attaching")
    release = asyncio.Event()

    async def held() -> None:
        await release.wait()

    store.on_delete = held
    delete = asyncio.create_task(say(deleting, "delete", DeleteSession(session_id=EARLIER)))
    await asyncio.sleep(0.01)
    await say(attaching, "attach", Attach(session=EARLIER))
    release.set()
    await delete
    events = await taken(attaching)
    await session.close()

    assert store.deleted == [EARLIER]
    assert attaching.viewing is None
    snapshots = [e for e in events if isinstance(e, Snapshot)]
    assert [(s.session.id, s.active) for s in snapshots] == [(EARLIER, False), (OPEN, True)]


async def test_a_viewed_session_gone_from_the_list_is_named_by_its_id() -> None:
    store = stored()
    session = workspace(store)
    await viewing(session, "viewer")
    other = await joined(session, "other")
    store.saved.clear()
    await say(other, "delete", DeleteSession(session_id=EARLIER))
    events = await taken(other)
    await session.close()

    message = f'Can\'t delete "{EARLIER}": 1 other frontend views it.'
    assert outcome(events) == CommandRefused(command_id="delete", code=BUSY, message=message)
