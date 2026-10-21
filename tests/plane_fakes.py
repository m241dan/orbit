"""Fakes for a control plane serving several workspaces."""

import asyncio
from collections.abc import Sequence

from orbit.agent import Agents, ControlPlane, WorkspaceHost, WorkspaceOwner
from orbit.agent.application.connection import SessionConnection
from orbit.protocol import (
    Attach,
    BusyChanged,
    Command,
    CommandEnvelope,
    Event,
    EventEnvelope,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import HELLO, ROLES, Call, FakeHost, ScriptedLauncher

TASK = TaskSpec(prompt="Add a flag.")


def workspace(name: str, *, owner: WorkspaceOwner | None = None) -> WorkspaceHost:
    """The host of the workspace ``name``, at ``/src/<name>``, its id ``name``, read-only if
    another ``owner`` owns it."""
    return WorkspaceHost(
        workspace=f"/src/{name}", workspace_id=name, new_id=lambda: f"{name}-1", owner=owner
    )


async def say(connection: SessionConnection, command_id: str, command: Command) -> None:
    await connection.send(CommandEnvelope(command_id=command_id, payload=command))


async def joined(plane: ControlPlane, host: WorkspaceHost) -> SessionConnection:
    """A new connection to ``plane`` that has said Hello and attached to ``host``'s open
    session, named for its workspace."""
    connection = plane.connect()
    name = host.workspace_id
    await say(connection, f"{name}-hello", HELLO)
    attach = Attach(workspace=name, session=host.saved_session.id)
    await say(connection, f"{name}-attach", attach)
    return connection


def taken(connection: SessionConnection) -> list[EventEnvelope]:
    """The envelopes queued for ``connection`` so far."""
    queue = connection._queue
    envelopes: list[EventEnvelope] = []
    while not queue.empty():
        envelopes.append(queue.get_nowait())
    return envelopes


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
    agents = Agents(agent_host, launcher, lambda: f"{name}-token", roles=ROLES, exit_grace=0.5)
    return WorkspaceHost(
        agents=agents, workspace=f"/src/{name}", workspace_id=name, new_id=lambda: f"{name}-1"
    )


async def idle(connection: SessionConnection) -> None:
    """Wait until the work lane of ``connection``'s workspace goes idle."""
    async for envelope in connection.events():
        event = envelope.payload
        if isinstance(event, BusyChanged) and event.lane == "work" and not event.busy:
            return
