"""Session locks: a lock file per session while an orbit has it open."""

import asyncio
import errno
import fcntl
import os
import stat
import subprocess
import sys
import threading
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from orbit.agent.application.ports import StorageFailed, StoreFailed
from orbit.agent.domain.storage_halt import StorageErrorKind
from orbit.agent.external import session_locks
from orbit.agent.external.session_locks import SessionLocks, locked_dir


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


async def test_a_lock_file_is_readable_by_its_owner_alone(tmp_path: Path) -> None:
    assert await locks_in(tmp_path).acquire("abc")
    assert stat.S_IMODE((sessions(tmp_path) / "abc.lock").stat().st_mode) == 0o600


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
    with pytest.raises(StoreFailed, match=r"Couldn't lock the session .*abc\.lock"):
        await locks_in(tmp_path).acquire("abc")


async def test_a_lock_that_fails_after_opening_is_let_go(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail(fd: int, length: int) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(os, "ftruncate", fail)
        with pytest.raises(StoreFailed, match=r"Couldn't lock the session .*abc\.lock"):
            await locks_in(tmp_path).acquire("abc")
    assert await locks_in(tmp_path).acquire("abc")


FAILURES = [(errno.ENOSPC, StorageErrorKind.DISK_FULL), (errno.EROFS, StorageErrorKind.READ_ONLY)]
"""Operating system errors, and what orbit makes of them."""

VERBS = {"release": "unlock", "remove": "delete"}
"""What failing to let go of a lock says it couldn't do, by how it was let go of."""


@pytest.mark.parametrize(("number", "kind"), FAILURES)
async def test_a_lock_that_fails_to_be_taken_says_what_kind_of_failure_on_which_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, number: int, kind: StorageErrorKind
) -> None:
    def fail(fd: int, length: int) -> None:
        raise OSError(number, os.strerror(number))

    monkeypatch.setattr(os, "ftruncate", fail)
    with pytest.raises(StorageFailed, match=r"^Couldn't lock the session ") as caught:
        await locks_in(tmp_path).acquire("abc")
    error = caught.value
    assert error.kind == kind and error.database == str(sessions(tmp_path) / "abc.lock")
    assert error.error == str(OSError(number, os.strerror(number)))


@pytest.mark.parametrize(("number", "kind"), FAILURES)
@pytest.mark.parametrize("action", ["release", "remove"])
async def test_a_lock_that_fails_to_be_let_go_of_says_what_kind_of_failure_on_which_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
    number: int,
    kind: StorageErrorKind,
) -> None:
    locks = locks_in(tmp_path)
    await locks.acquire("abc")

    def refused(path: Path, missing_ok: bool = False) -> None:
        raise OSError(number, os.strerror(number))

    monkeypatch.setattr(Path, "unlink", refused)
    with pytest.raises(StorageFailed, match=rf"^Couldn't {VERBS[action]} the session ") as caught:
        await getattr(locks, action)("abc")
    assert caught.value.kind == kind
    assert caught.value.database == str(sessions(tmp_path) / "abc.lock")


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
    with pytest.raises(StoreFailed, match=r"Couldn't unlock the session .*abc\.lock: read-only"):
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


async def test_an_acquire_cancelled_twice_still_leaves_no_lock_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = SessionLocks._acquire
    taken = threading.Event()

    def taking(self: SessionLocks, session_id: str) -> object:
        try:
            return real(self, session_id)
        finally:
            taken.set()

    monkeypatch.setattr(SessionLocks, "_acquire", taking)
    sessions(tmp_path).mkdir(parents=True)
    locks = locks_in(tmp_path)
    with directory_held(sessions(tmp_path)):
        acquiring = asyncio.create_task(locks.acquire("abc"))
        await asyncio.sleep(0.05)
        for _ in range(2):  # Again while it waits for the thread: it still lets go.
            acquiring.cancel()
            await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await acquiring
    await asyncio.to_thread(taken.wait)
    assert list(sessions(tmp_path).iterdir()) == []
    assert not locks.held_elsewhere("abc") and not locks_in(tmp_path).held_elsewhere("abc")


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
    with pytest.raises(
        StoreFailed, match=r"Couldn't lock the session .*abc\.lock: the locks"
    ) as caught:
        await locks.acquire("abc")
    assert type(caught.value) is StoreFailed
    assert await locks_in(tmp_path).acquire("abc")


async def test_a_lock_being_taken_when_closed_is_free_once_both_finish(tmp_path: Path) -> None:
    locks = locks_in(tmp_path)
    sessions(tmp_path).mkdir(parents=True)
    with directory_held(sessions(tmp_path)):  # Holds the acquire mid-flight.
        acquiring = asyncio.create_task(locks.acquire("abc"))
        await asyncio.sleep(0.05)
        locks.close()
    with pytest.raises(StoreFailed, match="closed"):
        await acquiring
    assert await locks_in(tmp_path).acquire("abc")


class CloseRecorder:
    """Stands in for ``os.close``, recording the inode of each descriptor it closes (``None``
    for one that's not open) and holding the first close in a thread whose name starts with
    ``pause_in``, if given, until told to go on."""

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
        thread = threading.current_thread().name
        if self._pause_in and thread.startswith(self._pause_in) and not self.paused.is_set():
            self.paused.set()
            self.go.wait(timeout=5)
        self._close(fd)


