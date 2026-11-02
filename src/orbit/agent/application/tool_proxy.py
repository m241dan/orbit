"""Proxying external MCP servers' tools to the agents whose roles allow them, for one workspace.

Orbit connects to each server itself, lazily, on the first ``tools/list`` or call that needs
it, and keeps the server's tool list for as long as the connection lasts. A ``shared`` server
has one connection for the workspace, started in a neutral directory of its own; an unshared
one has one per agent, started in the agent's own directory and closed when its run ends.
Either is closed once idle for its ``idle_minutes``.

A server that fails to start, crashes, times out or drops the connection fails the call with
the reason, and its connection is discarded, so the next call starts it afresh; the first
failure of a streak publishes a notice, and a call it answers ends the streak. Each proxied call
is traced in an ``orbit.mcp.call`` span, linked to the calling agent's span, with the server,
orbit's name for the tool and the outcome, never the arguments or the result.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Literal

from orbit.agent.application.failures import unexpected
from orbit.agent.application.ports import (
    Credentials,
    CredentialsUnreadable,
    McpConnector,
    Publish,
    Reply,
    UpstreamFailed,
    UpstreamSession,
)
from orbit.agent.domain.mcp_proxy import (
    SEPARATOR,
    ProxiedTool,
    UnsetVariable,
    UpstreamTool,
    call_failure,
    failure_notice,
    filled_env,
    find_proxied,
    grant_for,
    missing_tool_notices,
    proxied_tools,
    refusal,
    variables_in,
)
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.domain.refusals import failed_to
from orbit.agent.domain.roles import Role, ServerGrant
from orbit.execution.background import Background
from orbit.execution.cancelling import stop_all
from orbit.protocol import INTERNAL, ErrorOccurred, Notice
from orbit.tracing import NoTelemetry, Span, Telemetry, no_span

CALL_SPAN = "orbit.mcp.call"
SERVER = "orbit.mcp.server"
TOOL = "orbit.mcp.tool"
OUTCOME = "orbit.mcp.outcome"
REFUSED = "orbit.mcp.refused"
"""The event recorded on an agent's span when a call it makes to a proxied tool is refused."""

type Outcome = Literal["ok", "tool_error", "failed", "refused", "stopped"]
"""How a proxied call came out: answered, answered with the tool's error, failed by the server,
refused, or stopped before it was answered."""

STOPPED = Reply("Orbit stopped this call: the agent's run ended or orbit is closing.", True)
CLOSING = "orbit is closing"

_NO_TELEMETRY = NoTelemetry()


@dataclass(frozen=True)
class McpProxying:
    """What proxies MCP servers' tools to a workspace's agents: the ``servers`` declared, by
    name, the ``connector`` that connects to them, the ``credentials`` their environments are
    filled from, the ``folder`` the shared ones' neutral directories are in, one per server, and
    how to ``sleep`` a number of seconds while a connection is idle."""

    servers: Mapping[str, McpServer]
    connector: McpConnector
    credentials: Credentials
    folder: str
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass(frozen=True)
class Caller:
    """The agent making a call or listing its tools: its id, ``agent_id``, the name of its role,
    ``role_name``, the ``role`` itself, the directory it works in, ``cwd``, and the ``span`` of
    its process."""

    agent_id: str
    role_name: str
    role: Role
    cwd: str
    span: Span


@dataclass(frozen=True)
class _Failure:
    """Why a server couldn't be connected to: the ``reason``."""

    reason: str


class _Connection:
    """An open ``session`` to a server, the ``tools`` it offers, the calls on it in flight, and
    ``idle``, which closes it once it has none for a while."""

    def __init__(self, session: UpstreamSession, tools: tuple[UpstreamTool, ...], idle: Background):
        self.session = session
        self.tools = tools
        self.idle = idle
        self.calls = 0

    async def close(self) -> None:
        """Stop waiting for it to idle, and close the session."""
        await self.idle.close()
        await self.session.close()


