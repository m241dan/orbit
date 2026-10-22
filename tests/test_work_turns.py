"""When the work lane runs: the user's runs, a review's wake, a hold, and the configurator."""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import pytest

from orbit.agent import Agents
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.verifier import Verifier
from orbit.agent.application.work_lane import WorkLane
from orbit.agent.application.work_turns import (
    CONFIGURING_NOTE,
    NO_AGENTS_MESSAGE,
    NO_VERIFY_MESSAGE,
    TASKS_RUNNING_MESSAGE,
    WorkParts,
    WorkTurns,
)
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.endings import STOPPED_BY_YOU
from orbit.agent.domain.refusals import Refused
from orbit.protocol import (
    BUSY,
    CONFIGURATOR,
    NOT_CONFIGURED,
    WORK_LANE,
    BusyChanged,
    ErrorOccurred,
    Event,
    Notice,
    Settings,
    TaskSpec,
)
from tests.agent_fakes import ROLES, FakeHost, ScriptedLauncher, Steps, hangs, verified

TASK = TaskSpec(prompt="Add a flag.", settings=Settings(model="opus"))


class StopsCounted(WorkLane):
    """A work lane that counts the times it was told the user stopped its run."""

    stops = 0

    def stopped_by_user(self) -> bool:
        self.stops += 1
        return super().stopped_by_user()


@dataclass
class Halt:
    """Whether the session is halted, as a test sets it."""

    on: bool = False


@dataclass
class Lane:
    """Work turns over a ``work`` lane and a board, with the events they posted."""

    turns: WorkTurns
    work: StopsCounted
    tasks: Tasks
    posted: list[Event]
    halt: Halt

    def roles(self) -> list[str]:
        return [state.task.role for state in self.tasks.board.tasks]


def lane_parts(
    tasks: Tasks,
    posted: list[Event],
    verifying: bool,
    halted: Callable[[], bool] = lambda: False,
) -> WorkParts:
    """The parts of work turns whose agents all hang, verified if ``verifying``, in a session
    halted while ``halted`` holds."""

    async def publish(event: Event) -> None:
        posted.append(event)

    host = FakeHost()
    launcher = ScriptedLauncher(host, *[hangs] * 5)
    agents = Agents(host, launcher, lambda: "token-1", ROLES)
    desk = AgentDesk(agents, publish)
    verifier = Verifier(verified(Steps()), tasks, publish) if verifying else None
    work = StopsCounted(desk, tasks, verifier, None)
    return WorkParts(work, None, tasks, verifier, desk, posted.append, halted)


@pytest.fixture
async def lane() -> AsyncIterator[Lane]:
    posted: list[Event] = []
    halt = Halt()
    tasks = Tasks(TaskBoard(), lambda _: asyncio.sleep(0))
    parts = lane_parts(tasks, posted, verifying=True, halted=lambda: halt.on)
    turns = WorkTurns(parts)
    assert isinstance(parts.work, StopsCounted)
    yield Lane(turns, parts.work, tasks, posted, halt)
    await turns.close()
    assert parts.desk is not None
    await parts.desk.stop()


async def add(lane: Lane) -> None:
    await lane.tasks.change(lambda board: board.add(TASK))


async def turn_ended(lane: Lane) -> None:
    """Yield to the loop until the lane's turn ends, or give up after a while."""
    for _ in range(100):
        if not lane.turns.running:
            return
        await asyncio.sleep(0)


async def held(lane: Lane) -> None:
    """Run a task, then hold the lane and wait for the run to end, leaving a task waiting."""
    await stopped(lane)
    await add(lane)


async def stopped(lane: Lane) -> None:
    """Run a task, then hold the lane and wait for the run to end."""
    await add(lane)
    await lane.turns.run_tasks()
    lane.turns.hold()
    await turn_ended(lane)


async def test_a_run_for_the_user_starts_the_lane(lane: Lane) -> None:
    await add(lane)
    await lane.turns.run_tasks()
    assert lane.turns.running


async def test_a_second_run_while_one_runs_is_refused_as_busy(lane: Lane) -> None:
    await add(lane)
    await lane.turns.run_tasks()
    with pytest.raises(Refused) as refused:
        await lane.turns.run_tasks()
    assert (refused.value.code, str(refused.value)) == (BUSY, TASKS_RUNNING_MESSAGE)
    assert lane.turns.running


async def test_a_wake_starts_waiting_work_when_nothing_holds_it(lane: Lane) -> None:
    await add(lane)
    await lane.turns.wake()
    assert lane.turns.running


async def test_a_wake_while_running_leaves_the_run_alone(lane: Lane) -> None:
    await add(lane)
    await lane.turns.run_tasks()
    await add(lane)
    await lane.turns.wake()
    assert lane.turns.running


async def test_a_wake_while_halted_starts_no_work(lane: Lane) -> None:
    await add(lane)
    lane.halt.on = True
    await lane.turns.wake()
    assert not lane.turns.running
    assert BusyChanged(busy=True, lane=WORK_LANE) not in lane.posted


