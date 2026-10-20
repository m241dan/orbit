"""Activating sessions: making a saved one, or a new one, the workspace's open session, which
the activating connection then views live while the others keep viewing what they viewed."""

from orbit.agent import AgentSession
from orbit.agent.application.connection import SessionConnection
from orbit.protocol import (
    INVALID,
    ActivateSession,
    AddTask,
    Attach,
    Catalog,
    CommandAccepted,
    CommandRefused,
    NewSession,
    OpenSession,
    SessionRef,
    Snapshot,
    TasksChanged,
)
from tests.agent_fakes import Ticks
from tests.test_attach import (
    EARLIER,
    OPEN,
    SAVED_TASK,
    TASK,
    joined,
    outcome,
    say,
    snapshot_in,
    states,
    stored,
    taken,
    viewing,
    workspace,
)

OPEN_RETIRED = (
    "OpenSession is retired: send Attach to view a session, or ActivateSession to make it the "
    "active one."
)


async def used(session: AgentSession, name: str) -> SessionConnection:
    """A connection to ``session`` that has added a task to the open session, so it's saved,
    and read what that brought."""
    connection = await joined(session, name)
    await say(connection, "add", AddTask(task=TASK))
    await taken(connection)
    return connection


async def test_activating_a_saved_session_answers_with_its_live_snapshot() -> None:
    session = workspace(stored())
    activating = await used(session, "activating")
    await say(activating, "activate", ActivateSession(session=EARLIER))
    events = await taken(activating)
    await say(activating, "add", AddTask(task=TASK))
    added = await taken(activating)
    await session.close()

    snapshot = snapshot_in(events)
    assert (snapshot.session.id, snapshot.active, snapshot.viewers) == (EARLIER, True, 1)
    assert snapshot.tasks == (SAVED_TASK,)
    assert session.saved_session.id == EARLIER
    assert outcome(events) == CommandAccepted(command_id="activate")
    assert any(isinstance(e, TasksChanged) for e in added)


async def test_another_connection_keeps_the_old_session_as_a_read_only_view() -> None:
    session = workspace(stored())
    activating = await used(session, "activating")
    other = await joined(session, "other")
    await taken(activating)
    await say(activating, "activate", ActivateSession(session=EARLIER))
    await taken(activating)
    seen = await taken(other)
    await session.close()

    snapshot = snapshot_in(seen)
    assert (snapshot.session.id, snapshot.active, snapshot.viewers) == (OPEN, False, 1)
    assert snapshot.open_session == SessionRef(id=EARLIER, name="old work")
    assert snapshot.tasks[0].task == TASK
    assert states(seen) == [(OPEN, False, 1), (EARLIER, True, 1)]


async def test_a_connection_viewing_the_activated_session_gets_it_live() -> None:
    session = workspace(stored())
    activating = await used(session, "activating")
    viewer = await viewing(session, "viewer")
    await taken(activating)
    await say(activating, "activate", ActivateSession(session=EARLIER))
    seen = await taken(viewer)
    await session.close()

    snapshot = snapshot_in(seen)
    assert (snapshot.session.id, snapshot.active, snapshot.viewers) == (EARLIER, True, 2)


async def test_activating_from_a_view_makes_the_viewed_session_live() -> None:
    session = workspace(stored())
    await used(session, "user")
    viewer = await viewing(session, "viewer")
    await say(viewer, "activate", ActivateSession(session=EARLIER))
    events = await taken(viewer)
    await session.close()

    snapshot = snapshot_in(events)
    assert (snapshot.session.id, snapshot.active) == (EARLIER, True)
    assert outcome(events) == CommandAccepted(command_id="activate")


async def test_others_follow_an_unsaved_session_left_as_there_is_nothing_to_view() -> None:
    session = workspace(stored())
    activating = await joined(session, "activating")
    other = await joined(session, "other")
    await taken(activating)
    await say(activating, "activate", ActivateSession(session=EARLIER))
    await taken(other)
    await say(other, "add", AddTask(task=TASK))
    added = await taken(other)
    await session.close()

    assert outcome(added) == CommandAccepted(command_id="add")
    assert session.saved_session.id == EARLIER


