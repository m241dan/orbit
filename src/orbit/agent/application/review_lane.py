"""The review lane: reviews of landed work, run one at a time beside the work lane.

Each review runs in a read-only snapshot of the commit it reviews, on the :class:`LaneRunner` of
the review roles' lane, which runs the tasks of every role in it, the others as any lane runs its
tasks. Its suggestions become tasks at the end of the queue, which the work lane then runs; a task
of another role booked as a review, run in this lane or the work lane, ends its batch as a review
does once it ends. The tasks the triggers queue for one review are booked into it, sharing one
batch, which fills until they have all ended; once every one of a finding role, granted
``report_finding``, has ended, the lane raises ``reviews_finished`` with the findings they
reported, and the tasks queued on it join the review, which then fills until they end too.
Those tasks are its merge passes, which must decide every finding, as
:mod:`~orbit.agent.domain.merge_pass` says; a review keeps its findings while its batch is open,
so the tasks filed for them mark them fixed as they land, or given up as they give up.
:class:`ReviewPlanner` decides when a review is due and stops reviews generating work forever.
It's the ``review-rounds`` guard: the lane raises each landing as ``task_landed``, and a review
of commits is queued by the triggers it guards, from their templates. It counts each landing
toward the ``commits`` triggers too, and a super review is queued by those that fire, or by the
super-review trigger fired by hand; those of them that run a named set run it instead.
"""

import random
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Literal

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
from orbit.agent.application.task_trace import FINDINGS_FIXED, FINDINGS_GIVEN_UP
from orbit.agent.application.tasks import Changed, Tasks
from orbit.agent.application.triggers import ReviewRound, TriggerEngine, TriggerHooks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.endings import REVIEW_STOPPED_BY_YOU
from orbit.agent.domain.merge_pass import (
    NOT_A_MERGE_PASS,
    NOT_MERGING,
    Dismissal,
    ReviewSuggestion,
    TaskEnding,
    decision_refusal,
    ended_in,
    filed_in,
    filing_refusal,
    outcome_counts,
    undecided_refusal,
    with_dismissed,
    with_filed,
    with_task_ended,
)
from orbit.agent.domain.reviews import (
    NOT_BOOKED_FOR_FINDINGS,
    FindingReport,
    LandedSince,
    LaneSnapshot,
    ReviewGroup,
    ReviewJob,
    ReviewPlanner,
    ReviewTemplate,
    Severity,
    fan_in_due,
    finding_refusal,
    finished_fields,
    finished_values,
    pause_notice,
    pending_titles,
    review_over,
    review_values,
    tell_landed_since,
    with_finding,
    with_lens,
    with_task,
)
from orbit.agent.domain.role_lanes import review_lane, task_lanes
from orbit.agent.domain.suggestions import suggested_task, suggestion_refusal
from orbit.agent.domain.tools import REPORT_FINDING
from orbit.agent.domain.triggers import (
    REVIEWS_FINISHED,
    Trigger,
    TriggerEvent,
    render_template,
    restart_counts,
)
from orbit.agent.domain.usage_limits import RuntimeStop
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

_ENDED_FINDINGS: Mapping[TaskEnding, str] = {
    "fixed": FINDINGS_FIXED,
    "given_up": FINDINGS_GIVEN_UP,
}
"""The attribute of a filed task's span counting its findings, by what its end made of them."""

Publishing = Literal["published", "quiet"]
"""How what a review's end queues is shown: published and run, or, as a session opens, only put
on the board, for its snapshot to show."""


