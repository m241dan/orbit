"""When work is reviewed, and what a review may suggest.

A commit from one of the user's tasks (or the planner's) is reviewed on its own, in round 0. A
review's suggestions become a **batch** of tasks one round on; once every task in a batch has
finished, the batch's commits are reviewed together, and so on. A batch at the last round isn't
reviewed: review pauses until the user's next task lands, which bounds how much work reviews can
generate by themselves.
"""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field

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


class ReviewPlanner:
    """Tracks batches and decides when to review, within ``limits``."""

    def __init__(self, limits: ReviewLimits) -> None:
        self.limits = limits
        self.paused = False
        self._since_super = 0
        self._ids = itertools.count(1)
        self._open: dict[str, _Batch] = {}
        self._rounds: dict[str, int] = {}

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
        batch = f"batch-{next(self._ids)}"
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
        self._since_super += 1
        if self._since_super < self.limits.super_every or self.paused:
            return False
        self._since_super = 0
        return True

    def super_review(self, commit: str) -> ReviewJob:
        """A super review of the codebase at ``commit``; the count starts again."""
        self._since_super = 0
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

    Its first line titles it.
    """
    if job.whole:
        return _super_review_prompt(job.commits[0], cap)
    if job.batch is None:
        title = f"Review commit {job.commits[0][:7]}"
    else:
        title = f"Review the {len(job.commits)} commits made for review {job.batch}"
    missing = "".join(
        f"\ntask {task} of that review ended without a commit." for task in job.missing
    )
    if len(diff) > DIFF_LIMIT:
        diff = diff[:DIFF_LIMIT] + "\n… (diff trimmed)"
    return (
        f"{title}\n{missing}\n"
        "Look for bugs, missing tests and unclear code. For each problem worth fixing, call"
        f" suggest_task with a short title and a prompt a coder can act on: at most {cap},"
        " most important first. Suggest nothing if it's fine. Then call complete_task with a"
        " one-line summary of your review.\n\n"
        f"{diff}"
    )


def _super_review_prompt(commit: str, cap: int) -> str:
    return (
        f"Super review of the codebase at {commit[:7]}\n\n"
        "Review the whole codebase, not only recent changes: look for duplication, structure"
        " that no longer fits, missing tests and unclear code. Read the files you need. For each"
        " improvement worth making, call suggest_task with a short title and a prompt a coder"
        f" can act on: at most {cap}, most important first. Then call complete_task with a"
        " one-line summary of your review."
    )
