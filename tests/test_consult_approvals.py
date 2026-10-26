"""The consultations held for approval: published as they're held, taken once, and expired,
denied, after an hour with no frontend connected."""

import asyncio

from orbit.agent.application.consult_approvals import Approvals, Held
from orbit.agent.application.planner_seat import Timer
from orbit.agent.domain.consult_limits import HOUR
from orbit.protocol import PLANNER, ApprovalRequested, Event
from tests.waiting import eventually


def request(number: int) -> ApprovalRequested:
    return ApprovalRequested(
        consultation=number, asker=PLANNER, consultant="experimenter", title="Check X", asked=6
    )


class Clock:
    """A clock that sleeping moves on, at once."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class Holding:
    """Approvals over a :class:`Clock`, with what they posted and expired."""

    def __init__(self) -> None:
        self.clock = Clock()
        self.posted: list[Event] = []
        self.expired: list[Held[str]] = []
        self.approvals = Approvals[str](
            self.posted.append, self._expire, Timer(self.clock, self.clock.sleep)
        )

    async def _expire(self, held: Held[str]) -> None:
        self.expired.append(held)


async def test_a_consultation_held_is_published_and_waits() -> None:
    held = Holding()
    held.approvals.viewers(1)
    held.approvals.hold(request(1), "work")
    assert held.posted == [request(1)]
    assert held.approvals.waiting == (request(1),)


async def test_a_consultation_is_taken_once() -> None:
    held = Holding()
    held.approvals.viewers(1)
    held.approvals.hold(request(1), "work")
    taken = held.approvals.take(1)
    assert taken is not None and (taken.request, taken.work) == (request(1), "work")
    assert held.approvals.take(1) is None
    assert held.approvals.waiting == ()


async def test_one_held_with_no_frontend_ever_connected_expires_after_an_hour() -> None:
    held = Holding()
    held.clock.now = 50
    held.approvals.hold(request(1), "work")
    await eventually(lambda: held.expired, poll=0)
    assert held.clock.slept == [HOUR]
    assert [h.request for h in held.expired] == [request(1)]
    assert held.approvals.waiting == ()


async def test_one_held_doesnt_expire_while_a_frontend_is_connected() -> None:
    held = Holding()
    held.approvals.viewers(2)
    held.approvals.hold(request(1), "work")
    await asyncio.sleep(0)
    assert (held.clock.slept, held.expired) == ([], [])


async def test_one_held_expires_an_hour_after_the_last_frontend_leaves() -> None:
    held = Holding()
    held.approvals.viewers(1)
    held.approvals.hold(request(1), "work")
    held.clock.now = 100
    held.approvals.viewers(0)
    await eventually(lambda: held.expired, poll=0)
    assert held.clock.now == 100 + HOUR


async def test_a_frontend_connecting_stops_the_hour_running() -> None:
    held = Holding()
    held.approvals.hold(request(1), "work")
    held.approvals.viewers(1)
    await asyncio.sleep(0)
    assert held.expired == []
    assert held.approvals.waiting == (request(1),)


async def test_frontends_leaving_again_dont_restart_the_hour() -> None:
    held = Holding()
    held.approvals.viewers(0)
    held.clock.now = 100
    held.approvals.viewers(0)
    held.approvals.hold(request(1), "work")
    held.approvals.stop()
    assert held.approvals.waiting == (request(1),)


async def test_those_held_expire_in_turn_each_an_hour_after_its_hold() -> None:
    held = Holding()
    held.approvals.viewers(1)
    held.approvals.hold(request(1), "first")
    held.clock.now = 600
    held.approvals.hold(request(2), "second")
    held.clock.now = 0
    held.approvals.viewers(0)
    await eventually(lambda: len(held.expired) == 2, poll=0)
    assert [h.work for h in held.expired] == ["first", "second"]
    assert held.clock.slept == [HOUR, 600]


async def test_clearing_forgets_those_held_and_stops_their_hour() -> None:
    held = Holding()
    held.approvals.hold(request(1), "work")
    held.approvals.clear()
    await asyncio.sleep(0)
    assert (held.approvals.waiting, held.expired) == ((), [])


class Gated(Holding):
    """Holding whose first expiry waits on ``gate`` once it has ``started``."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def _expire(self, held: Held[str]) -> None:
        self.expired.append(held)
        self.started.set()
        await self.gate.wait()


class Failing(Holding):
    """Holding whose first expiry fails."""

    async def _expire(self, held: Held[str]) -> None:
        self.expired.append(held)
        if len(self.expired) == 1:
            raise RuntimeError("save failed")


def two_due_together(held: Holding) -> None:
    held.approvals.viewers(1)
    held.approvals.hold(request(1), "first")
    held.approvals.hold(request(2), "second")
    held.approvals.viewers(0)


async def test_stopping_mid_expiry_expires_no_more_and_arms_nothing() -> None:
    held = Gated()
    two_due_together(held)
    await held.started.wait()
    held.approvals.stop()
    held.gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert [h.work for h in held.expired] == ["first"]
    assert held.approvals.waiting == (request(2),)
    assert held.clock.slept == [HOUR]


async def test_clearing_mid_expiry_expires_no_more() -> None:
    held = Gated()
    two_due_together(held)
    await held.started.wait()
    held.approvals.clear()
    held.gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert [h.work for h in held.expired] == ["first"]
    assert (held.approvals.waiting, held.clock.slept) == ((), [HOUR])


async def test_those_due_together_expire_and_the_next_is_timed() -> None:
    held = Holding()
    two_due_together(held)
    held.clock.now = 600
    held.approvals.hold(request(3), "third")
    held.clock.now = 0
    await eventually(lambda: len(held.expired) == 3, poll=0)
    assert [h.work for h in held.expired] == ["first", "second", "third"]
    assert held.clock.slept == [HOUR, 600]


async def test_a_failed_expiry_leaves_the_rest_held_and_timed() -> None:
    held = Failing()
    loop = asyncio.get_running_loop()
    errors: list[object] = []
    loop.set_exception_handler(lambda _, context: errors.append(context))
    two_due_together(held)
    await eventually(lambda: len(held.expired) == 2, poll=0)
    assert [h.work for h in held.expired] == ["first", "second"]
    assert held.approvals.waiting == ()
