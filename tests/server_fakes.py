"""A real control plane server serving on loopback, and the token, URL and task the tests share."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from orbit.agent.domain.server import ServerSettings
from orbit.agent.external.control_plane import Connectable, ControlPlaneServer, Say
from orbit.protocol import TaskSpec

TOKEN = "the-token"


URL = "ws://127.0.0.1:7433/"
"""Where an orbit serving on the default port is."""


LOOPBACK = ServerSettings(port=0)


TASK = TaskSpec(prompt="Add a flag.")


class Lines:
    """The lines the server said."""

    def __init__(self) -> None:
        self.said: list[str] = []

    def __call__(self, line: str) -> None:
        self.said.append(line)


type Closer = Callable[[], Awaitable[None]]
type Tokens = Callable[[], str]


def the_token() -> str:
    return TOKEN


def quiet(line: str) -> None:
    pass


@asynccontextmanager
async def serving_plane(
    plane: Connectable,
    close: Closer,
    settings: ServerSettings = LOOPBACK,
    token: Tokens = the_token,
    say: Say = quiet,
) -> AsyncIterator[tuple[str, ControlPlaneServer]]:
    """The URL of a server of ``plane`` serving as ``settings`` say, showing the token ``token``
    returns and telling ``say``, and the server; stopped after, closing with ``close``."""
    server = ControlPlaneServer(plane, token, settings, say)
    url = await server.start()
    try:
        yield url, server
    finally:
        await server.stop(close)
