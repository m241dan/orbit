"""Connecting to external MCP servers over stdio: each server's command is started as a child
process orbit owns, in a session of its own, and spoken to with the ``mcp`` library's client
session over its stdin and stdout, one JSON-RPC message a line.

The process gets only ``PATH`` and ``HOME`` from orbit's environment, then the server's own
variables. Closing the connection stops it, its whole process group terminated and then killed,
as :func:`~orbit.agent.external.processes.spawned` stops agents. Every request waits at most the
server's ``timeout_seconds``; a server that exits fails the requests in flight at once.
"""

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.client.session import ClientSession
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from mcp.types import (
    CONNECTION_CLOSED,
    REQUEST_TIMEOUT,
    ContentBlock,
    PaginatedRequestParams,
    TextContent,
    Tool,
    jsonrpc_message_adapter,
)

from orbit.agent.application.ports import Reply, UpstreamFailed, UpstreamSession
from orbit.agent.domain.mcp_proxy import UpstreamTool
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.domain.refusals import reason
from orbit.agent.external.processes import spawned, start_failure
from orbit.execution.cancelling import stop, stop_all

MCP_DIR = "mcp"
"""The folder of the state directory shared servers' neutral directories are in."""

INHERITED = ("PATH", "HOME")
"""The variables of orbit's environment a server's process gets, besides its own."""

LINE_LIMIT = 16 * 1024 * 1024
"""The longest message a server may send, in bytes."""

PAGE_LIMIT = 100
"""The most pages of tools read from a server."""

CLOSED = "the connection to it was closed"

EXIT_GRACE_SECONDS = 1.0
"""How long a server whose connection dropped has to exit, so its exit is what's reported."""


def mcp_folder(state_dir: Path, workspace_id: str) -> Path:
    """The folder the neutral directories of the workspace ``workspace_id``'s shared servers are
    in, one per server, in ``state_dir``."""
    return state_dir / MCP_DIR / workspace_id


class StdioConnector:
    """Connects to MCP servers by starting their commands, with ``PATH`` and ``HOME`` from
    ``environ``, orbit's environment unless given."""

    def __init__(self, environ: Mapping[str, str] = os.environ) -> None:
        self._inherited = {name: environ[name] for name in INHERITED if name in environ}

    async def open(self, server: McpServer, cwd: str, env: Mapping[str, str]) -> UpstreamSession:
        """A connection to ``server``, started in ``cwd``, made if it's missing, with ``env``
        over the variables it inherits. Raises :class:`UpstreamFailed` if it has no command, or
        can't be started or initialised."""
        if server.command is None:
            raise UpstreamFailed("it's reached by a url, not a command")
        session = StdioSession(
            server.command, cwd, {**self._inherited, **env}, server.timeout_seconds
        )
        await session.start()
        return session


