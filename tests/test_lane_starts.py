"""A lane cancelled as it starts a task still ends the task, which can then be run or removed."""

import asyncio
from collections.abc import Callable

from orbit.agent import Agents
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.verifier import Verifier
from orbit.agent.application.work_lane import WorkLane
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.endings import CANCELLED, STOPPED_BY_YOU
from orbit.agent.domain.reviews import ReviewLimits, ReviewPlanner
from orbit.protocol import (
    REVIEWER,
    Event,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    VerificationChanged,
)
from tests.agent_fakes import ROLES, FakeHost, FakeTree, ScriptedLauncher, Steps, verified


class Gate:
    """Publishing that holds, once, the first event ``holds`` picks, until the test is done."""

    def __init__(self, holds: Callable[[Event], bool]) -> None:
        self.holds = holds
        self.reached = asyncio.Event()
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)
        if not self.reached.is_set() and self.holds(event):
            self.reached.set()
            await asyncio.Event().wait()


def running(event: Event) -> bool:
    """Whether ``event`` publishes a board with a task running."""
    return isinstance(event, TasksChanged) and any(t.status == "running" for t in event.tasks)


def desk_for(publish: Gate) -> AgentDesk:
    """A desk with no agents to run, in a tree, publishing through ``publish``."""
    host = FakeHost()
    agents = Agents(host, ScriptedLauncher(host), lambda: "token-1", ROLES, tree=FakeTree())
    return AgentDesk(agents, publish)


async def no_work() -> None:
    """Wakes no work lane."""


def ended_and_free(tasks: Tasks, task_id: str, reason: str) -> None:
    """Assert task ``task_id`` was cancelled for ``reason``, and can run again and be removed."""
    state = tasks.board.get(task_id)
    assert (state.status, state.reason, state.reason_detail) == ("cancelled", reason, "")
    assert tasks.board.next_runnable() == task_id
    tasks.board.remove(task_id)


class Hearing(ReviewLane):
    """A review lane that notes each task it hears ended, landed or not, and reviews none."""

    def __init__(self, desk: AgentDesk, tasks: Tasks) -> None:
        super().__init__(desk, tasks, ReviewPlanner(ReviewLimits()), no_work)
        self.tasks = tasks
        self.heard: list[str] = []

    async def landed(self, task_id: str, commit: str) -> None:
        self.heard.append(task_id)

    async def ended(self, task_id: str) -> None:
        self.heard.append(task_id)


async def cancel_work(
    gate: Gate, stopped: bool = False, review: Hearing | None = None
) -> tuple[Tasks, str]:
    """Run a work lane publishing through ``gate``, beside ``review`` if any, and cancel it once
    ``gate`` holds, as the user does if ``stopped``; returns its tasks and the task it ran."""
    tasks = review.tasks if review is not None else Tasks(TaskBoard(), gate)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    lane = WorkLane(desk_for(gate), tasks, Verifier(verified(Steps()), tasks, gate), review)
    run = asyncio.create_task(lane.run())
    await gate.reached.wait()
    if stopped:
        lane.stopped_by_user()
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    return tasks, task_id


async def test_a_work_lane_cancelled_while_starting_a_task_cancels_it() -> None:
    tasks, task_id = await cancel_work(Gate(running))
    ended_and_free(tasks, task_id, CANCELLED)


async def test_a_work_lane_the_user_stops_while_starting_a_task_says_so() -> None:
    tasks, task_id = await cancel_work(Gate(running), stopped=True)
    ended_and_free(tasks, task_id, STOPPED_BY_YOU)


async def test_a_work_lane_cancelled_while_resetting_verification_cancels_the_task() -> None:
    gate = Gate(lambda event: isinstance(event, VerificationChanged))
    tasks, task_id = await cancel_work(gate)
    assert running(gate.events[0])
    ended_and_free(tasks, task_id, CANCELLED)


async def test_a_review_lane_cancelled_while_starting_a_review_cancels_it() -> None:
    gate = Gate(running)
    tasks = Tasks(TaskBoard(), gate)
    task_id = tasks.board.add(TaskSpec(prompt="Review it.", role=REVIEWER))
    lane = ReviewLane(desk_for(gate), tasks, ReviewPlanner(ReviewLimits()), no_work)
    lane.kick()
    await gate.reached.wait()
    await lane.close()
    ended_and_free(tasks, task_id, CANCELLED)


async def test_a_work_lane_cancelled_again_as_it_cancels_a_task_just_cancels_it() -> None:
    gate = Gate(running)
    tasks = Tasks(TaskBoard(), gate)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    lane = WorkLane(desk_for(gate), tasks, Verifier(verified(Steps()), tasks, gate), None)
    run = asyncio.create_task(lane.run())
    await gate.reached.wait()
    gate.reached.clear()  # Hold again as the verification is marked cancelled.
    gate.holds = lambda event: isinstance(event, VerificationChanged)
    run.cancel()
    await gate.reached.wait()
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    ended_and_free(tasks, task_id, CANCELLED)


def hearing(gate: Gate) -> Hearing:
    """A review lane hearing of the tasks on a new board publishing through ``gate``."""
    return Hearing(desk_for(gate), Tasks(TaskBoard(), gate))


async def test_a_work_lane_closed_while_running_a_task_tells_the_review_lane_nothing() -> None:
    gate = Gate(lambda event: isinstance(event, TaskStarted))
    review = hearing(gate)
    tasks, task_id = await cancel_work(gate, review=review)
    ended_and_free(tasks, task_id, CANCELLED)
    assert review.heard == []


async def test_a_task_the_user_stops_is_still_reported_to_the_review_lane() -> None:
    gate = Gate(lambda event: isinstance(event, TaskStarted))
    review = hearing(gate)
    tasks, task_id = await cancel_work(gate, stopped=True, review=review)
    ended_and_free(tasks, task_id, STOPPED_BY_YOU)
    assert review.heard == [task_id]
