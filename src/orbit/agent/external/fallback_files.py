"""Fallback files: where a session's events go, one encoded envelope per line, while the store
can't take them.

A session's file is ``fallback/<workspace id>-<session id>.jsonl`` in the state directory, else
in the repository's orbit directory (``.git/orbit``, or ``.orbit`` outside git), which the
repository's worktrees share. Each is readable by its owner alone. Earlier orbits named the
repository's ``fallback/<session id>.jsonl``.
"""

import asyncio
import os
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from orbit.agent.application.ports import ParkedFile, ReadFailed, StoreFailed
from orbit.protocol import EventEnvelope, ProtocolError, decode_event, encode

FALLBACK = "fallback"
SUFFIX = ".jsonl"
SEPARATOR = "-"  # Ids have none, so a name without it is an earlier orbit's, by session alone.


def _append(path: Path, lines: str) -> None:
    """Add ``lines`` to the file at ``path``, making it, and its directory, owner-only as
    needed, after a newline if its last line has none; raises ``OSError`` when it can't,
    leaving the file as it was if it can."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        size = os.fstat(descriptor).st_size
        data = lines.encode("utf-8")
        if size and os.pread(descriptor, 1, size - 1) != b"\n":
            data = b"\n" + data  # Keeps a torn line, from a failed write, a line of its own.
        try:
            _write_all(descriptor, data)
        except OSError:
            with suppress(OSError):  # A torn line is skipped on reading anyway.
                os.ftruncate(descriptor, size)
            raise
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, data: bytes) -> None:
    """Write all of ``data`` to the file open as ``descriptor``; raises ``OSError`` when it
    can't."""
    while data:
        data = data[os.write(descriptor, data) :]


def _read(path: Path) -> list[EventEnvelope]:
    """The envelopes in the file at ``path``, one per line, but for those of types this orbit
    doesn't know and lines that aren't envelopes before the last that ends in a newline: those
    torn by a failed write, which the next write starts after. Raises :class:`ReadFailed` when
    it can't be read or that last line isn't an envelope, so it isn't such a file."""
    try:
        *lines, last = path.read_bytes().split(b"\n")
    except OSError as error:
        raise ReadFailed(str(error)) from error
    return [envelope for envelope in _decode_lines(lines, last) if envelope is not None]


def _decode_lines(lines: list[bytes], last: bytes) -> list[EventEnvelope | None]:
    """What each of ``lines``, those ending in a newline, and ``last`` hold, as :func:`_read`
    reads them."""
    *earlier, whole = [line for line in lines if line.strip()] or [b""]
    return [*map(_decode_torn, earlier), _decode(whole) if whole else None, _decode_torn(last)]


def _decode(line: bytes) -> EventEnvelope | None:
    """The envelope ``line`` holds, or ``None`` for one of a type this orbit doesn't know;
    raises :class:`ReadFailed` if it holds neither."""
    try:
        return decode_event(line.decode("utf-8"))
    except (UnicodeDecodeError, ProtocolError) as error:
        raise ReadFailed(str(error)) from error


def _decode_torn(line: bytes) -> EventEnvelope | None:
    """The envelope ``line`` holds, or ``None`` for one of a type this orbit doesn't know or a
    line that isn't one, as a torn line isn't."""
    with suppress(ReadFailed):
        return _decode(line)
    return None


def _delete(path: Path) -> None:
    """Delete the file at ``path``; raises :class:`StoreFailed` when it can't."""
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        raise StoreFailed(str(error)) from error


def _repository_file(path: Path, prefix: str) -> ParkedFile | None:
    """The file at ``path`` in the repository, if it may be the workspace's whose names start
    with ``prefix``: one so named, or a legacy one, named by its session alone; ``None`` for
    another workspace's."""
    if path.stem.startswith(prefix):
        return ParkedFile(str(path), path.stem.removeprefix(prefix))
    if SEPARATOR in path.stem:
        return None
    return ParkedFile(str(path), path.stem, legacy=True)


def remove_fallback_files(directories: Sequence[Path], workspace_id: str) -> int:
    """Delete the fallback files of the workspace ``workspace_id`` in each of ``directories``,
    the state directory and the repository's orbit directory, those named for it alone; returns
    how many it deleted. Raises ``OSError`` if one can't be."""
    files = [
        path
        for directory in directories
        for path in sorted((directory / FALLBACK).glob(f"{workspace_id}{SEPARATOR}*{SUFFIX}"))
    ]
    for path in files:
        path.unlink(missing_ok=True)
    return len(files)


class FallbackFiles:
    """The fallback files of the workspace ``workspace_id``, in ``state_dir`` or else in
    ``repository_dir``, the repository's orbit directory. A session's events go on to the file
    they last went to while it can be written. The files earlier runs left are found to be
    imported."""

    def __init__(self, state_dir: Path, workspace_id: str, repository_dir: Path) -> None:
        self._state_dir = state_dir
        self._workspace_id = workspace_id
        self._repository_dir = repository_dir
        self._used: dict[str, Path] = {}  # The file each session's events last went to.

    def places(self, session_id: str) -> list[Path]:
        """Where the session ``session_id``'s fallback file goes, the first that works."""
        name = f"{self._workspace_id}{SEPARATOR}{session_id}{SUFFIX}"
        return [self._state_dir / FALLBACK / name, self._repository_dir / FALLBACK / name]

    async def write(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> str | None:
        lines = "".join(f"{encode(envelope)}\n" for envelope in envelopes)
        return await asyncio.to_thread(self._write, session_id, lines)

    def _write(self, session_id: str, lines: str) -> str | None:
        """Add ``lines`` to the session ``session_id``'s file in the first place that can be
        written; returns its path, or ``None`` when neither can be."""
        places = self.places(session_id)
        used = self._used.get(session_id)
        if used is not None:
            places = [used, *(place for place in places if place != used)]
        for place in places:
            try:
                _append(place, lines)
            except OSError:
                continue
            self._used[session_id] = place
            return str(place)
        return None

    async def remove(self, session_id: str) -> None:
        await asyncio.to_thread(self._remove, session_id)

    def _remove(self, session_id: str) -> None:
        """Delete the session ``session_id``'s files, those that can be."""
        self._used.pop(session_id, None)
        for place in self.places(session_id):
            with suppress(OSError):  # Left to be imported again, its events skipped as stored.
                place.unlink(missing_ok=True)

    async def parked(self) -> list[ParkedFile]:
        return await asyncio.to_thread(self._parked)

    def _parked(self) -> list[ParkedFile]:
        """This workspace's files in the state directory, then the repository's, each by
        path, the repository's legacy ones, which may be this workspace's, among them."""
        prefix = f"{self._workspace_id}{SEPARATOR}"
        state = sorted((self._state_dir / FALLBACK).glob(f"{prefix}*{SUFFIX}"))
        repository = sorted((self._repository_dir / FALLBACK).glob(f"*{SUFFIX}"))
        found = (_repository_file(path, prefix) for path in repository)
        return [
            *(ParkedFile(str(path), path.stem.removeprefix(prefix)) for path in state),
            *(file for file in found if file is not None),
        ]

    async def read(self, file: ParkedFile) -> list[EventEnvelope]:
        return await asyncio.to_thread(_read, Path(file.path))

    async def delete(self, file: ParkedFile) -> None:
        await asyncio.to_thread(_delete, Path(file.path))
