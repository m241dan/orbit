"""A server purging the workspaces frontends remove with ``purge``, over the store and git
itself: their records go, other workspaces' stay, and a clone's directory goes only when it
holds nothing its remote doesn't."""

import asyncio
from contextlib import ExitStack
from pathlib import Path

import pytest

from orbit.agent import (
    Purged,
    ServedWorkspaces,
    SqliteStore,
    StorageSettings,
    WorkspaceLock,
    WorkspaceOwner,
    WorkspaceUnavailable,
    orbit_dir,
    purge_workspace,
)
from orbit.agent.application.connection import SessionConnection
from orbit.agent.external import served_workspaces
from orbit.cli.served import _workspaces
from orbit.protocol import (
    INTERNAL,
    INVALID,
    AddWorkspace,
    Catalog,
    CommandAccepted,
    Event,
    RemoveWorkspace,
    WorkspaceEntry,
)
from tests.agent_fakes import notice
from tests.connection_helpers import say, taken
from tests.git_repo import bare_repository, commit_file, git, init_repo
from tests.plane_fakes import payloads, refused, welcomed
from tests.serving import FLAGS, serving, until
from tests.store_views import database, record


@pytest.fixture
def stack() -> ExitStack:
    return ExitStack()


def served_entries(events: list[Event]) -> tuple[WorkspaceEntry, ...]:
    """The workspaces the last catalog in ``events`` lists."""
    catalog = [event for event in events if isinstance(event, Catalog)][-1]
    return catalog.workspaces


def served(events: list[Event]) -> dict[str, str]:
    """The id of each workspace the last catalog in ``events`` lists, by its path."""
    return {str(entry.path): entry.id for entry in served_entries(events)}


async def added(connection: SessionConnection, path: Path) -> dict[str, str]:
    """Add the workspace at ``path`` from ``connection``; the workspaces served then."""
    await say(connection, f"add {path.name}", AddWorkspace(path=str(path)))
    return served(payloads(taken(connection)))


async def saved(store: SqliteStore, workspace_id: str, seq: int) -> None:
    """Save a session, ``abc``, of the workspace ``workspace_id`` with an event, ``seq``."""
    await store.save(workspace_id, record())
    await store.append(workspace_id, "abc", [notice(seq, "abc", "hi")])


def entry_of(events: list[Event], path: Path) -> WorkspaceEntry:
    """The entry of the workspace at ``path`` in the last catalog in ``events``."""
    [entry] = [entry for entry in served_entries(events) if entry.path == str(path)]
    return entry


def counts(state: Path, workspace_id: str) -> tuple[int, int, int]:
    """How many sessions, events and registry rows the workspace ``workspace_id`` has."""
    with database(state) as connection:
        return tuple(
            connection.execute(query, (workspace_id,)).fetchone()[0]
            for query in (
                "SELECT count(*) FROM sessions WHERE workspace_id = ?",
                "SELECT count(*) FROM events WHERE workspace_id = ?",
                "SELECT count(*) FROM workspaces WHERE id = ?",
            )
        )


async def test_purging_a_workspace_deletes_its_records_and_keeps_its_directory(
    stack: ExitStack, store: SqliteStore, state: Path, tmp_path: Path
) -> None:
    first, second = init_repo(tmp_path / "a"), init_repo(tmp_path / "b")
    plane = serving(stack, store, tmp_path / "clones")
    connection = await welcomed(plane)
    await added(connection, first)
    ids = await added(connection, second)
    a, b = ids[str(first)], ids[str(second)]
    await saved(store, a, 1)
    await saved(store, b, 2)
    kept = counts(state, a)

    await say(connection, "purge", RemoveWorkspace(workspace=b, purge=True))
    outcome = payloads(taken(connection))[-1]
    purged = counts(state, b)
    await say(connection, "again", AddWorkspace(path=str(second)))
    entry = entry_of(payloads(taken(connection)), second)
    await plane.close()
    stack.close()

    assert outcome == CommandAccepted(command_id="purge")
    assert purged == (0, 0, 0) and counts(state, a) == kept == (1, 1, 1)
    assert entry.id != b
    assert "abc" not in [session.id for session in entry.sessions]
    assert second.is_dir()
    assert not (state / "locks" / b).exists()


async def test_a_clone_is_purged_only_once_its_commits_are_pushed(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    clones = tmp_path / "clones"
    plane = serving(stack, store, clones)
    connection = await welcomed(plane)
    await say(connection, "add", AddWorkspace(url=bare_repository(tmp_path).as_uri()))
    clone_id = served(await asyncio.wait_for(until(connection, Catalog), 30))[str(clones / "proj")]
    git(clones / "proj", "config", "user.name", "Test")
    git(clones / "proj", "config", "user.email", "test@example.com")
    commit_file(clones / "proj", "b.txt", "new\n", "more")
    purge = RemoveWorkspace(workspace=clone_id, purge=True)

    await say(connection, "refused", purge)
    refusal = payloads(taken(connection))
    kept = (clones / "proj").is_dir()
    git(clones / "proj", "push", "-q", "origin", "main")
    await say(connection, "purged", purge)
    outcome = payloads(taken(connection))[-1]
    await plane.close()
    stack.close()

    message = (
        f"Can't purge {clones / 'proj'}: deleting its clone would lose 1 commit on main not on its"
        " remote."
    )
    assert refusal == [refused("refused", INVALID, message)] and kept
    assert outcome == CommandAccepted(command_id="purged")
    assert not (clones / "proj").exists()


async def test_another_orbit_cant_take_a_workspace_lock_while_its_purge_runs(
    stack: ExitStack,
    store: SqliteStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = init_repo(tmp_path / "a")
    owners: list[WorkspaceOwner | None] = []

    async def purging(store: SqliteStore, workspace_id: str) -> Purged:
        lock = WorkspaceLock(orbit_dir(str(root)))
        owners.append(lock.take())
        lock.close()
        return await purge_workspace(store, workspace_id)

    monkeypatch.setattr(served_workspaces, "purge_workspace", purging)
    plane = serving(stack, store, tmp_path / "clones")
    connection = await welcomed(plane)
    workspace_id = (await added(connection, root))[str(root)]

    await say(connection, "purge", RemoveWorkspace(workspace=workspace_id, purge=True))
    outcome = payloads(taken(connection))[-1]
    after = WorkspaceLock(orbit_dir(str(root)))
    free = after.take()
    after.close()
    await plane.close()
    stack.close()

    assert outcome == CommandAccepted(command_id="purge")
    assert len(owners) == 1 and owners[0] is not None
    assert free is None


def source(stack: ExitStack, store: SqliteStore, tmp_path: Path) -> ServedWorkspaces:
    """The workspaces a server serves from ``store``, cloning into ``clones`` in ``tmp_path``."""
    return _workspaces(stack, FLAGS, store, StorageSettings(), tmp_path / "clones")


async def test_a_workspace_not_registered_would_lose_nothing_and_is_refused_purging(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    workspaces = source(stack, store, tmp_path)

    assert await workspaces.losses("nope") == []
    with pytest.raises(WorkspaceUnavailable) as refusal:
        await workspaces.purge("nope")
    assert (refusal.value.code, str(refusal.value)) == (INVALID, "No workspace nope is registered.")


async def test_a_store_that_fails_leaves_losses_and_purging_unavailable_as_internal(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    workspaces = source(stack, store, tmp_path)
    await store.close()

    for attempt in (workspaces.losses("w"), workspaces.purge("w")):
        with pytest.raises(WorkspaceUnavailable) as failure:
            await attempt
        assert failure.value.code == INTERNAL
