"""The workspace registry: finding or registering workspaces by path, and which are served."""

import sqlite3
import subprocess
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orbit.agent.external import repository, sqlite_store, workspace_registry
from orbit.agent.external.git import GitResult, git_sync
from orbit.agent.external.repository import workspace_fingerprint
from orbit.agent.external.sqlite_schema import StoreError
from orbit.agent.external.sqlite_store import SqliteStore
from orbit.agent.external.workspace_registry import WorkspaceRegistry, register


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


async def test_the_fingerprint_is_brought_up_to_date(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, tmp_path: Path
) -> None:
    workspace = directory(tmp_path, "project")
    registry = WorkspaceRegistry(store)
    monkeypatch.setattr(workspace_registry, "workspace_fingerprint", lambda path: "old")
    workspace_id = await registry.find_or_register(str(workspace))
    monkeypatch.setattr(workspace_registry, "workspace_fingerprint", lambda path: "new")
    assert await registry.find_or_register(str(workspace)) == workspace_id
    query = "SELECT fingerprint FROM workspaces"
    rows = await store.run("Couldn't read", lambda connection: connection.execute(query).fetchall())
    assert rows == [("new",)]


async def test_the_fingerprint_is_worked_out_off_the_database_thread(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, tmp_path: Path
) -> None:
    threads: list[str] = []

    def fingerprint(path: str) -> str:
        threads.append(threading.current_thread().name)
        return "fingerprint"

    monkeypatch.setattr(workspace_registry, "workspace_fingerprint", fingerprint)
    await WorkspaceRegistry(store).find_or_register(str(directory(tmp_path, "project")))
    assert len(threads) == 1
    assert not threads[0].startswith(sqlite_store.THREAD_PREFIX)


async def test_register_is_given_the_fingerprint_and_no_git_runs_on_the_database_thread(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, tmp_path: Path
) -> None:
    workspace = directory(tmp_path, "project")
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    commit = ["git", "-C", str(workspace), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*commit, "commit", "-q", "--allow-empty", "-m", "root"], check=True)
    expected = workspace_fingerprint(str(workspace))
    git_threads: list[str] = []
    given: list[str | None] = []

    def recorded_git(path: str, *args: str, stderr: bool) -> GitResult:
        git_threads.append(threading.current_thread().name)
        return git_sync(path, *args, stderr=stderr)

    def recorded_register(
        connection: sqlite3.Connection, path: Path, fingerprint: str | None
    ) -> str:
        given.append(fingerprint)
        return register(connection, path, fingerprint)

    monkeypatch.setattr(repository, "git_sync", recorded_git)
    monkeypatch.setattr(workspace_registry, "register", recorded_register)
    await WorkspaceRegistry(store).find_or_register(str(workspace))

    assert expected is not None and given == [expected]
    assert git_threads
    assert not any(name.startswith(sqlite_store.THREAD_PREFIX) for name in git_threads)


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
