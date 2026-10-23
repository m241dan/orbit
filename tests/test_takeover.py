"""Taking over a freed repository: a read-only orbit offers to own it once its owner goes."""

import asyncio
from collections.abc import Sequence

from orbit.agent import WorkspaceHost
from orbit.agent.application.ports import StoreFailed
from orbit.agent.application.recovery import RecoveryFailed
from orbit.agent.application.takeover import NO_OFFER, RetryTimes, Takeover
from orbit.agent.application.work_turns import TASKS_RUNNING_MESSAGE
from orbit.agent.application.workspace_host import OWNED_NOTE
from orbit.agent.domain.ownership import queued_dropped_note
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.protocol import (
    BUSY,
    CONFIGURE_CHECKS,
    INTERNAL,
    INVALID,
    STORAGE_FAILED,
    ActivateSession,
    AddTask,
    Catalog,
    CommandRefused,
    ErrorOccurred,
    Event,
    FireTrigger,
    NewSession,
    Notice,
    OwnershipDeclined,
    OwnershipOffered,
    OwnershipTaken,
    RunTasks,
    Snapshot,
    TakeOwnership,
    TasksChanged,
    TaskSpec,
    TurnEndReason,
    WorkspaceEntry,
)
from tests.agent_fakes import (
    ACCEPTED,
    COMMAND_ID,
    LOCK_RETRY,
    OWNER,
    Call,
    Configurator,
    FakeFallback,
    FakeLock,
    FakeStore,
    FakeTree,
    claims_done,
    commands_of,
    events_until,
    greeted,
    idle,
    lane_of,
    no_steps,
    queued,
    send,
    session_for,
    sole,
    tasks_of,
    turns_of,
)
from tests.attach_fakes import EARLIER, stored
from tests.connection_helpers import views_on_joining


def offered(event: Event) -> bool:
    return isinstance(event, OwnershipOffered)


def refused(event: Event) -> bool:
    return isinstance(event, CommandRefused)


async def snapshot_on_joining(session: WorkspaceHost) -> Snapshot:
    """The Snapshot a new connection to ``session`` gets for its Hello and its Attach."""
    snapshot = (await views_on_joining(session))[-1]
    assert isinstance(snapshot, Snapshot)
    return snapshot


async def test_a_read_only_session_offers_the_repository_once_its_owner_goes() -> None:
    lock = FakeLock()
    session, _, _ = session_for(owner=OWNER, lock=lock)
    await greeted(session)
    await asyncio.sleep(0.05)
    assert not any(offered(event) for event in queued(session))

    lock.free = True
    await events_until(session, offered)
    assert lock.held
    await asyncio.sleep(0.05)
    assert not any(offered(event) for event in queued(session))  # Once, until answered.
    await session.close()


async def test_a_snapshot_says_an_offer_is_open_only_while_it_is() -> None:
    lock = FakeLock()
    session, _, _ = session_for(owner=OWNER, lock=lock)
    await greeted(session)
    assert not (await snapshot_on_joining(session)).ownership_offered

    lock.free = True
    await events_until(session, offered)
    assert (await snapshot_on_joining(session)).ownership_offered

    await send(session, TakeOwnership(take=True))
    await events_until(session, lambda event: event == ACCEPTED)
    assert not (await snapshot_on_joining(session)).ownership_offered
    await session.close()


async def test_taking_over_runs_the_queued_work_and_clears_read_only() -> None:
    lock = FakeLock()
    session, _, launcher = session_for(claims_done, owner=OWNER, lock=lock)
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send(session, RunTasks())
    lock.free = True
    await events_until(session, offered)

    await send(session, TakeOwnership(take=True))
    events = await events_until(session, idle())

    assert events.index(OwnershipTaken()) < events.index(Notice(message=OWNED_NOTE))
    assert ACCEPTED in events
    assert len(launcher.launches) == 1
    assert not session.read_only and session.queued_work == ()
    await send(session, NewSession())
    snapshot = (await events_until(session, lambda event: isinstance(event, Snapshot)))[-1]
    assert isinstance(snapshot, Snapshot) and not snapshot.read_only
    await session.close()


