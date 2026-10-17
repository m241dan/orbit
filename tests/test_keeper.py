"""Keeping the open session saved: only once it's used, after each change, locked while open."""

import asyncio
from datetime import timedelta

from orbit.agent.application.keeper import (
    Contents,
    Left,
    SessionKeeper,
    local_now,
    new_session_id,
)
from orbit.agent.domain.reviews import LaneSnapshot, PlannerSnapshot
from orbit.agent.domain.sessions import SessionRecord
from orbit.protocol import ErrorOccurred, Event, Settings, TaskSpec, TaskState
from tests.agent_fakes import START, FakeStore, Ticks

TASK = TaskState(id="1", task=TaskSpec(prompt="Add a flag."))
EMPTY = Contents(settings=Settings(), planner_conversation=None, tasks=(), reviews=None)
UNLOCKABLE = "Couldn't unlock the session x.lock: read-only"
WITH_TASK = Contents(settings=Settings(), planner_conversation=None, tasks=(TASK,), reviews=None)


class Published:
    """Collects what a keeper publishes."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    @property
    def errors(self) -> list[str]:
        return [e.message for e in self.events if isinstance(e, ErrorOccurred)]


def keeper_for(store: FakeStore | None) -> tuple[SessionKeeper, Published]:
    published = Published()
    return SessionKeeper(store, published, Ticks(), lambda: "session-1"), published


async def save(keeper: SessionKeeper, contents: Contents) -> None:
    await keeper.save(lambda: contents)


def test_a_new_session_is_named_for_when_it_was_opened() -> None:
    keeper, _ = keeper_for(FakeStore())
    assert (keeper.id, keeper.created, keeper.name) == ("session-1", START, "Oct 17 14:30")
    assert not keeper.used


def test_a_session_can_be_renamed() -> None:
    keeper, _ = keeper_for(FakeStore())
    keeper.rename(" auth rework ")
    assert keeper.name == "auth rework"


def test_a_blank_or_missing_name_keeps_the_sessions_name() -> None:
    keeper, _ = keeper_for(FakeStore())
    keeper.rename(" ")
    keeper.rename(None)
    assert keeper.name == "Oct 17 14:30"


def test_by_default_sessions_get_fresh_ids_and_the_local_time() -> None:
    assert new_session_id() != new_session_id()
    assert local_now().utcoffset() is not None


async def test_an_unused_session_is_not_saved_or_locked() -> None:
    store = FakeStore()
    keeper, _ = keeper_for(store)
    await save(keeper, EMPTY)
    await keeper.close()
    assert store.saved == [] and store.locked == set() and store.released == []


async def test_a_session_with_a_task_is_saved_as_it_is() -> None:
    store = FakeStore()
    keeper, _ = keeper_for(store)
    reviews = LaneSnapshot(planner=PlannerSnapshot())
    contents = Contents(
        settings=Settings(model="opus"), planner_conversation="c1", tasks=(TASK,), reviews=reviews
    )
    await save(keeper, contents)
    assert keeper.used
    [record] = store.saved
    assert (record.id, record.name, record.created) == ("session-1", "Oct 17 14:30", START)
    assert record.settings == Settings(model="opus") and record.planner_conversation == "c1"
    assert record.tasks == (TASK,) and record.reviews == reviews


async def test_a_session_marked_used_is_saved_without_tasks() -> None:
    store = FakeStore()
    keeper, _ = keeper_for(store)
    keeper.use()
    await save(keeper, EMPTY)
    assert [r.tasks for r in store.saved] == [()]


async def test_a_session_that_had_a_task_stays_used_without_it() -> None:
    store = FakeStore()
    keeper, _ = keeper_for(store)
    await save(keeper, WITH_TASK)
    await save(keeper, EMPTY)
    assert [r.tasks for r in store.saved] == [(TASK,), ()]


async def test_an_unchanged_session_is_not_saved_again() -> None:
    store = FakeStore()
    keeper, _ = keeper_for(store)
    await save(keeper, WITH_TASK)
    await save(keeper, WITH_TASK)
    assert len(store.saved) == 1


async def test_each_save_is_last_active_when_it_was_saved() -> None:
    store = FakeStore()
    keeper, _ = keeper_for(store)
    await save(keeper, WITH_TASK)
    await save(keeper, EMPTY)
    assert [r.last_active for r in store.saved] == [
        START + timedelta(minutes=1),
        START + timedelta(minutes=2),
    ]
    assert all(r.created == START for r in store.saved)


async def test_saves_at_once_are_written_in_turn_the_newest_last() -> None:
    store = FakeStore()
    keeper, _ = keeper_for(store)
    current = [WITH_TASK]
    first = asyncio.ensure_future(keeper.save(lambda: current[0]))
    await asyncio.sleep(0)  # The first save is writing.
    current[0] = EMPTY
    await asyncio.gather(first, keeper.save(lambda: current[0]))
    assert [r.tasks for r in store.saved] == [(TASK,), ()]


async def test_the_session_is_locked_on_its_first_save_and_released_on_close() -> None:
    store = FakeStore()
    keeper, _ = keeper_for(store)
    await save(keeper, WITH_TASK)
    assert store.locked == {"session-1"}
    await save(keeper, EMPTY)
    await keeper.close()
    assert store.locked == set() and store.released == ["session-1"]


async def test_a_closed_session_is_not_saved() -> None:
    store = FakeStore()
    keeper, _ = keeper_for(store)
    await keeper.close()
    await save(keeper, WITH_TASK)
    assert store.saved == [] and store.released == []


async def test_a_lock_held_elsewhere_is_reported_and_nothing_is_saved() -> None:
    store = FakeStore()
    store.free = False
    keeper, published = keeper_for(store)
    await save(keeper, WITH_TASK)
    await save(keeper, EMPTY)
    await keeper.close()
    assert published.errors == [
        "Another orbit has the session session-1 open, so it won't be saved."
    ]
    assert store.saved == [] and store.released == []


async def test_a_lock_that_cannot_be_taken_is_reported() -> None:
    store = FakeStore()
    store.lock_failure = "Couldn't lock the session x.lock: disk full"
    keeper, published = keeper_for(store)
    await save(keeper, WITH_TASK)
    assert published.errors == [
        "Couldn't lock the session x.lock: disk full; the session won't be saved."
    ]
    assert store.saved == []


async def test_a_failing_save_is_reported_once_and_tried_again_at_the_next_change() -> None:
    store = FakeStore()
    store.failure = "Couldn't save the session: disk full"
    keeper, published = keeper_for(store)
    await save(keeper, WITH_TASK)
    await save(keeper, EMPTY)
    assert published.errors == ["Couldn't save the session: disk full"]
    store.failure = None
    await save(keeper, EMPTY)
    assert [r.tasks for r in store.saved] == [()]


async def test_a_save_failing_again_after_one_worked_is_reported_again() -> None:
    store = FakeStore()
    keeper, published = keeper_for(store)
    store.failure = "full"
    await save(keeper, WITH_TASK)
    store.failure = None
    await save(keeper, WITH_TASK)
    store.failure = "full again"
    await save(keeper, EMPTY)
    assert published.errors == ["full", "full again"]


async def test_without_a_store_nothing_is_saved_or_read() -> None:
    keeper, published = keeper_for(None)
    keeper.use()
    await save(keeper, WITH_TASK)
    await keeper.save_review_count(3)
    assert await keeper.review_count() is None
    await keeper.close()
    assert published.events == []


async def test_the_review_count_is_read_and_saved_when_it_changes() -> None:
    store = FakeStore(count=4)
    keeper, _ = keeper_for(store)
    assert await keeper.review_count() == 4
    await keeper.save_review_count(4)
    await keeper.save_review_count(5)
    await keeper.save_review_count(5)
    assert store.counts == [5]


async def test_a_review_count_that_cannot_be_saved_is_reported_and_tried_again() -> None:
    store = FakeStore()
    store.count_failure = "Couldn't save the review count: disk full"
    keeper, published = keeper_for(store)
    await keeper.save_review_count(1)
    store.count_failure = None
    await keeper.save_review_count(1)
    assert published.errors == ["Couldn't save the review count: disk full"]
    assert store.counts == [1]


async def test_a_failing_session_save_is_reported_once_though_count_saves_work() -> None:
    store = FakeStore()
    keeper, published = keeper_for(store)
    store.failure = "full"
    await save(keeper, WITH_TASK)
    await keeper.save_review_count(1)
    await save(keeper, EMPTY)
    assert published.errors == ["full"]
    assert store.counts == [1]


async def test_a_failing_session_save_is_reported_once_across_review_lane_changes() -> None:
    store = FakeStore()
    keeper, published = keeper_for(store)
    store.failure = "Couldn't save the session: sessions/ unwritable"
    for count, contents in enumerate((WITH_TASK, EMPTY, WITH_TASK, EMPTY), start=1):
        await keeper.save_review_count(count)
        await save(keeper, contents)
    assert published.errors == ["Couldn't save the session: sessions/ unwritable"]
    assert store.counts == [1, 2, 3, 4] and store.saved == []


async def test_session_and_count_save_failures_are_each_reported() -> None:
    store = FakeStore()
    keeper, published = keeper_for(store)
    store.failure = "A"
    await save(keeper, WITH_TASK)
    store.count_failure = "B"
    await keeper.save_review_count(1)
    assert published.errors == ["A", "B"]


async def test_a_failing_count_save_is_reported_once_though_session_saves_work() -> None:
    store = FakeStore()
    keeper, published = keeper_for(store)
    store.count_failure = "full"
    await keeper.save_review_count(1)
    await save(keeper, WITH_TASK)
    await keeper.save_review_count(2)
    assert published.errors == ["full"]
    assert len(store.saved) == 1


async def test_a_failing_session_save_is_reported_again_in_a_new_session() -> None:
    store = FakeStore()
    keeper, published = keeper_for(store)
    store.failure = "full"
    await save(keeper, WITH_TASK)
    await keeper.renew("next", lambda: True)
    store.failure = "still full"
    await save(keeper, WITH_TASK)
    assert published.errors == ["full", "still full"]


async def test_a_failing_count_save_is_reported_once_across_new_sessions() -> None:
    store = FakeStore()
    keeper, published = keeper_for(store)
    store.count_failure = "full"
    await keeper.save_review_count(1)
    await keeper.renew("next", lambda: True)
    await keeper.save_review_count(2)
    assert published.errors == ["full"]


async def test_count_saves_at_once_are_written_in_turn_the_newest_last() -> None:
    store = FakeStore()
    store.count_yields = [3, 0]
    keeper, _ = keeper_for(store)
    await asyncio.gather(keeper.save_review_count(1), keeper.save_review_count(2))
    assert store.counts == [1, 2] and store.count == 2


async def test_a_count_saved_while_another_waited_is_not_saved_again() -> None:
    store = FakeStore()
    store.count_yields = [1]
    keeper, _ = keeper_for(store)
    await asyncio.gather(keeper.save_review_count(1), keeper.save_review_count(1))
    assert store.counts == [1]


async def test_a_lock_that_cannot_be_released_on_close_is_reported() -> None:
    store = FakeStore()
    store.release_failure = UNLOCKABLE
    keeper, published = keeper_for(store)
    await save(keeper, WITH_TASK)
    await keeper.close()
    assert published.errors == [UNLOCKABLE]
    await save(keeper, EMPTY)
    assert len(store.saved) == 1


def unreleasable_store_with(session_id: str) -> FakeStore:
    """A store holding the session ``session_id``, whose locks can't be let go of."""
    store = FakeStore()
    store.saved = [SessionRecord(id=session_id, name=session_id, created=START, last_active=START)]
    store.release_failure = UNLOCKABLE
    return store


