"""A server cloning the repositories frontends add by URL, with git itself, into its clone
directory, then serving each clone, flagged served and managed."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import ExitStack
from pathlib import Path

import pytest

from orbit.agent import SqliteStore, WorkspaceRegistry
from orbit.protocol import (
    BUSY,
    GIT_FAILED,
    AddWorkspace,
    Catalog,
    CommandAccepted,
    ErrorOccurred,
    WorkspaceCloning,
)
from tests.git_repo import bare_repository, started
from tests.plane_fakes import payloads, refused, say, taken, welcomed
from tests.process_checks import gone
from tests.serving import serving, until


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    opened = await SqliteStore.open(tmp_path / "state")
    yield opened
    await opened.close()


@pytest.fixture
def stack() -> ExitStack:
    return ExitStack()


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
    events = await asyncio.wait_for(until(connection, Catalog), 30)
    await plane.close()
    stack.close()

    assert accepted == [CommandAccepted(command_id="add")]
    assert any(isinstance(event, WorkspaceCloning) for event in events)
    catalog = events[-1]
    assert isinstance(catalog, Catalog)
    [entry] = catalog.workspaces
    assert entry.path == str(clones / "proj")
    assert await WorkspaceRegistry(store).served() == [(entry.id, str(clones / "proj"))]
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

    catalog = events[-1]
    assert isinstance(catalog, Catalog)
    paths = sorted(str(entry.path) for entry in catalog.workspaces)
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
