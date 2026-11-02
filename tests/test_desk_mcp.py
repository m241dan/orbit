"""The desk proxying MCP servers' tools to its agents: each agent's listing and calls, through
orbit's real MCP server and the stand-in server, and with a fake connector, the agent's run
ending and the workspace closing."""

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from orbit.agent.application.desk import UNKNOWN_AGENT, AgentDesk, Agents
from orbit.agent.application.ports import AgentHost, McpConnector, Reply, ToolListing
from orbit.agent.application.tool_proxy import CALL_SPAN, STOPPED, McpProxying
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.domain.roles import Role, RoleTools
from orbit.agent.domain.tools import ORBIT_TOOLS
from orbit.agent.external.environment_credentials import EnvironmentCredentials
from orbit.agent.external.mcp_host import McpHost
from orbit.agent.external.mcp_stdio import StdioConnector
from orbit.agent.external.processes import process_alive
from orbit.protocol import Event, Settings
from orbit.tracing import NoSpan
from tests.agent_fakes import FakeHost, FakeTempDirs
from tests.mcp_fakes import FakeConnector
from tests.telemetry_helpers import Traced
from tests.waiting import eventually

STAND_IN = str(Path(__file__).with_name("mcp_stand_in.py"))


def role(orbit: tuple[str, ...], *mcp: object) -> Role:
    """A task role granted the ``orbit`` tools, whose agents may use the MCP servers ``mcp``."""
    tools = RoleTools.model_validate({"orbit": orbit, "mcp": mcp})
    return Role(instructions="Work.", tools=tools)


ROLES = {
    "coder": role(
        ("get_task", "complete_task"),
        {"server": "github", "tools": ["get_issue", "whoami", "slow"]},
        "own",
    ),
    "reviewer": role(("get_task", "report_finding")),
}


class Answering:
    """Answers every call to orbit's tools with ``ok``."""

    async def handle(self, tool: str, arguments: dict[str, Any]) -> Reply:
        return Reply("ok")


class Recorded:
    """Notes what's published."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)


def desk_with(
    host: AgentHost, connector: McpConnector, folder: Path, mcp: bool = True, **options: Any
) -> AgentDesk:
    """A desk whose agents reach orbit through ``host``, in :data:`ROLES`, proxied the stand-in
    as ``github`` and as ``own``, unshared, through ``connector``, the shared one in ``folder``,
    unless not ``mcp``; ``options`` go to the desk."""
    command = (sys.executable, "-I", STAND_IN)
    servers = {
        "github": McpServer(command=command),
        "own": McpServer(command=command, shared=False),
    }
    proxying = McpProxying(servers, connector, EnvironmentCredentials({}), str(folder))
    agents = Agents(
        host,
        {},
        iter(f"token-{number}" for number in range(1, 100)).__next__,
        ROLES,
        root="/work/root",
        temp_dirs=FakeTempDirs(),
        mcp=proxying if mcp else None,
    )
    return AgentDesk(agents, Recorded(), **options)


@asynccontextmanager
async def connected(url: str, token: str) -> AsyncIterator[Client]:
    """A client of orbit's MCP server at ``url``, as the agent with ``token``."""
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers) as http,
        Client(streamable_http_client(url, http_client=http)) as client,
    ):
        yield client


def text(result: Any) -> str:
    [content] = result.content
    assert isinstance(content, TextContent)
    return content.text


# Through orbit's MCP server and the stand-in


async def test_each_agent_lists_and_calls_the_tools_its_role_allows(tmp_path: Path) -> None:
    desk = desk_with(McpHost(), StdioConnector(), tmp_path / "mcp")
    coder = desk.open(Answering(), "coder", "1")
    reviewer = desk.open(Answering(), "reviewer", "2")
    url = await desk.url()
    try:
        async with connected(url, coder.token) as client:
            coders = [tool.name for tool in (await client.list_tools()).tools]
            issue = await client.call_tool("github__get_issue", {"number": 7})
            hidden = await client.call_tool("github__oops", {})
        async with connected(url, reviewer.token) as client:
            reviewers = [tool.name for tool in (await client.list_tools()).tools]
            refused = await client.call_tool("github__get_issue", {"number": 7})
    finally:
        await desk.stop()
    assert set(coders[:3]) == {"get_task", "complete_task", "read_skill"}
    assert coders[3:6] == ["github__get_issue", "github__whoami", "github__slow"]
    assert "github__oops" not in coders
    assert set(reviewers) == {"get_task", "report_finding", "read_skill"}
    assert (text(issue), issue.is_error) == ("Issue 7: it's broken.", False)
    assert hidden.is_error and text(hidden) == "roles.coder doesn't allow github__oops."
    assert refused.is_error and text(refused) == "roles.reviewer doesn't allow github__get_issue."


