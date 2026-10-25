"""The work lane's state for each task: landed work reaches review however the run ends, and
nothing a task leaves behind outlives it or its session."""

import asyncio
import itertools
from collections.abc import Awaitable, Callable

import pytest

from orbit.agent import Agents
from orbit.agent.application.coder_slots import CoderSeat, CoderSlots
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lanes import SessionGate
from orbit.agent.application.ports import Merge
from orbit.agent.application.review_lane import ReviewHooks, ReviewLane
from orbit.agent.application.tasks import Changed, Tasks
from orbit.agent.application.work_lane import Beside, WorkLane
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.endings import STOPPED_BY_YOU
from orbit.agent.domain.reviews import ReviewLimits, ReviewPlanner
from orbit.protocol import (
    ACTIVE_STATUSES,
    CODER,
    REVIEWER,
    Event,
    MergeResult,
    TaskMerged,
    TasksChanged,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import (
    ROLES,
    Call,
    FakeHost,
    FakeTree,
    Script,
    ScriptedLauncher,
    chats,
    claims_done,
    gives_up,
)
from tests.lane_fakes import Hearing, no_work
from tests.waiting import eventually

OTHERS_SLOT = ("other", "s")  # Another workspace's session, which holds the only coder's slot.


class Held:
    """Publishing that holds, once, the board with a task completed, until :attr:`released`
    is set."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.released = asyncio.Event()

    async def __call__(self, event: Event) -> None:
        if not self.reached.is_set() and completed(event):
            self.reached.set()
            await self.released.wait()


def completed(event: Event) -> bool:
    """Whether ``event`` publishes a board with a task completed."""
    return isinstance(event, TasksChanged) and any(t.status == "completed" for t in event.tasks)


async def quiet(event: Event) -> None:
    """Publishes nothing."""


def desk_for(
    publish: Callable[[Event], Awaitable[None]], *scripts: Script, tree: FakeTree | None = None
) -> AgentDesk:
    """A desk in ``tree``, or one that merges, whose agents play ``scripts``, publishing with
    ``publish``."""
    host = FakeHost()
    tokens = itertools.count(1)
    agents = Agents(
        host,
        ScriptedLauncher(host, *scripts),
        lambda: f"token-{next(tokens)}",
        ROLES,
        tree=tree or FakeTree(),
        root=".",
    )
    return AgentDesk(agents, publish)


def lane_for(
    publish: Callable[[Event], Awaitable[None]],
    tasks: Tasks,
    *scripts: Script,
    tree: FakeTree | None = None,
) -> tuple[WorkLane, Hearing]:
    """A work lane on ``tasks`` in ``tree``, or one that merges, whose agents play ``scripts``,
    publishing with ``publish``, beside a review lane that hears."""
    desk = desk_for(publish, *scripts, tree=tree)
    review = Hearing(desk, tasks)
    return WorkLane(desk, tasks, None, Beside(review)), review


def queueing_lane_for(
    tasks: Tasks, *scripts: Script, tree: FakeTree | None = None
) -> tuple[WorkLane, ReviewLane]:
    """A work lane on ``tasks`` in ``tree``, or one that merges, whose agents play ``scripts``,
    publishing nothing, beside a review lane that queues the reviews due but, read-only, runs
    none."""
    desk = desk_for(quiet, *scripts, tree=tree)
    hooks = ReviewHooks(no_work, no_work, SessionGate(read_only=lambda: True))
    review = ReviewLane(desk, tasks, ReviewPlanner(ReviewLimits()), hooks)
    return WorkLane(desk, tasks, None, Beside(review)), review


def removing_once_finished(board: TaskBoard, task_id: str) -> Changed:
    """A save, as the host's, during which task ``task_id`` is taken off ``board`` once it has
    finished, as a RemoveTask that arrives while the save is awaited is: the task is published
    as finished before the save, and a finished task isn't active."""

    async def save() -> None:
        state = board.find(task_id)
        if state is not None and state.status not in ("pending", *ACTIVE_STATUSES):
            board.remove(task_id)

    return save


async def run_reused(lane: WorkLane, tasks: Tasks, task_id: str) -> None:
    """Run ``lane`` once ``tasks``' board is emptied, as for a new session, and given a task
    that reuses id ``task_id``."""
    tasks.board.restore([])
    assert tasks.board.add(TaskSpec(prompt="b")) == task_id
    assert await asyncio.wait_for(lane.run(), 5) == "finished"


async def test_a_lane_cancelled_right_after_a_task_lands_still_reports_the_landing() -> None:
    held = Held()
    tasks = Tasks(TaskBoard(), held)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    lane, review = lane_for(held, tasks, claims_done)
    run = asyncio.create_task(lane.run())
    await asyncio.wait_for(held.reached.wait(), 5)

    run.cancel()
    await asyncio.gather(run, return_exceptions=True)

    assert run.cancelled()
    assert review.heard == [("landed", task_id, "c1")]


async def test_a_reused_id_after_a_cancelled_landing_reports_it_ended_not_the_old_commit() -> None:
    held = Held()
    tasks = Tasks(TaskBoard(), held)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    lane, review = lane_for(held, tasks, claims_done, gives_up)
    run = asyncio.create_task(lane.run())
    await asyncio.wait_for(held.reached.wait(), 5)
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)

    await run_reused(lane, tasks, task_id)

    assert review.heard == [("landed", task_id, "c1"), ("ended", task_id, "gave_up")]


