"""A tiny MCP server over streamable HTTP, standing in for the external servers orbit reaches at a
URL, served by uvicorn on a loopback port.

It answers each JSON-RPC request with JSON, offering its tools two to a page, the stdio
stand-in's (``tests/mcp_stand_in.py``) unless given others: ``get_issue`` and ``get issue!``
(an issue's text), ``whoami`` (the headers of the request, as JSON), ``oops`` (the tool's own
error), ``slow`` (answers once released, or the server stops), ``fail`` (answers with HTTP 500)
and ``scan`` (a scan's findings). With a token, a request without it as its bearer token is
answered with HTTP 401, and one with another token with HTTP 403. With ``drop_initialize``, it
answers ``initialize`` with an event stream that ends without an answer.
"""

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from orbit.agent.domain.mcp_proxy import UpstreamTool
from orbit.agent.external.embedded_server import EmbeddedServer
from tests.mcp_fakes import STAND_IN_TOOLS

PROTOCOL_VERSION = "2025-06-18"
PAGE_SIZE = 2
SESSION_ID = "stand-in-session"

type Handler = Callable[[dict[str, Any], Request], Awaitable[Response]]
"""Answers the call with the arguments given, made by the request given."""


def text(value: str, error: bool = False) -> dict[str, Any]:
    """A tool's result holding ``value`` as text, an error if ``error``."""
    return {"content": [{"type": "text", "text": value}], "isError": error}


def entry(tool: UpstreamTool) -> dict[str, Any]:
    """``tool`` as ``tools/list`` gives it."""
    described = {} if tool.description is None else {"description": tool.description}
    return {"name": tool.name, **described, "inputSchema": dict(tool.input_schema)}


class HttpStandIn:
    """The stand-in, offering ``tools``, requiring ``token`` as the bearer token if given. Notes
    the ``headers`` of every request it's sent, names lower-cased; ``drop_initialize`` may be set
    to drop ``initialize``."""

    def __init__(self, tools: tuple[UpstreamTool, ...] = STAND_IN_TOOLS, token: str | None = None):
        self.tools = [entry(tool) for tool in tools]
        self.token = token
        self.drop_initialize = False
        self.headers: list[dict[str, str]] = []
        self.port = 0
        self._release = asyncio.Event()
        self._server: EmbeddedServer | None = None
        self._serving: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        """Where it serves MCP."""
        return f"http://127.0.0.1:{self.port}/mcp"

    @property
    def serving(self) -> bool:
        """Whether it's serving."""
        return self._serving is not None and not self._serving.done()

    async def start(self) -> None:
        """Serve on its port, a free one the first time."""
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port))
        self.port = sock.getsockname()[1]
        self._release = asyncio.Event()
        config = uvicorn.Config(
            self.app,
            interface="asgi3",
            log_level="warning",
            lifespan="off",
            timeout_graceful_shutdown=1,
        )
        server = self._server = EmbeddedServer(config)
        self._serving = await server.start_serving(sock, "The stand-in failed to start.")

    async def stop(self) -> None:
        """Answer the slow calls waiting, and stop serving."""
        assert self._server is not None and self._serving is not None
        self._release.set()
        self._server.should_exit = True
        await asyncio.wait_for(self._serving, 10)

    async def app(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer the HTTP request ``scope`` gives."""
        request = Request(scope, receive)
        response = await self._answer(request)
        await response(scope, receive, send)

    async def _answer(self, request: Request) -> Response:
        """The response to ``request``."""
        self.headers.append(dict(request.headers))
        refused = self._refusal(request)
        if refused is not None:
            return Response(status_code=refused)
        if request.method != "POST":
            return Response(status_code=405 if request.method == "GET" else 200)
        message = await request.json()
        if "id" not in message:
            return Response(status_code=202)
        return await self._reply(message, request)

    def _refusal(self, request: Request) -> int | None:
        """The status ``request`` is refused with for its bearer token; ``None`` if it isn't."""
        authorization = request.headers.get("authorization")
        if self.token is None or authorization == f"Bearer {self.token}":
            return None
        return 401 if authorization is None else 403

    async def _reply(self, message: dict[str, Any], request: Request) -> Response:
        """The response to the JSON-RPC request ``message``, sent in ``request``."""
        params = message.get("params") or {}
        method = message["method"]
        if method == "tools/call":
            return await self._called(message["id"], params, request)
        if method == "initialize" and self.drop_initialize:
            return Response("", media_type="text/event-stream")
        result = self._result(method, params)
        if result is None:
            error = {"code": -32602, "message": f"no such method {method}"}
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "error": error})
        return self._result_response(message["id"], result)

    def _result(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """The result of the request ``method`` with ``params``; none if there's no such
        method."""
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stand-in", "version": "1"},
            }
        if method == "tools/list":
            return self._listed(params)
        return {} if method == "ping" else None

    def _listed(self, params: dict[str, Any]) -> dict[str, Any]:
        """The page of tools ``params``' cursor asks for."""
        start = int(params.get("cursor") or 0)
        following = start + PAGE_SIZE
        more = {"nextCursor": str(following)} if following < len(self.tools) else {}
        return {"tools": self.tools[start:following], **more}

    async def _called(self, id_: object, params: dict[str, Any], request: Request) -> Response:
        """The response to the call ``params`` give, request ``id_``, sent in ``request``."""
        name = params["name"]
        arguments = params.get("arguments") or {}
        if name == "fail":
            return Response("it broke", status_code=500)
        if name == "slow":
            await self._release.wait()
            return self._result_response(id_, text("slow, at last"))
        if name == "whoami":
            return self._result_response(id_, text(json.dumps({"headers": dict(request.headers)})))
        return self._result_response(id_, _answer(name, arguments))

    def _result_response(self, id_: object, result: dict[str, Any]) -> Response:
        """The response carrying ``result`` for request ``id_``."""
        body = {"jsonrpc": "2.0", "id": id_, "result": result}
        return JSONResponse(body, headers={"mcp-session-id": SESSION_ID})


def _answer(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """The result of a call to the plain tool ``name`` with ``arguments``."""
    if name in ("get_issue", "get issue!"):
        return text(f"Issue {arguments.get('number')}: it's broken.")
    if name == "oops":
        return text("it went wrong", error=True)
    if name == "scan":
        return text("Found 2 issues.")
    return text(f"no such tool {name}", error=True)


@asynccontextmanager
async def served(stand_in: HttpStandIn) -> AsyncIterator[HttpStandIn]:
    """``stand_in``, serving until the block ends, or stopped then if it's serving."""
    await stand_in.start()
    try:
        yield stand_in
    finally:
        if stand_in.serving:
            await stand_in.stop()
