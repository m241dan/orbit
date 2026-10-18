"""When a reconnecting frontend can catch up on the events it missed, rather than starting over
from a Snapshot."""

from dataclasses import dataclass


@dataclass(frozen=True)
class LogBounds:
    """Where a session's log stands in its workspace's: the ``first_seq`` of its oldest event
    kept, if any is, and the highest ``seq`` of any other session's, ``others_seq``, 0 if none."""

    first_seq: int | None
    others_seq: int


def can_replay(since: int, last_seq: int, bounds: LogBounds) -> bool:
    """Whether a frontend that saw the events up to ``since`` catches up on replaying the open
    session's after it: none of another session's came after it, given the ``bounds`` of the
    open session's log, it's no older than that session's oldest event kept, and it's no newer
    than ``last_seq``, the last event stamped."""
    if since > last_seq or bounds.others_seq > since:
        return False
    return bounds.first_seq is None or since >= bounds.first_seq - 1
