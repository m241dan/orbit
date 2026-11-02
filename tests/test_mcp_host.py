"""The MCP host over real HTTP on localhost, called the way an agent's CLI calls it."""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, TextContent

from orbit.agent.application.ports import HostFailed, Reply, ToolListing
from orbit.agent.domain.mcp_proxy import ProxiedTool, UpstreamTool
from orbit.agent.domain.tools import COMPLETE_TASK, GET_TASK, ORBIT_TOOLS
from orbit.agent.external import mcp_host
from orbit.agent.external.mcp_host import PROMPT_RECIPE, McpHost
from tests.waiting import eventually

OK = Reply("ok")


class Sockets:
    """Stands in for the socket module, keeping each socket it makes."""

    def __init__(self) -> None:
        self.made: list[socket.socket] = []

    def socket(self) -> socket.socket:
        made = socket.socket()
        self.made.append(made)
        return made


class RecordingHandler:
    def __init__(self, reply: Reply = OK) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(self, token: str, tool: str, arguments: dict[str, Any]) -> Reply:
        self.calls.append((token, tool, arguments))
        return self.reply


GET_ISSUE = ProxiedTool(
    "github__get_issue_",
    "github",
    UpstreamTool(
        "get issue!",
        "Get an issue.",
        {"type": "object", "properties": {"number": {"type": "integer"}}, "required": ["number"]},
    ),
)


class Lister:
    """Lists each token's tools as ``listings`` say, by token, and every orbit tool for others,
    noting the tokens asked for."""

    def __init__(self, listings: dict[str, ToolListing]) -> None:
        self.listings = listings
        self.asked: list[str] = []

    async def __call__(self, token: str) -> ToolListing:
        self.asked.append(token)
        return self.listings.get(token, ToolListing(ORBIT_TOOLS))


@asynccontextmanager
async def serving(handler: RecordingHandler, lister: Lister | None = None) -> AsyncIterator[str]:
    host = McpHost()
    url = await (host.start(handler) if lister is None else host.start(handler, lister))
    try:
        yield url
    finally:
        await host.stop()


@asynccontextmanager
async def connected(url: str, token: str | None = "tok-1") -> AsyncIterator[Client]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with (
        httpx2.AsyncClient(headers=headers) as http,
        Client(streamable_http_client(url, http_client=http)) as client,
    ):
        yield client


def text(result: Any) -> str:
    [content] = result.content
    assert isinstance(content, TextContent)
    return content.text


async def test_serves_on_a_random_local_port() -> None:
    async with serving(RecordingHandler()) as url:
        assert url.startswith("http://127.0.0.1:") and url.endswith("/mcp")
        assert int(url.split(":")[2].split("/")[0]) > 0


async def test_offers_orbit_tools_with_their_arguments() -> None:
    async with serving(RecordingHandler()) as url, connected(url) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(tools) == set(ORBIT_TOOLS)
        assert tools["complete_task"].input_schema["required"] == ["summary"]
        assert tools["run_check"].input_schema["required"] == ["step"]
        assert tools["suggest_task"].input_schema["required"] == ["title", "prompt"]
        assert tools["suggest_task"].input_schema["properties"]["severity"]["enum"] == [
            "blocking",
            "normal",
        ]
        assert tools["report_finding"].input_schema["required"] == ["title", "body", "severity"]
        assert tools["dismiss_finding"].input_schema["required"] == ["finding", "reason"]
        severity = tools["report_finding"].input_schema["properties"]["severity"]
        assert severity["enum"] == ["blocking", "normal"]
        assert tools["propose_task"].input_schema["required"] == ["title", "prompt"]
        assert tools["propose_checks"].input_schema["required"] == ["steps"]
        assert tools["read_skill"].input_schema["required"] == ["skill", "file"]
        assert all(tool.description for tool in tools.values())


async def test_the_tools_that_take_a_coders_prompt_say_what_it_must_cover() -> None:
    async with serving(RecordingHandler()) as url, connected(url) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        for name in ("suggest_task", "propose_task"):
            # What the tool is for, then the recipe; the role instructions say the rest.
            assert (tools[name].description or "").endswith(PROMPT_RECIPE)
        assert all(part in PROMPT_RECIPE for part in ("Where", "Change", "Keep", "Done"))


async def test_propose_task_says_to_name_the_skills_the_tasks_coder_needs() -> None:
    async with serving(RecordingHandler()) as url, connected(url) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert (
        "Name in skills what the task's coder needs beyond its role's own: the language and kind"
        " of the code it changes, and the project's skills that bear on it."
    ) in (tools["propose_task"].description or "")


async def test_read_skill_says_it_reads_a_file_of_ones_skills_by_their_names() -> None:
    async with serving(RecordingHandler()) as url, connected(url) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert tools["read_skill"].description == (
        "Read a file of one of your skills, by the skill's name and the file's path in it, such"
        " as testing.md."
    )


