"""Running a task through an agent: start it, answer its calls, verify its work, stop it.

The agent is a separate process that reaches orbit through the :class:`AgentHost`. Its calls
arrive at the :class:`AgentDesk`, which passes each to the :class:`AgentRun` for that agent.
Verification runs in the run's own task, not in a call's, so it carries on if the agent
disconnects while waiting for the result.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from orbit.agent.application.ports import (
    AgentHost,
    AgentLaunch,
    AgentLauncher,
    CoderTree,
    Reply,
)
from orbit.agent.application.verifier import Failure, Verifier
from orbit.agent.domain.agent_task import FINISHED, AgentTask, Limits, Phase
from orbit.agent.domain.agents import Agent, AgentRegistry, bootstrap_prompt
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.roles import BUILT_IN_ROLES, Role
from orbit.agent.domain.verification import trim_output
from orbit.protocol import (
    AgentExited,
    AgentExitReason,
    AgentReply,
    AgentRequest,
    AgentStarted,
    ErrorOccurred,
    Event,
    ProgressNoted,
    TaskGaveUp,
    TasksChanged,
    TaskStatus,
    TurnFinished,
)

Publish = Callable[[Event], Awaitable[None]]
# Longest progress note shown; agents can be chatty.
NOTE_LIMIT = 200

Wake = Literal["exited", "gave_up"]
"""Why the run loop wakes up, other than a claim that the task is done."""

STATUSES: dict[Phase, TaskStatus] = {
    "assigned": "error",
    "working": "error",
    "verifying": "error",
    "done": "completed",
    "fallback": "unverified",
    "gave_up": "gave_up",
}


@dataclass(frozen=True)
class Agents:
    """How tasks are run through agents.

    ``host`` is where agents reach orbit, ``launcher`` starts them, and ``new_token``
    makes the unguessable tokens they identify themselves with. ``exit_grace`` is how
    long, in seconds, an agent may take to exit once its task is finished before it's stopped.
    ``tree`` is where they work and their work is merged from; without one they work in the
    current directory and nothing is committed. ``roles`` are the roles tasks can name.
    With ``review`` (and a tree), landed work is reviewed; ``review_rounds`` and
    ``review_suggestions`` bound what reviews suggest, and the whole codebase is super
    reviewed every ``super_review_every`` commits, suggesting up to
    ``super_review_suggestions`` tasks.
    """

    host: AgentHost
    launcher: AgentLauncher
    new_token: Callable[[], str]
    limits: Limits = field(default_factory=Limits)
    exit_grace: float = 10.0
    tree: CoderTree | None = None
    roles: Mapping[str, Role] = field(default_factory=lambda: dict(BUILT_IN_ROLES))
    review: bool = False
    review_rounds: int = 3
    review_suggestions: int = 5
    super_review_every: int = 10
    super_review_suggestions: int = 10


@dataclass(frozen=True)
class Outcome:
    """How a task ended: its status, the failure to hand a fresh agent if it fell back, and
    the agent's summaries of what it did, one per claim."""

    status: TaskStatus
    failure: Failure | None = None
    summaries: tuple[str, ...] = ()


def _answer(claim: "asyncio.Future[Reply] | None", reply: Reply) -> None:
    """Answer ``claim`` with ``reply``, unless there's no claim or it was answered already."""
    if claim is not None and not claim.done():
        claim.set_result(reply)


class CallTarget(Protocol):
    """Whatever answers an agent's calls: its run, or the planner's conversation."""

    async def handle(self, tool: str, arguments: dict[str, Any]) -> Reply: ...


class AgentDesk:
    """Answers agents' calls by passing them to their runs, publishing each call and reply."""

    def __init__(self, agents: Agents, publish: Publish) -> None:
        self.agents = agents
        self.registry = AgentRegistry(agents.new_token)
        self.publish = publish
        # Takes a review task's suggestions: its id, then the suggestion's title and prompt.
        self.suggest: Callable[[str, str, str], Awaitable[Reply]] | None = None
        self._runs: dict[str, CallTarget] = {}
        self._url: str | None = None

    async def url(self) -> str:
        """Where agents reach orbit; starts the host the first time."""
        if self._url is None:
            self._url = await self.agents.host.start(self.handle)
        return self._url

    async def stop(self) -> None:
        """Stop the host, if it was started."""
        if self._url is not None:
            self._url = None
            await self.agents.host.stop()

    def open(self, run: CallTarget, role: str, task_id: str) -> Agent:
        agent = self.registry.register(role, task_id)
        self._runs[agent.id] = run
        return agent

    def find(self, agent_id: str) -> Agent | None:
        """Running agent ``agent_id``, if there is one."""
        return next((agent for agent in self.registry.live if agent.id == agent_id), None)

    def close(self, agent: Agent) -> None:
        self.registry.revoke(agent.id)
        self._runs.pop(agent.id, None)

    async def handle(self, token: str, tool: str, arguments: dict[str, Any]) -> Reply:
        agent = self.registry.by_token(token)
        if agent is None:
            return Reply("Unknown or expired agent token.", is_error=True)
        request_id = uuid.uuid4().hex
        await self.publish(
            AgentRequest(agent_id=agent.id, request_id=request_id, tool=tool, arguments=arguments)
        )
        reply = await self._runs[agent.id].handle(tool, arguments)
        await self.publish(
            AgentReply(
                agent_id=agent.id, request_id=request_id, text=reply.text, is_error=reply.is_error
            )
        )
        return reply


