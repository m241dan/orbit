"""Restoring the workspaces an earlier server flagged served, in the backend alone: one whose
directory is gone or that can't be served is skipped with a warning, one served already isn't
served twice, and a store failing as the flags are read leaves every flag as it was."""

import asyncio
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from orbit.agent import (
    Cloner,
    Configuring,
    OpenedStore,
    OrbitConfig,
    Restored,
    ServedWorkspace,
    ServedWorkspaces,
    SqliteStore,
    StoreError,
    WorkspaceHost,
    WorkspaceRegistry,
    WorkspaceUnavailable,
)
from orbit.protocol import INTERNAL
from tests.store_views import database


def build_no_host(
    workspace: ServedWorkspace, config: OrbitConfig
) -> Callable[[OpenedStore], WorkspaceHost]:
    """What would build the host of ``workspace``; serving it from the start builds none."""

    def build(opened: OpenedStore) -> WorkspaceHost:
        raise AssertionError(f"No host of {workspace.root} is built.")

    return build


@pytest.fixture
def opened(state: Path) -> Iterator[SqliteStore]:
    """A store in ``state``, opened and closed outside any event loop, as a server's is."""
    store = asyncio.run(SqliteStore.open(state))
    yield store
    asyncio.run(store.close())


def make_workspaces(store: SqliteStore, tmp_path: Path) -> ServedWorkspaces:
    """The workspaces a server serves from ``store``, without a user config or a worktree."""
    configuring = Configuring(tmp_path / "config", worktree=False)
    return ServedWorkspaces(
        store, build_no_host, Cloner(tmp_path / "clones"), configuring=configuring
    )


@pytest.fixture
def source(opened: SqliteStore, tmp_path: Path) -> Iterator[ServedWorkspaces]:
    """The workspaces this server serves, their locks let go after the test."""
    served = make_workspaces(opened, tmp_path)
    yield served
    served.let_go()


def make_directories(tmp_path: Path, *names: str) -> list[Path]:
    """A new directory in ``tmp_path`` for each of ``names``."""
    paths = [tmp_path / name for name in names]
    for path in paths:
        path.mkdir()
    return paths


def flag_earlier(store: SqliteStore, tmp_path: Path, *roots: Path) -> None:
    """Flag the workspaces at ``roots`` served in ``store``, as an earlier server did."""
    earlier = make_workspaces(store, tmp_path)
    for root in roots:
        earlier.served(root)
    earlier.flag_served()
    earlier.let_go()


def list_roots(restored: Restored) -> list[Path]:
    """The directory of each workspace ``restored`` serves again."""
    return [workspace.root for workspace, _ in restored.served]


def test_a_workspace_flagged_before_is_served_again(
    opened: SqliteStore, source: ServedWorkspaces, tmp_path: Path
) -> None:
    [root] = make_directories(tmp_path, "earlier")
    flag_earlier(opened, tmp_path, root)

    restored = source.flag_and_restore()

    assert list_roots(restored) == [root] and restored.warnings == ()
    assert len(source.locks) == 1


def test_a_workspace_flagged_before_whose_directory_is_gone_is_skipped_with_a_warning(
    opened: SqliteStore, source: ServedWorkspaces, tmp_path: Path
) -> None:
    fine, gone = make_directories(tmp_path, "fine", "gone")
    flag_earlier(opened, tmp_path, fine, gone)
    shutil.rmtree(gone)

    restored = source.flag_and_restore()

    assert list_roots(restored) == [fine]
    assert restored.warnings == (f"The workspace {gone} is gone, so it isn't served.",)


def test_a_workspace_flagged_before_that_cant_be_served_is_skipped_with_a_warning(
    opened: SqliteStore, source: ServedWorkspaces, tmp_path: Path
) -> None:
    broken, fine = make_directories(tmp_path, "broken", "fine")
    flag_earlier(opened, tmp_path, broken, fine)
    (broken / ".orbit").mkdir(exist_ok=True)
    (broken / ".orbit" / "config.toml").write_text("not = [toml\n")

    restored = source.flag_and_restore()

    assert list_roots(restored) == [fine]
    [warning] = restored.warnings
    assert warning.startswith(f"The workspace {broken} isn't served: Invalid orbit configuration")


def test_a_workspace_flagged_before_and_added_again_is_served_once(
    opened: SqliteStore, source: ServedWorkspaces, tmp_path: Path
) -> None:
    [root] = make_directories(tmp_path, "added")
    flag_earlier(opened, tmp_path, root)
    source.served(root)

    restored = source.flag_and_restore()

    assert restored.served == () and restored.warnings == ()
    assert len(source.locks) == 1


def test_the_workspace_added_stays_the_latest_added_over_those_flagged_before(
    opened: SqliteStore, source: ServedWorkspaces, tmp_path: Path
) -> None:
    added, earlier = make_directories(tmp_path, "added", "earlier")
    flag_earlier(opened, tmp_path, earlier)
    source.served(added)

    source.flag_and_restore()

    assert asyncio.run(source.latest_added()) == str(added)


def test_a_store_failing_as_the_flags_are_read_leaves_every_flag_as_it_was(
    opened: SqliteStore,
    source: ServedWorkspaces,
    state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, earlier = make_directories(tmp_path, "added", "earlier")
    flag_earlier(opened, tmp_path, earlier)
    source.served(added)

    async def gone(self: WorkspaceRegistry) -> list[tuple[str, str]]:
        raise StoreError("The store is gone.")

    monkeypatch.setattr(WorkspaceRegistry, "served", gone)

    with pytest.raises(WorkspaceUnavailable, match=r"^The store is gone\.$") as failure:
        source.flag_and_restore()

    assert failure.value.code == INTERNAL
    with database(state) as connection:
        rows = connection.execute("SELECT path, served FROM workspaces ORDER BY path").fetchall()
    assert rows == [(str(added), 0), (str(earlier), 1)]
