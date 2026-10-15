"""The review lane: reviews of landed work, run one at a time beside the work lane.

Each review runs in a read-only snapshot of the commit it reviews. Its suggestions become tasks
at the end of the queue, which the work lane then runs; :class:`ReviewPlanner` decides when a
review is due and stops reviews generating work forever.
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

from orbit.agent.application.agent_run import AgentDesk, AgentRun
from orbit.agent.application.ports import CoderTree, Reply
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.reviews import ReviewJob, ReviewPlanner, review_prompt
from orbit.agent.domain.roles import role_task, suggested_task
from orbit.protocol import (
    Notice,
    ReviewStatus,
    TaskOrigin,
    TasksChanged,
    TaskStatus,
)

REVIEW_ROLES = frozenset({"reviewer", "super-reviewer"})


class ReviewLane:
    """Queues and runs reviews of ``board``'s tasks through ``desk``'s agents.

    ``wake_work`` asks the work lane to run the suggestions a review queued.
    """

    def __init__(
        self,
        desk: AgentDesk,
        board: TaskBoard,
        planner: ReviewPlanner,
        wake_work: Callable[[], Awaitable[None]],
    ) -> None:
        assert desk.agents.tree is not None  # Reviews read commits.
        self._desk = desk
        self._tree: CoderTree = desk.agents.tree
        self._board = board
        self.planner = planner
        self._wake_work = wake_work
        self._publish = desk.publish
        self._jobs: dict[str, ReviewJob] = {}  # Review task id -> what it reviews.
        self._batches: dict[str, str] = {}  # Review task id -> the batch it suggests into.
        self._running: asyncio.Task[None] | None = None
        self._current: tuple[str, asyncio.Task[TaskStatus]] | None = None  # The review running.
        self._paused_noted = False
        self._status: ReviewStatus | None = None

    async def landed(self, task_id: str, commit: str) -> None:
        """Task ``task_id`` landed ``commit``."""
        await self._queue(self.planner.landed(self._board.get(task_id), commit))
        if self.planner.count():
            await self._queue(self.planner.super_review(commit))

    async def super_review(self) -> None:
        """Queue a super review of the codebase as it is now."""
        await self._queue(self.planner.super_review(await self._tree.head()))

    async def ended(self, task_id: str) -> None:
        """Task ``task_id`` finished without landing anything."""
        await self._queue(self.planner.ended(self._board.get(task_id)))

    def joined(self, task_id: str) -> None:
        """Task ``task_id`` was added to fix another; if that one is in a batch, so is it."""
        batch = self._board.get(task_id).batch
        if batch is not None and batch in self.planner.open_batches:
            self.planner.added(batch, task_id)

    async def suggest(self, task_id: str, title: str, prompt: str) -> Reply:
        """Review task ``task_id`` suggests a task; queue it unless it's refused."""
        batch = self._batches.get(task_id)
        if batch is None:
            return Reply("Only reviews orbit queued itself can suggest tasks.", is_error=True)
        refusal = self.planner.refusal(batch, title, self._board.tasks)
        if refusal is not None:
            return Reply(refusal, is_error=True)
        task = suggested_task(self._desk.agents.roles["coder"], title, prompt)
        origin: TaskOrigin = "super_review" if self._jobs[task_id].whole else "review"
        new_id = self._board.add_suggestion(task, batch, self.planner.round_of(batch), origin)
        self.planner.added(batch, new_id)
        await self._publish(TasksChanged(tasks=self._board.tasks))
        return Reply(f"Queued as task {new_id}.")

    def cancel(self, task_id: str) -> None:
        """Stop review task ``task_id`` if it's the one running; the lane carries on."""
        if self._current is not None and self._current[0] == task_id:
            self._current[1].cancel()

    def kick(self) -> None:
        """Run the reviews waiting, if they aren't being run already."""
        if self._running is None or self._running.done():
            self._running = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self._running is not None and not self._running.done():
            self._running.cancel()
            with suppress(asyncio.CancelledError):
                await self._running

    async def _queue(self, job: ReviewJob | None) -> None:
        await self._note_pause()
        if job is None:
            await self._publish_status()
            return
        diff = "" if job.whole else await self._tree.show(job.commits)
        batch = self.planner.open_batch(job)
        role = "super-reviewer" if job.whole else "reviewer"
        cap = self.planner.super_cap if job.whole else self.planner.cap
        task = role_task(role, self._desk.agents.roles[role], review_prompt(job, diff, cap))
        task_id = self._board.add_review(task)
        self._jobs[task_id], self._batches[task_id] = job, batch
        await self._publish(TasksChanged(tasks=self._board.tasks))
        await self._publish_status()
        self.kick()

    async def _publish_status(self) -> None:
        status = ReviewStatus(
            paused=self.planner.paused, open_batches=len(self.planner.open_batches)
        )
        if status != self._status:
            self._status = status
            await self._publish(status)

    async def _note_pause(self) -> None:
        if self.planner.paused and not self._paused_noted:
            message = (
                f"Review paused after {self.planner.rounds} round(s) of review-suggested work;"
                " it resumes when one of your tasks lands."
            )
            await self._publish(Notice(message=message))
        self._paused_noted = self.planner.paused

    async def _run(self) -> None:
        tried: set[str] = set()
        while (task_id := self._board.next_runnable(skip=tried, roles=REVIEW_ROLES)) is not None:
            tried.add(task_id)
            await self._review(task_id)

    async def _review(self, task_id: str) -> None:
        job = self._jobs.get(task_id)
        self._board.set_status(task_id, "running")
        await self._publish(TasksChanged(tasks=self._board.tasks))
        status: TaskStatus = "cancelled"
        reviewer = asyncio.create_task(
            self._run_reviewer(task_id, job.commits[-1] if job else None)
        )
        self._current = (task_id, reviewer)
        try:
            status = await reviewer
        except asyncio.CancelledError:
            if _being_cancelled():  # The lane itself is stopping, not just this review.
                raise
        finally:
            self._current = None
            self._board.finish(task_id, status)
            await self._publish(TasksChanged(tasks=self._board.tasks))
        await self._after(self._batches.get(task_id))

    async def _run_reviewer(self, task_id: str, commit: str | None) -> TaskStatus:
        """Run the reviewer in a snapshot of ``commit``; a review the user queued works in place."""
        root = await self._tree.snapshot(commit) if commit else "."
        try:
            return (await AgentRun(task_id, self._board, None, self._desk).run(None, root)).status
        finally:
            if commit:
                await self._tree.drop(root)

    async def _after(self, batch: str | None) -> None:
        """A review ended: wait on its batch's suggestions, and run them, if it made any."""
        if batch is None:
            return
        self.planner.close_if_empty(batch)
        await self._publish_status()
        if batch in self.planner.open_batches:
            await self._wake_work()


def _being_cancelled() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0