async def test_work_asked_once_owning_runs_at_once() -> None:
    lock = FakeLock(free=True)
    session, _, launcher = session_for(claims_done, owner=OWNER, lock=lock)
    await greeted(session)
    await events_until(session, offered)
    await send(session, TakeOwnership(take=True))
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send(session, RunTasks())
    await events_until(session, idle())

    assert len(launcher.launches) == 1
    await session.close()


async def test_taking_over_lets_reviews_run() -> None:
    lock = FakeLock(free=True)
    session, _, _ = session_for(owner=OWNER, lock=lock, tree=FakeTree(), review=True)
    await greeted(session)
    await events_until(session, offered)
    await send(session, TakeOwnership(take=True))

    assert lane_of(session).running
    await session.close()


async def test_taking_over_counts_on_from_the_super_count_the_owner_left() -> None:
    store = FakeStore(count=3)
    lock = FakeLock(free=True)
    session, _, _ = session_for(owner=OWNER, lock=lock, tree=FakeTree(), review=True, store=store)
    await events_until(session, offered)
    while lane_of(session).planner.since_super != 3:
        await asyncio.sleep(0)
    store.count = 5
    await send(session, TakeOwnership(take=True))
    await events_until(session, lambda event: event == ACCEPTED)

    assert lane_of(session).planner.since_super == 5
    await session._review_count.save(3)
    assert store.counts == [3]
    await session.close()


async def test_taking_over_clears_read_only_from_the_catalog_and_snapshot() -> None:
    lock = FakeLock(free=True)
    session, _, _ = session_for(owner=OWNER, lock=lock)
    await greeted(session)
    await events_until(session, offered)
    await send(session, TakeOwnership(take=True))
    events = await views_on_joining(session)

    [workspace] = sole(events, Catalog).workspaces
    assert not workspace.read_only and not sole(events, Snapshot).read_only
    await session.close()


async def test_taking_over_announces_the_workspace_no_longer_read_only() -> None:
    lock = FakeLock(free=True)
    session, _, _ = session_for(owner=OWNER, lock=lock)
    told: list[WorkspaceEntry] = []

    async def tell(entry: WorkspaceEntry) -> None:
        told.append(entry)

    await greeted(session)
    await events_until(session, offered)
    session.catalog_changed = tell
    await send(session, TakeOwnership(take=True))
    await events_until(session, lambda event: event == ACCEPTED)

    assert told == [await session.entry()] and not told[0].read_only
    await session.close()


async def test_declining_the_offer_announces_nothing() -> None:
    session, _, _ = session_for(owner=OWNER, lock=FakeLock(free=True))
    told: list[WorkspaceEntry] = []

    async def tell(entry: WorkspaceEntry) -> None:
        told.append(entry)

    session.catalog_changed = tell
    await declined(session)

    assert told == []
    await session.close()


async def test_queued_work_refused_once_owning_is_reported_as_an_error() -> None:
    release = asyncio.Event()

    async def waits(call: Call) -> TurnEndReason:
        await call("get_task")
        await release.wait()
        return "completed"

    lock = FakeLock()
    session, _, _ = session_for(waits, owner=OWNER, lock=lock)
    for prompt in ("Add a flag.", "Add a test."):
        await send(session, AddTask(task=TaskSpec(prompt=prompt)))
    await send(session, RunTasks())
    await send(session, RunTasks())
    lock.free = True
    await events_until(session, offered)

    await send(session, TakeOwnership(take=True))
    events = await events_until(session, lambda event: event == ACCEPTED)

    [error] = [event for event in events if isinstance(event, ErrorOccurred)]
    assert error == ErrorOccurred(code=BUSY, message=TASKS_RUNNING_MESSAGE)
    release.set()
    await session.close()


