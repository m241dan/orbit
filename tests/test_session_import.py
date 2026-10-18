"""Importing an older orbit's session files into the SQLite store."""

import fcntl
import os
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbit.agent import ImportBlocked, import_session_files
from orbit.agent.domain.sessions import (
    SessionRecord,
    Unreadable,
    record_json,
    review_count_json,
)
from orbit.agent.external import sqlite_store
from orbit.agent.external.session_import import imported_note
from orbit.agent.external.sqlite_store import SqliteStore

WHEN = datetime(2026, 10, 17, 14, 30, tzinfo=UTC)


def record(session_id: str, name: str = "Oct 17 14:30") -> SessionRecord:
    return SessionRecord(id=session_id, name=name, created=WHEN, last_active=WHEN)


@pytest.fixture
def legacy(tmp_path: Path) -> Path:
    """An older orbit's state directory, holding two sessions, an unreadable one and a review
    count of 3."""
    path = tmp_path / "legacy"
    sessions = path / "sessions"
    sessions.mkdir(parents=True)
    for session_id in ("one", "two"):
        (sessions / f"{session_id}.json").write_text(record_json(record(session_id)))
    (sessions / "bad.json").write_text("not json")
    (path / "reviews.json").write_text(review_count_json(3))
    return path


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    workspace = tmp_path / "project"
    workspace.mkdir()
    opened = await SqliteStore.open(tmp_path / "state", str(workspace))
    yield opened
    await opened.close()


@contextmanager
def held(lock: Path) -> Iterator[None]:
    """Hold ``lock`` as another orbit would."""
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def read_only(directory: Path) -> Iterator[None]:
    directory.chmod(0o500)
    try:
        yield
    finally:
        directory.chmod(0o700)


async def ids(store: SqliteStore) -> list[str]:
    return [stored.id for stored in await store.sessions()]


async def test_the_files_are_imported_and_the_folder_renamed(
    store: SqliteStore, legacy: Path
) -> None:
    assert await import_session_files(store, legacy) == 3
    assert await ids(store) == ["bad", "one", "two"]
    assert await store.load("one") == record("one")
    assert await store.review_count() == 3
    assert not (legacy / "sessions").exists()
    assert (legacy / "sessions.imported" / "one.json").exists()
    assert (legacy / "reviews.json.imported").exists()
    assert not (legacy / "reviews.json").exists()


async def test_an_unreadable_file_is_listed_unreadable_with_its_text_kept(
    store: SqliteStore, legacy: Path, tmp_path: Path
) -> None:
    await import_session_files(store, legacy)
    bad = next(stored for stored in await store.sessions() if stored.id == "bad")
    assert isinstance(bad.record, Unreadable)
    with closing(sqlite3.connect(tmp_path / "state" / "orbit.db")) as connection:
        row = connection.execute("SELECT name, last_active, record FROM sessions WHERE id = 'bad'")
        assert row.fetchone() == ("bad", "", "not json")


async def test_a_file_whose_record_has_another_id_is_imported_under_the_files_name(
    store: SqliteStore, legacy: Path
) -> None:
    (legacy / "sessions" / "copy.json").write_text(record_json(record("one", name="Copy")))
    assert await import_session_files(store, legacy) == 4
    assert await ids(store) == ["bad", "copy", "one", "two"]
    copy = await store.load("copy")
    assert isinstance(copy, SessionRecord) and copy.name == "Copy"
    one = await store.load("one")
    assert isinstance(one, SessionRecord) and one.name == "Oct 17 14:30"


async def test_a_record_whose_id_isnt_a_file_name_can_still_be_listed_and_loaded(
    store: SqliteStore, legacy: Path
) -> None:
    (legacy / "sessions" / "x.json").write_text(record_json(record("../x")))
    assert await import_session_files(store, legacy) == 4
    assert await ids(store) == ["bad", "one", "two", "x"]
    assert isinstance(await store.load("x"), SessionRecord)


async def test_a_file_that_isnt_utf_8_is_listed_unreadable(
    store: SqliteStore, legacy: Path
) -> None:
    text = record_json(record("latin", name="Caf\u00e9"))
    (legacy / "sessions" / "latin.json").write_bytes(text.encode("latin-1"))
    assert await import_session_files(store, legacy) == 4
    latin = next(stored for stored in await store.sessions() if stored.id == "latin")
    assert isinstance(latin.record, Unreadable)
    assert isinstance(await store.load("latin"), Unreadable)


