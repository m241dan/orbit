"""Schedules: an open workspace's ``schedule`` triggers fire once on opening for the times they
came due while it was closed, a trigger first seen not at all, then each as its time comes, on a
clock the test moves; the time each last fired is kept in the store, a failing read reported and
a failing save halting the session until retried; closing stops them."""

import asyncio
from datetime import UTC, datetime, timedelta

from orbit.agent.application.schedules import FiredTimes, ScheduleClock, ScheduleParts, Schedules
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.protocol import INTERNAL, ErrorOccurred, Event
from tests.agent_fakes import FakeHalt, FakeStore
from tests.schedule_clock import FakeScheduleClock

MONDAY_EVENING = datetime(2026, 3, 2, 22, tzinfo=UTC)
TUESDAY_MORNING = datetime(2026, 3, 3, 8, tzinfo=UTC)
NIGHTLY = {"nightly": "0 3 * * *"}


def day(hour: int, days: int = 0) -> datetime:
    """``hour`` o'clock, ``days`` after Tuesday morning's day."""
    return datetime(2026, 3, 3, hour, tzinfo=UTC) + timedelta(days=days)


class Scheduled:
    """Schedules over ``store``, a new :class:`FakeStore` unless given, on a clock standing at
    ``now``, recording each firing, as the triggers fired with the time they fired at, and what
    they published."""

    def __init__(self, now: datetime = TUESDAY_MORNING, store: FakeStore | None = None) -> None:
        self.store = FakeStore() if store is None else store
        self.halt = FakeHalt()
        self.clock = FakeScheduleClock(now)
        self.fired: list[tuple[tuple[str, ...], datetime]] = []
        self.events: list[Event] = []
        self.times = FiredTimes(self.store, self.halt, self.publish)
        parts = ScheduleParts(self.times, self.clock.schedule_clock, self.fire, self.publish)
        self.schedules = Schedules(parts)

    async def fire(self, names: tuple[str, ...]) -> None:
        self.fired.append((names, self.clock.now))

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    async def opened(self, crons: dict[str, str] = NIGHTLY) -> None:
        """Start the schedules of ``crons``, and wait until they wait for the time to move."""
        self.schedules.start(crons)
        await self.clock.parked()


def kept(fired: dict[str, datetime]) -> FakeStore:
    """A store keeping ``fired`` as the times the triggers last fired."""
    store = FakeStore()
    store.fired = dict(fired)
    return store


async def test_a_trigger_first_seen_doesnt_fire_on_opening_and_keeps_the_time_of_opening() -> None:
    scheduled = Scheduled()
    await scheduled.opened()
    await scheduled.schedules.close()

    assert scheduled.fired == []
    assert scheduled.store.fired == {"nightly": TUESDAY_MORNING}


async def test_a_trigger_that_came_due_while_closed_fires_once_on_opening() -> None:
    scheduled = Scheduled(store=kept({"nightly": MONDAY_EVENING - timedelta(days=7)}))
    await scheduled.opened()
    await scheduled.schedules.close()

    assert scheduled.fired == [(("nightly",), TUESDAY_MORNING)]
    assert scheduled.store.fired == {"nightly": TUESDAY_MORNING}


async def test_a_trigger_that_didnt_come_due_while_closed_saves_nothing_on_opening() -> None:
    scheduled = Scheduled(store=kept({"noon": MONDAY_EVENING}))
    await scheduled.opened({"noon": "0 12 * * *"})
    await scheduled.schedules.close()

    assert scheduled.fired == []
    assert scheduled.store.fired_saved == []


async def test_a_trigger_fires_at_its_time_and_again_at_the_next() -> None:
    scheduled = Scheduled()
    await scheduled.opened()
    await scheduled.clock.go_to(day(3, days=1) - timedelta(seconds=1))
    assert scheduled.fired == []

    await scheduled.clock.go_to(day(3, days=2))
    await scheduled.schedules.close()

    assert scheduled.fired == [(("nightly",), day(3, days=1)), (("nightly",), day(3, days=2))]
    assert scheduled.store.fired == {"nightly": day(3, days=2)}


async def test_triggers_due_together_fire_together_and_waits_last_no_longer_than_five_minutes() -> (
    None
):
    scheduled = Scheduled()
    await scheduled.opened({"nightly": "0 3 * * *", "early": "0 3 * * *", "yearly": "0 0 1 1 *"})
    await scheduled.clock.go_to(day(3, days=1))
    await scheduled.schedules.close()

    assert scheduled.fired == [(("nightly", "early"), day(3, days=1))]
    assert max(scheduled.clock.sleeps) == timedelta(minutes=5)
    assert scheduled.clock.sleeps[:2] == [timedelta(minutes=5)] * 2