async def test_passes_calls_with_the_token_to_the_handler_and_returns_its_text() -> None:
    handler = RecordingHandler(Reply("Checks passed. End your turn now."))
    async with serving(handler) as url, connected(url) as client:
        result = await client.call_tool("complete_task", {"summary": "did it"})
    assert handler.calls == [("tok-1", "complete_task", {"summary": "did it"})]
    assert not result.is_error
    assert text(result) == "Checks passed. End your turn now."
    assert result.structured_content is None


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_task", {}),
        ("complete_task", {"summary": "done"}),
        ("run_check", {"step": "Unit tests"}),
        ("report_progress", {"note": "halfway"}),
        ("give_up", {"reason": "blocked"}),
        (
            "suggest_task",
            {
                "title": "Add tests",
                "prompt": "Cover parse().",
                "findings": ["F1", "F3"],
                "severity": "blocking",
            },
        ),
        ("report_finding", {"title": "Off by one", "body": "In parse().", "severity": "normal"}),
        ("dismiss_finding", {"finding": "F2", "reason": "Already covered."}),
        (
            "propose_task",
            {"title": "Add a CLI flag", "prompt": "Add --fast.", "skills": ["python", "cli"]},
        ),
        ("consult", {"role": "experimenter", "message": "Check X"}),
        ("save_report", {"title": "Timings", "body": "It took 3s."}),
        ("list_reports", {}),
        ("read_report", {"id": 2}),
        ("list_drafts", {}),
        ("read_draft", {"task_id": "5"}),
        ("edit_draft", {"task_id": "5", "title": "New title", "prompt": "Body"}),
        ("remove_draft", {"task_id": "5"}),
        ("move_draft", {"task_id": "5", "before": "3"}),
        ("move_draft", {"task_id": "5", "before": None}),
        ("read_skill", {"skill": "code-style", "file": "testing.md"}),
        (
            "propose_checks",
            {
                "steps": [{"name": "Lint", "command": ["ruff", "check"]}],
                "setup": {"command": ["uv", "sync"], "watch": ["uv.lock"]},
            },
        ),
        (
            "propose_checks",
            {
                "steps": [
                    {"name": "Lint", "command": ["ruff", "check"]},
                    {"name": "Mutants", "command": ["mutmut", "run"], "set": "mutation"},
                ],
                "setup": None,
                "sets": {"mutation": True},
                "triggers": {
                    "mutants": {
                        "on": "manual",
                        "checks": "mutation",
                        "every": 2,
                        "manual": True,
                        "label": "Mutants",
                        "enabled": False,
                    }
                },
                "reports": {
                    "mutation-report": {
                        "set": "mutation",
                        "role": "tests-reviewer",
                        "status": "any",
                        "prompt": "Report {output}.",
                    }
                },
            },
        ),
    ],
)
async def test_every_tool_passes_its_call_through(tool: str, arguments: dict[str, Any]) -> None:
    handler = RecordingHandler()
    async with serving(handler) as url, connected(url) as client:
        await client.call_tool(tool, arguments)
    assert handler.calls == [("tok-1", tool, arguments)]


async def test_a_suggestion_without_findings_or_severity_passes_none_and_normal() -> None:
    handler = RecordingHandler()
    async with serving(handler) as url, connected(url) as client:
        await client.call_tool("suggest_task", {"title": "Add tests", "prompt": "Cover it."})
    assert handler.calls == [
        (
            "tok-1",
            "suggest_task",
            {"title": "Add tests", "prompt": "Cover it.", "findings": [], "severity": "normal"},
        )
    ]


async def test_a_proposal_without_setup_passes_none() -> None:
    handler = RecordingHandler()
    steps = [{"name": "Tests", "command": ["pytest"]}]
    async with serving(handler) as url, connected(url) as client:
        await client.call_tool("propose_checks", {"steps": steps})
    assert handler.calls == [("tok-1", "propose_checks", {"steps": steps, "setup": None})]


async def test_a_proposed_task_without_skills_passes_none() -> None:
    handler = RecordingHandler()
    async with serving(handler) as url, connected(url) as client:
        await client.call_tool("propose_task", {"title": "Add a flag", "prompt": "Add --fast."})
    assert handler.calls == [
        ("tok-1", "propose_task", {"title": "Add a flag", "prompt": "Add --fast.", "skills": None})
    ]


async def test_error_replies_become_tool_errors() -> None:
    handler = RecordingHandler(Reply("Call get_task first.", is_error=True))
    async with serving(handler) as url, connected(url) as client:
        result = await client.call_tool("give_up", {"reason": "nah"})
    assert result.is_error
    assert "Call get_task first." in text(result)


async def test_a_call_without_a_token_reaches_the_handler_with_an_empty_one() -> None:
    handler = RecordingHandler()
    async with serving(handler) as url, connected(url, token=None) as client:
        await client.call_tool("report_progress", {"note": "hi"})
    assert handler.calls == [("", "report_progress", {"note": "hi"})]


async def test_stop_closes_the_port() -> None:
    host = McpHost()
    url = await host.start(RecordingHandler())
    await host.stop()
    async with httpx2.AsyncClient() as http:
        with pytest.raises(httpx2.ConnectError):
            await http.get(url)


