"""The review lane: reviews of landed work, run one at a time beside the work lane.

Each review runs in a read-only snapshot of the commit it reviews. Its suggestions become tasks
at the end of the queue, which the work lane then runs; :class:`ReviewPlanner` decides when a
review is due and stops reviews generating work forever.
"""

import asyncio
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from functools import partial

from orbit.agent.application.agent_run import AgentRun
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lanes import Background, SessionGate
from orbit.agent.application.outcomes import Outcome, UserStop, cancelling, run_task_to_end
from orbit.agent.application.ports import CoderTree, Reply, TreeFailed
from orbit.agent.application.snapshots import snapshot_of
from orbit.agent.application.tasks import Changed, Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.endings import REVIEW_STOPPED_BY_YOU
from orbit.agent.domain.reviews import (
    LaneSnapshot,
    ReviewJob,
    ReviewPlanner,
    pause_notice,
    pending_titles,
    review_prompt,
)
from orbit.agent.domain.roles import role_task, suggested_task
from orbit.protocol import (
    CODER,
    GIT_FAILED,
    REVIEW_ROLES,
    ErrorOccurred,
    Notice,
    ReviewStatus,
    TaskState,
)


@dataclass(frozen=True)
class ReviewHooks:
    """How the review lane reaches the rest of the session.

    ``wake_work`` asks the work lane to run the suggestions a review queued, ``changed`` is
    called after the lane's state may have changed, and the session's ``gate`` tells whether
    another orbit owns the repository or the session is halted, when no review starts.
    """

    wake_work: Callable[[], Awaitable[None]]
    changed: Changed
    gate: SessionGate


