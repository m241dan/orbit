"""The task board as the application changes it: published whole after each change, so the
frontend never shows it stale."""

from collections.abc import Awaitable, Callable

from orbit.agent.application.ports import Publish
from orbit.agent.domain.board import TaskBoard
from orbit.protocol import TasksChanged, TaskStatus

Changed = Callable[[], Awaitable[None]]
"""Called after a change, to act on it."""


async def unchanged() -> None:
    """Does nothing about a change."""


class Tasks:
    """``board``, with each change made through it published with ``publish``, and then
    ``changed`` called."""

    def __init__(self, board: TaskBoard, publish: Publish, changed: Changed = unchanged) -> None:
        self._board = board
        self._publish = publish
        self._changed = changed

    @property
    def board(self) -> TaskBoard:
        return self._board

    async def publish(self) -> None:
        """Publish the board as it is now."""
        await self._publish(TasksChanged(tasks=self._board.tasks))
        await self._changed()

    async def start(self, task_id: str) -> None:
        """Mark task ``task_id`` running and publish the board."""
        self._board.set_status(task_id, "running")
        await self.publish()

    async def finish(self, task_id: str, status: TaskStatus) -> None:
        """Record that task ``task_id`` ended as ``status`` and publish the board."""
        self._board.finish(task_id, status)
        await self.publish()
