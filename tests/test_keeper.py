"""Keeping the open session saved: only once it's used, after each change, locked while open."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from orbit.agent.application.keeper import (
    ALREADY_OPEN,
    Contents,
    Left,
    NewSessions,
    SessionKeeper,
    local_now,
    new_session_id,
)
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.reviews import LaneSnapshot, PlannerSnapshot
from orbit.agent.domain.sessions import SessionRecord
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.protocol import (
    BUSY,
    LOCKED,
    SAVE_FAILED,
    UNKNOWN,
    ErrorOccurred,
    Event,
    Settings,
    TaskSpec,
    TaskState,
    Usage,
)
from tests.agent_fakes import START, FakeHalt, FakeStore, Ticks

TASK = TaskState(id="1", task=TaskSpec(prompt="Add a flag."))
EMPTY = Contents(
    settings=Settings(), planner_conversation=None, tasks=(), reviews=None, spent=Usage()
)
SPENT = Usage(input_tokens=500, cost_usd=1.4, turns=3)
UNLOCKABLE = "Couldn't unlock the session x.lock: read-only"
WITH_TASK = replace(EMPTY, tasks=(TASK,))


class Published:
    """Collects what a keeper publishes."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    @property
    def errors(self) -> list[str]:
        return [e.message for e in self.events if isinstance(e, ErrorOccurred)]

    @property
    def codes(self) -> list[str]:
        return [e.code for e in self.events if isinstance(e, ErrorOccurred)]


def keeper_for(store: FakeStore | None) -> tuple[SessionKeeper, Published]:
    published = Published()
    return SessionKeeper(
        store, published, FakeHalt(), NewSessions(Ticks(), lambda: "session-1")
    ), published


def halting_keeper(store: FakeStore) -> tuple[SessionKeeper, FakeHalt]:
    """A keeper over ``store``, and the halt its failing saves are told to."""
    halt = FakeHalt()
    return SessionKeeper(store, Published(), halt, NewSessions(Ticks(), lambda: "session-1")), halt


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
        settings=Settings(model="opus"),
        planner_conversation="c1",
        tasks=(TASK,),
        reviews=reviews,
        spent=SPENT,
    )
    await save(keeper, contents)
    assert keeper.used
    [record] = store.saved
    assert (record.id, record.name, record.created) == ("session-1", "Oct 17 14:30", START)
    assert record.settings == Settings(model="opus") and record.planner_conversation == "c1"
    assert record.tasks == (TASK,) and record.reviews == reviews and record.spent == SPENT


async def test_a_session_that_spent_more_is_saved_again() -> None:
    store = FakeStore()
    keeper, _ = keeper_for(store)
    await save(keeper, WITH_TASK)
    await save(keeper, replace(WITH_TASK, spent=SPENT))
    assert [r.spent for r in store.saved] == [Usage(), SPENT]


def test_the_open_sessions_summary_shows_what_it_spent() -> None:
    keeper, _ = keeper_for(FakeStore())
    summary = keeper.summary((TASK,), SPENT, None)
    assert (summary.usage, summary.counts, summary.current) == (SPENT, {"pending": 1}, True)


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


async def test_a_lock_held_elsewhere_is_never_tried_again() -> None:
    store = FakeStore()
    store.free = False
    keeper, published = keeper_for(store)
    await save(keeper, WITH_TASK)
    store.free = True

    assert await keeper.retry(lambda: EMPTY) is None
    assert published.codes == [LOCKED]
    assert store.saved == [] and store.locked == set() and not keeper.kept


async def test_a_lock_that_cannot_be_taken_halts_the_session_unkept() -> None:
    store = FakeStore()
    store.lock_failure = "Couldn't lock the session x.lock: disk full"
    keeper, halt = halting_keeper(store)
    await save(keeper, WITH_TASK)
    assert halt.failures == [
        (FailedWrite.SESSION_RECORD, "Couldn't lock the session x.lock: disk full")
    ]
    assert store.saved == [] and not keeper.kept


async def test_a_retry_takes_the_lock_that_could_not_be_taken_and_saves_the_session() -> None:
    store = FakeStore()
    store.lock_failure = "disk full"
    keeper, halt = halting_keeper(store)
    await save(keeper, WITH_TASK)
    error = await keeper.retry(lambda: WITH_TASK)
    assert isinstance(error, RuntimeError) and str(error) == "disk full"
    store.lock_failure = None

    assert await keeper.retry(lambda: WITH_TASK) is None
    assert store.locked == {"session-1"} and store.last.tasks == (TASK,) and keeper.kept
    assert len(halt.failures) == 1


