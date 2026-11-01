"""The review lane's slots: up to ``review_slots`` of its tasks run at once, picked in board order
as a slot frees up; cancelling one leaves the others running, stopping the lane stops them all, a
runtime's pause leaves the task it stopped pending and starts no other on that runtime, a
read-only orbit starts none, and one slot runs them strictly one after another."""

import asyncio
from collections.abc import Callable

from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lane_runner import Unpausable
from orbit.agent.application.lanes import SessionGate
from orbit.agent.application.review_lane import ReviewHooks, ReviewLane
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.endings import CANCELLED, REVIEW_STOPPED_BY_YOU
from orbit.agent.domain.reviews import ReviewLimits, ReviewPlanner
from orbit.agent.domain.runtimes import CLAUDE
from orbit.agent.domain.usage_limits import RuntimeStop
from orbit.protocol import BUGS_REVIEWER, Event, TaskSpec, TaskStatus, TurnEndReason
from tests.agent_fakes import Call, FakeTree, Script, agents_for, claims_done
from tests.waiting import eventually

LIMIT = "You've hit your session limit · resets 6pm (UTC)"


class Pauses(Unpausable):
    """Pauses the runtime of a task that hit its limit, as :attr:`paused` names."""

    def __init__(self) -> None:
        self.paused: str | None = None

    def pause_for(self, stop: RuntimeStop, error: str) -> bool:
        self.paused = stop.runtime
        return True


class Held:
    """Agents that each, once they have their task, wait to be let go, then claim it done: the
    agent launched ``n``-th waits for ``gates[n]``; :attr:`started` counts those waiting or
    let go."""

    def __init__(self, count: int) -> None:
        self.gates = tuple(asyncio.Event() for _ in range(count))
        self.started = 0

    def scripts(self) -> tuple[Script, ...]:
        return tuple(self._held(gate) for gate in self.gates)

    def _held(self, gate: asyncio.Event) -> Script:
        async def play(call: Call) -> TurnEndReason:
            await call("get_task")
            self.started += 1
            await gate.wait()
            await call("complete_task", summary="Reviewed")
            return "completed"

        return play


async def errs(call: Call) -> TurnEndReason:
    """An agent whose turn ends in error at once."""
    return "error"


async def counted(count: Callable[[], int], expected: int) -> None:
    """Return once ``count`` reaches ``expected``, for a while at most."""
    await eventually(lambda: count() == expected)


class Slots:
    """A review lane with ``slots`` slots on a board of ``count`` lens reviews, whose agents
    play ``scripts``, starting none while :attr:`read_only`, and pausing the runtime of a task
    that hits its limit in :attr:`pauses`; :attr:`stopped` is set whenever it stops."""

    def __init__(self, slots: int, count: int, *scripts: Script) -> None:
        self.read_only = False
        self.pauses = Pauses()
        self.tasks = Tasks(TaskBoard(), self.publish)
        for n in range(count):
            self.tasks.board.add(TaskSpec(prompt=f"Look at {n + 1}.", role=BUGS_REVIEWER))
        agents, self.launcher = agents_for(*scripts, tree=FakeTree())
        gate = SessionGate(read_only=lambda: self.read_only)
        hooks = ReviewHooks(
            self.nothing, self.nothing, gate, paused=lambda: self.pauses.paused, pauses=self.pauses
        )
        planner = ReviewPlanner(ReviewLimits(slots=slots))
        self.lane = ReviewLane(AgentDesk(agents, self.publish), self.tasks, planner, hooks)
        self.stopped = asyncio.Event()
        self.lane.on_stopped(self.stopped.set)

    async def publish(self, event: Event) -> None:
        """Publishes nothing."""

    async def nothing(self) -> None:
        """Wakes nothing."""

    def with_status(self, status: TaskStatus) -> list[str]:
        """The tasks with ``status``, in board order."""
        return [t.id for t in self.tasks.board.tasks if t.status == status]

    async def ran(self) -> None:
        """Kick the lane and wait until it stops."""
        self.stopped.clear()
        self.lane.kick()
        await asyncio.wait_for(self.stopped.wait(), 5)


async def test_three_slots_run_six_lens_reviews_three_at_a_time_in_board_order() -> None:
    held = Held(6)
    slots = Slots(3, 6, *held.scripts())
    slots.lane.kick()
    await eventually(lambda: held.started == 3)
    assert slots.with_status("running") == ["1", "2", "3"]
    assert slots.with_status("pending") == ["4", "5", "6"]
    assert slots.lane.runner.busy

    for ended, gate in enumerate(held.gates, start=1):
        gate.set()
        await counted(lambda: len(slots.with_status("completed")), ended)
        await counted(lambda: held.started, min(6, ended + 3))
        assert len(slots.with_status("running")) == min(3, 6 - ended)
    await asyncio.wait_for(slots.stopped.wait(), 5)
    assert not slots.lane.runner.busy


