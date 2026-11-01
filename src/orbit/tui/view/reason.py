"""The modal that says why a task ended: its title, its text and its keys."""

from orbit.protocol import TaskState

REASON_HINT = "j/k scroll · y copy · esc close"


def reason_title(state: TaskState, number: str) -> str:
    """The reason modal's title for task ``number``: why it stopped, if cancelled, else why it
    failed."""
    ending = "stopped" if state.status == "cancelled" else "failed"
    return f"Why task {number} {ending}"


def reason_text(state: TaskState) -> str:
    """Why ``state`` ended, in full: its reason, then a blank line and its detail, if it has
    one. It's what the reason modal shows and what y copies."""
    if not state.reason_detail:
        return state.reason
    return f"{state.reason}\n\n{state.reason_detail}"
