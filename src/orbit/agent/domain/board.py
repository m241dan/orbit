"""The task list and verification progress: plain state the session publishes as snapshots."""

import itertools
from collections.abc import Collection, Sequence

from orbit.protocol import (
    StepState,
    StepStatus,
    TaskOrigin,
    TaskSpec,
    TaskState,
    TaskStatus,
    Usage,
)

# Statuses a run leaves alone; any other task is worth (another) run.
SETTLED: frozenset[TaskStatus] = frozenset(
    {"draft", "running", "verifying", "completed", "unverified", "gave_up"}
)


def _in_roles(role: str, roles: Collection[str] | None, exclude: Collection[str]) -> bool:
    return role not in exclude and (roles is None or role in roles)


class TaskBoard:
    """Tasks in run order, each with a stable id, plus the verification steps' statuses."""

    def __init__(self, step_names: Sequence[str] = ()) -> None:
        self._tasks: list[TaskState] = []
        self._ids = itertools.count(1)
        self._steps = tuple(StepState(name=name) for name in step_names)

    @property
    def tasks(self) -> tuple[TaskState, ...]:
        return tuple(self._tasks)

    @property
    def steps(self) -> tuple[StepState, ...]:
        return self._steps

    def add(self, task: TaskSpec, fixes: str | None = None) -> str:
        """Add a pending ``task`` at the end; returns its id.

        A task that ``fixes`` another goes right after it instead, and belongs where that
        one does: the same origin, review batch and round.
        """
        state = TaskState(id=str(next(self._ids)), task=task, fixes=fixes)
        if fixes is None:
            self._tasks.append(state)
            return state.id
        fixed = self.get(fixes)
        inherited = {"origin": fixed.origin, "batch": fixed.batch, "round": fixed.round}
        self._tasks.insert(self.position(fixes) + 1, state.model_copy(update=inherited))
        return state.id

    def add_first(self, task: TaskSpec) -> str:
        """Add a pending ``task`` at the front, to run next; returns its id."""
        state = TaskState(id=str(next(self._ids)), task=task)
        self._tasks.insert(0, state)
        return state.id

    def add_suggestion(
        self, task: TaskSpec, batch: str, round: int, origin: TaskOrigin = "review"
    ) -> str:
        """Add ``task``, suggested by the review behind ``batch``, at the end; returns its id."""
        state = TaskState(
            id=str(next(self._ids)), task=task, origin=origin, batch=batch, round=round
        )
        self._tasks.append(state)
        return state.id

    def edit(self, task_id: str, task: TaskSpec) -> None:
        """Replace a task and make it pending again. Raises ``ValueError`` if it can't be."""
        current = self.get(task_id)
        if current.status in ("running", "verifying"):
            raise ValueError(f"Task {task_id!r} can't be edited while it runs.")
        fresh = {"task": task, "status": "pending", "attempt": 0, "max_attempts": 0}
        self._tasks[self.position(task_id)] = current.model_copy(update=fresh)

    def get(self, task_id: str) -> TaskState:
        return self._tasks[self.position(task_id)]

    def position(self, task_id: str) -> int:
        for index, state in enumerate(self._tasks):
            if state.id == task_id:
                return index
        raise ValueError(f"No task with id {task_id!r}.")

    def set_status(self, task_id: str, status: TaskStatus) -> None:
        index = self.position(task_id)
        self._tasks[index] = self._tasks[index].model_copy(update={"status": status})

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

    def finish(self, task_id: str, status: TaskStatus) -> None:
        """Record how task ``task_id`` ended; a completed fix also completes what it fixed."""
        self.set_status(task_id, status)
        if status == "completed":
            self.mark_fixed(task_id)

    def mark_fixed(self, task_id: str) -> None:
        """Task ``task_id`` passed verification, so the tasks it was fixing are completed too.

        Walks up the chain of fixes. Tasks that aren't unverified any more (say, edited
        since) stay as they are.
        """
        fixed = self.get(task_id).fixes
        while fixed is not None:
            if self.get(fixed).status == "unverified":
                self.set_status(fixed, "completed")
            fixed = self.get(fixed).fixes

    def next_runnable(
        self,
        skip: Collection[str] = (),
        roles: Collection[str] | None = None,
        exclude: Collection[str] = (),
    ) -> str | None:
        """The first task that isn't settled or in ``skip``, if any: only in ``roles`` if
        given, and never in ``exclude``."""
        for state in self._tasks:
            unsettled = state.status not in SETTLED and state.id not in skip
            if unsettled and _in_roles(state.task.role, roles, exclude):
                return state.id
        return None

    def add_draft(self, task: TaskSpec) -> str:
        """Add ``task``, which the planner proposed, at the end as a draft; returns its id."""
        state = TaskState(id=str(next(self._ids)), task=task, status="draft", origin="planner")
        self._tasks.append(state)
        return state.id

    def accept(self, task_id: str) -> None:
        """Make draft ``task_id`` pending. Raises ``ValueError`` if it isn't a draft."""
        if self.get(task_id).status != "draft":
            raise ValueError(f"Task {task_id!r} isn't a draft.")
        self.set_status(task_id, "pending")

    def remove(self, task_id: str) -> None:
        """Take ``task_id`` off the list. Raises ``ValueError`` if it can't be."""
        if self.get(task_id).status in ("running", "verifying"):
            raise ValueError(f"Task {task_id!r} can't be removed while it runs.")
        del self._tasks[self.position(task_id)]

    def add_review(self, task: TaskSpec) -> str:
        """Add a review orbit queued itself, at the end; returns its id."""
        state = TaskState(id=str(next(self._ids)), task=task, origin="review")
        self._tasks.append(state)
        return state.id

    def set_steps(self, step_names: Sequence[str]) -> None:
        """Track ``step_names`` from now on, none of them run yet."""
        self._steps = tuple(StepState(name=name) for name in step_names)

    def reset_steps(self) -> None:
        self._steps = tuple(StepState(name=s.name) for s in self._steps)

    def skip_steps(self) -> None:
        self._steps = tuple(s.model_copy(update={"status": "skipped"}) for s in self._steps)

    def set_step(self, step: int, status: StepStatus) -> None:
        steps = list(self._steps)
        steps[step] = steps[step].model_copy(update={"status": status})
        self._steps = tuple(steps)

    def cancel_running_steps(self) -> None:
        self._steps = tuple(
            s.model_copy(update={"status": "cancelled"}) if s.status == "running" else s
            for s in self._steps
        )
