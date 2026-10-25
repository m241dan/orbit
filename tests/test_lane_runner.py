"""A lane that starts by itself: once kicked, it runs its roles' tasks one at a time, each in a
snapshot of the base commit, shows busy while it does, starts none while blocked and skips those
on a paused runtime, and stops its task on the user's asking; and the session's lanes, by
name."""

import asyncio
from collections.abc import Collection

import pytest

from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lane_runner import LaneParts, LaneRunner, Lanes, LaneWork
from orbit.agent.application.lanes import SessionGate
from orbit.agent.application.ports import TreeFailed
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.endings import CANCELLED, STOPPED_BY_YOU
from orbit.agent.domain.roles import Role
from orbit.agent.domain.runtimes import CLAUDE
from orbit.protocol import (
    GIT_FAILED,
    BusyChanged,
    ErrorOccurred,
    Event,
    TaskSpec,
    TaskStatus,
    TurnEndReason,
)
from tests.agent_fakes import (
    Call,
    FakeHost,
    FakeTree,
    Script,
    ScriptedLauncher,
    claims_done,
    desk_for,
)
from tests.lane_fakes import WITH_SECURITY, audit
from tests.waiting import eventually

BUSY = BusyChanged(busy=True, lane="security")
IDLE = BusyChanged(busy=False, lane="security")

FAKE = "fake"
"""A runtime other than Claude."""

PENTEST = Role(instructions="Probe it.", verify=False, lane="security", runtime=FAKE)
"""A role in the lane ``security``, on the fake runtime."""


class Lane:
    """A board, what's published and what's posted, for runners of the lane ``security``,
    blocked as :attr:`read_only` and :attr:`halted` say, skipping the tasks on the runtime
    :attr:`paused` names, if any."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.posted: list[Event] = []
        self.tasks = Tasks(TaskBoard(), self.publish)
        self.read_only = self.halted = False
        self.paused: str | None = None
        self.stopped = asyncio.Event()

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    def parts(self, desk: AgentDesk) -> LaneParts:
        """What runners of the lane run with: ``desk``'s agents and :attr:`tasks`."""
        gate = SessionGate(lambda: self.halted, lambda: self.read_only)
        return LaneParts(desk, self.tasks, gate, lambda: self.paused, self.posted.append)

    def runner(
        self, *scripts: Script, tree: FakeTree | None = None, work: LaneWork | None = None
    ) -> LaneRunner:
        """A runner of the lane whose agents play ``scripts``, in ``tree`` if given, running
        ``work`` if given; :attr:`stopped` is set whenever it stops."""
        desk = desk_for(self.publish, *scripts, tree=tree, roles=WITH_SECURITY)
        runner = LaneRunner("security", {"security"}, self.parts(desk), work)
        runner.on_stopped(self.stopped.set)
        return runner

    def add(self, *tasks: TaskSpec) -> None:
        for task in tasks:
            self.tasks.board.add(task)

    async def ran(self, runner: LaneRunner) -> None:
        """Kick ``runner`` and wait until it stops."""
        self.stopped.clear()
        runner.kick()
        await asyncio.wait_for(self.stopped.wait(), 5)

    @property
    def statuses(self) -> list[TaskStatus]:
        return [state.status for state in self.tasks.board.tasks]


def endless(started: asyncio.Event) -> Script:
    """A run that sets ``started`` once it has its task, then never ends by itself."""

    async def play(call: Call) -> TurnEndReason:
        await call("get_task")
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    return play


async def test_a_kicked_runner_runs_its_roles_tasks_in_snapshots_showing_busy() -> None:
    lane, tree = Lane(), FakeTree()
    lane.add(audit("a"), TaskSpec(prompt="b"), audit("c"))
    runner = lane.runner(claims_done, claims_done, tree=tree)
    await lane.ran(runner)
    assert lane.statuses == ["completed", "pending", "completed"]
    assert lane.posted == [BUSY, IDLE]
    assert (tree.snapshots, tree.dropped) == (["base", "base"], ["/work/review-base"] * 2)
    assert not runner.busy and runner.current is None


async def test_a_kicked_runner_with_nothing_to_run_shows_no_busy() -> None:
    lane = Lane()
    lane.add(TaskSpec(prompt="b"))
    runner = lane.runner()
    runner.kick()
    assert runner.running
    await asyncio.wait_for(lane.stopped.wait(), 5)
    assert lane.posted == []


@pytest.mark.parametrize("blocked", ["read_only", "halted"])
async def test_a_blocked_runner_starts_nothing(blocked: str) -> None:
    lane = Lane()
    setattr(lane, blocked, True)
    lane.add(audit())
    runner = lane.runner()
    runner.kick()
    assert not runner.running


async def test_a_runner_skips_its_tasks_on_the_paused_runtime_showing_no_busy() -> None:
    lane = Lane()
    lane.paused = CLAUDE
    lane.add(audit())
    runner = lane.runner()
    await lane.ran(runner)
    assert lane.statuses == ["pending"]
    assert lane.posted == []
    assert runner.current is None


async def test_a_runner_runs_its_tasks_on_another_runtime_than_the_paused_one() -> None:
    lane, host = Lane(), FakeHost()
    fake = ScriptedLauncher(host, claims_done)
    roles = {**WITH_SECURITY, "pentest": PENTEST}
    desk = desk_for(lane.publish, host=host, roles=roles, launchers={FAKE: fake})
    runner = LaneRunner("security", {"security", "pentest"}, lane.parts(desk))
    runner.on_stopped(lane.stopped.set)
    lane.paused = CLAUDE
    lane.add(audit(), TaskSpec(prompt="Probe it.", role="pentest"))
    await lane.ran(runner)
    assert lane.statuses == ["pending", "completed"]
    assert [launch.agent.role for launch in fake.launches] == ["pentest"]


