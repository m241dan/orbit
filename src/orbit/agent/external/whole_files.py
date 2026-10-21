"""Files written whole or not at all, through a temporary file beside each."""

import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PRIVATE = 0o600  # Readable and writable by the owner alone.


def write_whole(path: Path, text: str, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` through a temporary file beside it that replaces ``path`` only
    once it's whole and on disk. The file's mode is ``mode``, or else the mode of the file it
    replaces, or else the one the umask gives a new file. Raises ``OSError`` when it can't."""
    with replacing(path, mode) as temporary:
        _write_synced(temporary, text)


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` as :func:`write_whole` does, readable and writable by its
    owner alone; the directory is made owner-only if it has to be made. Raises ``OSError`` when
    it can't."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_whole(path, text, PRIVATE)


def create_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` as :func:`write_private` does, but only if there's no file
    there yet, so that the first of several writers wins. Raises ``FileExistsError`` if there
    is one, and ``OSError`` when it can't."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = _create_beside(path, PRIVATE)
    try:
        _write_synced(temporary, text)
        os.link(temporary, path)
    finally:
        temporary.unlink()


@contextmanager
def replacing(path: Path, mode: int | None = None) -> Iterator[Path]:
    """A new, empty temporary file beside ``path``, with the mode :func:`write_whole` gives,
    that replaces ``path`` once the block ends; it's removed if the block or the replacing
    fails."""
    temporary = _create_beside(path, mode)
    try:
        yield temporary
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _create_beside(path: Path, mode: int | None) -> Path:
    """A new, empty file beside ``path`` with ``mode``, or else ``path``'s mode, or else the
    umask's."""
    if mode is None:
        mode = _mode_of(path)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o666 if mode is None else PRIVATE)
    try:
        if mode is not None:
            os.fchmod(descriptor, mode)
    except BaseException:
        temporary.unlink()
        raise
    finally:
        os.close(descriptor)
    return temporary


def _mode_of(path: Path) -> int | None:
    """The permission bits of the file at ``path``; none when there isn't one."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None


def _write_synced(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` and wait until it's on disk."""
    with path.open("w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
