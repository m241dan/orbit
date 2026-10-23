"""Attaching connections to sessions: the open one, live, or a saved one as a read-only view."""

import asyncio
from collections.abc import Callable

from orbit.agent import WorkspaceHost
from orbit.agent.application.connection import SessionConnection
from orbit.agent.application.ports import StoreFailed
from orbit.agent.domain.sessions import SessionRecord, missing_message, summarize
from orbit.protocol import (
    BUSY,
    INVALID,
    NOT_ACTIVE,
    SAVE_FAILED,
    STORAGE_FAILED,
    UNKNOWN,
    WRONG_SESSION,
    ActivateSession,
    AddTask,
    Attach,
    Catalog,
    Command,
    CommandAccepted,
    CommandRefused,
    DeleteSession,
    ErrorOccurred,
    Event,
    History,
    HistoryPage,
    OwnershipOffered,
    RenameSession,
    ResumeStorage,
    RunTasks,
    SessionRef,
    SessionStateChanged,
    Settings,
    Snapshot,
    TasksChanged,
)
from tests.agent_fakes import (
    HELLO,
    LOCK_RETRY,
    OWNER,
    WORKSPACE,
    FakeLock,
    FakeStore,
    Ticks,
    attach_to,
    connect,
    snapshot_of,
)
from tests.attach_fakes import (
    EARLIER,
    LOGGED,
    OPEN,
    SAVED_TASK,
    WHEN,
    outcome,
    snapshot_in,
    states,
    stored,
    viewing,
    workspace,
)
from tests.connection_helpers import envelopes, joined, received, say
from tests.server_fakes import TASK

LATER = "later"
"""The id of another saved session that isn't open."""

NOT_ACTIVE_MESSAGE = (
    "Session 'old work' isn't the active session; 'today' is. Activate it to work in it."
)


async def test_attaching_to_a_saved_session_answers_with_its_read_only_view() -> None:
    session = workspace(stored())
    viewer = await joined(session, "viewer", read=True)
    await say(viewer, "attach", Attach(workspace=WORKSPACE, session=EARLIER))
    events = await received(viewer)
    await session.close()

    snapshot = snapshot_in(events)
    assert (snapshot.session.id, snapshot.session.name) == (EARLIER, "old work")
    assert (snapshot.active, snapshot.viewers) == (False, 1)
    assert snapshot.open_session == SessionRef(id=OPEN, name="today")
    assert snapshot.tasks == (SAVED_TASK,)
    assert snapshot.transcript == LOGGED
    assert snapshot.busy == {"work": False, "planner": False}
    assert events[-1] == CommandAccepted(command_id="attach")


async def attached_view(session: WorkspaceHost, viewer: SessionConnection) -> Snapshot:
    """The read-only view of ``earlier`` that ``viewer`` gets for attaching to it."""
    await say(viewer, "attach", Attach(workspace=WORKSPACE, session=EARLIER))
    return snapshot_in(await received(viewer))


async def test_a_saved_sessions_view_carries_the_same_fields_as_ever() -> None:
    store = stored()
    session = workspace(store)
    viewer = await joined(session, "viewer", read=True)
    snapshot = await attached_view(session, viewer)
    await session.close()

    [record] = store.saved
    assert snapshot == Snapshot(
        session=summarize(record),
        settings=Settings(),
        model_choices=(),
        busy={"work": False, "planner": False},
        tasks=(SAVED_TASK,),
        steps=(),
        review=None,
        proposals=(),
        agents=(),
        transcript=LOGGED,
        active=False,
        open_session=SessionRef(id=OPEN, name="today"),
        viewers=1,
    )


async def test_a_saved_sessions_view_taken_while_storage_is_halted_carries_the_halt() -> None:
    store = stored()
    session = workspace(store)
    first = await joined(session, "first", read=True)
    store.failure = "Disk full."
    await say(first, "add", AddTask(task=TASK))
    errors = [e for e in await received(first) if isinstance(e, ErrorOccurred)]
    viewer = connect(session)
    await say(viewer, "viewer", HELLO)
    snapshot = await attached_view(session, viewer)
    halt = (await snapshot_of(session)).storage_halt
    await session.close()

    assert [e.code for e in errors] == [STORAGE_FAILED]
    assert halt is not None and "Disk full." in halt
    assert snapshot.storage_halt == halt