async def test_a_session_locked_on_a_retry_lets_go_of_its_lock_on_close() -> None:
    store = FakeStore()
    store.lock_failure = "disk full"
    keeper, forgotten = forgetting_keeper(store)
    await save(keeper, WITH_TASK)
    store.lock_failure = None
    await keeper.retry(lambda: WITH_TASK)
    await keeper.close()
    assert store.released == ["session-1"] and forgotten == []


async def test_a_session_halted_before_its_lock_was_taken_keeps_its_logged_events() -> None:
    store = FakeStore()
    store.lock_failure = "disk full"
    keeper, forgotten = forgetting_keeper(store)
    await save(keeper, WITH_TASK)
    await keeper.close()
    assert store.released == [] and forgotten == []


async def test_a_failing_save_halts_the_session_and_nothing_more_is_saved() -> None:
    store = FakeStore()
    store.failure = "Couldn't save the session: disk full"
    keeper, halt = halting_keeper(store)
    await save(keeper, WITH_TASK)
    store.failure = None
    await save(keeper, EMPTY)
    assert halt.failures == [(FailedWrite.SESSION_RECORD, "Couldn't save the session: disk full")]
    assert store.saved == []


async def test_retrying_saves_the_session_as_it_is_now_though_halted() -> None:
    store = FakeStore()
    store.failure = "disk full"
    keeper, halt = halting_keeper(store)
    await save(keeper, WITH_TASK)
    store.failure = None

    assert await keeper.retry(lambda: EMPTY) is None
    assert store.last.tasks == EMPTY.tasks and len(halt.failures) == 1
    assert await keeper.retry(lambda: EMPTY) is None
    assert len(store.saved) == 1  # Unchanged since, so not saved again.


async def test_a_retry_that_fails_returns_the_error_without_telling_the_halt() -> None:
    store = FakeStore()
    store.failure = "disk full"
    keeper, halt = halting_keeper(store)
    await save(keeper, WITH_TASK)

    error = await keeper.retry(lambda: WITH_TASK)
    assert isinstance(error, RuntimeError) and str(error) == "disk full"
    assert len(halt.failures) == 1


async def test_without_a_store_a_retry_saves_nothing() -> None:
    keeper = SessionKeeper(None, Published(), FakeHalt())

    assert await keeper.retry(lambda: WITH_TASK) is None


async def test_saves_that_work_tell_the_halt_nothing() -> None:
    store = FakeStore()
    keeper, halt = halting_keeper(store)
    await save(keeper, WITH_TASK)
    await save(keeper, EMPTY)
    assert halt.failures == [] and len(store.saved) == 2


async def test_without_a_store_nothing_is_saved() -> None:
    keeper, published = keeper_for(None)
    keeper.use()
    await save(keeper, WITH_TASK)
    await keeper.close()
    assert published.events == []


async def test_a_lock_that_cannot_be_released_on_close_is_reported() -> None:
    store = FakeStore()
    store.release_failure = UNLOCKABLE
    keeper, published = keeper_for(store)
    await save(keeper, WITH_TASK)
    await keeper.close()
    assert published.errors == [UNLOCKABLE]
    assert published.codes == [SAVE_FAILED]
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
    assert published.codes == [SAVE_FAILED]


def refuses(record: SessionRecord) -> None:
    raise Refused(BUSY, "Busy.")


async def test_a_lock_that_cannot_be_released_after_a_refused_open_is_reported() -> None:
    store = unreleasable_store_with("other")
    keeper, published = keeper_for(store)
    with pytest.raises(Refused):
        await keeper.open("other", refuses)
    assert published.errors == [UNLOCKABLE]
    assert published.codes == [SAVE_FAILED]
    assert keeper.id == "session-1"


async def test_a_failed_rename_halts_the_session_and_lets_go_of_its_lock() -> None:
    store = FakeStore()
    store.saved = [SessionRecord(id="other", name="other", created=START, last_active=START)]
    store.failure = "Couldn't save the session: disk full"
    keeper, halt = halting_keeper(store)
    await keeper.rename_saved("other", "Renamed")
    assert halt.failures == [(FailedWrite.SESSION_RECORD, "Couldn't save the session: disk full")]
    assert store.released == ["other"]


async def test_a_failed_rename_whose_lock_cannot_be_released_halts_and_reports_the_lock() -> None:
    store = unreleasable_store_with("other")
    store.failure = "Couldn't save the session: disk full"
    published = Published()
    halt = FakeHalt()
    keeper = SessionKeeper(store, published, halt, NewSessions(Ticks(), lambda: "session-1"))
    await keeper.rename_saved("other", "Renamed")
    assert halt.failures == [(FailedWrite.SESSION_RECORD, "Couldn't save the session: disk full")]
    assert published.errors == [UNLOCKABLE]


