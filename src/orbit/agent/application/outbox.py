"""Where the session's events are published, each stamped as it's published."""

from collections.abc import Callable
from typing import Protocol

from orbit.agent.application.log_writer import LogWriter, Overflow
from orbit.agent.application.ports import EventLog
from orbit.agent.domain.replay import can_replay
from orbit.agent.domain.storage import StorageSettings, cut_fields
from orbit.agent.domain.storage_halt import SeqSpan
from orbit.protocol import (
    CONNECTION_EVENTS,
    CONTROL_PLANE_EVENTS,
    SESSIONLESS_EVENTS,
    Event,
    EventEnvelope,
)

TRANSCRIPT_LENGTH = 500
"""How many of a session's latest events its transcript holds."""


_WHOLE = StorageSettings(tool_output_kb=0, session_log_mb=0)
"""Storage settings that cut nothing and trim nothing."""

_NO_OVERFLOW = Overflow()


def _drop(envelope: EventEnvelope) -> None:
    """Deliver ``envelope`` nowhere."""


class EventSink(Protocol):
    """What the session's parts publish their events into."""

    async def put(self, item: Event) -> None: ...

    def put_nowait(self, item: Event) -> None: ...


class Seqs:
    """The ``seq`` events are stamped with, counted on from ``last``: one count for every
    workspace's events, so no two of them share a ``seq``."""

    def __init__(self, last: int = 0) -> None:
        self.last = last

    def next(self) -> int:
        """The next ``seq``, counted as stamped."""
        self.last += 1
        return self.last

    def catch_up(self, latest: int) -> None:
        """Count on from ``latest``, if it's higher; the count never goes down."""
        self.last = max(self.last, latest)


