"""Merges held in a session while a set that holds them fails: the notice and a fix first; work
finishing meanwhile verified but waiting, unreviewed; the fix landing with the work beneath it;
the set run again, passing to lift the hold and land the rest, or failing until the fixes run out;
two sets holding at once; a hold kept across a restart; the holds read again on taking the
repository over; and none under ``--no-verify``."""

import asyncio

from orbit.agent import Verification, WorkspaceHost
from orbit.agent.domain.merge_holds import Hold
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import MergeHold, OwnershipOffered, RunTasks, TakeOwnership
from tests.agent_fakes import (
    ACCEPTED,
    LINT,
    OWNER,
    FakeLock,
    FakeStore,
    board_of,
    claims_done,
    events_until,
    greeted,
    holds_of,
    send,
    session_for,
    snapshot_of,
    start,
    turns_of,
)
from tests.merge_hold_helpers import (
    FAILED,
    HELD,
    NO_REVIEW,
    SOAK,
    STRESS,
    LandingTree,
    Seen,
    SetSteps,
    parts_of,
    reviews,
    status,
    waits_for,
)
from tests.waiting import eventually


async def test_held_work_waits_unreviewed_lands_beneath_the_fix_and_the_rest_once_passed() -> None:
    a_done, fix_done = asyncio.Event(), asyncio.Event()
    steps = SetSteps(stress=[False, True])
    session, _, _ = session_for(
        waits_for(a_done, "Add A"),
        waits_for(fix_done, "Fix stress"),
        claims_done,
        reviewers=(claims_done,) * 2,
        verification=Verification((LINT,), steps, sets=(STRESS,)),
        tree=LandingTree(),
        review=True,
    )
    seen = Seen(session)
    sets, holds = parts_of(session)
    await start(session, "Add A.", "Add B.")
    await eventually(lambda: status(session, "1")() == "running")
    sets.run("stress", "nightly")
    await eventually(lambda: holds.held and len(board_of(session).tasks) == 3)

    fix = board_of(session).tasks[0]
    assert (fix.id, fix.origin, fix.task.role, fix.status) == ("3", "nightly", "coder", "pending")
    assert fix.task.prompt.startswith("Fix the failing set: stress\n\n")
    assert FAILED in fix.task.prompt
    await eventually(lambda: HELD in seen.notices())

    a_done.set()
    await eventually(lambda: status(session, "3")() == "running")
    shown = await snapshot_of(session)
    assert shown.waiting_to_merge == ("1",)
    assert shown.merge_holds == (
        MergeHold(set="stress", commit="base", reason="Stress tests failed"),
    )
    assert status(session, "1")() == "completed"
    assert seen.merged() == [] and reviews(session) == []

    steps.gates["stress"].clear()  # The run after the fix lands waits.
    fix_done.set()
    await eventually(lambda: status(session, "2")() == "completed")
    await eventually(lambda: [state.id for state in reviews(session)] == ["4"])
    assert reviews(session)[0].task.prompt.startswith("Review commit c1\n")
    assert seen.merged() == [("1", "c1"), ("3", "c2")]
    assert (await snapshot_of(session)).waiting_to_merge == ("2",)
    assert steps.runs == ["stress", "stress"] and holds.held

    steps.gates["stress"].set()
    await eventually(lambda: len(reviews(session)) == 2)
    assert reviews(session)[1].task.prompt.startswith("Review commit c3\n")
    shown = await snapshot_of(session)
    seen.close()
    await session.close()
    assert seen.merged() == [("1", "c1"), ("3", "c2"), ("2", "c3")]
    assert seen.notices() == [HELD, "Merges resumed: stress passed."]
    assert (shown.waiting_to_merge, shown.merge_holds) == ((), ())


async def run_fix(session: WorkspaceHost, fixes: int) -> None:
    """Run the fix queued, the ``fixes``th, once it's on the board and the lane is idle, until
    it completes."""
    board = board_of(session)
    await eventually(lambda: len(board.tasks) == fixes and not turns_of(session).running)
    await send(session, RunTasks())
    await eventually(lambda: status(session, str(fixes))() == "completed")


async def test_a_set_failing_after_each_fix_stops_queuing_fixes_at_the_limit() -> None:
    steps = SetSteps(stress=[False] * 4)
    session, _, _ = session_for(
        claims_done,
        claims_done,
        claims_done,
        verification=Verification((LINT,), steps, fallback_fixes=3, sets=(STRESS,)),
        tree=LandingTree(),
        triggers=NO_REVIEW,
    )
    seen = Seen(session)
    sets, holds = parts_of(session)
    sets.run("stress", "nightly")
    for fixes in (1, 2, 3):
        await run_fix(session, fixes)
    step_in = (
        "stress still fails after 3 fixes; merges stay held. Release with ctrl+f or fix it"
        " yourself."
    )
    await eventually(lambda: step_in in seen.notices())
    seen.close()
    await session.close()

    assert len(board_of(session).tasks) == 3 and steps.runs == ["stress"] * 4
    assert seen.notices() == [HELD, step_in]
    assert holds.holds == (Hold("stress", "c3", "Stress tests", FAILED, 3),)


