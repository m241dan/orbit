"""Importing the sessions an older orbit saved as files into the SQLite store.

Once no orbit has one of the old sessions open, the old ``sessions`` folder and ``reviews.json``
are read and copied into the store in one
transaction that commits only once the folder is renamed to ``sessions.imported`` (and
``reviews.json`` to ``reviews.json.imported``), or to ``-2``, ``-3`` and so on after that name
when an earlier import left one there, so they're imported once; when anything fails the files
are renamed back and nothing is imported, ready for another try. Old files are never deleted or
replaced, and rows the store already has are never overwritten. The folder's shared lock is held
from the check until the rename, so no orbit can open a session and save it in between. A folder
that another orbit imports and renames first is left to it, as nothing to import.
"""

import asyncio
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from orbit.agent.application.ports import StorageFailed
from orbit.agent.domain.sessions import Unreadable, parse_record, parse_review_count
from orbit.agent.external.session_files import REVIEWS, SESSIONS
from orbit.agent.external.session_locks import SessionLocks
from orbit.agent.external.sqlite_store import NewRows, SessionRow, WorkspaceStore

IMPORTED = f"{SESSIONS}.imported"
REVIEWS_IMPORTED = f"{REVIEWS}.imported"
# Never JSON, so a session file that isn't UTF-8 can't be read from the store either.
NOT_UTF_8 = "This session file isn't UTF-8:\n"


class ImportBlocked(Exception):
    """The old session files can't be imported, for the reason in the message, which names the
    path."""


class _Gone(Exception):
    """The old sessions folder went, imported by another orbit, before this could import it."""


@dataclass(frozen=True)
class Imported:
    """An import of ``count`` saved sessions, whose folder is now named ``folder``."""

    count: int
    folder: str


async def import_session_files(store: WorkspaceStore, legacy_dir: Path) -> Imported | None:
    """Import the sessions and review count saved as files in ``legacy_dir`` into ``store``;
    returns how many sessions it imported and where their folder went, or none when there's no
    ``sessions`` folder, or it goes before it's renamed.

    Raises :class:`ImportBlocked`, having imported nothing, when an orbit has one of the sessions
    open, or the files can't be read or renamed, or the database fails.
    """
    sessions = legacy_dir / SESSIONS
    if not await asyncio.to_thread(sessions.is_dir):
        return None
    retired: list[str] = []
    try:
        count = await store.insert_new(
            partial(_checked, legacy_dir), partial(_retired, legacy_dir, retired)
        )
    except _Gone:
        return None
    except StorageFailed as failed:
        message = f"Couldn't import the sessions in {sessions}: {failed.error}"
        raise ImportBlocked(message) from failed
    return Imported(count, retired[0])


@contextmanager
def _checked(legacy_dir: Path) -> Iterator[NewRows]:
    """What's saved in ``legacy_dir``, read while no orbit has a session there open, and held so
    that none can open one until this is done.

    Raises :class:`ImportBlocked` when an orbit has a session there open, or the folder can't be
    checked or read.
    """
    sessions = legacy_dir / SESSIONS
    with ExitStack() as stack:
        locks = SessionLocks(sessions)
        try:
            stack.enter_context(locks.checking())
            held = [path for path in sessions.glob("*.lock") if locks.held_elsewhere(path.stem)]
        except FileNotFoundError as error:
            raise _Gone from error
        except OSError as error:
            raise ImportBlocked(f"Couldn't check the sessions in {sessions}: {error}") from error
        if held:
            raise ImportBlocked(
                f"Can't import the sessions in {sessions}: an orbit still has a session there "
                "open. Close it and try again."
            )
        yield _read(legacy_dir)


def _read(legacy_dir: Path) -> NewRows:
    """The sessions and review count saved in ``legacy_dir``.

    Raises :class:`ImportBlocked` when they can't be read.
    """
    sessions = legacy_dir / SESSIONS
    try:
        rows = tuple(_row(path) for path in sorted(sessions.glob("*.json")))
        return NewRows(rows, _review_count(legacy_dir / REVIEWS))
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
def _retired(legacy_dir: Path, retired: list[str]) -> Iterator[None]:
    """Rename ``legacy_dir``'s sessions folder and review count as imported, adding the
    folder's new name to ``retired``; they're renamed back if what this holds fails.

    Raises :class:`ImportBlocked` when they can't be renamed, and ``_Gone`` when the folder has
    gone.
    """
    sessions = legacy_dir / SESSIONS
    if not os.path.lexists(sessions):
        raise _Gone
    with ExitStack() as stack:
        folder = _free(legacy_dir / IMPORTED)
        stack.enter_context(_moved(sessions, folder))
        _retire_reviews(stack, legacy_dir)
        retired.append(folder.name)
        yield


def _retire_reviews(stack: ExitStack, legacy_dir: Path) -> None:
    """Rename ``legacy_dir``'s review count, if it has one, as imported, and back when
    ``stack`` closes on a failure."""
    reviews = legacy_dir / REVIEWS
    if reviews.exists():
        stack.enter_context(_moved(reviews, _free(legacy_dir / REVIEWS_IMPORTED)))


def _free(path: Path) -> Path:
    """``path``, or the first of ``path-2``, ``path-3`` and so on that nothing is at, not even a
    broken link, so that renaming to it replaces nothing."""
    free, number = path, 1
    while os.path.lexists(free):
        number += 1
        free = path.with_name(f"{path.name}-{number}")
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
