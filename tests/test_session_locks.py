"""Session locks: a lock file per session while an orbit has it open."""

import asyncio
import fcntl
import os
import subprocess
import sys
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from orbit.agent.external.session_locks import SessionLocks


def locks_in(directory: Path) -> SessionLocks:
    """Another orbit's locks on the sessions in ``directory``."""
    return SessionLocks(sessions(directory))


def sessions(directory: Path) -> Path:
    return directory / "sessions"


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
    assert await locks_in(tmp_path).acquire("abc")
    assert (sessions(tmp_path) / "abc.lock").read_text() == str(os.getpid())
    assert [path.name for path in sessions(tmp_path).iterdir()] == ["abc.lock"]


async def test_acquiring_a_lock_another_orbit_holds_is_refused(tmp_path: Path) -> None:
    other = locks_in(tmp_path)
    await other.acquire("abc")
    assert not await locks_in(tmp_path).acquire("abc")
    assert (sessions(tmp_path) / "abc.lock").exists()


@pytest.mark.parametrize("content", ["3000", "garbage", ""])
async def test_a_lock_file_nobody_holds_is_taken_over(tmp_path: Path, content: str) -> None:
    sessions(tmp_path).mkdir(parents=True)
    (sessions(tmp_path) / "abc.lock").write_text(content)
    assert await locks_in(tmp_path).acquire("abc")
    assert (sessions(tmp_path) / "abc.lock").read_text() == str(os.getpid())


async def test_a_lock_left_by_an_ended_process_is_taken_over(tmp_path: Path) -> None:
    sessions(tmp_path).mkdir(parents=True)
    hold_and_exit(sessions(tmp_path) / "abc.lock")
    locks = locks_in(tmp_path)
    with locks.checking():
        assert not locks.held_elsewhere("abc")
    assert await locks.acquire("abc")


async def test_a_lock_that_cant_be_taken_raises(tmp_path: Path) -> None:
    (sessions(tmp_path) / "abc.lock").mkdir(parents=True)
    with pytest.raises(RuntimeError, match=r"Couldn't lock the session .*abc\.lock"):
        await locks_in(tmp_path).acquire("abc")


