"""The formats of what orbit keeps per repository, beside its saved sessions: the super review
count, which files each super review sampled, the holds on merges, and when each ``schedule``
trigger last fired. None of them is part of a session record, so none is versioned with its
format."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal, assert_never

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    TypeAdapter,
    ValidationError,
)

from orbit.agent.domain.merge_holds import AnyHold, FindingHold, Hold
from orbit.agent.domain.reviews import SuperReviewCount

_SAMPLED: TypeAdapter[dict[str, int]] = TypeAdapter(dict[str, NonNegativeInt])
"""The number of the super review that last sampled each file, by path."""

_FIRED: TypeAdapter[dict[str, datetime]] = TypeAdapter(dict[str, AwareDatetime])
"""The time each ``schedule`` trigger last fired, by name."""


class _ReviewCount(BaseModel):
    """The commits since the last super review, and the commit it reviewed (none before the
    first, nor in a count saved before it was kept), with the super reviews so far and the
    number of the one that last sampled each file (none, and nothing, in a count saved before
    they were kept), kept per repository."""

    model_config = ConfigDict(frozen=True)

    since_super: int = Field(ge=0)
    last_super_commit: str | None = None
    super_reviews: int = Field(default=0, ge=0)
    sampled: dict[str, NonNegativeInt] = {}


class _SavedHold(BaseModel):
    """A named set's hold on merges, kept per repository: the set, the commit it failed on, the
    step that failed with its output, and the fixes queued for it. A hold saved before findings
    held merges has no ``kind``, and is a set's."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["set"] = "set"
    name: str
    commit: str
    step: str
    output: str
    fixes: int = Field(ge=0)


class _SavedFindingHold(BaseModel):
    """A blocking finding's hold on merges, kept per repository: the lens that raised it, the
    commit reviewed, the title of the task it was filed as, its latest fix and that fix's batch,
    none once no review can check it, and the fixes queued for it."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["finding"]
    lens: str
    commit: str
    title: str
    task: str
    batch: str | None
    fixes: int = Field(ge=0)


_HOLDS: TypeAdapter[tuple[_SavedHold | _SavedFindingHold, ...]] = TypeAdapter(
    tuple[_SavedHold | _SavedFindingHold, ...]
)
"""The holds on merges, in the order they started."""


def parse_review_count(text: str) -> SuperReviewCount:
    """The super review count in JSON ``text``; nothing counted when it's unreadable."""
    try:
        count = _ReviewCount.model_validate_json(text)
    except ValidationError:
        return SuperReviewCount()
    return SuperReviewCount(
        count.since_super, count.last_super_commit, count.super_reviews, count.sampled
    )


def merge_holds_json(holds: Sequence[AnyHold]) -> str:
    """``holds``, the holds on merges, the named sets' and the blocking findings', as JSON."""
    return _HOLDS.dump_json(tuple(_saved_hold(hold) for hold in holds)).decode()


def _saved_hold(hold: AnyHold) -> _SavedHold | _SavedFindingHold:
    """``hold`` as it's kept."""
    match hold:
        case Hold():
            return _SavedHold(
                name=hold.name,
                commit=hold.commit,
                step=hold.step,
                output=hold.output,
                fixes=hold.fixes,
            )
        case FindingHold():
            return _SavedFindingHold(
                kind="finding",
                lens=hold.lens,
                commit=hold.commit,
                title=hold.title,
                task=hold.task,
                batch=hold.batch,
                fixes=hold.fixes,
            )
        case _:
            assert_never(hold)


def parse_merge_holds(text: str) -> tuple[AnyHold, ...]:
    """The holds on merges in JSON ``text``, those saved before findings held merges read as the
    named sets' they were; none when it's unreadable."""
    try:
        saved = _HOLDS.validate_json(text)
    except ValidationError:
        return ()
    return tuple(_kept_hold(each) for each in saved)


def _kept_hold(saved: _SavedHold | _SavedFindingHold) -> AnyHold:
    """The hold ``saved`` keeps."""
    match saved:
        case _SavedHold():
            return Hold(saved.name, saved.commit, saved.step, saved.output, saved.fixes)
        case _SavedFindingHold():
            return FindingHold(
                saved.lens, saved.commit, saved.title, saved.task, saved.batch, saved.fixes
            )
        case _:
            assert_never(saved)


def sampled_json(sampled: Mapping[str, int]) -> str:
    """``sampled``, the number of the super review that last sampled each file, by path, as
    JSON."""
    return _SAMPLED.dump_json(dict(sampled)).decode()


def fired_json(fired: Mapping[str, datetime]) -> str:
    """``fired``, the time each ``schedule`` trigger last fired, by name, as JSON."""
    return _FIRED.dump_json(dict(fired)).decode()


def parse_fired(text: str) -> dict[str, datetime]:
    """The time each ``schedule`` trigger last fired, by name, in JSON ``text``; none when it's
    unreadable, so each trigger is seen afresh and fires at its next time."""
    try:
        return _FIRED.validate_json(text)
    except ValidationError:
        return {}


def parse_sampled(text: str) -> dict[str, int]:
    """The number of the super review that last sampled each file, by path, in JSON ``text``;
    none when it's unreadable, so the rotation starts again."""
    try:
        return _SAMPLED.validate_json(text)
    except ValidationError:
        return {}