async def test_a_pause_as_a_task_ends_starts_no_other_until_kicked_again() -> None:
    lane = Lane()

    async def pauses(call: Call) -> TurnEndReason:
        lane.paused = CLAUDE
        return await claims_done(call)

    lane.add(audit("a"), audit("b"))
    runner = lane.runner(pauses, claims_done)
    await lane.ran(runner)
    assert lane.statuses == ["completed", "pending"]

    lane.paused = None
    await lane.ran(runner)
    assert lane.statuses == ["completed", "completed"]
    assert lane.posted == [BUSY, IDLE, BUSY, IDLE]


async def test_cancelling_stops_only_the_task_running_and_the_lane_carries_on() -> None:
    lane, started = Lane(), asyncio.Event()
    lane.add(audit("a"), audit("b"))
    runner = lane.runner(endless(started), claims_done)
    runner.kick()
    await asyncio.wait_for(started.wait(), 5)
    assert runner.current == "1"

    runner.cancel("2")
    runner.cancel()
    await asyncio.wait_for(lane.stopped.wait(), 5)
    first = lane.tasks.board.get("1")
    assert (first.status, first.reason) == ("cancelled", STOPPED_BY_YOU)
    assert lane.statuses == ["cancelled", "completed"]


async def test_cancelling_with_nothing_running_does_nothing() -> None:
    lane = Lane()
    lane.runner().cancel()
    assert lane.events == []


async def test_a_task_whose_snapshot_cant_be_made_ends_in_error() -> None:
    lane, tree = Lane(), FakeTree()
    tree.failures["base_commit"] = TreeFailed("git rev-parse failed")
    lane.add(audit())
    await lane.ran(lane.runner(tree=tree))
    assert lane.statuses == ["error"]
    assert ErrorOccurred(code=GIT_FAILED, message="git rev-parse failed") in lane.events


async def test_a_stopped_runner_runs_again_once_kicked_but_a_closed_one_never() -> None:
    lane, started = Lane(), asyncio.Event()
    lane.add(audit())
    runner = lane.runner(endless(started), claims_done)
    runner.kick()
    await asyncio.wait_for(started.wait(), 5)
    await runner.stop()
    stopped = lane.tasks.board.get("1")
    assert (stopped.status, stopped.reason) == ("cancelled", CANCELLED)

    await lane.ran(runner)
    assert lane.statuses == ["completed"]
    lane.add(audit())
    await runner.close()
    runner.kick()
    assert not runner.running and lane.statuses == ["completed", "pending"]


async def test_a_run_started_as_the_last_one_ends_goes_on_showing_busy() -> None:
    lane = Lane()
    queue = ["x"]
    ran: list[str] = []
    runner: LaneRunner

    def next_task(tried: Collection[str]) -> str | None:
        return next((task for task in queue if task not in (*tried, *ran)), None)

    def queue_and_kick() -> None:
        queue.append("y")
        runner.kick()

    async def run(task_id: str) -> None:
        ran.append(task_id)
        if task_id == "x":  # Kicked again before this run's end is told.
            asyncio.get_running_loop().call_soon(queue_and_kick)

    stops: list[None] = []
    runner = lane.runner(work=LaneWork(next_task, run))
    runner.on_stopped(lambda: stops.append(None))
    runner.kick()
    await eventually(lambda: len(stops) == 2)
    assert ran == ["x", "y"]
    assert lane.posted == [BUSY, IDLE]


def lanes_of(lane: Lane) -> tuple[Lanes, LaneRunner]:
    """The lanes ``build``, the planner's, ``security`` and one for reviews, with the runner of
    ``security``."""
    security = lane.runner(claims_done)
    desk = desk_for(lane.publish, roles=WITH_SECURITY)
    review = LaneRunner("review", {"reviewer"}, lane.parts(desk))
    return Lanes("build", (security, review)), security


async def test_the_lanes_are_named_and_hold_their_runners_by_lane_and_role() -> None:
    lanes, security = lanes_of(Lane())
    assert lanes.names == ("build", "planner", "security", "review")
    assert lanes.elsewhere == frozenset({"security", "reviewer"})
    assert (lanes.runner("security"), lanes.runner("build")) == (security, None)
    assert (lanes.of_role("security"), lanes.of_role("coder")) == (security, None)
    assert lanes.busy() == {"security": False, "review": False}


def test_without_runners_the_lanes_are_the_work_lane_and_the_planners() -> None:
    lanes = Lanes()
    assert (lanes.names, lanes.elsewhere, lanes.busy()) == (("work", "planner"), frozenset(), {})


async def test_kicking_a_roles_lane_runs_only_a_lane_that_starts_by_itself() -> None:
    lane = Lane()
    lanes, security = lanes_of(lane)
    lane.add(TaskSpec(prompt="b"), audit())
    lanes.kick_for("coder")
    assert not security.running
    lanes.kick_for("security")
    await asyncio.wait_for(lane.stopped.wait(), 5)
    assert lane.statuses == ["pending", "completed"]


async def test_kicking_the_lanes_kicks_every_runner() -> None:
    lane = Lane()
    lanes, security = lanes_of(lane)
    lane.add(audit())
    lanes.kick()
    assert security.running
    await asyncio.wait_for(lane.stopped.wait(), 5)
    assert lane.statuses == ["completed"]