async def test_a_trigger_come_due_many_times_in_one_wait_fires_once() -> None:
    clock_jump = timedelta(days=3)  # As a machine asleep through three nights wakes.
    scheduled = Scheduled()
    await scheduled.opened({"hourly": "0 * * * *"})
    scheduled.clock.now += clock_jump
    await scheduled.clock.go_to(scheduled.clock.now + timedelta(minutes=5))
    await scheduled.schedules.close()

    assert scheduled.fired == [(("hourly",), TUESDAY_MORNING + clock_jump + timedelta(minutes=5))]


async def test_without_triggers_the_schedules_dont_run() -> None:
    scheduled = Scheduled()
    scheduled.schedules.start({})

    assert not scheduled.schedules.running
    assert scheduled.store.fired_saved == []


async def test_closing_stops_the_schedules_and_nothing_fires_after() -> None:
    scheduled = Scheduled()
    await scheduled.opened()
    await scheduled.schedules.close()
    scheduled.schedules.start(NIGHTLY)
    await asyncio.sleep(0)

    assert not scheduled.schedules.running
    assert scheduled.fired == []
    assert scheduled.store.fired_saved == [{"nightly": TUESDAY_MORNING}]


async def test_times_that_cant_be_read_are_reported_once_and_never_saved_over() -> None:
    store = kept({"nightly": MONDAY_EVENING})
    store.fired_load_failure = "database is locked"
    scheduled = Scheduled(store=store)
    await scheduled.opened()
    await scheduled.clock.go_to(day(3, days=1))
    await scheduled.schedules.close()

    assert scheduled.events == [ErrorOccurred(code=INTERNAL, message="database is locked")]
    assert scheduled.fired == [(("nightly",), day(3, days=1))]  # As first seen, at its time.
    assert store.fired_saved == []
    assert store.fired == {"nightly": MONDAY_EVENING}


async def test_a_trigger_that_came_due_while_closed_fires_once_when_its_time_can_be_read() -> None:
    store = kept({"nightly": MONDAY_EVENING})
    store.fired_load_failure = "database is locked"
    scheduled = Scheduled(store=store)
    await scheduled.opened()
    store.fired_load_failure = None
    read = TUESDAY_MORNING + timedelta(minutes=5)
    await scheduled.clock.go_to(read + timedelta(minutes=10))
    await scheduled.schedules.close()

    assert scheduled.fired == [(("nightly",), read)]
    assert store.fired == {"nightly": read}


async def test_a_trigger_fired_while_its_time_was_unread_keeps_that_time_once_read() -> None:
    store = kept({"nightly": MONDAY_EVENING})
    store.fired_load_failure = "database is locked"
    scheduled = Scheduled(store=store)
    await scheduled.opened()
    await scheduled.clock.go_to(day(3, days=1))
    store.fired_load_failure = None
    await scheduled.clock.go_to(day(3, days=1) + timedelta(minutes=10))
    await scheduled.schedules.close()

    assert scheduled.fired == [(("nightly",), day(3, days=1))]
    assert store.fired_saved == [{"nightly": day(3, days=1)}]


async def test_times_are_never_saved_before_a_read_of_them_works() -> None:
    store = kept({"nightly": MONDAY_EVENING})
    store.fired_load_failure = "database is locked"
    scheduled = Scheduled(store=store)
    assert await scheduled.times.load() is None
    await scheduled.times.save({"nightly": TUESDAY_MORNING})
    assert await scheduled.times.retry() is None

    assert store.fired_saved == []


async def test_a_failing_save_halts_the_session_and_retrying_saves_the_times() -> None:
    store = FakeStore()
    store.fired_failure = "disk full"
    scheduled = Scheduled(store=store)
    await scheduled.opened()
    await scheduled.clock.go_to(day(3, days=1))
    await scheduled.schedules.close()
    assert scheduled.halt.failures == [(FailedWrite.FIRED_TIMES, "disk full")]
    assert scheduled.fired == [(("nightly",), day(3, days=1))]  # Halted, nothing more is saved.

    store.fired_failure = None
    assert await scheduled.times.retry() is None
    assert store.fired == {"nightly": day(3, days=1)}
    assert await scheduled.times.retry() is None
    assert len(store.fired_saved) == 1


async def test_a_failing_retry_returns_its_error() -> None:
    store = FakeStore()
    store.fired_failure = "disk full"
    scheduled = Scheduled(store=store)
    await scheduled.opened()
    await scheduled.schedules.close()

    error = await scheduled.times.retry()
    assert str(error) == "disk full"


async def test_retrying_before_any_save_saves_nothing() -> None:
    scheduled = Scheduled()
    assert await scheduled.times.retry() is None
    assert scheduled.store.fired_saved == []


async def test_without_a_store_the_times_are_kept_only_while_open() -> None:
    times = FiredTimes(None, FakeHalt(), Scheduled().publish)
    assert await times.load() == {}
    await times.save({"nightly": MONDAY_EVENING})
    assert await times.retry() is None
    assert await times.load() == {}


def test_by_default_the_schedules_tell_the_time_now_in_utc() -> None:
    before = datetime.now(UTC)
    clock = ScheduleClock()
    assert clock.now().tzinfo is UTC
    assert clock.now() >= before
    assert clock.zone is UTC
