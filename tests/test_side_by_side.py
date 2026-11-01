"""Running jobs side by side: up to a number at once, picking again as one ends or on being woken,
a failure that escapes one raised once the others have stopped, and every one stopped when the run
is."""

import asyncio

import pytest

from orbit.execution.side_by_side import SideBySide
from tests.waiting import eventually


class Jobs:
    """Jobs picked from :attr:`queue` in order, each waiting for its own gate once started, or
    failing at once if it's ``"boom"``; :attr:`stopped` lists those cancelled."""

    def __init__(self, *queue: str) -> None:
        self.queue = list(queue)
        self.gates: dict[str, asyncio.Event] = {}
        self.stopped: list[str] = []

    def pick(self) -> str | None:
        return self.queue.pop(0) if self.queue else None

    async def run(self, key: str) -> None:
        if key == "boom":
            raise ValueError("boom")
        gate = self.gates.setdefault(key, asyncio.Event())
        try:
            await gate.wait()
        except asyncio.CancelledError:
            self.stopped.append(key)
            raise

    def let_go(self, key: str) -> None:
        self.gates.setdefault(key, asyncio.Event()).set()


async def test_it_runs_up_to_its_size_at_once_and_picks_the_next_as_one_ends() -> None:
    jobs = Jobs("a", "b", "c")
    slots = SideBySide(2, jobs.pick, jobs.run)
    run = asyncio.create_task(slots.run())
    await eventually(lambda: slots.running == ("a", "b"))
    assert jobs.queue == ["c"]

    jobs.let_go("a")
    await eventually(lambda: slots.running == ("b", "c"))
    jobs.let_go("b")
    jobs.let_go("c")
    await asyncio.wait_for(run, 5)
    assert slots.running == ()


async def test_with_nothing_picked_it_ends_at_once() -> None:
    jobs = Jobs()
    await asyncio.wait_for(SideBySide(3, jobs.pick, jobs.run).run(), 5)


async def test_waking_it_fills_the_slots_free_with_what_was_queued_since() -> None:
    jobs = Jobs("a")
    slots = SideBySide(3, jobs.pick, jobs.run)
    run = asyncio.create_task(slots.run())
    await eventually(lambda: slots.running == ("a",))
    jobs.queue.append("b")
    slots.wake()
    await eventually(lambda: slots.running == ("a", "b"))
    jobs.let_go("a")
    jobs.let_go("b")
    await asyncio.wait_for(run, 5)


async def test_a_failure_in_one_job_is_raised_once_the_others_have_stopped() -> None:
    jobs = Jobs("a", "boom")
    slots = SideBySide(2, jobs.pick, jobs.run)
    with pytest.raises(ValueError, match="boom"):
        await asyncio.wait_for(slots.run(), 5)
    assert jobs.stopped == ["a"]
    assert slots.running == ()


async def test_cancelling_the_run_stops_every_job_and_waits_for_them() -> None:
    jobs = Jobs("a", "b")
    slots = SideBySide(2, jobs.pick, jobs.run)
    run = asyncio.create_task(slots.run())
    await eventually(lambda: len(jobs.gates) == 2)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 5)
    assert sorted(jobs.stopped) == ["a", "b"]
    assert slots.running == ()