class _Booking:
    """Where the tasks queued for one review of ``job`` are booked, while ``cause`` was the
    current span: into ``batch``, the batch the first of them opened, once one has, or that
    the review opened already."""

    def __init__(self, job: ReviewJob, cause: Span, batch: str | None = None) -> None:
        self.job = job
        self.cause = cause
        self.batch = batch


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
        # Review task id -> what its prompt is rendered from, for a review of commits that may
        # still run.
        self._templates: dict[str, ReviewTemplate] = {}
        # Review task id -> the span current as it was queued, this time the session was open,
        # until it ends.
        self._causes: dict[str, Span] = {}
        self._tallies: dict[str, ReviewTally] = {}  # Review task id -> its suggestions, as it runs.
        # Batch -> the review behind it, if it keeps a group, while the batch is open.
        self._groups: dict[str, ReviewGroup] = {}
        # Task id -> the severity a merge pass filed it at, while its findings are kept.
        self._severities: dict[str, Severity] = {}
        self._fanning: set[str] = set()  # Batches whose reviews_finished is being raised.
        self._triggers = hooks.triggers or TriggerEngine(desk, tasks, TriggerHooks(self._kick_own))
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
            planner=self.planner.snapshot(),
            jobs=dict(self._jobs),
            batches=dict(self._batches),
            templates=dict(self._templates),
            groups=dict(self._groups),
            severities=dict(self._severities),
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
        self._templates = dict(snapshot.templates)
        self._groups = dict(snapshot.groups)
        self._severities = dict(snapshot.severities)
        self._causes.clear()  # The session's task ids are reused by the next.
        self._status = self.status  # The switch's snapshot shows the restored status,
        self._paused_noted = False  # but a pause is news.

    async def end_interrupted(self, interrupted: Collection[str]) -> None:
        """End the reviews in ``interrupted``, which were stopped by a crash, as a cancelled
        review ends, never to run again: a review of their batch that's due is queued, for the
        next run, as are the tasks of ``reviews_finished`` for a review whose lenses have all
        ended now. The board and the review status aren't published: the switch's snapshot,
        which follows, shows them, and :meth:`note_pause` then notes a pause."""
        for task_id in interrupted:
            self._templates.pop(task_id, None)
            batch = self._batches.get(task_id)
            if batch is not None and batch in self.planner.open_batches:
                await self._end_quietly(task_id, batch)
                await self._hooks.changed()
        self._status = self.status

    async def _end_quietly(self, task_id: str, batch: str) -> None:
        """End task ``task_id``, booked into the review behind open ``batch``, as a crash
        stopped it, queuing what that leads to without publishing it."""
        group = self._groups.get(batch)
        if group is None or not self.planner.filling(batch):
            await self._queue_quietly(self.planner.review_ended(batch))
            return
        await self._settle(batch, ended_in(group, task_id), "quiet")

    async def landed(self, state: TaskState, commit: str) -> None:
        """The task that ended as ``state`` landed ``commit``: raise ``task_landed``, its own
        review, if due, the one ``review-rounds`` finds, queue the review of the batch it
        finished, if it did, and count it toward the ``commits`` triggers, those that fire
        reviewing the codebase at it, or running the set they name; then end the batch it
        suggested into, if the lane booked it, as :meth:`_ended` says. The findings filed in it,
        or in a task it fixes, are fixed. The task isn't looked up: it may have been removed
        since it finished, and its work is reviewed all the same."""
        self._end_filed(state, "fixed")
        job = self.planner.landed(state, commit)
        self._prune()
        # Counted before the commit's own review is queued, which would hold a due super review;
        # one due is taken at once, so the count saved as that review is queued has its commit.
        fired = self._triggers.fire_checks(self._count(state))
        whole = self._super_review(commit) if fired else None
        await self._land(state, commit, job)
        if whole is not None:
            await self._queue_whole(fired, *whole)
        await self._ended(state.id)
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

    async def ended(self, state: TaskState, stop: RuntimeStop | None = None) -> None:
        """The task that ended as ``state`` finished without landing anything, or was stopped
        by ``stop``, its runtime's usage limit or an outage, which leaves it to run again. The
        batch it suggested into, if the lane booked it, ends as :meth:`_ended` says, unless it's
        left to run again. The findings filed in a task that gave up, or in a task it fixes,
        are given up; those of a task that ended otherwise stay filed. The task isn't looked
        up: it may have been removed since it finished."""
        if stop is None and state.status == "gave_up":
            self._end_filed(state, "given_up")
        job = self.planner.ended(state)
        self._prune()
        await self._queue(job)
        if stop is None:
            await self._ended(state.id)

    def _end_filed(self, state: TaskState, ending: TaskEnding) -> None:
        """Mark the findings filed in the task that ended as ``state``, or in a task it fixes,
        as ``ending`` says, counting them on the span current, the task's."""
        fixed = self._board.list_fixed(state)
        count = sum(len(filed_in(group, fixed)) for group in self._groups.values())
        if not count:
            return
        self._groups = {
            batch: with_task_ended(group, fixed, ending) for batch, group in self._groups.items()
        }
        current_span().set_attributes({_ENDED_FINDINGS[ending]: count})

    def _prune(self) -> None:
        """Forget the groups of the batches that closed, and the severities of the tasks whose
        findings they kept."""
        open_batches = self.planner.open_batches
        self._groups = {b: g for b, g in self._groups.items() if b in open_batches}
        filed = self._filed_tasks()
        self._severities = {t: s for t, s in self._severities.items() if t in filed}

    def _filed_tasks(self) -> set[str | None]:
        """The tasks the findings of the groups kept were filed in."""
        return {finding.task for group in self._groups.values() for finding in group.findings}

    async def removed(self, task_id: str, state: TaskState) -> None:
        """Task ``task_id``, which was ``state``, was taken off the board. A review's batch
        ends as if it suggested nothing, or, for a task of a review that keeps a group, the task
        counts as ended; any other task leaves its batch."""
        batch = self._batches.pop(task_id, None)
        if batch is None:
            job = self.planner.removed(state)
            self._prune()
            await self._queue(job)
            return
        del self._jobs[task_id]
        self._causes.pop(task_id, None)
        self._templates.pop(task_id, None)
        group = self._groups.get(batch)
        if group is not None and self.planner.filling(batch):
            await self._settle(batch, ended_in(group, task_id), "published")
            return
        open_batch = batch in self.planner.open_batches
        await self._queue(self.planner.review_ended(batch) if open_batch else None)

    def edited(self, state: TaskState) -> None:
        """A task was edited back to pending, as ``state``, so its batch waits for it again. It
        isn't a review the lane booked, which can't be edited, so the lane keeps no template for
        it. The task isn't looked up: it may have been removed since it was edited."""
        self.planner.reopened(state)

    def joined(self, task_id: str) -> None:
        """Task ``task_id`` was added to fix another; if that one is in a batch, so is it."""
        batch = self._board.get(task_id).batch
        if batch is not None and batch in self.planner.open_batches:
            self.planner.added(batch, task_id)

    async def suggest(self, task_id: str, suggestion: ReviewSuggestion) -> Reply:
        """Review task ``task_id`` makes ``suggestion``; queue it unless it's refused, from the
        review's own origin, its trigger's name, counting it on the review's span either way.
        A merge pass's suggestion files the findings it names."""
        # Taken now: the review may end as the suggestion is queued. One ended counts no more.
        tally = self._tallies.get(task_id, ReviewTally())
        reply = await self._suggestion(task_id, suggestion)
        tally.count(refused=reply.is_error)
        return reply

    async def _suggestion(self, task_id: str, suggestion: ReviewSuggestion) -> Reply:
        """What review task ``task_id`` making ``suggestion`` comes to: queued, or refused."""
        batch = self._batches.get(task_id)
        if batch is None:
            return Reply("Only reviews orbit queued itself can suggest tasks.", is_error=True)
        roles = self._desk.agents.roles
        title = suggestion.title
        refusal = (
            suggestion_refusal(roles)
            or self._filing_refusal(task_id, batch, suggestion.findings)
            or self.planner.refusal(batch, title, self._board.tasks)
        )
        if refusal is not None:
            return Reply(refusal, is_error=True)
        # The review is running, as its agent suggests, so it's on the board. Its suggestions
        # take its origin, the trigger that queued it, whatever its job is.
        review = self._board.get(task_id)
        task = suggested_task(roles[CODER], title, suggestion.prompt, review.task.skills)

        def add(board: TaskBoard) -> str:
            new_id = board.add_suggestion(task, batch, self.planner.round_of(batch), review.origin)
            self.planner.added(batch, new_id, title)
            self._file(batch, suggestion, new_id)
            return new_id

        new_id = await self._tasks.change(add)
        return Reply(f"Queued as task {new_id}.")

    def _filing_refusal(self, task_id: str, batch: str, findings: Sequence[str]) -> str | None:
        """Why review task ``task_id``, booked into the review behind ``batch``, can't file
        ``findings``: a merge pass must name some it may decide, and another review none;
        ``None`` if it can."""
        group = self._groups.get(batch)
        if group is None or task_id not in group.merges:
            return NOT_A_MERGE_PASS if findings else None
        return filing_refusal(group, findings)

    def _file(self, batch: str, suggestion: ReviewSuggestion, task_id: str) -> None:
        """Record the findings ``suggestion`` names, if any, of the review behind ``batch``, as
        filed in task ``task_id``, keeping the severity it was filed at."""
        if not suggestion.findings:
            return
        self._groups[batch] = with_filed(self._groups[batch], suggestion.findings, task_id)
        self._severities[task_id] = suggestion.severity

    async def dismiss(self, task_id: str, dismissed: Dismissal) -> Reply:
        """Merge pass ``task_id`` dismisses a finding of its review, unless it's refused: from a
        task that isn't a merge pass still running, or of a finding it can't decide."""
        batch = self._merging_in(task_id)
        if batch is None:
            return Reply(NOT_MERGING, is_error=True)
        group = self._groups[batch]
        refusal = decision_refusal(group, dismissed.finding)
        if refusal is not None:
            return Reply(refusal, is_error=True)
        self._groups[batch] = with_dismissed(group, dismissed)
        await self._hooks.changed()
        return Reply(f"Dismissed {dismissed.finding}.")

    def completion_refusal(self, task_id: str) -> str | None:
        """Why task ``task_id`` can't complete yet: it's a merge pass whose review has findings
        undecided; ``None`` if it can."""
        batch = self._merging_in(task_id)
        return None if batch is None else undecided_refusal(self._groups[batch])

    def _merging_in(self, task_id: str) -> str | None:
        """The batch of the review whose merge pass task ``task_id`` is, if it hasn't ended
        there; none otherwise."""
        batch = self._batches.get(task_id)
        group = None if batch is None else self._groups.get(batch)
        merging = group is not None and task_id in group.merges and task_id not in group.ended
        return batch if merging else None

    async def report(self, task_id: str, report: FindingReport) -> Reply:
        """Task ``task_id`` reports a finding; record it against the review it's booked into,
        unless it's refused: from a task booked into no review, or no longer reviewing, or as
        :func:`~orbit.agent.domain.reviews.finding_refusal` says."""
        batch = self._reviewing_in(task_id)
        if batch is None:
            return Reply(NOT_BOOKED_FOR_FINDINGS, is_error=True)
        group = self._groups[batch]
        reporter = self._board.get(task_id).task.role
        limit = self.planner.limits.lens_findings
        role = self._desk.agents.roles[reporter]
        refusal = finding_refusal(group, reporter, role, report, limit)
        if refusal is not None:
            return Reply(refusal, is_error=True)
        self._groups[batch], finding = with_finding(group, reporter, report)
        tally = self._tallies.get(task_id)
        if tally is not None:
            tally.found()
        await self._hooks.changed()
        return Reply(f"Recorded as {finding.id}.")

    def _reviewing_in(self, task_id: str) -> str | None:
        """The batch of the review keeping a group that task ``task_id``, on the board, is booked
        into and hasn't ended in; none if there's no such review."""
        batch = self._batches.get(task_id)
        group = None if batch is None else self._groups.get(batch)
        reviewing = group is not None and task_id not in group.ended
        return batch if reviewing and self._board.find(task_id) is not None else None

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
        """``job``'s review as the ``review-rounds`` guard finds it due, booking each task
        queued for it into it."""
        return ReviewRound(job, partial(self._book, _Booking(job, current_span())))

    def _book(self, booking: _Booking, task_id: str, template: ReviewTemplate) -> None:
        """Book task ``task_id``, queued as a review, whatever its role, into the review
        ``booking`` says, the first task opening its batch, as :meth:`_record` says, keeping it
        in the review's group, if it keeps one: once it has a second task, or one of a finding
        role."""
        if booking.batch is None:
            booking.batch = self.planner.open_batch(booking.job)
        batch = booking.batch
        self._record(task_id, batch, booking, template)
        lens = self._is_lens(task_id)
        group = self._groups.get(batch) or self._new_group(batch, booking.job, task_id)
        if group is not None:
            self._groups[batch] = with_lens(group, task_id) if lens else with_task(group, task_id)

    def _new_group(self, batch: str, job: ReviewJob, task_id: str) -> ReviewGroup | None:
        """The group the review of ``job`` behind ``batch``, keeping none yet, keeps now that
        task ``task_id`` joined it, if it's a finding role's or the review's second; none
        otherwise."""
        keeps = self._is_lens(task_id) or len(self._members(batch)) > 1
        return ReviewGroup(job) if keeps else None

    def _record(
        self, task_id: str, batch: str, booking: _Booking, template: ReviewTemplate
    ) -> None:
        """Record task ``task_id``, booked into the review ``booking`` says, behind ``batch``,
        which its suggestions join, its span linking to the span current as the review fell
        due, and, for a review of commits, the ``template`` its prompt was rendered from. A
        super review, told what landed as it was queued, isn't rendered again. The triggers
        queued its task as a review (see :data:`~orbit.protocol.QueuedAs`), so it can't be
        edited, and holds back ``suggestions-pending`` while it waits."""
        self._jobs[task_id], self._batches[task_id] = booking.job, batch
        self._causes[task_id] = booking.cause
        if not booking.job.whole:
            self._templates[task_id] = template

    def _is_lens(self, task_id: str) -> bool:
        """Whether task ``task_id``, just queued, is a finding role's: one granted
        ``report_finding``."""
        role = self._desk.agents.roles.get(self._board.get(task_id).task.role)
        return role is not None and REPORT_FINDING in role.granted_tools

    def _members(self, batch: str) -> tuple[str, ...]:
        """The tasks booked into the review behind ``batch`` that weren't taken off the board."""
        return tuple(task_id for task_id, booked in self._batches.items() if booked == batch)

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
        last one it reviews as they stand now, as it starts, rendering it again from its
        template, which lists them where it says, in place of those it listed as it last ran.
        One with no template kept keeps its prompt: a super review, told what landed as it was
        queued, or one an older orbit saved."""
        template = self._templates.get(task_id)
        if template is None:
            return
        subjects = await self._landed_after(job.commits[-1])
        told = render_template(template.template, tell_landed_since(template.values, subjects))
        if told != self._board.get(task_id).task.prompt:
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
        """Reviews that ended, however they did: their batch is over or no longer filling, or
        they ended in a review that keeps a group, so running them again could suggest
        nothing."""
        return {t for t, b in self._batches.items() if self._ended_in(t, b)}

    def _ended_in(self, task_id: str, batch: str) -> bool:
        """Whether task ``task_id``, booked into the review behind ``batch``, has ended there."""
        group = self._groups.get(batch)
        ended = group is not None and task_id in group.ended
        return ended or not self.planner.filling(batch)

    def _is_review(self, task_id: str) -> bool:
        """Whether task ``task_id`` is a review: a review role's task, whether orbit queued it
        or the user added it. One taken off the board is a review the lane still follows up. A
        task of another role isn't, even one the lane booked as a review, such as a ``commits``
        trigger's: it runs as any task of its lane does, this lane's or the work lane's, and its
        batch ends as it ends, however it does, as a review's does (see :meth:`_ended`), unless
        its runtime's limit or an outage leaves it to run again."""
        state = self._board.find(task_id)
        return state is None or state.task.role in REVIEW_ROLES

    async def _review(self, task_id: str) -> None:
        """Run task ``task_id`` to its end: as a review if it's one, else as any lane runs its
        tasks, traced as a review if it's a lens booked into one; either way, it then ends, as
        :meth:`_run_and_end` says."""
        if not self._is_review(task_id):
            run = partial(self.runner.run_in_workspace, task_id)
            with self._lens_traced(task_id):
                await self._run_and_end(task_id, run)
            return
        with self._traced(task_id):
            await self._run_and_end(task_id, partial(self._run_review, task_id))

    def _lens_traced(self, task_id: str) -> AbstractContextManager[None]:
        """What traces task ``task_id``, on the board, as it runs: a review's span if it's a
        finding role's booked into a review, else nothing."""
        lens = task_id in self._jobs and self._is_lens(task_id)
        return self._traced(task_id) if lens else nullcontext()

    async def _run_and_end(self, task_id: str, run: Callable[[], Awaitable[bool]]) -> None:
        """Run task ``task_id`` to its end with ``run``, which returns whether it's left to run
        again, as one its runtime's limit or an outage stopped is, its batch as it was; any
        other then ends, as :meth:`_ended` says."""
        again = False
        try:  # Even a bug ends the task, or the batch the lane booked it into would stay open.
            again = await run()
        finally:
            if not again:
                await self._ended(task_id)

    async def _run_review(self, task_id: str) -> bool:
        """Run review ``task_id`` to its end; returns whether it's left to run again, as
        :meth:`LaneRunner.ran` says."""
        # Only :meth:`cancel`, which the user asks for, stops a review on its own.
        run = partial(self._reviewed, task_id)
        stop = UserStop(REVIEW_STOPPED_BY_YOU)
        outcome = await run_task_to_end(self._tasks, task_id, run, stop, self._publish)
        return await self.runner.ran(task_id, outcome)

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
        state = self._board.find(task_id)
        lens = state.task.role if state is not None and self._is_lens(task_id) else None
        return Queued(job, batch, self.planner.cap_of(batch), self._causes.get(task_id), lens)

    async def _ended(self, task_id: str) -> None:
        """Task ``task_id``, a review or another task, ended: forget what queued it, if the lane
        booked it, and go on as :meth:`_after` says, unless the lane is stopping, which leaves
        its batches as they are and the task to run again, linked to what queued it."""
        if not cancelling():
            # Its batch no longer fills, so this lane won't run it again.
            self._causes.pop(task_id, None)
            self._templates.pop(task_id, None)
            await self._after(task_id, self._batches.get(task_id))

    async def _reviewed(self, task_id: str) -> Outcome:
        """How started review ``task_id`` ended, run as a task of its own that the user's stop
        cancels; anything unexpected escapes."""
        job = self._jobs.get(task_id)
        return await self.runner.alone(partial(self._run_reviewer, task_id, job))

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

    async def _after(self, task_id: str, batch: str | None) -> None:
        """A review, or another task the lane booked, ``task_id``, ended: its review goes on as
        :meth:`_settle` says, or, keeping no group, ends with it, as :meth:`_close_review` says;
        either links to the task's span, the current one. A batch ends once: nothing is done for
        one that's over or no longer filling, as when a booked task the work lane runs again,
        once it failed, ends again."""
        if batch is None or not self.planner.filling(batch):
            return
        group = self._groups.get(batch)
        if group is None:
            await self._close_review(batch, "published")
            return
        ended = ended_in(group, task_id)
        self._count_outcomes(task_id, ended)
        await self._settle(batch, ended, "published")

    def _count_outcomes(self, task_id: str, group: ReviewGroup) -> None:
        """Count the outcomes of ``group``'s findings on task ``task_id``'s review span, if it's
        a merge pass of it traced as it runs."""
        tally = self._tallies.get(task_id)
        if tally is not None and task_id in group.merges:
            tally.outcomes = outcome_counts(group)

    async def _settle(self, batch: str, group: ReviewGroup, publishing: Publishing) -> None:
        """Keep ``group`` as the review behind ``batch``, one of its tasks having just ended,
        raise ``reviews_finished`` for it once its lenses have all ended, and end it once it's
        over, showing what that queues as ``publishing`` says."""
        self._groups[batch] = group
        if fan_in_due(group) and batch not in self._fanning:
            await self._fan_in(batch, group, publishing)
        settled = self._groups[batch]
        if batch not in self._fanning and review_over(settled, self._members(batch)):
            await self._close_review(batch, publishing)

    async def _fan_in(self, batch: str, group: ReviewGroup, publishing: Publishing) -> None:
        """Raise ``reviews_finished`` once for ``group``, the review behind ``batch``, each task
        a trigger queues on it booked into it, showing them as ``publishing`` says. It's marked
        raised first, and its review held open while it's raised."""
        self._groups[batch] = replace(group, stage="merging")
        self._fanning.add(batch)
        try:
            queued = await self._raise_finished(batch, group, publishing)
        finally:
            self._fanning.discard(batch)
        if publishing == "published":
            await self._queue_ended(queued)

    async def _raise_finished(
        self, batch: str, group: ReviewGroup, publishing: Publishing
    ) -> tuple[str, ...]:
        """Raise ``reviews_finished`` for ``group``, the review behind ``batch``, as
        ``publishing`` says; returns the ids of the tasks queued, if they're published."""
        job = group.job
        event = TriggerEvent(REVIEWS_FINISHED, finished_fields(job, group.findings))
        values = partial(self._finished_values, batch, group)
        review = ReviewRound(job, partial(self._book, _Booking(job, current_span(), batch)))
        if publishing == "quiet":
            await self._triggers.fan_in_quietly(event, values, review)
            return ()
        return await self._triggers.fan_in(event, values, review)

    async def _finished_values(self, batch: str, group: ReviewGroup) -> Mapping[str, str]:
        """The variables of ``reviews_finished`` for ``group``, the review behind ``batch``,
        told what landed after the last commit it reviews as it stands now."""
        job = group.job
        landed = await self._landed_after(job.commits[-1])
        pending = pending_titles(self._board.tasks)
        cap = self.planner.cap_of(batch)
        return finished_values(job, group.findings, cap, pending, landed)

    async def _close_review(self, batch: str, publishing: Publishing) -> None:
        """End the review behind ``batch``: its batch no longer fills, and is reviewed if
        that's due, showing what's queued as ``publishing`` says; else, published, the
        suggestions it made run."""
        job = self.planner.review_ended(batch)
        self._prune()  # The group is kept while the findings it filed may still land.
        if publishing == "quiet":
            await self._queue_quietly(job)
            return
        await self._queue(job)
        if batch in self.planner.open_batches:
            await self._hooks.wake_work()
