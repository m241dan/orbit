"""Proxied calls to tools whose servers hold them for the user's approval, against a fake
connector: held before they go upstream, run once allowed, declined, expired or withdrawn, and
the spans that say so."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from orbit.agent.application.consult_approvals import Timer
from orbit.agent.application.ports import Reply
from orbit.agent.application.tool_call_approvals import ToolCallApprovals
from orbit.agent.application.tool_proxy import CALL_SPAN, STOPPED, Caller, McpProxying, ToolProxy
from orbit.agent.domain.consult_limits import HOUR
from orbit.agent.domain.mcp_proxy import UpstreamTool
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.domain.roles import Role, RoleTools
from orbit.protocol import DecideToolCall, Event, ToolCallDecided, ToolCallHeld
from orbit.tracing import NoSpan
from tests.mcp_fakes import FakeConnector, FakeCredentials, FakeSession
from tests.telemetry_helpers import Traced
from tests.waiting import eventually

TOOLS = (
    UpstreamTool("get_issue", "Get an issue.", {"type": "object"}),
    UpstreamTool("create_issue", "Create an issue.", {"type": "object"}),
)
COMMAND = ("github-mcp-server", "stdio")
SERVERS = {
    "github": McpServer(command=COMMAND, approve=("create_issue",)),
    "everything": McpServer(command=COMMAND, approve=True),
}
CODER = Role(
    instructions="Work.",
    tools=RoleTools.model_validate({"mcp": ["github", "everything"]}),
)
DECLINED = Reply("The user declined github__create_issue.", is_error=True)
CREATED = Reply("Created issue 'Flaky test'.")


class Clock:
    """A clock that sleeping moves on, at once."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


async def instantly(seconds: float) -> None:
    """Sleep no time at all."""
    await asyncio.sleep(0)


class Holding:
    """A proxy of :data:`SERVERS` to a coder, through a fake connector offering :data:`TOOLS`,
    holding calls in approvals over a :class:`Clock`, noting what's posted and tracing to
    ``traced``; ``approving`` false, it holds them in none. Connections idle for as long as
    ``idle`` sleeps, ten minutes unless given."""

    def __init__(
        self, approving: bool = True, idle: Callable[[float], Awaitable[None]] = asyncio.sleep
    ) -> None:
        self.connector = FakeConnector(TOOLS)
        self.clock = Clock()
        self.posted: list[Event] = []
        self.traced = Traced()
        self.approvals = ToolCallApprovals(
            self.posted.append, self._save, Timer(self.clock, self.clock.sleep)
        )
        proxying = McpProxying(SERVERS, self.connector, FakeCredentials({}), "/state/mcp", idle)
        self.proxy = ToolProxy(
            proxying,
            lambda: {"coder": CODER},
            self._publish,
            self.traced.telemetry,
            self.approvals if approving else None,
        )

    async def _save(self) -> None:
        return None

    async def _publish(self, event: Event) -> None:
        self.posted.append(event)

    def call(self, name: str = "github__create_issue", **arguments: Any) -> asyncio.Task[Reply]:
        """The coder's call to ``name``, in a task of its own."""
        caller = Caller("a1", "coder", CODER, "/work/a1", NoSpan("a"))
        return asyncio.ensure_future(self.proxy.call(caller, name, arguments))

    async def held(self, name: str = "github__create_issue") -> asyncio.Task[Reply]:
        """The coder's call to ``name`` with a title, once it's held."""
        task = self.call(name, title="Flaky test")
        await eventually(lambda: self.approvals.waiting, poll=0)
        return task

    @property
    def session(self) -> FakeSession:
        [session] = self.connector.sessions
        return session

    def decided(self) -> list[ToolCallDecided]:
        return [event for event in self.posted if isinstance(event, ToolCallDecided)]


def held_call(number: int = 1, tool: str = "github__create_issue") -> ToolCallHeld:
    return ToolCallHeld(
        call=number,
        agent_id="a1",
        role="coder",
        server=tool.partition("__")[0],
        tool=tool,
        arguments={"title": "Flaky test"},
    )


async def test_a_call_to_an_approved_tool_is_held_and_runs_once_allowed() -> None:
    holding = Holding()
    holding.approvals.viewers(1)
    task = await holding.held()
    assert holding.posted == [held_call()]
    assert (holding.session.calls, task.done()) == ([], False)
    holding.approvals.decide(DecideToolCall(call=1, decision="allow"))
    assert await task == CREATED
    assert holding.session.calls == [("create_issue", {"title": "Flaky test"})]
    assert holding.decided() == [ToolCallDecided(call=1, decision="allowed")]


