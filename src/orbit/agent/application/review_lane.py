"""The review lane: reviews of landed work, run one at a time beside the work lane.

Each review runs in a read-only snapshot of the commit it reviews. Its suggestions become tasks
at the end of the queue, which the work lane then runs; :class:`ReviewPlanner` decides when a
review is due and stops reviews generating work forever.
"""

import asyncio
from collections.abc import Awaitable, Callable

from orbit.agent.application.agent_run import AgentRun
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lanes import Background
from orbit.agent.application.ports import CoderTree, Reply
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.reviews import ReviewJob, ReviewPlanner, review_prompt
from orbit.agent.domain.roles import (
    CODER,
    REVIEW_ROLES,
    REVIEWER,
    SUPER_REVIEWER,
    role_task,
    suggested_task,
)
from orbit.protocol import (
    ErrorOccurred,
    Notice,
    ReviewStatus,
    TaskOrigin,
    TasksChanged,
    TaskState,
    TaskStatus,
)


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
        self._background = Background("review", desk.publish)
        self._current: tuple[str, asyncio.Task[TaskStatus]] | None = None  # The review running.
        self._paused_noted = False
        self._status: ReviewStatus | None = None

    async def landed(self, task_id: str, commit: str) -> None:
        """Task ``task_id`` landed ``commit``."""
        await self._queue(self.planner.landed(self._board.get(task_id), commit))
        if self.planner.count():
            await self._queue(self.planner.super_review(commit))

    async def super_review(self) -> None:
        """Queue a super review of the codebase as the user has it: the base's latest commit.
        (The coder's tree may lag behind it, or not exist yet.)"""
        try:
            commit = await self._tree.base_commit()
        except RuntimeError as exc:  # Git failed; the review is skipped rather than the run.
            await self._publish(ErrorOccurred(message=f"Super review skipped: {exc}"))
            return
        await self._queue(self.planner.super_review(commit))

    async def ended(self, task_id: str) -> None:
        """Task ``task_id`` finished without landing anything."""
        await self._queue(self.planner.ended(self._board.get(task_id)))

    async def removed(self, task_id: str, state: TaskState) -> None:
        """Task ``task_id``, which was ``state``, was taken off the board. A review's batch
        ends as if it suggested nothing; any other task leaves its batch."""
        batch = self._batches.pop(task_id, None)
        if batch is None:
            await self._queue(self.planner.removed(state))
            return
        del self._jobs[task_id]
        open_batch = batch in self.planner.open_batches
        await self._queue(self.planner.review_ended(batch) if open_batch else None)

    def edited(self, task_id: str) -> None:
        """Task ``task_id`` was edited back to pending, so its batch waits for it again."""
        self.planner.reopened(self._board.get(task_id))

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
        task = suggested_task(self._desk.agents.roles[CODER], title, prompt)
        origin: TaskOrigin = "super_review" if self._jobs[task_id].whole else "review"
        new_id = self._board.add_suggestion(task, batch, self.planner.round_of(batch), origin)
        self.planner.added(batch, new_id)
        await self._publish(TasksChanged(tasks=self._board.tasks))
        return Reply(f"Queued as task {new_id}.")

    def queued_by_orbit(self, task_id: str) -> bool:
        """Whether task ``task_id`` is a review this lane queued, rather than one the user added."""
        return task_id in self._batches

    def cancel(self, task_id: str) -> None:
        """Stop review task ``task_id`` if it's the one running; the lane carries on."""
        if self._current is not None and self._current[0] == task_id:
            self._current[1].cancel()

    def kick(self) -> None:
        """Run the reviews waiting, if they aren't being run already."""
        if not self._background.running:
            self._background.start(self._run_reviews())

    async def close(self) -> None:
        await self._background.close()

    async def _queue(self, job: ReviewJob | None) -> None:
        await self._note_pause()
        diff = None if job is None else await self._diff(job)
        if job is None or diff is None:  # No review is due, or its diff can't be read.
            await self._publish_status()
            return
        await self._add(job, diff)
        await self._publish_status()
        self.kick()

    async def _diff(self, job: ReviewJob) -> str | None:
        """The diff ``job``'s reviewer is shown; none, reported, if git can't show it."""
        if job.whole:
            return ""
        try:
            return await self._tree.show(job.commits)
        except RuntimeError as exc:  # Git failed; the review is skipped rather than the run.
            await self._publish(ErrorOccurred(message=f"Review skipped: {exc}"))
            return None

    async def _add(self, job: ReviewJob, diff: str) -> None:
        """Queue ``job``'s review task, told ``diff``."""
        batch = self.planner.open_batch(job)
        role = SUPER_REVIEWER if job.whole else REVIEWER
        prompt = review_prompt(job, diff, self.planner.cap(job))
        task = role_task(role, self._desk.agents.roles[role], prompt)
        task_id = self._board.add_review(task)
        self._jobs[task_id], self._batches[task_id] = job, batch
        await self._publish(TasksChanged(tasks=self._board.tasks))

    async def _publish_status(self) -> None:
        status = ReviewStatus(
            paused=self.planner.paused, open_batches=len(self.planner.open_batches)
        )
        if status != self._status:
            self._status = status
            await self._publish(status)

    async def _note_pause(self) -> None:
        if self.planner.paused and not self._paused_noted:
            rounds = self.planner.limits.rounds
            message = (
                f"Review paused after {rounds} round(s) of review-suggested work;"
                " it resumes when one of your tasks lands."
            )
            await self._publish(Notice(message=message))
        self._paused_noted = self.planner.paused

    async def _run_reviews(self) -> None:
        tried = self._spent()
        while (task_id := self._board.next_runnable(skip=tried, roles=REVIEW_ROLES)) is not None:
            tried.add(task_id)
            await self._review(task_id)

    def _spent(self) -> set[str]:
        """Reviews that ended, however they did: their batch is over, so running them again
        could suggest nothing."""
        return {t for t, b in self._batches.items() if b not in self.planner.open_batches}

    async def _review(self, task_id: str) -> None:
        job = self._jobs.get(task_id)
        self._board.set_status(task_id, "running")
        await self._publish(TasksChanged(tasks=self._board.tasks))
        status: TaskStatus = "cancelled"  # What the lane stopping leaves the review as.
        reviewer = asyncio.create_task(
            self._run_reviewer(task_id, job.commits[-1] if job else None)
        )
        self._current = (task_id, reviewer)
        try:
            status = await self._outcome(reviewer)
        except Exception:  # A bug; the review still ends, or its batch would stay open.
            status = "error"
            raise
        finally:
            self._current = None
            self._board.finish(task_id, status)
            await self._publish(TasksChanged(tasks=self._board.tasks))
            if not _being_cancelled():  # The lane stopping leaves its batches as they are.
                await self._after(self._batches.get(task_id))

    async def _outcome(self, reviewer: asyncio.Task[TaskStatus]) -> TaskStatus:
        """How the review ``reviewer`` ended; anything unexpected escapes."""
        try:
            return await reviewer
        except asyncio.CancelledError:
            if _being_cancelled():  # The lane itself is stopping, not just this review.
                raise
            return "cancelled"
        except RuntimeError as exc:  # Git couldn't make or drop the snapshot.
            await self._publish(ErrorOccurred(message=str(exc)))
            return "error"

    async def _run_reviewer(self, task_id: str, commit: str | None) -> TaskStatus:
        """Run the reviewer in a snapshot of ``commit``; a review the user queued works in place."""
        root = await self._tree.snapshot(commit) if commit else "."
        try:
            return (await AgentRun(task_id, self._board, None, self._desk).run(None, root)).status
        finally:
            if commit:
                await self._tree.drop(root)

    async def _after(self, batch: str | None) -> None:
        """A review ended: review its batch if that's done, else run the suggestions it made."""
        if batch is None:
            return
        await self._queue(self.planner.review_ended(batch))
        if batch in self.planner.open_batches:
            await self._wake_work()


def _being_cancelled() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0
