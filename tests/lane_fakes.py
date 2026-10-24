"""Fakes for the work and review lanes: a review lane that hears how tasks ended, and hooks
that wake nothing."""

from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lanes import OPEN_GATE
from orbit.agent.application.review_lane import ReviewHooks, ReviewLane
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.reviews import ReviewLimits, ReviewPlanner
from orbit.protocol import TaskState


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
