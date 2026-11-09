"""Saved sessions: stretches of work the user can come back to.

A saved session keeps its task list, its review state, the verified work waiting in the coder's
tree for merges to resume, and each conversational role's conversation, with the runtime it ran
on, and the settings it ran with. It is unrelated to the backend's connection,
:class:`WorkspaceHost`. The caller reads and writes the store and the clock; these rules only
shape what's in them.
"""

from collections import Counter
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import reduce
from typing import Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)
from pydantic_core import from_json

from orbit.agent.domain.consultations import RESTARTED
from orbit.agent.domain.endings import (
    CANCELLED_AT_UPGRADE,
    QUEUED_BEFORE_LENSES,
    QUEUED_FOR_RETIRED_ROLE,
)
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.reviews import LaneSnapshot
from orbit.agent.domain.roles import RETIRED_ROLES
from orbit.agent.domain.runtimes import CLAUDE
from orbit.agent.domain.wording import noun
from orbit.protocol import (
    BUSY,
    INVALID,
    LOCKED,
    PLANNER,
    SUPER_REVIEW,
    WRONG_SESSION,
    SessionSummary,
    Settings,
    TaskState,
    Usage,
    levels_of_tool_access,
)

FORMAT_VERSION = 30
"""The version of the record format this orbit writes, and the newest it reads."""

_OLD_SUPER_REVIEW_ORIGIN = "super_review"
"""The origin of the tasks a super review suggested before version 19, which named it so rather
than by its trigger's name."""

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
    """Version 3, 4, 5, 6, 7, 10, 11, 12, 13, 14, 15, 16, 17, 19, 21, 22, 23 or 26 ``record``
    as it is: the next version only added fields that have defaults."""
    return record


def _per_role(record: dict[str, Any]) -> dict[str, Any]:
    """Version 8 ``record`` with its settings and the planner's conversation, if it had one, as
    the planner's among those of every conversational role."""
    fields = {k: v for k, v in record.items() if k not in ("settings", "planner_conversation")}
    conversation = record.get("planner_conversation")
    conversations = {} if conversation is None else {PLANNER: conversation}
    role_settings = {PLANNER: record.get("settings", {})}
    return fields | {"role_settings": role_settings, "conversations": conversations}


def _on_claude(record: dict[str, Any]) -> dict[str, Any]:
    """Version 9 ``record`` with each conversation's handle as one on Claude, the only runtime
    version 9 ran conversations on. Conversations that aren't an object are left for the
    record's validation to say why."""
    conversations = record.get("conversations", {})
    if not isinstance(conversations, dict):
        return record
    saved = {role: {"runtime": CLAUDE, "handle": handle} for role, handle in conversations.items()}
    return record | {"conversations": saved}


def _origins_as_triggers(record: dict[str, Any]) -> dict[str, Any]:
    """Version 18 ``record`` with each task from the super review, ``super_review``, from the
    trigger that queues it, ``super-review``. Tasks that aren't a list are left for the record's
    validation to say why, as is a task that isn't an object."""
    tasks = record.get("tasks", [])
    if not isinstance(tasks, list):
        return record
    return record | {"tasks": [_origin_as_trigger(task) for task in tasks]}


def _origin_as_trigger(task: object) -> object:
    """``task``, a saved task, from the super-review trigger if it was from ``super_review``."""
    if isinstance(task, dict) and task.get("origin") == _OLD_SUPER_REVIEW_ORIGIN:
        return task | {"origin": SUPER_REVIEW}
    return task


def _reviews_as_booked(record: dict[str, Any]) -> dict[str, Any]:
    """Version 20 ``record`` with each task its review lane booked as a review, one of its
    ``jobs``, queued as a review, whatever its role. A record without reviews, as when they were
    off, books none; tasks that aren't a list, or reviews whose jobs aren't an object, are left
    for the record's validation to say why, as is a task that isn't an object."""
    reviews = record.get("reviews")
    tasks = record.get("tasks", [])
    jobs = reviews.get("jobs", {}) if isinstance(reviews, dict) else {}
    if not isinstance(tasks, list) or not isinstance(jobs, dict):
        return record
    booked = tuple(jobs)
    return record | {"tasks": [_booked(task, booked) for task in tasks]}


