"""The list of saved sessions, to pick one from."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo

from rich.text import Text

from orbit.protocol import (
    PAUSED_FOR_API_UNAVAILABLE,
    PAUSED_FOR_USAGE_LIMIT,
    ActivateSession,
    Activity,
    ActivityKind,
    Attach,
    NewSession,
    PauseReason,
    SessionSummary,
    TaskStatus,
    Usage,
    WorkspaceEntry,
)
from orbit.tui.view.common import (
    CODER_WAIT,
    SPINNER_FRAMES,
    STATUS_ICONS,
    ListHints,
    active_session,
    coder_place,
)
from orbit.tui.view.tasks import coder_wait, usage_label
from orbit.tui.view.workspaces import workspace_at

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
SESSIONS_HINTS = ListHints(closing=LIST_HINT, quitting=STARTUP_HINT)
ACTIVATION_TITLE = "Not the active session"
ACTIVATE_CHOICE = "Make this the active session"
JUST_LOOK_CHOICE = "Just look"
PAUSED_PREFIX = "run paused"

# What a paused run says of why it's paused, by reason, before the time it starts again.
PAUSED_REASONS = {
    PAUSED_FOR_API_UNAVAILABLE: ": Claude's API isn't answering; trying again at",
    PAUSED_FOR_USAGE_LIMIT: ": usage limit; starts again at",
}
PAUSED_UNKNOWN = "; starts again at"

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
    at_repository = workspace_at(workspaces, repository)
    return at_repository or next(iter(workspaces), None)


def workspace_sessions(
    workspaces: Sequence[WorkspaceEntry], workspace: str | None
) -> tuple[SessionSummary, ...]:
    """The sessions of the workspace ``workspace`` among ``workspaces``; none if it isn't
    listed."""
    entry = next((entry for entry in workspaces if entry.id == workspace), None)
    return entry.sessions if entry else ()


def active_id(sessions: Sequence[SessionSummary]) -> str | None:
    """The id of the active session among ``sessions``; none if none is."""
    active = active_session(sessions)
    return None if active is None else active.id


def opening_attach(workspace: str | None, sessions: Sequence[SessionSummary]) -> Attach | None:
    """What a frontend opening on the workspace ``workspace`` sends to view its active session
    among ``sessions``; none without either."""
    active = active_session(sessions)
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


@dataclass(frozen=True)
class PickerRow:
    """A row of the picker: the session ``summary``, marked if it's the one this frontend views,
    ``attached``, and saying ``Resume`` if it's to be resumed."""

    summary: SessionSummary
    attached: bool = False
    resume: bool = False


def picker_rows(
    sessions: Sequence[SessionSummary], viewing: Viewing, *, startup: bool, resume: bool
) -> tuple[PickerRow, ...]:
    """The rows the picker lists of ``sessions`` from ``viewing``: the session attached first,
    unless opened at ``startup`` without offering to ``resume`` it, marked once the frontend has
    joined the workspace, and to resume at ``startup`` or before it has joined, then the
    others."""
    attached, resuming = viewing.attached, startup or not viewing.joined
    listed = picker_sessions(sessions, attached, with_attached=resume or not startup)
    return tuple(
        PickerRow(
            summary,
            attached=viewing.joined and summary.id == attached,
            resume=resuming and summary.id == attached,
        )
        for summary in listed
    )


def unjoined_view(
    workspaces: Sequence[WorkspaceEntry], workspace: str
) -> tuple[Viewing, tuple[SessionSummary, ...]]:
    """What a frontend that hasn't joined the workspace ``workspace`` among ``workspaces`` views
    of it, its active session to resume, and its sessions."""
    sessions = workspace_sessions(workspaces, workspace)
    return Viewing(workspace, active_id(sessions), joined=False), sessions


def picker_viewing(
    workspaces: Sequence[WorkspaceEntry],
    on: str | None,
    attached: str | None,
    sessions: Sequence[SessionSummary],
    workspace: str | None,
) -> tuple[Viewing, tuple[SessionSummary, ...]]:
    """What the picker of the ``workspace`` among ``workspaces`` views, and its sessions: by
    default, or when it's the workspace ``on`` that this frontend is on, the session
    ``attached`` among ``sessions``; else that workspace, unjoined."""
    if workspace is None or workspace == on:
        # No session is listed, so none is picked, until a catalog names the workspace.
        return Viewing(workspace=on or "", attached=attached), tuple(sessions)
    return unjoined_view(workspaces, workspace)


def held_back(viewed: str, *, running: str | None, active: str | None) -> str | None:
    """The session ``viewed``, if the active session ``active``, ``running`` what it says, keeps
    it from being the active one; none while that's idle or isn't known."""
    return viewed if running is not None and active is not None else None


def session_turned_idle(sessions: Sequence[SessionSummary], held: str) -> str | None:
    """The name of the active session among ``sessions``, if it's idle and isn't the session
    ``held``; none otherwise."""
    active = active_session(sessions)
    if active is None or active.id == held or active.state != "idle":
        return None
    return active.name


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
    active = active_session(sessions)
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
    details.extend(state_notes(summary, attached, zone))
    counts = task_counts(summary.counts)
    if counts:
        details.append(counts)
    if summary.usage != Usage():
        details.append(usage_label(summary.usage))
    return details


