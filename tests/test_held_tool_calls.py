"""An agent's call to an MCP tool its server holds for approval, through a workspace's host:
published as it's held and shown in the snapshot, decided once by the user, withdrawn as the
agent is cancelled, declined as orbit closes, and numbered from the session's saved mark."""

import asyncio

from orbit.agent import WorkspaceHost
from orbit.agent.application.desk import STOPPED_HANDLING
from orbit.agent.application.ports import Reply
from orbit.agent.application.tool_proxy import McpProxying
from orbit.agent.domain.mcp_proxy import UpstreamTool
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.domain.roles import Role, ServerGrant
from orbit.protocol import (
    UNKNOWN,
    AddTask,
    AgentReply,
    AgentRequest,
    CancelAgent,
    CommandRefused,
    DecideToolCall,
    Event,
    RunTasks,
    TaskSpec,
    ToolCallDecided,
    ToolCallHeld,
    TurnEndReason,
)
from tests.agent_fakes import (
    ROLES,
    Call,
    FakeStore,
    Script,
    events_until,
    idle,
    of_type,
    refused,
    send,
    session_for,
    snapshot_of,
)
from tests.mcp_fakes import FakeConnector, FakeCredentials
from tests.waiting import eventually

TOOLS = (
    UpstreamTool("get_issue", "Get an issue.", {"type": "object"}),
    UpstreamTool("create_issue", "Create an issue.", {"type": "object"}),
)
GITHUB = ServerGrant(server="github", tools=("get_issue", "create_issue"))
CODER = ROLES["coder"].model_copy(
    update={"tools": ROLES["coder"].tools.model_copy(update={"mcp": (GITHUB,)})}
)
ROLES_WITH_GITHUB: dict[str, Role] = {**ROLES, "coder": CODER}
HELD = ToolCallHeld(
    call=1,
    agent_id="agent-1",
    role="coder",
    server="github",
    tool="github__create_issue",
    arguments={"title": "Flaky test"},
)


def filing(heard: list[Reply]) -> Script:
    """A coder that files an issue, noting what it hears, then claims its task done."""

    async def turn(call: Call) -> TurnEndReason:
        await call("get_task")
        heard.append(await call("github__create_issue", title="Flaky test"))
        await call("complete_task", summary="Filed it.")
        return "completed"

    return turn


class Filing:
    """A session whose coder files an issue through a server that holds ``create_issue`` for
    approval, saved in ``store``, with what the coder heard and the connector reaching the
    server."""

    def __init__(self, store: FakeStore | None = None) -> None:
        self.heard: list[Reply] = []
        self.connector = FakeConnector(TOOLS)
        servers = {"github": McpServer(command=("github-mcp",), approve=("create_issue",))}
        mcp = McpProxying(servers, self.connector, FakeCredentials({}), "/state/mcp")
        self.store = store or FakeStore()
        self.session: WorkspaceHost = session_for(
            filing(self.heard), roles=ROLES_WITH_GITHUB, mcp=mcp, store=self.store
        )[0]

    async def held(self) -> list[Event]:
        """The events of the coder's task, run until its call is held."""
        await send(self.session, AddTask(task=TaskSpec(prompt="File the flaky test.")))
        await send(self.session, RunTasks())
        return await events_until(self.session, of_type(ToolCallHeld))

    def upstream_calls(self) -> list[tuple[str, object]]:
        return [call for session in self.connector.sessions for call in session.calls]


async def test_a_held_call_reaches_the_server_only_once_the_user_allows_it() -> None:
    filing = Filing()
    await filing.held()
    snapshot = await snapshot_of(filing.session)
    assert filing.upstream_calls() == []
    await send(filing.session, DecideToolCall(call=1, decision="allow"))
    events = await events_until(filing.session, idle())
    await filing.session.close()

    assert snapshot.tool_calls == (HELD,)
    assert ToolCallDecided(call=1, decision="allowed") in events
    assert filing.upstream_calls() == [("create_issue", {"title": "Flaky test"})]
    assert filing.heard == [Reply("Created issue 'Flaky test'.")]
    assert filing.store.last.last_tool_call == 1


async def test_a_declined_call_never_reaches_the_server_and_a_later_decision_is_refused() -> None:
    filing = Filing()
    await filing.held()
    await send(filing.session, DecideToolCall(call=1, decision="deny"))
    await send(filing.session, DecideToolCall(call=1, decision="allow"))
    events = await events_until(filing.session, of_type(CommandRefused))
    await eventually(lambda: filing.heard)
    snapshot = await snapshot_of(filing.session)
    await filing.session.close()

    assert events[-1] == refused(UNKNOWN, "No tool call 1 is waiting for approval.")
    assert [e.decision for e in events if isinstance(e, ToolCallDecided)] == ["declined"]
    assert filing.heard == [Reply("The user declined github__create_issue.", is_error=True)]
    assert (filing.upstream_calls(), snapshot.tool_calls) == ([], ())


def requested(events: list[Event]) -> dict[str, str]:
    """The tool each call among ``events`` was to, by request."""
    return {e.request_id: e.tool for e in events if isinstance(e, AgentRequest)}


def reply_to(events: list[Event], tool: str) -> AgentReply:
    """The reply, among ``events``, to the call to ``tool`` among them."""
    tools = requested(events)
    return next(e for e in events if isinstance(e, AgentReply) and tools.get(e.request_id) == tool)


async def test_cancelling_the_agent_withdraws_its_call_and_answers_it() -> None:
    filing = Filing()
    events = await filing.held()
    await send(filing.session, CancelAgent(agent_id="agent-1"))
    events += await events_until(filing.session, idle())
    await filing.session.close()

    assert ToolCallDecided(call=1, decision="withdrawn") in events
    reply = reply_to(events, HELD.tool)
    assert (reply.text, reply.is_error) == (STOPPED_HANDLING.text, True)
    assert filing.upstream_calls() == []


async def test_closing_declines_the_calls_held() -> None:
    filing = Filing()
    await filing.held()
    decided = asyncio.ensure_future(events_until(filing.session, of_type(ToolCallDecided)))
    await asyncio.sleep(0)
    await filing.session.close()

    assert (await decided)[-1] == ToolCallDecided(call=1, decision="declined")
    assert filing.upstream_calls() == []
