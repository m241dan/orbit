"""In-process implementation of :class:`orbit.protocol.Session`: the application's core.

It takes commands from the frontend, asks the domain what follows from them, drives the
ports to do it, and reports what happened as events.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Any, assert_never

from orbit.agent.application.desk import AgentDesk, Agents
from orbit.agent.application.lanes import TurnSlot
from orbit.agent.application.planner import PlannerChat
from orbit.agent.application.ports import ProposalReview, UsageSource
from orbit.agent.application.proposals import FrontendReview
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.startup_check import AGENT_ID as STARTUP_AGENT
from orbit.agent.application.startup_check import StartupCheck
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.usage_monitor import UsageMonitor
from orbit.agent.application.verifier import Verification, Verifier
from orbit.agent.application.work_lane import WorkLane
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.board import WAITING, TaskBoard
from orbit.agent.domain.reviews import ReviewPlanner
from orbit.agent.domain.roles import REVIEW_ROLES, configure_prompt, role_task
from orbit.agent.domain.verification import VerificationStep, startup_fix_prompt
from orbit.protocol import (
    CODER,
    CONFIGURATOR,
    PLANNER,
    AcceptTask,
    AddTask,
    BusyChanged,
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
    TaskSpec,
    TurnFinished,
    UpdateSettings,
    VerificationChanged,
)

NO_STEPS_NOTE = "No verification steps configured. Press ctrl+e to set them up."
NO_AGENTS_MESSAGE = "Tasks need agents, and none are set up."
NO_VERIFY_MESSAGE = "Setting up checks needs verification on; orbit was started with --no-verify."
CONFIGURING_NOTE = "A configurator task is already queued or running."


def _verifier(
    verification: Verification | None,
    tasks: Tasks,
    events: "asyncio.Queue[Event]",
    agents: Agents | None,
) -> Verifier | None:
    """What checks ``tasks`` (in the agents' tree, if any); none without
    ``verification``."""
    if verification is None:
        return None
    return Verifier(verification, tasks, events.put, agents.tree if agents else None)


def _usage_monitor(
    usage: UsageSource | None, events: "asyncio.Queue[Event]"
) -> UsageMonitor | None:
    """What fetches the usage limits from ``usage`` into ``events``; none without a source."""
    return UsageMonitor(usage, events.put) if usage else None


def _reviewed(agents: Agents | None, review: ProposalReview) -> Agents | None:
    """``agents``, with proposals going to ``review`` unless they already go somewhere."""
    if agents is None or agents.proposal_review is not None:
        return agents
    return replace(agents, proposal_review=review)


class AgentSession:
    """Takes commands, runs work in the background and publishes its events.

    The session owns the task board and reports it whole on every change. It starts two
    lanes: the planner's, whose turns answer prompts, and the work lane's, which runs the
    tasks. ``WorkLane`` holds the rules of a task run: verification, fixes and reviews.

    With ``check_at_startup``, the ``verification`` steps also run once in the background as
    the events start, on the base commit; a failure adds a draft task to fix it. With a
    ``usage`` source, the subscription's usage limits are fetched in the background too.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        model_choices: Sequence[str] = (),
        verification: Verification | None = None,
        agents: Agents | None = None,
        workspace: str | None = None,
        check_at_startup: bool = False,
        *,
        usage: UsageSource | None = None,
    ) -> None:
        self._settings = settings or Settings()
        self._model_choices = tuple(model_choices)
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._tasks = Tasks(TaskBoard(), self._events.put)
        self._board = self._tasks.board
        self._proposals = FrontendReview(self._events.put, self._agent_on)
        agents = _reviewed(agents, self._proposals)
        self._verifier = _verifier(verification, self._tasks, self._events, agents)
        self._desk = AgentDesk(agents, self._events.put) if agents else None
        self._announced = False
        self._closing = False
        self._lane = self._review_lane()
        self._held = False  # The user cancelled: don't start work by itself.
        self._planner = self._planner_chat()
        self._events.put_nowait(self._settings_changed())
        if workspace is not None:
            self._events.put_nowait(Notice(message=f"Workspace: {workspace}"))
        self._announce_steps()
        self._startup = self._startup_check(verification, check_at_startup)
        self._work = self._work_lane()
        post = self._events.put_nowait
        self._work_turns = TurnSlot("work", "Tasks are already running.", post)
        self._planner_turns = TurnSlot("planner", "The planner is already answering.", post)
        self._usage = _usage_monitor(usage, self._events)

    def _work_lane(self) -> WorkLane | None:
        if self._desk is None:
            return None
        lane = WorkLane(self._desk, self._tasks, self._verifier, self._lane)
        lane.startup = self._startup
        return lane

    def _announce_steps(self) -> None:
        """Show the steps that will verify tasks, or note that none will."""
        if self._verifier and self._verifier.step_names:
            steps = self._verifier.progress.steps
            self._events.put_nowait(VerificationChanged(steps=steps))
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
            settings = self._desk.agents.roles[CODER].settings
        self._board.add_draft(TaskSpec(prompt=startup_fix_prompt(step, output), settings=settings))
        await self._tasks.publish()

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
                self._planner_turns.cancel()
            case CancelTurn():
                self._hold_work()
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
            await self._planner_turns.start(self._planner.answer(text, self._settings))

    async def _handle_tasks(self, command: AddTask | RunTasks | ConfigureChecks) -> None:
        match command:
            case AddTask(task=task):
                self._board.add(task)
                await self._tasks.publish()
            case RunTasks():
                await self._run_tasks_if_possible()
            case ConfigureChecks():
                await self._configure_checks()
            case _:
                assert_never(command)

    async def _configure_checks(self) -> None:
        """Queue a configurator first and run it, unless work is running already (then it goes
        next); refused without verification or while one is queued or running."""
        if self._desk is None or self._work is None:
            await self._events.put(ErrorOccurred(message=NO_AGENTS_MESSAGE))
            return
        if self._verifier is None:
            await self._events.put(ErrorOccurred(message=NO_VERIFY_MESSAGE))
            return
        if self._configuring():
            await self._events.put(Notice(message=CONFIGURING_NOTE))
            return
        self._board.add_first(self._configurator_task(self._desk.agents))
        await self._tasks.publish()
        if not self._work_turns.running:
            await self._start_work(asked=True)

    def _configuring(self) -> bool:
        """Whether a configurator task is queued or running."""
        return any(t.task.role == CONFIGURATOR and t.status in WAITING for t in self._board.tasks)

    def _configurator_task(self, agents: Agents) -> TaskSpec:
        """A configurator's task, told the steps and setup configured now."""
        steps = self.verification.steps if self.verification else ()
        prompt = configure_prompt(steps, agents.tree.setup if agents.tree else None)
        return role_task(CONFIGURATOR, agents.roles[CONFIGURATOR], prompt)

    async def _handle_board(self, command: EditTask | AcceptTask | RemoveTask) -> None:
        match command:
            case EditTask(task_id=task_id, task=task):
                await self._edit(task_id, task)
            case AcceptTask(task_id=task_id):
                await self._change(self._board.accept, task_id)
            case RemoveTask(task_id=task_id):
                await self._remove(task_id)
            case _:
                assert_never(command)

    async def _edit(self, task_id: str, task: TaskSpec) -> None:
        """Edit a task, unless it's a review orbit queued: its batch is spent, so it couldn't
        run again. An edited task in a review batch has to finish again before the batch does."""
        if self._lane is not None and self._lane.queued_by_orbit(task_id):
            message = "Reviews can't be edited; remove it instead."
            await self._events.put(ErrorOccurred(message=message))
            return
        if await self._change(self._board.edit, task_id, task) and self._lane is not None:
            self._lane.edited(task_id)

    async def _remove(self, task_id: str) -> None:
        """Remove a task, and take it out of its review batch, if any."""
        state = self._board.find(task_id)
        if await self._change(self._board.remove, task_id) and self._lane is not None:
            assert state is not None  # The board removed it, so it was there.
            await self._lane.removed(task_id, state)

    async def _run_tasks_if_possible(self) -> None:
        if self._work is None:
            await self._events.put(ErrorOccurred(message=NO_AGENTS_MESSAGE))
            return
        if self._lane is not None:
            self._lane.kick()
        await self._start_work(asked=True)

    async def _start_work(self, *, asked: bool) -> None:
        """Start the work lane's turn if there is work. The user ``asked`` lifts a hold, and a
        run already running refuses them; otherwise a hold or a running run keeps it waiting."""
        assert self._work is not None  # Work needs agents.
        if asked:
            self._held = False
        elif self._held or self._work_turns.running:
            return
        if self._work.next_work() is not None:
            await self._work_turns.start(self._work.run())

    def _hold_work(self) -> None:
        """Cancel the work lane's run, and hold it until the user asks for work again."""
        self._held = True
        self._work_turns.cancel()

    async def _handle_agents(
        self, command: RequestSuperReview | CancelAgent | DecideProposal
    ) -> None:
        match command:
            case CancelAgent(agent_id=agent_id):
                await self._cancel_agent(agent_id)
            case DecideProposal():
                await self._decide(command)
            case RequestSuperReview():
                await self._super_review()
            case _:
                assert_never(command)

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
        like ctrl+s."""
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
        elif agent.role == PLANNER:
            self._planner_turns.cancel()
        else:
            self._hold_work()

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

    async def _wake_work(self) -> None:
        """Run the work a review queued, unless something runs or the user cancelled."""
        await self._start_work(asked=False)

    def _review_lane(self) -> ReviewLane | None:
        desk = self._desk
        if desk is None or not desk.agents.review or desk.agents.tree is None:
            return None
        planner = ReviewPlanner(desk.agents.review_limits)
        lane = ReviewLane(desk, self._tasks, planner, self._wake_work)
        desk.suggest = lane.suggest
        return lane

    async def _change(self, change: Callable[..., None], *arguments: Any) -> bool:
        """Change the board, publishing it, or report why the change was refused; returns
        whether it was changed."""
        try:
            change(*arguments)
        except ValueError as exc:
            await self._events.put(ErrorOccurred(message=str(exc)))
            return False
        await self._tasks.publish()
        return True

    def _planner_chat(self) -> PlannerChat | None:
        if self._desk is None or PLANNER not in self._desk.agents.roles:
            return None
        return PlannerChat(self._desk, self._tasks, self._step_names)

    def _step_names(self) -> tuple[str, ...]:
        """The names of the steps verifying tasks now; none without verification."""
        return self._verifier.step_names if self._verifier else ()

    async def close(self) -> None:
        """Cancel the work in progress, which stops its agent, then stop the startup check, the
        usage monitor, the review lane, the planner and the agents' host, in that order. Each is
        stopped even if one before it fails; the first failure is raised after."""
        self._closing = True
        failure: Exception | None = None
        for stop in self._stops():
            try:
                await stop()
            except Exception as exc:  # A bug; the rest must still stop.
                failure = failure or exc
        if failure is not None:
            raise failure

    def _stops(self) -> list[Callable[[], Awaitable[None]]]:
        """What :meth:`close` awaits, in order."""
        parts = (self._startup, self._usage, self._lane, self._planner)
        stops: list[Callable[[], Awaitable[None]]] = [self._stop_turns]
        stops += [part.close for part in parts if part is not None]
        if self._desk is not None:
            stops.append(self._desk.stop)
        return stops

    async def _stop_turns(self) -> None:
        """Cancel the work lane's and the planner's turns, and wait for them to end."""
        for slot in (self._work_turns, self._planner_turns):
            await slot.close()

    async def events(self) -> AsyncIterator[Event]:
        if not self._announced:
            self._announced = True
            try:
                await self._announce_leftovers()
            finally:  # A listener cancelled meanwhile mustn't keep the background from starting.
                self._start_background()
        while True:
            event = await self._events.get()
            self._nudge_usage(event)
            yield event

    def _start_background(self) -> None:
        """Start the startup check and the usage monitor, if there are any and the session isn't
        closing."""
        if self._closing:
            return
        if self._startup is not None:
            self._startup.start()
        if self._usage is not None:
            self._usage.start()

    def _nudge_usage(self, event: Event) -> None:
        """Have the usage monitor fetch again, if due, once ``event`` ends an agent's turn."""
        if self._usage is not None and isinstance(event, TurnFinished) and event.agent_id:
            self._usage.nudge()

    async def _announce_leftovers(self) -> None:
        """Tell the user about work earlier runs left in the coder's tree."""
        tree = self._desk.agents.tree if self._desk else None
        for note in await tree.leftovers() if tree else []:
            await self._events.put(Notice(message=note))

    def _settings_changed(self) -> SettingsChanged:
        return SettingsChanged(settings=self._settings, model_choices=self._model_choices)