def _merges_as_booked(record: dict[str, Any]) -> dict[str, Any]:
    """Version 24 ``record`` with the merge passes of each review group that raised
    ``reviews_finished`` (see :func:`_with_merges`). Reviews, groups or batches that can't be
    read are left for the record's validation to say why."""
    reviews = record.get("reviews")
    if not isinstance(reviews, dict):
        return record
    groups, batches = reviews.get("groups", {}), reviews.get("batches", {})
    if not isinstance(groups, dict) or not isinstance(batches, dict):
        return record
    merged = {batch: _with_merges(group, batch, batches) for batch, group in groups.items()}
    return record | {"reviews": reviews | {"groups": merged}}


def _with_merges(group: object, batch: str, batches: dict[str, Any]) -> object:
    """``group``, the saved group of the review behind ``batch``, with its merge passes, once it
    raised ``reviews_finished``: the tasks ``batches`` books into it that aren't its lenses."""
    if not isinstance(group, dict) or group.get("stage") != "merging":
        return group
    lenses = group.get("lenses", [])
    if not isinstance(lenses, list):
        return group
    return group | {"merges": _booked_besides(batch, batches, lenses)}


def _booked_besides(batch: str, batches: dict[str, Any], lenses: list[Any]) -> list[str]:
    """The tasks ``batches`` books into ``batch`` that aren't among ``lenses``, in order."""
    booked = [task for task, into in batches.items() if into == batch]
    return [task for task in booked if task not in lenses]


def _old_reviews_cancelled(record: dict[str, Any]) -> dict[str, Any]:
    """Version 25 ``record`` with each task queued as a review that hadn't started cancelled,
    for :data:`~orbit.agent.domain.endings.QUEUED_BEFORE_LENSES`: its prompt is for the
    reviewer as it was, before lens reviewers. Tasks that aren't a list are left for the
    record's validation to say why, as is a task that isn't an object."""
    tasks = record.get("tasks", [])
    if not isinstance(tasks, list):
        return record
    return record | {"tasks": [_cancelled_if_waiting(task) for task in tasks]}


def _cancelled_if_waiting(task: object) -> object:
    """``task``, a saved task, cancelled if it's a review still to start."""
    if not isinstance(task, dict) or not _review_to_start(task):
        return task
    return _cancelled(task, QUEUED_BEFORE_LENSES)


def _cancelled(task: dict[str, Any], reason: str) -> dict[str, Any]:
    """``task``, a saved task, cancelled for ``reason``."""
    return task | {"status": "cancelled", "reason": reason, "reason_detail": ""}


def _review_to_start(task: dict[str, Any]) -> bool:
    """Whether ``task``, a saved task, is a review orbit queued that hasn't started."""
    return task.get("queued_as") == "review" and _to_start(task)


def _to_start(task: dict[str, Any]) -> bool:
    """Whether ``task``, a saved task, hasn't started."""
    status: object = task.get("status", "pending")
    return status == "pending"


def _retired_cancelled(record: dict[str, Any]) -> dict[str, Any]:
    """``record``, of the current version, with each task still to start for a role of
    :data:`~orbit.agent.domain.roles.RETIRED_ROLES` cancelled, for
    :data:`~orbit.agent.domain.endings.QUEUED_FOR_RETIRED_ROLE`: no agent can take it. Whatever
    version saved it, a record of the current one may hold such a task, so it isn't a
    migration. Tasks that aren't a list are left for the record's validation to say why, as is
    a task that isn't an object."""
    tasks = record.get("tasks", [])
    if not isinstance(tasks, list):
        return record
    return record | {"tasks": [_cancelled_if_retired(task) for task in tasks]}


def _cancelled_if_retired(task: object) -> object:
    """``task``, a saved task, cancelled if it's still to start for a retired role."""
    if not isinstance(task, dict) or not _to_start(task) or _role_of(task) not in RETIRED_ROLES:
        return task
    return _cancelled(task, QUEUED_FOR_RETIRED_ROLE)


def _role_of(task: dict[str, Any]) -> object:
    """The role of ``task``, a saved task, as it was saved; none if it can't be read."""
    spec = task.get("task")
    return spec.get("role") if isinstance(spec, dict) else None


