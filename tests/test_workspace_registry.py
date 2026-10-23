"""The workspace registry: finding or registering workspaces by path, taking over those whose
repositories moved, and which are served."""

import shutil
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbit.agent.domain.sessions import SessionRecord
from orbit.agent.external import repository, sqlite_store, workspace_registry
from orbit.agent.external.git import GitResult, git_sync
from orbit.agent.external.repository import workspace_fingerprint
from orbit.agent.external.sqlite_schema import StoreError
from orbit.agent.external.sqlite_store import SqliteStore
from orbit.agent.external.workspace_registry import Registered, WorkspaceRegistry, register
from tests.store_views import database

ROOT = "a" * 40
"""A root commit's hash."""
REPO = f"{ROOT} git@github.com:o/r.git"
"""The fingerprint of a repository, wherever it is."""


def directory(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


async def test_the_same_path_finds_the_same_id(store: SqliteStore, tmp_path: Path) -> None:
    workspace = directory(tmp_path, "project")
    registry = WorkspaceRegistry(store)
    registered = await registry.find_or_register(str(workspace))
    assert await registry.find_or_register(str(workspace / ".")) == registered
    assert await WorkspaceRegistry(store).find_or_register(str(workspace)) == registered
    assert registered.note is None


async def test_each_path_has_its_own_id(store: SqliteStore, tmp_path: Path) -> None:
    registry = WorkspaceRegistry(store)
    first = await registry.find_or_register(str(directory(tmp_path, "first")))
    second = await registry.find_or_register(str(directory(tmp_path, "second")))
    assert first.workspace_id != second.workspace_id


async def test_a_workspace_is_looked_up_by_its_id(store: SqliteStore, tmp_path: Path) -> None:
    workspace = directory(tmp_path, "project")
    registry = WorkspaceRegistry(store)
    workspace_id = (await registry.find_or_register(str(workspace))).workspace_id
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
    second_id = (await registry.find_or_register(str(second))).workspace_id
    first_id = (await registry.find_or_register(str(first))).workspace_id
    await registry.set_served(second_id, True)
    await registry.set_served(first_id, True)
    assert await registry.served() == [
        (first_id, str(first.resolve())),
        (second_id, str(second.resolve())),
    ]
    await registry.set_served(second_id, False)
    assert await registry.served() == [(first_id, str(first.resolve()))]


async def test_the_latest_added_is_the_served_workspace_flagged_served_last(
    store: SqliteStore, tmp_path: Path
) -> None:
    registry = WorkspaceRegistry(store)
    first, second = directory(tmp_path, "first"), directory(tmp_path, "second")
    first_id = (await registry.find_or_register(str(first))).workspace_id
    second_id = (await registry.find_or_register(str(second))).workspace_id
    assert await registry.latest_added() is None
    await registry.set_served(first_id, True)
    await registry.set_served(second_id, True)
    assert await registry.latest_added() == str(second.resolve())
    await registry.set_served(first_id, True)
    assert await registry.latest_added() == str(first.resolve())
    await registry.set_served(first_id, False)
    assert await registry.latest_added() == str(second.resolve())


async def test_flagging_served_stamps_the_time_and_unflagging_keeps_it(
    store: SqliteStore, tmp_path: Path
) -> None:
    registry = WorkspaceRegistry(store)
    workspace_id = (
        await registry.find_or_register(str(directory(tmp_path, "project")))
    ).workspace_id
    before = datetime.now(UTC)
    await registry.set_served(workspace_id, True)
    after = datetime.now(UTC)
    with database(tmp_path / "state") as connection:
        (stamped,) = connection.execute("SELECT added_at FROM workspaces").fetchone()
    await registry.set_served(workspace_id, False)
    with database(tmp_path / "state") as connection:
        kept = connection.execute("SELECT added_at FROM workspaces").fetchone()
    assert before <= datetime.fromisoformat(stamped) <= after
    assert kept == (stamped,)


async def test_a_workspace_marked_managed_stays_so_when_found_again(
    store: SqliteStore, tmp_path: Path
) -> None:
    registry = WorkspaceRegistry(store)
    clone = directory(tmp_path, "clone")
    other = directory(tmp_path, "other")
    workspace_id = (await registry.find_or_register(str(clone))).workspace_id
    await registry.find_or_register(str(other))
    await registry.set_managed(workspace_id)
    await registry.find_or_register(str(clone))
    assert await managed(store) == [(str(clone.resolve()), 1), (str(other.resolve()), 0)]


async def managed(store: SqliteStore) -> list[tuple[str, int]]:
    """The path of each workspace registered in ``store``, by path, and whether it's managed."""

    def listed(connection: sqlite3.Connection) -> list[tuple[str, int]]:
        rows = connection.execute("SELECT path, managed FROM workspaces ORDER BY path")
        return [(str(path), int(flag)) for path, flag in rows]

    return await store.run("Couldn't list the workspaces in", listed)


async def test_the_flag_is_kept_when_the_workspace_is_found_again(
    store: SqliteStore, tmp_path: Path
) -> None:
    workspace = directory(tmp_path, "project")
    registry = WorkspaceRegistry(store)
    workspace_id = (await registry.find_or_register(str(workspace))).workspace_id
    await registry.set_served(workspace_id, True)
    await registry.find_or_register(str(workspace))
    assert await registry.served() == [(workspace_id, str(workspace.resolve()))]


async def test_the_fingerprint_is_brought_up_to_date(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, tmp_path: Path
) -> None:
    workspace = directory(tmp_path, "project")
    registry = WorkspaceRegistry(store)
    monkeypatch.setattr(workspace_registry, "workspace_fingerprint", lambda path: "old")
    registered = await registry.find_or_register(str(workspace))
    monkeypatch.setattr(workspace_registry, "workspace_fingerprint", lambda path: "new")
    assert await registry.find_or_register(str(workspace)) == registered
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
    ) -> Registered:
        given.append(fingerprint)
        return register(connection, path, fingerprint)

    monkeypatch.setattr(repository, "git_sync", recorded_git)
    monkeypatch.setattr(workspace_registry, "register", recorded_register)
    await WorkspaceRegistry(store).find_or_register(str(workspace))

    assert expected is not None and given == [expected]
    assert git_threads
    assert not any(name.startswith(sqlite_store.THREAD_PREFIX) for name in git_threads)


