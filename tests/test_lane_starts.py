"""A lane cancelled as it starts a task still ends the task, which can then be run or removed."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from orbit.agent import Agents
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lanes import OPEN_GATE, SessionGate
from orbit.agent.application.review_lane import ReviewHooks, ReviewLane
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.verifier import Verifier
from orbit.agent.application.work_lane import Beside, RunEnd, WorkLane
from orbit.agent.application.work_turns import WorkParts, WorkTurns
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.endings import CANCELLED, STOPPED_BY_YOU
from orbit.agent.domain.reviews import ReviewLimits, ReviewPlanner
from orbit.protocol import (
    REVIEWER,
    Event,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskState,
    VerificationChanged,
)
from tests.agent_fakes import ROLES, FakeHost, FakeTree, ScriptedLauncher, Steps, verified


class Gate:
    """Publishing that holds, once, the first event ``holds`` picks, until :attr:`released` is
    set."""

    def __init__(self, holds: Callable[[Event], bool]) -> None:
        self.holds = holds
        self.reached = asyncio.Event()
        self.released = asyncio.Event()
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)
        if not self.reached.is_set() and self.holds(event):
            self.reached.set()
            await self.released.wait()


def running(event: Event) -> bool:
    """Whether ``event`` publishes a board with a task running."""
    return isinstance(event, TasksChanged) and any(t.status == "running" for t in event.tasks)


def desk_for(publish: Gate) -> AgentDesk:
    """A desk with no agents to run, in a tree, publishing through ``publish``."""
    host = FakeHost()
    agents = Agents(
        host, ScriptedLauncher(host), lambda: "token-1", ROLES, tree=FakeTree(), root="."
    )
    return AgentDesk(agents, publish)


async def no_work() -> None:
    """Wakes no work lane."""


def hooks(read_only: bool = False, halted: Callable[[], bool] = lambda: False) -> ReviewHooks:
    """Hooks waking no work lane and acting on no change, ``read_only`` or not, halted as
    ``halted`` says."""
    return ReviewHooks(no_work, no_work, SessionGate(halted, lambda: read_only))


def ended_and_free(tasks: Tasks, task_id: str, reason: str) -> None:
    """Assert task ``task_id`` was cancelled for ``reason``, and can run again and be removed."""
    state = tasks.board.get(task_id)
    assert (state.status, state.reason, state.reason_detail) == ("cancelled", reason, "")
    assert tasks.board.next_runnable() == task_id
    tasks.board.remove(task_id)


class Hearing(ReviewLane):
    """A review lane that notes each task it hears ended, landed or not, and reviews none."""

    def __init__(self, desk: AgentDesk, tasks: Tasks) -> None:
        super().__init__(desk, tasks, ReviewPlanner(ReviewLimits()), hooks())
        self.tasks = tasks
        self.heard: list[str] = []

    async def landed(self, state: TaskState, commit: str) -> None:
        self.heard.append(state.id)

    async def ended(self, state: TaskState) -> None:
        self.heard.append(state.id)


async def cancel_work(
    gate: Gate, stopped: bool = False, review: Hearing | None = None
) -> tuple[Tasks, str]:
    """Run a work lane publishing through ``gate``, beside ``review`` if any, and cancel its run
    once ``gate`` holds, or, if ``stopped``, stop it as the user does and let ``gate`` go;
    returns its tasks and the task it ran."""
    tasks = review.tasks if review is not None else Tasks(TaskBoard(), gate)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    lane = WorkLane(desk_for(gate), tasks, Verifier(verified(Steps()), tasks, gate), Beside(review))
    run = asyncio.create_task(lane.run())
    await gate.reached.wait()
    if stopped:
        assert lane.stopped_by_user()
        gate.released.set()
        assert await run == "cancelled"  # The user's stop ends the run, not its task.
    else:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        assert run.cancelled()
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
    lane = ReviewLane(desk_for(gate), tasks, ReviewPlanner(ReviewLimits()), hooks())
    lane.kick()
    await gate.reached.wait()
    await lane.close()
    ended_and_free(tasks, task_id, CANCELLED)


def review_lane_for(gate: Gate, built: ReviewHooks) -> ReviewLane:
    """A review lane with ``built``'s hooks and a review waiting, publishing through ``gate``."""
    tasks = Tasks(TaskBoard(), gate)
    tasks.board.add(TaskSpec(prompt="Review it.", role=REVIEWER))
    return ReviewLane(desk_for(gate), tasks, ReviewPlanner(ReviewLimits()), built)