def _booked(task: object, booked: tuple[str, ...]) -> object:
    """``task``, a saved task, queued as a review if its id is one of those ``booked``."""
    if isinstance(task, dict) and task.get("id") in booked:
        return task | {"queued_as": "review"}
    return task


def _levels_from_tool_access(record: dict[str, Any]) -> dict[str, Any]:
    """Version 27 ``record`` with the settings of each conversational role, and of each task,
    those waiting included, at the workspace level and with the web access their tool access
    gives. Parts that can't be read are left for the record's validation to say why."""
    return record | {
        "role_settings": _with_roles_levels(record.get("role_settings", {})),
        "tasks": _with_each_leveled(record.get("tasks", []), _with_task_levels),
        "waiting": _with_each_leveled(record.get("waiting", []), _with_waiting_levels),
    }


def _with_roles_levels(role_settings: object) -> object:
    """``role_settings``, saved settings by role, each leveled, if they're an object."""
    if not isinstance(role_settings, dict):
        return role_settings
    return {role: _leveled(each) for role, each in role_settings.items()}


def _with_each_leveled(saved: object, leveled: Callable[[object], object]) -> object:
    """``saved``, a saved list, with each of its items as ``leveled`` gives it, if it's a
    list."""
    if not isinstance(saved, list):
        return saved
    return [leveled(item) for item in saved]


def _with_waiting_levels(waiting: object) -> object:
    """``waiting``, saved work waiting for merges, with its task's settings leveled."""
    if not isinstance(waiting, dict) or "state" not in waiting:
        return waiting
    return waiting | {"state": _with_task_levels(waiting["state"])}


def _with_task_levels(task: object) -> object:
    """``task``, a saved task, with its settings leveled."""
    spec = task.get("task") if isinstance(task, dict) else None
    if not isinstance(task, dict) or not isinstance(spec, dict) or "settings" not in spec:
        return task
    return task | {"task": spec | {"settings": _leveled(spec["settings"])}}


def _leveled(settings: object) -> object:
    """``settings``, saved settings, at the workspace level and with the web access their tool
    access gives, if it's one there is."""
    levels = (
        levels_of_tool_access(settings.get("tool_access")) if isinstance(settings, dict) else None
    )
    if not isinstance(settings, dict) or levels is None:
        return settings
    level, web = levels
    return settings | {"workspace_level": level, "web": web}


