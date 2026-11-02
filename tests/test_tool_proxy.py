"""Proxying external MCP servers to the agents whose roles allow them, against a fake connector:
listing and calls, connections shared, unshared and idle, failures and their notices, shutdown
and the spans of calls."""

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from orbit.agent.application.ports import Reply
from orbit.agent.application.tool_proxy import (
    CALL_SPAN,
    STOPPED,
    Caller,
    McpProxying,
    ToolProxy,
)
from orbit.agent.domain.mcp_proxy import UpstreamTool
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.domain.roles import Role, RoleTools
from orbit.protocol import INTERNAL, ErrorOccurred, Event, Notice
from orbit.tracing import NoSpan, Span
from tests.mcp_fakes import STAND_IN_TOOLS, FakeConnector, FakeCredentials
from tests.telemetry_helpers import Traced
from tests.waiting import eventually

FOLDER = "/state/mcp/workspace-1"
GITHUB = McpServer(command=("github-mcp-server", "stdio"), env={"TOKEN": "$GITHUB_TOKEN"})
SERVERS = {
    "github": GITHUB,
    "own": McpServer(command=("own-server",), shared=False),
    "semgrep": McpServer(
        url="http://127.0.0.1:3010/mcp", headers={"Authorization": "Bearer ${SEMGREP_TOKEN}"}
    ),
}
CODER_TOOLS = ["get_issue", "get issue!", "oops", "slow", "fail", "create_isue"]
CREATE_ISUE = "github offers no tool create_isue, which roles.coder.tools.mcp names."
CRASH_NOTICE = (
    "MCP server github failed: it exited with status 3. Agents' calls to it return errors until"
    " it starts."
)


def role(*mcp: object) -> Role:
    """A task role whose agents may use the MCP servers ``mcp``, as a file gives them."""
    return Role(instructions="Work.", tools=RoleTools.model_validate({"mcp": mcp}))


ROLES = {
    "coder": role({"server": "github", "tools": CODER_TOOLS}),
    "planner": role("github", "own"),
    "reviewer": role(),
    "scanner": role("semgrep"),
}


