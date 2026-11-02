"""The rules of ``schedule`` triggers: ``cron`` checked with the event, when a cron expression
comes due, read in a time zone daylight saving moves, which triggers fire as a workspace opens
and as their times come, and how long to wait for the next."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.schedules import (
    LONGEST_WAIT,
    Firing,
    TimesRead,
    TimesUnread,
    due_triggers,
    fire_due,
    open_schedules,
    wait_for_due,
    wake_schedules,
)
from orbit.agent.domain.triggers import (
    Trigger,
    check_triggers,
    cron_problem,
    describe_firing,
    falls_due,
    next_due,
    scheduled_crons,
)

NEW_YORK = ZoneInfo("America/New_York")
"""A zone whose clocks went forward on 8 March 2026 and back on 1 November 2026."""

MONDAY_EVENING = datetime(2026, 3, 2, 22, tzinfo=UTC)
TUESDAY_MORNING = datetime(2026, 3, 3, 8, tzinfo=UTC)
"""Monday 22:00 and Tuesday 08:00, a night apart."""

NIGHTLY = "0 3 * * *"
NOON = "0 12 * * *"


def at(hour: int, minute: int = 0, day: int = 3) -> datetime:
    """``hour``:``minute`` on ``day`` March 2026, in UTC."""
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)


def nightly(**fields: object) -> Trigger:
    """A trigger running the mutation set at 3 every night, with ``fields`` over that."""
    return Trigger.model_validate(
        {"on": "schedule", "checks": "mutation", "cron": NIGHTLY, **fields}
    )


def problems(trigger: Trigger) -> str:
    """Why ``trigger``, named ``nightly``, is refused, with the coder its only role and the
    mutation set its only set."""
    with pytest.raises(ConfigError) as raised:
        check_triggers({"nightly": trigger}, ("coder",), ("mutation",))
    return str(raised.value)


# Checking cron


def test_a_schedule_trigger_with_a_cron_expression_passes() -> None:
    check_triggers({"nightly": nightly()}, ("coder",), ("mutation",))


def test_a_schedule_trigger_without_cron_is_refused_naming_it() -> None:
    assert problems(nightly(cron=None)) == "triggers.nightly.cron: a schedule trigger needs cron."


def test_cron_on_another_event_is_refused_naming_the_trigger() -> None:
    assert problems(nightly(on="task_landed")) == (
        'triggers.nightly.cron: cron needs on = "schedule".'
    )


@pytest.mark.parametrize(
    ("cron", "problem"),
    [
        ("nope", "isn't a cron expression: Wrong number of fields."),
        ("61 3 * * *", "isn't a cron expression: Bad minute."),
        ("0 0 31 2 *", "isn't a cron expression: Bad day-of-month."),
        (
            "0 0 3 * * *",
            "has six fields; cron takes five: minute, hour, day of month, month, day of week.",
        ),
    ],
)
def test_an_invalid_cron_expression_is_refused_quoting_why(cron: str, problem: str) -> None:
    assert cron_problem(cron) == problem
    assert problems(nightly(cron=cron)) == f'triggers.nightly.cron: cron = "{cron}" {problem}'


@pytest.mark.parametrize("cron", [NIGHTLY, "*/15 9-17 * * mon-fri", "0 0 1 1 *", "0 3 L * *"])
def test_a_five_field_cron_expression_has_no_problem(cron: str) -> None:
    assert cron_problem(cron) is None


# When a cron expression comes due


@pytest.mark.parametrize(
    ("cron", "after", "due"),
    [
        (NIGHTLY, at(2, 59), at(3)),
        (NIGHTLY, at(3), at(3, day=4)),
        (NIGHTLY, at(3, 0) + timedelta(microseconds=500), at(3, day=4)),
        ("*/15 * * * *", at(8, 1), at(8, 15)),
        ("0 9 * * mon", at(8), datetime(2026, 3, 9, 9, tzinfo=UTC)),
    ],
)
def test_the_next_due_time_is_the_first_after_the_time_given(
    cron: str, after: datetime, due: datetime
) -> None:
    assert next_due(cron, after, UTC) == due


def test_a_cron_expression_is_read_in_the_zone_given() -> None:
    assert next_due(NIGHTLY, at(2, 59), NEW_YORK) == at(8)  # 03:00 in New York, in winter.


@pytest.mark.parametrize(
    ("cron", "missed"),
    [(NIGHTLY, True), (NOON, False), ("0 22 * * *", False), ("0 8 * * *", True)],
)
def test_a_time_missed_overnight_is_found_only_if_it_came_due_after_the_evening_and_by_morning(
    cron: str, missed: bool
) -> None:
    assert falls_due(cron, MONDAY_EVENING, TUESDAY_MORNING, UTC) is missed


def test_a_day_the_clocks_go_forward_is_an_hour_shorter() -> None:
    saturday = datetime(2026, 3, 7, 3, tzinfo=NEW_YORK)
    sunday = next_due(NIGHTLY, saturday, NEW_YORK)
    assert sunday == datetime(2026, 3, 8, 3, tzinfo=NEW_YORK)
    # Within one zone, datetime arithmetic is the clocks'; the hours that passed are UTC's.
    assert sunday.astimezone(UTC) - saturday.astimezone(UTC) == timedelta(hours=23)


def test_a_time_the_clocks_skip_comes_due_as_they_go_forward() -> None:
    before = datetime(2026, 3, 8, 1, tzinfo=NEW_YORK)
    assert next_due("30 2 * * *", before, NEW_YORK) == datetime(2026, 3, 8, 3, tzinfo=NEW_YORK)


def test_a_time_the_clocks_repeat_comes_due_once() -> None:
    # A repeated time never equals one in another zone, so each is compared in UTC.
    first = next_due("30 1 * * *", datetime(2026, 11, 1, tzinfo=NEW_YORK), NEW_YORK)
    assert first.astimezone(UTC) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # Before they go back.
    later = next_due("30 1 * * *", first, NEW_YORK)
    assert later.astimezone(UTC) == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


# The schedule triggers


def test_the_enabled_schedule_triggers_give_their_cron_expressions_in_order() -> None:
    triggers = {
        "noon": nightly(cron=NOON),
        "off": nightly(enabled=False),
        "landing": Trigger(on="task_landed", checks="mutation"),
        "nightly": nightly(),
    }
    assert scheduled_crons(triggers) == {"noon": NOON, "nightly": NIGHTLY}


def test_a_schedule_trigger_fires_on_schedule_in_words() -> None:
    assert describe_firing(nightly()) == "on schedule 0 3 * * *"
    assert describe_firing(nightly(enabled=False)) == "on schedule 0 3 * * * (off)"


# Opening a workspace


def test_a_trigger_that_came_due_while_closed_fires_once_on_opening() -> None:
    opened = open_schedules({"nightly": NIGHTLY}, {"nightly": MONDAY_EVENING}, TUESDAY_MORNING, UTC)
    assert opened == Firing(("nightly",), TimesRead({"nightly": TUESDAY_MORNING}))


def test_a_trigger_that_came_due_many_times_while_closed_fires_once_on_opening() -> None:
    week_ago = MONDAY_EVENING - timedelta(days=7)
    opened = open_schedules({"nightly": NIGHTLY}, {"nightly": week_ago}, TUESDAY_MORNING, UTC)
    assert opened.due == ("nightly",)


def test_a_trigger_that_didnt_come_due_while_closed_keeps_its_time() -> None:
    opened = open_schedules({"noon": NOON}, {"noon": MONDAY_EVENING}, TUESDAY_MORNING, UTC)
    assert opened == Firing((), TimesRead({"noon": MONDAY_EVENING}))


def test_a_trigger_first_seen_doesnt_fire_and_takes_the_time_of_opening() -> None:
    opened = open_schedules({"nightly": NIGHTLY}, {}, TUESDAY_MORNING, UTC)
    assert opened == Firing((), TimesRead({"nightly": TUESDAY_MORNING}))


def test_the_times_of_triggers_no_longer_scheduled_are_dropped() -> None:
    kept = {"gone": MONDAY_EVENING, "noon": MONDAY_EVENING}
    opened = open_schedules({"noon": NOON}, kept, TUESDAY_MORNING, UTC)
    assert opened.times == TimesRead({"noon": MONDAY_EVENING})


def test_times_that_cant_be_read_leave_the_triggers_first_seen_and_unread() -> None:
    opened = open_schedules({"nightly": NIGHTLY}, None, TUESDAY_MORNING, UTC)
    assert opened == Firing((), TimesUnread({"nightly": TUESDAY_MORNING}, frozenset()))


# As the times come


def test_the_triggers_due_are_those_come_due_since_each_last_fired_in_order() -> None:
    crons = {"noon": NOON, "nightly": NIGHTLY, "hourly": "0 * * * *"}
    fired = {"noon": at(8), "nightly": at(3), "hourly": at(12)}
    assert due_triggers(crons, fired, at(12, 30), UTC) == ("noon",)
    assert due_triggers(crons, fired, at(13), UTC) == ("noon", "hourly")


def test_the_triggers_fired_take_the_time_they_fired() -> None:
    fired = {"noon": at(8), "nightly": at(3)}
    assert fire_due(fired, ("noon",), at(12)) == {"noon": at(12), "nightly": at(3)}


def test_with_the_times_read_the_triggers_due_fire_and_take_the_time_they_fired() -> None:
    times = TimesRead({"noon": at(8), "nightly": at(3)})
    woken = wake_schedules({"noon": NOON, "nightly": NIGHTLY}, times, None, at(12), UTC)
    assert woken == Firing(("noon",), TimesRead({"noon": at(12), "nightly": at(3)}))


def test_with_the_times_still_unread_the_triggers_due_fire_and_stay_unread() -> None:
    times = TimesUnread({"noon": at(8), "nightly": at(8)}, frozenset())
    woken = wake_schedules({"noon": NOON, "nightly": NIGHTLY}, times, None, at(12), UTC)
    assert woken == Firing(
        ("noon",), TimesUnread({"noon": at(12), "nightly": at(8)}, frozenset({"noon"}))
    )


def test_times_read_at_last_catch_up_the_triggers_unfired_since_opening_as_opening_does() -> None:
    crons = {"noon": NOON, "nightly": NIGHTLY, "new": NIGHTLY}
    times = TimesUnread({"noon": at(12), "nightly": at(8), "new": at(8)}, frozenset({"noon"}))
    kept = {"noon": MONDAY_EVENING, "nightly": MONDAY_EVENING, "gone": MONDAY_EVENING}
    woken = wake_schedules(crons, times, kept, at(12, 5), UTC)
    assert woken == Firing(
        ("nightly",), TimesRead({"noon": at(12), "nightly": at(12, 5), "new": at(8)})
    )


def test_times_read_while_they_are_known_already_change_nothing() -> None:
    times = TimesRead({"nightly": at(3)})
    woken = wake_schedules({"nightly": NIGHTLY}, times, {"nightly": MONDAY_EVENING}, at(4), UTC)
    assert woken == Firing((), times)


def test_the_wait_lasts_until_the_first_trigger_comes_due() -> None:
    crons = {"noon": NOON, "quarter": "*/15 * * * *"}
    fired = {"noon": at(8), "quarter": at(11, 50)}
    assert wait_for_due(crons, fired, at(11, 58), UTC) == timedelta(minutes=2)


def test_the_wait_lasts_no_longer_than_the_longest() -> None:
    assert wait_for_due({"nightly": NIGHTLY}, {"nightly": at(3)}, at(4), UTC) == LONGEST_WAIT


def test_a_trigger_already_due_is_waited_for_not_at_all() -> None:
    assert wait_for_due({"nightly": NIGHTLY}, {"nightly": at(2)}, at(4), UTC) == timedelta()
