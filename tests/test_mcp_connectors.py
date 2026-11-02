"""Connecting to external MCP servers: the contract the stdio connector, which starts the
stand-in server as a child process, and the fake connector both keep, then the stdio connector's
own behaviour."""

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path

import pytest

from orbit.agent.application.ports import McpConnector, UpstreamFailed, UpstreamSession
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.external import mcp_stdio
from orbit.agent.external.mcp_stdio import StdioConnector
from orbit.agent.external.processes import process_alive
from tests.mcp_fakes import STAND_IN_TOOLS, FakeConnector
from tests.waiting import eventually

STAND_IN = str(Path(__file__).with_name("mcp_stand_in.py"))
ENVIRON = {"PATH": "/usr/bin:/bin", "HOME": "/home/me", "SECRET": "leaked", "USER": "me"}

type Connect = Callable[[Mapping[str, str], Path], Awaitable[UpstreamSession]]
"""Connects to the stand-in with its own variables in a directory."""


def stand_in(*mode: str, timeout: int = 300) -> McpServer:
    """The stand-in server, run by this Python, in ``mode`` if given, each request waiting at
    most ``timeout`` seconds."""
    return McpServer(command=(sys.executable, "-I", STAND_IN, *mode), timeout_seconds=timeout)


def stdio_connector() -> StdioConnector:
    """The stdio connector, given this machine's ``PATH`` among :data:`ENVIRON`'s variables."""
    return StdioConnector({**ENVIRON, "PATH": _path()})


def _path() -> str:
    """This process's ``PATH``, which finds the programs the tests start."""
    return os.environ["PATH"]


@pytest.fixture(params=["stdio", "fake"])
async def connect(request: pytest.FixtureRequest) -> AsyncIterator[Connect]:
    """Connects to the stand-in through the stdio connector or the fake, closing every session
    after the test."""
    connector: McpConnector = stdio_connector() if request.param == "stdio" else FakeConnector()
    sessions: list[UpstreamSession] = []

    async def opened(env: Mapping[str, str], cwd: Path) -> UpstreamSession:
        session = await connector.open(stand_in(), str(cwd), env)
        sessions.append(session)
        return session

    yield opened
    for session in sessions:
        await session.close()


# The contract


async def test_a_session_lists_every_tool_the_server_offers_in_its_order(
    connect: Connect, tmp_path: Path
) -> None:
    session = await connect({}, tmp_path)
    assert await session.list_tools() == STAND_IN_TOOLS


async def test_a_call_returns_the_tools_text(connect: Connect, tmp_path: Path) -> None:
    session = await connect({}, tmp_path)
    reply = await session.call_tool("get issue!", {"number": 7})
    assert reply.text == "Issue 7: it's broken."
    assert not reply.is_error


async def test_a_tools_own_error_is_an_error_reply(connect: Connect, tmp_path: Path) -> None:
    session = await connect({}, tmp_path)
    reply = await session.call_tool("oops", {})
    assert reply.text == "it went wrong"
    assert reply.is_error


async def test_a_server_that_crashes_fails_the_call_and_those_after(
    connect: Connect, tmp_path: Path
) -> None:
    session = await connect({}, tmp_path)
    with pytest.raises(UpstreamFailed, match="it exited with status 3"):
        await session.call_tool("fail", {})
    with pytest.raises(UpstreamFailed):
        await session.call_tool("get_issue", {"number": 1})


async def test_the_server_runs_in_the_directory_with_its_variables(
    connect: Connect, tmp_path: Path
) -> None:
    session = await connect({"GITHUB_TOKEN": "secret"}, tmp_path)
    seen = json.loads((await session.call_tool("whoami", {})).text)
    assert seen["cwd"] == str(tmp_path)
    assert seen["env"]["GITHUB_TOKEN"] == "secret"


async def test_a_closed_session_fails_its_calls(connect: Connect, tmp_path: Path) -> None:
    session = await connect({}, tmp_path)
    await session.close()
    with pytest.raises(UpstreamFailed):
        await session.call_tool("get_issue", {"number": 1})


async def test_a_call_cancelled_is_cancelled(connect: Connect, tmp_path: Path) -> None:
    session = await connect({}, tmp_path)
    calling = asyncio.ensure_future(session.call_tool("slow", {}))
    await asyncio.sleep(0.2)  # fixed wait: the call reaches the server, which doesn't say so
    calling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(calling, 5)


# The stdio connector


async def open_stand_in(
    server: McpServer, cwd: Path, env: Mapping[str, str] | None = None
) -> UpstreamSession:
    """A session with ``server``, through the stdio connector, in ``cwd`` with ``env``."""
    return await stdio_connector().open(server, str(cwd), env or {})


