"""Importing an older orbit's session files into the SQLite store."""

import fcntl
import os
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractContextManager, closing, contextmanager
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
from orbit.agent.external import session_import, sqlite_store
from orbit.agent.external.session_import import Imported, imported_note
from orbit.agent.external.session_locks import SessionLocks
from orbit.agent.external.sqlite_store import NewRows, SqliteStore

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


def failing_rename(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make renaming a path called ``name`` fail, as a file the user can't move would."""
    rename = Path.rename

    def renaming(self: Path, target: Path) -> Path:
        if self.name == name:
            raise PermissionError(f"Permission denied: {self}")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", renaming)


def failing_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the import's commit fail once the old files are renamed, as a full disk would."""
    retired = session_import._retired

    def failing(legacy_dir: Path, folders: list[str]) -> AbstractContextManager[None]:
        with retired(legacy_dir, folders):
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(session_import, "_retired", failing)


def first(count: int) -> Imported:
    """A first import of ``count`` sessions, whose folder is now ``sessions.imported``."""
    return Imported(count, "sessions.imported")


async def ids(store: SqliteStore) -> list[str]:
    return [stored.id for stored in await store.sessions()]


async def test_the_files_are_imported_and_the_folder_renamed(
    store: SqliteStore, legacy: Path
) -> None:
    assert await import_session_files(store, legacy) == first(3)
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
    assert await import_session_files(store, legacy) == first(4)
    assert await ids(store) == ["bad", "copy", "one", "two"]
    copy = await store.load("copy")
    assert isinstance(copy, SessionRecord) and copy.name == "Copy"
    one = await store.load("one")
    assert isinstance(one, SessionRecord) and one.name == "Oct 17 14:30"


async def test_a_record_whose_id_isnt_a_file_name_can_still_be_listed_and_loaded(
    store: SqliteStore, legacy: Path
) -> None:
    (legacy / "sessions" / "x.json").write_text(record_json(record("../x")))
    assert await import_session_files(store, legacy) == first(4)
    assert await ids(store) == ["bad", "one", "two", "x"]
    assert isinstance(await store.load("x"), SessionRecord)


async def test_a_file_that_isnt_utf_8_is_listed_unreadable(
    store: SqliteStore, legacy: Path
) -> None:
    text = record_json(record("latin", name="Caf\u00e9"))
    (legacy / "sessions" / "latin.json").write_bytes(text.encode("latin-1"))
    assert await import_session_files(store, legacy) == first(4)
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
    assert await import_session_files(store, legacy) == first(3)
    assert await store.review_count() == 5


async def test_saved_sessions_and_review_count_are_never_overwritten(
    store: SqliteStore, legacy: Path
) -> None:
    await store.save(record("one", name="Mine"))
    await store.save_review_count(7)
    assert await import_session_files(store, legacy) == first(2)
    loaded = await store.load("one")
    assert isinstance(loaded, SessionRecord) and loaded.name == "Mine"
    assert await store.review_count() == 7


async def test_importing_twice_imports_once(store: SqliteStore, legacy: Path) -> None:
    assert await import_session_files(store, legacy) == first(3)
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


async def test_the_files_are_read_only_once_no_orbit_has_a_session_open(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, legacy: Path
) -> None:
    (legacy / "sessions" / "one.lock").touch()
    held_elsewhere = SessionLocks.held_elsewhere

    def saving_last(self: SessionLocks, session_id: str) -> bool:
        # Another orbit saves its last change and lets go just before the check.
        (legacy / "sessions" / "one.json").write_text(record_json(record("one", name="Last")))
        return held_elsewhere(self, session_id)

    monkeypatch.setattr(SessionLocks, "held_elsewhere", saving_last)
    assert await import_session_files(store, legacy) == first(3)
    one = await store.load("one")
    assert isinstance(one, SessionRecord) and one.name == "Last"


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
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, legacy: Path
) -> None:
    failing_rename(monkeypatch, "reviews.json")
    with pytest.raises(ImportBlocked, match=r"Couldn't rename .*legacy/reviews\.json to"):
        await import_session_files(store, legacy)
    assert await ids(store) == []
    assert await store.review_count() == 0
    assert (legacy / "sessions").is_dir()
    assert not (legacy / "sessions.imported").exists()


async def test_earlier_imported_files_are_kept_and_the_new_ones_get_fresh_names(
    store: SqliteStore, legacy: Path
) -> None:
    (legacy / "sessions.imported").mkdir()
    (legacy / "sessions.imported" / "old.json").write_text("earlier")
    (legacy / "reviews.json.imported").write_text("earlier")
    assert await import_session_files(store, legacy) == Imported(3, "sessions.imported-2")
    assert [path.name for path in (legacy / "sessions.imported").iterdir()] == ["old.json"]
    assert (legacy / "reviews.json.imported").read_text() == "earlier"
    assert (legacy / "sessions.imported-2" / "one.json").exists()
    assert (legacy / "reviews.json.imported-2").read_text() == review_count_json(3)
    assert not (legacy / "sessions").exists()
    assert not (legacy / "reviews.json").exists()