# By version N: what takes a record of version N and returns its fields in version N+1's shape.
# The upgrade sets the version for it. Version 3 dropped the planner's rounds through a migration
# rather than by having the snapshot ignore them, so that the format stays pinned by its fixtures
# and the snapshot model keeps forbidding fields it doesn't know. Version 4 added the reasons tasks
# ended for, version 5 the tasks each review reviews, version 6 the titles the review planner
# has suggested, version 7 the notes the planner is still to hear, and version 8 the effort in
# the session's and its tasks' settings; their versions stop an older orbit, which forbids fields
# it doesn't know, at a clear "written by a newer orbit" rather than a field it can't read.
# Version 9 keeps the settings and conversation of each conversational role, not just the
# planner's, so its migration moves them into place. Version 10 keeps each conversation with the
# runtime it ran on, as only that runtime's launcher understands its handle, so a handle can't
# be misread; its migration puts version 9's bare handles on Claude. Version 11 added the credits
# in what the session and its tasks spent, which an older record spent none of. Version 12
# added the copies the conversational roles work in, which an older record has none of, and
# version 13 the consultations pending, which an older record has none of. Version 14 added
# what each role spent and caused its consultants to spend, which an older record has none of,
# and version 15 the role that proposed each draft, which an older record's tasks read as none.
# Version 16 added whether that role edited it, which an older record's drafts read as not,
# and version 17 the number of the last consultation asked, which an older record asked none of.
# Version 18 added the skills each task names, which an older record's tasks name none of.
# Version 19 names each task's origin by the trigger that queued it, so a super review's
# suggestions are from ``super-review`` rather than ``super_review``: its migration renames them.
# Version 20 added the template and values each review of commits is rendered from, which an
# older record keeps none of, so its reviews start with their prompts as they were queued.
# Version 21 added what orbit queued each task as, so its reviews are told by their booking
# rather than their role: its migration queues each task an older record's review lane booked,
# one of its jobs, as a review, and every other as a task. Version 22 added the verified work
# waiting in the coder's tree for merges to resume, which an older record has none of. Version
# 23 added the subjects of each piece of work's commits, which find them after a rebase; an
# older record doesn't know them, so its work is told of as landing with the landing's last
# commit. Version 24 added the groups of the reviews under way, with their lenses' findings; an
# older record has none, as each of its reviews is one task, which ends it as it ends. Version 25
# added each group's merge passes, which its migration finds among the tasks booked into a group
# that raised ``reviews_finished``, and the severities merge passes filed tasks at, which an
# older record filed none at. Version 26 is the first whose reviews are lens reviewers' and the
# merge pass's: its migration cancels the reviews an older record had waiting, whose prompts are
# for the reviewer as it was, and the session opened ends them (see
# :func:`reviews_cancelled_at_upgrade`). Version 27 added the named set whose report a review
# reviews, which an older record's reviews, of commits or the codebase, review none of.
# Version 28 added the workspace level and web access of each setting, in place of the tool
# access it keeps for older peers: its migration gives each the level and web access its tool
# access maps to. Version 29 added the number of the last agent's call held for approval, which
# an older record held none of. Version 30 changed nothing in the record but when it's saved:
# with the protocol's ``TaskChanged``, a turn's usage is saved with its task's next change rather
# than at once, so a record may count a running task's latest turns short.
_MIGRATIONS: dict[int, Migration] = {
    1: _spent_by_tasks,
    2: _without_rounds,
    3: _unchanged,
    4: _unchanged,
    5: _unchanged,
    6: _unchanged,
    7: _unchanged,
    8: _per_role,
    9: _on_claude,
    10: _unchanged,
    11: _unchanged,
    12: _unchanged,
    13: _unchanged,
    14: _unchanged,
    15: _unchanged,
    16: _unchanged,
    17: _unchanged,
    18: _origins_as_triggers,
    19: _unchanged,
    20: _reviews_as_booked,
    21: _unchanged,
    22: _unchanged,
    23: _unchanged,
    24: _merges_as_booked,
    25: _old_reviews_cancelled,
    26: _unchanged,
    27: _levels_from_tool_access,
    28: _unchanged,
    29: _unchanged,
}

EMPTY_NAME = "A session needs a name."
OPEN_UNDELETABLE = "Can't delete the open session."
OPEN_UNRENAMABLE = "Can't rename the session as a saved one: it was opened meanwhile."


class SavedConversation(BaseModel):
    """A conversational role's conversation as a session saves it: the ``runtime`` it ran on, by
    name, and the ``handle`` that runtime's launcher carries it on by."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime: str = Field(min_length=1)
    handle: str


class SavedCopy(BaseModel):
    """The copy a conversational role works in, as a session saves it: its ``path``, and when
    the role's conversation last took a turn there (``last_turn``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    last_turn: AwareDatetime


class SavedConsultation(BaseModel):
    """A consultation pending as a session saves it: consultation ``number``, the role ``asker``
    asking ``consultant`` the message titled ``title``, and why it's interrupted as the session
    opens again (``why``): orbit restarted, unless it was left for another session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int
    asker: str = Field(min_length=1)
    consultant: str = Field(min_length=1)
    title: str
    why: str = RESTARTED


class SavedWaiting(BaseModel):
    """Verified work waiting in the coder's tree for merges to resume, as a session saves it:
    the ``count`` of commits of the task that ended as ``state``, whose id it goes by, and
    their ``subjects``, oldest first, which find them after a rebase; none if they aren't
    known, as before version 23."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: TaskState
    count: int = Field(ge=1)
    subjects: tuple[str, ...] = ()


