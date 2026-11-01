"""Keeping the repository's super review count: read once, saved when it changes."""

import asyncio

from orbit.agent.application.keeper import NewSessions, SessionKeeper
from orbit.agent.application.ports import StoreFailed
from orbit.agent.application.review_count import ReviewCount
from orbit.agent.domain.reviews import ReviewLimits, ReviewPlanner, SuperReviewCount
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.protocol import INTERNAL, ErrorOccurred
from tests.agent_fakes import FakeHalt, FakeStore, Ticks
from tests.keeper_fakes import EMPTY, WITH_TASK, Published, save


def both_for(store: FakeStore | None) -> tuple[SessionKeeper, ReviewCount, FakeHalt]:
    """A keeper and a review count over ``store``, telling the same halt of their failures."""
    halt = FakeHalt()
    keeper = SessionKeeper(store, Published(), halt, NewSessions(Ticks(), lambda: "session-1"))
    return keeper, ReviewCount(store, halt, Published()), halt


async def loaded(store: FakeStore) -> tuple[SessionKeeper, ReviewCount, FakeHalt]:
    """A keeper and a review count over ``store``, as :func:`both_for` makes them, the count
    read."""
    keeper, count, halt = both_for(store)
    await count.load()
    return keeper, count, halt


async def test_without_a_store_the_count_is_neither_read_nor_saved() -> None:
    _, count, halt = both_for(None)
    await count.save(SuperReviewCount(3))
    assert await count.load() is None
    assert halt.failures == []


async def test_the_review_count_is_read_and_saved_when_it_changes() -> None:
    store = FakeStore(count=4)
    _, count, _ = both_for(store)
    assert await count.load() == SuperReviewCount(4)
    await count.save(SuperReviewCount(4))
    await count.save(SuperReviewCount(5))
    await count.save(SuperReviewCount(5))
    assert store.counts == [SuperReviewCount(5)]


async def test_a_review_count_that_cannot_be_saved_halts_and_nothing_more_is_saved() -> None:
    store = FakeStore()
    store.count_failure = "Couldn't save the review count: disk full"
    _, count, halt = await loaded(store)
    await count.save(SuperReviewCount(1))
    store.count_failure = None
    await count.save(SuperReviewCount(2))
    assert halt.failures == [
        (FailedWrite.REVIEW_COUNT, "Couldn't save the review count: disk full")
    ]
    assert store.counts == []


async def test_a_forgotten_count_is_not_saved_until_it_is_read_again() -> None:
    store = FakeStore(count=4)
    _, count, _ = await loaded(store)
    count.forget()
    await count.save(SuperReviewCount(5))
    store.count = SuperReviewCount(6)
    assert await count.load() == SuperReviewCount(6)
    await count.save(SuperReviewCount(6))
    await count.save(SuperReviewCount(7))
    assert store.counts == [SuperReviewCount(7)]


async def test_retrying_saves_the_count_last_asked_for_though_halted() -> None:
    store = FakeStore()
    store.count_failure = "full"
    _, count, halt = await loaded(store)
    await count.save(SuperReviewCount(1))
    await count.save(SuperReviewCount(2))
    store.count_failure = None

    assert await count.retry() is None
    assert await count.retry() is None
    assert store.counts == [SuperReviewCount(2)] and len(halt.failures) == 1


async def test_a_count_retry_that_fails_returns_the_error() -> None:
    store = FakeStore()
    store.count_failure = "full"
    _, count, halt = await loaded(store)
    await count.save(SuperReviewCount(1))

    error = await count.retry()
    assert isinstance(error, StoreFailed) and str(error) == "full"
    assert len(halt.failures) == 1


async def test_a_count_never_asked_for_or_without_a_store_isnt_retried() -> None:
    store = FakeStore()
    _, count, _ = both_for(store)
    _, without, _ = both_for(None)
    await without.save(SuperReviewCount(1))

    assert await count.retry() is None and await without.retry() is None
    assert store.counts == []


async def test_a_failing_session_save_stops_the_count_being_saved_too() -> None:
    store = FakeStore()
    keeper, count, halt = await loaded(store)
    store.failure = "full"
    await save(keeper, WITH_TASK)
    await count.save(SuperReviewCount(1))
    await save(keeper, EMPTY)
    assert halt.failures == [(FailedWrite.SESSION_RECORD, "full")]
    assert store.counts == []


async def test_a_failing_count_save_stops_the_session_being_saved_too() -> None:
    store = FakeStore()
    keeper, count, halt = await loaded(store)
    store.count_failure = "full"
    await count.save(SuperReviewCount(1))
    await save(keeper, WITH_TASK)
    assert halt.failures == [(FailedWrite.REVIEW_COUNT, "full")]
    assert store.saved == []


async def test_count_saves_at_once_are_written_in_turn_the_newest_last() -> None:
    store = FakeStore()
    store.count_yields = [3, 0]
    _, count, _ = await loaded(store)
    await asyncio.gather(count.save(SuperReviewCount(1)), count.save(SuperReviewCount(2)))
    assert store.counts == [
        SuperReviewCount(1),
        SuperReviewCount(2),
    ] and store.count == SuperReviewCount(2)


