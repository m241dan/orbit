"""The desk every lane shares: it launches agents, routes their calls by token, and owns
the agent registry and the host they reach orbit through.

Whatever answers an agent's calls, a task's :class:`AgentRun` or the planner's conversation, is
a :class:`CallTarget` opened on the desk; :func:`dispatch` answers a call with the tools the
agent's role allows.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from orbit.agent.application.limit_pause import LimitPause
from orbit.agent.application.ports import (
    AgentHost,
    AgentLaunch,
    AgentLauncher,
    CoderTree,
    ConfigWriter,
    PlannerRoleReader,
    ProposalReview,
    Publish,
    Reply,
)
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.agents import Agent, AgentRegistry
from orbit.agent.domain.reviews import ReviewLimits
from orbit.agent.domain.roles import ORBIT_TOOLS, Role
from orbit.protocol import (
    INTERNAL,
    AgentExited,
    AgentReply,
    AgentRequest,
    AgentStarted,
    ErrorOccurred,
    Event,
    OpenRequest,
    ProgressNoted,
    RunningAgent,
    Settings,
)

# The refusal of a call from an agent that isn't running.
UNKNOWN_AGENT = Reply("Unknown or expired agent token.", is_error=True)

# The reply published for a call whose handling was cancelled, so it doesn't look unanswered.
STOPPED_HANDLING = Reply("Orbit stopped handling this call.", is_error=True)

# Longest progress note shown; agents can be chatty.
NOTE_LIMIT = 200


def progress_note(text: str) -> str:
    """``text`` as a progress note: stripped, and cut short at :data:`NOTE_LIMIT`."""
    note = text.strip()
    if len(note) > NOTE_LIMIT:
        note = note[: NOTE_LIMIT - 1] + "…"
    return note


Tool = Callable[[dict[str, Any]], Awaitable[Reply]]
"""Answers a call to one of orbit's tools, given its arguments."""


async def dispatch(
    role_name: str,
    role: Role,
    tools: Mapping[str, Tool],
    tool: str,
    arguments: dict[str, Any],
) -> Reply:
    """Answer a call to ``tool`` from ``tools``, refusing a tool orbit lacks, or one ``role``
    (named ``role_name``) may not use or ``tools`` doesn't answer."""
    if tool not in ORBIT_TOOLS:
        return Reply(f"Orbit has no tool {tool!r}.", is_error=True)
    answer = tools.get(tool)
    if answer is None or tool not in role.tools:
        return Reply(f"Your role ({role_name}) can't use {tool}.", is_error=True)
    return await answer(arguments)


async def report_progress(
    publish: Publish, agent_id: str, task_id: str, arguments: dict[str, Any]
) -> Reply:
    """Publish agent ``agent_id``'s progress note on task ``task_id`` from ``arguments``."""
    note = progress_note(str(arguments.get("note", "")))
    await publish(ProgressNoted(agent_id=agent_id, task_id=task_id, note=note))
    return Reply("Noted.")


@dataclass(frozen=True)
class Agents:
    """How tasks are run through agents.

    ``host`` is where agents reach orbit, ``launcher`` starts them, and ``new_token``
    makes the unguessable tokens they identify themselves with. ``roles`` are the roles tasks
    can name. ``exit_grace`` is how long, in seconds, an agent may take to exit once its task
    is finished before it's stopped. ``tree`` is where they work and their work is merged
    from; without one they work in the current directory and nothing is committed.
    With ``review`` (and a tree), landed work is reviewed, and the whole codebase super
    reviewed, within ``review_limits``. ``proposal_review`` is where the user decides on
    proposed checks; without it, proposals are refused. ``write_config`` saves the checks the
    user accepts into the project's config; without it, they're used but not saved.
    ``planner_role`` reads the planner's role from the project's config again when the planner
    is reset; without it, the planner keeps its role. ``root`` is the workspace's directory,
    where agents work without a tree, the planner always. ``pause`` times a run that Claude's
    usage limit stopped, until it starts again.
    """

    host: AgentHost
    launcher: AgentLauncher
    new_token: Callable[[], str]
    roles: Mapping[str, Role]
    limits: Limits = field(default_factory=Limits)
    exit_grace: float = 10.0
    tree: CoderTree | None = None
    review: bool = False
    review_limits: ReviewLimits = field(default_factory=ReviewLimits)
    proposal_review: ProposalReview | None = None
    write_config: ConfigWriter | None = None
    planner_role: PlannerRoleReader | None = None
    root: str = "."
    pause: LimitPause = field(default_factory=LimitPause)


class CallTarget(Protocol):
    """Whatever answers an agent's calls: its run, or the planner's conversation."""

    async def handle(self, tool: str, arguments: dict[str, Any]) -> Reply: ...


