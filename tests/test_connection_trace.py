"""Each frontend's ``orbit.connection`` span, from its WebSocket being accepted until it's closed,
with its Hello, its attaching and detaching, and its closing as events."""

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from orbit.agent import ControlPlane, WorkspaceHost
from orbit.agent.application.connection_trace import CONNECTION_SPAN
from orbit.agent.external.control_plane import ControlPlaneServer
from orbit.protocol import (
    NORMAL,
    POLICY_VIOLATION,
    PROTOCOL_VERSION,
    Attach,
    Catalog,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Event,
    EventEnvelope,
    Hello,
    decode_event,
    encode,
)
from tests.server_fakes import TOKEN, serving_plane
from tests.telemetry_helpers import Traced, trace_id
from tests.waiting import eventually

type Recorded = list[tuple[str, Mapping[str, Any]]]


@asynccontextmanager
async def traced_server(
    traced: Traced, *hosts: WorkspaceHost
) -> AsyncIterator[tuple[str, ControlPlaneServer]]:
    """The URL of a server of a control plane serving ``hosts``, or a new one if none, traced by
    ``traced``, and the server; stopped and closed after."""
    plane = ControlPlane(hosts or [WorkspaceHost()], telemetry=traced.telemetry)
    async with serving_plane(plane, plane.close) as served:
        yield served