async def test_a_saved_sessions_view_taken_while_ownership_is_offered_says_so() -> None:
    lock = FakeLock()
    session = WorkspaceHost(
        store=stored(),
        clock=Ticks(),
        new_id=lambda: OPEN,
        name="today",
        owner=OWNER,
        lock=lock,
        lock_retry=LOCK_RETRY,
    )
    viewer = await joined(session, "viewer", read=True)
    lock.free = True
    offers = [e for e in await received(viewer) if isinstance(e, OwnershipOffered)]
    snapshot = await attached_view(session, viewer)
    await session.close()

    assert len(offers) == 1
    assert (snapshot.read_only, snapshot.ownership_offered) == (True, True)


async def test_a_views_answers_belong_to_the_viewed_session_and_are_never_logged() -> None:
    store = stored()
    session = workspace(store)
    viewer = await joined(session, "viewer", read=True)
    await say(viewer, "attach", Attach(workspace=WORKSPACE, session=EARLIER))
    answers = [e for e in await envelopes(viewer) if not isinstance(e.payload, SessionStateChanged)]
    await session.close()

    assert {e.session for e in answers} == {EARLIER}
    assert {e.seq for e in store.logged if e.session == EARLIER} == {1, 2}


async def test_a_viewer_no_longer_gets_the_open_sessions_events_while_others_still_do() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    other = await joined(session, "other", read=True)
    await received(viewer)

    await say(other, "add", AddTask(task=TASK))
    seen_by_other = await received(other)
    seen_by_viewer = await received(viewer)
    await session.close()

    assert any(isinstance(e, TasksChanged) for e in seen_by_other)
    assert not any(isinstance(e, TasksChanged | CommandAccepted) for e in seen_by_viewer)


async def test_the_open_sessions_snapshot_is_active_and_counts_its_viewers() -> None:
    session = workspace(stored())
    await joined(session, "first", read=True)
    second = connect(session)
    await say(second, "second", HELLO)
    await say(second, "second-attach", attach_to(session))
    snapshot = snapshot_in(await received(second))
    await session.close()

    assert (snapshot.active, snapshot.viewers) == (True, 2)
    assert snapshot.open_session == SessionRef(id=OPEN, name="today")


async def test_attaching_back_to_the_open_session_makes_it_live_again() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "back", Attach(workspace=WORKSPACE, session=OPEN))
    back = await received(viewer)
    await say(viewer, "add", AddTask(task=TASK))
    added = await received(viewer)
    await session.close()

    snapshot = snapshot_in(back)
    assert (snapshot.session.id, snapshot.active, snapshot.viewers) == (OPEN, True, 1)
    assert states(back) == [(EARLIER, False, 0), (OPEN, True, 1)]
    assert any(isinstance(e, TasksChanged) for e in added)
    assert outcome(added) == CommandAccepted(command_id="add")


async def test_attaching_publishes_the_viewers_of_both_sessions_to_every_connection() -> None:
    session = workspace(stored())
    viewer = await joined(session, "viewer", read=True)
    other = await joined(session, "other", read=True)
    await received(viewer)

    await say(viewer, "attach", Attach(workspace=WORKSPACE, session=EARLIER))
    await received(viewer)
    seen = await received(other)
    await session.close()

    assert states(seen) == [(EARLIER, False, 1), (OPEN, True, 1)]


async def test_a_joining_connections_catalog_counts_the_viewers_but_not_itself() -> None:
    session = workspace(stored())
    await viewing(session, "viewer")
    joining = connect(session)
    await say(joining, "joining", HELLO)
    events = await received(joining)
    await session.close()

    [catalog] = [e for e in events if isinstance(e, Catalog)]
    [entry] = catalog.workspaces
    viewers = {s.id: (s.active, s.viewers) for s in entry.sessions}
    assert viewers == {OPEN: (True, 0), EARLIER: (False, 1)}


async def test_attaching_to_a_session_there_is_none_of_is_refused_as_unknown() -> None:
    session = workspace(stored())
    connection = await joined(session, "first", read=True)
    await say(connection, "attach", Attach(workspace=WORKSPACE, session="nowhere"))
    events = await received(connection)
    await session.close()

    message = "There's no saved session 'nowhere'."
    assert events == [CommandRefused(command_id="attach", code=UNKNOWN, message=message)]


async def test_attaching_to_an_unreadable_session_is_refused_as_invalid() -> None:
    store = stored()
    store.broken["broken"] = "it isn't JSON"
    session = workspace(store)
    connection = await joined(session, "first", read=True)
    await say(connection, "attach", Attach(workspace=WORKSPACE, session="broken"))
    events = await received(connection)
    await session.close()

    message = "Can't view the session broken: it isn't JSON."
    assert events == [CommandRefused(command_id="attach", code=INVALID, message=message)]


