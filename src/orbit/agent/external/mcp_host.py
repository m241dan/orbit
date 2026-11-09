"""Orbit's MCP server: where agents' CLIs connect to fetch tasks and report back.

It serves streamable HTTP on a random port on 127.0.0.1, inside the app's event loop. Each
agent sends ``Authorization: Bearer <token>``; every tool call goes to the handler with that
token, which decides who is asking.

A middleware, ``MCPServer``'s provisional ``middleware=`` hook, makes each agent's ``tools/list``
its own: orbit's tools its role lacks are dropped, and the proxied tools of the MCP servers its
role allows are added, as the lister says. A call to a proxied name goes to the handler as any
call does, and its reply comes back as the tool's result; every other call reaches orbit's tools.
"""

import asyncio
import socket
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, NotRequired, TypedDict

import uvicorn
from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ListToolsResult, TextContent
from mcp.types import Tool as McpTool

from orbit.agent.application.ports import CallHandler, Reply, ToolLister, ToolListing
from orbit.agent.domain.mcp_proxy import ProxiedTool, is_proxied
from orbit.agent.domain.tools import (
    COMPLETE_TASK,
    CONSULT,
    DISMISS_FINDING,
    EDIT_DRAFT,
    GET_TASK,
    GIVE_UP,
    LIST_DRAFTS,
    LIST_REPORTS,
    MOVE_DRAFT,
    ORBIT_TOOLS,
    PROPOSE_CHECKS,
    PROPOSE_TASK,
    READ_DRAFT,
    READ_REPORT,
    READ_SKILL,
    REMOVE_DRAFT,
    REPORT_FINDING,
    REPORT_PROGRESS,
    RUN_CHECK,
    SAVE_REPORT,
    SUGGEST_TASK,
)
from orbit.agent.external.embedded_server import EmbeddedServer
from orbit.protocol import bearer_token

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
    " prompt. A merge pass names in findings the ids of the findings the task covers, at least"
    ' one, the first filed and the rest merged into it, and gives its severity, "blocking" or'
    f' "normal" (the default); any other review leaves both out. {PROMPT_RECIPE}'
)
REPORT_FINDING_DESCRIPTION = (
    "Report a finding of your review: a short title, a body saying where it is and why it"
    ' matters, and its severity, "blocking" or "normal". Orbit replies with its id.'
)
DISMISS_FINDING_DESCRIPTION = (
    "Dismiss one of your review's undecided findings, by id, such as F2, saying why it needs no"
    " task."
)
PROPOSE_TASK_DESCRIPTION = (
    "Propose a task for the plan: a short title, and a prompt. It's added as a draft the user"
    " accepts or drops. Name in skills what the task's coder needs beyond its role's own: the"
    " language and kind of the code it changes, and the project's skills that bear on it."
    f" {PROMPT_RECIPE}"
)
EDIT_DRAFT_DESCRIPTION = (
    "Replace the title and prompt of one of your drafts; it stays a draft in its place."
    f" {PROMPT_RECIPE}"
)

READ_SKILL_DESCRIPTION = (
    "Read a file of one of your skills, by the skill's name and the file's path in it, such as"
    " testing.md."
)


class ProposedStep(TypedDict):
    """A verification step: its name, the command (program and arguments) it runs, and the
    named set it's in; in none, it's one of the task checks every task runs."""

    name: str
    command: list[str]
    set: NotRequired[str]


class ProposedTrigger(TypedDict):
    """A trigger that runs checks: the event it fires on, the checks it runs ("task-checks" or
    a named set), how many landings it fires after on "commits", whether the user can fire it,
    under what label, and whether it's enabled."""

    on: str
    checks: str
    every: NotRequired[int]
    manual: NotRequired[bool]
    label: NotRequired[str]
    enabled: NotRequired[bool]


class ProposedReport(TypedDict):
    """A report routing a named set's runs to a lens role: the set, the role (one granted
    report_finding) whose task it queues, whether it takes only failed runs ("failed", the
    default) or "any", and the task's prompt, which may use {set}, {status}, {commit} and
    {output}."""

    set: str
    role: str
    status: NotRequired[Literal["failed", "any"]]
    prompt: str


class ProposedSetup(TypedDict):
    """The command that readies a fresh checkout, and the files whose changes rerun it."""

    command: list[str]
    watch: NotRequired[list[str]]


ToolCall = Callable[[Context, str, dict[str, Any]], Awaitable[str]]
"""How a tool passes a call on: with its context, the tool's name and its arguments."""


ARGUMENTS_NOT_AN_OBJECT = Reply("A tool's arguments are an object.", is_error=True)


