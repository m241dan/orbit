"""In-process implementation of :class:`orbit.protocol.Session`: the application's core.

It takes commands from the frontend, asks the domain what follows from them, drives the
ports to do it, and reports what happened as events.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from contextlib import suppress
from dataclasses import replace
from typing import Any, assert_never

from orbit.agent.application.agent_run import AgentDesk, Agents
from orbit.agent.application.planner import PlannerChat
from orbit.agent.application.ports import ProposalReview
from orbit.agent.application.proposals import FrontendReview
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.startup_check import AGENT_ID as STARTUP_AGENT
from orbit.agent.application.startup_check import StartupCheck
from orbit.agent.application.verifier import Verification, Verifier
from orbit.agent.application.work_lane import WorkLane
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.board import WAITING, TaskBoard
from orbit.agent.domain.reviews import ReviewPlanner
from orbit.agent.domain.roles import (
    CODER,
    CONFIGURATOR,
    PLANNER,
    REVIEW_ROLES,
    configure_prompt,
    role_task,
)
from orbit.agent.domain.verification import VerificationStep, startup_fix_prompt
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
    TurnEndReason,
    TurnFinished,
    UpdateSettings,
    VerificationChanged,
)

NO_STEPS_NOTE = "No verification steps configured. Press ctrl+e to set them up."
NO_AGENTS_MESSAGE = "Tasks need agents, and none are set up."
CONFIGURING_NOTE = "A configurator task is already queued or running."


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
        self._held = False  # The user cancelled: don't start work by itself.
        self._planner = self._planner_chat()
        self._events.put_nowait(self._settings_changed())
        if workspace is not None:
            self._events.put_nowait(Notice(message=f"Workspace: {workspace}"))
        self._announce_steps()
        self._startup = self._startup_check(verification, check_at_startup)
        self._work = self._work_lane()
        self._turn: asyncio.Task[object] | None = None  # The work lane's.
        self._planner_turn: asyncio.Task[object] | None = None

    def _work_lane(self) -> WorkLane | None:
        if self._desk is None:
            return None
        lane = WorkLane(self._desk, self._board, self._verifier, self._lane)
        lane.startup = self._startup
        return lane

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
            settings = self._desk.agents.roles[CODER].settings
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
        if self._desk is None or self._work is None:
            await self._events.put(ErrorOccurred(message=NO_AGENTS_MESSAGE))
            return
        if self._configuring():
            await self._events.put(Notice(message=CONFIGURING_NOTE))
            return
        self._board.add_first(self._configurator_task(self._desk.agents))
        await self._publish_tasks()
        if not _running(self._turn):
            self._held = False
            await self._start(self._work.run())

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
                await self._change(self._board.edit, task_id, task)
            case AcceptTask(task_id=task_id):
                await self._change(self._board.accept, task_id)
            case RemoveTask(task_id=task_id):
                await self._change(self._board.remove, task_id)
            case _:
                assert_never(command)

    async def _run_tasks_if_possible(self) -> None:
        if self._work is None:
            await self._events.put(ErrorOccurred(message=NO_AGENTS_MESSAGE))
            return
        self._held = False
        if self._lane is not None:
            self._lane.kick()
        if self._work.next_work() is not None:
            await self._start(self._work.run())

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

    async def _wake_work(self) -> None:
        """Run the work a review queued, unless something runs or the user cancelled."""
        assert self._work is not None  # Only reviews wake it, and they need agents.
        if not (_running(self._turn) or self._held) and self._work.next_work() is not None:
            await self._start(self._work.run())

    def _review_lane(self) -> ReviewLane | None:
        desk = self._desk
        if desk is None or not desk.agents.review or desk.agents.tree is None:
            return None
        planner = ReviewPlanner(desk.agents.review_limits)
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
        if self._desk is None or PLANNER not in self._desk.agents.roles:
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
            await self._planner.close()
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
        turn = await self._launch(work, self._turn, "work", "Tasks are already running.")
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
