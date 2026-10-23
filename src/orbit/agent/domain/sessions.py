"""Saved sessions: stretches of work the user can come back to.

A saved session keeps its task list, its review state, the planner's conversation and the
settings it ran with. It is unrelated to the backend's connection, :class:`WorkspaceHost`. The
caller reads and writes the store and the clock; these rules only shape what's in them.
"""

from collections import Counter
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import reduce
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import from_json

from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.reviews import LaneSnapshot
from orbit.protocol import (
    BUSY,
    INVALID,
    LOCKED,
    WRONG_SESSION,
    SessionSummary,
    Settings,
    TaskState,
    Usage,
)

FORMAT_VERSION = 5
"""The version of the record format this orbit writes, and the newest it reads."""

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

Migration = Callable[[dict[str, Any]], dict[str, Any]]


def _spent_by_tasks(record: dict[str, Any]) -> dict[str, Any]:
    """Version 1 ``record`` with what it spent: its tasks' usage, all that version 1 kept. With
    tasks or usages that can't be read, it's left for the record's validation to say why."""
    tasks = record.get("tasks", [])
    if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
        return record
    try:
        usages = [Usage.model_validate(task.get("usage", {})) for task in tasks]
    except ValidationError:
        return record
    spent = sum(usages, Usage())
    return record | {"spent": spent.model_dump()}


def _without_rounds(record: dict[str, Any]) -> dict[str, Any]:
    """Version 2 ``record`` without its review planner's rounds, which the open batches hold.
    A record whose reviews can't be read is left for its validation to say why."""
    reviews = record.get("reviews")
    if not isinstance(reviews, dict) or not isinstance(reviews.get("planner"), dict):
        return record
    planner = {key: value for key, value in reviews["planner"].items() if key != "rounds"}
    return record | {"reviews": reviews | {"planner": planner}}


def _unchanged(record: dict[str, Any]) -> dict[str, Any]:
    """Version 3 or 4 ``record`` as it is: the next version only added fields that have
    defaults."""
    return record


# By version N: what takes a record of version N and returns its fields in version N+1's shape.
# The upgrade sets the version for it. Version 3 dropped the planner's rounds through a migration
# rather than by having the snapshot ignore them, so that the format stays pinned by its fixtures
# and the snapshot model keeps forbidding fields it doesn't know. Version 4 added the reasons tasks
# ended for, and version 5 the tasks each review reviews; their versions stop an older orbit,
# which forbids fields it doesn't know, at a clear "written by a newer orbit" rather than a field
# it can't read.
_MIGRATIONS: dict[int, Migration] = {
    1: _spent_by_tasks,
    2: _without_rounds,
    3: _unchanged,
    4: _unchanged,
}

EMPTY_NAME = "A session needs a name."
OPEN_UNDELETABLE = "Can't delete the open session."
OPEN_UNRENAMABLE = "Can't rename the session as a saved one: it was opened meanwhile."


class SessionRecord(BaseModel):
    """A saved session ``id``, ``name``d by the user, with the ``settings`` its planner and new
    tasks use, the planner's Claude conversation, its ``tasks`` and, when reviews are on, its
    ``reviews``' state, and what its agents have ``spent`` in all, the planner and tasks since
    removed included."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = FORMAT_VERSION
    id: str = Field(min_length=1)
    name: str
    created: AwareDatetime
    last_active: AwareDatetime
    settings: Settings = Settings()
    planner_conversation: str | None = None
    tasks: tuple[TaskState, ...] = ()
    reviews: LaneSnapshot | None = None
    spent: Usage = Usage()


class _ReviewCount(BaseModel):
    """The commits since the last super review, kept per repository."""

    model_config = ConfigDict(frozen=True)

    since_super: int = Field(ge=0)


@dataclass(frozen=True)
class Unreadable:
    """A record that can't be read, for ``reason``."""

    reason: str


def short_time(when: datetime) -> str:
    """``when`` as the user reads it, such as ``Oct 17 14:30``."""
    return f"{_MONTHS[when.month - 1]} {when.day} {when:%H:%M}"


def default_name(when: datetime) -> str:
    """The name of a session started at ``when``, such as ``Oct 17 14:30``."""
    return short_time(when)


def record_json(record: SessionRecord) -> str:
    """``record`` as JSON text, for :func:`parse_record` to read back."""
    return record.model_dump_json(indent=2)


