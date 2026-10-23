"""``orbit db forget``: purging a workspace no running server serves and no orbit holds the
lock of, saying what it deleted."""

import asyncio
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orbit.agent import RunFile, SqliteStore, WorkspaceLock, WorkspaceRegistry, orbit_dir
from orbit.agent.external import workspace_purge
from orbit.agent.external.clone_loss import clone_loss
from orbit.cli import app
from tests.agent_fakes import notice
from tests.git_repo import bare_repository, commit_file, git, init_repo
from tests.store_views import record

runner = CliRunner()


async def _used(state: Path, path: Path, sessions: int, *, managed: bool, served: bool) -> str:
    store = await SqliteStore.open(state)
    try:
        registry = WorkspaceRegistry(store)
        workspace_id = (await registry.find_or_register(str(path))).workspace_id
        if managed:
            await registry.set_managed(workspace_id)
        await registry.set_served(workspace_id, served)
        for number in range(sessions):
            await store.save(workspace_id, record(f"s{number}"))
            await store.append(workspace_id, f"s{number}", [notice(number + 1, f"s{number}", "")])
    finally:
        await store.close()
    return workspace_id


def used(
    state: Path, path: Path, sessions: int = 1, *, managed: bool = False, served: bool = False
) -> str:
    """The id of the workspace at ``path``, registered in the store in ``state`` with
    ``sessions`` sessions, each with an event, managed and flagged served as asked."""
    path.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_used(state, path, sessions, managed=managed, served=served))


def registered(state: Path) -> list[str]:
    """The ids of the workspaces registered in the store in ``state``."""
    with closing(sqlite3.connect(state / "orbit.db")) as connection:
        return [str(row[0]) for row in connection.execute("SELECT id FROM workspaces")]


def cloned(tmp_path: Path) -> Path:
    """A clone, with its own git identity, of a bare repository with one commit."""
    clone = tmp_path / "clones" / "proj"
    git(tmp_path, "clone", "-q", str(bare_repository(tmp_path)), str(clone))
    git(clone, "config", "user.name", "Test")
    git(clone, "config", "user.email", "test@example.com")
    return clone


def test_a_workspace_only_local_chats_used_is_purged_keeping_its_directory(
    state: Path, tmp_path: Path
) -> None:
    kept = used(state, tmp_path / "other")
    used(state, tmp_path / "project", 2)
    fallback = state / "fallback"

    result = runner.invoke(app, ["db", "forget", str(tmp_path / "project")])

    assert result.exit_code == 0, result.output
    message = f"Forgot {tmp_path / 'project'}: deleted 2 sessions and 0 fallback files.\n"
    assert result.stdout == message
    assert registered(state) == [kept] and (tmp_path / "project").is_dir()
    assert not fallback.exists() or list(fallback.iterdir()) == []


def test_a_workspace_is_found_by_its_id_and_its_fallback_files_go(
    state: Path, tmp_path: Path
) -> None:
    workspace_id = used(state, tmp_path / "project")
    fallback = state / "fallback" / f"{workspace_id}-s0.jsonl"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("")

    result = runner.invoke(app, ["db", "forget", workspace_id, "--state-dir", str(state)])

    message = f"Forgot {tmp_path / 'project'}: deleted 1 session and 1 fallback file.\n"
    assert result.exit_code == 0 and result.stdout == message
    assert registered(state) == [] and not fallback.exists()


def test_a_workspace_whose_directory_is_gone_is_purged(state: Path, tmp_path: Path) -> None:
    used(state, tmp_path / "project")
    (tmp_path / "project").rmdir()

    result = runner.invoke(app, ["db", "forget", str(tmp_path / "project")])

    assert result.exit_code == 0 and registered(state) == []


def test_a_workspace_not_registered_is_refused(state: Path, tmp_path: Path) -> None:
    used(state, tmp_path / "other")
    result = runner.invoke(app, ["db", "forget", str(tmp_path / "project")])

    assert result.exit_code == 2
    assert result.stderr == f"No workspace is registered at {tmp_path / 'project'}.\n"


def test_a_workspace_whose_lock_an_orbit_holds_is_refused(state: Path, tmp_path: Path) -> None:
    workspace_id = used(state, tmp_path / "project")
    lock = WorkspaceLock(orbit_dir(str(tmp_path / "project")))
    assert lock.take() is None
    try:
        result = runner.invoke(app, ["db", "forget", str(tmp_path / "project")])
    finally:
        lock.close()

    assert result.exit_code == 2
    message = f"Can't forget {tmp_path / 'project'}: an orbit chat, pid {os.getpid()} holds its"
    assert result.stderr == f"{message} lock.\n"
    assert registered(state) == [workspace_id]


def test_a_workspace_a_running_server_serves_is_refused(state: Path, tmp_path: Path) -> None:
    workspace_id = used(state, tmp_path / "project", served=True)
    RunFile(state).write("wss://orbit.example:4567/", os.getpid())

    result = runner.invoke(app, ["db", "forget", str(tmp_path / "project")])

    assert result.exit_code == 2
    assert result.stderr == (
        f"Can't forget {tmp_path / 'project'}: the orbit serve at wss://orbit.example:4567/ serves"
        " it; remove it there with its sessions, or stop that server first.\n"
    )
    assert registered(state) == [workspace_id]


def test_a_workspace_flagged_served_with_no_server_running_is_purged(
    state: Path, tmp_path: Path
) -> None:
    used(state, tmp_path / "project", served=True)

    result = runner.invoke(app, ["db", "forget", str(tmp_path / "project")])

    assert result.exit_code == 0 and registered(state) == []


def test_a_clean_clone_is_purged_with_its_directory(state: Path, tmp_path: Path) -> None:
    clone = cloned(tmp_path)
    used(state, clone, 0, managed=True)

    result = runner.invoke(app, ["db", "forget", str(clone)])

    message = f"Forgot {clone}: deleted 0 sessions, 0 fallback files and its clone {clone}.\n"
    assert result.exit_code == 0 and result.stdout == message
    assert not clone.exists()


def test_a_clone_holding_work_is_refused_listing_it(state: Path, tmp_path: Path) -> None:
    clone = cloned(tmp_path)
    used(state, clone, managed=True)
    commit_file(clone, "b.txt", "new\n", "more")

    result = runner.invoke(app, ["db", "forget", str(clone)])

    assert result.exit_code == 2
    assert result.stderr == (
        f"Can't purge {clone}: deleting its clone would lose 1 commit on main not on its remote.\n"
    )
    assert clone.is_dir()


def test_a_directory_in_a_registered_repository_purges_that_workspace(
    state: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    used(state, repo)
    (repo / "src").mkdir()

    result = runner.invoke(app, ["db", "forget", str(repo / "src")])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"Forgot {repo}: deleted 1 session and 0 fallback files.\n"
    assert registered(state) == []


def test_a_clone_is_checked_for_work_it_would_lose_once(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = cloned(tmp_path)
    used(state, clone, managed=True)
    checked: list[Path] = []

    async def counted(path: Path) -> list[str]:
        checked.append(path)
        return await clone_loss(path)

    monkeypatch.setattr(workspace_purge, "clone_loss", counted)

    result = runner.invoke(app, ["db", "forget", str(clone)])

    assert result.exit_code == 0 and checked == [clone]
