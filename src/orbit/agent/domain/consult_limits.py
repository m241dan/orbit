"""The hourly limit on a role's consultations: those it asked in a rolling hour count against its
``consult_limit_per_hour``, and past it each needs the user's approval, unless they let it consult
freely for an hour; one held while no frontend is connected for an hour expires, denied. Times are
seconds on the caller's clock."""

from dataclasses import dataclass, replace

HOUR = 3600.0


@dataclass(frozen=True)
class ConsultCount:
    """A role's consultations: when it ``asked`` each counted, oldest first, and until when it
    consults ``free_until`` the user's approval."""

    asked: tuple[float, ...] = ()
    free_until: float = float("-inf")

    def recent(self, now: float) -> int:
        """How many consultations it asked in the hour before ``now``."""
        return sum(1 for time in self.asked if now - time < HOUR)

    def needs_approval(self, limit: int, now: float) -> bool:
        """Whether a consultation asked at ``now`` needs the user's approval: it's past
        ``limit``, which 0 lifts, and the role isn't consulting freely."""
        return limit > 0 and now >= self.free_until and self.recent(now) >= limit

    def counted(self, now: float) -> ConsultCount:
        """The count with a consultation asked at ``now``, those older than an hour dropped."""
        kept = tuple(time for time in self.asked if now - time < HOUR)
        return replace(self, asked=(*kept, now))

    def freed(self, now: float) -> ConsultCount:
        """The count with the role consulting freely for an hour from ``now``."""
        return replace(self, free_until=now + HOUR)


def expires_at(held: float, alone_since: float | None) -> float | None:
    """When a consultation held at ``held`` expires, no frontend having been connected since
    ``alone_since``; none while one is."""
    if alone_since is None:
        return None
    return max(held, alone_since) + HOUR
