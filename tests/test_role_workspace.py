"""Where an agent works, as its role's workspace says: the coder's tree, readied; a fresh snapshot
of the base commit, dropped after the run; the user's checkout; or an empty temporary directory,
removed after the run, however it ends. Without a tree, the worktree and a snapshot are the
checkout."""

import asyncio

import pytest

from orbit.agent.application.ports import TempDirFailed
from orbit.agent.application.role_workspace import workspace_for
from orbit.agent.domain.roles import Workspace
from orbit.protocol import Event
from tests.agent_fakes import FakeTempDirs, FakeTree, desk_for


class Places:
    """A desk whose agents work in ``tree``, if given, or else in place at ``/src/app``, with
    temporary directories that record which were made and removed."""

    def __init__(self, tree: FakeTree | None = None) -> None:
        self.events: list[Event] = []
        self.temp_dirs = FakeTempDirs()
        self.desk = desk_for(self.publish, tree=tree, root="/src/app", temp_dirs=self.temp_dirs)

    async def publish(self, event: Event) -> None:
        self.events.append(event)


async def entered(where: Workspace, places: Places, keep: bool = False) -> str:
    """The directory an agent whose role works ``where`` runs in with ``places``."""
    async with workspace_for(where, places.desk, keep) as path:
        return path


async def test_a_worktree_role_works_in_the_coders_tree_readied_and_left_in_place() -> None:
    tree = FakeTree()
    places = Places(tree)
    assert await entered("worktree", places) == "/work/coder"
    assert tree.prepared == [False]
    assert (tree.snapshots, tree.dropped, places.temp_dirs.made) == ([], [], [])


async def test_a_worktree_role_can_keep_the_changes_left_in_the_tree() -> None:
    tree = FakeTree()
    await entered("worktree", Places(tree), keep=True)
    assert tree.prepared == [True]


async def test_a_snapshot_role_works_in_a_fresh_snapshot_of_the_base_dropped_after() -> None:
    tree = FakeTree()
    async with workspace_for("snapshot", Places(tree).desk) as path:
        assert (path, tree.snapshots, tree.dropped) == ("/work/review-base", ["base"], [])
    assert tree.dropped == ["/work/review-base"]
    assert tree.prepared == []


async def test_a_checkout_role_works_in_the_workspace_root_leaving_nothing_behind() -> None:
    tree = FakeTree()
    places = Places(tree)
    assert await entered("checkout", places) == "/src/app"
    assert (tree.prepared, tree.snapshots, places.temp_dirs.made) == ([], [], [])


async def test_a_role_working_in_none_gets_an_empty_temporary_directory_removed_after() -> None:
    tree = FakeTree()
    places = Places(tree)
    async with workspace_for("none", places.desk) as path:
        assert (path, places.temp_dirs.removed) == ("/tmp/orbit-1", [])
    assert places.temp_dirs.removed == ["/tmp/orbit-1"]
    assert (tree.prepared, tree.snapshots) == ([], [])


@pytest.mark.parametrize("where", ["none", "snapshot"])
async def test_a_runs_own_directory_is_removed_when_the_run_is_cancelled(where: Workspace) -> None:
    tree, inside = FakeTree(), asyncio.Event()
    places = Places(tree)

    async def run() -> None:
        async with workspace_for(where, places.desk):
            inside.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await asyncio.wait_for(inside.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(places.temp_dirs.made) + len(tree.snapshots) == 1
    assert places.temp_dirs.removed == places.temp_dirs.made
    assert len(tree.dropped) == len(tree.snapshots)


async def test_a_temporary_directory_is_removed_when_the_run_fails() -> None:
    places = Places()
    with pytest.raises(RuntimeError, match="broke"):
        async with workspace_for("none", places.desk):
            raise RuntimeError("broke")
    assert places.temp_dirs.removed == ["/tmp/orbit-1"]


@pytest.mark.parametrize("where", ["worktree", "snapshot"])
async def test_without_a_tree_the_worktree_and_a_snapshot_are_the_workspace_root(
    where: Workspace,
) -> None:
    places = Places()
    assert await entered(where, places) == "/src/app"
    assert places.temp_dirs.made == []


async def test_without_a_tree_a_role_working_in_none_still_gets_a_temporary_directory() -> None:
    places = Places()
    assert await entered("none", places) == "/tmp/orbit-1"
    assert places.temp_dirs.removed == ["/tmp/orbit-1"]


async def test_a_temporary_directory_that_cant_be_made_fails_with_nothing_to_remove() -> None:
    places = Places()
    places.temp_dirs.failure = TempDirFailed("Couldn't make a temporary directory: disk full")
    with pytest.raises(TempDirFailed, match="disk full"):
        await entered("none", places)
    assert places.temp_dirs.removed == []
