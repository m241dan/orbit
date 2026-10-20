"""What runs in a session: the one definition of whether it's idle or running, and so whether
it may be switched for another."""

from dataclasses import dataclass

TASKS_RUNNING = "Can't switch: tasks are running. Press ctrl+s to stop them."
REVIEW_RUNNING = "Can't switch: a review is running. Press c on it in the agents pane to stop it."
PLANNER_ANSWERING = "Can't switch: the planner is answering. Press ctrl+x to stop it."
STARTUP_CHECKING = (
    "Can't switch: the startup check is running. Press c on it in the agents pane to stop it."
)


@dataclass(frozen=True)
class Activity:
    """What's in progress in a session: a task run (``tasks``), a ``review``, a ``planner``
    turn and the ``startup_check``."""

    tasks: bool = False
    review: bool = False
    planner: bool = False
    startup_check: bool = False

    @property
    def running(self) -> bool:
        """Whether anything is in progress; the session is idle otherwise."""
        return switch_refusal(self) is not None


def switch_refusal(activity: Activity) -> str | None:
    """Why a switch is refused while ``activity`` is in progress, saying what runs first of a
    task run, a review, a planner turn and the startup check; none when nothing runs."""
    refusals = (
        (activity.tasks, TASKS_RUNNING),
        (activity.review, REVIEW_RUNNING),
        (activity.planner, PLANNER_ANSWERING),
        (activity.startup_check, STARTUP_CHECKING),
    )
    return next((refusal for runs, refusal in refusals if runs), None)
