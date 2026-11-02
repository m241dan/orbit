"""Connecting to each external MCP server the way it's reached: by starting its command, or at
its URL."""

from collections.abc import Mapping

from orbit.agent.application.ports import McpConnector, UpstreamSession
from orbit.agent.domain.mcp_servers import McpServer


class TransportConnector:
    """Connects to MCP servers with ``stdio`` if they have a command, else with ``http``."""

    def __init__(self, stdio: McpConnector, http: McpConnector) -> None:
        self.stdio = stdio
        self.http = http

    async def open(
        self, server: McpServer, cwd: str, env: Mapping[str, str], headers: Mapping[str, str]
    ) -> UpstreamSession:
        """A connection to ``server``, made by the connector for how it's reached, with ``cwd``,
        ``env`` and ``headers`` as :meth:`McpConnector.open` takes them. Raises
        :class:`~orbit.agent.application.ports.UpstreamFailed` when it can't connect."""
        connector = self.stdio if server.command is not None else self.http
        return await connector.open(server, cwd, env, headers)
