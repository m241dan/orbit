"""The list of saved sessions, to pick one from."""

from datetime import datetime, tzinfo

from rich.text import Text

from orbit.protocol import SessionSummary, TaskStatus, Usage
from orbit.tui.view.common import SPINNER_FRAMES, STATUS_ICONS
from orbit.tui.view.tasks import usage_label

SESSIONS_TITLE = "Sessions"
NEW_SESSION_LABEL = "+ new session"
NAME_PLACEHOLDER = "Name (empty: the date and time)"
CURRENT_MARK = "● "
LOCKED_NOTE = "open in another orbit"

# The order a session's task counts are shown in: finished work first, then what's still to do.
COUNT_ORDER: tuple[TaskStatus, ...] = (
    "completed",
    "unverified",
    "gave_up",
    "cancelled",
    "error",
    "verifying",
    "running",
    "pending",
    "draft",
)
# A session's tasks aren't animated, so a running one shows the spinner's first frame.
COUNT_ICONS: dict[TaskStatus, str] = STATUS_ICONS | {"running": SPINNER_FRAMES[0]}


def session_label(summary: SessionSummary, zone: tzinfo | None = None) -> Text:
    """A row: the current session's mark, then the name and what's known of the session, dim
    when it can't be opened. Times are in ``zone``, the local one by default."""
    mark = CURRENT_MARK if summary.current else " " * len(CURRENT_MARK)
    text = " · ".join([summary.name, *_details(summary, zone)])
    return Text(mark + text, style="" if can_open(summary) else "dim")


def can_open(summary: SessionSummary) -> bool:
    """Whether ``summary``'s session can be opened: it's readable and no other orbit has it."""
    return not summary.locked and summary.unreadable is None


def _details(summary: SessionSummary, zone: tzinfo | None) -> list[str]:
    """What the row says after the name."""
    if summary.unreadable is not None:
        return [f"unreadable: {summary.unreadable}"]
    details = [] if summary.last_active is None else [local_time(summary.last_active, zone)]
    if summary.locked:
        return [*details, LOCKED_NOTE]
    counts = task_counts(summary.counts)
    if counts:
        details.append(counts)
    if summary.usage != Usage():
        details.append(usage_label(summary.usage))
    return details


def local_time(when: datetime, zone: tzinfo | None = None) -> str:
    """``when`` in ``zone`` (the local one by default), such as ``Oct 16 18:02``."""
    local = when.astimezone(zone)
    return f"{local:%b} {local.day} {local:%H:%M}"


def task_counts(counts: dict[TaskStatus, int]) -> str:
    """How many tasks stand at each status, by the task list's icons, leaving out none:
    ``3 ✅ 2 ⏳ 1 📝``."""
    return " ".join(
        f"{counts[status]} {COUNT_ICONS[status]}" for status in COUNT_ORDER if counts.get(status)
    )
