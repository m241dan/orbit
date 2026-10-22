"""The work lane's state for each task: landed work reaches review however the run ends, and
nothing a task leaves behind outlives it or its session."""

import asyncio
import itertools
from collections.abc import Awaitable, Callable

from orbit.agent import Agents
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lanes import SessionGate
from orbit.agent.application.review_lane import ReviewHooks, ReviewLane
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.work_lane import Beside, WorkLane
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.reviews import ReviewLimits, ReviewPlanner
from orbit.protocol import Event, TasksChanged, TaskSpec, TurnEndReason
from tests.agent_fakes import ROLES, Call, FakeHost, FakeTree, Script, ScriptedLauncher, claims_done


async def no_work() -> None:
    """Wakes no work lane."""


class Hearing(ReviewLane):
    """A review lane that notes how it hears each task ended, and reviews none."""

    def __init__(self, desk: AgentDesk, tasks: Tasks) -> None:
        hooks = ReviewHooks(no_work, no_work, SessionGate(lambda: False))
        super().__init__(desk, tasks, ReviewPlanner(ReviewLimits()), hooks)
        self.heard: list[tuple[str, ...]] = []

    async def landed(self, task_id: str, commit: str) -> None:
        self.heard.append(("landed", task_id, commit))

    async def ended(self, task_id: str) -> None:
        self.heard.append(("ended", task_id))


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


async def gives_up(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("give_up", reason="blocked")
    return "completed"


async def wanders_off(call: Call) -> TurnEndReason:
    return "completed"  # Without calling get_task, which ends the task in error.


def lane_for(
    publish: Callable[[Event], Awaitable[None]], tasks: Tasks, *scripts: Script
) -> tuple[WorkLane, Hearing]:
    """A work lane on ``tasks`` in a tree whose agents play ``scripts``, publishing with
    ``publish``, beside a review lane that hears."""
    host = FakeHost()
    tokens = itertools.count(1)
    agents = Agents(
        host,
        ScriptedLauncher(host, *scripts),
        lambda: f"token-{next(tokens)}",
        ROLES,
        tree=FakeTree(),
    )
    desk = AgentDesk(agents, publish)
    review = Hearing(desk, tasks)
    return WorkLane(desk, tasks, None, Beside(review)), review


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

    tasks.board.restore([])
    assert tasks.board.add(TaskSpec(prompt="b")) == task_id
    assert await asyncio.wait_for(lane.run(), 5) == "finished"

    assert review.heard == [("landed", task_id, "c1"), ("ended", task_id)]


async def test_after_a_reset_a_reused_id_that_lands_nothing_reports_it_ended() -> None:
    tasks = Tasks(TaskBoard(), quiet)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    lane, review = lane_for(quiet, tasks, gives_up)
    lane._landed[task_id] = "stale"  # A landing the session left never reported.
    lane._starts[task_id] = "stale"

    lane.reset()
    assert lane._landed == {} and lane._starts == {}
    assert await asyncio.wait_for(lane.run(), 5) == "finished"

    assert review.heard == [("ended", task_id)]


async def test_a_task_that_errors_leaves_no_start_behind() -> None:
    tasks = Tasks(TaskBoard(), quiet)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    lane, review = lane_for(quiet, tasks, wanders_off)

    assert await asyncio.wait_for(lane.run(), 5) == "finished"

    assert tasks.board.get(task_id).status == "error"
    assert lane._starts == {}
    assert review.heard == [("ended", task_id)]


async def test_a_reused_id_after_an_error_lands_from_its_own_start() -> None:
    tasks = Tasks(TaskBoard(), quiet)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    lane, review = lane_for(quiet, tasks, wanders_off, claims_done)
    assert await asyncio.wait_for(lane.run(), 5) == "finished"

    tasks.board.restore([])
    assert tasks.board.add(TaskSpec(prompt="b")) == task_id
    assert await asyncio.wait_for(lane.run(), 5) == "finished"

    assert lane._starts == {}
    assert review.heard == [("ended", task_id), ("landed", task_id, "c1")]
