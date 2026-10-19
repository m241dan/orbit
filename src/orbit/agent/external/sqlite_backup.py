"""Backing the SQLite database up to a file, never onto the database itself."""

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

WAL_FILES = ("-wal", "-shm")  # The endings of the files SQLite keeps beside the database.


def is_database_file(target: Path, database: Path) -> bool:
    """Whether ``target`` is ``database`` or its ``-wal`` or ``-shm`` file, by any path."""
    files = (database, *(database.with_name(database.name + end) for end in WAL_FILES))
    return any(_same_file(target, path) for path in files)


def _same_file(target: Path, path: Path) -> bool:
    """Whether ``target`` and ``path`` resolve to one path, or exist as one file."""
    if target.resolve() == path.resolve():
        return True
    return target.exists() and path.exists() and os.path.samefile(target, path)


def copy_to(source: sqlite3.Connection, target: Path) -> None:
    """Back ``source`` up to ``target``, owner read-write, through a temporary file beside it
    that replaces ``target`` only once the copy is whole, and is removed if it fails."""
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        with closing(sqlite3.connect(temporary)) as copy:
            source.backup(copy)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
