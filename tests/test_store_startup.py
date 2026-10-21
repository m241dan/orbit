"""Opening a workspace's store at startup: importing old session files, then fallback files."""

import asyncio
import fcntl
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbit.agent import ImportBlocked, OpenedStore, RecoveryFailed, open_workspace_store
from orbit.agent.domain.sessions import SessionRecord, record_json
from orbit.agent.external import store_startup
from orbit.agent.external.session_import import Imported
from orbit.agent.external.sqlite_store import SqliteStore, WorkspaceStore
from orbit.protocol import EventEnvelope, Notice, encode
from tests.store_views import open_view

WHEN = datetime(2026, 10, 17, 14, 30, tzinfo=UTC)
SAVED = SessionRecord(id="abc", name="auth rework", created=WHEN, last_active=WHEN)
IMPORT_NOTE = (
    "Imported 1 saved session from .orbit/sessions; the old folder is now sessions.imported."
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A workspace outside git, so its orbit directory is ``.orbit`` in it."""
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture
def state(tmp_path: Path) -> Path:
    """A state directory outside the workspace."""
    return tmp_path / "state"


def legacy_sessions(root: Path) -> Path:
    """An older orbit's sessions folder in ``root``'s orbit directory, holding ``SAVED``."""
    sessions = root / ".orbit" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "abc.json").write_text(record_json(SAVED))
    return sessions


def parked_notice(seq: int) -> EventEnvelope:
    return EventEnvelope(seq=seq, session="s1", payload=Notice(message=f"parked {seq}"))


def park(path: Path, *seqs: int) -> Path:
    """A fallback file at ``path`` holding notices numbered ``seqs``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{encode(parked_notice(seq))}\n" for seq in seqs))
    return path


async def park_in_state(state: Path, root: Path, *seqs: int) -> Path:
    """The session ``s1``'s fallback file in ``state``, holding notices numbered ``seqs``."""
    store = await open_view(state, root)
    workspace_id = store.workspace_id
    await store.close()
    return park(state / "fallback" / f"{workspace_id}-s1.jsonl", *seqs)


def store_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("orbit-sqlite")]


@contextmanager
def held(lock: Path) -> Iterator[None]:
    """Hold ``lock`` as another orbit would."""
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


async def opened_notes(state: Path, root: Path) -> list[str]:
    """The notes on what opening ``root``'s view of a store of its own in ``state`` imported;
    the store is closed after."""
    store = await SqliteStore.open(state)
    try:
        return (await open_workspace_store(store, root, owned=True)).notes
    finally:
        await store.close()


async def logged(state: Path, root: Path) -> list[EventEnvelope]:
    """The session ``s1``'s events in the store in ``state`` for ``root``."""
    store = await open_view(state, root)
    try:
        return await store.recent("s1", 100)
    finally:
        await store.close()


async def test_the_store_is_opened_with_the_workspaces_fallback_files(
    state: Path, root: Path
) -> None:
    store = await SqliteStore.open(state)
    try:
        opened = await open_workspace_store(store, root, owned=True)
        assert isinstance(opened, OpenedStore)
        assert opened.notes == []
        workspace_id = opened.store.workspace_id
        assert opened.fallback.places("s1") == [
            state / "fallback" / f"{workspace_id}-s1.jsonl",
            root / ".orbit" / "fallback" / "s1.jsonl",
        ]
    finally:
        await store.close()


async def test_old_session_files_are_imported_before_fallback_files_are_recovered(
    state: Path, root: Path
) -> None:
    legacy_sessions(root)
    park(root / ".orbit" / "fallback" / "s1.jsonl", 1)

    assert await opened_notes(state, root) == [
        IMPORT_NOTE,
        "Recovered 1 event from .orbit/fallback/s1.jsonl into the session's log.",
    ]


async def test_a_blocked_import_leaves_the_fallback_files_unrecovered(
    state: Path, root: Path
) -> None:
    sessions = legacy_sessions(root)
    path = park(root / ".orbit" / "fallback" / "s1.jsonl", 1)
    with held(sessions / "abc.lock"), pytest.raises(ImportBlocked):
        await opened_notes(state, root)

    assert path.exists()
    assert await logged(state, root) == []


