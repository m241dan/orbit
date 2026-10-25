"""Moving a requirements record a task names to the next free number, when another record took
its number since the task was planned, such as by a plan made in another session; each move is
kept in the store, so a task that still names the old path, in any session, follows it."""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePath

from orbit.agent.application.failures import ReportedOnce, outcome
from orbit.agent.application.ports import FileNames, Publish, SessionStore, StoreFailed
from orbit.agent.application.storage_halt import Halting, written
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.records import (
    clashing_records,
    moved_note,
    moved_records,
    named_records,
    planner_note,
    record_directories,
)
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.protocol import INTERNAL, Notice


def _unheard(note: str) -> None:
    """Tell no one ``note``."""


@dataclass(frozen=True)
class MoveHooks:
    """What moving records uses: ``names`` lists the files in a directory (without it, records
    keep the numbers tasks give them), ``publish`` tells the user, and ``tell_planner`` has the
    session's planner hear a note ahead of its next prompt."""

    names: FileNames | None
    publish: Publish
    tell_planner: Callable[[str], None] = _unheard


class RecordMoves:
    """Moves the records ``tasks``' tasks name, keeping each move in ``store`` (if there is
    one); a failing read of the moves kept is reported, and a failing save halts the session
    through ``halt`` and is kept, with those after it, to be saved when the halt is retried.
    Nothing is saved while it's halted."""

    write = FailedWrite.RECORD_MOVES

    def __init__(
        self, tasks: Tasks, store: SessionStore | None, halt: Halting, hooks: MoveHooks
    ) -> None:
        self._tasks = tasks
        self._store = store
        self._halt = halt
        self._hooks = hooks
        self._reads = ReportedOnce(hooks.publish, INTERNAL)
        self._pending: dict[str, str] = {}  # The moves not yet kept.
        self._saving = asyncio.Lock()  # Saves once at a time, a retry's included.

    async def move(self, task_id: str, root: str) -> None:
        """Move each record task ``task_id`` names that isn't written at ``root`` yet but whose
        number another record there has: it's renamed in every task that hasn't completed,
        where a move kept or another task on the board says it went, if either does, the user
        and the planner told, and the move kept."""
        names = self._hooks.names
        if names is None:
            return
        moves = await self._moves(task_id, root, names)
        if not moves:
            return
        for old, new in moves.items():
            self._hooks.tell_planner(planner_note(old, new))
        # After telling the planner, so the session saved with the board holds what it heard.
        await self._tasks.change(lambda board: board.move_records(moves))
        for old, new in moves.items():
            await self._hooks.publish(Notice(message=moved_note(old, new)))
        await self._keep(moves)

    async def _moves(self, task_id: str, root: str, names: FileNames) -> dict[str, str]:
        """Where the records task ``task_id`` names go, the files at ``root`` listed with
        ``names``."""
        board = self._tasks.board
        prompt = board.get(task_id).task.prompt
        listings = {
            # A large directory, or a slow filesystem, would stall the event loop.
            directory: await asyncio.to_thread(names, str(PurePath(root, directory)))
            for directory in record_directories(prompt)
        }
        if not clashing_records(prompt, listings):
            return {}  # Scanning every other task's prompt for its records takes long.
        others = named_records(t.task.prompt for t in board.tasks if t.id != task_id)
        return moved_records(prompt, listings, others, await self._logged())

    async def _logged(self) -> Mapping[str, str]:
        """The moves kept; none without a store, or when they can't be read, the first such
        failure since a read worked reported."""
        if self._store is None:
            return {}
        return await self._reads.value(self._store.record_moves()) or {}

    async def _keep(self, moves: Mapping[str, str]) -> None:
        """Keep ``moves`` in the store, if there is one, unless the session is halted, halting
        it if the save fails."""
        if self._store is None:
            return
        store = self._store
        self._pending |= moves
        async with self._saving:
            await written(self._halt, self.write, lambda: self._save(store))

    async def retry(self) -> StoreFailed | None:
        """Keep the moves not yet kept, though the session is halted, as resuming does; returns
        the error when the save fails."""
        if self._store is None:
            return None
        async with self._saving:
            return await self._save(self._store)

    async def _save(self, store: SessionStore) -> StoreFailed | None:
        """Keep the moves not yet kept in ``store``; returns the error when it fails."""
        if not self._pending:
            return None
        pending = dict(self._pending)
        error = await outcome(store.save_record_moves(pending))
        if isinstance(error, StoreFailed):
            return error
        # A move made as it saved stays to be saved.
        self._pending = {old: new for old, new in self._pending.items() if pending.get(old) != new}
        return None
