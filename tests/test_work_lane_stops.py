"""The work lane's agent runs: the user's stop cancels the agent's run, even before it begins,
ending its task as stopped by them while the lane runs again once asked, and an agent's run that
ends by itself or at its usage limit forgets its runtime's outage, while one whose API didn't
answer or that the user stopped leaves it."""

import asyncio

import pytest

from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.work_lane import Beside, WorkLane
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.endings import STOPPED_BY_YOU
from orbit.agent.domain.runtimes import CLAUDE, Failure
from orbit.agent.domain.usage_limits import Outages, RuntimeStop
from orbit.protocol import AgentStarted, Event, TaskSpec, TaskStatus, TurnEndReason
from tests.agent_fakes import Call, FakeTree, Script, agents_for, claims_done
from tests.lane_fakes import Gate, running

OVERLOADED = 'API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}'

LIMIT = "You've hit your session limit · resets 6pm (UTC)"


async def quiet(event: Event) -> None:
    """Publishes nothing."""


async def errs(call: Call) -> TurnEndReason:
    """Ends its turn in error, before it fetches its task, as the CLI does when it fails."""
    return "error"


class Works:
    """A run that notes it has its task in :attr:`working`, then never ends by itself."""

    def __init__(self) -> None:
        self.working = asyncio.Event()

    async def __call__(self, call: Call) -> TurnEndReason:
        await call("get_task")
        self.working.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class Lane:
    """A work lane on a board of :attr:`tasks`, publishing to :attr:`events` through
    :attr:`gate`, if given, noting each runtime's outage in :attr:`outages`, with Claude's API
    not answering at first; its agents play the scripts given, each launch's turn ending with
    the error ``errors`` gives it, if any, in a tree that merges."""

    def __init__(
        self, *scripts: Script, errors: tuple[str, ...] = (), gate: Gate | None = None
    ) -> None:
        self.events: list[Event] = []
        self.gate = gate
        self.outages = Outages()
        self.outages.stopped(RuntimeStop(CLAUDE, Failure("api_unavailable")), OVERLOADED)
        agents, launcher = agents_for(*scripts, tree=FakeTree())
        launcher.turn_errors.extend(errors)
        self.tasks = Tasks(TaskBoard(), self.publish)
        self.lane = WorkLane(
            AgentDesk(agents, self.publish), self.tasks, None, Beside(outages=self.outages)
        )

    async def publish(self, event: Event) -> None:
        self.events.append(event)
        if self.gate is not None:
            await self.gate(event)

    def add(self, *prompts: str) -> None:
        for prompt in prompts:
            self.tasks.board.add(TaskSpec(prompt=prompt))

    @property
    def statuses(self) -> list[TaskStatus]:
        return [state.status for state in self.tasks.board.tasks]

    @property
    def agents_started(self) -> list[str]:
        """The tasks whose agents the lane started, in order."""
        return [e.task_id for e in self.events if isinstance(e, AgentStarted)]

    @property
    def outage_cleared(self) -> bool:
        """Whether Claude's outage is forgotten."""
        return self.outages.of(CLAUDE) is None


async def test_a_stop_as_a_task_starts_cancels_its_agents_run_before_it_begins() -> None:
    gate = Gate(running)
    lane = Lane(gate=gate)
    lane.add("a")
    run = asyncio.create_task(lane.lane.run())
    await asyncio.wait_for(gate.reached.wait(), 5)

    assert lane.lane.stopped_by_user()
    gate.released.set()

    assert await asyncio.wait_for(run, 5) == "cancelled"
    first = lane.tasks.board.get("1")
    assert (first.status, first.reason) == ("cancelled", STOPPED_BY_YOU)
    assert lane.agents_started == []


async def test_a_stop_mid_run_ends_its_task_as_stopped_and_the_lane_runs_again() -> None:
    works = Works()
    lane = Lane(works, claims_done)
    lane.add("a")
    run = asyncio.create_task(lane.lane.run())
    await asyncio.wait_for(works.working.wait(), 5)

    assert lane.lane.stopped_by_user()
    assert await asyncio.wait_for(run, 5) == "cancelled"
    first = lane.tasks.board.get("1")
    assert (first.status, first.reason) == ("cancelled", STOPPED_BY_YOU)

    assert await asyncio.wait_for(lane.lane.run(), 5) == "finished"
    assert lane.statuses == ["completed"]
    assert lane.agents_started == ["1", "1"]


@pytest.mark.parametrize(
    ("script", "errors", "cleared"),
    [(claims_done, (), True), (errs, (LIMIT,), True), (errs, (OVERLOADED,), False)],
)
async def test_a_run_ending_by_itself_or_at_its_limit_forgets_its_runtimes_outage(
    script: Script, errors: tuple[str, ...], cleared: bool
) -> None:
    lane = Lane(script, errors=errors)
    lane.add("a")
    await asyncio.wait_for(lane.lane.run(), 5)
    assert lane.outage_cleared == cleared


async def test_a_run_the_user_stops_leaves_its_runtimes_outage() -> None:
    works = Works()
    lane = Lane(works)
    lane.add("a")
    run = asyncio.create_task(lane.lane.run())
    await asyncio.wait_for(works.working.wait(), 5)

    assert lane.lane.stopped_by_user()
    assert await asyncio.wait_for(run, 5) == "cancelled"
    assert not lane.outage_cleared
