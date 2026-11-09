"""The task board as the application changes it: published after each change, whole or as the
one task changed, so the frontend never shows it stale."""

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial

from orbit.agent.application.ports import Publish
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.refusals import Refused
from orbit.protocol import Event, TaskChanged, TasksChanged, TaskStatus, Usage

Changed = Callable[[], Awaitable[None]]
"""Called after a change, to act on it."""


async def unchanged() -> None:
    """Does nothing about a change."""


class Tasks:
    """``board``, with each change made through it published with ``publish``, and then
    ``changed`` called, but for a turn's usage, which waits for the next change to be acted
    on."""

    def __init__(self, board: TaskBoard, publish: Publish, changed: Changed = unchanged) -> None:
        self._board = board
        self._publish = publish
        self._changed = changed
        self._moved = asyncio.Event()  # Set, then replaced, as the board is published.

    @property
    def board(self) -> TaskBoard:
        return self._board

    async def change[T](
        self,
        change: Callable[[TaskBoard], T],
        refused: Callable[[Refused], Awaitable[T]] | None = None,
    ) -> T:
        """Apply ``change`` to the board and publish it whole; returns what ``change`` returned.
        If the board refuses it with :class:`Refused`, nothing is published and that refusal
        escapes, or is handed to ``refused``, whose result is returned. Other errors, such as
        those from publishing, always escape. For a change to which tasks the board holds or
        their order, or to several of them."""
        return await self._apply(change, refused, self.publish)

    async def change_task[T](
        self,
        task_id: str,
        change: Callable[[TaskBoard], T],
        refused: Callable[[Refused], Awaitable[T]] | None = None,
    ) -> T:
        """Apply ``change``, which changes task ``task_id`` alone, to the board and publish that
        task; returns what ``change`` returned, and is refused as :meth:`change` is."""
        return await self._apply(
            change, refused, partial(self._publish_task, task_id, self._changed)
        )

    async def add_usage(
        self, task_id: str, usage: Usage, refused: Callable[[Refused], Awaitable[None]]
    ) -> None:
        """Count ``usage``, what a turn run for task ``task_id`` cost, towards its total and
        publish that task, leaving the change to be acted on with the next, so a turn costs no
        more as the board grows; a board refusing it, as for a removed task, hands its refusal
        to ``refused``."""

        def count(board: TaskBoard) -> None:
            board.add_usage(task_id, usage)

        await self._apply(count, refused, partial(self._publish_task, task_id, unchanged))

    async def _apply[T](
        self,
        change: Callable[[TaskBoard], T],
        refused: Callable[[Refused], Awaitable[T]] | None,
        publish: Callable[[], Awaitable[None]],
    ) -> T:
        """Apply ``change`` to the board, then ``publish`` it; returns what ``change`` returned,
        and is refused as :meth:`change` is."""
        try:
            result = change(self._board)
        except Refused as exc:
            if refused is None:
                raise
            return await refused(exc)
        await publish()
        return result

    async def publish(self) -> None:
        """Publish the board as it is now, whole."""
        await self._announce(TasksChanged(tasks=self._board.tasks), self._changed)

    async def _publish_task(self, task_id: str, then: Changed) -> None:
        """Publish task ``task_id`` as it is now, then call ``then``."""
        await self._announce(TaskChanged(task=self._board.get(task_id)), then)

    async def _announce(self, event: Event, then: Changed) -> None:
        """Publish ``event``, the board's change, wake those waiting for one, then call
        ``then``."""
        await self._publish(event)
        self._moved.set()
        self._moved = asyncio.Event()
        await then()

    async def moved(self) -> None:
        """Wait until the board is next published, whole or a task of it."""
        await self._moved.wait()

    async def changed_elsewhere(self) -> None:
        """Act on a change made outside the board, as on one made to it."""
        await self._changed()

    async def start(self, task_id: str) -> None:
        """Mark task ``task_id`` running and publish it."""
        await self.change_task(task_id, lambda board: board.set_status(task_id, "running"))

    async def finish(
        self, task_id: str, status: TaskStatus, reason: str = "", detail: str = ""
    ) -> None:
        """Record that task ``task_id`` ended as ``status``, for ``reason`` (``detail`` in full),
        and publish the board whole, as a completed fix also completes what it fixed."""
        await self.change(lambda board: board.finish(task_id, status, reason, detail))