class Timers:
    """Sleeps that end when the test lets them, each noted in ``sleeps``; with ``broken``, they
    raise it instead."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.broken: Exception | None = None
        self._pending: list[asyncio.Future[None]] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.broken is not None:
            raise self.broken
        pending = asyncio.get_running_loop().create_future()
        self._pending.append(pending)
        await pending

    def elapse(self) -> None:
        """End every sleep waiting."""
        for pending in self._pending:
            if not pending.done():
                pending.set_result(None)
        self._pending.clear()


class Proxied:
    """A proxy of :data:`SERVERS` to :data:`ROLES`, through a fake connector, with credentials
    from ``credentials``, noting what it publishes and tracing to ``traced``."""

    def __init__(self, credentials: Mapping[str, str] | None = None) -> None:
        self.connector = FakeConnector()
        self.credentials = FakeCredentials(
            {"GITHUB_TOKEN": "secret", "SEMGREP_TOKEN": "s3"}
            if credentials is None
            else credentials
        )
        self.timers = Timers()
        self.published: list[Event] = []
        self.traced = Traced()
        proxying = McpProxying(SERVERS, self.connector, self.credentials, FOLDER, self.timers.sleep)
        self.proxy = ToolProxy(proxying, lambda: ROLES, self.publish, self.traced.telemetry)

    async def publish(self, event: Event) -> None:
        self.published.append(event)

    @property
    def notices(self) -> list[str]:
        return [event.message for event in self.published if isinstance(event, Notice)]

    async def call(self, name: str, caller: Caller | None = None, **arguments: Any) -> Reply:
        return await self.proxy.call(caller or coder(), name, arguments)


def caller(agent_id: str, role_name: str, span: Span | None = None) -> Caller:
    """Agent ``agent_id`` in role ``role_name``, working in a directory of its own."""
    return Caller(agent_id, role_name, ROLES[role_name], f"/work/{agent_id}", span or NoSpan("a"))


def coder(span: Span | None = None) -> Caller:
    """A coder, agent ``a1``."""
    return caller("a1", "coder", span)


# Listing


async def test_a_role_lists_the_tools_it_allows_by_their_proxied_names() -> None:
    proxied = Proxied()
    tools = await proxied.proxy.list_tools(coder())
    names = [tool.name for tool in tools]
    assert names == [
        "github__get_issue",
        "github__get_issue_",
        "github__oops",
        "github__slow",
        "github__fail",
    ]
    assert tools[1].tool.name == "get issue!"
    assert tools[1].tool.description == "Get an issue, oddly named."


async def test_a_role_allowing_a_server_whole_lists_all_its_tools() -> None:
    proxied = Proxied()
    tools = await proxied.proxy.list_tools(caller("p1", "planner"))
    assert len([tool for tool in tools if tool.server == "github"]) == 6
    assert len([tool for tool in tools if tool.server == "own"]) == 6


async def test_a_role_without_servers_lists_none_and_connects_to_nothing() -> None:
    proxied = Proxied()
    assert await proxied.proxy.list_tools(caller("r1", "reviewer")) == ()
    assert proxied.connector.opened == []


async def test_a_server_reached_by_a_url_is_listed_and_called() -> None:
    proxied = Proxied()
    tools = await proxied.proxy.list_tools(caller("s1", "scanner"))
    assert tools[0].name == "semgrep__get_issue"
    reply = await proxied.call("semgrep__get_issue", caller("s1", "scanner"), number=4)
    assert reply == Reply("Issue 4: it's broken.")
    [opened] = proxied.connector.opened
    assert opened.server == SERVERS["semgrep"]


async def test_tools_a_role_lists_that_the_server_lacks_are_noticed_once() -> None:
    proxied = Proxied()
    await proxied.proxy.list_tools(coder())
    await proxied.call("github__fail")
    await proxied.proxy.list_tools(coder())
    assert proxied.notices.count(CREATE_ISUE) == 1


# Calls


async def test_a_call_goes_upstream_by_the_servers_name_for_the_tool() -> None:
    proxied = Proxied()
    reply = await proxied.call("github__get_issue_", number=7)
    assert reply == Reply("Issue 7: it's broken.")
    [session] = proxied.connector.sessions
    assert session.calls == [("get issue!", {"number": 7})]


async def test_tools_whose_names_collide_are_listed_apart_and_each_call_reaches_its_own() -> None:
    proxied = Proxied()
    proxied.connector.tools = (*STAND_IN_TOOLS, UpstreamTool("get issue", None, {}))
    planner = caller("p1", "planner")
    tools = await proxied.proxy.list_tools(planner)
    github = [tool for tool in tools if tool.server == "github"]
    names = {tool.tool.name: tool.name for tool in github}
    assert len(set(names.values())) == len(github)
    odd = names["get issue"]
    assert odd.startswith("github__get_issue-")
    await proxied.call("github__get_issue", planner, number=1)
    await proxied.call(odd, planner, number=2)
    [session] = [session for session in proxied.connector.sessions if session.calls]
    assert session.calls == [("get_issue", {"number": 1}), ("get issue", {"number": 2})]


async def test_a_call_to_a_server_the_role_doesnt_allow_is_refused_on_its_span() -> None:
    proxied = Proxied()
    agent = proxied.traced.telemetry.start("orbit.agent.process")
    reply = await proxied.call("github__get_issue", caller("r1", "reviewer", agent))
    agent.end()
    assert reply == Reply("roles.reviewer doesn't allow github__get_issue.", is_error=True)
    assert proxied.connector.opened == []
    [span] = proxied.traced.spans("orbit.agent.process")
    [event] = span.events
    assert event.name == "orbit.mcp.refused"
    assert event.attributes == {"orbit.mcp.tool": "github__get_issue"}


async def test_a_call_to_a_tool_the_role_doesnt_list_is_refused() -> None:
    proxied = Proxied()
    reply = await proxied.call("github__whoami")
    assert reply == Reply("roles.coder doesn't allow github__whoami.", is_error=True)
    [session] = proxied.connector.sessions
    assert session.calls == []


async def test_a_call_to_a_name_without_a_server_is_refused() -> None:
    proxied = Proxied()
    reply = await proxied.call("__get_issue")
    assert reply == Reply("roles.coder doesn't allow __get_issue.", is_error=True)


async def test_a_tools_own_error_is_passed_on() -> None:
    proxied = Proxied()
    assert await proxied.call("github__oops") == Reply("it went wrong", is_error=True)
    assert proxied.notices == [CREATE_ISUE]


# Failures


async def test_a_crash_fails_the_call_is_noticed_and_the_next_call_starts_afresh() -> None:
    proxied = Proxied()
    reply = await proxied.call("github__fail")
    assert reply == Reply("MCP server github failed: it exited with status 3.", is_error=True)
    assert proxied.notices == [CREATE_ISUE, CRASH_NOTICE]
    assert await proxied.call("github__get_issue", number=2) == Reply("Issue 2: it's broken.")
    first, second = proxied.connector.sessions
    await eventually(lambda: first.closed)
    assert not second.closed


async def test_two_failures_in_a_row_give_one_notice() -> None:
    proxied = Proxied()
    await proxied.call("github__fail")
    await proxied.call("github__fail")
    assert proxied.notices.count(CRASH_NOTICE) == 1
    assert len(proxied.connector.opened) == 2


async def test_a_call_answered_ends_the_streak() -> None:
    proxied = Proxied()
    await proxied.call("github__fail")
    await proxied.call("github__get_issue", number=1)
    await proxied.call("github__fail")
    assert proxied.notices.count(CRASH_NOTICE) == 2


async def test_a_server_that_fails_to_start_lists_nothing_and_fails_calls() -> None:
    proxied = Proxied()
    proxied.connector.failure = "Could not start 'github-mcp-server': no such program."
    assert await proxied.proxy.list_tools(coder()) == ()
    reply = await proxied.call("github__get_issue")
    assert reply.is_error
    assert reply.text == (
        "MCP server github failed: Could not start 'github-mcp-server': no such program."
    )
    assert proxied.notices == [
        "MCP server github failed: Could not start 'github-mcp-server': no such program. Agents'"
        " calls to it return errors until it starts."
    ]
    proxied.connector.failure = None
    assert await proxied.call("github__get_issue", number=3) == Reply("Issue 3: it's broken.")


async def test_calls_waiting_for_a_server_that_fails_to_start_fail_with_one_notice() -> None:
    proxied = Proxied()
    proxied.connector.failure = "it exited with status 1"
    proxied.connector.gate = asyncio.Event()
    calls = asyncio.gather(
        proxied.call("github__get_issue", coder()),
        proxied.call("github__get_issue", caller("a2", "coder")),
    )
    await eventually(lambda: proxied.connector.opened)
    proxied.connector.gate.set()
    first, second = await asyncio.wait_for(calls, 5)
    assert first == second == Reply("MCP server github failed: it exited with status 1.", True)
    assert len(proxied.connector.opened) == 1
    assert len(proxied.notices) == 1


async def test_a_server_failing_to_list_its_tools_fails_and_its_session_is_closed() -> None:
    proxied = Proxied()
    proxied.connector.list_failure = "it didn't answer within 300 seconds"
    reply = await proxied.call("github__get_issue")
    assert reply.text == "MCP server github failed: it didn't answer within 300 seconds."
    [session] = proxied.connector.sessions
    assert session.closed


async def test_a_bug_connecting_is_raised_and_the_next_call_connects_again() -> None:
    proxied = Proxied()
    proxied.connector.crash = RuntimeError("bug")
    with pytest.raises(RuntimeError, match="bug"):
        await proxied.call("github__get_issue")
    proxied.connector.crash = None
    assert not (await proxied.call("github__get_issue")).is_error
    assert len(proxied.connector.opened) == 2


# The environment


async def test_the_servers_environment_is_filled_from_the_credentials() -> None:
    proxied = Proxied()
    await proxied.proxy.list_tools(coder())
    [opened] = proxied.connector.opened
    assert opened.env == {"TOKEN": "secret"}


async def test_the_servers_headers_are_filled_from_the_credentials() -> None:
    proxied = Proxied()
    await proxied.proxy.list_tools(caller("s1", "scanner"))
    [opened] = proxied.connector.opened
    assert opened.headers == {"Authorization": "Bearer s3"}
    assert opened.env == {}


async def test_a_header_variable_set_nowhere_fails_the_connection() -> None:
    proxied = Proxied(credentials={"GITHUB_TOKEN": "secret"})
    reply = await proxied.call("semgrep__get_issue", caller("s1", "scanner"))
    assert reply == Reply(
        "MCP server semgrep failed: SEMGREP_TOKEN isn't set in orbit's environment or"
        " credentials.toml.",
        is_error=True,
    )
    assert proxied.connector.opened == []


async def test_a_server_refusing_orbits_credentials_says_so_once_per_streak() -> None:
    proxied = Proxied()
    proxied.connector.refused = 401
    first = await proxied.call("semgrep__get_issue", caller("s1", "scanner"))
    second = await proxied.call("semgrep__get_issue", caller("s1", "scanner"))
    message = "MCP server semgrep refused orbit's credentials (HTTP 401)."
    assert first == second == Reply(message, is_error=True)
    assert proxied.notices == [f"{message} Agents' calls to it return errors until it starts."]


async def test_a_variable_set_nowhere_fails_the_start() -> None:
    proxied = Proxied(credentials={})
    reply = await proxied.call("github__get_issue")
    assert reply.text == (
        "MCP server github failed: GITHUB_TOKEN isn't set in orbit's environment or"
        " credentials.toml."
    )
    assert proxied.connector.opened == []


async def test_credentials_that_cant_be_read_fail_the_start() -> None:
    proxied = Proxied()
    proxied.credentials.unreadable = "credentials.toml must be private: chmod 600 it."
    reply = await proxied.call("github__get_issue")
    assert reply.text == (
        "MCP server github failed: credentials.toml must be private: chmod 600 it."
    )


# Connections


async def test_agents_share_a_servers_connection_in_its_neutral_directory() -> None:
    proxied = Proxied()
    await asyncio.gather(
        proxied.call("github__get_issue", coder()),
        proxied.call("github__get_issue", caller("a2", "coder")),
        proxied.proxy.list_tools(caller("p1", "planner")),
    )
    github = [opened for opened in proxied.connector.opened if opened.server == GITHUB]
    assert [opened.cwd for opened in github] == [f"{FOLDER}/github"]


async def test_an_unshared_server_gives_each_agent_its_own_closed_as_its_run_ends() -> None:
    proxied = Proxied()
    first, second = caller("p1", "planner"), caller("p2", "planner")
    await proxied.call("own__get_issue", first)
    await proxied.call("own__get_issue", second)
    own = [opened.cwd for opened in proxied.connector.opened if opened.server != GITHUB]
    assert own == ["/work/p1", "/work/p2"]
    proxied.proxy.release("p1")
    mine, theirs = proxied.connector.sessions
    await eventually(lambda: mine.closed)
    assert not theirs.closed


async def test_a_connection_idle_for_its_minutes_is_closed_and_reopened_by_a_call() -> None:
    proxied = Proxied()
    await proxied.call("github__get_issue")
    assert proxied.timers.sleeps[-1] == 600
    [first] = proxied.connector.sessions
    proxied.timers.elapse()
    await eventually(lambda: first.closed)
    await proxied.call("github__get_issue")
    assert len(proxied.connector.opened) == 2


async def test_a_call_keeps_its_connection_from_idling_out() -> None:
    proxied = Proxied()
    await proxied.proxy.list_tools(coder())
    slow = asyncio.ensure_future(proxied.call("github__slow"))
    [session] = proxied.connector.sessions
    await asyncio.wait_for(session.waiting.wait(), 5)
    proxied.timers.elapse()  # No timer runs while a call is in flight.
    await asyncio.sleep(0)
    assert not session.closed
    session.release.set()
    assert await asyncio.wait_for(slow, 5) == Reply("slow, at last")


async def test_a_failure_waiting_for_a_connection_to_idle_is_reported() -> None:
    proxied = Proxied()
    proxied.timers.broken = RuntimeError("the clock broke")
    await proxied.call("github__get_issue")
    await eventually(lambda: any(isinstance(event, ErrorOccurred) for event in proxied.published))
    [error] = [event for event in proxied.published if isinstance(event, ErrorOccurred)]
    assert error.code == INTERNAL
    assert error.message == "Orbit failed to close an idle MCP connection: the clock broke"


# Stopping


async def test_closing_answers_calls_in_flight_and_closes_every_connection() -> None:
    proxied = Proxied()
    slow = asyncio.ensure_future(proxied.call("github__slow"))
    await proxied.call("own__get_issue", caller("p1", "planner"))
    github, own = sorted(proxied.connector.sessions, key=lambda session: session.cwd)
    assert github.cwd == f"{FOLDER}/github"
    await asyncio.wait_for(github.waiting.wait(), 5)
    await proxied.proxy.close()
    assert await asyncio.wait_for(slow, 5) == STOPPED
    assert [github.closed, own.closed, github.cancelled] == [True, True, 1]
    assert await proxied.call("github__get_issue") == Reply(
        "MCP server github failed: orbit is closing.", is_error=True
    )
    assert await proxied.proxy.list_tools(coder()) == ()


async def test_closing_while_a_server_starts_lists_nothing_from_it() -> None:
    proxied = Proxied()
    proxied.connector.gate = asyncio.Event()
    listing = asyncio.ensure_future(proxied.proxy.list_tools(coder()))
    await eventually(lambda: proxied.connector.opened)
    await proxied.proxy.close()
    assert await asyncio.wait_for(listing, 5) == ()
    assert proxied.connector.sessions == []


async def test_closing_while_a_server_lists_its_tools_closes_its_session() -> None:
    proxied = Proxied()
    proxied.connector.list_gate = asyncio.Event()
    listing = asyncio.ensure_future(proxied.proxy.list_tools(coder()))
    await eventually(lambda: proxied.connector.sessions)
    await proxied.proxy.close()
    assert await asyncio.wait_for(listing, 5) == ()
    [session] = proxied.connector.sessions
    assert session.closed


async def test_an_agents_run_ending_stops_its_call_in_flight() -> None:
    proxied = Proxied()
    slow = asyncio.ensure_future(proxied.call("github__slow"))
    await eventually(lambda: proxied.connector.sessions)
    [session] = proxied.connector.sessions
    await asyncio.wait_for(session.waiting.wait(), 5)
    proxied.proxy.release("a1")
    assert await asyncio.wait_for(slow, 5) == STOPPED
    assert session.cancelled == 1
    assert not session.closed  # Shared, it serves others still.


async def test_a_caller_cancelled_cancels_its_call_upstream() -> None:
    proxied = Proxied()
    slow = asyncio.ensure_future(proxied.call("github__slow"))
    await eventually(lambda: proxied.connector.sessions)
    [session] = proxied.connector.sessions
    await asyncio.wait_for(session.waiting.wait(), 5)
    slow.cancel()
    with pytest.raises(asyncio.CancelledError):
        await slow
    await eventually(lambda: session.cancelled == 1)


# Spans


async def test_a_call_is_traced_with_its_server_tool_and_outcome_and_no_arguments() -> None:
    proxied = Proxied()
    agent = proxied.traced.telemetry.start("orbit.agent.process")
    await proxied.call("github__get_issue_", coder(agent), number=7)
    agent.end()
    [span] = proxied.traced.spans(CALL_SPAN)
    assert span.attributes == {
        "orbit.mcp.server": "github",
        "orbit.mcp.tool": "github__get_issue_",
        "orbit.mcp.outcome": "ok",
    }
    [link] = span.links
    [process] = proxied.traced.spans("orbit.agent.process")
    assert process.context is not None
    assert link.context.span_id == process.context.span_id


@pytest.mark.parametrize(
    ("tool", "outcome", "error"),
    [
        ("github__oops", "tool_error", None),
        ("github__fail", "failed", "MCP server github failed: it exited with status 3."),
        ("github__whoami", "refused", "roles.coder doesn't allow github__whoami."),
    ],
)
async def test_a_calls_span_says_how_it_came_out(
    tool: str, outcome: str, error: str | None
) -> None:
    proxied = Proxied()
    await proxied.call(tool)
    [span] = proxied.traced.spans(CALL_SPAN)
    assert span.attributes is not None
    assert span.attributes["orbit.mcp.outcome"] == outcome
    assert span.status.description == error


async def test_a_stopped_calls_span_says_so() -> None:
    proxied = Proxied()
    slow = asyncio.ensure_future(proxied.call("github__slow"))
    await eventually(lambda: proxied.connector.sessions)
    await asyncio.wait_for(proxied.connector.sessions[0].waiting.wait(), 5)
    await proxied.proxy.close()
    await slow
    [span] = proxied.traced.spans(CALL_SPAN)
    assert span.attributes is not None
    assert span.attributes["orbit.mcp.outcome"] == "stopped"
