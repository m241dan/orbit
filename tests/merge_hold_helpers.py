"""What the tests of merge holds in a session share: sets that hold merges, a step runner
scripting their runs, a tree whose base follows what merges, agents that wait, and the events a
session publishes, gathered as it runs."""

import asyncio
from collections.abc import Callable

from orbit.agent import VerificationStep, WorkspaceHost
from orbit.agent.application.merge_holds import MergeHolds
from orbit.agent.application.ports import Merge
from orbit.agent.application.set_runs import SetRuns
from orbit.agent.domain.verification import CheckSet
from orbit.protocol import REVIEWER, Event, Notice, TaskMerged, TaskState, TurnEndReason
from tests.agent_fakes import (
    TRIGGERS,
    Call,
    FakeTree,
    Script,
    board_of,
    holds_of,
    payloads,
    sets_of,
)

STRESS = CheckSet(
    "stress", (VerificationStep("Stress tests", ("stress",), "stress"),), "holds_merges"
)
SOAK = CheckSet("soak", (VerificationStep("Soak", ("soak",), "soak"),), "holds_merges")
FAILED = "2 stress tests failed"
HELD = "Merges held: stress failed on base. A fix is queued first."
NO_REVIEW = {**TRIGGERS, "review": TRIGGERS["review"].model_copy(update={"enabled": False})}


class SetSteps:
    """A step runner coming out as scripted for each named set's steps, by set, once that set's
    gate is open, and for the task checks as :attr:`task_results` says, passing once they run
    out."""

    def __init__(self, **results: list[bool]) -> None:
        self.results = results
        self.task_results: list[bool] = []
        self.gates = {name: asyncio.Event() for name in results}
        for gate in self.gates.values():
            gate.set()
        self.runs: list[str] = []

    async def __call__(self, step: VerificationStep, root: str) -> tuple[bool, str]:
        if step.set is None:
            passed = self.task_results.pop(0) if self.task_results else True
            return passed, "ok" if passed else "lint failed"
        self.runs.append(step.set)
        await self.gates[step.set].wait()
        passed = self.results[step.set].pop(0)
        return passed, "all good" if passed else FAILED


class LandingTree(FakeTree):
    """A coder tree whose base moves to its head as its work merges."""

    async def integrate(self) -> Merge:
        merge = await super().integrate()
        if merge.result == "merged":
            self.base = await self.head()
        return merge


def waits_for(gate: asyncio.Event, summary: str = "done") -> Script:
    """An agent that fetches its task, then claims it done with ``summary`` once ``gate`` is
    set."""

    async def script(call: Call) -> TurnEndReason:
        await call("get_task")
        await gate.wait()
        await call("complete_task", summary=summary)
        return "completed"

    return script


class Seen:
    """The events ``session`` publishes, gathered in the background until closed."""

    def __init__(self, session: WorkspaceHost) -> None:
        self.events: list[Event] = []
        self._pump = asyncio.ensure_future(self._gather(session))

    async def _gather(self, session: WorkspaceHost) -> None:
        async for event in payloads(session):
            self.events.append(event)

    def notices(self) -> list[str]:
        return [event.message for event in self.events if isinstance(event, Notice)]

    def merged(self) -> list[tuple[str, str]]:
        """The tasks merged, each with its commit, in order."""
        merges = (event for event in self.events if isinstance(event, TaskMerged))
        return [(merge.task_id, merge.commit) for merge in merges if merge.result == "merged"]

    def close(self) -> None:
        self._pump.cancel()


def parts_of(session: WorkspaceHost) -> tuple[SetRuns, MergeHolds]:
    sets, holds = sets_of(session), holds_of(session)
    assert sets is not None and holds is not None
    return sets, holds


def reviews(session: WorkspaceHost) -> list[TaskState]:
    return [state for state in board_of(session).tasks if state.task.role == REVIEWER]


def status(session: WorkspaceHost, task_id: str) -> Callable[[], str]:
    return lambda: board_of(session).get(task_id).status
