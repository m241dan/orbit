"""The rule by which a task's update changes the task list, which a server and its frontends both
follow: the task with its id takes its place, and the rest stay as they are."""

from collections.abc import Sequence

from orbit.protocol.models import TaskState


def replace_task(tasks: Sequence[TaskState], task: TaskState) -> tuple[TaskState, ...]:
    """``tasks`` with the one whose id is ``task``'s replaced by ``task``, in its place; as they
    are when none has its id."""
    return tuple(task if state.id == task.id else state for state in tasks)
