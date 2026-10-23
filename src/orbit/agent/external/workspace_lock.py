"""The workspace lock: which orbit owns a repository, and so runs its work.

It's an exclusive ``flock`` on ``workspace.lock`` in orbit's directory (``.git/orbit``, or
``.orbit`` outside git), held open while the owner runs; the kernel drops it when the owner's
process ends, so a lock file nobody holds is free to take. The file names the owner, for whoever
reads it.
"""

import os
from pathlib import Path

from orbit.agent.domain.ownership import WorkspaceOwner, owner_text, parse_owner
from orbit.agent.external.file_locks import rewrite, take_flock

LOCK_FILE = "workspace.lock"


class WorkspaceLock:
    """The lock on the repository whose orbit directory is ``directory``.

    Each stands for one orbit: another ``WorkspaceLock`` on the same directory, even in the same
    process, can't take it while this holds it.
    """

    def __init__(self, directory: Path) -> None:
        self.path = directory / LOCK_FILE
        self._fd: int | None = None
        self._url: str | None = None

    @property
    def held(self) -> bool:
        """Whether this holds the lock."""
        return self._fd is not None

    def take(self) -> WorkspaceOwner | None:
        """Take the lock, naming this process as the owner; returns the owner when another orbit
        holds it instead.

        Raises ``OSError`` when the lock file can't be made, opened or written.
        """
        fd = take_flock(self.path, owner_text(WorkspaceOwner(pid=os.getpid(), url=self._url)))
        if fd is None:
            return self.owner()
        self._fd = fd
        return None

    def announce(self, url: str) -> None:
        """Say the owner is the ``orbit serve`` listening at ``url``, if this holds the lock,
        and once it takes it later; raises ``OSError`` when it can't be written."""
        self._url = url
        if self._fd is not None:
            rewrite(self._fd, owner_text(WorkspaceOwner(pid=os.getpid(), url=url)))

    def owner(self) -> WorkspaceOwner:
        """The owner the lock file names; one naming nobody if it can't be read."""
        try:
            return parse_owner(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return WorkspaceOwner()

    def close(self) -> None:
        """Let go of the lock, if this holds it, leaving its file for another orbit to take."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