async def test_reviewing_describes_the_earliest_started_of_the_tasks_running() -> None:
    held = Held(3)
    slots = Slots(3, 3, *held.scripts())
    slots.lane.kick()
    await eventually(lambda: held.started == 3)
    assert (slots.lane.reviewing, slots.lane.task) == ((), None)
    assert slots.lane.runner.current == "1"
    held.gates[0].set()
    await eventually(lambda: slots.with_status("completed") == ["1"])
    assert slots.lane.runner.current == "2"
    for gate in held.gates:
        gate.set()
    await asyncio.wait_for(slots.stopped.wait(), 5)


async def test_cancelling_one_of_three_running_ends_only_it_and_a_waiting_one_takes_its_slot() -> (
    None
):
    held = Held(4)
    slots = Slots(3, 4, *held.scripts())
    slots.lane.kick()
    await eventually(lambda: held.started == 3)
    slots.lane.cancel("2")
    await eventually(lambda: held.started == 4)
    cancelled = slots.tasks.board.get("2")
    assert (cancelled.status, cancelled.reason) == ("cancelled", REVIEW_STOPPED_BY_YOU)
    assert slots.with_status("running") == ["1", "3", "4"]

    for gate in held.gates:
        gate.set()
    await asyncio.wait_for(slots.stopped.wait(), 5)
    assert slots.with_status("completed") == ["1", "3", "4"]


async def test_stopping_the_lane_ends_all_three_running_and_a_kick_starts_them_again() -> None:
    held = Held(3)
    slots = Slots(3, 3, *held.scripts(), claims_done, claims_done, claims_done)
    slots.lane.kick()
    await eventually(lambda: held.started == 3)
    await asyncio.wait_for(slots.lane.stop(), 5)
    stopped = [(t.status, t.reason) for t in slots.tasks.board.tasks]
    assert stopped == [("cancelled", CANCELLED)] * 3
    assert not slots.lane.running

    await slots.ran()
    assert slots.with_status("completed") == ["1", "2", "3"]


async def test_a_limit_in_one_running_leaves_it_pending_and_starts_none_on_its_runtime() -> None:
    held = Held(2)
    slots = Slots(3, 4, errs, *held.scripts(), claims_done, claims_done)
    slots.launcher.turn_errors.append(LIMIT)
    slots.lane.kick()
    await eventually(lambda: slots.pauses.paused == CLAUDE)
    await eventually(lambda: slots.with_status("pending") == ["1", "4"])
    assert slots.with_status("running") == ["2", "3"]

    for gate in held.gates:
        gate.set()
    await asyncio.wait_for(slots.stopped.wait(), 5)
    assert slots.with_status("completed") == ["2", "3"]
    assert slots.with_status("pending") == ["1", "4"]
    assert len(slots.launcher.launches) == 3

    slots.pauses.paused = None
    await slots.ran()
    assert slots.with_status("completed") == ["1", "2", "3", "4"]


async def test_a_read_only_orbit_starts_no_review_task() -> None:
    slots = Slots(3, 2, claims_done, claims_done)
    slots.read_only = True
    slots.lane.kick()
    assert not slots.lane.running
    assert slots.with_status("pending") == ["1", "2"]
    assert slots.launcher.launches == []


async def test_a_kick_while_the_lane_runs_fills_the_slots_free() -> None:
    held = Held(2)
    slots = Slots(3, 1, *held.scripts())
    slots.lane.kick()
    await eventually(lambda: held.started == 1)
    slots.tasks.board.add(TaskSpec(prompt="Look at 2.", role=BUGS_REVIEWER))
    slots.lane.kick()
    await eventually(lambda: held.started == 2)
    assert slots.with_status("running") == ["1", "2"]
    for gate in held.gates:
        gate.set()
    await asyncio.wait_for(slots.stopped.wait(), 5)


async def test_one_slot_runs_the_tasks_strictly_one_after_another() -> None:
    held = Held(3)
    slots = Slots(1, 3, *held.scripts())
    slots.lane.kick()
    for ended, gate in enumerate(held.gates):
        await counted(lambda: held.started, ended + 1)
        assert slots.with_status("running") == [str(ended + 1)]
        assert len(slots.with_status("pending")) == 2 - ended
        gate.set()
    await asyncio.wait_for(slots.stopped.wait(), 5)
    assert slots.with_status("completed") == ["1", "2", "3"]
