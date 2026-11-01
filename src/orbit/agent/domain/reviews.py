"""When work is reviewed, and what a review may suggest.

A commit from one of the user's tasks (or the planner's) is reviewed on its own, in round 0. A
review's suggestions become a **batch** of tasks one round on; once every task in a batch has
finished, the batch's commits are reviewed together, and so on. A batch at the last round isn't
reviewed: review pauses until the user's next task lands, which bounds how much work reviews can
generate by themselves.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from orbit.agent.domain.board import WAITING
from orbit.agent.domain.review_sample import Choose, Draw, sample_files, with_sample
from orbit.protocol import (
    CONFIGURATOR,
    MANUAL_ORIGIN,
    PLANNER_ORIGIN,
    REVIEW_ORIGIN,
    REVIEW_ROLES,
    REVIEWER,
    SUPER_REVIEW,
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
# Commits landed since the reviewed one named in a review task, so the reviewer can check its
# findings against them; the rest are counted.
LANDED_LIMIT = 40
# Files the commits since the last super review changed named in a super review's task; the rest
# are counted.
FILES_LIMIT = 100
# Headings of a review task's lists.
_PENDING_HEADING = "Tasks already queued (don't suggest these):"
_LANDED_HEADING = "Landed since (check your findings against these):"


@dataclass(frozen=True)
class SuperReviewCount:
    """The repository's count towards its next super review: ``since_super`` commits counted
    since the last, and ``last_super_commit``, the commit that one reviewed (none before the
    first); with the ``super_reviews`` it has had, and the number of the one that last
    ``sampled`` each file, by path."""

    since_super: int = 0
    last_super_commit: str | None = None
    super_reviews: int = 0
    sampled: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class LandedSince:
    """What landed that a review's task lists: the ``subjects`` of the commits, oldest first,
    and, for a super review, the paths of the ``files`` they changed, with the paths of the
    other code files it reads too, ``sampled``."""

    subjects: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    sampled: tuple[str, ...] = ()


_NOTHING_LANDED = LandedSince()


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
        return SUPER_REVIEW if self.whole else REVIEW_ORIGIN

    @property
    def role(self) -> str:
        """The role of the review's task."""
        return SUPER_REVIEWER if self.whole else REVIEWER


@dataclass(frozen=True)
class ReviewTemplate:
    """What a review task of commits is rendered from: its trigger's ``template`` and the
    ``values`` it was queued with, the trigger's name among them, kept so that it's rendered
    again, told what landed since, as it starts (see :func:`tell_landed_since`)."""

    template: str
    values: Mapping[str, str]


@dataclass(frozen=True)
class ReviewLimits:
    """How much reviews may generate: ``rounds`` is the last round reviewed by itself, and
    ``suggestions`` the tasks one review may suggest. A super review, due as its trigger says,
    may suggest ``super_suggestions`` tasks, and reads a sample of ``super_sample`` code files
    besides those changed since the last."""

    rounds: int = 3
    suggestions: int = 5
    super_suggestions: int = 10
    super_sample: int = 10


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
    every suggestion it accepted, ``suggested``. The counts towards the ``commits`` triggers
    aren't in it: the super review's is kept per repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    paused: bool = False
    next_batch: int = 1
    batches: dict[str, BatchSnapshot] = {}
    suggested: frozenset[str] = frozenset()