class AgentRun:
    """One agent working on task ``task_id`` of ``board``, from launch until it exits."""

    def __init__(
        self, task_id: str, board: TaskBoard, verifier: Verifier | None, desk: AgentDesk
    ) -> None:
        self._task_id = task_id
        self._board = board
        self._verifier = verifier
        self._desk = desk
        self._publish = desk.publish
        self._work = AgentTask(board.get(task_id).task, desk.agents.limits)
        self._inbox: asyncio.Queue[asyncio.Future[Reply] | Wake] = asyncio.Queue()
        self._checking = asyncio.Lock()  # One verification step at a time.
        self._failure: Failure | None = None
        self._failed_state: object = None  # The tree when verification last failed.
        self._agent: Agent | None = None
        self._root = "."
        self._tools: dict[str, Callable[[dict[str, Any]], Awaitable[Reply]]] = {
            "get_task": self._get_task,
            "complete_task": self._complete_task,
            "run_check": self._run_check,
            "report_progress": self._report_progress,
            "give_up": self._give_up,
            "suggest_task": self._suggest_task,
        }

    @property
    def agent(self) -> Agent:
        assert self._agent is not None  # Set when the run starts.
        return self._agent

    async def run(self, before: object, root: str) -> Outcome:
        """Launch the agent in ``root`` and see the task through; ``before`` is the tree's
        state there beforehand."""
        task = self._work.task
        role = self._desk.agents.roles.get(task.role)
        if role is None:
            await self._publish(ErrorOccurred(message=f"No role named {task.role!r}."))
            return Outcome("error")
        self._role, self._root = role, root
        self._agent = self._desk.open(self, task.role, self._task_id)
        stream = await self._launch()
        try:
            await self._serve_in_time(before)
            if self._work.phase in FINISHED:
                await self._await_exit(stream)
        finally:
            await self._stop(stream)
            self._desk.close(self.agent)
        return Outcome(STATUSES[self._work.phase], self._failure, tuple(self._work.summaries))

    async def _launch(self) -> "asyncio.Task[None]":
        url = await self._desk.url()
        role = self._work.task.role
        await self._publish(AgentStarted(agent_id=self.agent.id, role=role, task_id=self._task_id))
        return asyncio.create_task(self._stream(url))

    async def _serve_in_time(self, before: object) -> None:
        """Serve the agent until it's done, or its role's time runs out."""
        minutes = self._role.timeout_minutes
        try:
            async with asyncio.timeout(None if minutes is None else minutes * 60):
                await self._serve(before)
        except TimeoutError:
            message = f"{self.agent.id} ran out of time ({minutes:g} minutes)."
            await self._publish(ErrorOccurred(agent_id=self.agent.id, message=message))

    async def handle(self, tool: str, arguments: dict[str, Any]) -> Reply:
        answer = self._tools.get(tool)
        if answer is None:
            return Reply(f"Orbit has no tool {tool!r}.", is_error=True)
        if tool not in self._role.tools:
            role = self._work.task.role
            return Reply(f"Your role ({role}) can't use {tool}.", is_error=True)
        return await answer(arguments)

    # The agent's process

    async def _stream(self, url: str) -> None:
        """Publish what the agent does until its process ends, then wake the run loop."""
        reason: AgentExitReason = "crashed"
        try:
            reason = await self._follow(url)
        except asyncio.CancelledError:
            reason = "killed"
            raise
        except Exception as exc:
            await self._publish(ErrorOccurred(agent_id=self.agent.id, message=str(exc)))
        finally:
            await self._publish(AgentExited(agent_id=self.agent.id, reason=reason))
            self._inbox.put_nowait("exited")

    async def _follow(self, url: str) -> AgentExitReason:
        reason: AgentExitReason = "finished"
        launch = AgentLaunch(
            self.agent,
            url,
            bootstrap_prompt(self.agent),
            self._work.task.settings,
            self._root,
            self._role.instructions,
            self._role.tools,
        )
        events = self._desk.agents.launcher.run_agent(launch)
        async for event in events:
            if isinstance(event, TurnFinished) and event.reason == "error":
                reason = "crashed"
            await self._publish(event)
        return reason

    async def _await_exit(self, stream: "asyncio.Task[None]") -> None:
        """Give a finished agent ``exit_grace`` seconds to exit on its own."""
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(stream), self._desk.agents.exit_grace)

    async def _stop(self, stream: "asyncio.Task[None]") -> None:
        stream.cancel()
        with suppress(asyncio.CancelledError):
            await stream

    # The run loop

    async def _serve(self, before: object) -> None:
        """Handle claims until the work is finished or the agent has gone."""
        while self._work.phase not in FINISHED:
            item = await self._inbox.get()
            if item == "exited":
                await self._exited(before)
                return
            if isinstance(item, asyncio.Future):
                await self._verify(before, item)

    async def _exited(self, before: object) -> None:
        if not self._work.exited():
            message = f"{self.agent.id} exited without calling get_task."
            await self._publish(ErrorOccurred(agent_id=self.agent.id, message=message))
        elif await self._unchanged_since_failure():
            assert self._verifier is not None and self._failure is not None
            await self._verifier.skip_unchanged(self._task_id)
            self._work.failed(*self._failure)  # The agent is gone, so this falls back.
        else:
            await self._verify(before, None)

    async def _unchanged_since_failure(self) -> bool:
        """Whether the agent stopped after a failed verification without changing anything."""
        if self._failed_state is None:
            return False
        assert self._verifier is not None  # Only a verification failure records a state.
        return await self._verifier.fingerprint(self._root) == self._failed_state

    async def _verify(self, before: object, claim: "asyncio.Future[Reply] | None") -> None:
        """Check the work, then answer ``claim`` (if the agent made one) with the verdict."""
        try:
            failure = await self._check(before)
        except asyncio.CancelledError:
            _answer(claim, Reply("The task was cancelled.", is_error=True))
            raise
        reply = self._work.passed() if failure is None else await self._failed(failure)
        _answer(claim, Reply(reply))

    async def _check(self, before: object) -> Failure | None:
        if self._verifier is None or not self._role.verify:
            return None
        async with self._checking:
            return await self._verifier.check(self._task_id, before, self._root)

    async def _failed(self, failure: Failure) -> str:
        reply = self._work.failed(*failure)
        self._failure = failure
        if self._work.phase == "working":
            assert self._verifier is not None  # Only verification fails.
            self._failed_state = await self._verifier.fingerprint(self._root)
            self._board.set_attempt(
                self._task_id, self._work.attempt, self._work.limits.max_attempts
            )
            self._board.set_status(self._task_id, "running")
            await self._publish(TasksChanged(tasks=self._board.tasks))
        return reply

    # Tools

    async def _get_task(self, arguments: dict[str, Any]) -> Reply:
        return Reply(self._work.get_task())

    async def _complete_task(self, arguments: dict[str, Any]) -> Reply:
        claim = self._work.claim_done(str(arguments.get("summary", "")))
        if not claim.verify:
            return Reply(claim.reply or "", is_error=True)
        answer: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        self._inbox.put_nowait(answer)
        # Shielded: if the agent disconnects, the claim is still verified.
        return await asyncio.shield(answer)

    async def _run_check(self, arguments: dict[str, Any]) -> Reply:
        if self._verifier is None:
            return Reply("No verification steps are configured.", is_error=True)
        steps = {step.name: step for step in self._verifier.verification.steps}
        step = steps.get(str(arguments.get("step", "")))
        if step is None:
            return Reply(f"No step by that name; the steps are: {', '.join(steps)}.", is_error=True)
        refusal = self._work.request_check()
        if refusal is not None:
            return Reply(refusal, is_error=True)
        async with self._checking:
            passed, output = await self._verifier.run_one(step, self._root)
        return Reply(f"{step.name}: {'passed' if passed else 'failed'}\n\n{trim_output(output)}")

    async def _report_progress(self, arguments: dict[str, Any]) -> Reply:
        note = str(arguments.get("note", "")).strip()
        if len(note) > NOTE_LIMIT:
            note = note[: NOTE_LIMIT - 1] + "…"
        await self._publish(ProgressNoted(agent_id=self.agent.id, task_id=self._task_id, note=note))
        return Reply("Noted.")

    async def _suggest_task(self, arguments: dict[str, Any]) -> Reply:
        if self._desk.suggest is None:
            return Reply("Orbit isn't taking suggestions.", is_error=True)
        title, prompt = str(arguments.get("title", "")), str(arguments.get("prompt", ""))
        return await self._desk.suggest(self._task_id, title, prompt)

    async def _give_up(self, arguments: dict[str, Any]) -> Reply:
        reason = str(arguments.get("reason", ""))
        reply = self._work.give_up(reason)
        if self._work.phase != "gave_up":
            return Reply(reply, is_error=True)
        await self._publish(
            TaskGaveUp(agent_id=self.agent.id, task_id=self._task_id, reason=reason)
        )
        self._inbox.put_nowait("gave_up")
        return Reply(reply)
