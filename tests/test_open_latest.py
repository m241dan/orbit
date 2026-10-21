"""Opening the most recently active saved session in place of the new one, as ``orbit serve``
does at startup."""

from datetime import timedelta

from tests.agent_fakes import FakeStore
from tests.session_builders import YESTERDAY, saved, store_with, switching

LATER = YESTERDAY + timedelta(hours=1)


async def test_the_most_recently_active_saved_session_is_opened() -> None:
    session, _ = switching(store_with(saved("old"), saved("recent", last_active=LATER)))
    await session.open_latest()
    assert session.saved_session.id == "recent"
    await session.close()


async def test_sessions_open_in_another_orbit_or_unreadable_are_passed_over() -> None:
    store = store_with(saved("old"), saved("taken", last_active=LATER))
    store.elsewhere.add("taken")
    store.broken["broken"] = "it isn't JSON"
    session, _ = switching(store)
    await session.open_latest()
    assert session.saved_session.id == "old"
    await session.close()


async def test_without_saved_sessions_the_new_one_stays_open() -> None:
    session, _ = switching(FakeStore())
    await session.open_latest()
    assert session.saved_session.id == "session-1"
    await session.close()


async def test_a_session_that_cant_be_opened_leaves_the_new_one_open() -> None:
    store = store_with(saved("auth"))
    store.free = False
    session, _ = switching(store)
    await session.open_latest()
    assert session.saved_session.id == "session-1"
    await session.close()
