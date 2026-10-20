"""The workspace registry: finding or registering workspaces by path, and which are served."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orbit.agent.external.sqlite_schema import StoreError
from orbit.agent.external.sqlite_store import SqliteStore
from orbit.agent.external.workspace_registry import WorkspaceRegistry


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    opened = await SqliteStore.open(tmp_path / "state")
    yield opened
    await opened.close()


def directory(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


async def test_the_same_path_finds_the_same_id(store: SqliteStore, tmp_path: Path) -> None:
    workspace = directory(tmp_path, "project")
    registry = WorkspaceRegistry(store)
    workspace_id = await registry.find_or_register(str(workspace))
    assert await registry.find_or_register(str(workspace / ".")) == workspace_id
    assert await WorkspaceRegistry(store).find_or_register(str(workspace)) == workspace_id


async def test_each_path_has_its_own_id(store: SqliteStore, tmp_path: Path) -> None:
    registry = WorkspaceRegistry(store)
    first = await registry.find_or_register(str(directory(tmp_path, "first")))
    second = await registry.find_or_register(str(directory(tmp_path, "second")))
    assert first != second


async def test_a_workspace_is_looked_up_by_its_id(store: SqliteStore, tmp_path: Path) -> None:
    workspace = directory(tmp_path, "project")
    registry = WorkspaceRegistry(store)
    workspace_id = await registry.find_or_register(str(workspace))
    assert await registry.path(workspace_id) == str(workspace.resolve())
    assert await registry.path("unknown") is None


async def test_a_registered_workspace_isnt_served(store: SqliteStore, tmp_path: Path) -> None:
    registry = WorkspaceRegistry(store)
    await registry.find_or_register(str(directory(tmp_path, "project")))
    assert await registry.served() == []


async def test_the_served_flag_round_trips(store: SqliteStore, tmp_path: Path) -> None:
    registry = WorkspaceRegistry(store)
    second = directory(tmp_path, "second")
    first = directory(tmp_path, "first")
    second_id = await registry.find_or_register(str(second))
    first_id = await registry.find_or_register(str(first))
    await registry.set_served(second_id, True)
    await registry.set_served(first_id, True)
    assert await registry.served() == [
        (first_id, str(first.resolve())),
        (second_id, str(second.resolve())),
    ]
    await registry.set_served(second_id, False)
    assert await registry.served() == [(first_id, str(first.resolve()))]


async def test_the_flag_is_kept_when_the_workspace_is_found_again(
    store: SqliteStore, tmp_path: Path
) -> None:
    workspace = directory(tmp_path, "project")
    registry = WorkspaceRegistry(store)
    workspace_id = await registry.find_or_register(str(workspace))
    await registry.set_served(workspace_id, True)
    await registry.find_or_register(str(workspace))
    assert await registry.served() == [(workspace_id, str(workspace.resolve()))]


async def test_a_closed_store_fails_each_call_naming_the_database(
    store: SqliteStore, tmp_path: Path
) -> None:
    registry = WorkspaceRegistry(store)
    await store.close()
    database = r".*orbit\.db: the store is closed"
    with pytest.raises(StoreError, match=f"Couldn't register the workspace .* in {database}"):
        await registry.find_or_register(str(tmp_path))
    with pytest.raises(StoreError, match=f"Couldn't list the served workspaces in {database}"):
        await registry.served()
    with pytest.raises(StoreError, match=f"Couldn't mark whether the workspace w .*{database}"):
        await registry.set_served("w", True)
    with pytest.raises(StoreError, match=f"Couldn't look up the workspace w in {database}"):
        await registry.path("w")
