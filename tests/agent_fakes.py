"""Fakes for running tasks through agents: a host and scripted agents stand in for MCP and the
CLI, and a step runner and workspace for verification."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from orbit.agent import Agents, AgentSession, TurnRunner, Verification, VerificationStep
from orbit.agent.application.ports import AgentLaunch, CallHandler, Reply
from orbit.agent.domain.agent_task import Limits
from orbit.protocol import (
    AddTask,
    AgentReply,
    AssistantMessage,
    BusyChanged,
    Event,
    RunTasks,
    Settings,
    SettingsChanged,
    TasksChanged,
    TaskSpec,
    TaskState,
    TurnEndReason,
    TurnFinished,
    TurnStarted,
    VerificationChanged,
)

URL = "http://orbit.test/mcp"
Call = Callable[..., Awaitable[Reply]]
Script = Callable[[Call], Awaitable[TurnEndReason]]


class FakeHost:
    def __init__(self) -> None:
        self.handler: CallHandler | None = None
        self.starts = 0

    async def start(self, handler: CallHandler) -> str:
        self.starts += 1
        self.handler = handler
        return URL

    async def call(self, token: str, tool: str, **arguments: Any) -> Reply:
        assert self.handler is not None
        return await self.handler(token, tool, arguments)


class ScriptedLauncher:
    """Each launch plays the next script as the agent, calling orbit through the host."""

    def __init__(self, host: FakeHost, *scripts: Script) -> None:
        self.host = host
        self.scripts = list(scripts)
        self.launches: list[AgentLaunch] = []

    async def run_agent(self, launch: AgentLaunch) -> AsyncIterator[Event]:
        self.launches.append(launch)
        agent = launch.agent
        script = self.scripts.pop(0)
        yield TurnStarted(turn_id=agent.id, agent_id=agent.id)
        yield AssistantMessage(turn_id=agent.id, text="on it", agent_id=agent.id)

        async def call(tool: str, **arguments: Any) -> Reply:
            return await self.host.call(agent.token, tool, **arguments)

        reason = await script(call)
        yield TurnFinished(turn_id=agent.id, reason=reason, agent_id=agent.id)


class Steps:
    """A step runner whose step passes or fails by a script of results, recording runs."""

    def __init__(self, *results: bool) -> None:
        self.results = list(results)
        self.runs: list[str] = []
        self.roots: list[str] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, step: VerificationStep, root: str) -> tuple[bool, str]:
        self.runs.append(step.name)
        self.roots.append(root)
        if self.gate is not None:
            await self.gate.wait()
        passed = self.results.pop(0)
        return passed, "all good" if passed else "1 test failed"


STEP = VerificationStep(name="Unit tests", command=("pytest",))


class FakeWorkspace:
    """Reports ``states`` in turn as the working tree's state."""

    def __init__(self, *states: object) -> None:
        self.states = list(states)

    async def __call__(self, root: str) -> object:
        return self.states.pop(0)


def verified(
    steps: Steps, workspace: FakeWorkspace | None = None, max_fix_attempts: int = 3
) -> Verification:
    return Verification((STEP,), steps, workspace, max_fix_attempts)


def session_for(
    *scripts: Script,
    verification: Verification | None = None,
    limits: Limits | None = None,
    runner: TurnRunner | None = None,
) -> tuple[AgentSession, FakeHost, ScriptedLauncher]:
    """A session whose tasks run through agents playing ``scripts``, one per launch."""
    host = FakeHost()
    launcher = ScriptedLauncher(host, *scripts)
    tokens = iter(f"token-{n}" for n in range(1, 100))
    agents = Agents(host, launcher, lambda: next(tokens), limits or Limits(), exit_grace=0.5)
    chat = runner or object()  # Tasks never use the conversation's runner.
    session = AgentSession(chat, verification=verification, agents=agents)  # type: ignore[arg-type]
    return session, host, launcher


async def run(session: AgentSession, *tasks: TaskSpec) -> list[Event]:
    """Queue ``tasks``, run them, and return the events until the session goes idle."""
    for task in tasks or (TaskSpec(prompt="Add a flag.", settings=Settings(model="opus")),):
        await session.send(AddTask(task=task))
    await session.send(RunTasks())
    events: list[Event] = []
    async for event in session.events():
        if isinstance(event, SettingsChanged | VerificationChanged):
            continue
        events.append(event)
        if event == BusyChanged(busy=False):
            return events
    raise AssertionError("unreachable")


def board(events: list[Event]) -> tuple[TaskState, ...]:
    return [e for e in events if isinstance(e, TasksChanged)][-1].tasks


def replies(events: list[Event]) -> list[str]:
    return [e.text for e in events if isinstance(e, AgentReply)]
