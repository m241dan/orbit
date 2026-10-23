"""The health checks ``orbit serve`` answers on its main port, without the token."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from orbit.agent import ControlPlane, WorkspaceHost
from orbit.agent.application.connection import SessionConnection
from orbit.agent.domain.health import Readiness, readiness, ready_answer
from orbit.agent.external.control_plane import ControlPlaneServer
from tests.server_fakes import LOOPBACK, TOKEN, quiet, serving_plane

HEALTH_JSON = "application/health+json"


class Standing:
    """A session whose ``read_only`` and ``storage_halted`` the test sets."""

    def __init__(self) -> None:
        self.session = ControlPlane([WorkspaceHost()])
        self.read_only = False
        self.storage_halted = False

    def connect(self) -> SessionConnection:
        return self.session.connect()


@asynccontextmanager
async def health_client(standing: Standing) -> AsyncIterator[httpx.AsyncClient]:
    """A client of a server of ``standing``, at its base URL, stopped and closed after."""
    async with (
        serving_plane(standing, standing.session.close) as (url, _),
        httpx.AsyncClient(base_url=url.replace("ws://", "http://")) as client,
    ):
        yield client


def test_a_server_accepting_with_storage_working_is_ready() -> None:
    assert readiness(accepting=True, stopping=False, halted=False, read_only=False) == "pass"


def test_a_server_not_yet_accepting_is_starting() -> None:
    assert readiness(accepting=False, stopping=False, halted=False, read_only=False) == "starting"


def test_a_server_stopping_is_shutting_down_whatever_else_holds() -> None:
    state = readiness(accepting=True, stopping=True, halted=True, read_only=True)
    assert state == "shutting_down"


def test_a_halt_outranks_being_read_only() -> None:
    state = readiness(accepting=True, stopping=False, halted=True, read_only=True)
    assert state == "storage_halted"


def test_a_read_only_server_says_so() -> None:
    state = readiness(accepting=True, stopping=False, halted=False, read_only=True)
    assert state == "read_only"


@pytest.mark.parametrize(
    ("state", "answer"),
    [
        ("pass", (200, {"status": "pass"})),
        ("read_only", (200, {"status": "warn", "output": "read_only"})),
        ("starting", (503, {"status": "fail", "output": "starting"})),
        ("shutting_down", (503, {"status": "fail", "output": "shutting_down"})),
        ("storage_halted", (503, {"status": "fail", "output": "storage_halted"})),
    ],
)
def test_readyz_answers_each_state_as_the_health_check_draft_does(
    state: Readiness, answer: tuple[int, dict[str, str]]
) -> None:
    assert ready_answer(state) == answer


async def test_healthz_passes_without_the_token() -> None:
    async with health_client(Standing()) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.headers["content-type"] == HEALTH_JSON
    assert response.json() == {"status": "pass"}


async def test_readyz_passes_once_serving() -> None:
    async with health_client(Standing()) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.headers["content-type"] == HEALTH_JSON
    assert response.json() == {"status": "pass"}


async def test_readyz_warns_while_read_only() -> None:
    standing = Standing()
    standing.read_only = True
    async with health_client(standing) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "warn", "output": "read_only"}


async def test_readyz_fails_while_storage_is_halted() -> None:
    standing = Standing()
    async with health_client(standing) as client:
        standing.storage_halted = True
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.headers["content-type"] == HEALTH_JSON
    assert response.json() == {"status": "fail", "output": "storage_halted"}


async def test_a_server_is_starting_until_it_accepts() -> None:
    standing = Standing()
    server = ControlPlaneServer(standing, lambda: TOKEN, LOOPBACK, quiet)
    assert server.readiness() == "starting"
    await server.start()
    assert server.readiness() == "pass"
    await server.stop(standing.session.close)


async def test_readyz_fails_once_shutdown_begins() -> None:
    standing = Standing()
    server = ControlPlaneServer(standing, lambda: TOKEN, LOOPBACK, quiet)
    url = await server.start()
    closing, release = asyncio.Event(), asyncio.Event()

    async def close_session() -> None:
        closing.set()
        await release.wait()

    async with httpx.AsyncClient(base_url=url.replace("ws://", "http://")) as client:
        assert (await client.get("/readyz")).status_code == 200
        stopping = asyncio.create_task(server.stop(close_session))
        await closing.wait()
        response = await client.get("/readyz")  # Over the connection already open.
    release.set()
    await stopping
    await standing.session.close()
    assert response.status_code == 503
    assert response.json() == {"status": "fail", "output": "shutting_down"}


async def test_the_websocket_still_needs_the_token() -> None:
    async with health_client(Standing()) as client:
        url = str(client.base_url).replace("http://", "ws://")
        with pytest.raises(InvalidStatus) as refused:
            async with connect(url):
                pass
    assert refused.value.response.status_code == 401
