"""Saved sessions on disk: where they live, their files, their locks and the review count."""

import asyncio
import fcntl
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbit.agent.application.ports import SessionStore, StoredSession
from orbit.agent.domain.sessions import SessionRecord, Unreadable, record_json
from orbit.agent.external.session_files import SessionFiles, pid_alive, state_dir

ME = 1000
OTHER = 2000
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


def store(directory: Path, alive: frozenset[int] = frozenset({ME})) -> SessionFiles:
    return SessionFiles(directory, pid=ME, alive=lambda pid: pid in alive)


def sessions(directory: Path) -> Path:
    return directory / "sessions"


def test_session_files_are_a_session_store(tmp_path: Path) -> None:
    files: SessionStore = store(tmp_path)
    assert files is not None


# Where sessions live


def test_in_a_repository_sessions_live_in_the_git_directory(repo: Path) -> None:
    assert state_dir(str(repo)) == repo / ".git" / "orbit"


def test_in_a_repositorys_subdirectory_sessions_still_live_in_the_git_directory(
    repo: Path,
) -> None:
    (repo / "sub").mkdir()
    assert state_dir(str(repo / "sub")) == repo / ".git" / "orbit"


def test_in_a_linked_worktree_sessions_live_in_the_shared_git_directory(repo: Path) -> None:
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
    linked = repo.parent / "linked"
    git(repo, "worktree", "add", "-q", str(linked))
    assert state_dir(str(linked)) == repo / ".git" / "orbit"


def test_outside_a_repository_sessions_live_in_the_workspace(tmp_path: Path) -> None:
    assert state_dir(str(tmp_path)) == tmp_path / ".orbit"


