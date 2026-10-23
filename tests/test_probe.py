"""Asking a control plane whether it takes a frontend's token."""

import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from orbit.client import TokenRejected, accepts
from tests.server_fakes import TOKEN


@asynccontextmanager
async def plane(status: int | None = None) -> AsyncIterator[str]:
    """The URL of a control plane that takes only ``TOKEN``, turning even that away with
    ``status`` if it's given."""

    def check(connection: ServerConnection, request: Request) -> Response | None:
        if status is not None:
            return connection.respond(status, "No.")
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return connection.respond(401, "No.")
        return None

    async def handle(connection: ServerConnection) -> None:
        await connection.wait_closed()

    async with serve(handle, "127.0.0.1", 0, process_request=check) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}/"


async def test_a_control_plane_taking_the_token_accepts_it() -> None:
    async with plane() as url:
        assert await accepts(url, TOKEN)


async def test_a_control_plane_refusing_the_token_rejects_it() -> None:
    async with plane() as url:
        with pytest.raises(TokenRejected):
            await accepts(url, "another")


async def test_a_control_plane_turning_the_upgrade_away_otherwise_is_unreachable() -> None:
    async with plane(503) as url:
        assert not await accepts(url, TOKEN)


async def test_nothing_listening_is_unreachable() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert not await accepts(f"ws://127.0.0.1:{port}/", TOKEN)
