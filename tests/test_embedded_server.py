"""Starting the embedded uvicorn server: serving once it's up, cleaned up when it isn't."""

import asyncio
import socket

import pytest
import uvicorn
from starlette.applications import Starlette

from orbit.agent.external.embedded_server import EmbeddedServer


def bound() -> socket.socket:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


def embedded() -> EmbeddedServer:
    return EmbeddedServer(uvicorn.Config(Starlette(), log_config=None, lifespan="off"))


async def hang(self: object, sockets: object = None) -> None:
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
    with pytest.raises(RuntimeError, match=r"^It failed\.$") as raised:
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
