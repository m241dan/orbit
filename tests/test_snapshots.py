"""Snapshots made for a run: dropped after it, without a failing drop masking how it ended."""

import asyncio

import pytest

from orbit.agent.application.ports import TreeFailed
from orbit.agent.application.snapshots import snapshot_of
from orbit.protocol import GIT_FAILED, ErrorOccurred, Event
from tests.agent_fakes import FakeTree

ROOT = "/work/review-c1"
LEFT_OVER = ErrorOccurred(
    code=GIT_FAILED,
    message=f"The snapshot at {ROOT} couldn't be removed and is left over: git worktree failed",
)


def broken_drop() -> FakeTree:
    tree = FakeTree()
    tree.failures["drop"] = TreeFailed("git worktree failed")
    return tree


async def test_the_body_runs_in_a_snapshot_of_the_commit_dropped_after() -> None:
    tree = FakeTree()
    events = asyncio.Queue[Event]()
    async with snapshot_of(tree, "c1", events.put) as root:
        assert (root, tree.snapshots, tree.dropped) == (ROOT, ["c1"], [])
    assert tree.dropped == [ROOT] and events.empty()


async def test_a_failing_drop_is_published_and_the_result_stands() -> None:
    tree = broken_drop()
    events = asyncio.Queue[Event]()

    async def run() -> str:
        async with snapshot_of(tree, "c1", events.put):
            return "reviewed"

    assert await run() == "reviewed"
    assert events.get_nowait() == LEFT_OVER and events.empty()


async def test_a_failing_drop_leaves_the_bodys_exception_to_raise() -> None:
    events = asyncio.Queue[Event]()
    with pytest.raises(ValueError, match="the run fell over"):
        async with snapshot_of(broken_drop(), "c1", events.put):
            raise ValueError("the run fell over")
    assert events.get_nowait() == LEFT_OVER


async def test_a_cancelled_body_still_drops_its_snapshot() -> None:
    tree = FakeTree()
    entered = asyncio.Event()

    async def run() -> None:
        async with snapshot_of(tree, "c1", asyncio.Queue[Event]().put):
            entered.set()
            await asyncio.Event().wait()

    running = asyncio.create_task(run())
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert tree.dropped == [ROOT]


async def test_a_snapshot_that_cannot_be_made_raises_and_runs_nothing() -> None:
    tree = FakeTree()
    tree.failures["snapshot"] = TreeFailed("git worktree add failed")
    with pytest.raises(TreeFailed, match="git worktree add failed"):
        async with snapshot_of(tree, "c1", asyncio.Queue[Event]().put):
            raise AssertionError("unreachable")
    assert tree.dropped == []