def failed_rename() -> tuple[SessionKeeper, Published, FakeStore]:
    """A keeper, what it publishes and its store, whose rename of the saved session ``other``
    failed to save; the store works again."""
    store = FakeStore()
    store.saved = [SessionRecord(id="other", name="other", created=START, last_active=START)]
    store.failure = "disk full"
    keeper, published = keeper_for(store)
    return keeper, published, store


async def test_a_retried_rename_that_works_is_not_saved_again() -> None:
    keeper, _, store = failed_rename()
    await keeper.rename_saved("other", "Renamed")
    store.failure = None

    assert await keeper.retry(lambda: EMPTY) is None
    assert store.last.name == "Renamed" and len(store.saved) == 2
    assert await keeper.retry(lambda: EMPTY) is None
    assert len(store.saved) == 2 and store.released == ["other", "other"]


async def test_a_retried_rename_that_fails_again_is_kept_to_retry() -> None:
    keeper, _, store = failed_rename()
    await keeper.rename_saved("other", "Renamed")

    error = await keeper.retry(lambda: EMPTY)
    assert isinstance(error, RuntimeError) and str(error) == "disk full"
    store.failure = None
    assert await keeper.retry(lambda: EMPTY) is None
    assert store.last.name == "Renamed" and store.released == ["other"] * 3


async def test_a_pending_rename_of_a_session_since_deleted_is_dropped_and_reported() -> None:
    keeper, published, store = failed_rename()
    await keeper.rename_saved("other", "Renamed")
    store.saved, store.failure = [], None

    assert await keeper.retry(lambda: EMPTY) is None
    assert published.codes == [UNKNOWN]
    store.saved = [SessionRecord(id="other", name="other", created=START, last_active=START)]
    assert await keeper.retry(lambda: EMPTY) is None
    assert store.last.name == "other"


async def test_a_pending_rename_of_a_session_open_elsewhere_is_dropped_and_reported() -> None:
    keeper, published, store = failed_rename()
    await keeper.rename_saved("other", "Renamed")
    store.elsewhere, store.failure = {"other"}, None

    assert await keeper.retry(lambda: EMPTY) is None
    assert published.codes == [LOCKED]
    store.elsewhere = set()
    assert await keeper.retry(lambda: EMPTY) is None
    assert store.last.name == "other"


async def retried_through(store: FakeStore, keeper: SessionKeeper) -> None:
    """Check that retrying ``keeper``'s pending rename while ``store`` has a problem fails,
    keeping it, and that it's renamed once ``store`` works, every lock taken let go of."""
    error = await keeper.retry(lambda: EMPTY)
    assert isinstance(error, RuntimeError) and "other" in str(error)
    assert store.last.name == "other" and store.locked == set()
    store.list_failure = store.lock_failure = store.load_failure = None
    assert await keeper.retry(lambda: EMPTY) is None
    assert store.last.name == "Renamed" and store.locked == set()


async def test_a_pending_rename_retried_while_the_sessions_cannot_be_listed_is_kept() -> None:
    keeper, _, store = failed_rename()
    await keeper.rename_saved("other", "Renamed")
    store.failure, store.list_failure = None, RuntimeError(LISTING_FAILED)
    await retried_through(store, keeper)


async def test_a_pending_rename_retried_while_its_lock_cannot_be_taken_is_kept() -> None:
    keeper, _, store = failed_rename()
    await keeper.rename_saved("other", "Renamed")
    store.failure, store.lock_failure = None, "Couldn't lock the session: read-only"
    await retried_through(store, keeper)


async def test_a_pending_rename_retried_while_its_session_cannot_be_read_is_kept() -> None:
    keeper, _, store = failed_rename()
    await keeper.rename_saved("other", "Renamed")
    store.failure, store.load_failure = None, "Couldn't read the session: permission denied"
    await retried_through(store, keeper)
    assert store.released == ["other"] * 3


async def test_a_rename_that_works_tells_the_halt_nothing() -> None:
    store = FakeStore()
    store.saved = [SessionRecord(id="other", name="other", created=START, last_active=START)]
    keeper, halt = halting_keeper(store)
    await keeper.rename_saved("other", "Renamed")
    assert halt.failures == [] and store.last.name == "Renamed"
    assert store.released == ["other"]


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
    assert published.codes == [SAVE_FAILED, SAVE_FAILED]