class StdioSession:
    """A connection to the server ``command`` starts in ``cwd`` with the environment ``env``,
    each request waiting at most ``timeout`` seconds, once :meth:`start` has started it."""

    def __init__(
        self, command: Sequence[str], cwd: str, env: Mapping[str, str], timeout: float
    ) -> None:
        self._command = command
        self._cwd = cwd
        self._env = dict(env)
        self._timeout = timeout
        self._ready: asyncio.Future[ClientSession] = asyncio.get_running_loop().create_future()
        self._serving: asyncio.Task[str] | None = None

    async def start(self) -> None:
        """Start the server and initialise the connection to it. Raises
        :class:`UpstreamFailed` if it can't; cancelled, the server is stopped."""
        serving = self._serving = asyncio.ensure_future(self._serve())
        waits: set[asyncio.Future[Any]] = {self._ready, serving}
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            await stop_all([serving])
            raise
        if not self._ready.done():
            raise UpstreamFailed(self._ended())

    async def list_tools(self) -> tuple[UpstreamTool, ...]:
        """The tools the server offers, every page of them, up to :data:`PAGE_LIMIT`. Raises
        :class:`UpstreamFailed` if the server fails or answers with an error."""
        try:
            tools = await self._pages(None, PAGE_LIMIT)
        except MCPError as exc:
            raise UpstreamFailed(await self._failure(exc, self._served)) from None
        return tuple(_upstream(tool) for tool in tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Reply:
        """The server's answer to a call to ``name`` with ``arguments``, an error if the tool,
        or the server, says the call is one. Raises :class:`UpstreamFailed` if the server
        fails."""
        session = self._ready.result()
        try:
            result = await session.call_tool(name, arguments)
        except MCPError as exc:
            if exc.code not in (REQUEST_TIMEOUT, CONNECTION_CLOSED):
                return Reply(exc.message, is_error=True)
            raise UpstreamFailed(await self._failure(exc, self._served)) from None
        return Reply(_text(result.content), is_error=bool(result.is_error))

    async def close(self) -> None:
        """Stop the server, if it's running, and wait for it to have stopped."""
        assert self._serving is not None
        await stop(self._serving)

    async def _pages(self, cursor: str | None, left: int) -> list[Tool]:
        """The tools on the page at ``cursor`` and those after it, at most ``left`` pages."""
        params = PaginatedRequestParams(cursor=cursor)
        page = await self._ready.result().list_tools(params=params)
        if page.next_cursor is None or left == 1:
            return list(page.tools)
        return [*page.tools, *await self._pages(page.next_cursor, left - 1)]

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
                await self._initialise(session, process)
                status = await process.wait()
        finally:
            for pipe in pipes:
                pipe.cancel()
            await asyncio.gather(*pipes, return_exceptions=True)
        return _exited(status)

    async def _initialise(
        self, session: ClientSession, process: asyncio.subprocess.Process
    ) -> None:
        """Initialise ``session`` with the server's ``process`` and make it the one requests go
        on. Raises :class:`UpstreamFailed` if the server doesn't answer, or refuses."""
        try:
            await session.initialize()
        except MCPError as exc:
            raise UpstreamFailed(await self._failure(exc, lambda: _exit(process))) from None
        self._ready.set_result(session)

    async def _failure(self, error: MCPError, exited: Callable[[], Awaitable[str | None]]) -> str:
        """Why the server failed, as ``error`` from a request to it says, unless the connection
        dropped as it exited, as ``exited`` says it has, given a moment to."""
        ended = await exited() if error.code == CONNECTION_CLOSED else None
        if ended is not None:
            return ended
        if error.code == REQUEST_TIMEOUT:
            return f"it didn't answer within {self._timeout:g} seconds"
        if error.code == CONNECTION_CLOSED:
            return "the connection to it dropped"
        return error.message

    async def _served(self) -> str | None:
        """How the server stopped serving, given a moment to; ``None`` if it hasn't."""
        assert self._serving is not None
        await asyncio.wait({self._serving}, timeout=EXIT_GRACE_SECONDS)
        return self._ended() if self._serving.done() else None

    def _ended(self) -> str:
        """Why the server stopped serving, which it has."""
        assert self._serving is not None
        if self._serving.cancelled():
            return CLOSED
        error = self._serving.exception()
        return self._serving.result() if error is None else reason(_cause(error))


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


def _upstream(tool: Tool) -> UpstreamTool:
    """``tool`` as a server offers it."""
    return UpstreamTool(tool.name, tool.description, tool.input_schema)


def _text(content: Sequence[ContentBlock]) -> str:
    """``content`` as text: each text block's text, and each other block as JSON, a line
    each."""
    return "\n".join(
        block.text if isinstance(block, TextContent) else block.model_dump_json(by_alias=True)
        for block in content
    )


def _exited(status: int) -> str:
    """How a server that exited with ``status`` stopped."""
    return f"it exited with status {status}"


async def _exit(process: asyncio.subprocess.Process) -> str | None:
    """How the server's ``process`` exited, given a moment to; ``None`` if it hasn't."""
    try:
        status = await asyncio.wait_for(process.wait(), EXIT_GRACE_SECONDS)
    except TimeoutError:
        return None
    return _exited(status)


def _cause(error: BaseException) -> BaseException:
    """The first error ``error`` groups, if it's a group, else ``error``."""
    return _cause(error.exceptions[0]) if isinstance(error, BaseExceptionGroup) else error
