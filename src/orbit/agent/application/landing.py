"""Landing a task's verified work: committing it in the coder's tree and merging it."""

from dataclasses import dataclass

from orbit.agent.application.lanes import waited_out
from orbit.agent.application.ports import CoderTree, Merge, Publish
from orbit.agent.application.task_trace import MERGE_CONFLICT_EVENT, MERGE_RESULT
from orbit.agent.application.verifier import Failure, Verifier
from orbit.protocol import MergeResult, TaskMerged
from orbit.tracing import current_span

# Times the work is rebased onto a base that keeps moving before orbit stops trying.
MAX_REBASES = 3


@dataclass(frozen=True)
class Landing:
    """What landing a task's work came to: the commit it made, if any, and the ``result`` of
    merging it, or why the work failed verification after being rebased onto a moved base.
    Only a ``merged`` commit is on the base."""

    commit: str | None = None
    result: MergeResult | None = None
    failure: Failure | None = None

    @property
    def merged(self) -> str | None:
        """The commit the work landed on the base, if it merged."""
        return self.commit if self.result == "merged" else None


class Lander:
    """Commits and merges tasks' work from ``tree``, verifying it again after a rebase; the
    merge's result is recorded on the current span. :attr:`merged` is the commit the work
    landed on the base, if it merged, even when the landing was cancelled after the merge."""

    def __init__(self, tree: CoderTree, verifier: Verifier | None, publish: Publish) -> None:
        self._tree = tree
        self._verifier = verifier
        self._publish = publish
        self.merged: str | None = None

    async def land(self, task_id: str, message: str, root: str, start: str) -> Landing:
        """Commit task ``task_id``'s work with ``message`` and merge it; ``start`` is the tree's
        head before the task, so commits the agent made itself count too."""
        await self._tree.commit(message)
        merge = await self._integrate(task_id, root)
        if not isinstance(merge, Merge):
            return Landing(failure=merge)
        _record_merge(merge)
        # Waited out: work that merged is on the base, so it's always noted and told of.
        return await waited_out(self._report(task_id, merge, start))

    async def _report(self, task_id: str, merge: Merge, start: str) -> Landing:
        """What task ``task_id``'s ``merge`` landed, from the tree's head before the task,
        ``start``, noted as :attr:`merged` and published."""
        # Read after any rebase, which rewrites the commits.
        head = await self._tree.head()
        landing = Landing(commit=head if head != start else None, result=merge.result)
        self.merged = landing.merged
        await self._publish(
            TaskMerged(
                task_id=task_id,
                result=merge.result,
                detail=merge.detail,
                commit=landing.commit or "",
            )
        )
        return landing

    async def _integrate(self, task_id: str, root: str) -> Merge | Failure:
        """Merge the tree's work, rechecking it after each of at most MAX_REBASES rebases;
        the first recheck that fails is returned instead."""
        merge = await self._tree.integrate()
        for _ in range(MAX_REBASES):
            if merge.result != "rebased":
                return merge
            failure = await self._recheck(task_id, root)
            if failure is not None:
                return failure
            merge = await self._tree.integrate()
        if merge.result == "rebased":
            return Merge(
                "rebased", f"The base branch kept moving; the work is on {self._tree.branch}."
            )
        return merge

    async def _recheck(self, task_id: str, root: str) -> Failure | None:
        if self._verifier is None:
            return None
        return await self._verifier.check(task_id, None, root)


def _record_merge(merge: Merge) -> None:
    """Record what came of ``merge`` on the current span, and a conflict as an event."""
    span = current_span()
    span.set_attributes({MERGE_RESULT: merge.result})
    if merge.result == "conflict":
        span.event(MERGE_CONFLICT_EVENT)
