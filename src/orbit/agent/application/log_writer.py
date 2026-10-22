"""The writer that keeps sessions' events in their log, behind their delivery."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from orbit.agent.application.failures import ReportedOnce, outcome
from orbit.agent.application.ports import EventLog, Fallback, Publish, StoreFailed
from orbit.agent.domain.replay import LogBounds
from orbit.agent.domain.storage import trim_target
from orbit.agent.domain.storage_halt import SeqSpan, lost_spans
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


Queued = list[tuple[str, EventEnvelope]]
"""Envelopes, each with its session's id."""


def _without(queued: Queued, session_id: str) -> Queued:
    """``queued`` but for the envelopes of the session ``session_id``."""
    return [(session, envelope) for session, envelope in queued if session != session_id]


Failed = Callable[[StoreFailed], None]
"""Told that an append failed with the error given."""


def _ignored(error: StoreFailed) -> None:
    pass


@dataclass(frozen=True)
class Overflow:
    """Where envelopes go once the log can't take them: the ``fallback``, if any; ``failed``
    is told of the append that failed."""

    fallback: Fallback | None = None
    failed: Failed = _ignored


_NO_OVERFLOW = Overflow()


def _with_pending(
    bounds: LogBounds,
    session_id: str,
    since: int,
    pending: Sequence[tuple[str, EventEnvelope]],
) -> LogBounds:
    """``bounds`` of the log of the session ``session_id``, for a frontend that saw the events up
    to ``since``, moved to take in the envelopes ``pending``, each with its session's id, as
    though they were logged."""
    own = [envelope.seq for session, envelope in pending if session == session_id]
    others = [envelope.seq for session, envelope in pending if session != session_id]
    return LogBounds(
        first_seq=min([*_listed(bounds.first_seq), *own], default=None),
        others_seq=max([bounds.others_seq, *others]),
        own_seq=max([*_listed(bounds.own_seq), *_up_to(own, since)], default=None),
    )


def _listed(seq: int | None) -> list[int]:
    """``seq`` alone, or nothing when it's ``None``."""
    return [] if seq is None else [seq]


def _up_to(seqs: Sequence[int], since: int) -> list[int]:
    """Those of ``seqs`` at or before ``since``."""
    return [seq for seq in seqs if seq <= since]


