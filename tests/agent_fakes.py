"""Fakes for running tasks through agents: a host and scripted agents stand in for MCP and the
CLI, and a step runner and workspace for verification."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any

from orbit.agent import Agents, AgentSession, Verification, VerificationStep
from orbit.agent.application.ports import AgentLaunch, CallHandler, Merge, Reply, UsageSource
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.roles import REVIEW_ROLES, Setup, built_in_roles
from orbit.protocol import (
    BUILT_IN_ROLE_NAMES,
    AddTask,
    AgentExited,
    AgentReply,
    AssistantMessage,
    BusyChanged,
    BusyLane,
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
    Usage,
    VerificationChanged,
)

ROLES = built_in_roles({name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES})
"""The built-in roles, with stand-in instructions."""

URL = "http://orbit.test/mcp"
Call = Callable[..., Awaitable[Reply]]
Script = Callable[[Call], Awaitable[TurnEndReason]]


class FakeHost:
    def __init__(self) -> None:
        self.handler: CallHandler | None = None
        self.starts = 0
        self.stops = 0

    async def start(self, handler: CallHandler) -> str:
        self.starts += 1
        self.handler = handler
        return URL

    async def stop(self) -> None:
        self.stops += 1

    async def call(self, token: str, tool: str, **arguments: Any) -> Reply:
        assert self.handler is not None
        return await self.handler(token, tool, arguments)


class ScriptedLauncher:
    """Each launch plays the next script as the agent, calling orbit through the host."""

    def __init__(self, host: FakeHost, *scripts: Script, reviewers: Sequence[Script] = ()) -> None:
        self.host = host
        self.scripts = list(scripts)
        self.reviewers = list(reviewers)
        self.launches: list[AgentLaunch] = []
        self.planner_turns: list[Script] = []  # Played by converse(), one per turn.
        self.planner_replies: list[str] = []  # What the planner says, a turn at a time.
        # The usage of each launch's turns, one launch at a time; one unmetered turn if none.
        self.usages: list[tuple[Usage, ...]] = []

    async def run_agent(self, launch: AgentLaunch) -> AsyncIterator[Event]:
        self.launches.append(launch)
        agent = launch.agent
        reviewing = agent.role in REVIEW_ROLES
        script = (self.reviewers if reviewing else self.scripts).pop(0)
        yield TurnStarted(turn_id=agent.id, agent_id=agent.id)
        yield AssistantMessage(turn_id=agent.id, text="on it", agent_id=agent.id)

        async def call(tool: str, **arguments: Any) -> Reply:
            return await self.host.call(agent.token, tool, **arguments)

        reason = await script(call)
        for usage in self.usages.pop(0) if self.usages else (None,):
            yield TurnFinished(turn_id=agent.id, reason=reason, agent_id=agent.id, usage=usage)

    async def converse(self, turn_id: str, launch: AgentLaunch) -> AsyncIterator[Event]:
        self.launches.append(launch)
        agent = launch.agent
        yield TurnStarted(turn_id=turn_id, agent_id=agent.id)
        if self.planner_replies:
            text = self.planner_replies.pop(0)
            yield AssistantMessage(turn_id=turn_id, text=text, agent_id=agent.id)

        async def call(tool: str, **arguments: Any) -> Reply:
            return await self.host.call(agent.token, tool, **arguments)

        reason = await self.planner_turns.pop(0)(call)
        yield TurnFinished(turn_id=turn_id, reason=reason, agent_id=agent.id)


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


class FakeTree:
    """A coder tree that records what it's asked to do and merges as scripted."""

    path = "/work/coder"
    branch = "orbit/coder"
    base = "base"  # The base branch's commit, apart from the tree's own heads.

    def __init__(
        self, *merges: Merge, leftovers: tuple[str, ...] = (), broken: str = "", empty: bool = False
    ) -> None:
        self.empty = empty  # Tasks change nothing, so there's nothing to commit.
        self.agent_commits = 0  # Commits agents made themselves.
        self.untracked_sets: list[frozenset[str]] = []  # Scripted answers to untracked().
        self.ignored: list[set[str]] = []
        self.merges = list(merges)
        self.notes = list(leftovers)
        self.broken = broken
        self.prepared: list[bool] = []
        self.commits: list[str] = []
        self.snapshots: list[str] = []
        self.dropped: list[str] = []
        self.setups: list[Setup | None] = []  # Each setup it was told to use, in order.
        self.setup: Setup | None = None
        self.failures: dict[str, Exception] = {}  # Method name -> what its next call raises.

    async def prepare(self, keep: bool) -> str:
        self.prepared.append(keep)
        self._fail("prepare")
        if self.broken:
            raise RuntimeError(self.broken)
        return self.path

    async def commit(self, message: str) -> bool:
        if self.empty:
            return False
        self.commits.append(message)
        return True

    async def integrate(self) -> Merge:
        return self.merges.pop(0) if self.merges else Merge("merged")

    async def head(self) -> str:
        if not self.prepared:  # Like git in a worktree that doesn't exist yet.
            raise RuntimeError("git rev-parse failed: the coder's tree doesn't exist")
        return f"c{len(self.commits) + self.agent_commits}"

    async def base_commit(self) -> str:
        self._fail("base_commit")
        return self.base

    async def show(self, commits: Sequence[str]) -> str:
        self._fail("show")
        return "\n".join(f"diff of {commit}" for commit in commits)

    async def snapshot(self, commit: str) -> str:
        self._fail("snapshot")
        self.snapshots.append(commit)
        return f"/work/review-{commit}"

    async def drop(self, path: str) -> None:
        self.dropped.append(path)

    async def untracked(self) -> frozenset[str]:
        return self.untracked_sets.pop(0) if self.untracked_sets else frozenset()

    async def ignore(self, paths: Collection[str]) -> None:
        self.ignored.append(set(paths))

    async def leftovers(self) -> list[str]:
        return self.notes

    def use_setup(self, setup: Setup | None) -> None:
        self.setups.append(setup)
        self.setup = setup

    def _fail(self, method: str) -> None:
        """Raise the failure scripted for ``method``, if any; it fails once."""
        failure = self.failures.pop(method, None)
        if failure is not None:
            raise failure


