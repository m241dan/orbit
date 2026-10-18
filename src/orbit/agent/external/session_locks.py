"""Session locks: a lock file per session while an orbit has it open.

A session's lock is an exclusive ``flock`` on its ``.lock`` file, held open until the orbit lets
go of it; the kernel drops it when the orbit's process ends, so a lock file nobody holds is free to
take. The file names the holder's process only for whoever reads it. Taking, letting go of and
checking locks happen under a lock on their directory, so a lock file is never removed between
another orbit opening it and locking it.
"""

import asyncio
import fcntl
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from enum import Enum, auto
from pathlib import Path


def check_session_id(session_id: str) -> None:
    """Raise ``ValueError`` if ``session_id`` isn't a plain file name, as session ids are."""
    if not session_id or Path(session_id).name != session_id:
        raise ValueError(f"Not a session id: {session_id!r}")


def session_file(directory: Path, session_id: str, suffix: str) -> Path:
    """The file in ``directory`` of the session ``session_id`` ending in ``suffix``.

    Raises ``ValueError`` for an id that isn't a plain file name.
    """
    check_session_id(session_id)
    return directory / f"{session_id}{suffix}"


@contextmanager
def locked_dir(directory: Path, operation: int) -> Iterator[None]:
    """Hold a lock of ``operation`` (``LOCK_EX`` or ``LOCK_SH``) on ``directory`` itself, so that
    orbits take, let go of and check the session locks in it one at a time."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(fd, operation)
        yield
    finally:
        os.close(fd)


def _lock_file(lock: Path) -> int | None:
    """A descriptor holding an exclusive lock on the file ``lock``, created if need be, with this
    process's id written in it; none when another open file holds the lock."""
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _lock_held(lock: Path) -> bool:
    """Whether an open file holds the lock on the file ``lock``."""
    try:
        fd = os.open(lock, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


class _Taken(Enum):
    """What came of trying to take a session's lock."""

    CREATED = auto()
    OURS = auto()
    REFUSED = auto()


class SessionLocks:
    """The locks of sessions, as ``<session id>.lock`` files in ``directory``.

    Each stands for one orbit: the locks it holds are its own, even against another
    ``SessionLocks`` on the same directory in the same process.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._held: dict[str, int] = {}
        # Guards _held and _closed between close() and a _take or _remove in a worker thread.
        self._guard = threading.Lock()
        self._closed = False

    async def acquire(self, session_id: str) -> bool:
        """Lock the session ``session_id``; returns ``False`` when another orbit holds its lock.

        Raises ``RuntimeError`` when the lock can't be taken, as after :meth:`close`.
        """
        taking = asyncio.ensure_future(asyncio.to_thread(self._acquire, session_id))
        try:
            return await asyncio.shield(taking) is not _Taken.REFUSED
        except asyncio.CancelledError:
            # The thread carries on; once it's done, let go of any lock it created (but not one
            # we already held). A lock that can't be let go of mustn't lose the cancellation.
            await asyncio.wait([taking])
            if taking.exception() is None and taking.result() is _Taken.CREATED:
                with suppress(RuntimeError):
                    await self.release(session_id)
            raise

    async def release(self, session_id: str) -> None:
        """Let go of the session ``session_id``'s lock if this holds it, and remove its file;
        raises ``RuntimeError`` when it can't, as after :meth:`close`."""
        if self._closed or session_id in self._held:
            await asyncio.to_thread(self._remove, session_id, "unlock")

    async def remove(self, session_id: str) -> None:
        """Remove the session ``session_id``'s lock, letting go of it if this holds it, for
        deleting the session; raises ``RuntimeError`` when it can't, as after :meth:`close`."""
        await asyncio.to_thread(self._remove, session_id, "delete")

    @contextmanager
    def checking(self) -> Iterator[None]:
        """Hold the directory's shared lock, so that :meth:`held_elsewhere` sees no lock half
        taken or let go of; raises ``OSError`` when the directory can't be locked."""
        with locked_dir(self._directory, fcntl.LOCK_SH):
            yield

    def held_elsewhere(self, session_id: str) -> bool:
        """Whether another orbit holds the session ``session_id``'s lock."""
        return session_id not in self._held and _lock_held(self.path(session_id))

    def close(self) -> None:
        """Let go of every lock this holds, leaving their files for another orbit to take, and take
        no more."""
        with self._guard:
            self._closed = True
            for fd in self._held.values():
                os.close(fd)
            self._held.clear()

    def path(self, session_id: str) -> Path:
        """The lock file of the session ``session_id``.

        Raises ``ValueError`` for an id that isn't a plain file name.
        """
        return session_file(self._directory, session_id, ".lock")

    def _acquire(self, session_id: str) -> _Taken:
        lock = self.path(session_id)
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            with locked_dir(lock.parent, fcntl.LOCK_EX):
                return self._take(session_id, lock)
        except OSError as error:
            raise RuntimeError(f"Couldn't lock the session {lock}: {error}") from error

    def _take(self, session_id: str, lock: Path) -> _Taken:
        """Take the session ``session_id``'s ``lock`` unless another orbit holds it; returns
        whether this took it, already held it, or was refused.

        Raises ``RuntimeError`` once this is closed.
        """
        with self._guard:
            if self._closed:
                raise RuntimeError(f"Couldn't lock the session {lock}: the locks are closed")
            if session_id in self._held:
                return _Taken.OURS
            fd = _lock_file(lock)
            if fd is None:
                return _Taken.REFUSED
            self._held[session_id] = fd
            return _Taken.CREATED

    def _remove(self, session_id: str, action: str) -> None:
        """Let go of the session ``session_id``'s lock if this holds it, and remove its file;
        errors say what failed to ``action`` it.

        Raises ``RuntimeError`` once this is closed, leaving the file to whichever orbit holds it.
        """
        lock = self.path(session_id)
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            with locked_dir(lock.parent, fcntl.LOCK_EX):
                self._let_go(session_id, lock, action)
                lock.unlink(missing_ok=True)
        except OSError as error:
            raise RuntimeError(f"Couldn't {action} the session {lock}: {error}") from error

    def _let_go(self, session_id: str, lock: Path, action: str) -> None:
        """Let go of the session ``session_id``'s ``lock`` if this holds it.

        Raises ``RuntimeError``, saying it failed to ``action`` it, once this is closed.
        """
        with self._guard:
            if self._closed:
                raise RuntimeError(f"Couldn't {action} the session {lock}: the locks are closed")
            fd = self._held.pop(session_id, None)
            if fd is not None:
                os.close(fd)
