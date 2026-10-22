"""A workspace's view of a SQLite store of its own, as one orbit serving one workspace has."""

import sqlite3
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbit.agent.domain.sessions import SessionRecord
from orbit.agent.external.sqlite_store import THREAD_PREFIX, SqliteStore, WorkspaceStore
from orbit.agent.external.workspace_registry import WorkspaceRegistry
from orbit.protocol import EventEnvelope
from tests.agent_fakes import notice


class OwnView(WorkspaceStore):
    """The view of the workspace ``workspace_id`` in ``store``, which is its own: closing the
    view closes the store."""

    def __init__(self, store: SqliteStore, workspace_id: str) -> None:
        super().__init__(store, workspace_id)
        self.store = store

    async def close(self) -> None:
        await self.store.close()


async def open_view(state: Path, workspace: Path | str) -> OwnView:
    """The view of the workspace at ``workspace``, registered if it isn't yet, in a store of its
    own in ``state``."""
    store = await SqliteStore.open(state)
    try:
        workspace_id = await WorkspaceRegistry(store).find_or_register(str(workspace))
    except BaseException:
        await store.close()
        raise
    return OwnView(store, workspace_id)


async def workspace_id_of(state: Path, workspace: Path | str) -> str:
    """The id of the workspace at ``workspace`` in a store in ``state``, registered if it isn't
    yet."""
    view = await open_view(state, workspace)
    await view.close()
    return view.workspace_id


type Opener = Callable[[Path], Awaitable[OwnView]]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An empty workspace directory, ``project``."""
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture
async def open_store(state: Path) -> AsyncIterator[Opener]:
    """Opens a store in ``state`` for a workspace, closing it after the test."""
    opened: list[OwnView] = []

    async def open_at(workspace: Path) -> OwnView:
        opened.append(await open_view(state, workspace))
        return opened[-1]

    yield open_at
    for store in opened:
        await store.close()


def big(seq: int, session: str = "abc") -> EventEnvelope:
    """An event of the session ``session`` of about 10 KB."""
    return notice(seq, session, "x" * 10_000)


WHEN = datetime(2026, 10, 17, 14, 30, tzinfo=UTC)


def record(session_id: str = "abc", name: str = "Oct 17 14:30") -> SessionRecord:
    """A session record ``session_id`` named ``name``, created and last active at ``WHEN``."""
    return SessionRecord(id=session_id, name=name, created=WHEN, last_active=WHEN)


@contextmanager
def database(state: Path) -> Iterator[sqlite3.Connection]:
    """Another connection to the store's database, in autocommit mode."""
    with closing(sqlite3.connect(state / "orbit.db", isolation_level=None)) as connection:
        yield connection


def store_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name.startswith(THREAD_PREFIX)]


async def fill_up(store: OwnView) -> None:
    """Have ``store``'s database refuse to grow, as a full disk does."""

    def cap() -> None:
        (pages,) = store.store._db.execute("PRAGMA page_count").fetchone()
        store.store._db.execute(f"PRAGMA max_page_count = {pages}")

    await store.store._call(cap)
