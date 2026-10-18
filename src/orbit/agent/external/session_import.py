"""Importing the sessions an older orbit saved as files into the SQLite store.

The old ``sessions`` folder and ``reviews.json`` are read, then copied into the store in one
transaction that commits only once the folder is renamed to ``sessions.imported`` (and
``reviews.json`` to ``reviews.json.imported``), or to ``.2``, ``.3`` and so on after that name
when an earlier import left one there, so they're imported once; when anything fails the files
are renamed back and nothing is imported, ready for another try. Old files are never deleted or
replaced, and rows the store already has are never overwritten.
"""

import asyncio
import os
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from orbit.agent.domain.sessions import Unreadable, parse_record, parse_review_count
from orbit.agent.external.session_files import REVIEWS, SESSIONS
from orbit.agent.external.session_locks import SessionLocks
from orbit.agent.external.sqlite_store import SessionRow, SqliteStore

IMPORTED = f"{SESSIONS}.imported"
REVIEWS_IMPORTED = f"{REVIEWS}.imported"
# Never JSON, so a session file that isn't UTF-8 can't be read from the store either.
NOT_UTF_8 = "This session file isn't UTF-8:\n"


class ImportBlocked(Exception):
    """The old session files can't be imported, for the reason in the message, which names the
    path."""


def imported_note(count: int, legacy_dir: Path) -> str:
    """The note that ``count`` saved sessions were imported from ``legacy_dir``'s sessions
    folder, which is now renamed."""
    noun = "session" if count == 1 else "sessions"
    sessions = legacy_dir / SESSIONS
    return f"Imported {count} saved {noun} from {sessions}; the old folder is now {IMPORTED}."


@dataclass(frozen=True)
class _Legacy:
    """What an older orbit saved: its sessions' ``rows`` and its ``review_count``, if any."""

    rows: tuple[SessionRow, ...]
    review_count: int | None


async def import_session_files(store: SqliteStore, legacy_dir: Path) -> int | None:
    """Import the sessions and review count saved as files in ``legacy_dir`` into ``store``;
    returns how many sessions it imported, or none when there's no ``sessions`` folder.

    Raises :class:`ImportBlocked`, having imported nothing, when an orbit has one of the sessions
    open, or the files can't be read or renamed, or the database fails.
    """
    sessions = legacy_dir / SESSIONS
    if not await asyncio.to_thread(sessions.is_dir):
        return None
    legacy = await asyncio.to_thread(_read, legacy_dir)
    try:
        return await store.insert_new(
            legacy.rows, legacy.review_count, partial(_retired, legacy_dir)
        )
    except sqlite3.Error as error:
        raise ImportBlocked(f"Couldn't import the sessions in {sessions}: {error}") from error


def _read(legacy_dir: Path) -> _Legacy:
    """The sessions and review count saved in ``legacy_dir``.

    Raises :class:`ImportBlocked` when they can't be read.
    """
    sessions = legacy_dir / SESSIONS
    try:
        rows = tuple(_row(path) for path in sorted(sessions.glob("*.json")))
        return _Legacy(rows, _review_count(legacy_dir / REVIEWS))
    except OSError as error:
        raise ImportBlocked(f"Couldn't read the sessions in {sessions}: {error}") from error


def _row(path: Path) -> SessionRow:
    """The row of the session saved in ``path``, which goes by the file's name, as the old files
    were listed, whatever id its record gives; an unreadable one is named by the file too, with
    its text kept so it lists as unreadable."""
    text = _text(path)
    record = parse_record(text)
    if isinstance(record, Unreadable):
        return SessionRow(path.stem, path.stem, "", text)
    return SessionRow(path.stem, record.name, record.last_active.isoformat(), text)


def _text(path: Path) -> str:
    """The text saved in ``path``; one that isn't UTF-8 is decoded with replacement characters
    behind ``NOT_UTF_8``, so that it stays unreadable as it was."""
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return NOT_UTF_8 + data.decode("utf-8", errors="replace")


def _review_count(path: Path) -> int | None:
    """The review count saved in ``path``; none when there's no such file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    return parse_review_count(text)


@contextmanager
def _retired(legacy_dir: Path) -> Iterator[None]:
    """Rename ``legacy_dir``'s sessions folder and review count as imported, while no orbit can
    take a session's lock in it; they're renamed back if what this holds fails.

    Raises :class:`ImportBlocked` when an orbit has a session there open, or the folder can't be
    checked or renamed.
    """
    sessions = legacy_dir / SESSIONS
    with ExitStack() as stack:
        locks = SessionLocks(sessions)
        try:
            stack.enter_context(locks.checking())
            held = [path for path in sessions.glob("*.lock") if locks.held_elsewhere(path.stem)]
        except OSError as error:
            raise ImportBlocked(f"Couldn't check the sessions in {sessions}: {error}") from error
        if held:
            raise ImportBlocked(
                f"Can't import the sessions in {sessions}: an orbit still has a session there "
                "open. Close it and try again."
            )
        stack.enter_context(_moved(sessions, _free(legacy_dir / IMPORTED)))
        if (legacy_dir / REVIEWS).exists():
            stack.enter_context(_moved(legacy_dir / REVIEWS, _free(legacy_dir / REVIEWS_IMPORTED)))
        yield


def _free(path: Path) -> Path:
    """``path``, or the first of ``path.2``, ``path.3`` and so on that nothing is at, not even a
    broken link, so that renaming to it replaces nothing."""
    free, number = path, 1
    while os.path.lexists(free):
        number += 1
        free = path.with_name(f"{path.name}.{number}")
    return free


@contextmanager
def _moved(source: Path, target: Path) -> Iterator[None]:
    """Rename ``source`` to ``target``, and back if what this holds fails.

    Raises :class:`ImportBlocked` naming both when it can't be renamed, or renamed back, the
    latter chained to what failed.
    """
    try:
        source.rename(target)
    except OSError as error:
        raise ImportBlocked(f"Couldn't rename {source} to {target}: {error}") from error
    try:
        yield
    except BaseException as error:
        try:
            target.rename(source)
        except OSError as undo:
            raise ImportBlocked(f"Couldn't rename {target} back to {source}: {undo}") from error
        raise
