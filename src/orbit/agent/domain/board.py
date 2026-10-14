"""The task list and verification progress: plain state the session publishes as snapshots."""

import itertools
from collections.abc import Collection, Sequence

from orbit.protocol import StepState, StepStatus, TaskSpec, TaskState, TaskStatus

# Statuses a run leaves alone; any other task is worth (another) run.
SETTLED: frozenset[TaskStatus] = frozenset(
    {"running", "verifying", "completed", "unverified", "gave_up"}
)


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

    def add_suggestion(self, task: TaskSpec, batch: str, round: int) -> str:
        """Add ``task``, suggested by the review behind ``batch``, at the end; returns its id."""
        state = TaskState(
            id=str(next(self._ids)), task=task, origin="review", batch=batch, round=round
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

    def next_runnable(self, skip: Collection[str] = (), role: str | None = None) -> str | None:
        """The first task that isn't settled or in ``skip``, if any; only ``role``'s if given."""
        for state in self._tasks:
            if state.status in SETTLED or state.id in skip:
                continue
            if role is None or state.task.role == role:
                return state.id
        return None

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
