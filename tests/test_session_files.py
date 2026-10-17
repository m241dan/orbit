"""Saved sessions on disk: where they live, their files, their locks and the review count."""

import asyncio
import fcntl
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbit.agent.application.ports import SessionStore, StoredSession
from orbit.agent.domain.sessions import SessionRecord, Unreadable, record_json
from orbit.agent.external.git_tree import GitCoderTree
from orbit.agent.external.session_files import SessionFiles, state_dir

WHEN = datetime(2026, 10, 17, 14, 30, tzinfo=UTC)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    return root


def record(session_id: str = "abc", name: str = "Oct 17 14:30") -> SessionRecord:
    return SessionRecord(id=session_id, name=name, created=WHEN, last_active=WHEN)


def store(directory: Path) -> SessionFiles:
    return SessionFiles(directory)


def sessions(directory: Path) -> Path:
    return directory / "sessions"


def test_session_files_are_a_session_store(tmp_path: Path) -> None:
    files: SessionStore = store(tmp_path)
    assert files is not None


# Where sessions live


def test_in_a_repository_sessions_live_in_the_git_directory(repo: Path) -> None:
    assert state_dir(str(repo)) == repo / ".git" / "orbit"


def test_outside_a_repository_sessions_live_in_the_workspace(tmp_path: Path) -> None:
    assert state_dir(str(tmp_path)) == tmp_path / ".orbit"


