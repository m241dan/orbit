"""Session locks: a lock file per session while an orbit has it open."""

import asyncio
import fcntl
import os
import subprocess
import sys
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