def release_in_thread(locks: SessionLocks) -> threading.Thread:
    """A started thread releasing ``abc`` from ``locks`` in a loop of its own, whose worker
    threads, where the release lets go of the lock, are named ``releaser_N``; the loop's own
    thread, which closes descriptors of its own as it starts, isn't."""

    async def release() -> None:
        pool = ThreadPoolExecutor(thread_name_prefix="releaser")
        asyncio.get_running_loop().set_default_executor(pool)
        await locks.release("abc")

    releaser = threading.Thread(target=asyncio.run, args=(release(),), name="release-loop")
    releaser.start()
    return releaser


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


class DirectoryHook:
    """Stands in for ``locked_dir``, setting ``entered`` once a worker thread holds the
    directory's lock and, when ``hold``, keeping it there until told to go on."""

    def __init__(self, hold: bool) -> None:
        self.entered = threading.Event()
        self.go = threading.Event()
        self._hold = hold

    @contextmanager
    def __call__(self, directory: Path, operation: int) -> Iterator[None]:
        with locked_dir(directory, operation):
            self.entered.set()
            if self._hold:
                self.go.wait(timeout=5)
            yield


async def test_closing_while_a_lock_is_let_go_closes_each_lock_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    locks = locks_in(tmp_path)
    await locks.acquire("abc")
    await locks.acquire("def")
    files = sessions(tmp_path) / "abc.lock", sessions(tmp_path) / "def.lock"
    inodes = {file.stat().st_ino for file in files}
    recorder = CloseRecorder(pause_in="closer")
    hook = DirectoryHook(hold=False)
    errors: list[BaseException] = []
    with monkeypatch.context() as patch:
        patch.setattr(os, "close", recorder)
        patch.setattr(session_locks, "locked_dir", hook)
        closer = close_in_thread(locks, errors)
        # close() is mid-way through its locks.
        assert await asyncio.to_thread(recorder.paused.wait, 5)
        releasing = asyncio.create_task(locks.release("abc"))
        await asyncio.to_thread(hook.entered.wait, 5)  # The release waits on close().
        recorder.go.set()
        await asyncio.to_thread(closer.join)
        with pytest.raises(StoreFailed, match=r"Couldn't unlock the session .*abc\.lock: .*closed"):
            await releasing
    assert errors == []
    assert Counter(i for i in recorder.closed if i is None or i in inodes) == Counter(inodes)
    assert all(file.exists() for file in files)
    assert await locks_in(tmp_path).acquire("abc")
    assert await locks_in(tmp_path).acquire("def")


async def test_closing_before_a_release_reaches_its_lock_leaves_it_open_to_take(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    locks = locks_in(tmp_path)
    await locks.acquire("abc")
    inode = (sessions(tmp_path) / "abc.lock").stat().st_ino
    recorder = CloseRecorder()
    hook = DirectoryHook(hold=True)
    with monkeypatch.context() as patch:
        patch.setattr(os, "close", recorder)
        patch.setattr(session_locks, "locked_dir", hook)
        releasing = asyncio.create_task(locks.release("abc"))
        await asyncio.to_thread(hook.entered.wait, 5)  # The release holds the directory.
        locks.close()
        hook.go.set()
        with pytest.raises(StoreFailed, match="closed"):
            await releasing
    assert [i for i in recorder.closed if i == inode] == [inode]
    assert (sessions(tmp_path) / "abc.lock").exists()
    assert await locks_in(tmp_path).acquire("abc")


async def test_closing_while_a_release_lets_go_of_its_lock_waits_for_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    locks = locks_in(tmp_path)
    await locks.acquire("abc")
    inode = (sessions(tmp_path) / "abc.lock").stat().st_ino
    recorder = CloseRecorder(pause_in="releaser")
    errors: list[BaseException] = []
    with monkeypatch.context() as patch:
        patch.setattr(os, "close", recorder)
        releaser = release_in_thread(locks)
        # The release is closing its lock.
        assert await asyncio.to_thread(recorder.paused.wait, 5)
        closer = close_in_thread(locks, errors)
        recorder.go.set()
        await asyncio.to_thread(closer.join)
        await asyncio.to_thread(releaser.join)
    assert errors == []
    assert [i for i in recorder.closed if i == inode] == [inode]
    assert list(sessions(tmp_path).iterdir()) == []
    assert await locks_in(tmp_path).acquire("abc")


@pytest.mark.parametrize("action", ["release", "remove"])
async def test_letting_go_after_closing_raises_and_leaves_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str
) -> None:
    locks = locks_in(tmp_path)
    await locks.acquire("abc")
    locks.close()
    holder = locks_in(tmp_path)
    assert await holder.acquire("abc")
    inode = (sessions(tmp_path) / "abc.lock").stat().st_ino
    recorder = CloseRecorder()
    with monkeypatch.context() as patch:
        patch.setattr(os, "close", recorder)
        with pytest.raises(
            StoreFailed, match=r"Couldn't \w+ the session .*abc\.lock: .*closed"
        ) as caught:
            await getattr(locks, action)("abc")
    assert type(caught.value) is StoreFailed
    assert [i for i in recorder.closed if i is None or i == inode] == []
    assert (sessions(tmp_path) / "abc.lock").read_text() == str(os.getpid())
    assert not await locks_in(tmp_path).acquire("abc")
