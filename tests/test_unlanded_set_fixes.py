"""Fixes for a set holding merges that end without their work merging, in a session: giving up,
ending in error or meeting a conflict queues the next fix first, counted with the hold, until the
fixes run out and the user is asked to step in; a fix carrying on from one that failed
verification ends the chain instead, and one the user stops is left to run again."""

import asyncio

from orbit.agent import Verification, WorkspaceHost
from orbit.agent.application.ports import Merge, TreeFailed
from orbit.agent.domain.merge_holds import Hold
from orbit.protocol import CancelTurn, RunTasks
from tests.agent_fakes import (
    LINT,
    FakeStore,
    Script,
    board_of,
    claims_done,
    gives_up,
    greeted,
    send,
    session_for,
    turns_of,
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
    waits_for,
)
from tests.waiting import eventually

RESUMED = "Merges resumed: stress passed."


async def held_session(
    steps: SetSteps, *scripts: Script, tree: LandingTree | None = None, fixes: int = 3
) -> tuple[WorkspaceHost, FakeStore, Seen]:
    """A session whose fixes run through agents playing ``scripts``, in ``tree``, allowing
    ``fixes`` fixes, held as stress, run by ``steps``, failed on the base, with its first fix
    queued and the lane idle; the store it saves to, and what it publishes, gathered."""
    store = FakeStore()
    session, _, _ = session_for(
        *scripts,
        verification=Verification((LINT,), steps, fallback_fixes=fixes, sets=(STRESS,)),
        tree=tree or LandingTree(),
        store=store,
        triggers=NO_REVIEW,
    )
    seen = Seen(session)
    await greeted(session)  # Reads the holds kept, without which none is saved.
    sets, _ = parts_of(session)
    sets.run("stress", "nightly")
    await eventually(lambda: len(board_of(session).tasks) == 1 and not turns_of(session).running)
    return session, store, seen


def saved_fixes(store: FakeStore) -> list[int]:
    """The fix counts of stress's hold, as each save of the holds had it."""
    return [hold.fixes for holds in store.holds_saved for hold in holds]


async def close(session: WorkspaceHost, seen: Seen) -> None:
    seen.close()
    await session.close()


async def test_a_fix_that_gives_up_queues_the_next_first_and_counts_it() -> None:
    steps = SetSteps(stress=[False, True])
    session, store, seen = await held_session(steps, gives_up, claims_done)
    await send(session, RunTasks())
    await eventually(lambda: RESUMED in seen.notices())
    await close(session, seen)

    board = board_of(session)
    assert [(state.id, state.status) for state in board.tasks] == [
        ("2", "completed"),
        ("1", "gave_up"),
    ]
    fix = board.get("2")
    assert fix.origin == "nightly" and fix.task.prompt.startswith("Fix the failing set: stress\n")
    assert FAILED in fix.task.prompt
    assert saved_fixes(store) == [1, 2] and steps.runs == ["stress", "stress"]
    assert seen.notices() == [HELD, RESUMED]


async def test_a_fix_whose_merge_conflicts_queues_the_next() -> None:
    steps = SetSteps(stress=[False, True])
    tree = LandingTree(Merge("conflict", "it clashes"))
    session, store, seen = await held_session(steps, claims_done, claims_done, tree=tree)
    await send(session, RunTasks())
    await eventually(lambda: RESUMED in seen.notices())
    await close(session, seen)
    assert [state.id for state in board_of(session).tasks] == ["2", "1"]
    assert saved_fixes(store) == [1, 2] and steps.runs == ["stress", "stress"]


async def test_a_fix_ending_in_error_queues_the_next() -> None:
    steps = SetSteps(stress=[False, True])
    tree = LandingTree()
    session, store, seen = await held_session(steps, claims_done, tree=tree)
    tree.failures["prepare"] = TreeFailed("disk gone")
    await send(session, RunTasks())
    await eventually(lambda: RESUMED in seen.notices())
    await close(session, seen)
    board = board_of(session)
    assert [(state.id, state.status) for state in board.tasks] == [
        ("2", "completed"),
        ("1", "error"),
    ]
    assert saved_fixes(store) == [1, 2]


async def test_fixes_ending_unlanded_ask_the_user_to_step_in_once_they_run_out() -> None:
    steps = SetSteps(stress=[False])
    session, store, seen = await held_session(steps, gives_up, gives_up, fixes=2)
    await send(session, RunTasks())
    step_in = (
        "stress still fails after 2 fixes; merges stay held. Release with ctrl+f or fix it"
        " yourself."
    )
    await eventually(lambda: step_in in seen.notices())
    await close(session, seen)
    assert len(board_of(session).tasks) == 2 and seen.notices() == [HELD, step_in]
    assert store.holds == (Hold("stress", "base", "Stress tests", FAILED, 2),)


async def test_a_fix_carrying_on_from_a_failed_verification_ends_the_chain_first() -> None:
    steps = SetSteps(stress=[False, True])
    steps.task_results = [False, False]  # The first fix fails lint; a fix carries on from it.
    session, store, seen = await held_session(steps, claims_done, gives_up, claims_done)
    await send(session, RunTasks())
    await eventually(lambda: RESUMED in seen.notices())
    await close(session, seen)

    board = board_of(session)
    assert [(state.id, state.status, state.fixes) for state in board.tasks] == [
        ("3", "completed", None),
        ("1", "unverified", None),
        ("2", "gave_up", "1"),
    ]
    assert board.get("3").task.prompt.startswith("Fix the failing set: stress\n")
    assert saved_fixes(store) == [1, 2]


async def test_a_fix_the_user_stops_is_left_to_run_again_with_no_other_queued() -> None:
    gate = asyncio.Event()
    session, store, seen = await held_session(SetSteps(stress=[False]), waits_for(gate))
    await send(session, RunTasks())
    await eventually(lambda: status(session, "1")() == "running")
    await send(session, CancelTurn())
    await eventually(lambda: status(session, "1")() == "cancelled")
    await eventually(lambda: not turns_of(session).running)
    await close(session, seen)
    assert len(board_of(session).tasks) == 1 and saved_fixes(store) == [1]
    assert seen.notices() == [HELD]
