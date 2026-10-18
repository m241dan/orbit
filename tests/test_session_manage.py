"""Renaming and deleting saved sessions from the list of sessions, which is listed again after."""

from orbit.agent import AgentSession
from orbit.protocol import (
    AddTask,
    DeleteSession,
    ErrorOccurred,
    RenameSession,
    SessionsListed,
    SessionSummary,
)
from tests.agent_fakes import FakeStore, events_until, of_type, send
from tests.test_session_switch import EARLIER, TASK, last_of, messages, saved, store_with, switching

Managing = RenameSession | DeleteSession


async def listed_after(session: AgentSession, command: Managing) -> tuple[SessionSummary, ...]:
    """The sessions listed after sending ``command``."""
    await send(session, command)
    return last_of(SessionsListed, await events_until(session, of_type(SessionsListed))).sessions


async def errors_of(session: AgentSession, command: Managing) -> list[str]:
    """The errors sending ``command`` reports before the sessions are listed again."""
    await send(session, command)
    return messages(ErrorOccurred, await events_until(session, of_type(SessionsListed)))


def names(sessions: tuple[SessionSummary, ...]) -> dict[str, str]:
    return {s.id: s.name for s in sessions}


async def test_the_sessions_listed_after_a_rename_or_delete_are_refreshed() -> None:
    session, _ = switching(store_with(saved("old"), saved("gone")))
    for command in (RenameSession(session_id="old", name="new"), DeleteSession(session_id="gone")):
        await send(session, command)
        listed = last_of(SessionsListed, await events_until(session, of_type(SessionsListed)))
        assert listed.reason == "refreshed"
    await session.close()


async def test_a_saved_session_is_renamed_under_its_lock() -> None:
    store = store_with(saved("old"))
    session, _ = switching(store)
    listed = await listed_after(session, RenameSession(session_id="old", name=" auth rework "))

    assert names(listed) == {"old": "auth rework"}
    assert store.last == saved("old", name="auth rework")
    assert store.released == ["old"] and store.locked == set()
    await session.close()


async def test_the_open_session_is_renamed_and_saved_at_once() -> None:
    store = FakeStore()
    session, _ = switching(store)
    await send(session, AddTask(task=TASK))
    listed = await listed_after(session, RenameSession(session_id="session-1", name="flags"))

    assert names(listed) == {"session-1": "flags"}
    assert store.last.name == "flags"
    assert session.saved_session.name == "flags"
    await session.close()


async def test_renaming_a_new_unsaved_session_only_names_it() -> None:
    store = FakeStore()
    session, _ = switching(store)
    listed = await listed_after(session, RenameSession(session_id="session-1", name="flags"))

    assert listed == ()
    assert session.saved_session.name == "flags"
    assert store.saved == []
    await session.close()


async def test_an_empty_name_is_refused() -> None:
    store = store_with(saved("old"))
    session, _ = switching(store)
    errors = await errors_of(session, RenameSession(session_id="old", name="  "))

    assert errors == ["A session needs a name."]
    assert store.saved == [saved("old")]
    await session.close()


async def test_renaming_a_session_open_in_another_orbit_is_refused() -> None:
    store = store_with(saved("old"))
    store.elsewhere.add("old")
    session, _ = switching(store)
    errors = await errors_of(session, RenameSession(session_id="old", name="new"))

    assert errors == ["Can't rename \"old\": it's open in another orbit."]
    assert store.saved == [saved("old")]
    await session.close()


async def test_renaming_a_session_another_orbit_locks_meanwhile_is_refused() -> None:
    store = store_with(saved("old"))
    store.free = False
    session, _ = switching(store)
    errors = await errors_of(session, RenameSession(session_id="old", name="new"))

    assert errors == ["Can't rename \"old\": it's open in another orbit."]
    assert store.saved == [saved("old")]
    await session.close()


async def test_renaming_an_unreadable_session_is_refused() -> None:
    store = FakeStore()
    store.broken["bad"] = "it isn't JSON"
    session, _ = switching(store)
    errors = await errors_of(session, RenameSession(session_id="bad", name="new"))

    assert errors == ["Can't rename the session bad: it isn't JSON."]
    await session.close()


