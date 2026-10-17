"""Saved sessions on disk: one JSON file per session, and a lock file while an orbit has it open.

Inside a git repository they live in ``.git/orbit/sessions``, beside the coder's worktree, even
when orbit works in the user's checkout; outside one, in ``.orbit/sessions`` in the workspace.
Files are replaced atomically, so a crash leaves the old version or the new one, and orbit never
overwrites or deletes a file it can't read unless asked to delete that session. Nothing is pruned.
"""

import asyncio
import fcntl
import os
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import Enum, auto
from pathlib import Path

from orbit.agent.application.ports import StoredSession
from orbit.agent.domain.sessions import (
    SessionRecord,
    Unreadable,
    lock_is_stale,
    lock_owner,
    parse_record,
    parse_review_count,
    record_json,
    review_count_json,
)

SESSIONS = "sessions"
REVIEWS = "reviews.json"


def state_dir(workspace: str) -> Path:
    """The directory orbit keeps saved sessions in for ``workspace``: ``orbit`` in the
    repository's shared git directory, or ``.orbit`` in ``workspace`` outside a repository (or
    without git)."""
    common = _git_common_dir(workspace)
    return Path(common) / "orbit" if common else Path(workspace) / ".orbit"


def _git_common_dir(path: str) -> str | None:
    """The git directory the repository ``path`` is in shares among its worktrees; none outside
    one (or without git)."""
    try:
        result = subprocess.run(
            ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def pid_alive(pid: int) -> bool:
    """Whether the process ``pid`` is running."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:  # It's running, as another user.
        return True
    return True


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
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
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


def _read_lock(path: Path) -> str:
    """The lock file ``path``'s content; empty when there's none or it can't be read."""
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


@contextmanager
def _exclusive(directory: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``directory`` itself, so that orbits taking locks in it take
    them one at a time."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class _Taken(Enum):
    """What came of trying to take a session's lock."""

    CREATED = auto()
    OURS = auto()
    REFUSED = auto()


class SessionFiles:
    """Saved sessions in ``directory``'s ``sessions`` directory, with the review count beside it.

    Locks name the process ``pid`` (this one by default); ``alive`` tells whether another
    lock's process is still running.
    """

    def __init__(
        self,
        directory: Path | str,
        pid: int | None = None,
        alive: Callable[[int], bool] = pid_alive,
    ) -> None:
        self._sessions = Path(directory) / SESSIONS
        self._reviews = Path(directory) / REVIEWS
        self._pid = os.getpid() if pid is None else pid
        self._alive = alive

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
        for path in (self._file(session_id, ".json"), self._file(session_id, ".lock")):
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except OSError as error:
                raise RuntimeError(f"Couldn't delete the session {path}: {error}") from error

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
        await asyncio.to_thread(self._release, self._file(session_id, ".lock"))

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
        return [
            StoredSession(
                path.stem, _read_record(path), self._held_elsewhere(path.with_suffix(".lock"))
            )
            for path in sorted(self._sessions.glob("*.json"))
        ]

    def _held_elsewhere(self, lock: Path) -> bool:
        """Whether another running orbit holds ``lock``."""
        text = _read_lock(lock)
        return lock_owner(text) != self._pid and not lock_is_stale(text, self._alive)

    def _acquire(self, session_id: str) -> _Taken:
        lock = self._file(session_id, ".lock")
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            with _exclusive(lock.parent):
                return self._take(lock)
        except OSError as error:
            raise RuntimeError(f"Couldn't lock the session {lock}: {error}") from error

    def _take(self, lock: Path) -> _Taken:
        """Take ``lock`` unless another running orbit holds it; returns whether this created it,
        found it already ours, or was refused.

        Callers hold the sessions directory's exclusive lock, so no other orbit takes over a
        stale lock between this deciding it's stale and replacing it.
        """
        if self._create_lock(lock):
            return _Taken.CREATED
        if self._held_elsewhere(lock):
            return _Taken.REFUSED
        if lock_owner(_read_lock(lock)) == self._pid:
            return _Taken.OURS
        lock.unlink(missing_ok=True)  # Stale: its orbit has ended.
        return _Taken.CREATED if self._create_lock(lock) else _Taken.REFUSED

    def _create_lock(self, lock: Path) -> bool:
        """Create ``lock`` naming this process, complete, unless it exists; returns whether it
        was created."""
        temp = _write_temp(lock, str(self._pid))
        try:
            os.link(temp, lock)
        except FileExistsError:
            return False
        finally:
            os.unlink(temp)
        return True

    def _release(self, lock: Path) -> None:
        if lock_owner(_read_lock(lock)) == self._pid:
            lock.unlink(missing_ok=True)

    def _read_review_count(self) -> int:
        try:
            return parse_review_count(self._reviews.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return 0