def parse_record(text: str) -> SessionRecord | Unreadable:
    """The record in JSON ``text``, upgraded from an older format if need be, or why it's
    unreadable."""
    try:
        data = from_json(text)
    except ValueError as error:
        return Unreadable(f"it isn't JSON: {error}")
    if not isinstance(data, dict):
        return Unreadable("it isn't a JSON object")
    upgraded = _upgraded(data)
    if isinstance(upgraded, Unreadable):
        return upgraded
    try:
        return SessionRecord.model_validate(upgraded)
    except ValidationError as error:
        return Unreadable(_first_problem(error))


def _upgraded(data: dict[str, Any]) -> dict[str, Any] | Unreadable:
    """``data`` in the current format, or why its version can't be read."""
    version = data.get("version")
    if type(version) is not int:
        return Unreadable(f"its version isn't a whole number: {version!r}")
    if version > FORMAT_VERSION:
        return Unreadable(
            f"it was written by a newer orbit (format {version}; "
            f"this one reads up to {FORMAT_VERSION})"
        )
    if version < 1:
        return Unreadable(f"its version is unknown: {version}")
    return reduce(_migrated, range(version, FORMAT_VERSION), data)


def _migrated(record: dict[str, Any], old: int) -> dict[str, Any]:
    """``record``, of version ``old``, upgraded to the next version."""
    return _MIGRATIONS[old](record) | {"version": old + 1}


def _first_problem(error: ValidationError) -> str:
    """The first thing wrong with a record, such as ``tasks.0.task: Field required``."""
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    return f"{where}: {first['msg']}"


def summarize(record: SessionRecord, current: bool = False, locked: bool = False) -> SessionSummary:
    """``record`` as the list of sessions shows it; ``current`` if it's the session open here,
    ``locked`` if another orbit has it open."""
    summary = tasks_summary(record.id, record.name, record.tasks, record.spent)
    return summary.model_copy(
        update={"last_active": record.last_active, "current": current, "locked": locked}
    )


def tasks_summary(
    session_id: str, name: str, tasks: Sequence[TaskState], spent: Usage
) -> SessionSummary:
    """The session ``session_id``, ``name``d so, holding ``tasks`` and having ``spent`` so much,
    as the list of sessions shows it, without a last active time."""
    return SessionSummary(
        id=session_id, name=name, counts=dict(Counter(task.status for task in tasks)), usage=spent
    )


def stored_summary(
    session_id: str, record: SessionRecord | Unreadable, current: bool, locked: bool
) -> SessionSummary:
    """The session ``session_id`` saved as ``record`` (or unreadable), as the list of sessions
    shows it; see :func:`summarize`. An unreadable one goes by its id."""
    if isinstance(record, SessionRecord):
        return summarize(record, current, locked)
    return SessionSummary(
        id=session_id, name=session_id, current=current, locked=locked, unreadable=record.reason
    )


def newest_first(summaries: Iterable[SessionSummary]) -> tuple[SessionSummary, ...]:
    """``summaries`` by when they were last active, the latest first; those with no time come
    last."""
    return tuple(sorted(summaries, key=_activity, reverse=True))


def _activity(summary: SessionSummary) -> tuple[bool, float]:
    last = summary.last_active
    return last is not None, last.timestamp() if last else 0.0


def openable(
    session_id: str, record: SessionRecord | Unreadable, locked: bool
) -> SessionRecord | Refused:
    """The record of saved session ``session_id`` to open, or why it can't be opened: it's
    unreadable, or ``locked`` by another orbit."""
    return _usable("open", session_id, record, locked)


def renamable(
    session_id: str, record: SessionRecord | Unreadable, locked: bool
) -> SessionRecord | Refused:
    """The record of saved session ``session_id`` to rename, or why it can't be renamed: it's
    unreadable, or ``locked`` by another orbit."""
    return _usable("rename", session_id, record, locked)


def viewable(
    session_id: str, record: SessionRecord | Unreadable, locked: bool
) -> SessionRecord | Refused:
    """The record of saved session ``session_id`` to view, or why it can't be viewed: it's
    unreadable. One another orbit has open, ``locked``, can be viewed as it was saved."""
    return _usable("view", session_id, record, locked=False)


