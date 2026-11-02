"""Fakes of the ports MCP servers are reached through: a connector whose sessions play the
stand-in server's tools (``tests/mcp_stand_in.py``) in memory, and credentials from a table."""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from orbit.agent.application.ports import (
    CredentialsRefused,
    CredentialsUnreadable,
    Reply,
    UpstreamFailed,
)
from orbit.agent.domain.mcp_proxy import UpstreamTool
from orbit.agent.domain.mcp_servers import McpServer

OBJECT = {"type": "object", "properties": {"number": {"type": "integer"}}}
STAND_IN_TOOLS = (
    UpstreamTool("get_issue", "Get an issue.", OBJECT),
    UpstreamTool("get issue!", "Get an issue, oddly named.", OBJECT),
    UpstreamTool("whoami", None, {"type": "object"}),
    UpstreamTool("oops", None, {"type": "object"}),
    UpstreamTool("slow", None, {"type": "object"}),
    UpstreamTool("fail", None, {"type": "object"}),
)
"""The tools the stand-in server offers, in its order."""

CRASHED = "it exited with status 3"
CLOSED = "the connection to it was closed"


@dataclass(frozen=True)
class Opened:
    """A connection the fake connector made: to ``server``, started in ``cwd`` with ``env``,
    sending ``headers``."""

    server: McpServer
    cwd: str
    env: Mapping[str, str]
    headers: Mapping[str, str]


class FakeSession:
    """A connection to a server offering ``tools``, started as ``opened`` says, playing the
    stand-in's tools: ``fail`` crashes it, ``slow`` waits until :attr:`release` is set, and
    ``whoami`` answers with its directory, environment and headers; ``create_issue``, which only
    a session offering it is called with, creates the issue its ``title`` names.
    ``list_failure``, if given, fails listing, and ``list_gate``, if given, holds it until set.
    Notes the calls made on it, by the server's names, and whether it's closed."""

    def __init__(
        self,
        tools: tuple[UpstreamTool, ...],
        opened: Opened,
        list_failure: str | None = None,
        list_gate: asyncio.Event | None = None,
    ) -> None:
        self.tools = tools
        self.list_gate = list_gate
        self.opened = opened
        self.cwd = opened.cwd
        self.list_failure = list_failure
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.crashed = False
        self.waiting = asyncio.Event()  # A slow call is waiting.
        self.release = asyncio.Event()
        self.cancelled = 0  # The calls cancelled while waiting.

    async def list_tools(self) -> tuple[UpstreamTool, ...]:
        self._check()
        if self.list_gate is not None:
            await self.list_gate.wait()
        if self.list_failure is not None:
            raise UpstreamFailed(self.list_failure)
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Reply:
        self._check()
        self.calls.append((name, arguments))
        if name == "fail":
            self.crashed = True
            raise UpstreamFailed(CRASHED)
        if name == "slow":
            return await self._slow()
        return _answer(name, arguments, self.opened)

    async def close(self) -> None:
        self.closed = True

    def _check(self) -> None:
        """Raise :class:`UpstreamFailed` if it's closed or crashed."""
        if self.closed:
            raise UpstreamFailed(CLOSED)
        if self.crashed:
            raise UpstreamFailed(CRASHED)

    async def _slow(self) -> Reply:
        """Wait to be released, noting a cancellation meanwhile."""
        self.waiting.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return Reply("slow, at last")


def _answer(name: str, arguments: dict[str, Any], opened: Opened) -> Reply:
    """The stand-in's answer to a call to ``name`` with ``arguments``, started as ``opened``
    says."""
    if name in ("get_issue", "get issue!"):
        return Reply(f"Issue {arguments.get('number')}: it's broken.")
    if name == "whoami":
        seen = {"cwd": opened.cwd, "env": dict(opened.env), "headers": dict(opened.headers)}
        return Reply(json.dumps(seen))
    if name == "oops":
        return Reply("it went wrong", is_error=True)
    if name == "create_issue":
        return Reply(f"Created issue '{arguments.get('title')}'.")
    return Reply(f"no such tool {name}", is_error=True)


class FakeConnector:
    """Connects to servers in memory, each session offering ``tools``, the stand-in's unless
    given. Notes each connection made in ``opened`` and keeps its ``sessions``. While
    ``failure`` is set, connecting fails with it; while ``refused`` is, the server refuses
    orbit's credentials with that HTTP status; while ``list_failure`` is, listing fails; while
    ``crash`` is set, connecting raises it, as a bug would."""

    def __init__(self, tools: tuple[UpstreamTool, ...] = STAND_IN_TOOLS) -> None:
        self.tools = tools
        self.opened: list[Opened] = []
        self.sessions: list[FakeSession] = []
        self.failure: str | None = None
        self.refused: int | None = None
        self.list_failure: str | None = None
        self.crash: Exception | None = None
        self.gate: asyncio.Event | None = None  # Connecting waits for it, if set.
        self.list_gate: asyncio.Event | None = None  # Listing waits for it, if set.

    async def open(
        self, server: McpServer, cwd: str, env: Mapping[str, str], headers: Mapping[str, str]
    ) -> FakeSession:
        opened = Opened(server, cwd, dict(env), dict(headers))
        self.opened.append(opened)
        if self.gate is not None:
            await self.gate.wait()
        self._check()
        session = FakeSession(self.tools, opened, self.list_failure, self.list_gate)
        self.sessions.append(session)
        return session

    def _check(self) -> None:
        """Raise what connecting would, as set."""
        if self.crash is not None:
            raise self.crash
        if self.failure is not None:
            raise UpstreamFailed(self.failure)
        if self.refused is not None:
            raise CredentialsRefused(self.refused)

    @property
    def open_sessions(self) -> list[FakeSession]:
        """The sessions not yet closed, oldest first."""
        return [session for session in self.sessions if not session.closed]


class FakeCredentials:
    """Credentials holding ``values``, by variable; with ``unreadable``, they can't be read, for
    that reason."""

    def __init__(self, values: Mapping[str, str], unreadable: str | None = None) -> None:
        self.values = dict(values)
        self.unreadable = unreadable

    def __call__(self, name: str) -> str | None:
        if self.unreadable is not None:
            raise CredentialsUnreadable(self.unreadable)
        return self.values.get(name)
