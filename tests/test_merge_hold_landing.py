"""Landing the work that waited for merges to resume, in a session: a hold lifting while a fix runs
lands the work beneath with it; work whose commits a rebase dropped landed nothing; a task that
committed nothing leaves nothing waiting; and once released, work that conflicts, or fails its
recheck after a rebase, is told of as ended without landing."""

import asyncio
from dataclasses import replace
from typing import Any

from orbit.agent import Verification, WorkspaceHost
from orbit.agent.application.ports import Merge
from orbit.agent.domain.commits import Commit
from orbit.agent.domain.merge_holds import Hold, title_fix
from orbit.protocol import TaskMerged
from tests.agent_fakes import (
    LINT,
    TRIGGERS,
    FakeStore,
    claims_done,
    session_for,
    snapshot_of,
    start,
    work_lane_of,
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
    reviews,
    status,
    waits_for,
)
from tests.waiting import eventually

KEPT = Hold("stress", "base", "Stress tests", FAILED, 1)


class SquashingTree(LandingTree):
    """A tree whose landings leave only the landing task's own commit, as a rebase drops the
    commits of the work beneath it left empty."""

    async def list_commits(self, start: str, end: str) -> list[Commit]:
        commits = await super().list_commits(start, end)
        return commits[-1:] if start == "base" else commits


def restored(**options: Any) -> tuple[WorkspaceHost, SetSteps, Seen]:
    """A session restoring the hold :data:`KEPT`, whose stress set passes when run, with
    ``options`` over the default, and the events it publishes."""
    store = FakeStore()
    store.holds = (KEPT,)
    steps = SetSteps(stress=[True])
    defaults: dict[str, Any] = {
        "verification": Verification((LINT,), steps, sets=(STRESS,)),
        "tree": LandingTree(),
        "store": store,
        "triggers": NO_REVIEW,
    }
    session, _, _ = session_for(claims_done, **{**defaults, **options})
    return session, steps, Seen(session)


async def waiting_in(session: WorkspaceHost) -> None:
    """Run a task once the hold is restored, until its work waits."""
    _, holds = parts_of(session)
    await eventually(lambda: holds.held)
    await start(session, "Add A.")
    await eventually(lambda: status(session, "1")() == "completed")


async def test_a_hold_lifting_as_a_fix_runs_lands_the_work_beneath_with_it() -> None:
    a_done, fix_done = asyncio.Event(), asyncio.Event()
    steps = SetSteps(stress=[False, True])
    session, _, _ = session_for(
        waits_for(a_done),
        waits_for(fix_done),
        verification=Verification((LINT,), steps, sets=(STRESS,)),
        tree=LandingTree(),
        triggers=NO_REVIEW,
    )
    seen = Seen(session)
    sets, holds = parts_of(session)
    await start(session, "Add A.")
    await eventually(lambda: status(session, "1")() == "running")
    sets.run("stress", "nightly")
    await eventually(lambda: holds.held)
    a_done.set()
    await eventually(lambda: status(session, "2")() == "running")
    sets.run("stress", "by-hand")  # The user fixed it; the fix still runs.
    await eventually(lambda: not holds.held)
    fix_done.set()
    await eventually(lambda: len(seen.merged()) == 2)
    seen.close()
    await session.close()
    assert seen.merged() == [("1", "c1"), ("2", "c2")]
    assert seen.notices() == [HELD, "Merges resumed: stress passed."]
    assert steps.runs == ["stress", "stress"]


def waiting(session: WorkspaceHost) -> tuple[str, ...]:
    """The ids of the tasks whose work waits in the session's tree."""
    lane = work_lane_of(session)
    assert lane is not None
    return lane.waiting_to_merge


async def test_work_beneath_whose_commits_were_dropped_landed_nothing() -> None:
    a_done = asyncio.Event()
    steps = SetSteps(stress=[False, True])
    session, _, _ = session_for(
        waits_for(a_done),
        claims_done,
        reviewers=(claims_done,),
        verification=Verification((LINT,), steps, sets=(STRESS,)),
        tree=SquashingTree(),
        review=True,
    )
    seen = Seen(session)
    sets, holds = parts_of(session)
    await start(session, "Add A.")
    await eventually(lambda: status(session, "1")() == "running")
    sets.run("stress", "nightly")
    await eventually(lambda: holds.held)
    a_done.set()
    await eventually(lambda: not holds.held)
    shown = await snapshot_of(session)
    seen.close()
    await session.close()
    assert seen.merged() == [("2", "c2")]
    assert shown.waiting_to_merge == () and reviews(session) == []


async def test_work_waiting_around_a_fix_that_conflicted_lands_with_its_own_commits() -> None:
    conflict = Merge("conflict", "The work on orbit/coder conflicts with main: a.txt.")
    store = FakeStore()
    store.holds = (replace(KEPT, fixes=3),)  # No fix is queued for the hold by itself.
    session, _, _ = session_for(
        claims_done,
        claims_done,
        claims_done,
        claims_done,
        verification=Verification((LINT,), SetSteps(stress=[True]), sets=(STRESS,)),
        tree=LandingTree(conflict),
        store=store,
        triggers=NO_REVIEW,
    )
    seen = Seen(session)
    _, holds = parts_of(session)
    await eventually(lambda: holds.held)
    fix = title_fix("stress")
    await start(session, "Add A.", fix, "Add B.", fix)
    await eventually(lambda: not holds.held)
    seen.close()
    await session.close()
    # Task 2's commit, c2, stays in the tree, beneath task 3's, as its merge conflicted.
    assert seen.merged() == [("1", "c1"), ("3", "c3"), ("4", "c4")]


async def test_a_task_that_committed_nothing_leaves_nothing_waiting() -> None:
    session, _, seen = restored(tree=LandingTree(empty=True))
    await waiting_in(session)
    shown = await snapshot_of(session)
    seen.close()
    await session.close()
    assert shown.waiting_to_merge == () and seen.merged() == []


async def test_released_work_that_conflicts_is_told_of_with_the_detail_and_landed_nothing() -> None:
    conflict = Merge("conflict", "The work on orbit/coder conflicts with main: a.txt.")
    session, _, seen = restored(tree=LandingTree(conflict), review=True, triggers=TRIGGERS)
    await waiting_in(session)
    sets, holds = parts_of(session)
    sets.run("stress", "nightly")
    await eventually(lambda: any(isinstance(e, TaskMerged) for e in seen.events))
    shown = await snapshot_of(session)
    seen.close()
    await session.close()
    assert [e for e in seen.events if isinstance(e, TaskMerged)] == [
        TaskMerged(task_id="1", result="conflict", detail=conflict.detail)
    ]
    assert shown.waiting_to_merge == () and not holds.held and reviews(session) == []


async def test_released_work_failing_its_recheck_after_a_rebase_landed_nothing() -> None:
    session, steps, seen = restored(tree=LandingTree(Merge("rebased")))
    steps.task_results = [True, False]
    await waiting_in(session)
    sets, holds = parts_of(session)
    sets.run("stress", "nightly")
    await eventually(lambda: not holds.held and steps.task_results == [])
    await eventually(lambda: waiting(session) == ())
    seen.close()
    await session.close()
    assert seen.merged() == []
