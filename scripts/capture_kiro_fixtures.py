"""Captures kiro-cli's stream-json output as the fixtures orbit's Kiro runtime is tested against.

A developer tool, run by hand with ``uv run python scripts/capture_kiro_fixtures.py`` on a
machine where ``kiro-cli`` is installed and logged in; the checks never run it. It serves
orbit's real MCP host on localhost with a test token, writes a temporary Kiro agent config that
reaches it, runs ``kiro-cli`` a few ways and saves what it printed, anonymised, under
``tests/fixtures/kiro/``. Each run spends a few Kiro credits.
"""

import asyncio
import json
import os
import re
import secrets
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orbit.agent.application.ports import CallHandler, Reply
from orbit.agent.domain.roles import COMPLETE_TASK, GET_TASK, REPORT_PROGRESS
from orbit.agent.external.mcp_host import McpHost

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "kiro"
EXIT_CODES = FIXTURES / "exit-codes.json"
AGENT = "orbit-capture"
AGENT_CONFIG = Path.home() / ".kiro" / "agents" / f"{AGENT}.json"
SERVER = "orbit"
RUN_SECONDS = 300

HOME = "/home/user"
TOKEN = "TOKEN"
REQUEST = "REQUEST"
SESSION = "00000000-0000-0000-0000-{:012d}"
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
REQUEST_ID = re.compile(rf"(?i)(request[_-]?id\W{{1,4}}){UUID}")

PROMPT = (
    "You are an orbit agent. Orbit hands out tasks and verifies the work through the tools of"
    " its MCP server. Call get_task first, then do exactly what it says."
)
TASK = (
    "Capture this run for orbit's fixtures: call report_progress with the note `capturing`,"
    " then call complete_task with the summary `done`. Do nothing else."
)
REPLIES = {
    GET_TASK: TASK,
    REPORT_PROGRESS: "Noted.",
    COMPLETE_TASK: "Verified: the work is done.",
}

# An unreachable proxy cuts Kiro off from its service, while orbit's host on localhost stays
# reachable, so the run starts and its first turn fails.
OUTAGE = {
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "HTTP_PROXY": "http://127.0.0.1:9",
    "NO_PROXY": "127.0.0.1,localhost",
}


@dataclass(frozen=True)
class Run:
    """A ``kiro-cli`` run to capture: the fixture's name, the first message, the environment it
    adds and the extra ``kiro-cli`` arguments it passes."""

    name: str
    message: str
    env: Mapping[str, str] = field(default_factory=dict)
    extra: tuple[str, ...] = ()


RUNS = (
    Run("plain", "Say ok"),
    Run("orbit-tools", "Fetch your task and do it"),
    Run("outage", "Fetch your task and do it", OUTAGE),
    Run(
        "tools",
        "List the files in the current directory and tell me the first line of README.md",
    ),
    Run("bad-model", "Say ok", extra=("--model", "no-such-model")),
)


@dataclass
class Calls:
    """The calls orbit's host answered, as ``tool(arguments)`` lines, and those it refused."""

    answered: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)


def make_handler(token: str, calls: Calls) -> CallHandler:
    """A call handler that answers the capture's task for ``token`` and refuses anyone else,
    noting each call in ``calls``."""

    async def handle(caller: str, tool: str, arguments: dict[str, Any]) -> Reply:
        call = f"{tool}({json.dumps(arguments)})"
        if caller != token or tool not in REPLIES:
            calls.refused.append(call)
            return Reply("Refused: unknown token or tool.", is_error=True)
        calls.answered.append(call)
        return Reply(REPLIES[tool])

    return handle


def agent_config(url: str, token: str) -> dict[str, Any]:
    """The Kiro agent config that reaches orbit's host at ``url`` with ``token``, its tools
    and Kiro's built-in ``read``, all pre-trusted."""
    return {
        "name": AGENT,
        "description": "Captures Kiro's output for orbit's fixtures.",
        "prompt": PROMPT,
        "mcpServers": {
            SERVER: {"url": url, "headers": {"Authorization": f"Bearer {token}"}},
        },
        "includeMcpJson": False,
        "tools": [f"@{SERVER}", "read"],
        "allowedTools": [f"@{SERVER}", "read"],
    }


def command(message: str, extra: tuple[str, ...] = ()) -> list[str]:
    """The ``kiro-cli`` command that sends ``message`` to the capture agent, with the ``extra``
    arguments."""
    return [
        "kiro-cli",
        "chat",
        "--output-format",
        "stream-json",
        "--agent",
        AGENT,
        "--agent-engine",
        "v2",
        "--require-mcp-startup",
        *extra,
        message,
    ]


async def run_kiro(run: Run, cwd: Path) -> tuple[str, int]:
    """What ``kiro-cli`` printed on stdout for ``run`` in ``cwd``, and its exit code; its
    stderr goes to ours. Stops it if it outlasts ``RUN_SECONDS`` or this is cancelled."""
    process = await asyncio.create_subprocess_exec(
        *command(run.message, run.extra),
        cwd=cwd,
        env={**os.environ, **run.env},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), RUN_SECONDS)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode is not None
    return stdout.decode(), process.returncode


def session_ids(value: Any) -> Iterator[str]:
    """The ``sessionId`` values anywhere in the JSON ``value``, in order."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "sessionId" and isinstance(item, str):
                yield item
            else:
                yield from session_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from session_ids(item)


def parsed(line: str) -> Any:
    """The JSON value on ``line``, or None if it isn't JSON."""
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def anonymised(output: str, token: str, home: str) -> str:
    """``output``, JSON lines and any other lines kept as they are, with ``home`` as
    ``/home/user``, session ids numbered from ``…0001``, ``token`` as ``TOKEN``
    and request ids as ``REQUEST``."""
    text = output.replace(home, HOME).replace(token, TOKEN)
    events = [event for line in text.splitlines() if (event := parsed(line)) is not None]
    numbered: dict[str, str] = {}
    for session in session_ids(events):
        numbered.setdefault(session, SESSION.format(len(numbered) + 1))
    for session, number in numbered.items():
        text = text.replace(session, number)
    return REQUEST_ID.sub(rf"\g<1>{REQUEST}", text)


async def capture(token: str, url: str, calls: Calls) -> dict[str, int]:
    """Run each of ``RUNS`` against orbit's host at ``url``, saving its anonymised output;
    returns their exit codes by fixture file name."""
    AGENT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    AGENT_CONFIG.write_text(json.dumps(agent_config(url, token), indent=2) + "\n")
    FIXTURES.mkdir(parents=True, exist_ok=True)
    exit_codes: dict[str, int] = {}
    for run in RUNS:
        print(f"Capturing {run.name}…", file=sys.stderr)
        before = len(calls.answered)
        output, exit_code = await run_kiro(run, ROOT)
        fixture = f"{run.name}.jsonl"
        (FIXTURES / fixture).write_text(anonymised(output, token, str(Path.home())))
        exit_codes[fixture] = exit_code
        print(f"  exit {exit_code}; orbit answered {calls.answered[before:]}", file=sys.stderr)
    return exit_codes


async def main() -> None:
    """Capture the fixtures, removing the agent config and stopping the host however it
    ends."""
    token = secrets.token_urlsafe(32)
    calls = Calls()
    host = McpHost()
    try:
        url = await host.start(make_handler(token, calls))
        exit_codes = await capture(token, url, calls)
        EXIT_CODES.write_text(json.dumps(exit_codes, indent=2) + "\n")
    finally:
        AGENT_CONFIG.unlink(missing_ok=True)
        await host.stop()
    if calls.refused:
        print(f"Orbit refused: {calls.refused}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
