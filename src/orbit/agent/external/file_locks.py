"""File locks: an exclusive ``flock`` on a file that names whoever holds it.

The lock is held while its descriptor stays open; the kernel drops it when the holder's process
ends, so a lock file nobody holds is free to take. Lock files are readable by their owner alone,
as only the user's own orbits read who holds them.
"""

import fcntl
import os
from pathlib import Path

from orbit.agent.external.whole_files import PRIVATE


def take_flock(path: Path, text: str, mode: int = PRIVATE) -> int | None:
    """A descriptor holding an exclusive lock on the file ``path``, created with ``mode`` if
    need be, with ``text`` alone written in it; none when another open file holds the lock.

    Raises ``OSError`` when the file can't be opened, locked or written.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, mode)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rewrite(fd, text)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def rewrite(fd: int, text: str) -> None:
    """Have the file open as ``fd`` say ``text`` alone."""
    os.ftruncate(fd, 0)
    data = text.encode()
    written = 0
    while written < len(data):
        written += os.pwrite(fd, data[written:], written)