async def test_sessions_live_beside_the_coders_worktree(repo: Path) -> None:
    git(
        repo,
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@e",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    coder = Path(await GitCoderTree(str(repo)).prepare(keep=False))
    assert coder.parent == state_dir(str(repo))


# Saving and loading


async def test_a_saved_record_loads_back(tmp_path: Path) -> None:
    files = store(tmp_path)
    await files.save(record())
    assert await files.load("abc") == record()
    assert (sessions(tmp_path) / "abc.json").read_text() == record_json(record())


async def test_saving_again_replaces_the_record_and_leaves_no_temporary_files(
    tmp_path: Path,
) -> None:
    files = store(tmp_path)
    await files.save(record(name="first"))
    await files.save(record(name="second"))
    assert await files.load("abc") == record(name="second")
    assert [path.name for path in sessions(tmp_path).iterdir()] == ["abc.json"]


async def test_a_failed_save_raises_keeps_the_old_file_and_cleans_up(tmp_path: Path) -> None:
    files = store(tmp_path)
    (sessions(tmp_path) / "abc.json").mkdir(parents=True)
    with pytest.raises(RuntimeError, match=r"Couldn't save the session to .*abc\.json"):
        await files.save(record())
    assert [path.name for path in sessions(tmp_path).iterdir()] == ["abc.json"]


async def test_a_missing_session_is_unreadable(tmp_path: Path) -> None:
    loaded = await store(tmp_path).load("abc")
    assert isinstance(loaded, Unreadable)
    assert loaded.reason.startswith("it can't be read: ")


async def test_a_file_that_isnt_text_is_unreadable(tmp_path: Path) -> None:
    sessions(tmp_path).mkdir(parents=True)
    (sessions(tmp_path) / "abc.json").write_bytes(b"\xff\xfe")
    loaded = await store(tmp_path).load("abc")
    assert isinstance(loaded, Unreadable)
    assert loaded.reason.startswith("it can't be read: ")


@pytest.mark.parametrize("session_id", ["", "a/b", "../abc", "."])
async def test_an_id_that_isnt_a_plain_file_name_is_refused(
    tmp_path: Path, session_id: str
) -> None:
    with pytest.raises(ValueError, match="Not a session id"):
        await store(tmp_path).load(session_id)


# Listing and deleting


async def test_without_a_sessions_directory_there_are_no_sessions(tmp_path: Path) -> None:
    assert await store(tmp_path).sessions() == []


async def test_a_sessions_directory_that_cant_be_opened_has_no_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    files = store(tmp_path)
    await files.save(record())
    real_open = os.open

    def refused(path: str | Path, flags: int, mode: int = 0o777) -> int:
        if Path(path) == sessions(tmp_path):
            raise PermissionError("permission denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", refused)
    assert await files.sessions() == []


async def test_a_sessions_directory_that_cant_be_locked_has_no_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    files = store(tmp_path)
    await files.save(record())

    def fail(fd: int, operation: int) -> None:
        raise OSError("no locks")

    monkeypatch.setattr(fcntl, "flock", fail)
    assert await files.sessions() == []


async def test_listing_gives_readable_and_unreadable_sessions_by_id(tmp_path: Path) -> None:
    files = store(tmp_path)
    await files.save(record("b"))
    (sessions(tmp_path) / "a.json").write_text("{oops")
    (sessions(tmp_path) / "notes.txt").write_text("not a session")
    listed = await files.sessions()
    assert [(entry.id, entry.locked) for entry in listed] == [("a", False), ("b", False)]
    assert isinstance(listed[0].record, Unreadable)
    assert listed[1] == StoredSession("b", record("b"), locked=False)
    assert (sessions(tmp_path) / "a.json").read_text() == "{oops"


async def test_listing_marks_sessions_another_orbit_holds_as_locked(tmp_path: Path) -> None:
    files, other = store(tmp_path), store(tmp_path)
    for session_id in ("mine", "theirs", "stale"):
        await files.save(record(session_id))
    await files.acquire("mine")
    await other.acquire("theirs")
    (sessions(tmp_path) / "stale.lock").write_text("3000")
    listed = {entry.id: entry.locked for entry in await files.sessions()}
    assert listed == {"mine": False, "stale": False, "theirs": True}
    assert {entry.id: entry.locked for entry in await other.sessions()}["mine"]


async def test_deleting_removes_the_session_and_its_lock(tmp_path: Path) -> None:
    files = store(tmp_path)
    await files.save(record())
    await files.acquire("abc")
    await files.delete("abc")
    assert list(sessions(tmp_path).iterdir()) == []


async def test_deleting_a_session_lets_go_of_its_lock(tmp_path: Path) -> None:
    files = store(tmp_path)
    await files.acquire("abc")
    await files.delete("abc")
    assert await store(tmp_path).acquire("abc")


async def test_deleting_works_on_unreadable_and_missing_sessions(tmp_path: Path) -> None:
    sessions(tmp_path).mkdir(parents=True)
    (sessions(tmp_path) / "bad.json").write_text("{oops")
    files = store(tmp_path)
    await files.delete("bad")
    await files.delete("never")
    assert list(sessions(tmp_path).iterdir()) == []


async def test_a_failed_delete_raises(tmp_path: Path) -> None:
    (sessions(tmp_path) / "abc.json").mkdir(parents=True)
    with pytest.raises(RuntimeError, match=r"Couldn't delete the session .*abc\.json"):
        await store(tmp_path).delete("abc")


async def test_a_failed_lock_delete_raises(tmp_path: Path) -> None:
    (sessions(tmp_path) / "abc.lock" / "inside").mkdir(parents=True)
    with pytest.raises(RuntimeError, match=r"Couldn't delete the session .*abc\.lock"):
        await store(tmp_path).delete("abc")


# Locks


def hold_and_exit(lock: Path) -> None:
    """Run a process that takes ``lock`` as an orbit does, then ends without letting go."""
    code = (
        "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
        "fcntl.flock(fd, fcntl.LOCK_EX); os.write(fd, str(os.getpid()).encode())"
    )
    subprocess.run([sys.executable, "-c", code, str(lock)], check=True)


@contextmanager
def directory_held(directory: Path) -> Iterator[None]:
    """Hold the lock on ``directory`` that orbits take before touching its locks."""
    guard = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(guard, fcntl.LOCK_EX)
        yield
    finally:
        os.close(guard)


async def test_acquiring_writes_a_lock_with_our_process_id(tmp_path: Path) -> None:
    assert await store(tmp_path).acquire("abc")
    assert (sessions(tmp_path) / "abc.lock").read_text() == str(os.getpid())
    assert [path.name for path in sessions(tmp_path).iterdir()] == ["abc.lock"]


async def test_acquiring_a_lock_we_hold_succeeds(tmp_path: Path) -> None:
    files = store(tmp_path)
    await files.acquire("abc")
    assert await files.acquire("abc")


async def test_acquiring_a_lock_another_orbit_holds_is_refused(tmp_path: Path) -> None:
    other = store(tmp_path)
    await other.acquire("abc")
    assert not await store(tmp_path).acquire("abc")
    assert (sessions(tmp_path) / "abc.lock").exists()


async def test_two_orbits_acquiring_at_once_cant_both_win(tmp_path: Path) -> None:
    orbits = [store(tmp_path) for _ in range(8)]
    won = await asyncio.gather(*(files.acquire("abc") for files in orbits))
    assert won.count(True) == 1


@pytest.mark.parametrize("content", ["3000", "garbage", ""])
async def test_a_lock_file_nobody_holds_is_taken_over(tmp_path: Path, content: str) -> None:
    sessions(tmp_path).mkdir(parents=True)
    (sessions(tmp_path) / "abc.lock").write_text(content)
    assert await store(tmp_path).acquire("abc")
    assert (sessions(tmp_path) / "abc.lock").read_text() == str(os.getpid())


async def test_a_lock_left_by_an_ended_process_is_taken_over(tmp_path: Path) -> None:
    sessions(tmp_path).mkdir(parents=True)
    hold_and_exit(sessions(tmp_path) / "abc.lock")
    files = store(tmp_path)
    await files.save(record())
    assert [entry.locked for entry in await files.sessions()] == [False]
    assert await files.acquire("abc")


async def test_a_lock_that_cant_be_taken_raises(tmp_path: Path) -> None:
    (sessions(tmp_path) / "abc.lock").mkdir(parents=True)
    with pytest.raises(RuntimeError, match=r"Couldn't lock the session .*abc\.lock"):
        await store(tmp_path).acquire("abc")


async def test_a_lock_that_fails_after_opening_is_let_go(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail(fd: int, length: int) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(os, "ftruncate", fail)
        with pytest.raises(RuntimeError, match=r"Couldn't lock the session .*abc\.lock"):
            await store(tmp_path).acquire("abc")
    assert await store(tmp_path).acquire("abc")


async def test_releasing_removes_our_lock(tmp_path: Path) -> None:
    files = store(tmp_path)
    await files.acquire("abc")
    await files.release("abc")
    assert list(sessions(tmp_path).iterdir()) == []
    assert await store(tmp_path).acquire("abc")


async def test_releasing_leaves_another_orbits_lock_and_a_missing_one_alone(
    tmp_path: Path,
) -> None:
    holder = store(tmp_path)
    await holder.acquire("abc")
    files = store(tmp_path)
    await files.release("abc")
    await files.release("never")
    assert (sessions(tmp_path) / "abc.lock").read_text() == str(os.getpid())
    assert not await files.acquire("abc")


async def test_a_lock_that_cant_be_released_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    files = store(tmp_path)
    await files.acquire("abc")

    def refused(path: Path, missing_ok: bool = False) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", refused)
    with pytest.raises(RuntimeError, match=r"Couldn't unlock the session .*abc\.lock: read-only"):
        await files.release("abc")


async def test_a_cancelled_acquire_leaves_no_lock_behind(tmp_path: Path) -> None:
    sessions(tmp_path).mkdir(parents=True)
    with directory_held(sessions(tmp_path)):  # Holds the acquire mid-flight.
        acquiring = asyncio.create_task(store(tmp_path).acquire("abc"))
        await asyncio.sleep(0.05)
        acquiring.cancel()
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await acquiring
    assert list(sessions(tmp_path).iterdir()) == []
    assert await store(tmp_path).acquire("abc")


async def test_a_cancelled_acquire_whose_lock_fails_stays_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail(fd: int, length: int) -> None:
        raise OSError("boom")

    sessions(tmp_path).mkdir(parents=True)
    (sessions(tmp_path) / "abc.lock").write_text("3000")
    monkeypatch.setattr(os, "ftruncate", fail)
    with directory_held(sessions(tmp_path)):
        acquiring = asyncio.create_task(store(tmp_path).acquire("abc"))
        await asyncio.sleep(0.05)
        acquiring.cancel()
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await acquiring
    assert (sessions(tmp_path) / "abc.lock").read_text() == "3000"


async def test_a_cancelled_acquire_whose_lock_cant_be_released_stays_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def refused(path: Path, missing_ok: bool = False) -> None:
        raise PermissionError("read-only")

    sessions(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(Path, "unlink", refused)
    with directory_held(sessions(tmp_path)):
        acquiring = asyncio.create_task(store(tmp_path).acquire("abc"))
        await asyncio.sleep(0.05)
        acquiring.cancel()
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await acquiring


async def test_a_cancelled_acquire_of_a_lock_we_hold_keeps_it(tmp_path: Path) -> None:
    files = store(tmp_path)
    assert await files.acquire("abc")
    with directory_held(sessions(tmp_path)):
        acquiring = asyncio.create_task(files.acquire("abc"))
        await asyncio.sleep(0.05)
        acquiring.cancel()
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await acquiring
    assert not await store(tmp_path).acquire("abc")


# The super review count


async def test_without_a_saved_review_count_it_is_zero(tmp_path: Path) -> None:
    assert await store(tmp_path).review_count() == 0


async def test_the_review_count_saves_and_loads_back(tmp_path: Path) -> None:
    files = store(tmp_path)
    await files.save_review_count(3)
    await files.save_review_count(4)
    assert await files.review_count() == 4
    assert [path.name for path in tmp_path.iterdir()] == ["reviews.json"]


async def test_an_unreadable_review_count_is_zero(tmp_path: Path) -> None:
    (tmp_path / "reviews.json").write_bytes(b"\xff")
    assert await store(tmp_path).review_count() == 0


async def test_a_failed_review_count_save_raises(tmp_path: Path) -> None:
    (tmp_path / "reviews.json").mkdir()
    with pytest.raises(RuntimeError, match=r"Couldn't save the review count to .*reviews\.json"):
        await store(tmp_path).save_review_count(1)


@pytest.fixture
def failing_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", fail)


@pytest.mark.usefixtures("failing_fsync")
async def test_a_save_that_fails_writing_raises_and_leaves_no_temporary_file(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match=r"Couldn't save the session to .*abc\.json"):
        await store(tmp_path).save(record())
    assert list(sessions(tmp_path).iterdir()) == []


@pytest.mark.usefixtures("failing_fsync")
async def test_a_review_count_save_that_fails_writing_raises_and_leaves_no_temporary_file(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match=r"Couldn't save the review count to .*reviews\.json"):
        await store(tmp_path).save_review_count(1)
    assert list(tmp_path.iterdir()) == []