class AgentDesk:
    """Answers agents' calls by passing them to their runs, publishing each call and reply."""

    def __init__(self, agents: Agents, publish: Publish) -> None:
        self.agents = agents
        self.registry = AgentRegistry(agents.new_token)
        self._publish = publish
        # Takes a review task's suggestions: its id, then the suggestion's title and prompt.
        self.suggest: Callable[[str, str, str], Awaitable[Reply]] | None = None
        self._runs: dict[str, CallTarget] = {}
        # The agents whose start was published and whose exit wasn't, so a snapshot shows what
        # the events have told the frontend.
        self._announced: set[str] = set()
        # The calls not yet answered, by request id, with the agent that made each.
        self._requests: dict[str, tuple[str, OpenRequest]] = {}
        self._starting: asyncio.Task[str] | None = None  # The host's start, shared by callers.

    async def publish(self, event: Event) -> None:
        """Publish ``event``, noting the agents it announces as started or exited."""
        if isinstance(event, AgentExited):
            self._announced.discard(event.agent_id)
        await self._publish(event)
        if isinstance(event, AgentStarted):
            self._announced.add(event.agent_id)

    async def url(self) -> str:
        """Where agents reach orbit; starts the host the first time, or after a failed start."""
        if self._starting is None:
            self._starting = asyncio.ensure_future(self.agents.host.start(self.handle))
            self._starting.add_done_callback(self._started)
        # Shielded: one caller giving up doesn't cancel the start the others wait on.
        return await asyncio.shield(self._starting)

    def _started(self, starting: "asyncio.Task[str]") -> None:
        """Forget ``starting`` if it failed, so the next call starts the host again."""
        failed = starting.cancelled() or starting.exception() is not None
        if failed and self._starting is starting:
            self._starting = None

    async def stop(self) -> None:
        """Stop the host, if it was started, cancelling a start that's under way; a host whose
        start failed is left alone."""
        starting, self._starting = self._starting, None
        if starting is None:
            return
        starting.cancel()
        await asyncio.wait({starting})
        # A start that failed left nothing to stop, though the desk may not have forgotten it yet.
        if starting.cancelled() or starting.exception() is None:
            await self.agents.host.stop()  # Also cleans up after a start cut short.

    async def launch(self, agent: Agent, prompt: str, settings: Settings, cwd: str) -> AgentLaunch:
        """How to start ``agent`` in its role: told ``prompt``, with ``settings``, in ``cwd``."""
        role = self.agents.roles[agent.role]
        url = await self.url()
        return AgentLaunch(agent, url, prompt, settings, cwd, role.instructions, role.tools)

    def use_role(self, name: str, role: Role) -> None:
        """Have ``role`` as the role ``name`` from now on."""
        self.agents = replace(self.agents, roles={**self.agents.roles, name: role})

    def open(self, run: CallTarget, role: str, task_id: str) -> Agent:
        agent = self.registry.register(role, task_id)
        self._runs[agent.id] = run
        return agent

    def find(self, agent_id: str) -> Agent | None:
        """Running agent ``agent_id``, if there is one."""
        return next((agent for agent in self.registry.live if agent.id == agent_id), None)

    @property
    def running(self) -> tuple[RunningAgent, ...]:
        """The agents running whose start was published and exit wasn't, oldest first, each
        with its calls not yet answered."""
        return tuple(
            RunningAgent(
                agent_id=agent.id,
                role=agent.role,
                task_id=agent.task_id,
                open_requests=self._open_requests(agent.id),
            )
            for agent in self.registry.live
            if agent.id in self._announced
        )

    def _open_requests(self, agent_id: str) -> tuple[OpenRequest, ...]:
        """Agent ``agent_id``'s calls not yet answered, oldest first."""
        return tuple(request for of, request in self._requests.values() if of == agent_id)

    def working_on(self, task_id: str) -> Agent:
        """The running agent working on task ``task_id``; there must be one."""
        return next(agent for agent in self.registry.live if agent.task_id == task_id)

    def close(self, agent: Agent) -> None:
        self.registry.revoke(agent.id)
        self._runs.pop(agent.id, None)
        self._announced.discard(agent.id)

    async def handle(self, token: str, tool: str, arguments: dict[str, Any]) -> Reply:
        agent = self.registry.by_token(token)
        if agent is None:
            return UNKNOWN_AGENT
        request_id = uuid.uuid4().hex
        await self.publish(
            AgentRequest(agent_id=agent.id, request_id=request_id, tool=tool, arguments=arguments)
        )
        # Open only once its request is published, so a snapshot can't show it twice.
        self._requests[request_id] = (agent.id, OpenRequest(request_id=request_id, tool=tool))
        reply = STOPPED_HANDLING  # Unless it's answered: a cancelled call is still replied to.
        try:
            reply = await self._request(agent, tool, arguments)
        finally:
            del self._requests[request_id]
            # Publishing never suspends, so a cancelled call's reply doesn't swallow the cancel.
            await self.publish(
                AgentReply(
                    agent_id=agent.id,
                    request_id=request_id,
                    text=reply.text,
                    is_error=reply.is_error,
                )
            )
        return reply

    async def _request(self, agent: Agent, tool: str, arguments: dict[str, Any]) -> Reply:
        """The answer to ``agent``'s call, once published."""
        run = self._runs.get(agent.id)  # Its run may have closed it during the publish.
        return UNKNOWN_AGENT if run is None else await self._answer(agent, run, tool, arguments)

    async def _answer(
        self, agent: Agent, run: CallTarget, tool: str, arguments: dict[str, Any]
    ) -> Reply:
        """``run``'s answer to ``agent``'s call; if handling it fails, the failure is reported
        and is the answer."""
        try:
            return await run.handle(tool, arguments)
        except Exception as exc:
            message = f"Orbit failed to handle {tool}: {exc}"
            await self.publish(ErrorOccurred(agent_id=agent.id, code=INTERNAL, message=message))
            return Reply(message, is_error=True)
