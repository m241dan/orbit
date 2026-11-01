"""The review lane: reviews of landed work, run one at a time beside the work lane.

Each review runs in a read-only snapshot of the commit it reviews, on the :class:`LaneRunner` of
the review roles' lane, which runs the tasks of every role in it, the others as any lane runs its
tasks. Its suggestions become tasks at the end of the queue, which the work lane then runs;
:class:`ReviewPlanner` decides when a review is due and stops reviews generating work forever.
It's the ``review-rounds`` guard: the lane raises each landing as ``task_landed``, and a review
of commits is queued by the triggers it guards, from their templates. It counts each landing
toward the ``commits`` triggers too, and a super review is queued by those that fire, or by the
super-review trigger fired by hand.
"""

import random
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import partial

from orbit.agent.application.agent_run import AgentRun
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lane_runner import (
    LaneParts,
    LaneRunner,
    LaneWork,
    RuntimePauses,
    Unpausable,
    run_in_role_workspace,
    unpaused,
    unposted,
)
from orbit.agent.application.lanes import SessionGate
from orbit.agent.application.outcomes import Outcome, UserStop, run_task_to_end
from orbit.agent.application.ports import CoderTree, Reply, TreeFailed
from orbit.agent.application.review_trace import Queued, ReviewTally, ReviewTracing
from orbit.agent.application.snapshots import snapshot_of
from orbit.agent.application.tasks import Changed, Tasks
from orbit.agent.application.triggers import ReviewRound, TriggerEngine
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.endings import REVIEW_STOPPED_BY_YOU
from orbit.agent.domain.reviews import (
    LandedSince,
    LaneSnapshot,
    ReviewJob,
    ReviewPlanner,
    pause_notice,
    pending_titles,
    review_values,
    tell_landed_since,
)
from orbit.agent.domain.role_lanes import review_lane, task_lanes
from orbit.agent.domain.suggestions import suggested_task, suggestion_refusal
from orbit.agent.domain.triggers import Trigger, restart_counts
from orbit.execution.cancelling import cancelling
from orbit.protocol import (
    CODER,
    GIT_FAILED,
    REVIEW_ROLES,
    SUPER_REVIEW,
    ErrorOccurred,
    Event,
    Notice,
    ReviewStatus,
    TaskState,
)
from orbit.tracing import Span, current_span


@dataclass(frozen=True)
class ReviewHooks:
    """How the review lane reaches the rest of the session.

    ``wake_work`` asks the work lane to run the suggestions a review queued, while the
    review's span is current, ``changed`` is called after the lane's state may have changed,
    the session's ``gate`` tells whether another orbit owns the repository or the session is
    halted, when no review starts, ``paused`` the runtime whose pause the work lane waits out,
    if any, on which no review starts, ``tracing`` traces the reviews, ``post`` publishes,
    without blocking, whether the lane is busy, ``pauses`` pauses the runtime of a task
    whose agent hit its limit or an outage, and ``triggers`` fires the triggers on the landings
    the lane hears of and on the reviews of batches it finds due; without them, the lane fires
    its agents' triggers itself, running only the tasks of its own lane they queue.
    """

    wake_work: Callable[[], Awaitable[None]]
    changed: Changed
    gate: SessionGate
    tracing: ReviewTracing = field(default_factory=ReviewTracing)
    paused: Callable[[], str | None] = unpaused
    post: Callable[[Event], None] = unposted
    pauses: RuntimePauses = field(default_factory=Unpausable)
    triggers: TriggerEngine | None = None


