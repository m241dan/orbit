"""Releasing the holds on merges by hand: ``ReleaseHold`` lifts every hold with its notice and
lands the work waiting, in order; it's refused while no hold is on; a set failing again after a
release starts a new hold; and one sent while read-only is queued, then run on taking the
repository over, on the holds the owner left."""

from orbit.agent import Verification, WorkspaceHost
from orbit.agent.application.merge_holds import MergeHolds
from orbit.agent.domain.merge_holds import NOT_HELD, Hold, released_note, word_unpassed
from orbit.protocol import (
    INVALID,
    Command,
    CommandRefused,
    ErrorOccurred,
    Event,
    MergeHold,
    Notice,
    OwnershipOffered,
    ReleaseHold,
    TakeOwnership,
    command_adapter,
)
from tests.agent_fakes import (
    ACCEPTED,
    LINT,
    OWNER,
    FakeLock,
    FakeStore,
    board_of,
    claims_done,
    commands_of,
    events_until,
    of_type,
    refused,
    send,
    session_for,
    snapshot_of,
    start,
)
from tests.merge_hold_helpers import (
    FAILED,
    HELD,
    NO_REVIEW,
    STRESS,
    LandingTree,
    Seen,
    SetSteps,
    parts_of,
    status,
)
from tests.waiting import eventually

HOLD = Hold("stress", "c4", "Stress tests", FAILED, 1)
"""A hold the owner of the repository keeps."""


async def refusal_of(session: WorkspaceHost, command: Command) -> Event:
    """What ``command``, sent to ``session``, is refused with."""
    await send(session, command)
    return (await events_until(session, of_type(CommandRefused)))[-1]


async def read_only_on(store: FakeStore) -> WorkspaceHost:
    """A session whose sets hold merges, read-only on a repository whose holds ``store`` keeps,
    once it's offered the repository."""
    session, _, _ = session_for(
        verification=Verification((LINT,), SetSteps(), sets=(STRESS,)),
        tree=LandingTree(),
        store=store,
        triggers=NO_REVIEW,
        owner=OWNER,
        lock=FakeLock(free=True),
    )
    await events_until(session, lambda event: isinstance(event, OwnershipOffered))
    return session


async def queue_release(session: WorkspaceHost) -> None:
    """Send ``session``, read-only, a ``ReleaseHold``, and wait until it's accepted."""
    await send(session, ReleaseHold())
    await events_until(session, lambda event: event == ACCEPTED)


async def take_over(session: WorkspaceHost) -> list[Event]:
    """Have ``session`` take the repository over; returns the events until that's accepted."""
    await send(session, TakeOwnership(take=True))
    return await events_until(session, lambda event: event == ACCEPTED)


def views_shown(holds: MergeHolds) -> list[tuple[MergeHold, ...]]:
    """The holds on as frontends see them, each time ``holds`` tells it changed from now on."""
    shown: list[tuple[MergeHold, ...]] = []
    told = holds.changed

    def note() -> None:
        shown.append(holds.views)
        told()

    holds.changed = note
    return shown


def test_the_wording_of_a_release_by_hand() -> None:
    assert NOT_HELD == "Merges aren't held."
    assert released_note(("stress",)) == "Merges resumed by hand; stress hasn't passed."
    assert word_unpassed(("stress", "soak")) == "stress, soak haven't passed"


def test_release_hold_round_trips() -> None:
    command = ReleaseHold()
    assert command_adapter.validate_json(command.model_dump_json()) == command


