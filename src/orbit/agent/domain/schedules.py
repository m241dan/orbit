"""Schedules: which ``schedule`` triggers fire as a workspace opens, for the times they came due
while it was closed, which fire as the time comes, and how long to wait for the next, from the
time each last fired.

A trigger fires once for any number of times it came due unseen, so runs don't pile up, and one
orbit has never seen fires first at its next time, never for times before orbit knew it
(decisions 13 and 27). Each trigger's time is the time it last fired, or was first seen, by
which every time it came due has been dealt with.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from orbit.agent.domain.triggers import falls_due, next_due

LONGEST_WAIT = timedelta(minutes=5)
"""The longest a schedule waits before looking at the time again: a machine asleep doesn't count
the time it sleeps in a wait, so a trigger that came due meanwhile fires within this of its
waking."""


@dataclass(frozen=True)
class Opened:
    """What opening a workspace makes of its schedules: the triggers ``due`` to fire once now, by
    name, in order, as each came due while it was closed, and the time each trigger last
    ``fired``, by name, after."""

    due: tuple[str, ...]
    fired: dict[str, datetime]


def open_schedules(
    crons: Mapping[str, str], kept: Mapping[str, datetime], now: datetime, zone: tzinfo
) -> Opened:
    """What opening a workspace at ``now`` makes of the schedule triggers' ``crons``, their cron
    expressions read in ``zone``, by name, from the times ``kept`` that each last fired: each
    that came due since its time fires once, its time ``now``; each with no time is first seen,
    its time ``now``, firing nothing; the others keep theirs. Times of triggers no longer among
    ``crons`` are dropped."""
    known = _known_times(crons, kept)
    due = tuple(name for name, last in known.items() if falls_due(crons[name], last, now, zone))
    fired = dict.fromkeys(crons, now) | {
        name: last for name, last in known.items() if name not in due
    }
    return Opened(due, fired)


def _known_times(crons: Mapping[str, str], kept: Mapping[str, datetime]) -> dict[str, datetime]:
    """The times ``kept`` of the triggers among ``crons``, by name, in their order."""
    return {name: kept[name] for name in crons if name in kept}


def due_triggers(
    crons: Mapping[str, str], fired: Mapping[str, datetime], now: datetime, zone: tzinfo
) -> tuple[str, ...]:
    """The schedule triggers among ``crons``, their cron expressions read in ``zone``, by name,
    in order, that came due by ``now`` since each last ``fired``."""
    return tuple(name for name, cron in crons.items() if falls_due(cron, fired[name], now, zone))


def fire_due(
    fired: Mapping[str, datetime], due: tuple[str, ...], now: datetime
) -> dict[str, datetime]:
    """``fired``, the time each schedule trigger last fired, by name, with those ``due`` firing
    ``now``."""
    return {**fired, **dict.fromkeys(due, now)}


def wait_for_due(
    crons: Mapping[str, str], fired: Mapping[str, datetime], now: datetime, zone: tzinfo
) -> timedelta:
    """How long after ``now`` to look at the time again: until the first of ``crons``, the
    schedule triggers' cron expressions read in ``zone``, by name, comes due after it last
    ``fired``, but never longer than :data:`LONGEST_WAIT`, nor less than nothing."""
    first = min(next_due(cron, fired[name], zone) for name, cron in crons.items())
    return max(timedelta(), min(first - now, LONGEST_WAIT))
