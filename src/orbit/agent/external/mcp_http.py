"""Connecting to external MCP servers over streamable HTTP: each server's URL is reached with the
``mcp`` library's streamable HTTP client, on an ``httpx2`` client of the connection's own that
sends the server's headers with every request, and spoken to with the library's client session
(:class:`~orbit.agent.external.mcp_client.ClientConnection`).

An answer with an HTTP error status ends the connection, so the next call connects afresh; 401
and 403 say the server refused orbit's credentials. A connection refused, or a TLS handshake
failed, fails it too, as does connecting, or waiting for an answer, past the server's
``timeout_seconds``. The headers, which may hold credentials, never reach a log or an error.
"""

import asyncio
from collections.abc import Mapping

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from orbit.agent.application.ports import CredentialsRefused, UpstreamFailed, UpstreamSession
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.domain.refusals import reason
from orbit.agent.external.mcp_client import ClientConnection, not_exited

REFUSING = frozenset({401, 403})
"""The HTTP statuses a server refusing orbit's credentials answers with."""

ERROR_STATUS = 400
"""The lowest HTTP status that's an error."""

BACKSTOP_SECONDS = 1.0
"""How much longer than the HTTP client the session waits for an answer, so a server that
doesn't answer in time is noticed by the client, which ends the connection, and the session's
timeout is left to catch only an answer that trickles in."""


class ErrorStatus(Exception):
    """A server answered with the HTTP error ``status``."""

    def __init__(self, status: int) -> None:
        super().__init__(f"it answered with HTTP {status}")
        self.status = status


class HttpConnector:
    """Connects to MCP servers at their URLs."""

    async def open(
        self, server: McpServer, cwd: str, env: Mapping[str, str], headers: Mapping[str, str]
    ) -> UpstreamSession:
        """A connection to ``server`` at its URL, sending ``headers`` with every request;
        ``cwd`` and ``env`` are for servers orbit starts. Raises :class:`UpstreamFailed` if it
        has no URL, or can't be reached or initialised."""
        if server.url is None:
            raise UpstreamFailed("it's started by a command, not reached by a url")
        session = HttpSession(server.url, headers, server.timeout_seconds)
        await session.start()
        return session


class HttpSession(ClientConnection):
    """A connection to the server at ``url``, sending ``headers`` with every request, connecting
    and each request waiting at most ``timeout`` seconds, once :meth:`start` has started it."""

    def __init__(self, url: str, headers: Mapping[str, str], timeout: float) -> None:
        super().__init__(timeout)
        self._url = url
        self._headers = dict(headers)

    async def _serve(self) -> str:
        """Run the connection until it fails or is closed. Raises if the server can't be
        reached or initialised."""
        forever: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        hooks = {"response": [_check_status]}
        async with (
            httpx2.AsyncClient(
                headers=self._headers, timeout=self._timeout, event_hooks=hooks
            ) as client,
            streamable_http_client(self._url, http_client=client) as (received, sent),
            ClientSession(
                received, sent, read_timeout_seconds=self._timeout + BACKSTOP_SECONDS
            ) as session,
        ):
            await self._initialise(session, not_exited)
            return await forever

    def _failed(self, error: BaseException) -> UpstreamFailed:
        """Why the connection failed on ``error``: an HTTP error status, a timeout, a connection
        that couldn't be made, or as ``error`` says."""
        if isinstance(error, ErrorStatus) and error.status in REFUSING:
            return CredentialsRefused(error.status)
        if isinstance(error, httpx2.TimeoutException):
            return UpstreamFailed(self._timed_out())
        if isinstance(error, httpx2.ConnectError):
            return UpstreamFailed(f"orbit couldn't connect to it: {reason(error)}")
        return super()._failed(error)


async def _check_status(response: httpx2.Response) -> None:
    """Raise :class:`ErrorStatus` if ``response``'s status is an HTTP error."""
    if response.status_code >= ERROR_STATUS:
        raise ErrorStatus(response.status_code)