async def test_an_import_of_old_session_files_is_noted_within_the_workspace(
    state: Path, root: Path
) -> None:
    legacy_sessions(root)
    assert await opened_notes(state, root) == [IMPORT_NOTE]


async def test_each_recovered_file_holding_new_events_is_noted(state: Path, root: Path) -> None:
    outside = await park_in_state(state, root, 2, 1)
    park(root / ".orbit" / "fallback" / "s2.jsonl")

    assert await opened_notes(state, root) == [
        f"Recovered 2 events from {outside} into the session's log."
    ]
    assert await logged(state, root) == [parked_notice(1), parked_notice(2)]


async def test_only_the_events_newly_stored_are_counted_in_the_note(
    state: Path, root: Path
) -> None:
    await park_in_state(state, root, 1, 2)
    await opened_notes(state, root)
    path = await park_in_state(state, root, 1, 2, 3)

    assert await opened_notes(state, root) == [
        f"Recovered 1 event from {path} into the session's log."
    ]
    assert not path.exists()


async def test_a_file_whose_events_are_all_stored_is_removed_without_a_note(
    state: Path, root: Path
) -> None:
    await park_in_state(state, root, 1, 2)
    await opened_notes(state, root)
    path = await park_in_state(state, root, 2, 1)

    assert await opened_notes(state, root) == []
    assert not path.exists()


async def test_a_parked_file_in_the_workspace_is_named_within_it(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    state = root / "state"
    path = await park_in_state(state, root, 1)

    assert await opened_notes(state, root) == [
        f"Recovered 1 event from {path.relative_to(root)} into the session's log."
    ]


async def test_a_failed_import_leaves_the_shared_store_open_for_other_workspaces(
    state: Path, root: Path, tmp_path: Path
) -> None:
    sessions = legacy_sessions(root)
    other = tmp_path / "other"
    other.mkdir()
    store = await SqliteStore.open(state)
    try:
        with held(sessions / "abc.lock"), pytest.raises(ImportBlocked):
            await open_workspace_store(store, root, owned=True)
        opened = await open_workspace_store(store, other, owned=True)
        assert await opened.store.sessions() == []
    finally:
        await store.close()


async def test_a_cancelled_import_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, state: Path, root: Path
) -> None:
    async def cancelled(store: WorkspaceStore, legacy_dir: Path) -> Imported | None:
        raise asyncio.CancelledError

    monkeypatch.setattr(store_startup, "import_session_files", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await opened_notes(state, root)

    assert store_threads() == []


async def test_a_failed_recovery_keeps_the_fallback_file(state: Path, root: Path) -> None:
    path = park(root / ".orbit" / "fallback" / "s1.jsonl")
    path.write_text("not an envelope\n")
    with pytest.raises(RecoveryFailed, match="Not JSON"):
        await opened_notes(state, root)

    assert path.exists()


async def opened_unowned(state: Path, root: Path) -> list[str]:
    """The notes on what opening ``root``'s view of a store of its own in ``state`` imported,
    another orbit owning the workspace; the store is closed after."""
    store = await SqliteStore.open(state)
    try:
        return (await open_workspace_store(store, root, owned=False)).notes
    finally:
        await store.close()


async def test_an_orbit_not_owning_the_workspace_leaves_its_fallback_files(
    state: Path, root: Path
) -> None:
    outside = await park_in_state(state, root, 1)
    inside = park(root / ".orbit" / "fallback" / "s2.jsonl", 2)

    assert await opened_unowned(state, root) == []
    assert outside.exists() and inside.exists()
    assert await logged(state, root) == []


async def test_an_orbit_not_owning_the_workspace_still_imports_old_session_files(
    state: Path, root: Path
) -> None:
    legacy_sessions(root)
    assert await opened_unowned(state, root) == [IMPORT_NOTE]


async def test_recovering_later_imports_and_deletes_the_fallback_files_with_notes(
    state: Path, root: Path
) -> None:
    path = park(root / ".orbit" / "fallback" / "s1.jsonl", 2, 1)
    store = await SqliteStore.open(state)
    try:
        opened = await open_workspace_store(store, root, owned=False)
        assert await opened.recover() == [
            "Recovered 2 events from .orbit/fallback/s1.jsonl into the session's log."
        ]
    finally:
        await store.close()

    assert not path.exists()
    assert await logged(state, root) == [parked_notice(1), parked_notice(2)]
