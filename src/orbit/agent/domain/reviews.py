"""When work is reviewed, and what a review may suggest.

A commit from one of the user's tasks (or the planner's) is reviewed on its own, in round 0. A
review's suggestions become a **batch** of tasks one round on; once every task in a batch has
finished, the batch's commits are reviewed together, and so on. A batch at the last round isn't
reviewed: review pauses until the user's next task lands, which bounds how much work reviews can
generate by themselves.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from orbit.agent.domain.board import WAITING
from orbit.protocol import (
    CONFIGURATOR,
    MANUAL_ORIGIN,
    PLANNER_ORIGIN,
    REVIEW_ORIGIN,
    REVIEW_ROLES,
    REVIEWER,
    SUPER_REVIEW_ORIGIN,
    SUPER_REVIEWER,
    TaskOrigin,
    TaskState,
)

# Characters of diff quoted in a review task; a reviewer can read files for more.
DIFF_LIMIT = 30000
# Roles of the tasks orbit queues for itself, which no reviewer would suggest.
_ORBITS_ROLES = REVIEW_ROLES | {CONFIGURATOR}
# Queued tasks named in a review task, so the reviewer doesn't suggest them; the rest are counted.
PENDING_LIMIT = 40


@dataclass(frozen=True)
class ReviewJob:
    """A review to run: of ``commits``, which are work at ``round``.

    ``batch`` is the batch whose work it is (``None`` for a commit of the user's task),
    ``missing`` the batch's tasks that ended without landing a commit, and ``reviewed`` the
    tasks that landed the commits, in task order. A ``whole`` review is a super review: of the
    whole codebase at its one commit, reviewing no particular task.
    """

    commits: tuple[str, ...]
    round: int
    batch: str | None
    missing: tuple[str, ...] = ()
    whole: bool = False
    reviewed: tuple[str, ...] = ()

    @property
    def origin(self) -> TaskOrigin:
        """The origin of the tasks the review suggests."""
        return SUPER_REVIEW_ORIGIN if self.whole else REVIEW_ORIGIN

    @property
    def role(self) -> str:
        """The role of the review's task."""
        return SUPER_REVIEWER if self.whole else REVIEWER


@dataclass(frozen=True)
class ReviewLimits:
    """How much reviews may generate: ``rounds`` is the last round reviewed by itself, and
    ``suggestions`` the tasks one review may suggest. A super review is due every
    ``super_every`` commits, and may suggest ``super_suggestions`` tasks."""

    rounds: int = 3
    suggestions: int = 5
    super_every: int = 10
    super_suggestions: int = 10


class BatchSnapshot(BaseModel):
    """An open batch as it stands: see :class:`ReviewPlanner`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    round: int
    cap: int
    tasks: tuple[str, ...] = ()
    finished: frozenset[str] = frozenset()
    commits: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    landed: tuple[str, ...] = ()
    filling: bool = True


@dataclass
class _Batch:
    round: int
    cap: int
    tasks: list[str] = field(default_factory=list)
    finished: set[str] = field(default_factory=set)
    commits: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    landed: list[str] = field(default_factory=list)
    filling: bool = True  # Its review is still running, and may suggest more.

    def reviewed(self) -> tuple[str, ...]:
        """The batch's tasks that landed commits, in task order."""
        return tuple(task for task in self.tasks if task in self.landed)

    def frozen(self) -> BatchSnapshot:
        """The batch as it stands, to save."""
        return BatchSnapshot(
            round=self.round,
            cap=self.cap,
            tasks=tuple(self.tasks),
            finished=frozenset(self.finished),
            commits=tuple(self.commits),
            missing=tuple(self.missing),
            landed=tuple(self.landed),
            filling=self.filling,
        )

    @classmethod
    def thawed(cls, snapshot: BatchSnapshot) -> _Batch:
        """The batch ``snapshot`` saved."""
        return cls(
            round=snapshot.round,
            cap=snapshot.cap,
            tasks=list(snapshot.tasks),
            finished=set(snapshot.finished),
            commits=list(snapshot.commits),
            missing=list(snapshot.missing),
            landed=list(snapshot.landed),
            filling=snapshot.filling,
        )


class PlannerSnapshot(BaseModel):
    """A :class:`ReviewPlanner`'s state for one session: whether review is ``paused``, the
    number of the ``next_batch``, the ``batches`` still open and the normalised titles of
    every suggestion it accepted, ``suggested``. The count towards a super review isn't in
    it: that's kept per repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    paused: bool = False
    next_batch: int = 1
    batches: dict[str, BatchSnapshot] = {}
    suggested: frozenset[str] = frozenset()


class LaneSnapshot(BaseModel):
    """A :class:`ReviewLane`'s state for one session: its ``planner``'s, and for each review
    task it queued, the ``jobs`` it reviews and the ``batches`` it suggests into."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    planner: PlannerSnapshot
    jobs: dict[str, ReviewJob] = {}
    batches: dict[str, str] = {}


class ReviewPlanner:
    """Tracks batches and decides when to review, within ``limits``."""

    def __init__(self, limits: ReviewLimits) -> None:
        self.limits = limits
        self.paused = False
        self.since_super = 0  # Commits since the last super review.
        self._next_batch = 1
        self._open: dict[str, _Batch] = {}
        self._suggested: set[str] = set()  # Normalised titles of the suggestions accepted.

    def snapshot(self) -> PlannerSnapshot:
        """The planner's state for this session, to :meth:`restore` later."""
        batches = {batch_id: batch.frozen() for batch_id, batch in self._open.items()}
        return PlannerSnapshot(
            paused=self.paused,
            next_batch=self._next_batch,
            batches=batches,
            suggested=frozenset(self._suggested),
        )

    def restore(self, snapshot: PlannerSnapshot) -> None:
        """Replace the planner's state with ``snapshot``'s; the super review count is kept."""
        self.paused = snapshot.paused
        self._next_batch = snapshot.next_batch
        self._open = {
            batch_id: _Batch.thawed(batch) for batch_id, batch in snapshot.batches.items()
        }
        self._suggested = set(snapshot.suggested)

    @property
    def open_batches(self) -> tuple[str, ...]:
        return tuple(self._open)

    def filling(self, batch: str) -> bool:
        """Whether ``batch`` is open and its review may still suggest into it."""
        return batch in self._open and self._open[batch].filling

    def round_of(self, batch: str) -> int:
        """The round of open ``batch``'s tasks; it's only asked about open batches."""
        return self._open[batch].round

    def cap(self, job: ReviewJob) -> int:
        """The tasks ``job``'s review may suggest."""
        return self.limits.super_suggestions if job.whole else self.limits.suggestions

    def cap_of(self, batch: str) -> int:
        """The tasks the review behind open ``batch`` may suggest, as capped when it opened;
        it's only asked about open batches."""
        return self._open[batch].cap

    def open_batch(self, job: ReviewJob) -> str:
        """Start the batch for ``job``'s suggestions; returns its id."""
        batch = f"batch-{self._next_batch}"
        self._next_batch += 1
        self._open[batch] = _Batch(round=job.round + 1, cap=self.cap(job))
        return batch

    def added(self, batch: str, task_id: str, title: str | None = None) -> None:
        """Task ``task_id`` joined ``batch``: a suggestion titled ``title``, which no later
        review of the session may suggest again, or (with no title) a fix of one."""
        self._open[batch].tasks.append(task_id)
        if title is not None:
            self._suggested.add(_normalized(title))

    def review_ended(self, batch_id: str) -> ReviewJob | None:
        """The review behind ``batch_id`` is over; returns the review of the batch, if that's
        due now. A batch it suggested nothing into is closed."""
        batch = self._open[batch_id]
        batch.filling = False
        if not batch.tasks:
            del self._open[batch_id]
            return None
        return self._due(batch_id)

    def refusal(self, batch: str, title: str, tasks: Sequence[TaskState]) -> str | None:
        """Why a suggestion titled ``title`` can't join ``batch``; ``None`` if it can.

        A title that, once normalised, is the first line of a task in ``tasks`` (the board's
        tasks, whatever their role and status) or of a suggestion the planner accepted this
        session is a repeat. Only an exact match after normalisation counts: a reworded repeat
        is accepted.
        """
        closed = self._closed(batch)
        if closed is not None:
            return closed
        cap = self._open[batch].cap
        if len(self._open[batch].tasks) >= cap:
            return f"This review has already suggested {cap} tasks, the most allowed."
        return self._repeat(title, tasks)

    def _repeat(self, title: str, tasks: Sequence[TaskState]) -> str | None:
        """Why a suggestion titled ``title`` repeats one of ``tasks`` or an earlier suggestion;
        ``None`` if it's new."""
        wanted = _normalized(title)
        for state in tasks:
            if _normalized(state.task.prompt) == wanted:
                return f"Task {state.id} already covers that."
        if wanted in self._suggested:
            return "An earlier review already suggested that."
        return None

    def _closed(self, batch: str) -> str | None:
        """Why ``batch`` takes no more suggestions; ``None`` if its review is still filling it."""
        if batch not in self._open:
            return "This review's suggestions are closed: its batch is over."
        if not self._open[batch].filling:
            return "This review's suggestions are closed: its review is over."
        return None

    def count(self, state: TaskState, tasks: Sequence[TaskState]) -> bool:
        """Task ``state`` landed a commit, with ``tasks`` on the board; returns whether a super
        review is due now. Only the user's and the planner's commits count: a suggested task's
        doesn't. One that falls due waits, as it does while review is paused, while a suggested
        task or a review (any task with origin review or super_review) is pending or running,
        and comes with the first of the user's commits to find none."""
        if not _users_work(state):
            return False
        self.since_super += 1
        if self.since_super < self.limits.super_every or self._holds_super_review(tasks):
            return False
        self.since_super = 0
        return True

    def _holds_super_review(self, tasks: Sequence[TaskState]) -> bool:
        """Whether a super review that's due waits: review is paused, or a task that reviews
        suggested, or a review itself, is in ``tasks`` still to run or running."""
        return self.paused or any(
            task.origin in (REVIEW_ORIGIN, SUPER_REVIEW_ORIGIN) and task.status in WAITING
            for task in tasks
        )

    def super_review(self, commit: str) -> ReviewJob:
        """A super review of the codebase at ``commit``; the count starts again."""
        self.since_super = 0
        return ReviewJob(commits=(commit,), round=0, batch=None, whole=True)

    def landed(self, state: TaskState, commit: str) -> ReviewJob | None:
        """Task ``state`` landed ``commit``; returns the review that's due, if any."""
        if state.batch is None:
            if state.origin not in (MANUAL_ORIGIN, PLANNER_ORIGIN):
                return None
            self.paused = False
            return ReviewJob(commits=(commit,), round=0, batch=None, reviewed=(state.id,))
        batch = self._open.get(state.batch)
        if batch is None:
            return None
        batch.commits.append(commit)
        batch.landed.append(state.id)
        batch.finished.add(state.id)
        return self._due(state.batch)

    def ended(self, state: TaskState) -> ReviewJob | None:
        """Task ``state`` finished without landing anything; returns the review that's due."""
        batch_id = state.batch
        if batch_id is None or batch_id not in self._open:
            return None
        batch = self._open[batch_id]
        batch.finished.add(state.id)
        if state.status != "unverified":  # An unverified task's fix carries its work on.
            batch.missing.append(state.id)
        return self._due(batch_id)

    def removed(self, state: TaskState) -> ReviewJob | None:
        """Task ``state`` was taken off the board; returns the review that's due without it."""
        batch_id = state.batch
        if batch_id is None or batch_id not in self._open:
            return None
        batch = self._open[batch_id]
        batch.tasks.remove(state.id)
        _forget_finish(batch, state.id)
        return self._due(batch_id)

    def reopened(self, state: TaskState) -> None:
        """Task ``state`` was edited back to pending: if its batch is open, it hasn't finished."""
        batch = self._open.get(state.batch) if state.batch is not None else None
        if batch is None:
            return
        _forget_finish(batch, state.id)

    def _due(self, batch_id: str) -> ReviewJob | None:
        batch = self._open[batch_id]
        if batch.filling or any(task not in batch.finished for task in batch.tasks):
            return None
        del self._open[batch_id]
        if not batch.commits:
            return None
        if batch.round >= self.limits.rounds:
            self.paused = True
            return None
        return ReviewJob(
            tuple(batch.commits),
            batch.round,
            batch_id,
            tuple(batch.missing),
            reviewed=batch.reviewed(),
        )


def _forget_finish(batch: _Batch, task_id: str) -> None:
    """Forget that task ``task_id`` of ``batch`` finished, and whether it ended without a commit."""
    batch.finished.discard(task_id)
    if task_id in batch.missing:
        batch.missing.remove(task_id)


def _first_line(text: str) -> str:
    """The first line of ``text``, stripped: a task's title."""
    return text.strip().split("\n", 1)[0].strip()


def _users_work(state: TaskState) -> bool:
    """Whether ``state`` is one of the user's own tasks, or the planner's, outside any batch."""
    return state.batch is None and state.origin in (MANUAL_ORIGIN, PLANNER_ORIGIN)


def _normalized(text: str) -> str:
    return " ".join(_first_line(text).lower().split())


def pending_titles(tasks: Sequence[TaskState]) -> tuple[str, ...]:
    """The first lines of the ``tasks`` still to run or running that a review could repeat:
    not the reviews and configurator tasks orbit queues for itself."""
    return tuple(
        _first_line(state.task.prompt)
        for state in tasks
        if state.status in WAITING and state.task.role not in _ORBITS_ROLES
    )


def pause_notice(rounds: int) -> str:
    """The notice that review paused after ``rounds`` rounds of review-suggested work."""
    return (
        f"Review paused after {rounds} round(s) of review-suggested work;"
        " it resumes when one of your tasks lands."
    )


def review_prompt(job: ReviewJob, diff: str, cap: int, pending: Sequence[str]) -> str:
    """The task for the reviewer of ``job``, whose commits' messages and diffs are ``diff``,
    allowed ``cap`` suggestions and told the tasks titled ``pending`` are queued already.

    Its first line titles it. It holds only the facts: how to review is the role's instructions.
    """
    parts = [_review_title(job)]
    parts += [f"Task {task} of that review ended without landing a commit." for task in job.missing]
    parts.append(f"You may make at most {cap} suggestions.")
    if pending:
        parts.append(_pending_list(pending))
    if job.whole:
        return "\n\n".join(parts)
    if len(diff) > DIFF_LIMIT:
        diff = diff[:DIFF_LIMIT] + "\n… (diff trimmed)"
    return "\n\n".join([*parts, diff])


def _review_title(job: ReviewJob) -> str:
    """The first line of ``job``'s review task."""
    if job.whole:
        return f"Super review of the codebase at {job.commits[0][:7]}"
    if job.batch is None:
        return f"Review commit {job.commits[0][:7]}"
    return f"Review the {len(job.commits)} commits made for review {job.batch}"


def _pending_list(pending: Sequence[str]) -> str:
    """The tasks titled ``pending`` for a reviewer not to suggest: the first
    :data:`PENDING_LIMIT` named, the rest counted."""
    lines = [f"- {title}" for title in pending[:PENDING_LIMIT]]
    if len(pending) > PENDING_LIMIT:
        lines.append(f"… and {len(pending) - PENDING_LIMIT} more")
    return "\n".join(["Tasks already queued (don't suggest these):", *lines])
