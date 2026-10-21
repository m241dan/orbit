"""The workspaces ``orbit serve`` serves as frontends add and remove them: each found or
registered in the store, flagged served and locked while it's served."""

import re
import subprocess
import threading
from collections.abc import AsyncIterator
from contextlib import ExitStack
from pathlib import Path

import pytest

from orbit.agent import (
    OrbitConfig,
    PathKind,
    SqliteStore,
    StorageSettings,
    WorkspaceLock,
    WorkspaceRegistry,
    WorkspaceUnavailable,
    read_config,
)
from orbit.cli import backend
from orbit.cli.backend import _Backend, _Workspaces

FLAGS = _Backend(None, None, False, False, False, False, new=False, name=None)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    opened = await SqliteStore.open(tmp_path / "state")
    yield opened
    await opened.close()


@pytest.fixture
def stack() -> ExitStack:
    return ExitStack()


def workspaces(stack: ExitStack, store: SqliteStore, flags: _Backend = FLAGS) -> _Workspaces:
    return _Workspaces(stack, flags, store, StorageSettings())


def directory(tmp_path: Path, name: str = "repo") -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def free(root: Path) -> bool:
    """Whether no orbit holds the workspace lock on the repository at ``root``."""
    lock = WorkspaceLock(root / ".orbit")
    owner = lock.take()
    lock.close()
    return owner is None


async def test_a_directory_a_file_and_nothing_are_told_apart(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    source = workspaces(stack, store)
    (tmp_path / "notes.txt").write_text("")

    assert await source.kind(str(directory(tmp_path))) == PathKind.DIRECTORY
    assert await source.kind(str(tmp_path / "notes.txt")) == PathKind.FILE
    assert await source.kind(str(tmp_path / "gone")) == PathKind.MISSING


async def test_a_directory_inside_a_repository_stands_for_its_top_level(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "docs").mkdir()
    source = workspaces(stack, store)

    assert await source.top_level(str(root / "docs")) == str(root.resolve())
    assert await source.top_level(str(tmp_path)) == str(tmp_path.resolve())


async def test_opening_a_workspace_flags_it_served_and_locks_it_until_closed(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    source = workspaces(stack, store)
    source.url = "ws://127.0.0.1:7433/"

    host = await source.open(str(root))
    registry = WorkspaceRegistry(store)
    served = await registry.served()
    owner = WorkspaceLock(root / ".orbit").owner()
    await host.close()
    await source.close(host.workspace_id)

    assert served == [(host.workspace_id, str(root))]
    assert owner.url == "ws://127.0.0.1:7433/" and not host.read_only
    assert await registry.served() == [] and free(root)
    assert source.locks == ()


async def test_a_workspace_another_orbit_owns_opens_read_only(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    other = WorkspaceLock(root / ".orbit")
    other.take()
    source = workspaces(stack, store)

    host = await source.open(str(root))
    await host.close()
    other.close()

    assert host.read_only


async def test_the_locks_still_held_are_let_go_as_the_stack_closes(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    source = workspaces(stack, store)
    host = await source.open(str(root))
    await host.close()
    held = not free(root)

    stack.close()

    assert held and free(root)


async def test_an_invalid_configuration_is_unavailable_saying_why(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    (root / ".orbit").mkdir()
    (root / ".orbit" / "config.toml").write_text("roles = 1\n")

    with pytest.raises(WorkspaceUnavailable, match=r"^Invalid orbit configuration: "):
        await workspaces(stack, store).open(str(root))
    assert free(root)


async def test_a_repository_git_has_no_identity_for_is_unavailable_with_a_worktree(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.setattr(backend, "has_git_identity", lambda path: False)
    flags = _Backend(None, None, False, True, False, False, new=False, name=None)

    with pytest.raises(
        WorkspaceUnavailable, match=f"^{re.escape(f'Git has no identity for commits in {root}')}"
    ):
        await workspaces(stack, store, flags).open(str(root))


async def test_a_lock_that_cant_be_made_is_unavailable_naming_it(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    (root / ".orbit").write_text("")

    with pytest.raises(WorkspaceUnavailable, match=r"^Couldn't lock the repository with "):
        await workspaces(stack, store).open(str(root))


async def test_a_store_that_fails_is_unavailable_and_lets_the_lock_go(
    stack: ExitStack, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    store = await SqliteStore.open(tmp_path / "state")
    await store.close()

    with pytest.raises(WorkspaceUnavailable):
        await workspaces(stack, store).open(str(root))
    assert free(root)


async def test_a_failure_opening_the_store_lets_the_lock_go(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)

    async def fails(store: SqliteStore, root: Path, *, owned: bool) -> None:
        raise RuntimeError("A bug.")

    monkeypatch.setattr(backend, "open_workspace_store", fails)

    with pytest.raises(RuntimeError, match="A bug"):
        await workspaces(stack, store).open(str(root))
    assert free(root)


async def test_the_configuration_is_read_off_the_event_loop(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    threads: list[threading.Thread] = []

    def recorded(path: str) -> OrbitConfig:
        threads.append(threading.current_thread())
        return read_config(path)

    monkeypatch.setattr(backend, "read_config", recorded)

    host = await workspaces(stack, store).open(str(root))
    await host.close()

    assert threads and threads[0] is not threading.main_thread()


async def test_a_failure_making_the_host_lets_the_lock_go(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)

    def fails(*args: object, **kwargs: object) -> None:
        raise RuntimeError("A bug.")

    monkeypatch.setattr(backend, "_agents", fails)

    with pytest.raises(RuntimeError, match="A bug"):
        await workspaces(stack, store).open(str(root))
    assert free(root) and await WorkspaceRegistry(store).served() == []