async def declined(session: WorkspaceHost) -> list[Event]:
    """The events up to accepting a decline of the offer ``session`` makes."""
    await greeted(session)
    await events_until(session, offered)
    await send(session, TakeOwnership(take=False))
    return await events_until(session, lambda event: event == ACCEPTED)


async def test_staying_read_only_lets_the_lock_go_at_once() -> None:
    lock = FakeLock(free=True)
    session, _, _ = session_for(owner=OWNER, lock=lock)

    assert OwnershipDeclined() in await declined(session)
    assert lock.free and not lock.held
    assert session.read_only
    assert not (await snapshot_on_joining(session)).ownership_offered
    await session.close()


async def test_a_declined_repository_left_free_is_not_offered_again_soon() -> None:
    lock = FakeLock(free=True)
    session, _, _ = session_for(owner=OWNER, lock=lock)
    await declined(session)
    tries = lock.tries

    await asyncio.sleep(LOCK_RETRY * 20)
    assert lock.tries > tries + 5
    assert not any(offered(event) for event in queued(session))
    assert lock.free and not lock.held
    await session.close()


async def test_a_declined_repository_is_offered_again_once_another_orbit_has_held_it() -> None:
    lock = FakeLock(free=True)
    session, _, _ = session_for(owner=OWNER, lock=lock)
    await declined(session)
    lock.free = False
    tries = lock.tries
    while lock.tries < tries + 2:
        await asyncio.sleep(LOCK_RETRY)

    lock.free = True
    await events_until(session, offered)
    assert lock.held and session.read_only
    await session.close()


async def test_a_declined_repository_still_free_is_offered_again_after_the_back_off() -> None:
    lock = FakeLock(free=True)
    session, _, _ = session_for(owner=OWNER, lock=lock, lock_declined_retry=LOCK_RETRY * 10)
    await declined(session)

    await events_until(session, offered)
    assert lock.held and session.read_only
    await session.close()


async def test_a_lock_that_cant_be_taken_is_tried_again() -> None:
    lock = FakeLock(free=True)
    lock.failing = True
    session, _, _ = session_for(owner=OWNER, lock=lock)
    await greeted(session)
    await asyncio.sleep(0.05)
    assert lock.tries > 1

    lock.failing = False
    await events_until(session, offered)
    await session.close()


async def test_taking_ownership_with_no_offer_open_is_refused() -> None:
    session, _, _ = session_for(owner=OWNER, lock=FakeLock())
    await send(session, TakeOwnership(take=True))
    events = await events_until(session, refused)
    assert events[-1] == CommandRefused(command_id=COMMAND_ID, code=INVALID, message=NO_OFFER)
    assert session.read_only
    await session.close()


async def test_an_owner_never_tries_the_lock_or_offers_it() -> None:
    lock = FakeLock(free=True)
    session, _, _ = session_for(lock=lock)
    await greeted(session)
    await asyncio.sleep(0.05)
    assert lock.tries == 0
    assert not any(offered(event) for event in queued(session))
    await send(session, TakeOwnership(take=True))
    await events_until(session, refused)
    await session.close()


async def offered_takeover(events: list[Event], lock: FakeLock) -> Takeover:
    """A read-only Takeover of ``lock``, publishing into ``events``, once it has offered it."""

    async def publish(event: Event) -> None:
        events.append(event)

    takeover = Takeover(True, lock, publish, RetryTimes(LOCK_RETRY))
    takeover.start()
    while not takeover.offered:
        await asyncio.sleep(LOCK_RETRY)
    return takeover


async def test_a_takeover_with_an_owner_is_read_only_until_taken() -> None:
    events: list[Event] = []
    takeover = await offered_takeover(events, FakeLock(free=True))
    assert takeover.read_only and events == [OwnershipOffered()]

    takeover.answer(True)
    assert not takeover.read_only
    await takeover.close()


async def test_a_takeover_declined_stays_read_only() -> None:
    lock = FakeLock(free=True)
    takeover = await offered_takeover([], lock)

    takeover.answer(False)
    assert takeover.read_only and not lock.held
    await takeover.close()


