"""Activating sessions: making a saved one, or a new one, the workspace's open session, which
the activating connection then views live while the others keep viewing what they viewed."""

import asyncio
from dataclasses import dataclass

from orbit.agent import WorkspaceHost
from orbit.agent.application.connection import SessionConnection
from orbit.agent.domain.sessions import OPEN_UNDELETABLE, OPEN_UNRENAMABLE, SessionRecord
from orbit.protocol import (
    INVALID,
    ActivateSession,
    AddTask,
    Attach,
    Catalog,
    CommandAccepted,
    CommandRefused,
    DeleteSession,
    Event,
    NewSession,
    RenameSession,
    SessionRef,
    Snapshot,
    TasksChanged,
)
from tests.agent_fakes import HELLO, WORKSPACE, FakeStore, Ticks, connect
from tests.attach_fakes import (
    EARLIER,
    OPEN,
    SAVED_TASK,
    TASK,
    WHEN,
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


async def used(session: WorkspaceHost, name: str) -> SessionConnection:
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


async def test_a_connection_not_yet_attached_stays_unattached_through_an_activation() -> None:
    session = workspace(stored())
    activating = await used(session, "activating")
    greeted = connect(session)
    await say(greeted, "hello", HELLO)
    await taken(greeted)
    await say(activating, "activate", ActivateSession(session=EARLIER))
    await taken(activating)
    seen = await taken(greeted)
    await session.close()

    assert greeted.workspace is None
    assert not any(isinstance(e, Snapshot) for e in seen)


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
    session = WorkspaceHost(store=stored(), clock=Ticks(), new_id=lambda: next(ids), name="today")
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
    session = WorkspaceHost(store=stored(), clock=Ticks(), new_id=lambda: next(ids), name="today")
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


async def test_attach_then_activate_counts_one_viewer_of_the_activated_session() -> None:
    session = workspace(stored())
    activating = await used(session, "activating")
    await say(activating, "attach", Attach(workspace=WORKSPACE, session=EARLIER))
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


@dataclass(frozen=True)
class Meanwhile:
    """Two connections activating: the ``first``, which activated ``earlier``, with the
    ``events`` that reached it, and the ``second``, which activated another session while that
    switch was under way, with what it ``saw``; the ``store`` the sessions are saved in."""

    first: SessionConnection
    events: list[Event]
    second: SessionConnection
    seen: list[Event]
    store: FakeStore


async def activated_meanwhile(session_id: str, once_switched: bool = False) -> Meanwhile:
    """Two connections, the second activating the session ``session_id`` while the first's
    switch to ``earlier`` was under way, before ``earlier`` was open unless ``once_switched``."""
    store = stored()
    store.saved.append(SessionRecord(id="later", name="later work", created=WHEN, last_active=WHEN))
    session = workspace(store)
    first = await used(session, "first")
    second = await joined(session, "second")
    await taken(first)
    activating: list[asyncio.Task[None]] = []

    async def activate() -> None:
        while once_switched and session.saved_session.id != EARLIER:
            await asyncio.sleep(0)
        await say(second, "activate", ActivateSession(session=session_id))

    async def activate_another() -> None:
        store.on_acquire = None
        activating.append(asyncio.create_task(activate()))

    store.on_acquire = activate_another
    await say(first, "activate", ActivateSession(session=EARLIER))
    await asyncio.gather(*activating)
    events = await taken(first)
    seen = await taken(second)
    await session.close()
    return Meanwhile(first, events, second, seen, store)


def last_snapshot(events: list[Event]) -> Snapshot:
    return [e for e in events if isinstance(e, Snapshot)][-1]


async def test_a_switch_away_meanwhile_leaves_the_first_asker_a_view_of_its_session() -> None:
    meanwhile = await activated_meanwhile("later")

    view = last_snapshot(meanwhile.events)
    assert (view.session.id, view.active) == (EARLIER, False)
    assert view.open_session == SessionRef(id="later", name="later work")
    live = last_snapshot(meanwhile.seen)
    assert (live.session.id, live.active) == ("later", True)
    assert meanwhile.first.viewing == SessionRef(id=EARLIER, name="old work")


async def test_a_switch_back_meanwhile_sends_no_view_of_the_session_open_again() -> None:
    meanwhile = await activated_meanwhile(OPEN, once_switched=True)

    snapshots = meanwhile.events + meanwhile.seen
    views = [e for e in snapshots if isinstance(e, Snapshot) and not e.active]
    assert [(v.session.id, v.open_session) for v in views] == [
        (EARLIER, SessionRef(id=OPEN, name="today"))
    ]
    live = last_snapshot(meanwhile.seen)
    assert (live.session.id, live.active) == (OPEN, True)
    assert meanwhile.first.viewing == SessionRef(id=EARLIER, name="old work")


async def test_activating_the_session_being_opened_meanwhile_attaches_to_it_live() -> None:
    meanwhile = await activated_meanwhile(EARLIER)

    asked = ((meanwhile.first, meanwhile.events), (meanwhile.second, meanwhile.seen))
    for connection, events in asked:
        live = last_snapshot(events)
        assert (live.session.id, live.active) == (EARLIER, True)
        assert connection.viewing is None
    assert CommandAccepted(command_id="activate") in meanwhile.seen
    # Only closing the workspace lets go of it.
    assert meanwhile.store.released == [OPEN, EARLIER]
    assert states(meanwhile.events) == [(OPEN, False, 0), (EARLIER, True, 2)]
    reloads = [e for e in meanwhile.events if isinstance(e, Snapshot) and e.active]
    assert len(reloads) == 1


async def test_a_viewer_attaching_elsewhere_meanwhile_gets_no_view_of_the_session_left() -> None:
    store = stored()
    session = workspace(store)
    activating = await used(session, "activating")
    other = await joined(session, "other")
    await taken(activating)

    async def attach_elsewhere(session_id: str) -> None:
        if session_id != OPEN:
            return
        store.on_recent = None
        await say(other, "attach", Attach(workspace=WORKSPACE, session=EARLIER))
        while other.attached is not None:
            await asyncio.sleep(0)

    store.on_recent = attach_elsewhere
    await say(activating, "activate", ActivateSession(session=EARLIER))
    await taken(activating)
    seen = await taken(other)
    await session.close()

    assert store.on_recent is None
    live = last_snapshot(seen)
    assert (live.session.id, live.active) == (EARLIER, True)
    assert other.viewing is None


@dataclass(frozen=True)
class Managed:
    """What ``seen`` reached the connection that renamed or deleted ``earlier`` while another
    opened it, the ``store`` the sessions are saved in, whether ``earlier`` was still
    ``locked`` once both were done, and the name of the session then open, ``open_name``."""

    seen: list[Event]
    store: FakeStore
    locked: bool
    open_name: str


async def managed_meanwhile(command: RenameSession | DeleteSession) -> Managed:
    """Two connections, the second sending ``command`` about ``earlier`` while the first's
    switch to ``earlier`` was taking its lock."""
    store = stored()
    session = workspace(store)
    first = await used(session, "first")
    second = await joined(session, "second")
    await taken(first)
    managing: list[asyncio.Task[None]] = []

    async def manage() -> None:
        store.on_acquire = None
        managing.append(asyncio.create_task(say(second, "manage", command)))

    store.on_acquire = manage
    await say(first, "activate", ActivateSession(session=EARLIER))
    await asyncio.gather(*managing)
    await taken(first)
    seen = await taken(second)
    managed = Managed(seen, store, EARLIER in store.locked, session.saved_session.name)
    await session.close()
    return managed


async def test_deleting_a_session_opened_meanwhile_is_refused_and_keeps_it() -> None:
    managed = await managed_meanwhile(DeleteSession(session_id=EARLIER))

    refused = outcome(managed.seen)
    assert isinstance(refused, CommandRefused)
    assert (refused.code, refused.message) == (INVALID, OPEN_UNDELETABLE)
    assert managed.store.deleted == [] and managed.locked
    assert any(r.id == EARLIER for r in managed.store.saved)
    assert EARLIER not in managed.store.forgotten
    assert any(envelope.session == EARLIER for envelope in managed.store.logged)


async def test_renaming_a_session_opened_meanwhile_is_refused_and_keeps_its_lock() -> None:
    managed = await managed_meanwhile(RenameSession(session_id=EARLIER, name="renamed"))

    refused = outcome(managed.seen)
    assert isinstance(refused, CommandRefused)
    assert (refused.code, refused.message) == (INVALID, OPEN_UNRENAMABLE)
    assert managed.locked
    assert managed.open_name == "old work"
    assert all(r.name == "old work" for r in managed.store.saved if r.id == EARLIER)
