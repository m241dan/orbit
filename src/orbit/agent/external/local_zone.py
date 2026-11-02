"""The machine's local time zone, as the C library tells it, daylight saving included, which the
``schedule`` triggers' cron expressions are read in."""

from datetime import UTC, datetime, timedelta, tzinfo

_DAY = timedelta(days=1)
"""How far either side of a local time to look for the offsets that may hold at it: the clocks
change at most once in so short a time."""


class LocalZone(tzinfo):
    """The machine's local time zone: each instant's offset is the one the C library gives it,
    as ``TZ`` or the system's setting says, so it follows daylight saving, unlike the fixed
    offset ``datetime.astimezone()`` gives a time. A local time the clocks repeat, or skip,
    takes its offset by its ``fold``, as :mod:`zoneinfo`'s zones do (PEP 495): the offset before
    the change, or with ``fold=1`` the one after."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return _offset_at(datetime.now(UTC).replace(tzinfo=None))
        return _offset_of(dt.replace(tzinfo=None), dt.fold)

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None  # The C library tells the offset whole, not its daylight saving part.

    def tzname(self, dt: datetime | None) -> str | None:
        instant = datetime.now(UTC) if dt is None else dt.astimezone(UTC)
        return instant.astimezone().tzname()

    def fromutc(self, dt: datetime) -> datetime:
        local = dt.replace(tzinfo=UTC).astimezone()
        wall = local.replace(tzinfo=None)
        # A time the clocks repeat is first at the offset before the change; a later one folds.
        fold = int(_offset_of(wall, 0) != local.utcoffset())
        return wall.replace(tzinfo=self, fold=fold)

    def __repr__(self) -> str:
        return "LocalZone()"


def _offset_at(instant: datetime) -> timedelta:
    """The offset the C library gives ``instant``, a naive UTC time."""
    return instant.replace(tzinfo=UTC).astimezone().utcoffset() or timedelta()


def _offset_of(wall: datetime, fold: int) -> timedelta:
    """The offset of ``wall``, a naive local time: of the offsets in force a day either side,
    the one that places it, or, if both or neither do, as the clocks repeat or skip it, the
    one before the change, or with ``fold`` the one after."""
    before, after = _offset_at(wall - _DAY), _offset_at(wall + _DAY)
    placing = [offset for offset in (before, after) if _offset_at(wall - offset) == offset]
    if len(placing) == 1:
        return placing[0]
    return after if fold else before
