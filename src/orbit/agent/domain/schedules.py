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
from typing import assert_never

from orbit.agent.domain.triggers import falls_due, next_due

LONGEST_WAIT = timedelta(minutes=5)
"""The longest a schedule waits before looking at the time again: a machine asleep doesn't count
the time it sleeps in a wait, so a trigger that came due meanwhile fires within this of its
waking."""


@dataclass(frozen=True)
class TimesRead:
    """The times kept have been read: ``fired`` is the time each trigger last fired, or was first
    seen, by name, to keep."""

    fired: dict[str, datetime]


@dataclass(frozen=True)
class TimesUnread:
    """The times kept are yet to be read: ``fired`` is the time each trigger last fired since
    opening, or was first seen, by name, and ``since`` names those that fired, whose times replace
    those kept once they're read; until then nothing is kept, so a failed read never loses
    them."""

    fired: dict[str, datetime]
    since: frozenset[str]


ScheduleTimes = TimesRead | TimesUnread
"""The time each schedule trigger last fired, as far as the schedules know it."""


@dataclass(frozen=True)
class Firing:
    """What a look at the time makes of the schedules: the triggers ``due`` to fire now, by name,
    in order, and the ``times`` each last fired, after."""

    due: tuple[str, ...]
    times: ScheduleTimes


def open_schedules(
    crons: Mapping[str, str], kept: Mapping[str, datetime] | None, now: datetime, zone: tzinfo
) -> Firing:
    """What opening a workspace at ``now`` makes of the schedule triggers' ``crons``, their cron
    expressions read in ``zone``, by name, from the times ``kept`` that each last fired, if they
    could be read: each that came due since its time fires once, its time ``now``; each with no
    time is first seen, its time ``now``, firing nothing; the others keep theirs. Times of
    triggers no longer among ``crons`` are dropped. Times that couldn't be read leave every
    trigger first seen, and the times unread."""
    return wake_schedules(
        crons, TimesUnread(dict.fromkeys(crons, now), frozenset()), kept, now, zone
    )


def wake_schedules(
    crons: Mapping[str, str],
    times: ScheduleTimes,
    kept: Mapping[str, datetime] | None,
    now: datetime,
    zone: tzinfo,
) -> Firing:
    """What a look at the time at ``now`` makes of the schedule triggers' ``crons``, their cron
    expressions read in ``zone``, by name, from the ``times`` each last fired: those come due
    since fire, their time ``now``. While the times are unread, the times ``kept``, once read,
    are merged in first, as opening does, each kept time standing for a trigger unfired since
    opening, and the times are read after; ``kept`` is otherwise ignored."""
    match times:
        case TimesUnread() if kept is not None:
            return _fire_read(crons, _merge_kept(crons, times, kept), now, zone)
        case TimesUnread():
            due = due_triggers(crons, times.fired, now, zone)
            fired = fire_due(times.fired, due, now)
            return Firing(due, TimesUnread(fired, times.since | frozenset(due)))
        case TimesRead():
            return _fire_read(crons, times.fired, now, zone)
        case _:
            assert_never(times)


def _fire_read(
    crons: Mapping[str, str], fired: Mapping[str, datetime], now: datetime, zone: tzinfo
) -> Firing:
    """The triggers among ``crons`` come due by ``now`` since each last ``fired``, and the times
    after they fire, read."""
    due = due_triggers(crons, fired, now, zone)
    return Firing(due, TimesRead(fire_due(fired, due, now)))


def _merge_kept(
    crons: Mapping[str, str], times: TimesUnread, kept: Mapping[str, datetime]
) -> dict[str, datetime]:
    """The time each trigger among ``crons`` last fired, by name, in their order: the time
    ``kept`` for each that has one and hasn't fired since opening, else its time in ``times``."""
    return {
        name: kept[name] if name in kept and name not in times.since else times.fired[name]
        for name in crons
    }


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
