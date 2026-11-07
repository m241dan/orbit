"""Cron expressions, read with cronsim: why one is refused, the next time one comes due in a time
zone, and whether one came due between two times."""

from datetime import datetime, tzinfo

from cronsim import CronSim, CronSimError

CRON_FIELDS = 5
"""The fields of a cron expression: minute, hour, day of month, month and day of week."""

_ANY_TIME = datetime(2000, 1, 1)
"""A time to read a cron expression from as it's checked, and to look for its first time after,
which cronsim gives up on finding after 50 years."""


def cron_problem(cron: str) -> str | None:
    """Why ``cron`` isn't a five-field cron expression that comes due, quoting cronsim's reason,
    if it isn't; none if it is. Cronsim reads six fields too, the first seconds, which orbit
    doesn't take. Cronsim refuses a day of month no month it names has, but not every
    expression that never matches, such as ``0 0 */30 2 1#5``, the 1st or 31st of February
    that's its fifth Monday; looking for one's next time would end the schedules."""
    try:
        times = CronSim(cron, _ANY_TIME)
    except CronSimError as exc:
        return f"isn't a cron expression: {exc}."
    if len(cron.split()) != CRON_FIELDS:
        return "has six fields; cron takes five: minute, hour, day of month, month, day of week."
    return None if next(times, None) is not None else "never comes due."


def next_due(cron: str, after: datetime, zone: tzinfo) -> datetime:
    """The first time after ``after`` that ``cron``, a valid cron expression, comes due, read in
    ``zone``, as cronsim reads it: a time daylight saving skips comes due as the clocks go
    forward, and one it repeats comes due once."""
    return next(CronSim(cron, after.astimezone(zone)))


def falls_due(cron: str, since: datetime, until: datetime, zone: tzinfo) -> bool:
    """Whether ``cron``, a valid cron expression read in ``zone``, comes due after ``since`` and
    by ``until``."""
    return next_due(cron, since, zone) <= until
