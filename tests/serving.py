"""A control plane serving the workspaces frontends add, over a real store and git, as ``orbit
serve`` does, for the tests that clone and purge them."""

from contextlib import ExitStack
from pathlib import Path

from orbit.agent import ControlPlane, SqliteStore, StorageSettings
from orbit.agent.application.connection import SessionConnection
from orbit.cli.backend import _Backend, _UserConfig
from orbit.cli.served import _workspaces
from orbit.protocol import Event
from tests.connection_helpers import taken
from tests.plane_fakes import payloads
from tests.waiting import eventually

FLAGS = _Backend(None, None, False, False, False, False, new=False, name=None)
NO_USER_CONFIG = _UserConfig(Path("/nonexistent/config"), StorageSettings())
"""The user config of a server without one."""


def serving(stack: ExitStack, store: SqliteStore, clones: Path) -> ControlPlane:
    """A control plane serving no workspace yet, cloning into ``clones``."""
    return ControlPlane(workspaces=_workspaces(stack, FLAGS, store, NO_USER_CONFIG, clones))


async def until(connection: SessionConnection, kind: type[Event]) -> list[Event]:
    """What's queued for ``connection`` up to the first event of ``kind``."""
    events: list[Event] = []

    def arrived() -> bool:
        events.extend(payloads(taken(connection)))
        return any(isinstance(event, kind) for event in events)

    await eventually(arrived)
    return events
