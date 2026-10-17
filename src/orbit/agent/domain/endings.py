"""Why a task ended other than completed or unverified: a line for the board, and the full text.

Each ending is a ``(reason, detail)`` pair; the detail is ``""`` when the reason says it all.
"""

from orbit.agent.domain.agent_task import Phase
from orbit.protocol import TaskStatus

Ending = tuple[str, str]
"""Why a task ended: the reason in a line, and its detail in full."""

STOPPED_BY_YOU = "Stopped by you"
"""Why a task the user stopped, with ctrl+s or by cancelling its agent, is cancelled."""
REVIEW_STOPPED_BY_YOU = "Review stopped by you"
"""Why a review whose reviewer the user cancelled is cancelled."""
ENDED_AT_PROPOSAL = "Ended at the checks proposal"
"""Why a task the user ended while deciding on its proposal of checks is cancelled."""
CANCELLED = "Cancelled"
"""Why a task cancelled any other way, such as by orbit quitting, is cancelled."""
BUG = "Error: orbit hit a bug running this task"
"""Why a task whose run broke on something orbit didn't expect ends in error."""

PHASE_ERRORS: dict[Phase, str] = {
    "assigned": "The agent exited before fetching its task",
    "working": "The agent stopped before finishing its task",
    "verifying": "The agent stopped while orbit checked its work",
}
"""What went wrong when an agent's run ends in a phase that's an error, without a message."""


def first_line(text: str) -> str:
    """The first line of ``text`` that isn't blank, stripped; ``""`` if there's none."""
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def gave_up(reason: str) -> Ending:
    """Why a task ended because its agent gave up, for ``reason``."""
    return _headed("Gave up", reason), reason


def error(message: str) -> Ending:
    """Why a task ended in error, as ``message`` says."""
    return _headed("Error", message), message


def bug(exc: BaseException) -> Ending:
    """Why a task ended in error when its run broke on ``exc``."""
    return BUG, str(exc)


def run_ending(status: TaskStatus, phase: Phase, given_up: str, failed: str | None) -> Ending:
    """Why an agent's run that left its work in ``phase`` ended its task as ``status``, other
    than for failing verification: the agent ``given_up`` for that reason, the error message
    ``failed`` (or else what the phase says went wrong), or the user ending it at its proposal,
    the only way a run ends cancelled rather than being cancelled. Nothing for any other."""
    match status:
        case "gave_up":
            return gave_up(given_up)
        case "error":
            return error(failed or PHASE_ERRORS[phase])
        case "cancelled":
            return ENDED_AT_PROPOSAL, ""
    return "", ""


def _headed(heading: str, text: str) -> str:
    """``heading``, followed by the first line of ``text`` if it has one."""
    line = first_line(text)
    return f"{heading}: {line}" if line else heading
