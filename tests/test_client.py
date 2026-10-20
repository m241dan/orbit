"""The WebSocket session a frontend talks to a control plane in another process through."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from orbit.agent import AgentSession
from orbit.agent.domain.server import ServerSettings
from orbit.agent.external.control_plane import ControlPlaneServer
from orbit.client import Backoff, TokenUnreadable, WebSocketSession, messages, read_token
from orbit.client.session import MAX_MESSAGE_BYTES
from orbit.protocol import (
    DEFAULT_MAX_MESSAGE_MB,
    NOT_CONNECTED,
    PROTOCOL_VERSION,
    UNKNOWN,
    Attach,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Connected,
    Ended,
    EventEnvelope,
    Hello,
    LinkState,
    NotConnected,
    Notice,
    ProtocolError,
    Reconnecting,
    RunTasks,
    SessionSummary,
    Settings,
    Snapshot,
    Stopped,
    TokenRefused,
    Welcome,
    decode_command,
    encode,
)

TOKEN = "the-token"
TOKEN_FILE = Path("token")  # In the directory each test runs in.
QUICK = Backoff(first=0.01, most=0.02)
HELLO = CommandEnvelope(
    command_id="hello", payload=Hello(client="tests", protocol_version=PROTOCOL_VERSION)
)
RUN = CommandEnvelope(command_id="run", payload=RunTasks())
ATTACH = CommandEnvelope(command_id="attach", payload=Attach(session="earlier"))


@pytest.fixture(autouse=True)
def in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test in its own directory, where ``TOKEN_FILE`` is written."""
    monkeypatch.chdir(tmp_path)


class FakePlane:
    """A control plane that notes the headers and commands each connection brings, sends what
    a test gives it, turns upgrades away with ``status`` while it's set, and closes each
    connection at once while ``hang_up`` is set."""

    def __init__(self) -> None:
        self.connections: asyncio.Queue[ServerConnection] = asyncio.Queue()
        self.commands: asyncio.Queue[CommandEnvelope] = asyncio.Queue()
        self.headers: list[Headers] = []
        self.status: int | None = None
        self.turned_away = asyncio.Event()
        self.hang_up = False

    def check(self, connection: ServerConnection, request: Request) -> Response | None:
        self.headers.append(request.headers)
        if self.status is None:
            return None
        self.turned_away.set()
        return connection.respond(self.status, "No.")

    async def handle(self, connection: ServerConnection) -> None:
        self.connections.put_nowait(connection)
        if self.hang_up:
            await connection.close(1008, "Too far behind")
            return
        async for text in connection:
            self.commands.put_nowait(decode_command(str(text)))

    async def connection(self) -> ServerConnection:
        return await asyncio.wait_for(self.connections.get(), 5)

    async def command(self) -> CommandEnvelope:
        return await asyncio.wait_for(self.commands.get(), 5)


@asynccontextmanager
async def plane() -> AsyncIterator[tuple[FakePlane, str]]:
    """A fake control plane and its URL."""
    fake = FakePlane()
    server: Server
    async with serve(fake.handle, "127.0.0.1", 0, process_request=fake.check) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield fake, f"ws://127.0.0.1:{port}/"


class States:
    """The states a session's link moved to, each set as it arrives."""

    def __init__(self) -> None:
        self.seen: list[LinkState] = []
        self._changed = asyncio.Event()

    def __call__(self, state: LinkState) -> None:
        self.seen.append(state)
        self._changed.set()

    async def until(self, kind: type[LinkState]) -> LinkState:
        """The first state of ``kind`` seen, once it is."""
        async with asyncio.timeout(5):
            while not any(isinstance(state, kind) for state in self.seen):
                self._changed.clear()
                await self._changed.wait()
        return next(state for state in self.seen if isinstance(state, kind))


def client(
    url: str, token: str = TOKEN, backoff: Backoff = QUICK
) -> tuple[WebSocketSession, States]:
    TOKEN_FILE.write_text(f"{token}\n")
    session = WebSocketSession(url, TOKEN_FILE, backoff)
    states = States()
    session.watch(states)
    return session, states


def event(seq: int, message: str = "hi") -> str:
    return encode(EventEnvelope(seq=seq, payload=Notice(message=message)))


