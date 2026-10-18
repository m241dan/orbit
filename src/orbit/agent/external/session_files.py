"""Saved sessions on disk: one JSON file per session, and a lock file while an orbit has it open.

Inside a git repository they live in ``.git/orbit/sessions``, beside the coder's worktree, even
when orbit works in the user's checkout; outside one, in ``.orbit/sessions`` in the workspace.
Files are replaced atomically, so a crash leaves the old version or the new one, and orbit never
overwrites or deletes a file it can't read unless asked to delete that session. Nothing is pruned.

A session's lock is a :class:`SessionLocks` lock file beside its JSON file.
"""

import asyncio
import os
import tempfile
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
from orbit.agent.external.session_locks import SessionLocks, session_file

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


class SessionFiles:
    """Saved sessions in ``directory``'s ``sessions`` directory, with the review count beside it.

    Each stands for one orbit: the session locks it holds are its own, even against another
    ``SessionFiles`` in the same process.
    """

    def __init__(self, directory: Path | str) -> None:
        self._sessions = Path(directory) / SESSIONS
        self._reviews = Path(directory) / REVIEWS
        self._locks = SessionLocks(self._sessions)

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
        await self._locks.remove(session_id)

    async def acquire(self, session_id: str) -> bool:
        return await self._locks.acquire(session_id)

    async def release(self, session_id: str) -> None:
        await self._locks.release(session_id)

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
        return session_file(self._sessions, session_id, suffix)

    def _list(self) -> list[StoredSession]:
        if not self._sessions.is_dir():
            return []
        # Like a missing directory, one that can't be opened or locked has no sessions to list.
        try:
            with self._locks.checking():
                return [
                    StoredSession(
                        path.stem, _read_record(path), self._locks.held_elsewhere(path.stem)
                    )
                    for path in sorted(self._sessions.glob("*.json"))
                ]
        except OSError:
            return []

    def _read_review_count(self) -> int:
        try:
            return parse_review_count(self._reviews.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return 0
