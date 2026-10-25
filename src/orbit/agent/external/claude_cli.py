"""Agent launcher backed by the Claude Code CLI (``claude -p``).

Each turn spawns ``claude -p --output-format stream-json --verbose`` with the
prompt on stdin and translates its JSON-lines output into protocol events.
Later turns of a conversation pass ``--resume <session_id>`` so Claude keeps it.

Agents are runs of the CLI connected to orbit's MCP server, told to fetch their task
from it. Their token travels in the environment, so other users can't read it from the
process list. Their effort is orbit's setting, or the CLI's default, never orbit's own
environment's. Given a collector, agents send the CLI's own metrics and logs to it, labelled
with who they are, but never with orbit's headers.
"""

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, get_args
from urllib.parse import quote

from orbit.agent.application.ports import AgentLaunch, ResumeFailed
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.roles import tool_rules
from orbit.agent.domain.runtimes import Capabilities, Failure, starting_with
from orbit.agent.domain.usage_limits import claude_failure
from orbit.agent.external.processes import spawned, start_failure
from orbit.protocol import (
    AGENT_FAILED,
    PLAN_TASK,
    AssistantMessage,
    Effort,
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
# What a full model id starts with, such as claude-sonnet-5-5; the aliases are the package's.
MODEL_ID_PREFIX = "claude-"
TOOL_ACCESS_ARGS: dict[ToolAccess, tuple[str, ...]] = {
    "off": ("--tools", ""),
    "restricted": (),
    "full": ("--permission-mode", "bypassPermissions"),
}
# The CLI resumes sessions, reports usage, caps turns, and takes every tool access and effort.
CAPABILITIES = Capabilities(
    resume=True,
    usage=True,
    max_turns=True,
    tool_access=frozenset(get_args(ToolAccess)),
    effort=frozenset(get_args(Effort)),
)
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
# The variable setting the CLI's effort, which would otherwise override orbit's setting, or the
# CLI's default when orbit has none.
EFFORT_VARIABLE = "CLAUDE_CODE_EFFORT_LEVEL"
# The variables that send headers with OTLP, which may hold the key to orbit's collector; an
# agent, which can run any command, could leak it.
OTLP_HEADER_VARIABLES = (
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
)
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
    tools = tool_rules(launch.settings.tool_access, launch.tools)
    rules = f"{AGENT_RULES}\n\n{launch.instructions}"
    args = [*("--mcp-config", json.dumps(config), "--strict-mcp-config")]
    if launch.settings.tool_access == "restricted":
        # --restricted removes Bash and its kin, so the agent can't run code.
        # --restricted skips CLAUDE.md discovery; naming the tree (with the variable agent_env
        # sets) brings the project's CLAUDE.md back.
        args += ["--restricted", "--add-dir", os.path.abspath(launch.cwd)]
    if tools.denied:
        args += ["--disallowedTools", ",".join(tools.denied)]
    return [*args, "--allowedTools", ",".join(tools.allowed), "--append-system-prompt", rules]


def agent_env(agent: Agent, telemetry: Mapping[str, str]) -> dict[str, str]:
    """The environment ``agent``'s CLI runs in: orbit's own, without its effort level, with the
    agent's token, and with the ``telemetry`` variables, if any, in place of the OTLP headers
    orbit's holds."""
    return {
        **_inherited(telemetry),
        TOKEN_VARIABLE: agent.token,
        "ENABLE_TOOL_SEARCH": "false",  # Load orbit's tools up front, not on first use.
        # The user's auto-memory is about their own sessions; an agent's context is its task.
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
        "MCP_TOOL_TIMEOUT": str(TOOL_TIMEOUT_MS),
        **telemetry,
    }


def _inherited(telemetry: Mapping[str, str]) -> dict[str, str]:
    """Orbit's environment, as an agent inherits it: without its effort level, and without the
    OTLP headers when the agent sends ``telemetry`` to orbit's collector."""
    dropped = (EFFORT_VARIABLE, *(OTLP_HEADER_VARIABLES if telemetry else ()))
    return {name: value for name, value in os.environ.items() if name not in dropped}


def agent_telemetry_env(
    endpoint: str | None, protocol: str, resource: Mapping[str, str]
) -> dict[str, str]:
    """The variables having an agent's CLI send its metrics and logs to the OTLP collector at
    ``endpoint``, its base URL, over ``protocol`` (``http/protobuf`` or ``grpc``), described by
    the attributes ``resource``; none without an endpoint."""
    if endpoint is None:
        return {}
    # OpenTelemetry's spec has the values percent-encoded, so commas and equals signs in them
    # don't split the list.
    attributes = ",".join(f"{key}={quote(value, safe='')}" for key, value in resource.items())
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": protocol,
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_RESOURCE_ATTRIBUTES": attributes,
    }