class ReviewLane:
    """Queues and runs reviews of ``tasks``' tasks through ``desk``'s agents, reaching the rest
    of the session through ``hooks``."""

    def __init__(
        self, desk: AgentDesk, tasks: Tasks, planner: ReviewPlanner, hooks: ReviewHooks
    ) -> None:
        assert desk.agents.tree is not None  # Reviews read commits.
        self._desk = desk
        self._tree: CoderTree = desk.agents.tree
        self._tasks = tasks
        self._board = tasks.board
        self.planner = planner
        self._hooks = hooks
        self._publish = desk.publish
        self._jobs: dict[str, ReviewJob] = {}  # Review task id -> what it reviews.
        self._batches: dict[str, str] = {}  # Review task id -> the batch it suggests into.
        self._background = Background("review", desk.publish)
        self._current: tuple[str, asyncio.Task[Outcome]] | None = None  # The review running.
        self._reviewing: str | None = None  # The review task running, until it's followed up.
        self._paused_noted = False
        self._status: ReviewStatus | None = None

    def snapshot(self) -> LaneSnapshot:
        """The lane's state for this session, to :meth:`load` later."""
        return LaneSnapshot(
            planner=self.planner.snapshot(), jobs=dict(self._jobs), batches=dict(self._batches)
        )

    @property
    def running(self) -> bool:
        """Whether reviews are being run."""
        return self._background.running

    @property
    def reviewing(self) -> tuple[str, ...] | None:
        """The tasks whose work the review running or, before the lane picks one, the one it
        will pick reviews, in task order; none when there's no review, and no tasks for a
        review of no particular task."""
        review = self._reviewing or self._next_review(self._spent())
        if review is None:
            return None
        job = self._jobs.get(review)
        return job.reviewed if job else ()

    def on_stopped(self, stopped: Callable[[], None]) -> None:
        """Have ``stopped`` called whenever the reviews stop running."""
        self._background.ended = stopped

    def load(self, snapshot: LaneSnapshot) -> None:
        """Replace the lane's state with ``snapshot``'s; :meth:`end_interrupted` follows."""
        self.planner.restore(snapshot.planner)
        self._jobs, self._batches = dict(snapshot.jobs), dict(snapshot.batches)
        self._status = self.status  # The switch's snapshot shows the restored status,
        self._paused_noted = False  # but a pause is news.

    async def end_interrupted(self, interrupted: Collection[str]) -> None:
        """End the reviews in ``interrupted``, which were stopped by a crash, as a cancelled
        review ends: a review of their batch that's due is queued, for the next run. The board
        and the review status aren't published: the switch's snapshot, which follows, shows
        them, and :meth:`note_pause` then notes a pause."""
        for task_id in interrupted:
            batch = self._batches.get(task_id)
            if batch is not None and batch in self.planner.open_batches:
                await self._queue_quietly(self.planner.review_ended(batch))
                await self._hooks.changed()
        self._status = self.status

    async def landed(self, state: TaskState, commit: str) -> None:
        """The task that ended as ``state`` landed ``commit``. The task isn't looked up: it may
        have been removed since it finished, and its work is reviewed all the same."""
        job = self.planner.landed(state, commit)
        # Counted before the commit's own review is queued, which would hold a due super review.
        due = self.planner.count(state, self._board.tasks)
        await self._queue(job)
        if due:
            await self._queue(self.planner.super_review(commit))
        await self._hooks.changed()  # The count changed even if no review is due.

    async def super_review(self) -> None:
        """Queue a super review of the codebase as the user has it: the base's latest commit.
        (The coder's tree may lag behind it, or not exist yet.)"""
        try:
            commit = await self._tree.base_commit()
        except TreeFailed as exc:  # Git failed; the review is skipped rather than the run.
            await self._publish(
                ErrorOccurred(code=GIT_FAILED, message=f"Super review skipped: {exc}")
            )
            return
        await self._queue(self.planner.super_review(commit))

    async def ended(self, state: TaskState) -> None:
        """The task that ended as ``state`` finished without landing anything. The task isn't
        looked up: it may have been removed since it finished."""
        await self._queue(self.planner.ended(state))

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

    def edited(self, state: TaskState) -> None:
        """A task was edited back to pending, as ``state``, so its batch waits for it again. The
        task isn't looked up: it may have been removed since it was edited."""
        self.planner.reopened(state)

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
        origin = self._jobs[task_id].origin

        def add(board: TaskBoard) -> str:
            new_id = board.add_suggestion(task, batch, self.planner.round_of(batch), origin)
            self.planner.added(batch, new_id, title)
            return new_id

        new_id = await self._tasks.change(add)
        return Reply(f"Queued as task {new_id}.")

    def cancel(self, task_id: str) -> None:
        """Stop review task ``task_id`` if it's the one running; the lane carries on."""
        if self._current is not None and self._current[0] == task_id:
            self._current[1].cancel()

    def kick(self) -> None:
        """Run the reviews waiting, if they aren't being run already, unless this orbit is
        read-only or the session is halted; those queued meanwhile wait on the board."""
        gate = self._hooks.gate
        if gate.read_only() or gate.halted() or self._background.running:
            return
        self._background.start(self._run_reviews())

    async def stop(self) -> None:
        """Stop the reviews running, and wait for them to; they run again once kicked."""
        await self._background.stop()

    async def close(self) -> None:
        """Stop the reviews running for good: a kick from now on runs none."""
        await self._background.close()

    async def _queue(self, job: ReviewJob | None) -> None:
        """Queue ``job``'s review, if there is one, and run it."""
        await self.note_pause()
        diff = None if job is None else await self._diff(job)
        if job is None or diff is None:  # No review is due, or its diff can't be read.
            await self._publish_status()
            await self._hooks.changed()
            return
        await self._add(job, diff)
        await self._publish_status()
        await self._hooks.changed()
        self.kick()

    async def _queue_quietly(self, job: ReviewJob | None) -> None:
        """Queue ``job``'s review, if there is one, on the board without publishing it."""
        diff = None if job is None else await self._diff(job)
        if job is not None and diff is not None:
            self._adder(job, diff)(self._board)

    async def _diff(self, job: ReviewJob) -> str | None:
        """The diff ``job``'s reviewer is shown; none, reported, if git can't show it."""
        if job.whole:
            return ""
        try:
            return await self._tree.show(job.commits)
        except TreeFailed as exc:  # Git failed; the review is skipped rather than the run.
            await self._publish(ErrorOccurred(code=GIT_FAILED, message=f"Review skipped: {exc}"))
            return None

    async def _add(self, job: ReviewJob, diff: str) -> None:
        """Queue ``job``'s review task, told ``diff``."""
        await self._tasks.change(self._adder(job, diff))

    def _adder(self, job: ReviewJob, diff: str) -> Callable[[TaskBoard], None]:
        """The change to the board that queues ``job``'s review task, told ``diff``."""
        batch = self.planner.open_batch(job)
        role = job.role
        pending = pending_titles(self._board.tasks)
        prompt = review_prompt(job, diff, self.planner.cap(job), pending)
        task = role_task(role, self._desk.agents.roles[role], prompt)

        def add(board: TaskBoard) -> None:
            task_id = board.add_review(task)
            self._jobs[task_id], self._batches[task_id] = job, batch

        return add

    @property
    def status(self) -> ReviewStatus:
        """Whether review is paused, and how many batches are open, now."""
        return ReviewStatus(paused=self.planner.paused, open_batches=len(self.planner.open_batches))

    async def _publish_status(self) -> None:
        status = self.status
        if status != self._status:
            self._status = status
            await self._publish(status)

    async def note_pause(self) -> None:
        """Note that review is paused, unless that was noted since it paused."""
        if self.planner.paused and not self._paused_noted:
            await self._publish(Notice(message=pause_notice(self.planner.limits.rounds)))
        self._paused_noted = self.planner.paused

    async def _run_reviews(self) -> None:
        """Run the reviews waiting, one at a time, until none are left or the session halts."""
        tried = self._spent()
        while (task_id := self._next_review(tried)) is not None:
            if self._hooks.gate.halted():  # It may have halted as the last review ended.
                return
            tried.add(task_id)
            self._reviewing = task_id
            try:
                await self._review(task_id)
            finally:
                self._reviewing = None

    def _next_review(self, tried: Collection[str]) -> str | None:
        """The next review to run, skipping those ``tried``."""
        return self._board.next_runnable(skip=tried, roles=REVIEW_ROLES)

    def _spent(self) -> set[str]:
        """Reviews that ended, however they did: their batch is over or no longer filling, so
        running them again could suggest nothing."""
        return {t for t, b in self._batches.items() if not self.planner.filling(b)}

    async def _review(self, task_id: str) -> None:
        # Only :meth:`cancel`, which the user asks for, stops a review on its own.
        run = partial(self._reviewed, task_id)
        stop = UserStop(REVIEW_STOPPED_BY_YOU)
        try:  # Even a bug ends the review, or its batch would stay open.
            await run_task_to_end(self._tasks, task_id, run, stop, self._publish)
        finally:
            await self._ended(task_id)

    async def _ended(self, task_id: str) -> None:
        """The review of ``task_id`` ended: go on as :meth:`_after` says, unless the lane is
        stopping, which leaves its batches as they are."""
        if not cancelling():
            await self._after(self._batches.get(task_id))

    async def _reviewed(self, task_id: str) -> Outcome:
        """How started review ``task_id`` ended, after which it's no longer the current one;
        anything unexpected escapes."""
        job = self._jobs.get(task_id)
        reviewer = asyncio.create_task(
            self._run_reviewer(task_id, job.commits[-1] if job else None)
        )
        self._current = (task_id, reviewer)
        try:
            return await reviewer
        finally:
            self._current = None

    async def _run_reviewer(self, task_id: str, commit: str | None) -> Outcome:
        """Run the reviewer in a snapshot of ``commit``; a review the user queued works in place."""
        run = AgentRun(task_id, self._tasks, None, self._desk).run
        if not commit:
            return await run(None, self._desk.agents.root)
        async with snapshot_of(self._tree, commit, self._publish) as root:
            return await run(None, root)

    async def _after(self, batch: str | None) -> None:
        """A review ended: review its batch if that's done, else run the suggestions it made."""
        if batch is None:
            return
        await self._queue(self.planner.review_ended(batch))
        if batch in self.planner.open_batches:
            await self._hooks.wake_work()