async def test_a_takeover_without_an_owner_is_not_read_only_and_never_tries_the_lock() -> None:
    async def publish(event: Event) -> None:
        raise AssertionError(event)

    lock = FakeLock(free=True)
    takeover = Takeover(False, lock, publish, RetryTimes(LOCK_RETRY))
    takeover.start()
    await asyncio.sleep(LOCK_RETRY * 5)
    assert not takeover.read_only and lock.tries == 0
    await takeover.close()


async def test_the_review_lane_and_work_commands_follow_the_takeover_once_taken() -> None:
    lock = FakeLock(free=True)
    session, _, launcher = session_for(
        claims_done, owner=OWNER, lock=lock, tree=FakeTree(), review=True
    )
    lane = lane_of(session)
    lane.kick()
    assert not lane.running
    await greeted(session)
    await events_until(session, offered)
    await send(session, TakeOwnership(take=True))
    await events_until(session, lambda event: event == ACCEPTED)

    assert lane.running
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send(session, RunTasks())
    await events_until(session, idle())
    assert session.queued_work == () and len(launcher.launches) == 1
    await session.close()


async def test_a_queued_run_that_fails_on_taking_over_is_reported_and_reviews_still_start() -> None:
    lock = FakeLock(free=True)
    session, _, launcher = session_for(
        claims_done, owner=OWNER, lock=lock, tree=FakeTree(), review=True
    )
    await greeted(session)
    await events_until(session, offered)
    await send(session, RunTasks())

    async def failing() -> None:
        raise RuntimeError("disk gone")

    turns_of(session).run_tasks = failing  # type: ignore[method-assign]
    await send(session, TakeOwnership(take=True))

    message = "Orbit failed to handle RunTasks: disk gone"
    await events_until(
        session, lambda event: event == ErrorOccurred(code=INTERNAL, message=message)
    )
    await events_until(session, lambda event: event == ACCEPTED)
    assert lane_of(session).running and launcher.launches == []
    await session.close()


async def test_reviews_still_start_when_running_the_queued_work_fails() -> None:
    lock = FakeLock(free=True)
    session, _, _ = session_for(claims_done, owner=OWNER, lock=lock, tree=FakeTree(), review=True)
    await greeted(session)
    await events_until(session, offered)

    async def failing(command_id: str) -> None:
        raise RuntimeError("outbox closed")

    commands_of(session).own = failing  # type: ignore[method-assign]
    await send(session, TakeOwnership(take=True))
    await events_until(session, refused)

    assert lane_of(session).running
    await session.close()


async def test_taking_over_leaves_the_work_commands_reviews_and_snapshot_all_owned() -> None:
    lock = FakeLock(free=True)
    session, _, launcher = session_for(
        claims_done, owner=OWNER, lock=lock, tree=FakeTree(), review=True
    )
    await greeted(session)
    assert (await snapshot_on_joining(session)).read_only
    await events_until(session, offered)
    await send(session, TakeOwnership(take=True))
    await events_until(session, lambda event: event == ACCEPTED)

    assert lane_of(session).running
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send(session, RunTasks())
    await events_until(session, idle())
    assert session.queued_work == () and len(launcher.launches) == 1
    assert not (await snapshot_on_joining(session)).read_only and not session.read_only
    await session.close()


async def test_taking_over_calls_owned_before_saying_so() -> None:
    lock = FakeLock(free=True)
    session, _, _ = session_for(owner=OWNER, lock=lock)
    calls: list[bool] = []
    session.owned = lambda: calls.append(session.read_only)
    await greeted(session)
    await events_until(session, offered)
    assert calls == []

    await send(session, TakeOwnership(take=True))
    await events_until(session, lambda event: isinstance(event, OwnershipTaken))
    assert calls == [False]
    await session.close()


async def test_declining_the_offer_does_not_call_owned() -> None:
    lock = FakeLock(free=True)
    session, _, _ = session_for(owner=OWNER, lock=lock)
    calls: list[bool] = []
    session.owned = lambda: calls.append(True)

    await declined(session)
    assert calls == []
    await session.close()