def state_notes(
    summary: SessionSummary, attached: bool = False, zone: tzinfo | None = None
) -> list[str]:
    """Whether ``summary``'s session is the active one, whether it runs work or waits for a
    coder, whether its run is paused, until a time in ``zone`` (the local one by default), and
    how many other frontends view it, besides this one if ``attached``: ``active · running ·
    viewed by 2``."""
    notes = [ACTIVE_NOTE] if summary.active else []
    if summary.state == "running":
        wait = coder_wait(summary.activity)
        notes.append(RUNNING_NOTE if wait is None else waiting_words(wait.position))
    paused = paused_words(summary, zone)
    if paused is not None:
        notes.append(paused)
    others = _others(summary, attached)
    if others:
        notes.append(f"viewed by {others}")
    return notes


def paused_words(summary: SessionSummary, zone: tzinfo | None = None) -> str | None:
    """What ``summary``'s session says of its run while it's paused, until a time in ``zone``
    (the local one by default): ``run paused: Claude's API isn't answering; trying again at
    14:20``, ``run paused: usage limit; starts again at 18:01``, or ``run paused; starts again at
    14:20`` for a reason not known; none while it isn't paused."""
    until = summary.paused_until
    return None if until is None else paused_note(until, summary.paused_for, zone)


def paused_note(until: datetime, reason: PauseReason | None, zone: tzinfo | None = None) -> str:
    """What a run paused for ``reason`` says of itself, starting again at ``until``, a time in
    ``zone`` (the local one by default), as :func:`paused_words` words it."""
    words = PAUSED_REASONS.get(reason or "", PAUSED_UNKNOWN)
    return f"{PAUSED_PREFIX}{words} {until.astimezone(zone):%H:%M}"


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


def name_hint(*, renaming: bool) -> str:
    """The keys the sessions modal's name box shows: for a new name for a session when
    ``renaming``, or else for a new session's name."""
    return RENAME_HINT if renaming else NAME_HINT


def viewing_mark(name: str, active: str | None, running: str | None) -> str:
    """The mark by the prompt while this frontend views the session ``name``, which isn't the
    active one: what the session ``active`` is doing, ``running``, or that it's idle while
    that's none."""
    if active is None:
        return f"viewing {name}, not the active session (enter to switch)"
    if running is None:
        return f"viewing {name}; {active} is idle (enter to switch)"
    return f"viewing {name}; {active} is {running} (enter to switch when it's idle)"


def activity_words(activity: Sequence[Activity]) -> str:
    """What ``activity`` is, in words: ``a task``, ``the review of a task``, ``the planner`` or
    ``the startup check``, several joined as ``the planner and a task``; ``work`` when there's
    none. Tasks go unnumbered: their ids are the active session's, and the task list numbers
    the session viewed's."""
    words = [_activity_word(each) for each in activity]
    if not words:
        return "work"
    if len(words) == 1:
        return words[0]
    return f"{', '.join(words[:-1])} and {words[-1]}"


def state_words(activity: Sequence[Activity]) -> str:
    """What a session running ``activity`` is doing, in words: ``running a task``, or
    ``waiting for a coder: 2nd in line`` while its task run waits for one, after what else
    runs: ``running the planner and waiting for a coder: 2nd in line``."""
    wait = coder_wait(activity)
    if wait is None:
        return f"running {activity_words(activity)}"
    others = [each for each in activity if each.kind not in ("task_run", "waiting_for_coder")]
    waiting = waiting_words(wait.position)
    return f"running {activity_words(others)} and {waiting}" if others else waiting


def waiting_words(position: int | None) -> str:
    """A wait for a coder at ``position`` in line, in words: ``waiting for a coder: 2nd in
    line``, ``next in line`` at 1, and just ``waiting for a coder`` without a position."""
    place = coder_place(position)
    return CODER_WAIT if place is None else f"{CODER_WAIT}: {place} in line"


def _activity_word(activity: Activity) -> str:
    """What ``activity`` is, in words."""
    words: dict[ActivityKind, str] = {
        "task_run": "a task",
        "review": "the review of a task",
        "planner": "the planner",
        "startup_check": "the startup check",
    }
    return words[activity.kind]


def active_name(sessions: Sequence[SessionSummary]) -> str | None:
    """The name of the active session among ``sessions``; none if none is."""
    active = active_session(sessions)
    return None if active is None else active.name


def running_words(sessions: Sequence[SessionSummary]) -> str | None:
    """What the active session among ``sessions`` is doing, in words, as :func:`state_words`
    says it; none while it's idle or none is active."""
    active = active_session(sessions)
    if active is None or active.state != "running":
        return None
    return state_words(active.activity)


def activation_message(viewed: str, open_name: str, *, running: str | None) -> str:
    """What the prompt to make the session ``viewed`` the active one says, while the session
    ``open_name`` is, and is doing what ``running`` says, or idle while that's none."""
    if running is not None:
        return f"{open_name} is {running}; you can switch once it's idle."
    return f"{viewed} is a read-only view; {open_name} is the active session, which runs work."


def idle_now_message(open_name: str) -> str:
    """What the prompt to make the session viewed the active one says when the session
    ``open_name``, which held it back by running, turns idle."""
    return f"{open_name} is idle now."


def activation_choices(*, running: bool) -> tuple[str, ...]:
    """What the prompt offers: making the session viewed the active one unless the active one is
    ``running``, then just looking."""
    return (JUST_LOOK_CHOICE,) if running else (ACTIVATE_CHOICE, JUST_LOOK_CHOICE)
