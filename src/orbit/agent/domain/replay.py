"""When a reconnecting frontend can catch up on the events it missed, rather than starting over
from a Snapshot."""

from dataclasses import dataclass


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
