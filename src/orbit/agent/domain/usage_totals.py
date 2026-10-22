"""The rules for adding up what turns cost: each finished turn's row, and the totals per
workspace and in all, today's and lifetime."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from orbit.protocol import Usage, UsageTotal, UsageTotals, WorkspaceUsage

SessionKey = tuple[str, str]
"""A session: its workspace's id and its own."""


@dataclass(frozen=True)
class UsageRow:
    """What a finished turn cost (``usage``), in the session ``session_id`` of the workspace
    ``workspace_id``, by an agent in the ``role`` on the ``model`` (``None`` for the backend's
    default), finishing ``at`` that time."""

    workspace_id: str
    session_id: str
    role: str
    model: str | None
    usage: Usage
    at: datetime


@dataclass(frozen=True)
class SpendTotals:
    """Tokens of each kind and cost, ``today``'s and over the ``lifetime``."""

    today: Usage = field(default_factory=Usage)
    lifetime: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class UsageTotalsData:
    """The totals of each workspace, by its id, and of them all."""

    workspaces: Mapping[str, SpendTotals]
    total: SpendTotals


def counted(usage: Usage) -> Usage:
    """``usage``'s tokens of each kind and cost, all that a turn's row keeps."""
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cost_usd=usage.cost_usd,
    )


def earlier_spend(spent: Usage, recorded: Usage) -> Usage:
    """What a session ``spent``, as its record says, beyond what its turns' rows have
    ``recorded``: the spend from before turns were recorded, each kind never below zero."""
    mine, theirs = counted(spent).model_dump(), counted(recorded).model_dump()
    return Usage(**{name: max(mine[name] - theirs[name], 0) for name in mine})


def _summed(usages: Iterable[Usage]) -> Usage:
    return sum(usages, Usage())


def usage_totals(
    today: Mapping[str, Usage],
    recorded: Mapping[SessionKey, Usage],
    spent: Mapping[SessionKey, Usage],
) -> UsageTotalsData:
    """The totals per workspace and in all, given each workspace's rows since today began
    (``today``), each session's rows of all time (``recorded``) and each saved session's record's
    ``spent``.

    A workspace's lifetime is all its rows, sessions since deleted included, plus each saved
    session's :func:`earlier_spend`: its record's spend beyond its rows, which is what it spent
    before turns were recorded.
    """
    lifetime: dict[str, Usage] = {}
    for (workspace, _), usage in recorded.items():
        lifetime[workspace] = lifetime.get(workspace, Usage()) + usage
    for key, usage in spent.items():
        earlier = earlier_spend(usage, recorded.get(key, Usage()))
        lifetime[key[0]] = lifetime.get(key[0], Usage()) + earlier
    workspaces = {
        workspace: SpendTotals(today.get(workspace, Usage()), lifetime.get(workspace, Usage()))
        for workspace in sorted({*today, *lifetime})
    }
    total = SpendTotals(
        _summed(totals.today for totals in workspaces.values()),
        _summed(totals.lifetime for totals in workspaces.values()),
    )
    return UsageTotalsData(workspaces, total)


def day_start(now: datetime) -> datetime:
    """When the day ``now`` falls in began: midnight in ``now``'s timezone."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _total(totals: SpendTotals) -> UsageTotal:
    return UsageTotal(today=totals.today, lifetime=totals.lifetime)


def totals_event(totals: UsageTotalsData) -> UsageTotals:
    """The event telling ``totals``: each workspace's, in the order they're given, and the
    total."""
    workspaces = tuple(
        WorkspaceUsage(workspace=workspace, today=spend.today, lifetime=spend.lifetime)
        for workspace, spend in totals.workspaces.items()
    )
    return UsageTotals(workspaces=workspaces, total=_total(totals.total))
