"""Turn runner backed by the Claude Code CLI (``claude -p``).

Each turn spawns ``claude -p --output-format stream-json --verbose`` with the
prompt on stdin and translates its JSON-lines output into protocol events.
Later turns pass ``--resume <session_id>`` so Claude keeps the conversation.

Agents are runs of the same CLI connected to orbit's MCP server, told to fetch their task
from it. Their token travels in the environment, so other users can't read it from the
process list.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from orbit.agent.application.ports import AgentLaunch
from orbit.agent.domain.agents import Agent
from orbit.protocol import (
    AssistantMessage,
    ErrorOccurred,
    Event,
    Settings,
    ToolAccess,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)

STREAM_ARGS = ("-p", "--output-format", "stream-json", "--verbose")
# Aliases the CLI resolves to the latest model of each family.
MODEL_ALIASES = ("fable", "opus", "sonnet", "haiku")
TOOL_ACCESS_ARGS: dict[ToolAccess, tuple[str, ...]] = {
    "off": ("--tools", ""),
    "restricted": (),
    "full": ("--permission-mode", "bypassPermissions"),
}
# Tool results (e.g. whole files) arrive as single JSON lines; allow large ones.
LINE_LIMIT = 64 * 1024 * 1024

# How agents reach orbit, and what they're told about it.
TOKEN_VARIABLE = "ORBIT_AGENT_TOKEN"
# complete_task waits for verification, which can take minutes.
TOOL_TIMEOUT_MS = 30 * 60 * 1000
# Statuses the CLI reports for an MCP server that is, or is about to be, usable.
LIVE_MCP_STATUSES = ("connected", "pending")
AGENT_RULES = (
    "You are an agent working for orbit, which hands out tasks and verifies the work. Talk to"
    " orbit only through its MCP tools (mcp__orbit__*). Fetch your task with get_task, and when"
    " you're done, call complete_task and wait for its reply: it says whether verification"
    " passed, or what to fix. Once it says verification passed, end your turn immediately."
)


def agent_args(url: str) -> list[str]:
    """Flags connecting a CLI to orbit's MCP server at ``url``, and only to it."""
    headers = {"Authorization": f"Bearer ${{{TOKEN_VARIABLE}}}"}
    config = {"mcpServers": {"orbit": {"type": "http", "url": url, "headers": headers}}}
    return [
        *("--mcp-config", json.dumps(config), "--strict-mcp-config"),
        *("--allowedTools", "mcp__orbit__*", "--append-system-prompt", AGENT_RULES),
    ]


def agent_env(agent: Agent) -> dict[str, str]:
    return {
        **os.environ,
        TOKEN_VARIABLE: agent.token,
        "ENABLE_TOOL_SEARCH": "false",  # Load orbit's tools up front, not on first use.
        "MCP_TOOL_TIMEOUT": str(TOOL_TIMEOUT_MS),
    }


def check_orbit_connected(message: dict[str, Any]) -> None:
    """Raise unless the CLI's ``init`` message shows orbit's MCP server connecting."""
    servers = {server["name"]: server["status"] for server in message.get("mcp_servers", [])}
    status = servers.get("orbit")
    if status not in LIVE_MCP_STATUSES:
        raise RuntimeError(f"The agent couldn't reach orbit's MCP server (status: {status}).")


def translate(turn_id: str, message: dict[str, Any]) -> list[Event]:
    """Map one stream-json message to protocol events. Unknown messages map to none."""
    match message.get("type"):
        case "assistant":
            return _assistant_events(turn_id, message["message"]["content"])
        case "user":
            return _tool_result_events(turn_id, message["message"]["content"])
        case "result":
            return _result_events(turn_id, message)
        case _:
            return []


def _assistant_events(turn_id: str, blocks: list[dict[str, Any]]) -> list[Event]:
    events: list[Event] = []
    for block in blocks:
        if block["type"] == "text" and block["text"]:
            events.append(AssistantMessage(turn_id=turn_id, text=block["text"]))
        elif block["type"] == "tool_use":
            events.append(
                ToolCallStarted(
                    turn_id=turn_id,
                    call_id=block["id"],
                    name=block["name"],
                    arguments=block["input"],
                )
            )
    return events


def _tool_result_events(turn_id: str, content: str | list[dict[str, Any]]) -> list[Event]:
    if isinstance(content, str):
        return []
    return [
        ToolCallFinished(
            turn_id=turn_id,
            call_id=block["tool_use_id"],
            output=_tool_output(block["content"]),
            is_error=block.get("is_error", False),
        )
        for block in content
        if block["type"] == "tool_result"
    ]


