"""The list of saved sessions, to pick one from."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo

from rich.text import Text

from orbit.protocol import (
    ActivateSession,
    Activity,
    ActivityKind,
    Attach,
    NewSession,
    SessionSummary,
    TaskStatus,
    Usage,
    WorkspaceEntry,
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


@dataclass(frozen=True)
class Viewing:
    """Which session, ``attached``, of the workspace ``workspace`` a frontend views; none
    while it views none. Unless it has ``joined`` the workspace, it views none there yet, and
    ``attached`` is the active session it would resume."""

    workspace: str
    attached: str | None
    joined: bool = True


def chosen_workspace(
    workspaces: Sequence[WorkspaceEntry], repository: str | None
) -> WorkspaceEntry | None:
    """The workspace among ``workspaces`` a frontend opens on: the one at the ``repository`` it
    runs in, if any is, else the first; none if there are none."""
    at_repository = (entry for entry in workspaces if repository and entry.path == repository)
    return next(at_repository, next(iter(workspaces), None))


def workspace_sessions(
    workspaces: Sequence[WorkspaceEntry], workspace: str | None
) -> tuple[SessionSummary, ...]:
    """The sessions of the workspace ``workspace`` among ``workspaces``; none if it isn't
    listed."""
    entry = next((entry for entry in workspaces if entry.id == workspace), None)
    return entry.sessions if entry else ()


def active_id(sessions: Sequence[SessionSummary]) -> str | None:
    """The id of the active session among ``sessions``; none if none is."""
    return next((summary.id for summary in sessions if summary.active), None)


def opening_attach(workspace: str | None, sessions: Sequence[SessionSummary]) -> Attach | None:
    """What a frontend opening on the workspace ``workspace`` sends to view its active session
    among ``sessions``; none without either."""
    active = next((summary for summary in sessions if summary.active), None)
    if workspace is None or active is None:
        return None
    return Attach(workspace=workspace, session=active.id)


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
    picked: SessionSummary,
    sessions: Sequence[SessionSummary],
    attached: str | None,
    workspace: str,
) -> tuple[Attach | ActivateSession, ...]:
    """What picking ``picked`` among ``sessions`` of the workspace ``workspace`` sends: nothing
    for the session ``attached``; else ``Attach``, then ``ActivateSession`` too unless
    ``picked`` is the active session already or the active one runs or is viewed by another
    frontend."""
    if picked.id == attached:
        return ()
    attach = Attach(workspace=workspace, session=picked.id)
    if picked.active or not _free(sessions, attached):
        return (attach,)
    return (attach, ActivateSession(session=picked.id))


def starting_commands(name: str | None, viewing: Viewing) -> tuple[Attach | NewSession, ...]:
    """What starting a new session called ``name`` (the date and time if none) sends from
    ``viewing``: ``NewSession``, after attaching to the workspace's active session if the
    frontend hasn't joined it yet."""
    start = NewSession(name=name)
    if viewing.joined or viewing.attached is None:
        return (start,)
    return (Attach(workspace=viewing.workspace, session=viewing.attached), start)


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


def can_open(summary: SessionSummary) -> bool:
    """Whether ``summary``'s session can be opened: it's readable and no other orbit has it."""
    return not summary.locked and summary.unreadable is None


def can_delete(summary: SessionSummary) -> bool:
    """Whether ``summary``'s session may be deleted: it isn't the active session, here or in
    another orbit."""
    return not summary.active and not summary.locked


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


def viewing_mark(name: str, active: str | None, running: str | None) -> str:
    """The mark by the prompt while this frontend views the session ``name``, which isn't the
    active one: what the session ``active`` is ``running``, or that it's idle while that's none."""
    if active is None:
        return f"viewing {name}, not the active session (enter to switch)"
    if running is None:
        return f"viewing {name}; {active} is idle (enter to switch)"
    return f"viewing {name}; {active} is running {running} (enter to switch when it's idle)"


def activity_words(activity: Sequence[Activity], numbers: Mapping[str, str]) -> str:
    """What ``activity`` is, in words, its tasks named by their ``numbers``: ``task 3``,
    ``the review of task 5``, ``the planner`` or ``the startup check``, several joined as
    ``the planner and task 3``; ``work`` when there's none."""
    words = [_activity_word(each, numbers) for each in activity]
    if not words:
        return "work"
    if len(words) == 1:
        return words[0]
    return f"{', '.join(words[:-1])} and {words[-1]}"


def _activity_word(activity: Activity, numbers: Mapping[str, str]) -> str:
    """What ``activity`` is, in words, its task named by its number in ``numbers``, or as
    ``a task`` without one."""
    number = numbers.get(activity.task_id or "")
    task = "a task" if number is None else f"task {number}"
    words: dict[ActivityKind, str] = {
        "task_run": task,
        "review": f"the review of {task}",
        "planner": "the planner",
        "startup_check": "the startup check",
    }
    return words[activity.kind]


def active_name(sessions: Sequence[SessionSummary]) -> str | None:
    """The name of the active session among ``sessions``; none if none is."""
    return next((summary.name for summary in sessions if summary.active), None)


def running_words(sessions: Sequence[SessionSummary], numbers: Mapping[str, str]) -> str | None:
    """What the active session among ``sessions`` runs, in words, its tasks named by their
    ``numbers``; none while it's idle or none is active."""
    active = next((summary for summary in sessions if summary.active), None)
    if active is None or active.state != "running":
        return None
    return activity_words(active.activity, numbers)


def activation_message(viewed: str, open_name: str, *, running: str | None) -> str:
    """What the prompt to make the session ``viewed`` the active one says, while the session
    ``open_name`` is, and is ``running`` what it says, or idle while that's none."""
    if running is not None:
        return f"{open_name} is running {running}; you can switch once it's idle."
    return f"{viewed} is a read-only view; {open_name} is the active session, which runs work."


def idle_now_message(open_name: str) -> str:
    """What the prompt to make the session viewed the active one says when the session
    ``open_name``, which held it back by running, turns idle."""
    return f"{open_name} is idle now."


def activation_choices(*, running: bool) -> tuple[str, ...]:
    """What the prompt offers: making the session viewed the active one unless the active one is
    ``running``, then just looking."""
    return (JUST_LOOK_CHOICE,) if running else (ACTIVATE_CHOICE, JUST_LOOK_CHOICE)
