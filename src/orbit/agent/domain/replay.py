"""When a reconnecting frontend can catch up on the events it missed, rather than starting over
from a Snapshot."""

from collections.abc import Sequence
from dataclasses import dataclass

from orbit.protocol import EventEnvelope


@dataclass(frozen=True)
class LogBounds:
    """Where a session's log stands in its workspace's, for a frontend that saw the events up to
    a ``seq``: the ``first_seq`` of its oldest event kept, if any is, the highest ``seq`` of any
    other session's, ``others_seq``, 0 if none, and the highest of its own at or before the
    frontend's, ``own_seq``, if any is."""

    first_seq: int | None
    others_seq: int
    own_seq: int | None


def can_replay(since: int, last_seq: int, bounds: LogBounds) -> bool:
    """Whether a frontend that saw the events up to ``since`` catches up on replaying the open
    session's after it, given the ``bounds`` of the open session's log: the last it saw of any
    session's was the open session's, none of another session's came after it, it's no older
    than that session's oldest event kept, and it's no newer than ``last_seq``, the last event
    stamped."""
    if since > last_seq or bounds.others_seq > since:
        return False
    if bounds.own_seq is None or bounds.own_seq <= bounds.others_seq:
        return False
    return bounds.first_seq is None or since >= bounds.first_seq - 1


def pending_bounds(
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


def in_order(envelopes: Sequence[EventEnvelope]) -> list[EventEnvelope]:
    """``envelopes`` by ``seq``, oldest first, each ``seq`` once."""
    by_seq = {envelope.seq: envelope for envelope in envelopes}
    return [by_seq[seq] for seq in sorted(by_seq)]


def latest(envelopes: Sequence[EventEnvelope], limit: int) -> list[EventEnvelope]:
    """The last ``limit`` of ``envelopes``, which are in order."""
    return list(envelopes[max(0, len(envelopes) - limit) :])
