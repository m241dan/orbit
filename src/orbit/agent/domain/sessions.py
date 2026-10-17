"""Saved sessions: stretches of work the user can come back to.

A saved session keeps its task list, its review state, the planner's conversation and the
settings it ran with. It is unrelated to the backend's connection, :class:`AgentSession`. The
caller reads and writes the files and the clock; these rules only shape what's in them, and
decide when a session's lock, held by the orbit that has it open, may be taken over.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import reduce
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import from_json

from orbit.agent.domain.reviews import LaneSnapshot
from orbit.protocol import SessionSummary, Settings, TaskState, Usage

FORMAT_VERSION = 1
"""The version of the record format this orbit writes, and the newest it reads."""

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

Migration = Callable[[dict[str, Any]], dict[str, Any]]

# By version: what upgrades a record of that version to the next. None are needed yet.
_MIGRATIONS: dict[int, Migration] = {}


class SessionRecord(BaseModel):
    """A saved session ``id``, ``name``d by the user, with the ``settings`` its planner and new
    tasks use, the planner's Claude conversation, its ``tasks`` and, when reviews are on, its
    ``reviews``' state."""

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


class _ReviewCount(BaseModel):
    """The commits since the last super review, kept per repository."""

    model_config = ConfigDict(frozen=True)

    since_super: int = Field(ge=0)


@dataclass(frozen=True)
class Unreadable:
    """A record that can't be read, for ``reason``."""

    reason: str


def default_name(when: datetime) -> str:
    """The name of a session started at ``when``, such as ``Oct 17 14:30``."""
    return f"{_MONTHS[when.month - 1]} {when.day} {when:%H:%M}"


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
    migrations = (_MIGRATIONS[old] for old in range(version, FORMAT_VERSION))
    return reduce(lambda record, migrate: migrate(record), migrations, data)


def _first_problem(error: ValidationError) -> str:
    """The first thing wrong with a record, such as ``tasks.0.task: Field required``."""
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    return f"{where}: {first['msg']}"


def summarize(record: SessionRecord, current: bool = False, locked: bool = False) -> SessionSummary:
    """``record`` as the list of sessions shows it; ``current`` if it's the session open here,
    ``locked`` if another orbit has it open."""
    return SessionSummary(
        id=record.id,
        name=record.name,
        last_active=record.last_active,
        counts=dict(Counter(task.status for task in record.tasks)),
        usage=sum((task.usage for task in record.tasks), Usage()),
        current=current,
        locked=locked,
    )


def lock_owner(text: str) -> int | None:
    """The process id a session lock's ``text`` names; ``None`` when it isn't one."""
    stripped = text.strip()
    if not (stripped.isascii() and stripped.isdecimal()):
        return None
    pid = int(stripped)
    return pid if pid > 0 else None


def lock_is_stale(text: str, alive: Callable[[int], bool]) -> bool:
    """Whether a session lock reading ``text`` may be taken over: it names no process, or one
    that ``alive`` says has ended."""
    owner = lock_owner(text)
    return owner is None or not alive(owner)


def review_count_json(count: int) -> str:
    """``count`` commits since the last super review as JSON text, for
    :func:`parse_review_count` to read back."""
    return _ReviewCount(since_super=count).model_dump_json()


def parse_review_count(text: str) -> int:
    """The commits since the last super review in JSON ``text``; 0 when it's unreadable."""
    try:
        return _ReviewCount.model_validate_json(text).since_super
    except ValidationError:
        return 0
