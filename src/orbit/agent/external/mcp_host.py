"""Orbit's MCP server: where agents' CLIs connect to fetch tasks and report back.

It serves streamable HTTP on a random port on 127.0.0.1, inside the app's event loop. Each
agent sends ``Authorization: Bearer <token>``; every tool call goes to the handler with that
token, which decides who is asking.
"""

import asyncio
import socket
from collections.abc import Mapping
from typing import Any, NotRequired, TypedDict

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from orbit.agent.application.ports import CallHandler
from orbit.agent.domain.roles import (
    COMPLETE_TASK,
    GET_TASK,
    GIVE_UP,
    PROPOSE_CHECKS,
    PROPOSE_TASK,
    REPORT_PROGRESS,
    RUN_CHECK,
    SUGGEST_TASK,
)

INSTRUCTIONS = (
    "Orbit hands out tasks and verifies the work; these tools are how an agent talks to it."
)

# What a prompt handed to a fresh coder must cover; the tools that take one say so.
PROMPT_RECIPE = (
    "The coder starts fresh with only the prompt, so write it in four parts: Where (the files"
    " and functions), Change (what to do), Keep (what must stay as it is) and Done (the"
    " behaviours tests must pin down)."
)
SUGGEST_TASK_DESCRIPTION = (
    "Suggest a follow-up task for a coder, queued after the current work: a short title, and a"
    f" prompt. {PROMPT_RECIPE}"
)
PROPOSE_TASK_DESCRIPTION = (
    "Propose a task for the plan: a short title, and a prompt. It's added as a draft the user"
    f" accepts or drops. {PROMPT_RECIPE}"
)


class ProposedStep(TypedDict):
    """A verification step: its name, and the command (program and arguments) it runs."""

    name: str
    command: list[str]


class ProposedSetup(TypedDict):
    """The command that readies a fresh checkout, and the files whose changes rerun it."""

    command: list[str]
    watch: NotRequired[list[str]]


def bearer_token(headers: Mapping[str, str] | None) -> str:
    """The token in a ``Bearer`` authorization header; empty when there isn't one."""
    scheme, _, token = (headers or {}).get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def build_server(handler: CallHandler) -> MCPServer:
    """An MCP server whose tools pass each call, with the caller's token, to ``handler``."""
    server = MCPServer("orbit", instructions=INSTRUCTIONS)

    async def call(ctx: Context, tool: str, arguments: dict[str, Any]) -> str:
        reply = await handler(bearer_token(ctx.headers), tool, arguments)
        if reply.is_error:
            raise ToolError(reply.text)
        return reply.text

    @server.tool(structured_output=False)
    async def get_task(ctx: Context) -> str:
        """Get the task you must do. Call this first."""
        return await call(ctx, GET_TASK, {})

    @server.tool(structured_output=False)
    async def complete_task(summary: str, ctx: Context) -> str:
        """Report that the task is finished, with a one-line summary of what you did.

        Orbit then verifies your work, which can take minutes: wait for the reply. It
        says whether verification passed or what to fix.
        """
        return await call(ctx, COMPLETE_TASK, {"summary": summary})

    @server.tool(structured_output=False)
    async def run_check(step: str, ctx: Context) -> str:
        """Run one of orbit's verification steps by name and get its output."""
        return await call(ctx, RUN_CHECK, {"step": step})

    @server.tool(structured_output=False)
    async def report_progress(note: str, ctx: Context) -> str:
        """Tell orbit's user, in a few words, what you're working on."""
        return await call(ctx, REPORT_PROGRESS, {"note": note})

    @server.tool(structured_output=False)
    async def give_up(reason: str, ctx: Context) -> str:
        """Report that you can't do the task, and why: it contradicts itself or the code, or
        needs a decision only the user can make. Use only when it's truly blocked."""
        return await call(ctx, GIVE_UP, {"reason": reason})

    @server.tool(structured_output=False, description=SUGGEST_TASK_DESCRIPTION)
    async def suggest_task(title: str, prompt: str, ctx: Context) -> str:
        return await call(ctx, SUGGEST_TASK, {"title": title, "prompt": prompt})

    @server.tool(structured_output=False, description=PROPOSE_TASK_DESCRIPTION)
    async def propose_task(title: str, prompt: str, ctx: Context) -> str:
        return await call(ctx, PROPOSE_TASK, {"title": title, "prompt": prompt})

    @server.tool(structured_output=False)
    async def propose_checks(
        steps: list[ProposedStep], ctx: Context, setup: ProposedSetup | None = None
    ) -> str:
        """Propose the project's verification steps, quick ones first, and optionally the
        setup command that readies a fresh checkout with the files that should rerun it.

        Orbit runs the setup and each step, which can take minutes, then the user decides:
        wait for the reply. It says what failed, what the user wants changed, or that
        they accepted or ended it.
        """
        return await call(ctx, PROPOSE_CHECKS, {"steps": steps, "setup": setup})

    return server


class McpHost:
    """Serves orbit's MCP tools to agents until stopped."""

    def __init__(self) -> None:
        self._server: uvicorn.Server | None = None
        self._serving: asyncio.Task[None] | None = None

    async def start(self, handler: CallHandler) -> str:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        app = build_server(handler).streamable_http_app()
        self._server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
        self._serving = asyncio.create_task(self._server.serve(sockets=[sock]))
        while not self._server.started:
            if self._serving.done():
                sock.close()
                raise RuntimeError("Orbit's MCP server failed to start.") from (
                    self._serving.exception()
                )
            await asyncio.sleep(0.01)
        return f"http://127.0.0.1:{port}/mcp"

    async def stop(self) -> None:
        if self._server is None or self._serving is None:
            return
        self._server.should_exit = True
        await self._serving