async def test_a_session_that_cannot_be_deleted_on_leaving_lets_go_of_its_lock() -> None:
    store = FakeStore()
    store.saved = [SessionRecord(id="old", name="old", created=START, last_active=START)]
    store.locked = {"old"}
    store.delete_failure = "Couldn't delete the session: read-only"
    keeper, published = keeper_for(store)
    await keeper.leave(Left("old"), discard=True)
    assert published.errors == ["Couldn't delete the session: read-only"]
    assert published.codes == [SAVE_FAILED]
    assert store.released == ["old"] and store.locked == set()


async def test_a_session_that_cannot_be_deleted_or_let_go_of_on_leaving_reports_both() -> None:
    store = unreleasable_store_with("old")
    store.delete_failure = "Couldn't delete the session: read-only"
    keeper, published = keeper_for(store)
    await keeper.leave(Left("old"), discard=True)
    assert published.errors == [UNLOCKABLE, "Couldn't delete the session: read-only"]
    assert published.codes == [SAVE_FAILED, SAVE_FAILED]


async def test_a_session_unreadable_once_locked_is_not_opened_and_lets_go_of_its_lock() -> None:
    store = FakeStore()
    store.saved = [SessionRecord(id="other", name="other", created=START, last_active=START)]

    async def break_it() -> None:
        store.broken["other"] = "it isn't JSON"

    store.on_acquire = break_it
    keeper, published = keeper_for(store)
    loaded: list[SessionRecord] = []

    with pytest.raises(Refused, match="it isn't JSON"):
        await keeper.open("other", loaded.append)
    assert published.errors == []
    assert loaded == [] and keeper.id == "session-1"
    assert store.released == ["other"] and store.locked == set()


async def test_an_open_whose_reread_fails_is_reported_and_lets_go_of_its_lock() -> None:
    store = FakeStore()
    store.saved = [SessionRecord(id="other", name="other", created=START, last_active=START)]
    store.load_failure = "Couldn't read the session: permission denied"
    keeper, published = keeper_for(store)
    assert await keeper.open("other", lambda record: None) is None
    assert published.errors == ["Couldn't read the session: permission denied"]
    assert keeper.id == "session-1"
    assert store.released == ["other"] and store.locked == set()


async def test_opening_the_session_open_already_takes_no_lock_and_loads_nothing() -> None:
    store = FakeStore()
    store.saved = [SessionRecord(id="session-1", name="today", created=START, last_active=START)]
    keeper, published = keeper_for(store)
    loaded: list[SessionRecord] = []
    assert await keeper.open("session-1", loaded.append) is ALREADY_OPEN
    assert loaded == [] and published.errors == []
    assert store.locked == set() and store.released == []


LISTING_FAILED = "Couldn't list the sessions in orbit.db: busy"


def unlistable_store_with(session_id: str) -> FakeStore:
    """A store holding the session ``session_id`` whose sessions can't be listed."""
    store = FakeStore()
    store.saved = [SessionRecord(id=session_id, name=session_id, created=START, last_active=START)]
    store.list_failure = RuntimeError(LISTING_FAILED)
    return store


async def test_sessions_that_cannot_be_listed_are_reported_and_none_are_summarised() -> None:
    keeper, published = keeper_for(unlistable_store_with("other"))
    assert await keeper.summaries() == ()
    assert published.errors == [LISTING_FAILED]
    assert published.codes == [SAVE_FAILED]


async def test_an_open_while_the_sessions_cannot_be_listed_is_reported_not_refused() -> None:
    store = unlistable_store_with("other")
    keeper, published = keeper_for(store)
    loaded: list[SessionRecord] = []
    assert await keeper.open("other", loaded.append) is None
    assert published.errors == [LISTING_FAILED]
    assert loaded == [] and keeper.id == "session-1" and store.locked == set()


async def test_a_rename_or_delete_while_the_sessions_cannot_be_listed_does_nothing() -> None:
    store = unlistable_store_with("other")
    keeper, published = keeper_for(store)
    await keeper.rename_saved("other", "Renamed")
    await keeper.delete_saved("other")
    assert published.errors == [LISTING_FAILED]
    assert store.last.name == "other" and store.locked == set()


async def test_listings_failing_in_a_row_are_reported_once() -> None:
    keeper, published = keeper_for(unlistable_store_with("other"))
    assert await keeper.summaries() == ()
    assert await keeper.summaries() == ()
    assert published.errors == [LISTING_FAILED]


