"""The embedded uvicorn server: serving once it's up, cleaned up when it isn't, and stopping as soon
as it's told to and its connections and tasks have finished, within its graceful shutdown."""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import uvicorn
from starlette.applications import Starlette

from orbit.agent.application.ports import HostFailed
from orbit.agent.external.embedded_server import EmbeddedServer


def bound() -> socket.socket:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


def embedded() -> EmbeddedServer:
    return EmbeddedServer(uvicorn.Config(Starlette(), log_config=None, lifespan="off"))


async def hang(self: object, sockets: object = None) -> None:
    await asyncio.Event().wait()


async def forever() -> None:
    await asyncio.Event().wait()


async def test_a_started_server_serves_until_told_to_exit() -> None:
    server, sock = embedded(), bound()
    serving = await server.start_serving(sock, "It failed.")
    assert server.started and not serving.done()
    server.should_exit = True
    await serving


async def test_serving_that_ends_before_starting_raises_and_closes_the_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(self: object, sockets: object = None) -> None:
        raise OSError("no sockets for you")

    monkeypatch.setattr("uvicorn.Server.serve", broken)
    sock = bound()
    with pytest.raises(HostFailed, match=r"^It failed\.$") as raised:
        await embedded().start_serving(sock, "It failed.")
    assert isinstance(raised.value.__cause__, OSError)
    assert sock.fileno() == -1


async def test_starting_cancelled_stops_serving_and_closes_the_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("uvicorn.Server.serve", hang)
    server, sock = embedded(), bound()
    starting = asyncio.create_task(server.start_serving(sock, "It failed."))
    await asyncio.sleep(0.05)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert server.should_exit
    assert asyncio.all_tasks() == {asyncio.current_task()}
    assert sock.fileno() == -1


class Lingering:
    """A connection to ``server`` that closes ``after`` seconds once asked to shut down, or
    never."""

    def __init__(self, server: EmbeddedServer, after: float | None) -> None:
        self._server, self._after = server, after
        self.asked = False
        connections: set[Any] = server.server_state.connections
        connections.add(self)

    def shutdown(self) -> None:
        self.asked = True
        if self._after is not None:
            connections: set[Any] = self._server.server_state.connections
            asyncio.get_running_loop().call_later(self._after, connections.discard, self)


def with_lifespan(shutdowns: list[str], grace: int | None = None) -> EmbeddedServer:
    """A server whose app notes its lifespan's shutdown in ``shutdowns``, giving its connections
    ``grace`` seconds to close as it shuts down."""

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        shutdowns.append("shut down")

    app = Starlette(lifespan=lifespan)
    config = uvicorn.Config(app, log_config=None, timeout_graceful_shutdown=grace)
    return EmbeddedServer(config)


async def test_a_server_told_to_exit_stops_at_once_rather_than_on_its_next_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orbit.agent.external.embedded_server.TICK_SECONDS", 3600)
    shutdowns: list[str] = []
    server = with_lifespan(shutdowns)
    serving = await server.start_serving(bound(), "It failed.")
    await asyncio.sleep(0)  # Into its main loop.
    server.should_exit = True
    await asyncio.wait_for(serving, 5)
    assert shutdowns == ["shut down"]


async def test_a_stopping_server_waits_only_until_its_connections_have_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orbit.agent.external.embedded_server.SETTLE_SECONDS", 3600)
    server = embedded()
    serving = await server.start_serving(bound(), "It failed.")
    connection = Lingering(server, after=0.01)
    server.should_exit = True
    await asyncio.wait_for(serving, 5)
    assert connection.asked
    assert not server.server_state.connections


async def test_a_connection_open_past_the_settling_is_waited_for_until_it_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orbit.agent.external.embedded_server.SETTLE_SECONDS", 0)
    server = embedded()
    serving = await server.start_serving(bound(), "It failed.")
    Lingering(server, after=0.05)
    server.should_exit = True
    await asyncio.wait_for(serving, 5)
    assert not server.server_state.connections


async def test_past_the_grace_a_connection_still_open_is_left_and_the_tasks_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orbit.agent.external.embedded_server.SETTLE_SECONDS", 0)
    shutdowns: list[str] = []
    server = with_lifespan(shutdowns, grace=0)
    serving = await server.start_serving(bound(), "It failed.")
    Lingering(server, after=None)
    task = asyncio.create_task(forever())
    server.server_state.tasks.add(task)
    server.should_exit = True
    await asyncio.wait_for(serving, 5)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert shutdowns == ["shut down"]


async def test_tasks_left_running_are_waited_for_until_they_finish() -> None:
    server = embedded()
    serving = await server.start_serving(bound(), "It failed.")
    task = asyncio.create_task(asyncio.sleep(0.05))
    server.server_state.tasks.add(task)
    task.add_done_callback(server.server_state.tasks.discard)
    server.should_exit = True
    await asyncio.wait_for(serving, 5)
    assert task.done() and not task.cancelled()


async def test_a_server_forced_to_exit_waits_for_nothing_and_skips_the_lifespans_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orbit.agent.external.embedded_server.SETTLE_SECONDS", 0)
    shutdowns: list[str] = []
    server = with_lifespan(shutdowns)
    serving = await server.start_serving(bound(), "It failed.")
    Lingering(server, after=None)
    server.force_exit = True
    server.should_exit = True
    await asyncio.wait_for(serving, 5)
    assert shutdowns == []


def test_a_server_told_to_exit_can_be_told_to_carry_on() -> None:
    server = embedded()
    assert not server.should_exit
    server.should_exit = True
    assert server.should_exit
    server.should_exit = False
    assert not server.should_exit


async def test_a_server_forced_to_exit_while_waiting_on_a_connection_stops_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orbit.agent.external.embedded_server.SETTLE_SECONDS", 0)
    server = embedded()
    serving = await server.start_serving(bound(), "It failed.")
    connection = Lingering(server, after=None)
    server.should_exit = True
    while not connection.asked:
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)
    assert not serving.done()
    server.force_exit = True
    await asyncio.wait_for(serving, 5)