async def test_a_wake_once_the_halt_lifts_starts_waiting_work(lane: Lane) -> None:
    await add(lane)
    lane.halt.on = True
    await lane.turns.wake()
    lane.halt.on = False
    await lane.turns.wake()
    assert lane.turns.running
    assert BusyChanged(busy=True, lane=WORK_LANE) in lane.posted


async def test_a_hold_cancels_the_run(lane: Lane) -> None:
    await held(lane)
    assert not lane.turns.running


async def test_a_hold_ends_the_run_by_itself(lane: Lane) -> None:
    await add(lane)
    await lane.turns.run_tasks()
    lane.turns.hold()
    await turn_ended(lane)
    assert not lane.turns.running


async def test_a_hold_mid_task_stops_it_and_the_run_ends_by_itself(lane: Lane) -> None:
    await add(lane)
    [task] = lane.tasks.board.tasks
    await lane.turns.run_tasks()
    for _ in range(100):
        if lane.tasks.board.get(task.id).status == "running":
            break
        await asyncio.sleep(0)
    lane.turns.hold()
    await turn_ended(lane)
    stopped_task = lane.tasks.board.get(task.id)
    assert (stopped_task.status, stopped_task.reason) == ("cancelled", STOPPED_BY_YOU)
    assert not any(isinstance(event, ErrorOccurred) for event in lane.posted)


async def test_a_hold_tells_the_work_lane_the_user_stopped_its_run(lane: Lane) -> None:
    await add(lane)
    await lane.turns.run_tasks()
    lane.turns.hold()
    await turn_ended(lane)
    assert lane.work.stops == 1


async def test_a_hold_with_nothing_running_tells_the_work_lane_nothing(lane: Lane) -> None:
    lane.turns.hold()
    assert lane.work.stops == 0


async def test_a_hold_keeps_a_wake_from_starting_work(lane: Lane) -> None:
    await held(lane)
    await lane.turns.wake()
    assert not lane.turns.running


async def test_a_run_for_the_user_lifts_a_hold(lane: Lane) -> None:
    await stopped(lane)
    [stopped_task] = lane.tasks.board.tasks
    await lane.tasks.change(lambda board: board.finish(stopped_task.id, "completed"))
    await lane.turns.run_tasks()
    assert not lane.turns.running
    await add(lane)
    await lane.turns.wake()
    assert lane.turns.running


async def test_a_hold_with_nothing_running_still_holds(lane: Lane) -> None:
    lane.turns.hold()
    await add(lane)
    await lane.turns.wake()
    assert not lane.turns.running


async def test_configuring_while_idle_queues_the_configurator_first_and_runs_it(
    lane: Lane,
) -> None:
    await add(lane)
    await lane.turns.configure_checks()
    assert lane.roles() == [CONFIGURATOR, "coder"]
    assert lane.turns.running


async def test_configuring_while_running_queues_the_configurator_first_without_starting(
    lane: Lane,
) -> None:
    await add(lane)
    await lane.turns.run_tasks()
    await add(lane)
    await lane.turns.configure_checks()
    assert lane.roles() == [CONFIGURATOR, "coder", "coder"]
    assert lane.turns.running


async def test_configuring_with_a_configurator_queued_gives_the_notice(lane: Lane) -> None:
    await lane.turns.configure_checks()
    await lane.turns.configure_checks()
    assert lane.roles() == [CONFIGURATOR]
    assert lane.posted[-1] == Notice(message=CONFIGURING_NOTE)


async def test_without_verification_configuring_is_refused() -> None:
    tasks = Tasks(TaskBoard(), lambda _: asyncio.sleep(0))
    parts = lane_parts(tasks, [], verifying=False)
    assert parts.desk is not None
    try:
        with pytest.raises(Refused) as refused:
            await WorkTurns(parts).configure_checks()
    finally:
        await parts.desk.stop()
    assert (refused.value.code, str(refused.value)) == (NOT_CONFIGURED, NO_VERIFY_MESSAGE)


async def test_without_agents_runs_and_configuring_are_refused() -> None:
    tasks = Tasks(TaskBoard(), lambda _: asyncio.sleep(0))
    turns = WorkTurns(WorkParts(None, None, tasks, None, None, lambda _: None, lambda: False))
    for attempt in (turns.run_tasks, turns.configure_checks):
        with pytest.raises(Refused) as refused:
            await attempt()
        assert (refused.value.code, str(refused.value)) == (NOT_CONFIGURED, NO_AGENTS_MESSAGE)


async def test_a_wake_once_closed_starts_no_work(lane: Lane) -> None:
    await lane.turns.close()
    await add(lane)
    await lane.turns.wake()
    assert not lane.turns.running
    assert BusyChanged(busy=True, lane=WORK_LANE) not in lane.posted


async def test_a_run_for_the_user_once_closed_starts_no_work(lane: Lane) -> None:
    await lane.turns.close()
    await add(lane)
    await lane.turns.run_tasks()
    assert not lane.turns.running
    assert BusyChanged(busy=True, lane=WORK_LANE) not in lane.posted
