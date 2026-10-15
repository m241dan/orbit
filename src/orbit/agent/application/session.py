"""In-process implementation of :class:`orbit.protocol.Session`: the application's core.

It takes commands from the frontend, asks the domain what follows from them, drives the
ports to do it, and reports what happened as events.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Collection, Coroutine, Sequence
from contextlib import suppress
from dataclasses import replace
from typing import Any, assert_never

from orbit.agent.application.agent_run import AgentDesk, AgentRun, Agents, Outcome
from orbit.agent.application.landing import Lander
from orbit.agent.application.planner import PlannerChat
from orbit.agent.application.ports import ProposalReview
from orbit.agent.application.proposals import FrontendReview
from orbit.agent.application.review_lane import REVIEW_ROLES, ReviewLane
from orbit.agent.application.startup_check import AGENT_ID as STARTUP_AGENT
from orbit.agent.application.startup_check import StartupCheck
from orbit.agent.application.verifier import Verification, Verifier
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.commits import commit_message, configure_message
from orbit.agent.domain.reviews import ReviewPlanner
from orbit.agent.domain.roles import ChecksProposal, configure_prompt
from orbit.agent.domain.verification import (
    FixAttempts,
    VerificationStep,
    fix_prompt,
    startup_fix_prompt,
)
from orbit.protocol import (
    AcceptTask,
    AddTask,
    BusyChanged,
    BusyLane,
    CancelAgent,
    CancelTurn,
    Command,
    ConfigureChecks,
    DecideProposal,
    EditTask,
    ErrorOccurred,
    Event,
    Notice,
    RemoveTask,
    RequestSuperReview,
    RunTasks,
    Settings,
    SettingsChanged,
    SubmitPrompt,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskStatus,
    TurnEndReason,
    TurnFinished,
    UpdateSettings,
    VerificationChanged,
)

NO_STEPS_NOTE = "No verification steps configured. Press ctrl+e to set them up."
CONFIGURING_NOTE = "A configurator task is already queued or running."
# Statuses of a task that's yet to finish.
WAITING: frozenset[TaskStatus] = frozenset({"pending", "running", "verifying"})


def _verifier(
    verification: Verification | None,
    board: TaskBoard,
    events: "asyncio.Queue[Event]",
    agents: Agents | None,
) -> Verifier | None:
    """What checks tasks on ``board`` (in the agents' tree, if any); none without
    ``verification``."""
    if verification is None:
        return None
    return Verifier(verification, board, events.put, agents.tree if agents else None)


def _reviewed(agents: Agents | None, review: ProposalReview) -> Agents | None:
    """``agents``, with proposals going to ``review`` unless they already go somewhere."""
    if agents is None or agents.proposal_review is not None:
        return agents
    return replace(agents, proposal_review=review)


def _running(turn: "asyncio.Task[object] | None") -> bool:
    return turn is not None and not turn.done()


def _cancel(turn: "asyncio.Task[object] | None") -> bool:
    """Cancel ``turn`` if it still runs; returns whether it did."""
    if turn is None or turn.done():
        return False
    turn.cancel()
    return True


class AgentSession:
    """Runs work in the background and publishes its events.

    Prompts go to the planner, whose turns run in a lane of their own, alongside the work
    lane's task runs.

    The session owns the task list and reports it whole on every change. A run
    goes through it in order; after each task that completes, the ``verification``
    steps run until one fails. A failure adds a task to fix it right after the
    failed one. Once a fix passes, the tasks it fixed count as completed.

    Tasks with tools off aren't verified, and neither are tasks that leave the
    working tree as it was. With a ``verification`` without steps, no task is: a note
    says so at startup, and each task that would have been verified is skipped.

    With ``check_at_startup``, the steps also run once in the background as the events start,
    on the base commit; a failure adds a draft task to fix it.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        model_choices: Sequence[str] = (),
        verification: Verification | None = None,
        agents: Agents | None = None,
        workspace: str | None = None,
        check_at_startup: bool = False,
    ) -> None:
        self._settings = settings or Settings()
        self._model_choices = tuple(model_choices)
        steps = verification.steps if verification else ()
        self._board = TaskBoard([s.name for s in steps])
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._proposals = FrontendReview(self._events.put, self._agent_on)
        agents = _reviewed(agents, self._proposals)
        self._verifier = _verifier(verification, self._board, self._events, agents)
        self._desk = AgentDesk(agents, self._events.put) if agents else None
        self._announced = False
        self._lane = self._review_lane()
        self._landed: dict[str, str] = {}  # Task id -> the commit it landed, until reported.
        self._starts: dict[str, str] = {}  # Task id -> the tree's head when it started.
        self._held = False  # The user cancelled: don't start work by itself.
        self._planner = self._planner_chat()
        self._events.put_nowait(self._settings_changed())
        if workspace is not None:
            self._events.put_nowait(Notice(message=f"Workspace: {workspace}"))
        self._announce_steps()
        self._startup = self._startup_check(verification, check_at_startup)
        self._turn: asyncio.Task[object] | None = None  # The work lane's.
        self._planner_turn: asyncio.Task[object] | None = None

    def _announce_steps(self) -> None:
        """Show the steps that will verify tasks, or note that none will."""
        if self._board.steps:
            self._events.put_nowait(VerificationChanged(steps=self._board.steps))
        elif self._verifier:
            self._events.put_nowait(Notice(message=NO_STEPS_NOTE))

    def _startup_check(
        self, verification: Verification | None, wanted: bool
    ) -> StartupCheck | None:
        """The check to run at startup, if ``wanted``; none without steps to run."""
        if not wanted or verification is None or not verification.steps:
            return None
        tree = self._desk.agents.tree if self._desk else None
        return StartupCheck(verification, tree, self._events.put, self._add_startup_fix)

    async def _add_startup_fix(self, step: VerificationStep, output: str) -> None:
        """Add a draft task to fix ``step``, which failed at startup with ``output``."""
        settings = Settings()
        if self._desk is not None:
            coder = self._desk.agents.roles["coder"]
            settings = Settings(model=coder.model, tool_access=coder.tool_access)
        self._board.add_draft(TaskSpec(prompt=startup_fix_prompt(step, output), settings=settings))
        await self._publish_tasks()

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def model_choices(self) -> tuple[str, ...]:
        return self._model_choices

    @property
    def agents(self) -> Agents | None:
        return self._desk.agents if self._desk else None

    @property
    def verification(self) -> Verification | None:
        return self._verifier.verification if self._verifier else None

    @property
    def _checks(self) -> Verifier:
        assert self._verifier is not None  # Only used while verifying a task.
        return self._verifier

    async def send(self, command: Command) -> None:
        match command:
            case SubmitPrompt() | CancelTurn() | UpdateSettings():
                await self._handle_prompts(command)
            case AddTask() | RunTasks() | ConfigureChecks():
                await self._handle_tasks(command)
            case EditTask() | AcceptTask() | RemoveTask():
                await self._handle_board(command)
            case RequestSuperReview() | CancelAgent() | DecideProposal():
                await self._handle_agents(command)
            case _:
                assert_never(command)

    async def _handle_prompts(self, command: SubmitPrompt | CancelTurn | UpdateSettings) -> None:
        match command:
            case SubmitPrompt(text=text):
                await self._submit(text)
            case CancelTurn(lane="planner"):
                _cancel(self._planner_turn)
            case CancelTurn():
                self._held = True
                _cancel(self._turn)
            case UpdateSettings(settings=settings):
                self._settings = settings
                await self._events.put(self._settings_changed())
            case _:
                assert_never(command)

    async def _submit(self, text: str) -> None:
        """Start a turn of the planner's conversation, in its own lane."""
        if self._planner is None:
            message = "Planning needs agents and a planner role."
            await self._events.put(ErrorOccurred(message=message))
            # Frees the prompt of a frontend that marked the lane busy when it sent this.
            await self._events.put(BusyChanged(busy=False, lane="planner"))
        else:
            await self._start_planner(self._run_turn(self._planner, text, self._settings))

    async def _handle_tasks(self, command: AddTask | RunTasks | ConfigureChecks) -> None:
        match command:
            case AddTask(task=task):
                self._board.add(task)
                await self._publish_tasks()
            case RunTasks():
                await self._run_tasks_if_possible()
            case ConfigureChecks():
                await self._configure_checks()
            case _:
                assert_never(command)

    async def _configure_checks(self) -> None:
        """Queue a configurator first and run it, unless work is running already (then it goes
        next); refused while one is queued or running."""
        if self._desk is None:
            await self._events.put(ErrorOccurred(message="Tasks need agents, and none are set up."))
            return
        if self._configuring():
            await self._events.put(Notice(message=CONFIGURING_NOTE))
            return
        self._board.add_first(self._configurator_task(self._desk.agents))
        await self._publish_tasks()
        if not _running(self._turn):
            self._held = False
            await self._start(self._run_tasks())

    def _configuring(self) -> bool:
        """Whether a configurator task is queued or running."""
        return any(t.task.role == "configurator" and t.status in WAITING for t in self._board.tasks)

    def _configurator_task(self, agents: Agents) -> TaskSpec:
        """A configurator's task, told the steps and setup configured now."""
        steps = self.verification.steps if self.verification else ()
        prompt = configure_prompt(steps, agents.tree.setup if agents.tree else None)
        role = agents.roles["configurator"]
        settings = Settings(model=role.model, tool_access=role.tool_access)
        return TaskSpec(prompt=prompt, settings=settings, role="configurator")

    async def _handle_board(self, command: EditTask | AcceptTask | RemoveTask) -> None:
        match command:
            case EditTask(task_id=task_id, task=task):
                await self._change(self._board.edit, task_id, task)
            case AcceptTask(task_id=task_id):
                await self._change(self._board.accept, task_id)
            case RemoveTask(task_id=task_id):
                await self._change(self._board.remove, task_id)
            case _:
                assert_never(command)

    async def _run_tasks_if_possible(self) -> None:
        if self._desk is None:
            await self._events.put(ErrorOccurred(message="Tasks need agents, and none are set up."))
            return
        self._held = False
        if self._lane is not None:
            self._lane.kick()
        if self._next_work(()) is not None:
            await self._start(self._run_tasks())

    async def _handle_agents(
        self, command: RequestSuperReview | CancelAgent | DecideProposal
    ) -> None:
        if isinstance(command, CancelAgent):
            await self._cancel_agent(command.agent_id)
        elif isinstance(command, DecideProposal):
            await self._decide(command)
        else:
            await self._super_review()

    async def _decide(self, command: DecideProposal) -> None:
        if not self._proposals.decide(command):
            message = f"No proposal is waiting for task {command.task_id!r}."
            await self._events.put(ErrorOccurred(message=message))

    def _agent_on(self, task_id: str) -> str:
        """The agent working on task ``task_id``; only asked while one is."""
        assert self._desk is not None  # Only agents propose.
        return self._desk.working_on(task_id).id

    async def _cancel_agent(self, agent_id: str) -> None:
        """Stop the startup check, or a reviewer's review, on its own; any other agent's work,
        like esc."""
        if self._cancel_startup(agent_id):
            return
        agent = self._desk.find(agent_id) if self._desk else None
        if agent is None:
            await self._events.put(ErrorOccurred(message=f"No running agent {agent_id!r}."))
        else:
            self._cancel_work_of(agent)

    def _cancel_work_of(self, agent: Agent) -> None:
        """Stop a reviewer's review, or the planner's turn, on its own; else the work lane's."""
        if agent.role in REVIEW_ROLES and self._lane is not None:
            self._lane.cancel(agent.task_id)
        elif agent.role == "planner":
            _cancel(self._planner_turn)
        else:
            self._held = True
            _cancel(self._turn)

    def _cancel_startup(self, agent_id: str) -> bool:
        """Cancel the startup check if it's agent ``agent_id`` and still running; returns
        whether it was."""
        startup = self._startup
        if agent_id != STARTUP_AGENT or startup is None or not startup.running:
            return False
        startup.cancel()
        return True

    async def _super_review(self) -> None:
        if self._lane is None:
            message = "Super reviews need reviews turned on, inside a git repository."
            await self._events.put(ErrorOccurred(message=message))
        else:
            await self._lane.super_review()

    def _next_work(self, tried: Collection[str]) -> str | None:
        """The next task for the work lane; reviews have their own lane, if there is one."""
        exclude = REVIEW_ROLES if self._lane is not None else ()
        return self._board.next_runnable(skip=tried, exclude=exclude)

    async def _wake_work(self) -> None:
        """Run the work a review queued, unless something runs or the user cancelled."""
        if not (_running(self._turn) or self._held) and self._next_work(()) is not None:
            await self._start(self._run_tasks())

    def _review_lane(self) -> ReviewLane | None:
        desk = self._desk
        if desk is None or not desk.agents.review or desk.agents.tree is None:
            return None
        agents = desk.agents
        planner = ReviewPlanner(
            agents.review_rounds,
            agents.review_suggestions,
            agents.super_review_every,
            agents.super_review_suggestions,
        )
        lane = ReviewLane(desk, self._board, planner, self._wake_work)
        desk.suggest = lane.suggest
        return lane

    async def _change(self, change: Callable[..., None], *arguments: Any) -> None:
        """Change the board, publishing it, or report why the change was refused."""
        try:
            change(*arguments)
        except ValueError as exc:
            await self._events.put(ErrorOccurred(message=str(exc)))
        else:
            await self._publish_tasks()

    def _planner_chat(self) -> PlannerChat | None:
        if self._desk is None or "planner" not in self._desk.agents.roles:
            return None
        return PlannerChat(self._desk, self._board)

    async def close(self) -> None:
        """Cancel the work in progress, which stops its agent, and stop the agents' host."""
        for turn in (self._turn, self._planner_turn):
            if turn is not None and _cancel(turn):
                with suppress(asyncio.CancelledError):
                    await turn
        await self._stop_background()
        if self._planner is not None:
            self._planner.close()
        if self._desk is not None:
            await self._desk.stop()

    async def _stop_background(self) -> None:
        """Stop the startup check and the review lane."""
        if self._startup is not None:
            await self._startup.close()
        if self._lane is not None:
            await self._lane.close()

    async def events(self) -> AsyncIterator[Event]:
        if not self._announced:
            self._announced = True
            await self._announce_leftovers()
            if self._startup is not None:
                self._startup.start()
        while True:
            yield await self._events.get()

    async def _announce_leftovers(self) -> None:
        """Tell the user about work earlier runs left in the coder's tree."""
        tree = self._desk.agents.tree if self._desk else None
        for note in await tree.leftovers() if tree else []:
            await self._events.put(Notice(message=note))

    async def _start(self, work: Coroutine[Any, Any, object]) -> None:
        """Run ``work`` in the work lane, where task runs go."""
        turn = await self._launch(work, self._turn, "work", "A turn is already running.")
        if turn is not None:
            self._turn = turn

    async def _start_planner(self, work: Coroutine[Any, Any, object]) -> None:
        """Run ``work`` in the planner's lane, alongside whatever the work lane runs."""
        turn = await self._launch(
            work, self._planner_turn, "planner", "The planner is already answering."
        )
        if turn is not None:
            self._planner_turn = turn

    async def _launch(
        self,
        work: Coroutine[Any, Any, object],
        current: "asyncio.Task[object] | None",
        lane: BusyLane,
        refusal: str,
    ) -> "asyncio.Task[object] | None":
        """Start ``work`` in ``lane``, unless ``current`` still runs there (then say
        ``refusal``); returns the started work."""
        if _running(current):
            work.close()
            await self._events.put(ErrorOccurred(message=refusal))
            return None
        await self._events.put(BusyChanged(busy=True, lane=lane))
        turn = asyncio.create_task(work)
        # A callback, so it also runs if the work is cancelled before it starts.
        turn.add_done_callback(lambda done: self._idle(done, lane))
        return turn

    def _idle(self, turn: "asyncio.Task[object]", lane: BusyLane) -> None:
        # Work started since then reports its own end; staying quiet keeps it shown busy.
        if turn is (self._planner_turn if lane == "planner" else self._turn):
            self._events.put_nowait(BusyChanged(busy=False, lane=lane))

    async def _run_tasks(self) -> None:
        tried: set[str] = set()
        attempts = FixAttempts()
        while (task_id := self._next_work(tried)) is not None:
            tried.add(task_id)
            if await self._run_task(task_id, attempts) == "cancelled":
                return

    async def _run_task(self, task_id: str, attempts: FixAttempts) -> TaskStatus:
        """Run one task through a new agent; returns its final status."""
        self._board.set_status(task_id, "running")
        await self._publish_tasks()
        if self._startup is not None:
            self._startup.release_panel()
        if self._verifier:
            self._board.reset_steps()
            await self._checks.publish_steps()
        await self._events.put(TaskStarted(task_id=task_id))
        assert self._desk is not None  # Runs only start with agents.
        try:
            status = await self._run_agent(self._desk, task_id, attempts)
        except asyncio.CancelledError:
            if self._verifier:
                await self._verifier.cancel()
            status = "cancelled"
        except RuntimeError as exc:  # Git or the tree's setup failed.
            await self._events.put(ErrorOccurred(message=str(exc)))
            status = "error"
        self._board.finish(task_id, status)
        await self._publish_tasks()
        await self._report_to_review(task_id)
        return status

    async def _report_to_review(self, task_id: str) -> None:
        """Tell the review lane how task ``task_id`` ended."""
        if self._lane is None:
            return
        commit = self._landed.pop(task_id, None)
        if commit is not None:
            await self._lane.landed(task_id, commit)
        else:
            await self._lane.ended(task_id)

    async def _run_agent(self, desk: AgentDesk, task_id: str, attempts: FixAttempts) -> TaskStatus:
        """Run task ``task_id`` through a new agent and land its work; a fallback queues a fix
        for a fresh one."""
        root = await self._prepare(desk, task_id)
        task = self._board.get(task_id).task
        before = await self._verifier.state_before(task, root) if self._verifier else None
        outcome = await AgentRun(task_id, self._board, self._verifier, desk).run(before, root)
        if outcome.checks is not None:
            await self._configure(desk, outcome.checks, root)
        if outcome.status == "completed":
            outcome = await self._land(desk, task_id, outcome, root)
        if outcome.failure is not None and outcome.status == "unverified":
            await self._queue_fix(task_id, attempts, *outcome.failure)
        return outcome.status

    async def _configure(self, desk: AgentDesk, checks: ChecksProposal, root: str) -> None:
        """Save the ``checks`` the user accepted into the config at ``root``, and use them from
        the next task on. This, or a restart, is the only way the checks change."""
        if desk.agents.write_config is not None:
            desk.agents.write_config(root, checks)
        if desk.agents.tree is not None:
            desk.agents.tree.use_setup(checks.setup)
        self._checks.verification = replace(self._checks.verification, steps=checks.steps)
        self._board.set_steps([step.name for step in checks.steps])
        await self._checks.publish_steps()

    async def _land(self, desk: AgentDesk, task_id: str, outcome: Outcome, root: str) -> Outcome:
        """Commit and merge a completed task's work, when there's a tree to do it from: the
        checks the user accepted from it, or else the work itself."""
        if desk.agents.tree is None:
            return outcome
        if outcome.checks is not None:
            message = configure_message(outcome.checks)
        else:
            message = commit_message(self._board.get(task_id).task, outcome.summaries)
        lander = Lander(desk.agents.tree, self._board, self._verifier, self._events.put)
        landing = await lander.land(task_id, message, root, self._starts.pop(task_id))
        if landing.failure is not None:
            return Outcome("unverified", landing.failure)
        if landing.commit is not None:
            self._landed[task_id] = landing.commit
        return outcome

    async def _prepare(self, desk: AgentDesk, task_id: str) -> str:
        """Where task ``task_id`` is worked on: the coder's tree, readied for it."""
        if desk.agents.tree is None:
            return "."
        # A fix carries on from the work it fixes.
        tree = desk.agents.tree
        root = await tree.prepare(keep=self._board.get(task_id).fixes is not None)
        self._starts[task_id] = await tree.head()
        return root

    async def _queue_fix(
        self, task_id: str, attempts: FixAttempts, step: VerificationStep, output: str
    ) -> None:
        """Add a task fixing ``step`` right after ``task_id``, unless its attempts ran out."""
        limit = self._checks.verification.max_fix_attempts
        if attempts.exhausted(task_id, step.name, limit):
            message = f"Verification step {step.name!r} still fails after {limit} fix attempts."
            await self._events.put(ErrorOccurred(message=message))
            return
        settings = self._board.get(task_id).task.settings
        fix = TaskSpec(prompt=fix_prompt(step, output), settings=settings)
        fix_id = self._board.add(fix, fixes=task_id)
        attempts.record(fix_id, task_id, step.name)
        if self._lane is not None:
            self._lane.joined(fix_id)

    async def _publish_tasks(self) -> None:
        await self._events.put(TasksChanged(tasks=self._board.tasks))

    async def _run_turn(self, planner: PlannerChat, text: str, settings: Settings) -> TurnEndReason:
        """Run one turn of the planner's, publishing its events; returns how it ended."""
        turn_id = uuid.uuid4().hex
        reason: TurnEndReason = "completed"
        try:
            async for event in planner.run_turn(turn_id, text, settings):
                if isinstance(event, TurnFinished):
                    reason = event.reason
                await self._events.put(event)
        except asyncio.CancelledError:
            reason = "cancelled"
            await self._events.put(TurnFinished(turn_id=turn_id, reason=reason))
        except Exception as exc:
            reason = "error"
            await self._events.put(ErrorOccurred(turn_id=turn_id, message=str(exc)))
            await self._events.put(TurnFinished(turn_id=turn_id, reason=reason))
        return reason

    def _settings_changed(self) -> SettingsChanged:
        return SettingsChanged(settings=self._settings, model_choices=self._model_choices)
