"""Fallback files: where a session's events go, one encoded envelope per line, while the store
can't take them.

A session's file is ``fallback/<workspace id>-<session id>.jsonl`` in the state directory, else
``fallback/<session id>.jsonl`` in the repository's orbit directory (``.git/orbit``, or
``.orbit`` outside git). Each is readable by its owner alone.
"""

import asyncio
import os
from collections.abc import Sequence
from pathlib import Path

from orbit.protocol import EventEnvelope, encode

FALLBACK = "fallback"


def _append(path: Path, lines: str) -> None:
    """Add ``lines`` to the file at ``path``, making it, and its directory, owner-only as
    needed; raises ``OSError`` when it can't."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with open(descriptor, "a", encoding="utf-8") as file:
        os.fchmod(descriptor, 0o600)
        file.write(lines)


class FallbackFiles:
    """The fallback files of the workspace ``workspace_id``, in ``state_dir`` or else in
    ``repository_dir``, the repository's orbit directory. A session's events go on to the file
    they last went to while it can be written."""

    def __init__(self, state_dir: Path, workspace_id: str, repository_dir: Path) -> None:
        self._state_dir = state_dir
        self._workspace_id = workspace_id
        self._repository_dir = repository_dir
        self._used: dict[str, Path] = {}  # The file each session's events last went to.

    def places(self, session_id: str) -> list[Path]:
        """Where the session ``session_id``'s fallback file goes, the first that works."""
        return [
            self._state_dir / FALLBACK / f"{self._workspace_id}-{session_id}.jsonl",
            self._repository_dir / FALLBACK / f"{session_id}.jsonl",
        ]

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
