"""Following what the backend makes of the commands sent: what to do once each is accepted or
refused, for the latest command of each kind."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field


def _nothing() -> None:
    """Does nothing, for an outcome nothing awaits."""


def _ignored(_why: str) -> None:
    """Does nothing with why a command was refused, or its code, when nothing awaits it."""


@dataclass(frozen=True)
class Awaited:
    """What to do once a command is ``accepted``, and once it's ``refused`` or isn't sent,
    after which ``why`` takes the message saying why and ``code`` the refusal's code."""

    accepted: Callable[[], None] = _nothing
    refused: Callable[[], None] = _nothing
    why: Callable[[str], None] = _ignored
    code: Callable[[str], None] = _ignored


@dataclass(frozen=True)
class Outcomes:
    """The commands whose outcome is ``awaited``, by command id, each with its kind and what
    awaits it."""

    awaited: Mapping[str, tuple[str, Awaited]] = field(default_factory=dict)


def awaiting(outcomes: Outcomes, command_id: str, kind: str, awaited: Awaited) -> Outcomes:
    """``outcomes`` also awaiting the outcome of ``command_id``, a command of the ``kind``, as
    ``awaited``, in place of any earlier command of that kind: only the latest counts."""
    kept = {key: entry for key, entry in outcomes.awaited.items() if entry[0] != kind}
    return Outcomes({**kept, command_id: (kind, awaited)})


def settled(outcomes: Outcomes, command_id: str) -> tuple[Outcomes, Awaited | None]:
    """``outcomes`` no longer awaiting ``command_id``'s outcome, and what awaited it; none
    when nothing does."""
    entry = outcomes.awaited.get(command_id)
    if entry is None:
        return outcomes, None
    rest = {key: other for key, other in outcomes.awaited.items() if key != command_id}
    return Outcomes(rest), entry[1]
