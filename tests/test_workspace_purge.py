"""Purging a workspace from the store: its records, session locks and fallback files, and a
managed clone's directory, unless that would lose work; other workspaces untouched."""

import shutil
from pathlib import Path

import pytest

from orbit.agent.external.fallback_files import FallbackFiles
from orbit.agent.external.repository import orbit_dir
from orbit.agent.external.sqlite_schema import StoreError
from orbit.agent.external.sqlite_store import SqliteStore
from orbit.agent.external.workspace_purge import PurgeRefused, purge_workspace
from orbit.agent.external.workspace_registry import Workspace, WorkspaceRegistry
from tests.agent_fakes import notice
from tests.git_repo import bare_repository, commit_file, git
from tests.store_views import database, record


async def used(store: SqliteStore, path: Path, *, managed: bool = False) -> str:
    """The id of the workspace at ``path``, registered with a session, ``abc``, holding its
    lock, an event, a review count and a fallback file in the state directory and the
    repository; marked managed if ``managed``."""
    path.mkdir(parents=True, exist_ok=True)
    registry = WorkspaceRegistry(store)
    workspace_id = (await registry.find_or_register(str(path))).workspace_id
    if managed:
        await registry.set_managed(workspace_id)
    await store.save(workspace_id, record())
    await store.append(workspace_id, "abc", [notice(1, "abc", "hi")])
    await store.save_review_count(workspace_id, 3)
    assert await store.acquire(workspace_id, "abc")
    files = FallbackFiles(store.state_dir, workspace_id, orbit_dir(str(path)))
    for place in files.places("abc"):
        place.parent.mkdir(parents=True, exist_ok=True)
        place.write_text("")
    return workspace_id


def rows(state: Path, workspace_id: str) -> dict[str, int]:
    """How many rows each table keyed by workspace has for ``workspace_id``."""
    tables = {"events": "workspace_id", "logs": "workspace_id", "sessions": "workspace_id"}
    tables |= {"reviews": "workspace_id", "workspaces": "id"}
    with database(state) as connection:
        return {
            table: connection.execute(
                f"SELECT count(*) FROM {table} WHERE {column} = ?", (workspace_id,)
            ).fetchone()[0]
            for table, column in tables.items()
        }


def leftovers(state: Path, path: Path, workspace_id: str) -> list[Path]:
    """The session lock directory and fallback files the workspace ``workspace_id`` at
    ``path`` has left."""
    places = [state / "locks" / workspace_id]
    places += FallbackFiles(state, workspace_id, orbit_dir(str(path))).places("abc")
    return [place for place in places if place.exists()]


def cloned(tmp_path: Path) -> Path:
    """A clone, with its own git identity, of a bare repository with one commit."""
    clone = tmp_path / "clones" / "proj"
    git(tmp_path, "clone", "-q", str(bare_repository(tmp_path)), str(clone))
    git(clone, "config", "user.name", "Test")
    git(clone, "config", "user.email", "test@example.com")
    return clone


async def test_purging_deletes_every_record_leaving_other_workspaces(
    store: SqliteStore, state: Path, tmp_path: Path
) -> None:
    first = await used(store, tmp_path / "a")
    second = await used(store, tmp_path / "b")

    purged = await purge_workspace(store, second)

    assert (purged.sessions, purged.fallback_files, purged.clone) == (1, 2, None)
    assert purged.workspace == Workspace(second, str(tmp_path / "b"), False, False)
    assert rows(state, second) == dict.fromkeys(rows(state, second), 0)
    assert rows(state, first) == dict.fromkeys(rows(state, first), 1)
    assert leftovers(state, tmp_path / "b", second) == []
    assert len(leftovers(state, tmp_path / "a", first)) == 3
    assert (tmp_path / "b").is_dir()
    again = await WorkspaceRegistry(store).find_or_register(str(tmp_path / "b"))
    assert again.workspace_id != second
    assert await store.sessions(again.workspace_id) == []


async def test_purging_a_clean_managed_clone_deletes_its_directory(
    store: SqliteStore, state: Path, tmp_path: Path
) -> None:
    clone = cloned(tmp_path)
    workspace_id = await used(store, clone, managed=True)

    purged = await purge_workspace(store, workspace_id)

    assert purged.clone == str(clone)
    assert not clone.exists()
    assert rows(state, workspace_id)["workspaces"] == 0


async def test_a_managed_clone_whose_directory_is_gone_is_purged(
    store: SqliteStore, state: Path, tmp_path: Path
) -> None:
    clone = tmp_path / "clones" / "gone"
    workspace_id = await used(store, clone, managed=True)
    shutil.rmtree(clone)

    purged = await purge_workspace(store, workspace_id)

    assert purged.clone == str(clone)
    assert rows(state, workspace_id)["workspaces"] == 0


async def test_a_managed_clone_with_work_not_pushed_is_refused_deleting_nothing(
    store: SqliteStore, state: Path, tmp_path: Path
) -> None:
    clone = cloned(tmp_path)
    workspace_id = await used(store, clone, managed=True)
    commit_file(clone, "b.txt", "new\n", "more")

    with pytest.raises(PurgeRefused) as refusal:
        await purge_workspace(store, workspace_id)

    assert str(refusal.value) == (
        f"Can't purge {clone}: deleting its clone would lose 1 commit on main not on its remote."
    )
    assert clone.is_dir()
    assert rows(state, workspace_id) == dict.fromkeys(rows(state, workspace_id), 1)
    assert len(leftovers(state, clone, workspace_id)) == 3


async def test_a_workspace_not_registered_is_refused(store: SqliteStore) -> None:
    with pytest.raises(PurgeRefused, match=r"No workspace nope is registered\."):
        await purge_workspace(store, "nope")


async def test_a_store_that_fails_raises_store_error(store: SqliteStore, tmp_path: Path) -> None:
    workspace_id = await used(store, tmp_path / "a")
    await store.close()
    with pytest.raises(StoreError):
        await purge_workspace(store, workspace_id)


async def test_a_workspace_is_found_by_its_id_or_its_path(
    store: SqliteStore, tmp_path: Path
) -> None:
    workspace_id = await used(store, tmp_path / "a")
    registry = WorkspaceRegistry(store)
    found = Workspace(workspace_id, str(tmp_path / "a"), False, False)
    assert await registry.find(workspace_id) == found
    assert await registry.find(str(tmp_path / "a" / ".")) == found
    assert await registry.find(str(tmp_path / "b")) is None
