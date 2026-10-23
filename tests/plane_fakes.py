"""Fakes for a control plane serving several workspaces."""

import asyncio
from collections.abc import Callable, Sequence
from pathlib import PurePath

from orbit.agent import (
    Agents,
    CloneFailed,
    ControlPlane,
    PathKind,
    WorkspaceHost,
    WorkspaceOwner,
    WorkspaceUnavailable,
)
from orbit.agent.application.connection import SessionConnection
from orbit.agent.application.ports import RepositoryLock
from orbit.protocol import (
    INTERNAL,
    INVALID,
    BusyChanged,
    Catalog,
    CommandRefused,
    Event,
    EventEnvelope,
    TurnEndReason,
    WorkspaceCloning,
)
from tests.agent_fakes import HELLO, LOCK_RETRY, ROLES, Call, FakeHost, ScriptedLauncher
from tests.connection_helpers import say, taken


def workspace(
    name: str, *, owner: WorkspaceOwner | None = None, lock: RepositoryLock | None = None
) -> WorkspaceHost:
    """The host of the workspace ``name``, at ``/src/<name>``, its id ``name``, read-only if
    another ``owner`` owns it, trying the repository's ``lock``, if given, to take it over."""
    return WorkspaceHost(
        workspace=f"/src/{name}",
        workspace_id=name,
        new_id=lambda: f"{name}-1",
        owner=owner,
        lock=lock,
        lock_retry=LOCK_RETRY,
    )


def payloads(envelopes: Sequence[EventEnvelope]) -> list[Event]:
    return [envelope.payload for envelope in envelopes]


def working(name: str, gate: asyncio.Event, started: list[str]) -> WorkspaceHost:
    """The host of the workspace ``name`` whose task's agent notes it in ``started`` and then
    waits for ``gate`` before giving up."""

    async def waits(call: Call) -> TurnEndReason:
        started.append(name)
        await gate.wait()
        await call("give_up", reason="Done waiting.")
        return "completed"

    agent_host = FakeHost()
    launcher = ScriptedLauncher(agent_host, waits)
    agents = Agents(
        agent_host,
        launcher,
        lambda: f"{name}-token",
        roles=ROLES,
        exit_grace=0.5,
        root=f"/src/{name}",
    )
    return WorkspaceHost(
        agents=agents, workspace=f"/src/{name}", workspace_id=name, new_id=lambda: f"{name}-1"
    )


async def idle(connection: SessionConnection) -> None:
    """Wait until the work lane of ``connection``'s workspace goes idle."""
    async for envelope in connection.events():
        event = envelope.payload
        if isinstance(event, BusyChanged) and event.lane == "work" and not event.busy:
            return


class FakeSource:
    """Workspaces for a control plane to serve: each path is a directory, but for those
    ``kinds`` says otherwise, standing for itself, but for those ``tops`` names the top level
    of; noting the workspaces ``opened``, by path, those opened ``managed``, ``closed``, by id,
    and the ``flags`` set, each waiting for ``flag_gate``, if given, and failing as
    ``unflag_fails`` says, if it does, when it's cleared. Each URL ``cloning`` is cloned to
    ``/clones/<its last part>``, reporting one step of progress, then waiting for
    ``clone_gate``, if given, and failing as ``clone_fails`` says, if it does. Purging a
    workspace would lose what ``lost`` says, by id, failing as ``losses_fail`` says, if it
    does; those ``purged`` are noted, failing as ``purge_fails`` says, if it does. The workspace
    added last is at ``latest``, if any, unless finding it fails as ``latest_fails`` says."""

    def __init__(
        self,
        kinds: dict[str, PathKind] | None = None,
        tops: dict[str, str] | None = None,
    ) -> None:
        self.cloning: list[str] = []
        self.clone_gate: asyncio.Event | None = None
        self.clone_fails: str | None = None
        self.managed: list[str] = []
        self.kinds = kinds or {}
        self.tops = tops or {}
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.hosts: list[WorkspaceHost] = []
        self.gate: asyncio.Event | None = None
        self.unavailable: str | None = None
        self.unavailable_code = INVALID
        self.flags: list[tuple[str, bool]] = []
        self.flag_gate: asyncio.Event | None = None
        self.unflag_fails: str | None = None
        self.lost: dict[str, list[str]] = {}
        self.losses_fail: str | None = None
        self.purged: list[str] = []
        self.purge_fails: str | None = None
        self.latest: str | None = None
        self.latest_fails: str | None = None

    async def kind(self, path: str) -> PathKind:
        return self.kinds.get(path, PathKind.DIRECTORY)

    async def top_level(self, path: str) -> str:
        return self.tops.get(path, path)

    async def clone(self, url: str, progress: Callable[[WorkspaceCloning], None]) -> str:
        self.cloning.append(url)
        progress(WorkspaceCloning(url=url, phase="Receiving objects", percent=50))
        if self.clone_gate is not None:
            await self.clone_gate.wait()
        if self.clone_fails is not None:
            raise CloneFailed(self.clone_fails)
        return f"/clones/{PurePath(url).name}"

    async def open(self, root: str, *, managed: bool = False) -> WorkspaceHost:
        self.opened.append(root)
        if managed:
            self.managed.append(root)
        if self.gate is not None:
            await self.gate.wait()
        if self.unavailable is not None:
            raise WorkspaceUnavailable(self.unavailable, self.unavailable_code)
        host = self.make(PurePath(root).name)
        self.hosts.append(host)
        return host

    def make(self, name: str) -> WorkspaceHost:
        """The host of the workspace ``name``."""
        return workspace(name)

    async def set_served(self, workspace_id: str, served: bool) -> None:
        if self.flag_gate is not None:
            await self.flag_gate.wait()
        if not served and self.unflag_fails is not None:
            raise WorkspaceUnavailable(self.unflag_fails, INTERNAL)
        self.flags.append((workspace_id, served))

    async def latest_added(self) -> str | None:
        if self.latest_fails is not None:
            raise WorkspaceUnavailable(self.latest_fails, INTERNAL)
        return self.latest

    async def close(self, workspace_id: str) -> None:
        assert (workspace_id, False) in self.flags
        self.closed.append(workspace_id)

    async def losses(self, workspace_id: str) -> list[str]:
        if self.losses_fail is not None:
            raise WorkspaceUnavailable(self.losses_fail, INTERNAL)
        return self.lost.get(workspace_id, [])

    async def purge(self, workspace_id: str) -> None:
        assert workspace_id in self.closed
        if self.purge_fails is not None:
            raise WorkspaceUnavailable(self.purge_fails, INTERNAL)
        self.purged.append(workspace_id)


async def welcomed(plane: ControlPlane) -> SessionConnection:
    """A new connection to ``plane`` that has said Hello, with nothing queued."""
    connection = plane.connect()
    await say(connection, "hello", HELLO)
    taken(connection)
    return connection


def ids(catalog: Event) -> list[str]:
    """The ids of the workspaces ``catalog`` lists."""
    assert isinstance(catalog, Catalog)
    return [entry.id for entry in catalog.workspaces]


def refused(command_id: str, code: str, message: str) -> CommandRefused:
    return CommandRefused(command_id=command_id, code=code, message=message)