def _tool_output(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(part["text"] for part in content if part["type"] == "text")


def _result_events(turn_id: str, message: dict[str, Any]) -> list[Event]:
    if message.get("subtype") == "error_max_turns":
        return [TurnFinished(turn_id=turn_id, reason="max_steps")]
    if message.get("is_error"):
        return [
            ErrorOccurred(turn_id=turn_id, message=message.get("result") or message["subtype"]),
            TurnFinished(turn_id=turn_id, reason="error"),
        ]
    return [TurnFinished(turn_id=turn_id, reason="completed")]


def _pipes(
    process: asyncio.subprocess.Process,
) -> tuple[asyncio.StreamWriter, asyncio.StreamReader, asyncio.StreamReader]:
    """The process's stdin, stdout and stderr; it was started with all three piped."""
    assert process.stdin and process.stdout and process.stderr
    return process.stdin, process.stdout, process.stderr


def _crash(status: int, stderr: str) -> RuntimeError:
    """The error for a CLI that exited without finishing the turn."""
    return RuntimeError(stderr or f"claude exited with status {status}")


class ClaudeCliRunner:
    def __init__(self, command: Sequence[str] = ("claude",)) -> None:
        self._command = tuple(command)
        self.session_id: str | None = None
        # Agents' runs only: orbit must be reachable, and where and how the process runs.
        self._needs_orbit = False
        self._env: Mapping[str, str] | None = None
        self._cwd: str | None = None

    def new_context(self) -> "ClaudeCliRunner":
        """Same CLI, new Claude session (no ``--resume``)."""
        return ClaudeCliRunner(self._command)

    async def run_turn(self, turn_id: str, prompt: str, settings: Settings) -> AsyncIterator[Event]:
        async for event in self._run(turn_id, self._args(settings), prompt):
            yield event

    async def run_agent(self, launch: AgentLaunch) -> AsyncIterator[Event]:
        """Run the agent as a new Claude session connected to orbit, as ``launch`` says."""
        agent = launch.agent
        runner = self.new_context()
        runner._needs_orbit = True
        runner._env, runner._cwd = agent_env(agent), launch.cwd
        args = [*runner._args(launch.settings), *agent_args(launch.url)]
        async for event in runner._run(agent.id, args, launch.prompt):
            yield event.model_copy(update={"agent_id": agent.id})

    async def _run(self, turn_id: str, args: Sequence[str], prompt: str) -> AsyncIterator[Event]:
        yield TurnStarted(turn_id=turn_id)
        process = await self._start(args)
        try:
            async for event in self._converse(turn_id, process, prompt):
                yield event
        finally:
            # Cancelled, or the CLI failed: don't leave it running.
            if process.returncode is None:
                process.kill()
                await process.wait()

    def _args(self, settings: Settings) -> list[str]:
        args = [*self._command, *STREAM_ARGS]
        if settings.model:
            args += ["--model", settings.model]
        args += TOOL_ACCESS_ARGS[settings.tool_access]
        if self.session_id:
            args += ["--resume", self.session_id]
        return args

    async def _start(self, args: Sequence[str]) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *args,
                env=self._env,
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=LINE_LIMIT,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"Could not start {self._command[0]!r}; is the Claude CLI installed and on PATH?"
            ) from None

    async def _converse(
        self, turn_id: str, process: asyncio.subprocess.Process, prompt: str
    ) -> AsyncIterator[Event]:
        """Send ``prompt``, then translate the CLI's output until it exits.

        Raises if it exits without reporting the turn's end, with its stderr as the message.
        """
        stdin, stdout, stderr_pipe = _pipes(process)
        stderr = asyncio.create_task(stderr_pipe.read())
        try:
            stdin.write(prompt.encode())
            await stdin.drain()
            stdin.close()
            finished = False
            async for event in self._read_events(turn_id, stdout):
                finished = finished or isinstance(event, TurnFinished)
                yield event
            # Let the CLI exit on its own once it's done, so it can save its session.
            status = await process.wait()
            if not finished:
                raise _crash(status, (await stderr).decode().strip())
        finally:
            stderr.cancel()

    async def _read_events(
        self, turn_id: str, stdout: asyncio.StreamReader
    ) -> AsyncIterator[Event]:
        async for raw in stdout:
            line = raw.decode().strip()
            if line:
                for event in translate(turn_id, self._parse(line)):
                    yield event

    def _parse(self, line: str) -> dict[str, Any]:
        try:
            message: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            raise RuntimeError(f"Unexpected output from claude: {line[:200]}") from None
        if session_id := message.get("session_id"):
            self.session_id = session_id
        if self._needs_orbit and message.get("subtype") == "init":
            check_orbit_connected(message)
        return message