async def test_a_lock_that_fails_after_opening_is_let_go(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail(fd: int, length: int) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(os, "ftruncate", fail)
        with pytest.raises(RuntimeError, match=r"Couldn't lock the session .*abc\.lock"):
            await locks_in(tmp_path).acquire("abc")
    assert await locks_in(tmp_path).acquire("abc")


async def test_releasing_removes_our_lock(tmp_path: Path) -> None:
    locks = locks_in(tmp_path)
    await locks.acquire("abc")
    await locks.release("abc")
    assert list(sessions(tmp_path).iterdir()) == []
    assert await locks_in(tmp_path).acquire("abc")


async def test_releasing_leaves_another_orbits_lock_and_a_missing_one_alone(
    tmp_path: Path,
) -> None:
    holder = locks_in(tmp_path)
    await holder.acquire("abc")
    locks = locks_in(tmp_path)
    await locks.release("abc")
    await locks.release("never")
    assert (sessions(tmp_path) / "abc.lock").read_text() == str(os.getpid())
    assert not await locks.acquire("abc")


async def test_a_lock_that_cant_be_released_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    locks = locks_in(tmp_path)
    await locks.acquire("abc")

    def refused(path: Path, missing_ok: bool = False) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", refused)
    with pytest.raises(RuntimeError, match=r"Couldn't unlock the session .*abc\.lock: read-only"):
        await locks.release("abc")


async def test_a_cancelled_acquire_leaves_no_lock_behind(tmp_path: Path) -> None:
    sessions(tmp_path).mkdir(parents=True)
    with directory_held(sessions(tmp_path)):  # Holds the acquire mid-flight.
        acquiring = asyncio.create_task(locks_in(tmp_path).acquire("abc"))
        await asyncio.sleep(0.05)
        acquiring.cancel()
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await acquiring
    assert list(sessions(tmp_path).iterdir()) == []
    assert await locks_in(tmp_path).acquire("abc")


async def test_a_cancelled_acquire_whose_lock_fails_stays_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail(fd: int, length: int) -> None:
        raise OSError("boom")

    sessions(tmp_path).mkdir(parents=True)
    (sessions(tmp_path) / "abc.lock").write_text("3000")
    monkeypatch.setattr(os, "ftruncate", fail)
    with directory_held(sessions(tmp_path)):
        acquiring = asyncio.create_task(locks_in(tmp_path).acquire("abc"))
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
        acquiring = asyncio.create_task(locks_in(tmp_path).acquire("abc"))
        await asyncio.sleep(0.05)
        acquiring.cancel()
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await acquiring


async def test_a_cancelled_acquire_of_a_lock_we_hold_keeps_it(tmp_path: Path) -> None:
    locks = locks_in(tmp_path)
    assert await locks.acquire("abc")
    with directory_held(sessions(tmp_path)):
        acquiring = asyncio.create_task(locks.acquire("abc"))
        await asyncio.sleep(0.05)
        acquiring.cancel()
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await acquiring
    assert not await locks_in(tmp_path).acquire("abc")


async def test_acquiring_after_closing_raises_and_takes_no_lock(tmp_path: Path) -> None:
    locks = locks_in(tmp_path)
    locks.close()
    with pytest.raises(RuntimeError, match=r"Couldn't lock the session .*abc\.lock: the locks"):
        await locks.acquire("abc")
    assert await locks_in(tmp_path).acquire("abc")


async def test_a_lock_being_taken_when_closed_is_free_once_both_finish(tmp_path: Path) -> None:
    locks = locks_in(tmp_path)
    sessions(tmp_path).mkdir(parents=True)
    with directory_held(sessions(tmp_path)):  # Holds the acquire mid-flight.
        acquiring = asyncio.create_task(locks.acquire("abc"))
        await asyncio.sleep(0.05)
        locks.close()
    with pytest.raises(RuntimeError, match="closed"):
        await acquiring
    assert await locks_in(tmp_path).acquire("abc")


class CloseRecorder:
    """Stands in for ``os.close``, recording the inode of each descriptor it closes (``None``
    for one that's not open) and holding the first close in the thread ``pause_in`` until told
    to go on."""

    def __init__(self, pause_in: str = "") -> None:
        self.closed: list[int | None] = []
        self.paused = threading.Event()
        self.go = threading.Event()
        self._pause_in = pause_in
        self._close = os.close

    def __call__(self, fd: int) -> None:
        try:
            self.closed.append(os.fstat(fd).st_ino)
        except OSError:
            self.closed.append(None)
        if threading.current_thread().name == self._pause_in and not self.paused.is_set():
            self.paused.set()
            self.go.wait(timeout=5)
        self._close(fd)


def close_in_thread(locks: SessionLocks, errors: list[BaseException]) -> threading.Thread:
    """A started thread named ``closer`` closing ``locks``, adding what it raises to ``errors``."""

    def run() -> None:
        try:
            locks.close()
        except BaseException as error:
            errors.append(error)

    closer = threading.Thread(target=run, name="closer")
    closer.start()
    return closer


async def test_closing_while_a_lock_is_let_go_closes_each_lock_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    locks = locks_in(tmp_path)
    await locks.acquire("abc")
    await locks.acquire("def")
    files = sessions(tmp_path) / "abc.lock", sessions(tmp_path) / "def.lock"
    inodes = {file.stat().st_ino for file in files}
    recorder = CloseRecorder(pause_in="closer")
    errors: list[BaseException] = []
    with monkeypatch.context() as patch:
        patch.setattr(os, "close", recorder)
        closer = close_in_thread(locks, errors)
        await asyncio.to_thread(recorder.paused.wait, 5)  # close() is mid-way through its locks.
        releasing = asyncio.create_task(locks.release("abc"))
        await asyncio.sleep(0.05)
        recorder.go.set()
        await asyncio.to_thread(closer.join)
        await releasing
    assert errors == []
    assert Counter(i for i in recorder.closed if i is None or i in inodes) == Counter(inodes)
    assert await locks_in(tmp_path).acquire("abc")
    assert await locks_in(tmp_path).acquire("def")


@pytest.mark.parametrize("action", ["release", "remove"])
async def test_letting_go_after_closing_closes_no_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str
) -> None:
    locks = locks_in(tmp_path)
    await locks.acquire("abc")
    inode = (sessions(tmp_path) / "abc.lock").stat().st_ino
    locks.close()
    recorder = CloseRecorder()
    with monkeypatch.context() as patch:
        patch.setattr(os, "close", recorder)
        await getattr(locks, action)("abc")
    assert [i for i in recorder.closed if i is None or i == inode] == []
    assert await locks_in(tmp_path).acquire("abc")