class SessionRecord(BaseModel):
    """A saved session ``id``, ``name``d by the user, with the settings each conversational role
    uses, by role (``role_settings``), the planner's for new tasks too, each one's conversation
    (``conversations``), the copies those working in one work in (``copies``), the consultations
    pending (``consultations``) and the number of the last asked
    (``last_consultation``), the number of the last agent's call held for approval
    (``last_tool_call``), the ``planner_notes`` the planner is still to hear, its
    ``tasks``, the work of theirs ``waiting`` in the coder's tree for merges to resume, in the
    order it lands, and, when reviews are on, its ``reviews``' state, and what its agents have
    ``spent`` in all, the planner and tasks since removed included: by role (``role_spent``),
    and what each role caused its consultants to spend, by asker then consultant
    (``caused_spent``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = FORMAT_VERSION
    id: str = Field(min_length=1)
    name: str
    created: AwareDatetime
    last_active: AwareDatetime
    role_settings: dict[str, Settings] = Field(default={})
    conversations: dict[str, SavedConversation] = Field(default={})
    copies: dict[str, SavedCopy] = Field(default={})
    consultations: tuple[SavedConsultation, ...] = ()
    last_consultation: int = Field(default=0, ge=0)
    last_tool_call: int = Field(default=0, ge=0)
    planner_notes: tuple[str, ...] = ()
    tasks: tuple[TaskState, ...] = ()
    waiting: tuple[SavedWaiting, ...] = ()
    reviews: LaneSnapshot | None = None
    spent: Usage = Usage()
    role_spent: dict[str, Usage] = Field(default={})
    caused_spent: dict[str, dict[str, Usage]] = Field(default={})


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
    """The record in JSON ``text``, upgraded from an older format if need be, its tasks still to
    start for a retired role cancelled, or why it's unreadable."""
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
    """``data`` in the current format, its tasks still to start for a retired role cancelled,
    or why its version can't be read."""
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
    return _retired_cancelled(reduce(_migrated, range(version, FORMAT_VERSION), data))


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


def reviews_cancelled_at_upgrade(record: SessionRecord) -> tuple[str, ...]:
    """The ids of ``record``'s reviews that an upgrade cancelled, to lens reviewers or past a
    retired role such as the super-reviewer, and that haven't ended in its review lane yet, as
    their batches are still open: those the session opened still has to end, as if they
    suggested nothing."""
    reviews = record.reviews
    if reviews is None:
        return ()
    return tuple(
        state.id
        for state in record.tasks
        if state.reason in CANCELLED_AT_UPGRADE
        and reviews.batches.get(state.id) in reviews.planner.batches
    )


def unreviewed_notes(record: SessionRecord, cancelled: Sequence[str]) -> tuple[str, ...]:
    """What to tell the user of ``record``'s reviews ``cancelled`` at the upgrade to lens
    reviewers: the commits they leave unreviewed, abbreviated, each once; nothing if none were
    cancelled."""
    if not cancelled or record.reviews is None:
        return ()
    jobs = record.reviews.jobs
    commits = dict.fromkeys(commit[:7] for task in cancelled for commit in jobs[task].commits)
    return (
        f"Reviews queued before lens reviewers were cancelled; unreviewed: {', '.join(commits)}.",
    )


def runtime_changed(conversation: SavedConversation | None, runtime: str) -> bool:
    """Whether a role's saved ``conversation``, if it has one, ran on another runtime than
    ``runtime``, the one its role runs on now, so it can't be carried on."""
    return conversation is not None and conversation.runtime != runtime


def runtime_changed_message(role: str, saved: str, runtime: str) -> str:
    """Why the conversational role ``role`` can't be messaged: its saved conversation is from the
    runtime ``saved``, and it now runs on ``runtime``."""
    return (
        f"The {role}'s saved conversation is from {saved}, and it now runs on {runtime}; "
        f"reset it (ctrl+n on its tab) to start one on {runtime}."
    )


def resumed_settings(saved: Settings, given: Settings, pinned: Collection[str]) -> Settings:
    """The settings to resume a session ``saved`` with: those settings, except the ones
    ``pinned``, by field name, which are as ``given``."""
    return saved.updated({name: getattr(given, name) for name in pinned})


def imported_note(count: int, sessions: str, folder: str) -> str:
    """The note that ``count`` sessions saved as files in the folder ``sessions`` were imported,
    and that the folder is now named ``folder``."""
    return (
        f"Imported {count} saved {noun(count, 'session')} from {sessions}; "
        f"the old folder is now {folder}."
    )