async def test_attaching_when_the_sessions_cant_be_listed_changes_nothing() -> None:
    store = stored()
    session = workspace(store)
    connection = await joined(session, "first", read=True)
    store.list_failure = StoreFailed("The store is gone.")
    await say(connection, "attach", Attach(workspace=WORKSPACE, session=EARLIER))
    events = await received(connection)
    store.list_failure = None
    await say(connection, "add", AddTask(task=TASK))
    added = await received(connection)
    await session.close()

    assert not any(isinstance(e, Snapshot) for e in events)
    assert outcome(events) == CommandAccepted(command_id="attach")
    assert outcome(added) == CommandAccepted(command_id="add")


async def test_a_view_refuses_work_as_not_active_naming_both_sessions() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "run", RunTasks())
    events = await received(viewer)
    await session.close()

    assert events == [CommandRefused(command_id="run", code=NOT_ACTIVE, message=NOT_ACTIVE_MESSAGE)]


async def halted_with_a_view(store: FakeStore) -> tuple[WorkspaceHost, SessionConnection]:
    """A session over ``store``, halted as adding a task failed to save with "Disk full.", and a
    connection viewing ``earlier`` that has read what the halt brought."""
    session = workspace(store)
    viewer = await viewing(session, "viewer")
    other = await joined(session, "other", read=True)
    store.failure = "Disk full."
    await say(other, "add", AddTask(task=TASK))
    await received(other)
    await received(viewer)
    return session, viewer


async def test_a_view_resuming_storage_once_writing_works_lifts_the_halt() -> None:
    store = stored()
    session, viewer = await halted_with_a_view(store)
    store.failure = None
    await say(viewer, "resume", ResumeStorage())
    resumed = await received(viewer)
    halt = (await snapshot_of(session)).storage_halt
    await session.close()

    assert outcome(resumed) == CommandAccepted(command_id="resume")
    assert halt is None


async def test_a_view_resuming_storage_while_writing_still_fails_is_refused_as_halted() -> None:
    session, viewer = await halted_with_a_view(stored())
    await say(viewer, "resume", ResumeStorage())
    refused = outcome(await received(viewer))
    halt = (await snapshot_of(session)).storage_halt
    await session.close()

    assert isinstance(refused, CommandRefused)
    assert (refused.command_id, refused.code) == ("resume", STORAGE_FAILED)
    assert "Disk full." in refused.message and halt == refused.message


async def test_a_view_still_refuses_work_as_not_active_while_storage_is_halted() -> None:
    session, viewer = await halted_with_a_view(stored())
    await say(viewer, "run", RunTasks())
    events = await received(viewer)
    await session.close()

    assert events == [CommandRefused(command_id="run", code=NOT_ACTIVE, message=NOT_ACTIVE_MESSAGE)]


async def test_a_view_answers_history_with_the_viewed_sessions_events() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "history", History(session=EARLIER, before_seq=2))
    events = await received(viewer)
    await session.close()

    [page] = [e for e in events if isinstance(e, HistoryPage)]
    assert page.events == LOGGED[:1]
    assert outcome(events) == CommandAccepted(command_id="history")


async def test_a_view_renames_the_session_it_views() -> None:
    store = stored()
    session = workspace(store)
    viewer = await viewing(session, "viewer")
    await say(viewer, "rename", RenameSession(session_id=EARLIER, name="older work"))
    events = await received(viewer)
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
    await received(viewer)
    await say(viewer, "run", RunTasks())
    events = await received(viewer)
    await session.close()

    assert events == [CommandRefused(command_id="run", code=NOT_ACTIVE, message=RENAMED_MESSAGE)]


async def test_a_view_refuses_work_naming_the_session_as_another_renamed_it() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    other = await joined(session, "other", read=True)
    await say(other, "rename", RenameSession(session_id=EARLIER, name="  older work "))
    await received(other)
    await received(viewer)
    await say(viewer, "run", RunTasks())
    events = await received(viewer)
    await session.close()

    assert events == [CommandRefused(command_id="run", code=NOT_ACTIVE, message=RENAMED_MESSAGE)]


async def test_a_command_for_another_session_than_the_one_viewed_is_wrong_session() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "history", History(session=EARLIER, before_seq=2), session=OPEN)
    events = await received(viewer)
    await session.close()

    message = f"Session {OPEN!r} isn't the one viewed; this connection views {EARLIER}."
    assert events == [CommandRefused(command_id="history", code=WRONG_SESSION, message=message)]