def agent_resource(launch: AgentLaunch) -> dict[str, str]:
    """The attributes describing ``launch``'s agent in its telemetry: its task's id, unless it's
    the planner, which has none, its own id, its role and its workspace's id."""
    agent = launch.agent
    task = {} if agent.task_id == PLAN_TASK else {"orbit.task.id": agent.task_id}
    return {
        **task,
        "orbit.agent.id": agent.id,
        "orbit.role": agent.role,
        "orbit.workspace.id": launch.workspace_id,
    }


def check_orbit_connected(message: dict[str, Any]) -> None:
    """Raise unless the CLI's ``init`` message shows orbit's MCP server connecting."""
    servers = {server["name"]: server["status"] for server in message.get("mcp_servers", [])}
    status = servers.get("orbit")
    if status not in LIVE_MCP_STATUSES:
        raise RuntimeError(f"The agent couldn't reach orbit's MCP server (status: {status}).")


def translate(turn_id: str, message: dict[str, Any]) -> list[Event]:
    """Map one stream-json message to protocol events. Unknown messages map to none; a known
    one missing what it should hold raises RuntimeError."""
    try:
        return _events(turn_id, message)
    except KeyError, TypeError:
        raise RuntimeError(f"Unexpected output from claude: {json.dumps(message)[:200]}") from None


def _events(turn_id: str, message: dict[str, Any]) -> list[Event]:
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
    """Runs agents as Claude CLI sessions connected to orbit, run as ``command``, which send
    their telemetry to the OTLP collector at ``endpoint`` over ``protocol``, if given. Its full
    model ids are those ``full_model_ids`` takes, starting with ``MODEL_ID_PREFIX``."""

    capabilities = CAPABILITIES
    full_model_ids = starting_with(MODEL_ID_PREFIX)

    def __init__(
        self,
        command: Sequence[str] = ("claude",),
        *,
        endpoint: str | None = None,
        protocol: str = "http/protobuf",
    ) -> None:
        self._command = tuple(command)
        self._endpoint = endpoint
        self._protocol = protocol
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

    def _remember(self, agent_id: str, turn: _Turn) -> None:
        """Note the Claude session ``turn`` ran in as agent ``agent_id``'s, if it reported one."""
        if turn.session_id:
            self._conversations[agent_id] = turn.session_id

    def conversation(self, agent_id: str) -> str | None:
        """The id of the Claude session agent ``agent_id``'s turns resume."""
        return self._conversations.get(agent_id)

    def set_conversation(self, agent_id: str, handle: str | None) -> None:
        """Have agent ``agent_id``'s next turn resume the Claude session ``handle``, or start a
        new one when it's ``None``."""
        if handle is None:
            self._conversations.pop(agent_id, None)
        else:
            self._conversations[agent_id] = handle

    def failure(self, message: str) -> Failure | None:
        """The failure Claude Code's error ``message`` tells, as :func:`claude_failure` says."""
        return claude_failure(message)

    def _turn(self, launch: AgentLaunch, resume: str | None) -> _Turn:
        """A turn of ``launch``'s agent, resuming the Claude session ``resume`` if given."""
        args = [*self._command, *STREAM_ARGS]
        if launch.settings.model:
            args += ["--model", launch.settings.model]
        if launch.settings.effort:
            args += ["--effort", launch.settings.effort]
        args += TOOL_ACCESS_ARGS[launch.settings.tool_access]
        if resume:
            args += ["--resume", resume]
        args += agent_args(launch)
        return _Turn(args, self.env(launch), launch.cwd, launch.agent.id)

    def env(self, launch: AgentLaunch) -> dict[str, str]:
        """The environment ``launch``'s agent runs in."""
        telemetry = agent_telemetry_env(self._endpoint, self._protocol, agent_resource(launch))
        return agent_env(launch.agent, telemetry)


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
        # Cancelled, or the CLI failed: leaving the stack stops it and whatever it started.
        async with contextlib.AsyncExitStack() as stack:
            process = await self._start(stack)
            async for event in self._converse(turn_id, process, prompt):
                yield event

    async def _start(self, stack: contextlib.AsyncExitStack) -> asyncio.subprocess.Process:
        """The running CLI, stopped when ``stack`` closes."""
        try:
            return await stack.enter_async_context(
                spawned(
                    self._args,
                    env=self._env,
                    cwd=self._cwd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=LINE_LIMIT,
                )
            )
        except OSError as error:
            hint = "Is the Claude CLI installed and on PATH?"
            raise RuntimeError(
                start_failure(self._args, self._cwd, error, missing_hint=hint)
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
                raise _crash(status, (await stderr).decode(errors="replace").strip())
        finally:
            stderr.cancel()

    async def _read_events(
        self, turn_id: str, stdout: asyncio.StreamReader
    ) -> AsyncIterator[Event]:
        async for raw in stdout:
            line = raw.decode(errors="replace").strip()
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