async def test_the_process_gets_only_path_home_and_its_own_variables(tmp_path: Path) -> None:
    session = await open_stand_in(stand_in(), tmp_path, {"GITHUB_TOKEN": "secret"})
    try:
        seen = json.loads((await session.call_tool("whoami", {})).text)
    finally:
        await session.close()
    assert seen["env"] == {"PATH": _path(), "HOME": "/home/me", "GITHUB_TOKEN": "secret"}


async def test_a_missing_directory_is_made(tmp_path: Path) -> None:
    where = tmp_path / "mcp" / "github"
    session = await open_stand_in(stand_in(), where)
    await session.close()
    assert where.is_dir()


async def test_closing_stops_the_server(tmp_path: Path) -> None:
    session = await open_stand_in(stand_in(), tmp_path)
    pid = json.loads((await session.call_tool("whoami", {})).text)["pid"]
    await session.close()
    assert not process_alive(pid)


async def test_a_program_that_isnt_there_fails_to_start(tmp_path: Path) -> None:
    server = McpServer(command=("no-such-mcp-server",))
    with pytest.raises(UpstreamFailed, match="Could not start 'no-such-mcp-server': no such"):
        await open_stand_in(server, tmp_path)


async def test_a_server_that_exits_at_once_says_how(tmp_path: Path) -> None:
    with pytest.raises(UpstreamFailed, match=r"^it exited with status 1$"):
        await open_stand_in(McpServer(command=("false",)), tmp_path)


async def test_a_server_that_hangs_up_before_starting_dropped_the_connection(
    tmp_path: Path,
) -> None:
    with pytest.raises(UpstreamFailed, match=r"^the connection to it dropped$"):
        await open_stand_in(stand_in("hang-up"), tmp_path)


async def test_a_server_that_hangs_up_on_a_call_dropped_the_connection(tmp_path: Path) -> None:
    session = await open_stand_in(stand_in(), tmp_path)
    try:
        with pytest.raises(UpstreamFailed, match=r"^the connection to it dropped$"):
            await session.call_tool("hang up", {})
    finally:
        await session.close()


async def test_a_call_unanswered_within_the_timeout_fails(tmp_path: Path) -> None:
    session = await open_stand_in(stand_in(timeout=1), tmp_path)
    try:
        with pytest.raises(UpstreamFailed, match=r"^it didn't answer within 1 seconds$"):
            await session.call_tool("slow", {})
    finally:
        await session.close()


async def test_a_server_refusing_to_list_its_tools_fails_with_its_message(tmp_path: Path) -> None:
    session = await open_stand_in(stand_in("refuse-list"), tmp_path)
    try:
        with pytest.raises(UpstreamFailed, match="no such method or tool"):
            await session.list_tools()
    finally:
        await session.close()


async def test_a_call_to_a_tool_the_server_lacks_is_an_error_reply(tmp_path: Path) -> None:
    session = await open_stand_in(stand_in(), tmp_path)
    try:
        reply = await session.call_tool("no_such_tool", {})
    finally:
        await session.close()
    assert reply.is_error
    assert "no such method or tool" in reply.text


async def test_content_other_than_text_comes_back_as_json(tmp_path: Path) -> None:
    session = await open_stand_in(stand_in(), tmp_path)
    try:
        reply = await session.call_tool("picture", {})
    finally:
        await session.close()
    assert reply.text.splitlines()[0] == "A picture:"
    assert json.loads(reply.text.splitlines()[1])["mimeType"] == "image/png"


async def test_lines_that_arent_messages_are_passed_over(tmp_path: Path) -> None:
    session = await open_stand_in(stand_in("garbage"), tmp_path)
    try:
        assert len(await session.list_tools()) == len(STAND_IN_TOOLS)
    finally:
        await session.close()


async def test_only_so_many_pages_of_tools_are_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_stdio, "PAGE_LIMIT", 2)
    session = await open_stand_in(stand_in(), tmp_path)
    try:
        assert await session.list_tools() == STAND_IN_TOOLS[:4]
    finally:
        await session.close()


async def test_a_server_reached_by_a_url_isnt_started(tmp_path: Path) -> None:
    with pytest.raises(UpstreamFailed, match="reached by a url"):
        await open_stand_in(McpServer(url="http://127.0.0.1:1/mcp"), tmp_path)


async def test_cancelling_a_start_stops_the_server(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    starting = asyncio.ensure_future(open_stand_in(stand_in("silent", str(pid_file)), tmp_path))
    await eventually(pid_file.exists)
    pid = int(pid_file.read_text())
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(starting, 10)
    assert not process_alive(pid)
