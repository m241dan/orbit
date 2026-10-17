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
from orbit.protocol import TaskState

# Characters of diff quoted in a review task; a reviewer can read files for more.
DIFF_LIMIT = 30000


@dataclass(frozen=True)
class ReviewJob:
    """A review to run: of ``commits``, which are work at ``round``.

    ``batch`` is the batch whose work it is (``None`` for a commit of the user's task), and
    ``missing`` the batch's tasks that ended without a commit. A ``whole`` review is a super
    review: of the whole codebase at its one commit.
    """

    commits: tuple[str, ...]
    round: int
    batch: str | None
    missing: tuple[str, ...] = ()
    whole: bool = False


@dataclass(frozen=True)
class ReviewLimits:
    """How much reviews may generate: ``rounds`` is the last round reviewed by itself, and
    ``suggestions`` the tasks one review may suggest. A super review is due every
    ``super_every`` commits, and may suggest ``super_suggestions`` tasks."""

    rounds: int = 3
    suggestions: int = 5
    super_every: int = 10
    super_suggestions: int = 10


@dataclass
class _Batch:
    round: int
    cap: int
    tasks: list[str] = field(default_factory=list)
    finished: set[str] = field(default_factory=set)
    commits: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    filling: bool = True  # Its review is still running, and may suggest more.


class BatchSnapshot(BaseModel):
    """An open batch as it stands: see :class:`ReviewPlanner`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    round: int
    cap: int
    tasks: tuple[str, ...] = ()
    finished: frozenset[str] = frozenset()
    commits: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    filling: bool = True


class PlannerSnapshot(BaseModel):
    """A :class:`ReviewPlanner`'s state for one session: whether review is ``paused``, the
    number of the ``next_batch``, the ``batches`` still open and every batch's round. The
    count towards a super review isn't in it: that's kept per repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    paused: bool = False
    next_batch: int = 1
    batches: dict[str, BatchSnapshot] = {}
    rounds: dict[str, int] = {}


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
        self._rounds: dict[str, int] = {}

    def snapshot(self) -> PlannerSnapshot:
        """The planner's state for this session, to :meth:`restore` later."""
        batches = {
            batch_id: BatchSnapshot(
                round=batch.round,
                cap=batch.cap,
                tasks=tuple(batch.tasks),
                finished=frozenset(batch.finished),
                commits=tuple(batch.commits),
                missing=tuple(batch.missing),
                filling=batch.filling,
            )
            for batch_id, batch in self._open.items()
        }
        return PlannerSnapshot(
            paused=self.paused,
            next_batch=self._next_batch,
            batches=batches,
            rounds=dict(self._rounds),
        )

    def restore(self, snapshot: PlannerSnapshot) -> None:
        """Replace the planner's state with ``snapshot``'s; the super review count is kept."""
        self.paused = snapshot.paused
        self._next_batch = snapshot.next_batch
        self._open = {
            batch_id: _Batch(
                round=batch.round,
                cap=batch.cap,
                tasks=list(batch.tasks),
                finished=set(batch.finished),
                commits=list(batch.commits),
                missing=list(batch.missing),
                filling=batch.filling,
            )
            for batch_id, batch in snapshot.batches.items()
        }
        self._rounds = dict(snapshot.rounds)

    @property
    def open_batches(self) -> tuple[str, ...]:
        return tuple(self._open)

    def round_of(self, batch: str) -> int:
        return self._rounds[batch]

    def cap(self, job: ReviewJob) -> int:
        """The tasks ``job``'s review may suggest."""
        return self.limits.super_suggestions if job.whole else self.limits.suggestions

    def open_batch(self, job: ReviewJob) -> str:
        """Start the batch for ``job``'s suggestions; returns its id."""
        batch = f"batch-{self._next_batch}"
        self._next_batch += 1
        self._open[batch] = _Batch(round=job.round + 1, cap=self.cap(job))
        self._rounds[batch] = job.round + 1
        return batch

    def added(self, batch: str, task_id: str) -> None:
        """Task ``task_id`` joined ``batch``: a suggestion, or a fix of one."""
        self._open[batch].tasks.append(task_id)

    def review_ended(self, batch_id: str) -> ReviewJob | None:
        """The review behind ``batch_id`` is over; returns the review of the batch, if that's
        due now. A batch it suggested nothing into is closed."""
        batch = self._open[batch_id]
        batch.filling = False
        if not batch.tasks:
            del self._open[batch_id]
            return None
        return self._due(batch_id)

    def refusal(self, batch: str, title: str, queued: Sequence[TaskState]) -> str | None:
        """Why a suggestion titled ``title`` can't join ``batch``; ``None`` if it can."""
        if batch not in self._open:
            return "This review's suggestions are closed: its batch is over."
        cap = self._open[batch].cap
        if len(self._open[batch].tasks) >= cap:
            return f"This review has already suggested {cap} tasks, the most allowed."
        wanted = _normalized(title)
        for state in queued:
            if state.status in WAITING and _normalized(state.task.prompt) == wanted:
                return f"Task {state.id} already covers that."
        return None

    def count(self) -> bool:
        """A commit landed; returns whether a super review is due now. One that falls due
        while review is paused waits until it resumes."""
        self.since_super += 1
        if self.since_super < self.limits.super_every or self.paused:
            return False
        self.since_super = 0
        return True

    def super_review(self, commit: str) -> ReviewJob:
        """A super review of the codebase at ``commit``; the count starts again."""
        self.since_super = 0
        return ReviewJob(commits=(commit,), round=0, batch=None, whole=True)

    def landed(self, state: TaskState, commit: str) -> ReviewJob | None:
        """Task ``state`` landed ``commit``; returns the review that's due, if any."""
        if state.batch is None:
            if state.origin not in ("manual", "planner"):
                return None
            self.paused = False
            return ReviewJob(commits=(commit,), round=0, batch=None)
        batch = self._open.get(state.batch)
        if batch is None:
            return None
        batch.commits.append(commit)
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
        batch.finished.discard(state.id)
        return self._due(batch_id)

    def reopened(self, state: TaskState) -> None:
        """Task ``state`` was edited back to pending: if its batch is open, it hasn't finished."""
        batch = self._open.get(state.batch) if state.batch is not None else None
        if batch is None:
            return
        batch.finished.discard(state.id)
        if state.id in batch.missing:
            batch.missing.remove(state.id)

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
        return ReviewJob(tuple(batch.commits), batch.round, batch_id, tuple(batch.missing))


def _normalized(text: str) -> str:
    return " ".join(text.strip().split("\n", 1)[0].lower().split())


def review_prompt(job: ReviewJob, diff: str, cap: int) -> str:
    """The task for the reviewer of ``job``, whose commits' messages and diffs are ``diff``.

    Its first line titles it. It holds only the facts: how to review is the role's instructions.
    """
    limit = f"You may make at most {cap} suggestions."
    if job.whole:
        return f"Super review of the codebase at {job.commits[0][:7]}\n\n{limit}"
    if job.batch is None:
        title = f"Review commit {job.commits[0][:7]}"
    else:
        title = f"Review the {len(job.commits)} commits made for review {job.batch}"
    parts = [title]
    parts += [f"Task {task} of that review ended without a commit." for task in job.missing]
    if len(diff) > DIFF_LIMIT:
        diff = diff[:DIFF_LIMIT] + "\n… (diff trimmed)"
    return "\n\n".join([*parts, limit, diff])