async def test_a_lock_that_cannot_be_released_on_leaving_is_reported() -> None:
    keeper, published = keeper_for(unreleasable_store_with("old"))
    await keeper.leave(Left("old"), discard=False)
    assert published.errors == [UNLOCKABLE]


async def test_a_lock_that_cannot_be_released_after_a_refused_open_is_reported() -> None:
    store = unreleasable_store_with("other")
    keeper, published = keeper_for(store)
    assert await keeper.open("other", lambda record: False) is None
    assert published.errors == [UNLOCKABLE]
    assert keeper.id == "session-1"


async def test_a_failed_rename_whose_lock_cannot_be_released_reports_both() -> None:
    store = unreleasable_store_with("other")
    store.failure = "Couldn't save the session: disk full"
    keeper, published = keeper_for(store)
    await keeper.rename_saved("other", "Renamed")
    assert published.errors == ["Couldn't save the session: disk full", UNLOCKABLE]


async def test_a_rename_whose_session_cannot_be_read_is_reported_and_lets_go_of_its_lock() -> None:
    store = FakeStore()
    store.saved = [SessionRecord(id="other", name="other", created=START, last_active=START)]
    store.load_failure = "Couldn't read the session: permission denied"
    keeper, published = keeper_for(store)
    await keeper.rename_saved("other", "Renamed")
    assert published.errors == ["Couldn't read the session: permission denied"]
    assert store.released == ["other"] and store.last.name == "other"


