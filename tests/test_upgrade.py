"""Opening a WebSocket to a control plane, showing a frontend's token, as the probe and the
session both do."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from orbit.client import TokenRejected, WebSocketSession, accepts, probe, session
from orbit.client.upgrade import open_connection
from orbit.protocol import UNAUTHORIZED, LinkState, TokenRefused, bearer_header

TOKEN = "the-token"


@asynccontextmanager
async def plane(status: int | None = None) -> AsyncIterator[tuple[str, list[str | None]]]:
    """The URL of a control plane turning upgrades away with ``status`` if it's given, and the
    ``Authorization`` header each upgrade showed."""
    shown: list[str | None] = []

    def check(connection: ServerConnection, request: Request) -> Response | None:
        shown.append(request.headers.get("Authorization"))
        return None if status is None else connection.respond(status, "No.")

    async def handle(connection: ServerConnection) -> None:
        await connection.wait_closed()

    async with serve(handle, "127.0.0.1", 0, process_request=check) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}/", shown


def test_the_bearer_header_shows_the_token() -> None:
    assert bearer_header(TOKEN) == {"Authorization": f"Bearer {TOKEN}"}


async def test_a_connection_shows_the_token_on_the_upgrade() -> None:
    async with plane() as (url, shown):
        websocket = await open_connection(url, TOKEN, max_size=1024)
        assert websocket is not None
        await websocket.close()
        assert shown == [f"Bearer {TOKEN}"]


async def test_an_unauthorized_upgrade_rejects_the_token() -> None:
    async with plane(UNAUTHORIZED) as (url, _):
        with pytest.raises(TokenRejected):
            await open_connection(url, TOKEN)


async def test_an_upgrade_turned_away_otherwise_makes_no_connection() -> None:
    async with plane(503) as (url, _):
        assert await open_connection(url, TOKEN) is None


async def rejecting(url: str, token: str, **options: Any) -> ClientConnection | None:
    """Turns every token away, as the shared opener does on a 401."""
    raise TokenRejected()


async def test_the_probe_is_turned_away_through_the_shared_opener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "open_connection", rejecting)
    with pytest.raises(TokenRejected):
        await accepts("ws://127.0.0.1:1/", TOKEN)


async def test_the_session_is_turned_away_through_the_shared_opener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(session, "open_connection", rejecting)
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    states: asyncio.Queue[LinkState] = asyncio.Queue()
    websocket_session = WebSocketSession("ws://127.0.0.1:1/", token_file)
    websocket_session.watch(states.put_nowait)
    websocket_session.events()
    refused = await asyncio.wait_for(states.get(), 5)
    assert refused == TokenRefused(f"The control plane refused the token from {token_file}.")
    await websocket_session.close()