async def test_after_a_reset_a_reused_id_that_lands_nothing_reports_it_ended() -> None:
    tasks = Tasks(TaskBoard(), quiet)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    lane, review = lane_for(quiet, tasks, claims_done, gives_up)
    assert await asyncio.wait_for(lane.run(), 5) == "finished"

    lane.reset()
    await run_reused(lane, tasks, task_id)

    assert review.heard == [("landed", task_id, "c1"), ("ended", task_id, "gave_up")]


async def test_a_task_that_errors_reports_it_ended() -> None:
    tasks = Tasks(TaskBoard(), quiet)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    lane, review = lane_for(quiet, tasks, chats)

    assert await asyncio.wait_for(lane.run(), 5) == "finished"

    assert tasks.board.get(task_id).status == "error"
    assert review.heard == [("ended", task_id, "error")]


async def test_a_reused_id_after_an_error_lands_from_its_own_start() -> None:
    tasks = Tasks(TaskBoard(), quiet)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    lane, review = lane_for(quiet, tasks, chats, claims_done)
    assert await asyncio.wait_for(lane.run(), 5) == "finished"

    await run_reused(lane, tasks, task_id)

    assert review.heard == [("ended", task_id, "error"), ("landed", task_id, "c1")]


async def test_a_task_removed_once_it_landed_still_has_its_commit_reviewed() -> None:
    board = TaskBoard()
    task_id = board.add(TaskSpec(prompt="a"))
    next_id = board.add(TaskSpec(prompt="b"))
    tasks = Tasks(board, quiet, removing_once_finished(board, task_id))
    lane, review = queueing_lane_for(tasks, claims_done, gives_up)

    assert await asyncio.wait_for(lane.run(), 5) == "finished"

    assert board.find(task_id) is None
    assert [(t.id, t.task.role, t.status) for t in board.tasks] == [
        (next_id, CODER, "gave_up"),
        ("3", REVIEWER, "pending"),
    ]
    assert [job.commits for job in review.snapshot().jobs.values()] == [("c1",)]


async def test_a_task_removed_once_it_ended_without_landing_does_not_stop_the_lane() -> None:
    board = TaskBoard()
    task_id = board.add(TaskSpec(prompt="a"))
    next_id = board.add(TaskSpec(prompt="b"))
    tasks = Tasks(board, quiet, removing_once_finished(board, task_id))
    lane, review = lane_for(quiet, tasks, gives_up, claims_done)

    assert await asyncio.wait_for(lane.run(), 5) == "finished"

    assert board.find(task_id) is None
    assert review.heard == [("ended", task_id, "gave_up"), ("landed", next_id, "c1")]