class ReviewLane:
    """Queues and runs reviews of ``tasks``' tasks through ``desk``'s agents, reaching the rest
    of the session through ``hooks``.

    Each review is traced from its start until it's followed up, linked to the span current as
    it was queued: the task whose landing, or ending, made it due, the command asking for it,
    or the review whose end did."""

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
        # Review task id -> the span current as it was queued, this time the session was open,
        # until it ends.
        self._causes: dict[str, Span] = {}
        self._tallies: dict[str, ReviewTally] = {}  # Review task id -> its suggestions, as it runs.
        self._triggers = hooks.triggers or TriggerEngine(desk, tasks, self._kick_own)
        self.runner = self._runner(hooks)
        self._paused_noted = False
        self._status: ReviewStatus | None = None

    def _runner(self, hooks: ReviewHooks) -> LaneRunner:
        """The runner of the review roles' lane, running the reviews, and the tasks of the other
        roles in it as it runs any lane's, reaching the rest of the session through
        ``hooks``."""
        roles = self._desk.agents.roles
        lane = review_lane(roles)
        work = LaneWork(self._next_review, self._review, self._spent)
        parts = LaneParts(
            self._desk, self._tasks, hooks.gate, hooks.paused, hooks.post, hooks.pauses
        )
        return LaneRunner(lane, task_lanes(roles).get(lane, frozenset()), parts, work)

    def snapshot(self) -> LaneSnapshot:
        """The lane's state for this session, to :meth:`load` later."""
        return LaneSnapshot(
            planner=self.planner.snapshot(), jobs=dict(self._jobs), batches=dict(self._batches)
        )

    @property
    def running(self) -> bool:
        """Whether reviews are being run."""
        return self.runner.running

    @property
    def roles(self) -> frozenset[str]:
        """The roles whose tasks the lane runs: those in the review roles' lane."""
        return self.runner.roles

    @property
    def reviewing(self) -> tuple[str, ...] | None:
        """The tasks whose work the review running or, before the lane picks one, the one it
        will pick reviews, in task order; none when there's no review, such as while the lane
        is on another role's task, and no tasks for a review of no particular task."""
        review = self.runner.current
        if review is None or not self._is_review(review):
            return None
        job = self._jobs.get(review)
        return job.reviewed if job else ()

    @property
    def task(self) -> str | None:
        """The task of another role than the review roles that the lane, while it runs, is on
        or, before it picks one, will pick; none when that's a review, or there's none."""
        task_id = self.runner.current if self.running else None
        return None if task_id is None or self._is_review(task_id) else task_id

    def on_stopped(self, stopped: Callable[[], None]) -> None:
        """Have ``stopped`` called whenever the reviews stop running."""
        self.runner.on_stopped(stopped)

    def load(self, snapshot: LaneSnapshot) -> None:
        """Replace the lane's state with ``snapshot``'s; :meth:`end_interrupted` follows."""
        self.planner.restore(snapshot.planner)
        self._jobs, self._batches = dict(snapshot.jobs), dict(snapshot.batches)
        self._causes.clear()  # The session's task ids are reused by the next.
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
        """The task that ended as ``state`` landed ``commit``: raise ``task_landed``, its own
        review, if due, the one ``review-rounds`` finds, queue the review of the batch it
        finished, if it did, and count it toward the ``commits`` triggers, those that fire
        reviewing the codebase at it. The task isn't looked up: it may have been removed since it
        finished, and its work is reviewed all the same."""
        job = self.planner.landed(state, commit)
        # Counted before the commit's own review is queued, which would hold a due super review;
        # one due is taken at once, so the count saved as that review is queued has its commit.
        fired = self._count(state)
        whole = self._super_review(commit) if fired else None
        await self._land(state, commit, job)
        if whole is not None:
            await self._queue_whole(fired, *whole)
        await self._hooks.changed()  # The count changed even if no review is due.

    def _count(self, state: TaskState) -> list[tuple[str, Trigger]]:
        """Count the landing of the task that ended as ``state`` toward the ``commits``
        triggers; returns those that fire, their counts starting again."""
        counted = self._triggers.count(state, self.planner.counts)
        self.planner.counts = counted.counts
        return counted.fires

    async def _land(self, state: TaskState, commit: str, job: ReviewJob | None) -> None:
        """Raise ``task_landed`` for the task that ended as ``state`` landing ``commit``, its
        own review due if ``job`` is that, and queue ``job`` if it's the review of the batch the
        landing finished."""
        await self.note_pause()
        own = job if job is not None and job.batch is None else None
        review = None if own is None else self._round(own)
        queued = await self._triggers.landed(state, commit, review)
        batch = await self._queue_job(job if own is None else None)
        await self._queue_ended((*queued, *batch))

    async def super_review(self) -> None:
        """Fire the super-review trigger by hand, its count starting again: a super review of
        the codebase as the user has it, the base's latest commit. (The coder's tree may lag
        behind it, or not exist yet.) Nothing is queued if the trigger is disabled."""
        fired = self._triggers.by_hand(SUPER_REVIEW)
        if not fired:
            return
        try:
            commit = await self._tree.base_commit()
        except TreeFailed as exc:  # Git failed; the review is skipped rather than the run.
            await self._publish(
                ErrorOccurred(code=GIT_FAILED, message=f"Super review skipped: {exc}")
            )
            return
        self.planner.counts = restart_counts(self.planner.counts, (SUPER_REVIEW,))
        await self._queue_whole(fired, *self._super_review(commit))

    def _super_review(self, commit: str) -> tuple[ReviewJob, str | None]:
        """A super review of the codebase at ``commit``, and the commit the last one reviewed,
        if any, after which it's told what landed."""
        since = self.planner.last_super_commit
        return self.planner.start_super_review(commit), since

    async def _queue_whole(
        self, fired: Sequence[tuple[str, Trigger]], job: ReviewJob, since: str | None
    ) -> None:
        """Have the triggers ``fired``, by name, queue their tasks, each reviewing ``job``, a
        super review, told what landed after ``since``, then run them."""
        await self.note_pause()
        values = await self._whole_values(job, since)
        queued = await self._triggers.fire_counted(fired, values, self._round(job))
        await self._queue_ended(queued)

    async def _whole_values(self, job: ReviewJob, since: str | None) -> Mapping[str, str]:
        """The variables of ``commits`` for ``job``, a super review told what landed after
        ``since``, as its task renders from them."""
        landed = await self._landed(job, since)
        pending = pending_titles(self._board.tasks)
        return review_values(job, "", self.planner.cap(job), pending, landed)

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
        self._causes.pop(task_id, None)
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
        """Review task ``task_id`` suggests a task; queue it unless it's refused, counting it on
        the review's span either way."""
        # Taken now: the review may end as the suggestion is queued. One ended counts no more.
        tally = self._tallies.get(task_id, ReviewTally())
        reply = await self._suggestion(task_id, title, prompt)
        tally.count(refused=reply.is_error)
        return reply

    async def _suggestion(self, task_id: str, title: str, prompt: str) -> Reply:
        """What review task ``task_id`` suggesting a task titled ``title`` comes to: queued, or
        refused."""
        batch = self._batches.get(task_id)
        if batch is None:
            return Reply("Only reviews orbit queued itself can suggest tasks.", is_error=True)
        roles = self._desk.agents.roles
        refusal = suggestion_refusal(roles) or self.planner.refusal(batch, title, self._board.tasks)
        if refusal is not None:
            return Reply(refusal, is_error=True)
        # The review is running, as its agent suggests, so it's on the board.
        skills = self._board.get(task_id).task.skills
        task = suggested_task(roles[CODER], title, prompt, skills)
        origin = self._jobs[task_id].origin

        def add(board: TaskBoard) -> str:
            new_id = board.add_suggestion(task, batch, self.planner.round_of(batch), origin)
            self.planner.added(batch, new_id, title)
            return new_id

        new_id = await self._tasks.change(add)
        return Reply(f"Queued as task {new_id}.")

    def cancel(self, task_id: str) -> None:
        """Stop review task ``task_id`` if it's the one running; the lane carries on."""
        self.runner.cancel(task_id)

    def kick(self) -> None:
        """Run the reviews waiting, if they aren't being run already, unless this orbit is
        read-only or the session is halted, skipping those on the runtime whose pause the work
        lane waits out; those queued meanwhile wait on the board."""
        self.runner.kick()

    async def stop(self) -> None:
        """Stop the reviews running, and wait for them to; they run again once kicked."""
        await self.runner.stop()

    async def close(self) -> None:
        """Stop the reviews running for good: a kick from now on runs none."""
        await self.runner.close()

    async def _queue(self, job: ReviewJob | None) -> None:
        """Queue ``job``'s review, the review of a batch, if there is one, and run it."""
        await self.note_pause()
        await self._queue_ended(await self._queue_job(job))

    async def _queue_job(self, job: ReviewJob | None) -> tuple[str, ...]:
        """Queue ``job``'s review, the review of a batch, if there is one, by the triggers
        ``review-rounds`` guards; returns the ids of the tasks queued."""
        if job is None:
            return ()
        return await self._triggers.reviewed(self._round(job))

    async def _queue_ended(self, queued: tuple[str, ...]) -> None:
        """Publish the review status and the lane's change once the tasks ``queued``, if any,
        were, then run them."""
        await self._publish_status()
        await self._hooks.changed()
        self._triggers.run(queued)

    def _kick_own(self, role: str) -> None:
        """Run the tasks waiting in the lane, if ``role``'s run there."""
        if role in self.roles:
            self.kick()

    async def _queue_quietly(self, job: ReviewJob | None) -> None:
        """Queue ``job``'s review, the review of a batch, if there is one, on the board without
        publishing it."""
        if job is not None:
            await self._triggers.reviewed_quietly(self._round(job))

    def _round(self, job: ReviewJob) -> ReviewRound:
        """``job``'s review as the ``review-rounds`` guard finds it due, booking the review task
        queued for it."""
        return ReviewRound(job, partial(self._book, job, current_span()))

    def _book(self, job: ReviewJob, cause: Span, task_id: str) -> None:
        """Record review task ``task_id``, queued for ``job`` while ``cause`` was the current
        span, which its span links to, opening the batch its suggestions join."""
        batch = self.planner.open_batch(job)
        self._jobs[task_id], self._batches[task_id] = job, batch
        self._causes[task_id] = cause

    async def _landed(self, job: ReviewJob, since: str | None) -> LandedSince:
        """What landed that ``job``'s reviewer, a super reviewer, is told as it's queued: the
        commits after ``since`` up to its commit, oldest first, with the files they changed and
        the sample of other code files it reads too. (A review of commits is told what landed by
        :meth:`_tell_landed`, as it starts.) What git can't list is left out: the review goes
        ahead without it, as it's only a hint."""
        commit = job.commits[0]
        landed = await self._since_super(since, commit)
        return replace(landed, sampled=await self._sample(commit, landed.files))

    async def _tell_landed(self, task_id: str, job: ReviewJob) -> None:
        """Have review ``task_id``'s task, of ``job``, list the commits on the base after the
        last one it reviews as they stand now, as it starts, in place of those it listed as it
        last ran. A super review was told what landed as it was queued."""
        if job.whole:
            return
        subjects = await self._landed_after(job.commits[-1])
        prompt = self._board.get(task_id).task.prompt
        told = tell_landed_since(prompt, job, subjects)
        if told != prompt:
            await self._tasks.change(lambda board: board.set_prompt(task_id, told))

    async def _landed_after(self, commit: str) -> tuple[str, ...]:
        """The subjects of the commits on the base after ``commit``, oldest first; none if git
        can't list them."""
        try:
            return tuple(await self._tree.landed_since(commit))
        except TreeFailed:
            return ()

    async def _since_super(self, since: str | None, commit: str) -> LandedSince:
        """The commits after ``since``, the last super review's commit, up to ``commit``, and
        the files they changed; nothing before the first super review, without ``since``, or if
        git can't list them."""
        if since is None:
            return LandedSince()
        try:
            subjects = await self._tree.landed_since(since, commit)
            files = await self._tree.changed_files(since, commit)
        except TreeFailed:
            return LandedSince()
        return LandedSince(tuple(subjects), tuple(files))

    async def _sample(self, commit: str, changed: Sequence[str]) -> tuple[str, ...]:
        """The code files tracked at ``commit`` that a super review of it reads besides those
        ``changed`` since the last, under the project's mapped paths, as many as the limits
        allow: those sampled longest ago, ties chosen at random, recorded as sampled by it.
        None, and the record as it was, if git can't list them."""
        try:
            tracked = await self._tree.tracked_files(commit)
        except TreeFailed:
            return ()
        mapped = [path for mapping in self._desk.agents.skill_mappings for path in mapping.paths]
        return self.planner.sample(tracked, mapped, changed, random.sample)

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

    def _next_review(self, tried: Collection[str]) -> str | None:
        """The next review, or other task of the lane's roles, to run, skipping those
        ``tried``."""
        return self._board.next_runnable(skip=tried, roles=self.runner.roles)

    def _spent(self) -> set[str]:
        """Reviews that ended, however they did: their batch is over or no longer filling, so
        running them again could suggest nothing."""
        return {t for t, b in self._batches.items() if not self.planner.filling(b)}

    def _is_review(self, task_id: str) -> bool:
        """Whether task ``task_id`` is a review: a review role's task, whether orbit queued it
        or the user added it. One taken off the board is a review the lane still follows up, as
        only a review is followed up once it ends."""
        state = self._board.find(task_id)
        return state is None or state.task.role in REVIEW_ROLES

    async def _review(self, task_id: str) -> None:
        """Run task ``task_id`` to its end: as a review if it's one, else as any lane runs its
        tasks. A review its runtime's limit or an outage stopped is left to run again, its batch
        as it was."""
        if not self._is_review(task_id):
            await self.runner.run_in_workspace(task_id)
            return
        # Only :meth:`cancel`, which the user asks for, stops a review on its own.
        run = partial(self._reviewed, task_id)
        stop = UserStop(REVIEW_STOPPED_BY_YOU)
        again = False
        with self._traced(task_id):
            try:  # Even a bug ends the review, or its batch would stay open.
                outcome = await run_task_to_end(self._tasks, task_id, run, stop, self._publish)
                again = await self.runner.ran(task_id, outcome)
            finally:
                if not again:
                    await self._ended(task_id)

    @contextmanager
    def _traced(self, task_id: str) -> Iterator[None]:
        """Trace review ``task_id`` over the block, its span current, tallying its
        suggestions."""
        tracing = self._hooks.tracing
        with tracing.review(self._board, task_id, self._queued(task_id)) as tally:
            self._tallies[task_id] = tally
            try:
                yield
            finally:
                del self._tallies[task_id]

    def _queued(self, task_id: str) -> Queued | None:
        """What orbit queued review ``task_id`` for; none if the user queued it. A review orbit
        queued only runs while its batch is open."""
        job = self._jobs.get(task_id)
        if job is None:
            return None
        batch = self._batches[task_id]
        return Queued(job, batch, self.planner.cap_of(batch), self._causes.get(task_id))

    async def _ended(self, task_id: str) -> None:
        """The review of ``task_id`` ended: forget what queued it and go on as :meth:`_after`
        says, unless the lane is stopping, which leaves its batches as they are and the review
        to run again, linked to what queued it."""
        if not cancelling():
            self._causes.pop(task_id, None)  # Its batch no longer fills, so it won't run again.
            await self._after(self._batches.get(task_id))

    async def _reviewed(self, task_id: str) -> Outcome:
        """How started review ``task_id`` ended, run as a task of its own that the user's stop
        cancels; anything unexpected escapes."""
        job = self._jobs.get(task_id)
        return await self.runner.alone(task_id, partial(self._run_reviewer, task_id, job))

    async def _run_reviewer(self, task_id: str, job: ReviewJob | None) -> Outcome:
        """Run the reviewer of ``job``, told what landed since as it starts, in a snapshot of the
        last commit it reviews; a review the user queued, of no job, works in its role's
        workspace, as :func:`run_in_role_workspace` says."""
        if job is None:
            return await run_in_role_workspace(task_id, self._tasks, self._desk)
        await self._tell_landed(task_id, job)
        run = AgentRun(task_id, self._tasks, None, self._desk).run
        async with snapshot_of(self._tree, job.commits[-1], self._publish) as root:
            return await run(None, root)

    async def _after(self, batch: str | None) -> None:
        """A review ended: review its batch if that's done, else run the suggestions it made;
        either links to the review's span, the current one."""
        if batch is None:
            return
        await self._queue(self.planner.review_ended(batch))
        if batch in self.planner.open_batches:
            await self._hooks.wake_work()
