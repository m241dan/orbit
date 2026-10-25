"""The desk every lane shares: it launches agents, routes their calls by token, and owns
the agent registry and the host they reach orbit through.

Whatever answers an agent's calls, a task's :class:`AgentRun` or the planner's conversation, is
a :class:`CallTarget` opened on the desk; :func:`dispatch` answers a call with the tools the
agent's role allows.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from orbit.agent.application.agent_trace import REQUEST_ERROR, AgentProcess, AgentTracing
from orbit.agent.application.failures import unexpected
from orbit.agent.application.limit_pause import LimitPause
from orbit.agent.application.ports import (
    AgentHost,
    AgentLaunch,
    AgentLauncher,
    CoderTree,
    ConfigWriter,
    FileNames,
    PlannerRoleReader,
    ProposalReview,
    Publish,
    Reply,
    TempDirs,
)
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.agents import Agent, AgentRegistry
from orbit.agent.domain.refusals import failed_to
from orbit.agent.domain.reviews import ReviewLimits
from orbit.agent.domain.roles import ORBIT_TOOLS, Role
from orbit.agent.domain.runtimes import ModelAliases, resolve_model
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
from orbit.tracing import NoTelemetry, Span, Telemetry, current_span, no_span

# The refusal of a call from an agent that isn't running.
UNKNOWN_AGENT = Reply("Unknown or expired agent token.", is_error=True)

# The reply published for a call whose handling was cancelled, so it doesn't look unanswered.
STOPPED_HANDLING = Reply("Orbit stopped handling this call.", is_error=True)

_NO_TELEMETRY = NoTelemetry()

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

    ``host`` is where agents reach orbit, ``launchers`` start them, one for each runtime the
    roles run on, by its name, and ``new_token`` makes the unguessable tokens they identify
    themselves with. ``roles`` are the roles tasks can name, those named in ``personal`` defined
    whole in the user config. ``exit_grace`` is how long, in seconds, an agent may take to exit
    once its task is finished before it's stopped. ``tree`` is where they work and their work is
    merged from; without one they work in ``root`` and nothing is committed.
    With ``review`` (and a tree), landed work is reviewed, and the whole codebase super
    reviewed, within ``review_limits``. ``proposal_review`` is where the user decides on
    proposed checks; without it, proposals are refused. ``write_config`` saves the checks the
    user accepts into the project's config; without it, they're used but not saved.
    ``planner_role`` reads the planner's role from the project's config again when the planner
    is reset; without it, the planner keeps its role. ``file_names`` lists the files in a
    directory, for moving a record a task names whose number another record took; without it,
    records keep the numbers tasks give them. ``root`` is the workspace's directory, the user's
    checkout, where agents work without a tree; ``temp_dirs`` makes the empty directories
    agents whose role works in none work in. ``pause`` times a run that a runtime's usage limit
    or outage stopped, until it starts again. ``models`` are the models each runtime takes, by
    its name, which agents' models resolve through; a runtime without them is given models as
    they are.
    """

    host: AgentHost
    launchers: Mapping[str, AgentLauncher]
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
    file_names: FileNames | None = None
    root: str = field(kw_only=True)
    temp_dirs: TempDirs = field(kw_only=True)
    pause: LimitPause = field(default_factory=LimitPause)
    personal: frozenset[str] = field(default=frozenset(), kw_only=True)
    models: Mapping[str, ModelAliases] = field(default_factory=dict, kw_only=True)

    def runtime(self, role: str) -> str | None:
        """The runtime role ``role``'s agents run on; none for a role they don't have."""
        found = self.roles.get(role)
        return None if found is None else found.runtime

    def launcher(self, role: str) -> AgentLauncher:
        """What starts role ``role``'s agents: the launcher of the runtime it runs on. It must
        be one of the roles, on a runtime there's a launcher for."""
        return self.launchers[self.roles[role].runtime]

    def model_aliases(self, role: str) -> ModelAliases | None:
        """The models role ``role``'s agents may be given, those its runtime takes; none for a
        role they don't have, or a runtime without them."""
        runtime = self.runtime(role)
        return None if runtime is None else self.models.get(runtime)

    def launch_model(self, role: str, model: str | None) -> str | None:
        """The id role ``role``'s agents are given for ``model``: the one it resolves to among
        the models their runtime takes, or ``model`` as it is if there are none; none for none.

        Raises :class:`~orbit.agent.domain.runtimes.UnknownModel`, listing the aliases, if they
        don't resolve it.
        """
        models = self.model_aliases(role)
        if model is None or models is None:
            return model
        return resolve_model(models, model)

    @property
    def usage_unknown(self) -> frozenset[str]:
        """The names of the roles whose agents' usage is unknown: the launcher of the runtime
        each runs on says it doesn't report what their turns cost."""
        return frozenset(
            name
            for name, role in self.roles.items()
            if role.runtime in self.launchers
            and not self.launchers[role.runtime].capabilities.usage
        )


