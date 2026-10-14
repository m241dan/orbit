"""The MCP host over real HTTP on localhost, called the way an agent's CLI calls it."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from orbit.agent.application.ports import Reply
from orbit.agent.external.mcp_host import TOOL_NAMES, McpHost, bearer_token

OK = Reply("ok")


class RecordingHandler:
    def __init__(self, reply: Reply = OK) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(self, token: str, tool: str, arguments: dict[str, Any]) -> Reply:
        self.calls.append((token, tool, arguments))
        return self.reply


@asynccontextmanager
async def serving(handler: RecordingHandler) -> AsyncIterator[str]:
    host = McpHost()
    url = await host.start(handler)
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
        assert set(tools) == set(TOOL_NAMES)
        assert tools["complete_task"].input_schema["required"] == ["summary"]
        assert tools["run_check"].input_schema["required"] == ["step"]
        assert tools["suggest_task"].input_schema["required"] == ["title", "prompt"]
        assert tools["propose_checks"].input_schema["required"] == ["steps"]
        assert all(tool.description for tool in tools.values())


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
        ("suggest_task", {"title": "Add tests", "prompt": "Cover parse()."}),
        ("propose_task", {"title": "Add a CLI flag", "prompt": "Add --fast."}),
        (
            "propose_checks",
            {
                "steps": [{"name": "Lint", "command": ["ruff", "check"]}],
                "setup": {"command": ["uv", "sync"], "watch": ["uv.lock"]},
            },
        ),
    ],
)
async def test_every_tool_passes_its_call_through(tool: str, arguments: dict[str, Any]) -> None:
    handler = RecordingHandler()
    async with serving(handler) as url, connected(url) as client:
        await client.call_tool(tool, arguments)
    assert handler.calls == [("tok-1", tool, arguments)]


async def test_a_proposal_without_setup_passes_none() -> None:
    handler = RecordingHandler()
    steps = [{"name": "Tests", "command": ["pytest"]}]
    async with serving(handler) as url, connected(url) as client:
        await client.call_tool("propose_checks", {"steps": steps})
    assert handler.calls == [("tok-1", "propose_checks", {"steps": steps, "setup": None})]


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


@pytest.mark.parametrize(
    ("header", "token"),
    [(None, ""), ("Bearer abc", "abc"), ("bearer abc", "abc"), ("Basic abc", ""), ("Bearer", "")],
)
def test_bearer_token_reads_only_bearer_authorization(header: str | None, token: str) -> None:
    headers = {} if header is None else {"authorization": header}
    assert bearer_token(headers) == token
    assert bearer_token(None) == ""


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
    with pytest.raises(RuntimeError, match="failed to start") as raised:
        await McpHost().start(RecordingHandler())
    assert isinstance(raised.value.__cause__, OSError)