async def test_a_command_naming_the_viewed_session_is_taken() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "history", History(session=EARLIER, before_seq=2), session=EARLIER)
    events = await received(viewer)
    await session.close()

    assert outcome(events) == CommandAccepted(command_id="history")


async def test_deleting_a_session_another_connection_views_is_refused_as_busy() -> None:
    store = stored()
    session = workspace(store)
    await viewing(session, "viewer")
    other = await joined(session, "other", read=True)
    await say(other, "delete", DeleteSession(session_id=EARLIER))
    events = await received(other)
    await session.close()

    message = 'Can\'t delete "old work": 1 other frontend views it.'
    assert outcome(events) == CommandRefused(command_id="delete", code=BUSY, message=message)
    assert store.deleted == []


async def test_deleting_a_session_two_connections_view_names_them_both() -> None:
    session = workspace(stored())
    await viewing(session, "first")
    await viewing(session, "second")
    other = await joined(session, "other", read=True)
    await say(other, "delete", DeleteSession(session_id=EARLIER))
    events = await received(other)
    await session.close()

    message = 'Can\'t delete "old work": 2 other frontends view it.'
    assert outcome(events) == CommandRefused(command_id="delete", code=BUSY, message=message)


async def test_a_session_only_the_deleting_connection_views_is_deleted() -> None:
    store = stored()
    session = workspace(store)
    viewer = await viewing(session, "viewer")
    await say(viewer, "delete", DeleteSession(session_id=EARLIER))
    events = await received(viewer)
    await session.close()

    assert outcome(events) == CommandAccepted(command_id="delete")
    assert store.deleted == [EARLIER]


async def test_deleting_the_viewed_session_moves_the_connection_to_the_open_one() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "delete", DeleteSession(session_id=EARLIER))
    deleted = await received(viewer)
    await say(viewer, "add", AddTask(task=TASK))
    added = await received(viewer)
    await session.close()

    snapshot = snapshot_in(deleted)
    assert (snapshot.session.id, snapshot.active, snapshot.viewers) == (OPEN, True, 1)
    assert outcome(deleted) == CommandAccepted(command_id="delete")
    assert outcome(added) == CommandAccepted(command_id="add")


async def test_deleting_the_viewed_session_publishes_it_left_with_no_viewers() -> None:
    session = workspace(stored())
    viewer = await viewing(session, "viewer")
    await say(viewer, "delete", DeleteSession(session_id=EARLIER))
    deleted = await received(viewer)
    joining = connect(session)
    await say(joining, "joining", HELLO)
    [catalog] = [e for e in await received(joining) if isinstance(e, Catalog)]
    await session.close()

    assert states(deleted) == [(EARLIER, False, 0), (OPEN, True, 1)]
    [entry] = catalog.workspaces
    assert {s.id: s.viewers for s in entry.sessions} == {OPEN: 1}


async def test_a_connection_attaching_while_its_session_is_deleted_views_the_open_one() -> None:
    store = stored()
    session = workspace(store)
    deleting = await joined(session, "deleting", read=True)
    attaching = await joined(session, "attaching", read=True)
    release = asyncio.Event()

    async def held() -> None:
        await release.wait()

    store.on_delete = held
    delete = asyncio.create_task(say(deleting, "delete", DeleteSession(session_id=EARLIER)))
    await asyncio.sleep(0.01)
    await say(attaching, "attach", Attach(workspace=WORKSPACE, session=EARLIER))
    release.set()
    await delete
    events = await received(attaching)
    await session.close()

    assert store.deleted == [EARLIER]
    assert attaching.viewing is None
    snapshots = [e for e in events if isinstance(e, Snapshot)]
    assert [(s.session.id, s.active) for s in snapshots] == [(EARLIER, False), (OPEN, True)]


async def attached_meanwhile(meanwhile: Command) -> tuple[SessionConnection, list[Event]]:
    """A connection attaching to ``earlier`` while another sends ``meanwhile`` as the saved
    sessions are listed for it, and what reached it."""
    store = stored()
    session = workspace(store)
    other = await joined(session, "other", read=True)
    attaching = await joined(session, "attaching", read=True)

    async def sent() -> None:
        store.on_list = None
        await say(other, "meanwhile", meanwhile)

    store.on_list = sent
    await say(attaching, "attach", Attach(workspace=WORKSPACE, session=EARLIER))
    events = await received(attaching)
    await session.close()
    assert store.on_list is None
    return attaching, events