class LogWriter:
    """Writes the envelopes added here to ``log`` in a task of its own, in the order they were
    added: whatever has queued since its last write goes in one append per session. After each
    append, unless ``log_bytes`` is 0, a session's log over it is trimmed to under
    :func:`trim_target` of it; a failed trim is reported with ``publish`` once, until one works
    again. :meth:`forget` has a session's events deleted from the log once those added before it
    are written, a failed one reported so too, and :meth:`reserve` has the log reserve the
    ``seq`` up to one once they're written. Adding never waits for the log.

    A failed append or reservation, or :meth:`stop`, stops the appending and the reserving: the
    envelopes not appended, and every one added from then on, go to the ``overflow``'s fallback
    instead (nowhere without one), and a failed append or reservation is told to its ``failed``.
    Those envelopes are still read back as still to be written, until :meth:`resume` appends them
    and appending carries on. A session forgotten meanwhile has its envelopes not in the log
    dropped instead, and its events deleted from the log then, or, when that fails, once it
    resumes. The ``seq`` asked to be reserved meanwhile are reserved once it resumes."""

    def __init__(
        self, log: EventLog, publish: Publish, log_bytes: int = 0, overflow: Overflow = _NO_OVERFLOW
    ) -> None:
        self._log = log
        self._log_bytes = log_bytes
        self._fallback = overflow.fallback
        self._failed = overflow.failed
        self._trims = ReportedOnce(publish)
        self._forgets = ReportedOnce(publish)
        self._queued: Queued = []
        self._forgetting: list[str] = []  # The sessions whose events to delete, once written.
        self._held: list[str] = []  # Those whose delete failed while stopped, to retry resumed.
        self._writing: Queued = []  # Taken from the queue to write.
        self._diverted: Queued = []  # Sent to the fallback, as the log can't take them.
        self._places: dict[str, str | None] = {}  # Each session's fallback file, if written.
        self._reserving = 0  # The highest seq to reserve in the log.
        self._reserved = 0  # The highest seq the log reserved.
        self._parked: set[int] = set()  # The seqs written to a fallback file.
        self._lost: set[int] = set()  # The seqs no fallback file could take.
        self._diverting = asyncio.Lock()  # One fallback write at a time, so lines stay whole.
        self._appending = asyncio.Lock()  # Held through each batch's appends, for stop to await.
        self._stopped = False  # An append failed, or the writer was stopped: nothing appends.
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
        self._wake()

    def forget(self, session_id: str) -> None:
        """Have the events of the session ``session_id`` deleted from the log once those added
        so far are written; once the writer is closed, nothing more is deleted."""
        if self._closed:
            return
        self._forgetting.append(session_id)
        self._wake()

    def reserve(self, seq: int) -> None:
        """Have the log reserve every ``seq`` up to ``seq`` once what's added so far is written,
        starting the writer if it isn't yet; once the writer is closed, nothing more is
        reserved."""
        if self._closed:
            return
        self._reserving = max(self._reserving, seq)
        self._wake()

    def _wake(self) -> None:
        """Have the writer write what's queued, starting it if it isn't yet."""
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
        first, logged or still to be written, each once, and whether earlier ones remain. Raises
        :class:`ReadFailed` when the log can't be read."""
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

    async def bounds(self, session_id: str, since: int) -> LogBounds | None:
        """Where the log of the session ``session_id`` stands in its workspace's, for a frontend
        that saw the events up to ``since``, counting the events still to be written as logged;
        ``None`` when the log can't be read."""
        # Read on both sides of the log's read, as recent does.
        pending = self._pending()
        logged = await self._log.bounds(session_id, since)
        if logged is None:
            return None
        return _with_pending(logged, session_id, since, [*pending, *self._pending()])

    async def trimmed(self, session_id: str) -> bool:
        """Whether events of the session ``session_id`` were trimmed from the log; ``False``
        when that can't be read."""
        return await self._log.trimmed(session_id)

    def _unwritten(self, session_id: str) -> list[EventEnvelope]:
        """The envelopes of the session ``session_id`` not in the log: being written, queued
        or sent to the fallback."""
        return [envelope for session, envelope in self._pending() if session == session_id]

    def _pending(self) -> Queued:
        """The envelopes not in the log, each with its session's id: those sent to the
        fallback, being written and queued."""
        return [*self._diverted, *self._writing, *self._queued]

    async def stop(self, session_id: str) -> tuple[list[EventEnvelope], str | None]:
        """Stop appending to the log: once the append in progress is done, the envelopes not
        appended, and every one added from now on, go to the fallback instead. Returns every
        envelope sent there so far, by ``seq``, and the fallback file of the session
        ``session_id``, made now if need be; ``None`` when it couldn't be written."""
        self._stopped = True
        async with self._appending:
            queued, self._queued = self._queued, []
            await self._divert(queued)
        if session_id not in self._places:
            async with self._diverting:
                self._places[session_id] = await self._to_fallback(session_id, [])
        return _in_order([envelope for _, envelope in self._diverted]), self._places[session_id]

    async def resume(self) -> list[SeqSpan] | StoreFailed:
        """Append to the log again, once stopped: the envelopes the fallback took, then those
        queued, in ``seq`` order, one append per session. Once that works, the fallback files are
        removed and appending carries on; returns the spans of the envelopes no fallback took,
        which are lost, and the sessions forgotten meanwhile have their events deleted after.
        Returns the error when an append fails, and stays stopped, as it does when cancelled."""
        if not self._stopped:
            return []
        async with self._diverting:
            queued, self._queued = self._queued, []
            parked = [(s, e) for s, e in self._diverted if e.seq in self._parked]
            error = await self._append_taken(parked, queued)
            if error is not None:
                return error
            seqs = [envelope.seq for _, envelope in [*self._diverted, *queued]]
            spans = lost_spans(self._lost, seqs)
            await self._settle()
        return spans

    def _release(self) -> None:
        """Have the writer delete the events of the sessions forgotten while stopped."""
        held, self._held = self._held, []
        if held:
            self._forgetting[:0] = held
            self._wake()

    async def _append_taken(self, parked: Queued, queued: Queued) -> StoreFailed | None:
        """Append ``parked`` and ``queued``, taken from the queue, as :meth:`_append_whole`
        does; when that fails or is cancelled, ``queued`` are queued again, so that, still
        stopped, the writer sends them to the fallback."""
        try:
            error = await self._append_whole([*parked, *queued])
        except BaseException:
            self._put_back(queued)
            raise
        if error is not None:
            self._put_back(queued)
        return error

    def _put_back(self, queued: Queued) -> None:
        """Queue ``queued`` again, ahead of what's queued since."""
        self._queued[:0] = queued
        self._ready.set()

    async def _append_whole(self, queued: Queued) -> StoreFailed | None:
        """Append ``queued`` to the log by ``seq``, one append per session; returns the error
        of the first that fails."""
        for session_id, envelopes in _by_session(queued).items():
            error = await outcome(self._log.append(session_id, _in_order(envelopes)))
            if isinstance(error, StoreFailed):
                return error
        return None

    async def _settle(self) -> None:
        """Forget the envelopes the fallback took or lost, removing its files, and append
        again."""
        if self._fallback is not None:
            for session_id in self._places:
                await self._fallback.remove(session_id)
        settled = self._parked | self._lost
        self._diverted = [(s, e) for s, e in self._diverted if e.seq not in settled]
        self._places, self._parked, self._lost = {}, set(), set()
        self._stopped = False
        self._release()
        self._ready.set()  # For the seqs reserved meanwhile.

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
        while not self._closing or self._queued or self._forgetting or self._unreserved:
            await self._ready.wait()
            self._ready.clear()
            await self._write()

    @property
    def _unreserved(self) -> bool:
        """Whether a ``seq`` is still to be reserved, and can be, the writer not stopped."""
        return self._reserving > self._reserved and not self._stopped

    async def _write(self) -> None:
        """Write what's queued, then delete the events of the sessions to forget, whose events
        added before that are written by then, and reserve the ``seq`` asked to be."""
        self._writing, self._queued = self._queued, []
        forgetting, self._forgetting = self._forgetting, []
        if self._stopped:
            await self._divert(self._writing)
        else:
            async with self._appending:
                await self._append(self._writing)
        self._writing = []
        for session_id in forgetting:
            await self._forget(session_id)
        await self._reserve()

    async def _reserve(self) -> None:
        """Have the log reserve the ``seq`` asked to be, if it can be; a failed reservation stops
        the appending, as a failed append does, and is told to ``failed``."""
        if not self._unreserved:
            return
        seq = self._reserving
        error = await outcome(self._log.reserve(seq))
        if isinstance(error, StoreFailed):
            self._stopped = True
            self._failed(error)
            return
        self._reserved = max(self._reserved, seq)

    async def _forget(self, session_id: str) -> None:
        """Delete the events of the session ``session_id`` from the log; once stopped, drop
        those not in it too, and when the delete fails, unreported, try it again once
        resumed."""
        async with self._diverting:
            if self._stopped:
                await self._drop(session_id)
                # The halt that stopped the writer already reported the storage problem.
                if isinstance(await outcome(self._log.forget(session_id)), StoreFailed):
                    self._held.append(session_id)
                return
        await self._forgets.attempt(self._log.forget(session_id))

    async def _drop(self, session_id: str) -> None:
        """Drop the envelopes of the session ``session_id`` that aren't in the log, sent to the
        fallback or queued, and remove its fallback file."""
        dropped = {envelope.seq for envelope in self._unwritten(session_id)}
        self._diverted = _without(self._diverted, session_id)
        self._queued = _without(self._queued, session_id)
        self._parked -= dropped
        self._lost -= dropped
        self._places.pop(session_id, None)
        if self._fallback is not None:
            await self._fallback.remove(session_id)

    async def _append(self, queued: Queued) -> None:
        """Append ``queued`` to the log, one append per session; on a failed append, or once
        stopped, stop appending and send what wasn't appended to the fallback."""
        sessions = list(_by_session(queued).items())
        for done, (session_id, envelopes) in enumerate(sessions):
            if self._stopped or not await self._appended(session_id, envelopes):
                await self._divert([(s, e) for s, rest in sessions[done:] for e in rest])
                return

    async def _appended(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> bool:
        """Whether ``envelopes`` of the session ``session_id`` were appended, its log trimmed
        after if it's over the limit and the writer still running; a failed append stops the
        appending and is told to ``failed``."""
        error = await outcome(self._log.append(session_id, envelopes))
        if isinstance(error, StoreFailed):
            self._stopped = True
            self._failed(error)
            return False
        if self._log_bytes and not self._stopped:
            limit = self._log_bytes
            await self._trims.attempt(self._log.trim(session_id, limit, trim_target(limit)))
        return True

    async def _divert(self, queued: Queued) -> None:
        """Send ``queued`` to the fallback, each session's to its own file, noting which it
        took; queue them to append instead if the writer resumed meanwhile."""
        self._diverted += queued
        async with self._diverting:
            if not self._stopped:
                self._requeue(queued)
                return
            for session_id, envelopes in _by_session(queued).items():
                place = await self._to_fallback(session_id, envelopes)
                self._places[session_id] = place
                taken = self._lost if place is None else self._parked
                taken.update(envelope.seq for envelope in envelopes)

    def _requeue(self, queued: Queued) -> None:
        """Queue ``queued``, on their way to the fallback, to be appended after all."""
        seqs = {envelope.seq for _, envelope in queued}
        self._diverted = [(s, e) for s, e in self._diverted if e.seq not in seqs]
        self._queued[:0] = queued
        self._ready.set()

    async def _to_fallback(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> str | None:
        """Add ``envelopes`` to the fallback file of the session ``session_id``; returns its
        path, or ``None`` when it can't be written, or there's no fallback."""
        if self._fallback is None:
            return None
        return await self._fallback.write(session_id, envelopes)
