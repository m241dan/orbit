"""Proxying a server at a URL to an agent whose role allows it, through the HTTP connector, to
the HTTP stand-in on a loopback port: its tools listed and called with its headers filled from
the credentials, a stopped server's failure noticed once and reconnected to once it's back, and
a refusal of orbit's credentials."""

from collections.abc import AsyncIterator

import pytest

from orbit.agent.application.ports import Reply
from orbit.agent.application.tool_proxy import Caller, McpProxying, ToolProxy
from orbit.agent.domain.mcp_proxy import UpstreamTool
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.domain.roles import Role, RoleTools
from orbit.agent.external.mcp_http import HttpConnector
from orbit.protocol import Event, Notice
from orbit.tracing import NoSpan
from tests.mcp_fakes import FakeCredentials
from tests.mcp_http_stand_in import HttpStandIn, served

SCAN = UpstreamTool("scan", "Scan the code.", {"type": "object"})
SCANNER = Role(instructions="Scan.", tools=RoleTools.model_validate({"mcp": ["semgrep"]}))
CALLER = Caller("s1", "scanner", SCANNER, "/work/s1", NoSpan("agent"))
CREDENTIALS_MESSAGE = "MCP server semgrep refused orbit's credentials (HTTP 401)."


BEARER = {"Authorization": "Bearer $SEMGREP_TOKEN"}


class Proxied:
    """A proxy of semgrep, served by ``stand_in`` and sent ``headers``, ``Bearer $SEMGREP_TOKEN``
    unless given, to the scanner, its token ``token`` in the credentials, noting the notices it
    publishes."""

    def __init__(
        self, stand_in: HttpStandIn, token: str, headers: dict[str, str] | None = None
    ) -> None:
        sent = BEARER if headers is None else headers
        server = McpServer(url=stand_in.url, headers=sent, timeout_seconds=5)
        proxying = McpProxying(
            {"semgrep": server},
            HttpConnector(),
            FakeCredentials({"SEMGREP_TOKEN": token}),
            "/state/mcp/workspace-1",
        )
        self.notices: list[str] = []
        self.proxy = ToolProxy(proxying, lambda: {"scanner": SCANNER}, self.publish)

    async def publish(self, event: Event) -> None:
        if isinstance(event, Notice):
            self.notices.append(event.message)

    async def scan(self) -> Reply:
        return await self.proxy.call(CALLER, "semgrep__scan", {})


@pytest.fixture
async def stand_in() -> AsyncIterator[HttpStandIn]:
    """The stand-in, offering ``scan`` and requiring the token ``s3cret``, serving for the
    test."""
    async with served(HttpStandIn((SCAN,), token="s3cret")) as serving:
        yield serving


@pytest.fixture
async def proxied(stand_in: HttpStandIn) -> AsyncIterator[Proxied]:
    """A proxy of the stand-in, with the token it requires, closed after the test."""
    proxied = Proxied(stand_in, "s3cret")
    yield proxied
    await proxied.proxy.close()


async def test_an_agent_lists_the_servers_tool_and_gets_its_result(
    stand_in: HttpStandIn, proxied: Proxied
) -> None:
    [tool] = await proxied.proxy.list_tools(CALLER)
    assert (tool.name, tool.tool) == ("semgrep__scan", SCAN)
    assert await proxied.scan() == Reply("Found 2 issues.")
    assert {headers["authorization"] for headers in stand_in.headers} == {"Bearer s3cret"}


async def test_a_stopped_server_fails_with_one_notice_and_is_reconnected_once_back(
    stand_in: HttpStandIn, proxied: Proxied
) -> None:
    assert await proxied.scan() == Reply("Found 2 issues.")
    await stand_in.stop()
    first = await proxied.scan()
    second = await proxied.scan()
    assert first.is_error and second.is_error
    assert first.text.startswith("MCP server semgrep failed: ")
    assert len(proxied.notices) == 1
    await stand_in.start()
    assert await proxied.scan() == Reply("Found 2 issues.")


async def test_a_server_sent_no_credentials_says_it_refused_them(stand_in: HttpStandIn) -> None:
    proxied = Proxied(stand_in, "s3cret", headers={})
    try:
        reply = await proxied.scan()
    finally:
        await proxied.proxy.close()
    assert reply == Reply(CREDENTIALS_MESSAGE, is_error=True)
    assert proxied.notices == [
        f"{CREDENTIALS_MESSAGE} Agents' calls to it return errors until it starts."
    ]


async def test_a_server_sent_the_wrong_credentials_says_it_refused_them(
    stand_in: HttpStandIn,
) -> None:
    proxied = Proxied(stand_in, "wrong")
    try:
        reply = await proxied.scan()
    finally:
        await proxied.proxy.close()
    assert reply == Reply(CREDENTIALS_MESSAGE.replace("401", "403"), is_error=True)