class CallTarget(Protocol):
    """Whatever answers an agent's calls: its run, or the planner's conversation."""

    async def handle(self, tool: str, arguments: dict[str, Any]) -> Reply: ...


class AgentDesk:
    """Answers agents' calls by passing them to their runs, publishing each call and reply.
    It launches agents for the workspace ``workspace_id``.

    Each call is traced with ``telemetry`` under the span of its agent's process, while that's
    traced (see :meth:`traced`), and otherwise under the span current."""

    def __init__(
        self,
        agents: Agents,
        publish: Publish,
        telemetry: Telemetry = _NO_TELEMETRY,
        workspace_id: str = "",
    ) -> None:
        self.agents = agents
        self._workspace_id = workspace_id
        self.registry = AgentRegistry(agents.new_token)
        self._publish = publish
        self._tracing = AgentTracing(telemetry)
        self._processes: dict[str, Span] = {}  # The spans of the agents' processes, by agent id.
        # Takes a review task's suggestions: its id, then the suggestion's title and prompt.
        self.suggest: Callable[[str, str, str], Awaitable[Reply]] | None = None
        self._runs: dict[str, CallTarget] = {}
        # The agents whose start was published and whose exit wasn't, so a snapshot shows what
        # the events have told the frontend.
        self._announced: set[str] = set()
        # The calls not yet answered, by request id, with the agent that made each.
        self._requests: dict[str, tuple[str, OpenRequest]] = {}
        self._starting: asyncio.Task[str] | None = None  # The host's start, shared by callers.
        # How each agent running was last launched, by its id.
        self._launches: dict[str, AgentLaunch] = {}

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
            # Detached: the host serves every later agent, not the task whose launch started it.
            with no_span():
                self._starting = asyncio.ensure_future(self.agents.host.start(self.handle))
            self._starting.add_done_callback(self._started)
        # Shielded: one caller giving up doesn't cancel the start the others wait on.
        return await asyncio.shield(self._starting)

    def _started(self, starting: asyncio.Task[str]) -> None:
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
        """How to start ``agent`` in its role: told ``prompt``, with ``settings`` (or its role's
        effort, if they set none), their model resolved to the id its runtime is given, in
        ``cwd``.

        Raises :class:`~orbit.agent.domain.runtimes.UnknownModel` if their model doesn't
        resolve, as one saved before its alias was taken out of the user config doesn't.
        """
        role = self.agents.roles[agent.role]
        model = self.agents.launch_model(agent.role, settings.model)
        url = await self.url()
        own = role.launch_settings(settings).model_copy(update={"model": model})
        launch = AgentLaunch(
            agent, url, prompt, own, cwd, role.instructions, role.tools, self._workspace_id
        )
        self._launches[agent.id] = launch
        return launch

    def launched(self, agent_id: str | None) -> AgentLaunch | None:
        """How agent ``agent_id``, if it's running, was last launched; none before it was."""
        return None if agent_id is None else self._launches.get(agent_id)

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
        self._launches.pop(agent.id, None)
        self._announced.discard(agent.id)

    @contextmanager
    def traced(self, agent: Agent, parent: Span) -> Iterator[AgentProcess]:
        """``agent``'s process, traced over the block in a span that's a child of ``parent``, as
        :meth:`AgentTracing.process` traces it, with the agent's calls traced under it
        meanwhile."""
        runtime = self.agents.roles[agent.role].runtime
        with self._tracing.process(agent, runtime, parent) as process:
            self._processes[agent.id] = process.span
            try:
                yield process
            finally:
                del self._processes[agent.id]

    async def handle(self, token: str, tool: str, arguments: dict[str, Any]) -> Reply:
        agent = self.registry.by_token(token)
        if agent is None:
            return UNKNOWN_AGENT
        process = self._processes.get(agent.id, current_span())
        with self._tracing.request(process, tool) as request:
            return await self._handle(agent, tool, arguments, request)

    async def _handle(
        self, agent: Agent, tool: str, arguments: dict[str, Any], request: Span
    ) -> Reply:
        """The answer to ``agent``'s call to ``tool``, publishing the call and the reply and
        recording on ``request``, the call's span, whether the reply is an error."""
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
            request.set_attributes({REQUEST_ERROR: reply.is_error})
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
            message = unexpected(failed_to(f"handle {tool}", exc), exc)
            await self.publish(ErrorOccurred(agent_id=agent.id, code=INTERNAL, message=message))
            return Reply(message, is_error=True)