class Recording:
    """Publishing that keeps every event."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)


UNMERGED: tuple[MergeResult, ...] = ("conflict", "blocked", "detached")


@pytest.mark.parametrize("result", UNMERGED)
async def test_work_that_did_not_merge_ends_completed_without_landing(result: MergeResult) -> None:
    published = Recording()
    tasks = Tasks(TaskBoard(), published)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    tree = FakeTree(Merge(result, "why it didn't merge"))
    lane, review = lane_for(published, tasks, claims_done, gives_up, tree=tree)

    assert await asyncio.wait_for(lane.run(), 5) == "finished"

    assert tasks.board.get(task_id).status == "completed"
    merged = TaskMerged(task_id=task_id, result=result, detail="why it didn't merge", commit="c1")
    assert merged in published.events
    await run_reused(lane, tasks, task_id)  # Which lands nothing, so must not hear of "c1".
    assert review.heard == [("ended", task_id, "completed"), ("ended", task_id, "gave_up")]


@pytest.mark.parametrize("result", UNMERGED)
async def test_work_that_did_not_merge_does_not_count_toward_a_super_review(
    result: MergeResult,
) -> None:
    tasks = Tasks(TaskBoard(), quiet)
    tasks.board.add(TaskSpec(prompt="a"))
    tree = FakeTree(Merge(result, "why it didn't merge"))
    lane, review = queueing_lane_for(tasks, claims_done, tree=tree)

    assert await asyncio.wait_for(lane.run(), 5) == "finished"

    assert review.planner.since_super == 0
    assert review.snapshot().jobs == {}


async def test_work_that_merged_lands_its_commit_and_counts() -> None:
    tasks = Tasks(TaskBoard(), quiet)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    lane, review = queueing_lane_for(tasks, claims_done, tree=FakeTree(Merge("merged")))

    assert await asyncio.wait_for(lane.run(), 5) == "finished"

    assert review.planner.since_super == 1
    assert [job.commits for job in review.snapshot().jobs.values()] == [("c1",)]
    assert tasks.board.get(task_id).status == "completed"


async def test_work_whose_base_kept_moving_ends_without_landing() -> None:
    published = Recording()
    tasks = Tasks(TaskBoard(), published)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    tree = FakeTree(*[Merge("rebased")] * 4)
    lane, review = lane_for(published, tasks, claims_done, tree=tree)

    assert await asyncio.wait_for(lane.run(), 5) == "finished"

    assert tasks.board.get(task_id).status == "completed"
    [merged] = [e for e in published.events if isinstance(e, TaskMerged)]
    assert merged.result == "rebased" and "kept moving" in merged.detail
    assert review.heard == [("ended", task_id, "completed")]


class HeldHead(FakeTree):
    """A tree whose head, once its work merged, is read only after :attr:`released` is set;
    :attr:`merged` is set as the merge returns."""

    def __init__(self) -> None:
        super().__init__()
        self.merged = asyncio.Event()
        self.released = asyncio.Event()

    async def integrate(self) -> Merge:
        merge = await super().integrate()
        self.merged.set()
        return merge

    async def head(self) -> str:
        if self.merged.is_set():
            await self.released.wait()
        return await super().head()


async def test_a_stop_between_the_merge_and_its_report_leaves_the_task_landed() -> None:
    published = Recording()
    tasks = Tasks(TaskBoard(), published)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    tree = HeldHead()
    lane, review = lane_for(published, tasks, claims_done, tree=tree)
    run = asyncio.create_task(lane.run())
    await asyncio.wait_for(tree.merged.wait(), 5)

    assert lane.stopped_by_user()
    tree.released.set()
    assert await run == "cancelled"

    assert tasks.board.get(task_id).status == "completed"
    assert TaskMerged(task_id=task_id, result="merged", commit="c1") in published.events
    assert review.heard == [("landed", task_id, "c1")]
    assert tasks.board.next_runnable() is None


async def test_a_lane_cancelled_between_the_merge_and_its_report_still_reports_the_landing() -> (
    None
):
    published = Recording()
    tasks = Tasks(TaskBoard(), published)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    tree = HeldHead()
    lane, review = lane_for(published, tasks, claims_done, gives_up, tree=tree)
    run = asyncio.create_task(lane.run())
    await asyncio.wait_for(tree.merged.wait(), 5)

    run.cancel()
    await asyncio.sleep(0)
    tree.released.set()
    await asyncio.gather(run, return_exceptions=True)

    assert run.cancelled()
    assert TaskMerged(task_id=task_id, result="merged", commit="c1") in published.events
    await run_reused(lane, tasks, task_id)  # Which lands nothing, so must not hear of "c1".
    assert review.heard == [("landed", task_id, "c1"), ("ended", task_id, "gave_up")]


async def test_a_stop_during_the_agents_run_cancels_the_task_and_lands_nothing() -> None:
    working = asyncio.Event()

    async def works_on(call: Call) -> TurnEndReason:
        await call("get_task")
        working.set()
        await asyncio.Event().wait()  # Until the run is cancelled.
        return "completed"

    tasks = Tasks(TaskBoard(), quiet)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    tree = FakeTree()
    lane, review = lane_for(quiet, tasks, works_on, tree=tree)
    run = asyncio.create_task(lane.run())
    await asyncio.wait_for(working.wait(), 5)

    assert lane.stopped_by_user()
    assert await asyncio.wait_for(run, 5) == "cancelled"

    state = tasks.board.get(task_id)
    assert (state.status, state.reason) == ("cancelled", STOPPED_BY_YOU)
    assert tree.commits == []
    assert review.heard == [("ended", task_id, "cancelled")]


class Unwatchable(Tasks):
    """A board whose next publish can't be waited for: a bug in whatever waits."""

    async def moved(self) -> None:
        raise RuntimeError("no watching")


