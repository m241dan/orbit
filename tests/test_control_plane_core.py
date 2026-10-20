"""The control plane over one workspace host: connections, the shared usage monitor, routing."""

import asyncio

from orbit import __version__
from orbit.agent import ControlPlane, WorkspaceHost, WorkspaceOwner
from orbit.agent.application.connection import SessionConnection
from orbit.agent.domain.usage import UsageReport
from orbit.protocol import (
    AddTask,
    Catalog,
    CommandAccepted,
    CommandEnvelope,
    Event,
    Notice,
    Snapshot,
    TasksChanged,
    TaskSpec,
    UsageChanged,
    Welcome,
)
from tests.agent_fakes import HELLO
from tests.test_usage_monitor import LIMITS, Source

NOTE = Notice(message="From the host.")


async def welcomed(plane: ControlPlane, name: str) -> SessionConnection:
    """A new connection to ``plane`` that has said Hello as ``name``."""
    connection = plane.connect()
    await connection.send(CommandEnvelope(command_id=name, payload=HELLO))
    return connection


async def until(connection: SessionConnection, kind: type[Event]) -> list[Event]:
    """What reaches ``connection`` up to and including the first ``kind`` event."""
    events: list[Event] = []
    stream = connection.events()
    while not events or not isinstance(events[-1], kind):
        events.append((await asyncio.wait_for(anext(stream), 5)).payload)
    return events


async def test_a_connection_is_welcomed_with_the_catalog_and_snapshot_of_the_host() -> None:
    plane = ControlPlane(WorkspaceHost())
    connection = await welcomed(plane, "first")
    events = await until(connection, Snapshot)
    await plane.close()

    assert events[0] == Welcome(command_id="first", server_version=__version__)
    assert [type(event) for event in events[1:]] == [Catalog, Snapshot]


async def test_the_hosts_events_reach_every_connection() -> None:
    host = WorkspaceHost()
    plane = ControlPlane(host)
    first, second = await welcomed(plane, "first"), await welcomed(plane, "second")
    await until(first, Snapshot)
    await until(second, Snapshot)

    await host.outbox.put(NOTE)

    assert (await until(first, Notice))[-1] == NOTE
    assert (await until(second, Notice))[-1] == NOTE
    await plane.close()


async def test_a_command_is_routed_to_the_host_and_answered() -> None:
    plane = ControlPlane(WorkspaceHost())
    connection = await welcomed(plane, "first")
    await until(connection, Snapshot)

    add = AddTask(task=TaskSpec(prompt="Add a flag."))
    await connection.send(CommandEnvelope(command_id="add", payload=add))

    events = await until(connection, CommandAccepted)
    assert events[-1] == CommandAccepted(command_id="add")
    assert any(isinstance(event, TasksChanged) for event in events)
    await plane.close()


async def test_the_usage_monitor_runs_once_however_many_connect() -> None:
    source = Source(UsageReport(LIMITS))
    plane = ControlPlane(WorkspaceHost(), source)
    first, second = await welcomed(plane, "first"), await welcomed(plane, "second")

    assert (await until(first, UsageChanged))[-1] == UsageChanged(limits=LIMITS)
    assert (await until(second, UsageChanged))[-1] == UsageChanged(limits=LIMITS)
    assert source.calls == 1
    await plane.close()


async def test_closing_stops_the_usage_monitor_and_ends_every_stream() -> None:
    source = Source(UsageReport(LIMITS))
    plane = ControlPlane(WorkspaceHost(), source)
    connection = await welcomed(plane, "first")
    await until(connection, UsageChanged)

    await plane.close()

    assert [event async for event in connection.events()] == []
    assert source.calls == 1


async def test_a_connection_after_closing_starts_no_usage_monitor() -> None:
    source = Source(UsageReport(LIMITS))
    plane = ControlPlane(WorkspaceHost(), source)
    await plane.close()

    connection = plane.connect()
    await connection.send(CommandEnvelope(command_id="late", payload=HELLO))

    assert source.calls == 0


def test_it_reads_the_hosts_ownership_and_storage() -> None:
    host = WorkspaceHost(owner=WorkspaceOwner(pid=1))
    plane = ControlPlane(host)

    assert plane.host is host
    assert plane.read_only
    assert not plane.storage_halted
