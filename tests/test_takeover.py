"""Taking over a freed repository: a read-only orbit offers to own it once its owner goes."""

import asyncio

from orbit.agent.application.session import OWNED_NOTE
from orbit.agent.application.takeover import NO_OFFER
from orbit.agent.application.work_turns import TASKS_RUNNING_MESSAGE
from orbit.agent.domain.ownership import WorkspaceOwner
from orbit.protocol import (
    BUSY,
    INVALID,
    AddTask,
    CommandRefused,
    ErrorOccurred,
    Event,
    NewSession,
    Notice,
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
    Call,
    FakeTree,
    events_until,
    greeted,
    idle,
    lane_of,
    queued,
    send,
    session_for,
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

    lane = lane_of(session)
    lane.kick()
    assert lane.running
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


async def test_staying_read_only_lets_the_lock_go_and_offers_it_again_later() -> None:
    lock = FakeLock(free=True)
    session, _, _ = session_for(owner=OWNER, lock=lock)
    await greeted(session)
    await events_until(session, offered)
    lock.failing = True  # Hold off the next offer until another orbit has had its chance.

    await send(session, TakeOwnership(take=False))
    await events_until(session, lambda event: event == ACCEPTED)
    assert lock.free and not lock.held
    assert session.read_only

    lock.failing = False
    await events_until(session, offered)
    assert session.read_only
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
