"""What runs in a session: the one definition of whether it's idle or running, and so whether
it may be switched for another, and of what connections are told is in progress."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto

from orbit.agent.domain.refusals import joined
from orbit.protocol import PLANNER
from orbit.protocol import Activity as Reported


class Running(Enum):
    """What may run in a session that keeps it from being idle, in the order it's told: a task
    run, a task in a lane of its own, a review and a conversational role's turn."""

    TASKS = auto()
    LANES = auto()
    REVIEW = auto()
    PLANNER = auto()


_DOING = {
    Running.TASKS: "tasks are running",
    Running.LANES: "a task is running in its own lane",
    Running.REVIEW: "a review is running",
}
"""What each kind of running work but a conversational role's turn is called when it keeps a
session busy."""

_TO_STOP = {
    Running.TASKS: "Press ctrl+s to stop them.",
    Running.LANES: "Press c on it in the agents pane to stop it.",
    Running.REVIEW: "Press c on it in the agents pane to stop it.",
}
"""How the user stops each kind of running work but a conversational role's turn."""


def answering(roles: Sequence[str]) -> str:
    """What the conversational roles ``roles`` are called while they answer: "the planner is
    answering", or "the planner and the architect are answering" for several."""
    verb = "is" if len(roles) == 1 else "are"
    return f"{joined([f'the {role}' for role in roles])} {verb} answering"


def _stopping(roles: Sequence[str]) -> str:
    """How the user stops the turns of the conversational roles ``roles``."""
    return f"Press ctrl+x to stop {'it' if len(roles) == 1 else 'them'}."


def _refusal(work: str, to_stop: str) -> str:
    """Why a switch is refused while ``work`` runs, which ``to_stop`` says how to stop."""
    return f"Can't switch: {work}. {to_stop}"


TASKS_RUNNING = _refusal(_DOING[Running.TASKS], _TO_STOP[Running.TASKS])
LANE_TASK_RUNNING = _refusal(_DOING[Running.LANES], _TO_STOP[Running.LANES])
REVIEW_RUNNING = _refusal(_DOING[Running.REVIEW], _TO_STOP[Running.REVIEW])
PLANNER_ANSWERING = _refusal(answering((PLANNER,)), _stopping((PLANNER,)))


@dataclass(frozen=True)
class Activity:
    """What's in progress in a session: a task run (``tasks``) on task ``task_id``, a
    ``review`` of the tasks ``reviewing``, the turns of the conversational roles ``answering``,
    the planner first, and the ``startup_check``. The task run's task and the reviewed tasks
    are none while they aren't known; a review of no particular task, such as a super review,
    reviews no tasks. The startup check runs on the base commit rather than in a session, so it
    leaves the session idle. A task run waiting in line for a coder's slot waits at position
    ``waiting_for_coder``, 1 next, while ``coders_busy`` coders hold the slots, and both are
    none otherwise. ``lane_tasks`` are the tasks running in lanes that start by themselves,
    reviews aside."""

    tasks: bool = False
    review: bool = False
    answering: tuple[str, ...] = ()
    startup_check: bool = False
    task_id: str | None = None
    reviewing: tuple[str, ...] | None = None
    waiting_for_coder: int | None = None
    coders_busy: int | None = None
    lane_tasks: tuple[str, ...] = ()

    @property
    def running(self) -> bool:
        """Whether anything but the startup check is in progress; the session is idle
        otherwise."""
        return first_running(self) is not None


def first_running(activity: Activity) -> Running | None:
    """What runs first of a task run, a task in a lane of its own, a review and a conversational
    role's turn in ``activity``; none when nothing does."""
    kinds = (
        (activity.tasks, Running.TASKS),
        (bool(activity.lane_tasks), Running.LANES),
        (activity.review, Running.REVIEW),
        (bool(activity.answering), Running.PLANNER),
    )
    return next((kind for runs, kind in kinds if runs), None)


def doing(activity: Activity) -> str | None:
    """What runs first of what :func:`first_running` orders in ``activity``, as it's
    called when it keeps the session busy, such as "tasks are running" or "the architect is
    answering"; none when nothing runs."""
    said = _said(activity)
    return None if said is None else said[0]


def switch_refusal(activity: Activity) -> str | None:
    """Why a switch is refused while ``activity`` is in progress, saying what runs first of what
    :func:`first_running` orders, naming the conversational roles answering; none when nothing
    runs."""
    said = _said(activity)
    return None if said is None else _refusal(*said)


def _said(activity: Activity) -> tuple[str, str] | None:
    """What runs first of what :func:`first_running` orders in ``activity``, as it's called and
    as the user stops it; none when nothing runs."""
    kind = first_running(activity)
    if kind is Running.PLANNER:
        return answering(activity.answering), _stopping(activity.answering)
    return None if kind is None else (_DOING[kind], _TO_STOP[kind])


def reported(activity: Activity) -> tuple[Reported, ...]:
    """What's in progress in ``activity`` as connections are told it: a task run, its wait for
    a coder, the tasks running in lanes of their own, each as a task run, a review, the
    conversational roles' turns, told as one planner turn, and the startup check, in that order;
    a task run or a review once its tasks are known. A review is told once per task it reviews,
    in order, or once without a task if it reviews no particular one."""
    task_run = activity.tasks and activity.task_id is not None
    position, busy = activity.waiting_for_coder, activity.coders_busy
    waiting = Reported(kind="waiting_for_coder", position=position, busy=busy)
    entries = (
        (task_run, (Reported(kind="task_run", task_id=activity.task_id),)),
        (position is not None, (waiting,)),
        (True, tuple(Reported(kind="task_run", task_id=task) for task in activity.lane_tasks)),
        (activity.review, _reviews(activity.reviewing)),
        (bool(activity.answering), (Reported(kind="planner"),)),
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
