"""When the work lane runs: the user's runs, a review's wake, a hold, and the configurator."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import pytest

from orbit.agent.application.lanes import OPEN_GATE, SessionGate
from orbit.agent.application.limit_pause import LimitPause
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.verifier import Verifier
from orbit.agent.application.work_lane import Beside, RunEnd, WorkLane
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
from orbit.agent.domain.usage_limits import RESUMING_NOTE, Paused, paused_note
from orbit.protocol import (
    BUSY,
    CONFIGURATOR,
    INTERNAL,
    NOT_CONFIGURED,
    PAUSED_FOR_USAGE_LIMIT,
    STORAGE_FAILED,
    WORK_LANE,
    BusyChanged,
    ErrorOccurred,
    Event,
    Notice,
    Settings,
    TaskSpec,
)
from tests.agent_fakes import START, Steps, desk_for, hangs, settled, verified

TASK = TaskSpec(prompt="Add a flag.", settings=Settings(model="opus"))


class StopsCounted(WorkLane):
    """A work lane that counts the times it was told the user stopped its run, and whose runs
    end at once as ``ends`` says, if set."""

    stops = 0
    ends: RunEnd | None = None

    def stopped_by_user(self) -> bool:
        self.stops += 1
        return super().stopped_by_user()

    async def run(self) -> RunEnd:
        if self.ends is not None:
            return self.ends
        return await super().run()


class Sleeps:
    """A pause whose clock stands at ``now``, :data:`START` until a test moves it, and whose
    sleeps note the time they last ``until``, then last until released and raise ``error``, if
    set."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.error: Exception | None = None
        self.now = START
        self.until: list[datetime] = []

    async def sleep(self, seconds: float) -> None:
        self.until.append(self.now + timedelta(seconds=seconds))
        await self.release.wait()
        if self.error is not None:
            raise self.error

    @property
    def pause(self) -> LimitPause:
        return LimitPause(now=lambda: self.now, sleep=self.sleep)


HALTED_MESSAGE = "The session is halted."


@dataclass
class Halt:
    """Whether the session is halted, as a test sets it."""

    on: bool = False

    def check(self) -> None:
        """Refuse, as the halt does, while on."""
        if self.on:
            raise Refused(STORAGE_FAILED, HALTED_MESSAGE)


@dataclass
class Lane:
    """Work turns over a ``work`` lane and a board, with the events they posted, and what their
    pause was each time they told it changed."""

    turns: WorkTurns
    work: StopsCounted
    tasks: Tasks
    posted: list[Event]
    halt: Halt
    sleeps: Sleeps
    pauses: list[Paused | None] = field(default_factory=list)

    def roles(self) -> list[str]:
        return [state.task.role for state in self.tasks.board.tasks]


def lane_parts(
    tasks: Tasks,
    posted: list[Event],
    verifying: bool,
    halt: Halt | None = None,
    pause: LimitPause | None = None,
) -> WorkParts:
    """The parts of work turns whose agents all hang, verified if ``verifying``, in a session
    halted as ``halt`` says, timing a run the usage limit stopped with ``pause``."""

    async def publish(event: Event) -> None:
        posted.append(event)

    desk = desk_for(publish, *[hangs] * 5, pause=pause or LimitPause())
    verifier = Verifier(verified(Steps()), tasks, publish) if verifying else None
    work = StopsCounted(desk, tasks, verifier, Beside())
    halted = halt or Halt()
    return WorkParts(
        work,
        None,
        tasks,
        verifier,
        desk,
        posted.append,
        SessionGate(lambda: halted.on, check=halted.check),
    )


@pytest.fixture
async def lane() -> AsyncIterator[Lane]:
    posted: list[Event] = []
    halt, sleeps = Halt(), Sleeps()
    tasks = Tasks(TaskBoard(), lambda _: asyncio.sleep(0))
    pauses: list[Paused | None] = []
    parts = lane_parts(tasks, posted, True, halt, sleeps.pause)
    parts = replace(parts, paused_changed=lambda: pauses.append(turns.paused))
    turns = WorkTurns(parts)
    assert isinstance(parts.work, StopsCounted)
    yield Lane(turns, parts.work, tasks, posted, halt, sleeps, pauses)
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


async def test_a_run_for_the_user_while_halted_is_refused_as_the_halt_refuses(
    lane: Lane,
) -> None:
    await add(lane)
    lane.halt.on = True
    with pytest.raises(Refused) as refused:
        await lane.turns.run_tasks()
    assert (refused.value.code, str(refused.value)) == (STORAGE_FAILED, HALTED_MESSAGE)
    assert not lane.turns.running


async def test_configuring_whose_save_halts_the_session_is_refused_and_starts_no_run() -> None:
    halt = Halt()

    async def halts() -> None:
        halt.on = True

    tasks = Tasks(TaskBoard(), lambda _: asyncio.sleep(0), halts)
    parts = lane_parts(tasks, [], True, halt)
    turns = WorkTurns(parts)
    assert parts.desk is not None
    try:
        with pytest.raises(Refused) as refused:
            await turns.configure_checks()
    finally:
        await parts.desk.stop()
    assert (refused.value.code, str(refused.value)) == (STORAGE_FAILED, HALTED_MESSAGE)
    assert not turns.running


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
    turns = WorkTurns(WorkParts(None, None, tasks, None, None, lambda _: None, OPEN_GATE))
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


