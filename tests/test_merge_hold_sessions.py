"""Merges held in a session while a set that holds them fails: the notice and a fix first; work
finishing meanwhile verified but waiting, unreviewed; the fix landing with the work beneath it;
the set run again, passing to lift the hold and land the rest, or failing until the fixes run out;
two sets holding at once; a hold kept across a restart, and the work waiting under it, which lands
and is reviewed once it lifts, as it does on opening where none is left, and on a halt lifting
only once the holds kept are read; the work waiting kept with the session a switch leaves; the
holds read again on taking the repository over; and none under ``--no-verify``."""

import asyncio
from typing import Any

from orbit.agent import Verification, WorkspaceHost
from orbit.agent.domain.merge_holds import Hold
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import (
    STORAGE_FAILED,
    ActivateSession,
    AddTask,
    ErrorOccurred,
    Event,
    MergeHold,
    NewSession,
    OwnershipOffered,
    RunTasks,
    TakeOwnership,
)
from tests.agent_fakes import (
    ACCEPTED,
    LINT,
    OWNER,
    TRIGGERS,
    FakeLock,
    FakeStore,
    board_of,
    claims_done,
    events_until,
    greeted,
    holds_of,
    releasing_of,
    resume_storage,
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
from tests.server_fakes import TASK
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


KEPT = Hold("stress", "base", "Stress tests", FAILED, 1)


def held_session(store: FakeStore, tree: LandingTree, **options: Any) -> WorkspaceHost:
    """A session saved in ``store``, working in ``tree``, whose coder's tasks claim done and
    whose stress set holds merges, passing when run, with ``options`` over the default."""
    defaults: dict[str, Any] = {
        "verification": Verification((LINT,), SetSteps(stress=[True]), sets=(STRESS,)),
        "tree": tree,
        "store": store,
        "triggers": NO_REVIEW,
    }
    session, _, _ = session_for(claims_done, claims_done, **{**defaults, **options})
    return session


async def left_waiting(store: FakeStore, tree: LandingTree) -> None:
    """Run two tasks in a session holding the hold :data:`KEPT`, saved in ``store``, until
    their work waits in ``tree``, then close it."""
    store.holds = (KEPT,)
    session = held_session(store, tree)
    _, holds = parts_of(session)
    await greeted(session)
    await eventually(lambda: holds.held)
    await start(session, "Add A.", "Add B.")
    await eventually(lambda: status(session, "2")() == "completed")
    assert (await snapshot_of(session)).waiting_to_merge == ("1", "2")
    await session.close()


async def reopened(store: FakeStore, tree: LandingTree, **options: Any) -> WorkspaceHost:
    """The session saved in ``store`` opened again in a new one, as a restart does, working in
    ``tree``, with ``options`` as :func:`held_session` takes them."""
    session = held_session(store, tree, new_id=lambda: "session-2", **options)
    await session.open_latest()
    return session


async def test_work_waiting_across_a_restart_lands_and_is_reviewed_once_the_hold_lifts() -> None:
    store, tree = FakeStore(), LandingTree()
    await left_waiting(store, tree)

    session = await reopened(
        store, tree, reviewers=(claims_done,) * 2, review=True, triggers=TRIGGERS
    )
    seen = Seen(session)
    sets, holds = parts_of(session)
    await eventually(lambda: holds.held)
    assert (await snapshot_of(session)).waiting_to_merge == ("1", "2")
    sets.run("stress", "nightly")
    await eventually(lambda: len(reviews(session)) == 2)
    shown = await snapshot_of(session)
    seen.close()
    await session.close()
    assert seen.merged() == [("1", "c1"), ("2", "c2")]
    assert [state.task.prompt.partition("\n")[0] for state in reviews(session)] == [
        "Review commit c1",
        "Review commit c2",
    ]
    assert shown.waiting_to_merge == () and store.saved[-1].waiting == ()


async def test_work_waiting_across_a_restart_lands_as_the_holds_read_show_none_left() -> None:
    store, tree = FakeStore(), LandingTree()
    await left_waiting(store, tree)
    store.holds = ()  # Another orbit lifted the hold meanwhile.

    session = await reopened(store, tree)
    seen = Seen(session)
    await eventually(lambda: len(seen.merged()) == 2)
    seen.close()
    await session.close()
    assert seen.merged() == [("1", "c1"), ("2", "c2")]


async def halt(session: WorkspaceHost, store: FakeStore, seen: Seen) -> None:
    """Halt ``session``, saved in ``store``, with a failing save, as ``seen`` sees it, and
    have saves work again, ready to resume."""
    store.failure = "disk full"
    await send(session, AddTask(task=TASK))
    await eventually(lambda: any(storage_failed(event) for event in seen.events))
    store.failure = None


def storage_failed(event: Event) -> bool:
    return isinstance(event, ErrorOccurred) and event.code == STORAGE_FAILED


async def test_work_waiting_lands_on_a_halt_lifting_only_once_the_holds_kept_are_read() -> None:
    store, tree = FakeStore(), LandingTree()
    await left_waiting(store, tree)
    store.holds_load_failure = "disk gone"

    session = await reopened(store, tree)
    seen = Seen(session)
    _, holds = parts_of(session)
    await eventually(lambda: any(isinstance(event, ErrorOccurred) for event in seen.events))
    await halt(session, store, seen)
    await resume_storage(session)
    await eventually(lambda: not releasing_of(session).running)
    assert seen.merged() == [] and (await snapshot_of(session)).waiting_to_merge == ("1", "2")

    store.holds, store.holds_load_failure = (), None
    await holds.restore()
    await eventually(lambda: len(seen.merged()) == 2)
    seen.close()
    await session.close()
    assert seen.merged() == [("1", "c1"), ("2", "c2")]


async def test_work_waiting_lands_on_a_halt_lifting_with_the_holds_known_and_none_on() -> None:
    store, tree = FakeStore(), LandingTree()
    await left_waiting(store, tree)

    session = await reopened(store, tree)
    seen = Seen(session)
    _, holds = parts_of(session)
    await eventually(lambda: holds.held)
    await halt(session, store, seen)
    await holds.release()  # Lands nothing while halted.
    assert seen.merged() == []
    await resume_storage(session)
    await eventually(lambda: len(seen.merged()) == 2)
    seen.close()
    await session.close()
    assert seen.merged() == [("1", "c1"), ("2", "c2")]


async def test_work_waiting_across_a_restart_into_no_verify_lands_as_the_session_opens() -> None:
    store, tree = FakeStore(), LandingTree()
    await left_waiting(store, tree)

    session = await reopened(store, tree, verification=None)
    seen = Seen(session)
    await eventually(lambda: len(seen.merged()) == 2)
    seen.close()
    await session.close()
    assert seen.merged() == [("1", "c1"), ("2", "c2")] and holds_of(session) is None


async def test_a_switch_keeps_the_work_waiting_with_the_session_left_until_it_opens_again() -> None:
    store, tree = FakeStore(), LandingTree()
    store.holds = (KEPT,)
    ids = iter(("session-1", "session-2"))
    session = held_session(store, tree, new_id=lambda: next(ids))
    seen = Seen(session)
    sets, holds = parts_of(session)
    await eventually(lambda: holds.held)
    await start(session, "Add A.")
    await eventually(lambda: status(session, "1")() == "completed")

    await send(session, NewSession(name="other"))
    await eventually(lambda: board_of(session).tasks == ())
    assert (await snapshot_of(session)).waiting_to_merge == ()
    await send(session, ActivateSession(session="session-1"))
    await eventually(lambda: len(board_of(session).tasks) == 1)
    assert (await snapshot_of(session)).waiting_to_merge == ("1",) and seen.merged() == []

    sets.run("stress", "nightly")
    await eventually(lambda: len(seen.merged()) == 1)
    seen.close()
    await session.close()
    assert seen.merged() == [("1", "c1")]


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
