"""The rules every session store keeps, checked on the fake the other tests use and on the real
SQLite store alike, so that tests built on the fake hold for the real one."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from orbit.agent.application.ports import ReadFailed, SessionStore
from orbit.agent.domain.sessions import Unreadable
from tests.agent_fakes import FakeStore, notice
from tests.store_views import Opener, database, record


@dataclass(frozen=True)
class Subject:
    """A ``store`` under test, how to ``spoil`` a session saved in it, leaving its record
    unreadable, and how to ``break_down`` its database, leaving its log unreadable."""

    store: SessionStore
    spoil: Callable[[str], None]
    break_down: Callable[[], Awaitable[None]]


def fake_subject() -> Subject:
    store = FakeStore()

    def spoil(session_id: str) -> None:
        store.broken[session_id] = "it isn't JSON"

    async def break_down() -> None:
        store.after_failure = store.bounds_failure = "database is locked"

    return Subject(store, spoil, break_down)


async def sqlite_subject(open_store: Opener, state: Path, workspace: Path) -> Subject:
    store = await open_store(workspace)

    def spoil(session_id: str) -> None:
        with database(state) as connection:
            connection.execute("UPDATE sessions SET record = '{oops' WHERE id = ?", (session_id,))

    return Subject(store, spoil, store.close)


@pytest.fixture(params=["fake", "sqlite"])
async def subject(
    request: pytest.FixtureRequest, open_store: Opener, state: Path, workspace: Path
) -> Subject:
    """Each store in turn: the fake, and a real one over a database in ``state``."""
    if request.param == "fake":
        return fake_subject()
    return await sqlite_subject(open_store, state, workspace)


async def test_a_reserved_seq_raises_the_mark(subject: Subject) -> None:
    store = subject.store
    await store.append("a", [notice(3)])
    await store.reserve(10)
    assert store.max_seq() == 10
    assert await store.latest_seq() == 10


async def test_a_reservation_below_the_mark_leaves_it(subject: Subject) -> None:
    store = subject.store
    await store.append("a", [notice(30)])
    await store.reserve(10)
    assert store.max_seq() == 30


async def test_deleting_a_session_removes_its_events_and_counts_them_as_anothers(
    subject: Subject,
) -> None:
    store = subject.store
    await store.save(record("a"))
    await store.append("b", [notice(1, "b")])
    await store.append("a", [notice(2), notice(3)])
    await store.delete("a")
    assert await store.recent("a", 10) == []
    assert await store.after("a", 0) == []
    bounds = await store.bounds("b", 1)
    assert bounds is not None and bounds.others_seq == 3
    assert store.max_seq() == 3


async def test_forgetting_a_session_removes_its_events_and_counts_them_as_anothers(
    subject: Subject,
) -> None:
    store = subject.store
    await store.append("b", [notice(1, "b")])
    await store.append("a", [notice(2), notice(3)])
    await store.forget("a")
    assert await store.recent("a", 10) == []
    bounds = await store.bounds("b", 1)
    assert bounds is not None and bounds.others_seq == 3
    assert store.max_seq() == 3


async def test_a_session_appended_after_another_was_forgotten_still_counts_it(
    subject: Subject,
) -> None:
    store = subject.store
    await store.append("a", [notice(5)])
    await store.forget("a")
    await store.append("b", [notice(6, "b")])
    bounds = await store.bounds("b", 6)
    assert bounds is not None
    assert (bounds.first_seq, bounds.others_seq, bounds.own_seq) == (6, 5, 6)


async def test_a_missing_session_is_unreadable(subject: Subject) -> None:
    assert isinstance(await subject.store.load("abc"), Unreadable)


async def test_a_broken_session_is_unreadable(subject: Subject) -> None:
    await subject.store.save(record("abc"))
    subject.spoil("abc")
    assert isinstance(await subject.store.load("abc"), Unreadable)


async def test_appending_skips_the_events_held_already(subject: Subject) -> None:
    store = subject.store
    await store.append("a", [notice(1), notice(2)])
    assert await store.append("a", [notice(2), notice(3)]) == 1
    assert await store.recent("a", 10) == [notice(1), notice(2), notice(3)]


async def test_events_after_a_seq_that_cant_be_read_raise(subject: Subject) -> None:
    await subject.store.append("a", [notice(1)])
    await subject.break_down()
    with pytest.raises(ReadFailed):
        await subject.store.after("a", 0)


async def test_bounds_that_cant_be_read_raise(subject: Subject) -> None:
    await subject.store.append("a", [notice(1)])
    await subject.break_down()
    with pytest.raises(ReadFailed):
        await subject.store.bounds("a", 1)
