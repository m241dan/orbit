"""Keeping the repository's super review count: read once, saved when it changes."""

import asyncio

from orbit.agent.application.keeper import SessionKeeper
from orbit.agent.application.review_count import ReviewCount
from tests.agent_fakes import FakeStore, Ticks
from tests.test_keeper import EMPTY, WITH_TASK, Published, save


def both_for(store: FakeStore | None) -> tuple[SessionKeeper, ReviewCount, Published]:
    """A keeper and a review count over ``store``, publishing to the same place."""
    published = Published()
    keeper = SessionKeeper(store, published, Ticks(), lambda: "session-1")
    return keeper, ReviewCount(store, published), published


async def test_without_a_store_the_count_is_neither_read_nor_saved() -> None:
    _, count, published = both_for(None)
    await count.save(3)
    assert await count.load() is None
    assert published.events == []


async def test_the_review_count_is_read_and_saved_when_it_changes() -> None:
    store = FakeStore(count=4)
    _, count, _ = both_for(store)
    assert await count.load() == 4
    await count.save(4)
    await count.save(5)
    await count.save(5)
    assert store.counts == [5]


async def test_a_review_count_that_cannot_be_saved_is_reported_and_tried_again() -> None:
    store = FakeStore()
    store.count_failure = "Couldn't save the review count: disk full"
    _, count, published = both_for(store)
    await count.save(1)
    store.count_failure = None
    await count.save(1)
    assert published.errors == ["Couldn't save the review count: disk full"]
    assert store.counts == [1]


async def test_a_failing_session_save_is_reported_once_though_count_saves_work() -> None:
    store = FakeStore()
    keeper, count, published = both_for(store)
    store.failure = "full"
    await save(keeper, WITH_TASK)
    await count.save(1)
    await save(keeper, EMPTY)
    assert published.errors == ["full"]
    assert store.counts == [1]


async def test_a_failing_session_save_is_reported_once_across_review_lane_changes() -> None:
    store = FakeStore()
    keeper, count, published = both_for(store)
    store.failure = "Couldn't save the session: sessions/ unwritable"
    for saved, contents in enumerate((WITH_TASK, EMPTY, WITH_TASK, EMPTY), start=1):
        await count.save(saved)
        await save(keeper, contents)
    assert published.errors == ["Couldn't save the session: sessions/ unwritable"]
    assert store.counts == [1, 2, 3, 4] and store.saved == []


async def test_session_and_count_save_failures_are_each_reported() -> None:
    store = FakeStore()
    keeper, count, published = both_for(store)
    store.failure = "A"
    await save(keeper, WITH_TASK)
    store.count_failure = "B"
    await count.save(1)
    assert published.errors == ["A", "B"]


async def test_a_failing_count_save_is_reported_once_though_session_saves_work() -> None:
    store = FakeStore()
    keeper, count, published = both_for(store)
    store.count_failure = "full"
    await count.save(1)
    await save(keeper, WITH_TASK)
    await count.save(2)
    assert published.errors == ["full"]
    assert len(store.saved) == 1


async def test_a_failing_count_save_is_reported_once_across_new_sessions() -> None:
    store = FakeStore()
    keeper, count, published = both_for(store)
    store.count_failure = "full"
    await count.save(1)
    await keeper.renew("next", lambda: True)
    await count.save(2)
    assert published.errors == ["full"]


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
