"""The writer that keeps sessions' events in their log, behind their delivery."""

import asyncio
from collections.abc import Sequence

from orbit.agent.application.failures import ReportedOnce
from orbit.agent.application.ports import EventLog, Publish
from orbit.agent.domain.replay import LogBounds
from orbit.protocol import EventEnvelope


def _in_order(envelopes: Sequence[EventEnvelope]) -> list[EventEnvelope]:
    """``envelopes`` by ``seq``, oldest first, each ``seq`` once."""
    by_seq = {envelope.seq: envelope for envelope in envelopes}
    return [by_seq[seq] for seq in sorted(by_seq)]


def _latest(envelopes: Sequence[EventEnvelope], limit: int) -> list[EventEnvelope]:
    """The last ``limit`` of ``envelopes``, which are in order."""
    return list(envelopes[max(0, len(envelopes) - limit) :])


def _by_session(queued: Sequence[tuple[str, EventEnvelope]]) -> dict[str, list[EventEnvelope]]:
    """The envelopes ``queued``, each with its session's id, grouped by session in the order
    they were queued."""
    sessions: dict[str, list[EventEnvelope]] = {}
    for session_id, envelope in queued:
        sessions.setdefault(session_id, []).append(envelope)
    return sessions


def _with_pending(
    bounds: LogBounds, session_id: str, pending: Sequence[tuple[str, EventEnvelope]]
) -> LogBounds:
    """``bounds`` of the log of the session ``session_id`` moved to take in the envelopes
    ``pending``, each with its session's id, as though they were logged."""
    own = [envelope.seq for session, envelope in pending if session == session_id]
    others = [envelope.seq for session, envelope in pending if session != session_id]
    first = [] if bounds.first_seq is None else [bounds.first_seq]
    return LogBounds(
        first_seq=min([*first, *own], default=None),
        others_seq=max([bounds.others_seq, *others]),
    )


class LogWriter:
    """Writes the envelopes added here to ``log`` in a task of its own, in the order they were
    added: whatever has queued since its last write goes in one append per session. A failed
    append is reported with ``publish`` once, until one works again, and later envelopes are
    still written. Adding never waits for the log."""

    def __init__(self, log: EventLog, publish: Publish) -> None:
        self._log = log
        self._appends = ReportedOnce(publish)
        self._queued: list[tuple[str, EventEnvelope]] = []
        self._writing: list[tuple[str, EventEnvelope]] = []  # Taken from the queue to write.
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

    async def recent(self, session_id: str, limit: int) -> list[EventEnvelope]:
        """The last ``limit`` events of the session ``session_id``, oldest first: those in the
        log, and after them those still to be written, each once."""
        # Read on both sides of the log's read: an event written meanwhile is in the log, or
        # was still to be written before it, and one added meanwhile is still to be written.
        before = self._unwritten(session_id)
        logged = await self._log.recent(session_id, limit)
        return _latest(_in_order([*logged, *before, *self._unwritten(session_id)]), limit)

    async def before(
        self, session_id: str, before_seq: int, limit: int
    ) -> tuple[list[EventEnvelope], bool]:
        """The last ``limit`` events of the session ``session_id`` before ``before_seq``, oldest
        first, logged or still to be written, each once, and whether earlier ones remain."""
        # Read on both sides of the log's read, as recent does.
        pending = self._unwritten(session_id)
        logged, more = await self._log.before(session_id, before_seq, limit)
        found = _in_order([*logged, *pending, *self._unwritten(session_id)])
        earlier = [envelope for envelope in found if envelope.seq < before_seq]
        return _latest(earlier, limit), more or len(earlier) > limit

    async def after(self, session_id: str, after_seq: int) -> list[EventEnvelope] | None:
        """The events of the session ``session_id`` after ``after_seq``, oldest first, logged or
        still to be written, each once; ``None`` when the log can't be read."""
        # Read on both sides of the log's read, as recent does.
        pending = self._unwritten(session_id)
        logged = await self._log.after(session_id, after_seq)
        if logged is None:
            return None
        found = _in_order([*logged, *pending, *self._unwritten(session_id)])
        return [envelope for envelope in found if envelope.seq > after_seq]

    async def bounds(self, session_id: str) -> LogBounds | None:
        """Where the log of the session ``session_id`` stands in its workspace's, counting the
        events still to be written as logged; ``None`` when the log can't be read."""
        # Read on both sides of the log's read, as recent does.
        pending = [*self._writing, *self._queued]
        logged = await self._log.bounds(session_id)
        if logged is None:
            return None
        return _with_pending(logged, session_id, [*pending, *self._writing, *self._queued])

    def _unwritten(self, session_id: str) -> list[EventEnvelope]:
        """The envelopes of the session ``session_id`` being written or queued, in order."""
        pending = [*self._writing, *self._queued]
        return [envelope for session, envelope in pending if session == session_id]

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
            self._writing, self._queued = self._queued, []
            for session_id, envelopes in _by_session(self._writing).items():
                await self._appends.attempt(self._log.append(session_id, envelopes))
            self._writing = []
