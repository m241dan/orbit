"""What every session store does, run against the SQLite adapter."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from orbit.agent.application.ports import SessionStore, StoredSession
from orbit.agent.domain.sessions import Unreadable
from tests.store_views import OwnView, open_view, record

type Opener = Callable[[], Awaitable[SessionStore]]


@pytest.fixture
async def open_store(tmp_path: Path) -> AsyncIterator[Opener]:
    """Opens another orbit's store on the same saved sessions each time it's called."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    opened: list[OwnView] = []

    async def open_sqlite() -> SessionStore:
        opened.append(await open_view(tmp_path / "state", workspace))
        return opened[-1]

    yield open_sqlite
    for store in opened:
        await store.close()


# Saving and loading


async def test_a_saved_record_loads_back_and_saving_again_replaces_it(open_store: Opener) -> None:
    store = await open_store()
    await store.save(record(name="first"))
    assert await store.load("abc") == record(name="first")
    await store.save(record(name="second"))
    assert await store.load("abc") == record(name="second")
    assert await (await open_store()).load("abc") == record(name="second")


async def test_a_missing_session_is_unreadable(open_store: Opener) -> None:
    assert isinstance(await (await open_store()).load("abc"), Unreadable)


@pytest.mark.parametrize("session_id", ["", "a/b", "../abc", "."])
async def test_an_id_that_isnt_a_plain_file_name_is_refused(
    open_store: Opener, session_id: str
) -> None:
    store = await open_store()
    with pytest.raises(ValueError, match="Not a session id"):
        await store.load(session_id)
    with pytest.raises(ValueError, match="Not a session id"):
        await store.save(record().model_copy(update={"id": session_id}))
    with pytest.raises(ValueError, match="Not a session id"):
        await store.delete(session_id)
    with pytest.raises(ValueError, match="Not a session id"):
        await store.acquire(session_id)


# Listing and deleting


async def test_without_sessions_none_are_listed(open_store: Opener) -> None:
    assert await (await open_store()).sessions() == []


async def test_without_a_saved_review_count_it_reads_as_zero(open_store: Opener) -> None:
    assert await (await open_store()).review_count() == 0


async def test_listing_gives_the_sessions_by_id_marking_those_another_orbit_holds(
    open_store: Opener,
) -> None:
    store, other = await open_store(), await open_store()
    for session_id in ("c", "a", "b"):
        await store.save(record(session_id))
    await store.acquire("a")
    await other.acquire("b")
    assert await store.sessions() == [
        StoredSession("a", record("a"), locked=False),
        StoredSession("b", record("b"), locked=True),
        StoredSession("c", record("c"), locked=False),
    ]
    assert [entry.locked for entry in await other.sessions()] == [True, False, False]


async def test_deleting_removes_the_session_and_lets_go_of_its_lock(open_store: Opener) -> None:
    store = await open_store()
    await store.save(record())
    await store.acquire("abc")
    await store.delete("abc")
    await store.delete("never")
    assert await store.sessions() == []
    assert isinstance(await store.load("abc"), Unreadable)
    assert await (await open_store()).acquire("abc")


# Locks


async def test_acquiring_a_lock_we_hold_succeeds(open_store: Opener) -> None:
    store = await open_store()
    assert await store.acquire("abc")
    assert await store.acquire("abc")


async def test_a_lock_another_orbit_holds_is_refused_until_released(open_store: Opener) -> None:
    holder, store = await open_store(), await open_store()
    assert await holder.acquire("abc")
    assert not await store.acquire("abc")
    await holder.release("abc")
    assert await store.acquire("abc")


async def test_releasing_leaves_another_orbits_lock_and_a_missing_one_alone(
    open_store: Opener,
) -> None:
    holder, store = await open_store(), await open_store()
    await holder.acquire("abc")
    await store.release("abc")
    await store.release("never")
    assert not await store.acquire("abc")


async def test_two_orbits_acquiring_at_once_cant_both_win(open_store: Opener) -> None:
    stores = [await open_store() for _ in range(8)]
    won = await asyncio.gather(*(store.acquire("abc") for store in stores))
    assert won.count(True) == 1


# The super review count


async def test_the_review_count_is_zero_until_saved_then_loads_back(open_store: Opener) -> None:
    store = await open_store()
    assert await store.review_count() == 0
    await store.save_review_count(3)
    await store.save_review_count(4)
    assert await store.review_count() == 4
    assert await (await open_store()).review_count() == 4
