"""The rule of which tasks can be edited, which a server and its frontends both follow: not
while a task runs, and never a review orbit queued itself."""

from dataclasses import dataclass

from orbit.protocol.codes import BUSY, INVALID
from orbit.protocol.models import ACTIVE_STATUSES, TaskState
from orbit.protocol.names import MANUAL_ORIGIN, PLANNER_ORIGIN, REVIEW_ROLES

_USERS_ORIGINS = frozenset({MANUAL_ORIGIN, PLANNER_ORIGIN})
"""The origins of the tasks the user queued, themselves or from the planner's drafts; any other
is the name of the trigger that queued its task."""


@dataclass(frozen=True)
class EditRefusal:
    """Why a task can't be edited: the refusal ``code`` the backend answers with, and its
    ``message``, which says why."""

    code: str
    message: str


def is_orbit_review(state: TaskState) -> bool:
    """Whether ``state`` is a review orbit queued itself: a review role's task that a trigger
    queued, its origin that trigger's name, such as ``review``, ``super-review`` or a project's
    own, rather than the user's or the planner's. Its saved origin says so, so a session read
    back from a save knows its reviews."""
    return state.task.role in REVIEW_ROLES and state.origin not in _USERS_ORIGINS


def edit_refusal(state: TaskState) -> EditRefusal | None:
    """Why ``state`` can't be edited; None if it can.

    A running or verifying task can't change under its agent, and a review orbit queued (see
    :func:`is_orbit_review`) has a batch that's spent, so it couldn't run again. A review the
    user added, or a task a review suggested, can be edited.
    """
    if state.status in ACTIVE_STATUSES:
        return EditRefusal(BUSY, f"Task {state.id!r} can't be edited while it runs.")
    if is_orbit_review(state):
        return EditRefusal(INVALID, "Reviews can't be edited; remove it instead.")
    return None