@pytest.mark.parametrize("built", [hooks(read_only=True), hooks(halted=lambda: True)])
async def test_a_review_lane_built_read_only_or_halted_starts_no_review(
    built: ReviewHooks,
) -> None:
    lane = review_lane_for(Gate(running), built)
    lane.kick()
    assert not lane.running


async def test_a_halted_review_lane_starts_its_review_once_the_halt_lifts() -> None:
    halted = [True]
    gate = Gate(running)
    lane = review_lane_for(gate, hooks(halted=lambda: halted[0]))
    lane.kick()
    halted[0] = False
    lane.kick()
    await gate.reached.wait()
    assert lane.running
    await lane.close()


async def test_a_work_lane_cancelled_again_as_it_cancels_a_task_just_cancels_it() -> None:
    gate = Gate(running)
    tasks = Tasks(TaskBoard(), gate)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    lane = WorkLane(desk_for(gate), tasks, Verifier(verified(Steps()), tasks, gate), Beside())
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


async def test_a_work_lane_the_user_stops_as_it_closes_still_just_closes() -> None:
    gate = Gate(lambda event: isinstance(event, TaskStarted))
    review = hearing(gate)
    task_id = review.tasks.board.add(TaskSpec(prompt="a"))
    verifier = Verifier(verified(Steps()), review.tasks, gate)
    lane = WorkLane(desk_for(gate), review.tasks, verifier, Beside(review))
    run = asyncio.create_task(lane.run())
    await gate.reached.wait()
    gate.reached.clear()  # Hold again as the verification is marked cancelled.
    gate.holds = lambda event: isinstance(event, VerificationChanged)
    run.cancel()
    await gate.reached.wait()
    lane.stopped_by_user()
    gate.released.set()
    await asyncio.gather(run, return_exceptions=True)
    assert run.cancelled()
    ended_and_free(review.tasks, task_id, CANCELLED)
    assert review.heard == []


class Watched(WorkLane):
    """A work lane that keeps the turn each of its runs runs as."""

    def __init__(self, desk: AgentDesk, tasks: Tasks, verifier: Verifier, review: Hearing) -> None:
        super().__init__(desk, tasks, verifier, Beside(review))
        self.turns: list[asyncio.Task[object]] = []

    async def run(self) -> RunEnd:
        turn = asyncio.current_task()
        assert turn is not None
        self.turns.append(turn)
        return await super().run()


async def stop_turns_mid_run(stop: Callable[[WorkTurns], Awaitable[None]]) -> None:
    """Start a work lane's turn beside a review lane, ``stop`` the turns once its task has
    started, and assert the turn ended cancelled, its task too, telling the review lane
    nothing."""
    gate = Gate(lambda event: isinstance(event, TaskStarted))
    review = hearing(gate)
    tasks = review.tasks
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    desk, verifier = desk_for(gate), Verifier(verified(Steps()), tasks, gate)
    work = Watched(desk, tasks, verifier, review)
    turns = WorkTurns(WorkParts(work, review, tasks, verifier, desk, lambda _: None, OPEN_GATE))
    await turns.run_tasks()
    await gate.reached.wait()
    await stop(turns)
    [turn] = work.turns
    assert turn.cancelled()
    ended_and_free(tasks, task_id, CANCELLED)
    assert review.heard == []


async def test_closing_the_turns_mid_run_ends_the_turn_cancelled_telling_review_nothing() -> None:
    await stop_turns_mid_run(WorkTurns.close)


async def test_a_halt_mid_run_ends_the_turn_cancelled_telling_review_nothing() -> None:
    await stop_turns_mid_run(WorkTurns.stop_turn)