async def test_renaming_a_session_that_isnt_saved_is_refused() -> None:
    session = AgentSession()
    errors = await errors_of(session, RenameSession(session_id="gone", name="new"))
    assert errors == ["There's no saved session 'gone'."]
    await session.close()


async def test_a_rename_that_cannot_be_saved_is_reported_and_lets_go_of_the_lock() -> None:
    store = store_with(saved("old"))
    store.failure = "Disk full."
    session, _ = switching(store)
    errors = await errors_of(session, RenameSession(session_id="old", name="new"))

    assert errors == ["Disk full."]
    assert store.released == ["old"] and store.locked == set()
    await session.close()


async def test_a_rename_renames_the_record_saved_by_the_time_the_lock_is_taken() -> None:
    store = store_with(saved("old"))
    newer = saved("old", last_active=EARLIER)

    async def save_newer() -> None:
        store.saved.append(newer)

    store.on_acquire = save_newer
    session, _ = switching(store)
    await listed_after(session, RenameSession(session_id="old", name="new"))

    assert store.last == newer.model_copy(update={"name": "new"})
    assert store.released == ["old"] and store.locked == set()
    await session.close()


async def test_a_session_unreadable_by_the_time_the_lock_is_taken_is_not_renamed() -> None:
    store = store_with(saved("old"))

    async def break_it() -> None:
        store.broken["old"] = "it isn't JSON"

    store.on_acquire = break_it
    session, _ = switching(store)
    errors = await errors_of(session, RenameSession(session_id="old", name="new"))

    assert errors == ["Can't rename the session old: it isn't JSON."]
    assert store.saved == [saved("old")]
    assert store.released == ["old"] and store.locked == set()
    await session.close()


async def test_a_saved_session_is_deleted() -> None:
    store = store_with(saved("old"), saved("kept"))
    session, _ = switching(store)
    listed = await listed_after(session, DeleteSession(session_id="old"))

    assert names(listed) == {"kept": "kept"}
    assert store.deleted == ["old"]
    await session.close()


async def test_an_unreadable_session_can_be_deleted() -> None:
    store = FakeStore()
    store.broken["bad"] = "it isn't JSON"
    session, _ = switching(store)
    await send(session, DeleteSession(session_id="bad"))
    await events_until(session, of_type(SessionsListed))

    assert store.deleted == ["bad"]
    await session.close()


async def test_deleting_the_open_session_is_refused() -> None:
    store = FakeStore()
    session, _ = switching(store)
    await send(session, AddTask(task=TASK))
    errors = await errors_of(session, DeleteSession(session_id="session-1"))

    assert errors == ["Can't delete the open session."]
    assert store.deleted == []
    await session.close()


async def test_deleting_a_session_open_in_another_orbit_is_refused() -> None:
    store = store_with(saved("old"))
    store.elsewhere.add("old")
    session, _ = switching(store)
    errors = await errors_of(session, DeleteSession(session_id="old"))

    assert errors == ["Can't delete \"old\": it's open in another orbit."]
    assert store.deleted == []
    await session.close()


async def test_deleting_a_session_another_orbit_locks_meanwhile_is_refused() -> None:
    store = store_with(saved("old"))
    store.free = False
    session, _ = switching(store)
    errors = await errors_of(session, DeleteSession(session_id="old"))

    assert errors == ["Can't delete \"old\": it's open in another orbit."]
    assert store.deleted == []
    await session.close()


async def test_deleting_a_session_that_isnt_saved_is_refused() -> None:
    session, _ = switching(FakeStore())
    errors = await errors_of(session, DeleteSession(session_id="gone"))
    assert errors == ["There's no saved session 'gone'."]
    await session.close()


async def test_a_deletion_that_fails_is_reported_and_lets_go_of_the_lock() -> None:
    store = store_with(saved("old"))
    store.delete_failure = "Permission denied."
    session, _ = switching(store)
    errors = await errors_of(session, DeleteSession(session_id="old"))

    assert errors == ["Permission denied."]
    assert store.released == ["old"] and store.locked == set()
    await session.close()