async def test_an_owner_from_the_start_never_calls_owned() -> None:
    session, _, _ = session_for(lock=FakeLock(free=True))
    calls: list[bool] = []
    session.owned = lambda: calls.append(True)
    await greeted(session)
    await send(session, TakeOwnership(take=True))
    await events_until(session, refused)
    assert calls == []
    await session.close()


RECOVERED_NOTE = "Recovered 2 events from .orbit/fallback/s1.jsonl into the session's log."


class FakeRecovery:
    """Recovers the fallback files the owner left, noting ``notes``, or fails with ``failure``;
    counts its ``runs``."""

    def __init__(self, *notes: str, failure: RecoveryFailed | None = None) -> None:
        self.notes = notes
        self.failure = failure
        self.runs = 0

    async def __call__(self) -> Sequence[str]:
        self.runs += 1
        if self.failure is not None:
            raise self.failure
        return self.notes


async def test_taking_over_recovers_the_parked_events_before_running_queued_work() -> None:
    lock, recovery = FakeLock(), FakeRecovery(RECOVERED_NOTE)
    session, _, _ = session_for(claims_done, owner=OWNER, lock=lock, recover=recovery)
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send(session, RunTasks())
    lock.free = True
    await events_until(session, offered)
    assert recovery.runs == 0

    await send(session, TakeOwnership(take=True))
    events = await events_until(session, idle())

    owned_at = events.index(Notice(message=OWNED_NOTE))
    recovered_at = events.index(Notice(message=RECOVERED_NOTE))
    assert owned_at < recovered_at < events.index(ACCEPTED)
    assert recovery.runs == 1
    await session.close()


async def test_a_recovery_that_fails_on_taking_over_is_reported_and_work_still_runs() -> None:
    failure = RecoveryFailed(".orbit/fallback/s1.jsonl", "Not JSON")
    lock, recovery = FakeLock(), FakeRecovery(failure=failure)
    session, _, launcher = session_for(claims_done, owner=OWNER, lock=lock, recover=recovery)
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send(session, RunTasks())
    lock.free = True
    await events_until(session, offered)

    await send(session, TakeOwnership(take=True))
    events = await events_until(session, idle())

    assert ErrorOccurred(code=INTERNAL, message=str(failure)) in events
    assert len(launcher.launches) == 1 and not session.read_only
    await session.close()


async def test_the_queued_work_does_not_start_once_recovering_on_taking_over_halts() -> None:
    lock, sessions = FakeLock(), list[WorkspaceHost]()

    async def halts() -> Sequence[str]:
        sessions[0]._halt.fail(FailedWrite.EVENT_LOG, StoreFailed("disk full"))
        return ()

    session, _, launcher = session_for(
        claims_done, owner=OWNER, lock=lock, recover=halts, fallback=FakeFallback()
    )
    sessions.append(session)
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send(session, RunTasks())
    lock.free = True
    await events_until(session, offered)

    await send(session, TakeOwnership(take=True))
    events = await events_until(session, lambda event: event == ACCEPTED)
    await session.close()

    errors = [event for event in events if isinstance(event, ErrorOccurred)]
    assert errors and all(error.code == STORAGE_FAILED for error in errors)
    assert launcher.launches == []
    assert tasks_of(session).board.tasks[0].status == "pending"


async def test_declining_the_offer_recovers_nothing() -> None:
    lock, recovery = FakeLock(free=True), FakeRecovery(RECOVERED_NOTE)
    session, _, _ = session_for(owner=OWNER, lock=lock, recover=recovery)

    events = await declined(session)

    assert Notice(message=RECOVERED_NOTE) not in events
    assert recovery.runs == 0
    await session.close()