async def list_every_tool(token: str) -> ToolListing:
    """Every one of orbit's tools and no proxied ones, whoever asks."""
    return ToolListing(ORBIT_TOOLS)


def build_server(handler: CallHandler, lister: ToolLister = list_every_tool) -> MCPServer:
    """An MCP server whose tools pass each call, with the caller's token, to ``handler``, and
    whose ``tools/list`` shows each caller the tools ``lister`` gives for its token."""
    server = MCPServer(
        "orbit", instructions=INSTRUCTIONS, middleware=[_build_proxy(handler, lister)]
    )

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

    @server.tool(structured_output=False, description=PROPOSE_TASK_DESCRIPTION)
    async def propose_task(
        title: str, prompt: str, ctx: Context, skills: list[str] | None = None
    ) -> str:
        return await call(ctx, PROPOSE_TASK, {"title": title, "prompt": prompt, "skills": skills})

    @server.tool(structured_output=False)
    async def consult(role: str, message: str, ctx: Context) -> str:
        """Ask another conversational role, by name, something only it can find out, such as by
        running code: the message's first line is its title, and the rest the details.

        It returns at once; carry on meanwhile. The role's reply arrives later as a message.
        """
        return await call(ctx, CONSULT, {"role": role, "message": message})

    @server.tool(structured_output=False)
    async def save_report(title: str, body: str, ctx: Context) -> str:
        """Save a finding that will matter later as a report for this workspace: a short title,
        and a Markdown body of at most 200 KB. Returns the report's id."""
        return await call(ctx, SAVE_REPORT, {"title": title, "body": body})

    @server.tool(structured_output=False)
    async def list_reports(ctx: Context) -> str:
        """List this workspace's saved reports, newest first: each one's id, title, author and
        date."""
        return await call(ctx, LIST_REPORTS, {})

    @server.tool(structured_output=False)
    async def read_report(id: int, ctx: Context) -> str:
        """Read a saved report, by its id from list_reports: its title and body."""
        return await call(ctx, READ_REPORT, {"id": id})

    @server.tool(structured_output=False, description=READ_SKILL_DESCRIPTION)
    async def read_skill(skill: str, file: str, ctx: Context) -> str:
        return await call(ctx, READ_SKILL, {"skill": skill, "file": file})

    _add_review_tools(server, call)
    _add_checks_tool(server, call)
    _add_draft_tools(server, call)
    return server


def _add_review_tools(server: MCPServer, call: ToolCall) -> None:
    """Give ``server`` the tools reviews suggest tasks, report findings and dismiss them with,
    passed to ``call``."""

    @server.tool(structured_output=False, description=SUGGEST_TASK_DESCRIPTION)
    async def suggest_task(
        title: str,
        prompt: str,
        ctx: Context,
        findings: list[str] | None = None,
        severity: Literal["blocking", "normal"] = "normal",
    ) -> str:
        arguments = {
            "title": title,
            "prompt": prompt,
            "findings": findings or [],
            "severity": severity,
        }
        return await call(ctx, SUGGEST_TASK, arguments)

    @server.tool(structured_output=False, description=REPORT_FINDING_DESCRIPTION)
    async def report_finding(
        title: str, body: str, severity: Literal["blocking", "normal"], ctx: Context
    ) -> str:
        arguments = {"title": title, "body": body, "severity": severity}
        return await call(ctx, REPORT_FINDING, arguments)

    @server.tool(structured_output=False, description=DISMISS_FINDING_DESCRIPTION)
    async def dismiss_finding(finding: str, reason: str, ctx: Context) -> str:
        return await call(ctx, DISMISS_FINDING, {"finding": finding, "reason": reason})


def _add_checks_tool(server: MCPServer, call: ToolCall) -> None:
    """Give ``server`` the tool a role proposes checks with, passed to ``call``."""

    @server.tool(structured_output=False)
    async def propose_checks(
        # #lizard forgives(parameter_count): the SDK takes each of the tool's arguments as one.
        steps: list[ProposedStep],
        ctx: Context,
        setup: ProposedSetup | None = None,
        sets: dict[str, bool] | None = None,
        triggers: dict[str, ProposedTrigger] | None = None,
        reports: dict[str, ProposedReport] | None = None,
    ) -> str:
        """Propose the project's verification steps, quick ones first, and optionally the
        setup command that readies a fresh checkout with the files that should rerun it.
        Steps in no set are the task checks every task runs; a slower step joins a named set.
        Optionally say, in sets, whether each set holds merges when it fails (by name),
        propose the triggers that run sets (by name), and propose reports (by name) that hand
        a set's output to a lens role when it finishes.

        The user sees the proposal first; if they accept it, orbit runs the setup and each
        step, which can take minutes: wait for the reply. It says what the user wants
        changed, that they ended it, what failed, or that it passed and was accepted.
        """
        given = {"sets": sets, "triggers": triggers, "reports": reports}
        extras = {key: value for key, value in given.items() if value is not None}
        return await call(ctx, PROPOSE_CHECKS, {"steps": steps, "setup": setup, **extras})


