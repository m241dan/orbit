"""Agent launcher backed by the Claude Code CLI (``claude -p``).

Each turn spawns ``claude -p --output-format stream-json --verbose`` with the
prompt on stdin and translates its JSON-lines output into protocol events.
Later turns of a conversation pass ``--resume <session_id>`` so Claude keeps it.

Agents are runs of the CLI connected to orbit's MCP server, told to fetch their task
from it. Their token travels in the environment, so other users can't read it from the
process list.
"""

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from orbit.agent.application.ports import AgentLaunch, ResumeFailed
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.roles import ORBIT_TOOLS
from orbit.protocol import (
    AGENT_FAILED,
    AssistantMessage,
    ErrorOccurred,
    Event,
    ToolAccess,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    Usage,
)

STREAM_ARGS = ("-p", "--output-format", "stream-json", "--verbose")
# Aliases the CLI resolves to the latest model of each family.
MODEL_ALIASES = ("fable", "opus", "sonnet", "haiku")
TOOL_ACCESS_ARGS: dict[ToolAccess, tuple[str, ...]] = {
    "off": ("--tools", ""),
    "restricted": (),
    "full": ("--permission-mode", "bypassPermissions"),
}
# What a ``result`` message reports about the turn's cost: token counts under ``usage``, and
# totals named differently from Usage's fields.
TOKEN_KINDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
USAGE_TOTALS = {"total_cost_usd": "cost_usd", "num_turns": "turns", "duration_ms": "duration_ms"}
# Tool results (e.g. whole files) arrive as single JSON lines; allow large ones.
LINE_LIMIT = 64 * 1024 * 1024

# How agents reach orbit, and what they're told about it.
TOKEN_VARIABLE = "ORBIT_AGENT_TOKEN"
# complete_task waits for verification, which can take minutes.
TOOL_TIMEOUT_MS = 30 * 60 * 1000
# Statuses the CLI reports for an MCP server that is, or is about to be, usable.
LIVE_MCP_STATUSES = ("connected", "pending")
# What a restricted agent may use besides orbit's tools: reading and searching its tree and
# the web, never running code (--restricted removes Bash and its kin) or writing files.
# WebFetch is read-only too, but --restricted disables it unless --tools names it, and --tools
# would replace the built-in set.
READ_ONLY_TOOLS = ("Read", "Glob", "Grep", "WebSearch")
WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")
# How agents are told to work is in the replies to their calls, which know their role.
AGENT_RULES = (
    "You are an agent working for orbit, which hands out tasks and verifies the work. Talk to"
    " orbit only through its MCP tools (mcp__orbit__*)."
)


def agent_args(launch: AgentLaunch) -> list[str]:
    """Flags connecting a CLI to orbit's MCP server, and only to it, allowing the role's tools
    (and hiding orbit's others) and telling it the rules and its role. A restricted agent can
    read and search its tree and the web, and nothing more."""
    headers = {"Authorization": f"Bearer ${{{TOKEN_VARIABLE}}}"}
    config = {"mcpServers": {"orbit": {"type": "http", "url": launch.url, "headers": headers}}}
    allowed = [f"mcp__orbit__{tool}" for tool in launch.tools]
    hidden = [f"mcp__orbit__{tool}" for tool in ORBIT_TOOLS if tool not in launch.tools]
    rules = f"{AGENT_RULES}\n\n{launch.instructions}"
    args = [*("--mcp-config", json.dumps(config), "--strict-mcp-config")]
    if launch.settings.tool_access == "restricted":
        allowed += READ_ONLY_TOOLS
        hidden += WRITE_TOOLS
        # --restricted skips CLAUDE.md discovery; naming the tree (with the variable agent_env
        # sets) brings the project's CLAUDE.md back.
        args += ["--restricted", "--add-dir", os.path.abspath(launch.cwd)]
    if hidden:
        args += ["--disallowedTools", ",".join(hidden)]
    return [*args, "--allowedTools", ",".join(allowed), "--append-system-prompt", rules]