async def next_event(events: AsyncIterator[EventEnvelope]) -> EventEnvelope:
    return await asyncio.wait_for(anext(events), 5)


def snapshot(seq: int, active: bool) -> str:
    """A Snapshot of the session ``earlier``, the open one if ``active``, else a view."""
    shown = Snapshot(
        session=SessionSummary(id="earlier", name="Earlier"),
        settings=Settings(),
        model_choices=(),
        busy={},
        tasks=(),
        steps=(),
        review=None,
        proposals=(),
        agents=(),
        active=active,
    )
    return encode(EventEnvelope(seq=seq, session="earlier", payload=shown))


def answer(seq: int, command: CommandEnvelope, refused: bool = False) -> str:
    """The answer to ``command``: refused, if ``refused``, else accepted."""
    answered: CommandAccepted | CommandRefused = CommandAccepted(command_id=command.command_id)
    if refused:
        answered = CommandRefused(command_id=command.command_id, code=UNKNOWN, message="Gone.")
    return encode(EventEnvelope(seq=seq, payload=answered))


async def drop_after(
    connection: ServerConnection, events: AsyncIterator[EventEnvelope], *frames: str
) -> None:
    """Send ``frames`` on ``connection``, wait for the client to take each, then drop it."""
    for frame in frames:
        await connection.send(frame)
        await next_event(events)
    await connection.close(1011, "Oops")


async def reconnecting(
    fake: FakePlane, session: WebSocketSession
) -> tuple[ServerConnection, list[CommandEnvelope]]:
    """The next connection the client makes and the commands it greets with, up to ``RUN``,
    sent once the first arrives."""
    connection = await fake.connection()
    greeting = [await fake.command()]
    await session.send(RUN)
    while (command := await fake.command()) != RUN:
        greeting.append(command)
    return connection, greeting


@asynccontextmanager
async def viewing() -> AsyncIterator[
    tuple[FakePlane, WebSocketSession, AsyncIterator[EventEnvelope], ServerConnection]
]:
    """A client that said Hello and attached to ``earlier``, a view, on its connection."""
    async with plane() as (fake, url):
        session, _ = client(url)
        events = session.events()
        await session.send(HELLO)
        first = await fake.connection()
        await fake.command()
        await session.send(ATTACH)
        await fake.command()
        await first.send(snapshot(3, active=False))
        await first.send(answer(4, ATTACH))
        await next_event(events)
        await next_event(events)
        yield fake, session, events, first
        await session.close()


async def test_it_connects_showing_the_token_and_greets_with_the_hello() -> None:
    async with plane() as (fake, url):
        session, states = client(url)
        await session.send(HELLO)
        assert await fake.command() == HELLO
        assert fake.headers[0]["Authorization"] == f"Bearer {TOKEN}"
        assert await states.until(Connected) == Connected(url) == session.state
        await session.close()


async def test_a_hello_sent_while_connected_goes_at_once() -> None:
    async with plane() as (fake, url):
        session, states = client(url)
        session.events()
        await states.until(Connected)
        await session.send(HELLO)
        assert await fake.command() == HELLO
        await session.send(RUN)
        assert await fake.command() == RUN
        await session.close()


async def test_events_are_passed_on_and_unknown_types_skipped() -> None:
    async with plane() as (fake, url):
        session, _ = client(url)
        events = session.events()
        connection = await fake.connection()
        await connection.send('{"seq": 1, "payload": {"type": "from_the_future"}}')
        await connection.send(event(2))
        received = await next_event(events)
        assert received.seq == 2 and received.payload == Notice(message="hi")
        await session.close()


async def test_an_unreadable_event_ends_the_events_with_its_error() -> None:
    async with plane() as (fake, url):
        session, _ = client(url)
        events = session.events()
        await (await fake.connection()).send("not json")
        with pytest.raises(ProtocolError):
            await next_event(events)
        await session.close()


async def test_a_lost_connection_reconnects_asking_for_what_was_missed() -> None:
    async with plane() as (fake, url):
        session, states = client(url)
        events = session.events()
        await session.send(HELLO)
        first = await fake.connection()
        await fake.command()
        await first.send(event(7))
        await next_event(events)
        await first.close(1011, "Oops")
        reconnecting = await states.until(Reconnecting)
        assert isinstance(reconnecting, Reconnecting) and reconnecting.url == url
        again = await fake.command()
        assert isinstance(again.payload, Hello) and again.payload.since == 7
        assert again.command_id == HELLO.command_id
        assert states.seen[-1] == Connected(url)
        assert not any(isinstance(state, Stopped) for state in states.seen)
        await session.close()