async def test_others_following_an_unsaved_session_left_get_the_activated_one() -> None:
    session = workspace(stored())
    activating = await joined(session, "activating")
    other = await joined(session, "other")
    await taken(activating)
    await say(activating, "activate", ActivateSession(session=EARLIER))
    seen = await taken(other)
    await session.close()

    snapshot = snapshot_in(seen)
    assert (snapshot.session.id, snapshot.active) == (EARLIER, True)
    [catalog] = [e for e in seen if isinstance(e, Catalog)]
    listed = [s.id for w in catalog.workspaces for s in w.sessions]
    assert OPEN not in listed
    assert seen.index(catalog) < seen.index(snapshot)


async def test_others_following_an_unsaved_session_left_get_a_new_one() -> None:
    ids = iter((OPEN, "fresh-id"))
    session = AgentSession(store=stored(), clock=Ticks(), new_id=lambda: next(ids), name="today")
    creating = await joined(session, "creating")
    other = await joined(session, "other")
    await taken(creating)
    await say(creating, "new", NewSession(name="fresh"))
    seen = await taken(other)
    await session.close()

    snapshot = snapshot_in(seen)
    assert (snapshot.session.id, snapshot.active) == ("fresh-id", True)


async def test_a_new_session_is_activated_for_the_connection_creating_it() -> None:
    ids = iter((OPEN, "fresh-id"))
    session = AgentSession(store=stored(), clock=Ticks(), new_id=lambda: next(ids), name="today")
    creating = await used(session, "creating")
    other = await joined(session, "other")
    await taken(creating)
    await say(creating, "new", NewSession(name="fresh"))
    events = await taken(creating)
    seen = await taken(other)
    await session.close()

    snapshot = snapshot_in(events)
    assert (snapshot.session.name, snapshot.active) == ("fresh", True)
    left = snapshot_in(seen)
    assert (left.session.id, left.active) == (OPEN, False)
    assert left.open_session == SessionRef(id="fresh-id", name="fresh")


async def test_activating_after_a_failed_switch_leaves_the_others_live() -> None:
    store = stored()
    session = workspace(store)
    activating = await used(session, "activating")
    other = await joined(session, "other")
    await taken(activating)
    store.lock_failure = "Couldn't lock the session: disk full"
    await say(activating, "activate", ActivateSession(session=EARLIER))
    seen = await taken(other)
    await say(other, "add", AddTask(task=TASK))
    added = await taken(other)
    await session.close()

    assert not any(isinstance(e, Snapshot) for e in seen)
    assert outcome(added) == CommandAccepted(command_id="add")


async def test_open_session_is_refused_as_invalid_naming_attach_and_activate() -> None:
    session = workspace(stored())
    connection = await joined(session, "first")
    await say(connection, "open", OpenSession(session_id=EARLIER))
    events = await taken(connection)
    await session.close()

    assert events == [CommandRefused(command_id="open", code=INVALID, message=OPEN_RETIRED)]
    assert session.saved_session.id == OPEN


async def test_attach_then_activate_counts_one_viewer_of_the_activated_session() -> None:
    session = workspace(stored())
    activating = await used(session, "activating")
    await say(activating, "attach", Attach(session=EARLIER))
    await taken(activating)
    await say(activating, "activate", ActivateSession(session=EARLIER))
    events = await taken(activating)
    await session.close()

    assert snapshot_in(events).viewers == 1


async def test_activating_the_open_session_from_a_view_attaches_to_it_live() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "activate", ActivateSession(session=OPEN))
    events = await taken(viewer)
    await session.close()

    snapshot = snapshot_in(events)
    assert (snapshot.session.id, snapshot.active) == (OPEN, True)
    assert viewer.viewing is None
    assert outcome(events) == CommandAccepted(command_id="activate")


async def test_a_left_session_that_cant_be_read_back_sends_its_viewers_no_view() -> None:
    store = stored()
    session = workspace(store)
    activating = await used(session, "activating")
    other = await joined(session, "other")
    await taken(activating)

    async def fail_listing() -> None:
        store.on_acquire = None
        store.list_failure = RuntimeError("The store is gone.")

    store.on_acquire = fail_listing
    await say(activating, "activate", ActivateSession(session=EARLIER))
    events = await taken(activating)
    seen = await taken(other)
    await session.close()

    assert outcome(events) == CommandAccepted(command_id="activate")
    assert not any(isinstance(e, Snapshot) for e in seen)
    assert session.saved_session.id == EARLIER