async def test_without_a_sessions_folder_nothing_is_done(
    store: SqliteStore, tmp_path: Path
) -> None:
    (tmp_path / "reviews.json").write_text(review_count_json(3))
    assert await import_session_files(store, tmp_path) is None
    assert await ids(store) == []
    assert await store.review_count() == 0
    assert (tmp_path / "reviews.json").exists()


async def test_without_a_review_count_only_the_sessions_are_imported(
    store: SqliteStore, legacy: Path
) -> None:
    (legacy / "reviews.json").unlink()
    await store.save_review_count(5)
    assert await import_session_files(store, legacy) == 3
    assert await store.review_count() == 5


async def test_saved_sessions_and_review_count_are_never_overwritten(
    store: SqliteStore, legacy: Path
) -> None:
    await store.save(record("one", name="Mine"))
    await store.save_review_count(7)
    assert await import_session_files(store, legacy) == 2
    loaded = await store.load("one")
    assert isinstance(loaded, SessionRecord) and loaded.name == "Mine"
    assert await store.review_count() == 7


async def test_importing_twice_imports_once(store: SqliteStore, legacy: Path) -> None:
    assert await import_session_files(store, legacy) == 3
    assert await import_session_files(store, legacy) is None
    assert await ids(store) == ["bad", "one", "two"]


async def test_a_session_open_in_another_orbit_blocks_the_import(
    store: SqliteStore, legacy: Path
) -> None:
    (legacy / "sessions" / "free.lock").touch()
    with (
        held(legacy / "sessions" / "one.lock"),
        pytest.raises(ImportBlocked, match=r"sessions in .*legacy/sessions: an orbit still has"),
    ):
        await import_session_files(store, legacy)
    assert await ids(store) == []
    assert await store.review_count() == 0
    assert (legacy / "sessions").is_dir()
    assert (legacy / "reviews.json").exists()


async def test_a_folder_that_cant_be_renamed_blocks_the_import(
    store: SqliteStore, legacy: Path
) -> None:
    with (
        read_only(legacy),
        pytest.raises(ImportBlocked, match=r"Couldn't rename .*legacy/sessions to .*Permission"),
    ):
        await import_session_files(store, legacy)
    assert await ids(store) == []
    assert (legacy / "sessions").is_dir()


async def test_a_review_count_that_cant_be_renamed_puts_the_folder_back(
    store: SqliteStore, legacy: Path
) -> None:
    (legacy / "reviews.json.imported" / "in-the-way").mkdir(parents=True)
    with pytest.raises(ImportBlocked, match=r"Couldn't rename .*legacy/reviews\.json to"):
        await import_session_files(store, legacy)
    assert await ids(store) == []
    assert await store.review_count() == 0
    assert (legacy / "sessions").is_dir()
    assert not (legacy / "sessions.imported").exists()


async def test_a_file_that_cant_be_read_blocks_the_import(store: SqliteStore, legacy: Path) -> None:
    (legacy / "sessions" / "folder.json").mkdir()
    with pytest.raises(ImportBlocked, match=r"Couldn't read the sessions in .*legacy/sessions:"):
        await import_session_files(store, legacy)
    assert await ids(store) == []
    assert (legacy / "sessions").is_dir()


async def test_a_folder_that_cant_be_checked_blocks_the_import(
    store: SqliteStore, legacy: Path
) -> None:
    (legacy / "sessions").chmod(0o300)
    try:
        with pytest.raises(
            ImportBlocked, match=r"Couldn't check the sessions in .*legacy/sessions"
        ):
            await import_session_files(store, legacy)
    finally:
        (legacy / "sessions").chmod(0o700)
    assert await store.review_count() == 0
    assert (legacy / "sessions").is_dir()


async def test_a_database_that_fails_blocks_the_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, legacy: Path
) -> None:
    monkeypatch.setattr(sqlite_store, "BUSY_TIMEOUT", 0.05)
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = await SqliteStore.open(tmp_path / "state", str(workspace))
    try:
        with closing(sqlite3.connect(tmp_path / "state" / "orbit.db", isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")  # Another writer holds the database.
            with pytest.raises(ImportBlocked, match=r"sessions in .*: database is locked"):
                await import_session_files(store, legacy)
            db.execute("ROLLBACK")
        assert await ids(store) == []
    finally:
        await store.close()
    assert (legacy / "sessions").is_dir()


def test_the_import_note_says_how_many_sessions_came_from_where() -> None:
    assert imported_note(3, Path(".git/orbit")) == (
        "Imported 3 saved sessions from .git/orbit/sessions; the old folder is now "
        "sessions.imported."
    )


def test_the_import_note_counts_one_session_as_one() -> None:
    assert imported_note(1, Path(".orbit")).startswith("Imported 1 saved session from ")
