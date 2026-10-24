"""Following what the backend makes of the commands sent: what to do once each is accepted or
refused, by command id, or for only the latest of a kind where a newer one supersedes it."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from orbit.protocol import Command


def _nothing() -> None:
    """Does nothing, for an outcome nothing awaits."""


def _ignored(_code: str) -> None:
    """Does nothing with a refusal's code, when nothing awaits it."""


@dataclass(frozen=True)
class Awaited:
    """What to do once a command is ``accepted``, and once it's ``refused`` or isn't sent,
    after which ``why``, if given, takes the message saying why, in place of the conversation
    showing it, and ``code`` the refusal's code; when it ``supersedes``, it's awaited in place
    of any earlier command of its kind that does too."""

    accepted: Callable[[], None] = _nothing
    refused: Callable[[], None] = _nothing
    why: Callable[[str], None] | None = None
    code: Callable[[str], None] = _ignored
    supersedes: bool = False


@dataclass(frozen=True)
class Outcomes:
    """The commands whose outcome is ``awaited``, by command id, each with its kind and what
    awaits it."""

    awaited: Mapping[str, tuple[str, Awaited]] = field(default_factory=dict)


def awaiting(outcomes: Outcomes, command_id: str, kind: str, awaited: Awaited) -> Outcomes:
    """``outcomes`` also awaiting the outcome of ``command_id``, a command of the ``kind``, as
    ``awaited``; one that supersedes takes the place of any earlier superseding command of that
    kind, so only the latest counts."""
    kept = {
        key: entry
        for key, entry in outcomes.awaited.items()
        if not (awaited.supersedes and entry[1].supersedes and entry[0] == kind)
    }
    return Outcomes({**kept, command_id: (kind, awaited)})


def settled(outcomes: Outcomes, command_id: str) -> tuple[Outcomes, Awaited | None]:
    """``outcomes`` no longer awaiting ``command_id``'s outcome, and what awaited it; none
    when nothing does."""
    entry = outcomes.awaited.get(command_id)
    if entry is None:
        return outcomes, None
    rest = {key: other for key, other in outcomes.awaited.items() if key != command_id}
    return Outcomes(rest), entry[1]


Send = Callable[[Command, Awaited], Awaitable[object]]
"""Sends a command to the backend, awaiting its outcome as told; whatever it returns is
ignored."""