class Outbox:
    """Events published here, each handed to :attr:`deliver` (which drops it until set) in an
    envelope stamped when it was put in: the next of the :attr:`seqs`, counted on from the
    highest in ``log`` (if there is one) else from one, and shared with the outboxes of other
    workspaces once they're given the same, the session ``open_session`` names then, unless
    the event belongs to none, and the :attr:`workspace`, unless it belongs to the control plane.

    With a ``log``, each envelope of a session is also written to it in the background, but for
    the events answering one connection; :meth:`catch_up` counts on from its highest ``seq``
    now, :meth:`transcript` reads a session's latest back, :meth:`history` those before them,
    :meth:`missed` those after a ``seq``, :meth:`forget` deletes a session's after those
    queued, and :meth:`close` writes those still queued. The copies written have their text
    fields cut and each session's log is trimmed as the ``storage`` settings say; those
    delivered are whole. A failed append is told to the
    ``overflow``'s ``failed``; from then on, or once :meth:`stop_log` is called, the envelopes
    go to its fallback instead (nowhere without one), until :meth:`resume_log` works."""

    def __init__(
        self,
        open_session: Callable[[], str],
        log: EventLog | None = None,
        storage: StorageSettings = _WHOLE,
        overflow: Overflow = _NO_OVERFLOW,
    ) -> None:
        self._open = open_session
        # Stamped on every event but the control plane's own, once set.
        self.workspace: str | None = None
        self._cut_bytes = storage.tool_output_bytes
        # The last event's place in the stream, counted across sessions and workspaces.
        self.seqs = Seqs(log.max_seq() if log else 0)
        self._event_log = log
        self.deliver: Callable[[EventEnvelope], None] = _drop
        log_bytes = storage.session_log_bytes
        self._writer = LogWriter(log, self.put, log_bytes, overflow) if log else None

    def put_nowait(self, item: Event) -> None:
        """Stamp ``item`` and deliver it."""
        self.deliver(self.stamp(item))

    async def put(self, item: Event) -> None:
        """Stamp ``item`` and deliver it."""
        self.put_nowait(item)

    def stamp(
        self, item: Event, viewed: str | None = None, *, logged: bool = True
    ) -> EventEnvelope:
        """``item`` in its envelope, stamped now, and logged as if published, unless not
        ``logged``, but not delivered; with ``viewed``, it's stamped as that session's, a
        session that isn't open, and never logged, as it answers a connection viewing it."""
        seq = self.seqs.next()
        workspace, session = self._place(item, viewed)
        envelope = EventEnvelope(seq=seq, workspace=workspace, session=session, payload=item)
        if session is not None and viewed is None and logged:
            self._log(session, envelope)
        return envelope

    def _place(self, item: Event, viewed: str | None) -> tuple[str | None, str | None]:
        """The workspace and session ``item`` is stamped as: the session ``viewed``, if given,
        else the open one, unless it belongs to none."""
        session = None if isinstance(item, SESSIONLESS_EVENTS) else viewed or self._open()
        workspace = None if isinstance(item, CONTROL_PLANE_EVENTS) else self.workspace
        return workspace, session

    @property
    def last_seq(self) -> int:
        """The ``seq`` of the last event stamped; 0 before any is."""
        return self.seqs.last

    async def catch_up(self) -> None:
        """Count on from the highest ``seq`` in the log now, if there is one and it's higher, as
        another orbit may have logged events since this one opened; the count never goes
        down."""
        latest = await self._event_log.latest_seq() if self._event_log else None
        self.seqs.catch_up(latest or 0)

    async def transcript(self, session_id: str) -> tuple[EventEnvelope, ...]:
        """The last :data:`TRANSCRIPT_LENGTH` events of the session ``session_id``, oldest
        first, logged or still to be; none without a log."""
        if self._writer is None:
            return ()
        return tuple(await self._writer.recent(session_id, TRANSCRIPT_LENGTH))

    async def history(
        self, session_id: str, before_seq: int, limit: int
    ) -> tuple[tuple[EventEnvelope, ...], bool, bool]:
        """The last ``limit`` events of the session ``session_id`` before ``before_seq``, oldest
        first, logged or still to be, whether earlier ones remain, and, when none do, whether
        earlier ones were trimmed from the log; none without a log. Raises ``RuntimeError`` when
        the log can't be read."""
        if self._writer is None:
            return (), False, False
        earlier, more = await self._writer.before(session_id, before_seq, limit)
        trimmed = not more and await self._writer.trimmed(session_id)
        return tuple(earlier), more, trimmed

    async def missed(self, session_id: str, since: int) -> tuple[EventEnvelope, ...] | None:
        """The events of the session ``session_id`` after ``since`` and up to the last stamped,
        oldest first, logged or still to be, when a frontend that saw those up to ``since``
        catches up on them alone; ``None`` when it can't, or the log can't be read, or there's
        no log."""
        last = self.seqs.last  # Those stamped from now on reach the frontend anyway.
        if self._writer is None or not await self._replayable(session_id, since, last):
            return None
        events = await self._writer.after(session_id, since)
        return None if events is None else tuple(e for e in events if e.seq <= last)

    async def _replayable(self, session_id: str, since: int, last: int) -> bool:
        """Whether a frontend that saw the events up to ``since``, with ``last`` the last one
        stamped, catches up on those of the session ``session_id`` after it alone."""
        assert self._writer is not None, "There's no log."
        bounds = await self._writer.bounds(session_id, since)
        return bounds is not None and can_replay(since, last, bounds)

    def forget(self, session_id: str) -> None:
        """Have the events of the session ``session_id`` deleted from the log, if there is one,
        once those queued for it are written."""
        if self._writer is not None:
            self._writer.forget(session_id)

    async def stop_log(self, session_id: str) -> tuple[list[EventEnvelope], str | None]:
        """Stop writing to the log, sending the envelopes to the fallback instead; returns
        those sent there so far, by ``seq``, and the fallback file of the session
        ``session_id``, or ``None`` when it couldn't be written or there's no log."""
        if self._writer is None:
            return [], None
        return await self._writer.stop(session_id)

    async def resume_log(self) -> list[SeqSpan] | RuntimeError:
        """Write to the log again, once stopped, starting with the envelopes the fallback took
        and those queued; returns the spans of the envelopes lost, which reached neither, or the
        error when the log still fails. Nothing is lost without a log."""
        if self._writer is None:
            return []
        return await self._writer.resume()

    async def close(self) -> None:
        """Write the envelopes still queued for the log, if there is one, and stop writing."""
        if self._writer is not None:
            await self._writer.close()

    def _log(self, session_id: str, envelope: EventEnvelope) -> None:
        """Have ``envelope``, of the session ``session_id``, written to the log, if there is one
        and it's kept there, with its long text fields cut."""
        if self._writer is not None and not isinstance(envelope.payload, CONNECTION_EVENTS):
            payload = cut_fields(envelope.payload, self._cut_bytes)
            self._writer.add(session_id, envelope.model_copy(update={"payload": payload}))
