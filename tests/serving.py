"""A control plane serving the workspaces frontends add, over a real store and git, as ``orbit
serve`` does, for the tests that clone and purge them."""

import asyncio
from contextlib import ExitStack
from pathlib import Path

from orbit.agent import ControlPlane, SqliteStore, StorageSettings
from orbit.agent.application.connection import SessionConnection
from orbit.cli.backend import _Backend
from orbit.cli.served import _workspaces
from orbit.protocol import Event
from tests.plane_fakes import payloads, taken

FLAGS = _Backend(None, None, False, False, False, False, new=False, name=None)


def serving(stack: ExitStack, store: SqliteStore, clones: Path) -> ControlPlane:
    """A control plane serving no workspace yet, cloning into ``clones``."""
    return ControlPlane(workspaces=_workspaces(stack, FLAGS, store, StorageSettings(), clones))


async def until(connection: SessionConnection, kind: type[Event]) -> list[Event]:
    """What's queued for ``connection`` up to the first event of ``kind``."""
    events: list[Event] = []
    while not any(isinstance(event, kind) for event in events):
        await asyncio.sleep(0.01)
        events += payloads(taken(connection))
    return events
