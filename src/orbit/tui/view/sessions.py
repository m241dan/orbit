"""The list of saved sessions, to pick one from."""

from collections.abc import Sequence
from datetime import datetime, tzinfo

from rich.text import Text

from orbit.protocol import (
    ActivateSession,
    Attach,
    NewSession,
    SessionStateChanged,
    SessionSummary,
    TaskStatus,
    Usage,
)
from orbit.tui.view.common import SPINNER_FRAMES, STATUS_ICONS
from orbit.tui.view.tasks import usage_label

SESSIONS_TITLE = "Sessions"
NEW_SESSION_LABEL = "New session"
RESUME_PREFIX = "Resume "
NAME_PLACEHOLDER = "Name (empty: the date and time)"
CURRENT_MARK = "● "
LOCKED_NOTE = "open in another orbit"
ACTIVE_NOTE = "active"
RUNNING_NOTE = "running"
DELETE_TITLE = "Delete session"
DELETE_YES = "delete"
LIST_HINT = "j/k move · enter open · r rename · x delete · esc close"
STARTUP_HINT = "j/k move · enter open · r rename · x delete · esc quit"
NAME_HINT = "enter start · esc back"
RENAME_HINT = "enter rename · esc back"
ACTIVATION_TITLE = "Not the active session"
ACTIVATE_CHOICE = "Make this the active session"
JUST_LOOK_CHOICE = "Just look"

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


def picker_sessions(
    sessions: Sequence[SessionSummary], attached: str | None, *, with_attached: bool
) -> tuple[SessionSummary, ...]:
    """The ``sessions`` the picker lists, in order: the one ``attached`` first, if
    ``with_attached``, then the others as ``sessions`` has them, the most recently active
    first."""
    first = [summary for summary in sessions if summary.id == attached and with_attached]
    return (*first, *(summary for summary in sessions if summary.id != attached))


def session_label(
    summary: SessionSummary,
    zone: tzinfo | None = None,
    *,
    attached: bool = False,
    resume: bool = False,
) -> Text:
    """A row: the mark of the session ``attached``, then ``Resume`` if ``resume``, the name and
    what's known of the session, dim when it can't be opened. Times are in ``zone``, the local
    one by default."""
    mark = CURRENT_MARK if attached else " " * len(CURRENT_MARK)
    name = RESUME_PREFIX + summary.name if resume else summary.name
    text = " · ".join([name, *_details(summary, zone, attached)])
    return Text(mark + text, style="" if can_open(summary) else "dim")


def picked_commands(
    picked: SessionSummary, sessions: Sequence[SessionSummary], attached: str | None
) -> tuple[Attach | ActivateSession, ...]:
    """What picking ``picked`` among ``sessions`` sends: nothing for the session ``attached``;
    else ``Attach``, then ``ActivateSession`` too unless ``picked`` is the active session already
    or the active one runs or is viewed by another frontend."""
    if picked.id == attached:
        return ()
    attach = Attach(session=picked.id)
    if picked.active or not _free(sessions, attached):
        return (attach,)
    return (attach, ActivateSession(session=picked.id))


def viewed_only(
    choice: Sequence[Attach | ActivateSession | NewSession], sessions: Sequence[SessionSummary]
) -> str | None:
    """The session ``choice`` views without activating it, if it views one that isn't the
    active session among ``sessions``; none otherwise."""
    match tuple(choice):
        case (Attach(session=session),):
            active = any(summary.id == session and summary.active for summary in sessions)
            return None if active else session
        case _:
            return None


def _free(sessions: Sequence[SessionSummary], attached: str | None) -> bool:
    """Whether the active session among ``sessions``, if any, is idle with no frontend viewing it
    but this one, which views the session ``attached``."""
    active = next((summary for summary in sessions if summary.active), None)
    if active is None:
        return True
    return active.state == "idle" and _others(active, active.id == attached) == 0


def _others(summary: SessionSummary, attached: bool) -> int:
    """How many frontends view ``summary``'s session besides this one, which views it if
    ``attached``."""
    return max(summary.viewers - int(attached), 0)


def with_state(
    sessions: Sequence[SessionSummary], changed: SessionStateChanged
) -> tuple[SessionSummary, ...]:
    """``sessions``, with ``changed``'s session now as active, running and viewed as it says."""
    update = {"active": changed.active, "state": changed.state, "viewers": changed.viewers}
    return tuple(
        summary.model_copy(update=update) if summary.id == changed.session else summary
        for summary in sessions
    )


def can_open(summary: SessionSummary) -> bool:
    """Whether ``summary``'s session can be opened: it's readable and no other orbit has it."""
    return not summary.locked and summary.unreadable is None


def can_delete(summary: SessionSummary) -> bool:
    """Whether ``summary``'s session may be deleted: it isn't open, here or in another orbit."""
    return not summary.current and not summary.locked


def delete_message(summary: SessionSummary) -> str:
    """What deleting ``summary``'s session asks the user to confirm."""
    return f'Delete "{summary.name}"? Its tasks and review state are gone for good.'


def _details(summary: SessionSummary, zone: tzinfo | None, attached: bool) -> list[str]:
    """What the row says after the name, of a session this frontend views if ``attached``."""
    if summary.unreadable is not None:
        return [f"unreadable: {summary.unreadable}"]
    details = [] if summary.last_active is None else [local_time(summary.last_active, zone)]
    if summary.locked:
        return [*details, LOCKED_NOTE]
    details.extend(state_notes(summary, attached))
    counts = task_counts(summary.counts)
    if counts:
        details.append(counts)
    if summary.usage != Usage():
        details.append(usage_label(summary.usage))
    return details


def state_notes(summary: SessionSummary, attached: bool = False) -> list[str]:
    """Whether ``summary``'s session is the active one, whether it runs work and how many other
    frontends view it, besides this one if ``attached``: ``active · running · viewed by 2``."""
    notes = [ACTIVE_NOTE] if summary.active else []
    if summary.state == "running":
        notes.append(RUNNING_NOTE)
    others = _others(summary, attached)
    if others:
        notes.append(f"viewed by {others}")
    return notes


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


def sessions_hint(*, startup: bool, naming: bool, renaming: bool) -> str:
    """The keys the sessions modal shows: the name box's while ``naming`` (a new name for a
    session when ``renaming``), or else the list's, where escape quits at ``startup``."""
    if naming:
        return RENAME_HINT if renaming else NAME_HINT
    return STARTUP_HINT if startup else LIST_HINT


def viewing_mark(name: str) -> str:
    """The mark by the prompt while this frontend views the session ``name``, which isn't the
    active one."""
    return f"viewing {name}, not the active session (enter to switch)"


def active_running(sessions: Sequence[SessionSummary]) -> bool:
    """Whether the active session among ``sessions`` runs work."""
    return any(summary.active and summary.state == "running" for summary in sessions)


def activation_message(viewed: str, open_name: str, *, running: bool) -> str:
    """What the prompt to make the session ``viewed`` the active one says, while the session
    ``open_name`` is, and is ``running`` or idle."""
    if running:
        return f"{open_name} is running; you can switch once it's idle."
    return f"{viewed} is a read-only view; {open_name} is the active session, which runs work."


def activation_choices(*, running: bool) -> tuple[str, ...]:
    """What the prompt offers: making the session viewed the active one unless the active one is
    ``running``, then just looking."""
    return (JUST_LOOK_CHOICE,) if running else (ACTIVATE_CHOICE, JUST_LOOK_CHOICE)
