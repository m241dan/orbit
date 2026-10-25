"""Purging a workspace from the store: its records, session locks and fallback files, and a
managed clone's directory, unless that would lose work, deleting the files before the records;
other workspaces untouched. Forgetting one, as ``orbit db forget`` does, holds its workspace
lock, and refuses one a running server serves or another orbit holds."""

import os
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from orbit.agent.application.purge import purge
from orbit.agent.domain.purging import Purged, PurgeRefused
from orbit.agent.domain.workspaces import RegisteredWorkspace
from orbit.agent.external import workspace_purge
from orbit.agent.external.fallback_files import FallbackFiles
from orbit.agent.external.repository import orbit_dir
from orbit.agent.external.run_file import RunFile
from orbit.agent.external.sqlite_schema import StoreError
from orbit.agent.external.sqlite_store import SqliteStore
from orbit.agent.external.workspace_lock import WorkspaceLock
from orbit.agent.external.workspace_purge import StorePurging, forget_workspace, purge_workspace
from orbit.agent.external.workspace_registry import WorkspaceRegistry
from tests.agent_fakes import notice
from tests.git_repo import (
    bare_repository,
    commit_file,
    git,
    init_repo,
    rmtree_stopping_partway,
)
from tests.store_views import database, record


async def used(store: SqliteStore, path: Path, *, managed: bool = False) -> str:
    """The id of the workspace at ``path``, registered with a session, ``abc``, holding its
    lock, an event, a review count, a record move and a fallback file in the state directory
    and the repository; marked managed if ``managed``."""
    path.mkdir(parents=True, exist_ok=True)
    registry = WorkspaceRegistry(store)
    workspace_id = (await registry.find_or_register(str(path))).workspace_id
    if managed:
        await registry.set_managed(workspace_id)
    await store.save(workspace_id, record())
    await store.append(workspace_id, "abc", [notice(1, "abc", "hi")])
    await store.save_review_count(workspace_id, 3)
    await store.save_record_moves(workspace_id, {"docs/00001-a.md": "docs/00002-a.md"})
    assert await store.acquire(workspace_id, "abc")
    files = FallbackFiles(store.state_dir, workspace_id, orbit_dir(str(path)))
    for place in files.places("abc"):
        place.parent.mkdir(parents=True, exist_ok=True)
        place.write_text("")
    return workspace_id


def rows(state: Path, workspace_id: str) -> dict[str, int]:
    """How many rows each table keyed by workspace has for ``workspace_id``."""
    tables = {"events": "workspace_id", "logs": "workspace_id", "sessions": "workspace_id"}
    tables |= {"reviews": "workspace_id", "record_moves": "workspace_id", "workspaces": "id"}
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
    assert purged.workspace == RegisteredWorkspace(second, str(tmp_path / "b"), False, False)
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

    assert purged == Purged(
        RegisteredWorkspace(workspace_id, str(clone), False, True), 1, 2, str(clone)
    )
    assert list(clone.parent.iterdir()) == []
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


async def test_a_clone_re_attached_to_a_checkout_is_purged_leaving_the_checkout(
    store: SqliteStore, state: Path, tmp_path: Path
) -> None:
    clone = cloned(tmp_path)
    workspace_id = await used(store, clone, managed=True)
    shutil.rmtree(clone)
    checkout = tmp_path / "code" / "proj"
    git(tmp_path, "clone", "-q", str(tmp_path / "proj.git"), str(checkout))
    registry = WorkspaceRegistry(store)
    assert (await registry.find_or_register(str(checkout))).workspace_id == workspace_id

    purged = await purge_workspace(store, workspace_id)

    assert purged.clone is None
    assert (checkout / "a.txt").is_file()
    assert rows(state, workspace_id)["workspaces"] == 0