class LaneSnapshot(BaseModel):
    """A :class:`ReviewLane`'s state for one session: its ``planner``'s, and for each review
    task it queued, the ``jobs`` it reviews and the ``batches`` it suggests into, with the
    ``templates`` each review of commits is rendered from, while it may still run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    planner: PlannerSnapshot
    jobs: dict[str, ReviewJob] = {}
    batches: dict[str, str] = {}
    templates: dict[str, ReviewTemplate] = {}


class ReviewPlanner:
    """Tracks batches and decides when to review, within ``limits``: only the reviews of the
    ``reviewers``, the review roles enabled, fall due. It keeps the landings counted toward
    each ``commits`` trigger, which decide when a super review is."""

    def __init__(self, limits: ReviewLimits, reviewers: Collection[str] = REVIEW_ROLES) -> None:
        self.limits = limits
        self._reviewers = frozenset(reviewers)
        self.paused = False
        # The landings counted toward each commits trigger since it last fired, by name.
        self.counts: Mapping[str, int] = {}
        self.last_super_commit: str | None = None  # The commit it reviewed; none before one.
        self.super_reviews = 0  # The super reviews the repository has had.
        # The number of the super review that last sampled each file, by path.
        self.sampled: Mapping[str, int] = {}
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
    def super_count(self) -> SuperReviewCount:
        """The count towards the next super review, the super-review trigger's, with the super
        reviews so far and the files they sampled, kept per repository rather than in
        snapshots."""
        since = self.counts.get(SUPER_REVIEW, 0)
        return SuperReviewCount(since, self.last_super_commit, self.super_reviews, self.sampled)

    @super_count.setter
    def super_count(self, count: SuperReviewCount) -> None:
        self.counts = {**self.counts, SUPER_REVIEW: count.since_super}
        self.last_super_commit = count.last_super_commit
        self.super_reviews, self.sampled = count.super_reviews, count.sampled

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

    def start_super_review(self, commit: str) -> ReviewJob:
        """A super review of the codebase at ``commit``, one more for the repository, which the
        next is told what landed after."""
        self.last_super_commit = commit
        self.super_reviews += 1
        return ReviewJob(commits=(commit,), round=0, batch=None, whole=True)

    def sample(
        self,
        tracked: Collection[str],
        mapped_paths: Collection[str],
        changed: Collection[str],
        choose: Choose,
    ) -> tuple[str, ...]:
        """The code files of the ``tracked`` ones that the super review queued last reads
        besides those ``changed`` since the one before, within ``mapped_paths``, as many as the
        limits allow, as :func:`sample_files` draws them with ``choose``. They're recorded as
        sampled by that review, and the files no longer tracked forgotten."""
        draw = Draw(self.limits.super_sample, choose)
        sample = sample_files(tracked, mapped_paths, changed, self.sampled, draw)
        self.sampled = with_sample(self.sampled, sample, tracked, self.super_reviews)
        return sample

    def landed(self, state: TaskState, commit: str) -> ReviewJob | None:
        """Task ``state`` landed ``commit``; returns the review that's due, if any: none while
        the reviewer is disabled."""
        if state.batch is None:
            if state.origin not in (MANUAL_ORIGIN, PLANNER_ORIGIN):
                return None
            self.paused = False
            return commit_review(state.id, commit) if REVIEWER in self._reviewers else None
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
        """The review of batch ``batch_id`` that's due, if any; a batch whose tasks have all
        finished closes, reviewed or not."""
        batch = self._open[batch_id]
        if batch.filling or any(task not in batch.finished for task in batch.tasks):
            return None
        del self._open[batch_id]
        return self._batch_review(batch_id, batch)

    def _batch_review(self, batch_id: str, batch: _Batch) -> ReviewJob | None:
        """The review of ``batch``, ``batch_id``, which just closed: due if it landed commits
        and the reviewer is enabled, unless it's at the last round, which pauses review."""
        if not batch.commits or REVIEWER not in self._reviewers:
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


def commit_review(task_id: str, commit: str) -> ReviewJob:
    """The review of ``commit`` on its own, which task ``task_id`` landed, as one of the user's
    or the planner's commits is reviewed."""
    return ReviewJob(commits=(commit,), round=0, batch=None, reviewed=(task_id,))


def _forget_finish(batch: _Batch, task_id: str) -> None:
    """Forget that task ``task_id`` of ``batch`` finished, and whether it ended without a commit."""
    batch.finished.discard(task_id)
    if task_id in batch.missing:
        batch.missing.remove(task_id)


def _first_line(text: str) -> str:
    """The first line of ``text``, stripped: a task's title."""
    return text.strip().split("\n", 1)[0].strip()


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


def review_skills(job: ReviewJob, tasks: Sequence[TaskState]) -> tuple[str, ...]:
    """The skills ``job``'s review task names: those of the tasks it reviews, in task order,
    each once, but for any no longer among ``tasks``. None for a super review, which reviews no
    particular task."""
    found = {state.id: state.task.skills for state in tasks}
    return tuple(dict.fromkeys(name for task in job.reviewed for name in found.get(task, ())))


def pause_notice(rounds: int) -> str:
    """The notice that review paused after ``rounds`` rounds of review-suggested work."""
    return (
        f"Review paused after {rounds} round(s) of review-suggested work;"
        " it resumes when one of your tasks lands."
    )


def review_prompt(
    job: ReviewJob,
    diff: str,
    cap: int,
    pending: Sequence[str],
    landed_since: LandedSince = _NOTHING_LANDED,
) -> str:
    """The task for the reviewer of ``job``, whose commits' messages and diffs are ``diff``,
    allowed ``cap`` suggestions, told the tasks titled ``pending`` are queued already and that
    the commits ``landed_since`` landed: after the last one it reviews or, for a super review,
    since the last super review, with the files they changed and those it samples.

    Its first line titles it. It holds only the facts: how to review is the role's instructions.
    """
    parts = [_review_title(job), *_missing(job), f"You may make at most {cap} suggestions."]
    parts += _pending(pending)
    if job.whole:
        sampled = _sampled(landed_since.sampled)
        return "\n\n".join([*parts, *_since_super(landed_since), *sampled])
    return "\n\n".join([*parts, *_landed(landed_since.subjects), _trimmed(diff)])