def _usable(
    action: str, session_id: str, record: SessionRecord | Unreadable, locked: bool
) -> SessionRecord | Refused:
    """The record of saved session ``session_id``, or why the user can't ``action`` it."""
    if isinstance(record, Unreadable):
        return Refused(INVALID, f"Can't {action} the session {session_id}: {record.reason}.")
    if locked:
        return Refused(LOCKED, locked_message(record.name, action))
    return record


def deletion_refusal(
    session_id: str, record: SessionRecord | Unreadable, locked: bool
) -> Refused | None:
    """Why the saved session ``session_id``, not the one open here, can't be deleted, if it
    can't: it's ``locked`` by another orbit. An unreadable one can be. The open session can't
    be deleted either (:data:`OPEN_UNDELETABLE`)."""
    if locked:
        return Refused(LOCKED, locked_message(record_name(session_id, record), "delete"))
    return None


def viewed_refusal(name: str, viewers: int) -> Refused:
    """Why the session ``name`` can't be deleted while ``viewers`` other frontends view it."""
    frontends = "1 other frontend views" if viewers == 1 else f"{viewers} other frontends view"
    return Refused(BUSY, f'Can\'t delete "{name}": {frontends} it.')


def not_active_message(viewed: str, open_name: str) -> str:
    """Why a command that would change or run something can't be done in the session named
    ``viewed`` while the workspace's open session is the one named ``open_name``."""
    return (
        f"Session {viewed!r} isn't the active session; {open_name!r} is. Activate it to work in it."
    )


def check_addressed_session(addressed: str | None, viewing_id: str | None, open_id: str) -> None:
    """Refuse a command for the session ``addressed`` as ``wrong_session`` unless that's none or
    the one its connection views: ``viewing_id``, or the workspace's open session ``open_id``
    when that's none."""
    viewed = open_id if viewing_id is None else viewing_id
    if addressed is None or addressed == viewed:
        return
    if viewing_id is None:
        message = f"Session {addressed!r} isn't open; the open session is {viewed}."
    else:
        message = f"Session {addressed!r} isn't the one viewed; this connection views {viewed}."
    raise Refused(WRONG_SESSION, message)


def record_name(session_id: str, record: SessionRecord | Unreadable) -> str:
    """The name of the session ``session_id`` saved as ``record``; an unreadable one goes by its
    id."""
    return record.name if isinstance(record, SessionRecord) else session_id


def session_name(name: str | None) -> str | None:
    """``name`` as a session is named, without the spaces around it; none when it's none or
    empty."""
    return (name or "").strip() or None


def locked_message(name: str, action: str = "open") -> str:
    """Why the user can't ``action`` the session ``name`` while another orbit has it open."""
    return f"Can't {action} \"{name}\": it's open in another orbit."


def missing_message(session_id: str) -> str:
    """Why the session ``session_id``, which isn't saved, can't be opened."""
    return f"There's no saved session {session_id!r}."


def resumed_notes(record: SessionRecord, interrupted: int) -> tuple[str, ...]:
    """What to tell the user on opening ``record``, in which ``interrupted`` tasks were
    cancelled because they were running when it was last saved."""
    resumed = f'Resumed "{record.name}" (last active {short_time(record.last_active)})'
    if interrupted == 0:
        return (resumed,)
    if interrupted == 1:
        return resumed, "1 task was interrupted and is cancelled."
    return resumed, f"{interrupted} tasks were interrupted and are cancelled."


def resumed_settings(saved: Settings, given: Settings, pinned: Collection[str]) -> Settings:
    """The settings to resume a session ``saved`` with: those settings, except the ones
    ``pinned``, by field name, which are as ``given``."""
    return saved.model_copy(update={name: getattr(given, name) for name in pinned})


def imported_note(count: int, sessions: str, folder: str) -> str:
    """The note that ``count`` sessions saved as files in the folder ``sessions`` were imported,
    and that the folder is now named ``folder``."""
    noun = "session" if count == 1 else "sessions"
    return f"Imported {count} saved {noun} from {sessions}; the old folder is now {folder}."


def parse_review_count(text: str) -> int:
    """The commits since the last super review in JSON ``text``; 0 when it's unreadable."""
    try:
        return _ReviewCount.model_validate_json(text).since_super
    except ValidationError:
        return 0
