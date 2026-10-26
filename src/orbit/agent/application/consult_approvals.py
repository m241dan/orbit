"""The consultations held past their asker's hourly limit, each until the user decides on it or,
after an hour with no frontend connected, it expires."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from orbit.agent.application.planner_seat import Timer
from orbit.agent.domain.consult_limits import expires_at
from orbit.protocol import ApprovalRequested, Event


@dataclass(frozen=True)
class Held[T]:
    """A consultation, ``held`` at that time, that ``request`` asks the user about, with what
    starts it once allowed (``work``)."""

    request: ApprovalRequested
    held: float
    work: T


class Approvals[T]:
    """The consultations held for approval, published through ``post`` as they're held; timed by
    ``timer``, each still held after an hour with no frontend connected is passed
    to ``expire``. Until told otherwise, no frontend is connected."""

    def __init__(
        self,
        post: Callable[[Event], None],
        expire: Callable[[Held[T]], Awaitable[None]],
        timer: Timer,
    ) -> None:
        self._post = post
        self._expire = expire
        self._clock = timer.clock
        self._sleep = timer.sleep
        self._held: dict[int, Held[T]] = {}  # By consultation.
        self._alone_since: float | None = float("-inf")  # None while a frontend is connected.
        self._timer: asyncio.Task[None] | None = None
        self._timing = 0  # Bumped by each stop, ending any expiry in flight.

    @property
    def waiting(self) -> tuple[ApprovalRequested, ...]:
        """What the user is asked about the consultations held, in the order they were held."""
        return tuple(held.request for held in self._held.values())

    def hold(self, request: ApprovalRequested, work: T) -> None:
        """Hold the consultation ``request`` asks about, with its ``work``, and publish
        ``request``."""
        self._held[request.consultation] = Held(request, self._clock(), work)
        self._post(request)
        self._arm()

    def take(self, consultation: int) -> Held[T] | None:
        """Stop holding consultation ``consultation``, returning it; none if it isn't held."""
        held = self._held.pop(consultation, None)
        self._arm()
        return held

    def viewers(self, count: int) -> None:
        """Note that ``count`` frontends are connected now."""
        if count:
            self._alone_since = None
        elif self._alone_since is None:
            self._alone_since = self._clock()
        self._arm()

    def clear(self) -> None:
        """Forget the consultations held, as the session holding them is left."""
        self._held = {}
        self.stop()

    def stop(self) -> None:
        """Stop timing the consultations held, until one is next held or frontends come or go."""
        self._timing += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _arm(self) -> None:
        """Time the next consultation held to expire, if any will."""
        self.stop()
        due = [expires_at(held.held, self._alone_since) for held in self._held.values()]
        times = [time for time in due if time is not None]
        if times:
            self._timer = asyncio.get_running_loop().create_task(self._expiring(min(times)))

    async def _expiring(self, due: float) -> None:
        """Expire the consultations due by ``due``, once it comes, one at a time, then time the
        next; a stop while one expires ends it there, leaving the rest held."""
        await self._sleep(max(0.0, due - self._clock()))
        self._timer = None
        timing = self._timing
        try:
            while timing == self._timing and (held := self._next_due(self._clock())):
                await self._expire(held)
        finally:
            if timing == self._timing:
                self._arm()

    def _next_due(self, now: float) -> Held[T] | None:
        """Stop holding the first consultation that expires by ``now``, returning it; none if
        none does."""
        due = [number for number, held in self._held.items() if self._expires(held, now)]
        return self._held.pop(due[0]) if due else None

    def _expires(self, held: Held[T], now: float) -> bool:
        """Whether ``held`` expires by ``now``."""
        ends = expires_at(held.held, self._alone_since)
        return ends is not None and ends <= now
