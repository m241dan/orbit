"""The task list and verification progress: plain state the session publishes as snapshots."""

import itertools
from collections.abc import Collection, Sequence

from orbit.protocol import StepState, StepStatus, TaskSpec, TaskState, TaskStatus

# Statuses a run leaves alone; any other task is worth (another) run.
SETTLED: frozenset[TaskStatus] = frozenset({"running", "verifying", "completed", "unverified"})


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

    def add(self, task: TaskSpec, after: str | None = None) -> str:
        """Add a pending ``task`` at the end, or right after task ``after``; returns its id."""
        state = TaskState(id=str(next(self._ids)), task=task)
        index = len(self._tasks) if after is None else self.position(after) + 1
        self._tasks.insert(index, state)
        return state.id

    def edit(self, task_id: str, task: TaskSpec) -> None:
        """Replace a task and make it pending again. Raises ``ValueError`` if it can't be."""
        if self.get(task_id).status in ("running", "verifying"):
            raise ValueError(f"Task {task_id!r} can't be edited while it runs.")
        self._tasks[self.position(task_id)] = TaskState(id=task_id, task=task)

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

    def next_runnable(self, skip: Collection[str] = ()) -> str | None:
        """The first task that isn't settled or in ``skip``, if any."""
        for state in self._tasks:
            if state.status not in SETTLED and state.id not in skip:
                return state.id
        return None

    def reset_steps(self) -> None:
        self._steps = tuple(StepState(name=s.name) for s in self._steps)

    def set_step(self, step: int, status: StepStatus) -> None:
        steps = list(self._steps)
        steps[step] = steps[step].model_copy(update={"status": status})
        self._steps = tuple(steps)

    def cancel_running_steps(self) -> None:
        self._steps = tuple(
            s.model_copy(update={"status": "cancelled"}) if s.status == "running" else s
            for s in self._steps
        )
