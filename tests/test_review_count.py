"""Keeping the repository's super review count: read once, saved when it changes."""

import asyncio

from orbit.agent.application.keeper import NewSessions, SessionKeeper
from orbit.agent.application.review_count import ReviewCount
from orbit.agent.domain.storage_halt import FailedWrite
from tests.agent_fakes import FakeHalt, FakeStore, Ticks
from tests.test_keeper import EMPTY, WITH_TASK, Published, save


def both_for(store: FakeStore | None) -> tuple[SessionKeeper, ReviewCount, FakeHalt]:
    """A keeper and a review count over ``store``, telling the same halt of their failures."""
    halt = FakeHalt()
    keeper = SessionKeeper(store, Published(), halt, NewSessions(Ticks(), lambda: "session-1"))
    return keeper, ReviewCount(store, halt), halt


async def test_without_a_store_the_count_is_neither_read_nor_saved() -> None:
    _, count, halt = both_for(None)
    await count.save(3)
    assert await count.load() is None
    assert halt.failures == []


async def test_the_review_count_is_read_and_saved_when_it_changes() -> None:
    store = FakeStore(count=4)
    _, count, _ = both_for(store)
    assert await count.load() == 4
    await count.save(4)
    await count.save(5)
    await count.save(5)
    assert store.counts == [5]


async def test_a_review_count_that_cannot_be_saved_halts_and_nothing_more_is_saved() -> None:
    store = FakeStore()
    store.count_failure = "Couldn't save the review count: disk full"
    _, count, halt = both_for(store)
    await count.save(1)
    store.count_failure = None
    await count.save(2)
    assert halt.failures == [
        (FailedWrite.REVIEW_COUNT, "Couldn't save the review count: disk full")
    ]
    assert store.counts == []


async def test_a_failing_session_save_stops_the_count_being_saved_too() -> None:
    store = FakeStore()
    keeper, count, halt = both_for(store)
    store.failure = "full"
    await save(keeper, WITH_TASK)
    await count.save(1)
    await save(keeper, EMPTY)
    assert halt.failures == [(FailedWrite.SESSION_RECORD, "full")]
    assert store.counts == []


async def test_a_failing_count_save_stops_the_session_being_saved_too() -> None:
    store = FakeStore()
    keeper, count, halt = both_for(store)
    store.count_failure = "full"
    await count.save(1)
    await save(keeper, WITH_TASK)
    assert halt.failures == [(FailedWrite.REVIEW_COUNT, "full")]
    assert store.saved == []


async def test_count_saves_at_once_are_written_in_turn_the_newest_last() -> None:
    store = FakeStore()
    store.count_yields = [3, 0]
    _, count, _ = both_for(store)
    await asyncio.gather(count.save(1), count.save(2))
    assert store.counts == [1, 2] and store.count == 2


async def test_a_count_saved_while_another_waited_is_not_saved_again() -> None:
    store = FakeStore()
    store.count_yields = [1]
    _, count, _ = both_for(store)
    await asyncio.gather(count.save(1), count.save(1))
    assert store.counts == [1]