def answer(events: list[Event]) -> CommandAccepted | CommandRefused:
    """The answer to the command sent as ``attach`` among ``events``."""
    [answered] = [
        e
        for e in events
        if isinstance(e, CommandAccepted | CommandRefused) and e.command_id == "attach"
    ]
    return answered


async def test_attaching_to_a_session_deleted_while_it_is_read_is_refused_as_unknown() -> None:
    attaching, events = await attached_meanwhile(DeleteSession(session_id=EARLIER))

    refused = CommandRefused(command_id="attach", code=UNKNOWN, message=missing_message(EARLIER))
    assert answer(events) == refused
    assert attaching.viewing is None
    assert [e for e in events if isinstance(e, Snapshot)] == []


async def test_attaching_to_a_session_opened_while_it_is_read_attaches_live() -> None:
    attaching, events = await attached_meanwhile(ActivateSession(session=EARLIER))

    assert answer(events) == CommandAccepted(command_id="attach")
    assert attaching.viewing is None
    snapshots = [e for e in events if isinstance(e, Snapshot)]
    assert (snapshots[-1].session.id, snapshots[-1].active) == (EARLIER, True)
    assert all(s.active for s in snapshots)


async def attached_while_read(meanwhile: Command) -> tuple[SessionConnection, list[Snapshot]]:
    """A connection attaching first to ``earlier`` while another sends ``meanwhile`` as its
    transcript is read for the view, and the snapshots that reached it."""
    store = stored()
    session = workspace(store)
    other = await joined(session, "other", read=True)
    attaching = connect(session)
    await say(attaching, "attaching", HELLO)

    async def sent(session_id: str) -> None:
        store.on_recent = None
        await say(other, "meanwhile", meanwhile)

    store.on_recent = sent
    await say(attaching, "attach", Attach(workspace=WORKSPACE, session=EARLIER))
    events = await received(attaching)
    await session.close()
    assert store.on_recent is None
    return attaching, [e for e in events if isinstance(e, Snapshot)]


async def test_attaching_to_a_session_deleted_while_its_transcript_is_read_ends_live() -> None:
    attaching, snapshots = await attached_while_read(DeleteSession(session_id=EARLIER))

    assert attaching.attached is None
    assert (snapshots[-1].session.id, snapshots[-1].active) == (OPEN, True)


async def test_attaching_to_a_session_opened_while_its_transcript_is_read_sends_no_view() -> None:
    attaching, snapshots = await attached_while_read(ActivateSession(session=EARLIER))

    assert attaching.viewing is None
    assert (snapshots[-1].session.id, snapshots[-1].active) == (EARLIER, True)
    assert all(s.active for s in snapshots)


async def test_a_viewed_session_gone_from_the_list_is_named_by_its_id() -> None:
    store = stored()
    session = workspace(store)
    await viewing(session, "viewer")
    other = await joined(session, "other", read=True)
    store.saved.clear()
    await say(other, "delete", DeleteSession(session_id=EARLIER))
    events = await received(other)
    await session.close()

    message = f'Can\'t delete "{EARLIER}": 1 other frontend views it.'
    assert outcome(events) == CommandRefused(command_id="delete", code=BUSY, message=message)


async def failed_change(
    store: FakeStore, command: RenameSession | DeleteSession, breaking: Callable[[], None]
) -> tuple[list[Event], list[Event], list[Event]]:
    """What a connection viewing ``earlier`` in a session saved in ``store`` gets as it sends
    ``command`` once ``breaking`` breaks the store, and then as it runs tasks, and what one
    viewing the open session gets meanwhile."""
    session = workspace(store)
    viewer = await viewing(session, "viewer")
    watcher = await joined(session, "watcher", read=True)
    breaking()
    await say(viewer, "change", command)
    changed = await received(viewer)
    await say(viewer, "run", RunTasks())
    ran = await received(viewer)
    watched = await received(watcher)
    await session.close()
    return changed, ran, watched


async def test_a_failed_delete_leaves_the_connection_viewing_the_session() -> None:
    store = stored()

    def failing() -> None:
        store.delete_failure = "disk full"

    deleted, ran, watched = await failed_change(store, DeleteSession(session_id=EARLIER), failing)

    assert store.saved
    assert not any(isinstance(e, Snapshot) for e in deleted)
    assert ErrorOccurred(code=SAVE_FAILED, message="disk full") in watched
    assert ran == [CommandRefused(command_id="run", code=NOT_ACTIVE, message=NOT_ACTIVE_MESSAGE)]