async def test_a_host_given_nothing_to_recover_takes_over_with_no_recovered_note() -> None:
    session = WorkspaceHost(owner=OWNER, lock=FakeLock(free=True), lock_retry=LOCK_RETRY)
    await greeted(session)
    await events_until(session, offered)

    await send(session, TakeOwnership(take=True))
    events = await events_until(session, lambda event: event == ACCEPTED)

    notices = [event.message for event in events if isinstance(event, Notice)]
    assert notices == [OWNED_NOTE]
    assert not session.read_only
    await session.close()


class BlockedRecovery:
    """Recovers nothing, but only once ``release`` is set; ``started`` is set once it's run."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self) -> Sequence[str]:
        self.started.set()
        await self.release.wait()
        return ()


async def test_work_sent_while_taking_over_runs_after_the_work_queued_before() -> None:
    lock, recovery = FakeLock(), BlockedRecovery()
    session, _, launcher = session_for(
        Configurator(),
        claims_done,
        verification=no_steps(),
        owner=OWNER,
        lock=lock,
        recover=recovery,
    )
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send(session, RunTasks())
    lock.free = True
    await events_until(session, offered)

    taking = asyncio.create_task(send(session, TakeOwnership(take=True)))
    await recovery.started.wait()
    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    assert launcher.launches == []
    queued_then = session.queued_work
    assert queued_then == (RunTasks(), FireTrigger(trigger=CONFIGURE_CHECKS))
    recovery.release.set()
    await taking
    events = await events_until(session, idle())

    assert len(launcher.launches) == 2  # The configurator goes first, as it does while running.
    assert not any(isinstance(event, ErrorOccurred) for event in events)
    assert session.queued_work == ()
    await session.close()


async def test_work_sent_after_a_takeover_that_failed_runs_at_once() -> None:
    lock = FakeLock(free=True)
    session, _, launcher = session_for(claims_done, owner=OWNER, lock=lock)
    await greeted(session)
    await events_until(session, offered)

    async def failing() -> None:
        raise RuntimeError("store gone")

    session._switcher.publish_catalog = failing  # type: ignore[method-assign]
    await send(session, TakeOwnership(take=True))
    await events_until(session, refused)
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send(session, RunTasks())
    await events_until(session, idle())

    assert session.queued_work == () and len(launcher.launches) == 1
    await session.close()


def dropped_notes(events: Sequence[Event]) -> list[Event]:
    """The notices in ``events`` saying queued work was dropped by a switch."""
    return [e for e in events if isinstance(e, Notice) and e.message.startswith("Switched")]


async def switched_with_work_queued(switch: ActivateSession | NewSession) -> list[Event]:
    """The events of a read-only session that queues a run of its tasks, makes the ``switch``,
    adds a task there and takes ownership, checking nothing ran and nothing stayed queued."""
    lock = FakeLock()
    session, _, launcher = session_for(claims_done, owner=OWNER, lock=lock, store=stored())
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send(session, RunTasks())
    await send(session, switch)
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    events = await events_until(session, lambda event: isinstance(event, TasksChanged))
    lock.free = True
    events += await events_until(session, offered)

    await send(session, TakeOwnership(take=True))
    events += await events_until(session, lambda event: event == Notice(message=OWNED_NOTE))
    await asyncio.sleep(0.05)
    assert launcher.launches == [] and session.queued_work == ()
    await session.close()
    return events


async def test_switching_to_a_saved_session_drops_the_work_queued_and_says_so_once() -> None:
    events = await switched_with_work_queued(ActivateSession(session=EARLIER))
    assert dropped_notes(events) == [Notice(message=queued_dropped_note(1))]


async def test_switching_to_a_new_session_drops_the_work_queued_and_says_so_once() -> None:
    events = await switched_with_work_queued(NewSession())
    assert dropped_notes(events) == [Notice(message=queued_dropped_note(1))]


async def test_switching_with_nothing_queued_says_nothing_dropped() -> None:
    session, _, _ = session_for(owner=OWNER, store=stored())
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send(session, ActivateSession(session=EARLIER))
    events = await events_until(session, lambda event: isinstance(event, Snapshot))
    await asyncio.sleep(0.05)
    events += queued(session)
    await session.close()
    assert dropped_notes(events) == []
