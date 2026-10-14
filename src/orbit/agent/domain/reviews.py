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

from orbit.protocol import TaskState

# Statuses of tasks that are still to run or running, so a duplicate of one is refused.
UNFINISHED = frozenset({"pending", "running", "verifying"})
# Characters of diff quoted in a review task; a reviewer can read files for more.
DIFF_LIMIT = 30000


@dataclass(frozen=True)
class ReviewJob:
    """A review to run: of ``commits``, which are work at ``round``.

    ``batch`` is the batch whose work it is (``None`` for a commit of the user's task), and
    ``missing`` the batch's tasks that ended without a commit.
    """

    commits: tuple[str, ...]
    round: int
    batch: str | None
    missing: tuple[str, ...] = ()


@dataclass
class _Batch:
    round: int
    tasks: list[str] = field(default_factory=list)
    finished: set[str] = field(default_factory=set)
    commits: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


class ReviewPlanner:
    """Tracks batches and decides when to review; ``rounds`` is the last round reviewed by
    itself, and ``cap`` the tasks one review may suggest."""

    def __init__(self, rounds: int, cap: int) -> None:
        self.rounds = rounds
        self.cap = cap
        self.paused = False
        self._ids = itertools.count(1)
        self._open: dict[str, _Batch] = {}
        self._rounds: dict[str, int] = {}

    @property
    def open_batches(self) -> tuple[str, ...]:
        return tuple(self._open)

    def round_of(self, batch: str) -> int:
        return self._rounds[batch]

    def open_batch(self, job: ReviewJob) -> str:
        """Start the batch for ``job``'s suggestions; returns its id."""
        batch = f"batch-{next(self._ids)}"
        self._open[batch] = _Batch(round=job.round + 1)
        self._rounds[batch] = job.round + 1
        return batch

    def added(self, batch: str, task_id: str) -> None:
        """Task ``task_id`` joined ``batch``: a suggestion, or a fix of one."""
        self._open[batch].tasks.append(task_id)

    def close_if_empty(self, batch: str) -> None:
        """The review behind ``batch`` is over: if it suggested nothing, stop waiting on it."""
        if batch in self._open and not self._open[batch].tasks:
            del self._open[batch]

    def refusal(self, batch: str, title: str, queued: Sequence[TaskState]) -> str | None:
        """Why a suggestion titled ``title`` can't join ``batch``; ``None`` if it can."""
        if len(self._open[batch].tasks) >= self.cap:
            return f"This review has already suggested {self.cap} tasks, the most allowed."
        wanted = _normalized(title)
        for state in queued:
            if state.status in UNFINISHED and _normalized(state.task.prompt) == wanted:
                return f"Task {state.id} already covers that."
        return None

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
        if any(task not in batch.finished for task in batch.tasks):
            return None
        del self._open[batch_id]
        if not batch.commits:
            return None
        if batch.round >= self.rounds:
            self.paused = True
            return None
        return ReviewJob(tuple(batch.commits), batch.round, batch_id, tuple(batch.missing))


def _normalized(text: str) -> str:
    return " ".join(text.strip().split("\n", 1)[0].lower().split())


def review_prompt(job: ReviewJob, diff: str, cap: int) -> str:
    """The task for the reviewer of ``job``, whose commits' messages and diffs are ``diff``.

    Its first line titles it.
    """
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