def review_values(
    job: ReviewJob,
    diff: str,
    cap: int,
    pending: Sequence[str],
    landed_since: LandedSince = _NOTHING_LANDED,
) -> dict[str, str]:
    """The variables a trigger's template renders ``job``'s review task from, as
    :func:`review_prompt` writes it from the same ``diff``, ``cap``, ``pending`` and
    ``landed_since``, each list as its block or, with nothing in it, empty: for a super review,
    those of ``commits``; for a review of commits, those of ``task_landed`` but ``{task}`` and
    ``{task_skills}``, which come with the landing. ``{trigger}`` is the caller's.

    ``{commit}`` is the reviewed commit, the last of a batch's, abbreviated, and ``{title}`` the
    review task's first line.
    """
    shared = {
        "commit": job.commits[-1][:7],
        "cap": str(cap),
        "queued": _paragraphs(_pending(pending)),
    }
    if job.whole:
        return shared | {
            "since_commits": _paragraphs(_since_commits(landed_since)),
            "changed_files": _paragraphs(_changed_files(landed_since)),
            "sample": _paragraphs(_sampled(landed_since.sampled)),
        }
    return shared | {
        "title": _review_title(job),
        "missing": _paragraphs(_missing(job)),
        "landed_since": _paragraphs(_landed(landed_since.subjects)),
        "diff": _trimmed(diff),
    }


def _paragraphs(parts: Sequence[str]) -> str:
    """``parts`` as paragraphs, apart by a blank line; empty when there are none."""
    return "\n\n".join(parts)


def _missing(job: ReviewJob) -> list[str]:
    """The parts of ``job``'s review task saying which of its batch's tasks ended without
    landing a commit, one each."""
    return [f"Task {task} of that review ended without landing a commit." for task in job.missing]


def _pending(pending: Sequence[str]) -> list[str]:
    """The part of a review's task listing the titles of the tasks ``pending``, the first
    :data:`PENDING_LIMIT` named and the rest counted; none when there are none."""
    if not pending:
        return []
    return [_capped_list(_PENDING_HEADING, pending, PENDING_LIMIT)]


def tell_landed_since(values: Mapping[str, str], subjects: Sequence[str]) -> dict[str, str]:
    """``values``, those a review of commits is rendered from, telling it of the commits whose
    ``subjects`` landed since in place of those they told of: ``{landed_since}`` lists them
    under its heading, or is empty when none did."""
    return {**values, "landed_since": _paragraphs(_landed(subjects))}


def _landed(subjects: Sequence[str]) -> list[str]:
    """The part of a review's task listing the commits whose ``subjects`` landed since the last
    one it reviews, the first :data:`LANDED_LIMIT` named and the rest counted; none when none
    did."""
    if not subjects:
        return []
    return [_capped_list(_LANDED_HEADING, subjects, LANDED_LIMIT)]


def _since_super(landed: LandedSince) -> list[str]:
    """The parts of a super review's task listing the commits ``landed`` since the last super
    review and the files they changed (see :func:`_since_commits` and :func:`_changed_files`)."""
    return [*_since_commits(landed), *_changed_files(landed)]


def _since_commits(landed: LandedSince) -> list[str]:
    """The part of a super review's task listing the commits ``landed`` since the last super
    review, every one; none when no commit landed."""
    if not landed.subjects:
        return []
    return ["\n".join(["Commits since the last super review:", *_items(landed.subjects)])]


def _changed_files(landed: LandedSince) -> list[str]:
    """The part of a super review's task listing the files the commits ``landed`` since the last
    super review changed, sorted, the first :data:`FILES_LIMIT` named and the rest counted; none
    when no commit landed or they changed none."""
    if not landed.subjects or not landed.files:
        return []
    return [_capped_list("Files they changed:", sorted(landed.files), FILES_LIMIT)]


def _sampled(paths: Sequence[str]) -> list[str]:
    """The part of a super review's task listing the ``paths`` it samples, as they are; none
    when there are none."""
    if not paths:
        return []
    return ["\n".join(["Sampled files (read these too):", *_items(paths)])]


def _trimmed(diff: str) -> str:
    """``diff``, its end trimmed past :data:`DIFF_LIMIT` characters."""
    if len(diff) > DIFF_LIMIT:
        return diff[:DIFF_LIMIT] + "\n… (diff trimmed)"
    return diff


def _review_title(job: ReviewJob) -> str:
    """The first line of ``job``'s review task."""
    if job.whole:
        return f"Super review of the codebase at {job.commits[0][:7]}"
    if job.batch is None:
        return f"Review commit {job.commits[0][:7]}"
    return f"Review the {len(job.commits)} commits made for review {job.batch}"


def _capped_list(heading: str, items: Sequence[str], limit: int) -> str:
    """``items`` listed under ``heading``: the first ``limit`` named, the rest counted."""
    lines = _items(items[:limit])
    if len(items) > limit:
        lines.append(f"… and {len(items) - limit} more")
    return "\n".join([heading, *lines])


def _items(items: Sequence[str]) -> list[str]:
    """``items`` as the lines of a list, one each."""
    return [f"- {item}" for item in items]