async def test_a_listing_failing_after_one_that_worked_is_reported_again() -> None:
    store = unlistable_store_with("other")
    keeper, published = keeper_for(store)
    await keeper.summaries()
    store.list_failure = None
    assert [summary.id for summary in await keeper.summaries()] == ["other"]
    store.list_failure = RuntimeError(LISTING_FAILED)
    assert await keeper.summaries() == ()
    assert published.errors == [LISTING_FAILED, LISTING_FAILED]


def forgetting_keeper(store: FakeStore) -> tuple[SessionKeeper, list[str]]:
    """A keeper over ``store``, making ``session-1`` then ``session-2`` and so on, and the
    sessions whose logged events it has removed."""
    forgotten: list[str] = []
    ids = iter(f"session-{n}" for n in range(1, 100))
    new_sessions = NewSessions(Ticks(), lambda: next(ids), forgotten.append)
    return SessionKeeper(store, Published(), FakeHalt(), new_sessions), forgotten


def no_commit() -> None:
    pass


async def test_an_unused_session_closed_has_its_logged_events_removed() -> None:
    keeper, forgotten = forgetting_keeper(FakeStore())
    await keeper.close()
    assert forgotten == ["session-1"]


async def test_a_saved_session_closed_keeps_its_logged_events() -> None:
    keeper, forgotten = forgetting_keeper(FakeStore())
    await save(keeper, WITH_TASK)
    await keeper.close()
    assert forgotten == []


async def test_an_unused_session_left_has_its_logged_events_removed() -> None:
    keeper, forgotten = forgetting_keeper(FakeStore())
    left = await keeper.renew(None, no_commit)
    await keeper.leave(left, discard=False)
    assert left == Left(None) and forgotten == ["session-1"]


async def test_a_saved_session_left_keeps_its_logged_events() -> None:
    keeper, forgotten = forgetting_keeper(FakeStore())
    await save(keeper, WITH_TASK)
    await keeper.leave(await keeper.renew(None, no_commit), discard=False)
    assert forgotten == []


async def test_a_session_discarded_has_its_logged_events_removed_after_its_deletion() -> None:
    store = FakeStore()
    keeper, forgotten = forgetting_keeper(store)
    await save(keeper, WITH_TASK)
    await keeper.leave(await keeper.renew(None, no_commit), discard=True)
    assert store.deleted == ["session-1"] and forgotten == ["session-1"]


async def test_a_saved_session_deleted_has_its_logged_events_removed() -> None:
    store = FakeStore()
    store.saved = [SessionRecord(id="other", name="other", created=START, last_active=START)]
    keeper, forgotten = forgetting_keeper(store)
    await keeper.delete_saved("other")
    assert store.deleted == ["other"] and forgotten == ["other"]


async def test_a_session_that_cannot_be_deleted_keeps_its_logged_events() -> None:
    store = FakeStore()
    store.saved = [SessionRecord(id="other", name="other", created=START, last_active=START)]
    store.delete_failure = "Couldn't delete the session: read-only"
    keeper, forgotten = forgetting_keeper(store)
    await keeper.delete_saved("other")
    assert forgotten == []


def loads(record: SessionRecord) -> None:
    pass


async def test_opening_a_saved_session_catches_up_on_its_log_once_locked_before_switching() -> None:
    store = FakeStore()
    store.saved = [SessionRecord(id="other", name="other", created=START, last_active=START)]
    caught_up: list[tuple[str, set[str]]] = []

    async def catch_up() -> None:
        caught_up.append((keeper.id, set(store.locked)))

    keeper = SessionKeeper(
        store, Published(), FakeHalt(), NewSessions(Ticks(), lambda: "one", catch_up=catch_up)
    )
    await keeper.open("other", loads)

    assert caught_up == [("one", {"other"})] and keeper.id == "other"


async def test_an_open_cancelled_while_catching_up_lets_go_of_its_lock() -> None:
    store = FakeStore()
    store.saved = [SessionRecord(id="other", name="other", created=START, last_active=START)]
    catching_up = asyncio.Event()

    async def catch_up() -> None:
        catching_up.set()
        await asyncio.Event().wait()

    keeper = SessionKeeper(
        store, Published(), FakeHalt(), NewSessions(Ticks(), lambda: "one", catch_up=catch_up)
    )
    opening = asyncio.create_task(keeper.open("other", loads))
    await catching_up.wait()
    opening.cancel()

    with pytest.raises(asyncio.CancelledError):
        await opening
    assert store.released == ["other"] and store.locked == set()
    assert keeper.id == "one" and not keeper.used
