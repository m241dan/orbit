"""Landing a task's verified work: committing it in the coder's tree and merging it, with any
work stacked beneath it there, which waited for merges to resume."""

from collections.abc import Sequence
from dataclasses import dataclass

from orbit.agent.application.ports import CoderTree, Merge, Publish
from orbit.agent.application.task_trace import MERGE_CONFLICT_EVENT, MERGE_RESULT
from orbit.agent.application.verifier import Failure, Verifier
from orbit.agent.domain.merge_holds import stacked_commits
from orbit.execution.cancelling import waited_out
from orbit.protocol import MergeResult, TaskMerged
from orbit.tracing import current_span

# Times the work is rebased onto a base that keeps moving before orbit stops trying.
MAX_REBASES = 3


@dataclass(frozen=True)
class Landing:
    """What landing a task's work came to: the commit it made, if any, and the ``result`` of
    merging it, or why the work failed verification after being rebased onto a moved base.
    Only a ``merged`` commit is on the base. ``detail`` says why the work didn't merge, as the
    tree told. ``stacked`` is, for each piece of work stacked beneath it, in order, the last
    commit it landed with, if it merged, as its commits' subjects find it after any rebase: none
    if the rebase dropped all its commits, left empty, and the last commit of the landing if its
    subjects aren't known."""

    commit: str | None = None
    result: MergeResult | None = None
    failure: Failure | None = None
    stacked: tuple[str | None, ...] = ()
    detail: str = ""

    @property
    def merged(self) -> str | None:
        """The commit the work landed on the base, if it merged."""
        return self.commit if self.result == "merged" else None


Stacked = tuple[str, tuple[str, ...]]
"""A piece of work waiting in the tree: the id of the task whose it is, and the subjects of the
commits it made, oldest first; none if they aren't known."""


@dataclass(frozen=True)
class _Stack:
    """The work in the tree ahead of the base's ``base`` commit: the subjects of the commits
    each piece of it made, as :data:`Stacked` gives them, in order, oldest first, the last of
    them the landing task's own if it lands with them."""

    base: str
    pieces: tuple[tuple[str, ...], ...]


class Lander:
    """Commits and merges tasks' work from ``tree``, verifying it again after a rebase; the
    merge's result is recorded on the current span. :attr:`merged` is the commit the work
    landed on the base, if it merged, even when the landing was cancelled after the merge, and
    :attr:`stacked` the commits that landed with it, as :attr:`Landing.stacked` says. Each
    piece of work that merges is published as merged, those stacked first, in order."""

    def __init__(self, tree: CoderTree, verifier: Verifier | None, publish: Publish) -> None:
        self._tree = tree
        self._verifier = verifier
        self._publish = publish
        self.merged: str | None = None
        self.stacked: tuple[str | None, ...] = ()

    async def land(
        self, task_id: str, message: str, root: str, start: str, beneath: Sequence[Stacked] = ()
    ) -> Landing:
        """Commit task ``task_id``'s work with ``message`` and merge it; ``start`` is the tree's
        head before the task, so commits the agent made itself count too. The work stacked
        ``beneath`` it, in order, merges with it."""
        await self._tree.commit(message)
        stack = await self._stack(start, beneath)
        merge = await self._integrate(task_id, root)
        if not isinstance(merge, Merge):
            return Landing(failure=merge)
        _record_merge(merge)
        # Waited out: work that merged is on the base, so it's always noted and told of.
        return await waited_out(self._report(task_id, merge, start, (beneath, stack)))

    async def land_stacked(self, root: str, stacked: Sequence[Stacked]) -> Landing:
        """Merge the work ``stacked`` in the tree, in order, rechecking it after a rebase as
        the last piece's task's, at ``root``; nothing is committed. Its result is published for
        the pieces that merge."""
        stack = _Stack(await self._tree.base_commit(), tuple(subjects for _, subjects in stacked))
        merge = await self._integrate(stacked[-1][0], root)
        if not isinstance(merge, Merge):
            return Landing(failure=merge)
        _record_merge(merge)
        return await waited_out(self._landed(merge, stacked, stack))

    async def _stack(self, start: str, beneath: Sequence[Stacked]) -> _Stack | None:
        """The work stacked in the tree, with the commits since ``start`` its own on top of the
        work ``beneath``; none if nothing's beneath it."""
        if not beneath:
            return None
        own = await self._tree.list_commits(start, await self._tree.head())
        pieces = (*(subjects for _, subjects in beneath), tuple(c.subject for c in own))
        return _Stack(await self._tree.base_commit(), pieces)

    async def _report(
        self,
        task_id: str,
        merge: Merge,
        start: str,
        beneath: tuple[Sequence[Stacked], _Stack | None],
    ) -> Landing:
        """What task ``task_id``'s ``merge`` landed, from the tree's head before the task,
        ``start``, with the work stacked ``beneath`` it, if any, noted and published."""
        # Read after any rebase, which rewrites the commits.
        head = await self._tree.head()
        pieces, stack = beneath
        stacked = () if stack is None else await self._stacked(merge, head, stack)
        landing = Landing(
            commit=head if head != start else None, result=merge.result, stacked=stacked[:-1]
        )
        self.merged = landing.merged
        self.stacked = landing.stacked
        await self._publish_stacked(pieces, landing.stacked)
        await self._publish(
            TaskMerged(
                task_id=task_id,
                result=merge.result,
                detail=merge.detail,
                commit=landing.commit or "",
            )
        )
        return landing

    async def _landed(self, merge: Merge, pieces: Sequence[Stacked], stack: _Stack) -> Landing:
        """What ``merge`` landed of the work ``pieces``, stacked as ``stack`` says, noted as
        :attr:`merged` and :attr:`stacked` and published."""
        head = await self._tree.head()
        stacked = await self._stacked(merge, head, stack)
        landing = Landing(head, merge.result, stacked=stacked, detail=merge.detail)
        self.merged = landing.merged
        self.stacked = landing.stacked
        await self._publish_stacked(pieces, stacked)
        return landing

    async def _publish_stacked(
        self, pieces: Sequence[Stacked], commits: Sequence[str | None]
    ) -> None:
        """Publish each of the work ``pieces`` that landed as merged, with its last commit, of
        ``commits``, in order; none if none merged."""
        for (task_id, _), commit in zip(pieces, commits, strict=False):
            if commit is not None:
                await self._publish(TaskMerged(task_id=task_id, result="merged", commit=commit))

    async def _stacked(self, merge: Merge, head: str, stack: _Stack) -> tuple[str | None, ...]:
        """The last commit each piece of ``stack``'s work landed with, up to ``head``, if
        ``merge`` merged it; none otherwise."""
        if merge.result != "merged":
            return ()
        return stacked_commits(await self._tree.list_commits(stack.base, head), stack.pieces)

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
