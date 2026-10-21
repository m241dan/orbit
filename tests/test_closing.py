"""Closing every part in turn: each is awaited, whatever happens to the ones before it."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from orbit.agent.application.closing import close_all


def recording(
    done: list[str], name: str, failure: Exception | None = None
) -> Callable[[], Awaitable[None]]:
    """A step that records ``name`` in ``done`` as it runs, then raises ``failure``, if any."""

    async def step() -> None:
        done.append(name)
        if failure is not None:
            raise failure

    return step


async def test_every_step_runs_in_order_when_one_fails_and_the_first_failure_is_raised() -> None:
    done: list[str] = []
    steps = [
        recording(done, "a"),
        recording(done, "b", ValueError("b broke")),
        recording(done, "c", KeyError("c broke")),
        recording(done, "d"),
    ]

    with pytest.raises(ValueError, match="b broke"):
        await close_all(steps)

    assert done == ["a", "b", "c", "d"]


async def test_a_cancellation_during_a_step_still_runs_the_rest_then_propagates() -> None:
    done: list[str] = []
    entered = asyncio.Event()

    async def blocked() -> None:
        done.append("blocked")
        entered.set()
        await asyncio.Event().wait()

    steps = [
        recording(done, "a"),
        blocked,
        recording(done, "b", ValueError("b")),
        recording(done, "c"),
    ]
    closing = asyncio.create_task(close_all(steps))
    await entered.wait()
    closing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await closing

    assert done == ["a", "blocked", "b", "c"]


async def test_without_a_failure_it_returns_after_every_step() -> None:
    done: list[str] = []

    await close_all([recording(done, "a"), recording(done, "b")])

    assert done == ["a", "b"]