async def test_a_connection_lost_at_once_each_time_is_retried_with_growing_waits() -> None:
    async with plane() as (fake, url):
        fake.hang_up = True
        session, _ = client(url, backoff=Backoff(first=0.02, most=1, jitter=0))
        session.events()
        await asyncio.sleep(0.3)  # Waits of 0.02, 0.04, 0.08 and 0.16 s fit in it.
        await session.close()
        assert 2 <= fake.connections.qsize() <= 6


async def test_a_lost_connection_waits_the_backoff_before_reconnecting() -> None:
    async with plane() as (fake, url):
        session, states = client(url, backoff=Backoff(first=0.2, most=1, jitter=0))
        events = session.events()
        first = await fake.connection()
        await first.send(event(1))
        await next_event(events)
        await first.close(1011, "Oops")
        reconnecting = await states.until(Reconnecting)
        await fake.connection()
        assert isinstance(reconnecting, Reconnecting)
        assert time.monotonic() - reconnecting.since >= 0.2
        await session.close()


async def test_a_reconnecting_client_viewing_a_session_attaches_to_it_again() -> None:
    async with viewing() as (fake, session, events, first):
        await drop_after(first, events)
        _, again = await reconnecting(fake, session)

    assert again == [again[0], ATTACH] and isinstance(again[0].payload, Hello)


async def test_a_link_lost_before_the_view_is_shown_again_attaches_on_the_next() -> None:
    async with viewing() as (fake, session, events, first):
        await drop_after(first, events)
        second, _ = await reconnecting(fake, session)
        await drop_after(second, events, snapshot(5, active=True))
        _, again = await reconnecting(fake, session)

    assert [type(c.payload) for c in again] == [Hello, Attach]


async def test_a_refused_attach_is_not_sent_again_on_reconnecting() -> None:
    async with viewing() as (fake, session, events, first):
        await drop_after(first, events)
        second, _ = await reconnecting(fake, session)
        await drop_after(second, events, snapshot(5, active=True), answer(6, ATTACH, True))
        _, again = await reconnecting(fake, session)

    assert [type(c.payload) for c in again] == [Hello]


async def test_a_reconnecting_client_back_on_the_open_session_only_says_hello() -> None:
    back = CommandEnvelope(command_id="back", payload=Attach(session="today"))
    async with viewing() as (fake, session, events, first):
        await session.send(back)
        await fake.command()
        await drop_after(first, events, snapshot(5, active=True), answer(6, back))
        _, again = await reconnecting(fake, session)

    assert [type(c.payload) for c in again] == [Hello]


async def test_an_attach_that_couldnt_be_sent_is_not_sent_on_reconnecting() -> None:
    back = CommandEnvelope(command_id="back", payload=Attach(session="today"))
    async with viewing() as (fake, session, events, first):
        fake.status = 503
        await drop_after(first, events)
        await asyncio.wait_for(fake.turned_away.wait(), 5)
        with pytest.raises(NotConnected):
            await session.send(back)
        fake.status = None
        _, again = await reconnecting(fake, session)

    assert again[1:] == [ATTACH]


async def test_an_answer_to_another_command_leaves_the_view_to_attach_again() -> None:
    async with viewing() as (fake, session, events, first):
        await drop_after(first, events, snapshot(5, active=True), answer(6, RUN))
        _, again = await reconnecting(fake, session)

    assert [type(c.payload) for c in again] == [Hello, Attach]


async def test_a_command_sent_while_disconnected_raises_not_connected() -> None:
    async with plane() as (fake, url):
        fake.status = 503
        session, states = client(url)
        await session.send(HELLO)  # Kept for when it connects.
        with pytest.raises(NotConnected, match=NOT_CONNECTED):
            await session.send(RUN)
        await states.until(Reconnecting)
        await asyncio.wait_for(fake.turned_away.wait(), 5)
        fake.status = None
        assert await fake.command() == HELLO
        await states.until(Connected)
        await session.close()