async def test_a_count_saved_while_another_waited_is_not_saved_again() -> None:
    store = FakeStore()
    store.count_yields = [1]
    _, count, _ = await loaded(store)
    await asyncio.gather(count.save(SuperReviewCount(1)), count.save(SuperReviewCount(1)))
    assert store.counts == [SuperReviewCount(1)]


async def test_a_count_never_read_is_not_saved() -> None:
    store = FakeStore(count=7)
    _, count, halt = both_for(store)
    await count.save(SuperReviewCount(1))
    assert store.counts == [] and halt.failures == []


async def test_a_count_that_cannot_be_read_is_reported_once_and_never_saved() -> None:
    store = FakeStore(count=7)
    store.count_load_failure = "Couldn't read the review count from orbit.db: busy"
    published = Published()
    count = ReviewCount(store, FakeHalt(), published)
    assert await count.load() is None
    assert await count.load() is None
    await count.save(SuperReviewCount(1))
    assert await count.retry() is None
    assert published.events == [ErrorOccurred(code=INTERNAL, message=store.count_load_failure)]
    assert store.counts == [] and store.count == SuperReviewCount(7)


async def test_a_count_read_after_a_failed_read_saves_as_usual() -> None:
    store = FakeStore(count=7)
    store.count_load_failure = "busy"
    _, count, _ = both_for(store)
    await count.load()
    store.count_load_failure = None
    assert await count.load() == SuperReviewCount(7)
    await count.save(SuperReviewCount(8))
    assert store.counts == [SuperReviewCount(8)]


class SlowStore(FakeStore):
    """A store whose count reads yield once before they're done, counted in ``reads``."""

    def __init__(self, count: int) -> None:
        super().__init__(count=count)
        self.reads = 0

    async def review_count(self) -> SuperReviewCount:
        self.reads += 1
        await asyncio.sleep(0)  # Reading takes a while, as reading a file does.
        return await super().review_count()


def planner() -> ReviewPlanner:
    """A review planner that hasn't counted on yet."""
    return ReviewPlanner(ReviewLimits())


async def test_loads_at_once_read_the_count_once() -> None:
    store = SlowStore(count=4)
    _, count, _ = both_for(store)
    first, second = planner(), planner()
    await asyncio.gather(count.count_on(first), count.count_on(second))
    assert store.reads == 1
    assert first.since_super == 4 and second.since_super == 0


async def test_a_count_read_once_is_not_read_again() -> None:
    store = SlowStore(count=4)
    _, count, _ = both_for(store)
    counting = planner()
    await count.count_on(counting)
    counting.since_super = 5
    await count.count_on(counting)
    assert store.reads == 1 and counting.since_super == 5


async def test_a_load_that_failed_is_tried_again_on_the_next() -> None:
    store = SlowStore(count=4)
    store.count_load_failure = "busy"
    _, count, _ = both_for(store)
    counting = planner()
    await count.count_on(counting)
    assert counting.since_super == 0
    store.count_load_failure = None
    await count.count_on(counting)
    assert store.reads == 2 and counting.since_super == 4


async def test_a_planner_that_counted_on_during_the_read_keeps_its_count() -> None:
    store = SlowStore(count=4)
    _, count, _ = both_for(store)
    counting = planner()

    async def count_one() -> None:
        counting.since_super += 1

    await asyncio.gather(count.count_on(counting), count_one())
    assert counting.since_super == 1
    await count.save(SuperReviewCount(1))
    assert store.counts == [SuperReviewCount(1)]


async def test_a_recount_reads_the_count_again_after_a_load_in_flight() -> None:
    store = SlowStore(count=4)
    _, count, _ = both_for(store)
    counting = planner()

    async def moved() -> None:
        await asyncio.sleep(0)
        store.count = SuperReviewCount(6)

    await asyncio.gather(count.count_on(counting), count.recount_on(counting), moved())
    assert store.reads == 2 and counting.since_super == 6


async def test_without_a_store_nothing_counts_on() -> None:
    _, count, _ = both_for(None)
    counting = planner()
    await count.count_on(counting)
    await count.recount_on(counting)
    assert counting.since_super == 0


async def test_a_planner_counts_on_from_the_saved_count_and_last_super_commit() -> None:
    store = FakeStore()
    store.count = SuperReviewCount(4, "abc")
    _, count, _ = both_for(store)
    counting = planner()
    await count.count_on(counting)
    assert counting.super_count == SuperReviewCount(4, "abc")
    counting.super_review("def")
    await count.save(counting.super_count)
    assert store.counts == [SuperReviewCount(0, "def")]


async def test_a_planner_that_queued_a_super_review_during_the_read_keeps_its_commit() -> None:
    store = SlowStore(count=0)
    store.count = SuperReviewCount(0, "old")
    _, count, _ = both_for(store)
    counting = planner()

    async def super_review() -> None:
        counting.super_review("new")

    await asyncio.gather(count.count_on(counting), super_review())
    assert counting.super_count == SuperReviewCount(0, "new")