async def test_with_two_sets_holding_work_lands_only_once_both_pass() -> None:
    steps = SetSteps(stress=[False, True], soak=[False, True])
    session, _, _ = session_for(
        claims_done,
        claims_done,
        claims_done,
        verification=Verification((LINT,), steps, sets=(STRESS, SOAK)),
        tree=LandingTree(),
        triggers=NO_REVIEW,
    )
    seen = Seen(session)
    sets, holds = parts_of(session)
    sets.run("stress", "nightly")
    sets.run("soak", "nightly")
    await eventually(lambda: len(board_of(session).tasks) == 2)
    steps.gates["stress"].clear()
    steps.gates["soak"].clear()
    await start(session, "Add B.")
    await eventually(lambda: status(session, "3")() == "completed")
    assert seen.merged() == [("2", "c1"), ("1", "c2")]
    assert (await snapshot_of(session)).waiting_to_merge == ("3",)

    steps.gates["soak"].set()
    await eventually(lambda: "Merges resumed: soak passed." in seen.notices())
    assert holds.held and seen.merged() == [("2", "c1"), ("1", "c2")]
    steps.gates["stress"].set()
    await eventually(lambda: len(seen.merged()) == 3)
    seen.close()
    await session.close()
    assert seen.merged()[-1] == ("3", "c3") and not holds.held


async def test_a_hold_is_kept_across_closing_and_reopening() -> None:
    store = FakeStore()
    steps = SetSteps(stress=[False])
    session, _, _ = session_for(
        verification=Verification((LINT,), steps, sets=(STRESS,)),
        tree=LandingTree(),
        store=store,
        triggers=NO_REVIEW,
    )
    sets, _ = parts_of(session)
    await greeted(session)  # Reads the holds kept, without which none is saved.
    sets.run("stress", "nightly")
    await eventually(lambda: store.holds == (Hold("stress", "base", "Stress tests", FAILED, 1),))
    await session.close()

    reopened, _, _ = session_for(
        claims_done,
        verification=Verification((LINT,), SetSteps(), sets=(STRESS,)),
        tree=LandingTree(),
        store=store,
        triggers=NO_REVIEW,
    )
    seen = Seen(reopened)
    _, restored = parts_of(reopened)
    await eventually(lambda: restored.held)
    await start(reopened, "Add A.")
    await eventually(lambda: status(reopened, "1")() == "completed")
    shown = await snapshot_of(reopened)
    seen.close()
    await reopened.close()
    assert shown.merge_holds == (
        MergeHold(set="stress", commit="base", reason="Stress tests failed"),
    )
    assert shown.waiting_to_merge == ("1",) and seen.merged() == []


async def test_taking_over_reads_the_holds_the_owner_left_again() -> None:
    store = FakeStore()
    store.holds = (Hold("stress", "c4", "Stress tests", FAILED, 1),)
    session, _, _ = session_for(
        verification=Verification((LINT,), SetSteps(), sets=(STRESS, SOAK)),
        tree=LandingTree(),
        store=store,
        triggers=NO_REVIEW,
        owner=OWNER,
        lock=FakeLock(free=True),
    )
    _, holds = parts_of(session)
    await events_until(session, lambda event: isinstance(event, OwnershipOffered))
    await eventually(lambda: holds.held)
    store.holds = (Hold("soak", "c7", "Soak", "1 failed"),)  # The owner lifted stress's hold.
    await send(session, TakeOwnership(take=True))
    await events_until(session, lambda event: event == ACCEPTED)
    shown = await snapshot_of(session)
    await session.close()
    assert holds.views == (MergeHold(set="soak", commit="c7", reason="Soak failed"),)
    assert shown.merge_holds == holds.views


async def test_under_no_verify_nothing_holds_merges() -> None:
    store = FakeStore()
    store.holds = (Hold("stress", "base", "Stress tests", FAILED, 1),)
    trigger = Trigger.model_validate({"on": "task_landed", "checks": "stress"})
    session, _, _ = session_for(
        claims_done, tree=LandingTree(), store=store, triggers={**NO_REVIEW, "stress": trigger}
    )
    seen = Seen(session)
    await start(session)
    await eventually(lambda: seen.merged() == [("1", "c1")])
    seen.close()
    await session.close()
    assert holds_of(session) is None


async def test_the_fix_comes_from_the_trigger_that_ran_the_set() -> None:
    steps = SetSteps(stress=[False])
    trigger = Trigger.model_validate({"on": "task_finished", "checks": "stress"})
    session, _, _ = session_for(
        claims_done,
        verification=Verification((LINT,), steps, sets=(STRESS,)),
        tree=LandingTree(),
        triggers={**NO_REVIEW, "stress-check": trigger},
    )
    _, holds = parts_of(session)
    await start(session, "Add A.")
    await eventually(lambda: holds.held and len(board_of(session).tasks) == 2)
    await session.close()
    fix = board_of(session).tasks[0]
    assert (fix.origin, fix.task.prompt.partition("\n")[0]) == (
        "stress-check",
        "Fix the failing set: stress",
    )
