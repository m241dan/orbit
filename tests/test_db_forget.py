"""``orbit db forget``: purging a workspace, as :func:`forget_workspace` does, saying what it
deleted, or why it's refused."""

import asyncio
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orbit.agent import SqliteStore, WorkspaceRegistry
from orbit.agent.external import workspace_purge
from orbit.agent.external.clone_loss import clone_loss
from orbit.cli import app
from tests.agent_fakes import notice
from tests.git_repo import (
    bare_repository,
    commit_file,
    git,
    init_repo,
    rmtree_stopping_partway,
)
from tests.store_views import record

runner = CliRunner()


async def _used(state: Path, path: Path, sessions: int, *, managed: bool) -> str:
    store = await SqliteStore.open(state)
    try:
        registry = WorkspaceRegistry(store)
        workspace_id = (await registry.find_or_register(str(path))).workspace_id
        if managed:
            await registry.set_managed(workspace_id)
        for number in range(sessions):
            await store.save(workspace_id, record(f"s{number}"))
            await store.append(workspace_id, f"s{number}", [notice(number + 1, f"s{number}", "")])
    finally:
        await store.close()
    return workspace_id


def used(state: Path, path: Path, sessions: int = 1, *, managed: bool = False) -> str:
    """The id of the workspace at ``path``, registered in the store in ``state`` with
    ``sessions`` sessions, each with an event, managed if asked."""
    path.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_used(state, path, sessions, managed=managed))


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


def test_a_clean_clone_is_purged_with_its_directory(state: Path, tmp_path: Path) -> None:
    clone = cloned(tmp_path)
    used(state, clone, 0, managed=True)

    result = runner.invoke(app, ["db", "forget", str(clone)])

    message = f"Forgot {clone}: deleted 0 sessions, 0 fallback files and its clone {clone}.\n"
    assert result.exit_code == 0 and result.stdout == message
    assert list(clone.parent.iterdir()) == []


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


def test_a_clone_whose_coder_tree_holds_work_is_refused(state: Path, tmp_path: Path) -> None:
    clone = cloned(tmp_path)
    used(state, clone, managed=True)
    coder = clone / ".git" / "orbit" / "coder"
    git(clone, "worktree", "add", "-q", "--detach", str(coder))
    (coder / "b.txt").write_text("new\n")

    result = runner.invoke(app, ["db", "forget", str(clone)])

    assert result.exit_code == 2
    assert result.stderr == (
        f"Can't purge {clone}: deleting its clone would lose uncommitted changes to 1 file.\n"
    )
    assert (coder / "b.txt").is_file()


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


def test_a_clone_that_cant_be_deleted_is_forgotten_by_forgetting_it_again(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = cloned(tmp_path)
    workspace_id = used(state, clone, managed=True)

    def refused(path: Path) -> None:
        raise PermissionError(f"Permission denied: {path}")

    with monkeypatch.context() as patched:
        patched.setattr(workspace_purge, "_remove_clone", refused)
        failed = runner.invoke(app, ["db", "forget", str(clone)])
    assert (failed.exit_code, failed.stderr) == (2, f"Permission denied: {clone}\n")
    assert registered(state) == [workspace_id] and clone.is_dir()

    result = runner.invoke(app, ["db", "forget", str(clone)])

    message = f"Forgot {clone}: deleted 1 session, 0 fallback files and its clone {clone}.\n"
    assert result.exit_code == 0 and result.stdout == message
    assert registered(state) == [] and not clone.exists()


def test_a_clone_whose_deletion_stops_partway_is_forgotten_by_forgetting_it_again(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = cloned(tmp_path)
    workspace_id = used(state, clone, managed=True)

    with monkeypatch.context() as patched:
        patched.setattr(shutil, "rmtree", rmtree_stopping_partway)
        failed = runner.invoke(app, ["db", "forget", str(clone)])
    assert (failed.exit_code, failed.stderr) == (2, "Permission denied\n")
    assert registered(state) == [workspace_id]

    result = runner.invoke(app, ["db", "forget", str(clone)])

    message = f"Forgot {clone}: deleted 1 session, 0 fallback files and its clone {clone}.\n"
    assert result.exit_code == 0 and result.stdout == message
    assert registered(state) == [] and list(clone.parent.iterdir()) == []