def test_without_git_sessions_live_in_the_workspace(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    assert state_dir(str(repo)) == repo / ".orbit"


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


async def test_listing_marks_sessions_another_live_orbit_has_locked(tmp_path: Path) -> None:
    files = store(tmp_path, alive=frozenset({ME, OTHER}))
    for session_id in ("mine", "theirs", "stale"):
        await files.save(record(session_id))
    await files.acquire("mine")
    (sessions(tmp_path) / "theirs.lock").write_text(str(OTHER))
    (sessions(tmp_path) / "stale.lock").write_text("3000")
    listed = {entry.id: entry.locked for entry in await files.sessions()}
    assert listed == {"mine": False, "stale": False, "theirs": True}


async def test_deleting_removes_the_session_and_its_lock(tmp_path: Path) -> None:
    files = store(tmp_path)
    await files.save(record())
    await files.acquire("abc")
    await files.delete("abc")
    assert list(sessions(tmp_path).iterdir()) == []


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


# Locks


async def test_acquiring_writes_a_lock_with_our_process_id(tmp_path: Path) -> None:
    assert await store(tmp_path).acquire("abc")
    assert (sessions(tmp_path) / "abc.lock").read_text() == str(ME)
    assert [path.name for path in sessions(tmp_path).iterdir()] == ["abc.lock"]


async def test_acquiring_a_lock_we_hold_succeeds(tmp_path: Path) -> None:
    files = store(tmp_path)
    await files.acquire("abc")
    assert await files.acquire("abc")


async def test_acquiring_a_lock_another_live_orbit_holds_is_refused(tmp_path: Path) -> None:
    sessions(tmp_path).mkdir(parents=True)
    (sessions(tmp_path) / "abc.lock").write_text(str(OTHER))
    assert not await store(tmp_path, alive=frozenset({ME, OTHER})).acquire("abc")
    assert (sessions(tmp_path) / "abc.lock").read_text() == str(OTHER)


@pytest.mark.parametrize("content", [str(OTHER), "garbage", ""])
async def test_a_stale_lock_is_taken_over(tmp_path: Path, content: str) -> None:
    sessions(tmp_path).mkdir(parents=True)
    (sessions(tmp_path) / "abc.lock").write_text(content)
    assert await store(tmp_path).acquire("abc")
    assert (sessions(tmp_path) / "abc.lock").read_text() == str(ME)


async def test_a_lock_that_cant_be_taken_raises(tmp_path: Path) -> None:
    (sessions(tmp_path) / "abc.lock").mkdir(parents=True)
    with pytest.raises(RuntimeError, match=r"Couldn't lock the session .*abc\.lock"):
        await store(tmp_path).acquire("abc")


async def test_releasing_removes_our_lock(tmp_path: Path) -> None:
    files = store(tmp_path)
    await files.acquire("abc")
    await files.release("abc")
    assert list(sessions(tmp_path).iterdir()) == []


async def test_releasing_leaves_another_orbits_lock_and_a_missing_one_alone(
    tmp_path: Path,
) -> None:
    sessions(tmp_path).mkdir(parents=True)
    (sessions(tmp_path) / "abc.lock").write_text(str(OTHER))
    files = store(tmp_path)
    await files.release("abc")
    await files.release("never")
    assert (sessions(tmp_path) / "abc.lock").read_text() == str(OTHER)


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
    (sessions(tmp_path) / "abc.lock").write_text(str(OTHER))
    asking, answer = threading.Event(), threading.Event()

    def alive(pid: int) -> bool:
        asking.set()
        answer.wait()
        return False

    acquiring = asyncio.create_task(SessionFiles(tmp_path, pid=ME, alive=alive).acquire("abc"))
    await asyncio.to_thread(asking.wait)
    acquiring.cancel()
    await asyncio.sleep(0)
    answer.set()
    with pytest.raises(asyncio.CancelledError):
        await acquiring
    assert list(sessions(tmp_path).iterdir()) == []


async def test_two_orbits_taking_over_the_same_stale_lock_cant_both_win(tmp_path: Path) -> None:
    sessions(tmp_path).mkdir(parents=True)
    (sessions(tmp_path) / "abc.lock").write_text("3000")
    asking, answer = threading.Event(), threading.Event()

    def slow_alive(pid: int) -> bool:
        if pid == 3000:
            asking.set()
            answer.wait()
        return pid in {ME, OTHER}

    def alive(pid: int) -> bool:
        return pid in {ME, OTHER}

    slow = asyncio.create_task(SessionFiles(tmp_path, pid=ME, alive=slow_alive).acquire("abc"))
    await asyncio.to_thread(asking.wait)
    fast = asyncio.create_task(SessionFiles(tmp_path, pid=OTHER, alive=alive).acquire("abc"))
    await asyncio.wait([fast], timeout=0.2)  # Unguarded, it takes over meanwhile.
    answer.set()
    won = dict(zip((ME, OTHER), await asyncio.gather(slow, fast), strict=True))
    assert sorted(won.values()) == [False, True]
    winner = next(pid for pid, took in won.items() if took)
    assert (sessions(tmp_path) / "abc.lock").read_text() == str(winner)


async def test_a_cancelled_acquire_of_a_lock_we_hold_keeps_it(tmp_path: Path) -> None:
    files = store(tmp_path)
    assert await files.acquire("abc")
    guard = os.open(sessions(tmp_path), os.O_RDONLY)
    try:
        fcntl.flock(guard, fcntl.LOCK_EX)  # Holds the second acquire mid-flight.
        acquiring = asyncio.create_task(files.acquire("abc"))
        await asyncio.sleep(0.05)
        acquiring.cancel()
        await asyncio.sleep(0)
    finally:
        os.close(guard)
    with pytest.raises(asyncio.CancelledError):
        await acquiring
    assert (sessions(tmp_path) / "abc.lock").read_text() == str(ME)


async def test_by_default_locks_name_this_process(tmp_path: Path) -> None:
    files = SessionFiles(tmp_path)
    await files.acquire("abc")
    assert (sessions(tmp_path) / "abc.lock").read_text() == str(os.getpid())
    assert not await SessionFiles(tmp_path, pid=os.getpid() + 1).acquire("abc")


# Whether a process is alive


def test_this_process_is_alive() -> None:
    assert pid_alive(os.getpid())


def test_an_ended_process_isnt_alive() -> None:
    ended = subprocess.Popen([sys.executable, "-c", ""])
    ended.wait()
    assert not pid_alive(ended.pid)


def test_a_process_id_too_large_to_exist_isnt_alive() -> None:
    assert not pid_alive(10**30)


def test_someone_elses_process_is_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(pid: int, signal: int) -> None:
        raise PermissionError(pid)

    monkeypatch.setattr(os, "kill", refused)
    assert pid_alive(1)


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


@pytest.mark.usefixtures("failing_fsync")
async def test_an_acquire_that_fails_writing_raises_and_leaves_no_temporary_file(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match=r"Couldn't lock the session .*abc\.lock"):
        await store(tmp_path).acquire("abc")
    assert list(sessions(tmp_path).iterdir()) == []