async def test_after_importing_beside_earlier_imported_files_another_import_does_nothing(
    store: SqliteStore, legacy: Path
) -> None:
    (legacy / "sessions.imported").mkdir()
    (legacy / "reviews.json.imported").write_text("earlier")
    await import_session_files(store, legacy)
    assert await import_session_files(store, legacy) is None
    assert sorted(path.name for path in legacy.iterdir()) == [
        "reviews.json.imported",
        "reviews.json.imported-2",
        "sessions.imported",
        "sessions.imported-2",
    ]


async def test_an_empty_earlier_imported_folder_or_a_dangling_link_is_not_replaced(
    store: SqliteStore, legacy: Path
) -> None:
    (legacy / "sessions.imported").mkdir()
    (legacy / "sessions.imported-2").symlink_to(legacy / "nowhere")
    assert await import_session_files(store, legacy) == Imported(3, "sessions.imported-3")
    assert (legacy / "sessions.imported").is_dir()
    assert (legacy / "sessions.imported-2").is_symlink()
    assert (legacy / "sessions.imported-3" / "one.json").exists()


async def test_a_third_import_takes_the_next_free_name(store: SqliteStore, legacy: Path) -> None:
    for name in ("sessions.imported", "sessions.imported-2"):
        (legacy / name).mkdir()
        (legacy / name / "old.json").write_text(name)
    for name in ("reviews.json.imported", "reviews.json.imported-2"):
        (legacy / name).write_text(name)
    assert await import_session_files(store, legacy) == Imported(3, "sessions.imported-3")
    for name in ("sessions.imported", "sessions.imported-2"):
        assert (legacy / name / "old.json").read_text() == name
    for name in ("reviews.json.imported", "reviews.json.imported-2"):
        assert (legacy / name).read_text() == name
    assert (legacy / "sessions.imported-3" / "one.json").exists()
    assert (legacy / "reviews.json.imported-3").read_text() == review_count_json(3)


async def test_a_folder_another_orbit_imports_before_the_rename_is_left_to_it(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, legacy: Path
) -> None:
    retired = session_import._retired

    def importing_first(legacy_dir: Path, folders: list[str]) -> AbstractContextManager[None]:
        # Another orbit imports the folder and renames it just before this one does.
        (legacy_dir / "sessions").rename(legacy_dir / "elsewhere")
        return retired(legacy_dir, folders)

    monkeypatch.setattr(session_import, "_retired", importing_first)
    assert await import_session_files(store, legacy) is None
    assert await ids(store) == []
    assert await store.review_count() == 0
    assert (legacy / "elsewhere" / "one.json").exists()
    assert (legacy / "reviews.json").exists()
    assert not (legacy / "sessions.imported").exists()


async def test_a_folder_another_orbit_imports_before_the_check_is_left_to_it(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, legacy: Path
) -> None:
    checked = session_import._checked

    def importing_first(legacy_dir: Path) -> AbstractContextManager[NewRows]:
        # Another orbit imports the folder and renames it just after this one saw it.
        (legacy_dir / "sessions").rename(legacy_dir / "elsewhere")
        return checked(legacy_dir)

    monkeypatch.setattr(session_import, "_checked", importing_first)
    assert await import_session_files(store, legacy) is None
    assert await ids(store) == []
    assert await store.review_count() == 0
    assert (legacy / "reviews.json").exists()


async def test_a_failing_commit_puts_the_folder_and_review_count_back(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, legacy: Path
) -> None:
    failing_commit(monkeypatch)
    with pytest.raises(ImportBlocked, match=r"sessions in .*legacy/sessions: disk I/O error"):
        await import_session_files(store, legacy)
    assert await ids(store) == []
    assert await store.review_count() == 0
    assert (legacy / "sessions" / "one.json").exists()
    assert (legacy / "reviews.json").exists()
    assert not (legacy / "sessions.imported").exists()
    assert not (legacy / "reviews.json.imported").exists()


async def test_a_file_that_cant_be_renamed_back_blocks_the_import_naming_both(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, legacy: Path
) -> None:
    failing_commit(monkeypatch)
    failing_rename(monkeypatch, "reviews.json.imported")
    with pytest.raises(
        ImportBlocked,
        match=r"rename .*legacy/reviews\.json\.imported back to .*legacy/reviews\.json",
    ) as blocked:
        await import_session_files(store, legacy)
    assert isinstance(blocked.value.__cause__, sqlite3.OperationalError)
    assert await ids(store) == []
    assert (legacy / "sessions" / "one.json").exists()
    assert (legacy / "reviews.json.imported").exists()


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
    assert imported_note(first(3), Path(".git/orbit")) == (
        "Imported 3 saved sessions from .git/orbit/sessions; the old folder is now "
        "sessions.imported."
    )


def test_the_import_note_counts_one_session_as_one() -> None:
    assert imported_note(first(1), Path(".orbit")).startswith("Imported 1 saved session from ")


def test_the_import_note_names_the_folder_the_sessions_went_to() -> None:
    assert imported_note(Imported(2, "sessions.imported-2"), Path(".orbit")).endswith(
        "; the old folder is now sessions.imported-2."
    )
