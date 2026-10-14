"""Landing a task's verified work: committing it in the coder's tree and merging it."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from orbit.agent.application.ports import CoderTree, Merge
from orbit.agent.application.verifier import Failure, Verifier
from orbit.agent.domain.board import TaskBoard
from orbit.protocol import Event, TaskMerged

# Times the work is rebased onto a base that keeps moving before orbit stops trying.
MAX_REBASES = 3


@dataclass(frozen=True)
class Landing:
    """What landing a task's work came to: the commit it made, if any, or why the work
    failed verification after being rebased onto a moved base."""

    commit: str | None = None
    failure: Failure | None = None


class Lander:
    """Commits and merges tasks' work from ``tree``, verifying it again after a rebase."""

    def __init__(
        self,
        tree: CoderTree,
        board: TaskBoard,
        verifier: Verifier | None,
        publish: Callable[[Event], Awaitable[None]],
    ) -> None:
        self._tree = tree
        self._board = board
        self._verifier = verifier
        self._publish = publish

    async def land(self, task_id: str, message: str, root: str, start: str) -> Landing:
        """Commit task ``task_id``'s work with ``message`` and merge it; ``start`` is the tree's
        head before the task, so commits the agent made itself count too."""
        await self._tree.commit(message)
        merge = await self._tree.integrate()
        for _ in range(MAX_REBASES):
            if merge.result != "rebased":
                break
            failure = await self._recheck(task_id, root)
            if failure is not None:
                return Landing(failure=failure)
            merge = await self._tree.integrate()
        else:
            merge = Merge("rebased", "The base branch kept moving; the work is on orbit/coder.")
        # Read after any rebase, which rewrites the commits.
        head = await self._tree.head()
        commit = head if head != start else None
        await self._publish(
            TaskMerged(
                task_id=task_id, result=merge.result, detail=merge.detail, commit=commit or ""
            )
        )
        return Landing(commit=commit)

    async def _recheck(self, task_id: str, root: str) -> Failure | None:
        if self._verifier is None:
            return None
        return await self._verifier.check(task_id, None, root)