@asynccontextmanager
async def frontend(url: str, server: ControlPlaneServer) -> AsyncIterator[ClientConnection]:
    """A frontend's WebSocket to ``server`` at ``url``, once the server serves it; closed after,
    once the server has served it to the end."""
    before = len(server._open)
    async with connect(url, additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
        await eventually(lambda: len(server._open) > before)
        yield ws
    await eventually(lambda: len(server._open) <= before)


async def say(websocket: ClientConnection, command_id: str, command: Command) -> None:
    await websocket.send(encode(CommandEnvelope(command_id=command_id, payload=command)))


async def received(websocket: ClientConnection) -> EventEnvelope:
    envelope = decode_event(str(await asyncio.wait_for(websocket.recv(), 5)))
    assert envelope is not None
    return envelope


async def until(websocket: ClientConnection, kind: type[Event]) -> EventEnvelope:
    """The first envelope ``websocket`` receives holding a ``kind``."""
    envelope = await received(websocket)
    while not isinstance(envelope.payload, kind):
        envelope = await received(websocket)
    return envelope


async def hello(websocket: ClientConnection, client: str, since: int | None = None) -> Catalog:
    """Say Hello on ``websocket`` as ``client``, from ``since``; the catalog it's welcomed with."""
    await say(
        websocket, "hello", Hello(client=client, protocol_version=PROTOCOL_VERSION, since=since)
    )
    catalog = (await until(websocket, Catalog)).payload
    assert isinstance(catalog, Catalog)
    return catalog


async def attach(websocket: ClientConnection, catalog: Catalog, workspace: str) -> str:
    """Attach ``websocket`` to the active session of ``workspace``, as ``catalog`` lists it;
    returns the session's id."""
    [entry] = [entry for entry in catalog.workspaces if entry.id == workspace]
    [active] = [summary for summary in entry.sessions if summary.active]
    await say(websocket, f"attach-{workspace}", Attach(workspace=workspace, session=active.id))
    await until(websocket, CommandAccepted)
    return active.id


def connections(traced: Traced) -> list[ReadableSpan]:
    return traced.spans(CONNECTION_SPAN)


def attributes(span: ReadableSpan) -> Mapping[str, Any]:
    return dict(span.attributes or {})


def events(span: ReadableSpan) -> Recorded:
    """The events recorded on ``span``, with their attributes, in order."""
    return [(event.name, dict(event.attributes or {})) for event in span.events]


def closing(code: int, reason: str = "") -> tuple[str, Mapping[str, Any]]:
    return "close", {"code": code, "reason": reason}


async def test_a_connection_is_one_root_span_with_its_hello_attach_and_close_in_order() -> None:
    traced = Traced()
    async with (
        traced_server(traced, WorkspaceHost(workspace_id="w")) as (url, server),
        frontend(url, server) as websocket,
    ):
        session = await attach(websocket, await hello(websocket, "orbit-tui"), "w")
        port = websocket.local_address[1]

    [span] = connections(traced)
    assert span.parent is None
    assert attributes(span) == {
        "network.peer.address": "127.0.0.1",
        "network.peer.port": port,
        "orbit.client": "orbit-tui",
        "orbit.protocol_version": PROTOCOL_VERSION,
    }
    assert events(span) == [
        ("hello", {}),
        ("attach", {"orbit.workspace.id": "w", "orbit.session.id": session}),
        closing(NORMAL),
    ]
    # The commands sent over it start traces of their own.
    assert trace_id(traced.command("hello")) != trace_id(span)


async def test_a_reconnect_records_the_seq_it_gives_and_how_many_were_stamped_since() -> None:
    traced = Traced()
    async with (
        traced_server(traced) as (url, server),
        frontend(url, server) as first,
        frontend(url, server) as second,
    ):
        await say(first, "hello", Hello(client="first", protocol_version=PROTOCOL_VERSION))
        last = (await until(first, Catalog)).seq  # Nothing else is stamped until one attaches.
        await hello(second, "second", since=last - 1)

    [reconnected] = [
        span for span in connections(traced) if attributes(span)["orbit.client"] == "second"
    ]
    assert events(reconnected)[:2] == [
        ("hello", {}),
        ("reconnect", {"since": last - 1, "gap": 1}),
    ]


async def test_moving_to_another_workspace_detaches_then_attaches_there() -> None:
    traced = Traced()
    hosts = WorkspaceHost(workspace_id="a"), WorkspaceHost(workspace_id="b")
    async with traced_server(traced, *hosts) as (url, server), frontend(url, server) as websocket:
        catalog = await hello(websocket, "orbit-tui")
        first = await attach(websocket, catalog, "a")
        second = await attach(websocket, catalog, "b")

    [span] = connections(traced)
    assert [name for name, _ in events(span)] == ["hello", "attach", "detach", "attach", "close"]
    assert events(span)[1:4] == [
        ("attach", {"orbit.workspace.id": "a", "orbit.session.id": first}),
        ("detach", {}),
        ("attach", {"orbit.workspace.id": "b", "orbit.session.id": second}),
    ]


async def test_a_connection_that_never_says_hello_still_ends_its_span_closed() -> None:
    traced = Traced()
    async with traced_server(traced) as (url, server), frontend(url, server):
        pass

    [span] = connections(traced)
    assert "orbit.client" not in attributes(span)
    assert events(span) == [closing(NORMAL)]


async def test_a_websocket_the_server_closes_records_its_code_and_reason() -> None:
    traced = Traced()
    async with traced_server(traced) as (url, server), frontend(url, server) as websocket:
        await websocket.send(b"{}")
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(websocket.recv(), 5)

    [span] = connections(traced)
    reason = "Send each envelope as JSON in a text frame."
    assert events(span) == [closing(POLICY_VIOLATION, reason)]


async def test_an_incompatible_hello_records_the_version_it_speaks() -> None:
    traced = Traced()
    async with traced_server(traced) as (url, server), frontend(url, server) as websocket:
        old = Hello(client="old", protocol_version=PROTOCOL_VERSION + 1)
        await say(websocket, "hello", old)
        await until(websocket, CommandRefused)

    [span] = connections(traced)
    assert attributes(span)["orbit.client"] == "old"
    assert attributes(span)["orbit.protocol_version"] == PROTOCOL_VERSION + 1
    assert events(span) == [("hello", {}), closing(NORMAL)]


async def test_a_second_hello_is_no_second_hello_on_the_span() -> None:
    traced = Traced()
    async with traced_server(traced) as (url, server), frontend(url, server) as websocket:
        await hello(websocket, "orbit-tui")
        again = Hello(client="again", protocol_version=PROTOCOL_VERSION, since=1)
        await say(websocket, "again", again)
        await until(websocket, CommandRefused)

    [span] = connections(traced)
    assert attributes(span)["orbit.client"] == "orbit-tui"
    assert [name for name, _ in events(span)] == ["hello", "close"]
