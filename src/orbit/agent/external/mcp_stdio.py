"""Connecting to external MCP servers over stdio: each server's command is started as a child
process orbit owns, in a session of its own, and spoken to with the ``mcp`` library's client
session (:class:`~orbit.agent.external.mcp_client.ClientConnection`) over its stdin and stdout,
one JSON-RPC message a line.

The process gets only ``PATH`` and ``HOME`` from orbit's environment, then the server's own
variables. Closing the connection stops it, its whole process group terminated and then killed,
as :func:`~orbit.agent.external.processes.spawned` stops agents. Every request waits at most the
server's ``timeout_seconds``; a server that exits fails the requests in flight at once.
"""

import asyncio
import contextlib
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.client.session import ClientSession
from mcp.shared.message import SessionMessage
from mcp.types import jsonrpc_message_adapter

from orbit.agent.application.ports import UpstreamFailed, UpstreamSession
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.external.mcp_client import EXIT_GRACE_SECONDS, ClientConnection
from orbit.agent.external.processes import spawned, start_failure

MCP_DIR = "mcp"
"""The folder of the state directory shared servers' neutral directories are in."""

INHERITED = ("PATH", "HOME")
"""The variables of orbit's environment a server's process gets, besides its own."""

LINE_LIMIT = 16 * 1024 * 1024
"""The longest message a server may send, in bytes."""


def mcp_folder(state_dir: Path, workspace_id: str) -> Path:
    """The folder the neutral directories of the workspace ``workspace_id``'s shared servers are
    in, one per server, in ``state_dir``."""
    return state_dir / MCP_DIR / workspace_id


class StdioConnector:
    """Connects to MCP servers by starting their commands, with ``PATH`` and ``HOME`` from
    ``environ``, orbit's environment unless given."""

    def __init__(self, environ: Mapping[str, str] = os.environ) -> None:
        self._inherited = {name: environ[name] for name in INHERITED if name in environ}

    async def open(
        self, server: McpServer, cwd: str, env: Mapping[str, str], headers: Mapping[str, str]
    ) -> UpstreamSession:
        """A connection to ``server``, started in ``cwd``, made if it's missing, with ``env``
        over the variables it inherits; ``headers`` are for servers at a URL. Raises
        :class:`UpstreamFailed` if it has no command, or can't be started or initialised."""
        if server.command is None:
            raise UpstreamFailed("it's reached by a url, not a command")
        session = StdioSession(
            server.command, cwd, {**self._inherited, **env}, server.timeout_seconds
        )
        await session.start()
        return session


class StdioSession(ClientConnection):
    """A connection to the server ``command`` starts in ``cwd`` with the environment ``env``,
    each request waiting at most ``timeout`` seconds, once :meth:`start` has started it."""

    def __init__(
        self, command: Sequence[str], cwd: str, env: Mapping[str, str], timeout: float
    ) -> None:
        super().__init__(timeout)
        self._command = command
        self._cwd = cwd
        self._env = dict(env)

    async def _serve(self) -> str:
        """Run the server and the connection to it until the server exits; returns how it
        exited. Raises :class:`UpstreamFailed` if it can't be started or initialised."""
        try:
            await asyncio.to_thread(os.makedirs, self._cwd, exist_ok=True)
            async with spawned(
                self._command,
                cwd=self._cwd,
                env=self._env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=LINE_LIMIT,
            ) as process:
                return await self._connected(process)
        except OSError as exc:
            raise UpstreamFailed(start_failure(self._command, self._cwd, exc)) from None

    async def _connected(self, process: asyncio.subprocess.Process) -> str:
        """Speak MCP with ``process`` until it exits; returns how it exited."""
        assert process.stdin is not None and process.stdout is not None
        incoming, received = anyio.create_memory_object_stream[SessionMessage | Exception](0)
        outgoing, sent = anyio.create_memory_object_stream[SessionMessage](0)
        pipes = [
            asyncio.ensure_future(_read(process.stdout, incoming)),
            asyncio.ensure_future(_write(sent, process.stdin)),
        ]
        try:
            async with ClientSession(
                received, outgoing, read_timeout_seconds=self._timeout
            ) as session:
                await self._initialise(session, lambda: _exit(process))
                status = await process.wait()
        finally:
            for pipe in pipes:
                pipe.cancel()
            await asyncio.gather(*pipes, return_exceptions=True)
        return _exited(status)


async def _read(
    stdout: asyncio.StreamReader, incoming: MemoryObjectSendStream[SessionMessage | Exception]
) -> None:
    """Pass each line the server writes to ``stdout`` on to ``incoming`` as a message, or the
    error parsing it, until it ends; ``incoming`` is closed after."""
    async with incoming:
        while line := await stdout.readline():
            await incoming.send(_parsed(line))


async def _write(
    sent: MemoryObjectReceiveStream[SessionMessage], stdin: asyncio.StreamWriter
) -> None:
    """Write each message ``sent`` to the server's ``stdin``, a line each, until either ends."""
    with contextlib.suppress(OSError):
        async with sent:
            async for message in sent:
                encoded = message.message.model_dump_json(by_alias=True, exclude_unset=True)
                stdin.write(encoded.encode() + b"\n")
                await stdin.drain()


def _parsed(line: bytes) -> SessionMessage | Exception:
    """The message ``line`` holds, or why it holds none."""
    try:
        return SessionMessage(jsonrpc_message_adapter.validate_json(line, by_name=False))
    except ValueError as exc:
        return exc


def _exited(status: int) -> str:
    """How a server that exited with ``status`` stopped."""
    return f"it exited with status {status}"


async def _exit(process: asyncio.subprocess.Process) -> UpstreamFailed | None:
    """How the server's ``process`` exited, given a moment to; ``None`` if it hasn't."""
    try:
        status = await asyncio.wait_for(process.wait(), EXIT_GRACE_SECONDS)
    except TimeoutError:
        return None
    return UpstreamFailed(_exited(status))