type _Key = tuple[str, str | None]
"""What a connection is for: its server, and the agent it's that agent's own for, if unshared."""

type _Link = asyncio.Task[_Connection | _Failure]
"""A connection being opened, or opened, or why it couldn't be."""


@dataclass(frozen=True)
class _Opened:
    """The ``connection`` opened by ``link``, kept under ``key`` while it lasts."""

    key: _Key
    link: _Link
    connection: _Connection


class ToolProxy:
    """Proxies the MCP servers of ``proxying`` to the agents whose roles, among those ``roles``
    gives as they stand, allow them, publishing notices with ``publish`` and tracing calls with
    ``telemetry``."""

    def __init__(
        self,
        proxying: McpProxying,
        roles: Callable[[], Mapping[str, Role]],
        publish: Publish,
        telemetry: Telemetry = _NO_TELEMETRY,
    ) -> None:
        self._proxying = proxying
        self._roles = roles
        self._publish = publish
        self._telemetry = telemetry
        self._links: dict[_Key, _Link] = {}
        self._calls: dict[str, set[asyncio.Task[Reply]]] = {}  # In flight, by agent.
        self._closing: set[asyncio.Task[None]] = set()  # Connections being closed.
        self._failing: set[str] = set()  # The servers in a streak of failures.
        self._checked: set[str] = set()  # The servers whose tools were checked against roles.
        self._closed = False

    async def list_tools(self, caller: Caller) -> tuple[ProxiedTool, ...]:
        """The proxied tools ``caller``'s role allows, connecting to the servers it names; a
        server that can't be connected to gives none."""
        grants = [grant for grant in caller.role.tools.mcp if self._proxied(grant.server)]
        found = await asyncio.gather(*(self._opened(caller, grant.server) for grant in grants))
        return tuple(
            tool
            for grant, opened in zip(grants, found, strict=True)
            for tool in _allowed(grant, opened)
        )

    async def call(self, caller: Caller, name: str, arguments: dict[str, Any]) -> Reply:
        """The answer to ``caller``'s call to the proxied tool ``name`` with ``arguments``: the
        server's, or why it's refused or failed. Cancelled, the call upstream is cancelled too;
        stopped by :meth:`release` or :meth:`close`, it's answered with an error."""
        if self._closed:
            return Reply(call_failure(name.partition(SEPARATOR)[0], CLOSING), is_error=True)
        task = asyncio.ensure_future(self._call(caller, name, arguments))
        calls = self._calls.setdefault(caller.agent_id, set())
        calls.add(task)
        task.add_done_callback(calls.discard)
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            task.cancel()
            raise
        return STOPPED if task.cancelled() else task.result()

    def release(self, agent_id: str) -> None:
        """Stop agent ``agent_id``'s calls in flight, and close its own connections, as its run
        ends."""
        for task in self._calls.pop(agent_id, set()):
            task.cancel()
        for key, link in list(self._links.items()):
            if key[1] == agent_id:
                self._forget(key, link)

    async def close(self) -> None:
        """Stop every call in flight, answering it with an error, and close every connection,
        stopping the servers started for them; nothing is connected to from now on."""
        self._closed = True
        await stop_all([task for calls in self._calls.values() for task in calls])
        for key, link in list(self._links.items()):
            self._forget(key, link)
        await asyncio.gather(*list(self._closing), return_exceptions=True)

    def _proxied(self, server: str) -> bool:
        """Whether ``server`` is declared, and proxied: started by a command, as URL servers
        aren't proxied yet."""
        settings = self._proxying.servers.get(server)
        return settings is not None and settings.command is not None

    async def _call(self, caller: Caller, name: str, arguments: dict[str, Any]) -> Reply:
        """The answer to ``caller``'s call to ``name``, traced unless refused out of hand."""
        server = name.partition(SEPARATOR)[0]
        grant = grant_for(caller.role, server)
        if grant is None or not self._proxied(server):
            return self._refuse(caller, name)
        attributes = {SERVER: server, TOOL: name}
        with self._telemetry.span(CALL_SPAN, attributes, links=[caller.span]) as span:
            try:
                reply, outcome = await self._answer(caller, grant, name, arguments)
            except asyncio.CancelledError:
                span.set_attributes({OUTCOME: "stopped"})
                raise
            span.set_attributes({OUTCOME: outcome})
            if outcome in ("failed", "refused"):
                span.error(reply.text)
            return reply

    async def _answer(
        self, caller: Caller, grant: ServerGrant, name: str, arguments: dict[str, Any]
    ) -> tuple[Reply, Outcome]:
        """The answer to ``caller``'s call to ``name`` on the server ``grant`` allows it, and how
        it came out."""
        server = grant.server
        opened = await self._opened(caller, server)
        if isinstance(opened, _Failure):
            return Reply(call_failure(server, opened.reason), is_error=True), "failed"
        tool = find_proxied(proxied_tools(server, grant, opened.connection.tools), name)
        if tool is None:
            return self._refuse(caller, name), "refused"
        return await self._run(opened, tool, arguments)

    async def _run(
        self, opened: _Opened, tool: ProxiedTool, arguments: dict[str, Any]
    ) -> tuple[Reply, Outcome]:
        """The server's answer to a call to ``tool`` with ``arguments`` on ``opened``, and how it
        came out; a failure discards the connection."""
        connection = opened.connection
        connection.calls += 1
        try:
            await connection.idle.stop()
            reply = await connection.session.call_tool(tool.tool.name, arguments)
        except UpstreamFailed as exc:
            self._forget(opened.key, opened.link)
            return await self._failed(tool.server, str(exc)), "failed"
        finally:
            connection.calls -= 1
            self._rest(opened)
        self._failing.discard(tool.server)
        return reply, "tool_error" if reply.is_error else "ok"

    def _refuse(self, caller: Caller, name: str) -> Reply:
        """The refusal of ``caller``'s call to ``name``, recorded on its span."""
        caller.span.event(REFUSED, {TOOL: name})
        return Reply(refusal(caller.role_name, name), is_error=True)

    async def _failed(self, server: str, reason: str) -> Reply:
        """The answer to a call ``server`` failed for ``reason``, noticed if it starts a
        streak."""
        if server not in self._failing:
            self._failing.add(server)
            await self._publish(Notice(message=failure_notice(server, reason)))
        return Reply(call_failure(server, reason), is_error=True)

    async def _opened(self, caller: Caller, server: str) -> _Opened | _Failure:
        """``caller``'s connection to ``server``, opened if there's none, or why it couldn't be;
        the first failure of a streak is noticed."""
        if self._closed:
            return _Failure(CLOSING)
        key: _Key = (server, None if self._proxying.servers[server].shared else caller.agent_id)
        link = self._links.get(key) or self._open_link(key, caller)
        await asyncio.wait({link})
        return await self._resolved(key, link)

    async def _resolved(self, key: _Key, link: _Link) -> _Opened | _Failure:
        """The connection ``link``, done, opened for ``key``, or why it wasn't; a failure is
        forgotten, and noticed if it starts a streak."""
        if link.cancelled():
            return _Failure(CLOSING)
        try:
            result = link.result()
        except Exception:  # A bug: raised to the caller, the link forgotten, to try again.
            self._forget(key, link)
            raise
        if isinstance(result, _Failure):
            self._forget(key, link)
            await self._failed(key[0], result.reason)
            return result
        opened = _Opened(key, link, result)
        self._rest(opened)
        return opened

    def _open_link(self, key: _Key, caller: Caller) -> _Link:
        """A connection being opened for ``key``, for ``caller``, kept under it."""
        with no_span():  # It outlives the call that opens it, serving others.
            link = asyncio.ensure_future(self._open(key, caller))
        self._links[key] = link
        return link

    async def _open(self, key: _Key, caller: Caller) -> _Connection | _Failure:
        """A connection to the server ``key`` names, started in ``caller``'s directory if it's
        the agent's own, else in the server's neutral directory, or why it couldn't be."""
        server = key[0]
        settings = self._proxying.servers[server]
        env = self._environment(settings)
        if isinstance(env, _Failure):
            return env
        cwd = caller.cwd if key[1] is not None else str(PurePath(self._proxying.folder, server))
        try:
            session = await self._proxying.connector.open(settings, cwd, env)
        except UpstreamFailed as exc:
            return _Failure(str(exc))
        return await self._listed(server, session)

    def _environment(self, settings: McpServer) -> dict[str, str] | _Failure:
        """The environment ``settings`` give their server, its variables filled in from the
        credentials, or why it can't be."""
        try:
            values = {
                name: value
                for name in variables_in(settings.env)
                if (value := self._proxying.credentials(name)) is not None
            }
        except CredentialsUnreadable as exc:
            return _Failure(str(exc))
        filled = filled_env(settings.env, values)
        return _Failure(filled.message) if isinstance(filled, UnsetVariable) else filled

    async def _listed(self, server: str, session: UpstreamSession) -> _Connection | _Failure:
        """``session`` to ``server`` as a connection with the tools it offers, checked against
        the roles' lists, or why they couldn't be listed, the session closed."""
        try:
            tools = await session.list_tools()
        except UpstreamFailed as exc:
            await session.close()
            return _Failure(str(exc))
        except BaseException:
            await session.close()
            raise
        await self._check_tools(server, tools)
        return _Connection(session, tools, Background(self._crashed))

    async def _check_tools(self, server: str, tools: tuple[UpstreamTool, ...]) -> None:
        """Notice each tool a role lists that ``server`` doesn't offer among ``tools``, the first
        time it's connected to."""
        if server in self._checked:
            return
        self._checked.add(server)
        for notice in missing_tool_notices(server, self._roles(), tools):
            await self._publish(Notice(message=notice))

    def _rest(self, opened: _Opened) -> None:
        """Have ``opened`` closed once idle for its server's ``idle_minutes``, if no call is in
        flight on it and it's still kept."""
        if opened.connection.calls == 0 and self._links.get(opened.key) is opened.link:
            opened.connection.idle.start(self._close_idle(opened))

    async def _close_idle(self, opened: _Opened) -> None:
        """Close ``opened`` once its server's ``idle_minutes`` have passed."""
        minutes = self._proxying.servers[opened.key[0]].idle_minutes
        await self._proxying.sleep(minutes * 60)
        self._forget(opened.key, opened.link)

    def _forget(self, key: _Key, link: _Link) -> None:
        """Stop keeping ``link`` under ``key``, if it's kept there, and close its connection in
        the background."""
        if self._links.get(key) is not link:
            return
        del self._links[key]
        with no_span():
            closing = asyncio.ensure_future(_end(link))
        self._closing.add(closing)
        closing.add_done_callback(self._closing.discard)

    async def _crashed(self, exc: Exception) -> None:
        """Report ``exc``, which escaped waiting for a connection to idle."""
        message = unexpected(failed_to("close an idle MCP connection", exc), exc)
        await self._publish(ErrorOccurred(code=INTERNAL, message=message))


def _allowed(grant: ServerGrant, opened: _Opened | _Failure) -> tuple[ProxiedTool, ...]:
    """The tools ``grant`` allows of those its server offers on ``opened``; none if it couldn't
    be connected to."""
    if isinstance(opened, _Failure):
        return ()
    return proxied_tools(grant.server, grant, opened.connection.tools)


async def _end(link: _Link) -> None:
    """Stop opening ``link``, if it still is, and close the connection it opened, if any."""
    link.cancel()
    await asyncio.wait({link})
    if link.cancelled() or link.exception() is not None:
        return
    connection = link.result()
    if isinstance(connection, _Connection):
        await connection.close()
