"""The rule of which tasks can be edited, which a server and its frontends both follow: not
while a task runs, and never a review orbit queued itself."""

from dataclasses import dataclass

from orbit.protocol.codes import BUSY, INVALID
from orbit.protocol.models import ACTIVE_STATUSES, TaskState
from orbit.protocol.names import REVIEW_ORIGIN, REVIEW_ROLES, SUPER_REVIEW_ORIGIN

_REVIEW_ORIGINS = frozenset({REVIEW_ORIGIN, SUPER_REVIEW_ORIGIN})
"""The origins of what orbit's reviews queue: their reviews and the tasks they suggest."""


@dataclass(frozen=True)
class EditRefusal:
    """Why a task can't be edited: the refusal ``code`` the backend answers with, and its
    ``message``, which says why."""

    code: str
    message: str


def edit_refusal(state: TaskState) -> EditRefusal | None:
    """Why ``state`` can't be edited; None if it can.

    A running or verifying task can't change under its agent, and a review orbit queued, in a
    review role with a review's origin, has a batch that's spent, so it couldn't run again. A
    review the user added, or a task a review suggested, can be edited.
    """
    if state.status in ACTIVE_STATUSES:
        return EditRefusal(BUSY, f"Task {state.id!r} can't be edited while it runs.")
    if state.task.role in REVIEW_ROLES and state.origin in _REVIEW_ORIGINS:
        return EditRefusal(INVALID, "Reviews can't be edited; remove it instead.")
    return None
