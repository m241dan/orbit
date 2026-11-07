"""The review lane forgetting what it booked once nothing needs it: a closed batch's reviews done
for good are dropped, while those a hold on merges still needs, or cut short and so kept from
running again, stay."""

from orbit.agent import Verification, WorkspaceHost
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.domain.merge_holds import FindingHold
from orbit.agent.domain.reviews import LaneSnapshot, PlannerSnapshot, ReviewJob
from orbit.protocol import BUGS_REVIEWER, CODER, TaskSpec, TaskState, TaskStatus
from tests.agent_fakes import (
    LINT,
    FakeStore,
    FakeTree,
    board_of,
    holds_of,
    lane_of,
    session_for,
)
from tests.merge_hold_helpers import SetSteps

TESTS = "tests-reviewer"
CHECKED = FindingHold(BUGS_REVIEWER, "c1", "Fix the crash", "4", "batch-2")
"""A hold whose fix, task 4 in batch-2, task 5 reviewed by its lens."""
UNCHECKED = FindingHold(TESTS, "c1", "Add the test", "6", "batch-7")
"""A hold whose fix, task 6 in batch-7, no review by its lens was booked for."""
UNCHECKED_LEFT = FindingHold(TESTS, "c1", "Add the test", "6", None)
FIX_REVIEW = ReviewJob(commits=("c2",), round=1, batch="batch-2", reviewed=("4",))
COMMIT_REVIEW = ReviewJob(commits=("c1",), round=0, batch=None, reviewed=("1",))


def task(task_id: str, role: str, status: TaskStatus) -> TaskState:
    return TaskState(id=task_id, task=TaskSpec(prompt=f"Task {task_id}", role=role), status=status)


BOARD = (
    task("1", CODER, "completed"),
    task("2", "reviewer", "completed"),  # Reviewed c1 into batch-1, which closed.
    task("3", "reviewer", "cancelled"),  # Cut short in batch-1.
    task("4", CODER, "completed"),
    task("5", BUGS_REVIEWER, "completed"),  # Reviewed the fix's batch-2 into batch-3.
    task("6", CODER, "completed"),
    task("7", CODER, "completed"),  # Booked nowhere; its end has the lane prune.
)
BOOKED = LaneSnapshot(
    planner=PlannerSnapshot(next_batch=8),
    jobs={"2": COMMIT_REVIEW, "3": COMMIT_REVIEW, "5": FIX_REVIEW},
    batches={"2": "batch-1", "3": "batch-1", "5": "batch-3"},
)


async def held_lane(*holds: FindingHold) -> tuple[WorkspaceHost, ReviewLane]:
    """A session holding merges for ``holds``, its lane and board as :data:`BOOKED` and
    :data:`BOARD` say, every batch closed, once a task booked nowhere ended."""
    store = FakeStore()
    store.holds = holds
    verification = Verification((LINT,), SetSteps())
    session, _, _ = session_for(
        tree=FakeTree(), review=True, verification=verification, store=store
    )
    restored = holds_of(session)
    assert restored is not None
    await restored.restore()
    board_of(session).restore(BOARD)
    lane = lane_of(session)
    lane.load(BOOKED)
    await lane.ended(board_of(session).get("7"))
    return session, lane


async def test_a_closed_batchs_reviews_done_for_good_are_forgotten_but_those_cut_short_kept() -> (
    None
):
    session, lane = await held_lane()
    snapshot = lane.snapshot()
    await session.close()
    assert snapshot.batches == {"3": "batch-1"}
    assert snapshot.jobs == {"3": COMMIT_REVIEW}


async def test_a_review_of_a_live_holds_fix_is_kept_and_an_unreviewed_fix_still_flagged() -> None:
    session, lane = await held_lane(CHECKED, UNCHECKED)
    snapshot = lane.snapshot()
    holds = holds_of(session)
    await session.close()
    assert snapshot.batches == {"3": "batch-1", "5": "batch-3"}
    assert snapshot.jobs == {"3": COMMIT_REVIEW, "5": FIX_REVIEW}
    # The unreviewed fix's hold stays, checked by no review; the reviewed one is still checked.
    assert holds is not None and holds.checking == (CHECKED,)
    assert holds.holds == (CHECKED, UNCHECKED_LEFT)