def fingerprinted(monkeypatch: pytest.MonkeyPatch, fingerprint: str | None) -> None:
    """Have every repository's fingerprint be ``fingerprint``."""
    monkeypatch.setattr(workspace_registry, "workspace_fingerprint", lambda path: fingerprint)


async def test_a_moved_repository_takes_over_its_workspace_and_sessions(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, tmp_path: Path
) -> None:
    fingerprinted(monkeypatch, REPO)
    old = directory(tmp_path, "a")
    registry = WorkspaceRegistry(store)
    first = await registry.find_or_register(str(old))
    when = datetime(2026, 10, 22, tzinfo=UTC)
    saved = SessionRecord(id="abc", name="work", created=when, last_active=when)
    await store.workspace_view(first.workspace_id).save(saved)
    new = directory(tmp_path, "b")
    shutil.rmtree(old)
    fingerprinted(monkeypatch, f"{ROOT} https://github.com/o/r")

    moved = await registry.find_or_register(str(new))

    assert moved == Registered(
        first.workspace_id,
        f"Re-attached the sessions of {old.resolve()}, which moved to {new.resolve()}.",
    )
    assert await registry.path(first.workspace_id) == str(new.resolve())
    sessions = await store.workspace_view(first.workspace_id).sessions()
    assert [session.id for session in sessions] == ["abc"]
    query = "SELECT name, fingerprint FROM workspaces"
    rows = await store.run("Couldn't read", lambda connection: connection.execute(query).fetchall())
    assert rows == [("b", f"{ROOT} https://github.com/o/r")]
    assert await registry.find_or_register(str(new)) == Registered(first.workspace_id)


async def test_a_managed_workspace_re_attached_to_a_new_path_isnt_managed(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, tmp_path: Path
) -> None:
    fingerprinted(monkeypatch, REPO)
    old = directory(tmp_path, "clone")
    registry = WorkspaceRegistry(store)
    first = await registry.find_or_register(str(old))
    await registry.set_managed(first.workspace_id)
    shutil.rmtree(old)
    checkout = directory(tmp_path, "checkout")

    moved = await registry.find_or_register(str(checkout))

    assert moved.workspace_id == first.workspace_id
    assert await managed(store) == [(str(checkout.resolve()), 0)]


async def test_a_second_clone_while_the_first_is_there_is_a_new_workspace(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, tmp_path: Path
) -> None:
    fingerprinted(monkeypatch, REPO)
    registry = WorkspaceRegistry(store)
    first = await registry.find_or_register(str(directory(tmp_path, "a")))
    second = await registry.find_or_register(str(directory(tmp_path, "c")))
    assert second.workspace_id != first.workspace_id and second.note is None


async def test_a_repository_like_several_gone_ones_is_a_new_workspace_naming_them(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, tmp_path: Path
) -> None:
    fingerprinted(monkeypatch, REPO)
    registry = WorkspaceRegistry(store)
    olds = [directory(tmp_path, "a"), directory(tmp_path, "b")]
    ids = {(await registry.find_or_register(str(old))).workspace_id for old in olds}
    for old in olds:
        shutil.rmtree(old)
    new = directory(tmp_path, "c")

    registered = await registry.find_or_register(str(new))

    assert registered.workspace_id not in ids
    named = ", ".join(str(old.resolve()) for old in olds)
    assert registered.note == f"{new.resolve()} looks like {named}; started it as a new workspace."


async def test_a_gone_workspace_of_another_repository_isnt_taken_over(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, tmp_path: Path
) -> None:
    fingerprinted(monkeypatch, REPO)
    old = directory(tmp_path, "a")
    registry = WorkspaceRegistry(store)
    first = await registry.find_or_register(str(old))
    old.rmdir()
    fingerprinted(monkeypatch, f"{'b' * 40} git@github.com:o/r.git")
    second = await registry.find_or_register(str(directory(tmp_path, "b")))
    assert second.workspace_id != first.workspace_id and second.note is None


async def test_a_path_without_a_fingerprint_never_takes_over_a_workspace(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStore, tmp_path: Path
) -> None:
    fingerprinted(monkeypatch, None)
    old = directory(tmp_path, "a")
    registry = WorkspaceRegistry(store)
    first = await registry.find_or_register(str(old))
    old.rmdir()
    second = await registry.find_or_register(str(directory(tmp_path, "b")))
    assert second.workspace_id != first.workspace_id and second.note is None


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
    with pytest.raises(StoreError, match=f"Couldn't find the latest added workspace in {database}"):
        await registry.latest_added()
    with pytest.raises(StoreError, match=f"Couldn't mark the workspace w managed in {database}"):
        await registry.set_managed("w")
    with pytest.raises(StoreError, match=f"Couldn't look up the workspace w in {database}"):
        await registry.path("w")