async def test_a_managed_clone_with_work_not_pushed_is_refused_deleting_nothing(
    store: SqliteStore, state: Path, tmp_path: Path
) -> None:
    clone = cloned(tmp_path)
    workspace_id = await used(store, clone, managed=True)
    commit_file(clone, "b.txt", "new\n", "more")

    with pytest.raises(PurgeRefused) as refusal:
        await purge(StorePurging(store), workspace_id)

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
    found = RegisteredWorkspace(workspace_id, str(tmp_path / "a"), False, False)
    assert await registry.find(workspace_id) == found
    assert await registry.find(str(tmp_path / "a" / ".")) == found
    assert await registry.find(str(tmp_path / "b")) is None


async def test_a_clone_that_cant_be_deleted_stays_registered_with_its_records_for_a_retry(
    store: SqliteStore, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = cloned(tmp_path)
    workspace_id = await used(store, clone, managed=True)

    def refused(path: Path) -> None:
        raise PermissionError(f"Can't delete {path}")

    with monkeypatch.context() as patched:
        patched.setattr(workspace_purge, "_remove_clone", refused)
        with pytest.raises(PermissionError):
            await purge(StorePurging(store), str(clone))
    assert rows(state, workspace_id) == dict.fromkeys(rows(state, workspace_id), 1)
    assert clone.is_dir()

    purged = await purge(StorePurging(store), str(clone))

    # The first attempt deleted the fallback files before it came to the clone.
    assert (purged.sessions, purged.fallback_files, purged.clone) == (1, 0, str(clone))
    assert not clone.exists()
    assert rows(state, workspace_id) == dict.fromkeys(rows(state, workspace_id), 0)


async def test_a_clone_whose_deletion_stops_partway_is_purged_by_purging_it_again(
    store: SqliteStore, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = cloned(tmp_path)
    workspace_id = await used(store, clone, managed=True)
    with monkeypatch.context() as patched:
        patched.setattr(shutil, "rmtree", rmtree_stopping_partway)
        with pytest.raises(PermissionError):
            await forget_workspace(store, str(clone))
    assert rows(state, workspace_id) == dict.fromkeys(rows(state, workspace_id), 1)

    purged = await forget_workspace(store, str(clone))

    assert (purged.sessions, purged.fallback_files, purged.clone) == (1, 0, str(clone))
    assert list(clone.parent.iterdir()) == []
    assert rows(state, workspace_id) == dict.fromkeys(rows(state, workspace_id), 0)


async def test_what_a_purge_stopped_partway_left_goes_with_a_clone_made_in_its_place(
    store: SqliteStore, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = cloned(tmp_path)
    workspace_id = await used(store, clone, managed=True)
    with monkeypatch.context() as patched:
        patched.setattr(shutil, "rmtree", rmtree_stopping_partway)
        with pytest.raises(PermissionError):
            await purge_workspace(store, workspace_id)
    git(tmp_path, "clone", "-q", str(tmp_path / "proj.git"), str(clone))

    purged = await purge_workspace(store, workspace_id)

    assert purged.clone == str(clone)
    assert list(clone.parent.iterdir()) == []
    assert rows(state, workspace_id)["workspaces"] == 0


async def test_fallback_files_that_cant_be_deleted_leave_it_registered_for_a_retry(
    store: SqliteStore, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = await used(store, tmp_path / "project")

    def refused(directories: Sequence[Path], workspace_id: str) -> int:
        raise PermissionError("Can't delete a fallback file")

    with monkeypatch.context() as patched:
        patched.setattr(workspace_purge, "remove_fallback_files", refused)
        with pytest.raises(PermissionError):
            await forget_workspace(store, str(tmp_path / "project"))
    assert rows(state, workspace_id) == dict.fromkeys(rows(state, workspace_id), 1)
    assert len(leftovers(state, tmp_path / "project", workspace_id)) == 3

    purged = await forget_workspace(store, str(tmp_path / "project"))

    assert (purged.sessions, purged.fallback_files, purged.clone) == (1, 2, None)
    assert rows(state, workspace_id) == dict.fromkeys(rows(state, workspace_id), 0)
    assert leftovers(state, tmp_path / "project", workspace_id) == []


async def test_a_workspace_is_found_by_a_directory_in_its_repository(
    store: SqliteStore, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "repo")
    (repo / "src").mkdir()
    registry = WorkspaceRegistry(store)
    workspace_id = (await registry.find_or_register(str(repo))).workspace_id

    found = await registry.find(str(repo / "src"))

    assert found == RegisteredWorkspace(workspace_id, str(repo), False, False)


async def test_forgetting_a_key_no_workspace_is_registered_at_is_refused(
    store: SqliteStore, tmp_path: Path
) -> None:
    await used(store, tmp_path / "other")

    with pytest.raises(PurgeRefused) as refusal:
        await forget_workspace(store, str(tmp_path / "project"))

    assert str(refusal.value) == f"No workspace is registered at {tmp_path / 'project'}."


async def test_a_workspace_a_running_server_serves_is_refused_deleting_nothing(
    store: SqliteStore, state: Path, tmp_path: Path
) -> None:
    workspace_id = await used(store, tmp_path / "project")
    await WorkspaceRegistry(store).set_served(workspace_id, True)
    RunFile(state).write("wss://orbit.example:4567/", os.getpid())

    with pytest.raises(PurgeRefused) as refusal:
        await forget_workspace(store, str(tmp_path / "project"))

    assert str(refusal.value) == (
        f"Can't forget {tmp_path / 'project'}: the orbit serve at wss://orbit.example:4567/ serves"
        " it; remove it there with its sessions, or stop that server first."
    )
    assert rows(state, workspace_id) == dict.fromkeys(rows(state, workspace_id), 1)
    assert len(leftovers(state, tmp_path / "project", workspace_id)) == 3


async def test_a_workspace_flagged_served_with_no_server_running_is_forgotten(
    store: SqliteStore, state: Path, tmp_path: Path
) -> None:
    workspace_id = await used(store, tmp_path / "project")
    await WorkspaceRegistry(store).set_served(workspace_id, True)

    purged = await forget_workspace(store, workspace_id)

    assert purged.workspace.id == workspace_id
    assert rows(state, workspace_id)["workspaces"] == 0


async def test_a_workspace_whose_lock_another_orbit_holds_is_refused_deleting_nothing(
    store: SqliteStore, state: Path, tmp_path: Path
) -> None:
    workspace_id = await used(store, tmp_path / "project")
    lock = WorkspaceLock(orbit_dir(str(tmp_path / "project")))
    assert lock.take() is None
    try:
        with pytest.raises(PurgeRefused) as refusal:
            await forget_workspace(store, str(tmp_path / "project"))
    finally:
        lock.close()

    assert str(refusal.value) == (
        f"Can't forget {tmp_path / 'project'}: an orbit chat, pid {os.getpid()} holds its lock."
    )
    assert rows(state, workspace_id) == dict.fromkeys(rows(state, workspace_id), 1)
    assert len(leftovers(state, tmp_path / "project", workspace_id)) == 3


async def test_a_workspace_whose_directory_is_gone_is_forgotten_without_taking_a_lock(
    store: SqliteStore, state: Path, tmp_path: Path
) -> None:
    workspace_id = await used(store, tmp_path / "project")
    shutil.rmtree(tmp_path / "project")

    purged = await forget_workspace(store, str(tmp_path / "project"))

    assert purged.workspace.id == workspace_id
    assert rows(state, workspace_id)["workspaces"] == 0
    # Taking the lock would have made the orbit directory, and so the workspace's, again.
    assert not (tmp_path / "project").exists()


async def test_forgetting_holds_the_workspace_lock_while_deleting_and_lets_it_go_after(
    store: SqliteStore, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = await used(store, tmp_path / "project")
    lock = WorkspaceLock(orbit_dir(str(tmp_path / "project")))
    holders: list[str | None] = []

    def noted(directories: Sequence[Path], workspace_id: str) -> int:
        owner = lock.take()
        holders.append(None if owner is None else owner.described)
        lock.close()
        return 0

    monkeypatch.setattr(workspace_purge, "remove_fallback_files", noted)

    purged = await forget_workspace(store, str(tmp_path / "project"))

    assert purged.workspace.id == workspace_id
    assert holders == [f"an orbit chat, pid {os.getpid()}"]
    assert lock.take() is None
    lock.close()
