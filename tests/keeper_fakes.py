"""Fakes for a session keeper: the contents it saves and what it publishes."""

from dataclasses import replace

from orbit.agent.application.keeper import Contents, SessionKeeper
from orbit.protocol import PLANNER, ErrorOccurred, Event, Settings, TaskState, Usage
from tests import server_fakes

TASK = TaskState(id="1", task=server_fakes.TASK)


EMPTY = Contents(
    role_settings={PLANNER: Settings()}, conversations={}, tasks=(), reviews=None, spent=Usage()
)


WITH_TASK = replace(EMPTY, tasks=(TASK,))


class Published:
    """Collects what a keeper publishes."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    @property
    def errors(self) -> list[str]:
        return [e.message for e in self.events if isinstance(e, ErrorOccurred)]

    @property
    def codes(self) -> list[str]:
        return [e.code for e in self.events if isinstance(e, ErrorOccurred)]


async def save(keeper: SessionKeeper, contents: Contents) -> None:
    await keeper.save(lambda: contents)