async def test_a_shared_server_runs_in_the_state_directory_and_an_unshared_one_in_the_agents(
    tmp_path: Path,
) -> None:
    desk = desk_with(FakeHost(), StdioConnector(), tmp_path / "mcp")
    coder = desk.open(Answering(), "coder", "1")
    await desk.launch(coder, "Do it.", Settings(), str(tmp_path / "work"))
    try:
        shared = json.loads((await desk.handle(coder.token, "github__whoami", {})).text)
        own = json.loads((await desk.handle(coder.token, "own__whoami", {})).text)
    finally:
        await desk.stop()
    assert shared["cwd"] == str(tmp_path / "mcp" / "github")
    assert own["cwd"] == str(tmp_path / "work")


async def test_closing_the_workspace_answers_a_call_in_flight_and_stops_the_server(
    tmp_path: Path,
) -> None:
    desk = desk_with(FakeHost(), StdioConnector(), tmp_path / "mcp")
    coder = desk.open(Answering(), "coder", "1")
    pid = json.loads((await desk.handle(coder.token, "github__whoami", {})).text)["pid"]
    slow = asyncio.ensure_future(desk.handle(coder.token, "github__slow", {}))
    await asyncio.sleep(0.2)  # fixed wait: the call reaches the server, which doesn't say so
    await desk.stop()
    assert await asyncio.wait_for(slow, 5) == STOPPED
    assert not process_alive(pid)


# With a fake connector


async def test_a_token_no_agent_has_lists_orbits_tools_and_nothing_proxied(tmp_path: Path) -> None:
    host = FakeHost()
    desk = desk_with(host, FakeConnector(), tmp_path)
    await desk.url()
    assert await host.list_tools("nobody") == ToolListing(ORBIT_TOOLS)


async def test_an_agent_lists_its_roles_orbit_tools_and_the_proxied_ones(tmp_path: Path) -> None:
    host = FakeHost()
    desk = desk_with(host, FakeConnector(), tmp_path)
    reviewer = desk.open(Answering(), "reviewer", "2")
    await desk.url()
    assert await host.list_tools(reviewer.token) == ToolListing(
        ("get_task", "report_finding", "read_skill")
    )


async def test_without_servers_an_agent_lists_none_and_its_calls_to_them_are_refused(
    tmp_path: Path,
) -> None:
    host = FakeHost()
    desk = desk_with(host, FakeConnector(), tmp_path, mcp=False)
    coder = desk.open(Answering(), "coder", "1")
    await desk.url()
    listing = await host.list_tools(coder.token)
    assert listing.proxied == ()
    assert await host.call(coder.token, "github__get_issue") == Reply(
        "roles.coder doesn't allow github__get_issue.", is_error=True
    )
    await desk.stop()


async def test_an_agents_run_ending_closes_its_own_connections_and_its_token(
    tmp_path: Path,
) -> None:
    connector = FakeConnector()
    desk = desk_with(FakeHost(), connector, tmp_path)
    coder = desk.open(Answering(), "coder", "1")
    assert not (await desk.handle(coder.token, "own__get_issue", {})).is_error
    desk.close(coder)
    [session] = connector.sessions
    await eventually(lambda: session.closed)
    assert session.cwd == "/work/root"  # It was never launched.
    assert await desk.handle(coder.token, "own__get_issue", {}) == UNKNOWN_AGENT
    assert await desk.list_tools(coder.token) == ToolListing(ORBIT_TOOLS)


async def test_a_proxied_call_is_traced_linked_to_its_agents_process(tmp_path: Path) -> None:
    traced = Traced()
    desk = desk_with(FakeHost(), FakeConnector(), tmp_path, telemetry=traced.telemetry)
    coder = desk.open(Answering(), "coder", "1")
    with desk.traced(coder, NoSpan("task")):
        await desk.handle(coder.token, "github__get_issue", {"number": 1})
    [call] = traced.spans(CALL_SPAN)
    [process] = traced.spans("orbit.agent.process")
    [link] = call.links
    assert process.context is not None
    assert link.context.span_id == process.context.span_id


async def test_calls_still_reach_orbits_tools(tmp_path: Path) -> None:
    desk = desk_with(FakeHost(), FakeConnector(), tmp_path)
    coder = desk.open(Answering(), "coder", "1")
    assert await desk.handle(coder.token, "get_task", {}) == Reply("ok")


@pytest.mark.parametrize("tool", ["github__get_issue", "get_task"])
async def test_a_revoked_token_is_refused(tmp_path: Path, tool: str) -> None:
    desk = desk_with(FakeHost(), FakeConnector(), tmp_path)
    coder = desk.open(Answering(), "coder", "1")
    desk.close(coder)
    assert await desk.handle(coder.token, tool, {}) == UNKNOWN_AGENT