async def test_a_delete_that_cant_list_the_sessions_leaves_the_connection_viewing_it() -> None:
    store = stored()

    def unlistable() -> None:
        store.list_failure = StoreFailed("store gone")

    deleted, ran, watched = await failed_change(
        store, DeleteSession(session_id=EARLIER), unlistable
    )

    assert store.deleted == []
    assert not any(isinstance(e, Snapshot) for e in deleted)
    assert any(isinstance(e, ErrorOccurred) for e in watched)
    assert ran == [CommandRefused(command_id="run", code=NOT_ACTIVE, message=NOT_ACTIVE_MESSAGE)]


async def test_a_rename_whose_save_fails_leaves_the_view_naming_the_old_name() -> None:
    store = stored()

    def failing() -> None:
        store.failure = "disk full"

    _, ran, _ = await failed_change(store, RenameSession(session_id=EARLIER, name="older"), failing)

    assert ran == [CommandRefused(command_id="run", code=NOT_ACTIVE, message=NOT_ACTIVE_MESSAGE)]


async def test_a_rename_that_cant_claim_the_session_leaves_the_view_naming_the_old_name() -> None:
    store = stored()

    def failing() -> None:
        store.lock_failure = "lock broken"

    _, ran, _ = await failed_change(store, RenameSession(session_id=EARLIER, name="older"), failing)

    assert ran == [CommandRefused(command_id="run", code=NOT_ACTIVE, message=NOT_ACTIVE_MESSAGE)]


async def test_attaching_to_a_session_opened_while_its_record_is_held_attaches_live() -> None:
    store = stored()
    session = workspace(store)
    other = await joined(session, "other", read=True)
    attaching = await joined(session, "attaching", read=True)
    release = asyncio.Event()

    async def held() -> None:
        store.on_list = None
        await release.wait()

    store.on_list = held
    attach = asyncio.create_task(
        say(attaching, "attach", Attach(workspace=WORKSPACE, session=EARLIER))
    )
    await asyncio.sleep(0.01)
    await say(other, "activate", ActivateSession(session=EARLIER))
    release.set()
    await attach
    events = await received(attaching)
    await session.close()

    assert answer(events) == CommandAccepted(command_id="attach")
    assert attaching.viewing is None
    snapshots = [e for e in events if isinstance(e, Snapshot)]
    assert (snapshots[-1].session.id, snapshots[-1].active) == (EARLIER, True)
    assert snapshots[-1].tasks == (SAVED_TASK,)
    assert all(s.active for s in snapshots)


async def attach_held(
    store: FakeStore, attaching: SessionConnection
) -> tuple[asyncio.Task[None], asyncio.Event]:
    """``attaching``'s attach to ``earlier``, held as its view's events are read from
    ``store`` until the event returned is set."""
    release = asyncio.Event()

    async def held(session_id: str) -> None:
        store.on_recent = None
        await release.wait()

    store.on_recent = held
    attach = asyncio.create_task(
        say(attaching, "attach", Attach(workspace=WORKSPACE, session=EARLIER))
    )
    await asyncio.sleep(0.01)
    return attach, release


async def test_a_session_opened_while_its_view_is_held_leaves_the_viewer_live() -> None:
    store = stored()
    session = workspace(store)
    other = await joined(session, "other", read=True)
    attaching = await joined(session, "attaching", read=True)
    attach, release = await attach_held(store, attaching)
    await say(other, "activate", ActivateSession(session=EARLIER))
    release.set()
    await attach
    snapshots = [e for e in await received(attaching) if isinstance(e, Snapshot)]
    await session.close()

    assert attaching.viewing is None
    assert (snapshots[-1].session.id, snapshots[-1].active) == (EARLIER, True)
    assert all(s.active for s in snapshots)


async def test_viewing_another_session_while_a_view_is_held_ends_on_the_other() -> None:
    store = stored()
    store.saved.append(SessionRecord(id=LATER, name="new work", created=WHEN, last_active=WHEN))
    session = workspace(store)
    attaching = await joined(session, "attaching", read=True)
    attach, release = await attach_held(store, attaching)
    await say(attaching, "later", Attach(workspace=WORKSPACE, session=LATER))
    release.set()
    await attach
    snapshots = [e for e in await received(attaching) if isinstance(e, Snapshot)]
    await session.close()

    assert attaching.viewing == SessionRef(id=LATER, name="new work")
    assert [s.session.id for s in snapshots] == [LATER]