class NotingStart:
    """Publishing that notes the tasks still pending as the board is first published with a
    task running."""

    def __init__(self) -> None:
        self.pending: set[asyncio.Task[object]] | None = None

    async def __call__(self, event: Event) -> None:
        if self.pending is None and running(event):
            self.pending = {t for t in asyncio.all_tasks() if not t.done()}


def running(event: Event) -> bool:
    """Whether ``event`` publishes a board with a task running."""
    return isinstance(event, TasksChanged) and any(t.status == "running" for t in event.tasks)


async def waiting_lane_for(
    publish: Callable[[Event], Awaitable[None]], tasks: Tasks, *scripts: Script
) -> tuple[WorkLane, CoderSlots]:
    """A work lane on ``tasks``, whose agents play ``scripts``, publishing with ``publish``,
    seated at a single coder's slot that another workspace holds: its runs wait in line."""
    slots = CoderSlots(1)
    await slots.acquire(*OTHERS_SLOT)
    coders = CoderSeat(lambda: slots, "ws", lambda: "s1")
    lane = WorkLane(desk_for(publish, *scripts), tasks, None, Beside(coders=coders))
    return lane, slots


async def in_line(lane: WorkLane, run: asyncio.Task[object]) -> asyncio.Task[object]:
    """The task watching the board for ``lane``'s ``run``, once it waits in line for a slot."""
    await eventually(lambda: lane.waiting_for_coder == 1, poll=0)
    (watch,) = asyncio.all_tasks() - {asyncio.current_task(), run}
    return watch


async def test_the_watch_has_ended_before_a_seated_task_starts() -> None:
    noting = NotingStart()
    tasks = Tasks(TaskBoard(), noting)
    tasks.board.add(TaskSpec(prompt="a"))
    lane, slots = await waiting_lane_for(noting, tasks, claims_done)
    run = asyncio.create_task(lane.run())
    watch = await in_line(lane, run)

    slots.release()
    assert await run == "finished"
    assert noting.pending is not None
    assert watch not in noting.pending
    assert slots.taken == 0


async def test_the_watch_has_ended_once_the_run_leaves_the_line() -> None:
    tasks = Tasks(TaskBoard(), quiet)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    lane, slots = await waiting_lane_for(quiet, tasks, claims_done)
    run = asyncio.create_task(lane.run())
    watch = await in_line(lane, run)

    await tasks.change(lambda board: board.remove(task_id))
    assert await run == "finished"
    assert watch.done()
    assert slots.waiting == 0
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_stopping_a_run_as_it_waits_leaves_no_watch_behind() -> None:
    tasks = Tasks(TaskBoard(), quiet)
    tasks.board.add(TaskSpec(prompt="a"))
    lane, slots = await waiting_lane_for(quiet, tasks, claims_done)
    run = asyncio.create_task(lane.run())
    watch = await in_line(lane, run)

    assert lane.stopped_by_user()
    assert await run == "cancelled"
    assert watch.done()
    assert slots.waiting == 0
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_cancelling_a_run_as_it_waits_leaves_no_watch_behind() -> None:
    tasks = Tasks(TaskBoard(), quiet)
    tasks.board.add(TaskSpec(prompt="a"))
    lane, slots = await waiting_lane_for(quiet, tasks, claims_done)
    run = asyncio.create_task(lane.run())
    watch = await in_line(lane, run)

    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    assert run.cancelled()
    assert watch.done()
    assert slots.waiting == 0
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_a_watch_that_fails_fails_the_run_and_gives_the_slot_back() -> None:
    tasks = Unwatchable(TaskBoard(), quiet)
    tasks.board.add(TaskSpec(prompt="a"))
    lane, slots = await waiting_lane_for(quiet, tasks, claims_done)
    run = asyncio.create_task(lane.run())
    await in_line(lane, run)

    slots.release()
    with pytest.raises(RuntimeError, match="no watching"):
        await run
    assert slots.taken == 0
    assert asyncio.all_tasks() == {asyncio.current_task()}