def _add_draft_tools(server: MCPServer, call: ToolCall) -> None:
    """Give ``server`` the tools a role uses on its drafts, each passed to ``call``."""

    @server.tool(structured_output=False)
    async def list_drafts(ctx: Context) -> str:
        """List your drafts the user hasn't accepted yet: each one's id and title, in list
        order."""
        return await call(ctx, LIST_DRAFTS, {})

    @server.tool(structured_output=False)
    async def read_draft(task_id: str, ctx: Context) -> str:
        """Read one of your drafts as it stands now: its title and full prompt."""
        return await call(ctx, READ_DRAFT, {"task_id": task_id})

    @server.tool(structured_output=False, description=EDIT_DRAFT_DESCRIPTION)
    async def edit_draft(task_id: str, title: str, prompt: str, ctx: Context) -> str:
        return await call(ctx, EDIT_DRAFT, {"task_id": task_id, "title": title, "prompt": prompt})

    @server.tool(structured_output=False)
    async def remove_draft(task_id: str, ctx: Context) -> str:
        """Withdraw one of your drafts from the task list."""
        return await call(ctx, REMOVE_DRAFT, {"task_id": task_id})

    @server.tool(structured_output=False)
    async def move_draft(task_id: str, ctx: Context, before: str | None = None) -> str:
        """Move one of your drafts right before another of your drafts, or to the end of the
        task list when before is left out."""
        return await call(ctx, MOVE_DRAFT, {"task_id": task_id, "before": before})


def _build_proxy(handler: CallHandler, lister: ToolLister) -> ServerMiddleware[Any]:
    """The middleware making each caller's ``tools/list`` what ``lister`` gives for its token,
    and passing its calls to proxied names to ``handler``."""

    async def proxy(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        token = bearer_token(getattr(ctx.request, "headers", None))
        params = ctx.params or {}
        if ctx.method == "tools/list":
            return _list_tools_reply(await call_next(ctx), await lister(token))
        if ctx.method == "tools/call" and is_proxied(str(params.get("name", ""))):
            reply = await _proxy_call(handler, token, params)
            return CallToolResult(
                content=[TextContent(type="text", text=reply.text)], is_error=reply.is_error
            )
        return await call_next(ctx)

    return proxy


def _list_tools_reply(result: HandlerResult, listing: ToolListing) -> dict[str, Any]:
    """The ``tools/list`` ``result`` with only the orbit tools ``listing`` names, and then its
    proxied tools."""
    listed = ListToolsResult.model_validate(result).model_dump(by_alias=True, exclude_unset=True)
    orbit = [tool for tool in listed["tools"] if tool["name"] in listing.orbit]
    return {**listed, "tools": [*orbit, *map(_describe_tool, listing.proxied)]}


def _describe_tool(tool: ProxiedTool) -> dict[str, Any]:
    """``tool`` as ``tools/list`` gives it: by its proxied name, with the server's description
    and input schema."""
    entry = McpTool(
        name=tool.name, description=tool.tool.description, input_schema=dict(tool.tool.input_schema)
    )
    return entry.model_dump(by_alias=True, exclude_none=True)


async def _proxy_call(handler: CallHandler, token: str, params: Mapping[str, Any]) -> Reply:
    """``handler``'s answer to the call ``params`` give, from the agent with ``token``, refused
    if its arguments aren't an object."""
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return ARGUMENTS_NOT_AN_OBJECT
    return await handler(token, str(params["name"]), arguments)


class McpHost:
    """Serves orbit's MCP tools to agents until stopped."""

    def __init__(self) -> None:
        self._server: uvicorn.Server | None = None
        self._serving: asyncio.Task[None] | None = None

    async def start(self, handler: CallHandler, lister: ToolLister = list_every_tool) -> str:
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            app = build_server(handler, lister).streamable_http_app()
            server = EmbeddedServer(uvicorn.Config(app, log_level="warning", lifespan="on"))
        except BaseException:
            sock.close()
            raise
        self._serving = await server.start_serving(sock, "Orbit's MCP server failed to start.")
        self._server = server
        return f"http://127.0.0.1:{port}/mcp"

    async def stop(self) -> None:
        if self._server is None or self._serving is None:
            return
        self._server.should_exit = True
        await self._serving
