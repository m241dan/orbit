"""Coders' slots shared across workspaces: taken first come, first served, and always given back."""

import asyncio

import pytest

from orbit.agent.application.coder_slots import CoderSeat, CoderSlots


class Changes:
    """Counts the times the line changed."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


async def waiting(slots: CoderSlots, workspace: str, session: str = "s") -> asyncio.Task[bool]:
    """A task taking a slot for ``workspace``'s ``session``, waiting in line by now."""
    task = asyncio.create_task(slots.acquire(workspace, session))
    await asyncio.sleep(0)
    assert slots.position(workspace, session) is not None
    return task


async def test_a_free_slot_is_taken_at_once() -> None:
    slots = CoderSlots(2)

    assert await slots.acquire("a", "s")
    assert await slots.acquire("b", "s")
    assert slots.taken == 2


async def test_no_limit_never_waits() -> None:
    slots = CoderSlots(0)

    for _ in range(10):
        assert await slots.acquire("a", "s")

    assert slots.taken == 10


async def test_waiters_take_slots_in_the_order_they_asked() -> None:
    changes = Changes()
    slots = CoderSlots(1, changes)
    await slots.acquire("a", "s")
    b = await waiting(slots, "b")
    c = await waiting(slots, "c")

    assert (slots.position("b", "s"), slots.position("c", "s")) == (1, 2)
    slots.release()
    assert await b
    assert slots.position("c", "s") == 1
    assert not c.done()
    slots.release()
    assert await c
    assert slots.taken == 1
    assert changes.count == 4  # Two joined the line, and each left it.


async def test_one_asking_after_others_waits_behind_them_though_a_slot_is_free() -> None:
    slots = CoderSlots(1)
    await slots.acquire("a", "s")
    b = await waiting(slots, "b")
    slots.release()
    a = asyncio.create_task(slots.acquire("a", "s"))
    await asyncio.sleep(0)

    assert await b
    assert slots.position("a", "s") == 1
    slots.release()
    assert await a


async def test_a_session_not_in_line_has_no_position() -> None:
    slots = CoderSlots(1)
    await slots.acquire("a", "s")

    assert slots.position("a", "s") is None


async def test_a_dropped_place_takes_no_slot() -> None:
    changes = Changes()
    slots = CoderSlots(1, changes)
    await slots.acquire("a", "s")
    b = await waiting(slots, "b")
    c = await waiting(slots, "c")
    slots.drop("b", "s")

    assert not await b
    assert slots.position("c", "s") == 1
    slots.release()
    assert await c
    assert slots.taken == 1
    assert changes.count == 4


async def test_dropping_a_workspace_drops_its_every_session() -> None:
    slots = CoderSlots(1)
    await slots.acquire("a", "s")
    first, second = await waiting(slots, "b", "s1"), await waiting(slots, "b", "s2")
    slots.drop("b")

    assert not await first
    assert not await second


async def test_dropping_another_session_keeps_the_place() -> None:
    changes = Changes()
    slots = CoderSlots(1, changes)
    await slots.acquire("a", "s")
    await waiting(slots, "b", "s1")
    slots.drop("b", "s2")
    slots.drop("c")

    assert slots.position("b", "s1") == 1
    assert changes.count == 1


async def test_a_waiter_cancelled_leaves_the_line() -> None:
    slots = CoderSlots(1)
    await slots.acquire("a", "s")
    b = await waiting(slots, "b")
    c = await waiting(slots, "c")
    b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await b

    assert slots.position("c", "s") == 1
    slots.release()
    assert await c
    assert slots.taken == 1


async def test_a_waiter_cancelled_as_it_is_granted_gives_the_slot_back() -> None:
    slots = CoderSlots(1)
    await slots.acquire("a", "s")
    b = await waiting(slots, "b")
    slots.release()
    b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await b

    assert slots.taken == 0


async def test_a_waiter_cancelled_as_it_is_dropped_gives_nothing_back() -> None:
    slots = CoderSlots(1)
    await slots.acquire("a", "s")
    b = await waiting(slots, "b")
    slots.drop("b")
    b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await b

    assert slots.taken == 1


async def test_raising_the_cap_grants_those_waiting() -> None:
    slots = CoderSlots(1)
    await slots.acquire("a", "s")
    b = await waiting(slots, "b")
    c = await waiting(slots, "c")
    slots.cap(2)

    assert await b
    assert slots.position("c", "s") == 1
    slots.cap(0)
    assert await c
    assert slots.taken == 3


async def test_a_seat_takes_its_workspaces_open_session_slots() -> None:
    sessions = iter(("s1", "s1", "s2"))
    shared = CoderSlots(1)
    seat = CoderSeat(lambda: shared, "b", lambda: next(sessions))
    await shared.acquire("a", "s")
    task = asyncio.create_task(seat.acquire())
    await asyncio.sleep(0)

    assert seat.position == 1
    seat.drop()  # Of session s2, now open, so the place in line is kept.
    assert shared.position("b", "s1") == 1
    shared.release()
    assert await task
    seat.release()
    assert shared.taken == 0


async def test_a_waiter_cancelled_before_a_release_passes_the_slot_to_the_next() -> None:
    changes = Changes()
    slots = CoderSlots(1, changes)
    await slots.acquire("a", "s")
    b = await waiting(slots, "b")
    c = await waiting(slots, "c")
    b.cancel()
    slots.release()

    with pytest.raises(asyncio.CancelledError):
        await b
    assert await c
    assert slots.taken == 1
    assert changes.count == 3


async def test_a_lone_waiter_cancelled_before_a_release_leaves_the_slot_free() -> None:
    slots = CoderSlots(1)
    await slots.acquire("a", "s")
    b = await waiting(slots, "b")
    b.cancel()
    slots.release()

    with pytest.raises(asyncio.CancelledError):
        await b
    assert slots.taken == 0
    assert slots.open


async def test_a_waiter_cancelled_before_its_workspace_is_dropped_takes_nothing() -> None:
    changes = Changes()
    slots = CoderSlots(1, changes)
    await slots.acquire("a", "s")
    b = await waiting(slots, "b")
    b.cancel()
    slots.drop("b")

    with pytest.raises(asyncio.CancelledError):
        await b
    assert slots.taken == 1
    assert slots.waiting == 0
    assert changes.count == 2