async def test_a_command_on_a_connection_closed_unnoticed_raises_not_connected() -> None:
    async with plane() as (_, url):
        session, _ = client(url)
        async with connect(url) as closed:
            await closed.close()
            session._websocket = closed
            with pytest.raises(NotConnected):
                await session.send(RUN)
            await session.send(HELLO)  # Kept for the next connection.
        await session.close()


async def test_failing_to_connect_is_retried_and_reported_once() -> None:
    async with plane() as (_, url):
        pass  # Nothing listens there any more.
    session, states = client(url)
    session.events()
    await states.until(Reconnecting)
    await asyncio.sleep(0.1)  # Long enough for several attempts.
    assert len(states.seen) == 1
    await session.close()


async def test_a_control_plane_shutting_down_is_reported_and_retried() -> None:
    async with plane() as (fake, url):
        session, states = client(url)
        await session.send(HELLO)
        await fake.command()
        await (await fake.connection()).close(1001, "orbit serve is shutting down")
        await states.until(Stopped)
        assert await fake.command() == HELLO
        kinds = [type(state) for state in states.seen]
        assert kinds == [Connected, Stopped, Reconnecting, Connected]
        await session.close()


@pytest.mark.parametrize(
    ("code", "reason"),
    [(1000, ""), (1009, "An event of 99 bytes is over the 9-byte limit")],
)
async def test_a_close_that_would_recur_ends_the_link_with_its_reason(
    code: int, reason: str
) -> None:
    async with plane() as (fake, url):
        session, states = client(url)
        await session.send(HELLO)
        await fake.command()
        await (await fake.connection()).close(code, reason)
        assert await states.until(Ended) == Ended(url, reason) == session.state
        await asyncio.sleep(0.1)  # Long enough for several attempts.
        assert fake.connections.empty()
        assert [type(state) for state in states.seen] == [Connected, Ended]
        await session.close()


async def test_an_event_over_the_clients_limit_ends_the_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orbit.client.session.MAX_MESSAGE_BYTES", 100)
    async with plane() as (fake, url):
        session, states = client(url)
        session.events()
        await (await fake.connection()).send(event(1, "x" * 200))
        ended = await states.until(Ended)
        assert isinstance(ended, Ended) and ended.url == url
        await asyncio.sleep(0.1)
        assert fake.connections.empty()
        await session.close()


@pytest.mark.parametrize("code", [1008, 1011])
async def test_a_close_that_may_pass_is_reconnected_after(code: int) -> None:
    async with plane() as (fake, url):
        session, states = client(url)
        await session.send(HELLO)
        await fake.command()
        await (await fake.connection()).close(code, "Too far behind")
        assert await fake.command() == HELLO
        assert [type(state) for state in states.seen] == [Connected, Reconnecting, Connected]
        await session.close()


async def test_a_connection_dropped_without_a_close_frame_is_reconnected_after() -> None:
    async with plane() as (fake, url):
        session, states = client(url)
        await session.send(HELLO)
        await fake.command()
        (await fake.connection()).transport.abort()
        assert await fake.command() == HELLO
        assert [type(state) for state in states.seen] == [Connected, Reconnecting, Connected]
        await session.close()


async def test_an_incompatible_hello_ends_the_link_to_the_control_plane_server() -> None:
    served = AgentSession()
    server = ControlPlaneServer(served, lambda: TOKEN, ServerSettings(port=0), lambda line: None)
    url = await server.start()
    session, states = client(url)
    session.events()
    future = HELLO.model_copy(update={"payload": Hello(client="tests", protocol_version=99)})
    try:
        await session.send(future)
        assert await states.until(Ended) == Ended(url, "")
    finally:
        await session.close()
        await server.stop(served.close)


async def test_a_refused_token_is_reported_naming_its_file_and_not_retried() -> None:
    async with plane() as (fake, url):
        fake.status = 401
        session, states = client(url, "wrong")
        session.events()
        refused = await states.until(TokenRefused)
        assert refused == TokenRefused(messages.TOKEN_REFUSED.format(path=TOKEN_FILE))
        await asyncio.sleep(0.1)
        assert len(fake.headers) == 1
        await session.close()


async def test_a_token_still_refused_on_reconnecting_is_reported_naming_its_file() -> None:
    async with plane() as (fake, url):
        session, states = client(url)
        session.events()
        connection = await fake.connection()
        fake.status = 401
        await connection.close(1011, "Oops")
        refused = await states.until(TokenRefused)
        assert refused == TokenRefused(messages.TOKEN_REFUSED.format(path=TOKEN_FILE))
        await session.close()