def agent_env(agent: Agent) -> dict[str, str]:
    return {
        **os.environ,
        TOKEN_VARIABLE: agent.token,
        "ENABLE_TOOL_SEARCH": "false",  # Load orbit's tools up front, not on first use.
        # The user's auto-memory is about their own sessions; an agent's context is its task.
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
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
    usage = _usage(message)
    if message.get("subtype") == "error_max_turns":
        return [TurnFinished(turn_id=turn_id, reason="max_steps", usage=usage)]
    if message.get("is_error"):
        return [
            ErrorOccurred(
                turn_id=turn_id,
                code=AGENT_FAILED,
                message=message.get("result") or message["subtype"],
            ),
            TurnFinished(turn_id=turn_id, reason="error", usage=usage),
        ]
    return [TurnFinished(turn_id=turn_id, reason="completed", usage=usage)]


def _usage(message: dict[str, Any]) -> Usage | None:
    """What the turn a ``result`` message ends cost, if it says anything about that."""
    tokens = message.get("usage") or {}
    fields = {**_known(message, USAGE_TOTALS), **_known(tokens, TOKEN_KINDS)}
    return Usage(**fields) if fields else None


def _known(values: dict[str, Any], names: Mapping[str, str] | tuple[str, ...]) -> dict[str, Any]:
    """The ``values`` given under ``names``' keys, keyed by the Usage fields they name (a tuple
    names fields of the same name)."""
    fields = names if isinstance(names, Mapping) else {name: name for name in names}
    return {field: values[key] for key, field in fields.items() if values.get(key) is not None}


def _pipes(
    process: asyncio.subprocess.Process,
) -> tuple[asyncio.StreamWriter, asyncio.StreamReader, asyncio.StreamReader]:
    """The process's stdin, stdout and stderr; it was started with all three piped."""
    assert process.stdin and process.stdout and process.stderr
    return process.stdin, process.stdout, process.stderr


async def _send(stdin: asyncio.StreamWriter, prompt: str) -> None:
    """Write ``prompt`` to the CLI and close its stdin."""
    # A CLI that exits before reading its prompt (rejecting its flags, say) breaks the pipe;
    # what it printed says why, so the turn reads that rather than failing here.
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        stdin.write(prompt.encode())
        await stdin.drain()
    stdin.close()


class _Crashed(RuntimeError):
    """The CLI ran but exited without finishing the turn."""


def _crash(status: int, stderr: str) -> _Crashed:
    """The error for a CLI that exited without finishing the turn."""
    return _Crashed(stderr or f"claude exited with status {status}")


class ClaudeCliRunner:
    """Runs agents as Claude CLI sessions connected to orbit."""

    def __init__(self, command: Sequence[str] = ("claude",)) -> None:
        self._command = tuple(command)
        self._conversations: dict[str, str] = {}  # Agent id -> its Claude session's id.

    async def run_agent(self, launch: AgentLaunch) -> AsyncIterator[Event]:
        """Run the agent as a new Claude session connected to orbit, as ``launch`` says."""
        async for event in self._turn(launch, None).run(launch.agent.id, launch.prompt):
            yield event

    async def converse(self, turn_id: str, launch: AgentLaunch) -> AsyncIterator[Event]:
        """A turn of the agent's conversation: its first starts a Claude session, later ones
        resume it, each agent's apart from the others'. A resuming turn whose CLI exits before
        reporting its session raises ResumeFailed."""
        agent_id = launch.agent.id
        resume = self._conversations.get(agent_id)
        turn = self._turn(launch, resume)
        try:
            async for event in turn.run(turn_id, launch.prompt):
                yield event
        except _Crashed as exc:
            if resume and not turn.session_id:
                raise ResumeFailed(str(exc)) from exc
            raise
        finally:
            self._remember(agent_id, turn)

    def _remember(self, agent_id: str, turn: "_Turn") -> None:
        """Note the Claude session ``turn`` ran in as agent ``agent_id``'s, if it reported one."""
        if turn.session_id:
            self._conversations[agent_id] = turn.session_id

    def conversation(self, agent_id: str) -> str | None:
        """The id of the Claude session agent ``agent_id``'s turns resume."""
        return self._conversations.get(agent_id)

    def set_conversation(self, agent_id: str, conversation_id: str | None) -> None:
        """Have agent ``agent_id``'s next turn resume the Claude session ``conversation_id``, or
        start a new one when it's ``None``."""
        if conversation_id is None:
            self._conversations.pop(agent_id, None)
        else:
            self._conversations[agent_id] = conversation_id

    def _turn(self, launch: AgentLaunch, resume: str | None) -> "_Turn":
        """A turn of ``launch``'s agent, resuming the Claude session ``resume`` if given."""
        args = [*self._command, *STREAM_ARGS]
        if launch.settings.model:
            args += ["--model", launch.settings.model]
        args += TOOL_ACCESS_ARGS[launch.settings.tool_access]
        if resume:
            args += ["--resume", resume]
        args += agent_args(launch)
        return _Turn(args, agent_env(launch.agent), launch.cwd, launch.agent.id)


class _Turn:
    """One run of the CLI: sends a prompt and translates what it says, noting the id of the
    Claude session it ran in."""

    def __init__(self, args: Sequence[str], env: Mapping[str, str], cwd: str, agent_id: str):
        self._args = args
        self._env = env
        self._cwd = cwd
        self._agent_id = agent_id
        self.session_id: str | None = None

    async def run(self, turn_id: str, prompt: str) -> AsyncIterator[Event]:
        async for event in self._run(turn_id, prompt):
            yield event.model_copy(update={"agent_id": self._agent_id})

    async def _run(self, turn_id: str, prompt: str) -> AsyncIterator[Event]:
        yield TurnStarted(turn_id=turn_id)
        process = await self._start()
        try:
            async for event in self._converse(turn_id, process, prompt):
                yield event
        finally:
            # Cancelled, or the CLI failed: don't leave it running.
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def _start(self) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *self._args,
                env=self._env,
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=LINE_LIMIT,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"Could not start {self._args[0]!r}; is the Claude CLI installed and on PATH?"
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
            await _send(stdin, prompt)
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
        if message.get("subtype") == "init":
            check_orbit_connected(message)
        return message
