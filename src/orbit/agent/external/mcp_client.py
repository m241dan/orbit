"""Speaking to an external MCP server with the ``mcp`` library's client session, however its
messages travel: a task the connection owns runs the transport and the session until the server
stops serving, or the connection is closed, and requests go on the session meanwhile.

Every request waits at most the server's ``timeout_seconds``; a server that stops serving fails
the requests in flight at once, saying why it stopped. A reply the client can't validate fails
its request too, as the server's failure.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Any

from mcp.client.session import ClientSession
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CONNECTION_CLOSED,
    REQUEST_TIMEOUT,
    ContentBlock,
    PaginatedRequestParams,
    TextContent,
    Tool,
)
from pydantic import ValidationError

from orbit.agent.application.ports import Reply, UpstreamFailed
from orbit.agent.domain.mcp_proxy import UpstreamTool
from orbit.agent.domain.refusals import reason
from orbit.execution.cancelling import stop_all

PAGE_LIMIT = 100
"""The most pages of tools read from a server."""

CLOSED = "the connection to it was closed"

UNREADABLE = "it sent a reply orbit can't read"
"""Why a request whose reply the client can't validate failed."""

EXIT_GRACE = timedelta(seconds=1)
"""How long a server whose connection dropped has to stop serving, so why it stopped is what's
reported."""

type Exited = Callable[[], Awaitable[UpstreamFailed | None]]
"""Why the server stopped serving, given a moment to; ``None`` if it hasn't."""


class ClientConnection(ABC):
    """A connection to an MCP server, each request waiting at most ``timeout``, once
    :meth:`start` has started it; :meth:`_serve` says how the server is reached."""

    def __init__(self, timeout: timedelta) -> None:
        self._timeout = timeout
        self._ready: asyncio.Future[ClientSession] = asyncio.get_running_loop().create_future()
        self._serving: asyncio.Task[str] | None = None

    async def start(self) -> None:
        """Start serving and initialise the connection. Raises :class:`UpstreamFailed` if it
        can't; cancelled, serving is stopped."""
        serving = self._serving = asyncio.ensure_future(self._serve())
        waits: set[asyncio.Future[Any]] = {self._ready, serving}
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            await stop_all([serving])
            raise
        if not self._ready.done():
            raise self._ended()

    async def list_tools(self) -> tuple[UpstreamTool, ...]:
        """The tools the server offers, every page of them, up to :data:`PAGE_LIMIT`. Raises
        :class:`UpstreamFailed` if the server fails, answers with an error, or answers with a
        list that can't be read."""
        try:
            tools = await self._pages(None, PAGE_LIMIT)
        except MCPError as exc:
            raise await self._failure(exc, self._served) from None
        except ValidationError:
            raise UpstreamFailed(UNREADABLE) from None
        return tuple(_upstream(tool) for tool in tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Reply:
        """The server's answer to a call to ``name`` with ``arguments``, an error if the tool,
        or the server, says the call is one. Raises :class:`UpstreamFailed` if the server
        fails, or answers with a result that can't be read."""
        session = self._ready.result()
        try:
            result = await session.call_tool(name, arguments)
        except MCPError as exc:
            if exc.code not in (REQUEST_TIMEOUT, CONNECTION_CLOSED):
                return Reply(exc.message, is_error=True)
            raise await self._failure(exc, self._served) from None
        except ValidationError:
            raise UpstreamFailed(UNREADABLE) from None
        return Reply(_join_text(result.content), is_error=bool(result.is_error))

    async def close(self) -> None:
        """Stop serving, if it still is, and wait for it to have stopped; why it stopped, if it
        had, was the requests' to say."""
        assert self._serving is not None
        await stop_all([self._serving])

    @abstractmethod
    async def _serve(self) -> str:
        """Run the transport and a session on it, initialised with :meth:`_initialise`, until
        the server stops serving; returns how it stopped. Raises if it can't be reached or
        initialised; :meth:`_explain_failure` says why the error means it stopped."""

    def _explain_failure(self, error: BaseException) -> UpstreamFailed:
        """Why serving stopped on ``error``, as it says."""
        return UpstreamFailed(reason(error))

    async def _initialise(self, session: ClientSession, exited: Exited) -> None:
        """Initialise ``session`` and make it the one requests go on. Raises
        :class:`UpstreamFailed` if the server doesn't answer, or refuses, why as ``exited`` says
        if it's stopped serving."""
        try:
            await session.initialize()
        except MCPError as exc:
            raise await self._failure(exc, exited) from None
        self._ready.set_result(session)

    async def _pages(self, cursor: str | None, left: int) -> list[Tool]:
        """The tools on the page at ``cursor`` and those after it, at most ``left`` pages."""
        params = PaginatedRequestParams(cursor=cursor)
        page = await self._ready.result().list_tools(params=params)
        if page.next_cursor is None or left == 1:
            return list(page.tools)
        return [*page.tools, *await self._pages(page.next_cursor, left - 1)]

    async def _failure(self, error: MCPError, exited: Exited) -> UpstreamFailed:
        """Why the server failed, as ``error`` from a request to it says, unless the connection
        dropped as it stopped serving, as ``exited`` says it has, given a moment to."""
        ended = await exited() if error.code == CONNECTION_CLOSED else None
        if ended is not None:
            return ended
        if error.code == REQUEST_TIMEOUT:
            return UpstreamFailed(self._word_timed_out())
        if error.code == CONNECTION_CLOSED:
            return UpstreamFailed("the connection to it dropped")
        return UpstreamFailed(error.message)

    def _word_timed_out(self) -> str:
        """Why a request the server didn't answer in time failed."""
        return f"it didn't answer within {self._timeout.total_seconds():g} seconds"

    async def _served(self) -> UpstreamFailed | None:
        """Why serving stopped, given a moment to; ``None`` if it hasn't."""
        assert self._serving is not None
        await asyncio.wait({self._serving}, timeout=EXIT_GRACE.total_seconds())
        return self._ended() if self._serving.done() else None

    def _ended(self) -> UpstreamFailed:
        """Why serving stopped, which it has."""
        assert self._serving is not None
        if self._serving.cancelled():
            return UpstreamFailed(CLOSED)
        error = self._serving.exception()
        if error is None:
            return UpstreamFailed(self._serving.result())
        cause = _cause(error)
        return cause if isinstance(cause, UpstreamFailed) else self._explain_failure(cause)


async def report_not_exited() -> UpstreamFailed | None:
    """Nothing: what's known of a server's having stopped serving while it's serving."""
    return None


def _upstream(tool: Tool) -> UpstreamTool:
    """``tool`` as a server offers it."""
    return UpstreamTool(tool.name, tool.description, tool.input_schema)


def _join_text(content: Sequence[ContentBlock]) -> str:
    """``content`` as text: each text block's text, and each other block as JSON, a line
    each."""
    return "\n".join(
        block.text if isinstance(block, TextContent) else block.model_dump_json(by_alias=True)
        for block in content
    )


def _cause(error: BaseException) -> BaseException:
    """The first error ``error`` groups, if it's a group, else ``error``."""
    return _cause(error.exceptions[0]) if isinstance(error, BaseExceptionGroup) else error
