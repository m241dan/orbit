"""The rule of which tasks can be edited, which a server and its frontends both follow: not
while a task runs, and never a review orbit queued itself."""

from dataclasses import dataclass

from orbit.protocol.codes import BUSY, INVALID
from orbit.protocol.models import ACTIVE_STATUSES, TaskState


@dataclass(frozen=True)
class EditRefusal:
    """Why a task can't be edited: the refusal ``code`` the backend answers with, and its
    ``message``, which says why."""

    code: str
    message: str


def is_orbit_review(state: TaskState) -> bool:
    """Whether ``state`` is a review orbit queued itself: a task it queued as a review, booked
    into its review lane with a batch of its own, whatever its role or the trigger that queued
    it, rather than a review the user or the planner added, or one a trigger queued without
    booking it. The task says so (see :data:`~orbit.protocol.QueuedAs`), so a session read back
    from a save knows its reviews."""
    return state.queued_as == "review"


def edit_refusal(state: TaskState) -> EditRefusal | None:
    """Why ``state`` can't be edited; None if it can.

    A running or verifying task can't change under its agent, and a review orbit queued (see
    :func:`is_orbit_review`) has a batch that's spent, so it couldn't run again. A review the
    user or the planner added, or one a trigger queued without booking it, or a task a review
    suggested, can be edited.
    """
    if state.status in ACTIVE_STATUSES:
        return EditRefusal(BUSY, f"Task {state.id!r} can't be edited while it runs.")
    if is_orbit_review(state):
        return EditRefusal(INVALID, "Reviews can't be edited; remove it instead.")
    return None
