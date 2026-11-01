"""Releasing the holds on merges by hand: ``ReleaseHold`` lifts every hold with its notice and
lands the work waiting, in order; it's refused while no hold is on; and a set
failing again after a release starts a new hold."""

from orbit.agent import Verification, WorkspaceHost
from orbit.agent.domain.merge_holds import NOT_HELD, Hold, released_note, unpassed
from orbit.protocol import (
    INVALID,
    Command,
    CommandRefused,
    Event,
    ReleaseHold,
    command_adapter,
)
from tests.agent_fakes import (
    LINT,
    board_of,
    claims_done,
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


async def refusal_of(session: WorkspaceHost, command: Command) -> Event:
    """What ``command``, sent to ``session``, is refused with."""
    await send(session, command)
    return (await events_until(session, of_type(CommandRefused)))[-1]


def test_the_wording_of_a_release_by_hand() -> None:
    assert NOT_HELD == "Merges aren't held."
    assert released_note(("stress",)) == "Merges resumed by hand; stress hasn't passed."
    assert unpassed(("stress", "soak")) == "stress, soak haven't passed"


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
