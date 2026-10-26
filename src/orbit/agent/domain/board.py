"""The task list: plain state the session publishes as snapshots."""

import itertools
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from orbit.agent.domain.endings import INTERRUPTED
from orbit.agent.domain.records import renumbered
from orbit.agent.domain.refusals import Refused
from orbit.protocol import (
    ACTIVE_STATUSES,
    BUSY,
    INVALID,
    PLANNER_ORIGIN,
    REVIEW_ORIGIN,
    UNKNOWN,
    TaskOrigin,
    TaskSpec,
    TaskState,
    TaskStatus,
    Usage,
    edit_refusal,
)

# Statuses of a task that's yet to finish: still to run, or running.
WAITING: frozenset[TaskStatus] = ACTIVE_STATUSES | {"pending"}
# Statuses a run leaves alone; any other task is worth (another) run.
SETTLED: frozenset[TaskStatus] = ACTIVE_STATUSES | {"draft", "completed", "unverified", "gave_up"}
# Statuses of a fix that stopped short, whose work is still in the tree: it runs before any other
# task puts that work aside.
CUT_SHORT: frozenset[TaskStatus] = frozenset({"error", "cancelled"})

# What clears a task's reason for ending badly.
_NO_REASON = {"reason": "", "reason_detail": ""}


def interrupted(tasks: Sequence[TaskState]) -> tuple[tuple[TaskState, ...], int]:
    """``tasks`` as a crash leaves them, and how many changed: those that were running are
    cancelled, :data:`INTERRUPTED`, so the next run picks them up again."""
    cancelled = {"status": "cancelled", "reason": INTERRUPTED, "reason_detail": ""}
    left = tuple(
        state.model_copy(update=cancelled) if state.status in ACTIVE_STATUSES else state
        for state in tasks
    )
    return left, sum(state.status in ACTIVE_STATUSES for state in tasks)


def _in_roles(role: str, roles: Collection[str] | None, exclude: Collection[str]) -> bool:
    return role not in exclude and (roles is None or role in roles)


def _cut_short(state: TaskState) -> bool:
    return state.fixes is not None and state.status in CUT_SHORT