def verified(
    steps: Steps, workspace: FakeWorkspace | None = None, fallback_fixes: int = 3
) -> Verification:
    return Verification((STEP,), steps, workspace, fallback_fixes)


def session_for(
    *scripts: Script,
    verification: Verification | None = None,
    reviewers: Sequence[Script] = (),
    check_at_startup: bool = False,
    usage: UsageSource | None = None,
    **options: Any,
) -> tuple[AgentSession, FakeHost, ScriptedLauncher]:
    """A session whose tasks run through agents playing ``scripts``, one per launch, and
    whose reviews run through agents playing ``reviewers``, with usage from ``usage``.

    ``options`` go to :class:`Agents`, such as ``limits``, ``tree`` or ``roles`` (:data:`ROLES`
    unless given).
    """
    host = FakeHost()
    launcher = ScriptedLauncher(host, *scripts, reviewers=reviewers)
    tokens = iter(f"token-{n}" for n in range(1, 100))
    options = {"limits": Limits(), "exit_grace": 0.5, "roles": ROLES, **options}
    agents = Agents(host, launcher, lambda: next(tokens), **options)
    session = AgentSession(
        verification=verification,
        agents=agents,
        check_at_startup=check_at_startup,
        usage=usage,
    )
    return session, host, launcher


async def run(session: AgentSession, *tasks: TaskSpec) -> list[Event]:
    """Queue ``tasks``, run them, and return the events until the session goes idle."""
    for task in tasks or (TaskSpec(prompt="Add a flag.", settings=Settings(model="opus")),):
        await session.send(AddTask(task=task))
    await session.send(RunTasks())
    return await events_until(session, idle(), skip=(SettingsChanged, VerificationChanged))


@dataclass(frozen=True)
class Stop:
    """A test of the event to stop at, named by ``description``."""

    description: str
    matches: Callable[[Event], bool]

    def __call__(self, event: Event) -> bool:
        return self.matches(event)

    def __repr__(self) -> str:
        return self.description


def idle(lane: BusyLane | None = "work") -> Stop:
    """Stops when ``lane`` goes idle, or any lane if ``lane`` is None."""
    return Stop(
        f"idle(lane={lane!r})",
        lambda e: isinstance(e, BusyChanged) and not e.busy and lane in (None, e.lane),
    )


def agent_exited(agent_id: str) -> Stop:
    """Stops when agent ``agent_id`` exits."""
    return Stop(
        f"agent_exited({agent_id!r})",
        lambda e: isinstance(e, AgentExited) and e.agent_id == agent_id,
    )


def of_type(kind: type) -> Stop:
    """Stops at the first event of type ``kind``."""
    return Stop(f"of_type({kind.__name__})", lambda e: isinstance(e, kind))


async def events_until(
    session: AgentSession,
    done: Callable[[Event], bool],
    *,
    skip: tuple[type, ...] = (),
    timeout: float = 5,
) -> list[Event]:
    """The session's events up to and including the first that's ``done``, leaving out those
    of the ``skip`` types; fails if none comes within ``timeout`` seconds."""
    events: list[Event] = []

    async def collect() -> None:
        async for event in session.events():
            if isinstance(event, skip):
                continue
            events.append(event)
            if done(event):
                return

    try:
        await asyncio.wait_for(collect(), timeout)
    except TimeoutError:
        raise AssertionError(f"no event matched {done!r} within {timeout}s") from None
    return events


def board(events: list[Event]) -> tuple[TaskState, ...]:
    return [e for e in events if isinstance(e, TasksChanged)][-1].tasks


def replies(events: list[Event]) -> list[str]:
    return [e.text for e in events if isinstance(e, AgentReply)]
