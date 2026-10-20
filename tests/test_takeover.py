"""Taking over a freed repository: a read-only orbit offers to own it once its owner goes."""

import asyncio

from orbit.agent import AgentSession
from orbit.agent.application.session import OWNED_NOTE
from orbit.agent.application.takeover import NO_OFFER, RetryTimes, Takeover
from orbit.agent.application.work_turns import TASKS_RUNNING_MESSAGE
from orbit.agent.domain.ownership import WorkspaceOwner
from orbit.protocol import (
    BUSY,
    INVALID,
    AddTask,
    Catalog,
    CommandEnvelope,
    CommandRefused,
    ErrorOccurred,
    Event,
    NewSession,
    Notice,
    OwnershipDeclined,
    OwnershipOffered,
    OwnershipTaken,
    RunTasks,
    Snapshot,
    TakeOwnership,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import (
    ACCEPTED,
    COMMAND_ID,
    HELLO,
    LOCK_RETRY,
    Call,
    FakeTree,
    events_until,
    greeted,
    idle,
    lane_of,
    queued,
    send,
    session_for,
    sole,
    views_on_joining,
)

OWNER = WorkspaceOwner(pid=42)


class FakeLock:
    """A repository's lock, held by another orbit until it's ``free``; ``failing`` makes
    taking it fail."""

    def __init__(self, *, free: bool = False) -> None:
        self.free = free
        self.held = False
        self.failing = False
        self.tries = 0

    def take(self) -> WorkspaceOwner | None:
        self.tries += 1
        if self.failing:
            raise OSError("No space left on device")
        if not self.free:
            return OWNER
        self.free, self.held = False, True
        return None

    def close(self) -> None:
        if self.held:
            self.free, self.held = True, False


async def claims_done(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("complete_task", summary="Done")
    return "completed"


def offered(event: Event) -> bool:
    return isinstance(event, OwnershipOffered)


def refused(event: Event) -> bool:
    return isinstance(event, CommandRefused)


async def snapshot_on_joining(session: AgentSession) -> Snapshot:
    """The Snapshot a new connection to ``session`` gets for its Hello."""
    connection = session.connect()
    await connection.send(CommandEnvelope(command_id="join", payload=HELLO))
    stream = connection.events()
    while not isinstance(payload := (await asyncio.wait_for(anext(stream), 5)).payload, Snapshot):
        pass
    return payload


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


async def test_taking_over_clears_read_only_from_the_catalog_and_snapshot() -> None:
    lock = FakeLock(free=True)
    session, _, _ = session_for(owner=OWNER, lock=lock)
    await greeted(session)
    await events_until(session, offered)
    await send(session, TakeOwnership(take=True))
    events = await views_on_joining(session)

    assert not sole(events, Catalog).read_only and not sole(events, Snapshot).read_only
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


async def declined(session: AgentSession) -> list[Event]:
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