class TaskBoard:
    """Tasks in run order, each with a stable id."""

    def __init__(self) -> None:
        self._tasks: list[TaskState] = []
        self._ids = itertools.count(1)

    @property
    def tasks(self) -> tuple[TaskState, ...]:
        return tuple(self._tasks)

    @property
    def pending(self) -> int:
        """How many tasks are pending, waiting to run."""
        return sum(state.status == "pending" for state in self.tasks)

    def restore(self, tasks: Sequence[TaskState]) -> None:
        """Replace the board's tasks with ``tasks``, in order; new ids carry on after the
        highest numeric one among them."""
        self._tasks = list(tasks)
        highest = max((int(state.id) for state in tasks if state.id.isdecimal()), default=0)
        self._ids = itertools.count(highest + 1)

    def add(self, task: TaskSpec, fixes: str | None = None) -> str:
        """Add a pending ``task`` at the end; returns its id.

        A task that ``fixes`` another goes right after it instead, and belongs where that
        one does: the same origin, review batch and round.
        """
        state = self._new(task, fixes=fixes)
        if fixes is None:
            self._tasks.append(state)
            return state.id
        fixed = self.get(fixes)
        inherited = {"origin": fixed.origin, "batch": fixed.batch, "round": fixed.round}
        self._tasks.insert(self.position(fixes) + 1, state.model_copy(update=inherited))
        return state.id

    def add_first(self, task: TaskSpec) -> str:
        """Add a pending ``task`` at the front, to run next; returns its id."""
        state = self._new(task)
        self._tasks.insert(0, state)
        return state.id

    def add_suggestion(
        self, task: TaskSpec, batch: str, round: int, origin: TaskOrigin = REVIEW_ORIGIN
    ) -> str:
        """Add ``task``, suggested by the review behind ``batch``, at the end; returns its id."""
        state = self._new(task, origin=origin, batch=batch, round=round)
        self._tasks.append(state)
        return state.id

    def edit(self, task_id: str, task: TaskSpec) -> None:
        """Replace a task and make it pending again, without a reason for how it last ended.
        Raises ``Refused`` if it can't be: while it runs, or for a review orbit queued, whose
        batch is spent."""
        current = self.get(task_id)
        refusal = edit_refusal(current)
        if refusal is not None:
            raise Refused(refusal.code, refusal.message)
        fresh = {"task": task, "status": "pending", "attempt": 0, "max_attempts": 0} | _NO_REASON
        self._tasks[self.position(task_id)] = current.model_copy(update=fresh)

    def move_records(self, moves: Mapping[str, str]) -> None:
        """Name each record ``moves`` maps by its new path in every task that hasn't completed,
        leaving its status as it is; a completed task's prompt is history."""
        for index, state in enumerate(self._tasks):
            prompt = renumbered(state.task.prompt, moves)
            if state.status != "completed" and prompt != state.task.prompt:
                task = state.task.model_copy(update={"prompt": prompt})
                self._tasks[index] = state.model_copy(update={"task": task})

    def get(self, task_id: str) -> TaskState:
        return self._tasks[self.position(task_id)]

    def position(self, task_id: str) -> int:
        """Where task ``task_id`` is in run order. Raises ``Refused`` if there's no such task."""
        for index, state in enumerate(self._tasks):
            if state.id == task_id:
                return index
        raise Refused(UNKNOWN, f"No task with id {task_id!r}.")

    def set_status(self, task_id: str, status: TaskStatus) -> None:
        """Set task ``task_id``'s ``status``; one running again loses its last reason."""
        update: dict[str, str] = {"status": status}
        if status == "running":
            update |= _NO_REASON
        self._update(task_id, update)

    def set_attempt(self, task_id: str, attempt: int, max_attempts: int) -> None:
        """Task ``task_id``'s agent has been asked for fix ``attempt`` of ``max_attempts``."""
        index = self.position(task_id)
        update = {"attempt": attempt, "max_attempts": max_attempts}
        self._tasks[index] = self._tasks[index].model_copy(update=update)

    def add_usage(self, task_id: str, usage: Usage) -> None:
        """Count ``usage``, what a turn run for task ``task_id`` cost, towards its total."""
        index = self.position(task_id)
        total = self._tasks[index].usage + usage
        self._tasks[index] = self._tasks[index].model_copy(update={"usage": total})

    def finish(self, task_id: str, status: TaskStatus, reason: str = "", detail: str = "") -> None:
        """Record how task ``task_id`` ended, and why: ``reason`` in a line, ``detail`` in full.
        A completed fix also completes what it fixed."""
        self._update(task_id, {"status": status, "reason": reason, "reason_detail": detail})
        if status == "completed":
            self.mark_fixed(task_id)

    def mark_fixed(self, task_id: str) -> None:
        """Task ``task_id`` passed verification, so the tasks it was fixing are completed too,
        without the reasons they failed.

        Walks up the chain of fixes, stopping at a task that's no longer on the board. Tasks
        that aren't unverified any more (say, edited since) stay as they are.
        """
        fixed = self.find(self.get(task_id).fixes)
        while fixed is not None:
            if fixed.status == "unverified":
                self._update(fixed.id, {"status": "completed"} | _NO_REASON)
            fixed = self.find(fixed.fixes)

    def find(self, task_id: str | None) -> TaskState | None:
        """Task ``task_id``, or None if there's no such task on the board."""
        return next((state for state in self._tasks if state.id == task_id), None)

    def next_runnable(
        self,
        skip: Collection[str] = (),
        roles: Collection[str] | None = None,
        exclude: Collection[str] = (),
        first: str | None = None,
    ) -> str | None:
        """The first task that isn't settled or in ``skip``, if any: only in ``roles`` if
        given, and never in ``exclude``. A fix that ended in error or was cancelled comes first,
        before another task puts its work aside; then task ``first``, if given, while it's one of
        them; then the board's order."""
        runnable = self._unsettled(skip, roles, exclude)
        cut_short = [state.id for state in runnable if _cut_short(state)]
        ids = [state.id for state in runnable]
        return next(iter(cut_short + [i for i in ids if i == first] + ids), None)

    def _unsettled(
        self, skip: Collection[str], roles: Collection[str] | None, exclude: Collection[str]
    ) -> list[TaskState]:
        """The tasks, in order, that aren't settled or in ``skip``: only in ``roles`` if given,
        and never in ``exclude``."""
        return [
            state
            for state in self._tasks
            if state.status not in SETTLED
            and state.id not in skip
            and _in_roles(state.task.role, roles, exclude)
        ]

    def has_waiting(self, role: str) -> bool:
        """Whether a task in ``role`` is still to run, or running."""
        return any(state.task.role == role and state.status in WAITING for state in self._tasks)

    def add_draft(self, task: TaskSpec, proposed_by: str | None) -> str:
        """Add ``task``, which role ``proposed_by`` proposed (none for orbit itself), at the end
        as a draft; returns its id."""
        state = self._new(task, status="draft", origin=PLANNER_ORIGIN, proposed_by=proposed_by)
        self._tasks.append(state)
        return state.id

    def accept(self, task_id: str) -> None:
        """Make draft ``task_id`` pending. Raises ``Refused`` if it isn't a draft."""
        if self.get(task_id).status != "draft":
            raise Refused(INVALID, f"Task {task_id!r} isn't a draft.")
        self.set_status(task_id, "pending")

    def remove(self, task_id: str) -> None:
        """Take ``task_id`` off the list. Raises ``Refused`` if it can't be.

        Its fixes take its place in the chain: they fix what it fixed, or become top-level
        tasks if it was one.
        """
        removed = self.get(task_id)
        if removed.status in ACTIVE_STATUSES:
            raise Refused(BUSY, f"Task {task_id!r} can't be removed while it runs.")
        del self._tasks[self.position(task_id)]
        self._tasks = [
            state.model_copy(update={"fixes": removed.fixes}) if state.fixes == task_id else state
            for state in self._tasks
        ]

    def append(self, state: TaskState) -> str:
        """Add ``state``, a task from another board, at the end as it is but with the next id;
        returns that id."""
        new_id = str(next(self._ids))
        self._tasks.append(state.model_copy(update={"id": new_id}))
        return new_id

    def add_review(self, task: TaskSpec) -> str:
        """Add a review orbit queued itself, at the end; returns its id."""
        state = self._new(task, origin=REVIEW_ORIGIN)
        self._tasks.append(state)
        return state.id

    def _update(self, task_id: str, update: dict[str, str]) -> None:
        """Change task ``task_id``'s fields as ``update`` says."""
        index = self.position(task_id)
        self._tasks[index] = self._tasks[index].model_copy(update=update)

    def _new(self, task: TaskSpec, **fields: Any) -> TaskState:
        """A new task state for ``task``, with the next id."""
        return TaskState(id=str(next(self._ids)), task=task, **fields)
