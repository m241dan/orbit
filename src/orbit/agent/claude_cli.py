"""Turn runner backed by the Claude Code CLI (``claude -p``).

Each turn spawns ``claude -p --output-format stream-json --verbose`` with the
prompt on stdin and translates its JSON-lines output into protocol events.
Later turns pass ``--resume <session_id>`` so Claude keeps the conversation.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

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


class ClaudeCliRunner:
    def __init__(self, command: Sequence[str] = ("claude",)) -> None:
        self._command = tuple(command)
        self.session_id: str | None = None

    def new_context(self) -> "ClaudeCliRunner":
        """Same CLI, new Claude session (no ``--resume``)."""
        return ClaudeCliRunner(self._command)

    async def run_turn(self, turn_id: str, prompt: str, settings: Settings) -> AsyncIterator[Event]:
        yield TurnStarted(turn_id=turn_id)
        args = [*self._command, *STREAM_ARGS]
        if settings.model:
            args += ["--model", settings.model]
        args += TOOL_ACCESS_ARGS[settings.tool_access]
        if self.session_id:
            args += ["--resume", self.session_id]
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=LINE_LIMIT,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"Could not start {self._command[0]!r}; is the Claude CLI installed and on PATH?"
            ) from None

        assert process.stdin and process.stdout and process.stderr
        stderr = asyncio.create_task(process.stderr.read())
        try:
            process.stdin.write(prompt.encode())
            await process.stdin.drain()
            process.stdin.close()

            finished = False
            async for raw in process.stdout:
                line = raw.decode().strip()
                if not line:
                    continue
                for event in translate(turn_id, self._parse(line)):
                    finished = finished or isinstance(event, TurnFinished)
                    yield event

            status = await process.wait()
            if not finished:
                message = (await stderr).decode().strip()
                raise RuntimeError(message or f"claude exited with status {status}")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            stderr.cancel()

    def _parse(self, line: str) -> dict[str, Any]:
        try:
            message: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            raise RuntimeError(f"Unexpected output from claude: {line[:200]}") from None
        if session_id := message.get("session_id"):
            self.session_id = session_id
        return message