async def test_a_declined_call_is_answered_so_and_never_goes_upstream() -> None:
    holding = Holding()
    holding.approvals.viewers(1)
    task = await holding.held()
    holding.approvals.decide(DecideToolCall(call=1, decision="deny"))
    assert await task == DECLINED
    assert holding.session.calls == []


async def test_a_call_to_a_tool_the_list_doesnt_name_isnt_held() -> None:
    holding = Holding()
    reply = await holding.call("github__get_issue", number=7)
    assert reply == Reply("Issue 7: it's broken.")
    assert holding.posted == []


async def test_a_server_approving_all_holds_every_tool() -> None:
    holding = Holding()
    holding.approvals.viewers(1)
    task = await holding.held("everything__get_issue")
    assert holding.approvals.waiting == (held_call(tool="everything__get_issue"),)
    task.cancel()


async def test_a_call_held_with_no_frontend_connected_expires_as_declined() -> None:
    holding = Holding()
    task = await holding.held()
    assert await task == DECLINED
    assert holding.decided() == [ToolCallDecided(call=1, decision="expired")]
    assert holding.session.calls == []


async def test_a_call_held_doesnt_expire_while_a_frontend_is_connected() -> None:
    holding = Holding()
    holding.approvals.viewers(1)
    task = await holding.held()
    await asyncio.sleep(0)
    assert (task.done(), holding.clock.now) == (False, 0.0)
    task.cancel()


async def test_the_agents_run_ending_while_held_withdraws_and_answers_the_call() -> None:
    holding = Holding()
    holding.approvals.viewers(1)
    task = await holding.held()
    holding.proxy.release("a1")
    assert await asyncio.wait_for(task, 5) == STOPPED
    assert holding.decided() == [ToolCallDecided(call=1, decision="withdrawn")]
    assert holding.approvals.waiting == ()


async def test_a_call_allowed_after_its_connection_idled_out_connects_again() -> None:
    holding = Holding(idle=instantly)
    holding.approvals.viewers(1)
    task = await holding.held()
    await eventually(lambda: holding.connector.sessions[0].closed, poll=0)
    holding.approvals.decide(DecideToolCall(call=1, decision="allow"))
    assert await task == CREATED
    first, second = holding.connector.sessions
    assert (first.calls, second.calls) == ([], [("create_issue", {"title": "Flaky test"})])


async def test_a_call_allowed_once_its_server_cant_be_reached_again_fails() -> None:
    holding = Holding(idle=instantly)
    holding.approvals.viewers(1)
    task = await holding.held()
    await eventually(lambda: holding.connector.sessions[0].closed, poll=0)
    holding.connector.failure = "it went away"
    holding.approvals.decide(DecideToolCall(call=1, decision="allow"))
    assert await task == Reply("MCP server github failed: it went away.", is_error=True)
    assert holding.connector.sessions[0].calls == []


async def test_without_approvals_a_call_needing_one_is_declined() -> None:
    holding = Holding(approving=False)
    assert await holding.call(title="Flaky test") == DECLINED
    assert holding.session.calls == []


@pytest.mark.parametrize(
    ("decision", "outcome", "approval"),
    [("allow", "ok", "allowed"), ("deny", "declined", "declined")],
)
async def test_a_held_calls_span_records_how_it_was_settled_and_how_long_it_waited(
    decision: str, outcome: str, approval: str
) -> None:
    holding = Holding()
    holding.approvals.viewers(1)
    task = await holding.held()
    holding.clock.now = 90
    holding.approvals.decide(DecideToolCall.model_validate({"call": 1, "decision": decision}))
    await task
    [span] = holding.traced.spans(CALL_SPAN)
    assert span.attributes is not None
    assert span.attributes["orbit.mcp.outcome"] == outcome
    assert span.attributes["orbit.mcp.approval"] == approval
    assert span.attributes["orbit.mcp.approval_wait_ms"] == 90_000


async def test_a_withdrawn_calls_span_says_so() -> None:
    holding = Holding()
    holding.approvals.viewers(1)
    task = await holding.held()
    holding.proxy.release("a1")
    await task
    [span] = holding.traced.spans(CALL_SPAN)
    assert span.attributes is not None
    assert span.attributes["orbit.mcp.approval"] == "withdrawn"
    assert span.attributes["orbit.mcp.outcome"] == "stopped"


async def test_an_expired_calls_span_says_so_after_an_hour() -> None:
    holding = Holding()
    await (await holding.held())
    [span] = holding.traced.spans(CALL_SPAN)
    assert span.attributes is not None
    assert span.attributes["orbit.mcp.approval"] == "expired"
    assert span.attributes["orbit.mcp.approval_wait_ms"] == HOUR * 1000
