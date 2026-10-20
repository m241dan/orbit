"""The control plane's server over a real WebSocket on loopback, as a frontend uses it."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast

import pytest
from starlette.responses import Response
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from orbit.agent import ControlPlane, WorkspaceHost
from orbit.agent.application import connection as connection_module
from orbit.agent.application.connection import SessionConnection
from orbit.agent.domain.server import ServerSettings
from orbit.agent.external.control_plane import (
    UNAUTHORIZED_BODY,
    ControlPlaneServer,
    ListenFailed,
    _Conversation,
    close_reason,
    ws_url,
)
from orbit.client import accepts
from orbit.protocol import (
    GOING_AWAY,
    INVALID,
    MESSAGE_TOO_BIG,
    NORMAL,
    POLICY_VIOLATION,
    PROTOCOL_VERSION,
    SHUTTING_DOWN,
    UNAUTHORIZED,
    UNAVAILABLE,
    UNKNOWN,
    AddTask,
    Catalog,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Event,
    Hello,
    RunTasks,
    SessionStateChanged,
    Snapshot,
    TasksChanged,
    TaskSpec,
    TurnEndReason,
    Welcome,
    decode_event,
    encode,
)
from tests.agent_fakes import Call, board_of, control_plane, session_for

TOKEN = "the-token"
LOOPBACK = ServerSettings(port=0)
TASK = TaskSpec(prompt="Add a flag.")


class Lines:
    """The lines the server said."""

    def __init__(self) -> None:
        self.said: list[str] = []

    def __call__(self, line: str) -> None:
        self.said.append(line)


@asynccontextmanager
async def serving(
    session: WorkspaceHost | None = None,
    settings: ServerSettings = LOOPBACK,
    lines: Lines | None = None,
) -> AsyncIterator[str]:
    """The URL of a server of ``session`` (a new one if none), stopped and closed after."""
    served = control_plane(session or WorkspaceHost())
    server = ControlPlaneServer(served, lambda: TOKEN, settings, lines or Lines())
    url = await server.start()
    try:
        yield url
    finally:
        await server.stop(served.close)


def frontend(url: str, token: str | None = TOKEN) -> "connect":
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return connect(url, additional_headers=headers, max_size=None)


async def say(websocket: ClientConnection, command_id: str, command: Command) -> None:
    await websocket.send(encode(CommandEnvelope(command_id=command_id, payload=command)))


async def hello(websocket: ClientConnection, client: str = "tests") -> None:
    await say(websocket, "hello", Hello(client=client, protocol_version=PROTOCOL_VERSION))


async def received(websocket: ClientConnection) -> Event:
    envelope = decode_event(str(await asyncio.wait_for(websocket.recv(), 5)))
    assert envelope is not None
    return envelope.payload


async def until[E: Event](websocket: ClientConnection, kind: type[E]) -> list[Event]:
    """What ``websocket`` received, up to and including the first ``kind``."""
    events = [await received(websocket)]
    while not isinstance(events[-1], kind):
        events.append(await received(websocket))
    return events


async def closed(websocket: ClientConnection) -> tuple[int | None, str | None]:
    """The code and reason ``websocket`` was closed with, once the server closes it."""
    with pytest.raises(ConnectionClosed):
        while True:
            await asyncio.wait_for(websocket.recv(), 5)
    return websocket.close_code, websocket.close_reason


async def test_a_frontend_with_the_token_is_welcomed_with_the_catalog_and_snapshot() -> None:
    async with serving() as url, frontend(url) as websocket:
        assert url.startswith("ws://127.0.0.1:") and not url.endswith(":0/")
        await hello(websocket)
        events = await until(websocket, Snapshot)
        assert [type(e) for e in events] == [Welcome, Catalog, Snapshot]


@pytest.mark.parametrize("token", [None, "wrong"])
async def test_a_frontend_without_the_token_is_turned_away(token: str | None) -> None:
    async with serving() as url:
        with pytest.raises(InvalidStatus) as refused:
            async with frontend(url, token):
                pass
        assert refused.value.response.status_code == 401


async def test_a_new_token_admits_new_frontends_at_once_and_keeps_open_ones() -> None:
    tokens = [TOKEN]
    served = ControlPlane(WorkspaceHost())
    server = ControlPlaneServer(served, lambda: tokens[-1], LOOPBACK, Lines())
    url = await server.start()
    try:
        async with frontend(url) as before:
            await hello(before)
            await until(before, Snapshot)
            tokens.append("the-new-token")
            with pytest.raises(InvalidStatus) as refused:
                async with frontend(url, TOKEN):
                    pass
            assert refused.value.response.status_code == 401
            async with frontend(url, "the-new-token") as after:
                await hello(after)
                await until(after, Snapshot)
            await say(before, "add", AddTask(task=TASK))
            await until(before, CommandAccepted)
    finally:
        await server.stop(served.close)


async def test_an_empty_token_admits_no_frontend() -> None:
    served = ControlPlane(WorkspaceHost())
    server = ControlPlaneServer(served, lambda: "", LOOPBACK, Lines())
    url = await server.start()
    try:
        with pytest.raises(InvalidStatus) as refused:
            async with connect(url):
                pass
        assert refused.value.response.status_code == 401
    finally:
        await server.stop(served.close)


async def test_every_frontend_sees_a_change_one_of_them_makes() -> None:
    async with serving() as url, frontend(url) as first, frontend(url) as second:
        for websocket in (first, second):
            await hello(websocket)
            await until(websocket, Snapshot)
        await say(first, "add", AddTask(task=TASK))
        for websocket in (first, second):
            events = await until(websocket, CommandAccepted)
            changes = [type(e) for e in events if not isinstance(e, SessionStateChanged)]
            assert changes == [TasksChanged, CommandAccepted]


async def test_a_command_of_an_unknown_type_is_refused_as_unknown() -> None:
    async with serving() as url, frontend(url) as websocket:
        await hello(websocket)
        await until(websocket, Snapshot)
        await websocket.send('{"command_id":"c1","payload":{"type":"nope"}}')
        *_, refusal = await until(websocket, CommandRefused)
        assert isinstance(refusal, CommandRefused)
        assert (refusal.command_id, refusal.code) == ("c1", UNKNOWN)


async def test_an_invalid_command_with_an_id_is_refused_as_invalid_before_hello() -> None:
    async with serving() as url, frontend(url) as websocket:
        await websocket.send('{"command_id":"c2","payload":{"type":"add_task"}}')
        refusal = await received(websocket)
        assert isinstance(refusal, CommandRefused)
        assert (refusal.command_id, refusal.code) == ("c2", INVALID)


async def test_a_frame_without_a_command_id_closes_the_websocket() -> None:
    async with serving() as url, frontend(url) as websocket:
        await websocket.send("not json")
        code, reason = await closed(websocket)
        assert code == POLICY_VIOLATION and reason is not None and reason.startswith("Not JSON")


async def test_a_binary_frame_closes_the_websocket() -> None:
    async with serving() as url, frontend(url) as websocket:
        await websocket.send(b"{}")
        assert await closed(websocket) == (
            POLICY_VIOLATION,
            "Send each envelope as JSON in a text frame.",
        )


async def test_an_incompatible_hello_ends_the_stream_and_closes_the_websocket() -> None:
    async with serving() as url, frontend(url) as websocket:
        await say(websocket, "hello", Hello(client="old", protocol_version=PROTOCOL_VERSION + 1))
        assert isinstance(await received(websocket), CommandRefused)
        assert await closed(websocket) == (NORMAL, "")


async def test_a_message_over_the_limit_closes_the_websocket() -> None:
    limited = ServerSettings(port=0, max_message_mb=1)
    async with serving(settings=limited) as url, frontend(url) as websocket:
        await websocket.send("x" * (1024 * 1024 + 1))
        code, reason = await closed(websocket)
        assert code == MESSAGE_TOO_BIG and reason


async def test_an_event_over_the_limit_closes_the_websocket() -> None:
    big = TaskSpec(prompt="x" * (600 * 1024))
    limited = ServerSettings(port=0, max_message_mb=1)
    async with serving(settings=limited) as url, frontend(url) as websocket:
        await hello(websocket)
        await until(websocket, Snapshot)
        await say(websocket, "one", AddTask(task=big))
        await until(websocket, CommandAccepted)
        await say(websocket, "two", AddTask(task=big))
        code, reason = await closed(websocket)
        assert code == MESSAGE_TOO_BIG
        assert reason is not None and reason.endswith("over the 1048576-byte limit")


async def test_a_frontend_too_far_behind_is_closed_saying_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connection_module, "QUEUE_LIMIT", 1)
    async with serving() as url, frontend(url) as websocket:
        await hello(websocket)
        assert await closed(websocket) == (POLICY_VIOLATION, "too far behind")


async def test_shutting_down_closes_every_websocket_saying_so() -> None:
    session = ControlPlane(WorkspaceHost())
    server = ControlPlaneServer(session, lambda: TOKEN, LOOPBACK, Lines())
    url = await server.start()
    async with frontend(url) as greeted, frontend(url) as silent:
        await hello(greeted)
        await until(greeted, Snapshot)
        await server.stop(session.close)
        for websocket in (greeted, silent):
            assert await closed(websocket) == (GOING_AWAY, SHUTTING_DOWN)
    with pytest.raises(OSError):
        async with frontend(url):
            pass


async def test_the_server_says_as_frontends_connect_and_disconnect() -> None:
    lines = Lines()
    async with serving(lines=lines) as url:
        async with frontend(url) as websocket:
            await hello(websocket, "orbit-tui")
            await until(websocket, Snapshot)
            port = websocket.local_address[1]
        while len(lines.said) < 2:
            await asyncio.sleep(0.01)
    assert lines.said == [
        f"orbit-tui connected from 127.0.0.1:{port}.",
        f"orbit-tui disconnected from 127.0.0.1:{port}.",
    ]


async def test_the_server_says_as_an_unnamed_frontend_connects_and_disconnects() -> None:
    lines = Lines()
    async with serving(lines=lines) as url:
        async with frontend(url) as websocket:
            await hello(websocket, "")
            await until(websocket, Snapshot)
            port = websocket.local_address[1]
        while len(lines.said) < 2:
            await asyncio.sleep(0.01)
    assert lines.said == [
        f" connected from 127.0.0.1:{port}.",
        f" disconnected from 127.0.0.1:{port}.",
    ]


async def test_the_server_says_nothing_of_a_frontend_that_closes_without_a_hello() -> None:
    lines = Lines()
    async with serving(lines=lines) as url, frontend(url):
        pass
    assert lines.said == []


async def test_discoverys_probe_is_accepted_without_the_server_saying_anything() -> None:
    lines = Lines()
    async with serving(lines=lines) as url:
        assert await accepts(url, TOKEN)
    assert lines.said == []


async def claims_done(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("complete_task", summary="done")
    return "completed"


async def test_queued_work_runs_to_the_end_with_no_frontend_connected() -> None:
    session, _, _ = session_for(claims_done)
    async with serving(session) as url:
        async with frontend(url) as websocket:
            await hello(websocket)
            await until(websocket, Snapshot)
            await say(websocket, "add", AddTask(task=TASK))
            await until(websocket, CommandAccepted)
            await say(websocket, "run", RunTasks())
            await until(websocket, CommandAccepted)
        while board_of(session).tasks[0].status != "completed":
            await asyncio.sleep(0.01)


async def test_a_port_in_use_cant_be_listened_on() -> None:
    async with serving() as url:
        port = int(url.split(":")[2].rstrip("/"))
        server = ControlPlaneServer(
            ControlPlane(WorkspaceHost()), lambda: TOKEN, ServerSettings(port=port), Lines()
        )
        with pytest.raises(ListenFailed, match=f"Can't listen on 127.0.0.1:{port}: ") as failed:
            await server.start()
        assert failed.value.port == port


async def test_a_server_that_fails_to_start_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(self: object, sockets: object = None) -> None:
        raise OSError("no sockets for you")

    monkeypatch.setattr("uvicorn.Server.serve", broken)
    server = ControlPlaneServer(ControlPlane(WorkspaceHost()), lambda: TOKEN, LOOPBACK, Lines())
    with pytest.raises(RuntimeError, match="failed to start") as raised:
        await server.start()
    assert isinstance(raised.value.__cause__, OSError)


async def test_stopping_a_server_that_never_started_is_harmless() -> None:
    session = ControlPlane(WorkspaceHost())
    await ControlPlaneServer(session, lambda: TOKEN, LOOPBACK, Lines()).stop(session.close)
    await session.close()


def test_an_ipv6_url_has_its_address_in_brackets() -> None:
    assert ws_url("::1", 80) == "ws://[::1]:80/"
    assert ws_url("localhost", 80) == "ws://localhost:80/"


def test_a_secure_url_is_wss() -> None:
    assert ws_url("0.0.0.0", 443, secure=True) == "wss://0.0.0.0:443/"


def test_a_close_reason_is_cut_to_fit_a_close_frame() -> None:
    assert len(close_reason("é" * 100).encode()) == 122
    assert close_reason("short") == "short"


def normally() -> tuple[int, str]:
    return NORMAL, ""


class VanishedWebSocket:
    """A WebSocket whose frontend has gone: sending fails, after which receiving says it's
    disconnected; closing is counted."""

    def __init__(self) -> None:
        self.gone = asyncio.Event()
        self.closes = 0

    async def receive(self) -> dict[str, str]:
        await self.gone.wait()
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        self.gone.set()
        raise WebSocketDisconnect(1006)

    async def close(self, code: int, reason: str) -> None:
        self.closes += 1


async def test_a_frontend_gone_as_an_event_is_sent_ends_its_connection_unclosed() -> None:
    session = ControlPlane(WorkspaceHost())
    vanished = VanishedWebSocket()
    connection = session.connect()
    conversation = _Conversation(cast(WebSocket, vanished), connection, 1024, normally)
    running = asyncio.create_task(conversation.run())
    hello = Hello(client="tests", protocol_version=PROTOCOL_VERSION)
    await connection.send(CommandEnvelope(command_id="hello", payload=hello))

    await asyncio.wait_for(running, 5)

    assert not connection.listening and vanished.closes == 0
    await session.close()


class Upgrade:
    """A WebSocket upgrade request showing ``token``: denial responses are recorded, and
    accepting it fails."""

    def __init__(self, token: str) -> None:
        self.headers = {"authorization": f"Bearer {token}"}
        self.client = None
        self.denied: list[tuple[int, bytes]] = []

    async def send_denial_response(self, response: Response) -> None:
        self.denied.append((response.status_code, bytes(response.body)))

    async def accept(self) -> None:
        raise AssertionError("A WebSocket was accepted.")


class Counted:
    """A session that counts the connections made to it."""

    def __init__(self) -> None:
        self.session = ControlPlane(WorkspaceHost())
        self.connections = 0
        self.read_only = False
        self.storage_halted = False

    def connect(self) -> SessionConnection:
        self.connections += 1
        return self.session.connect()


@pytest.mark.parametrize(
    ("token", "denied"),
    [
        (TOKEN, (UNAVAILABLE, SHUTTING_DOWN.encode())),
        ("wrong", (UNAUTHORIZED, UNAUTHORIZED_BODY.encode())),
    ],
)
async def test_a_websocket_arriving_as_the_server_stops_is_turned_away(
    token: str, denied: tuple[int, bytes]
) -> None:
    counted = Counted()
    server = ControlPlaneServer(counted, lambda: TOKEN, LOOPBACK, Lines())
    await server.start()
    closing, release = asyncio.Event(), asyncio.Event()

    async def close_session() -> None:
        closing.set()
        await release.wait()

    stopping = asyncio.create_task(server.stop(close_session))
    await closing.wait()
    upgrade = Upgrade(token)

    await server._websocket(cast(WebSocket, upgrade))

    release.set()
    await stopping
    assert upgrade.denied == [denied] and counted.connections == 0
    await counted.session.close()


class Accepting:
    """A WebSocket upgrade request showing the token, which awaits ``accepted`` as it's
    accepted; closes are recorded."""

    def __init__(self, accepted: Callable[[], Awaitable[None]]) -> None:
        self.headers = {"authorization": f"Bearer {TOKEN}"}
        self.client = None
        self.closed: list[tuple[int, str]] = []
        self._accepted = accepted

    async def accept(self) -> None:
        await self._accepted()

    async def close(self, code: int, reason: str) -> None:
        self.closed.append((code, reason))


async def test_a_websocket_accepted_as_the_server_stops_is_closed_unconnected() -> None:
    counted = Counted()
    server = ControlPlaneServer(counted, lambda: TOKEN, LOOPBACK, Lines())
    await server.start()
    closing, release = asyncio.Event(), asyncio.Event()
    stops: list[asyncio.Task[None]] = []

    async def close_session() -> None:
        closing.set()
        await release.wait()

    async def accepted() -> None:
        stops.append(asyncio.create_task(server.stop(close_session)))
        await closing.wait()

    accepting = Accepting(accepted)

    await server._websocket(cast(WebSocket, accepting))

    release.set()
    await stops[0]
    assert accepting.closed == [(GOING_AWAY, close_reason(SHUTTING_DOWN))]
    assert counted.connections == 0
    await counted.session.close()
