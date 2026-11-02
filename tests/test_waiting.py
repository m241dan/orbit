"""The test helper that waits for a condition to hold, for a while at most."""

import asyncio

import pytest

from tests.waiting import eventually, eventually_awaited, eventually_sync


class Countdown:
    """A condition that holds once it has been asked ``times`` times."""

    def __init__(self, times: int) -> None:
        self.asked = 0
        self._times = times

    def __call__(self) -> bool:
        self.asked += 1
        return self.asked >= self._times


async def test_it_fails_with_a_timeout_when_the_condition_never_holds() -> None:
    with pytest.raises(TimeoutError):
        await eventually(lambda: False, timeout=0.05)


async def test_it_returns_once_the_condition_holds() -> None:
    condition = Countdown(3)
    await eventually(condition)
    assert condition.asked == 3


async def test_it_returns_at_once_without_yielding_when_the_condition_already_holds() -> None:
    others: list[str] = []
    asyncio.get_running_loop().call_soon(others.append, "ran")
    await eventually(lambda: True)
    assert others == []


async def test_with_no_delay_it_looks_again_on_every_turn_of_the_loop() -> None:
    turns: list[int] = []

    async def count() -> None:
        for turn in range(3):
            turns.append(turn)
            await asyncio.sleep(0)

    counting = asyncio.ensure_future(count())
    # Only true for one turn of the loop, so seen only by looking again on every turn.
    await eventually(lambda: turns == [0], poll=0, timeout=0.05)
    await counting


async def test_waiting_on_an_awaited_condition_fails_with_a_timeout_when_it_never_holds() -> None:
    async def never() -> bool:
        return False

    with pytest.raises(TimeoutError):
        await eventually_awaited(never, timeout=0.05)


async def test_waiting_on_an_awaited_condition_returns_once_it_holds() -> None:
    condition = Countdown(3)

    async def holds() -> bool:
        return condition()

    await eventually_awaited(holds)
    assert condition.asked == 3


def test_waiting_in_the_thread_fails_with_a_timeout_when_the_condition_never_holds() -> None:
    with pytest.raises(TimeoutError):
        eventually_sync(lambda: False, timeout=0.05)


def test_waiting_in_the_thread_returns_once_the_condition_holds() -> None:
    condition = Countdown(3)
    eventually_sync(condition)
    assert condition.asked == 3