async def limited(lane: Lane) -> None:
    """Run a task the usage limit stops, leaving the run waiting to start again."""
    await add(lane)
    lane.work.ends = "limited"
    await lane.turns.run_tasks()
    await turn_ended(lane)
    lane.work.ends = None
    await settled()


def notes(lane: Lane) -> list[str]:
    return [event.message for event in lane.posted if isinstance(event, Notice)]


async def test_a_wait_ending_starts_the_run_again_and_says_so(lane: Lane) -> None:
    await limited(lane)
    lane.sleeps.release.set()
    await settled()
    assert lane.turns.running
    assert notes(lane)[-1] == RESUMING_NOTE


async def test_a_wake_while_waiting_to_start_again_starts_nothing(lane: Lane) -> None:
    await limited(lane)
    await lane.turns.wake()
    assert not lane.turns.running


async def test_a_wait_ending_while_halted_starts_nothing_and_says_nothing(lane: Lane) -> None:
    await limited(lane)
    lane.halt.on = True
    lane.sleeps.release.set()
    await settled()
    assert not lane.turns.running
    assert RESUMING_NOTE not in notes(lane)


async def test_a_wait_ending_with_no_work_left_says_nothing(lane: Lane) -> None:
    await limited(lane)
    [task] = lane.tasks.board.tasks
    await lane.tasks.change(lambda board: board.remove(task.id))
    lane.sleeps.release.set()
    await settled()
    assert not lane.turns.running
    assert RESUMING_NOTE not in notes(lane)


async def test_a_wake_once_a_wait_failed_starts_the_run(lane: Lane) -> None:
    await limited(lane)
    lane.sleeps.error = RuntimeError("clock broke")
    lane.sleeps.release.set()
    await settled()
    await lane.turns.wake()
    assert lane.turns.running


async def test_an_error_in_the_wait_is_reported(lane: Lane) -> None:
    await limited(lane)
    lane.sleeps.error = RuntimeError("clock broke")
    lane.sleeps.release.set()
    await settled()
    [error] = [event for event in lane.posted if isinstance(event, ErrorOccurred)]
    assert error.code == INTERNAL
    assert f"The {WORK_LANE} lane" in error.message
    assert "clock broke" in error.message
    assert not lane.turns.running


async def test_running_once_a_wait_failed_starts_the_run(lane: Lane) -> None:
    await limited(lane)
    lane.sleeps.error = RuntimeError("clock broke")
    lane.sleeps.release.set()
    await settled()
    await lane.turns.run_tasks()
    assert lane.turns.running


async def test_closing_while_waiting_leaves_no_task_running(lane: Lane) -> None:
    await limited(lane)
    await lane.turns.close()
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_configuring_while_waiting_to_start_again_cancels_the_wait(lane: Lane) -> None:
    await limited(lane)
    lane.work.ends = "finished"
    await lane.turns.configure_checks()
    await turn_ended(lane)
    lane.sleeps.release.set()
    await settled()
    assert not lane.turns.running
    assert RESUMING_NOTE not in notes(lane)


async def test_a_configurator_run_limited_again_starts_again_at_the_new_reset(
    lane: Lane,
) -> None:
    await limited(lane)
    lane.sleeps.now = START + timedelta(minutes=5)
    lane.work.ends = "limited"
    await lane.turns.configure_checks()
    await turn_ended(lane)
    await settled()
    reset = START + timedelta(minutes=20)
    assert notes(lane)[-1] == paused_note(reset)
    assert lane.sleeps.until == [START + timedelta(minutes=15), reset]


LIMIT_PAUSE = Paused(START + timedelta(minutes=15), PAUSED_FOR_USAGE_LIMIT)
"""The pause of a run the usage limit stopped at :data:`START`, with no reset known."""


async def test_a_run_the_usage_limit_stopped_is_paused_for_it_until_it_starts_again(
    lane: Lane,
) -> None:
    await limited(lane)
    paused = lane.turns.paused
    lane.sleeps.release.set()
    await settled()
    assert (paused, lane.turns.paused) == (LIMIT_PAUSE, None)
    assert lane.pauses == [LIMIT_PAUSE, None]


async def test_a_hold_during_the_wait_ends_the_pause(lane: Lane) -> None:
    await limited(lane)
    lane.turns.hold()
    assert lane.turns.paused is None
    assert lane.pauses == [LIMIT_PAUSE, None]


async def test_a_wait_failing_ends_the_pause(lane: Lane) -> None:
    await limited(lane)
    lane.sleeps.error = RuntimeError("clock broke")
    lane.sleeps.release.set()
    await settled()
    assert lane.turns.paused is None
    assert lane.pauses == [LIMIT_PAUSE, None]


async def test_a_wait_ending_while_halted_stays_paused_until_the_run_starts_again(
    lane: Lane,
) -> None:
    await limited(lane)
    lane.halt.on = True
    lane.sleeps.release.set()
    await settled()
    halted = lane.turns.paused
    lane.halt.on = False
    await lane.turns.lifted()
    assert (halted, lane.turns.paused) == (LIMIT_PAUSE, None)
    assert lane.turns.running


async def test_forgetting_no_wait_tells_of_no_change(lane: Lane) -> None:
    lane.turns.forget_wait()
    assert lane.pauses == []
