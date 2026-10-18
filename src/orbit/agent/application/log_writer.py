"""The writer that keeps sessions' events in their log, behind their delivery."""

import asyncio
from collections.abc import Sequence

from orbit.agent.application.failures import ReportedOnce
from orbit.agent.application.ports import EventLog, Publish
from orbit.protocol import EventEnvelope


def _by_session(queued: Sequence[tuple[str, EventEnvelope]]) -> dict[str, list[EventEnvelope]]:
    """The envelopes ``queued``, each with its session's id, grouped by session in the order
    they were queued."""
    sessions: dict[str, list[EventEnvelope]] = {}
    for session_id, envelope in queued:
        sessions.setdefault(session_id, []).append(envelope)
    return sessions


class LogWriter:
    """Writes the envelopes added here to ``log`` in a task of its own, in the order they were
    added: whatever has queued since its last write goes in one append per session. A failed
    append is reported with ``publish`` once, until one works again, and later envelopes are
    still written. Adding never waits for the log."""

    def __init__(self, log: EventLog, publish: Publish) -> None:
        self._log = log
        self._appends = ReportedOnce(publish)
        self._queued: list[tuple[str, EventEnvelope]] = []
        self._ready = asyncio.Event()  # Set while envelopes are queued, or once closing.
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False

    def add(self, session_id: str, envelope: EventEnvelope) -> None:
        """Queue ``envelope``, of the session ``session_id``, to be written, starting the writer
        with the first; once the writer is closed, nothing more is written."""
        if self._closed:
            return
        self._queued.append((session_id, envelope))
        self._ready.set()
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        """Write what's queued, then stop the writer; cancelled, it stops at once, an append in
        progress written whole or not at all."""
        self._closing = True
        self._ready.set()
        try:
            if self._task is not None:
                await self._task
        finally:
            self._closed = True

    async def _run(self) -> None:
        """Write what's queued as it queues, until closing leaves nothing queued."""
        while not self._closing or self._queued:
            await self._ready.wait()
            self._ready.clear()
            queued, self._queued = self._queued, []
            for session_id, envelopes in _by_session(queued).items():
                await self._appends.attempt(self._log.append(session_id, envelopes))