async def test_release_hold_lands_the_waiting_work_and_a_failure_then_holds_anew() -> None:
    steps = SetSteps(stress=[False, False])
    session, _, _ = session_for(
        claims_done,
        claims_done,
        verification=Verification((LINT,), steps, sets=(STRESS,)),
        tree=LandingTree(),
        triggers=NO_REVIEW,
    )
    seen = Seen(session)
    sets, holds = parts_of(session)
    sets.run("stress", "nightly")
    await eventually(lambda: len(board_of(session).tasks) == 1)
    steps.gates["stress"].clear()  # The run after the fix lands waits.
    await start(session, "Add B.")
    await eventually(lambda: status(session, "2")() == "completed")
    assert (await snapshot_of(session)).waiting_to_merge == ("2",)
    assert seen.merged() == [("1", "c1")] and holds.held

    await send(session, ReleaseHold())
    await eventually(lambda: len(seen.merged()) == 2)
    shown = await snapshot_of(session)
    assert (shown.merge_holds, shown.waiting_to_merge) == ((), ())
    assert seen.merged()[-1] == ("2", "c2")

    steps.gates["stress"].set()  # Failing again starts a new hold, with a fix of its own.
    await eventually(lambda: holds.held and len(board_of(session).tasks) == 3)
    seen.close()
    await session.close()
    assert seen.notices() == [
        HELD,
        "Merges resumed by hand; stress hasn't passed.",
        "Merges held: stress failed on c1. A fix is queued first.",
    ]
    assert holds.holds == (Hold("stress", "c1", "Stress tests", FAILED, 1),)
    assert board_of(session).tasks[0].status == "pending"


async def test_release_hold_with_no_hold_on_is_refused() -> None:
    session, _, _ = session_for(
        verification=Verification((LINT,), SetSteps(), sets=(STRESS,)),
        tree=LandingTree(),
        triggers=NO_REVIEW,
    )
    refusal = await refusal_of(session, ReleaseHold())
    await session.close()
    assert refusal == refused(INVALID, "Merges aren't held.")


async def test_release_hold_without_sets_that_hold_merges_is_refused() -> None:
    session, _, _ = session_for()
    refusal = await refusal_of(session, ReleaseHold())
    await session.close()
    assert refusal == refused(INVALID, "Merges aren't held.")


async def test_release_hold_sent_read_only_with_a_hold_on_is_queued_until_taking_over() -> None:
    store = FakeStore()
    store.holds = (HOLD,)
    session = await read_only_on(store)
    _, holds = parts_of(session)
    await eventually(lambda: holds.held)
    await queue_release(session)
    queued = commands_of(session).queued
    await session.close()
    assert queued == (ReleaseHold(),)
    assert holds.holds == (HOLD,) and store.holds == (HOLD,)


async def test_a_release_queued_read_only_is_refused_once_the_owner_lifted_the_hold() -> None:
    store = FakeStore()
    store.holds = (HOLD,)
    session = await read_only_on(store)
    _, holds = parts_of(session)
    await eventually(lambda: holds.held)
    await queue_release(session)
    store.holds = ()  # The owner released the hold before handing over.
    events = await take_over(session)
    shown = await snapshot_of(session)
    await session.close()
    assert shown.merge_holds == ()
    assert ErrorOccurred(code=INVALID, message="Merges aren't held.") in events
    assert not any(isinstance(event, Notice) and "by hand" in event.message for event in events)


async def test_a_release_queued_read_only_lifts_a_hold_the_owner_started_since() -> None:
    store = FakeStore()
    session = await read_only_on(store)
    _, holds = parts_of(session)
    shown = views_shown(holds)
    await queue_release(session)
    store.holds = (HOLD,)  # The owner started a hold after this orbit opened.
    events = await take_over(session)
    await session.close()
    assert (MergeHold(set="stress", commit="c4", reason="Stress tests failed"),) in shown
    assert Notice(message="Merges resumed by hand; stress hasn't passed.") in events
    assert holds.holds == () and not store.holds  # Saved, released.


async def test_release_hold_sent_read_only_without_sets_that_hold_merges_is_refused() -> None:
    session, _, _ = session_for(owner=OWNER, lock=FakeLock(free=True))
    await events_until(session, lambda event: isinstance(event, OwnershipOffered))
    refusal = await refusal_of(session, ReleaseHold())
    queued = commands_of(session).queued
    await session.close()
    assert refusal == refused(INVALID, "Merges aren't held.") and queued == ()