async def test_stopping_a_host_that_never_started_is_harmless() -> None:
    await McpHost().stop()


async def test_a_server_that_fails_to_start_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(self: object, sockets: object = None) -> None:
        raise OSError("no sockets for you")

    monkeypatch.setattr("uvicorn.Server.serve", broken)
    with pytest.raises(HostFailed, match="failed to start") as raised:
        await McpHost().start(RecordingHandler())
    assert isinstance(raised.value.__cause__, OSError)


async def test_a_server_that_fails_to_start_closes_its_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(self: object, sockets: object = None) -> None:
        raise OSError("no sockets for you")

    sockets = Sockets()
    monkeypatch.setattr(mcp_host, "socket", sockets)
    monkeypatch.setattr("uvicorn.Server.serve", broken)
    with pytest.raises(HostFailed, match=r"^Orbit's MCP server failed to start\.$"):
        await McpHost().start(RecordingHandler())
    [made] = sockets.made
    assert made.fileno() == -1


async def test_a_server_that_cant_be_built_closes_its_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unbuildable(handler: object, lister: object) -> None:
        raise ValueError("no server for you")

    sockets = Sockets()
    monkeypatch.setattr(mcp_host, "socket", sockets)
    monkeypatch.setattr(mcp_host, "build_server", unbuildable)
    with pytest.raises(ValueError, match="no server for you"):
        await McpHost().start(RecordingHandler())
    [made] = sockets.made
    assert made.fileno() == -1


async def test_starting_cancelled_leaves_nothing_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    serving = asyncio.Event()

    async def hang(self: object, sockets: object = None) -> None:
        serving.set()
        await asyncio.Event().wait()

    sockets = Sockets()
    monkeypatch.setattr(mcp_host, "socket", sockets)
    monkeypatch.setattr("uvicorn.Server.serve", hang)
    host = McpHost()
    starting = asyncio.create_task(host.start(RecordingHandler()))
    await eventually(serving.is_set)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert asyncio.all_tasks() == {asyncio.current_task()}
    [made] = sockets.made
    assert made.fileno() == -1
    await host.stop()


# Each agent's own tools


LISTINGS = {
    "tok-1": ToolListing((GET_TASK, COMPLETE_TASK), (GET_ISSUE,)),
    "tok-2": ToolListing((GET_TASK,)),
}


async def test_each_token_lists_its_own_orbit_tools_and_proxied_ones() -> None:
    lister = Lister(LISTINGS)
    async with serving(RecordingHandler(), lister) as url:
        async with connected(url, "tok-1") as client:
            first = {tool.name: tool for tool in (await client.list_tools()).tools}
        async with connected(url, "tok-2") as client:
            second = [tool.name for tool in (await client.list_tools()).tools]
    assert list(first) == [GET_TASK, COMPLETE_TASK, "github__get_issue_"]
    assert first["github__get_issue_"].description == "Get an issue."
    assert first["github__get_issue_"].input_schema == GET_ISSUE.tool.input_schema
    assert second == [GET_TASK]
    assert lister.asked == ["tok-1", "tok-2"]


async def test_a_call_to_a_proxied_name_goes_to_the_handler_with_the_token() -> None:
    handler = RecordingHandler(Reply("Issue 7: it's broken."))
    async with serving(handler, Lister(LISTINGS)) as url, connected(url) as client:
        result = await client.call_tool("github__get_issue_", {"number": 7})
    assert handler.calls == [("tok-1", "github__get_issue_", {"number": 7})]
    assert not result.is_error
    assert text(result) == "Issue 7: it's broken."


async def test_a_proxied_call_the_handler_refuses_is_an_error() -> None:
    handler = RecordingHandler(Reply("roles.coder doesn't allow github__create_issue.", True))
    async with serving(handler, Lister(LISTINGS)) as url, connected(url, "tok-2") as client:
        result = await client.call_tool("github__create_issue", None)
    assert handler.calls == [("tok-2", "github__create_issue", {})]
    assert result.is_error
    assert text(result) == "roles.coder doesn't allow github__create_issue."


async def test_a_proxied_call_whose_arguments_arent_an_object_is_refused() -> None:
    handler = RecordingHandler()
    async with serving(handler, Lister(LISTINGS)) as url, connected(url) as client:
        # Built unchecked, as an agent's CLI could send it; serializing it warns.
        params = CallToolRequestParams.model_construct(name="github__x", arguments=[1])
        with pytest.warns(UserWarning, match="serialized value may not be as expected"):
            result = await client.session.send_request(
                CallToolRequest(params=params), CallToolResult
            )
    assert handler.calls == []
    assert result.is_error
    assert text(result) == "A tool's arguments are an object."


async def test_a_call_to_an_orbit_tool_still_reaches_orbits_tool() -> None:
    handler = RecordingHandler()
    async with serving(handler, Lister(LISTINGS)) as url, connected(url) as client:
        result = await client.call_tool("complete_task", {"summary": "done"})
    assert handler.calls == [("tok-1", "complete_task", {"summary": "done"})]
    assert text(result) == "ok"
