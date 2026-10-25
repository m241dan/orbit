"""Fakes for the lanes: a role in a lane of its own, a review lane that hears how tasks ended,
hooks that wake nothing, and publishing that holds an event."""

import asyncio
from collections.abc import Callable

from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lanes import OPEN_GATE
from orbit.agent.application.review_lane import ReviewHooks, ReviewLane
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.reviews import ReviewLimits, ReviewPlanner
from orbit.agent.domain.roles import Role
from orbit.protocol import Event, TaskSpec, TaskState
from tests.agent_fakes import ROLES

SECURITY = Role(instructions="Audit the code.", verify=False, lane="security", workspace="snapshot")
WITH_SECURITY = {**ROLES, "security": SECURITY}
"""The built-in roles, with ``security`` in a lane of its own."""


def audit(prompt: str = "Audit it.") -> TaskSpec:
    """A task for the ``security`` role."""
    return TaskSpec(prompt=prompt, role="security")


async def no_work() -> None:
    """Wakes no work lane."""


class Hearing(ReviewLane):
    """A review lane on :attr:`tasks` that notes how it hears each task ended, as which state,
    and reviews none."""

    def __init__(self, desk: AgentDesk, tasks: Tasks) -> None:
        hooks = ReviewHooks(no_work, no_work, OPEN_GATE)
        super().__init__(desk, tasks, ReviewPlanner(ReviewLimits()), hooks)
        self.tasks = tasks
        self.heard: list[tuple[str, ...]] = []

    async def landed(self, state: TaskState, commit: str) -> None:
        self.heard.append(("landed", state.id, commit))

    async def ended(self, state: TaskState) -> None:
        self.heard.append(("ended", state.id, state.status))


class Gate:
    """Publishing that holds, once, the first event ``holds`` picks, until :attr:`released` is
    set; :attr:`reached` is set as it holds, and :attr:`events` keeps every event published."""

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
