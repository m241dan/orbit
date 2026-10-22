"""A lane rechecks the halt before each task or review it starts: a halt that lands as one ends,
while its divert is still held open, starts no other."""

import asyncio
from collections.abc import Sequence

from orbit.agent import Agents
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lanes import SessionGate
from orbit.agent.application.review_lane import ReviewHooks, ReviewLane
from orbit.agent.application.storage_halt import HaltHooks, StorageHalt
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.work_lane import Beside, WorkLane
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.reviews import ReviewLimits, ReviewPlanner
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.protocol import (
    REVIEWER,
    Event,
    EventEnvelope,
    TasksChanged,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import ROLES, Call, FakeHost, FakeTree, Script, ScriptedLauncher

WAITING = ("pending", "running")
"""The statuses of a task that hasn't ended."""


async def gives_up(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("give_up", reason="blocked")
    return "completed"


async def reviews(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("complete_task", summary="Reviewed")
    return "completed"


class Halting:
    """A session whose store fails as task ``first`` ends: its halt's divert is held open, so
    the work isn't stopped yet, and what the session publishes is kept."""

    def __init__(self) -> None:
        self.first = ""
        self.events: list[Event] = []
        self.diverting = asyncio.Event()
        self.halt = StorageHalt(self.quiet, HaltHooks(divert=self.held_divert))
        self.gate = SessionGate(lambda: self.halt.halted)
        self.tasks = Tasks(TaskBoard(), self.publish)

    async def quiet(self, event: Event) -> None:
        """Publishes nothing."""

    async def held_divert(self) -> tuple[Sequence[EventEnvelope], str | None]:
        self.diverting.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def publish(self, event: Event) -> None:
        self.events.append(event)
        if isinstance(event, TasksChanged) and self._first_ended(event):
            self.halt.fail(FailedWrite.EVENT_LOG, RuntimeError("disk full"))

    def _first_ended(self, event: TasksChanged) -> bool:
        return any(t.id == self.first and t.status not in WAITING for t in event.tasks)

    def desk(self, launcher: ScriptedLauncher, tree: FakeTree) -> AgentDesk:
        """A desk launching through ``launcher`` in ``tree``, publishing into the session."""
        agents = Agents(launcher.host, launcher, lambda: "token-1", ROLES, tree=tree)
        return AgentDesk(agents, self.publish)

    def add(self, role: str | None = None) -> str:
        """Add a task, as ``role`` if given; returns its id."""
        spec = TaskSpec(prompt="Do it.") if role is None else TaskSpec(prompt="Do it.", role=role)
        return self.tasks.board.add(spec)


def launcher_of(*scripts: Script, reviewers: Sequence[Script] = ()) -> ScriptedLauncher:
    return ScriptedLauncher(FakeHost(), *scripts, reviewers=reviewers)


async def test_a_halt_as_a_task_ends_starts_no_other_task() -> None:
    session = Halting()
    session.first = session.add()
    second = session.add()
    launcher, tree = launcher_of(gives_up, gives_up), FakeTree()
    lane = WorkLane(session.desk(launcher, tree), session.tasks, None, Beside(gate=session.gate))

    assert await lane.run() == "cancelled"

    await asyncio.wait_for(session.diverting.wait(), 5)  # The work is left to stop.
    assert tree.prepared == [False]
    assert len(launcher.launches) == 1
    assert session.tasks.board.get(second).status == "pending"
    assert lane.next_work() == second
    await session.halt.close()


async def test_a_halt_as_a_review_ends_starts_no_other_review() -> None:
    session = Halting()
    session.first = session.add(REVIEWER)
    second = session.add(REVIEWER)
    launcher, tree = launcher_of(reviewers=[reviews, reviews]), FakeTree()

    async def nothing() -> None:
        pass

    hooks = ReviewHooks(nothing, nothing, session.gate)
    desk = session.desk(launcher, tree)
    lane = ReviewLane(desk, session.tasks, ReviewPlanner(ReviewLimits()), hooks)
    stopped = asyncio.Event()
    lane.on_stopped(stopped.set)
    lane.kick()
    await asyncio.wait_for(stopped.wait(), 5)

    await asyncio.wait_for(session.diverting.wait(), 5)
    assert len(launcher.launches) == 1
    assert session.tasks.board.get(second).status == "pending"
    await session.halt.close()
