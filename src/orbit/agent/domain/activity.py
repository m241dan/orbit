"""What runs in a session: the one definition of whether it's idle or running, and so whether
it may be switched for another, and of what connections are told is in progress."""

from dataclasses import dataclass

from orbit.protocol import Activity as Reported

TASKS_RUNNING = "Can't switch: tasks are running. Press ctrl+s to stop them."
REVIEW_RUNNING = "Can't switch: a review is running. Press c on it in the agents pane to stop it."
PLANNER_ANSWERING = "Can't switch: the planner is answering. Press ctrl+x to stop it."


@dataclass(frozen=True)
class Activity:
    """What's in progress in a session: a task run (``tasks``) on task ``task_id``, a
    ``review`` of the tasks ``reviewing``, a ``planner`` turn and the ``startup_check``. The
    task run's task and the reviewed tasks are none while they aren't known; a review of no
    particular task, such as a super review, reviews no tasks. The startup check runs on the
    base commit rather than in a session, so it leaves the session idle."""

    tasks: bool = False
    review: bool = False
    planner: bool = False
    startup_check: bool = False
    task_id: str | None = None
    reviewing: tuple[str, ...] | None = None

    @property
    def running(self) -> bool:
        """Whether anything but the startup check is in progress; the session is idle
        otherwise."""
        return switch_refusal(self) is not None


def switch_refusal(activity: Activity) -> str | None:
    """Why a switch is refused while ``activity`` is in progress, saying what runs first of a
    task run, a review and a planner turn; none when nothing runs."""
    refusals = (
        (activity.tasks, TASKS_RUNNING),
        (activity.review, REVIEW_RUNNING),
        (activity.planner, PLANNER_ANSWERING),
    )
    return next((refusal for runs, refusal in refusals if runs), None)


def reported(activity: Activity) -> tuple[Reported, ...]:
    """What's in progress in ``activity`` as connections are told it: a task run, a review, a
    planner turn and the startup check, in that order; a task run or a review once its tasks
    are known. A review is told once per task it reviews, in order, or once without a task if
    it reviews no particular one."""
    task_run = activity.tasks and activity.task_id is not None
    entries = (
        (task_run, (Reported(kind="task_run", task_id=activity.task_id),)),
        (activity.review, _reviews(activity.reviewing)),
        (activity.planner, (Reported(kind="planner"),)),
        (activity.startup_check, (Reported(kind="startup_check"),)),
    )
    return tuple(entry for runs, group in entries if runs for entry in group)


def _reviews(reviewing: tuple[str, ...] | None) -> tuple[Reported, ...]:
    """A review of the tasks ``reviewing`` as connections are told it: nothing while they
    aren't known, and one review without a task for a review of no particular one."""
    if reviewing is None:
        return ()
    if not reviewing:
        return (Reported(kind="review"),)
    return tuple(Reported(kind="review", task_id=task_id) for task_id in reviewing)