@pytest.mark.parametrize(
    ("spoil", "message"),
    [
        (lambda path: path.unlink(), f"Can't read the token file {TOKEN_FILE}: "),
        (lambda path: path.write_text("\n"), f"The token file {TOKEN_FILE} is empty."),
    ],
)
async def test_a_token_file_unreadable_on_reconnecting_is_reported_naming_it(
    spoil: Callable[[Path], object], message: str
) -> None:
    async with plane() as (fake, url):
        session, states = client(url)
        session.events()
        connection = await fake.connection()
        spoil(TOKEN_FILE)
        await connection.close(1011, "Oops")
        refused = await states.until(TokenRefused)
        assert isinstance(refused, TokenRefused) and refused.message.startswith(message)
        assert len(fake.headers) == 1
        await session.close()


async def test_closing_ends_the_events_and_stops_reconnecting() -> None:
    async with plane() as (fake, url):
        session, states = client(url)
        events = session.events()
        connection = await fake.connection()
        await states.until(Connected)
        await session.close()
        await asyncio.wait_for(connection.wait_closed(), 5)
        assert [state async for state in events] == []
        assert states.seen == [Connected(url)]
        assert fake.connections.empty()


async def test_closing_before_using_it_ends_the_events_without_connecting() -> None:
    session, states = client("ws://127.0.0.1:9/")
    await session.close()
    assert [state async for state in session.events()] == []
    assert states.seen == [] and session.state is None


async def test_the_backoff_doubles_up_to_its_most_and_jitters() -> None:
    backoff = Backoff()
    assert [backoff.delay(failures, 0) for failures in range(7)] == [
        0.5,
        1.0,
        2.0,
        4.0,
        8.0,
        10.0,
        10.0,
    ]
    assert backoff.delay(0, 1) == 0.25
    assert backoff.delay(100_000, 0) == 10.0


async def test_it_talks_to_the_control_plane_server() -> None:
    served = AgentSession()
    server = ControlPlaneServer(served, lambda: TOKEN, ServerSettings(port=0), lambda line: None)
    url = await server.start()
    session, _ = client(url)
    events = session.events()
    try:
        await session.send(HELLO)
        kinds = [type((await next_event(events)).payload) for _ in range(3)]
        assert kinds[0] is Welcome and Snapshot in kinds
    finally:
        await session.close()
        await server.stop(served.close)


async def test_a_rotated_token_file_is_shown_on_reconnecting() -> None:
    tokens = [TOKEN]
    served = AgentSession()
    server = ControlPlaneServer(
        served, lambda: tokens[-1], ServerSettings(port=0), lambda line: None
    )
    url = await server.start()
    session, states = client(url)
    events = session.events()
    try:
        await session.send(HELLO)
        assert type((await next_event(events)).payload) is Welcome
        TOKEN_FILE.write_text("the-new-token\n")
        tokens.append("the-new-token")
        assert session._websocket is not None
        await session._websocket.close(1011)
        while type((await next_event(events)).payload) is not Welcome:
            pass
        assert [type(state) for state in states.seen] == [Connected, Reconnecting, Connected]
    finally:
        await session.close()
        await server.stop(served.close)


def test_messages_up_to_the_servers_default_limit_are_taken() -> None:
    assert ServerSettings().max_message_bytes == MAX_MESSAGE_BYTES


def test_both_limits_come_from_the_shared_default() -> None:
    assert ServerSettings().max_message_mb == DEFAULT_MAX_MESSAGE_MB
    assert MAX_MESSAGE_BYTES == DEFAULT_MAX_MESSAGE_MB * 1024 * 1024


def test_the_token_is_read_from_its_file(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text(" the-token \n")
    assert read_token(path) == "the-token"


def test_a_missing_token_file_is_named(tmp_path: Path) -> None:
    path = tmp_path / "token"
    with pytest.raises(TokenUnreadable, match=f"Can't read the token file {path}: "):
        read_token(path)


def test_an_empty_token_file_is_named(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("\n")
    with pytest.raises(TokenUnreadable, match=f"The token file {path} is empty."):
        read_token(path)
