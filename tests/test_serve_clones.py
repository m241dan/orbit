"""A server cloning the repositories frontends add by URL, with git itself, into its clone
directory, then serving each clone, flagged served and managed."""

import asyncio
import shutil
import sqlite3
from contextlib import ExitStack
from pathlib import Path

from orbit.agent import SqliteStore, WorkspaceRegistry
from orbit.agent.application.connection import SessionConnection
from orbit.protocol import (
    BUSY,
    GIT_FAILED,
    INVALID,
    AddWorkspace,
    Catalog,
    CommandAccepted,
    ErrorOccurred,
    Event,
    WorkspaceCloning,
)
from tests.connection_helpers import say, taken
from tests.git_repo import bare_repository, commit_file, git, init_repo, started
from tests.plane_fakes import payloads, refused, welcomed
from tests.process_checks import gone
from tests.serving import serving, until
from tests.waiting import eventually


async def served(connection: SessionConnection) -> list[Event]:
    """What's queued for ``connection`` up to a clone's last news, once it's served."""
    events: list[Event] = []

    def arrived() -> bool:
        events.extend(payloads(taken(connection)))
        return any(isinstance(event, WorkspaceCloning) and event.workspace for event in events)

    await eventually(arrived)
    return events


def last_catalog(events: list[Event]) -> Catalog:
    """The last of ``events`` that's a ``Catalog``."""
    return [event for event in events if isinstance(event, Catalog)][-1]


def managed(connection: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = connection.execute("SELECT path, managed FROM workspaces ORDER BY path")
    return [(str(path), int(flag)) for path, flag in rows]


async def test_a_url_is_cloned_then_served_flagged_and_managed(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    url = bare_repository(tmp_path).as_uri()
    clones = tmp_path / "clones"
    plane = serving(stack, store, clones)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(url=url))
    accepted = payloads(taken(connection))
    events = await asyncio.wait_for(served(connection), 30)
    await plane.close()
    stack.close()

    assert accepted == [CommandAccepted(command_id="add")]
    assert any(isinstance(event, WorkspaceCloning) for event in events)
    [entry] = last_catalog(events).workspaces
    done = events[-1]
    assert isinstance(done, WorkspaceCloning)
    assert (done.url, done.name, done.workspace) == (url, "proj", entry.id)
    assert entry.path == str(clones / "proj") and entry.managed
    assert await WorkspaceRegistry(store).served() == [(entry.id, str(clones / "proj"))]
    assert await store.run("Couldn't list", managed) == [(str(clones / "proj"), 1)]


async def test_a_workspace_is_listed_managed_as_its_registry_entry_says(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    clone, plain = init_repo(tmp_path / "clone"), init_repo(tmp_path / "plain")
    registry = WorkspaceRegistry(store)
    await registry.set_managed((await registry.find_or_register(str(clone))).workspace_id)
    plane = serving(stack, store, tmp_path / "clones")
    connection = await welcomed(plane)

    await say(connection, "clone", AddWorkspace(path=str(clone)))
    await say(connection, "plain", AddWorkspace(path=str(plain)))
    events = payloads(taken(connection))
    await plane.close()
    stack.close()

    catalog = [event for event in events if isinstance(event, Catalog)][-1]
    managed_by_path = {entry.path: entry.managed for entry in catalog.workspaces}
    assert managed_by_path == {str(clone): True, str(plain): False}


async def test_a_clone_that_re_attaches_to_a_gone_managed_workspace_stays_managed(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    url = bare_repository(tmp_path).as_uri()
    gone_clone = tmp_path / "gone" / "proj"
    git(tmp_path, "clone", "-q", url, str(gone_clone))
    registry = WorkspaceRegistry(store)
    workspace_id = (await registry.find_or_register(str(gone_clone))).workspace_id
    await registry.set_managed(workspace_id)
    shutil.rmtree(gone_clone)
    clones = tmp_path / "clones"
    plane = serving(stack, store, clones)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(url=url))
    events = await asyncio.wait_for(until(connection, Catalog), 30)
    await plane.close()
    stack.close()

    [entry] = last_catalog(events).workspaces
    assert (entry.id, entry.path, entry.managed) == (workspace_id, str(clones / "proj"), True)
    assert await store.run("Couldn't list", managed) == [(str(clones / "proj"), 1)]


async def test_a_second_clone_of_a_name_taken_lands_beside_it(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    url = bare_repository(tmp_path).as_uri()
    clones = tmp_path / "clones"
    plane = serving(stack, store, clones)
    connection = await welcomed(plane)

    await say(connection, "first", AddWorkspace(url=url))
    await asyncio.wait_for(until(connection, Catalog), 30)
    await say(connection, "second", AddWorkspace(url=url))
    events = await asyncio.wait_for(until(connection, Catalog), 30)
    await plane.close()
    stack.close()

    paths = sorted(str(entry.path) for entry in last_catalog(events).workspaces)
    assert paths == [str(clones / "proj"), str(clones / "proj-2")]


async def test_a_url_that_fails_is_told_as_git_failed_leaving_no_directory(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    url = (tmp_path / "nothing.git").as_uri()
    clones = tmp_path / "clones"
    plane = serving(stack, store, clones)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(url=url))
    events = await asyncio.wait_for(until(connection, ErrorOccurred), 30)
    await plane.close()
    stack.close()

    error = events[-1]
    assert isinstance(error, ErrorOccurred) and error.code == GIT_FAILED
    assert error.message.startswith(f"Couldn't clone {url}: ")
    assert list(clones.iterdir()) == []


async def test_a_clone_that_cant_be_served_is_removed(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    source = init_repo(tmp_path / "source")
    (source / ".orbit").mkdir()
    commit_file(source, ".orbit/config.toml", "roles = 1\n", "configure")
    git(tmp_path, "clone", "-q", "--bare", str(source), str(tmp_path / "proj.git"))
    clones = tmp_path / "clones"
    plane = serving(stack, store, clones)
    connection = await welcomed(plane)

    url = (tmp_path / "proj.git").as_uri()
    await say(connection, "add", AddWorkspace(url=url))
    events = await asyncio.wait_for(until(connection, ErrorOccurred), 30)
    await plane.close()
    stack.close()

    error = events[-1]
    assert isinstance(error, ErrorOccurred) and error.code == INVALID
    assert error.message.startswith(f"Couldn't clone {url}: Can't add {clones / 'proj'}: ")
    assert list(clones.iterdir()) == []


async def test_the_same_url_twice_at_once_is_refused_as_busy(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, slow_ssh: Path
) -> None:
    url = "ssh://git@example.invalid/o/proj.git"
    plane = serving(stack, store, tmp_path / "clones")
    connection = await welcomed(plane)

    await say(connection, "first", AddWorkspace(url=url))
    await asyncio.wait_for(started(slow_ssh), 30)
    await say(connection, "second", AddWorkspace(url=url))
    outcomes = payloads(taken(connection))
    await plane.close()
    stack.close()

    message = f"Can't clone {url}: it's being cloned already."
    assert outcomes[-1] == refused("second", BUSY, message)


async def test_closing_during_a_clone_leaves_no_git_and_no_directory(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, slow_ssh: Path
) -> None:
    clones = tmp_path / "clones"
    plane = serving(stack, store, clones)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(url="ssh://git@example.invalid/o/proj.git"))
    pid = await asyncio.wait_for(started(slow_ssh), 30)
    await asyncio.wait_for(plane.close(), 30)
    stack.close()

    assert await gone(pid)
    assert list(clones.iterdir()) == []
