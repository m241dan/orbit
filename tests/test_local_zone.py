"""The machine's local time zone: with ``TZ`` set to New York, it places every time as
:mod:`zoneinfo`'s New York does, through both of a year's daylight saving changes, so a cron
expression read in it comes due as it does in New York."""

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from orbit.agent.domain.triggers import next_due
from orbit.agent.external.local_zone import LocalZone

NEW_YORK = ZoneInfo("America/New_York")
SPRING = datetime(2026, 3, 8, 4, tzinfo=UTC)
AUTUMN = datetime(2026, 11, 1, 3, tzinfo=UTC)
"""Hours before New York's clocks go forward, and back, in 2026."""


@pytest.fixture(autouse=True)
def in_new_york(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The C library's local time zone, New York's, for the test, then the machine's again."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def hours_around(start: datetime) -> list[datetime]:
    """Each half hour for the six hours after ``start``."""
    return [start + timedelta(minutes=30 * half) for half in range(12)]


def placed(when: datetime) -> tuple[datetime, timedelta | None, int]:
    """``when``'s wall time, offset and fold."""
    return when.replace(tzinfo=None), when.utcoffset(), when.fold


@pytest.mark.parametrize("instant", [*hours_around(SPRING), *hours_around(AUTUMN)])
def test_a_time_is_placed_as_new_york_places_it(instant: datetime) -> None:
    assert placed(instant.astimezone(LocalZone())) == placed(instant.astimezone(NEW_YORK))


@pytest.mark.parametrize("fold", [0, 1])
@pytest.mark.parametrize(
    "wall", [datetime(2026, 11, 1, 1, 30), datetime(2026, 3, 8, 2, 30), datetime(2026, 7, 1, 9)]
)
def test_a_local_time_is_the_instant_new_york_makes_it(wall: datetime, fold: int) -> None:
    local = wall.replace(tzinfo=LocalZone(), fold=fold)
    assert local.astimezone(UTC) == wall.replace(tzinfo=NEW_YORK, fold=fold).astimezone(UTC)


def test_a_cron_expression_read_locally_comes_due_as_in_new_york() -> None:
    before = datetime(2026, 3, 8, 1, tzinfo=NEW_YORK)
    due = next_due("30 2 * * *", before, LocalZone())
    assert due.astimezone(UTC) == next_due("30 2 * * *", before, NEW_YORK).astimezone(UTC)


def test_the_zone_names_the_offset_in_force() -> None:
    assert datetime(2026, 1, 1, tzinfo=LocalZone()).tzname() == "EST"
    assert datetime(2026, 7, 1, tzinfo=LocalZone()).tzname() == "EDT"
    assert datetime(2026, 7, 1, tzinfo=LocalZone()).dst() is None


def test_without_a_time_the_zone_gives_the_offset_now() -> None:
    zone = LocalZone()
    assert zone.utcoffset(None) == datetime.now().astimezone().utcoffset()
    assert zone.tzname(None) == datetime.now().astimezone().tzname()
    assert repr(zone) == "LocalZone()"
