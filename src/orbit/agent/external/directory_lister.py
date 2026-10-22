"""Reading the directories on the server's host for frontends browsing them, off the event
loop."""

import asyncio
import os
from pathlib import Path

from orbit.agent.domain.listings import FoundDirectory


class DirectoryLister:
    """Reads the directories on the server's host as its account sees them."""

    async def resolved(self, path: str) -> str:
        """``path``, absolute, with every symlink in it followed, as far as there's anything
        there."""
        return await asyncio.to_thread(lambda: str(Path(path).resolve()))

    async def directories(self, path: str) -> list[FoundDirectory]:
        """The directories in the directory at ``path``, symlinks to directories included.
        Raises :class:`OSError`, such as :class:`FileNotFoundError` or
        :class:`PermissionError`, if it can't be read."""
        # A large directory, or a slow filesystem, would stall the event loop.
        return await asyncio.to_thread(_directories, path)


def _directories(path: str) -> list[FoundDirectory]:
    """The directories in the directory at ``path``, symlinks to directories included."""
    with os.scandir(path) as entries:
        return [_found(entry) for entry in entries if entry.is_dir()]


def _found(entry: os.DirEntry[str]) -> FoundDirectory:
    """The directory ``entry``, with its path once its symlinks are followed, whether it holds
    ``.git``, and, for a symlink, its target."""
    return FoundDirectory(
        name=entry.name,
        path=os.path.realpath(entry.path),
        is_repository=os.path.lexists(os.path.join(entry.path, ".git")),
        target=os.readlink(entry.path) if entry.is_symlink() else None,
    )
