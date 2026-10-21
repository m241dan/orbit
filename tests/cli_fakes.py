"""Fakes for the CLI: the TUI it runs and the WebSocket session it connects."""

import asyncio
import ssl
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pytest

from orbit.agent import ControlPlane, WorkspaceHost
from orbit.agent.application.ports import UsageSource
from orbit.agent.domain.server import ServerSettings
from orbit.agent.external.control_plane import ControlPlaneServer
from orbit.cli.threaded import ThreadedSession
from orbit.protocol import PROTOCOL_VERSION, CommandEnvelope, Hello
from orbit.tui import Opening


@dataclass
class FakeClient:
    """The WebSocket session built: where to, shown the token read from which file, verified
    with which context."""

    url: str
    token_file: Path | None
    ssl_context: ssl.SSLContext | None = None


@dataclass
class FakeApp:
    """The TUI built over a control plane, run once."""

    built: ClassVar[list["FakeApp"]] = []

    session: object
    link: object = None
    opening: Opening = field(default_factory=Opening)
    storage_halt: str | None = None
    ran: bool = field(default=False)

    def __post_init__(self) -> None:
        FakeApp.built.append(self)

    def run(self) -> None:
        self.ran = True


def fake_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    """Have chat build a :class:`FakeApp` over a :class:`FakeClient`, with none built yet."""
    monkeypatch.setattr("orbit.cli.chat.OrbitApp", FakeApp)
    monkeypatch.setattr("orbit.cli.chat.WebSocketSession", FakeClient)
    FakeApp.built = []


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Have chat connect a :class:`FakeApp` over a :class:`FakeClient`, building no backend."""

    def no_backend(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("A connected chat builds no backend.")

    fake_tui(monkeypatch)
    monkeypatch.setattr("orbit.cli.chat._backend", no_backend)


class LocalApp:
    """Starts its session, which builds the backend on its thread, and closes it."""

    # The backend sessions built, the threads they were built on and their usage sources.
    launched: ClassVar[list[WorkspaceHost]] = []
    threads: ClassVar[list[str]] = []
    usage_sources: ClassVar[list[UsageSource | None]] = []
    # The files in profile_dir while each app ran async.
    profile_dir: ClassVar[Path] = Path()
    profiling: ClassVar[list[list[str]]] = []
    # What each app does with the backend session while it runs, if anything.
    during: ClassVar[Callable[[WorkspaceHost], Coroutine[Any, Any, None]] | None] = None
    # Why each app's session is halted as it exits, if it is.
    halted: ClassVar[str | None] = None
    # Whether each app was to have the user pick a session at startup.
    picks: ClassVar[list[bool]] = []
    # The repository each app was to open on the workspace of.
    repositories: ClassVar[list[str | None]] = []

    def __init__(self, session: ThreadedSession, *, opening: Opening) -> None:
        self.session = session
        self.storage_halt = LocalApp.halted
        LocalApp.picks.append(opening.pick)
        LocalApp.repositories.append(opening.repository)

    def run(self) -> None:
        asyncio.run(self._open_and_close())

    async def run_async(self) -> None:
        await self._open_and_close()

    async def _open_and_close(self) -> None:
        self.session.start()
        if LocalApp.profile_dir.exists():
            LocalApp.profiling.append(sorted(p.name for p in LocalApp.profile_dir.iterdir()))
        if LocalApp.during is not None:
            await self._on_backend(LocalApp.during(LocalApp.launched[-1]))
        await self.session.close()

    async def _on_backend(self, work: Coroutine[Any, Any, None]) -> None:
        """Run ``work`` on the backend's loop, where its session lives, and wait for it."""
        backend = self.session._backend
        assert backend is not None
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(work, backend.loop))


TOKEN = "the-token"


HELLO = CommandEnvelope(
    command_id="hello", payload=Hello(client="tests", protocol_version=PROTOCOL_VERSION)
)


@asynccontextmanager
async def control_plane(settings: ServerSettings) -> AsyncIterator[int]:
    """A control plane serving as ``settings`` say, and its port."""
    served = ControlPlane([WorkspaceHost()])
    server = ControlPlaneServer(served, lambda: TOKEN, settings, lambda line: None)
    url = await server.start()
    try:
        yield int(url.rstrip("/").rsplit(":", 1)[1])
    finally:
        await server.stop(served.close)
