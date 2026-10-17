"""Saved sessions on disk: one JSON file per session, and a lock file while an orbit has it open.

Inside a git repository they live in ``.git/orbit/sessions``, beside the coder's worktree, even
when orbit works in the user's checkout; outside one, in ``.orbit/sessions`` in the workspace.
Files are replaced atomically, so a crash leaves the old version or the new one, and orbit never
overwrites or deletes a file it can't read unless asked to delete that session. Nothing is pruned.

A session's lock is an exclusive ``flock`` on its ``.lock`` file, held open until the orbit lets
go of it; the kernel drops it when the orbit's process ends, so a lock file nobody holds is free to
take. The file names the holder's process only for whoever reads it. Taking, letting go of and
checking locks happen under a lock on the sessions directory, so a lock file is never removed
between another orbit opening it and locking it.
"""

import asyncio
import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum, auto
from pathlib import Path

from orbit.agent.application.ports import StoredSession
from orbit.agent.domain.sessions import (
    SessionRecord,
    Unreadable,
    parse_record,
    parse_review_count,
    record_json,
    review_count_json,
)
from orbit.agent.external.git_tree import ORBIT_DIR, git_common_dir

SESSIONS = "sessions"
REVIEWS = "reviews.json"


def state_dir(workspace: str) -> Path:
    """The directory orbit keeps saved sessions in for ``workspace``: ``orbit`` in the
    repository's shared git directory, or ``.orbit`` in ``workspace`` outside a repository (or
    without git)."""
    common = git_common_dir(workspace)
    return Path(common) / ORBIT_DIR if common else Path(workspace) / ".orbit"


def _write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` so that a crash leaves the old content or the new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(_write_temp(path, text))
    try:
        temp.replace(path)
    except BaseException:
        temp.unlink()
        raise
    _sync_dir(path.parent)


def _write_temp(path: Path, text: str) -> str:
    """A new file beside ``path`` holding ``text``, on disk; returns its path."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        os.unlink(temp)
        raise
    return temp


def _sync_dir(directory: Path) -> None:
    """Put ``directory``'s entries on disk, so a rename into it survives a crash."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_record(path: Path) -> SessionRecord | Unreadable:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return Unreadable(f"it can't be read: {error}")
    return parse_record(text)


@contextmanager
def _locked_dir(directory: Path, operation: int) -> Iterator[None]:
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


class SessionFiles:
    """Saved sessions in ``directory``'s ``sessions`` directory, with the review count beside it.

    Each stands for one orbit: the session locks it holds are its own, even against another
    ``SessionFiles`` in the same process.
    """

    def __init__(self, directory: Path | str) -> None:
        self._sessions = Path(directory) / SESSIONS
        self._reviews = Path(directory) / REVIEWS
        self._held: dict[str, int] = {}

    async def save(self, record: SessionRecord) -> None:
        path = self._file(record.id, ".json")
        try:
            await asyncio.to_thread(_write_atomic, path, record_json(record))
        except OSError as error:
            raise RuntimeError(f"Couldn't save the session to {path}: {error}") from error

    async def load(self, session_id: str) -> SessionRecord | Unreadable:
        return await asyncio.to_thread(_read_record, self._file(session_id, ".json"))

    async def sessions(self) -> list[StoredSession]:
        return await asyncio.to_thread(self._list)

    async def delete(self, session_id: str) -> None:
        path = self._file(session_id, ".json")
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError as error:
            raise RuntimeError(f"Couldn't delete the session {path}: {error}") from error
        await asyncio.to_thread(self._remove_lock, session_id, "delete")

    async def acquire(self, session_id: str) -> bool:
        taking = asyncio.ensure_future(asyncio.to_thread(self._acquire, session_id))
        try:
            return await asyncio.shield(taking) is not _Taken.REFUSED
        except asyncio.CancelledError:
            # The thread carries on; once it's done, let go of any lock it created (but not one
            # we already held).
            await asyncio.wait([taking])
            if taking.exception() is None and taking.result() is _Taken.CREATED:
                await self.release(session_id)
            raise

    async def release(self, session_id: str) -> None:
        if session_id in self._held:
            await asyncio.to_thread(self._remove_lock, session_id, "unlock")

    async def review_count(self) -> int:
        return await asyncio.to_thread(self._read_review_count)

    async def save_review_count(self, count: int) -> None:
        try:
            await asyncio.to_thread(_write_atomic, self._reviews, review_count_json(count))
        except OSError as error:
            message = f"Couldn't save the review count to {self._reviews}: {error}"
            raise RuntimeError(message) from error

    def _file(self, session_id: str, suffix: str) -> Path:
        """The file of the session ``session_id`` ending in ``suffix``.

        Raises ``ValueError`` for an id that isn't a plain file name.
        """
        if not session_id or Path(session_id).name != session_id:
            raise ValueError(f"Not a session id: {session_id!r}")
        return self._sessions / f"{session_id}{suffix}"

    def _list(self) -> list[StoredSession]:
        if not self._sessions.is_dir():
            return []
        with _locked_dir(self._sessions, fcntl.LOCK_SH):
            return [
                StoredSession(path.stem, _read_record(path), self._held_elsewhere(path.stem))
                for path in sorted(self._sessions.glob("*.json"))
            ]

    def _held_elsewhere(self, session_id: str) -> bool:
        """Whether another orbit holds the session ``session_id``'s lock."""
        return session_id not in self._held and _lock_held(self._file(session_id, ".lock"))

    def _acquire(self, session_id: str) -> _Taken:
        lock = self._file(session_id, ".lock")
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            with _locked_dir(lock.parent, fcntl.LOCK_EX):
                return self._take(session_id, lock)
        except OSError as error:
            raise RuntimeError(f"Couldn't lock the session {lock}: {error}") from error

    def _take(self, session_id: str, lock: Path) -> _Taken:
        """Take the session ``session_id``'s ``lock`` unless another orbit holds it; returns
        whether this took it, already held it, or was refused."""
        if session_id in self._held:
            return _Taken.OURS
        fd = _lock_file(lock)
        if fd is None:
            return _Taken.REFUSED
        self._held[session_id] = fd
        return _Taken.CREATED

    def _remove_lock(self, session_id: str, action: str) -> None:
        """Let go of the session ``session_id``'s lock if this holds it, and remove its file;
        errors say what failed to ``action`` it."""
        lock = self._file(session_id, ".lock")
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            with _locked_dir(lock.parent, fcntl.LOCK_EX):
                fd = self._held.pop(session_id, None)
                if fd is not None:
                    os.close(fd)
                lock.unlink(missing_ok=True)
        except OSError as error:
            raise RuntimeError(f"Couldn't {action} the session {lock}: {error}") from error

    def _read_review_count(self) -> int:
        try:
            return parse_review_count(self._reviews.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return 0