async def test_a_failed_delete_whose_lock_cannot_be_released_reports_both() -> None:
    store = unreleasable_store_with("other")
    store.delete_failure = "Couldn't delete the session: read-only"
    keeper, published = keeper_for(store)
    await keeper.delete_saved("other")
    assert published.errors == [UNLOCKABLE, "Couldn't delete the session: read-only"]


async def test_a_session_that_cannot_be_deleted_on_leaving_lets_go_of_its_lock() -> None:
    store = FakeStore()
    store.saved = [SessionRecord(id="old", name="old", created=START, last_active=START)]
    store.locked = {"old"}
    store.delete_failure = "Couldn't delete the session: read-only"
    keeper, published = keeper_for(store)
    await keeper.leave(Left("old"), discard=True)
    assert published.errors == ["Couldn't delete the session: read-only"]
    assert store.released == ["old"] and store.locked == set()


async def test_a_session_unreadable_once_locked_is_not_opened_and_lets_go_of_its_lock() -> None:
    store = FakeStore()
    store.saved = [SessionRecord(id="other", name="other", created=START, last_active=START)]

    async def break_it() -> None:
        store.broken["other"] = "it isn't JSON"

    store.on_acquire = break_it
    keeper, published = keeper_for(store)
    loaded: list[SessionRecord] = []

    def load(record: SessionRecord) -> bool:
        loaded.append(record)
        return True

    assert await keeper.open("other", load) is None
    assert published.errors == ["Can't open the session other: it isn't JSON."]
    assert loaded == [] and keeper.id == "session-1"
    assert store.released == ["other"] and store.locked == set()
